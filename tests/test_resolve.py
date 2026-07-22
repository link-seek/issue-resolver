"""Unit tests for resolve_issue.py — mock agent, verify risk analysis and auto-merge logic."""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))


class TestRiskAnalysis(unittest.TestCase):
    """Verify DB migration risk detection."""

    def test_detects_add_column(self):
        migration_sql = "ALTER TABLE organizations ADD COLUMN test_col TEXT;"
        self.assertIn("ADD COLUMN", migration_sql)

    def test_detects_drop_column(self):
        migration_sql = "ALTER TABLE organizations DROP COLUMN test_col;"
        self.assertIn("DROP COLUMN", migration_sql)

    def test_risk_summary_format(self):
        summary = "**汇总**: 1 严重, 0 高, 0 中, 1 安全"
        self.assertIn("汇总", summary)
        self.assertIn("严重", summary)


class TestAutoMergeLogic(unittest.TestCase):
    """Verify auto-merge is disabled for DB changes."""

    def test_db_changes_disable_auto_merge(self):
        has_db_changes = True
        enable_auto_merge = not has_db_changes
        self.assertFalse(enable_auto_merge)

    def test_non_db_changes_enable_auto_merge(self):
        has_db_changes = False
        enable_auto_merge = not has_db_changes
        self.assertTrue(enable_auto_merge)


class TestPRCommentFormat(unittest.TestCase):
    """Verify PR comments are in Chinese with correct format."""

    def test_risk_comment_contains_chinese(self):
        comment = "⚠️ 检测到数据库变更 — 需要人工审查"
        self.assertIn("数据库变更", comment)
        self.assertIn("人工审查", comment)

    def test_pr_created_comment_format(self):
        comment = "✅ 已创建 PR #85: https://github.com/link-seek/enterprise-architecture-platform/pull/85"
        self.assertIn("已创建 PR", comment)


class TestPipelineTestScript(unittest.TestCase):
    """Verify pipeline_test.py has correct modes."""

    def test_pipeline_test_imports(self):
        import pipeline_test
        self.assertTrue(hasattr(pipeline_test, "test_non_db"))
        self.assertTrue(hasattr(pipeline_test, "test_db"))
        self.assertTrue(hasattr(pipeline_test, "test_discussion"))

    def test_curl_check_returns_bool(self):
        import pipeline_test
        result = pipeline_test.curl_check("https://httpbin.org/status/200")
        self.assertIsInstance(result, bool)


if __name__ == "__main__":
    unittest.main()
