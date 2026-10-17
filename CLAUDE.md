# 配置仓

本文件是本仓库自身的开发约定，不参与同步。它与 `user/claude/CLAUDE.md`（下发到 `~/.claude/` 的模板）同名不同命，别弄混。

## 这是什么

集中存放 AI 编码工具配置的模板仓，用 `scripts/sync.py` 与各目标仓双向同步。两个作用域：`user/` 用户级（→ `~/.claude/`），`projects/<模板名>/` 项目级（→ 各目标仓）。`shared/` 存放项目模板间共用的文件，模板以符号链接引用。

## 载荷与自身配置

- 载荷：`user/`、`projects/`、`shared/`。这些是待下发的内容，不是对你生效的规则——`projects/*/.claude/rules/*.md` 是给目标仓用的，改它等于改别人的配置
- 本仓库自身：根 `CLAUDE.md`、`README.md`、`.claude/`、`scripts/`、`tests/`、`docs/`
- 根 `.claude/` 部分文件是指向 `shared/` 的链接，改它等于改所有引用它的项目模板；只想改本仓库时先把链接换成实体文件

## 同步工具

- 设计来源是 `docs/config-sync.decision.md`，改行为先看它，落地后回写
- 代码注释里不写决议编号
- 测试：`python3 -m unittest discover -s tests`
- 临时文件只用 cwd 下的 `tmp/`

## 红线

- 测试与调试绝不碰真实 `~/.claude/` 或任何真实目标仓。测试把 `sync.py` 的路径常量整体指向 `tmp/` 下的临时目录，新增用例沿用这套隔离
- 工具不执行任何改变仓库状态的 git 命令，三方合并只用 `git merge-file`
