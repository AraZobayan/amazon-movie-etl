"""任务 2（上半）：生成产品采集清单 + 构造候选链接。

输入：stg_review 里出现过的全部 ASIN（去重）
输出：
    product_collection  每个 ASIN 一行，初始状态 pending
    evidence/product_list.csv   完整清单（作业要求"为去重后的全部产品建立采集清单"）

为什么要单独一步？
    采集清单是"范围声明"：它规定了后面所有采集统计的分母。
    作业要求"产品清单的互斥最终状态数之和 = 不同 ASIN 总数"，
    这条恒等式只有在清单先固化、状态只做更新时才成立。
"""
from __future__ import annotations

import argparse
import sys

from src import common as C


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--reset", action="store_true", help="把已有状态重置为 pending")
    args = ap.parse_args(argv)

    conn = C.connect(args.db)
    log = C.Logger("s02_build_product_list")
    run_id = C.start_run(conn, "s02_build_product_list", {"reset": args.reset})
    log.run_id = run_id

    tmpl = C.CFG["collect"]["base_url_template"]
    with C.step(conn, run_id, "build_list", 1, log) as st:
        products = [r["product_id"] for r in conn.execute(
            "SELECT product_id FROM product WHERE product_id IS NOT NULL ORDER BY product_id")]

        rows = [(pid, tmpl.format(asin=pid), "pending", 0, run_id) for pid in products]
        with C.transaction(conn):
            conn.executemany(
                "INSERT INTO product_collection (product_id,original_url,status,attempts,run_id)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(product_id) DO "
                + ("UPDATE SET status='pending', attempts=0, run_id=excluded.run_id"
                   if args.reset else "NOTHING"),
                rows)

        n = conn.execute("SELECT COUNT(*) c FROM product_collection").fetchone()["c"]
        st["counters"].update({"products_in_list": n, "inserted": len(rows)})

    # 导出完整清单（CSV 用 utf-8-sig，Excel 打开不乱码）
    #
    # 列必须覆盖作业点名的 9 个字段，一个都不能少：
    #   ASIN(product_id) / 原始 URL / **最终 URL** / 处理状态 / HTTP 状态 /
    #   尝试次数 / **获取时间** / **页面快照位置** / **内容哈希**
    # 早先版本只导了前 6 个 + reason_code，最终 URL / 获取时间 / 快照 / 哈希
    # 留在表里没进 CSV —— 表里有、交付物里没有，等于没交付。
    # 另外补上 canonical_url / asin_on_page / template_sig：
    # 这三列是"页面与目标 ASIN 的对应关系"和"模板变化"的证据（作业明确要求保留）。
    out = C.path("evidence", "product_list.csv")
    COLS = ["product_id", "original_url", "final_url", "status", "http_status", "attempts",
            "fetched_at", "snapshot_path", "content_hash", "source_type", "reason_code",
            "canonical_url", "asin_on_page", "template_sig", "review_count"]
    rows = conn.execute(
        "SELECT c.product_id, c.original_url, c.final_url, c.status, c.http_status, c.attempts,"
        " c.fetched_at, c.snapshot_path, c.content_hash, c.source_type, c.reason_code,"
        " c.canonical_url, c.asin_on_page, c.template_sig, p.review_count"
        " FROM product_collection c LEFT JOIN product p USING (product_id)"
        " ORDER BY p.review_count DESC")
    n = C.write_csv(out, COLS, ([r[k] for k in COLS] for r in rows))
    log.info(f"采集清单已导出 {n:,} 行 × {len(COLS)} 列 -> {out}")

    C.end_run(conn, run_id, "success", f"products={n}")
    conn.close()
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
