# XLB Topic Index RAG Migration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 `xlb-topic-index` 从“透传返回 Markdown”升级为“分层索引 + 渐进检索 + 多轮迭代 + 文件下载解析”的 RAG 能力，并量化验证速度与 token 消耗改进。

**Architecture:** 将 API 返回的 Markdown 解析为虚拟文件树（VFS），建立本地检索索引，先检索目录与摘要，再按需下载 URL 指向文件（PDF/Excel/HTML）做局部提取与二次检索。输出改为“命中片段 + 溯源”，支持自动停止。

**Tech Stack:** Bash, Python 3.11, sqlite3(FTS5), curl, pdftotext, pandas/openpyxl, pytest

---

## 1. Scope

### In Scope
- Markdown -> 虚拟文件夹/文件 的代码化映射
- 本地缓存与索引（去重、增量更新）
- 检索编排（多轮迭代，最多 5 轮）
- URL 文件下载与解析（PDF/Excel/HTML）
- 基准测试与改造前后指标对比
- 与 `rag-skill` 能力对齐评估

### Out of Scope
- UI 产品化
- 远程向量数据库
- 全网爬虫

## 2. Current State Baseline

当前 `xlb-topic-index` 行为：
- 入口脚本：`skills/xlb-topic-index/scripts/fetch-topic-index.sh`
- 逻辑：仅解析触发词并透传 `title` 到 `/getPluginInfo`
- 输出：返回全量 Markdown（无分层检索、无停止条件、无本地缓存索引）

基线风险：
- 大响应直接灌给模型，token 高
- 重复查询重复传输
- 不能按问题意图做局部检索

## 3. Target Capability Parity (vs rag-skill)

| 能力 | rag-skill | xlb-target | 验收 |
|---|---|---|---|
| 分层索引导航 | `data_structure.md` 递归 | VFS 自动生成 `data_structure.md` | 目录导航测试通过 |
| 渐进式检索 | 先 grep 再局部读 | 先索引检索再局部提取 | 片段检索测试通过 |
| 多轮迭代 | 最多 5 轮 | 最多 5 轮 | 停止条件测试通过 |
| 先处理后检索 | PDF/Excel 预处理 | URL 文件下载 + 提取后检索 | 文件类型测试通过 |
| 大文件 token 控制 | 禁止整文件加载 | chunk + 局部摘要 + 命中窗口 | token 指标下降 |
| 溯源 | 文件/位置输出 | URL/文件/chunk 输出 | 输出结构测试通过 |

判定“完整复刻”的标准：上表全部达成，且 E2E 评估满足第 9 节门槛。

## 4. Data Model (Virtual File System)

缓存根目录：
- `skills/xlb-topic-index/cache/`

建议结构：
- `skills/xlb-topic-index/cache/raw/<query_hash>.md`
- `skills/xlb-topic-index/cache/vfs/<topic_slug>/<snapshot_id>/data_structure.md`
- `skills/xlb-topic-index/cache/vfs/<topic_slug>/<snapshot_id>/<section_slug>/*.link.md`
- `skills/xlb-topic-index/cache/vfs/<topic_slug>/<snapshot_id>/<section_slug>/*.query.txt`
- `skills/xlb-topic-index/cache/artifacts/<url_hash>/original.*`
- `skills/xlb-topic-index/cache/artifacts/<url_hash>/extracted.txt`
- `skills/xlb-topic-index/cache/index/index.db`

索引字段（sqlite）：
- `doc_id`, `topic`, `section`, `node_type(link/query/text)`, `title`, `url`, `query_cmd`, `content_excerpt`, `source_query`, `updated_at`

## 5. Retrieval Pipeline

1. 解析用户输入 -> `title`
2. 拉取 Markdown（保留 raw）
3. 代码解析 raw -> VFS + 索引增量更新
4. 按问题检索索引（BM25/关键词）得到 Top-K 节点
5. 对节点执行动作：
- `query` 节点：按需继续调用子查询
- `link` 节点：按 MIME 下载与提取
- `text` 节点：直接返回命中片段
6. 每轮计算 `marginal_gain`，满足停止条件则结束
7. 输出结果与溯源

停止条件：
- 命中覆盖达到阈值（默认 0.85）或
- 连续 3 轮边际增益 `< 0.05` 或
- 达到 `max_iter=5` / `max_tokens` / `max_time_ms`

## 6. File-Type Strategy

### PDF
- 下载后使用 `pdftotext input.pdf output.txt`
- 对 `output.txt` 做局部检索，禁止全量读入模型

### Excel
- `pandas.read_excel(..., nrows=50)` 探测结构
- 仅按命中列过滤并输出必要行

### HTML/Markdown
- HTML 转文本（保留标题与段落）
- Markdown 按标题块切片

## 7. Implementation Tasks (TDD, bite-sized)

### Task 1: 搭建解析器与 VFS 生成器

**Files:**
- Create: `skills/xlb-topic-index/scripts/parse_markdown_to_vfs.py`
- Create: `skills/xlb-topic-index/tests/test_parse_markdown_to_vfs.py`

**Step 1: Write failing test**
- 构造包含 `# / ## / ### / URL / >query` 的 fixture，断言生成目录和节点文件数量。

**Step 2: Run test to verify fail**
- Run: `pytest skills/xlb-topic-index/tests/test_parse_markdown_to_vfs.py -q`
- Expected: FAIL（模块不存在）

**Step 3: Minimal implementation**
- 实现标题状态机解析与文件落盘。

**Step 4: Run test to verify pass**
- Run: `pytest skills/xlb-topic-index/tests/test_parse_markdown_to_vfs.py -q`
- Expected: PASS

