#!/usr/bin/env python3
"""Config sync tool: syncs this template repo's content into target repos and brings target-side edits back.

See --help for usage and docs/config-sync.decision.md for the design background.
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
# Single source for files shared by project templates, which link to them; project-level pullbacks write through those links.
SHARED_ROOT = os.path.join(REPO_ROOT, "shared")
STATE_ROOT = os.path.join(REPO_ROOT, ".state")
CONFIG_PATH = os.path.join(STATE_ROOT, "config.json")
SNAPSHOT_ROOT = os.path.join(STATE_ROOT, "snapshots")
BACKUP_ROOT = os.path.join(REPO_ROOT, ".backup")

# User level: fixed roots, and a short ID that can't collide with numeric ones.
USER_TEMPLATE_ROOT = os.path.join(REPO_ROOT, "user", "claude")
USER_TARGET_ROOT = os.path.expanduser("~/.claude")
USER_SHORT_ID = "user"

# Importable whether this file is run directly or loaded by path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sync_settings as jsonmerge  # noqa: E402

# Machine-local, so never synced.
EXCLUDE_RELPATHS = frozenset({".claude/settings.local.json"})


def is_excluded(relpath: str) -> bool:
    return relpath in EXCLUDE_RELPATHS


def gitignored_relpaths(template_root: str, relpaths) -> set[str]:
    """Relpaths ignored by the template repo's .gitignore. Runs from REPO_ROOT because template_root may not exist."""
    rels = list(relpaths)
    if not rels:
        return set()
    prefix = os.path.relpath(template_root, REPO_ROOT)
    args = [rel if prefix == "." else f"{prefix}/{rel}" for rel in rels]
    proc = subprocess.run(
        ["git", "check-ignore", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"git check-ignore failed: {proc.stderr}")
    ignored = set(proc.stdout.splitlines())
    return {rel for rel, arg in zip(rels, args) if arg in ignored}


def atomic_write_bytes(path: str, data: bytes, executable: bool) -> None:
    """Write data to path atomically via a temp file and rename, so a crash never leaves a half-written file."""
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
    """Return (content, executable) of relpath under root, following symlinks, or None if absent. Other I/O errors propagate: treating them as absence would propagate a deletion."""
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
    """A write resolved to a real path outside the sync scope."""


def _is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def resolve_write_path(root: str, relpath: str) -> str:
    """Real path a write to relpath lands on, following symlinks so the link itself is kept. Raises OutOfScope outside root, plus shared/ for project templates."""
    path = os.path.join(root, *relpath.split("/"))
    real = os.path.realpath(path)
    real_root = os.path.realpath(root)
    scopes = [real_root]
    if _is_within(real_root, os.path.realpath(PROJECTS_ROOT)):
        scopes.append(os.path.realpath(SHARED_ROOT))
    if not any(_is_within(real, scope) for scope in scopes):
        raise OutOfScope(real)
    return real


def write_state(root: str, relpath: str, state) -> None:
    data, executable = state
    real_path = resolve_write_path(root, relpath)
    atomic_write_bytes(real_path, data, executable)


def remove_state(root: str, relpath: str) -> None:
    """Delete relpath without following symlinks, then prune emptied parents up to root."""
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
    """List file relpaths under root, following symlinks; broken and circular links go into issues.

    Only absence is skipped. Other I/O errors propagate, since files missed by a partial scan would read as deletions.
    """
    files: dict[str, bool] = {}
    issues: list[tuple[str, str]] = []

    def walk(dir_path: str, rel_prefix: str, ancestry: frozenset):
        try:
            entries = list(os.scandir(dir_path))
        except (FileNotFoundError, NotADirectoryError):
            # A missing root is an empty tree; a missing subdirectory was moved away mid-scan.
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
                    # A self-referencing link fails here, before the ancestry check.
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
    """Union of both snapshot trees, which can diverge, e.g. when one side is deleted by hand during a hold."""
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
    """Three-way decision on content and executable bit, never mtime. t/l are the current template/target states, ts/ls their snapshots; None means absent.

    Differing snapshots are a known customization, so a one-sided change then goes through a merge instead of overwriting the other side. Deletions always propagate directly.
    """
    if not has_snapshot:
        if t is not None and l is None:
            return "push"
        if t is not None and l is not None:
            return "first_adopt"
        # Only in the target: the target repo's own file.
        return "none"

    t_changed = t != ts
    l_changed = l != ls
    if not t_changed and not l_changed:
        return "none"
    if t is None and l is None:
        # Nothing left to compare; clearing the snapshot lets it converge.
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
    # Snapshots may hold paths saved before an exclusion rule existed.
    candidates = {
        rel for rel in set(template_files) | list_snapshot_relpaths(short_id)
        if not is_excluded(rel)
    }
    universe = candidates - gitignored_relpaths(template_root, candidates)
    return universe, issues


def plan_sync(short_id: str, template_root: str, target_root: str):
    """Decide each file's action without writing anything."""
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


NEEDS_REVIEW = "NEEDS REVIEW"

# Report line per file: (label, note), keyed by action, outcome, or scan issue; "{repo}" is the target's display name.
REPORT_LINES = {
    "push": ("template → {repo}", ""),
    "first_adopt": ("template → {repo}", "first sync, {repo} version replaced"),
    "pullback": ("{repo} → template", ""),
    "delete_target": ("removed from {repo}", "deleted in template"),
    "delete_template": ("removed from template", "deleted in {repo}"),
    "needs_merge": ("will merge", ""),
    "merged_clean": ("merged", ""),
    "merged_conflict": (NEEDS_REVIEW, "conflict markers in both files"),
    "skip_absent": (NEEDS_REVIEW, "deleted on one side, edited on the other"),
    "skip_binary": (NEEDS_REVIEW, "binary file changed on both sides"),
    "settings_merged_clean": ("merged", ""),
    "settings_merged_conflict": (NEEDS_REVIEW, "conflicting keys"),
    "settings_invalid_json": (NEEDS_REVIEW, "invalid JSON"),
    "out_of_scope": ("skipped", "symlink points outside the template or {repo}"),
    "broken": ("skipped", "broken symlink"),
    "circular": ("skipped", "symlink loop"),
}

# Not worth reporting: both sides deleted only clears a snapshot.
_SILENT_ACTIONS = ("none", "both_deleted")


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def print_report(actions, issues, repo_name: str) -> None:
    lines = []
    for a in actions:
        if a["action"] in _SILENT_ACTIONS:
            continue
        label, note = REPORT_LINES[a.get("outcome", a["action"])]
        notes = [note] if note else []
        if a.get("settings_conflict_paths"):
            notes[-1] += ": " + ", ".join(a["settings_conflict_paths"])
        if a.get("exec_conflict"):
            notes.append("executable bit differs, each side kept as is")
        lines.append((label, a["relpath"], "; ".join(notes)))
    for rel, reason in issues:
        label, note = REPORT_LINES[reason]
        lines.append((label, rel, note))

    lines = [(label.format(repo=repo_name), rel, note.format(repo=repo_name)) for label, rel, note in lines]
    width = max((len(label) for label, _, _ in lines), default=0)
    for label, rel, note in lines:
        print(f"  {label:<{width}}  {rel}" + (f"  ({note})" if note else ""))

    skipped = sum(1 for label, _, _ in lines if label == "skipped")
    review = sum(1 for label, _, _ in lines if label == NEEDS_REVIEW)
    changed = len(lines) - skipped - review
    print(f"{changed} changed, {review} need review, {skipped} skipped.")


def backup_before_overwrite(batch_dir: str, short_id: str, side: str, relpath: str, current_state) -> None:
    """Back up content about to be overwritten or deleted into this batch's directory."""
    if current_state is None:
        return
    data, executable = current_state
    backup_path = os.path.join(batch_dir, short_id, side, *relpath.split("/"))
    atomic_write_bytes(backup_path, data, executable)


def apply_writes(batch_dir, short_id, relpath, writes) -> bool:
    """Write each side's new state, skipping unchanged sides. writes is [(side, root, current, new)].

    If any side resolves outside the sync scope, nothing is written, so a merge never lands on one side only; returns False so the caller leaves the snapshot alone and the change is reported again.
    """
    changed = [(side, root, cur, new) for side, root, cur, new in writes if cur != new]
    for _side, root, _cur, _new in changed:
        try:
            resolve_write_path(root, relpath)
        except OutOfScope:
            return False
    for side, root, cur, new in changed:
        backup_before_overwrite(batch_dir, short_id, side, relpath, cur)
        write_state(root, relpath, new)
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
    """A None state removes the snapshot file."""
    if state is None:
        remove_state(root, relpath)
    else:
        write_state(root, relpath, state)


def save_snapshot_pair(short_id: str, template_root: str, target_root: str, relpath: str) -> None:
    """Snapshot what both sides contain right now."""
    t = read_state(template_root, relpath)
    l = read_state(target_root, relpath)
    save_one_snapshot(snapshot_dir(short_id, "template"), relpath, t)
    save_one_snapshot(snapshot_dir(short_id, "target"), relpath, l)


def is_text_content(data: bytes) -> bool:
    return b"\x00" not in data


_CONFLICT_MARKER_PREFIXES = (b"<<<<<<<", b">>>>>>>")


def has_conflict_markers(data: bytes) -> bool:
    """Whether git conflict marker lines remain."""
    for line in data.split(b"\n"):
        if line.startswith(_CONFLICT_MARKER_PREFIXES[0]) or line.startswith(_CONFLICT_MARKER_PREFIXES[1]):
            return True
    return False


def merge_bool_3way(base: bool, current: bool, other: bool) -> tuple[bool, bool]:
    """Three-way merge of the executable bit. On a real conflict keeps current and returns True as the second value."""
    if current == other:
        return current, False
    if current == base:
        return other, False
    if other == base:
        return current, False
    return current, True


def git_merge_file(current: bytes, base: bytes, other: bytes, labels: tuple[str, str, str]) -> tuple[bytes, int]:
    """Three-way merge via git merge-file -p. Returns (content, conflict count); raises if git itself fails."""
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
                f"git merge-file failed: {proc.stderr.decode('utf-8', 'replace')}"
            )
        return proc.stdout, proc.returncode


