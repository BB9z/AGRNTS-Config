#!/usr/bin/env python3
import difflib
import itertools
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

CONTEXT = 2
# Above this many lines in one changed run, exact matching gets quadratic on repetitive lines; autojunk trades alignment quality for speed.
AUTOJUNK_THRESHOLD = 4000
GAP_MARK = "  ..."


@dataclass
class Entry:
    status: str
    old_path: str
    new_path: str


@dataclass
class Section:
    status: str
    path: str
    start: int
    end: int


def run_git(args: list[str]) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def difft_json_objects(diff_range: list[str]) -> dict[str, dict]:
    env = os.environ.copy()
    env.update(GIT_EXTERNAL_DIFF="difft", DFT_DISPLAY="json", DFT_UNSTABLE="yes")
    raw = subprocess.run(
        # --find-renames is forced so rename detection doesn't depend on the
        # user's global diff.renames setting.
        ["git", "diff", "--find-renames", *diff_range],
        capture_output=True, text=True, env=env, check=True,
    ).stdout

    # difft writes one JSON object per file, back-to-back with no separators
    # or wrapping array, so a plain json.loads(raw) only works for one file.
    decoder = json.JSONDecoder()
    objects = {}
    raw = raw.strip()
    idx = 0
    while idx < len(raw):
        obj, idx = decoder.raw_decode(raw, idx)
        objects[obj["path"]] = obj
        while idx < len(raw) and raw[idx].isspace():
            idx += 1
    return objects


def read_lines(rev: str, path: str) -> list[str]:
    result = subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.splitlines()
    if "does not exist" in result.stderr:
        return []
    raise RuntimeError(result.stderr.strip())


def changed_line_numbers(chunks: list[list[dict]]) -> tuple[set[int], set[int]]:
    old, new = set(), set()
    for chunk in chunks:
        for row in chunk:
            if "lhs" in row:
                old.add(row["lhs"]["line_number"])
            if "rhs" in row:
                new.add(row["rhs"]["line_number"])
    return old, new


def at(lines: list[str], idx: int | None) -> str | None:
    # difft's aligned_lines can include a trailing EOF sentinel pair whose
    # numbers equal each file's line count (one past the last valid index) —
    # not real content, so out-of-range indices must be treated as absent.
    return lines[idx] if idx is not None and 0 <= idx < len(lines) else None


def render_changed(old_lines: list[str], new_lines: list[str], obj: dict) -> list[str]:
    changed_old, changed_new = changed_line_numbers(obj.get("chunks", []))
    aligned = obj.get("aligned_lines", [])

    # difft decides whether a region changed; the text only decides how to show it.
    ops: list[tuple[str, str]] = []
    runs = itertools.groupby(
        aligned, key=lambda row: (row[0] is not None and row[0] in changed_old) or (row[1] is not None and row[1] in changed_new)
    )
    for is_marked, rows in runs:
        rows = list(rows)
        if not is_marked:
            # Rows difft considers unchanged (including pure reformatting) show the new side only.
            ops.extend((" ", line) for _, n in rows if (line := at(new_lines, n)) is not None)
            continue

        # A changed atom such as a comment marks every line it spans and aligns them by position, so re-align the run by text.
        olds = [line for o, _ in rows if (line := at(old_lines, o)) is not None]
        news = [line for _, n in rows if (line := at(new_lines, n)) is not None]
        matcher = difflib.SequenceMatcher(None, olds, news, autojunk=len(olds) + len(news) > AUTOJUNK_THRESHOLD)
        for tag, o1, o2, n1, n2 in matcher.get_opcodes():
            if tag == "equal":
                ops.extend((" ", line) for line in news[n1:n2])
            else:
                ops.extend(("-", line) for line in olds[o1:o2])
                ops.extend(("+", line) for line in news[n1:n2])

    keep = [False] * len(ops)
    for idx, (kind, _) in enumerate(ops):
        if kind != " ":
            for j in range(max(0, idx - CONTEXT), min(len(ops), idx + CONTEXT + 1)):
                keep[j] = True

    out = []
    prev_kept = False
    for idx, (kind, line) in enumerate(ops):
        if not keep[idx]:
            if prev_kept:
                out.append(GAP_MARK)
            prev_kept = False
            continue
        prev_kept = True
        out.append(f"{kind} {line}")
    return out


