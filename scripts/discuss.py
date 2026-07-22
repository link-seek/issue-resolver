#!/usr/bin/env python3
"""Discussion handler — @oh triggered, LLM searches + browses + replies in Chinese."""

import json
import os
import subprocess
import sys
import tempfile
import urllib.request


def gh_graphql(token: str, query: str, variables: dict = None) -> dict:
    url = "https://api.github.com/graphql"
    body = json.dumps({"query": query, "variables": variables or {}})
    req = urllib.request.Request(url, data=body.encode(), headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def get_discussion(token: str, node_id: str) -> dict:
    query = """
    query($id: ID!) {
      node(id: $id) {
        ... on Discussion {
          title
          body
          category { name }
          comments(first: 50) {
            nodes {
              body
              author { login }
            }
          }
        }
      }
    }
    """
    result = gh_graphql(token, query, {"id": node_id})
    return result.get("data", {}).get("node", {})


def reply_discussion(token: str, discussion_node_id: str, body: str):
    query = """
    mutation($input: AddDiscussionCommentInput!) {
      addDiscussionComment(input: $input) {
        comment { id }
      }
    }
    """
    variables = {
        "input": {
            "discussionId": discussion_node_id,
            "body": body,
        }
    }
    return gh_graphql(token, query, variables)


def get_file_tree(max_depth: int = 3) -> str:
    """Get a file tree of the current directory, excluding noise."""
    try:
        result = subprocess.run(
            ["find", ".", "-type", "f",
             "-not", "-path", "./.git/*",
             "-not", "-path", "./node_modules/*",
             "-not", "-path", "./target/*",
             "-not", "-path", "./__pycache__/*",
             "-not", "-path", "./.next/*",
             "-not", "-path", "./dist/*",
             "-not", "-name", "*.pyc",
             "-not", "-name", "*.log"],
            capture_output=True, text=True, timeout=10,
        )
        files = result.stdout.strip().split("\n") if result.stdout.strip() else []
        if len(files) > 200:
            files = files[:200]
        return "\n".join(files)
    except Exception:
        return "(无法获取文件树)"


def main():
    token = os.environ.get("GITHUB_TOKEN", "")
    discussion_node_id = os.environ.get("DISCUSSION_NODE_ID", "")
    repo_name = os.environ.get("REPO_NAME", "")
    llm_model = os.environ.get("LLM_MODEL", "openai/glm-5.2")
    llm_base_url = os.environ.get("LLM_BASE_URL", "https://api.modelarts-maas.com/v2")
    llm_api_key = os.environ.get("LLM_API_KEY", "")

    if not discussion_node_id:
        print("No DISCUSSION_NODE_ID set")
        sys.exit(1)

    discussion = get_discussion(token, discussion_node_id)
    title = discussion.get("title", "")
    body = discussion.get("body", "")
    category = discussion.get("category", {}).get("name", "")
    comments = discussion.get("comments", {}).get("nodes", [])

    print(f"Discussion: {title}")
    print(f"Category: {category}")
    print(f"Comments: {len(comments)}")

    comment_history = "\n\n".join([
        f"**{c['author']['login']}**: {c['body']}" for c in comments
    ])

    file_tree = get_file_tree()
    print(f"File tree: {len(file_tree.split(chr(10)))} files")

    prompt = f"""你是一个技术架构师。请分析以下讨论内容，结合仓库实际代码，给出技术方案建议。

## 仓库信息
- 仓库: {repo_name}
- 当前工作目录包含完整代码，你可以使用 FileEditor 工具查看文件内容，使用 Terminal 工具运行命令

## 仓库文件结构
```
{file_tree}
```

## 讨论标题
{title}

## 讨论分类
{category}

## 讨论内容
{body}

## 已有评论
{comment_history}

## 要求
1. 请用简体中文回复
2. **先阅读相关代码文件**：使用 FileEditor 工具查看与讨论相关的源代码文件，理解现有实现
3. 分析需求的技术可行性，基于实际代码给出判断
4. 给出实现方案建议，包括：
   - 涉及哪些文件/模块（给出具体文件路径）
   - 大致的改动方向（引用现有代码结构）
   - 推荐的技术方案
   - 潜在风险和注意事项
5. 如果需求不够明确，提出需要澄清的问题
6. 不要直接修改代码，只给出方案建议"""

    print("Sending to LLM...")

    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write(prompt)
        prompt_file = f.name

    agent_script = """import os, sys, io, re, json

# Capture stdout to extract LLM response
captured = io.StringIO()
old_stdout = sys.stdout
sys.stdout = captured

from openhands.sdk import LLM, Agent, AgentContext, Conversation
from openhands.sdk.tool import Tool
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.terminal import TerminalTool

llm = LLM(
    model=os.environ["LLM_MODEL"],
    base_url=os.environ["LLM_BASE_URL"],
    api_key=os.environ["LLM_API_KEY"],
)

tools = [
    Tool(name=TerminalTool.name),
    Tool(name=FileEditorTool.name),
]

agent = Agent(llm=llm, tools=tools)
conversation = Conversation(agent=agent)

with open(os.environ["PROMPT_FILE"]) as f:
    prompt = f.read()

conversation.send_message(prompt)
conversation.run()

sys.stdout = old_stdout
raw = captured.getvalue()

# Debug: inspect conversation object
debug_info = []
debug_info.append(f"conversation type: {type(conversation)}")
debug_info.append(f"conversation dir: {[x for x in dir(conversation) if not x.startswith('__')]}")

# Try various ways to get the response
response = ""

# Method 1: conversation.messages
for attr in ['messages', '_messages', 'history', '_history', 'events', '_events', 'turns', '_turns']:
    try:
        val = getattr(conversation, attr)
        if val:
            debug_info.append(f"conversation.{attr} = {type(val)} len={len(val) if hasattr(val, '__len__') else 'N/A'}")
            # Try to get last message
            if isinstance(val, list) and len(val) > 0:
                last = val[-1]
                debug_info.append(f"  last item type: {type(last)}, dir: {[x for x in dir(last) if not x.startswith('_')]}")
                for msg_attr in ['content', 'text', 'message', 'response', 'output', 'data']:
                    try:
                        msg_val = getattr(last, msg_attr)
                        if msg_val and isinstance(msg_val, str) and len(msg_val) > 10:
                            response = msg_val
                            debug_info.append(f"  FOUND response via .{msg_attr}: {len(response)} chars")
                            break
                    except:
                        pass
    except:
        pass

# Method 2: agent attributes
if not response:
    for attr in ['last_response', 'response', '_response', 'messages', '_messages']:
        try:
            val = getattr(agent, attr)
            if val and isinstance(val, str) and len(val) > 10:
                response = val
                debug_info.append(f"FOUND response via agent.{attr}: {len(response)} chars")
                break
        except:
            pass

# Method 3: parse raw stdout for the response
if not response:
    # Look for the last substantial text block after all tool calls
    # The LLM response typically comes after tool results
    lines = raw.split('\\n')
    # Find lines that look like a response (not system prompt, not tool output)
    response_lines = []
    in_response = False
    for i, line in enumerate(lines):
        # Skip system prompt markers
        if any(m in line for m in ['System Prompt', '<SOUL>', '<ROLE>', '<MEMORY>', '<EFFICIENCY>']):
            continue
        # Look for response indicators
        if re.search(r'[\\u4e00-\\u9fff]{3,}', line) and not line.startswith('*') and not line.startswith(' '):
            in_response = True
        if in_response:
            response_lines.append(line)

    if response_lines:
        response = '\\n'.join(response_lines[-100:])

# Fallback: use last 3000 chars of raw output
if not response:
    response = raw[-3000:] if len(raw) > 3000 else raw

# Truncate
if len(response) > 8000:
    response = response[:8000] + "\\n\\n...(内容过长已截断)"

with open(os.environ["RESPONSE_FILE"], 'w') as f:
    f.write(response)

# Write debug info to stderr
sys.stderr.write("\\n".join(debug_info) + "\\n")
"""

    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(agent_script)
        script_file = f.name

    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        response_file = f.name

    result = subprocess.run(
        ["uv", "run", "--no-project",
         "--with", "openhands-sdk",
         "--with", "openhands-tools",
         "python", script_file],
        capture_output=True, text=True,
        env={**os.environ, "PROMPT_FILE": prompt_file, "RESPONSE_FILE": response_file},
        cwd=os.getcwd(),
    )

    if result.stderr:
        print(f"[stderr] {result.stderr[:500]}", file=sys.stderr)

    try:
        with open(response_file) as f:
            llm_response = f.read().strip()
    except Exception:
        llm_response = "(LLM 未返回文本回复)"

    print(f"Response length: {len(llm_response)} chars")

    reply_body = f"## 技术方案建议\n\n{llm_response}\n\n---\n🤖 由 GLM-5.2 生成"

    try:
        result_gql = reply_discussion(token, discussion_node_id, reply_body)
        if "errors" in result_gql:
            print(f"GraphQL errors: {result_gql['errors']}")
        else:
            print("Reply posted to discussion")
    except Exception as e:
        print(f"Failed to post reply: {e}")


if __name__ == "__main__":
    main()
