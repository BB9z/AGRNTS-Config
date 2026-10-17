"""End-to-end tests for scripts/sync.py.

Each test builds a throwaway directory under tmp/ and points sync.py's path constants (REPO_ROOT/PROJECTS_ROOT/STATE_ROOT/...) at it as a whole. Real ~/.claude, real projects/, .state/, .backup/ and real target repos are never touched.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_PATH = os.path.join(REPO_ROOT, "scripts", "sync.py")
TMP_ROOT = os.path.join(REPO_ROOT, "tmp")

NO_CHANGES = "0 changed, 0 need review, 0 skipped."


def load_fresh_module():
    """Fresh module copy per test so module globals don't leak between cases."""
    spec = importlib.util.spec_from_file_location(
        f"sync_under_test_{id(object())}", SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SyncTestCase(unittest.TestCase):
    def setUp(self) -> None:
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.workdir = tempfile.mkdtemp(prefix="synctest-", dir=TMP_ROOT)
        self.addCleanup(lambda: shutil.rmtree(self.workdir, ignore_errors=True))

        self.module = load_fresh_module()
        repo_root = os.path.join(self.workdir, "repo")
        projects_root = os.path.join(repo_root, "projects")
        os.makedirs(projects_root)
        # check-ignore needs a real git repo; it doesn't commit, so no identity is needed.
        subprocess.run(
            ["git", "init", "-q"], cwd=repo_root, check=True, capture_output=True
        )
        with open(os.path.join(repo_root, ".gitignore"), "w", encoding="utf-8") as f:
            f.write(".DS_Store\n")
        self.module.REPO_ROOT = repo_root
        self.module.PROJECTS_ROOT = projects_root
        self.module.SHARED_ROOT = os.path.join(repo_root, "shared")
        self.module.STATE_ROOT = os.path.join(repo_root, ".state")
        self.module.CONFIG_PATH = os.path.join(self.module.STATE_ROOT, "config.json")
        self.module.SNAPSHOT_ROOT = os.path.join(self.module.STATE_ROOT, "snapshots")
        self.module.BACKUP_ROOT = os.path.join(repo_root, ".backup")

        # Not created up front: cmd_status/cmd_sync use isdir to decide whether the user-level template exists.
        # Must sit inside repo_root: the gitignore check finds .gitignore by walking up from the template root.
        self.user_template_root = os.path.join(repo_root, "user", "claude")
        self.user_target_root = os.path.join(self.workdir, "user_target")
        self.module.USER_TEMPLATE_ROOT = self.user_template_root
        self.module.USER_TARGET_ROOT = self.user_target_root

        self.target_root = os.path.join(self.workdir, "target")
        os.makedirs(self.target_root)

        # Fakes for the editor spawn and the stdin block. fake wait_for_enter stands for "a human pressed Enter"; non-interactive behavior is tested with the real implementation in UnattendedHoldTests.
        self.opened_editor_calls: list[list[str]] = []
        self.wait_for_enter_calls = 0

        def fake_open_editor(paths):
            self.opened_editor_calls.append(list(paths))

        def fake_wait_for_enter(prompt):
            self.wait_for_enter_calls += 1
            print(prompt)
            return True

        self.module.open_editor = fake_open_editor
        self.module.wait_for_enter = fake_wait_for_enter


    def run_cli(self, *argv: str):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = self.module.main(list(argv))
        return code, buf.getvalue()

    def make_template(self, name: str = "tpl") -> str:
        path = os.path.join(self.module.PROJECTS_ROOT, name)
        os.makedirs(path, exist_ok=True)
        return path

    def make_user_template(self) -> str:
        os.makedirs(self.user_template_root, exist_ok=True)
        return self.user_template_root

    def make_user_target(self) -> str:
        os.makedirs(self.user_target_root, exist_ok=True)
        return self.user_target_root

    @staticmethod
    def write(root: str, relpath: str, content: str, executable: bool = False) -> None:
        path = os.path.join(root, *relpath.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        if executable:
            os.chmod(path, 0o755)

    @staticmethod
    def read(root: str, relpath: str) -> str:
        path = os.path.join(root, *relpath.split("/"))
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def exists(root: str, relpath: str) -> bool:
        return os.path.exists(os.path.join(root, *relpath.split("/")))

    def add_target(self, template: str = "tpl") -> str:
        code, out = self.run_cli("add", self.target_root, "--template", template)
        self.assertEqual(code, 0, out)
        return "1"


class AddCommandTests(SyncTestCase):
    def test_add_creates_config_entry(self):
        self.make_template("tpl")
        short_id = self.add_target("tpl")
        config = self.module.load_config()
        self.assertEqual(config["targets"][short_id]["template"], "tpl")
        self.assertEqual(
            os.path.abspath(config["targets"][short_id]["path"]),
            os.path.abspath(self.target_root),
        )

    def test_add_rejects_missing_template(self):
        code, out = self.run_cli("add", self.target_root, "--template", "nope")
        self.assertEqual(code, 1)
        self.assertIn("error: no template named 'nope'", out)

    def test_add_rejects_missing_target_dir(self):
        self.make_template("tpl")
        missing = os.path.join(self.workdir, "does-not-exist")
        code, out = self.run_cli("add", missing, "--template", "tpl")
        self.assertEqual(code, 1)
        self.assertIn("error: not a directory", out)

    def test_add_rejects_duplicate_path(self):
        self.make_template("tpl")
        self.add_target("tpl")
        code, out = self.run_cli("add", self.target_root, "--template", "tpl")
        self.assertEqual(code, 1)
        self.assertIn("is already registered", out)

    def test_sync_unregistered_target_errors(self):
        self.make_template("tpl")
        code, out = self.run_cli("sync", self.target_root, "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("error: unknown repo", out)

    def test_resolve_target_by_path_or_id(self):
        self.make_template("tpl")
        self.write(os.path.join(self.module.PROJECTS_ROOT, "tpl"), "a.md", "x")
        self.add_target("tpl")
        code1, out1 = self.run_cli("sync", "1", "--dry-run")
        code2, out2 = self.run_cli("sync", self.target_root, "--dry-run")
        self.assertEqual(code1, 0)
        self.assertEqual(code2, 0)
        self.assertRegex(out1, r"template → repo\s+a\.md")
        self.assertRegex(out2, r"template → repo\s+a\.md")


class StatusCommandTests(SyncTestCase):
    def test_status_no_targets(self):
        code, out = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("No repos registered.", out)

    def test_status_counts_pending_and_customized(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "template v1")
        self.add_target("tpl")

        code, out = self.run_cli("status")
        self.assertIn(": 1 pending", out)
        self.assertIn("0 customized", out)

        self.run_cli("sync", "1")
        code, out = self.run_cli("status")
        self.assertIn(": 0 pending", out)
        self.assertIn("0 customized", out)

        # needs_merge doesn't count as customization (customization only compares the snapshot pair)
        self.write(tpl, "a.md", "template v2")
        self.write(self.target_root, "a.md", "target v2")
        code, out = self.run_cli("status")
        self.assertIn(": 1 pending", out)
        self.assertIn("0 customized", out)

    def test_status_settings_customized_counts_only_managed_keys(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "settings.json", json.dumps({"a": 1, "b": 2}))
        self.add_target("tpl")
        self.write(self.target_root, "settings.json", json.dumps({"own": 1, "b": 2, "a": 1}))
        self.run_cli("sync", "1")

        code, out = self.run_cli("status")
        self.assertIn("0 customized", out)

        # A managed key with a different value in the target snapshot is a real customization.
        target_snapshot = os.path.join(self.module.snapshot_dir("1", "target"), "settings.json")
        with open(target_snapshot, "w") as f:
            f.write(json.dumps({"own": 1, "b": 3, "a": 1}))
        self.write(self.target_root, "settings.json", json.dumps({"own": 1, "b": 3, "a": 1}))
        code, out = self.run_cli("status")
        self.assertIn("1 customized", out)


class SyncDecisionTableTests(SyncTestCase):
    def test_first_sync_pushes_new_files_and_skips_settings_local(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "AGENTS.md", "hello")
        self.write(tpl, ".claude/settings.json", '{"a":1}')
        self.write(tpl, ".claude/settings.local.json", '{"local":true}')
        self.add_target("tpl")

        code, out = self.run_cli("sync", "1", "--dry-run")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"template → repo\s+AGENTS\.md")
        self.assertRegex(out, r"template → repo\s+\.claude/settings\.json")
        self.assertNotIn("settings.local.json", out)
        self.assertFalse(self.exists(self.target_root, "AGENTS.md"), "dry-run must not write")

        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0)
        self.assertEqual(self.read(self.target_root, "AGENTS.md"), "hello")
        self.assertFalse(self.exists(self.target_root, ".claude/settings.local.json"))

    def test_second_sync_is_noop_when_nothing_changed(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "AGENTS.md", "hello")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        code, out = self.run_cli("sync", "1", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(NO_CHANGES, out)

    def test_template_edit_propagates_push(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, "a.md", "v2")
        code, out = self.run_cli("sync", "1")
        self.assertRegex(out, r"template → repo\s+a\.md")
        self.assertEqual(self.read(self.target_root, "a.md"), "v2")

    def test_target_edit_propagates_pullback(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(self.target_root, "a.md", "customized in target")
        code, out = self.run_cli("sync", "1")
        self.assertRegex(out, r"repo → template\s+a\.md")
        self.assertEqual(self.read(tpl, "a.md"), "customized in target")

    def test_dry_run_never_executes_merge_or_waits(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, "a.md", "template side")
        self.write(self.target_root, "a.md", "target side")
        self.wait_for_enter_calls = 0
        self.opened_editor_calls = []
        code, out = self.run_cli("sync", "1", "--dry-run")
        self.assertRegex(out, r"will merge\s+a\.md")
        self.assertEqual(self.read(tpl, "a.md"), "template side")
        self.assertEqual(self.read(self.target_root, "a.md"), "target side")
        self.assertEqual(self.wait_for_enter_calls, 0)
        self.assertEqual(self.opened_editor_calls, [])

    def test_conflicting_merge_writes_markers_to_both_sides_and_pends(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, "a.md", "template side")
        self.write(self.target_root, "a.md", "target side")
        self.wait_for_enter_calls = 0
        self.opened_editor_calls = []
        code, out = self.run_cli("sync", "1")
        # Markers remain after Enter, so the exit code must be non-zero rather than let the caller assume the conflict is resolved.
        self.assertEqual(code, 1)
        self.assertRegex(out, r"NEEDS REVIEW\s+a\.md")
        self.assertIn("conflict markers in both files", out)

        template_result = self.read(tpl, "a.md")
        target_result = self.read(self.target_root, "a.md")
        self.assertIn("<<<<<<<", template_result)
        self.assertIn("<<<<<<<", target_result)
        self.assertIn("template side", template_result)
        self.assertIn("target side", template_result)

        self.assertEqual(len(self.opened_editor_calls), 1)
        self.assertEqual(len(self.opened_editor_calls[0]), 2)
        self.assertEqual(self.wait_for_enter_calls, 1)

        # Markers remain, so no snapshot is saved: saving one would register the unresolved conflict as confirmed and it would never be reported again.
        code, out = self.run_cli("status")
        self.assertIn(": 1 pending", out)

    def test_conflicting_merge_saves_snapshot_once_markers_are_resolved(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, "a.md", "template side")
        self.write(self.target_root, "a.md", "target side")

        original_wait = self.module.wait_for_enter

        def resolve_then_wait(prompt):
            self.write(tpl, "a.md", "resolved")
            self.write(self.target_root, "a.md", "resolved")
            return original_wait(prompt)

        self.module.wait_for_enter = resolve_then_wait
        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0)

        code, out = self.run_cli("status")
        self.assertIn(": 0 pending", out)

    def test_clean_three_way_merge_writes_both_sides(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "alpha\nbeta\ngamma\n")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, "a.md", "ALPHA\nbeta\ngamma\n")
        self.write(self.target_root, "a.md", "alpha\nbeta\nGAMMA\n")
        self.wait_for_enter_calls = 0
        self.opened_editor_calls = []
        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"merged\s+a\.md")
        self.assertNotIn("<<<<<<<", out)

        expected = "ALPHA\nbeta\nGAMMA\n"
        self.assertEqual(self.read(tpl, "a.md"), expected)
        self.assertEqual(self.read(self.target_root, "a.md"), expected)
        self.assertEqual(self.wait_for_enter_calls, 1)
        self.assertEqual(self.opened_editor_calls, [])

    def test_customization_survives_clean_two_way_merge(self):
        """Each side is merged separately using its own side's snapshot as base, so a target customization doesn't resurface as a conflict. A single shared base would report these non-overlapping edits as a conflict."""
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "line1\nline2\nline3\n")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(self.target_root, "a.md", "line1\nline2\ncustomized\n")
        self.write(tpl, "a.md", "updated\nline2\nline3\n")
        self.wait_for_enter_calls = 0
        self.opened_editor_calls = []
        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"merged\s+a\.md")
        self.assertNotIn("<<<<<<<", out)

        expected = "updated\nline2\ncustomized\n"
        self.assertEqual(self.read(tpl, "a.md"), expected)
        self.assertEqual(self.read(self.target_root, "a.md"), expected)
        self.assertEqual(self.wait_for_enter_calls, 1)
        self.assertEqual(self.opened_editor_calls, [])

    def test_binary_file_both_changed_pends_without_merge(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.bin", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        with open(os.path.join(tpl, "a.bin"), "wb") as f:
            f.write(b"template\x00side")
        with open(os.path.join(self.target_root, "a.bin"), "wb") as f:
            f.write(b"target\x00side")

        self.wait_for_enter_calls = 0
        self.opened_editor_calls = []
        code, out = self.run_cli("sync", "1")
        self.assertRegex(out, r"NEEDS REVIEW\s+a\.bin\s+\(binary file changed on both sides\)")
        with open(os.path.join(tpl, "a.bin"), "rb") as f:
            self.assertEqual(f.read(), b"template\x00side")
        with open(os.path.join(self.target_root, "a.bin"), "rb") as f:
            self.assertEqual(f.read(), b"target\x00side")
        self.assertEqual(len(self.opened_editor_calls), 1)
        self.assertEqual(self.wait_for_enter_calls, 1)

        # Nothing was written, only held: must not register as confirmed, so the next sync reports it again.
        code, out = self.run_cli("status")
        self.assertIn(": 1 pending", out)

    def test_one_side_absent_pends_without_merge(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, "a.md", "template changed")
        os.remove(os.path.join(self.target_root, "a.md"))
        self.wait_for_enter_calls = 0
        self.opened_editor_calls = []
        code, out = self.run_cli("sync", "1")
        self.assertRegex(out, r"NEEDS REVIEW\s+a\.md\s+\(deleted on one side, edited on the other\)")
        self.assertEqual(self.read(tpl, "a.md"), "template changed")
        self.assertFalse(self.exists(self.target_root, "a.md"))
        self.assertEqual(self.wait_for_enter_calls, 1)

        # Same: nothing was written, so no snapshot is saved and it stays pending.
        code, out = self.run_cli("status")
        self.assertIn(": 1 pending", out)

    def test_hold_then_undo_pullback_leaves_snapshots_unequal(self):
        """The user reverts the pullback during the hold: after Enter the tool must read the current contents of both sides, not what was just written. This is how reverting registers a customization."""
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(self.target_root, "a.md", "customized in target")

        original_wait = self.module.wait_for_enter

        def undo_pullback_then_wait(prompt):
            self.write(tpl, "a.md", "v1")
            original_wait(prompt)

        self.module.wait_for_enter = undo_pullback_then_wait
        code, out = self.run_cli("sync", "1")
        self.assertRegex(out, r"repo → template\s+a\.md")

        ts = self.module.read_state(self.module.snapshot_dir("1", "template"), "a.md")
        ls = self.module.read_state(self.module.snapshot_dir("1", "target"), "a.md")
        self.assertEqual(ts[0], b"v1")
        self.assertEqual(ls[0], b"customized in target")
        self.assertNotEqual(ts, ls, "differing snapshots are how a customization is registered")

        # Converges to no action; the customization is kept
        code, out = self.run_cli("sync", "1", "--dry-run")
        self.assertIn(NO_CHANGES, out)
        self.assertEqual(self.read(tpl, "a.md"), "v1")
        self.assertEqual(self.read(self.target_root, "a.md"), "customized in target")

    def test_template_delete_propagates_to_target_and_clears_snapshot(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")
        self.assertTrue(self.exists(self.target_root, "a.md"))

        os.remove(os.path.join(tpl, "a.md"))
        code, out = self.run_cli("sync", "1")
        self.assertRegex(out, r"removed from repo\s+a\.md")
        self.assertFalse(self.exists(self.target_root, "a.md"))
        self.assertIsNone(self.module.read_state(self.module.snapshot_dir("1", "template"), "a.md"))
        self.assertIsNone(self.module.read_state(self.module.snapshot_dir("1", "target"), "a.md"))

        # No tombstone: a same-named file put back by the target repo is the target's own file
        self.write(self.target_root, "a.md", "user restored this")
        code, out = self.run_cli("sync", "1", "--dry-run")
        self.assertIn(NO_CHANGES, out)

    def test_target_delete_propagates_to_template_and_clears_snapshot(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        os.remove(os.path.join(self.target_root, "a.md"))
        code, out = self.run_cli("sync", "1")
        self.assertRegex(out, r"removed from template\s+a\.md")
        self.assertFalse(self.exists(tpl, "a.md"))
        self.assertIsNone(self.module.read_state(self.module.snapshot_dir("1", "template"), "a.md"))
        self.assertIsNone(self.module.read_state(self.module.snapshot_dir("1", "target"), "a.md"))

    def test_first_adopt_overwrites_target_with_template_version(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "template version")
        self.add_target("tpl")
        self.write(self.target_root, "a.md", "target pre-existing content")

        code, out = self.run_cli("sync", "1")
        self.assertRegex(out, r"template → repo\s+a\.md\s+\(first sync, repo version replaced\)")
        self.assertEqual(self.read(self.target_root, "a.md"), "template version")
        ts = self.module.read_state(self.module.snapshot_dir("1", "template"), "a.md")
        ls = self.module.read_state(self.module.snapshot_dir("1", "target"), "a.md")
        self.assertEqual(ts, ls)
        self.assertEqual(ts[0], b"template version")

    def test_target_only_file_is_ignored(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.write(self.target_root, "own.md", "target's own file")
        self.add_target("tpl")

        code, out = self.run_cli("sync", "1")
        self.assertNotIn("own.md", out)
        self.assertEqual(self.read(self.target_root, "own.md"), "target's own file")

    def test_executable_bit_is_synced(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "run.sh", "#!/bin/sh\necho hi\n", executable=True)
        self.add_target("tpl")
        self.run_cli("sync", "1")

        mode = os.stat(os.path.join(self.target_root, "run.sh")).st_mode
        self.assertTrue(mode & 0o111)

        os.chmod(os.path.join(self.target_root, "run.sh"), 0o644)
        code, out = self.run_cli("sync", "1")
        self.assertRegex(out, r"repo → template\s+run\.sh")
        self.assertFalse(
            os.stat(os.path.join(tpl, "run.sh")).st_mode & 0o111,
            "an executable bit change should propagate like a content change",
        )


class SymlinkTests(SyncTestCase):
    def test_symlink_file_is_followed_not_copied_as_link(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "shared_source.md", "shared content")
        os.symlink("shared_source.md", os.path.join(tpl, "shared_link.md"))
        self.add_target("tpl")

        code, out = self.run_cli("sync", "1")
        self.assertIn("shared_link.md", out)
        target_link_path = os.path.join(self.target_root, "shared_link.md")
        self.assertFalse(os.path.islink(target_link_path))
        self.assertEqual(self.read(self.target_root, "shared_link.md"), "shared content")

    def test_broken_symlink_is_reported_and_skipped(self):
        tpl = self.make_template("tpl")
        os.symlink("/no/such/file", os.path.join(tpl, "broken.md"))
        self.add_target("tpl")

        code, out = self.run_cli("sync", "1", "--dry-run")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"skipped\s+broken\.md\s+\(broken symlink\)")

    def test_circular_symlink_is_reported_and_skipped(self):
        tpl = self.make_template("tpl")
        loop_dir = os.path.join(tpl, "loopdir")
        os.makedirs(loop_dir)
        os.symlink(os.path.join(tpl, "loopdir"), os.path.join(loop_dir, "self"))
        self.add_target("tpl")

        code, out = self.run_cli("sync", "1", "--dry-run")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"skipped\s+loopdir/self\s+\(symlink loop\)")

    def test_pullback_writes_through_symlink_to_shared_source(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "shared_source.md", "shared content")
        os.symlink("shared_source.md", os.path.join(tpl, "shared_link.md"))
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(self.target_root, "shared_link.md", "edited in target")
        code, out = self.run_cli("sync", "1")
        self.assertRegex(out, r"repo → template\s+shared_link\.md")

        link_path = os.path.join(tpl, "shared_link.md")
        self.assertTrue(os.path.islink(link_path), "the link itself must not be replaced by a regular file")
        self.assertEqual(self.read(tpl, "shared_source.md"), "edited in target")

    def test_pullback_symlink_pointing_outside_template_scope_is_skipped(self):
        tpl = self.make_template("tpl")
        outside_file = os.path.join(self.workdir, "outside.md")
        with open(outside_file, "w", encoding="utf-8") as f:
            f.write("outside original")
        os.symlink(outside_file, os.path.join(tpl, "escape.md"))
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(self.target_root, "escape.md", "edited in target")
        code, out = self.run_cli("sync", "1")
        self.assertRegex(out, r"skipped\s+escape\.md\s+\(symlink points outside the template or repo\)")
        self.assertTrue(os.path.islink(os.path.join(tpl, "escape.md")))
        with open(outside_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "outside original", "file outside the scope must not be written")
        self.assertEqual(self.read(self.target_root, "escape.md"), "edited in target")

    def test_project_pullback_writes_through_symlink_into_shared(self):
        tpl = self.make_template("tpl")
        shared = self.module.SHARED_ROOT
        self.write(shared, "rules/a.md", "shared v1")
        os.makedirs(os.path.join(tpl, "rules"))
        os.symlink(os.path.join(shared, "rules", "a.md"), os.path.join(tpl, "rules", "a.md"))
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(self.target_root, "rules/a.md", "edited in target")
        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"repo → template\s+rules/a\.md")
        self.assertTrue(os.path.islink(os.path.join(tpl, "rules", "a.md")))
        self.assertEqual(self.read(shared, "rules/a.md"), "edited in target")

    def test_user_level_symlink_into_shared_is_out_of_scope(self):
        user_tpl = self.make_user_template()
        user_target = self.make_user_target()
        shared = self.module.SHARED_ROOT
        self.write(shared, "a.md", "shared v1")
        os.symlink(os.path.join(shared, "a.md"), os.path.join(user_tpl, "a.md"))
        self.run_cli("sync", "user")

        self.write(user_target, "a.md", "edited in user target")
        code, out = self.run_cli("sync", "user")
        self.assertRegex(out, r"skipped\s+a\.md\s+\(symlink points outside the template or ~/\.claude\)")
        self.assertEqual(self.read(shared, "a.md"), "shared v1")

    def test_merge_with_one_side_out_of_scope_writes_neither_side(self):
        tpl = self.make_template("tpl")
        outside_file = os.path.join(self.workdir, "outside.md")
        with open(outside_file, "w", encoding="utf-8") as f:
            f.write("a\nb\nc\n")
        os.symlink(outside_file, os.path.join(tpl, "escape.md"))
        self.add_target("tpl")
        self.run_cli("sync", "1")

        with open(outside_file, "w", encoding="utf-8") as f:
            f.write("A\nb\nc\n")
        self.write(self.target_root, "escape.md", "a\nb\nC\n")
        code, out = self.run_cli("sync", "1")
        self.assertRegex(out, r"skipped\s+escape\.md\s+\(symlink points outside the template or repo\)")
        self.assertEqual(self.read(self.target_root, "escape.md"), "a\nb\nC\n", "template side unwritable, so the target side must not get the merge result either")
        with open(outside_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "A\nb\nc\n")

    def test_delete_removes_only_the_symlink_not_its_real_file(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "shared_source.md", "shared content")
        os.symlink("shared_source.md", os.path.join(tpl, "shared_link.md"))
        self.add_target("tpl")
        self.run_cli("sync", "1")

        os.remove(os.path.join(tpl, "shared_link.md"))
        code, out = self.run_cli("sync", "1")
        self.assertRegex(out, r"removed from repo\s+shared_link\.md")
        self.assertFalse(self.exists(self.target_root, "shared_link.md"))
        # The real file is in scope with its own decision; deleting the link must not take it down
        self.assertEqual(self.read(tpl, "shared_source.md"), "shared content")
        self.assertEqual(self.read(self.target_root, "shared_source.md"), "shared content")

    def test_self_referential_symlink_is_reported_as_circular(self):
        tpl = self.make_template("tpl")
        # ELOOP at stat, distinct from a broken link to a nonexistent path
        os.symlink("loop.md", os.path.join(tpl, "loop.md"))
        self.add_target("tpl")

        code, out = self.run_cli("sync", "1", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"skipped\s+loop\.md\s+\(symlink loop\)")
        self.assertNotIn("broken symlink", out)


class ScanErrorTests(SyncTestCase):
    """I/O errors during the scan must abort the sync rather than scan less: the scan result is the template whitelist, so scanning less would declare those files nonexistent."""

    def setUp(self) -> None:
        super().setUp()
        if os.geteuid() == 0:
            self.skipTest("root is not restricted by directory permissions")

    def test_unlistable_template_dir_aborts_sync(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "sub/a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, "sub/b.md", "new file")
        sub = os.path.join(tpl, "sub")
        # Enterable but not listable: known paths stay readable, so only the scan error handling can catch it
        os.chmod(sub, 0o111)
        self.addCleanup(os.chmod, sub, 0o755)

        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 1, out)
        self.assertIn("error: sync aborted", out)
        self.assertFalse(self.exists(self.target_root, "sub/b.md"))

    def test_unlistable_template_dir_reported_as_error_in_status(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "sub/a.md", "v1")
        self.add_target("tpl")

        sub = os.path.join(tpl, "sub")
        os.chmod(sub, 0o111)
        self.addCleanup(os.chmod, sub, 0o755)

        code, out = self.run_cli("status")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"\(tpl\): error: ")
        self.assertNotIn("pending", out)

    def test_missing_template_dir_is_an_empty_tree_not_an_error(self):
        tpl = self.make_template("tpl")
        files, issues = self.module.scan_tree(os.path.join(tpl, "never_created"))
        self.assertEqual(files, {})
        self.assertEqual(issues, [])


