"""验证"中断后恢复"：跑到一半被杀掉，再续跑，结果必须与一次跑完完全一致。

作业原文："人为中断后须能从已完成批次恢复……证明不会漏处理或重复装载。"

做法（用 --limit 模拟"跑到一半"）：
    1. 建一个临时库，只处理前 N 条评论（相当于进程在处理中途被终止）；
    2. 用 --resume 从检查点继续，把剩下的处理完；
    3. 与"一次跑完"的参考结果对比：
         * 记录数必须相同（不漏）
         * 主键不能重复（不重复装载）
         * 统计恒等式仍然成立

用法：python scripts/verify_recovery.py --cut 200000 --bound 400000

注意：--cut 必须是 200,000 的整数倍。
s01 每 40,000 条一批、每 5 批提交一次，检查点只在 200,000 的边界落盘。
若 --cut 落在一个未提交的批次内部，续跑会从上一个检查点重新开始
（宁可重做一批，不可跳过一批），"续跑后总数"就会小于参照值。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import common as C

TMP_DB = "data/db/recovery_test.sqlite3"
REF_DB = "data/db/recovery_ref.sqlite3"


def run(mod: str, extra: list[str], db: str = TMP_DB) -> int:
    cmd = [sys.executable, "-m", mod, "--db", db] + extra
    return subprocess.run(cmd, cwd=C.ROOT).returncode


def run_s01(extra: list[str], db: str, tag: str) -> int:
    """跑 s01，并把汇总写到临时路径。

    必须指定 --summary：s01 默认把汇总写进 evidence/s01_ingest_summary.json，
    而那是**全量运行**的证据文件。有界验证若用默认路径，会把它覆盖成 40 万条口径，
    让报告里"全量 7,911,696 条"的引用失去依据（踩过这个坑）。
    """
    summary = C.path("evidence", f"recovery_{tag}.json")
    return run("src.s01_ingest_reviews", extra + ["--summary", summary], db)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cut", type=int, default=200000, help="第一阶段处理多少条（模拟中断点）")
    ap.add_argument("--bound", type=int, default=400000,
                    help="两阶段合计只处理到第 N 条（默认 40 万）。"
                         "0 表示续跑到底（全量 791 万条，约 1 小时 47 分）")
    args = ap.parse_args()

    # 检查点只在批边界落盘（40,000 × 5 = 200,000）。--cut 落不到边界时，
    # 阶段1 的 --limit 与阶段2 的 --limit bound-cut 不再对应同一条记录序号，
    # 会得到"续跑少跑"的假失败。文档一直这么写，但代码从没强制过。
    if args.cut % 200000:
        ap.error("--cut 必须是 200,000 的整数倍（reviews_per_batch 40,000 × commit_every 5）")
    if args.bound and args.bound < args.cut:
        ap.error("--bound 必须 ≥ --cut（否则阶段2 的 --limit 为负）")
    if args.bound and args.bound % 200000:
        ap.error("--bound 必须是 200,000 的整数倍，否则对照点不在检查点边界上")

    for p in (TMP_DB, REF_DB):
        abs_p = C.path(p)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(abs_p + suffix):
                os.remove(abs_p + suffix)

    bound = args.bound
    if bound:
        # 有界模式：三个阶段都停在同一个记录序号上，比的是"分两次跑"和"一次跑完"
        # 是否落到同一个状态。断点逻辑与输入总量无关，所以 20 万条足够证明它；
        # 全量 791 万条的最终计数由 s01_ingest_summary.json 单独给出。
        print(f"[参考] 一次跑完前 {bound:,} 条")
        rc = run_s01(["--limit", str(bound), "--reset"], REF_DB, "ref")
        if rc != 0:
            print(f"[error] 参考运行失败 rc={rc}")
            return 2
        conn = C.connect(REF_DB, readonly=True)
        ref_total = conn.execute("SELECT COUNT(*) FROM raw_review").fetchone()[0]
        ref_valid = conn.execute("SELECT COUNT(*) FROM raw_review WHERE is_valid=1").fetchone()[0]
        ref_quar = conn.execute("SELECT COUNT(*) FROM raw_review WHERE is_valid=0").fetchone()[0]
        conn.close()
        print(f"  参考 {ref_total:,} 条（有效 {ref_valid:,} / 隔离 {ref_quar:,}）")
    else:
        ref_fp = C.path("evidence", "s01_ingest_summary.json")
        ref = {}
        if os.path.exists(ref_fp):
            with open(ref_fp, encoding="utf-8") as f:
                ref = json.load(f)
        ref_total = ref.get("records_read")
        if ref_total is None:
            print("[error] s01_ingest_summary.json 里没有 records_read，无法做全量对照")
        else:
            print(f"[参考] 取自 s01_ingest_summary.json：{ref_total:,} 条（全量）")

    print(f"[阶段1] 只处理前 {args.cut} 条，模拟中途中断")
    rc1 = run_s01(["--limit", str(args.cut), "--reset"], TMP_DB, "phase1")
    if rc1 != 0:
        print(f"[error] 阶段1 返回 {rc1}，不能继续（失败时的计数对比没有意义）")
        return 3
    conn = C.connect(TMP_DB)
    c1 = conn.execute("SELECT COUNT(*) FROM raw_review").fetchone()[0]
    conn.close()
    print(f"  阶段1 入库 {c1:,} 条")

    print("[阶段2] 从检查点续跑，处理剩余记录")
    extra = ["--resume"]
    if bound:
        extra += ["--limit", str(max(bound - args.cut, 0))]
    rc2 = run_s01(extra, TMP_DB, "phase2")
    if rc2 != 0:
        print(f"[error] 阶段2 返回 {rc2}，不能继续")
        return 4
    conn = C.connect(TMP_DB)
    c2 = conn.execute("SELECT COUNT(*) FROM raw_review").fetchone()[0]
    # 重复装载的检查必须落在**映射表**上：
    # raw_review.source_review_id 是主键，对它做 GROUP BY … HAVING COUNT(*)>1 恒为 0
    # （只能证明主键约束生效，不能证明没有重复装载）。
    dup = conn.execute(
        "SELECT COUNT(*) FROM (SELECT source_review_id FROM review_mapping"
        " GROUP BY source_review_id HAVING COUNT(*)>1)").fetchone()[0]
    orphan = conn.execute(
        "SELECT COUNT(*) FROM review_mapping rm"
        " LEFT JOIN raw_review r ON r.source_review_id = rm.source_review_id"
        " WHERE r.source_review_id IS NULL").fetchone()[0]
    valid = conn.execute("SELECT COUNT(*) FROM raw_review WHERE is_valid=1").fetchone()[0]
    quar = conn.execute("SELECT COUNT(*) FROM raw_review WHERE is_valid=0").fetchone()[0]
    ck = [dict(r) for r in conn.execute(
        "SELECT step_name,batch_no,cursor_value,rows_done FROM meta_checkpoint")]
    conn.close()

    ok_dup = (dup == 0 and orphan == 0)
    ok_identity = (valid + quar == c2)
    ok_matches_ref = (ref_total is not None) and (c2 == ref_total)
    ok = ok_dup and ok_identity and ok_matches_ref

    report = {
        "checked_at": C.now_iso(),
        "cut_at": args.cut,
        "bound_records": bound or None,
        "phase1_rows": c1,
        "phase2_rows_after_resume": c2,
        "reference_rows": ref_total,
        "reference_mode": (f"有界对照：一次跑完前 {bound:,} 条" if bound else "全量对照"),
        "duplicate_primary_keys": dup,
        "orphan_mappings": orphan,
        "duplicate_check_note": "重复装载按 review_mapping.source_review_id 分组检查"
                                "（raw_review.source_review_id 是主键，对它分组恒为 0，"
                                "属同义反复）；orphan_mappings 检查映射是否指向不存在的原始记录。",
        "identity_check": {"valid": valid, "quarantined": quar,
                           "sum": valid + quar, "total": c2, "ok": ok_identity},
        "checkpoints": ck,
        "recovery_ok": ok,
        "scope_note": (
            f"本次为有界验证：两阶段合计处理前 {bound:,} 条，三个阶段都停在同一条记录序号上。"
            f"断点逻辑与输入总量无关，所以这个规模足以证明检查点游标正确；"
            f"全量 7,911,696 条的最终计数见 s01_ingest_summary.json。"
            if bound else
            "本次为全量续跑验证（791 万条，耗时约 1 小时 47 分 × 2）。"),
        "conclusion": ("续跑后记录数与一次跑完一致，且无重复主键：断点恢复成立"
                       if ok else "续跑结果与参考不一致，需检查检查点游标逻辑"),
    }
    C.write_json(C.path("evidence", "recovery_report.json"), report)

    print("=" * 60)
    ref_txt = f"{ref_total:,}" if ref_total is not None else "缺失"
    print(f"阶段1 {c1:,} 条 -> 续跑后 {c2:,} 条（参考 {ref_txt}）")
    print(f"重复映射 {dup} 条；孤儿映射 {orphan} 条；"
          f"恒等式 有效{valid:,}+隔离{quar:,}={valid + quar:,}")
    print("结论：" + ("断点恢复验证通过" if ok else "断点恢复验证失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
