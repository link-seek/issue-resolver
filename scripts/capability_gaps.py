#!/usr/bin/env python3
"""Capability gap pattern library for L2 (capability-resolver).

When L1 (auto-fix) hits its iteration limit, L2 uses this library to:
1. Classify the CI failures into known capability gap patterns
2. Determine what L1 capability is missing
3. Generate the fix to upgrade L1

Each pattern specifies:
- name: Unique identifier
- match: Function/logic to detect this pattern from CI failure logs
- diagnosis: What L1 capability is missing
- fix_type: Type of fix (design-fix, knowledge-append, context-source-add)
- fix_target: Which file in issue-resolver to modify
- fix_action: What modification to make
- tech_stack: Optional tech stack requirement (e.g., "sqlite", "sea-orm")

Usage:
    from capability_gaps import classify_failures
    gaps = classify_failures(ci_logs, pr_diff, tech_stack)
    for gap in gaps:
        print(f"Pattern: {gap.name}, Fix: {gap.fix_type} → {gap.fix_target}")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CapabilityGap:
    """A detected capability gap in L1."""
    name: str
    diagnosis: str
    fix_type: str  # design-fix, knowledge-append, context-source-add
    fix_target: str  # file path in issue-resolver repo
    fix_action: str  # description of what to change
    fix_content: str = ""  # actual content to add/modify
    tech_stack: Optional[str] = None  # required tech stack, None = universal
    confidence: float = 0.0  # 0-1, how confident we are this is the right pattern


@dataclass
class Pattern:
    """A capability gap pattern definition."""
    name: str
    match_keywords: list[str]  # keywords to search in CI logs
    match_all: bool  # if True, all keywords must match; if False, any match
    diagnosis: str
    fix_type: str
    fix_target: str
    fix_action: str
    fix_content: str
    tech_stack: Optional[str]
    confidence: float


PATTERNS: list[Pattern] = [
    Pattern(
        name="dead-ci-gate",
        match_keywords=["Missing .github/workflows/", "Config validation FAILED"],
        match_all=True,
        diagnosis=(
            "L1's CI gate requires a specific workflow filename that the consumer "
            "repo doesn't have. The gate is too strict — it should check for "
            "ANY workflow referencing issue-resolver, not a hardcoded filename."
        ),
        fix_type="design-fix",
        fix_target="scripts/validate_config.py",
        fix_action="replace validate_workflow_file() to check for any workflow referencing issue-resolver",
        fix_content="""def validate_workflow_file() -> list[str]:
    errors = []
    workflow_dir = Path(".github/workflows")
    if not workflow_dir.exists():
        errors.append("Missing .github/workflows/ directory")
        return errors
    resolver_ref = "issue-resolver/.github/workflows"
    has_resolver = False
    for wf_file in workflow_dir.glob("*.yml"):
        if resolver_ref in wf_file.read_text():
            has_resolver = True
            break
    if not has_resolver:
        errors.append("No workflow calls issue-resolver reusable workflows.")
    return errors""",
        tech_stack=None,  # universal
        confidence=0.95,
    ),
    Pattern(
        name="sqlite-uuid-blindness",
        match_keywords=["FOREIGN KEY constraint failed", "row must exist", "SqliteError"],
        match_all=False,  # any one of these is specific enough for SQLite issues
        diagnosis=(
            "L1 doesn't know that SeaORM stores UUIDs as 16-byte binary blobs "
            "in SQLite. Raw SQL queries using string UUIDs can't match binary blobs."
        ),
        fix_type="knowledge-append",
        fix_target="templates/prompt_fix_pr.md",
        fix_action="append SQLite UUID handling rules to the prompt template",
        fix_content="""## SQLite + SeaORM UUID 规则
SeaORM 在 SQLite 中将 Uuid 类型存为 16 字节 binary blob（X'...'），不是字符串。
- ❌ WHERE "id" = '00000000-0000-0000-0000-000000000010' （string，查不到）
- ✅ WHERE "id" = X'00000000000000000000000000000010' （binary，匹配）
- 或用 hex() 函数：WHERE hex("id") = '00000000000000000000000000000010'""",
        tech_stack=None,  # universal - "FOREIGN KEY constraint" is specific enough
        confidence=0.90,
    ),
    Pattern(
        name="e2e-context-blindness",
        match_keywords=["not.toBeVisible", "加载"],
        match_all=False,  # either keyword indicates E2E context issue
        diagnosis=(
            "L1 only sees CI failure logs but not what the PR changed. "
            "When business logic changes cause E2E test failures, L1 can't "
            "diagnose the root cause without seeing the PR diff."
        ),
        fix_type="context-source-add",
        fix_target=".github/workflows/fix.yml",
        fix_action="add PR diff gathering to the context collection step",
        fix_content="""# Gather PR diff for context
