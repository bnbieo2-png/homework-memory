from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import learning_enrichment
import web_app


ROOT = Path(__file__).resolve().parent


class PublicReleaseTest(unittest.TestCase):
    def test_privacy_audit_passes(self) -> None:
        result = subprocess.run(
            ["python3", str(ROOT / "scripts/privacy_audit.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_private_runtime_files_are_ignored(self) -> None:
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for marker in ("/data/", ".env", "*.sqlite3", "*.jpg", "*.pdf", "*.log"):
            self.assertIn(marker, gitignore)

    def test_public_container_contains_only_core_app(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("COPY .", dockerfile)
        self.assertIn("web_app.py learning_enrichment.py", dockerfile)
        self.assertNotIn("trading", dockerfile.lower())
        self.assertNotIn("cloud_mirror", dockerfile)

    def test_ai_is_opt_in(self) -> None:
        previous = os.environ.pop("HOMEWORK_NOTEBOOK_ENABLE_AI", None)
        try:
            with self.assertRaisesRegex(RuntimeError, "默认关闭"):
                learning_enrichment.run_openclaw("fictional homework prompt")
        finally:
            if previous is not None:
                os.environ["HOMEWORK_NOTEBOOK_ENABLE_AI"] = previous

    def test_unconfigured_email_does_not_send(self) -> None:
        original = web_app.SELECTED_EMAIL_RECIPIENT
        web_app.SELECTED_EMAIL_RECIPIENT = ""
        try:
            with self.assertRaisesRegex(RuntimeError, "收件人尚未配置"):
                web_app.email_selected_questions(
                    [{"prompt_text": "虚构示例题"}],
                    "questions",
                    "http://127.0.0.1:18765",
                    pdf_generator=lambda *_args: Path(tempfile.gettempdir()) / "unused.pdf",
                )
        finally:
            web_app.SELECTED_EMAIL_RECIPIENT = original

    def test_server_defaults_to_localhost(self) -> None:
        source = (ROOT / "web_app.py").read_text(encoding="utf-8")
        self.assertIn('parser.add_argument("--host", default="127.0.0.1")', source)

    def test_server_refuses_remote_access_without_login(self) -> None:
        env = {
            **os.environ,
            "HOMEWORK_NOTEBOOK_ACCESS_CODE": "",
            "HOMEWORK_NOTEBOOK_SESSION_SECRET": "",
        }
        result = subprocess.run(
            ["python3", "web_app.py", "--host", "0.0.0.0", "--port", "0"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("必须先设置", result.stdout + result.stderr)

    def test_fictional_demo_can_be_seeded_without_private_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {**os.environ, "HOMEWORK_NOTEBOOK_DATA_DIR": temp_dir}
            result = subprocess.run(
                ["python3", "scripts/seed_demo.py"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((Path(temp_dir) / "mistakes.sqlite3").is_file())


if __name__ == "__main__":
    unittest.main()
