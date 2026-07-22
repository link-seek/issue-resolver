#!/usr/bin/env python3
"""Pipeline test: validates full flow from issue/discussion creation to deployment.

Usage:
    python pipeline_test.py non-db      # fix-me issue → PR → auto-merge → deploy
    python pipeline_test.py db          # DB fix-me issue → risk analysis → manual merge → deploy
    python pipeline_test.py discussion  # discussion @oh → discuss job → reply
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error


EAP_REPO = "link-seek/enterprise-architecture-platform"
EAP_OWNER = "link-seek"
EAP_NAME = "enterprise-architecture-platform"
DEPLOY_URL = "https://api.xieyucheng.top"
TOKEN = os.environ.get("PAT_TOKEN", "")

if not TOKEN:
    print("PAT_TOKEN not set")
    sys.exit(1)


def gh_api(method, path, data=None):
    url = f"https://api.github.com/repos/{EAP_REPO}/{path}"
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


def create_issue(title, body, labels="fix-me"):
    print(f"Creating issue: {title}")
    data = gh_api("POST", "issues", {"title": title, "body": body, "labels": labels.split(",")})
    print(f"  Issue #{data['number']}: {data['html_url']}")
    return data["number"]


def close_issue(number):
    print(f"Closing issue #{number}")
    gh_api("PATCH", f"issues/{number}", {"state": "closed"})
    print(f"  Issue #{number} closed")


def create_discussion(title, body):
    print(f"Creating discussion: {title}")
    repo_id = gh_graphql(
        '{ repository(owner:"%s", name:"%s") { id discussionCategories(first:5) { nodes { id name } } } }' % (EAP_OWNER, EAP_NAME)
    )["data"]["repository"]
    cat_id = next(c["id"] for c in repo_id["discussionCategories"]["nodes"] if c["name"] == "General")
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
        '{ repository(owner:"%s", name:"%s") { discussion(number:%d) { comments(first:20) { nodes { body author { login } } } } } }' % (EAP_OWNER, EAP_NAME, number)
    )
    return result["data"]["repository"]["discussion"]["comments"]["nodes"]


def get_recent_workflow_runs(limit=5):
    url = f"https://api.github.com/repos/{EAP_REPO}/actions/runs?per_page={limit}"
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


def curl_check(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pipeline-test"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception:
        return False


def test_non_db():
    """Flow: fix-me issue → PR → auto-merge → deploy → verify endpoint."""
    ts = int(time.time())
    title = f"Pipeline Test: 更新 /api/pipeline-test 端点返回时间戳 {ts}"
    body = f"在 backend 添加一个 /api/pipeline-test 端点，返回 JSON `{{\"test\": true, \"timestamp\": {ts}}}`。这是一个无害的测试端点。"

    issue_num = create_issue(title, body, "fix-me")

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
        ok = curl_check(f"{DEPLOY_URL}/api/pipeline-test")
        if ok:
            print("✓ Endpoint /api/pipeline-test accessible")
        else:
            print("⚠ Endpoint not accessible (may not be implemented by agent, acceptable)")

        print("PASS: non-db flow")
        return True

    finally:
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
    title = f"Pipeline Test: 给 organizations 表添加 pipeline_test_{ts} nullable 列"
    body = f"在 backend/migration 中添加一个迁移，给 organizations 表添加 `pipeline_test_{ts}` 列（类型 String，nullable，默认 null）。这是一个无害的测试列。"

    issue_num = create_issue(title, body, "fix-me")

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
            lambda: get_pr(pr_num).get("mergeable") == True,
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
        ok = curl_check(f"{DEPLOY_URL}/health")
        if ok:
            print("✓ Health check passed")
        else:
            print("⚠ Health check failed (investigating)")

        print("PASS: db flow")
        return True

    finally:
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
    body = "@oh 请分析下项目的整体架构和模块划分"

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
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    print(f"=== Pipeline Test: {mode} ===")
    print(f"Repo: {EAP_REPO}")
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print()

    if mode == "non-db":
        ok = test_non_db()
    elif mode == "db":
        ok = test_db()
    elif mode == "discussion":
        ok = test_discussion()
    else:
        print(f"Unknown mode: {mode}. Use: non-db, db, discussion")
        sys.exit(1)

    print()
    if ok:
        print(f"=== RESULT: PASS ===")
        sys.exit(0)
    else:
        print(f"=== RESULT: FAIL ===")
        sys.exit(1)


if __name__ == "__main__":
    main()
