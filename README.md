# 数据存储与管理 · ETL 课程作业

基于 Stanford SNAP Amazon 电影评论数据集，构建一条**可复现、可追溯、可重跑**的
ETL 流水线：流式抽取 → 商品页采集 → 页面解析与非电影识别 → 电影/人物实体消歧 →
上映信息补全 → 来源冲突处理 → 字段级数据血缘 → 幂等与断点恢复。

> **完整的过程记录与知识讲解**见 [`docs/完成过程与知识手册.md`](docs/完成过程与知识手册.md)（约 4000 行）。
> 本文只讲"怎么跑起来"和"产出什么"。

---

## 1. 环境要求

| 项 | 要求 |
|---|---|
| Python | ≥ 3.10 |
| 第三方包 | **无**（`requirements.txt` 里解释了为什么这本身是个工程结论） |
| 磁盘 | 约 55 GB（`data/` 实测 55.17 GB = 主库 **37.71 GB** + 下载中间产物 `.parts`/`.joining` 6.64 GB + 源文件与切片 3.54 GB + 切片/恢复验证库 6.33 GB + IMDb 表 0.54 GB + 页面/Wikidata 快照 0.38 GB + PG 临时 0.01 GB；删掉下载中间产物与验证库可压到约 42 GB） |
| 内存 | 峰值 < 300 MB（设计目标：流式处理，不整表载入；实测口径见报告 9.6） |
| 网络 | 仅任务 6（补全）与实时抽样需要；**无网络也能跑完**（自动降级为离线夹具） |
| 运行时长 | 全量约 **2.3–2.4 小时** = s01 抽取 6,448 s + s08 补全 1,514 s（两项 ≈ 2.2 h）+ 其余 11 步合计约 5 分钟（实测 s02–s07+s09 共 236 s，s10 切片 72 s） |

```bash
python --version          # 确认 ≥ 3.10
python -m unittest discover -s tests -v     # 30 个单元测试，应全部通过
```

---

## 2. 一键运行（推荐）

```bash
python -m src.s12_run_all --fresh
```

`--fresh` 会删除并重建 `data/db/etl.sqlite3`，然后按依赖顺序执行 13 个步骤（s01..s11，
其中 s02b 是夹具生成、s03b 是第三方元数据）。从空库到全部产物，**一条命令**。

日志同时输出到终端和 `data/logs/<run_id>__run_all.log`。

**预期耗时**：全量约 **2.3–2.4 小时** = s01 抽取 6,448 s + s08 补全 1,514 s（≈ 2.2 h）+ 其余 11 步合计 < 10 min
（实测依据：`data/logs/full_s01.log` 6,448.3 s、`full_run_rest.log` 里 s02–s07+s09 共 236 s、s08 1,514.1 s）。
用 **52 万条**的切片库（`--file data/raw/movies.slice.gz`，实测 522,327 条）调试时**实测 295.5 s ≈ 5 分钟**
（`evidence/run_all_summary.json` 的 2026-11-03 切片运行，12 步——该次未跑 s03b）—— 两者别混用同一句"预期耗时"。

### 不带 `--fresh` 的增量运行

```bash
python -m src.s12_run_all            # 保留已有库，逐步执行（幂等，重复跑结果不变）
```

### 单步运行

```bash
python -m src.s01_ingest_reviews --limit 50000   # 只跑前 5 万条（调试）
python -m src.s01_ingest_reviews --resume        # 从检查点续跑
python -m src.s03_collect_pages --live-sample 100  # 只发 100 个真实请求
```

---

## 3. 十三个步骤

