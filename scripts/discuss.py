#!/usr/bin/env python3
"""Discussion handler — @oh triggered, LLM searches + browses + replies in Chinese.

Tools available to the LLM via terminal:
  - Tavily search: curl -sS "https://api.tavily.com/search" -H "Content-Type: application/json" -d '{"api_key":"KEY","query":"QUERY","max_results":5}'
  - Obscura browse: obscura fetch <url> --dump text
  - Obscura HTML: obscura fetch <url> --dump html
  - Playwright screenshot: npx playwright screenshot <url> <output.png> --full-page
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


def playwright_screenshot(url: str, output_path: str = "screenshots") -> str:
    """Take a screenshot of a URL using Playwright."""
    import os
    os.makedirs(output_path, exist_ok=True)
    # Sanitize URL to filename
    safe_name = url.replace("https://", "").replace("http://", "").replace("/", "_").replace(":", "_")
    screenshot_file = os.path.join(output_path, f"{safe_name}.png")

    try:
        result = subprocess.run(
            ["npx", "playwright", "screenshot", url, screenshot_file,
             "--full-page", "--wait-for-timeout", "3000"],
            capture_output=True, text=True, timeout=60,
        )
        if os.path.exists(screenshot_file):
            size = os.path.getsize(screenshot_file)
            print(f"Screenshot saved: {screenshot_file} ({size} bytes)")
            return f"Screenshot saved to {screenshot_file} ({size} bytes)"
        else:
            return f"Playwright screenshot failed: {result.stderr[:500]}"
    except subprocess.TimeoutExpired:
        return f"Playwright timed out for {url}"
    except Exception as e:
        return f"Playwright error: {e}"


def extract_llm_response(raw_output: str) -> str:
    """Extract the LLM's final response from OpenHands SDK output.

    OpenHands SDK output structure:
      [downloads/install logs]
      [discussion.py debug prints]
      [System Prompt: ...]  ← skip
      [LLM Thought/Action iterations]
      [LLM final message]   ← THIS is what we want
      Finish with message: ...
      [Tokens: ...]
      [OpenHands banner]

    Strategy: Find 'Finish with message:' and walk backwards to find
    the start of the LLM response (after the last system prompt section).
    """
    lines = raw_output.split("\n")

    # Find the "Finish with message:" line — LLM response is before it
    finish_idx = -1
    for i, line in enumerate(lines):
        if "Finish with message:" in line:
            finish_idx = i
            break

    if finish_idx < 0:
        # Fallback: look for the AI disclosure marker
        for i, line in enumerate(lines):
            if "此回复由 AI agent" in line or "generated by an AI" in line:
                finish_idx = i
                break

    if finish_idx < 0:
        # Last resort: use last 200 lines
        finish_idx = len(lines)

    # Now walk backwards from finish_idx to find where LLM response starts
    # The LLM response starts after the last system prompt / security section
    skip_markers = [
        "System Prompt", "<SOUL>", "<ROLE>", "<MEMORY>", "<EFFICIENCY>",
        "<SECURITY>", "<SECURITY_RISK_ASSESSMENT>", "<EXTERNAL_SERVICES>",
        "OK to do without", "Do only with", "Never Do",
        "General Security Guidelines", "Repository Context Supply Chain",
        "</SECURITY>", "</SECURITY_RISK_ASSESSMENT>", "</EXTERNAL_SERVICES>",
        "UserWarning", "warnings.warn",
    ]

    # Find the last skip marker before finish_idx
    start_idx = 0
    for i in range(finish_idx - 1, -1, -1):
        if any(m in lines[i] for m in skip_markers):
            start_idx = i + 1
            break

    # Extract candidate lines
    candidate = lines[start_idx:finish_idx]

    # Filter out obvious noise lines
    noise_patterns = [
        "openhands.sdk", "openhands.tools", "site-packages",
        "Cost calculation", "import os", "from openhands",
        "conversation.", "__main__", "levelname", "python -c",
        "uv run", "Traceback", 'File "', "raise child_exception",
        "exit code", "Downloading", "Downloaded", "Installed",
        "Discussion:", "Category:", "Comments:", "Searching for:",
        "Search results:", "Browsing:", "Content:", "Screenshot:",
        "Screenshot result:", "Screenshot saved:", "Sending to LLM",
        "Reply posted", "asctime",
    ]

    response_lines = []
    for line in candidate:
        stripped = line.strip()
        if not stripped:
            if response_lines:  # Keep internal blank lines
                response_lines.append(line)
            continue
        if any(n in stripped for n in noise_patterns):
            continue
        response_lines.append(line)

    # Remove trailing blank lines
    while response_lines and not response_lines[-1].strip():
        response_lines.pop()

    if response_lines:
        result = "\n".join(response_lines).strip()
        if len(result) > 5000:
            result = result[:5000] + "\n\n... (已截断)"
        return result

    return "（LLM 回复提取失败，请查看 workflow 日志获取完整输出）"


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
    screenshot_results = ""
    if enable_browsing:
        import re
        # Prioritize URLs from the latest @oh comment, then discussion body
        all_text = user_question + " " + body + " " + comment_history
        urls = re.findall(r'https?://[^\s<>"\')\]]+', all_text)
        # Deduplicate while preserving order
        seen = set()
        urls = [u for u in urls if not (u in seen or seen.add(u))]

        for url in urls[:5]:  # Limit to 5 URLs
            print(f"Browsing: {url}")
            content = obscura_fetch(url, "text")
            browse_results += f"\n\n## Browsed: {url}\n{content}\n"
            print(f"Content: {content[:200]}...")

            # Take screenshot
            print(f"Screenshot: {url}")
            shot_result = playwright_screenshot(url)
            screenshot_results += f"- {url}: {shot_result}\n"
            print(f"Screenshot result: {shot_result}")

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

    # Step 4: Call LLM via temp file (avoids "Argument list too long")
    # The LLM script writes the final response to a separate file
    import tempfile, json
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({
            "model": llm_model,
            "base_url": llm_base_url,
            "api_key": llm_api_key,
            "prompt": prompt,
        }, f, ensure_ascii=False)
        config_path = f.name

    response_path = config_path + ".response"

    llm_script = f"""