class BackupTests(SyncTestCase):
    def test_backup_created_before_overwrite(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, "a.md", "v2")
        self.run_cli("sync", "1")

        backups = []
        for dirpath, _dirs, files in os.walk(self.module.BACKUP_ROOT):
            for name in files:
                backups.append(os.path.join(dirpath, name))
        self.assertEqual(len(backups), 1)
        with open(backups[0], "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "v1")

    def test_no_leftover_tmp_files_after_sync(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        for name in os.listdir(self.target_root):
            self.assertFalse(name.startswith(".sync-tmp-"))


class ConvergenceTests(SyncTestCase):
    def test_both_sides_delete_converges_without_repeating(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        os.remove(os.path.join(tpl, "a.md"))
        os.remove(os.path.join(self.target_root, "a.md"))
        code, out = self.run_cli("sync", "1")
        self.assertNotIn("a.md", out)
        self.assertIn(NO_CHANGES, out)
        self.assertIsNone(self.module.read_state(self.module.snapshot_dir("1", "template"), "a.md"))
        self.assertIsNone(self.module.read_state(self.module.snapshot_dir("1", "target"), "a.md"))

        code, out = self.run_cli("sync", "1", "--dry-run")
        self.assertIn(NO_CHANGES, out)
        code, out = self.run_cli("sync", "1", "--dry-run")
        self.assertIn(NO_CHANGES, out)

    def test_permission_error_aborts_instead_of_being_treated_as_deletion(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, "a.md", "v2")
        path = os.path.join(self.target_root, "a.md")
        os.chmod(self.target_root, 0o000)
        try:
            code, out = self.run_cli("sync", "1", "--dry-run")
        finally:
            os.chmod(self.target_root, 0o755)
        self.assertEqual(code, 1)
        self.assertIn("error:", out)


class CrossBaseMergeTests(SyncTestCase):
    """The three-way merge base is the opposite side's snapshot (crosswise). Once the snapshots differ (a known customization), a merge is required even if only one side moved: sending content straight over would overwrite the customization."""

    def test_template_only_update_merges_into_existing_customization(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "line1\nline2\nline3\n")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        # Create a customization via pullback then revert during the hold, leaving the two snapshots unequal
        self.write(self.target_root, "a.md", "line1\nline2\ncustomized\n")
        original_wait = self.module.wait_for_enter

        def undo_pullback_then_wait(prompt):
            self.write(tpl, "a.md", "line1\nline2\nline3\n")
            return original_wait(prompt)

        self.module.wait_for_enter = undo_pullback_then_wait
        self.run_cli("sync", "1")
        self.module.wait_for_enter = original_wait

        ts = self.module.read_state(self.module.snapshot_dir("1", "template"), "a.md")
        ls = self.module.read_state(self.module.snapshot_dir("1", "target"), "a.md")
        self.assertNotEqual(ts, ls)

        self.write(tpl, "a.md", "updated\nline2\nline3\n")
        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"merged\s+a\.md", "differing snapshots require a merge even when only one side changed")
        self.assertNotIn("template → repo", out, "a straight send would overwrite the target repo's customization")

        self.assertEqual(
            self.read(self.target_root, "a.md"),
            "updated\nline2\ncustomized\n",
        )
        self.assertEqual(
            self.read(tpl, "a.md"),
            "updated\nline2\nline3\n",
            "own-side snapshot as base would merge the target repo's customization into the template as a change; the crosswise base avoids polluting the template",
        )


class SettingsJsonMergeTests(SyncTestCase):
    def test_push_new_key_preserves_target_extra_key(self):
        tpl = self.make_template("tpl")
        self.write(tpl, ".claude/settings.json", json.dumps({"a": 1}))
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(self.target_root, ".claude/settings.json", json.dumps({"a": 1, "own": "mine"}))
        self.write(tpl, ".claude/settings.json", json.dumps({"a": 1, "b": 2}))
        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0)

        self.assertEqual(
            json.loads(self.read(self.target_root, ".claude/settings.json")),
            {"a": 1, "b": 2, "own": "mine"},
        )
        self.assertEqual(
            json.loads(self.read(tpl, ".claude/settings.json")),
            {"a": 1, "b": 2},
            "target-only keys must not pollute the template repo",
        )

    def test_conflicting_scalar_leaf_keeps_own_value_and_flags_it(self):
        tpl = self.make_template("tpl")
        self.write(tpl, ".claude/settings.json", json.dumps({"mode": "a"}))
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, ".claude/settings.json", json.dumps({"mode": "template-value"}))
        self.write(self.target_root, ".claude/settings.json", json.dumps({"mode": "target-value"}))
        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"NEEDS REVIEW\s+\.claude/settings\.json\s+\(conflicting keys: mode\)")
        self.assertEqual(json.loads(self.read(tpl, ".claude/settings.json")), {"mode": "template-value"})
        self.assertEqual(json.loads(self.read(self.target_root, ".claude/settings.json")), {"mode": "target-value"})

    def test_string_array_leaf_merges_as_a_set(self):
        tpl = self.make_template("tpl")
        self.write(tpl, ".claude/settings.json", json.dumps({"permissions": {"allow": ["Bash(git:*)"]}}))
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(
            tpl, ".claude/settings.json", json.dumps({"permissions": {"allow": ["Bash(git:*)", "Bash(ls:*)"]}})
        )
        self.write(
            self.target_root,
            ".claude/settings.json",
            json.dumps({"permissions": {"allow": ["Bash(git:*)", "Bash(find:*)"]}}),
        )
        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0)
        self.assertNotIn("conflicting keys", out)

        expected = {"Bash(git:*)", "Bash(ls:*)", "Bash(find:*)"}
        target_allow = json.loads(self.read(self.target_root, ".claude/settings.json"))["permissions"]["allow"]
        template_allow = json.loads(self.read(tpl, ".claude/settings.json"))["permissions"]["allow"]
        self.assertEqual(set(target_allow), expected)
        self.assertEqual(set(template_allow), expected)

    def test_mixed_type_array_conflict_falls_back_to_whole_leaf(self):
        tpl = self.make_template("tpl")
        self.write(tpl, ".claude/settings.json", json.dumps({"hooks": {"PostToolUse": [{"matcher": "Edit"}]}}))
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, ".claude/settings.json", json.dumps({"hooks": {"PostToolUse": [{"matcher": "Write"}]}}))
        self.write(
            self.target_root, ".claude/settings.json", json.dumps({"hooks": {"PostToolUse": [{"matcher": "Bash"}]}})
        )
        code, out = self.run_cli("sync", "1")
        self.assertRegex(out, r"NEEDS REVIEW\s+\.claude/settings\.json\s+\(conflicting keys: hooks\.PostToolUse\)")
        self.assertEqual(
            json.loads(self.read(tpl, ".claude/settings.json"))["hooks"]["PostToolUse"], [{"matcher": "Write"}]
        )
        self.assertEqual(
            json.loads(self.read(self.target_root, ".claude/settings.json"))["hooks"]["PostToolUse"],
            [{"matcher": "Bash"}],
        )

    def test_invalid_json_on_one_side_pends_without_merge(self):
        tpl = self.make_template("tpl")
        self.write(tpl, ".claude/settings.json", json.dumps({"a": 1}))
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, ".claude/settings.json", json.dumps({"a": 2}))
        self.write(self.target_root, ".claude/settings.json", "{not valid json")
        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"NEEDS REVIEW\s+\.claude/settings\.json\s+\(invalid JSON\)")
        self.assertEqual(self.read(self.target_root, ".claude/settings.json"), "{not valid json")

        code, out = self.run_cli("status")
        self.assertIn(": 1 pending", out, "nothing was written, so it must not register as confirmed")

    def test_leaf_deletion_propagates_without_tombstone(self):
        tpl = self.make_template("tpl")
        self.write(tpl, ".claude/settings.json", json.dumps({"a": 1, "b": 2}))
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, ".claude/settings.json", json.dumps({"a": 1}))
        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.read(self.target_root, ".claude/settings.json")), {"a": 1})

    def test_pullback_leaf_propagates_to_template_and_keeps_snapshots_equal(self):
        """A changed leaf must be written back to the template, not just update target_leaves: otherwise the snapshot is saved with the template untouched and the change registers as a customization that is never pulled back."""
        tpl = self.make_template("tpl")
        self.write(tpl, ".claude/settings.json", json.dumps({"model": "opus"}))
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(self.target_root, ".claude/settings.json", json.dumps({"model": "sonnet"}))
        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"repo → template\s+\.claude/settings\.json")
        self.assertEqual(
            json.loads(self.read(tpl, ".claude/settings.json")),
            {"model": "sonnet"},
        )

        ts = self.module.read_state(self.module.snapshot_dir("1", "template"), ".claude/settings.json")
        ls = self.module.read_state(self.module.snapshot_dir("1", "target"), ".claude/settings.json")
        self.assertEqual(ts, ls, "equal snapshots, or it would be mistaken for a customization")

        code, out = self.run_cli("status")
        self.assertIn("0 customized", out)

    def test_identical_new_key_on_both_sides_is_not_a_conflict(self):
        """Identical independent additions already agree and aren't a conflict, matching git merge-file on identical edits."""
        tpl = self.make_template("tpl")
        self.write(tpl, ".claude/settings.json", json.dumps({"a": 1}))
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, ".claude/settings.json", json.dumps({"a": 1, "newkey": "same"}))
        self.write(self.target_root, ".claude/settings.json", json.dumps({"a": 1, "newkey": "same"}))
        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0)
        self.assertNotIn("conflicting keys", out)
        self.assertRegex(out, r"merged\s+\.claude/settings\.json")
        self.assertEqual(json.loads(self.read(tpl, ".claude/settings.json")), {"a": 1, "newkey": "same"})
        self.assertEqual(
            json.loads(self.read(self.target_root, ".claude/settings.json")), {"a": 1, "newkey": "same"}
        )

    def test_empty_object_leaf_survives_round_trip(self):
        """A key written as an empty object ({"a": {}}) must survive flattening into leaves and writing back."""
        tpl = self.make_template("tpl")
        self.write(tpl, ".claude/settings.json", json.dumps({"a": {}, "b": 1}))
        self.add_target("tpl")
        self.run_cli("sync", "1")
        self.assertEqual(json.loads(self.read(self.target_root, ".claude/settings.json")), {"a": {}, "b": 1})
        self.assertEqual(json.loads(self.read(tpl, ".claude/settings.json")), {"a": {}, "b": 1})

        self.write(tpl, ".claude/settings.json", json.dumps({"a": {}, "b": 2}))
        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.read(self.target_root, ".claude/settings.json")), {"a": {}, "b": 2})


