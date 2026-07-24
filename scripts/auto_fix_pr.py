#!/usr/bin/env python3
"""
PR Auto-Fix — reads review feedback and fixes code automatically.

Triggered when review-ai posts CHANGES_REQUESTED.
Agent reads the review body, fixes the issues, pushes to the PR branch.
Max 3 iterations (enforced by the workflow).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

from templates import get_template


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


def main():
    print("=" * 60)
    print("PR Auto-Fix (OpenHands SDK + LocalWorkspace)")
    print("=" * 60)

    api_key = get_env("LLM_API_KEY")
    model = get_env("LLM_MODEL", "openai/glm-5.2")
    base_url = get_env("LLM_BASE_URL", "https://api.modelarts-maas.com/v2")
    github_token = get_env("GITHUB_TOKEN")
    pr_number = int(get_env("PR_NUMBER"))
    repo_name = get_env("REPO_NAME")
    review_body = get_env("REVIEW_BODY")
    iteration = int(get_env("ITERATION", "1"))

    print(f"Repo: {repo_name}, PR: #{pr_number}, Iteration: {iteration}")
    print(f"CWD: {os.getcwd()}")

    # Fetch PR info
    pr = gh_api("GET", f"{repo_name}/pulls/{pr_number}", github_token)
    pr_title = pr["title"]
    pr_branch = pr["head"]["ref"]
    print(f"PR: {pr_title} (branch: {pr_branch})")

    # Comment: started
    gh_api("POST", f"{repo_name}/issues/{pr_number}/comments", github_token,
           {"body": get_template("auto_fix_started", iteration=iteration)})

    # Build prompt from review feedback
    task_prompt = get_template(
        "prompt_auto_fix",
        pr_title=pr_title, repo_name=repo_name, pr_branch=pr_branch, review_body=review_body,
    )

    # Create agent
    from openhands.sdk import LLM, Agent, Conversation, get_logger
    from openhands.sdk.workspace import LocalWorkspace
    from openhands.tools.preset.default import get_default_condenser, get_default_tools

    logger = get_logger(__name__)
    logger.info("Creating OpenHands agent for auto-fix...")

    llm_config = {
        "model": model,
        "api_key": api_key,
        "usage_id": "auto_fix_pr",
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
        gh_api("POST", f"{repo_name}/issues/{pr_number}/comments", github_token,
               {"body": get_template("auto_fix_error", error=e)})
        sys.exit(1)

    # Check if agent made changes
    status_after = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True
    ).stdout.strip()

    if not status_after:
        print("No changes detected from auto-fix")
        gh_api("POST", f"{repo_name}/issues/{pr_number}/comments", github_token,
               {"body": get_template("auto_fix_no_changes")})
        sys.exit(0)

    print(f"Changes detected:\n{status_after}")

    # Commit and push
    subprocess.run(["git", "add", "-A"], check=True)
    commit_msg = get_template("auto_fix_commit", iteration=iteration)
    subprocess.run(["git", "commit", "-m", commit_msg], check=True)

    push_url = f"https://x-access-token:{github_token}@github.com/{repo_name}.git"
    subprocess.run(["git", "push", push_url], check=True)

    # Get commit SHA
    commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()[:12]

    gh_api("POST", f"{repo_name}/issues/{pr_number}/comments", github_token,
           {"body": get_template("auto_fix_pushed", commit_sha=commit_sha)})

    print(f"\n✅ Done! Pushed {commit_sha} to {pr_branch}")


if __name__ == "__main__":
    main()
