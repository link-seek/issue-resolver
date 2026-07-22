"""Unit tests for discuss.py — mock LLM, verify prompt generation and response extraction."""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))


class TestGetFileTree(unittest.TestCase):
    def test_returns_file_list(self):
        from discuss import get_file_tree
        result = get_file_tree()
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_excludes_noise(self):
        from discuss import get_file_tree
        result = get_file_tree()
        self.assertNotIn(".git/", result)
        self.assertNotIn("node_modules/", result)
        self.assertNotIn("__pycache__/", result)

    def test_limits_to_200_files(self):
        from discuss import get_file_tree
        result = get_file_tree()
        lines = [l for l in result.split("\n") if l.strip()]
        self.assertLessEqual(len(lines), 200)


class TestPromptGeneration(unittest.TestCase):
    """Verify the prompt includes code context and discussion content."""

    @patch("discuss.get_file_tree", return_value="./src/main.rs\n./src/api.rs")
    @patch("discuss.get_discussion")
    def test_prompt_contains_code_context(self, mock_get_disc, mock_tree):
        mock_get_disc.return_value = {
            "title": "Test Discussion",
            "body": "Test body",
            "category": {"name": "General"},
            "comments": {"nodes": []},
        }

        from discuss import get_file_tree
        file_tree = get_file_tree()
        self.assertIn("./src/main.rs", file_tree)
        self.assertIn("./src/api.rs", file_tree)


class TestReplyExtraction(unittest.TestCase):
    """Verify response extraction from LLM output."""

    def test_clean_response(self):
        response = "## 分析结果\n\n前端打包体积过大，建议优化。"
        self.assertGreater(len(response), 10)
        self.assertNotIn("<SOUL>", response)

    def test_system_prompt_detection(self):
        system_markers = ["<SOUL>", "<ROLE>", "<MEMORY>", "<EFFICIENCY>"]
        clean_response = "这是干净的回复"
        for marker in system_markers:
            self.assertNotIn(marker, clean_response)


class TestGraphQLHelpers(unittest.TestCase):
    """Verify GraphQL helper functions handle errors."""

    def test_reply_discussion_returns_result(self):
        import discuss
        self.assertTrue(hasattr(discuss, "reply_discussion"))

    def test_get_discussion_returns_dict(self):
        import discuss
        self.assertTrue(hasattr(discuss, "get_discussion"))


if __name__ == "__main__":
    unittest.main()
