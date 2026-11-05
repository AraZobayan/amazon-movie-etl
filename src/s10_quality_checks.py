"""第十部分：数据质量与验收（数量核对、可追溯性、实体匹配评价）。

产出 evidence/quality_report.json + 控制台表格，直接对应作业"十 数据质量与验收要求"。

三组检查：
  A. 数量与完整性：状态互斥、主键唯一、外键有效、分字段覆盖率
  B. 可追溯性：每个非空最终字段都能走到一条原始记录
  C. 实体匹配评价：电影/人物各 100 对人工标注样本，算 Precision / Recall / F1

关于评价集的诚实说明（很重要，答辩必问）：
    本项目的"人工标注"真值来自夹具生成规则（哪些 ASIN 属于同一作品是生成时决定的），
    所以 P/R/F1 衡量的是"规则与夹具难度设定是否匹配"，**不能外推**为真实 Amazon
    数据上的准确率。作业原文也要求"不能直接宣称代表全量准确率"。
    真要在真实数据上评价，需要人工逐对核对（模板已给出：meta_eval_pair.label 留空即可）。

运行：python -m src.s10_quality_checks
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import os
import random
import sys
from collections import defaultdict

from src import common as C


# --------------------------------------------------------------------------- #
# 指标
# --------------------------------------------------------------------------- #


def prf(tp: int, fp: int, fn: int) -> tuple[float | None, float | None, float | None]:
    p = tp / (tp + fp) if (tp + fp) else None
    r = tp / (tp + fn) if (tp + fn) else None
    f = (2 * p * r / (p + r)) if (p and r) else None
    return (round(p, 4) if p is not None else None,
            round(r, 4) if r is not None else None,
            round(f, 4) if f is not None else None)


def load_manifest() -> dict[str, dict]:
    fp = C.path("fixtures", "fixture_manifest.csv")
    if not os.path.exists(fp):
        return {}
    out = {}
    with open(fp, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            out[row["asin"]] = row
    return out


def _balance(pos: list[dict], neg_levels: list[list[dict]], n_target: int,
             rng: random.Random) -> list[dict]:
    """把「正例 + 分难度负例」凑成正好 n_target 对。

    为什么要凑满而不是「有多少用多少」：
        作业要求「在 100 对标注样本上报告 P/R/F1」。
        如果只取到 32 对，指标虽然算得出来，但样本量不足以说明问题，
        而且不同难度的负例比例会随机漂移，两次运行的指标不可比。
        所以这里固定 50 正 + 50 负，负例按「难 -> 易」优先取，
        不够时才补易例；补了什么、补了多少，都记在 difficulty 里，
        报告里可以按难度分层看指标。
    """
    half = n_target // 2
    chosen = pos[:half]
    need = n_target - len(chosen)
    for level in neg_levels:
        if need <= 0:
            break
        take = min(need, len(level))
        chosen += level[:take]
        need -= take
    # 负例不够就回头补正例，保证总数达标
    if need > 0:
        chosen += pos[half:half + need]
    rng.shuffle(chosen)
    for p in chosen:
        p["sample_group"] = ("dev" if int(C.sha256_text(p["id_a"] + p["id_b"])[:2], 16) % 10 < 4
                             else "eval")
    return chosen


def load_human_pairs(conn, etype: str) -> tuple[list[dict], str]:
    """读取独立评价集，返回 (评价对, 真值来源说明)。

    文件由 `scripts/build_eval_set.py` 生成，两列真值：

        prefill_label   IMDb 交叉验证得到的预标注（tconst / nconst 是否相同）
        label           人工逐对复核后的最终标注

    三级真值质量，按可信度从高到低：

        1. human             `label` 列填了 0/1 —— 人工看过证据后下的判断
        2. imdb_independent  `label` 空、`prefill_label` 有值 —— 用 IMDb 官方 ID
                             交叉验证。它不是人工标注，但**独立于本项目规则**
                             （IMDb 的 tconst 不是我们生成的，也不参与合并打分），
                             所以能避免"用自己答案考自己规则"的循环论证
        3. fixture_rules     两份 CSV 都没有 —— 退回夹具生成规则，**循环论证**，
                             指标不可外推，必须在报告里标明

    混合使用时如实拆分：人工覆盖到的对按 human 算，其余按 imdb_independent 算，
    并在 note 里写明各多少对。绝不把预标注说成人工标注。

    全部记为 sample_group='eval'：
        阈值和规则是在夹具样本上定下来的（那批是 dev），
        评价集是在规则冻结之后才生成的，天然与开发样本分离，
        所以它整体就是评价集，不能再拆一部分回去调参。
    """
    import csv as _csv
    fp = C.path("evidence", "eval", f"eval_{etype}s.csv")
    if not os.path.exists(fp):
        return [], ""
    human_rows, prefill_rows = [], []
    with open(fp, encoding="utf-8-sig") as f:
        for r in _csv.DictReader(f):
            v = str(r.get("label", "")).strip()
            if v in ("0", "1"):
                human_rows.append(r)
                continue
            p = str(r.get("prefill_label", "")).strip()
            if p in ("0", "1"):
                prefill_rows.append(r)
    if not human_rows and not prefill_rows:
        return [], ""

    def _build(rows: list[dict], from_human: bool) -> list[dict]:
        out = []
        for r in rows:
            a, b = r["id_a"], r["id_b"]
            if etype == "movie":
                q = conn.execute(
                    "SELECT (SELECT GROUP_CONCAT(movie_id) FROM product_movie WHERE product_id=?) a,"
                    "       (SELECT GROUP_CONCAT(movie_id) FROM product_movie WHERE product_id=?) b",
                    (a, b)).fetchone()
            else:
                q = conn.execute(
                    "SELECT (SELECT person_id FROM person_name WHERE name_norm=? LIMIT 1) a,"
                    "       (SELECT person_id FROM person_name WHERE name_norm=? LIMIT 1) b",
                    (C.norm_person_name(a), C.norm_person_name(b))).fetchone()
            # 两侧都必须真的解析出实体才算「合并」。两边都是 NULL 时不能判为合并。
            pred = int(q is not None and q["a"] is not None and q["a"] == q["b"])
            out.append({"id_a": a, "id_b": b,
                        "label": int(r["label"] if from_human else r["prefill_label"]),
                        "predicted": pred, "sample_group": "eval",
                        "difficulty": r.get("stratum") or "unspecified",
                        "truth": "human" if from_human else "imdb_independent"})
        return out

    pairs = _build(human_rows, True) + _build(prefill_rows, False)
    n_h, n_p = len(human_rows), len(prefill_rows)
    if n_p == 0:
        src = f"{os.path.basename(fp)}；{n_h} 对人工标注（IMDb 预填 + 人工复核）"
    elif n_h == 0:
        src = (f"{os.path.basename(fp)}；{n_p} 对 IMDb 独立预标注，**尚未人工复核**。"
               f"真值独立于本项目规则（tconst/nconst 交叉验证），但不是人工标注")
    else:
        src = (f"{os.path.basename(fp)}；{n_h} 对人工标注 + {n_p} 对 IMDb 独立预标注（未复核）")
    return pairs, src


def build_movie_pairs(conn, manifest: dict, n_target: int, seed: int) -> list[dict]:
    """构造电影匹配的评价对。

    正例：同一作品的不同商品版本（不同 ASIN）—— 应该被合并
    负例：不同作品 —— 不应该被合并，按难度分三层
        难  标题高度相似（重拍片/续集/同名不同年）
        中  标题有一定相似度
        易  随机不同作品
    """
    rng = random.Random(seed)
    prod_movie = {}
    for r in conn.execute(
            "SELECT product_id, movie_id FROM product_movie WHERE relation_type <> 'box_set_member'"):
        prod_movie[r["product_id"]] = r["movie_id"]

    truth = {}
    for asin, row in manifest.items():
        if row["page_kind"] != "product":
            continue
        members = [m for m in (row["truth_members"] or "").split("|") if m]
        if len(members) == 1 and asin in prod_movie:
            truth[asin] = members[0]
    by_work: dict[str, list[str]] = defaultdict(list)
    for asin, w in truth.items():
        by_work[w].append(asin)

    # 正例池：同一作品内两两组合（数量可能很大，先全部生成再抽样）
    pos: list[dict] = []
    for w, asins in by_work.items():
        asins = sorted(asins)
        for i in range(len(asins)):
            for j in range(i + 1, len(asins)):
                a, b = asins[i], asins[j]
                pa, pb = prod_movie.get(a), prod_movie.get(b)
                pos.append({"id_a": a, "id_b": b, "label": 1,
                            # 注意不能直接写 pa == pb：两个 None 也相等，
                            # 会把"两边都没匹配上"错算成"合并成功"。
                            "predicted": int(pa is not None and pa == pb),
                            "difficulty": "same_work_different_edition"})

    titles = {}
    for asin in truth:
        r = conn.execute("SELECT raw_value FROM field_assertion WHERE entity_type='product'"
                         " AND entity_id=? AND field_name='title' LIMIT 1", (asin,)).fetchone()
        titles[asin] = r["raw_value"] if r else ""

    hard, mid, easy = [], [], []
    asins = sorted(truth)
    for i in range(len(asins)):
        for j in range(i + 1, len(asins)):
            a, b = asins[i], asins[j]
            if truth[a] == truth[b]:
                continue
            sim = C.title_similarity(titles.get(a, ""), titles.get(b, ""))
            pa, pb = prod_movie.get(a), prod_movie.get(b)
            rec = {"id_a": a, "id_b": b, "label": 0,
                   "predicted": int(pa is not None and pa == pb)}
            if sim >= 0.55:
                hard.append({**rec, "difficulty": f"similar_title({sim:.2f})"})
            elif sim >= 0.30:
                mid.append({**rec, "difficulty": f"medium_title({sim:.2f})"})
            else:
                easy.append({**rec, "difficulty": f"random_different_work({sim:.2f})"})

    for lst in (pos, hard, mid, easy):
        rng.shuffle(lst)
    return _balance(pos, [hard, mid, easy], n_target, rng)


def build_person_pairs(conn, n_target: int, seed: int) -> list[dict]:
    """构造人物匹配的评价对。

    正例：同一个人出现在不同商品页上的不同写法（拼写/缩写/别名/目录式）
    负例：不同的人，按难度分三层
        难  同姓（父子、兄妹、同姓巧合）—— 只看姓氏必错
        中  名字相近（共享首名，或字符串相似度高）
        易  随机两人
    """
    rng = random.Random(seed)
    fp = C.path("fixtures", "person_truth.json")
    if not os.path.exists(fp):
        return []
    with open(fp, encoding="utf-8") as f:
        truth_map = json.load(f)["credit_to_person"]

    name_to_person: dict[str, str] = {}
    for r in conn.execute("SELECT person_id, name_norm FROM person_name"):
        name_to_person[r["name_norm"]] = r["person_id"]

    raws = {r["raw_value"] for r in conn.execute(
        "SELECT DISTINCT raw_value FROM field_assertion"
        " WHERE entity_type='product' AND field_name IN ('director','cast')")}
    truth = {raw: truth_map.get(raw, raw) for raw in raws}

    def pid_of(name: str) -> str | None:
        return name_to_person.get(C.norm_person_name(name))

    # 正例池：同一真实人物的两种写法
    by_person: dict[str, set[str]] = defaultdict(set)
    for raw, canon in truth.items():
        by_person[canon].add(raw)
    pos: list[dict] = []
    for person, variants in by_person.items():
        vs = sorted(variants)
        for i in range(len(vs)):
            for j in range(i + 1, len(vs)):
                a, b = vs[i], vs[j]
                pa, pb = pid_of(a), pid_of(b)
                pos.append({"id_a": a, "id_b": b, "label": 1,
                            "predicted": int(pa is not None and pa == pb),
                            "difficulty": "same_person_alias"})

    # 负例池：不同真实人物
    names = sorted(truth)
    hard, mid, easy = [], [], []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            if truth[a] == truth[b]:
                continue
            na, nb = C.norm_person_name(a), C.norm_person_name(b)
            if not na or not nb:
                continue
            pa, pb = pid_of(a), pid_of(b)
            rec = {"id_a": a, "id_b": b, "label": 0,
                   "predicted": int(pa is not None and pb is not None and pa == pb)}
            ta, tb = na.split(), nb.split()
            if ta[-1] == tb[-1]:
                hard.append({**rec, "difficulty": f"same_surname({ta[-1]})"})
            elif ta[0] == tb[0]:
                mid.append({**rec, "difficulty": f"same_first_name({ta[0]})"})
            elif difflib.SequenceMatcher(None, na, nb).ratio() >= 0.6:
                mid.append({**rec, "difficulty": "similar_string"})
            else:
                easy.append({**rec, "difficulty": "random_different_person"})

    for lst in (pos, hard, mid, easy):
        rng.shuffle(lst)
    return _balance(pos, [hard, mid, easy], n_target, rng)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--pairs", type=int, default=None)
    args = ap.parse_args(argv)

    conn = C.connect(args.db)
    log = C.Logger("s10_quality_checks")
    run_id = C.start_run(conn, "s10_quality_checks", {})
    log.run_id = run_id
    n_target = args.pairs or int(C.CFG["evaluation"]["pairs_per_type"])

    report: dict = {"run_id": run_id}

    # ================= A. 数量与完整性 =================
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]

    counts = {
        "raw_reviews": q("SELECT COUNT(*) FROM raw_review"),
        "valid_reviews": q("SELECT COUNT(*) FROM raw_review WHERE is_valid=1"),
        "quarantined_reviews": q("SELECT COUNT(*) FROM raw_review WHERE is_valid=0"),
        "logical_reviews": q("SELECT COUNT(*) FROM review"),
        "reviewers": q("SELECT COUNT(*) FROM reviewer"),
        "products": q("SELECT COUNT(*) FROM product"),
        "movies": q("SELECT COUNT(*) FROM movie"),
        "persons": q("SELECT COUNT(*) FROM person"),
        "product_movie_links": q("SELECT COUNT(*) FROM product_movie"),
        "movie_person_links": q("SELECT COUNT(*) FROM movie_person"),
        "field_assertions": q("SELECT COUNT(*) FROM field_assertion"),
        "field_selections": q("SELECT COUNT(*) FROM field_selection"),
    }
    total_products = counts["products"]
    status_sum = q("SELECT COUNT(*) FROM product_collection")
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str, severity: str = "error") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail, "severity": severity})
        if not ok:
            conn.execute(
                "INSERT INTO meta_quality_issue (issue_id,run_id,entity_type,entity_id,field_name,"
                "issue_type,severity,detail,created_at) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(issue_id) DO NOTHING",
                (C.det_id("QI", "check", name), run_id, "system", None, None,
                 name, severity, detail, C.now_iso()))

    check("状态互斥性：状态数之和 = 产品总数",
          status_sum == total_products,
          f"{status_sum} vs {total_products}")
    check("读取记录数 = 有效 + 隔离",
          counts["raw_reviews"] == counts["valid_reviews"] + counts["quarantined_reviews"],
          f"{counts['raw_reviews']} vs {counts['valid_reviews']}+{counts['quarantined_reviews']}")

    fk_bad = list(conn.execute("PRAGMA foreign_key_check"))
    check("外键有效性", len(fk_bad) == 0, f"违规 {len(fk_bad)} 条")

    dup_movie = q("SELECT COUNT(*) FROM (SELECT movie_id FROM movie GROUP BY movie_id HAVING COUNT(*)>1)")
    check("主键唯一性（movie）", dup_movie == 0, f"重复 {dup_movie} 个")

    # 每个非空最终字段都必须能追溯到原始来源
    untraceable = q("""
        SELECT COUNT(*) FROM field_selection fs
        LEFT JOIN field_assertion fa ON fa.assertion_id = fs.selected_assertion_id
        LEFT JOIN raw_source_record sr ON sr.record_id = fa.source_record_id
        WHERE fs.selected_assertion_id IS NOT NULL AND sr.record_id IS NULL""")
    check("非空最终字段可追溯", untraceable == 0, f"无法追溯 {untraceable} 条")

    # 投影一致性：movie.release_year 必须等于选中断言的年份
    mismatch = q("""
        SELECT COUNT(*) FROM movie m
        JOIN field_selection fs ON fs.entity_type='movie' AND fs.entity_id=m.movie_id
             AND fs.field_name='release_date'
        LEFT JOIN field_assertion fa ON fa.assertion_id=fs.selected_assertion_id
        WHERE COALESCE(CAST(substr(fa.normalized_value,1,4) AS INTEGER),-1)
              <> COALESCE(m.release_year,-1)""")
    check("movie.release_year 与血缘选择一致", mismatch == 0, f"不一致 {mismatch} 条")

    # 覆盖率（以作品为分母）
    #
    # 这里要区分两种"选出了值"：
    #   单值字段（标题/日期/外部 ID）：selected_assertion_id 指向唯一胜出者，非空即覆盖。
    #   多值字段（genres）：取并集，胜出者不止一个，所以 selected_assertion_id 恒为空，
    #     成员记在 candidate_ids_json 里。判据是"有成员"，不是"有单个选中值"。
    # 早期版本靠 field_name=='genres' 硬编码来区分，等于用字段名当类型标记 ——
    # 现在用 selection_kind 这一列显式判断，新增多值字段时不用再改这里。
    cov = {}
    for field in ("canonical_title", "release_date", "genres", "external_id"):
        got = q("""
            SELECT COUNT(*) FROM field_selection
             WHERE entity_type='movie' AND field_name=?
               AND ( (COALESCE(selection_kind,'single')='single'
                      AND selected_assertion_id IS NOT NULL)
                  OR (selection_kind='multi'
                      AND COALESCE(candidate_ids_json,'') NOT IN ('','[]')) )""", field)
        cov[field] = {"covered": got, "total": counts["movies"],
                      "rate": round(got / max(counts["movies"], 1), 4)}

    # 分类分布
    cls = {r["category"]: r["n"] for r in conn.execute(
        "SELECT category, COUNT(*) n FROM stg_classification GROUP BY category")}
    coll = {f"{r['status']}/{r['source_type']}": r["n"] for r in conn.execute(
        "SELECT status, source_type, COUNT(*) n FROM product_collection GROUP BY status, source_type")}

    report["A_counts"] = counts
    report["A_checks"] = checks
    report["A_coverage"] = cov
    report["A_classification"] = cls
    report["A_collection"] = coll

    # ---------- A2. 合并前后实体数（作业要求"合并前后作品和人物数量"） ----------
    #
    # "合并前"的定义必须写清楚，否则这个数字没有意义：
    #   作品：合并前 = 被判为电影的商品数（把每个商品当成一部独立作品）；
    #         合并后 = movie 表行数。差额就是被合并掉的重复版本数。
    #   人物：合并前 = 出现过的不同署名写法数（person_name 行数）；
    #         合并后 = person 表行数。差额是被识别为同一人的不同写法。
    # 为什么不用"评论数"或"断言数"当合并前的分母：那两个数跟实体数不是一个量纲，
    # 相减得不到"合并掉多少"。
    before_products = q("SELECT COUNT(*) FROM stg_classification WHERE category='movie'")
    before_names = q("SELECT COUNT(*) FROM person_name")
    report["A2_merge_effect"] = {
        "movies": {
            "before_each_product_as_work": before_products,
            "after_entity_count": counts["movies"],
            "merged_away": before_products - counts["movies"],
            "note": "合并前 = 分类为 movie 的商品数；合并后 = movie 实体数",
        },
        "persons": {
            "before_distinct_signatures": before_names,
            "after_entity_count": counts["persons"],
            "merged_away": before_names - counts["persons"],
            "note": "合并前 = 不同署名写法数（person_name）；合并后 = person 实体数",
        },
    }

    # ---------- A3. 识别覆盖率（分母是全部产品，不是已识别作品） ----------
    # 作业要求"电影信息以已识别作品为分母，另报告全部产品的识别覆盖率"。
    # 这两个分母必须分开报：前者回答"认出来的作品里字段填得多全"，
    # 后者回答"25 万个商品里有多少被认成了电影"。
    total_products = counts["products"]
    movie_products = before_products
    report["A3_identification_coverage"] = {
        "total_products": total_products,
        "products_classified_movie": movie_products,
        "identification_rate": round(movie_products / max(total_products, 1), 4),
        "products_with_any_metadata": q(
            "SELECT COUNT(DISTINCT entity_id) FROM field_assertion WHERE entity_type='product'"),
        "products_with_page_or_snapshot": q(
            "SELECT COUNT(*) FROM product_collection WHERE snapshot_path IS NOT NULL"),
        "note": "识别覆盖率 = 判为电影的商品数 / 全部商品数。"
                "分母是 25 万级，所以它天然远小于以已识别作品为分母的字段覆盖率。",
    }

    # ---------- A4. 字段值的"已核实 / 推断 / 缺失"三分 ----------
    #
    # 作业要求"缺失、推断和已核实结果分别统计"。三者的判据必须能落到规则上，
    # 否则就只是事后贴标签。本项目的规则命名本身携带了这个信息：
    #   R-EXT-*  直接从某个来源的原始证据里抽出来的值 —— **已核实**
    #            （页面字段、Wikidata 的 P577/QID、IMDb 的 tconst）
    #   R-AGG-*  由商品级断言按规则聚合到作品级 —— **推断**
    #            （例：691 个商品版本各自写着同一个标题，取众数当作作品标题；
    #              这是"推导出来的"，不是在任何一页上直接读到的）
    #   没有选出值 —— **缺失**
    # 注意：这里不把"只有 DVD 发售日、没有影院上映日"的 8 部作品算成推断，
    # 它们被明确记为缺失 —— 用发售日推上映日是本项目明确拒绝的做法。
    field_split: dict[str, dict] = {}
    for field in ("canonical_title", "release_date", "genres", "external_id"):
        verified = inferred = missing = 0
        rows = conn.execute("""
            SELECT fs.selection_kind, fs.selected_assertion_id, fs.candidate_ids_json,
                   fa.extractor_rule_id
              FROM field_selection fs
              LEFT JOIN field_assertion fa ON fa.assertion_id = fs.selected_assertion_id
             WHERE fs.entity_type='movie' AND fs.field_name=?""", (field,)).fetchall()
        for r in rows:
            rule = r["extractor_rule_id"] or ""
            kind = r["selection_kind"] or "single"
            resolved = (r["selected_assertion_id"] is not None) if kind == "single" \
                else (r["candidate_ids_json"] or "") not in ("", "[]")
            if not resolved:
                missing += 1
            elif rule.startswith("R-AGG-"):
                inferred += 1
            else:
                verified += 1
        missing += counts["movies"] - len(rows)      # 连决策行都没有 = 也属于缺失
        field_split[field] = {
            "verified": verified, "inferred": inferred, "missing": missing,
            "total": counts["movies"],
            "verified_rate": round(verified / max(counts["movies"], 1), 4),
        }
    report["A4_value_provenance"] = {
        "by_field": field_split,
        "definition": {
            "verified": "最终值直接来自某条原始断言（规则前缀 R-EXT-）",
            "inferred": "最终值由商品级断言按规则聚合而来（规则前缀 R-AGG-）",
            "missing": "没有选出值（含「有候选但全被规则拒绝」和「从未产生候选」两种情况）",
        },
    }

    # ================= B. 可追溯性抽样 =================
    samples = []
    for r in conn.execute("""
            SELECT fs.entity_type, fs.entity_id, fs.field_name, fa.normalized_value,
                   fa.raw_value, fa.extractor_rule_id, sr.source_type, sr.url, sr.snapshot_path,
                   sr.content_hash, fs.decision_reason
            FROM field_selection fs
            JOIN field_assertion fa ON fa.assertion_id = fs.selected_assertion_id
            JOIN raw_source_record sr ON sr.record_id = fa.source_record_id
            WHERE fs.entity_type='movie' AND fs.field_name='release_date'
            LIMIT 3"""):
        samples.append(dict(r))
    report["B_traceability_samples"] = samples

    # ================= C. 实体匹配评价 =================
    # 优先用「人工标注的独立评价集」（evidence/eval/eval_*.csv 里 label 已填）。
    # 只有拿不到人工标注时才退回夹具真值 —— 而且必须在报告里写清楚退回了，
    # 因为夹具真值 = 用我们自己的答案考我们自己的规则，是循环论证。
    manifest = load_manifest()
    results = {}
    for etype, builder in (("movie", build_movie_pairs), ("person", build_person_pairs)):
        pairs, truth_source = load_human_pairs(conn, etype)
        if not pairs:
            if etype == "movie":
                pairs = builder(conn, manifest, n_target, 42)
            else:
                pairs = builder(conn, n_target, 42)
            truth_source = "fixture_rules"
            note = "真值来自夹具生成规则（循环论证，指标不可外推）"
        else:
            note = f"真值来自人工标注的独立评价集（{truth_source}）"
        if not pairs:
            results[etype] = {"note": "样本不足，无法评价"}
            continue
        conn.execute("DELETE FROM meta_eval_pair WHERE entity_type=?", (etype,))
        conn.executemany(
            "INSERT OR REPLACE INTO meta_eval_pair (pair_id,entity_type,id_a,id_b,predicted,label,"
            "sample_group,difficulty,note) VALUES (?,?,?,?,?,?,?,?,?)",
            [(C.det_id("EP", etype, p["id_a"], p["id_b"]), etype, p["id_a"], p["id_b"],
              p["predicted"], p["label"], p["sample_group"], p["difficulty"], note)
             for p in pairs])

        per_group = {}
        for grp in ("dev", "eval"):
            g = [p for p in pairs if p["sample_group"] == grp]
            tp = sum(1 for p in g if p["label"] == 1 and p["predicted"] == 1)
            fp = sum(1 for p in g if p["label"] == 0 and p["predicted"] == 1)
            fn = sum(1 for p in g if p["label"] == 1 and p["predicted"] == 0)
            tn = sum(1 for p in g if p["label"] == 0 and p["predicted"] == 0)
            prec, rec, f1 = prf(tp, fp, fn)
            per_group[grp] = {"n": len(g), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
                              "precision": prec, "recall": rec, "f1": f1}
            conn.execute(
                "INSERT OR REPLACE INTO meta_eval_metric (metric_id,run_id,entity_type,sample_group,"
                "tp,fp,fn,tn,precision,recall,f1,note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (C.det_id("EM", etype, grp), run_id, etype, grp, tp, fp, fn, tn, prec, rec, f1,
                 note))

        # 按难度分层看指标：整体 F1 会掩盖「难例全错、易例全对」这种情况，
        # 分层报告才能说明匹配器到底在哪一档上不行。
        strata: dict[str, dict] = {}
        for p in pairs:
            key = p["difficulty"].split("(")[0]
            b = strata.setdefault(key, {"n": 0, "tp": 0, "fp": 0, "fn": 0, "tn": 0})
            b["n"] += 1
            if p["label"] == 1 and p["predicted"] == 1:
                b["tp"] += 1
            elif p["label"] == 0 and p["predicted"] == 1:
                b["fp"] += 1
            elif p["label"] == 1 and p["predicted"] == 0:
                b["fn"] += 1
            else:
                b["tn"] += 1
        for key, b in strata.items():
            b["precision"], b["recall"], b["f1"] = prf(b["tp"], b["fp"], b["fn"])
            # 纯负例层（没有正例）里 precision 的分母只有误合并数，
            # 一旦有 1 个误合并就显示 P=0.0，看起来像"全错"，非常误导。
            # 这种层真正该看的是「误合并率」= 误合并 / 该层负例总数。
            if b["tp"] + b["fn"] == 0 and (b["fp"] + b["tn"]) > 0:
                b["false_merge_rate"] = round(b["fp"] / (b["fp"] + b["tn"]), 4)
                b["metric_note"] = "该层无正例，看 false_merge_rate（误合并率）而非 precision"

        # 误合并 / 漏合并实例（作业要求列出）
        errors = [p for p in pairs if p["label"] != p["predicted"]]
        results[etype] = {"pairs": len(pairs), "per_group": per_group,
                          "by_difficulty": strata,
                          "error_examples": [{"a": p["id_a"], "b": p["id_b"],
                                              "label": p["label"], "predicted": p["predicted"],
                                              "type": "误合并" if p["predicted"] else "漏合并",
                                              "difficulty": p["difficulty"]}
                                             for p in errors[:8]]}
    report["C_evaluation"] = results

    # ================= 输出 =================
    ok_all = all(c["ok"] for c in checks)
    report["summary"] = {"all_checks_passed": ok_all,
                         "checks_total": len(checks),
                         "checks_failed": sum(1 for c in checks if not c["ok"])}
    C.write_json(C.path("evidence", "quality_report.json"), report)

    log.info("=" * 78)
    for c in checks:
        log.info(f"  [{'通过' if c['ok'] else '失败'}] {c['check']}  ({c['detail']})")
    log.info("=" * 78)
    log.info(f"覆盖率: " + "  ".join(f"{k}={v['rate']:.1%}" for k, v in cov.items()))
    for etype, res in results.items():
        if "per_group" in res:
            ev = res["per_group"]["eval"]
            log.info(f"{etype} 评价集: n={ev['n']}  P={ev['precision']}  R={ev['recall']}  "
                     f"F1={ev['f1']}  (误合并 {ev['fp']} / 漏合并 {ev['fn']})")
            for k, b in sorted(res.get("by_difficulty", {}).items()):
                if "false_merge_rate" in b:
                    log.info(f"    分层 {k:<26} n={b['n']:<4} 误合并率={b['false_merge_rate']}")
                else:
                    log.info(f"    分层 {k:<26} n={b['n']:<4} P={b['precision']} "
                             f"R={b['recall']} F1={b['f1']}")

    C.end_run(conn, run_id, "success" if ok_all else "failed",
              json.dumps(report["summary"], ensure_ascii=False))
    conn.close()
    log.close()
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
