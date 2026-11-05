"""任务 4：定义并识别"同一电影"（实体消歧 / entity resolution）。

一、什么叫"同一部电影"（操作性定义，先写文字再写代码）
--------------------------------------------------------
同一创作作品在 DVD / Blu-ray / 不同语言 / 不同地区 / 不同剪辑版中的**商品**，
归为同一个 movie_id，同时保留各自的 product_id（ASIN）与 edition 信息。

例外规则（本实现采用）：
  R1 重拍片、续集、同名不同作品：分别建模，不合并。
  R2 合集商品：一个 product 映射多个 movie（多对多），合集评论仍只归属商品。
  R3 剧集/单集：不进 movie 表（category=series），单独统计，避免与电影混算。
  R4 导演剪辑版/修复版/不同片长：归入同一 movie，用 edition 字段区分（本实现保留
     product 层的 edition_format，不把它们拆成新作品）。

二、算法（四步，教科书式的 entity resolution）
--------------------------------------------------------
  1) 分块 Blocking：按标题的"显著词签名"分桶，只在桶内两两比较。
     把 O(n^2) 降到 O(Σ bucket^2)，这是处理十万级商品的唯一可行办法。
  2) 打分 Scoring：标题相似度 + 导演重合 + 主演重合 + 年份接近度，加权成 0~1。
  3) 判定 Decision：>0.86 自动合并；0.62~0.86 人工复核；<0.62 不合并。
  4) 聚类 Clustering：用并查集（union-find）把自动合并的边连成簇。
     并查集天然产生"传递闭包"——A~B、B~C 会把 A、C 也连起来，
     所以必须额外检查"传递合并错误"（见 transitivity_conflicts 统计）。

三、为什么年份不能当主证据
--------------------------------------------------------
商品页上的日期是**发售日期**（DVD 上市日），同一部电影的 1999 年首版和
2007 年修复版相差 8 年。所以"年份差"只能做弱证据；
真正区分同名作品（True Grit 1969 / 2010）的是**导演和主演是否同一批人**。

运行：python -m src.s06_match_movies
"""
from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
from collections import defaultdict

from src import common as C

RULES_VERSION = "merge-rules-v1.0.0"


# --------------------------------------------------------------------------- #
# 并查集（Union-Find）
# --------------------------------------------------------------------------- #
class DSU:
    """带路径压缩 + 按秩合并的并查集。

    为什么用它：实体消歧的最后一步是"把互为同一实体的点连成连通分量"。
    并查集能在近 O(1) 的均摊时间内完成合并与查询，
    比反复做图遍历简单得多。

    它也是"传递合并错误"的来源：A-B 相似、B-C 相似，会让 A-C 被连到一起，
    即使 A 与 C 其实不是同一部作品。所以合并完必须抽查簇内一致性。
    """

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.rank: dict[str, int] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        self.rank.setdefault(x, 0)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:      # 路径压缩
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1
        return True

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for x in list(self.parent):
            out[self.find(x)].append(x)
        return dict(out)


# --------------------------------------------------------------------------- #
# 打分
# --------------------------------------------------------------------------- #


def clean_title(raw: str | None) -> str:
    """从商品标题里剥掉版本噪声，得到"作品名"。

    "The Matrix (2-Disc Special Edition)"  ->  "The Matrix"
    "Matrix, The (DVD)"                    ->  "Matrix, The"  （再交给 norm_title 处理词序）
    """
    if not raw:
        return ""
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", raw)
    t = re.sub(r"\b(DVD|Blu-?ray|UHD|4K|Widescreen Edition|Collector'?s Edition|"
               r"Special Edition|Prime Video)\b", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" ,-")


def block_key(title_norm: str) -> str:
    """分块键：取规范化标题里最长的两个词，排序后拼起来。

    "the matrix" -> "matrix|the"
    "matrix, the" -> "matrix|the"      （同一个桶，正确）
    "the matrix reloaded" -> "matrix|reloaded"（不同桶，也就不会误合并）

    这种"词袋签名"分块对词序不敏感，但对"续集多一个词"敏感 ——
    正好符合我们的需要。
    """
    toks = sorted({t for t in title_norm.split() if len(t) >= 3})
    if not toks:
        toks = sorted(title_norm.split())
    return "|".join(toks[:2])


