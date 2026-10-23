# Agent Config

我的 AI 编码 agent 配置：规则、skills、commands、subagents、hooks、settings。

## 结构

- `user/claude/` → `~/.claude/`
- `projects/<模板>/` → 目标仓
- `shared/` 项目模板之间共用的文件，模板里用符号链接引用；`scripts/link_shared.py` 把模板里与 shared 一致的实体文件换成链接

## 同步工具用法

用 `scripts/sync.py` 与用户配置及各项目双向同步。

```sh
scripts/sync.py add <仓库路径> --template <模板>
scripts/sync.py status
scripts/sync.py sync [<ID|路径>|user] [--dry-run]
scripts/sync.py adopt <文件路径>
```

冲突时，工具把带冲突标记的内容写进两侧，用 `$EDITOR` 打开后等待回车。把两侧分别改成想要的内容再回车，回车时两侧的差异会记为定制，以后同步时保留。一侧删除、另一侧修改时不合并，同样等待处理。