class UserSyncTests(SyncTestCase):
    def test_sync_user_pushes_template_files_and_ignores_runtime_data(self):
        user_tpl = self.make_user_template()
        self.write(user_tpl, "CLAUDE.md", "user rules")
        user_target = self.make_user_target()
        # Runtime data isn't in the template whitelist, so it is neither scanned nor touched
        self.write(user_target, "history.jsonl", "some runtime data")

        code, out = self.run_cli("sync", "user")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"template → ~/\.claude\s+CLAUDE\.md")
        self.assertEqual(self.read(user_target, "CLAUDE.md"), "user rules")
        self.assertEqual(self.read(user_target, "history.jsonl"), "some runtime data")

    def test_sync_user_shares_customization_model_with_project_level(self):
        user_tpl = self.make_user_template()
        self.write(user_tpl, "rules/ppt.md", "v1")
        self.make_user_target()
        self.run_cli("sync", "user")

        self.write(self.user_target_root, "rules/ppt.md", "customized")
        code, out = self.run_cli("sync", "user")
        self.assertRegex(out, r"~/\.claude → template\s+rules/ppt\.md")
        self.assertEqual(self.read(user_tpl, "rules/ppt.md"), "customized")

        ts = self.module.read_state(self.module.snapshot_dir("user", "template"), "rules/ppt.md")
        ls = self.module.read_state(self.module.snapshot_dir("user", "target"), "rules/ppt.md")
        self.assertEqual(ts, ls)

    def test_sync_user_missing_template_errors(self):
        self.make_user_target()
        code, out = self.run_cli("sync", "user")
        self.assertEqual(code, 1)
        self.assertIn("error: template not found", out)

    def test_sync_user_prints_backup_dir_when_holding(self):
        user_tpl = self.make_user_template()
        self.write(user_tpl, "a.md", "v1")
        self.make_user_target()
        self.run_cli("sync", "user")

        with open(os.path.join(user_tpl, "a.md"), "wb") as f:
            f.write(b"template\x00side")
        with open(os.path.join(self.user_target_root, "a.md"), "wb") as f:
            f.write(b"target\x00side")
        code, out = self.run_cli("sync", "user")
        self.assertIn("backed up to", out, "the user level has no git diff, so the hold must give a path to compare against")

    def test_status_includes_user_row_when_template_exists(self):
        user_tpl = self.make_user_template()
        self.write(user_tpl, "CLAUDE.md", "v1")
        self.make_user_target()
        code, out = self.run_cli("status")
        self.assertIn("[user] ", out)


