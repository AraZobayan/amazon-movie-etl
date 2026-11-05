"""导出作业要求的四类「合并 / 不合并」实例证据。

作业说明第六部分要求：

    任务4「展示至少 5 组跨版本合并实例及 5 组易混淆而未合并的实例」
    任务5「至少展示 5 组人物别名合并和 5 组同名或近似名不合并的案例，附可核实证据」

这份脚本把四类实例直接从库里查出来，落成
    evidence/merge_examples.md     可直接粘进报告
    evidence/merge_examples.json   机器可复核

**为什么必须从库里查而不是手写**：手写的实例无法证明它真的在库里，
也无法在重跑后保持一致。这里的每一行都带 decision_id / movie_id / person_id，
老师可以拿 ID 回库复核。

用法：
    python scripts/export_merge_examples.py
    python scripts/export_merge_examples.py --per 8 --db data/db/etl.sqlite3
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import common as C  # noqa: E402


# ---------------------------------------------------------------- 工具

def _title(conn: sqlite3.Connection, product_id: str) -> str:
    """商品的展示名：优先用解析出的页面标题，退回第三方/页面断言里的原始标题。"""
    row = conn.execute(
        "SELECT page_title FROM v_product_edition WHERE product_id=?", (product_id,)).fetchone()
    if row and row[0]:
        return row[0]
    row = conn.execute(
        "SELECT normalized_value FROM field_assertion "
        "WHERE entity_type='product' AND entity_id=? AND field_name='title' "
        "ORDER BY CASE WHEN extractor_rule_id LIKE '%PAGE%' THEN 0 ELSE 1 END LIMIT 1",
        (product_id,)).fetchone()
    return row[0] if row else "(无标题证据)"


def _movie_title(conn: sqlite3.Connection, movie_id: str) -> str:
    row = conn.execute("SELECT canonical_title FROM movie WHERE movie_id=?", (movie_id,)).fetchone()
    return row[0] if row and row[0] else movie_id


def _versions(conn: sqlite3.Connection, movie_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT product_id, page_title, edition_format, edition_release_date, studio,"
        "       collect_status, source_type, review_count"
        " FROM v_product_edition WHERE movie_id=? ORDER BY review_count DESC", (movie_id,)).fetchall()
    return [dict(zip(("product_id", "page_title", "edition_format", "edition_release_date",
                      "studio", "collect_status", "source_type", "review_count"), r)) for r in rows]


def _decisions(conn: sqlite3.Connection, etype: str, action: str, limit: int,
               min_score: float = 0.0) -> list[dict]:
    rows = conn.execute(
        "SELECT decision_id, action, id_a, id_b, cluster_key, score, rule_id, evidence_json"
        " FROM int_merge_decision WHERE entity_type=? AND action=? AND score>=?"
        " ORDER BY score DESC, decision_id LIMIT ?", (etype, action, min_score, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(zip(("decision_id", "action", "id_a", "id_b", "cluster_key", "score", "rule_id",
                      "evidence_json"), r))
        d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
        out.append(d)
    return out


def _ev_text(ev: dict) -> str:
    """把 evidence_json 压成一行人话，别让读者去解析 JSON。"""
    bits = []
    if "title_sim" in ev:
        bits.append(f"标题相似度 {ev['title_sim']}")
    if ev.get("title_relation"):
        bits.append(f"关系 {ev['title_relation']}")
    if ev.get("director_sim") is not None:
        bits.append(f"导演相似度 {ev['director_sim']}")
    if ev.get("cast_sim") is not None:
        bits.append(f"演员重合度 {ev['cast_sim']}")
    if "year_diff" in ev:
        bits.append("年份相同" if ev["year_diff"] == 0 else
                    (f"年份差 {ev['year_diff']}" if ev["year_diff"] is not None else "年份未知"))
    if ev.get("relation"):
        bits.append(f"姓名关系 {ev['relation']}")
    if ev.get("shared_movies"):
        bits.append(f"共现作品 {len(ev['shared_movies'])} 部")
    if ev.get("shared_pages"):
        bits.append(f"共现页面 {len(ev['shared_pages'])} 个")
    return "；".join(bits) if bits else "(无结构化证据)"


# ---------------------------------------------------------------- 四类实例

def movie_version_merges(conn, per: int) -> list[dict]:
    """同一 movie_id 下挂多个 ASIN —— 跨版本合并。"""
    rows = conn.execute(
        "SELECT movie_id, COUNT(*) n FROM v_product_edition WHERE movie_id IS NOT NULL"
        " GROUP BY movie_id HAVING n>=3 ORDER BY n DESC LIMIT ?", (per * 3,)).fetchall()
    out = []
    for movie_id, n in rows:
        vs = _versions(conn, movie_id)
        # 版本之间必须真的「不同版本」，否则只是重复商品：要求至少两种载体或两个发售日
        fmts = {v["edition_format"] for v in vs if v["edition_format"]}
        dates = {v["edition_release_date"] for v in vs if v["edition_release_date"]}
        if len(fmts) < 2 and len(dates) < 2:
            continue
        ds = conn.execute(
            "SELECT decision_id, id_a, id_b, score, rule_id, evidence_json FROM int_merge_decision"
            " WHERE entity_type='movie' AND action='auto_merge'"
            "   AND (id_a IN (SELECT product_id FROM product_movie WHERE movie_id=?)"
            "        OR id_b IN (SELECT product_id FROM product_movie WHERE movie_id=?))"
            " ORDER BY score DESC LIMIT 2", (movie_id, movie_id)).fetchall()
        evs = [{"decision_id": d[0], "pair": [d[1], d[2]], "score": d[3], "rule_id": d[4],
                "evidence": json.loads(d[5] or "{}")} for d in ds]
        out.append({"movie_id": movie_id, "canonical_title": _movie_title(conn, movie_id),
                    "version_count": n, "formats": sorted(fmts), "versions": vs[:6],
                    "decisions": evs})
        if len(out) >= per:
            break
    return out


def movie_not_merged(conn, per: int) -> list[dict]:
    """分数够高但没合并 —— 易混淆的对照组。

    同一簇（cluster_key）只取一对：Kill Bill Vol.1/Vol.2 之间有十几对候选，
    全列出来只会把表填满，读者看不到别的情形。
    """
    out, seen, seen_cluster = [], set(), set()
    for action, min_score in (("manual_review", 0.80), ("no_merge", 0.55)):
        for d in _decisions(conn, "movie", action, per * 20, min_score):
            key = (d["id_a"], d["id_b"])
            if key in seen or d["cluster_key"] in seen_cluster:
                continue
            seen.add(key)
            ta, tb = _title(conn, d["id_a"]), _title(conn, d["id_b"])
            out.append({**d, "title_a": ta, "title_b": tb,
                        "movie_a": _movie_of(conn, d["id_a"]),
                        "movie_b": _movie_of(conn, d["id_b"])})
            seen_cluster.add(d["cluster_key"])
            if len(out) >= per:
                return out
    return out


def _movie_of(conn, product_id: str) -> str | None:
    row = conn.execute("SELECT movie_id FROM product_movie WHERE product_id=?",
                       (product_id,)).fetchone()
    return row[0] if row else None


def person_alias_merges(conn, per: int) -> list[dict]:
    """一个 person_id 下挂多个署名写法 —— 别名归并。"""
    rows = conn.execute(
        "SELECT person_id, COUNT(*) n FROM person_name GROUP BY person_id HAVING n>=2"
        " ORDER BY n DESC LIMIT ?", (per * 3,)).fetchall()
    out = []
    for pid, n in rows:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM person_name WHERE person_id=? ORDER BY name", (pid,))]
        canon = conn.execute("SELECT canonical_name FROM person WHERE person_id=?",
                             (pid,)).fetchone()
        ds = conn.execute(
            "SELECT decision_id, id_a, id_b, score, rule_id, evidence_json FROM int_merge_decision"
            " WHERE entity_type='person' AND action='auto_merge' AND (id_a=? OR id_b=?)"
            " ORDER BY score DESC LIMIT 3",
            tuple([C.norm_person_name(x) for x in names[:1]] * 2)).fetchall()
        evs = [{"decision_id": d[0], "pair": [d[1], d[2]], "score": d[3], "rule_id": d[4],
                "evidence": json.loads(d[5] or "{}")} for d in ds]
        movies = [r[0] for r in conn.execute(
            "SELECT DISTINCT m.canonical_title FROM movie_person mp"
            " JOIN movie m ON m.movie_id=mp.movie_id WHERE mp.person_id=? LIMIT 4", (pid,))]
        out.append({"person_id": pid, "canonical_name": canon[0] if canon else None,
                    "aliases": names, "decisions": evs, "movies": movies})
        if len(out) >= per:
            break
    return out


def person_not_merged(conn, per: int) -> list[dict]:
    """同姓 / 同首字母但没合并 —— 待复核或明确不合并。

    两类要分开，混在一起会得出错误结论：

    * `same_surname` 且**两边都是完整姓名、名字不同** → 确实是两个人（如 Bill / Brian Murray）。
    * `initials_*` → 缩写能展开成多人，规则不敢合并，状态是「待复核」，不是「确认不同」。

    只取 `action != 'auto_merge'` 的决策：已经合并的对不能拿来当「不合并案例」，
    那是自相矛盾。
    """
    out, seen = [], set()

    def _rows(rel: str) -> list[dict]:
        rs = conn.execute(
            "SELECT decision_id, action, id_a, id_b, cluster_key, score, rule_id, evidence_json"
            " FROM int_merge_decision WHERE entity_type='person'"
            "   AND action<>'auto_merge'"
            "   AND json_extract(evidence_json,'$.relation')=?"
            " ORDER BY score DESC, decision_id", (rel,)).fetchall()
        return [dict(zip(("decision_id", "action", "id_a", "id_b", "cluster_key", "score",
                          "rule_id", "evidence_json"), r)) for r in rs]

    def _take(rel: str, quota: int, verdict: str, accept) -> None:
        n = 0
        for d in _rows(rel):
            if n >= quota:
                return
            if not accept(d):
                continue
            key = tuple(sorted((d["id_a"], d["id_b"])))
            if key in seen:
                continue
            seen.add(key)
            d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
            pa = conn.execute("SELECT person_id FROM person_name WHERE name_norm=? LIMIT 1",
                              (d["id_a"],)).fetchone()
            pb = conn.execute("SELECT person_id FROM person_name WHERE name_norm=? LIMIT 1",
                              (d["id_b"],)).fetchone()
            out.append({**d, "verdict": verdict,
                        "person_a": pa[0] if pa else None,
                        "person_b": pb[0] if pb else None,
                        "same_entity": bool(pa and pb and pa[0] == pb[0])})
            n += 1

    def _two_full_names_different_first(d: dict) -> bool:
        """两边都是完整名字（首名不是单个字母）、且首名不同。

        单字母缩写（`b howard`）不能当"不同的人"的证据 —— 它可能就是
        `bryce dallas howard`。这种对留给「待复核」，不放进"确认是两个人"。
        """
        a, b = d["id_a"].split(), d["id_b"].split()
        return (len(a) >= 2 and len(b) >= 2
                and len(a[0]) > 2 and len(b[0]) > 2 and a[0] != b[0])

    _take("same_surname", max(per - 2, 3), "同姓、名字不同 → 规则判为两个人",
          _two_full_names_different_first)
    _take("initials_first_abbrev", 2, "缩写可展开成多人 → 待复核，未合并", lambda d: True)
    return out[:per]


# ---------------------------------------------------------------- 渲染

def render_md(data: dict) -> str:
    L = ["# 合并 / 不合并实例（自动导出）", "",
         f"数据库：`{data['db']}`　生成时间：{data['generated_at']}", "",
         "作业说明第六部分要求的四类实例。每行都带 `decision_id` / `movie_id` / `person_id`，",
         "可以回库复核，不是手写的说明文字。", ""]

    L += ["## 1 电影：跨版本合并（同一作品，多个 ASIN）", "",
          "判据：标题归一后相同或近似 + 演职员重合 + 年份不冲突，",
          "分数 ≥ 0.86 自动合并。载体（DVD / Blu-ray / Prime Video）与发售日**不同**恰恰是要合并的理由——",
          "它们是同一部作品的不同发行版本。", ""]
    for i, x in enumerate(data["movie_version_merges"], 1):
        L.append(f"### 实例 M{i}　{x['canonical_title']}　`{x['movie_id']}`")
        L.append("")
        L.append(f"- 商品版本数：**{x['version_count']}**；载体：{'、'.join(x['formats']) or '—'}")
        L.append("")
        L.append("| ASIN | 页面标题 | 载体 | 商品发售日 | 发行商 | 评论数 | 采集 |")
        L.append("|---|---|---|---|---|---|---|")
        for v in x["versions"]:
            L.append(f"| {v['product_id']} | {(v['page_title'] or '—')[:46]} | "
                     f"{v['edition_format'] or '—'} | {v['edition_release_date'] or '—'} | "
                     f"{(v['studio'] or '—')[:18]} | {v['review_count']} | {v['collect_status']} |")
        L.append("")
        if x["decisions"]:
            d = x["decisions"][0]
            L.append(f"- 合并证据：`{d['rule_id']}` 分数 {d['score']}（`{d['decision_id']}`）—— {_ev_text(d['evidence'])}")
            L.append(f"- 合并的 ASIN 对：{d['pair'][0]} ↔ {d['pair'][1]}")
        else:
            L.append("- 合并证据：该簇内无成对 `auto_merge` 记录（仅通过传递闭包并入）")
        L.append("")

    L += ["## 2 电影：易混淆但未合并（对照组）", "",
          "这些对的标题非常像，但**不足以判定同一作品**，所以停在 `manual_review` 或 `no_merge`。",
          "不合并的依据是：导演/演员不重合、年份冲突、或标题只是词面重叠。", ""]
    L.append("| # | ASIN A / 标题 | ASIN B / 标题 | 分数 | 判定 | 规则 | 证据 |")
    L.append("|---|---|---|---|---|---|---|")
    for i, x in enumerate(data["movie_not_merged"], 1):
        L.append(f"| {i} | {x['id_a']}<br>{(x['title_a'] or '')[:40]} | {x['id_b']}<br>"
                 f"{(x['title_b'] or '')[:40]} | {x['score']} | {x['action']} | `{x['rule_id']}` | "
                 f"{_ev_text(x['evidence'])} |")
    L.append("")

    L += ["## 3 人物：别名合并（同一人的不同署名）", "",
          "判据：缩写展开一致（`K. Reeves` → `Keanu Reeves`）、目录式写法、",
          "且参演作品重合。姓相同**不足以**合并——见下一节。", ""]
    for i, x in enumerate(data["person_alias_merges"], 1):
        L.append(f"### 实例 P{i}　{x['canonical_name']}　`{x['person_id']}`")
        L.append("")
        L.append(f"- 归并的署名：{'、'.join('`' + a + '`' for a in x['aliases'])}")
        if x["movies"]:
            L.append(f"- 参与作品：{'、'.join(x['movies'])}")
        if x["decisions"]:
            d = x["decisions"][0]
            L.append(f"- 合并证据：`{d['rule_id']}` 分数 {d['score']}（`{d['decision_id']}`）—— {_ev_text(d['evidence'])}")
        L.append("")

    L += ["## 4 人物：同名 / 近似名但未合并（对照组）", "",
          "作业原文：**「姓名相同不代表同一人；名字缩写相同更不能直接合并。」**",
          "下面这些对停在待复核或明确不合并，各自保留独立 `person_id`。", ""]
    L.append("| # | 署名 A | 署名 B | 分数 | 判定 | 规则 | 证据 | 当前实体 | 复核判断 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for i, x in enumerate(data["person_not_merged"], 1):
        state = ("已合并" if x["same_entity"] else
                 f"{x['person_a'] or '—'} / {x['person_b'] or '—'}")
        L.append(f"| {i} | `{x['id_a']}` | `{x['id_b']}` | {x['score']} | {x['action']} | "
                 f"`{x['rule_id']}` | {_ev_text(x['evidence'])} | {state} | {x['verdict']} |")
    L.append("")
    L.append("> `manual_review` 表示规则不敢自动合并，等人工复核；`no_merge` 表示证据明确不支持合并。")
    L.append("> 两者都**不会**产生合并，因此不会污染实体表。")
    L.append(">")
    L.append("> 这张表说明的是**规则做了什么决定**，不是「这两个署名一定属于不同的人」。")
    L.append("> 表里仍有个别对实际上是同一人（例如改名、仅署名姓氏的职员表写法），")
    L.append("> 这类漏合并是已知缺陷，见报告第 9 节「局限」。")
    L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--per", type=int, default=6, help="每类导出几组（作业要求至少 5）")
    args = ap.parse_args()

    conn = C.connect(args.db, readonly=True)
    data = {
        "db": args.db or C.CFG["db"]["path"],
        "generated_at": C.now_iso(),
        "movie_version_merges": movie_version_merges(conn, args.per),
        "movie_not_merged": movie_not_merged(conn, args.per),
        "person_alias_merges": person_alias_merges(conn, args.per),
        "person_not_merged": person_not_merged(conn, args.per),
    }
    conn.close()

    C.write_json(C.path("evidence", "merge_examples.json"), data)
    md = render_md(data)
    with open(C.path("evidence", "merge_examples.md"), "w", encoding="utf-8") as f:
        f.write(md)

    print(f"跨版本合并 {len(data['movie_version_merges'])} 组 / "
          f"未合并对照 {len(data['movie_not_merged'])} 组 / "
          f"人物别名合并 {len(data['person_alias_merges'])} 组 / "
          f"人物不合并对照 {len(data['person_not_merged'])} 组")
    print("-> evidence/merge_examples.md, evidence/merge_examples.json")
    ok = all(len(data[k]) >= 5 for k in data if k.endswith(("merges", "merged")))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
