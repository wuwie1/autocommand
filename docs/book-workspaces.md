# 查找拆书资料与原创规划

下列命令从 `autocommand/` 根目录运行。盘点时读取当前工作区。已知书名时直接进入对应作品目录。

## 查拆书资料

参考书分析位于 `../ani-book-skill/analyses/<参考书目录>/`。按实际问题选最小的一组材料。

| 问题 | 先查哪里 | 核对什么 |
| --- | --- | --- |
| 哪些书拆完了，覆盖到了哪里？ | 各书的 `analysis-state.yaml` 和 `coverage-map.md` | `analysis.status` 是分析进度。用 `source.coverage` 和覆盖图确认全文或抽样，用 `source.read_scope`（如有）核实已读范围。 |
| 哪种写法可以借，前提是什么？ | 按问题浏览[拆书成果索引](../../ani-book-skill/analyses/README.md)，或在各书的 `pattern-cards.md` 中搜写法、人物行动或读者回报 | 读命中卡片的适用条件和反例。正式接入原创时依[机制迁移方法](../../ani-book-skill/references/analysis-to-novel.md)核验来源。 |
| 卡片的结论在哪一章有依据？ | 顺卡片的来源链接读分段笔记。给出 `CLAIM` 编号时，在同书的 `evidence-ledger.md` 查段号，打开 `notes/segment-*.md` | 核对原章号、证据范围和判断。跨段追踪人物或伏笔时可用[全文与关系检索](../../ani-book-skill/references/book-analysis-retrieval.md)。 |

列出分析状态文件，或以“代价”为例，直接查到机制卡的文件和行号。

```sh
rg --files --no-ignore --max-depth 2 -g 'analysis-state.yaml' ../ani-book-skill/analyses
rg -n --no-ignore -m 2 -g 'pattern-cards.md' '代价' ../ani-book-skill/analyses
```

新增分析或续拆现有参考书时，工作区结构和分段流程见[拆书约定](../../ani-book-skill/references/book-analysis.md)。想在浏览器里跨书搜索，可用本地阅读台，默认启动地址为 `http://127.0.0.1:8790`。[阅读台说明](../../ani-book-skill/apps/workspace-reader/README.md)

## 查原创规划

原创工作区位于 `../ani-book-skill/novels/<作品目录>/`。用状态文件识别作品，目录内也可存放参考书 TXT。

```sh
rg --files --no-ignore --max-depth 2 -g '小说状态.yaml' -g 'novel-state.yaml' ../ani-book-skill/novels
```

| 问题 | 在目标作品状态文件里读什么 | 接下来去哪 |
| --- | --- | --- |
| 这是哪本书，目前做到哪一步？ | `novel.title`、`novel.mode`、`novel.current_stage` 和 `next_action` | 读取下一步引用的计划或候选。要查已经生成的章节，到[Web 作品和章节页](web-handoff.md#功能与路径)核对。 |
| 当前生效的卷纲、人物、写法或章计划在哪？ | 查 `artifacts` 对应条目的 `path`、`status` 和 `depends_on` | 路径相对于该作品目录。打开登记的文件，调优时沿依赖核对受影响卷章。卷纲的创作规则见[故事与卷规划](../../ani-book-skill/references/story-and-volume-planning.md)。 |
| 哪一版规划已经交接到 Web？ | 已有交接记录时，从本仓 `handoffs/<书名>/<交接版本>/交接记录.md` 取源文件版本、大纲项目和小说入口 | 在目标环境核对绑定修订。[交接记录字段](web-handoff.md#交接记录) |

需要看一组实际文件如何对应时，见[带日期的查阅示例](../AGENT_MAP.md)。