| # | 模块 | 做什么 | 主要产物 |
|---|---|---|---|
| 1 | `s01_ingest_reviews` | 流式解压 + 解析记录块 + 隔离坏记录 + 批提交 | `raw_review` / `stg_review` / `stg_quarantine` |
| 2 | `s02_build_product_list` | 建 DDL、视图、去重商品清单 | 全部表结构 + `product` / `product_collection` |
| 2b | `fixtures.make_offline_snapshots` | 生成离线页面夹具（带 provenance 标注） | `data/snapshots/pages/*.html` |
| 3 | `s03_collect_pages` | 采集状态机（robots + 限速 + 重试 + 快照哈希） | `stg_product_page.final_state` |
| 3b | `s03b_ingest_thirdparty` | 读 SNAP meta 快照补标题/类目（纯离线，读 `../_tmp/meta_Movies_and_TV.json.gz`） | `field_assertion`（`third_party`）+ `stg_classification` |
| 4 | `s04_parse_and_classify` | 解析页面 + 有序规则分类（电影/非电影/剧集/套装/演唱会） | `stg_classification` |
| 5 | `s05_extract_assertions` | 抽取候选值（标题/演职员/日期/类型/外部 ID） | `field_assertion` |
| 6 | `s06_match_movies` | 电影实体消歧（分块 → 打分 → DSU → 传递冲突检测） | `movie` / `product_movie` |
| 7 | `s07_resolve_persons` | 人物实体消歧（DP 缩写识别 + 页面级共现） | `person` / `person_name` / `movie_person` |
| 8 | `s08_enrich_release` | Wikidata 补全上映信息（MediaWiki REST，含降级） | `field_assertion`（`R-EXT-DATE-THEATRICAL*` / `-YEARONLY`） |
| 9 | `s09_resolve_fields` | 来源冲突处理 + 最终字段选择 + 写血缘 | `field_selection` + `movie` 指针回填 |
| 10 | `s10_quality_checks` | 6 项质量检查 + P/R/F1 评价集 + 分层指标 | `evidence/quality_report.json` |
| 11 | `s11_queries` | 5 个验收查询 + 5 个血缘查询 | `evidence/queries/*.csv` |
| 12 | `s12_run_all` | 编排 + 汇总 | `evidence/run_all_summary.json` |

---

## 4. 目录结构

```
etl_project/
├── config/config.json            # 全部可调参数（阈值、批量、限速、评价集大小）
├── sql/
│   ├── schema_sqlite.sql         # 建表 DDL（四层 + meta）
│   ├── schema_postgres.sql       # PostgreSQL 版（由 scripts/gen_pg_schema.py 生成）
│   ├── views.sql                 # 审计视图（含 v_provenance_audit）
│   └── queries/                  # 验收查询与血缘查询 SQL（带 :param 占位符）
├── src/
│   ├── common.py                 # 连接/日志/确定性 ID/文本规范化/事务/PRF
│   ├── s01..s12                  # 13 个步骤模块（s02b/s03b 与 s12 编排器）
├── fixtures/
│   ├── make_offline_snapshots.py # 合成页面生成器（含 provenance 标记）
│   ├── person_truth.json         # 人物真值（用于评价集）
│   └── fixture_manifest.csv      # 夹具清单
├── scripts/
│   ├── download_full.py          # 12 线程 Range 下载全量源文件（落 .meta.json 下载时间）
│   ├── download_prefix.py        # HTTP Range 下载切片（记录 SHA-256）
│   ├── gen_pg_schema.py          # 生成 PostgreSQL DDL
│   ├── sync_to_pg.py             # 21 张表同步进 PostgreSQL（逐表核对行数）
│   ├── report_numbers.py         # 一条 SQL 一个数，生成 report_numbers.json
│   ├── build_eval_set.py         # 生成消歧评价集（IMDb 交叉验证预填，label 留空）
│   ├── export_merge_examples.py  # 四类合并/不合并实例
│   ├── verify_idempotency.py     # 幂等验证（23 表快照比对）
│   └── verify_recovery.py        # 断点恢复验证（--cut 模拟中断）
├── tests/test_core.py            # 30 个单元测试
├── docs/完成过程与知识手册.md      # ★ 过程记录 + 知识讲解（主要交付物）
├── evidence/                     # 全部证据与产物
└── data/
    ├── raw/                      # 原始切片
    ├── db/                       # SQLite 库
    ├── snapshots/                # 页面快照 + Wikidata 缓存
    ├── out/                      # 中间输出
    ├── queries/                  # 查询结果
    └── logs/                     # 运行日志
```

---

## 5. 产出（`evidence/`）

下表路径均相对于 `evidence/`。