import json, sys
from openhands.sdk import LLM, Agent, AgentContext, Conversation
from openhands.sdk.tool import Tool
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.terminal import TerminalTool

with open(sys.argv[1]) as f:
    cfg = json.load(f)

llm = LLM(model=cfg["model"], base_url=cfg["base_url"], api_key=cfg["api_key"])
tools = [Tool(name=TerminalTool.name), Tool(name=FileEditorTool.name)]
agent = Agent(llm=llm, tools=tools)
conversation = Conversation(agent=agent)
conversation.send_message(cfg["prompt"])
conversation.run()

# Extract the last assistant message from the conversation
response_text = ""
try:
    messages = conversation.get_messages()
    for msg in reversed(messages):
        if hasattr(msg, 'role') and msg.role == 'assistant':
            response_text = msg.content if hasattr(msg, 'content') else str(msg)
            break
except Exception:
    pass

# Fallback: use the conversation's last event
if not response_text:
    try:
        events = conversation.get_events()
        for evt in reversed(events):
            if hasattr(evt, 'message') and hasattr(evt.message, 'content'):
                response_text = evt.message.content
                break
    except Exception:
        pass

with open(sys.argv[2], 'w') as f:
    f.write(response_text or "(LLM returned empty response)")
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(llm_script)
        script_path = f.name

    result = subprocess.run(
        ["uv", "run", "--no-project",
         "--with", "openhands-sdk",
         "--with", "openhands-tools",
         "python", script_path, config_path, response_path],
        capture_output=True, text=True,
        env={**os.environ},
        cwd=os.getcwd(),
    )

    # Read LLM response from file
    try:
        with open(response_path) as f:
            llm_response = f.read().strip()
    except FileNotFoundError:
        llm_response = ""

    if not llm_response:
        # Fallback: extract from stdout
        raw_output = result.stdout + "\n" + result.stderr
        print(raw_output)
        llm_response = extract_llm_response(raw_output)
    else:
        print(f"LLM response extracted ({len(llm_response)} chars)")

    os.unlink(config_path)
    os.unlink(script_path)
    try:
        os.unlink(response_path)
    except FileNotFoundError:
        pass

    # Step 5: Build reply with search and browse evidence
    reply_parts = ["## 技术方案建议\n"]
    reply_parts.append(llm_response)
    reply_parts.append("\n---\n### 搜索证据\n")
    reply_parts.append(f"**搜索关键词**: {search_query}\n")
    reply_parts.append(f"**搜索结果**:\n{search_results[:1000]}\n")
    if browse_results:
        reply_parts.append(f"\n### 网站浏览结果\n{browse_results[:2000]}\n")
    if screenshot_results:
        reply_parts.append(f"\n### 截图\n{screenshot_results}\n")
        reply_parts.append("截图已上传为 GitHub Actions artifact，可在 workflow run 页面下载。\n")
    reply_parts.append("\n🤖 由 GLM-5.2 生成 | Tavily 搜索 + Obscura 浏览 + Playwright 截图")

    reply_body = "\n".join(reply_parts)
    reply_discussion(token, discussion_node_id, reply_body)
    print("Reply posted to discussion")


if __name__ == "__main__":
    main()
