"""scripts/sync.py 的端到端测试。

每个用例都在仓库 cwd 下的 tmp/ 里建一次性临时目录，把被测模块的 REPO_ROOT/PROJECTS_ROOT/STATE_ROOT/... 等模块级常量整体指向临时目录，绝不触碰真实的 projects/、.state/、.backup/ 或任何真实目标仓。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_PATH = os.path.join(REPO_ROOT, "scripts", "sync.py")
TMP_ROOT = os.path.join(REPO_ROOT, "tmp")


def load_fresh_module():
    """每个用例都独立加载一份模块，避免用例之间通过模块全局状态互相影响。"""
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
        self.module.REPO_ROOT = repo_root
        self.module.PROJECTS_ROOT = projects_root
        self.module.STATE_ROOT = os.path.join(repo_root, ".state")
        self.module.CONFIG_PATH = os.path.join(self.module.STATE_ROOT, "config.json")
        self.module.SNAPSHOT_ROOT = os.path.join(self.module.STATE_ROOT, "snapshots")
        self.module.BACKUP_ROOT = os.path.join(repo_root, ".backup")

        # 用户级模板根、落盘根也做成可替换常量，指到 tmp/ 下的临时目录；不预先创建，大多数用例不需要用户级，cmd_status/cmd_sync 靠 os.path.isdir 判断用户级模板是否存在。
        self.user_template_root = os.path.join(self.workdir, "user_template")
        self.user_target_root = os.path.join(self.workdir, "user_target")
        self.module.USER_TEMPLATE_ROOT = self.user_template_root
        self.module.USER_TARGET_ROOT = self.user_target_root

        self.target_root = os.path.join(self.workdir, "target")
        os.makedirs(self.target_root)

        # 挂起交互里「开编辑器」「等回车」换成记录调用的假实现：真实实现会用 $EDITOR 起子进程、等 stdin，测试环境里两者都不能碰。这里的假 wait_for_enter 代表「有个真人在场，按下了回车」，返回 True——真实实现里 True/False 表示是否真的等到了人工确认，非交互场景下的行为由专门的测试用真实实现验证（见 UnattendedHoldTests）。
        self.opened_editor_calls: list[list[str]] = []
        self.wait_for_enter_calls = 0

        def fake_open_editor(paths):
            self.opened_editor_calls.append(list(paths))

        def fake_wait_for_enter():
            self.wait_for_enter_calls += 1
            return True

        self.module.open_editor = fake_open_editor
        self.module.wait_for_enter = fake_wait_for_enter

    # ---- 小工具 ----

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
        self.assertIn("模板不存在", out)

    def test_add_rejects_missing_target_dir(self):
        self.make_template("tpl")
        missing = os.path.join(self.workdir, "does-not-exist")
        code, out = self.run_cli("add", missing, "--template", "tpl")
        self.assertEqual(code, 1)
        self.assertIn("不存在", out)

    def test_add_rejects_duplicate_path(self):
        self.make_template("tpl")
        self.add_target("tpl")
        code, out = self.run_cli("add", self.target_root, "--template", "tpl")
        self.assertEqual(code, 1)
        self.assertIn("已登记", out)

    def test_sync_unregistered_target_errors(self):
        self.make_template("tpl")
        code, out = self.run_cli("sync", self.target_root, "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("未登记", out)

    def test_resolve_target_by_path_or_id(self):
        self.make_template("tpl")
        self.write(os.path.join(self.module.PROJECTS_ROOT, "tpl"), "a.md", "x")
        self.add_target("tpl")
        code1, out1 = self.run_cli("sync", "1", "--dry-run")
        code2, out2 = self.run_cli("sync", self.target_root, "--dry-run")
        self.assertEqual(code1, 0)
        self.assertEqual(code2, 0)
        self.assertIn("下发", out1)
        self.assertIn("下发", out2)


class StatusCommandTests(SyncTestCase):
    def test_status_no_targets(self):
        code, out = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("尚未登记", out)

    def test_status_counts_pending_and_customized(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "template v1")
        self.add_target("tpl")

        code, out = self.run_cli("status")
        self.assertIn("待同步=1", out)
        self.assertIn("定制=0", out)

        self.run_cli("sync", "1")
        code, out = self.run_cli("status")
        self.assertIn("待同步=0", out)
        self.assertIn("定制=0", out)

        # 两侧各自偏离快照 -> needs_merge，不计入定制（定制只看快照对本身）
        self.write(tpl, "a.md", "template v2")
        self.write(self.target_root, "a.md", "target v2")
        code, out = self.run_cli("status")
        self.assertIn("待同步=1", out)
        self.assertIn("定制=0", out)


class SyncDecisionTableTests(SyncTestCase):
    def test_first_sync_pushes_new_files_and_skips_settings_local(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "AGENTS.md", "hello")
        self.write(tpl, ".claude/settings.json", '{"a":1}')
        self.write(tpl, ".claude/settings.local.json", '{"local":true}')
        self.add_target("tpl")

        code, out = self.run_cli("sync", "1", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("[下发] AGENTS.md", out)
        self.assertIn("[下发] .claude/settings.json", out)
        self.assertNotIn("settings.local.json", out)
        self.assertFalse(self.exists(self.target_root, "AGENTS.md"), "dry-run 不应写入")

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
        self.assertIn("共 0 个文件有动作", out)

    def test_template_edit_propagates_push(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, "a.md", "v2")
        code, out = self.run_cli("sync", "1")
        self.assertIn("[下发] a.md", out)
        self.assertEqual(self.read(self.target_root, "a.md"), "v2")

    def test_target_edit_propagates_pullback(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(self.target_root, "a.md", "customized in target")
        code, out = self.run_cli("sync", "1")
        self.assertIn("[回流] a.md", out)
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
        self.assertIn("需要合并", out)
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
        # 回车之后文件仍带着冲突标记（用户没有真的处理），退出码必须非零：命令不能悄悄成功退出，让调用方以为冲突已经解决。
        self.assertEqual(code, 1)
        self.assertIn("合并-冲突", out)

        template_result = self.read(tpl, "a.md")
        target_result = self.read(self.target_root, "a.md")
        self.assertIn("<<<<<<<", template_result)
        self.assertIn("<<<<<<<", target_result)
        self.assertIn("template side", template_result)
        self.assertIn("target side", template_result)

        # 一次同步只挂起一次：编辑器打开两侧文件，CLI 等了恰好一次回车
        self.assertEqual(len(self.opened_editor_calls), 1)
        self.assertEqual(len(self.opened_editor_calls[0]), 2)
        self.assertEqual(self.wait_for_enter_calls, 1)

        # 回车之后文件此刻仍带着冲突标记（用户没有真的处理），不落快照：落了就等于把一个没解决的冲突静默登记成「已确认」，下次不再报。
        code, out = self.run_cli("status")
        self.assertIn("待同步=1", out)

    def test_conflicting_merge_saves_snapshot_once_markers_are_resolved(self):
        """挂起期间用户真的把冲突标记清掉之后，回车才应该落快照、判定收敛。"""
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, "a.md", "template side")
        self.write(self.target_root, "a.md", "target side")

        original_wait = self.module.wait_for_enter

        def resolve_then_wait():
            # 模拟用户在挂起期间手工解决了冲突
            self.write(tpl, "a.md", "resolved")
            self.write(self.target_root, "a.md", "resolved")
            return original_wait()

        self.module.wait_for_enter = resolve_then_wait
        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0)

        code, out = self.run_cli("status")
        self.assertIn("待同步=0", out)

    def test_clean_three_way_merge_writes_both_sides(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "alpha\nbeta\ngamma\n")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        # 同一轮内两侧各自改了不重叠的行，不该被当成冲突
        self.write(tpl, "a.md", "ALPHA\nbeta\ngamma\n")
        self.write(self.target_root, "a.md", "alpha\nbeta\nGAMMA\n")
        self.wait_for_enter_calls = 0
        self.opened_editor_calls = []
        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0)
        self.assertIn("合并-干净", out)
        self.assertNotIn("<<<<<<<", out)

        expected = "ALPHA\nbeta\nGAMMA\n"
        self.assertEqual(self.read(tpl, "a.md"), expected)
        self.assertEqual(self.read(self.target_root, "a.md"), expected)
        self.assertEqual(self.wait_for_enter_calls, 1)
        self.assertEqual(self.opened_editor_calls, [])

    def test_customization_survives_clean_two_way_merge(self):
        """两侧都偏离快照时必须各自以自己那侧快照为 base 分别合并：目标仓的定制不能被当作要跟模板仓「对齐」的冲突重新抛出来。用单一 base 合并会把这种不重叠的改动错误地报成冲突。"""
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "line1\nline2\nline3\n")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        # 同一轮内：目标仓第一次定制第三行，模板仓同时更新第一行，两处不重叠
        self.write(self.target_root, "a.md", "line1\nline2\ncustomized\n")
        self.write(tpl, "a.md", "updated\nline2\nline3\n")
        self.wait_for_enter_calls = 0
        self.opened_editor_calls = []
        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0)
        self.assertIn("合并-干净", out)
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
        self.assertIn("跳过（非文本）", out)
        with open(os.path.join(tpl, "a.bin"), "rb") as f:
            self.assertEqual(f.read(), b"template\x00side")
        with open(os.path.join(self.target_root, "a.bin"), "rb") as f:
            self.assertEqual(f.read(), b"target\x00side")
        self.assertEqual(len(self.opened_editor_calls), 1)
        self.assertEqual(self.wait_for_enter_calls, 1)

        # 工具什么都没写，只是挂起：不能把这个未处理的冲突静默登记成「已确认」，下次同步必须还报出来
        code, out = self.run_cli("status")
        self.assertIn("待同步=1", out)

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
        self.assertIn("跳过（一侧缺席）", out)
        self.assertEqual(self.read(tpl, "a.md"), "template changed")
        self.assertFalse(self.exists(self.target_root, "a.md"))
        self.assertEqual(self.wait_for_enter_calls, 1)

        # 同上：一侧缺席也没有真的写入任何东西，快照不能落，下次还要再报
        code, out = self.run_cli("status")
        self.assertIn("待同步=1", out)

    def test_hold_then_undo_pullback_leaves_snapshots_unequal(self):
        """挂起期间用户在模板仓把回流撤销掉：回车之后必须读两侧此刻的实际内容，而不是刚写完时的内容——这正是「撤销即登记」定制的方式。"""
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(self.target_root, "a.md", "customized in target")

        original_wait = self.module.wait_for_enter

        def undo_pullback_then_wait():
            # 模拟用户在挂起期间 git checkout 撤掉了这次回流
            self.write(tpl, "a.md", "v1")
            original_wait()

        self.module.wait_for_enter = undo_pullback_then_wait
        code, out = self.run_cli("sync", "1")
        self.assertIn("[回流] a.md", out)

        ts = self.module.read_state(self.module.snapshot_dir("1", "template"), "a.md")
        ls = self.module.read_state(self.module.snapshot_dir("1", "target"), "a.md")
        self.assertEqual(ts[0], b"v1")
        self.assertEqual(ls[0], b"customized in target")
        self.assertNotEqual(ts, ls, "撤销后两侧快照应该不等，这就是定制的登记方式")

        # 下次同步：模板侧相对快照没变（撤销后仍是 v1），目标侧也没变，应该收敛为无动作，不会把定制清掉
        code, out = self.run_cli("sync", "1", "--dry-run")
        self.assertIn("共 0 个文件有动作", out)
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
        self.assertIn("[删除-目标仓] a.md", out)
        self.assertFalse(self.exists(self.target_root, "a.md"))
        self.assertIsNone(self.module.read_state(self.module.snapshot_dir("1", "template"), "a.md"))
        self.assertIsNone(self.module.read_state(self.module.snapshot_dir("1", "target"), "a.md"))

        # 墓碑不留：目标仓自己再放一个同名文件回去，工具应视其为目标仓自有文件
        self.write(self.target_root, "a.md", "user restored this")
        code, out = self.run_cli("sync", "1", "--dry-run")
        self.assertIn("共 0 个文件有动作", out)

    def test_target_delete_propagates_to_template_and_clears_snapshot(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        os.remove(os.path.join(self.target_root, "a.md"))
        code, out = self.run_cli("sync", "1")
        self.assertIn("[删除-模板仓] a.md", out)
        self.assertFalse(self.exists(tpl, "a.md"))
        self.assertIsNone(self.module.read_state(self.module.snapshot_dir("1", "template"), "a.md"))
        self.assertIsNone(self.module.read_state(self.module.snapshot_dir("1", "target"), "a.md"))

    def test_first_adopt_overwrites_target_with_template_version(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "template version")
        self.add_target("tpl")
        # 在没有任何快照的情况下，目标仓已经自带一份不同内容
        self.write(self.target_root, "a.md", "target pre-existing content")

        code, out = self.run_cli("sync", "1")
        self.assertIn("[首次接管] a.md", out)
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
        self.assertTrue(mode & 0o111, "可执行位应该被同步过去")

        # 只改可执行位、内容不变，也应该视为一次改动并回流
        os.chmod(os.path.join(self.target_root, "run.sh"), 0o644)
        code, out = self.run_cli("sync", "1")
        self.assertIn("[回流] run.sh", out)
        self.assertFalse(
            os.stat(os.path.join(tpl, "run.sh")).st_mode & 0o111,
            "可执行位变化应该当作一次内容改动传播过去",
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
        self.assertFalse(os.path.islink(target_link_path), "目标仓拿到的应该是内容副本，不是链接")
        self.assertEqual(self.read(self.target_root, "shared_link.md"), "shared content")

    def test_broken_symlink_is_reported_and_skipped(self):
        tpl = self.make_template("tpl")
        os.symlink("/no/such/file", os.path.join(tpl, "broken.md"))
        self.add_target("tpl")

        code, out = self.run_cli("sync", "1", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("[断链，跳过] broken.md", out)

    def test_circular_symlink_is_reported_and_skipped(self):
        tpl = self.make_template("tpl")
        loop_dir = os.path.join(tpl, "loopdir")
        os.makedirs(loop_dir)
        os.symlink(os.path.join(tpl, "loopdir"), os.path.join(loop_dir, "self"))
        self.add_target("tpl")

        code, out = self.run_cli("sync", "1", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("循环链接", out)

    def test_pullback_writes_through_symlink_to_shared_source(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "shared_source.md", "shared content")
        os.symlink("shared_source.md", os.path.join(tpl, "shared_link.md"))
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(self.target_root, "shared_link.md", "edited in target")
        code, out = self.run_cli("sync", "1")
        self.assertIn("[回流] shared_link.md", out)

        link_path = os.path.join(tpl, "shared_link.md")
        self.assertTrue(os.path.islink(link_path), "链接本身不该被替换成普通文件")
        self.assertEqual(self.read(tpl, "shared_source.md"), "edited in target")

    def test_pullback_symlink_pointing_outside_template_scope_is_skipped(self):
        tpl = self.make_template("tpl")
        outside_file = os.path.join(self.workdir, "outside.md")
        with open(outside_file, "w", encoding="utf-8") as f:
            f.write("outside original")
        os.symlink(outside_file, os.path.join(tpl, "escape.md"))
        self.add_target("tpl")
        self.run_cli("sync", "1")  # 首次同步：把跟随后的内容当普通文件推给目标仓

        self.write(self.target_root, "escape.md", "edited in target")
        code, out = self.run_cli("sync", "1")
        self.assertIn("链接目标超出同步范围", out)
        self.assertTrue(os.path.islink(os.path.join(tpl, "escape.md")), "链接本身不该被动")
        with open(outside_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "outside original", "范围外的真实文件不该被写入")
        self.assertEqual(self.read(self.target_root, "escape.md"), "edited in target")

    def test_delete_removes_only_the_symlink_not_its_real_file(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "shared_source.md", "shared content")
        os.symlink("shared_source.md", os.path.join(tpl, "shared_link.md"))
        self.add_target("tpl")
        self.run_cli("sync", "1")

        os.remove(os.path.join(tpl, "shared_link.md"))
        code, out = self.run_cli("sync", "1")
        self.assertIn("[删除-目标仓] shared_link.md", out)
        self.assertFalse(self.exists(self.target_root, "shared_link.md"))
        # 真实文件本身仍在同步范围内，有自己的一条判定管着，不该被链接的删除连带
        self.assertEqual(self.read(tpl, "shared_source.md"), "shared content")
        self.assertEqual(self.read(self.target_root, "shared_source.md"), "shared content")

    def test_self_referential_symlink_is_reported_as_circular(self):
        tpl = self.make_template("tpl")
        # 指向自己的链接在 stat 这一步就以 ELOOP 失败，跟指向不存在路径的断链不是一回事
        os.symlink("loop.md", os.path.join(tpl, "loop.md"))
        self.add_target("tpl")

        code, out = self.run_cli("sync", "1", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("循环链接", out)
        self.assertNotIn("断链", out)


class ScanErrorTests(SyncTestCase):
    """扫描阶段遇到权限一类的真 I/O 错误必须中止，不能静默少扫：扫描结果就是模板侧的白名单，少扫等于宣告那些文件不存在。"""

    def setUp(self) -> None:
        super().setUp()
        if os.geteuid() == 0:
            self.skipTest("root 不受目录权限限制")

    def test_unlistable_template_dir_aborts_sync(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "sub/a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, "sub/b.md", "new file")
        sub = os.path.join(tpl, "sub")
        # 可进入、不可列举：已知路径照样读得到，只有扫描会失败，因此只有扫描这一处的错误处理能兜住
        os.chmod(sub, 0o111)
        self.addCleanup(os.chmod, sub, 0o755)

        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 1, out)
        self.assertIn("同步中止", out)
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
        self.assertIn("错误", out)
        self.assertNotIn("待同步=", out)

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
            self.assertEqual(f.read(), "v1", "备份应该是被覆盖之前的旧内容")

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
        self.assertIn("两侧均已删除", out)
        self.assertIsNone(self.module.read_state(self.module.snapshot_dir("1", "template"), "a.md"))
        self.assertIsNone(self.module.read_state(self.module.snapshot_dir("1", "target"), "a.md"))

        # 收敛：不会一遍遍重复报同一条「需要合并」
        code, out = self.run_cli("sync", "1", "--dry-run")
        self.assertIn("共 0 个文件有动作", out)
        code, out = self.run_cli("sync", "1", "--dry-run")
        self.assertIn("共 0 个文件有动作", out)

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
        self.assertIn("错误", out)


class CrossBaseMergeTests(SyncTestCase):
    """三方合并的 base 必须交叉取（对侧快照），且两侧快照一旦不等（存在已知定制），哪怕只有一侧偏离也要走合并——直送会把定制覆盖掉。"""

    def test_template_only_update_merges_into_existing_customization(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "line1\nline2\nline3\n")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        # 用「回流后在挂起期间撤销」的方式建立一次真实定制：目标仓改第三行，模板仓在挂起期间把这次回流撤销回旧内容，两侧快照就此不再相等
        self.write(self.target_root, "a.md", "line1\nline2\ncustomized\n")
        original_wait = self.module.wait_for_enter

        def undo_pullback_then_wait():
            self.write(tpl, "a.md", "line1\nline2\nline3\n")
            return original_wait()

        self.module.wait_for_enter = undo_pullback_then_wait
        self.run_cli("sync", "1")
        self.module.wait_for_enter = original_wait

        ts = self.module.read_state(self.module.snapshot_dir("1", "template"), "a.md")
        ls = self.module.read_state(self.module.snapshot_dir("1", "target"), "a.md")
        self.assertNotEqual(ts, ls, "此刻应已存在定制：两侧快照不等")

        # 模板侧做一处不相干的更新（改第一行），目标侧不动
        self.write(tpl, "a.md", "updated\nline2\nline3\n")
        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0)
        self.assertIn("合并", out, "两侧快照不等时，哪怕只有一侧变化也要走合并，不能直送")
        self.assertNotIn("[下发]", out, "不能走「只有一侧变化就直送」的快速路径，那会覆盖掉目标仓的定制")

        self.assertEqual(
            self.read(self.target_root, "a.md"),
            "updated\nline2\ncustomized\n",
            "目标仓应同时保有既有定制和模板的新更新",
        )
        self.assertEqual(
            self.read(tpl, "a.md"),
            "updated\nline2\nline3\n",
            "base 取本侧快照会把目标仓的定制当成变化合并进模板仓；取对侧（交叉）快照才不会污染模板仓",
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
            "目标仓自己加的键不该污染模板仓",
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
        self.assertIn("键级合并-冲突", out)
        self.assertIn("mode", out)
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
        self.assertNotIn("键级合并-冲突", out)

        expected = {"Bash(git:*)", "Bash(ls:*)", "Bash(find:*)"}
        target_allow = json.loads(self.read(self.target_root, ".claude/settings.json"))["permissions"]["allow"]
        template_allow = json.loads(self.read(tpl, ".claude/settings.json"))["permissions"]["allow"]
        self.assertEqual(set(target_allow), expected, "目标仓自己加的元素得保留，模板新增的元素得并入")
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
        self.assertIn("键级合并-冲突", out)
        self.assertIn("hooks.PostToolUse", out)
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
        self.assertIn("键级合并-跳过（无法解析）", out)
        self.assertEqual(self.read(self.target_root, ".claude/settings.json"), "{not valid json")

        code, out = self.run_cli("status")
        self.assertIn("待同步=1", out, "什么都没写，不能把它登记成已确认")

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
        """目标仓改的托管叶子必须真的写回模板仓，而不是只更新 target_leaves：否则模板侧原样不动，快照却已落，这处改动就被静默登记成「定制」，以后再也不会回流。"""
        tpl = self.make_template("tpl")
        self.write(tpl, ".claude/settings.json", json.dumps({"model": "opus"}))
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(self.target_root, ".claude/settings.json", json.dumps({"model": "sonnet"}))
        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0)
        self.assertIn("键级合并-干净", out)
        self.assertEqual(
            json.loads(self.read(tpl, ".claude/settings.json")),
            {"model": "sonnet"},
            "模板侧的值应该真的跟着目标仓的改动回流",
        )

        ts = self.module.read_state(self.module.snapshot_dir("1", "template"), ".claude/settings.json")
        ls = self.module.read_state(self.module.snapshot_dir("1", "target"), ".claude/settings.json")
        self.assertEqual(ts, ls, "回流之后两侧快照应该相等，不能被误登记成定制")

        code, out = self.run_cli("status")
        self.assertIn("定制=0", out)

    def test_identical_new_key_on_both_sides_is_not_a_conflict(self):
        """两侧独立加上同一个键、同样的值，值本来就一致，没有要协调的东西，不该被报成冲突——跟整文件级两侧做相同修改时 git merge-file 干净合并、不报冲突的行为一致。"""
        tpl = self.make_template("tpl")
        self.write(tpl, ".claude/settings.json", json.dumps({"a": 1}))
        self.add_target("tpl")
        self.run_cli("sync", "1")

        self.write(tpl, ".claude/settings.json", json.dumps({"a": 1, "newkey": "same"}))
        self.write(self.target_root, ".claude/settings.json", json.dumps({"a": 1, "newkey": "same"}))
        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0)
        self.assertNotIn("键级合并-冲突", out)
        self.assertIn("键级合并-干净", out)
        self.assertEqual(json.loads(self.read(tpl, ".claude/settings.json")), {"a": 1, "newkey": "same"})
        self.assertEqual(
            json.loads(self.read(self.target_root, ".claude/settings.json")), {"a": 1, "newkey": "same"}
        )

    def test_empty_object_leaf_survives_round_trip(self):
        """{"a": {}} 这样原本就写成空对象的键，展开成叶子字典再写回对象时不该被静默吃掉。"""
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
        # ~/.claude 下的运行时数据不在模板白名单里，天然不该被扫描、不该被动
        self.write(user_target, "history.jsonl", "some runtime data")

        code, out = self.run_cli("sync", "user")
        self.assertEqual(code, 0)
        self.assertIn("[下发] CLAUDE.md", out)
        self.assertEqual(self.read(user_target, "CLAUDE.md"), "user rules")
        self.assertEqual(self.read(user_target, "history.jsonl"), "some runtime data")

    def test_sync_user_shares_customization_model_with_project_level(self):
        user_tpl = self.make_user_template()
        self.write(user_tpl, "rules/ppt.md", "v1")
        self.make_user_target()
        self.run_cli("sync", "user")

        self.write(self.user_target_root, "rules/ppt.md", "customized")
        code, out = self.run_cli("sync", "user")
        self.assertIn("[回流] rules/ppt.md", out)
        self.assertEqual(self.read(user_tpl, "rules/ppt.md"), "customized")

        ts = self.module.read_state(self.module.snapshot_dir("user", "template"), "rules/ppt.md")
        ls = self.module.read_state(self.module.snapshot_dir("user", "target"), "rules/ppt.md")
        self.assertEqual(ts, ls)

    def test_sync_user_missing_template_errors(self):
        self.make_user_target()
        code, out = self.run_cli("sync", "user")
        self.assertEqual(code, 1)
        self.assertIn("错误", out)

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
        self.assertIn("本次备份目录", out, "用户级没有 git diff 可用，挂起时要给一条能比对现状的路径")

    def test_status_includes_user_row_when_template_exists(self):
        user_tpl = self.make_user_template()
        self.write(user_tpl, "CLAUDE.md", "v1")
        self.make_user_target()
        code, out = self.run_cli("status")
        self.assertIn("user\t", out)


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
        self.assertNotIn("brand_new.md", out, "建立快照对之后不该再把它当新文件报出来")

    def test_adopt_refuses_to_overwrite_existing_template_file(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "template version")
        self.add_target("tpl")
        self.write(self.target_root, "a.md", "target version")

        code, out = self.run_cli("adopt", os.path.join(self.target_root, "a.md"))
        self.assertEqual(code, 1)
        self.assertIn("已有同名文件", out)
        self.assertEqual(self.read(tpl, "a.md"), "template version")

    def test_adopt_rejects_file_outside_any_registered_target(self):
        outside = os.path.join(self.workdir, "nope.md")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("x")
        code, out = self.run_cli("adopt", outside)
        self.assertEqual(code, 1)
        self.assertIn("不属于任何已登记", out)

    def test_adopt_rejects_settings_local_json(self):
        tpl = self.make_template("tpl")
        self.add_target("tpl")
        self.write(self.target_root, ".claude/settings.local.json", "{}")

        code, out = self.run_cli("adopt", os.path.join(self.target_root, ".claude/settings.local.json"))
        self.assertEqual(code, 1)
        self.assertIn("不参与同步", out)

    def test_adopt_rejects_ds_store(self):
        tpl = self.make_template("tpl")
        self.add_target("tpl")
        self.write(self.target_root, ".claude/.DS_Store", "junk")

        code, out = self.run_cli("adopt", os.path.join(self.target_root, ".claude/.DS_Store"))
        self.assertEqual(code, 1)
        self.assertIn("不参与同步", out)


class ExcludedNameTests(SyncTestCase):
    """`.DS_Store` 一类的自动生成文件在任何一层都不参与同步。"""

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
        self.assertEqual(self.read(self.target_root, ".DS_Store"), "target junk", "目标仓自己的不该被覆盖")
        self.assertFalse(
            os.path.exists(os.path.join(self.module.snapshot_dir("1", "target"), ".DS_Store")),
            "被排除的路径不该留下快照",
        )

    def test_stale_snapshot_of_excluded_path_is_ignored(self):
        tpl = self.make_template("tpl")
        self.write(tpl, "a.md", "v1")
        self.add_target("tpl")
        self.run_cli("sync", "1")

        # 排除规则生效之前存下的快照：那时两侧都同步过 .DS_Store，现在两侧都还留着文件
        for side in ("template", "target"):
            self.write(self.module.snapshot_dir("1", side), ".DS_Store", "old junk")
        self.write(self.target_root, ".DS_Store", "old junk")

        code, out = self.run_cli("sync", "1")
        self.assertEqual(code, 0, out)
        self.assertNotIn(".DS_Store", out)
        self.assertTrue(self.exists(self.target_root, ".DS_Store"), "旧快照不该把它判成删除")


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
        self.assertIn("警告", out)
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
        self.assertIn("警告", out)
        self.assertEqual(self.read(tpl, "a.md"), "target customized")
        self.assertEqual(self.read(self.target_root, "a.md"), "target customized")

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
        self.assertIn("不是交互终端", out)

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
        self.assertIn("[下发] a.md", out)


class UnattendedHoldTests(SyncTestCase):
    """stdin 不是交互终端时，有挂起项就不能悄悄自动确认——这类场景绕不开真实的 wait_for_enter/sys.stdin.isatty，所以这里不用 setUp 里那个整体替换掉的假实现，而是单独加载一份干净模块，只换掉会真的弹窗口的 open_editor，并把 sys.stdin 换成一个 isatty() 返回 False 的假对象。"""

    def _run_fresh(self, argv):
        fresh = load_fresh_module()
        fresh.REPO_ROOT = self.module.REPO_ROOT
        fresh.PROJECTS_ROOT = self.module.PROJECTS_ROOT
        fresh.STATE_ROOT = self.module.STATE_ROOT
        fresh.CONFIG_PATH = self.module.CONFIG_PATH
        fresh.SNAPSHOT_ROOT = self.module.SNAPSHOT_ROOT
        fresh.BACKUP_ROOT = self.module.BACKUP_ROOT
        fresh.USER_TEMPLATE_ROOT = self.module.USER_TEMPLATE_ROOT
        fresh.USER_TARGET_ROOT = self.module.USER_TARGET_ROOT
        fresh.open_editor = lambda paths: None

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
        self.assertIn("不是交互终端", out)

        # 合并本身已经把冲突标记写进了两侧文件——只有「登记快照」这一步被刻意扣住，不然下次同步就再也发现不了这处还没人看过的冲突
        self.assertIn("<<<<<<<", self.read(tpl, "a.md"))
        self.assertIn("<<<<<<<", self.read(self.target_root, "a.md"))
        ts = self.module.read_state(self.module.snapshot_dir("1", "template"), "a.md")
        self.assertEqual(ts[0], b"v1", "快照应该保持原样，不能被写入带冲突标记的内容")

        code, out = self.run_cli("status")
        self.assertIn("待同步=1", out)

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