| 文件 | 内容 |
|---|---|
| `s01_ingest_summary.json` | 读取/有效/隔离计数、SHA-256、输入区间、吞吐 |
| `s03_collection_summary.json` | 采集状态分布、未采原因、robots 决策 |
| `s04_parse_classify_summary.json` | 分类结果分布、命中规则统计 |
| `s05_assertions_summary.json` | 断言数量、按字段/来源分布 |
| `s06_movie_match_summary.json` | 电影消歧决策分布、合并簇大小 |
| `s07_person_summary.json` | 人物消歧决策分布、缩写识别统计 |
| `s08_enrich_summary.json` | 补全匹配率、请求次数、缓存命中、**降级事实** |
| `s09_selection_summary.json` | 字段选择分布、冲突处理统计 |
| `quality_report.json` | **6 项质量检查 + P/R/F1 + 分层指标** |
| `report_numbers.json` | 报告里引用的每个数字（一条 SQL 一个数，可复核） |
| `merge_examples.md` | **四类合并/不合并实例**：跨版本合并 6 组、易混淆未合并 6 组、人物别名合并 6 组、同姓未合并 6 组 |
| `queries/q1–q5.csv` | 5 个验收查询的结果 |
| `queries/l1,l1b,l2–l4.csv` | 血缘查询：字段级追溯、候选来源、选择理由、冲突处理 |
| `queries/查询结果.md` | 查询结果的 Markdown 汇总（可直接贴报告） |
| `run_all_summary.json` | 全流程汇总（各步耗时、关键计数） |
| `idempotency_report.json` | 幂等验证（需手动运行，见下） |
| `recovery_report.json` | 断点恢复验证（需手动运行，见下） |
| `recovery_{ref,phase1,phase2}.json` | 恢复验证三个阶段的 s01 汇总，**与全量 `s01_ingest_summary.json` 分开存放** |

---

## 6. 验证脚本

```bash
# 幂等：重放写入步骤，比对 23 张表的记录数
python scripts/verify_idempotency.py --run              # 默认重放 s02,s04,s05,s06,s07,s09,s11（约 3 分钟）
python scripts/verify_idempotency.py --run --steps all  # 完整 s12 重放（全量库约 2 小时）

# 断点恢复：处理前 20 万条 → 从检查点续跑到 40 万条 → 与"一次跑完 40 万条"对照（约 4 分钟）
python scripts/verify_recovery.py --cut 200000 --bound 400000

# 四类合并/不合并实例（作业第六部分要求）
python scripts/export_merge_examples.py
```

> `--run` 默认只重放**写入步骤**：s01（全量抽取 1h47m）由 `verify_recovery.py` 单独证明恢复性，
> s03/s08 依赖外部站点，重放结果受网络影响、不是幂等对象。跳过了哪些步骤会写进
> `idempotency_report.json` 的 `scope.skipped_steps`，不会悄悄跳过。

> `--cut` **必须是 200,000 的整数倍**。检查点只在批边界落盘
> （`reviews_per_batch 40,000 × commit_every_batches 5`），
> 若 `--cut` 落在一个未提交的批次里，续跑时会从上一个检查点重新开始，
> 结果看起来像"续跑少跑了"（这是正确的保守行为，不是 bug）。
> 实测：`--cut 200000 --bound 400000` → 阶段1 200,000 条，续跑后 400,000 条，
> 参考 400,000 条，重复映射 0，恒等式 399,995 + 5 = 400,000 ✓
> （`--cut`/`--bound` 不是 200,000 的整数倍时脚本会直接报错，不再"跑出一个假失败"。）

> 验证脚本调用 s01 时会传 `--summary evidence/recovery_<阶段>.json`，
> **不会**覆盖 `evidence/s01_ingest_summary.json`——那个文件是全量运行的证据。
> 手动跑小规模调试时也请带上 `--summary`，例如：
> `python -m src.s01_ingest_reviews --limit 40000 --summary evidence/debug_s01.json`

两者都会打印结论并把 JSON 写入 `evidence/`。

---

## 7. 输入规模与切片回退

当前 `data/db/etl.sqlite3` 由**完整源文件**建成：

```
data/raw/movies.txt.gz   3,321,791,660 字节
SHA-256                  4b6ee09ea4880f3fb197b1407d45dfb08a892fbb48a9e7a2d74aa0572cdcc715
读取记录                 7,911,696 条（有效 7,911,629 + 隔离 67）
商品 / 作品 / 人物       253,059 / 43,838 / 406
```

`config.source.raw_file` 指向全量文件，所以 `python -m src.s12_run_all --fresh`
重建出来的库与 `evidence/` 里的口径一致。**不要**把它改回切片路径——那会重建出一个
52 万条的小库，而 evidence 里的数字是 791 万条的，两者混在一起就无法复核。

