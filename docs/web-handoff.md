# 从本地规划到 Web 章节

交接有两个版本要能对上。一个是作者选用的本地规划，另一个是 Web 开书时实际绑定的大纲修订。准备交接稿、导入大纲、绑定小说和写正文可以分别交付。

## 功能与路径

页面路径相对于本次使用的 Web 环境。按项目默认开发配置，本机导入页为 `http://localhost:5173/outlines`。已有交接记录时可直接使用其中的环境和入口。[Web 启动说明](../../assistant/README.md)

`/outlines` 下的 `:id` 是大纲项目 ID，`/novels` 下的 `:id` 是小说 ID，`:chapterId` 是章节 ID。其他页面地址查[前端路由](../../assistant/client/src/router/index.tsx)，大纲 API 查[接口路由](../../assistant/server/src/modules/planning/outline/http/outlineRoutes.ts)。

| 要查的问题 | 先打开哪里，核对什么 | 排查实现时读哪里 |
| --- | --- | --- |
| 导入漏了一章或分错卷 | `/outlines` → “导入大纲” → “解析并预览”。按章名找原文片段，依次看未匹配内容和章节所属卷 | [导入确认页](../../assistant/client/src/pages/outlines/OutlineImportPreview.tsx) · [解析与卷章映射](../../assistant/server/src/modules/planning/outline/application/OutlineImportPreviewService.ts) |
| 开书实际采用哪一版 | 从 `/outlines/:id` 查看大纲修订，从 `/outlines/:id/open-book` 查看原书绑定。只读接口 `GET /api/outlines/:id/book-binding` 返回已有绑定的 `revisionId` 和 `novelId` | [开书页面](../../assistant/client/src/pages/outlines/openBook/OutlineOpenBookPage.tsx) · [绑定服务](../../assistant/server/src/modules/planning/outline/openBook/application/OutlineOpenBookService.ts) |
| 有哪些作品，某章是否已写入 | 从 `/novels` 定位目标作品，同名时核对 ID。打开 `/novels/:id/chapters/:chapterId` 查看已保存正文。需核实生成来源时对照章节运行记录 | [小说列表](../../assistant/client/src/pages/novels/NovelList.tsx) · [章节页](../../assistant/client/src/pages/novels/NovelChapterEdit.tsx) · [正文保存](../../assistant/server/src/services/novel/runtime/lifecycle/ChapterLifecycleService.ts) |
| 正文没有用上约定的写法 | 从 `/style-engine` 查写法档案，在本书核对绑定，再检查目标章任务和上下文 | [写法引擎](../../assistant/mintlify-docs/features/style-engine.mdx) · [章节执行说明](../../assistant/mintlify-docs/guides/chapter-execution.mdx) |
| 写章中断，或生成后有审校问题 | 从 `/tasks` 的运行记录确认目标章和中断位置，再回来源工作台处理。查分项依据见[单章审校](evaluation.md#查单章审校) | [运行记录页](../../assistant/client/src/pages/tasks/TaskCenterPage.tsx) · [章节执行入口](../../assistant/server/src/services/novel/production/ChapterExecutionStageRunner.ts) |

## 整理本次采用的材料

从作品状态文件登记的路径读取规划，有文件指纹时与当前内容比对。确定本次交接的卷章范围，列出已选定和待决定的内容。比如 2026-11-25 核对《我在异界打天下》时，导览写的首章名是《他说我偷了他的稿》，实际首章计划的标题是《你早就知道她喜欢你》。交接草案可以继续整理，首章定版时要记录作者最终选用的标题和计划。[作品导览](../../ani-book-skill/novels/我在异界打天下/作品导览.md) · [首章计划](../../ani-book-skill/novels/我在异界打天下/章节/第001章/章节计划.md)

准备实际交接稿时，在本仓 `handoffs/<书名>/<交接版本>/` 放一份供粘贴的 `大纲.md` 和一份供接续的 `交接记录.md`。导入文本第一行用 `# 书名`，让每章能找到所属卷。来源路径、版本和待决事项留在交接记录中。原始参考小说与拆书笔记留在分析工作区。

| 从本书取材 | 整理到哪里 | 应当核对的内容 |
| --- | --- | --- |
| 新书简报、故事主线 | 书名、总纲 | 目标读者、核心卖点、主要冲突、结局方向 |
| 分卷规划、卷纲、节奏表 | 卷、卷内节奏 | 卷序、各卷预计章数、阶段回报、卷末变化 |
| 逐章细纲、已选章节计划 | 归属该卷的章纲 | 全书章序、章名、目标、冲突、结尾钩子。尚待展开的位置只计入卷预算 |
| 人物总览、世界设定 | 人物、固定设定 | 人物目标、出场位置、适用范围，核对约束是否标为不可违背 |
| 故事主线里的承诺和伏笔 | 伏笔计划 | 埋设与拟回收的章节，实际兑现要有已写正文证据 |
| 写作风格、目标章上下文 | 总纲基调、本书的写法档案和章节任务 | 大纲中写明基调，开写前核对具体写法与本章约束。详细风格资料按需绑定到本书 |

文本解析先生成候选节点，详细字段需要在预览或工作台逐项核对、补齐并保存。[大纲字段定义](../../assistant/shared/types/outline.ts)

## 在 Web 应用并核验

1. **定位目标。** 在目标 Web 环境查已有大纲项目和绑定。已有项目继续在原项目保存修订，已有绑定从“打开创作工作台”回到原书。首次导入从电脑端“大纲工作台”进入。[大纲列表](../../assistant/client/src/pages/outlines/OutlineListPage.tsx)
2. **预览并保存大纲。** 首次导入时选“导入大纲”，粘贴 Markdown 或 TXT，点“解析并预览”。核对标题、卷章归属、人物出场、伏笔引用、原文片段及未覆盖内容。处理影响本次交接的遗漏后点“确认创建大纲”。在工作台补齐总纲等详细字段，把改动保存为新修订。[工作台操作说明](../../assistant/mintlify-docs/features/outline-workbench.mdx)
3. **采用修订并开书。** 对尚未绑定的大纲，在“按大纲开书”选择已保存的修订，点“检查开书内容”。核对每卷章数、章序区间和保留字段后，点“创建小说并查看准备”。记录小说入口及采用的修订。对已有绑定的书，独立大纲的新修订会显示差异，按本次范围在原书核对和调整后续运行规划。[绑定规则](../../assistant/docs/wiki/workflows/outline-open-book.md)
4. **在目标章生成正文。** 请求包含写章时，从本书创作工作台定位目标章节，核对章节任务、写法和前文事实，执行该章。重新打开章节，核实保存的正文、审校结果和待修项。中断时先看运行记录定位原因，回到对应工作台按已有进度恢复。

## 交接记录

每次到达新阶段，在同一份 `交接记录.md` 填入实际结果。后续会话可用环境、项目 ID 和修订定位同一本书。

| 字段 | 记录内容 |
| --- | --- |
| 本地来源与范围 | 书名、交接版本、源文件相对路径及提交号或内容指纹、卷章范围。未提交的材料用文件内容指纹定位 |
| 核对结果 | 章序、硬设定、写法及伏笔的保留情况，未覆盖内容、待选项和采纳决定 |
| Web 定位 | 目标环境、大纲项目入口或 ID、已保存修订。开书后补记采用的修订及小说入口或 ID |
| 正文结果 | 实际生成的章号、已保存版本或章节入口、审校与待修事项 |
| 接续位置 | 最后完成的动作、可复核的凭据、下一步。首次交接只做到本地稿时，状态填“待导入” |
