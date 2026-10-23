"""Tests for scripts/link_shared.py, run against a throwaway tree under tmp/."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_PATH = os.path.join(REPO_ROOT, "scripts", "link_shared.py")
TMP_ROOT = os.path.join(REPO_ROOT, "tmp")


class LinkSharedTest(unittest.TestCase):
    def setUp(self) -> None:
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.root = tempfile.mkdtemp(prefix="linkshared-", dir=TMP_ROOT)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        spec = importlib.util.spec_from_file_location(f"link_shared_{id(self)}", SCRIPT_PATH)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.module.REPO_ROOT = self.root
        self.module.PROJECTS_ROOT = os.path.join(self.root, "projects")
        self.module.SHARED_ROOT = os.path.join(self.root, "shared")

    def write(self, rel: str, content: str, mode: int = 0o644) -> str:
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(path, mode)
        return path

    def run_script(self) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.module.main()
        return out.getvalue()

    def test_identical_file_becomes_relative_link(self) -> None:
        source = self.write("shared/.claude/rules/a.md", "x")
        path = self.write("projects/t/.claude/rules/a.md", "x")
        output = self.run_script()
        self.assertTrue(os.path.islink(path))
        self.assertEqual(os.readlink(path), "../../../../shared/.claude/rules/a.md")
        self.assertEqual(os.path.realpath(path), os.path.realpath(source))
        self.assertIn("linked    projects/t/.claude/rules/a.md", output)

    def test_different_content_is_reported_and_kept(self) -> None:
        self.write("shared/a.md", "x")
        path = self.write("projects/t/a.md", "y")
        output = self.run_script()
        self.assertFalse(os.path.islink(path))
        self.assertIn("diverged  projects/t/a.md", output)

    def test_different_executable_bit_is_reported_and_kept(self) -> None:
        self.write("shared/run.sh", "x", 0o755)
        path = self.write("projects/t/run.sh", "x", 0o644)
        output = self.run_script()
        self.assertFalse(os.path.islink(path))
        self.assertIn("diverged  projects/t/run.sh", output)

    def test_existing_link_is_skipped(self) -> None:
        self.write("shared/a.md", "x")
        os.makedirs(os.path.join(self.root, "projects/t"))
        os.symlink("../../shared/a.md", os.path.join(self.root, "projects/t/a.md"))
        self.assertEqual(self.run_script(), "")

    def test_missing_file_is_skipped(self) -> None:
        self.write("shared/a.md", "x")
        os.makedirs(os.path.join(self.root, "projects/t"))
        self.assertEqual(self.run_script(), "")
        self.assertFalse(os.path.lexists(os.path.join(self.root, "projects/t/a.md")))


if __name__ == "__main__":
    unittest.main()