PR_DIFF=$(gh api repos/$GITHUB_REPOSITORY/pulls/$PR_NUMBER/files \\
  --jq '.[] | "### \\(.status): \\(.path)\\n\\`\\`\\`diff\\n\\(.patch // "binary")\\n\\`\\`\\`"' 2>/dev/null | head -200)
if [ -n "$PR_DIFF" ]; then
  COMBINED+="\\n\\n## PR Changes (diff)\\n$PR_DIFF"
fi""",
        tech_stack=None,  # universal
        confidence=0.85,
    ),
    Pattern(
        name="graphql-e2e-cascade",
        match_keywords=["GraphQL errors detected during test", "not.toBeVisible"],
        match_all=False,  # either symptom indicates the cascade
        diagnosis=(
            "L1 doesn't understand the causal chain: backend bug → GraphQL error "
            "→ E2E test failure. When review-ai finds a backend issue (e.g., "
            "transactional violation in space_service.rs) and E2E tests fail with "
            "GraphQL errors, L1 treats them as separate problems instead of "
            "fixing the root cause (backend) to resolve the symptom (E2E)."
        ),
        fix_type="knowledge-append",
        fix_target="templates/prompt_fix_pr.md",
        fix_action="append GraphQL→E2E cascade knowledge to the prompt template",
        fix_content="""## GraphQL Error → E2E Test Failure 因果链
当 E2E 测试报 `GraphQL errors detected during test` 时，根因通常在后端 resolver：
1. 后端 resolver 返回 Error → GraphQL response 包含 errors 字段
2. 前端 Apollo Client 收到 errors → 抛出异常或数据为空
3. E2E 断言失败（如 not.toBeVisible、元素不存在等）

修复策略：
- **不要修前端测试或组件** — 那是症状不是根因
- **修后端 resolver/mutation** — 让它正确返回数据或处理错误
- 如果 review-ai 同时报了 blocking issue（如事务违反 Result 契约），
  那个就是根因，修它就能同时解决 E2E 失败

