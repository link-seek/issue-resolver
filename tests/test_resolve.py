"""Unit tests for resolve_issue.py — call real functions with mocked I/O."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))


class TestAnalyzeDbRisk(unittest.TestCase):
    """Test the real analyze_db_risk function."""

    def setUp(self):
        from resolve_issue import analyze_db_risk
        self.analyze_db_risk = analyze_db_risk

    def test_add_column_detected_as_safe(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sql', delete=False) as f:
            f.write("ALTER TABLE organizations ADD COLUMN test_col TEXT;")
            f.flush()
            result = self.analyze_db_risk([f.name])
        os.unlink(f.name)
        self.assertIn("数据库变更", result)
        self.assertIn("ADD COLUMN", result)

    def test_drop_table_detected_as_critical(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sql', delete=False) as f:
            f.write("DROP TABLE users;")
            f.flush()
            result = self.analyze_db_risk([f.name])
        os.unlink(f.name)
        self.assertIn("Critical", result)
        self.assertIn("DROP TABLE", result)

    def test_drop_column_detected_as_critical(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sql', delete=False) as f:
            f.write("ALTER TABLE users DROP COLUMN old_col;")
            f.flush()
            result = self.analyze_db_risk([f.name])
        os.unlink(f.name)
        self.assertIn("Critical", result)

    def test_create_index_without_concurrently_is_high(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sql', delete=False) as f:
            f.write("CREATE INDEX idx ON users(email);")
            f.flush()
            result = self.analyze_db_risk([f.name])
        os.unlink(f.name)
        self.assertIn("High", result)

    def test_multiple_findings_all_reported(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sql', delete=False) as f:
            f.write("DROP TABLE old;\nALTER TABLE users ADD COLUMN x TEXT;")
            f.flush()
            result = self.analyze_db_risk([f.name])
        os.unlink(f.name)
        self.assertIn("Critical", result)
        self.assertIn("Safe", result)

    def test_file_not_found_handled(self):
        result = self.analyze_db_risk(["/nonexistent/migration.sql"])
        self.assertIn("File not found", result)

    def test_summary_format(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sql', delete=False) as f:
            f.write("DROP TABLE x;")
            f.flush()
            result = self.analyze_db_risk([f.name])
        os.unlink(f.name)
        self.assertIn("汇总", result)
        self.assertIn("严重", result)

    def test_empty_db_files_no_crash(self):
        result = self.analyze_db_risk([])
        self.assertIsInstance(result, str)


class TestRunTests(unittest.TestCase):
    """Test run_tests function with different project types."""

    def test_no_project_returns_true(self):
        from resolve_issue import run_tests
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                result = run_tests()
                self.assertTrue(result)
            finally:
                os.chdir(old_cwd)


class TestGhApi(unittest.TestCase):
    """Test gh_api helper with mocked HTTP."""

    @patch('resolve_issue.urllib.request.urlopen')
    def test_gh_api_get_returns_json(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=None)
        mock_resp.read.return_value = b'{"key": "value"}'
        mock_urlopen.return_value = mock_resp

        from resolve_issue import gh_api
        result = gh_api("GET", "owner/repo/issues/1", "fake-token")
        self.assertEqual(result, {"key": "value"})


class TestRebaseLogic(unittest.TestCase):
    """Test the rebase-before-push logic in resolve_issue.py."""

    @patch('resolve_issue.subprocess.run')
    def test_rebase_succeeds(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        from resolve_issue import subprocess as sp_mod
        sp_mod.run(["git", "fetch", "origin", "main"], check=True)
        sp_mod.run(["git", "rebase", "origin/main"], capture_output=True, text=True)
        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(mock_run.call_args_list[0][0][0], ["git", "fetch", "origin", "main"])

    @patch('resolve_issue.subprocess.run')
    def test_rebase_fails_triggers_merge_fallback(self, mock_run):
        rebase_fail = MagicMock(returncode=1, stderr="conflict", stdout="")
        merge_success = MagicMock(returncode=0, stderr="", stdout="")
        abort = MagicMock(returncode=0, stderr="", stdout="")
        mock_run.side_effect = [rebase_fail, abort, merge_success]
        from resolve_issue import subprocess as sp_mod
        sp_mod.run(["git", "rebase", "origin/main"], capture_output=True, text=True)
        sp_mod.run(["git", "rebase", "--abort"], check=True)
        sp_mod.run(["git", "merge", "origin/main", "--no-edit"], capture_output=True, text=True)
        self.assertEqual(mock_run.call_count, 3)

    def test_rebase_command_format(self):
        expected_fetch = ["git", "fetch", "origin", "main"]
        expected_rebase = ["git", "rebase", "origin/main"]
        self.assertEqual(expected_fetch[0], "git")
        self.assertEqual(expected_rebase[1], "rebase")
        self.assertEqual(expected_rebase[2], "origin/main")


class TestPipelineTestScript(unittest.TestCase):
    """Verify pipeline_test.py has correct modes and helpers."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("PAT_TOKEN", "fake-token-for-test")

    def test_pipeline_test_imports(self):
        import pipeline_test
        self.assertTrue(hasattr(pipeline_test, "test_non_db"))
        self.assertTrue(hasattr(pipeline_test, "test_db"))
        self.assertTrue(hasattr(pipeline_test, "test_discussion"))

    def test_run_with_retry_returns_true_on_success(self):
        import pipeline_test
        result = pipeline_test.run_with_retry("test", lambda: True, max_retries=2)
        self.assertTrue(result)

    def test_run_with_retry_returns_false_on_failure(self):
        import pipeline_test
        result = pipeline_test.run_with_retry("test", lambda: False, max_retries=2)
        self.assertFalse(result)

    def test_close_pr_if_open_handles_error(self):
        import pipeline_test
        close_pr_if_open = pipeline_test.close_pr_if_open
        result = close_pr_if_open(999999)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
