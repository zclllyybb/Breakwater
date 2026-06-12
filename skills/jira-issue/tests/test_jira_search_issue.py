from __future__ import annotations

import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import jira_search_issue  # noqa: E402


class FakeResponse:
    status_code = 200

    def __init__(self, payload: dict[str, object]):
        self._payload = payload
        self.text = ""

    def json(self) -> dict[str, object]:
        return self._payload


class JiraSearchIssueTests(unittest.TestCase):
    def test_output_json_without_output_file_keeps_full_stdout_behavior(self) -> None:
        payload = {
            "total": 1,
            "issues": [{"key": "OPS-1", "fields": {"comment": {"comments": [{"body": "x" * 5000}]}}}],
        }

        env = {"JIRA_URL": "https://jira.example", "JIRA_TOKEN": "token"}
        with patch.dict(os.environ, env), patch("jira_search_issue.requests.post", return_value=FakeResponse(payload)):
            result = CliRunner().invoke(jira_search_issue.main, ["--jql", "issuekey = OPS-1", "--output-json"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output)["issues"][0]["key"], "OPS-1")
        self.assertIn("x" * 100, result.output)

    def test_output_file_writes_full_json_and_keeps_stdout_small(self) -> None:
        payload = {
            "total": 1,
            "issues": [
                {
                    "key": "OPS-1",
                    "fields": {
                        "summary": "large issue",
                        "comment": {"comments": [{"body": "x" * 5000}]},
                    },
                }
            ],
            "isLast": True,
        }

        with tempfile.TemporaryDirectory() as tempdir:
            output_file = Path(tempdir) / "jira-result.json"
            env = {"JIRA_URL": "https://jira.example", "JIRA_TOKEN": "token"}
            with patch.dict(os.environ, env), patch("jira_search_issue.requests.post", return_value=FakeResponse(payload)):
                result = CliRunner().invoke(
                    jira_search_issue.main,
                    [
                        "--jql",
                        "issuekey = OPS-1",
                        "--output-json",
                        "--output-file",
                        str(output_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("output_file=", result.output)
            self.assertIn("keys=OPS-1", result.output)
            self.assertNotIn("x" * 100, result.output)
            self.assertIn("x" * 100, output_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