class AdoptTests(SyncTestCase):
    def test_adopt_copies_target_file_into_template_and_establishes_snapshot(self):
        tpl = self.make_template("tpl")
        self.add_target("tpl")
        self.write(self.target_root, "brand_new.md", "target only content")

        code, out = self.run_cli("adopt", os.path.join(self.target_root, "brand_new.md"))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read(tpl, "brand_new.md"), "target only content")

        ts = self.module.read_state(self.module.snapshot_dir("1", "template"), "brand_new.md")
        ls = self.module.read_state(self.module.snapshot_dir("1", "target"), "brand_new.md")
        self.assertEqual(ts, ls)
        self.assertEqual(ts[0], b"target only content")

        code, out = self.run_cli("sync", "1", "--dry-run")
        self.assertNotIn("brand_new.md", out)

    def test_adopt_refuses_to_overwrite_existing_template_file(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "template version")
        self.add_target("tpl")
        self.write(self.target_root, "a.md", "target version")

        code, out = self.run_cli("adopt", os.path.join(self.target_root, "a.md"))
        self.assertEqual(code, 1)
        self.assertIn("template already has", out)
        self.assertEqual(self.read(tpl, "a.md"), "template version")

    def test_adopt_rejects_file_outside_any_registered_target(self):
        outside = os.path.join(self.workdir, "nope.md")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("x")
        code, out = self.run_cli("adopt", outside)
        self.assertEqual(code, 1)
        self.assertIn("is not inside a registered repo", out)

    def test_adopt_rejects_settings_local_json(self):
        tpl = self.make_template("tpl")
        self.add_target("tpl")
        self.write(self.target_root, ".claude/settings.local.json", "{}")

        code, out = self.run_cli("adopt", os.path.join(self.target_root, ".claude/settings.local.json"))
        self.assertEqual(code, 1)
        self.assertIn("excluded from sync", out)

    def test_adopt_rejects_ds_store(self):
        tpl = self.make_template("tpl")
        self.add_target("tpl")
        self.write(self.target_root, ".claude/.DS_Store", "junk")

        code, out = self.run_cli("adopt", os.path.join(self.target_root, ".claude/.DS_Store"))
        self.assertEqual(code, 1)
        self.assertIn("excluded from sync", out)