def render_entry(entry: Entry, obj: dict | None, old_rev: str, new_rev: str) -> list[str]:
    if obj and obj.get("language") == "Binary":
        return ["  binary file"]

    if entry.status == "A":
        return [f"+ {line}" for line in read_lines(new_rev, entry.new_path)]

    if entry.status == "D":
        return [f"- {line}" for line in read_lines(old_rev, entry.old_path)]

    lines = []
    if entry.status.startswith("R"):
        lines.append(f"  renamed from {entry.old_path}")
    elif entry.status.startswith("C"):
        lines.append(f"  copied from {entry.old_path}")

    old_lines = read_lines(old_rev, entry.old_path)
    new_lines = read_lines(new_rev, entry.new_path)
    if old_lines == new_lines:
        # Pure rename/copy, or a metadata-only change such as a mode bit.
        if not lines:
            lines.append("  no content change")
        return lines
    if obj is None:
        lines.append("  (difft output missing)")
        return lines
    if not obj.get("chunks"):
        lines.append("  formatting only")
        return lines

    lines.extend(render_changed(old_lines, new_lines, obj))
    return lines


def main() -> int:
    if shutil.which("difft") is None:
        print("difft not found. Install: brew install difftastic", file=sys.stderr)
        return 1

    ref = sys.argv[1] if len(sys.argv) > 1 else None
    diff_range = ["--cached"] if ref is None else [f"{ref}~1", ref]
    old_rev = "HEAD" if ref is None else f"{ref}~1"
    new_rev = "" if ref is None else ref  # "" + ":path" == ":path" (the index)

    try:
        repo_root = run_git(["rev-parse", "--show-toplevel"]).strip()
        # -z keeps paths verbatim; the default output quotes non-ASCII paths and
        # would break on a path containing a tab or newline.
        name_status = run_git(["diff", "--find-renames", "--name-status", "-z", *diff_range])
    except subprocess.CalledProcessError as e:
        print(e.stderr.strip() or f"git {' '.join(e.cmd[1:])} failed", file=sys.stderr)
        return 1

    entries = []
    fields = name_status.split("\0")
    i = 0
    while i < len(fields) and fields[i]:
        status = fields[i]
        # A rename/copy record carries both source and destination path;
        # every other record carries one path.
        if status[0] in "RC":
            entries.append(Entry(status, fields[i + 1], fields[i + 2]))
            i += 3
        else:
            entries.append(Entry(status, fields[i + 1], fields[i + 1]))
            i += 2

    if not entries:
        print("No staged changes." if ref is None else f"No changes in {ref}.")
        return 0

    tmp_dir = os.path.join(repo_root, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    diff_file = os.path.join(tmp_dir, "review-diff.txt")

    objects_by_path = difft_json_objects(diff_range)

    body: list[str] = []
    sections: list[Section] = []
    for entry in entries:
        if body:
            body.append("")
        start = len(body) + 1
        body.append(entry.new_path)
        try:
            body.extend(render_entry(entry, objects_by_path.get(entry.new_path), old_rev, new_rev))
        except Exception as e:
            body.append(f"  (diff unavailable: {e})")
        sections.append(Section(entry.status, entry.new_path, start, len(body)))

    with open(diff_file, "w") as f:
        f.write("\n".join(body) + "\n")

    print(f"Diff file: {diff_file}")
    print()
    for section in sections:
        print(f"{section.status} {section.path} L{section.start}-{section.end}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
