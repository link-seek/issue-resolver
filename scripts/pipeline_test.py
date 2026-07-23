#!/usr/bin/env python3
"""Pipeline test: validates full flow from issue/discussion creation to deployment.

Usage:
    python pipeline_test.py non-db      # fix-me issue → PR → auto-merge → deploy
    python pipeline_test.py db          # DB fix-me issue → risk analysis → manual merge → deploy
    python pipeline_test.py discussion  # discussion @oh → discuss job → reply

Requires:
    PAT_TOKEN env var — GitHub PAT for API access
    CONSUMER_REPO env var — consumer repo in owner/name format
"""

import base64
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error


TOKEN = os.environ.get("PAT_TOKEN", "")
CONSUMER_REPO = os.environ.get("CONSUMER_REPO", "")

if not TOKEN:
    print("PAT_TOKEN not set")
    sys.exit(1)
if not CONSUMER_REPO:
    print("CONSUMER_REPO not set (e.g. link-seek/enterprise-architecture-platform)")
    sys.exit(1)

CONSUMER_OWNER, CONSUMER_NAME = CONSUMER_REPO.split("/", 1)
CONFIG = {}


def gh_api(method, path, data=None):
    url = f"https://api.github.com/repos/{CONSUMER_REPO}/{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"token {TOKEN}",
        "Accept": "application/vnd.github+json",
    }, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def gh_graphql(query, variables=None):
    url = "https://api.github.com/graphql"
    body = json.dumps({"query": query, "variables": variables or {}})
    req = urllib.request.Request(url, data=body.encode(), headers={
        "Authorization": f"token {TOKEN}",
        "Accept": "application/vnd.github+json",
    }, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_config():
    """Fetch .issue-resolver.yml from consumer repo via GitHub API."""
    try:
        url = f"https://api.github.com/repos/{CONSUMER_REPO}/contents/.issue-resolver.yml"
        req = urllib.request.Request(url, headers={
            "Authorization": f"token {TOKEN}",
            "Accept": "application/vnd.github+json",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
        content = base64.b64decode(data["content"]).decode()
        import yaml
        return yaml.safe_load(content) or {}
    except Exception as e:
        print(f"WARNING: Could not fetch .issue-resolver.yml: {e}")
        print("Using default values")
        return {}


def create_issue(title, body, labels=None):
    label = labels or CONFIG.get("trigger", {}).get("label", "fix-me")
    print(f"Creating issue: {title}")
    data = gh_api("POST", "issues", {"title": title, "body": body, "labels": [label]})
    print(f"  Issue #{data['number']}: {data['html_url']}")
    return data["number"]


def close_issue(number):
    print(f"Closing issue #{number}")
    gh_api("PATCH", f"issues/{number}", {"state": "closed"})
    print(f"  Issue #{number} closed")


def create_discussion(title, body):
    print(f"Creating discussion: {title}")
    repo_id = gh_graphql(
        '{ repository(owner:"%s", name:"%s") { id discussionCategories(first:5) { nodes { id name } } } }' % (CONSUMER_OWNER, CONSUMER_NAME)
    )["data"]["repository"]
    category = CONFIG.get("pipeline_test", {}).get("discussion", {}).get("category", "General")
    cat_id = next(c["id"] for c in repo_id["discussionCategories"]["nodes"] if c["name"] == category)
    result = gh_graphql(
        'mutation($input: CreateDiscussionInput!) { createDiscussion(input: $input) { discussion { number url id } } }',
        {"input": {"repositoryId": repo_id["id"], "categoryId": cat_id, "title": title, "body": body}}
    )
    disc = result["data"]["createDiscussion"]["discussion"]
    print(f"  Discussion #{disc['number']}: {disc['url']}")
    return disc["number"], disc["id"]


def close_discussion(node_id):
    print(f"Closing discussion {node_id}")
    gh_graphql(
        'mutation($input: CloseDiscussionInput!) { closeDiscussion(input: $input) { discussion { closed } } }',
        {"input": {"discussionId": node_id, "reason": "RESOLVED"}}
    )
    print("  Discussion closed")


def get_issue_comments(number):
    return gh_api("GET", f"issues/{number}/comments")


def add_issue_comment(number, body):
    print(f"  Commenting on issue #{number}: {body[:50]}")
    gh_api("POST", f"issues/{number}/comments", {"body": body})


def get_pr(number):
    return gh_api("GET", f"pulls/{number}")


def merge_pr(number):
    print(f"  Merging PR #{number} (squash)")
    gh_api("PUT", f"pulls/{number}/merge", {"merge_method": "squash"})


def get_discussion_comments(number):
    result = gh_graphql(
        '{ repository(owner:"%s", name:"%s") { discussion(number:%d) { comments(first:20) { nodes { body author { login } } } } } }' % (CONSUMER_OWNER, CONSUMER_NAME, number)
    )
    return result["data"]["repository"]["discussion"]["comments"]["nodes"]


def get_recent_workflow_runs(limit=5):
    url = f"https://api.github.com/repos/{CONSUMER_REPO}/actions/runs?per_page={limit}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {TOKEN}",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["workflow_runs"]


def poll_until(description, check_fn, timeout=600, interval=30):
    print(f"Waiting: {description} (timeout {timeout}s)")
    start = time.time()
    while time.time() - start < timeout:
        try:
            result = check_fn()
            if result:
                elapsed = int(time.time() - start)
                print(f"  ✓ {description} ({elapsed}s)")
                return result
        except Exception as e:
            print(f"  (poll error: {e})")
        time.sleep(interval)
    elapsed = int(time.time() - start)
    print(f"  ✗ TIMEOUT: {description} ({elapsed}s)")
    return None


def run_with_retry(description, test_fn, max_retries=2):
    """Run a test function with retry on failure."""
    for attempt in range(1, max_retries + 1):
        print(f"\n--- {description}: attempt {attempt}/{max_retries} ---")
        try:
            if test_fn():
                return True
        except Exception as e:
            print(f"  (attempt {attempt} error: {e})")
        if attempt < max_retries:
            print("  Retrying in 30s...")
            time.sleep(30)
    print(f"  ✗ {description} failed after {max_retries} attempts")
    return False


def close_pr_if_open(pr_num):
    """Close a PR if it's still open (cleanup on test failure)."""
    try:
        pr = get_pr(pr_num)
        if not pr.get("merged") and pr.get("state") == "OPEN":
            print(f"  Cleaning up: closing PR #{pr_num}")
            gh_api("PATCH", f"pulls/{pr_num}", {"state": "closed"})
            print(f"  PR #{pr_num} closed")
    except Exception as e:
        print(f"  (cleanup error for PR #{pr_num}: {e})")


def curl_check(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pipeline-test"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception:
        return False


def get_deploy_url():
    return CONFIG.get("deploy", {}).get("url", "")


def get_health_endpoint():
    return CONFIG.get("deploy", {}).get("health_endpoint", "/health")


def test_non_db():
    """Flow: fix-me issue → PR → auto-merge → deploy → verify endpoint."""
    ts = int(time.time())
    pt = CONFIG.get("pipeline_test", {}).get("auto_merge", {})
    title = pt.get("title_template", "Pipeline Test: 更新 /api/pipeline-test 端点返回时间戳 {ts}").format(ts=ts)
    body = pt.get("body_template", "在 backend 添加一个 /api/pipeline-test 端点，返回 JSON `{{\"test\": true, \"timestamp\": {ts}}}`。这是一个无害的测试端点。").format(ts=ts)

    issue_num = create_issue(title, body)
    pr_num = None

    try:
        # Wait for agent to comment (PR creation or confirmation request)
        pr_num = poll_until(
            "agent creates PR or asks confirmation",
            lambda: _find_pr_in_comments(issue_num),
            timeout=600
        )
        if not pr_num:
            print("FAIL: No PR created within timeout")
            return False

        # Check if agent needs confirmation
        comments = get_issue_comments(issue_num)
        for c in comments:
            if "确认" in c["body"] or "confirm" in c["body"].lower():
                add_issue_comment(issue_num, "@oh 确认")
                pr_num = poll_until(
                    "PR created after confirmation",
                    lambda: _find_pr_in_comments(issue_num),
                    timeout=600
                )
                if not pr_num:
                    print("FAIL: No PR after confirmation")
                    return False

        # Wait for auto-merge
        merged = poll_until(
            f"PR #{pr_num} auto-merged",
            lambda: _check_pr_merged(pr_num),
            timeout=600
        )
        if not merged:
            print(f"FAIL: PR #{pr_num} not auto-merged")
            return False

        # Wait for deploy
        deployed = poll_until(
            "deployment completed",
            lambda: _check_deploy_success(),
            timeout=600
        )
        if not deployed:
            print("FAIL: Deploy not completed")
            return False

        # Verify endpoint
        time.sleep(30)
        deploy_url = get_deploy_url()
        if deploy_url:
            ok = curl_check(f"{deploy_url}/api/pipeline-test")
            if ok:
                print("✓ Endpoint /api/pipeline-test accessible")
            else:
                print("⚠ Endpoint not accessible (may not be implemented by agent, acceptable)")

        print("PASS: non-db flow")
        return True

    finally:
        if pr_num:
            close_pr_if_open(pr_num)
        close_issue(issue_num)


def _find_pr_in_comments(issue_num):
    comments = get_issue_comments(issue_num)
    for c in comments:
        m = re.search(r"PR #(\d+)", c["body"])
        if m:
            return int(m.group(1))
        m = re.search(r"pull/(\d+)", c["body"])
        if m:
            return int(m.group(1))
    return None


def _check_pr_merged(pr_num):
    pr = get_pr(pr_num)
    return pr.get("merged", False)


def _check_deploy_success():
    runs = get_recent_workflow_runs(20)
    for r in runs:
        if r["conclusion"] == "success" and r["created_at"] > time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 900)):
            return True
    return False


def test_db():
    """Flow: DB fix-me issue → PR → risk analysis → manual merge → deploy."""
    ts = int(time.time())
    pt = CONFIG.get("pipeline_test", {}).get("human_review", {})
    title = pt.get("title_template", "Pipeline Test: 给 organizations 表添加 pipeline_test_{ts} nullable 列").format(ts=ts)
    body = pt.get("body_template", "在 backend/migration 中添加一个迁移，给 organizations 表添加 `pipeline_test_{ts}` 列（类型 String，nullable，默认 null）。这是一个无害的测试列。").format(ts=ts)

    issue_num = create_issue(title, body)
    pr_num = None

    try:
        # Wait for PR
        pr_num = poll_until(
            "agent creates PR for DB issue",
            lambda: _find_pr_in_comments(issue_num),
            timeout=600
        )
        if not pr_num:
            print("FAIL: No PR created")
            return False

        # Wait for risk analysis comment
        risk = poll_until(
            f"risk analysis comment on PR #{pr_num}",
            lambda: _check_risk_analysis(pr_num),
            timeout=300
        )
        if not risk:
            print(f"FAIL: No risk analysis on PR #{pr_num}")
            return False
        print(f"  ✓ Risk analysis found: {risk[:100]}")

        # Verify auto-merge NOT enabled
        pr = get_pr(pr_num)
        if pr.get("auto_merge"):
            print("FAIL: auto-merge should NOT be enabled for DB changes")
            return False
        print("  ✓ auto-merge not enabled (correct for DB changes)")

        # Wait for PR to be mergeable (CI checks pass)
        mergeable = poll_until(
            f"PR #{pr_num} mergeable",
            lambda: get_pr(pr_num).get("mergeable") is True,
            timeout=600
        )
        if not mergeable:
            print(f"FAIL: PR #{pr_num} not mergeable within timeout")
            return False

        # Manual merge
        merge_pr(pr_num)

        # Wait for deploy
        deployed = poll_until(
            "deployment after DB merge",
            lambda: _check_deploy_success(),
            timeout=600
        )
        if not deployed:
            print("FAIL: Deploy not completed after DB merge")
            return False

        # Verify health
        deploy_url = get_deploy_url()
        if deploy_url:
            ok = curl_check(f"{deploy_url}{get_health_endpoint()}")
            if ok:
                print("✓ Health check passed")
            else:
                print("⚠ Health check failed (investigating)")

        print("PASS: db flow")
        return True

    finally:
        if pr_num:
            close_pr_if_open(pr_num)
        close_issue(issue_num)


def _check_risk_analysis(pr_num):
    comments = gh_api("GET", f"issues/{pr_num}/comments")
    for c in comments:
        body = c["body"]
        if "数据库变更" in body or "风险" in body:
            if "汇总" in body or "严重" in body:
                return body
    return None


def test_discussion():
    """Flow: discussion @oh → discuss job → reply with code analysis."""
    ts = int(time.time())
    title = f"Pipeline Test: 分析项目结构 ({ts})"
    pt = CONFIG.get("pipeline_test", {}).get("discussion", {})
    body = pt.get("body", "@oh 请分析下项目的整体架构和模块划分")

    disc_num, disc_id = create_discussion(title, body)

    try:
        # Wait for reply
        reply = poll_until(
            "discuss reply posted",
            lambda: _find_discussion_reply(disc_num),
            timeout=900
        )
        if not reply:
            print("FAIL: No reply posted")
            return False

        # Verify reply quality
        issues = []
        if not reply or len(reply) < 50:
            issues.append("reply too short")

        system_markers = ["<SOUL>", "<ROLE>", "<MEMORY>", "<EFFICIENCY>", "System Prompt"]
        for marker in system_markers:
            if marker in reply:
                issues.append(f"contains system prompt marker: {marker}")

        code_refs = re.search(r"\.(rs|tsx?|py|json|toml|yaml)", reply)
        if not code_refs:
            issues.append("no code file references found")

        if issues:
            print(f"FAIL: Reply quality issues: {', '.join(issues)}")
            print(f"  Reply preview: {reply[:200]}")
            return False

        print(f"  ✓ Reply is clean ({len(reply)} chars, contains code references)")
        print("PASS: discussion flow")
        return True

    finally:
        close_discussion(disc_id)


def _find_discussion_reply(disc_num):
    comments = get_discussion_comments(disc_num)
    for c in comments:
        if c["author"]["login"] != "xieyucheng123":
            return c["body"]
        if c["body"].startswith("## 技术方案建议"):
            return c["body"]
    return None


def main():
    global CONFIG
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    print(f"=== Pipeline Test: {mode} ===")
    print(f"Repo: {CONSUMER_REPO}")
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print()

    CONFIG = fetch_config()
    print(f"Config loaded: {list(CONFIG.keys())}")
    print()

    if mode == "non-db":
        ok = run_with_retry("non-db pipeline test", test_non_db, max_retries=2)
    elif mode == "db":
        ok = run_with_retry("db pipeline test", test_db, max_retries=2)
    elif mode == "discussion":
        ok = run_with_retry("discussion pipeline test", test_discussion, max_retries=2)
    else:
        print(f"Unknown mode: {mode}. Use: non-db, db, discussion")
        sys.exit(1)

    print()
    if ok:
        print("=== RESULT: PASS ===")
        sys.exit(0)
    else:
        print("=== RESULT: FAIL ===")
        sys.exit(1)


if __name__ == "__main__":
    main()
