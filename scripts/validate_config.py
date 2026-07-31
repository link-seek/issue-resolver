#!/usr/bin/env python3
"""Validate .issue-resolver.yml — lightweight contract check for consumer repos.

Verifies:
1. All required config fields exist (reuses DEFAULT_CONFIG from fix_issue.py)
2. pipeline_test + deploy sections present (needed for pipeline tests)
3. test.command references valid build files (Cargo.toml / package.json)
4. At least one workflow calls issue-resolver reusable workflows (flexible, not hardcoded filename)
5. Trigger config consistency (if a dedicated issue-resolver.yml exists, check its params)

Design principle: The contract is .issue-resolver.yml (what the consumer declares).
The flow repo should NOT reach into the consumer's file structure beyond checking
that the consumer has wired up the reusable workflows somehow.

Usage:
    python validate_config.py    # run from consumer repo root
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from fix_issue import DEFAULT_CONFIG, load_config


def get_required_fields_from_defaults() -> list[str]:
    fields = []
    for section, value in DEFAULT_CONFIG.items():
        if isinstance(value, dict):
            for key in value:
                fields.append(f"{section}.{key}")
    return fields


PIPELINE_TEST_REQUIRED = [
    "pipeline_test.auto_merge.title_template",
    "pipeline_test.auto_merge.body_template",
    "pipeline_test.human_review.title_template",
    "pipeline_test.human_review.body_template",
    "pipeline_test.discussion.category",
    "pipeline_test.discussion.body",
    "deploy.url",
    "deploy.health_endpoint",
]


def get_nested(d: dict, path: str):
    keys = path.split(".")
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def find_manifest(filename: str) -> Path | None:
    """Locate a build manifest (Cargo.toml / package.json).

    Searches the repo root first, then common monorepo subdirectories, so
    repos whose backend lives under backend/ or frontend/ validate correctly.
    """
    candidates = [Path(filename), Path("backend", filename), Path("frontend", filename)]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def validate_config_fields(config: dict) -> list[str]:
    errors = []
    for field in get_required_fields_from_defaults():
        if get_nested(config, field) is None:
            errors.append(f"Missing required field: {field}")
    for field in PIPELINE_TEST_REQUIRED:
        if get_nested(config, field) is None:
            errors.append(f"Missing required field: {field}")
    return errors


def validate_test_command(config: dict) -> list[str]:
    errors = []
    test_cmd = get_nested(config, "test.command")
    if test_cmd:
        if "cargo" in test_cmd and find_manifest("Cargo.toml") is None:
            errors.append("test.command uses cargo but Cargo.toml not found at root, backend/, or frontend/")
        if "npm" in test_cmd and find_manifest("package.json") is None:
            errors.append("test.command uses npm but package.json not found at root, backend/, or frontend/")
    return errors


def validate_workflow_file() -> list[str]:
    """Check that at least one workflow in .github/workflows/ calls issue-resolver.

    Instead of requiring a specific filename (issue-resolver.yml), we check that
    ANY workflow file references the issue-resolver repo's reusable workflows.
    This follows the override-first pattern: the consumer can wire up the
    workflows however they want, as long as they're connected.
    """
    errors = []
    workflow_dir = Path(".github/workflows")
    if not workflow_dir.exists():
        errors.append("Missing .github/workflows/ directory")
        return errors

    # Check if any workflow file references issue-resolver reusable workflows
    resolver_ref = "issue-resolver/.github/workflows"
    has_resolver = False
    for wf_file in workflow_dir.glob("*.yml"):
        content = wf_file.read_text()
        if resolver_ref in content:
            has_resolver = True
            break

    if not has_resolver:
        errors.append(
            "No workflow calls issue-resolver reusable workflows. "
            "Add a 'uses: link-seek/issue-resolver/.github/workflows/...' step "
            "to one of your workflow files."
        )
    return errors


def validate_workflow_consistency(config: dict) -> list[str]:
    """Check trigger config consistency if a dedicated issue-resolver.yml exists.

    If the consumer has a .github/workflows/issue-resolver.yml, verify its
    trigger params match .issue-resolver.yml. If they don't have this specific
    file, that's fine — they might be using a different filename.
    """
    errors = []
    workflow_path = Path(".github/workflows/issue-resolver.yml")
    if not workflow_path.exists():
        return errors

    content = workflow_path.read_text()

    config_label = get_nested(config, "trigger.label")
    config_mention = get_nested(config, "trigger.mention")

    checks = [("trigger-label", "label", config_label), ("mention", "mention", config_mention)]
    for input_name, config_key, config_val in checks:
        if config_val is None:
            continue
        pattern = rf'{input_name}:\s*["\']([^"\']+)["\']'
        m = re.search(pattern, content)
        if m:
            workflow_val = m.group(1)
            if workflow_val != config_val:
                errors.append(
                    f"Inconsistency: workflow passes {input_name}='{workflow_val}' "
                    f"but .issue-resolver.yml has trigger.{config_key}='{config_val}'"
                )
    return errors


def main():
    print("=== Pipeline Config Validation ===")
    config = load_config()

    errors = []
    errors += validate_config_fields(config)
    errors += validate_test_command(config)
    errors += validate_workflow_file()
    errors += validate_workflow_consistency(config)

    if errors:
        print("\n❌ Config validation FAILED:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    print("\n✅ Config validation PASSED")
    print(f"  trigger.label: {get_nested(config, 'trigger.label')}")
    print(f"  trigger.mention: {get_nested(config, 'trigger.mention')}")
    print(f"  test.command: {get_nested(config, 'test.command')}")
    print(f"  deploy.url: {get_nested(config, 'deploy.url')}")


if __name__ == "__main__":
    main()
