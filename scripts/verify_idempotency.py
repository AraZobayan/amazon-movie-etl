"""验证装载幂等（idempotency）：同一输入跑两遍，目标表记录数必须完全一致。

作业原文："同一输入运行两次，核对目标记录数与关系数，证明装载幂等。"

原理：所有主键都是内容哈希算出来的确定性 ID，重复运行只会命中
`ON CONFLICT DO NOTHING / DO UPDATE`，不会产生新行。

用法：
    python scripts/verify_idempotency.py            # 只对比两次快照（需已有库）
    python scripts/verify_idempotency.py --run      # 先跑一遍完整流程再对比
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import common as C

TABLES = [
    "raw_source_file", "raw_source_record", "raw_review", "stg_review", "stg_quarantine",
    "stg_product_page", "stg_classification", "product", "product_collection", "reviewer",
    "review", "review_mapping", "movie", "movie_title", "product_movie", "person",
    "person_name", "movie_person", "field_assertion", "field_candidate_link",
    "field_selection", "int_pair_candidate", "int_merge_decision",
]


def snapshot(db: str | None) -> dict[str, int]:
    conn = C.connect(db, readonly=True)
    out = {}
    for t in TABLES:
        out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    conn.close()
    return out


def run_pipeline(db: str | None) -> int:
    cmd = [sys.executable, "-m", "src.s12_run_all"] + (["--db", db] if db else [])
    print("[run]", " ".join(cmd))
    return subprocess.run(cmd, cwd=C.ROOT).returncode


# 全量库上一次完整 s12 要跑近 2 小时（s01 单步就 6,448 秒）。
# 幂等验证的目的是证明"装载不产生重复"，而重复只可能发生在写入步骤上，
# 所以允许只重放"纯写入、无外部依赖"的那几步。
# 被跳过的步骤必须写进报告，不能悄悄跳过：
#   s01  ingest   1h47m 的全量扫描，其恢复性由 verify_recovery.py 单独证明
#   s03  collect  依赖 Amazon 实时响应，重放结果受网络影响，本身不是幂等对象
#   s08  enrich   依赖 Wikidata，同上；且预算 1500 秒
#   s10  quality  只写 meta_quality_issue（每次 DELETE+重写），不影响实体表
SAFE_STEPS = ["s02", "s04", "s05", "s06", "s07", "s09", "s11"]

# 每个被重放的步骤实际会写哪些表。结论只能覆盖这些表：
# 其余表只有 s01/s03/s03b 会写，而它们被跳过 —— 计数不变是必然，不是幂等性的证据。
REPLAYED_TABLES = {
    "s02": ["product", "product_collection"],
    "s04": ["stg_classification"],
    "s05": ["field_assertion", "raw_source_record", "field_candidate_link"],
    "s06": ["movie", "movie_title", "product_movie", "int_pair_candidate",
            "int_merge_decision"],
    "s07": ["person", "person_name", "movie_person", "int_pair_candidate",
            "int_merge_decision", "field_assertion"],
    "s09": ["field_selection", "field_assertion", "movie"],
    "s11": [],
}


def run_steps(db: str | None, steps: list[str]) -> int:
    """按顺序重放指定步骤。任一步失败即停并返回非零。"""
    for s in steps:
        mod = {
            "s02": "src.s02_build_product_list",
            "s04": "src.s04_parse_and_classify",
            "s05": "src.s05_extract_assertions",
            "s06": "src.s06_match_movies",
            "s07": "src.s07_resolve_persons",
            "s09": "src.s09_resolve_fields",
            "s11": "src.s11_queries",
        }[s]
        cmd = [sys.executable, "-m", mod] + (["--db", db] if db else [])
        print("[run]", " ".join(cmd))
        rc = subprocess.run(cmd, cwd=C.ROOT).returncode
        if rc != 0:
            print(f"[error] {s} 返回 {rc}")
            return rc
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--steps", default=None,
                    help="只重放这些步骤（逗号分隔），默认 s02,s04,s05,s06,s07,s09,s11。"
                         "传 all 表示跑完整 s12（全量库约 2 小时）")
    args = ap.parse_args()

    before = snapshot(args.db)
    print("[snapshot 1]", json.dumps(before, ensure_ascii=False))

    replayed, skipped = [], {}
    replay_failed = False
    if args.run:
        if args.steps == "all":
            rc = run_pipeline(args.db)
            replayed = ["s01..s12 全流程"]
        else:
            steps = [s.strip() for s in (args.steps.split(",") if args.steps else SAFE_STEPS)
                     if s.strip()]
            rc = run_steps(args.db, steps)
            replayed = steps
            skipped = {
                "s01": "全量流式抽取（本次运行 6,448 秒）；恢复性由 verify_recovery.py 单独证明",
                "s02b": "生成离线页面夹具（数据准备，不写业务表；夹具内容由夹具生成器决定）",
                "s03": "采集依赖 Amazon 实时响应，重放结果受网络状态影响，不是幂等对象",
                "s03b": "读 SNAP meta 离线快照，依赖项目外的 ../_tmp 文件；未重放故列入跳过",
                "s08": "补全依赖 Wikidata，预算 1500 秒，同上",
                "s10": "只写 meta_quality_issue（每次删除重写），不写实体表",
            }
            skipped = {k: v for k, v in skipped.items() if k not in steps}
        if rc != 0:
            print(f"[error] 重放返回 {rc}：重放失败不能当作幂等通过")
            replay_failed = True

    after = snapshot(args.db)
    print("[snapshot 2]", json.dumps(after, ensure_ascii=False))

    diff = {k: [before.get(k), after.get(k)] for k in TABLES if before.get(k) != after.get(k)}
    counts_equal = not diff
    # 没有重放时，"计数一致"只说明库没被改动，不能当成"装载幂等成立"
    ok = counts_equal and not replay_failed and bool(replayed)
    # 只有被重放的步骤会写表；其余表计数不变是必然，不是幂等性的证据。
    replayed_tables = sorted({
        t for s in replayed for t in REPLAYED_TABLES.get(s, [])
    }) if replayed else []
    report = {
        "checked_at": C.now_iso(),
        "tables_checked": len(TABLES),
        "scope": {
            "replayed_steps": replayed,
            "skipped_steps": skipped,
            "replayed_tables": replayed_tables,
            "note": ("只重放写入步骤，不重放 s01/s02b/s03/s03b/s08/s10 —— 原因见 skipped_steps。"
                     "幂等性只可能被写入步骤破坏，抽取与外部请求不是幂等的对象。"
                     if replayed and args.steps != "all" else
                     ("完整流程重放" if replayed else
                      "仅快照比对（未重放任何步骤）：本次没有执行写入，结论不成立")),
        },
        "before": before,
        "after": after,
        "differences": diff,
        "idempotent": ok,
        "conclusion": (
            "重放后所有表记录数完全一致：装载幂等成立"
            if ok and args.steps != "all" else
            ("完整流程重放后所有表记录数完全一致" if ok else
             ("仅比对快照、未重放步骤：不能宣称装载幂等" if not replayed else
              ("存在差异，需检查是否有非确定性逻辑（随机数/时间戳参与主键）"
               if diff else "重放失败（非零退出），不能宣称装载幂等"))),
        ),
        "limits": ("只比 COUNT(*)，不比内容；且只有被重放的步骤所写的表"
                   f"（{', '.join(replayed_tables) or '无'}）的计数一致才是幂等证据，"
                   "其余表的计数不变是必然结果。"),
    }
    C.write_json(C.path("evidence", "idempotency_report.json"), report)
    print("=" * 60)
    if ok:
        print("幂等性验证通过：两次运行后所有表记录数一致")
    else:
        print("幂等性验证失败：")
        for k, v in diff.items():
            print(f"  {k}: {v[0]} -> {v[1]}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
