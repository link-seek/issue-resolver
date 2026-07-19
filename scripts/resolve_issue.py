#!/usr/bin/env python3
"""
Issue Resolver — OpenHands SDK + LocalWorkspace.

Agent runs directly on the runner filesystem (no sandbox).
Agent can multi-turn iterate: explore → edit → test → fix errors.
Script handles all git operations after agent finishes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path


def get_env(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None:
        raise ValueError(f"{name} environment variable is required")
    return v


def gh_api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    url = f"https://api.github.com/repos/{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def run_tests() -> bool:
    if Path("Cargo.toml").exists():
        r = subprocess.run(["cargo", "test", "--", "--nocapture"],
                         capture_output=True, text=True, timeout=600)
        print(f"cargo test exit: {r.returncode}")
        if r.returncode != 0:
            print(f"stderr: {r.stderr[:500]}")
        return r.returncode == 0
    if Path("package.json").exists():
        r = subprocess.run(["npm", "test", "--", "--passWithNoTests"],
                         capture_output=True, text=True, timeout=120)
        return r.returncode == 0
    return True


def main():
    print("=" * 60)
    print("Issue Resolver (OpenHands SDK + LocalWorkspace)")
    print("=" * 60)

    api_key = get_env("LLM_API_KEY")
    model = get_env("LLM_MODEL", "openai/glm-5.2")
    base_url = get_env("LLM_BASE_URL", "https://api.modelarts-maas.com/v2")
    github_token = get_env("GITHUB_TOKEN")
    issue_number = int(get_env("ISSUE_NUMBER"))
    issue_type = get_env("ISSUE_TYPE", "issue")
    repo_name = get_env("REPO_NAME")

    print(f"Repo: {repo_name}, Issue: #{issue_number}, Model: {model}")
    print(f"CWD: {os.getcwd()}")

    # Fetch issue
    issue = gh_api("GET", f"{repo_name}/issues/{issue_number}", github_token)
    title = issue["title"]
    body = issue.get("body", "") or "(no description)"
    print(f"Title: {title}")

    # Fetch comments
    comments = gh_api("GET", f"{repo_name}/issues/{issue_number}/comments", github_token)
    comments_text = ""
    if comments:
        comments_text = "\n\n## Additional Context from Comments:\n"
        for c in comments:
            comments_text += f"\n**{c['user']['login']}**:\n{c['body']}\n"

    # Comment: started
    gh_api("POST", f"{repo_name}/issues/{issue_number}/comments", github_token,
           {"body": "🤖 OpenHands agent started working on this issue using GLM-5.2."})

    # Record state before agent
    commit_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    print(f"Commit before: {commit_before[:12]}")
    print(f"Git status before: {subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True).stdout}")

    # Build prompt — tell agent NOT to use git
    task_prompt = f"""You are a software engineer working on the repository: {repo_name}

## Issue to Resolve
**Title**: {title}
**Description**:
{body}
{comments_text}

## Your Task
1. Explore the codebase to understand the project structure (use ls, cat, find)
2. Identify the root cause or what needs to be implemented
3. Make the necessary code changes to resolve this issue
4. Run the tests to verify your changes work
5. If tests fail, analyze the errors and fix your changes — iterate until tests pass
6. Ensure code quality and follow existing code style

## Important
- Make minimal, focused changes
- Follow existing code conventions
- Don't break existing functionality
- **DO NOT run any git commands** (git add, git commit, git checkout, git push, etc.)
- **DO NOT use git at all** — just create/modify files directly
- The CI system will handle git operations automatically

Start by exploring the project structure, then implement the fix, then run tests to verify.
"""

    # Create agent
    from openhands.sdk import LLM, Agent, Conversation, get_logger
    from openhands.sdk.workspace import LocalWorkspace
    from openhands.tools.preset.default import get_default_condenser, get_default_tools

    logger = get_logger(__name__)
    logger.info("Creating OpenHands agent with LocalWorkspace...")

    llm_config = {
        "model": model,
        "api_key": api_key,
        "usage_id": "issue_resolver",
        "drop_params": True,
    }
    if base_url:
        llm_config["base_url"] = base_url

    llm = LLM(**llm_config)

    agent = Agent(
        llm=llm,
        tools=get_default_tools(enable_browser=False),
        system_prompt_kwargs={"cli_mode": True},
        condenser=get_default_condenser(
            llm=llm.model_copy(update={"usage_id": "condenser"})
        ),
    )

    cwd = os.getcwd()
    workspace = LocalWorkspace(working_dir=cwd)
    print(f"LocalWorkspace working_dir: {workspace.working_dir}")

    secrets = {
        "LLM_API_KEY": api_key,
        "GITHUB_TOKEN": github_token,
    }

    logger.info("Starting agent conversation...")
    conversation = Conversation(
        agent=agent,
        workspace=workspace,
        secrets=secrets,
    )

    try:
        conversation.send_message(task_prompt)
        conversation.run()
        logger.info("Agent completed successfully")
    except Exception as e:
        logger.error(f"Agent failed: {type(e).__name__}: {e}")
        gh_api("POST", f"{repo_name}/issues/{issue_number}/comments", github_token,
               {"body": f"❌ Agent error: {e}"})
        sys.exit(1)

    # Check state after agent
    commit_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    status_after = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True
    ).stdout.strip()

    print(f"Commit after: {commit_after[:12]}")
    print(f"Git status after:\n{status_after}")
    print(f"HEAD changed: {commit_before != commit_after}")
    print(f"Has uncommitted: {bool(status_after)}")

    has_uncommitted = bool(status_after)
    has_new_commits = commit_before != commit_after

    if not has_new_commits and not has_uncommitted:
        print("No changes detected")
        gh_api("POST", f"{repo_name}/issues/{issue_number}/comments", github_token,
               {"body": "⚠️ Agent analyzed the issue but no code changes were made."})
        sys.exit(0)

    # Create branch
    branch = f"agent/fix-{issue_type}-{issue_number}"
    subprocess.run(["git", "checkout", "-b", branch], check=True)

    # Commit any uncommitted changes (agent should have left files changed, not committed)
    if has_uncommitted:
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-m",
                       f"Fix #{issue_number}: {title}\n\nGenerated by OpenHands agent using GLM-5.2."],
                       check=True)

    # If agent committed to main (shouldn't happen but just in case), the branch
    # already has those commits. We need to reset main — but branch is already
    # checked out so we're fine.

    # Push
    push_url = f"https://x-access-token:{github_token}@github.com/{repo_name}.git"
    subprocess.run(["git", "push", push_url, branch], check=True)

    # Run tests
    print("Running tests...")
    tests_ok = run_tests()

    # Create PR
    pr = gh_api("POST", f"{repo_name}/pulls", github_token, {
        "title": f"Fix #{issue_number}: {title}",
        "body": f"## Automated Fix\n\nAgent: OpenHands SDK + LocalWorkspace\nModel: GLM-5.2 via MAAS\n\nCloses #{issue_number}",
        "head": branch,
        "base": "main",
    })
    pr_url = pr["html_url"]
    pr_num = pr["number"]
    print(f"PR created: {pr_url}")

    # Comment on issue
    emoji = "✅" if tests_ok else "⚠️"
    gh_api("POST", f"{repo_name}/issues/{issue_number}/comments", github_token,
           {"body": f"{emoji} Agent created PR #{pr_num}: {pr_url}\n\n**Tests**: {'passed' if tests_ok else 'failed'}\n**Model**: GLM-5.2"})

    print(f"\n✅ Done! PR: {pr_url}")


if __name__ == "__main__":
    main()
