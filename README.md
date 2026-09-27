# Mir3 地图数据审计

中文静态报告对照 MUD3 英雄杀配置、mir3 网站怪物 / 地图目录，以及当前 Godot 客户端实际加载的地图数据库。范围限结构化数据，不读取 `.map` 文件、地图格子、贴图或渲染。报告给出结论、逐地图 NPC / 刷新 / 连接明细、逐项差异、未映射项、数据字典与来源指纹。

- 报告入口：`site/index.html`；部署后由 GitHub Pages 发布。
- 原始机器数据：[`audit.json`](site/data/audit.json)、[`comparisons.csv`](site/data/comparisons.csv)、[`issues.json`](site/data/issues.json)。
- 发布工作流：[`pages.yml`](.github/workflows/pages.yml)；Pages 构建先验证数据、运行测试、检查静态资源，再上传 `dist/`。

## 本地复核

所有输入目录在运行时通过环境变量提供；不要将本机路径、原始 MUD3 配置或 System.db 复制到本仓库。

```sh
python scripts/audit.py extract \
  --mud3-envir \"$MIR3_MUD3_ROOT/Envir\" \
  --website-data \"$MIR3_WEBSITE_ROOT/data\" \
  --godot-system-db \"$MIR3_ZIRCON_ROOT/Debug/Client/Data/System.db\" \
  --zircon-repo \"$MIR3_ZIRCON_ROOT\" \
  --output site/data

python scripts/audit.py validate
python -m unittest discover -s tests -v
python scripts/build_site.py
```

提取 Godot 数据需要本地 .NET 10 SDK 与 `$MIR3_ZIRCON_ROOT` 中的 `LibraryCore` 源码；导出器以只读 MirDB Session 读取 `System.db`。静态 Pages 构建不访问这些输入源。

审计目标、来源角色与验收边界见 [`MIR3MAP_AUDIT_GOAL.md`](MIR3MAP_AUDIT_GOAL.md)；完整口径与字段含义见报告首页。
