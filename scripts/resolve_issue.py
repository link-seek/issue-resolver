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

    # Check if user confirmed the plan in comments
    user_confirmed = any(
        c["body"].strip().lower() in ("confirm", "确认", "approved", "ok", "yes", "y")
        for c in comments
    )
    bot_posted_plan = any("Implementation Plan" in c.get("body", "") for c in comments)

    if not bot_posted_plan:
        # Phase 1: Agent assesses confidence and either plans or implements directly
        task_prompt = f"""You are a software engineer working on the repository: {repo_name}

## Issue to Resolve
**Title**: {title}
**Description**:
{body}
{comments_text}

## Workflow
1. Explore the codebase to understand the project structure
2. Assess your confidence: Are you 100% sure what to do and how to do it?

### If 100% confident (simple, clear issue):
- Directly implement the fix
- Run tests to verify
- Self-review: run `git diff` to review all your changes. Check for:
  - Error handling and edge cases
  - Security issues (injection, secrets exposure)
  - Unused imports or variables
  - Missing pagination or bounds checking
- Fix any issues found, then re-run tests
- Do NOT post a plan, just do it

### If NOT 100% confident (complex, ambiguous, needs design):
- Output your plan and questions in this format:

📋 Plan:
1. [step 1]
2. [step 2]
...

❓ Questions:
- [question 1]
- [question 2]

- Do NOT write any code yet
- I will ask the user to confirm

## Important
- **DO NOT run any git commands** — just create/modify files directly
- Make minimal, focused changes
- Follow existing code conventions
"""
    elif not user_confirmed:
        print("Plan posted, waiting for user confirmation")
        gh_api("POST", f"{repo_name}/issues/{issue_number}/comments", github_token,
               {"body": "⏳ Waiting for confirmation. Reply with 'confirm' or '确认' to proceed."})
        sys.exit(0)
    else:
        # Phase 2: User confirmed, implement
        task_prompt = f"""You are a software engineer working on the repository: {repo_name}

## Issue to Resolve
**Title**: {title}
**Description**:
{body}
{comments_text}

## Your Task
The user has confirmed the plan. Now implement it:
1. Make the necessary code changes
2. Run tests to verify
3. If tests fail, fix and iterate
4. Self-review: run `git diff` to review all your changes. Check for:
   - Error handling and edge cases
   - Security issues (injection, secrets exposure)
   - Unused imports or variables
   - Naming conventions consistency
   - Missing pagination or bounds checking
5. Fix any issues found in self-review
6. Re-run tests to confirm everything still passes

## Important
- **DO NOT run any git commands** — just create/modify files directly
- Make minimal, focused changes
- Follow existing code conventions

Start implementing now.
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

    # If this was the planning phase, check if agent wrote code or just planned
    if not bot_posted_plan:
        # Check if agent made any code changes
        status_after_plan = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True
        ).stdout.strip()
        
        if not status_after_plan:
            # No code changes = agent posted a plan, waiting for confirmation
            from openhands.sdk.conversation.response_utils import get_agent_final_response
            try:
                plan_response = get_agent_final_response(conversation)
            except Exception:
                plan_response = "Plan generated. Please review the workflow logs."
            
            gh_api("POST", f"{repo_name}/issues/{issue_number}/comments", github_token,
                   {"body": f"📋 **Implementation Plan**\n\n{plan_response}\n\n---\nReply with `confirm` or `确认` to proceed, or provide feedback."})
            print("Plan posted to issue, waiting for confirmation")
            sys.exit(0)
        else:
            print("Agent was confident, implemented directly (no plan needed)")

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