**切片回退（调试用）**：仓库里保留了前 220 MB 的切片（`raw_file_slice`），
它让解析逻辑可以在 2 分钟内迭代一次，而不是每次等 1 小时 47 分。

```bash
python -m src.s01_ingest_reviews --file data/raw/movies.slice.gz --db data/db/etl_slice.sqlite3
```

用切片跑出来的库**只能**用于调试，不能用来出证据：`s01` 会按字节数自动判定
`is_slice=true` 并写进 `s01_ingest_summary.json`，两套口径不会互相冒充。

**全量运行的资源画像**（实测）：

| 项 | 实测值 |
|---|---|
| 抽取耗时 | 6,448 秒（1 小时 47 分），吞吐 1,227 条/秒 |
| 补全耗时 | 1,514 秒（受 API 限速，1500 秒预算用尽） |
| 其余 11 步合计 | 约 5 分钟（实测 s02–s07+s09 共 236 s，s10 切片 72 s） |
| SQLite 库体积 | 37.7 GB |
| 峰值内存 | < 300 MB（流式，不整表载入） |

> 37.7 GB 之后 SQLite 的 `COUNT(*)` 与全表扫描明显变慢（`report_numbers.py` 要跑 12 分钟）。
> 上生产应换 PostgreSQL：`sql/schema_postgres.sql` + `scripts/sync_to_pg.py` 已经可用。

---

## 8. 设计要点速览

| 主题 | 做法 | 为什么 |
|---|---|---|
| **血缘** | `field_assertion`（候选）→ `field_candidate_link`（关系）→ `field_selection`（选择+理由）；实体表只存 `selected_assertion_id` 指针 | 一旦把值写进实体表，血缘永久丢失 |
| **幂等** | `det_id(prefix, *parts)` = `prefix + sha256(内容)[:16]` + `ON CONFLICT DO NOTHING` | 事实不覆盖，派生量覆盖 |
| **恢复** | 检查点存**记录序号**，且**滞后于**数据提交 | 靠幂等吸收重复，永不漏数据 |
| **隔离区** | `读取数 = 有效 + 隔离`（恒等式检查） | 把静默数据丢失变成显式报错 |
| **消歧** | 分块 → 加权打分 → 三档阈值 → DSU 合并 → 传递冲突检测 | 相似不满足传递性，必须回头验 |
| **日期语义** | 影院上映（`R-EXT-DATE-THEATRICAL*`）与实体发售（`R-EXT-DATE-PHYSICAL*`）用规则 ID 前缀物理隔离；只知道年份的带 `-YEARONLY` 后缀 | 两者语义不同，混用会错 7 年 |
| **日期精度** | Wikidata `precision` 9/10/11 → `year`/`month`/`day`，按 ISO 字符串长度反推 | 绝不把"只有年份"补成 1 月 1 日 |
| **诚实** | 合成页面三处标注（库字段 / HTML 注释 / `.meta.json`）+ `v_provenance_audit` 视图 | 依赖比例必须可审计 |

---

## 9. 已知局限

完整清单见报告第 9 节。最关键的四条：

1. **采集覆盖极低**：253,059 个商品里 251,986 个（99.6%）是 `not_collected`。
   真发过 HTTP 请求的只有 155 个（98 个成功）；702 个"成功"来自标注过的离线夹具，
   不计入任何成功率；
2. **上映信息只补全了 39 部作品**（查询 172 部、匹配 37 部），
   受 Wikidata 限速与 1500 秒预算约束。43,838 部里 43,666 部从未被查询；
3. **消歧评价集不是人工标注**：CSV 的 `label` 列留空、`label_source` 列写的是
   `imdb_prefill_pending_human`（预填待人工复核），真值来自 IMDb `tconst`/`nconst`
   交叉验证；**人工复核未完成**，所以只有"IMDb 交叉验证"这一层真值。
   电影侧 100 对（50 正 50 负）P=1.00/R=0.68；人物侧正例只有 **2** 对（2 正 98 负），
   Recall 不具统计意义；
4. **`canonical_title` 100% 覆盖但 100% 是聚合值**（由商品标题按 `R-AGG-` 聚合），
   不是从权威作品记录抽取的——覆盖率满格不等于质量满格；

---

## 10. 一句话总结

> 这个项目的重点不是"数字漂亮"，而是**每个数字都能说清它是怎么来的、边界在哪**。