class ExcludedNameTests(SyncTestCase):
    """Paths matched by the template repo's .gitignore are excluded at any depth. setUp writes a `.DS_Store` rule; the tests verify the exclusion takes effect, not the rule."""

    def test_ds_store_is_not_pushed_or_pulled_back_at_any_depth(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.write(tpl, ".DS_Store", "template junk")
        self.write(tpl, ".claude/.DS_Store", "nested junk")
        self.add_target("tpl")
        self.write(self.target_root, ".DS_Store", "target junk")

        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0, out)
        self.assertNotIn(".DS_Store", out)
        self.assertFalse(self.exists(self.target_root, ".claude/.DS_Store"))
        self.assertEqual(self.read(self.target_root, ".DS_Store"), "target junk", "target's own file must not be overwritten")
        self.assertFalse(
            os.path.exists(os.path.join(self.module.snapshot_dir("1", "target"), ".DS_Store")),
        )

    def test_stale_snapshot_of_excluded_path_is_ignored(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        # Stale snapshots of an excluded path, with the file still present on both sides
        for side in ("template", "target"):
            self.write(self.module.snapshot_dir("1", side), ".DS_Store", "old junk")
        self.write(self.target_root, ".DS_Store", "old junk")

        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0, out)
        self.assertNotIn(".DS_Store", out)
        self.assertTrue(self.exists(self.target_root, ".DS_Store"), "stale snapshot must not count as deletion")

    def test_arbitrary_gitignore_pattern_is_honored_not_just_ds_store(self):
        """Exclusion follows the template repo's .gitignore itself, not a hard-coded name list."""
        with open(os.path.join(self.module.REPO_ROOT, ".gitignore"), "a", encoding="utf-8") as f:
            f.write("*.generated\n")
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.write(tpl, "cache.generated", "junk")
        self.add_target("tpl")

        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0, out)
        self.assertFalse(self.exists(self.target_root, "cache.generated"))
        self.assertTrue(self.exists(self.target_root, "a.md"))


