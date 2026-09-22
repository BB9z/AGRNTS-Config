"""Key-level merge helpers for settings.json: pure data transforms with no file system access.

Objects are flattened into leaf paths; arrays and scalars are leaves.
"""

from __future__ import annotations

_MISSING = object()


def flatten(obj) -> dict[tuple, object]:
    """Flatten to {key path tuple: leaf value}. Non-root empty dicts are leaves, so a key like {"a": {}} survives rebuild_object."""
    leaves: dict[tuple, object] = {}

    def walk(node, prefix):
        if isinstance(node, dict) and (node or prefix == ()):
            for k, v in node.items():
                walk(v, prefix + (k,))
        else:
            leaves[prefix] = node

    walk(obj, ())
    return leaves


def is_string_array(value) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def merge_string_array(base: list, current: list, other: list) -> list:
    """Three-way set merge: apply other's additions and removals relative to base onto current, keeping current's own elements."""
    base_set = set(base)
    other_set = set(other)
    removed = base_set - other_set
    added = [x for x in other if x not in base_set]
    result = [x for x in current if x not in removed]
    for x in added:
        if x not in result:
            result.append(x)
    return result


def rebuild_object(original: dict, leaves: dict[tuple, object]) -> dict:
    """Rebuild from leaves in original's key order, dropping parents left empty and appending new paths sorted."""

    def walk(node, prefix):
        # Same expand rule as flatten().
        if isinstance(node, dict) and (node or prefix == ()):
            out = {}
            for k, v in node.items():
                sub = walk(v, prefix + (k,))
                if sub is not _MISSING:
                    out[k] = sub
            return out if (out or prefix == ()) else _MISSING
        return leaves.get(prefix, _MISSING)

    result = walk(original, ())
    if result is _MISSING:
        result = {}

    existing_paths = set(flatten(original).keys())
    new_paths = sorted(p for p in leaves if p not in existing_paths)
    for path in new_paths:
        node = result
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = leaves[path]
    return result


def resolve_leaf_conflict(t_val, l_val, ts_val, ls_val):
    """Resolve a leaf both sides changed. String arrays are set-merged; anything else keeps each side's value and is a conflict.

    Returns (target_new, template_new, conflicted).
    """
    if t_val == l_val:
        return l_val, t_val, False
    candidates = [v for v in (t_val, l_val, ts_val, ls_val) if v is not None]
    if candidates and all(is_string_array(v) for v in candidates):
        t_arr = t_val if isinstance(t_val, list) else []
        l_arr = l_val if isinstance(l_val, list) else []
        ts_arr = ts_val if isinstance(ts_val, list) else []
        ls_arr = ls_val if isinstance(ls_val, list) else []
        target_new = merge_string_array(base=ts_arr, current=l_arr, other=t_arr)
        template_new = merge_string_array(base=ls_arr, current=t_arr, other=l_arr)
        return target_new, template_new, False
    return l_val, t_val, True
