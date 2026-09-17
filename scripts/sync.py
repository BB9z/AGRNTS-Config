#!/usr/bin/env python3
"""配置同步工具：把模板仓（本仓库）的内容同步到目标仓，并把目标仓的改动带回来。

用法见 --help。设计背景见 docs/config-sync.decision.md。
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import subprocess
import sys
import tempfile
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECTS_ROOT = os.path.join(REPO_ROOT, "projects")
STATE_ROOT = os.path.join(REPO_ROOT, ".state")
CONFIG_PATH = os.path.join(STATE_ROOT, "config.json")
SNAPSHOT_ROOT = os.path.join(STATE_ROOT, "snapshots")
BACKUP_ROOT = os.path.join(REPO_ROOT, ".backup")

# 用户级同步：模板固定是 user/claude/，落盘固定是 ~/.claude/，用一个不会与数字短 ID 冲突的固定短 ID 存状态与备份。两个根都做成可替换的模块级常量，测试指向 tmp/ 下的临时目录，不碰真实的 ~/.claude/。
USER_TEMPLATE_ROOT = os.path.join(REPO_ROOT, "user", "claude")
USER_TARGET_ROOT = os.path.expanduser("~/.claude")
USER_SHORT_ID = "user"

# scripts/sync_settings.py 是纯数据变换模块，不碰路径常量，独立出去不影响测试对上面这些常量的整体替换；这里保证无论 sync.py 是被直接执行还是被 importlib 按文件路径加载，都能找到同目录下的它。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sync_settings as jsonmerge  # noqa: E402

# 不参与同步的相对路径：本机专属，不应该有通用版本可下发或回流。
EXCLUDE_RELPATHS = frozenset({".claude/settings.local.json"})

# 不参与同步的文件名：出现在哪一层都排除。系统与工具自动生成，跟配置无关，且两侧各自随时会再生，纳入同步只会制造无休止的改动。
EXCLUDE_NAMES = frozenset({".DS_Store"})


def is_excluded(relpath: str) -> bool:
    return relpath in EXCLUDE_RELPATHS or any(
        part in EXCLUDE_NAMES for part in relpath.split("/")
    )


def atomic_write_bytes(path: str, data: bytes, executable: bool) -> None:
    """把 data 原子写入 path：先写临时文件再 rename，中途崩溃不留半截文件。"""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".sync-tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        mode = 0o755 if executable else 0o644
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def snapshot_dir(short_id: str, side: str) -> str:
    return os.path.join(SNAPSHOT_ROOT, short_id, side)


def read_state(root: str, relpath: str):
    """读 root 下 relpath 的当前内容与可执行位。文件缺席（本身不存在，或路径中间某段不是目录）时返回 None；其它 I/O 错误（权限不足等）原样抛出——那不是「缺席」，不能被判定当成删除传播到另一侧，应该中止本次同步。

    始终按 open()/os.stat() 的默认行为跟随符号链接，这样模板仓、目标仓、快照区三类根目录可以复用同一份读取逻辑。
    """
    path = os.path.join(root, *relpath.split("/"))
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    if stat.S_ISDIR(st.st_mode):
        return None
    try:
        with open(path, "rb") as f:
            data = f.read()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return (data, bool(st.st_mode & 0o111))


class OutOfScope(Exception):
    """写入解析到的真实路径落在本次同步范围之外时抛出。范围是模板侧那个模板目录、目标侧那个目标仓根——同步动作不允许溢出到不受管的地方。"""


def resolve_write_path(root: str, relpath: str) -> str:
    """算出 relpath 真正应该落笔的路径：如果 relpath 本身或路径中间是符号链接，跟随到链接指向的真实位置，写入落在真实文件上而不是替换链接本身。真实位置若不在 root 之内，抛 OutOfScope。"""
    path = os.path.join(root, *relpath.split("/"))
    real = os.path.realpath(path)
    real_root = os.path.realpath(root)
    try:
        common = os.path.commonpath([real, real_root])
    except ValueError:
        common = None
    if common != real_root:
        raise OutOfScope(real)
    return real


def write_state(root: str, relpath: str, state) -> None:
    data, executable = state
    real_path = resolve_write_path(root, relpath)
    atomic_write_bytes(real_path, data, executable)


def remove_state(root: str, relpath: str) -> None:
    """删除只删 relpath 这个路径本身（符号链接就删链接，不跟随），再顺手清理因此变空的父目录，但绝不越过 root 边界——用规范化路径的 commonpath 判断父目录是否仍在 root 之内，而不是对字符串做 startswith。"""
    path = os.path.join(root, *relpath.split("/"))
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    root_norm = os.path.normpath(root)
    parent = os.path.dirname(path)
    while parent:
        parent_norm = os.path.normpath(parent)
        if parent_norm == root_norm:
            break
        try:
            common = os.path.commonpath([parent_norm, root_norm])
        except ValueError:
            break
        if common != root_norm:
            break
        try:
            os.rmdir(parent)
        except OSError:
            break
        parent = os.path.dirname(parent)


def scan_tree(root: str):
    """列出 root 下所有文件的相对路径（白名单式，目录不是同步单位）。

    符号链接一律跟随：文件链接直接记为普通文件（读写走目标真实内容）；目录链接跟随展开，用真实路径做祖先集合防止循环。断链与循环记入 issues，不计入 files。

    出错处理与 read_state 同一条准则：只有「这里没有东西」才当缺席跳过，权限不足一类真正的 I/O 错误原样抛出中止本次同步。扫描结果就是模板侧的白名单，静默少扫一批文件等同于宣告它们已被删除，会把删除传播到另一侧。
    """
    files: dict[str, bool] = {}
    issues: list[tuple[str, str]] = []

    def walk(dir_path: str, rel_prefix: str, ancestry: frozenset):
        try:
            entries = list(os.scandir(dir_path))
        except (FileNotFoundError, NotADirectoryError):
            # 根目录不存在是合法的空树；子目录走到这里说明扫描期间它被挪走了，这一枝确实已经没有内容。
            return
        for entry in sorted(entries, key=lambda e: e.name):
            rel = f"{rel_prefix}/{entry.name}" if rel_prefix else entry.name
            try:
                is_symlink = entry.is_symlink()
            except (FileNotFoundError, NotADirectoryError):
                continue
            if is_symlink:
                try:
                    st = os.stat(entry.path)
                except (FileNotFoundError, NotADirectoryError):
                    issues.append((rel, "broken"))
                    continue
                except OSError as e:
                    # 自成环的链接在 stat 这一步就失败，走不到下面的祖先集合判定。
                    if e.errno != errno.ELOOP:
                        raise
                    issues.append((rel, "circular"))
                    continue
                real = os.path.realpath(entry.path)
                if stat.S_ISDIR(st.st_mode):
                    if real in ancestry:
                        issues.append((rel, "circular"))
                        continue
                    walk(entry.path, rel, ancestry | {real})
                elif not is_excluded(rel):
                    files[rel] = True
                continue
            try:
                is_dir = entry.is_dir()
                is_file = entry.is_file()
            except (FileNotFoundError, NotADirectoryError):
                continue
            if is_dir:
                walk(entry.path, rel, ancestry | {os.path.realpath(entry.path)})
            elif is_file and not is_excluded(rel):
                files[rel] = True

    walk(root, "", frozenset({os.path.realpath(root)}))
    return files, issues


def list_snapshot_relpaths(short_id: str) -> set[str]:
    """两侧快照树的路径集合取并集，不假设它们恒等——一侧缺席、一侧仍有快照的情况在本批次开始出现（比如合并被挂起后用户手工删掉了某一侧）。"""
    relpaths: set[str] = set()
    for side in ("template", "target"):
        base = snapshot_dir(short_id, side)
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in filenames:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, base).replace(os.sep, "/")
                relpaths.add(rel)
    return relpaths


def decide(t, l, ts, ls, has_snapshot: bool) -> str:
    """三方判定表：只看内容与可执行位，不看 mtime。

    t/l 是模板侧、目标侧现在的状态；ts/ls 是快照对里记录的对应状态； None 表示文件缺席，缺席也是内容的一种，参与同一张表。

    两侧快照允许不等，差异就是已知定制。只要存在这种定制（ts != ls），哪怕这一轮只有一侧偏离了自己的快照，也不能直接把内容送到对侧——那会把定制覆盖掉。

    改成走合并：合并以两侧各自的快照为交叉 base（在 apply_needs_merge 里实现），current 与 base 相等时 diff3 的结果就是 other，效果和直送完全一样，所以两侧快照相等时走合并没有区别，直送只是为了清单可读性保留的快速路径，不是另一套语义。

    删除不受这条规则影响：模板删除即使目标侧有定制也直接删，目标删除同理直接从模板摘掉，删除是明确动作，没有「合并」的余地。
    """
    if not has_snapshot:
        if t is not None and l is None:
            return "push"
        if t is not None and l is not None:
            return "first_adopt"
        # t is None：模板没有也没快照，若还出现在 universe 里说明只在目标仓存在，属于目标仓自有文件，不应参与判定；防御性地当无动作处理。
        return "none"

    t_changed = t != ts
    l_changed = l != ls
    if not t_changed and not l_changed:
        return "none"
    if t is None and l is None:
        # 两侧都缺席：都被删了，没有内容可比较，也没有理由继续报「需要合并」——那样只会一次又一次重复同一条、永远收敛不了。直接清快照、记无动作。
        return "both_deleted"

    customized = ts != ls
    if t_changed and not l_changed:
        if t is None:
            return "delete_target"
        return "needs_merge" if customized else "push"
    if l_changed and not t_changed:
        if l is None:
            return "delete_template"
        return "needs_merge" if customized else "pullback"
    return "needs_merge"


def collect_universe(short_id: str, template_root: str):
    template_files, issues = scan_tree(template_root)
    # 快照里可能留着排除规则生效之前存下的路径，一并按现行规则过滤，免得它们继续被判定、被传播。
    universe = {
        rel for rel in set(template_files) | list_snapshot_relpaths(short_id)
        if not is_excluded(rel)
    }
    return universe, issues


def plan_sync(short_id: str, template_root: str, target_root: str):
    """扫描 + 判定，返回该目标每个受关注文件的动作，不做任何写入。

    项目级、用户级共用这一份：调用方直接给模板根与落盘根，不再要求一个「登记记录」的形状，用户级同步没有配置记录也能走同一条路径。
    """
    universe, issues = collect_universe(short_id, template_root)

    actions = []
    for rel in sorted(universe):
        t = read_state(template_root, rel)
        l = read_state(target_root, rel)
        ts = read_state(snapshot_dir(short_id, "template"), rel)
        ls = read_state(snapshot_dir(short_id, "target"), rel)
        has_snapshot = ts is not None or ls is not None
        action = decide(t, l, ts, ls, has_snapshot)
        actions.append(
            {"relpath": rel, "action": action, "t": t, "l": l, "ts": ts, "ls": ls}
        )
    return actions, issues


ACTION_LABELS = {
    "push": "下发",
    "pullback": "回流",
    "delete_target": "删除-目标仓",
    "delete_template": "删除-模板仓",
    "both_deleted": "两侧均已删除",
    "first_adopt": "首次接管",
    "needs_merge": "需要合并",
}

MERGE_OUTCOME_LABELS = {
    "merged_clean": "合并-干净",
    "merged_conflict": "合并-冲突，已挂起",
    "skip_absent": "合并-跳过（一侧缺席），已挂起",
    "skip_binary": "合并-跳过（非文本），已挂起",
    "skipped_out_of_scope": "合并-跳过（链接超出范围）",
    "settings_merged_clean": "键级合并-干净",
    "settings_merged_conflict": "键级合并-冲突，已挂起",
    "settings_invalid_json": "键级合并-跳过（无法解析），已挂起",
}

ISSUE_LABELS = {
    "broken": "断链",
    "circular": "循环链接",
    "out_of_scope": "链接目标超出同步范围",
}


def print_report(actions, issues) -> None:
    changed = [a for a in actions if a["action"] != "none"]
    for a in changed:
        label = MERGE_OUTCOME_LABELS[a["merge_outcome"]] if "merge_outcome" in a else ACTION_LABELS[a["action"]]
        notes = []
        if a.get("exec_conflict"):
            notes.append("可执行位冲突，已保留各侧现值")
        if a.get("settings_conflict_paths"):
            notes.append("冲突键：" + ", ".join(a["settings_conflict_paths"]))
        note = f"（{'；'.join(notes)}）" if notes else ""
        print(f"[{label}] {a['relpath']}{note}")
    for rel, reason in issues:
        print(f"[{ISSUE_LABELS[reason]}，跳过] {rel}")
    print(f"共 {len(changed)} 个文件有动作，{len(issues)} 个链接问题被跳过")


def backup_before_overwrite(batch_dir: str, short_id: str, side: str, relpath: str, current_state) -> None:
    """把即将被覆盖或删除的原内容备份到本次同步批次目录下。current_state 为 None（原本就不存在）时无需备份。"""
    if current_state is None:
        return
    data, executable = current_state
    backup_path = os.path.join(batch_dir, short_id, side, *relpath.split("/"))
    atomic_write_bytes(backup_path, data, executable)


def apply_write(batch_dir, short_id, side, root, relpath, current_state, new_state, issues) -> bool:
    """把 new_state 写进 root/relpath。写入目标解析到同步范围之外（穿透符号链接后落在别处）时不写，往 issues 里记一条，返回 False 让调用方据此跳过这个文件的快照更新——不然下次同步就再也发现不了这处遗留改动。"""
    if current_state == new_state:
        return True
    try:
        resolve_write_path(root, relpath)
    except OutOfScope:
        issues.append((relpath, "out_of_scope"))
        return False
    backup_before_overwrite(batch_dir, short_id, side, relpath, current_state)
    write_state(root, relpath, new_state)
    return True


def apply_delete(batch_dir, short_id, side, root, relpath, current_state) -> None:
    if current_state is None:
        return
    backup_before_overwrite(batch_dir, short_id, side, relpath, current_state)
    remove_state(root, relpath)


def clear_snapshot_pair(short_id: str, relpath: str) -> None:
    remove_state(snapshot_dir(short_id, "template"), relpath)
    remove_state(snapshot_dir(short_id, "target"), relpath)


def save_one_snapshot(root: str, relpath: str, state) -> None:
    """state 为 None（该侧此刻缺席）时清掉这一侧的快照文件，而不是把 None 往下传给期待 (data, executable) 二元组的 write_state。"""
    if state is None:
        remove_state(root, relpath)
    else:
        write_state(root, relpath, state)


def save_snapshot_pair(short_id: str, template_root: str, target_root: str, relpath: str) -> None:
    """快照永远存两侧此刻的实际内容，不凭动作类型推断——这样首次接管、下发、回流、合并都不需要各自记一套快照写入逻辑。调用时机同样要紧：必须在用户确认（挂起交互的那一次回车）之后才读，读早了会把「撤销即登记」这条语义打掉。"""
    t = read_state(template_root, relpath)
    l = read_state(target_root, relpath)
    save_one_snapshot(snapshot_dir(short_id, "template"), relpath, t)
    save_one_snapshot(snapshot_dir(short_id, "target"), relpath, l)


def is_text_content(data: bytes) -> bool:
    return b"\x00" not in data


_CONFLICT_MARKER_PREFIXES = (b"<<<<<<<", b">>>>>>>")


def has_conflict_markers(data: bytes) -> bool:
    """检查内容里是否还留着 git 冲突标记行。回车确认之后要不要落快照，看的就是这个客观事实，不是猜用户到底有没有动手改。"""
    for line in data.split(b"\n"):
        if line.startswith(_CONFLICT_MARKER_PREFIXES[0]) or line.startswith(_CONFLICT_MARKER_PREFIXES[1]):
            return True
    return False


def merge_bool_3way(base: bool, current: bool, other: bool) -> tuple[bool, bool]:
    """可执行位的三方判定，形状和内容合并一样：只有一侧变了就用变了的那个值；两侧都变但变成同一个值也没有冲突；两侧都变且不同，保留 current（本侧现值），第二个返回值告诉调用方这是一次冲突，需要在清单里标注。"""
    if current == other:
        return current, False
    if current == base:
        return other, False
    if other == base:
        return current, False
    return current, True


def git_merge_file(current: bytes, base: bytes, other: bytes, labels: tuple[str, str, str]) -> tuple[bytes, int]:
    """用 git merge-file -p 做一次三方合并，三个输入各写一个临时文件（放在系统临时目录，不进模板仓也不进目标仓），结果从 stdout 拿到。

    返回 (合并后内容, 冲突数)：0 表示干净合并，正数是冲突段数，git 本身出错时返回码是负数，这里转成异常让调用方中止而不是当作「有冲突」处理。
    """
    with tempfile.TemporaryDirectory(prefix="agent-config-sync-merge-") as d:
        current_path = os.path.join(d, "current")
        base_path = os.path.join(d, "base")
        other_path = os.path.join(d, "other")
        with open(current_path, "wb") as f:
            f.write(current)
        with open(base_path, "wb") as f:
            f.write(base)
        with open(other_path, "wb") as f:
            f.write(other)
        proc = subprocess.run(
            [
                "git", "merge-file", "-p",
                "-L", labels[0], "-L", labels[1], "-L", labels[2],
                current_path, base_path, other_path,
            ],
            capture_output=True,
        )
        if proc.returncode < 0:
            raise RuntimeError(
                f"git merge-file 出错：{proc.stderr.decode('utf-8', 'replace')}"
            )
        return proc.stdout, proc.returncode


def apply_needs_merge(batch_dir, short_id, template_root, target_root, a: dict, issues: list):
    """处理判定表里「需要合并」的一格：可能是两侧都偏离了快照，也可能只有一侧偏离但两侧快照本就不等（已有定制）。

    三方合并是把 other 相对 base 的变化应用到 current，所以 base 必须取「变化来源那一侧」的快照，两次合并各用对侧快照、互不干扰：
    - 目标侧结果：current=目标现内容，base=模板快照，other=模板现内容
    - 模板侧结果：current=模板现内容，base=目标快照，other=目标现内容
    取本侧快照当 base 是错的——那会把「两侧快照本来就不同」这个已知定制本身也当成一处要合并的变化，合并结果就把定制抹掉了。

    返回 (outcome, exec_conflict, 需要打开编辑器看的路径列表)。两种情况不合并、两侧都不动，直接进挂起：一侧缺席（空文件当一方合并没有意义）、非文本文件（合并只会产出无意义的冲突标记）。
    """
    rel = a["relpath"]
    t, l, ts, ls = a["t"], a["l"], a["ts"], a["ls"]
    target_path = os.path.join(target_root, *rel.split("/"))
    template_path = os.path.join(template_root, *rel.split("/"))

    if t is None or l is None:
        paths = [p for p, present in ((template_path, t is not None), (target_path, l is not None)) if present]
        return "skip_absent", False, paths

    t_data, t_exec = t
    l_data, l_exec = l
    if not (is_text_content(t_data) and is_text_content(l_data)):
        return "skip_binary", False, [template_path, target_path]

    ts_data, ts_exec = ts if ts is not None else (b"", False)
    ls_data, ls_exec = ls if ls is not None else (b"", False)

    target_merged, target_conflicts = git_merge_file(
        current=l_data, base=ts_data, other=t_data,
        labels=("目标仓", "模板仓快照", "模板仓"),
    )
    template_merged, template_conflicts = git_merge_file(
        current=t_data, base=ls_data, other=l_data,
        labels=("模板仓", "目标仓快照", "目标仓"),
    )
    target_exec, target_exec_conflict = merge_bool_3way(ts_exec, l_exec, t_exec)
    template_exec, template_exec_conflict = merge_bool_3way(ls_exec, t_exec, l_exec)

    target_ok = apply_write(batch_dir, short_id, "target", target_root, rel, l, (target_merged, target_exec), issues)
    template_ok = apply_write(
        batch_dir, short_id, "template", template_root, rel, t, (template_merged, template_exec), issues
    )
    if not (target_ok and template_ok):
        # 某一侧的写入穿透符号链接后落到范围外被拦下：整份都不算完成，留给下次重跑，快照也不更新，免得把这处未完成的改动悄悄吞掉。
        return "skipped_out_of_scope", False, []

    conflicted = target_conflicts != 0 or template_conflicts != 0
    editor_paths = [template_path, target_path] if conflicted else []
    exec_conflict = target_exec_conflict or template_exec_conflict
    return ("merged_conflict" if conflicted else "merged_clean"), exec_conflict, editor_paths


def is_settings_json(relpath: str) -> bool:
    return relpath.rsplit("/", 1)[-1] == "settings.json"


_SETTINGS_INVALID = object()


def _load_json_side(state):
    """state 是 read_state() 的返回值。文件整份缺席时当作空对象——键级判定会让每条托管叶子自然表现为「待下发」或「待接管」，不需要特殊分支。解析失败（不是合法 JSON，或顶层不是对象）返回 _SETTINGS_INVALID，让调用方整体退回「跳过、挂起」，不去猜半份坏文件里哪些键还能信。"""
    if state is None:
        return {}
    try:
        obj = json.loads(state[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _SETTINGS_INVALID
    return obj if isinstance(obj, dict) else _SETTINGS_INVALID


def apply_settings_merge(batch_dir, short_id, template_root, target_root, a: dict, issues: list):
    """settings.json 的键级合并：对象逐层展开，数组与标量是叶子；托管集合是模板当前或模板快照里出现过的叶子路径，目标仓自己独有的路径完全不看。

    每条叶子套用整文件那套三方判定表（decide()），字符串数组按集合合并。

    其余类型的叶子在两侧都变且值不同时没有可靠的合并方式，保留各自现值、点名报告为冲突，交给用户在挂起环节里手工协调——不写冲突标记进 JSON，那会让文件直接无法解析。

    返回契约与 apply_needs_merge 一致：(outcome, exec_conflict, 挂起路径)。
    """
    rel = a["relpath"]
    t, l, ts, ls = a["t"], a["l"], a["ts"], a["ls"]
    target_path = os.path.join(target_root, *rel.split("/"))
    template_path = os.path.join(template_root, *rel.split("/"))

    t_obj, l_obj, ts_obj, ls_obj = (_load_json_side(s) for s in (t, l, ts, ls))
    if _SETTINGS_INVALID in (t_obj, l_obj, ts_obj, ls_obj):
        return "settings_invalid_json", False, [template_path, target_path]

    t_leaves = jsonmerge.flatten(t_obj)
    l_leaves = jsonmerge.flatten(l_obj)
    ts_leaves = jsonmerge.flatten(ts_obj)
    ls_leaves = jsonmerge.flatten(ls_obj)

    has_snapshot = ts is not None or ls is not None
    universe = set(t_leaves) | set(ts_leaves)

    target_leaves = dict(l_leaves)
    template_leaves = dict(t_leaves)
    conflicted_paths: list[tuple] = []

    for path in universe:
        tv, lv = t_leaves.get(path), l_leaves.get(path)
        tsv, lsv = ts_leaves.get(path), ls_leaves.get(path)
        outcome = decide(tv, lv, tsv, lsv, has_snapshot)
        if outcome in ("none", "both_deleted"):
            continue
        if outcome in ("push", "first_adopt"):
            target_leaves[path] = tv
        elif outcome == "pullback":
            # 目标侧已经是源头，target_leaves 里本来就是 lv；模板侧要接住这个新值，不然目标仓对托管键的修改永远回流不到模板仓。
            template_leaves[path] = lv
        elif outcome == "delete_target":
            target_leaves.pop(path, None)
        elif outcome == "delete_template":
            template_leaves.pop(path, None)
        else:  # needs_merge
            new_target, new_template, conflicted = jsonmerge.resolve_leaf_conflict(tv, lv, tsv, lsv)
            if new_target is None:
                target_leaves.pop(path, None)
            else:
                target_leaves[path] = new_target
            if new_template is None:
                template_leaves.pop(path, None)
            else:
                template_leaves[path] = new_template
            if conflicted:
                conflicted_paths.append(path)

    new_target_obj = jsonmerge.rebuild_object(l_obj, target_leaves)
    new_template_obj = jsonmerge.rebuild_object(t_obj, template_leaves)
    new_target_bytes = (json.dumps(new_target_obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    new_template_bytes = (json.dumps(new_template_obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    t_exec = t[1] if t is not None else False
    l_exec = l[1] if l is not None else False
    ts_exec = ts[1] if ts is not None else False
    ls_exec = ls[1] if ls is not None else False
    target_exec, target_exec_conflict = merge_bool_3way(ts_exec, l_exec, t_exec)
    template_exec, template_exec_conflict = merge_bool_3way(ls_exec, t_exec, l_exec)

    target_ok = apply_write(
        batch_dir, short_id, "target", target_root, rel, l, (new_target_bytes, target_exec), issues
    )
    template_ok = apply_write(
        batch_dir, short_id, "template", template_root, rel, t, (new_template_bytes, template_exec), issues
    )
    if not (target_ok and template_ok):
        return "skipped_out_of_scope", False, []

    exec_conflict = target_exec_conflict or template_exec_conflict
    if conflicted_paths:
        a["settings_conflict_paths"] = [".".join(p) for p in sorted(conflicted_paths)]
        return "settings_merged_conflict", exec_conflict, [template_path, target_path]
    return "settings_merged_clean", exec_conflict, []


def wait_for_enter() -> bool:
    """挂起交互里等用户处理完文件后按一次回车，返回值是「是否真的等到了一次人工确认」。stdin 不是交互终端（测试、脚本调用、无人值守跑批）时没有人来按回车，返回 False——调用方据此知道这次「确认」缺席，不能把它当成真的确认对待。"""
    if not sys.stdin.isatty():
        return False
    try:
        input()
    except EOFError:
        pass
    return True


def open_editor(paths: list[str]) -> None:
    """用 $EDITOR 打开挂起文件，不等它退出——用户在挂起期间可以用任意方式改两侧文件，不必被绑定在这一个编辑器进程上。没设置 $EDITOR 时只打印提示，不阻塞。"""
    editor = os.environ.get("EDITOR")
    if not editor:
        print("未设置 $EDITOR，请自行打开并处理以下文件：")
        for p in paths:
            print(f"  {p}")
        return
    try:
        subprocess.Popen([editor, *paths])
    except OSError as e:
        print(f"启动编辑器失败（{e}），请自行打开并处理以下文件：")
        for p in paths:
            print(f"  {p}")


# 这几种结果工具其实什么都没写、或者写出来的内容本身还带着待处理的标记，只是挂起等用户处理，不能落快照——落了就等于把一个没解决的冲突静默登记成「已确认」，删除没传播、修改没传播，下次同步也不会再报了。
_NEVER_SNAPSHOT_OUTCOMES = ("skip_absent", "skip_binary", "skipped_out_of_scope", "settings_invalid_json")
_HOLD_OUTCOMES = ("merged_conflict", "settings_merged_conflict")


def apply_force_override(actions: list, side: str) -> None:
    """--force-template / --force-target 逃生舱：忽略快照与对侧改动，把每个文件的动作强改成朝一个方向的单向覆盖或删除。已经没有差异的条目不去动它，报告里不会平白多出一堆没意义的条目。"""
    for a in actions:
        if a["action"] in ("none", "both_deleted"):
            continue
        if side == "template":
            a["action"] = "delete_target" if a["t"] is None else "push"
        else:
            a["action"] = "delete_template" if a["l"] is None else "pullback"
        a.pop("merge_outcome", None)
        a.pop("exec_conflict", None)
        a.pop("settings_conflict_paths", None)


def execute_sync(short_id: str, template_root: str, target_root: str, actions: list, issues: list, is_user: bool = False) -> int:
    """真正落盘：备份、原子写、删除、三方合并（含 settings.json 的键级合并），然后是一次性的挂起交互，最后才是快照更新。

    快照必须等这一切（包括用户在挂起期间可能做的手工改动）结束后才读取两侧此刻的实际内容来存——写完立刻存会让「撤销即登记」失效。

    例如：回流写进模板仓后马上存快照，用户随后 git checkout 撤掉这次回流，下次同步会读到「模板侧变了」，把撤销后的内容当新改动又推回目标仓。

    返回值是 CLI 退出码：0 正常。

    本次存在挂起项但没有人能在场确认（非交互终端），或者回车之后文件此刻仍带着冲突标记（有人在场但没有真的处理完），都返回 1——这些文件的快照被刻意不落，留到下次同步继续报，不能让命令悄悄退出成功、把没解决的冲突当没发生过。
    """
    batch_dir = os.path.join(BACKUP_ROOT, datetime.now().strftime("%Y%m%dT%H%M%S%f"))

    snapshot_pending: list[str] = []
    hold_relpaths: list[str] = []
    editor_paths: list[str] = []

    for a in actions:
        rel = a["relpath"]
        action = a["action"]
        if action == "none":
            continue

        if is_settings_json(rel) and action in ("push", "pullback", "needs_merge", "first_adopt"):
            outcome, exec_conflict, paths = apply_settings_merge(
                batch_dir, short_id, template_root, target_root, a, issues
            )
            a["merge_outcome"] = outcome
            if exec_conflict:
                a["exec_conflict"] = True
            if outcome not in _NEVER_SNAPSHOT_OUTCOMES:
                snapshot_pending.append(rel)
                if outcome in _HOLD_OUTCOMES:
                    hold_relpaths.append(rel)
            editor_paths.extend(paths)
            continue

        if action == "push" or action == "first_adopt":
            if apply_write(batch_dir, short_id, "target", target_root, rel, a["l"], a["t"], issues):
                snapshot_pending.append(rel)
        elif action == "pullback":
            if apply_write(batch_dir, short_id, "template", template_root, rel, a["t"], a["l"], issues):
                snapshot_pending.append(rel)
        elif action == "delete_target":
            apply_delete(batch_dir, short_id, "target", target_root, rel, a["l"])
            clear_snapshot_pair(short_id, rel)
        elif action == "delete_template":
            apply_delete(batch_dir, short_id, "template", template_root, rel, a["t"])
            clear_snapshot_pair(short_id, rel)
        elif action == "both_deleted":
            clear_snapshot_pair(short_id, rel)
        elif action == "needs_merge":
            outcome, exec_conflict, paths = apply_needs_merge(
                batch_dir, short_id, template_root, target_root, a, issues
            )
            a["merge_outcome"] = outcome
            if exec_conflict:
                a["exec_conflict"] = True
            if outcome not in _NEVER_SNAPSHOT_OUTCOMES:
                snapshot_pending.append(rel)
                if outcome in _HOLD_OUTCOMES:
                    hold_relpaths.append(rel)
            editor_paths.extend(paths)

    print(f"目标 {short_id} ({target_root}) 同步结果：")
    print_report(actions, issues)

    # 一次同步只挂起一次：只要这批里有文件的快照要等这次确认之后才存（push/pullback/first_adopt/合并），就等一次回车再落快照，让用户有机会在两侧工作区里用 git diff / checkout 检查、撤销。纯删除动作已经在上面立刻完成并清了快照，没有什么要等的，不占用这次确认。
    got_confirmation = True
    if snapshot_pending or editor_paths:
        if editor_paths:
            print(f"{len(editor_paths)} 个文件需要人工确认：")
            for p in editor_paths:
                print(f"  {p}")
            open_editor(editor_paths)
        if is_user:
            print(f"本次备份目录：{batch_dir}（用户级没有 git diff 可用，可以拿它跟现状比对）")
        print("处理完毕后按回车继续，工具将按两侧此刻的实际内容重新落定快照……")
        got_confirmation = wait_for_enter()
        if not got_confirmation and editor_paths:
            print("stdin 不是交互终端，本次有未处理的挂起项：不会自动确认，这些文件的快照不落，留到下次同步继续报。")

    withheld = set(hold_relpaths) if (editor_paths and not got_confirmation) else set()
    still_conflicted: list[str] = []
    for rel in snapshot_pending:
        if rel in withheld:
            continue
        t_now = read_state(template_root, rel)
        l_now = read_state(target_root, rel)
        if (t_now is not None and has_conflict_markers(t_now[0])) or (
            l_now is not None and has_conflict_markers(l_now[0])
        ):
            still_conflicted.append(rel)
            continue
        save_one_snapshot(snapshot_dir(short_id, "template"), rel, t_now)
        save_one_snapshot(snapshot_dir(short_id, "target"), rel, l_now)

    if still_conflicted:
        print("以下文件此刻仍带冲突标记，快照不落，留到下次同步继续报：")
        for rel in still_conflicted:
            print(f"  {rel}")

    exit_code = 1 if (editor_paths and not got_confirmation) or still_conflicted else 0
    if exit_code == 0:
        print(f"目标 {short_id} 同步完成")
    return exit_code


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {"next_id": 1, "targets": {}}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config: dict) -> None:
    data = json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    atomic_write_bytes(CONFIG_PATH, data, executable=False)


def cmd_add(args: argparse.Namespace) -> int:
    template = args.template
    template_dir = os.path.join(PROJECTS_ROOT, template)
    if not os.path.isdir(template_dir):
        print(f"错误：模板不存在 projects/{template}", file=sys.stderr)
        return 1

    target_path = os.path.abspath(args.target_path)
    if not os.path.isdir(target_path):
        print(f"错误：目标仓路径不存在或不是目录：{target_path}", file=sys.stderr)
        return 1

    config = load_config()
    for short_id, entry in config["targets"].items():
        if os.path.abspath(entry["path"]) == target_path:
            print(f"错误：该路径已登记为 {short_id}", file=sys.stderr)
            return 1

    short_id = str(config["next_id"])
    config["next_id"] += 1
    config["targets"][short_id] = {"path": target_path, "template": template}
    save_config(config)
    print(f"已登记：{short_id} -> {target_path}（模板：{template}）")
    return 0


def resolve_target(config: dict, ref: str):
    """按短 ID 或路径找目标仓记录，找不到返回 (None, None)。"""
    if ref in config["targets"]:
        return ref, config["targets"][ref]
    ref_abs = os.path.abspath(ref)
    for short_id, entry in config["targets"].items():
        if os.path.abspath(entry["path"]) == ref_abs:
            return short_id, entry
    return None, None


def pick_target_interactively(config: dict):
    """sync 不带参数时的交互选择：列出已登记目标仓，选一个、确认后执行。非交互终端直接报错退出，不猜用户想同步哪个。"""
    if not sys.stdin.isatty():
        print("错误：未指定目标，且当前不是交互终端，无法选择", file=sys.stderr)
        return None, None
    targets = sorted(config["targets"].items(), key=lambda kv: int(kv[0]))
    if not targets:
        print("错误：尚未登记任何目标仓，请先用 add 登记", file=sys.stderr)
        return None, None
    print("已登记的目标仓：")
    for i, (short_id, entry) in enumerate(targets, 1):
        print(f"  {i}. [{short_id}] {entry['path']}（模板：{entry['template']}）")
    try:
        choice = input("选择序号：").strip()
    except EOFError:
        print("错误：未读到选择，取消", file=sys.stderr)
        return None, None
    if not choice.isdigit() or not (1 <= int(choice) <= len(targets)):
        print("错误：无效选择，取消", file=sys.stderr)
        return None, None
    short_id, entry = targets[int(choice) - 1]
    try:
        confirm = input(f"确认同步 [{short_id}] {entry['path']}？(y/N) ").strip().lower()
    except EOFError:
        confirm = ""
    if confirm != "y":
        print("已取消")
        return None, None
    return short_id, entry


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config()
    rows = [
        (short_id, entry["path"], os.path.join(PROJECTS_ROOT, entry["template"]), entry["template"])
        for short_id, entry in sorted(config["targets"].items(), key=lambda kv: int(kv[0]))
    ]
    user_available = os.path.isdir(USER_TEMPLATE_ROOT)
    if not rows and not user_available:
        print("尚未登记任何目标仓")
        return 0

    for short_id, target_root, template_root, template_name in rows:
        if not os.path.isdir(target_root):
            print(f"{short_id}\t{target_root}\t模板={template_name}\t错误：目标仓路径不存在")
            continue
        if not os.path.isdir(template_root):
            print(f"{short_id}\t{target_root}\t模板={template_name}\t错误：模板不存在")
            continue
        try:
            actions, issues = plan_sync(short_id, template_root, target_root)
        except OSError as e:
            print(f"{short_id}\t{target_root}\t模板={template_name}\t错误：{e}")
            continue
        pending = sum(1 for a in actions if a["action"] != "none")
        customized = sum(
            1 for a in actions if a["ts"] is not None and a["ls"] is not None and a["ts"] != a["ls"]
        )
        extra = f"\t链接问题={len(issues)}" if issues else ""
        print(f"{short_id}\t{target_root}\t模板={template_name}\t待同步={pending}\t定制={customized}{extra}")

    if user_available:
        try:
            actions, issues = plan_sync(USER_SHORT_ID, USER_TEMPLATE_ROOT, USER_TARGET_ROOT)
            pending = sum(1 for a in actions if a["action"] != "none")
            customized = sum(
                1 for a in actions if a["ts"] is not None and a["ls"] is not None and a["ts"] != a["ls"]
            )
            print(f"{USER_SHORT_ID}\t{USER_TARGET_ROOT}\t模板=user/claude\t待同步={pending}\t定制={customized}")
        except OSError as e:
            print(f"{USER_SHORT_ID}\t{USER_TARGET_ROOT}\t模板=user/claude\t错误：{e}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    config = load_config()

    if args.target is None:
        short_id, entry = pick_target_interactively(config)
        if short_id is None:
            return 1
    elif args.target == "user":
        short_id, entry = USER_SHORT_ID, {"path": USER_TARGET_ROOT, "template": None}
    else:
        short_id, entry = resolve_target(config, args.target)
        if short_id is None:
            print(f"错误：未登记的目标仓：{args.target}，请先用 add 登记", file=sys.stderr)
            return 1

    is_user = short_id == USER_SHORT_ID
    template_root = USER_TEMPLATE_ROOT if is_user else os.path.join(PROJECTS_ROOT, entry["template"])
    target_root = entry["path"]

    if not os.path.isdir(template_root):
        print(f"错误：模板不存在 {template_root}", file=sys.stderr)
        return 1
    if not os.path.isdir(target_root):
        hint = "" if is_user else "，如已迁移请手工更新 .state/config.json"
        print(f"错误：目标路径不存在：{target_root}{hint}", file=sys.stderr)
        return 1

    if args.force_template or args.force_target:
        side_name, side_key = ("模板仓", "template") if args.force_template else ("目标仓", "target")
        print(f"警告：--force-{side_key} 会忽略快照与对侧改动，以{side_name}为准全量覆盖，可能抹掉一批定制。")

    try:
        actions, issues = plan_sync(short_id, template_root, target_root)

        if args.force_template:
            apply_force_override(actions, "template")
        elif args.force_target:
            apply_force_override(actions, "target")

        if args.dry_run:
            print(f"[dry-run] 目标 {short_id} ({target_root})")
            print_report(actions, issues)
            return 0

        return execute_sync(short_id, template_root, target_root, actions, issues, is_user=is_user)
    except OSError as e:
        # 文件缺席已经在 read_state 里被识别并当作 None 处理；能冒到这里的只有权限不足一类真正的 I/O 错误，不能被当成删除去传播，中止本次同步。
        print(f"错误：同步中止，{e}", file=sys.stderr)
        return 1


def cmd_adopt(args: argparse.Namespace) -> int:
    """把目标仓（或用户级落盘目录）里的一个文件提升为模板文件：从路径反查它属于哪个已登记的目标，拷到该目标所用模板的相同相对路径下，再建立快照对。模板侧已经有同名文件时报错退出，不覆盖。"""
    file_path = os.path.abspath(args.file_path)
    if not os.path.isfile(file_path):
        print(f"错误：文件不存在：{file_path}", file=sys.stderr)
        return 1

    config = load_config()
    candidates = [
        (short_id, entry["path"], os.path.join(PROJECTS_ROOT, entry["template"]))
        for short_id, entry in config["targets"].items()
    ]
    candidates.append((USER_SHORT_ID, USER_TARGET_ROOT, USER_TEMPLATE_ROOT))

    match = None
    for short_id, target_root, template_root in candidates:
        target_root_real = os.path.realpath(target_root)
        try:
            rel = os.path.relpath(file_path, target_root_real)
        except ValueError:
            continue
        if rel == os.pardir or rel.startswith(os.pardir + os.sep):
            continue
        if match is None or len(target_root_real) > len(os.path.realpath(match[1])):
            match = (short_id, target_root, template_root, rel.replace(os.sep, "/"))

    if match is None:
        print("错误：该文件不属于任何已登记的目标仓", file=sys.stderr)
        return 1
    short_id, target_root, template_root, rel = match

    if is_excluded(rel):
        print(f"错误：{rel} 不参与同步，不能提升为模板文件", file=sys.stderr)
        return 1

    state = read_state(target_root, rel)
    if state is None:
        print(f"错误：读取失败：{file_path}", file=sys.stderr)
        return 1

    if read_state(template_root, rel) is not None:
        print(f"错误：模板已有同名文件 {os.path.join(template_root, *rel.split('/'))}，不覆盖", file=sys.stderr)
        return 1

    write_state(template_root, rel, state)
    save_snapshot_pair(short_id, template_root, target_root, rel)
    print(f"已提升为模板文件：{rel} -> {os.path.join(template_root, *rel.split('/'))}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sync.py", description="模板仓与目标仓的配置同步工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="登记一个目标仓")
    p_add.add_argument("target_path", help="目标仓路径")
    p_add.add_argument("--template", required=True, help="使用的模板名（projects/<模板名>）")
    p_add.set_defaults(func=cmd_add)

    p_status = sub.add_parser("status", help="列出各目标仓（含用户级）的待同步与定制数量")
    p_status.set_defaults(func=cmd_status)

    p_sync = sub.add_parser("sync", help="同步一个目标；不带参数进交互选择；user 表示用户级")
    p_sync.add_argument("target", nargs="?", default=None, help="目标仓短 ID / 路径 / user")
    p_sync.add_argument("--dry-run", action="store_true", help="只打印清单，不写入任何内容")
    force_group = p_sync.add_mutually_exclusive_group()
    force_group.add_argument(
        "--force-template", action="store_true", help="逃生舱：忽略快照与定制，以模板仓为准全量覆盖目标"
    )
    force_group.add_argument(
        "--force-target", action="store_true", help="逃生舱：忽略快照与定制，以目标为准全量覆盖模板仓"
    )
    p_sync.set_defaults(func=cmd_sync)

    p_adopt = sub.add_parser("adopt", help="把目标里的一个文件提升为模板文件")
    p_adopt.add_argument("file_path", help="目标（目标仓或 ~/.claude）内某文件的路径")
    p_adopt.set_defaults(func=cmd_adopt)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
