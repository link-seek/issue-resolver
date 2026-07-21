#!/usr/bin/env python3
"""Discussion handler — @oh triggered, LLM searches + browses + replies in Chinese.

Tools available to the LLM via terminal:
  - Tavily search: curl -sS "https://api.tavily.com/search" -H "Content-Type: application/json" -d '{"api_key":"KEY","query":"QUERY","max_results":5}'
  - Obscura browse: obscura fetch <url> --dump text
  - Obscura HTML: obscura fetch <url> --dump html
"""

import json
import os
import subprocess
import sys
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
    gh_graphql(token, query, variables)


def tavily_search(api_key: str, query: str, max_results: int = 5) -> str:
    """Search using Tavily API and return formatted results."""
    if not api_key:
        return "Tavily API key not configured (SEARCH_API_KEY not set)"

    body = json.dumps({
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        "include_answer": True,
    }).encode()

    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)

        parts = []
        if data.get("answer"):
            parts.append(f"**AI Answer**: {data['answer']}")
        for r in data.get("results", []):
            parts.append(f"### {r.get('title', 'N/A')}\nURL: {r.get('url', 'N/A')}\n{r.get('content', 'N/A')[:500]}")
        return "\n\n".join(parts) if parts else "No results found"
    except Exception as e:
        return f"Search failed: {e}"


def obscura_fetch(url: str, mode: str = "text") -> str:
    """Fetch a URL using Obscura headless browser."""
    try:
        result = subprocess.run(
            ["obscura", "fetch", url, "--dump", mode, "--timeout", "15"],
            capture_output=True, text=True, timeout=30,
        )
        output = result.stdout.strip()
        if not output and result.stderr:
            return f"Obscura error: {result.stderr[:500]}"
        return output[:3000]  # Limit to 3000 chars
    except subprocess.TimeoutExpired:
        return f"Obscura timed out fetching {url}"
    except FileNotFoundError:
        return "Obscura not installed"
    except Exception as e:
        return f"Obscura error: {e}"


def main():
    token = os.environ.get("GITHUB_TOKEN", "")
    discussion_node_id = os.environ.get("DISCUSSION_NODE_ID", "")
    repo_name = os.environ.get("REPO_NAME", "")
    llm_model = os.environ.get("LLM_MODEL", "openai/glm-5.2")
    llm_base_url = os.environ.get("LLM_BASE_URL", "https://api.modelarts-maas.com/v2")
    llm_api_key = os.environ.get("LLM_API_KEY", "")
    search_api_key = os.environ.get("SEARCH_API_KEY", "")
    enable_browsing = os.environ.get("AGENT_ENABLE_BROWSING", "false").lower() == "true"

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

    # Find the latest @oh comment to get the user's question
    user_question = ""
    for c in reversed(comments):
        if "@oh" in c.get("body", ""):
            user_question = c["body"].replace("@oh", "").strip()
            break

    # Step 1: Search for relevant information using Tavily
    search_query = f"{title} {user_question}".strip() or title
    print(f"Searching for: {search_query}")
    search_results = tavily_search(search_api_key, search_query) if search_api_key else "No Tavily API key"
    print(f"Search results: {search_results[:200]}...")

    # Step 2: Try to fetch any URLs mentioned in the discussion
    browse_results = ""
    if enable_browsing:
        import re
        urls = re.findall(r'https?://[^\s<>"\']+', body + " " + comment_history)
        # Also try xieyucheng.top if mentioned
        if "xieyucheng.top" in (title + body + comment_history).lower():
            urls.append("https://xieyucheng.top")

        for url in urls[:3]:  # Limit to 3 URLs
            print(f"Browsing: {url}")
            content = obscura_fetch(url, "text")
            browse_results += f"\n\n## Browsed: {url}\n{content}\n"
            print(f"Content: {content[:200]}...")

    # Step 3: Build prompt with search and browse results
    prompt = f"""你是一个技术架构师。用户在 GitHub Discussion 中提问，请分析并回复。

## 讨论标题
{title}

## 讨论内容
{body}

## 用户问题
{user_question or "（见讨论内容）"}

## 已有评论
{comment_history}

## 搜索结果（Tavily）
{search_results}

## 网站浏览结果（Obscura）
{browse_results if browse_results else "（无 URL 需要浏览）"}

## 要求
1. 请用简体中文回复
2. 基于搜索结果和浏览内容给出技术方案建议
3. 如果搜索到了相关信息，引用来源
4. 如果浏览了网站，总结网站内容
5. 给出实现方案建议，包括：
   - 涉及哪些文件/模块
   - 大致的改动方向
   - 推荐的技术方案
   - 潜在风险和注意事项
6. 如果需求不够明确，提出需要澄清的问题
7. 不要直接修改代码，只给出方案建议"""

    print("Sending to LLM...")

    # Step 4: Call LLM
    result = subprocess.run(
        ["uv", "run", "--no-project",
         "--with", "openhands-sdk",
         "--with", "openhands-tools",
         "python", "-c", f"""
import os, sys
from openhands.sdk import LLM, Agent, AgentContext, Conversation
from openhands.sdk.tool import Tool
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.terminal import TerminalTool

llm = LLM(
    model="{llm_model}",
    base_url="{llm_base_url}",
    api_key="{llm_api_key}",
)

tools = [
    Tool(name=TerminalTool.name),
    Tool(name=FileEditorTool.name),
]

agent = Agent(llm=llm, tools=tools)
conversation = Conversation(agent=agent)
conversation.send_message('''{prompt}''')
conversation.run()
"""],
        capture_output=True, text=True,
        env={**os.environ},
        cwd=os.getcwd(),
    )

    output = result.stdout + "\n" + result.stderr
    print(output)

    # Step 5: Build reply with search and browse evidence
    reply_parts = ["## 技术方案建议\n"]
    reply_parts.append(output)
    reply_parts.append("\n---\n### 搜索证据\n")
    reply_parts.append(f"**搜索关键词**: {search_query}\n")
    reply_parts.append(f"**搜索结果**:\n{search_results[:1000]}\n")
    if browse_results:
        reply_parts.append(f"\n### 网站浏览结果\n{browse_results[:2000]}\n")
    reply_parts.append("\n🤖 由 GLM-5.2 生成 | Tavily 搜索 + Obscura 浏览")

    reply_body = "\n".join(reply_parts)
    reply_discussion(token, discussion_node_id, reply_body)
    print("Reply posted to discussion")


if __name__ == "__main__":
    main()