class ForceFlagsTests(SyncTestCase):
    def test_force_template_overwrites_target_customization(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(self.target_root, "a.md", "target customized")
        self.write(tpl, "a.md", "template updated")
        code, out = self.run_cli("sync", "1", "--force-template")
        self.assertEqual(code, 0)
        self.assertIn("Warning:", out)
        self.assertEqual(self.read(self.target_root, "a.md"), "template updated")
        self.assertEqual(self.read(tpl, "a.md"), "template updated")

    def test_force_target_overwrites_template(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(self.target_root, "a.md", "target customized")
        self.write(tpl, "a.md", "template updated")
        code, out = self.run_cli("sync", "1", "--force-target")
        self.assertEqual(code, 0)
        self.assertIn("Warning:", out)
        self.assertEqual(self.read(tpl, "a.md"), "target customized")
        self.assertEqual(self.read(self.target_root, "a.md"), "target customized")

    def test_force_template_replaces_settings_json_whole(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "settings.json", json.dumps({"a": 1, "b": 1}))
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(self.target_root, "settings.json", json.dumps({"a": 1, "b": 2, "own": 1}))
        code, out = self.run_cli("sync", "1", "--force-template")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(self.read(tpl, "settings.json")), {"a": 1, "b": 1})
        self.assertEqual(json.loads(self.read(self.target_root, "settings.json")), {"a": 1, "b": 1})

    def test_force_target_replaces_settings_json_whole(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "settings.json", json.dumps({"a": 1, "b": 1}))
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, "settings.json", json.dumps({"a": 1, "b": 2, "new": 1}))
        code, out = self.run_cli("sync", "1", "--force-target")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(self.read(tpl, "settings.json")), {"a": 1, "b": 1})
        self.assertEqual(json.loads(self.read(self.target_root, "settings.json")), {"a": 1, "b": 1})

    def test_force_flags_are_mutually_exclusive(self):
        tpl = self.make_template("tpl")
        self.add_target("tpl")
        with self.assertRaises(SystemExit):
            self.run_cli("sync", "1", "--force-template", "--force-target")