def title_relation(a: str, b: str) -> str:
    """标题关系：equal / prefix / overlap / different。

    prefix 专门用来抓续集与"上下集"：
        the matrix  ⊂  the matrix reloaded
        kill bill vol 1 ⊂ kill bill vol 2  （不是前缀，但词集高度重合 -> overlap）
    """
    ta, tb = a.split(), b.split()
    if a == b:
        return "equal"
    if len(ta) < len(tb) and tb[:len(ta)] == ta:
        return "prefix"
    if len(tb) < len(ta) and ta[:len(tb)] == tb:
        return "prefix"
    return "overlap"


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def score_pair(a: dict, b: dict) -> tuple[float, dict]:
    """给一对产品打分，返回 (score, features)。

    权重说明（在 dev 样本上调出来的，见手册第 9 章）：
        标题 0.50 —— 最强信号，但会被续集污染，所以配合 prefix 规则
        导演 0.22 —— 同名作品的判别主力
        主演 0.13
        年份 0.15 —— 弱信号（发售年 ≠ 上映年）
    """
    ts = C.title_similarity(a["title"], b["title"])
    rel = title_relation(a["title_norm"], b["title_norm"])
    dsim = jaccard(a["directors"], b["directors"])
    csim = jaccard(a["cast"], b["cast"])
    ydiff = abs((a["year"] or 0) - (b["year"] or 0)) if (a["year"] and b["year"]) else None
    yscore = 1.0 if ydiff is None else max(0.0, 1.0 - max(0, ydiff - 2) / 10.0)

    score = 0.50 * ts + 0.22 * dsim + 0.13 * csim + 0.15 * yscore

    # 硬规则调整（规则优先级高于打分，这是可解释性的关键）
    if rel == "prefix" and ts < 0.95:
        score = min(score, 0.30)          # 续集/上下集：压到不合并区间
    if rel == "equal" and dsim > 0:
        score = max(score, 0.93)          # 同名 + 同导演 = 同一作品的版本
    if rel == "equal" and dsim == 0 and csim == 0 and a["directors"] and b["directors"]:
        score = min(score, 0.45)          # 同名但创作团队完全不同 = 重拍片

    feats = {
        "title_sim": round(ts, 4), "title_relation": rel,
        "director_sim": round(dsim, 4), "cast_sim": round(csim, 4),
        "year_diff": ydiff, "year_score": round(yscore, 4),
    }
    return round(score, 4), feats


