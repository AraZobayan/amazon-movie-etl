"""把所有"要写进报告的数字"一次性从库里抽出来，落成 evidence/report_numbers.json。

## 为什么要有这个脚本

报告和手册里会出现几十个数字：读取了多少条、隔离了多少条、多少个 ASIN、
多少条实时采集成功、多少个受限、第三方覆盖多少、多少部电影、多少个人物、
字段覆盖率多少、质量检查哪几项通过……

手工从日志里抄这些数字有三个问题：

1. **会抄错。** 7,911,684 和 7,911,864 看上去一样，但差 180 条。
2. **会抄旧。** 切片时代的数字和全量时代的数字混在一份文档里，
   读者无法判断哪个是当前结论。
3. **无法复核。** 老师看到"覆盖 83.7%"时，没法知道这个数怎么算出来的。

所以这里把每个数字都定义成一条 SQL，跑一次生成一份 JSON。
报告里的每个数字都必须能在这份 JSON 里找到同名条目 —— 找不到就是没证据。

## 用法

    python scripts/report_numbers.py                # 默认库
    python scripts/report_numbers.py --db data/db/etl.sqlite3
    python scripts/report_numbers.py --markdown     # 顺便打印一张 markdown 表

输出：`evidence/report_numbers.json`
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import common as C  # noqa: E402


def q1(conn, sql: str, *args):
    """取单个标量。查不到返回 None，而不是抛异常 —— 报告脚本不该因为
    某张表还是空的就整个失败。"""
    try:
        r = conn.execute(sql, args).fetchone()
        return r[0] if r else None
    except Exception:
        return None


def collect(conn) -> dict:
    out: dict = {}

    # ---------------- 1. 源文件 ----------------
    out["source"] = {
        "raw_source_files": q1(conn, "SELECT COUNT(*) FROM raw_source_file"),
        "files": [dict(r) for r in conn.execute(
            "SELECT name, byte_size, sha256, range_start, range_end, note"
            "  FROM raw_source_file ORDER BY byte_size DESC")],
    }

    # ---------------- 2. 评论抽取（原始层 / 暂存层） ----------------
    raw = q1(conn, "SELECT COUNT(*) FROM raw_review")
    valid = q1(conn, "SELECT COUNT(*) FROM raw_review WHERE is_valid=1")
    quar = q1(conn, "SELECT COUNT(*) FROM raw_review WHERE is_valid=0")
    out["reviews"] = {
        "records_read": raw,
        "valid_records": valid,
        "quarantined_records": quar,
        "identity_left": raw,
        "identity_right": (valid or 0) + (quar or 0),
        "identity_ok": raw == (valid or 0) + (quar or 0),
        "logical_reviews": q1(conn, "SELECT COUNT(*) FROM review"),
        "stg_review_rows": q1(conn, "SELECT COUNT(*) FROM stg_review"),
        "quarantine_rows": q1(conn, "SELECT COUNT(*) FROM stg_quarantine"),
        "quarantine_reasons": {
            r["reason"]: r["n"] for r in conn.execute(
                "SELECT reason, COUNT(*) n FROM stg_quarantine GROUP BY reason ORDER BY n DESC")},
        "distinct_products": q1(conn, "SELECT COUNT(*) FROM product"),
        "distinct_reviewers": q1(conn, "SELECT COUNT(*) FROM reviewer"),
        "multi_raw_reviews": q1(conn, "SELECT COUNT(*) FROM review WHERE raw_count>1"),
    }
    official = C.CFG["source"]
    out["reviews"]["official"] = {
        "reviews": official["official_review_count"],
        "products": official["official_product_count"],
        "users": official["official_user_count"],
    }
    # 与官方统计的偏差：这是最能说明"全量是否真的全量"的一组数字
    out["reviews"]["delta_vs_official"] = {
        "reviews": (raw or 0) - official["official_review_count"],
        "products": (out["reviews"]["distinct_products"] or 0) - official["official_product_count"],
        "users": (out["reviews"]["distinct_reviewers"] or 0) - official["official_user_count"],
    }

    # ---------------- 3. 采集（清单 / 状态 / 来源） ----------------
    out["collection"] = {
        "products_in_list": q1(conn, "SELECT COUNT(*) FROM product_collection"),
        "status_sum": q1(conn, "SELECT COUNT(*) FROM product_collection"),
        "status_by_status": {
            r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) n FROM product_collection GROUP BY status ORDER BY n DESC")},
        "status_by_source": {
            f"{r['status']}|{r['source_type']}": r["n"] for r in conn.execute(
                "SELECT status, source_type, COUNT(*) n FROM product_collection"
                " GROUP BY status, source_type ORDER BY n DESC")},
        "reason_by_code": {
            r["reason_code"]: r["n"] for r in conn.execute(
                "SELECT reason_code, COUNT(*) n FROM product_collection"
                " WHERE reason_code IS NOT NULL GROUP BY reason_code ORDER BY n DESC")},
        "live_success": q1(conn, "SELECT COUNT(*) FROM product_collection"
                                 " WHERE status='success' AND source_type='live'"),
        "live_attempted": q1(conn, "SELECT COUNT(*) FROM product_collection"
                                   " WHERE source_type='live' AND attempts>0"),
        "http_200_but_not_success": q1(conn, "SELECT COUNT(*) FROM product_collection"
                                           " WHERE http_status=200 AND status<>'success'"),
        "with_canonical": q1(conn, "SELECT COUNT(*) FROM product_collection"
                                  " WHERE canonical_url IS NOT NULL AND canonical_url<>''"),
        "with_template_sig": q1(conn, "SELECT COUNT(*) FROM product_collection"
                                     " WHERE template_sig IS NOT NULL"),
    }

    # ---------------- 4. 第三方补充 ----------------
    out["third_party"] = {
        "records": q1(conn, "SELECT COUNT(*) FROM raw_source_record WHERE source_type='third_party'"),
        "products_with_title": q1(conn, "SELECT COUNT(DISTINCT entity_id) FROM field_assertion"
                                        " WHERE entity_type='product' AND field_name='title'"
                                        "   AND extractor_rule_id='R-EXT-SNAP-TITLE'"),
        "coverage_pct_of_all_asins": None,
    }
    tot = out["collection"]["products_in_list"] or 0
    if tot:
        out["third_party"]["coverage_pct_of_all_asins"] = round(
            100.0 * (out["third_party"]["products_with_title"] or 0) / tot, 2)

    # ---------------- 5. 分类 ----------------
    out["classification"] = {
        "by_category": {r["category"]: r["n"] for r in conn.execute(
            "SELECT category, COUNT(*) n FROM stg_classification GROUP BY category ORDER BY n DESC")},
        "by_rule": {r["rule_id"]: r["n"] for r in conn.execute(
            "SELECT rule_id, COUNT(*) n FROM stg_classification GROUP BY rule_id ORDER BY n DESC")},
    }

    # ---------------- 6. 实体（电影 / 人物） ----------------
    out["entities"] = {
        "movies": q1(conn, "SELECT COUNT(*) FROM movie"),
        "movie_titles": q1(conn, "SELECT COUNT(*) FROM movie_title"),
        "persons": q1(conn, "SELECT COUNT(*) FROM person"),
        "person_names": q1(conn, "SELECT COUNT(*) FROM person_name"),
        "product_movie_links": q1(conn, "SELECT COUNT(*) FROM product_movie"),
        "movie_person_links": q1(conn, "SELECT COUNT(*) FROM movie_person"),
        # 有多少部电影是"多个商品版本合并来的"——这是消歧是否真的生效的判据
        "movies_with_multiple_products": q1(conn, """
            SELECT COUNT(*) FROM (
              SELECT movie_id FROM product_movie GROUP BY movie_id HAVING COUNT(*)>1)"""),
        "max_products_per_movie": q1(conn, "SELECT MAX(n) FROM ("
                                          " SELECT COUNT(*) n FROM product_movie GROUP BY movie_id)"),
    }
    out["merge_decisions"] = {
        "pair_candidates": q1(conn, "SELECT COUNT(*) FROM int_pair_candidate"),
        "by_action": {r["action"]: r["n"] for r in conn.execute(
            "SELECT action, COUNT(*) n FROM int_merge_decision GROUP BY action ORDER BY n DESC")},
        "by_entity_type": {r["entity_type"]: r["n"] for r in conn.execute(
            "SELECT entity_type, COUNT(*) n FROM int_merge_decision GROUP BY entity_type")},
    }

    # ---------------- 7. 字段断言 / 选择 / 血缘 ----------------
    out["fields"] = {
        "assertions": q1(conn, "SELECT COUNT(*) FROM field_assertion"),
        "assertions_by_entity": {r["entity_type"]: r["n"] for r in conn.execute(
            "SELECT entity_type, COUNT(*) n FROM field_assertion GROUP BY entity_type")},
        "assertions_by_field": {r["field_name"]: r["n"] for r in conn.execute(
            "SELECT field_name, COUNT(*) n FROM field_assertion GROUP BY field_name ORDER BY n DESC")},
        "candidate_links": q1(conn, "SELECT COUNT(*) FROM field_candidate_link"),
        "selections": q1(conn, "SELECT COUNT(*) FROM field_selection"),
        "selections_with_value": q1(conn, "SELECT COUNT(*) FROM field_selection"
                                         " WHERE selected_assertion_id IS NOT NULL"),
        "selections_unresolved": q1(conn, "SELECT COUNT(*) FROM field_selection"
                                         " WHERE selected_assertion_id IS NULL"),
        "selections_by_rule": {r["rule_id"]: r["n"] for r in conn.execute(
            "SELECT rule_id, COUNT(*) n FROM field_selection"
            " GROUP BY rule_id ORDER BY n DESC")},
    }
    # 字段覆盖率：分母是"有多少实体该有这个字段"，分子是"最终真的选出了值"。
    # 单值字段看 selected_assertion_id；多值字段（genres）看 candidate_ids_json 是否有成员
    # —— 多值字段的"胜出者"不止一个，selected_assertion_id 天生为空，
    # 直接判 NULL 会把它误报成 0% 覆盖（第一版就踩了这个坑）。
    for f in ("canonical_title", "release_date", "external_id", "genres"):
        denom = q1(conn, "SELECT COUNT(*) FROM movie")
        got = q1(conn, """
            SELECT COUNT(*) FROM field_selection
             WHERE entity_type='movie' AND field_name=?
               AND ( (COALESCE(selection_kind,'single')='single'
                      AND selected_assertion_id IS NOT NULL)
                  OR (selection_kind='multi'
                      AND COALESCE(candidate_ids_json,'') NOT IN ('','[]')) )""", f)
        out["fields"].setdefault("coverage", {})[f] = {
            "selected": got, "denominator": denom,
            "pct": round(100.0 * (got or 0) / denom, 2) if denom else None}
    # 这些字段不落在 field_selection 上，而是落在实体表（movie_title / movie_person）。
    # 列在这里是为了防止读者误以为"覆盖率 0"等于"没抽到"。
    out["fields"]["entity_table_fields"] = {
        "movie_title_rows": q1(conn, "SELECT COUNT(*) FROM movie_title"),
        "movie_person_rows": q1(conn, "SELECT COUNT(*) FROM movie_person"),
        "note": "标题/演职员存在实体表里（movie_title / movie_person），"
                "不在 field_selection 中，所以不参与上面的覆盖率计算。",
    }

    # ---------------- 8. 日期语义 ----------------
    # 精度不在 field_assertion 上，而是落在 movie.release_precision
    # （断言层只记录"某个来源说了什么"，精度是"最终选出来的是什么精度"）
    out["release_date"] = {
        "by_precision": {r["release_precision"]: r["n"] for r in conn.execute(
            "SELECT release_precision, COUNT(*) n FROM movie"
            " GROUP BY release_precision ORDER BY n DESC")},
        "by_rule": {r["extractor_rule_id"]: r["n"] for r in conn.execute(
            "SELECT extractor_rule_id, COUNT(*) n FROM field_assertion"
            " WHERE field_name='release_date' GROUP BY extractor_rule_id ORDER BY n DESC")},
    }

    # ---------------- 9. 质量检查 ----------------
    out["quality"] = {
        "issues": q1(conn, "SELECT COUNT(*) FROM meta_quality_issue"),
        "issues_by_type": {r["issue_type"]: r["n"] for r in conn.execute(
            "SELECT issue_type, COUNT(*) n FROM meta_quality_issue"
            " GROUP BY issue_type ORDER BY n DESC")},
        "issues_by_severity": {r["severity"]: r["n"] for r in conn.execute(
            "SELECT severity, COUNT(*) n FROM meta_quality_issue"
            " GROUP BY severity ORDER BY n DESC")},
        "foreign_key_violations": len(list(conn.execute("PRAGMA foreign_key_check"))),
    }
    out["eval"] = {
        "pairs": q1(conn, "SELECT COUNT(*) FROM meta_eval_pair"),
        "pairs_by_type": {r["entity_type"]: r["n"] for r in conn.execute(
            "SELECT entity_type, COUNT(*) n FROM meta_eval_pair GROUP BY entity_type")},
        "pairs_labeled": q1(conn, "SELECT COUNT(*) FROM meta_eval_pair WHERE label IS NOT NULL"),
        "pairs_by_group": {r["sample_group"]: r["n"] for r in conn.execute(
            "SELECT sample_group, COUNT(*) n FROM meta_eval_pair GROUP BY sample_group")},
        "metrics": [dict(r) for r in conn.execute(
            "SELECT entity_type, sample_group, tp, fp, fn, tn, precision, recall, f1, note"
            "  FROM meta_eval_metric ORDER BY entity_type, sample_group")],
    }

    # ---------------- 10. 运行记录 ----------------
    out["runs"] = [dict(r) for r in conn.execute(
        "SELECT run_id, pipeline, status, started_at, finished_at FROM meta_etl_run"
        " ORDER BY started_at DESC LIMIT 30")]
    # 只取"父运行成功"的步骤记录，再按 finished_at 升序覆盖（字典推导后写覆盖先写，
    # 所以升序才能让最新的一条留在字典里）。这样既不会把早期中断的尝试当成当前状态，
    # 也不会因为 DESC 让最老的状态覆盖最新的。
    out["steps"] = {r["step_name"]: r["status"] for r in conn.execute(
        "SELECT s.step_name, s.status FROM meta_etl_step s"
        " JOIN meta_etl_run r ON r.run_id = s.run_id"
        " WHERE r.status = 'success' AND s.finished_at IS NOT NULL"
        " ORDER BY s.finished_at ASC")}
    # 被中止的尝试单独列出，不混进上面的"当前状态"
    out["aborted_steps"] = [dict(r) for r in conn.execute(
        "SELECT s.run_id, s.step_name, s.status, s.started_at, s.error"
        " FROM meta_etl_step s JOIN meta_etl_run r ON r.run_id = s.run_id"
        " WHERE r.status <> 'success' ORDER BY s.started_at DESC")]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--markdown", action="store_true", help="顺便打印一张 markdown 表")
    args = ap.parse_args(argv)

    conn = C.connect(args.db, readonly=True)
    data = collect(conn)
    conn.close()

    out = C.path("evidence", "report_numbers.json")
    C.write_json(out, data)
    print(f"已写入 {os.path.relpath(out, C.ROOT)}")

    r = data["reviews"]
    c = data["collection"]
    t = data["third_party"]
    e = data["entities"]
    f = data["fields"]
    print(f"\n读取记录      {r['records_read']:,}  (有效 {r['valid_records']:,} + 隔离 "
          f"{r['quarantined_records']:,})  恒等式 {'通过' if r['identity_ok'] else '不通过'}")
    print(f"官方口径      {r['official']['reviews']:,} 条评论 / "
          f"{r['official']['products']:,} 商品 / {r['official']['users']:,} 用户  "
          f"偏差 {r['delta_vs_official']['reviews']:+,} / "
          f"{r['delta_vs_official']['products']:+,} / {r['delta_vs_official']['users']:+,}")
    print(f"采集清单      {c['products_in_list']:,} 个 ASIN，实时成功 {c['live_success']:,}，"
          f"HTTP200但非成功 {c['http_200_but_not_success']:,}")
    print(f"第三方覆盖    {t['products_with_title']:,} 个 ASIN "
          f"({t['coverage_pct_of_all_asins']}%)")
    print(f"实体          {e['movies']:,} 部电影 / {e['persons']:,} 个人物，"
          f"商品-电影链接 {e['product_movie_links']:,}")
    print(f"字段选择      {f['selections']:,} 条（有值 {f['selections_with_value']:,}）")
    for k, v in (f.get("coverage") or {}).items():
        print(f"  覆盖 {k:14s} {v['pct']}%  ({v['selected']:,}/{v['denominator']:,})")

    if args.markdown:
        print("\n| 指标 | 数值 |\n|---|---|")
        for k, v in (
            ("读取记录", f"{r['records_read']:,}"),
            ("有效记录", f"{r['valid_records']:,}"),
            ("隔离记录", f"{r['quarantined_records']:,}"),
            ("逻辑评论", f"{r['logical_reviews']:,}"),
            ("商品 ASIN", f"{r['distinct_products']:,}"),
            ("评论用户", f"{r['distinct_reviewers']:,}"),
            ("实时采集成功", f"{c['live_success']:,}"),
            ("第三方元数据覆盖", f"{t['products_with_title']:,}"),
            ("电影实体", f"{e['movies']:,}"),
            ("人物实体", f"{e['persons']:,}"),
        ):
            print(f"| {k} | {v} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
