"""Amazon 电影评论与多源电影信息集成 —— ETL 作业定义（Airflow DAG）

对应课程课件第 28 页「工作流编排负责协调任务」与第 29 页工具清单中的
「工作流编排：Airflow —— 依赖、调度、重试与历史回填」。

设计原则（也是作业说明第八部分的要求）：

1. **编排与计算分离**。DAG 只负责"谁在谁之后跑、失败了怎么办、参数从哪来"；
   记录级计算（gzip 流式解析、正则抽取、实体消歧、IMDb 匹配）全部留在 Python 模块里，
   由本 DAG 通过 BashOperator 调用。作业原文："爬虫与复杂匹配可用 Python 等实现，
   并由 ETL 作业调用。"
2. **每一步都幂等**。任务重复执行不会产生重复实体或重复关系（详见 s01..s11 的
   INSERT OR IGNORE / 确定性主键 / 检查点）。这是 Airflow 重试能安全的前提。
3. **失败分支显式**。s03（采集）遇到网络问题重试 3 次，失败即停；
   s03b/s08 在 `s12_run_all` 里是 continue（第三方文件可能没下载、补不到就是 NULL），
   但 Airflow 的 trigger_rule 是**任务级**的、不能按上游逐个区分，
   所以 DAG 侧让这两个模块自己把非致命错误降级（s08 恒返回 0；s03b 缺文件时返回 2
   并在任务日志里给出下载命令）—— 换言之：**DAG 与 run_all 的失败语义不完全等价，
   差异写在报告 10.5 节，不靠一条 ALL_DONE 假装等价**。
4. **参数化**。采集条数、是否重跑都通过 dag_run.conf 传入，不写死在代码里 ——
   这样"重新执行小批次"（答辩要求）只需要改一个参数。
   （"跳过已成功的步骤"只在 `s12_run_all --skip-done` 上实现，DAG 不提供该参数：
   DAG 的每个 task 就是一步，跳过与否由 Airflow 的 task 状态表达。）
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

from airflow import DAG
from airflow.models.param import Param
from airflow.operators.bash import BashOperator
from airflow.operators.python import ShortCircuitOperator
from airflow.utils.trigger_rule import TriggerRule

PROJECT = os.environ.get(
    "ETL_PROJECT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EVIDENCE = os.path.join(PROJECT, "evidence")

DOC = """
## Amazon 电影评论与多源电影信息集成

**输入**：`data/raw/movies.txt.gz`（Stanford SNAP，7,911,684 条评论 / 253,059 个 ASIN）
**输出**：`etl` 库的四层模型 + `evidence/` 下的全部证据文件

### 依赖图

```
ingest_reviews → build_product_list → fixtures_enabled（闸门）
                    ├─(use_fixtures=true)→ make_fixtures → collect_pages
                    └─(use_fixtures=false)→ collect_pages
    → ingest_thirdparty_meta → parse_and_classify → extract_assertions
    → match_movies → resolve_persons → enrich_release → resolve_fields
    → quality_checks → queries
```

### 参数（Trigger DAG w/ config）

| 参数 | 默认 | 说明 |
|---|---|---|
| `live_sample` | 3000 | 实时采集条数上限，0 表示纯离线 |
| `limit` | 0 | 只处理前 N 条评论（调试/答辩重放小批次用），0 = 全量 |
| `resume` | true | 从检查点续跑 |
| `use_fixtures` | false | 是否生成离线教学夹具；false 时 `collect_pages` 会带 `--no-fixtures`，既不生成也不装载合成页面 |
| `db_path` | （空） | 目标库路径；留空用 config.json 的默认值 |

### 重试与失败分支

- 采集（`collect_pages`）：重试 3 次，指数退避 —— 网络抖动不该让整条链失败；失败即停
- 补全（`enrich_release`）：模块自身把非致命错误降级为返回 0（补不到就是 NULL）
- 第三方元数据（`ingest_thirdparty_meta`）：文件缺失时返回非零，**DAG 侧会阻断下游**
  （`run_all` 侧是 continue）——差异见报告 10.5 节