def decide(score: float, feats: dict, cfg: dict) -> tuple[str, str]:
    """阈值判定，返回 (action, rule_id)。"""
    if feats["title_relation"] == "prefix" and score < 0.6:
        return "no_merge", "R-MRG-SEQUEL-PREFIX"
    if score >= cfg["movie_auto_merge"]:
        if feats["title_relation"] == "equal" and feats["director_sim"] > 0:
            return "auto_merge", "R-MRG-SAME-TITLE-SAME-CREW"
        return "auto_merge", "R-MRG-HIGH-SCORE"
    if score >= cfg["movie_manual_review"]:
        return "manual_review", "R-MRG-MID-SCORE"
    return "no_merge", "R-MRG-LOW-SCORE"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def load_products(conn) -> dict[str, dict]:
    """从断言表里还原"产品 -> 可用于匹配的特征"。"""
    prods: dict[str, dict] = defaultdict(lambda: {
        "title": None, "title_norm": None, "year": None,
        "directors": set(), "cast": set(), "source_record_id": None, "category": None})

    for r in conn.execute(
            "SELECT entity_id, field_name, raw_value, normalized_value, value_num, source_record_id"
            " FROM field_assertion WHERE entity_type='product'"):
        p = prods[r["entity_id"]]
        f = r["field_name"]
        if f == "title" and not p["title"]:
            p["title"] = r["raw_value"]
            p["title_norm"] = r["normalized_value"]
            p["source_record_id"] = r["source_record_id"]
        elif f == "release_date" and p["year"] is None:
            p["year"] = int(r["value_num"]) if r["value_num"] else None
        elif f == "director":
            p["directors"].add(r["normalized_value"])
        elif f == "cast":
            p["cast"].add(r["normalized_value"])

    for r in conn.execute("SELECT product_id, category FROM stg_classification"):
        if r["product_id"] in prods:
            prods[r["product_id"]]["category"] = r["category"]
    return {k: v for k, v in prods.items() if v["title"] and v["title_norm"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args(argv)

    cfg = C.CFG["match"]
    conn = C.connect(args.db)
    log = C.Logger("s06_match_movies")
    run_id = C.start_run(conn, "s06_match_movies", {"rules": RULES_VERSION})
    log.run_id = run_id

    if args.reset:
        for t in ("product_movie", "movie_title", "movie", "int_pair_candidate", "int_merge_decision"):
            conn.execute(f"DELETE FROM {t}")
        log.warn("已清空 movie / product_movie / 候选对 / 合并决策")

    prods = load_products(conn)
    log.info(f"可参与匹配的产品 {len(prods):,} 个")

    # ---------- 1) 分块 ----------
    buckets: dict[str, list[str]] = defaultdict(list)
    for pid, p in prods.items():
        if p["category"] in ("non_movie", "series", "concert_other"):
            continue
        buckets[block_key(p["title_norm"])].append(pid)
    sizes = sorted((len(v) for v in buckets.values()), reverse=True)
    n_pairs = sum(len(v) * (len(v) - 1) // 2 for v in buckets.values())
    n_all = len(prods) * (len(prods) - 1) // 2
    log.info(f"分块：{len(buckets):,} 个桶，最大桶 {sizes[0] if sizes else 0}，"
             f"候选对 {n_pairs:,}（全量两两比较需 {n_all:,}，省 "
             f"{(1 - n_pairs / max(n_all,1)) * 100:.2f}%）")

    # ---------- 2) 桶内打分 ----------
    dsu = DSU()
    decisions: list[tuple] = []
    pairs: list[tuple] = []
    stat = {"pairs": 0, "auto_merge": 0, "manual_review": 0, "no_merge": 0,
            "transitive_conflicts": 0, "clusters": 0, "singletons": 0}
    merged_edges: list[tuple[str, str, float]] = []

    for key, pids in buckets.items():
        # 超大桶怎么办？
        #
        # 早期版本的做法是**整桶跳过**，理由是"桶太大说明分块失败"。
        # 但实测发现这是错的：桶大恰恰说明这一组商品共享同样的两个关键词，
        # 而同名多版本（DVD / Blu-ray / 修复版 / 合集）正是最该合并的情形。
        # 全量数据里最大的桶有 177 个商品 —— 把这一桶丢掉，
        # 等于**把所有最热门的作品的合并机会全部放弃**，剩下的全是长尾单件，
        # 消歧这一步就白做了。
        #
        # 正确做法是**二次分块**：在粗桶内部按"完整规范化标题"再分一次，
        # 只比较真正同名的商品。这样既避开了 O(n²)，又保住了最有价值的比较。
        # 同名组如果还是太大（比如 200 个都是 "the matrix"），才真的跳过 ——
        # 那种情况下分数区分度已经很低，人工复核更合适。
        if len(pids) > cfg["max_block_size"]:
            sub: dict[str, list[str]] = defaultdict(list)
            for pid in pids:
                sub[prods[pid]["title_norm"]].append(pid)
            groups = [sorted(g) for g in sub.values() if len(g) > 1]
            too_big = [g for g in groups if len(g) > 200]
            groups = [g for g in groups if len(g) <= 200]
            stat.setdefault("oversized_buckets", 0)
            stat["oversized_buckets"] += 1
            stat.setdefault("subblocked_groups", 0)
            stat["subblocked_groups"] += len(groups)
            if too_big:
                log.warn(f"桶 {key} 内有 {len(too_big)} 个同名组超过 200 个商品，跳过（需人工复核）")
        else:
            groups = [sorted(pids)]

        for grp in groups:
            for a, b in itertools.combinations(grp, 2):
                pa, pb = prods[a], prods[b]
                score, feats = score_pair(pa, pb)
                if feats["title_sim"] < cfg["min_title_similarity"]:
                    continue
                action, rule = decide(score, feats, cfg)
                stat["pairs"] += 1
                stat[action] += 1
                cid = C.det_id("PC", "movie", a, b)
                pairs.append((cid, "movie", a, b, key, score,
                              json.dumps(feats, ensure_ascii=False), RULES_VERSION, run_id))
                did = C.det_id("D", "movie", a, b, RULES_VERSION)
                decisions.append((did, "movie", action, a, b, key, score, RULES_VERSION, rule,
                                  json.dumps(feats, ensure_ascii=False), run_id, C.now_iso()))
                if action == "auto_merge":
                    dsu.union(a, b)
                    merged_edges.append((a, b, score))

    # ---------- 3) 传递合并错误检查 ----------
    # 簇内任意两点如果"单独比较"时分数低于自动合并阈值，说明是传递闭包带进来的
    edge_score = {(a, b): s for a, b, s in merged_edges}
    groups = dsu.groups()
    for root, members in groups.items():
        if len(members) < 3:
            continue
        for a, b in itertools.combinations(sorted(members), 2):
            if (a, b) in edge_score or (b, a) in edge_score:
                continue
            score, feats = score_pair(prods[a], prods[b])
            if score < cfg["movie_auto_merge"]:
                stat["transitive_conflicts"] += 1
                conn.execute(
                    "INSERT INTO meta_quality_issue (issue_id,run_id,entity_type,entity_id,field_name,"
                    "issue_type,severity,detail,created_at) VALUES (?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(issue_id) DO NOTHING",
                    (C.det_id("QI", "transitive", a, b), run_id, "movie", root, None,
                     "transitive_merge_conflict", "warn",
                     json.dumps({"a": a, "b": b, "score": score, "cluster": root,
                                 "note": "簇内两点直接相似度低于阈值，属于传递闭包带入"},
                                ensure_ascii=False), C.now_iso()))

    # ---------- 4) 落库：候选对 + 决策 ----------
    with C.transaction(conn):
        conn.executemany(
            "INSERT INTO int_pair_candidate (candidate_id,entity_type,id_a,id_b,block_key,score,"
            "features_json,rules_version,run_id) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(candidate_id) DO UPDATE SET score=excluded.score,"
            " features_json=excluded.features_json, run_id=excluded.run_id", pairs)
        conn.executemany(
            "INSERT INTO int_merge_decision (decision_id,entity_type,action,id_a,id_b,cluster_key,"
            "score,threshold_version,rule_id,evidence_json,run_id,decided_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(decision_id) DO UPDATE SET action=excluded.action, score=excluded.score,"
            " evidence_json=excluded.evidence_json, run_id=excluded.run_id", decisions)

    # ---------- 5) 建 movie 实体 ----------
    groups = dsu.groups()
    singles = [pid for pid in prods
               if pid not in {m for g in groups.values() for m in g}
               and prods[pid]["category"] in ("movie", "box_set", None)]
    all_groups = [sorted(v) for v in groups.values()] + [[p] for p in singles]
    stat["clusters"] = len(all_groups)
    stat["singletons"] = len(singles)

    # 同一规范化标题可能有多个簇（重拍片），用最小年份消歧，保证 ID 稳定
    by_title: dict[str, list[int]] = defaultdict(list)
    for i, g in enumerate(all_groups):
        by_title[prods[g[0]]["title_norm"]].append(i)

    movie_rows, title_rows, link_rows, agg_rows, agg_links = [], [], [], [], []
    for i, g in enumerate(all_groups):
        rep = min(g, key=lambda p: (len(prods[p]["title"]), p))
        base = prods[rep]["title_norm"]
        years = [prods[p]["year"] for p in g if prods[p]["year"]]
        if len(by_title[base]) > 1:
            movie_id = C.det_id("M", base, min(years) if years else 0)
        else:
            movie_id = C.det_id("M", base)
        p0 = prods[rep]
        canon = clean_title(p0["title"]) or p0["title"]
        movie_rows.append((movie_id, canon, canon,
                           None, None, "unknown", None, None,
                           f"cluster_size={len(g)}", "active", run_id))
        title_rows.append((movie_id, p0["title"], "en", "original", p0["source_record_id"]))
        title_rows.append((movie_id, canon, "en", "canonical", p0["source_record_id"]))
        for pid in g:
            rel = "box_set_member" if prods[pid]["category"] == "box_set" else "single"
            link_rows.append((pid, movie_id, rel, "title_year_director",
                              max((s for a, b, s in merged_edges
                                   if {a, b} & {pid} and ({a, b} <= set(g))), default=None),
                              None, prods[pid]["source_record_id"]))

        # movie 级断言：把 product 级断言"升格"为 movie 级，并用 link 记录血缘
        for pid in g:
            for r in conn.execute(
                    "SELECT assertion_id, field_name, raw_value, normalized_value, source_record_id,"
                    " extractor_rule_id, confidence FROM field_assertion"
                    " WHERE entity_type='product' AND entity_id=? AND field_name IN"
                    " ('title','release_date','edition_format','studio')", (pid,)):
                child = C.det_id("A", "movie", movie_id, r["field_name"], r["source_record_id"],
                                 r["normalized_value"])
                agg_rows.append((child, "movie", movie_id, r["field_name"], r["raw_value"],
                                 r["normalized_value"], None, r["source_record_id"],
                                 "R-AGG-" + r["extractor_rule_id"], "aggregate-v1",
                                 r["confidence"], "candidate", run_id, C.now_iso()))
                agg_links.append((child, r["assertion_id"], "merge",
                                  "R-LINK-PRODUCT-TO-MOVIE", f"来自产品 {pid}"))

    with C.transaction(conn):
        conn.executemany(
            "INSERT INTO movie (movie_id,canonical_title,original_title,release_year,release_date,"
            "release_precision,release_region,release_type,edition_scope,status,created_run_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(movie_id) DO NOTHING", movie_rows)
        conn.executemany(
            "INSERT INTO movie_title (movie_id,title,lang,title_type,source_record_id)"
            " VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING", title_rows)
        conn.executemany(
            "INSERT INTO product_movie (product_id,movie_id,relation_type,match_method,confidence,"
            "decision_id,source_record_id) VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING", link_rows)
        conn.executemany(
            "INSERT INTO field_assertion (assertion_id,entity_type,entity_id,field_name,raw_value,"
            "normalized_value,value_num,source_record_id,extractor_rule_id,extractor_version,"
            "confidence,status,run_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(assertion_id) DO NOTHING", agg_rows)
        conn.executemany(
            "INSERT INTO field_candidate_link (parent_assertion_id,child_assertion_id,operation,"
            "rule_id,note) VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING", agg_links)

    # ---------- 6) 汇总 ----------
    n_movie = conn.execute("SELECT COUNT(*) c FROM movie").fetchone()["c"]
    n_link = conn.execute("SELECT COUNT(*) c FROM product_movie").fetchone()["c"]
    merged_pairs = conn.execute(
        "SELECT COUNT(*) c FROM int_merge_decision WHERE action='auto_merge'").fetchone()["c"]
    summary = {
        "run_id": run_id, "rules_version": RULES_VERSION,
        "products_participating": len(prods),
        "blocking": {"buckets": len(buckets), "candidate_pairs": n_pairs,
                     "all_pairs": n_all,
                     "reduction_pct": round((1 - n_pairs / max(n_all, 1)) * 100, 2),
                     "max_bucket": sizes[0] if sizes else 0},
        "decisions": {"pairs_scored": stat["pairs"], "auto_merge": stat["auto_merge"],
                      "manual_review": stat["manual_review"], "no_merge": stat["no_merge"]},
        "transitive_conflicts": stat["transitive_conflicts"],
        "clusters": stat["clusters"], "singletons": stat["singletons"],
        "movies_created": n_movie, "product_movie_links": n_link,
        "before_after": {"products": len(prods), "movies": n_movie,
                         "compression_ratio": round(len(prods) / max(n_movie, 1), 3)},
        "thresholds": {"auto_merge": cfg["movie_auto_merge"],
                       "manual_review": cfg["movie_manual_review"]},
    }
    C.write_json(C.path("evidence", "s06_movie_match_summary.json"), summary)
    log.info(f"候选对 {stat['pairs']:,}：自动合并 {stat['auto_merge']:,} / "
             f"人工复核 {stat['manual_review']:,} / 不合并 {stat['no_merge']:,}")
    log.info(f"产品 {len(prods):,} -> 作品 {n_movie:,}（压缩比 "
             f"{len(prods)/max(n_movie,1):.2f}）；传递冲突 {stat['transitive_conflicts']}")

    C.end_run(conn, run_id, "success", f"movies={n_movie}")
    conn.close()
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
