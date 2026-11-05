"""任务 5：人物姓名归一与消歧。

难点在于两个方向都要防：
    漏合并（同一人不同写法没合起来）  -> 召回率下降
    误合并（同名不同人合起来）        -> 精确率下降，且会把两部电影的演员串成一人

本实现的规则（全部可解释、可复现）：

  R-PER-EXACT              规范化姓名完全相同              -> 自动合并
  R-PER-SUFFIX-DIFF        完全相同但后缀不同（jr/sr/iii） -> 人工复核
                           （Dean Martin / Dean Martin, Jr. 是父子两人）
  R-PER-INITIALS           首名被缩写，且两写法从不同页出现 -> 自动合并
  R-PER-INITIALS-AMBIGUOUS 缩写在本块内能展开成多个全名     -> 人工复核
                           （"J. Cusack" 同时是 Joan 和 John，不能当桥）
  R-PER-INITIALS-COOCCUR   首名被缩写，但同一页里两者都出现 -> 人工复核
                           （一页演职员表不会把同一个人写两遍）
  R-PER-INITIALS-MIDDLE    首名和姓都相同，只差中间名        -> 人工复核
                           （Michael Jordan / Michael B. Jordan 就是两个人）
  R-PER-SURNAME-COOCCUR    同姓、名不同，且同页出现          -> 人工复核（父子同片）
  R-PER-SURNAME-ALIAS      同姓、名不同，不同页              -> 人工复核（可能是改名/艺名）
  R-PER-NO-MERGE           其余                            -> 不合并

★ 三个关键的实现修正（调试记录见手册第 12 章）：

  1) "共现"必须在**同一商品页的署名列表**里判断，不能只看"是否出现在同一部电影"。
     因为同一部电影的不同版本（DVD / 蓝光 / 再版）天然共现，
     用电影级共现会把所有别名都判成"同片出现 -> 可能是两个人"，导致大量漏合并
     （实测召回率只有 0.15）。改到商品页级后，别名能正常合并。

  2) 裸姓氏（"Coppola"）不是缩写；"跳过单字母"只能跳中间名，不能跳首名。
     否则 Francis Coppola 会和 Sofia Coppola（S. Coppola）合并，
     Luke Wilson 会和 Owen Wilson（O. Wilson）合并。

  3) 缩写歧义必须**在块内**判断：如果一个缩写能展开成本块里两个以上全名，
     它就是歧义的，只能交人工。否则 "J. Cusack" 会作为"桥"把
     Joan Cusack 和 John Cusack 连成同一个实体（DSU 的传递性）。

★ 另一条作业原文强调的点：
    "缩写相同不能直接合并"。本实现把它拆成两层：
      is_initials_of()  只判断**结构上是否兼容**（纯字符串算法）
      decide()          再结合**共现证据**判断**是否同一人**（决策规则）
    两者分离，才能既解释得清、又改得动。

运行：python -m src.s07_resolve_persons
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections import defaultdict

from src import common as C
from src.s06_match_movies import DSU

RULES_VERSION = "person-rules-v1.0.0"
SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def name_tokens(norm: str) -> list[str]:
    return [t for t in norm.split() if t and t not in SUFFIXES]


def surname(norm: str) -> str:
    t = name_tokens(norm)
    return t[-1] if t else ""


def suffix_of(norm: str) -> str:
    """取姓名后缀（jr / sr / ii / iii / iv）。

    为什么单独取出来：后缀是**区分父子的唯一信号**。
    "Dean Martin" 和 "Dean Martin, Jr." 是同姓同名的父子两人，
    去掉后缀之后两个名字在字符串层面完全一样。
    所以后缀不能当噪声剥掉，必须参与决策（见 decide() 的 R-PER-SUFFIX-DIFF）。
    """
    t = norm.split()
    return t[-1] if t and t[-1] in SUFFIXES else ""


def _tok_match(tok: str, lt: str) -> bool:
    """短名的一个词能否对应长名的一个词。"""
    return tok == lt or (len(tok) == 1 and lt.startswith(tok))


def is_initials_of(short: str, long: str) -> bool:
    """判断 short 是否是 long 的缩写写法（**只做结构判断，不代表应该合并**）。

    "h hathaway"   vs "henry hathaway"              -> True
    "s jackson"    vs "samuel l jackson"            -> True（省略了中间名首字母）
    "jrr tolkien"  vs "john ronald reuel tolkien"   -> True（一个词压缩了三个词）
    "michael jordan" vs "michael b jordan"          -> True（结构上兼容）

    注意最后一条：结构兼容不等于"是同一个人"。
    Michael Jordan（篮球运动员）和 Michael B. Jordan（演员）结构上完全兼容，
    但确实是两个人。**这个歧义靠姓名本身无法解决**，必须交给决策规则
    （见 decide() 里的 "中间名差异 -> 人工复核"）。

    实现用记忆化递归（DP），因为要同时支持三种对齐方式：
      1. 一对一（henry -> h）
      2. 一对多（jrr -> john + ronald + reuel）
      3. 跳过（短名省略了长名里某个单字母中间名）
    用简单的 for 循环 + 指针做会漏掉组合情况，DP 才是正确做法。
    """
    s, l = name_tokens(short), name_tokens(long)
    if not s or not l or s == l:
        return False

    # ★ 裸姓氏（"coppola" / "clooney" / "mara"）不是任何全名的缩写形式。
    #   亚马逊演职员表里经常只写姓氏（"Coppola"），它是**信息缺失**，不是缩写。
    #   如果不挡住这种情况，DP 的"跳过单字母"分支会让 "coppola" 吃掉 "f coppola"
    #   里的 "f"，从而把 Francis Coppola 和 Sofia Coppola（S. Coppola）合并成一个实体。
    #   区分"裸姓氏"和"纯缩写"的办法：单 token 且等于长名的姓氏 -> 裸姓氏。
    if len(s) == 1 and s[0] == l[-1]:
        return False

    from functools import lru_cache

    @lru_cache(maxsize=None)
    def go(i: int, j: int) -> bool:
        if i == len(s):
            return j == len(l)          # 短名的词用完，长名的词也必须用完
        tok = s[i]
        # 1) 一对一
        if j < len(l) and _tok_match(tok, l[j]) and go(i + 1, j + 1):
            return True
        # 2) 一对多：tok 是 l[j..j+k-1] 的首字母串
        if len(tok) > 1:
            for k in range(2, len(tok) + 1):
                if j + k > len(l):
                    break
                if all(tok[x] == l[j + x][0] for x in range(k)) and go(i + 1, j + k):
                    return True
        # 3) 跳过：长名这里是个**中间名**首字母，短名省略了它。
        #    必须要求 j > 0：只能跳过"中间"的单字母，不能跳过**首名**。
        #    否则 "luke wilson" vs "o wilson" 会被判成缩写关系
        #    （跳过首字母 o，让 luke 去对 wilson），
        #    把 Luke Wilson 和 Owen Wilson 这对兄弟合并成一个人。
        if j > 0 and j < len(l) and len(l[j]) == 1 and go(i, j + 1):
            return True
        return False

    return go(0, 0)


def abbrev_kind(short: str, long: str) -> str | None:
    """细分缩写类型，因为两种缩写的"可信度"完全不同。

    "first_abbrev"  短名的**首名被缩写**（h hathaway / s jackson）
                    -> 这是录入习惯差异，很可能是同一个人
    "middle_only"   首名和姓都完整，只是**中间名有无不同**（michael jordan / michael b jordan）
                    -> 这恰恰是"同名不同人"的典型特征（父子、同名演员），不能自动合并
    """
    if not is_initials_of(short, long):
        return None
    s, l = name_tokens(short), name_tokens(long)
    if not s or not l:
        return None
    if s[0] == l[0]:
        return "middle_only"
    return "first_abbrev"


def pair_score(na: str, nb: str) -> tuple[float, str]:
    """给出姓名层面的相似度与关系类型。分数只用于排序/筛除，判定看 decide()。"""
    if na == nb:
        return 1.0, "exact"
    ka = abbrev_kind(na, nb) or abbrev_kind(nb, na)
    if ka:
        return (0.9 if ka == "first_abbrev" else 0.8), f"initials_{ka}"
    if surname(na) and surname(na) == surname(nb):
        return 0.55, "same_surname"
    return 0.15, "different"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args(argv)

    conn = C.connect(args.db)
    log = C.Logger("s07_resolve_persons")
    run_id = C.start_run(conn, "s07_resolve_persons", {"rules": RULES_VERSION})
    log.run_id = run_id

    if args.reset:
        for t in ("movie_person", "person_name", "person"):
            conn.execute(f"DELETE FROM {t}")

    # ---------- 1) 收集署名 ----------
    credits = [dict(r) for r in conn.execute(
        "SELECT fa.entity_id AS product_id, fa.raw_value AS raw, fa.normalized_value AS norm,"
        "       fa.field_name AS role, fa.source_record_id, pm.movie_id"
        " FROM field_assertion fa"
        " JOIN product_movie pm ON pm.product_id = fa.entity_id"
        " WHERE fa.entity_type='product' AND fa.field_name IN ('director','cast')")]
    log.info(f"演职员署名 {len(credits):,} 条")

    names: dict[str, dict] = {}
    for c in credits:
        n = names.setdefault(c["norm"], {"norm": c["norm"], "raws": set(), "movies": set(),
                                         "products": set(), "roles": set(), "records": set()})
        n["raws"].add(c["raw"])
        n["movies"].add(c["movie_id"])
        n["products"].add(c["product_id"])
        n["roles"].add(c["role"])
        n["records"].add(c["source_record_id"])
    log.info(f"不同规范化姓名 {len(names):,} 个")

    # ---------- 2) 按姓氏分块，块内两两比较 ----------
    blocks: dict[str, list[str]] = defaultdict(list)
    for norm in names:
        blocks[surname(norm) or norm].append(norm)
    log.info(f"姓氏分块：{len(blocks):,} 块，最大块 {max((len(v) for v in blocks.values()), default=0)}")

    dsu = DSU()
    pairs, decisions = [], []
    stat = {"pairs": 0, "auto_merge": 0, "manual_review": 0, "no_merge": 0}

    for key, norms in blocks.items():
        if len(norms) > 40:      # 姓太常见（如 "lee"），退化处理，避免 O(n^2) 爆炸
            continue
        # ★ 预先算：每个缩写写法在**本块内**能展开成哪几个全名。
        #   如果能展开成两个以上（"j cusack" -> joan cusack / john cusack），
        #   这个缩写本身就是歧义的，不能用来做自动合并——
        #   否则 DSU 会以它为"桥"，把 Joan Cusack 和 John Cusack 连成一个实体。
        #   这个检查必须在块内做：歧义是"相对于当前语料"的，不是绝对属性。
        expansions: dict[str, set[str]] = {}
        for n in norms:
            expansions[n] = {m for m in norms if m != n and is_initials_of(n, m)}
        for a, b in itertools.combinations(sorted(norms), 2):
            score, rel = pair_score(a, b)
            if score < 0.5:
                continue
            # ★ 共现必须在"同一商品页的署名列表"里判断，不能只看"同一部电影"。
            # 因为同一部电影的不同版本（DVD/蓝光/再版）天然共现，
            # 用电影级共现会让所有别名都被判成"同片出现 -> 可能是两个人"，永远合不了。
            # 而一页署名里同时出现 "H. Hathaway" 和 "Henry Hathaway"，
            # 才是"这是两个人"的真正证据（一页演职员表不会把同一人写两遍）。
            shared_pages = names[a]["products"] & names[b]["products"]
            cooccur = bool(shared_pages)
            shared = sorted(names[a]["movies"] & names[b]["movies"])[:3]

            if rel == "exact":
                # 规范化后完全相同：直接合并。若真有两个同名者，需要别的证据来区分，
                # 姓名本身已经无法再提供信息了。
                # 唯一的例外是**后缀不同**（"dean martin jr" vs "dean martin"）：
                # 后缀是父子唯一的区分信号，必须交人工。
                action, rule = (("manual_review", "R-PER-SUFFIX-DIFF")
                                if suffix_of(a) != suffix_of(b)
                                else ("auto_merge", "R-PER-EXACT"))
            elif rel == "initials_first_abbrev":
                # 缩写歧义：这个缩写在本块内能展开成多个全名 -> 不能自动合并
                short = a if is_initials_of(a, b) else b
                ambiguous = len(expansions.get(short, ())) > 1
                if ambiguous:
                    action, rule = "manual_review", "R-PER-INITIALS-AMBIGUOUS"
                elif cooccur:
                    # 首名被缩写，但同一页里两者都出现 -> 一页不会把同一人写两遍
                    action, rule = "manual_review", "R-PER-INITIALS-COOCCUR"
                else:
                    action, rule = "auto_merge", "R-PER-INITIALS"
            elif rel == "initials_middle_only":
                # 只有中间名有无不同 -> 这正是"同名不同人"的典型形态，交人工
                action, rule = "manual_review", "R-PER-INITIALS-MIDDLE"
            elif rel == "same_surname":
                # 同姓、名不同：同页出现说明是两个人（如父子同片）；不同页则可能是改名/艺名
                action, rule = (("manual_review", "R-PER-SURNAME-COOCCUR") if cooccur
                                else ("manual_review", "R-PER-SURNAME-ALIAS"))
            else:
                action, rule = "no_merge", "R-PER-NO-MERGE"

            stat["pairs"] += 1
            stat[action] += 1
            feats = {"relation": rel, "same_page": cooccur,
                     "shared_pages": sorted(shared_pages)[:3],
                     "shared_movies": shared}
            pairs.append((C.det_id("PC", "person", a, b), "person", a, b, key, score,
                          json.dumps(feats, ensure_ascii=False), RULES_VERSION, run_id))
            decisions.append((C.det_id("D", "person", a, b, RULES_VERSION), "person", action,
                              a, b, key, score, RULES_VERSION, rule,
                              json.dumps(feats, ensure_ascii=False), run_id, C.now_iso()))
            if action == "auto_merge":
                dsu.union(a, b)

    # ---------- 3) 建 person 实体 ----------
    groups = dsu.groups()
    members_of: dict[str, list[str]] = {}
    for root, mem in groups.items():
        members_of[root] = sorted(mem)
    clustered = {m for mem in groups.values() for m in mem}
    singles = [n for n in names if n not in clustered]
    all_groups = [sorted(v) for v in groups.values()] + [[s] for s in singles]

    person_rows, pname_rows, mp_rows, p_assert, p_links = [], [], [], [], []
    for g in all_groups:
        # 规范名取"最完整"的写法：词数最多、字符最长
        canon = max(g, key=lambda n: (len(name_tokens(n)), len(n)))
        pid = C.det_id("P", canon)
        person_rows.append((pid, canon.title(), canon, "active", run_id))
        for n in g:
            for raw in sorted(names[n]["raws"]):
                pname_rows.append((pid, raw, n,
                                   "canonical" if n == canon else "alias",
                                   sorted(names[n]["records"])[0] if names[n]["records"] else None))
        # movie_person 关系 + 人物级断言（血缘：person.canonical_name 来自哪条署名）
        for c in credits:
            if c["norm"] in g:
                mp_rows.append((c["movie_id"], pid, "director" if c["role"] == "director" else "cast",
                                None, c["source_record_id"]))
                aid = C.det_id("A", "person", pid, "canonical_name", c["source_record_id"], c["norm"])
                p_assert.append((aid, "person", pid, "canonical_name", c["raw"], c["norm"], None,
                                 c["source_record_id"], "R-AGG-PERSON-CANONICAL", "aggregate-v1",
                                 0.8, "candidate", run_id, C.now_iso()))
                # 把"原始署名断言"和"人物规范名断言"连起来 -> 别名归并过程可重建
                p_links.append((aid, C.det_id("A", "product", c["product_id"],
                                              "director" if c["role"] == "director" else "cast",
                                              c["source_record_id"], c["norm"]),
                                "merge", "R-LINK-CREDIT-TO-PERSON", f"署名 {c['raw']}"))

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
            " ON CONFLICT(decision_id) DO UPDATE SET action=excluded.action, run_id=excluded.run_id",
            decisions)
        conn.executemany(
            "INSERT INTO person (person_id,canonical_name,canonical_name_norm,status,created_run_id)"
            " VALUES (?,?,?,?,?) ON CONFLICT(person_id) DO NOTHING", person_rows)
        conn.executemany(
            "INSERT INTO person_name (person_id,name,name_norm,name_type,source_record_id)"
            " VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING", pname_rows)
        conn.executemany(
            "INSERT INTO movie_person (movie_id,person_id,role,billing_order,source_record_id)"
            " VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING", mp_rows)
        conn.executemany(
            "INSERT INTO field_assertion (assertion_id,entity_type,entity_id,field_name,raw_value,"
            "normalized_value,value_num,source_record_id,extractor_rule_id,extractor_version,"
            "confidence,status,run_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(assertion_id) DO NOTHING", p_assert)
        conn.executemany(
            "INSERT INTO field_candidate_link (parent_assertion_id,child_assertion_id,operation,"
            "rule_id,note) VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING", p_links)

    n_person = conn.execute("SELECT COUNT(*) c FROM person").fetchone()["c"]
    n_mp = conn.execute("SELECT COUNT(*) c FROM movie_person").fetchone()["c"]
    merged = conn.execute(
        "SELECT COUNT(*) c FROM person WHERE person_id IN"
        " (SELECT person_id FROM person_name GROUP BY person_id HAVING COUNT(*)>1)").fetchone()["c"]

    summary = {
        "run_id": run_id, "rules_version": RULES_VERSION,
        "credits": len(credits), "distinct_normalized_names": len(names),
        "blocks": len(blocks),
        "decisions": {"pairs_scored": stat["pairs"], "auto_merge": stat["auto_merge"],
                      "manual_review": stat["manual_review"], "no_merge": stat["no_merge"]},
        "persons_created": n_person,
        "persons_with_alias": merged,
        "movie_person_links": n_mp,
        "compression": {"names": len(names), "persons": n_person,
                        "ratio": round(len(names) / max(n_person, 1), 3)},
    }
    C.write_json(C.path("evidence", "s07_person_summary.json"), summary)
    log.info(f"姓名 {len(names):,} -> 人物 {n_person:,}（含别名的人物 {merged:,}）；"
             f"关系 {n_mp:,}；自动合并 {stat['auto_merge']}，待复核 {stat['manual_review']}")

    C.end_run(conn, run_id, "success", f"persons={n_person}")
    conn.close()
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