class InteractiveSyncTests(SyncTestCase):
    def test_sync_without_target_errors_when_not_a_tty(self):
        tpl = self.make_template("tpl")
        self.add_target("tpl")
        code, out = self.run_cli("sync")
        self.assertEqual(code, 1)
        self.assertIn("no terminal to choose one", out)

    def test_sync_without_target_lets_user_pick_when_tty(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")

        answers = iter(["1", "y"])
        self.module.input = lambda prompt="": next(answers)

        class FakeStdin:
            def isatty(self):
                return True

        original_stdin = self.module.sys.stdin
        self.module.sys.stdin = FakeStdin()
        try:
            code, out = self.run_cli("sync")
        finally:
            self.module.sys.stdin = original_stdin

        self.assertEqual(code, 0)
        self.assertRegex(out, r"template → repo\s+a\.md")


class GitFailureTests(SyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")

        def failing_check_ignore(*_args):
            raise RuntimeError("git check-ignore failed: boom")

        self.module.gitignored_relpaths = failing_check_ignore

    def test_sync_reports_git_failure_as_error(self):
        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 1)
        self.assertIn("error: sync aborted: git check-ignore failed: boom", out)
        self.assertFalse(self.exists(self.target_root, "a.md"))

    def test_status_reports_git_failure_per_repo(self):
        code, out = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"\(tpl\): error: git check-ignore failed: boom")