- 其余步骤：失败即停，`on_failure_callback` 写 `evidence/airflow_failures.json`
"""


def on_task_failure(context) -> None:
    """失败分支的证据落盘：哪个 task、哪次 run、什么异常。

    写文件而不是只留在 Airflow 日志里，是因为作业要求"执行日志和失败分支"
    要能作为提交材料的一部分被审阅。
    """
    ti = context.get("task_instance")
    exc = context.get("exception")
    try:
        # rec 的构造也放进 try：context 缺键时回调自身抛异常，会让失败证据静默丢失。
        dag = context.get("dag")
        rec = {
            "dag_id": getattr(dag, "dag_id", None),
            "run_id": context.get("run_id"),
            "task_id": ti.task_id if ti else None,
            "try_number": ti.try_number if ti else None,
            "logical_date": str(context.get("logical_date")),
            "exception": repr(exc) if exc else None,
            "logged_at": datetime.now().isoformat(timespec="seconds"),
        }
        os.makedirs(EVIDENCE, exist_ok=True)
        fp = os.path.join(EVIDENCE, "airflow_failures.json")
        rows = []
        if os.path.exists(fp):
            with open(fp, encoding="utf-8") as f:
                rows = json.load(f)
        if not isinstance(rows, list):      # 文件被手工改坏时不要静默覆盖
            rows = []
        rows.append(rec)
        with open(fp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
    except Exception:  # 证据写失败不能掩盖原始错误
        pass


default_args = {
    "owner": "etl-coursework",
    "depends_on_past": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=2),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=20),
    "execution_timeout": timedelta(hours=8),
    "on_failure_callback": on_task_failure,
}

with DAG(
    dag_id="amazon_movie_etl",
    description="SNAP Amazon 电影评论 → 产品采集 → 实体消歧 → 血缘（13 步）",
    doc_md=DOC,
    start_date=datetime(2026, 11, 1),
    schedule=None,                 # 由人工/外部触发，不跟日历
    catchup=False,                 # 不自动回填历史区间
    max_active_runs=1,             # 同一时刻只允许一个 run，避免并发写同一张表
    default_args=default_args,
    params={
        "live_sample": Param(3000, type="integer", minimum=0,
                             description="实时采集条数上限；0 表示只跑离线快照"),
        "limit": Param(0, type="integer", minimum=0,
                       description="只处理前 N 条评论（0=全量），答辩重放小批次用"),
        "resume": Param(True, type="boolean", description="从检查点续跑"),
        "use_fixtures": Param(False, type="boolean",
                              description="是否生成离线教学夹具（纯离线演示用，真实采集时应关闭）"),
        "db_path": Param("", type="string",
                         description="目标库路径；留空用 config.json 里的默认值。"
                                     "冒烟测试时可指向临时库，避免污染正式数据"),
    },
    tags=["etl", "amazon", "movies", "coursework"],
) as dag:

    def py(module: str, *args: str, **kwargs) -> BashOperator:
        """统一构造"调用 Python 模块"的任务。

        用 `cd PROJECT && python -m ...` 而不是绝对路径，
        保证与宿主机上 `python -m src.sXX` 的行为完全一致。
        db_path 参数对所有步骤统一注入，这样同一条 DAG 既能跑正式库，
        也能指向临时库做冒烟测试。
        """
        # 注意：--db 必须拼进同一条 python 命令里，不能用 && 连接。
        # 用 && 会渲染成 `python -m x --resume && --db /tmp/a.db`，
        # 而 `--db` 不是可执行文件，任务会直接报 command not found。
        cmd = " ".join(a for a in (f"python -m {module}", *args) if a)
        cmd = cmd.rstrip() + " {% if params.db_path %}--db '{{ params.db_path }}'{% endif %}"
        # 路径加引号：项目路径或 db_path 含空格时，不加引号会被 shell 拆成两个参数
        return BashOperator(task_id=kwargs.pop("task_id"),
                            bash_command=f"cd '{PROJECT}' && {cmd}", **kwargs)

    t01 = py(
        "src.s01_ingest_reviews",
        "--limit {{ params.limit }}",
        '{% if params.resume %}--resume{% endif %}',
        task_id="ingest_reviews",
        doc_md="流式解压 + 记录块解析 + 隔离坏记录 + 批提交。**最重的一步**（全量约 90 分钟）。",
    )

    t02 = py(
        "src.s02_build_product_list",
        task_id="build_product_list",
        doc_md="建全部 DDL/视图，去重生成产品采集清单（253,059 个 ASIN）。",
    )

    # 采集：网络操作，单独放宽重试。
    # 夹具被闸门跳过时仍要执行，所以用 none_failed（允许"上游被跳过"，拒绝"上游失败"）——
    # 不能用 ALL_DONE：那会让 build_product_list/make_fixtures 失败时照样在坏库上采集。
    # 闸门关闭（use_fixtures=false）时追加 --no-fixtures：只控制"不生成"不够，
    # s03 只要看到 data/snapshots/pages/*.html 就会装载，合成页面会污染来源统计。
    t03 = py(
        "src.s03_collect_pages",
        "--live-sample {{ params.live_sample }}",
        '{% if not params.use_fixtures %}--no-fixtures{% endif %}',
        task_id="collect_pages",
        retries=3,
        retry_delay=timedelta(minutes=5),
        trigger_rule=TriggerRule.NONE_FAILED,
        doc_md=(
            "采集状态机：robots → 限速 → 有上限重试 → 快照落盘 + 哈希。\n\n"
            "**HTTP 200 不等于成功**：canonical 校验不通过一律 `parse_failed`。\n"
            "遇到 403/验证码标 `access_restricted` 并停止，不绕过。"
        ),
    )

    # 条件分支：只有显式要求时才生成离线夹具。
    # 真实采集路径下必须关闭 —— 合成页面不能混进真实来源的统计里
    # （作业："离线快照、第三方补充与实时采集分别统计，不得混为同一成功指标"）。
    gate_fixtures = ShortCircuitOperator(
        task_id="fixtures_enabled",
        python_callable=lambda **ctx: bool(ctx["params"].get("use_fixtures", False)),
        # 关键：默认 True 会把所有直接下游无条件置 SKIPPED、绕过 trigger_rule，
        # 结果 use_fixtures=false 时 collect_pages 及其后 11 个任务全被跳过。
        # 置 False 后，下游按自己的 trigger_rule 求值（t03 用 none_failed）。
        ignore_downstream_trigger_rules=False,
        doc_md="闸门：`use_fixtures=false` 时只跳过夹具生成；`collect_pages` 仍按 "
               "`none_failed` 规则执行（跳过不等于失败）。",
    )

    t02b = py(
        "fixtures.make_offline_snapshots",
        task_id="make_fixtures",
        doc_md="生成离线页面夹具（带 provenance 标注）。仅纯离线演示时启用。",
    )

    # 第三条元数据来路：第三方离线快照（SNAP meta）。
    # 和 t03 并列，不是它的补救：t03 拿页面（有演职员，但受访问限制只拿到百来条），
    # t03b 拿商品元数据（覆盖约四成 ASIN，但只有标题/类目）。
    # 两者写入不同的表，报告里分开统计 —— 作业明确要求"不得混为同一成功指标"。
    t03b = py(
        "src.s03b_ingest_thirdparty",
        task_id="ingest_thirdparty_meta",
        doc_md=(
            "SNAP `meta_Movies_and_TV.json.gz`（Python repr 格式，用 `ast.literal_eval`）。\n\n"
            "产出：`raw_source_record`（`source_type='third_party'`，locator=行号）、\n"
            "`field_assertion`（标题）、`stg_classification`（类目初判）。\n"
            "**补的是「这是什么作品」，补不了「谁演的」** —— 这是必须写进报告的数据边界。"
        ),
    )

    t04 = py("src.s04_parse_and_classify", task_id="parse_and_classify",
             doc_md="解析页面 + 有序规则分类（电影/非电影/剧集/套装/演唱会/待确认）。")
    t05 = py("src.s05_extract_assertions", task_id="extract_assertions",
             doc_md="抽取候选值写入 field_assertion（标题/演职员/日期/类型/外部 ID）。")

    t06 = py("src.s06_match_movies", task_id="match_movies",
             doc_md="电影实体消歧：分块 → 加权打分 → DSU 并查集 → 传递冲突检测。")

    t07 = py("src.s07_resolve_persons", task_id="resolve_persons",
             doc_md="人物消歧：DP 缩写识别 + 页面级共现证据 + 别名表。")

    # 补全：模块自身把非致命错误降级为返回 0（补不到就是 NULL）。
    # 这里不能再挂 ALL_DONE —— 那会让 resolve_persons 失败时补全照跑、整条链不停，
    # 与"其余步骤失败即停"相反。
    t08 = py(
        "src.s08_enrich_release",
        task_id="enrich_release",
        retries=1,
        doc_md="补全上映信息（Wikidata/IMDb）。缺失即 NULL，模块自身不因单条失败而中断。",
    )

    t09 = py("src.s09_resolve_fields", task_id="resolve_fields",
             doc_md="来源冲突处理 + 最终字段选择 + 写血缘（field_selection）。")

    t10 = py("src.s10_quality_checks", task_id="quality_checks",
             doc_md="数量恒等式、状态互斥性、主外键、覆盖率、P/R/F1。")

    t11 = py("src.s11_queries", task_id="queries",
             doc_md="5 个验收查询 + 5 个血缘查询，结果落 evidence/queries/。")

    # 依赖链：与 s12_run_all.py 的 STEPS 顺序一致，但这里是显式的 DAG 边。
    # 夹具那一步是条件分支：闸门放行才跑；闸门关闭时 make_fixtures 被跳过，
    # t03 用 none_failed 允许"跳过"、拒绝"失败"，所以采集照常执行。
    t01 >> t02 >> gate_fixtures >> t02b >> t03
    gate_fixtures >> t03          # 闸门关闭时，t03 不等待 t02b
    t03 >> t03b
    [t03, t03b] >> t04 >> t05 >> t06 >> t07 >> t08 >> t09 >> t10 >> t11
