"""Unit tests for validate_config.py."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))


class TestValidateConfigFields(unittest.TestCase):
    """Test config field validation."""

    def setUp(self):
        from validate_config import validate_config_fields
        self.validate_config_fields = validate_config_fields

    def test_complete_config_no_errors(self):
        config = {
            "trigger": {"label": "fix-me", "mention": "@oh"},
            "risk_detection": {"file_patterns": ["*.rs"]},
            "test": {"command": "cargo test"},
            "pipeline_test": {
                "auto_merge": {"title_template": "t", "body_template": "b"},
                "human_review": {"title_template": "t", "body_template": "b"},
                "discussion": {"category": "General", "body": "test"},
            },
            "deploy": {"url": "http://example.com", "health_endpoint": "/health"},
        }
        errors = self.validate_config_fields(config)
        self.assertEqual(errors, [])

    def test_missing_deploy_url(self):
        config = {
            "pipeline_test": {
                "auto_merge": {"title_template": "t", "body_template": "b"},
                "human_review": {"title_template": "t", "body_template": "b"},
                "discussion": {"category": "General", "body": "test"},
            },
            "deploy": {"health_endpoint": "/health"},
        }
        errors = self.validate_config_fields(config)
        self.assertTrue(any("deploy.url" in e for e in errors))

    def test_missing_pipeline_test_section(self):
        config = {"deploy": {"url": "http://example.com", "health_endpoint": "/health"}}
        errors = self.validate_config_fields(config)
        self.assertTrue(len(errors) > 0)
        self.assertTrue(any("pipeline_test" in e for e in errors))


class TestValidateTestCommand(unittest.TestCase):
    """Test test command validation."""

    def setUp(self):
        from validate_config import validate_test_command
        self.validate_test_command = validate_test_command

    def test_cargo_command_with_cargo_toml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                Path("Cargo.toml").touch()
                errors = self.validate_test_command({"test": {"command": "cargo test"}})
                self.assertEqual(errors, [])
            finally:
                os.chdir(old_cwd)

    def test_cargo_command_without_cargo_toml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                errors = self.validate_test_command({"test": {"command": "cargo test"}})
                self.assertTrue(any("Cargo.toml" in e for e in errors))
            finally:
                os.chdir(old_cwd)

    def test_no_test_command_no_error(self):
        errors = self.validate_test_command({"test": {}})
        self.assertEqual(errors, [])


class TestValidateWorkflowConsistency(unittest.TestCase):
    """Test workflow parameter consistency."""

    def setUp(self):
        from validate_config import validate_workflow_consistency
        self.validate_workflow_consistency = validate_workflow_consistency

    def test_consistent_params_no_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                Path(".github/workflows").mkdir(parents=True)
                Path(".github/workflows/issue-resolver.yml").write_text(
                    'with:\n  trigger-label: "fix-me"\n  mention: "@oh"\n'
                )
                config = {"trigger": {"label": "fix-me", "mention": "@oh"}}
                errors = self.validate_workflow_consistency(config)
                self.assertEqual(errors, [])
            finally:
                os.chdir(old_cwd)

    def test_inconsistent_label_reports_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                Path(".github/workflows").mkdir(parents=True)
                Path(".github/workflows/issue-resolver.yml").write_text(
                    'with:\n  trigger-label: "bug"\n  mention: "@oh"\n'
                )
                config = {"trigger": {"label": "fix-me", "mention": "@oh"}}
                errors = self.validate_workflow_consistency(config)
                self.assertTrue(any("Inconsistency" in e for e in errors))
            finally:
                os.chdir(old_cwd)

    def test_no_workflow_file_no_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                config = {"trigger": {"label": "fix-me", "mention": "@oh"}}
                errors = self.validate_workflow_consistency(config)
                self.assertEqual(errors, [])
            finally:
                os.chdir(old_cwd)


class TestGetNested(unittest.TestCase):
    """Test get_nested helper."""

    def setUp(self):
        from validate_config import get_nested
        self.get_nested = get_nested

    def test_simple_key(self):
        self.assertEqual(self.get_nested({"a": 1}, "a"), 1)

    def test_nested_key(self):
        self.assertEqual(self.get_nested({"a": {"b": {"c": 1}}}, "a.b.c"), 1)

    def test_missing_key_returns_none(self):
        self.assertIsNone(self.get_nested({"a": 1}, "b"))

    def test_missing_nested_key_returns_none(self):
        self.assertIsNone(self.get_nested({"a": {"b": 1}}, "a.c"))


if __name__ == "__main__":
    unittest.main()