class UnattendedHoldTests(SyncTestCase):
    """Non-interactive stdin must not auto-confirm held items. These cases need the real wait_for_enter/isatty, so they bypass setUp's fakes: a separate clean module with sys.stdin faked as non-tty."""

    def _run_fresh(self, argv):
        fresh = load_fresh_module()
        fresh.REPO_ROOT = self.module.REPO_ROOT
        fresh.PROJECTS_ROOT = self.module.PROJECTS_ROOT
        fresh.SHARED_ROOT = self.module.SHARED_ROOT
        fresh.STATE_ROOT = self.module.STATE_ROOT
        fresh.CONFIG_PATH = self.module.CONFIG_PATH
        fresh.SNAPSHOT_ROOT = self.module.SNAPSHOT_ROOT
        fresh.BACKUP_ROOT = self.module.BACKUP_ROOT
        fresh.USER_TEMPLATE_ROOT = self.module.USER_TEMPLATE_ROOT
        fresh.USER_TARGET_ROOT = self.module.USER_TARGET_ROOT

        class FakeStdin:
            def isatty(self):
                return False

        original_stdin = fresh.sys.stdin
        fresh.sys.stdin = FakeStdin()
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                code = fresh.main(argv)
            return code, buf.getvalue()
        finally:
            fresh.sys.stdin = original_stdin

    def test_non_tty_with_pending_conflict_withholds_snapshot_and_exits_nonzero(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, "a.md", "template side")
        self.write(self.target_root, "a.md", "target side")

        code, out = self._run_fresh(["sync", "1"])
        self.assertEqual(code, 1)
        self.assertIn("No terminal to confirm", out)

        # Markers are already written to both files; only snapshot registration is withheld, else the next sync would never find this unreviewed conflict.
        self.assertIn("<<<<<<<", self.read(tpl, "a.md"))
        self.assertIn("<<<<<<<", self.read(self.target_root, "a.md"))
        ts = self.module.read_state(self.module.snapshot_dir("1", "template"), "a.md")
        self.assertEqual(ts[0], b"v1", "snapshot must not be overwritten with marker content")

        code, out = self.run_cli("status")
        self.assertIn(": 1 pending", out)

    def test_non_tty_without_review_items_skips_prompt_and_succeeds(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")

        code, out = self._run_fresh(["sync", "1"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("Press Enter", out)
        self.assertNotIn("Revert anything", out)
        ts = self.module.read_state(self.module.snapshot_dir("1", "template"), "a.md")
        self.assertEqual(ts[0], b"v1")

    def test_non_tty_does_not_launch_editor(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, "a.md", "template side")
        self.write(self.target_root, "a.md", "target side")
        # A nonexistent editor makes any launch attempt show up in the output instead of opening something.
        with mock.patch.dict(os.environ, {"EDITOR": os.path.join(self.workdir, "no-such-editor")}):
            code, out = self._run_fresh(["sync", "1"])
        self.assertEqual(code, 1)
        self.assertNotIn("Couldn't start $EDITOR", out)
        self.assertNotIn("$EDITOR is not set", out)

    def test_non_tty_with_absent_side_pending_exits_nonzero(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, "a.md", "template changed")
        os.remove(os.path.join(self.target_root, "a.md"))

        code, out = self._run_fresh(["sync", "1"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