## Rust 事务安全规则
当 `save()` 后跟 `audit_log()` 时，如果 audit_log 失败：
- ❌ 返回 Err（调用方认为操作未生效，但数据已入库）
- ✅ 用事务/补偿机制，或先写 audit log 再 save，或用 outbox pattern""",
        tech_stack=None,  # universal
        confidence=0.88,
    ),
    Pattern(
        name="config-validation-too-strict",
        match_keywords=["Config validation FAILED", "Missing required field"],
        match_all=True,
        diagnosis=(
            "L1's config validation requires fields that may not be needed "
            "by all consumers. The validation should be lenient — use defaults "
            "for optional fields instead of failing."
        ),
        fix_type="design-fix",
        fix_target="scripts/validate_config.py",
        fix_action="make required fields optional with defaults",
        fix_content="",
        tech_stack=None,
        confidence=0.70,
    ),
    Pattern(
        name="empty-loop-iteration",
        match_keywords=["Max auto-fix iterations", "Fix already applied"],
        match_all=True,
        diagnosis=(
            "L1 hits iteration limit, L2 upgrades L1 (idempotent), L2 retries L1, "
            "but review-ai re-triggers L1 with default max_iterations=10. "
            "Since the PR already has 10+ auto-fix commits, L1 immediately hits "
            "the limit again, creating an empty loop. "
            "Fix: change L1's iteration count from all-time to a sliding window "
            "(only count auto-fix commits in the last 15 commits). "
            "This way L1 gets fresh iterations after each L2 upgrade."
        ),
        fix_type="design-fix",
        fix_target=".github/workflows/fix.yml",
        fix_action="change iteration count from all-time to sliding window of 15 commits",
        fix_content=(
            'git log --oneline --grep="^auto-fix:" | wc -l | tr -d \' \''
            '>>>'
            'git log --oneline -15 | grep "auto-fix:" | wc -l | tr -d \' \''
        ),
        tech_stack=None,
        confidence=0.92,
    ),
    Pattern(
        name="empty-loop-dispatch",
        match_keywords=["Max auto-fix iterations", "Fix already applied"],
        match_all=True,
        diagnosis=(
            "review-ai dispatches L1 with hardcoded max_iterations (default 10), "
            "but the PR already has 10+ auto-fix commits. L1 immediately hits "
            "the limit. Fix: pr-review.yml should dynamically set max_iterations "
            "based on existing auto-fix count (existing + 10)."
        ),
        fix_type="design-fix",
        fix_target=".github/workflows/pr-review.yml",
        fix_action="dynamic max_iterations = existing auto-fix count + 10",
        fix_content=(
            '-f max_iterations="${{ inputs.max-auto-fix-iterations }}" || echo "Failed to dispatch auto-fix"'
            '>>>'
            '-f max_iterations="$(gh api repos/$GITHUB_REPOSITORY/commits?sha=$HEAD_BRANCH\\&per_page=100 --jq \'[.[]|select(.commit.message|startswith(\"auto-fix:\"))]|length\' 2>/dev/null | tr -d \' \' || echo 0)" || echo "Failed to dispatch auto-fix"'
        ),
        tech_stack=None,
        confidence=0.90,
    ),
]


def classify_failures(
    ci_logs: str,
    pr_diff: str = "",
    tech_stack: list[str] | None = None,
) -> list[CapabilityGap]:
    """Classify CI failures into capability gaps.

    Args:
        ci_logs: The CI failure log output
        pr_diff: The PR diff (optional, for context-aware classification)
        tech_stack: List of tech stack identifiers (e.g., ["sqlite", "sea-orm"])

    Returns:
        List of detected capability gaps, sorted by confidence (highest first)
    """
    tech_stack = tech_stack or []
    gaps: list[CapabilityGap] = []

    for pattern in PATTERNS:
        # Skip patterns that require a specific tech stack the consumer doesn't have
        if pattern.tech_stack and pattern.tech_stack not in tech_stack:
            continue

        # Check if the pattern matches
        if pattern.match_all:
            matched = all(kw in ci_logs for kw in pattern.match_keywords)
        else:
            matched = any(kw in ci_logs for kw in pattern.match_keywords)

        if matched:
            gap = CapabilityGap(
                name=pattern.name,
                diagnosis=pattern.diagnosis,
                fix_type=pattern.fix_type,
                fix_target=pattern.fix_target,
                fix_action=pattern.fix_action,
                fix_content=pattern.fix_content,
                tech_stack=pattern.tech_stack,
                confidence=pattern.confidence,
            )
            gaps.append(gap)

    # Sort by confidence (highest first)
    gaps.sort(key=lambda g: g.confidence, reverse=True)
    return gaps


def detect_tech_stack(pr_diff: str, ci_logs: str) -> list[str]:
    """Detect tech stack from PR diff and CI logs.

    This allows L2 to automatically determine which patterns are relevant
    without requiring the consumer to declare their tech stack.
    """
    stack = []
    combined = pr_diff + ci_logs

    if any(kw in combined for kw in ["Cargo.toml", "cargo test", "sea-orm", "SeaORM"]):
        stack.append("rust")
    if any(kw in combined for kw in ["sqlite", "SQLite", "SqliteError"]):
        stack.append("sqlite")
    if any(kw in combined for kw in ["sea-orm", "SeaORM", "sea_orm"]):
        stack.append("sea-orm")
    if any(kw in combined for kw in ["package.json", "npm", "vite", "Vite"]):
        stack.append("node")
    if any(kw in combined for kw in ["playwright", "Playwright", "E2E"]):
        stack.append("e2e")
    if any(kw in combined for kw in ["react", "React", "jsx", "tsx"]):
        stack.append("react")

    return stack


def format_gap_report(gaps: list[CapabilityGap]) -> str:
    """Format capability gaps as a readable report for L2 dispatch."""
    if not gaps:
        return "No known capability gap patterns matched."

    lines = ["## Capability Gap Analysis (L2)", ""]
    for i, gap in enumerate(gaps, 1):
        lines.append(f"### Gap #{i}: {gap.name} (confidence: {gap.confidence:.0%})")
        lines.append(f"- **Diagnosis**: {gap.diagnosis}")
        lines.append(f"- **Fix type**: {gap.fix_type}")
        lines.append(f"- **Fix target**: {gap.fix_target}")
        lines.append(f"- **Fix action**: {gap.fix_action}")
        if gap.tech_stack:
            lines.append(f"- **Tech stack**: {gap.tech_stack}")
        lines.append("")

    return "\n".join(lines)
