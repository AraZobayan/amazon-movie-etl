"""ETL 作业定义：把 13 个步骤组织成一条有依赖、有失败分支、可续跑的处理链。

为什么必须有这个文件？
    作业原文："只交若干不相连的脚本不能证明已实现完整 ETL"。
    所以这里把每一步显式声明成 DAG 节点：依赖、参数、失败后的处置方式。

用法：
    python -m src.s12_run_all                 # 完整跑一遍（会复用已有结果）
    python -m src.s12_run_all --from s05      # 从某一步开始
    python -m src.s12_run_all --only s10      # 只跑一步
    python -m src.s12_run_all --fresh         # 删库重建，从头跑
    python -m src.s12_run_all --skip-done     # 跳过上次已成功的步骤（断点续跑）

失败分支（每个步骤声明 on_fail）：
    stop      致命错误，整条链停止（数据被破坏的风险）
    retry     可重试（网络类），本脚本会重试一次
    continue  非致命，记录后继续（例如第三方补全失败，不该阻断主流程）
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

from src import common as C

# (步骤名, 模块, 额外参数, 依赖, 失败处置)
STEPS = [
    ("s01", "src.s01_ingest_reviews", [], [], "stop"),
    ("s02", "src.s02_build_product_list", [], ["s01"], "stop"),
    # 采集前置：生成离线快照夹具。它不是业务步骤，而是"采集层的数据源准备"。
    # 之所以放在作业链里而不是手工执行，是为了保证"从空库一条命令能跑完"。
    # 真实采集/已有一套夹具时用 --skip-fixture-gen（或 DAG 的 use_fixtures=false）跳过生成。
    ("s02b", "fixtures.make_offline_snapshots", [], ["s02"], "stop"),
    ("s03", "src.s03_collect_pages", [], ["s02b"], "retry"),
    # 第三条元数据来路：第三方离线快照（SNAP meta）。
    # 它和 s03 是**并列的两条采集路径**，不是 s03 的补救措施：
    # s03 拿页面（有演职员，但受访问限制只能拿到百来条），
    # s03b 拿商品元数据（覆盖 4 成 ASIN，但只有标题/类目，没有演职员）。
    # 两者写的是不同的表，产出在报告里必须分开统计，不能合成一个"成功率"。
    # 失败策略 continue：第三方缺失只降低覆盖率，不该阻断主链。
    ("s03b", "src.s03b_ingest_thirdparty", [], ["s03"], "continue"),
    ("s04", "src.s04_parse_and_classify", [], ["s03", "s03b"], "stop"),
    ("s05", "src.s05_extract_assertions", [], ["s04"], "stop"),
    ("s06", "src.s06_match_movies", [], ["s05"], "stop"),
    ("s07", "src.s07_resolve_persons", [], ["s06"], "stop"),
    ("s08", "src.s08_enrich_release", [], ["s07"], "continue"),
    ("s09", "src.s09_resolve_fields", [], ["s08"], "stop"),
    ("s10", "src.s10_quality_checks", [], ["s09"], "stop"),
    ("s11", "src.s11_queries", [], ["s10"], "continue"),
]


def last_success(conn, step_name: str) -> bool | None:
    """查这一步上次是否成功。

    `meta_etl_step.step_name` 存的是**业务步骤名**（build_list / extract / enrich …），
    不是 sid（s02 / s05 / s08）。早期版本用 `LIKE '%s02%'` 匹配，13 个 LIKE 全部 0 行，
    `--skip-done` 因此是个静默空操作。这里改成显式映射。
    返回 None 表示该步骤根本不写 meta_etl_step，无法判定（按"未完成"处理并提示）。
    """
    name = STEP_NAMES.get(step_name)
    if not name:
        return None
    r = conn.execute(
        "SELECT status FROM meta_etl_step WHERE step_name = ?"
        " ORDER BY finished_at DESC LIMIT 1", (name,)).fetchone()
    return bool(r and r["status"] == "success")


# sid -> meta_etl_step.step_name（只有调用 C.step() 的模块才会留下记录）
STEP_NAMES = {
    "s02": "build_list",
    "s03b": "ingest_third_party",
    "s04": "parse_and_classify",
    "s05": "extract",
    "s08": "enrich",
    "s09": "select_movie_fields",
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--from", dest="start", default=None)
    ap.add_argument("--only", default=None)
    ap.add_argument("--fresh", action="store_true", help="删库重建")
    ap.add_argument("--skip-done", action="store_true")
    ap.add_argument("--skip-fixture-gen", action="store_true",
                    help="跳过 s02b（生成离线页面夹具）。注意：这只跳过**重新生成**，"
                         "data/snapshots/ 下已有的夹具仍会被 s03 正常装载 —— "
                         "全量运行时用这个开关，避免重跑夹具覆盖已有快照。")
    args = ap.parse_args(argv)

    # 步骤名校验：--from/--only 写错一个字，旧实现会让循环空转、results=[]，
    # 然后照样写一份 steps:[] 的"成功"汇总并覆盖 evidence/run_all_summary.json。
    # 现在直接报错，不产生任何证据。
    sids = [s[0] for s in STEPS]
    for opt, val in (("--from", args.start), ("--only", args.only)):
        if val is not None and val not in sids:
            ap.error(f"{opt} {val!r} 不存在；可选：{', '.join(sids)}")
    if args.fresh and (args.start or args.only):
        ap.error("--fresh 会删库重建，不能与 --from/--only 同时使用"
                 "（空库上从中间步骤开始必然失败）")
    if args.only and args.start:
        ap.error("--only 与 --from 互斥")
    # 依赖顺序自检：deps 里出现的步骤必须排在当前步骤之前（顺序即依赖）
    seen: set[str] = set()
    for sid, _mod, _extra, deps, _on_fail in STEPS:
        for d in deps:
            if d not in seen:
                ap.error(f"STEPS 依赖顺序错误：{sid} 依赖的 {d} 未排在它前面")
        seen.add(sid)

    if args.fresh:
        db = C.init_db(args.db, drop=True)
        print(f"[fresh] 已重建数据库 {db}")
    else:
        C.init_db(args.db)
    conn = C.connect(args.db)

    log = C.Logger("run_all")
    run_id = C.start_run(conn, "run_all", vars(args))
    log.run_id = run_id
    log.info(f"作业启动 run_id={run_id}，共 {len(STEPS)} 步")

    # 视图每次运行都重建，保证和 DDL 一致（只执行一次；早期版本重复 executescript 两遍）
    with open(C.path("sql", "views.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())

    started = False
    results = []
    t_all = time.time()

    def run_cmd(cmd: list[str]) -> int:
        """跑一个步骤；被 Ctrl-C 中断时把本次 run 标记为 aborted 再退出。

        旧实现没有中断处理：进程被杀后 meta_etl_run 永远停在 running，
        库里那条 aborted 记录只能手工写入（代码里没有任何路径产生它）。
        """
        try:
            return subprocess.run(cmd, cwd=C.ROOT).returncode
        except KeyboardInterrupt:
            log.error("收到中断信号：把本次 run 标记为 aborted 后退出"
                      "（已提交的批次不回滚，重跑由幂等吸收）")
            C.end_run(conn, run_id, "aborted", "KeyboardInterrupt")
            log.close()
            conn.close()
            raise SystemExit(130)

    for i, (sid, mod, extra, deps, on_fail) in enumerate(STEPS, start=1):
        if args.only and sid != args.only:
            continue
        if args.start and not started and sid != args.start:
            continue
        started = True

        if args.skip_fixture_gen and sid == "s02b":
            log.info(f"[{i}/{len(STEPS)}] {sid} 按 --skip-fixture-gen 跳过（已有夹具仍会被 s03 装载）")
            results.append((sid, "skipped", 0.0))
            continue

        if args.skip_done and not args.only:
            prev = last_success(conn, sid)
            if prev is None:
                log.warn(f"[{i}/{len(STEPS)}] {sid} 不写 meta_etl_step，--skip-done 无法判定，按未完成处理")
            elif prev:
                log.info(f"[{i}/{len(STEPS)}] {sid} 上次已成功（meta_etl_step={STEP_NAMES[sid]}），跳过（--skip-done）")
                results.append((sid, "skipped", 0.0))
                continue

        cmd = [sys.executable, "-m", mod] + (["--db", args.db] if args.db else []) + extra
        log.info(f"[{i}/{len(STEPS)}] 执行 {sid}: {' '.join(cmd[1:])}")
        t0 = time.time()
        rc = run_cmd(cmd)
        dt = time.time() - t0

        if rc != 0:
            log.error(f"[{sid}] 退出码 {rc}，处置策略={on_fail}")
            if on_fail == "retry":
                log.warn(f"[{sid}] 按 retry 策略重试一次")
                rc = run_cmd(cmd)
                dt = time.time() - t0
            if rc != 0:
                results.append((sid, f"failed({on_fail})", dt))
                if on_fail == "continue":
                    log.warn(f"[{sid}] 非致命失败，继续后续步骤（依赖它的结果会缺失，需在报告中说明）")
                    continue
                log.error("致命失败，整条链中止。修复后可用 --from {} 续跑".format(sid))
                C.end_run(conn, run_id, "failed", f"{sid} failed")
                log.close()
                conn.close()
                return 1
        results.append((sid, "ok", dt))
        log.info(f"[{sid}] 完成，用时 {dt:.1f}s")

    total = time.time() - t_all
    log.info("=" * 78)
    for sid, status, dt in results:
        log.info(f"  {sid:<5} {status:<12} {dt:6.1f}s")
    log.info(f"总计 {total:.1f}s")

    if not results:
        # --from/--only 让循环一步都没跑：不能写"成功"的空汇总覆盖全量证据
        log.error("没有任何步骤被执行（--from/--only 组合未命中），不写汇总")
        C.end_run(conn, run_id, "failed", "no step executed")
        conn.close()
        log.close()
        return 2

    failed = [r for r in results if r[1].startswith("failed")]
    C.end_run(conn, run_id, "success" if not failed else "failed",
              json.dumps({"steps": len(results), "failed": len(failed)}, ensure_ascii=False))
    C.write_json(C.path("evidence", "run_all_summary.json"), {
        "run_id": run_id, "total_seconds": round(total, 1),
        "steps": [{"step": s, "status": st, "seconds": round(d, 1)} for s, st, d in results],
    })
    conn.close()
    log.close()
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