### Task 2: 建立本地索引与检索

**Files:**
- Create: `skills/xlb-topic-index/scripts/build_index.py`
- Create: `skills/xlb-topic-index/scripts/search_index.py`
- Create: `skills/xlb-topic-index/tests/test_index_search.py`

**Step 1:** 写 failing tests（插入节点后 Top-K 检索正确）
**Step 2:** 跑测试确认失败
**Step 3:** 实现 sqlite + FTS5 建表与查询
**Step 4:** 跑测试确认通过

### Task 3: URL 下载与文件提取

**Files:**
- Create: `skills/xlb-topic-index/scripts/fetch_artifact.py`
- Create: `skills/xlb-topic-index/scripts/extract_pdf_text.sh`
- Create: `skills/xlb-topic-index/scripts/extract_excel_text.py`
- Create: `skills/xlb-topic-index/tests/test_artifact_pipeline.py`

**Step 1:** 写 failing tests（mock URL、MIME 分流）
**Step 2:** 跑测试确认失败
**Step 3:** 实现下载、哈希缓存、提取逻辑
**Step 4:** 跑测试确认通过

### Task 4: 多轮检索编排器

**Files:**
- Create: `skills/xlb-topic-index/scripts/retrieve_iterative.py`
- Create: `skills/xlb-topic-index/tests/test_iterative_retrieval.py`

**Step 1:** 写 failing tests（max_iter、gain stop、生效）
**Step 2:** 跑测试确认失败
**Step 3:** 实现迭代策略和停止条件
**Step 4:** 跑测试确认通过

### Task 5: 集成入口脚本

**Files:**
- Modify: `skills/xlb-topic-index/scripts/fetch-topic-index.sh`
- Modify: `skills/xlb-topic-index/SKILL.md`
- Create: `skills/xlb-topic-index/tests/test_entry_routing.py`

**Step 1:** 写 failing tests（`xlb` 输入映射 + 新模式开关）
**Step 2:** 跑测试确认失败
**Step 3:** 集成“raw-only / rag-mode”双模式
**Step 4:** 跑测试确认通过

### Task 6: 基准与评估脚本

**Files:**
- Create: `skills/xlb-topic-index/bench/queries.txt`
- Create: `skills/xlb-topic-index/bench/run_benchmark.py`
- Create: `skills/xlb-topic-index/bench/report_template.md`
- Create: `skills/xlb-topic-index/tests/test_benchmark_metrics.py`

**Step 1:** 写 failing tests（指标字段完整性）
**Step 2:** 跑测试确认失败
**Step 3:** 实现基准采集与对比报告
**Step 4:** 跑测试确认通过

## 8. Test Cases

### Unit Tests
1. Markdown 结构解析：
- 输入包含混合标题层级和 URL 列表，输出树节点正确。
2. 查询节点识别：
- `>...`、`>>...` 映射为 `query` 节点。
3. URL 去重：
- 同 URL 多次出现仅建一份 artifact。
4. Slug 冲突处理：
- 同名标题生成稳定唯一文件名。

### Integration Tests
1. API markdown -> VFS -> index 一次流程成功。
2. `query` 节点触发二次调用并合并结果。
3. PDF 下载提取后可命中关键词。
4. Excel 下载读取后可命中列与值。

### E2E Scenarios
1. `xlb >vibe coding/coding`：返回 Top-K 片段而非全量全文。
2. `xlb ??vibe coding`：触发主题相关检索并可继续扩展。
3. `查询xlb vibe coding主题`：隐式触发可完成一轮检索与溯源。

## 9. Metrics & Evaluation

### Baseline vs After
核心指标：
1. `latency_ms_p50/p95`：端到端耗时
2. `output_bytes`：返回内容字节
3. `estimated_tokens`：`ceil(output_bytes/4)`
4. `retrieval_precision_at_5`：前 5 命中相关率
5. `cache_hit_rate`：缓存命中比例
6. `expansion_calls`：二次/多次扩展调用次数

### Success Criteria
1. `estimated_tokens` 下降 >= 40%（默认摘要模式）
2. 重复查询 `latency_ms_p50` 下降 >= 30%
3. `retrieval_precision_at_5` >= 0.8
4. 多轮检索在 5 轮内稳定停止，无死循环

### Benchmark Protocol
1. 固定查询集 `bench/queries.txt`
2. 每条查询执行 5 次，去掉首轮冷启动后统计
3. 记录 raw 模式与 rag 模式两组结果
4. 生成对比报告 `bench/report.md`

## 10. Risks and Mitigations

1. 本地服务不可用（`localhost:5000`）  
- 方案：支持 fixture 回放模式，先完成离线验证。

2. 大文件下载导致耗时高  
- 方案：设置大小上限、并发上限、超时与白名单域名策略。

3. 内容重复与噪声高  
- 方案：URL 哈希去重 + 片段去重 + 主题重排。

## 11. Deliverables

1. 新版 `xlb-topic-index` 脚本与文档
2. 完整测试集（unit/integration/e2e）
3. baseline vs after 对比报告
4. rag 能力复刻验收矩阵结果

## 12. Execution Order

1. 先完成 Task 1-2（VFS + index）  
2. 再做 Task 3（下载提取）  
3. 再做 Task 4-5（检索编排 + 入口集成）  
4. 最后 Task 6（评估与报告）

---

Plan complete and saved to `docs/plans/2026-02-10-xlb-topic-index-rag-migration.md`.

Two execution options:

1. Subagent-Driven (this session) - I dispatch fresh subagent per task, review between tasks, fast iteration
2. Parallel Session (separate) - Open new session with executing-plans, batch execution with checkpoints

Which approach?
