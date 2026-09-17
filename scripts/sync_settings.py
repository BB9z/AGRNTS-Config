"""settings.json 的键级合并算法：纯数据变换，不碰文件系统、不依赖任何模块级路径常量。

独立成模块也不影响测试里对 sync.py 常量的整体替换。

模板托管它在自己 settings.json 里出现过的键，目标仓自行新增的键原样保留。对象逐层展开，数组与标量都是叶子，不再往下探；叶子路径的判定复用整文件判定用的同一套三方逻辑（在 sync.py 里以 decide() 驱动）。

本模块只提供 "展开成叶子字典" 与 "叶子字典写回成对象" 这两个方向的转换，以及数组的集合合并。
"""

from __future__ import annotations

_MISSING = object()


def flatten(obj) -> dict[tuple, object]:
    """把对象展开成 {键路径元组: 叶子值}。数组与标量都是叶子；字典展开，除非它是空字典——空字典没有子键可展开，本身就当叶子处理（值是 {}），否则像 {"a": {}} 这样的键会在没有任何叶子记录它的情况下，于 rebuild_object 时被当成空产物直接消失。根对象即便是空字典也不当叶子，维持整份文件「没有任何托管键」的原有语义。"""
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
    """字符串数组按集合三方合并：other 相对 base 新增的元素并入 current， other 相对 base 删除的元素从 current 移除，current 自己独有的元素保留。"""
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
    """用 original 的键序重建对象：叶子还在 leaves 里的键保留在原位置（取 leaves 里的新值），叶子被摘掉的键连同因此清空的父对象一并消失；leaves 里 original 从未出现过的新键路径，按路径排序追加在最外层末尾。"""

    def walk(node, prefix):
        # 判定是否展开成子树，必须跟 flatten() 用同一条件：空字典（非根）是叶子，不递归——否则永远查不到 leaves 里给它记的那条 {} 值。
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
    """两侧都偏离快照、且值不再一致的叶子路径怎么处理：字符串数组走集合合并，不会有冲突；其余类型（标量、混合数组）没有可靠的合并方式，保留各自现值，报告为冲突，交给用户在挂起环节里手工协调。

    返回 (target_new, template_new, conflicted)。
    """
    if t_val == l_val:
        # 两侧现值本来就一致，没有要协调的东西：不算冲突，直接确认这个值。
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