def apply_needs_merge(batch_dir, short_id, template_root, target_root, a: dict):
    """Merge a file changed on both sides, or changed on one side while customized.

    Each side takes the other's change with the other side's snapshot as base. Using its own snapshot as base would count the customization itself as a change and erase it.

    Returns (outcome, exec_conflict, paths to open). Files absent on one side or non-text are held without merging.
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
        labels=("repo", "template (last sync)", "template"),
    )
    template_merged, template_conflicts = git_merge_file(
        current=t_data, base=ls_data, other=l_data,
        labels=("template", "repo (last sync)", "repo"),
    )
    target_exec, target_exec_conflict = merge_bool_3way(ts_exec, l_exec, t_exec)
    template_exec, template_exec_conflict = merge_bool_3way(ls_exec, t_exec, l_exec)

    writes = [
        ("target", target_root, l, (target_merged, target_exec)),
        ("template", template_root, t, (template_merged, template_exec)),
    ]
    if not apply_writes(batch_dir, short_id, rel, writes):
        return "out_of_scope", False, []

    conflicted = target_conflicts != 0 or template_conflicts != 0
    editor_paths = [template_path, target_path] if conflicted else []
    exec_conflict = target_exec_conflict or template_exec_conflict
    return ("merged_conflict" if conflicted else "merged_clean"), exec_conflict, editor_paths


def is_settings_json(relpath: str) -> bool:
    return relpath.rsplit("/", 1)[-1] == "settings.json"


_SETTINGS_INVALID = object()
_ABSENT = object()


def _load_json_side(state):
    """Parse a settings.json state; absent counts as {}. Returns _SETTINGS_INVALID unless it's a JSON object, so a broken file is held rather than partially trusted."""
    if state is None:
        return {}
    try:
        obj = json.loads(state[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _SETTINGS_INVALID
    return obj if isinstance(obj, dict) else _SETTINGS_INVALID


def is_customized(relpath: str, ts, ls) -> bool:
    """Whether the snapshot pair records a customization. For settings.json only template-managed leaves count; target-only keys and key order don't."""
    if ts is None or ls is None:
        return False
    if not is_settings_json(relpath) or ts == ls:
        return ts != ls
    if ts[1] != ls[1]:
        return True
    ts_obj, ls_obj = _load_json_side(ts), _load_json_side(ls)
    if _SETTINGS_INVALID in (ts_obj, ls_obj):
        return True
    ls_leaves = jsonmerge.flatten(ls_obj)
    return any(ls_leaves.get(path, _ABSENT) != value for path, value in jsonmerge.flatten(ts_obj).items())


def apply_settings_merge(batch_dir, short_id, template_root, target_root, a: dict):
    """Key-level merge of settings.json. Managed leaves are those in the template or its snapshot; target-only keys are left alone. Each leaf goes through decide(). A conflicting leaf keeps each side's value and is reported, since conflict markers would break the JSON.

    Same return contract as apply_needs_merge.
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
            # target_leaves already holds lv; the template takes it too.
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

    writes = [
        ("target", target_root, l, (new_target_bytes, target_exec)),
        ("template", template_root, t, (new_template_bytes, template_exec)),
    ]
    if not apply_writes(batch_dir, short_id, rel, writes):
        return "out_of_scope", False, []

    exec_conflict = target_exec_conflict or template_exec_conflict
    if conflicted_paths:
        a["settings_conflict_paths"] = [".".join(p) for p in sorted(conflicted_paths)]
        return "settings_merged_conflict", exec_conflict, [template_path, target_path]
    return "settings_merged_clean", exec_conflict, []


def wait_for_enter(prompt: str) -> bool:
    """Show prompt and wait for Enter. Without a terminal prints nothing and returns False, meaning nobody confirmed."""
    if not sys.stdin.isatty():
        return False
    print(prompt)
    try:
        input()
    except EOFError:
        pass
    return True


def open_editor(paths: list[str]) -> None:
    """Open files in $EDITOR without waiting, so the user can edit either side by any means during the hold. Does nothing without a terminal."""
    if not sys.stdin.isatty():
        return
    editor = os.environ.get("EDITOR")
    if not editor:
        print("$EDITOR is not set; open these files yourself:")
        for p in paths:
            print(f"  {p}")
        return
    try:
        subprocess.Popen([editor, *paths])
    except OSError as e:
        print(f"Couldn't start $EDITOR ({e}); open these files yourself:")
        for p in paths:
            print(f"  {p}")


# Nothing confirmable was written; snapshotting these would hide an unresolved change.
_NEVER_SNAPSHOT_OUTCOMES = ("skip_absent", "skip_binary", "out_of_scope", "settings_invalid_json")
_HOLD_OUTCOMES = ("merged_conflict", "settings_merged_conflict")


def apply_force_override(actions: list, side: str) -> None:
    """Escape hatch: make every differing file match `side`, ignoring snapshots and the other side's changes."""
    for a in actions:
        if a["action"] in ("none", "both_deleted"):
            continue
        if side == "template":
            a["action"] = "delete_target" if a["t"] is None else "push"
        else:
            a["action"] = "delete_template" if a["l"] is None else "pullback"
        a.pop("outcome", None)
        a.pop("exec_conflict", None)
        a.pop("settings_conflict_paths", None)
        a["forced"] = True


def execute_sync(short_id: str, template_root: str, target_root: str, actions: list, issues: list, title: str, is_user: bool = False) -> int:
    """Apply the plan, hold once for review, then snapshot both sides' actual content.

    Snapshots wait until after the hold so reverts made during it register as customizations.

    Returns 1 if held files went unconfirmed (no terminal) or still have conflict markers; their snapshots aren't saved, so they are reported again.
    """
    batch_dir = os.path.join(BACKUP_ROOT, datetime.now().strftime("%Y%m%dT%H%M%S%f"))

    snapshot_pending: list[str] = []
    hold_relpaths: list[str] = []
    editor_paths: list[str] = []

    def record_merge(a, outcome, exec_conflict, paths):
        a["outcome"] = outcome
        if exec_conflict:
            a["exec_conflict"] = True
        if outcome not in _NEVER_SNAPSHOT_OUTCOMES:
            snapshot_pending.append(a["relpath"])
            if outcome in _HOLD_OUTCOMES:
                hold_relpaths.append(a["relpath"])
        editor_paths.extend(paths)

    for a in actions:
        rel = a["relpath"]
        action = a["action"]
        if action == "none":
            continue

        # Forced overwrites replace settings.json whole instead of merging keys.
        if is_settings_json(rel) and not a.get("forced") and action in ("push", "pullback", "needs_merge", "first_adopt"):
            record_merge(a, *apply_settings_merge(batch_dir, short_id, template_root, target_root, a))
            if a["outcome"] == "settings_merged_clean" and action != "needs_merge":
                # To the user, a one-sided change merged cleanly is just a push or pullback.
                del a["outcome"]
            continue

        if action in ("push", "first_adopt", "pullback"):
            if action == "pullback":
                write = ("template", template_root, a["t"], a["l"])
            else:
                write = ("target", target_root, a["l"], a["t"])
            if apply_writes(batch_dir, short_id, rel, [write]):
                snapshot_pending.append(rel)
            else:
                a["outcome"] = "out_of_scope"
        elif action == "delete_target":
            apply_delete(batch_dir, short_id, "target", target_root, rel, a["l"])
            clear_snapshot_pair(short_id, rel)
        elif action == "delete_template":
            apply_delete(batch_dir, short_id, "template", template_root, rel, a["t"])
            clear_snapshot_pair(short_id, rel)
        elif action == "both_deleted":
            clear_snapshot_pair(short_id, rel)
        elif action == "needs_merge":
            record_merge(a, *apply_needs_merge(batch_dir, short_id, template_root, target_root, a))

    repo_name = "~/.claude" if is_user else "repo"
    print(f"Syncing {title}")
    print_report(actions, issues, repo_name)

    # One hold per sync, covering every file whose snapshot waits on confirmation. Deletions are already done.
    got_confirmation = True
    if snapshot_pending or editor_paths:
        if editor_paths:
            print("Files to review:")
            for p in editor_paths:
                print(f"  {p}")
            open_editor(editor_paths)
        if is_user:
            check = f"Overwritten files were backed up to {batch_dir}; diff against ~/.claude to check."
        else:
            check = "Check the changes with git diff in both repos."
        got_confirmation = wait_for_enter(
            f"{check}\n"
            f"Revert anything you don't want. Changes you revert in the template stay as customizations of {'~/.claude' if is_user else 'this repo'}.\n"
            "Press Enter when done..."
        )
        if not got_confirmation and editor_paths:
            print("No terminal to confirm. Files needing review are left as is and will be reported again next run.")

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
        print("These files still have conflict markers and will be reported again next run:")
        for rel in still_conflicted:
            print(f"  {rel}")

    exit_code = 1 if (editor_paths and not got_confirmation) or still_conflicted else 0
    if exit_code == 0:
        print("Done.")
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
        print(f"error: no template named '{template}' under projects/", file=sys.stderr)
        return 1

    target_path = os.path.abspath(args.target_path)
    if not os.path.isdir(target_path):
        print(f"error: not a directory: {target_path}", file=sys.stderr)
        return 1

    config = load_config()
    for short_id, entry in config["targets"].items():
        if os.path.abspath(entry["path"]) == target_path:
            print(f"error: {target_path} is already registered as repo {short_id}", file=sys.stderr)
            return 1

    short_id = str(config["next_id"])
    config["next_id"] += 1
    config["targets"][short_id] = {"path": target_path, "template": template}
    save_config(config)
    print(f"Registered repo {short_id}: {target_path} (template: {template})")
    return 0


def resolve_target(config: dict, ref: str):
    """Find a target by short ID or path; (None, None) if not found."""
    if ref in config["targets"]:
        return ref, config["targets"][ref]
    ref_abs = os.path.abspath(ref)
    for short_id, entry in config["targets"].items():
        if os.path.abspath(entry["path"]) == ref_abs:
            return short_id, entry
    return None, None


def pick_target_interactively(config: dict):
    """Pick a registered repo interactively; refuses without a terminal."""
    if not sys.stdin.isatty():
        print("error: no repo given, and no terminal to choose one", file=sys.stderr)
        return None, None
    targets = sorted(config["targets"].items(), key=lambda kv: int(kv[0]))
    if not targets:
        print("error: no repos registered yet. Add one: sync.py add <path> --template <name>", file=sys.stderr)
        return None, None
    print("Registered repos:")
    for i, (short_id, entry) in enumerate(targets, 1):
        print(f"  {i}. [{short_id}] {entry['path']} ({entry['template']})")
    try:
        choice = input("Choose a number: ").strip()
    except EOFError:
        print("Cancelled.")
        return None, None
    if not choice.isdigit() or not (1 <= int(choice) <= len(targets)):
        print("error: invalid choice", file=sys.stderr)
        return None, None
    short_id, entry = targets[int(choice) - 1]
    try:
        confirm = input(f"Sync [{short_id}] {entry['path']}? [y/N] ").strip().lower()
    except EOFError:
        confirm = ""
    if confirm != "y":
        print("Cancelled.")
        return None, None
    return short_id, entry


def status_summary(short_id: str, template_root: str, target_root: str) -> str:
    actions, issues = plan_sync(short_id, template_root, target_root)
    pending = sum(1 for a in actions if a["action"] not in _SILENT_ACTIONS)
    customized = sum(1 for a in actions if is_customized(a["relpath"], a["ts"], a["ls"]))
    summary = f"{pending} pending, {customized} customized"
    if issues:
        summary += f", {len(issues)} skipped"
    return summary


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config()
    targets = sorted(config["targets"].items(), key=lambda kv: int(kv[0]))
    user_available = os.path.isdir(USER_TEMPLATE_ROOT)
    if not targets and not user_available:
        print("No repos registered.")
        return 0

    rows = [
        (short_id, entry["path"], os.path.join(PROJECTS_ROOT, entry["template"]), entry["template"])
        for short_id, entry in targets
    ]
    if user_available:
        rows.append((USER_SHORT_ID, USER_TARGET_ROOT, USER_TEMPLATE_ROOT, "user/claude"))

    for short_id, target_root, template_root, template_name in rows:
        if not os.path.isdir(target_root):
            summary = "repo not found"
        elif not os.path.isdir(template_root):
            summary = "template not found"
        else:
            try:
                summary = status_summary(short_id, template_root, target_root)
            except (OSError, RuntimeError) as e:
                summary = f"error: {e}"
        print(f"[{short_id}] {target_root} ({template_name}): {summary}")
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
            print(
                f"error: unknown repo '{args.target}'. Register it first: sync.py add <path> --template <name>",
                file=sys.stderr,
            )
            return 1

    is_user = short_id == USER_SHORT_ID
    template_root = USER_TEMPLATE_ROOT if is_user else os.path.join(PROJECTS_ROOT, entry["template"])
    target_root = entry["path"]
    title = (
        f"{target_root} ⇄ user/claude" if is_user
        else f"repo {short_id} ({target_root}) ⇄ template {entry['template']}"
    )

    if not os.path.isdir(template_root):
        print(f"error: template not found: {template_root}", file=sys.stderr)
        return 1
    if not os.path.isdir(target_root):
        hint = "" if is_user else ". If it moved, update its path in .state/config.json"
        print(f"error: repo not found at {target_root}{hint}", file=sys.stderr)
        return 1

    if args.force_template:
        print("Warning: --force-template makes the repo match the template, discarding repo customizations.")
    elif args.force_target:
        print("Warning: --force-target makes the template match the repo, discarding template changes.")

    try:
        actions, issues = plan_sync(short_id, template_root, target_root)

        if args.force_template:
            apply_force_override(actions, "template")
        elif args.force_target:
            apply_force_override(actions, "target")

        if args.dry_run:
            print(f"Dry run, nothing will be written. {title}")
            print_report(actions, issues, "~/.claude" if is_user else "repo")
            return 0

        return execute_sync(short_id, template_root, target_root, actions, issues, title, is_user=is_user)
    except (OSError, RuntimeError) as e:
        # Absence is handled in read_state, so this is a real I/O or git failure; abort rather than treat it as a deletion.
        print(f"error: sync aborted: {e}", file=sys.stderr)
        return 1


def cmd_adopt(args: argparse.Namespace) -> int:
    """Copy a file from a registered repo or ~/.claude into its template and record a snapshot pair. Never overwrites an existing template file."""
    file_path = os.path.abspath(args.file_path)
    if not os.path.isfile(file_path):
        print(f"error: file not found: {file_path}", file=sys.stderr)
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
        print(f"error: {file_path} is not inside a registered repo or ~/.claude", file=sys.stderr)
        return 1
    short_id, target_root, template_root, rel = match

    if is_excluded(rel) or gitignored_relpaths(template_root, [rel]):
        print(f"error: {rel} is excluded from sync (gitignored or machine-local)", file=sys.stderr)
        return 1

    state = read_state(target_root, rel)
    if state is None:
        print(f"error: can't read {file_path}", file=sys.stderr)
        return 1

    template_path = os.path.join(template_root, *rel.split("/"))
    if read_state(template_root, rel) is not None:
        print(f"error: template already has {template_path}; not overwriting", file=sys.stderr)
        return 1

    write_state(template_root, rel, state)
    save_snapshot_pair(short_id, template_root, target_root, rel)
    print(f"Added to template: {rel} → {template_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync.py", description="Sync AI tool config between this template repo and your project repos."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="register a repo")
    p_add.add_argument("target_path", help="path to the repo")
    p_add.add_argument("--template", required=True, help="template name under projects/")
    p_add.set_defaults(func=cmd_add)

    p_status = sub.add_parser("status", help="show pending changes and customizations per repo, including ~/.claude")
    p_status.set_defaults(func=cmd_status)

    p_sync = sub.add_parser("sync", help="sync one repo; 'user' for ~/.claude; omit to pick interactively")
    p_sync.add_argument("target", nargs="?", default=None, help="repo ID, repo path, or 'user'")
    p_sync.add_argument("--dry-run", action="store_true", help="show what would change without writing anything")
    force_group = p_sync.add_mutually_exclusive_group()
    force_group.add_argument(
        "--force-template", action="store_true", help="make the repo match the template, discarding repo customizations"
    )
    force_group.add_argument(
        "--force-target", action="store_true", help="make the template match the repo"
    )
    p_sync.set_defaults(func=cmd_sync)

    p_adopt = sub.add_parser("adopt", help="copy a file from a repo or ~/.claude into its template")
    p_adopt.add_argument("file_path", help="path to a file inside a registered repo or ~/.claude")
    p_adopt.set_defaults(func=cmd_adopt)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
