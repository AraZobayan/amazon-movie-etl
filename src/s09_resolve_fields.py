"""任务 7：处理来源冲突 + 最终字段选择（Field Selection）。

这是"血缘"落地的地方：每个最终字段都记录
    采用了哪条断言、还有哪些候选、为什么这么选、用的是哪条规则。

本实现的选择规则（可复核，不是"最后抓到的值"）：

  S1 语义优先：字段语义必须匹配。release_date 只接受 theatrical（影院上映）来源；
     商品页的 "Release date" 是 DVD 上市日，规则 ID 里带 PHYSICAL，
     永远不会被选成上映日期 —— 但它作为候选仍然可查（作业要求"保留所有候选值"）。
  S2 权威性排序：同语义下按来源权威性排序（Wikidata > 商品页 > 合成夹具）。
  S3 精度保护：只知道年份就存 precision=year，绝不补成 1 月 1 日。
  S4 多值字段取并集：genre / cast 这类多值字段合并所有成员，每个成员单独可追溯。
  S5 无法决策时保留冲突状态：不猜、不编。

运行：python -m src.s09_resolve_fields
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

from src import common as C

RULES_VERSION = "select-rules-v1.0.0"


def authority(rule: str) -> float:
    """来源权威性（0~1）。规则 ID 自带语义，所以权威性也能从规则推出来。"""
    if rule.startswith("R-EXT-DATE-THEATRICAL"):
        return 1.00
    if rule.startswith("R-EXT-WD-GENRE"):
        return 0.90
    if rule.startswith("R-EXT-WD"):
        return 0.85
    if "PHYSICAL" in rule:
        return 0.35          # 商品发售日期：语义不同，权威性最低
    if rule.startswith("R-AGG"):
        return 0.50
    return 0.40


def select_scalar(conn, run_id: str, entity_type: str, entity_id: str, field: str,
                  cands: list[dict], *, rule_filter=None, decision_note: str,
                  rule_id: str) -> dict | None:
    """单值字段选择。返回被选中的候选（dict）或 None。"""
    pool = [c for c in cands if (rule_filter is None or rule_filter(c["extractor_rule_id"]))]
    if not pool:
        # 没有合格候选 -> 明确记录"缺失"，不填假值
        conn.execute(
            "INSERT OR REPLACE INTO field_selection (entity_type,entity_id,field_name,"
            "selected_assertion_id,candidate_ids_json,decision_reason,rule_id,"
            "selection_kind,run_id,decided_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (entity_type, entity_id, field, None,
             json.dumps([c["assertion_id"] for c in cands], ensure_ascii=False),
             decision_note, rule_id, "single", run_id, C.now_iso()))
        return None

    pool.sort(key=lambda c: (-authority(c["extractor_rule_id"]), -c["confidence"],
                             str(c["normalized_value"])))
    chosen = pool[0]
    conn.execute(
        "INSERT OR REPLACE INTO field_selection (entity_type,entity_id,field_name,"
        "selected_assertion_id,candidate_ids_json,decision_reason,rule_id,"
        "selection_kind,run_id,decided_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (entity_type, entity_id, field, chosen["assertion_id"],
         json.dumps([c["assertion_id"] for c in cands], ensure_ascii=False),
         f"{decision_note}；采用 {chosen['extractor_rule_id']}"
         f"（权威性 {authority(chosen['extractor_rule_id']):.2f}，"
         f"候选 {len(pool)} 个，共 {len(cands)} 个）",
         rule_id, "single", run_id, C.now_iso()))
    conn.execute("UPDATE field_assertion SET status='selected' WHERE assertion_id=?",
                 (chosen["assertion_id"],))
    for c in cands:
        if c["assertion_id"] != chosen["assertion_id"]:
            conn.execute("UPDATE field_assertion SET status='rejected' WHERE assertion_id=?",
                         (c["assertion_id"],))
    return chosen


def select_multi(conn, run_id: str, entity_type: str, entity_id: str, field: str,
                 cands: list[dict], *, rule_id: str, note: str) -> int:
    """多值字段：取并集，每个成员都保留并可追溯。"""
    if not cands:
        return 0
    seen: dict[str, dict] = {}
    for c in cands:
        seen.setdefault(c["normalized_value"], c)
    for c in seen.values():
        conn.execute("UPDATE field_assertion SET status='selected' WHERE assertion_id=?",
                     (c["assertion_id"],))
    conn.execute(
        "INSERT OR REPLACE INTO field_selection (entity_type,entity_id,field_name,"
        "selected_assertion_id,candidate_ids_json,decision_reason,rule_id,"
        "selection_kind,run_id,decided_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (entity_type, entity_id, field, None,
         json.dumps([c["assertion_id"] for c in seen.values()], ensure_ascii=False),
         f"{note}（并集，去重后 {len(seen)} 个成员）", rule_id, "multi", run_id, C.now_iso()))
    return len(seen)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    conn = C.connect(args.db)
    log = C.Logger("s09_resolve_fields")
    run_id = C.start_run(conn, "s09_resolve_fields", {"rules": RULES_VERSION})
    log.run_id = run_id

    movies = [r["movie_id"] for r in conn.execute("SELECT movie_id FROM movie ORDER BY movie_id")]
    log.info(f"待选择字段的作品 {len(movies):,} 部")

    stat = {"movies": len(movies), "with_theatrical": 0, "only_physical": 0,
            "no_date": 0, "conflicts": 0, "genre_members": 0, "precision": defaultdict(int)}

    with C.step(conn, run_id, "select_movie_fields", 1, log) as st:
        for mid in movies:
            by_field: dict[str, list[dict]] = defaultdict(list)
            for r in conn.execute(
                    "SELECT assertion_id,field_name,raw_value,normalized_value,value_num,"
                    "extractor_rule_id,confidence FROM field_assertion"
                    " WHERE entity_type='movie' AND entity_id=?", (mid,)):
                by_field[r["field_name"]].append(dict(r))

            # ---- 标题：取"出现次数最多"的规范化标题（众数），平票取更长的 ----
            titles = by_field.get("title", [])
            if titles:
                freq: dict[str, int] = defaultdict(int)
                for t in titles:
                    freq[t["normalized_value"]] += 1
                best_norm = max(freq, key=lambda k: (freq[k], len(k)))
                chosen = select_scalar(conn, run_id, "movie", mid, "canonical_title", titles,
                                       rule_filter=lambda r, bn=best_norm: True,
                                       decision_note=f"标题取众数（{freq[best_norm]} 次出现）",
                                       rule_id="R-SEL-TITLE-MODE")
                # 若众数对应的不是排序第一名，人工纠正为众数那条
                if chosen and chosen["normalized_value"] != best_norm:
                    target = next(t for t in titles if t["normalized_value"] == best_norm)
                    conn.execute(
                        "UPDATE field_selection SET selected_assertion_id=?, decision_reason=?"
                        " WHERE entity_type='movie' AND entity_id=? AND field_name='canonical_title'",
                        (target["assertion_id"],
                         f"标题取众数（{freq[best_norm]} 次出现，共 {len(titles)} 条候选）",
                         mid))
                    conn.execute("UPDATE field_assertion SET status='selected' WHERE assertion_id=?",
                                 (target["assertion_id"],))

            # ---- 上映日期：只接受 theatrical 语义 ----
            dates = by_field.get("release_date", [])
            picked = select_scalar(
                conn, run_id, "movie", mid, "release_date", dates,
                rule_filter=lambda r: "THEATRICAL" in r,
                decision_note="上映日期只接受影院上映（theatrical）语义；"
                              "商品发售日期（physical）语义不同，不作为上映日期",
                rule_id="R-SEL-DATE-THEATRICAL-ONLY")
            if picked:
                stat["with_theatrical"] += 1
                d = picked["normalized_value"] or ""
                # 精度从值的「长度」反推，而不是从来源猜：
                #   YYYY-MM-DD(10) -> day；YYYY-MM(7) -> month；YYYY(4) -> year
                # 这样即使换数据源，只要它按 ISO 截断，精度就不会丢。
                prec = {10: "day", 7: "month", 4: "year"}.get(len(d), "unknown")
                stat["precision"][prec] += 1
                conn.execute(
                    "UPDATE movie SET release_date=?, release_year=?, release_precision=?,"
                    " release_type='theatrical' WHERE movie_id=?",
                    (d if prec == "day" else None,
                     int(d[:4]) if d[:4].isdigit() else None, prec, mid))
            else:
                has_phys = any("PHYSICAL" in x["extractor_rule_id"] for x in dates)
                stat["only_physical" if has_phys else "no_date"] += 1
                conn.execute(
                    "UPDATE movie SET release_date=NULL, release_year=NULL,"
                    " release_precision='unknown', release_type=NULL WHERE movie_id=?", (mid,))

            # ---- 冲突登记：同字段出现多个不同规范化值 ----
            # 注意两个层次：断言表用**源级字段名**（title / genre），
            # 选择表用**实体级字段名**（canonical_title / genres）。
            # 早期版本这里直接读 by_field["canonical_title"]（恒空），
            # 于是标题冲突从未登记（全库 source_conflict 只有 release_date）。
            # 记录到 meta_quality_issue 的 field_name 用实体级名，读断言用源级名。
            for field, assert_field in (("release_date", "release_date"),
                                        ("canonical_title", "title")):
                vals = {x["normalized_value"] for x in by_field.get(assert_field, [])}
                if len(vals) > 1:
                    stat["conflicts"] += 1
                    conn.execute(
                        "INSERT INTO meta_quality_issue (issue_id,run_id,entity_type,entity_id,"
                        "field_name,issue_type,severity,detail,created_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(issue_id) DO NOTHING",
                        (C.det_id("QI", "conflict", mid, field, RULES_VERSION), run_id, "movie",
                         mid, field, "source_conflict", "info",
                         json.dumps({"values": sorted(vals),
                                     "note": "已按权威性/众数规则选择，全部候选保留可查"},
                                    ensure_ascii=False), C.now_iso()))

            # ---- 类型（多值并集）----
            n_genre = select_multi(conn, run_id, "movie", mid, "genres",
                                   by_field.get("genre", []), rule_id="R-SEL-GENRE-UNION",
                                   note="电影类型为多值字段，取各来源并集")
            stat["genre_members"] += n_genre

            # ---- 外部标识 ----
            select_scalar(conn, run_id, "movie", mid, "external_id",
                          by_field.get("external_id", []),
                          rule_filter=lambda r: r.startswith("R-EXT-WD-QID"),
                          decision_note="优先采用 Wikidata QID 作为作品外部标识",
                          rule_id="R-SEL-EXTERNAL-ID")

            # ---- 版本归并层次说明（作业要求"明确归并层次与依据"）----
            eds = sorted({x["normalized_value"] for x in by_field.get("edition_format", [])})
            if eds:
                conn.execute("UPDATE movie SET edition_scope=? WHERE movie_id=?",
                             ("同一作品的商品版本：" + " / ".join(eds), mid))

        st["counters"].update({k: v for k, v in stat.items() if k != "precision"})
        st["counters"]["precision"] = dict(stat["precision"])

    # ---- 人物规范名选择 ----
    persons = [r["person_id"] for r in conn.execute("SELECT person_id FROM person")]
    for pid in persons:
        cands = [dict(r) for r in conn.execute(
            "SELECT assertion_id,field_name,raw_value,normalized_value,value_num,"
            "extractor_rule_id,confidence FROM field_assertion"
            " WHERE entity_type='person' AND entity_id=? AND field_name='canonical_name'", (pid,))]
        if cands:
            select_scalar(conn, run_id, "person", pid, "canonical_name", cands,
                          decision_note="人物规范名取最完整写法（别名保留在 person_name）",
                          rule_id="R-SEL-PERSON-CANONICAL")

    # ---- 投影一致性：movie.release_* 必须与 field_selection 一致 ----
    bad = conn.execute("""
        SELECT COUNT(*) c FROM movie m
        LEFT JOIN field_selection fs ON fs.entity_type='movie' AND fs.entity_id=m.movie_id
             AND fs.field_name='release_date'
        LEFT JOIN field_assertion fa ON fa.assertion_id=fs.selected_assertion_id
        WHERE COALESCE(CAST(substr(fa.normalized_value,1,4) AS INTEGER),-1)
              <> COALESCE(m.release_year,-1)
    """).fetchone()["c"]

    n_sel = conn.execute("SELECT COUNT(*) c FROM field_selection").fetchone()["c"]
    # "没有值"必须按 selection_kind 分支统计：
    # 多值字段（genres）的 selected_assertion_id 恒为 NULL，成员在 candidate_ids_json 里。
    # 只看 selected_assertion_id IS NULL 会把 39 条有成员的 genres 选择也算成"无值"。
    n_none = conn.execute(
        "SELECT COUNT(*) c FROM field_selection WHERE selected_assertion_id IS NULL"
        " AND NOT (selection_kind='multi' AND candidate_ids_json IS NOT NULL"
        "          AND candidate_ids_json NOT IN ('','[]','null'))").fetchone()["c"]
    summary = {
        "run_id": run_id, "rules_version": RULES_VERSION,
        "movies": stat["movies"],
        "release_date_selected": stat["with_theatrical"],
        "only_physical_date_available": stat["only_physical"],
        "no_date_at_all": stat["no_date"],
        "precision_distribution": dict(stat["precision"]),
        "conflicts_logged": stat["conflicts"],
        "genre_members_selected": stat["genre_members"],
        "field_selections": n_sel,
        "selections_without_value": n_none,
        "projection_mismatch": bad,
        "note": "only_physical_date_available 表示该作品只查到 DVD 发售日、查不到影院上映日 —— "
                "按规则上映日期保持 NULL，商品发售日留在候选里可查。"
                "字段名分两个层次：field_selection 用实体级名（canonical_title/genres），"
                "field_assertion 用源级名（title/genre）；血缘查询通过 candidate_ids_json "
                "关联候选，不依赖两侧同名。",
    }
    C.write_json(C.path("evidence", "s09_selection_summary.json"), summary)
    log.info(f"上映日期选定 {stat['with_theatrical']:,} 部；仅有发售日 {stat['only_physical']:,} 部；"
             f"完全缺失 {stat['no_date']:,} 部；冲突登记 {stat['conflicts']}；"
             f"投影不一致 {bad}")

    C.end_run(conn, run_id, "success", f"selections={n_sel}")
    conn.close()
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
