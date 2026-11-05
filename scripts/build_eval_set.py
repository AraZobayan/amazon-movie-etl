"""构建实体匹配的人工标注评价集（电影 + 人物各 ≥100 对）。

## 为什么需要这个脚本

作业第十部分原文要求：

> 电影与人物各建立至少 100 对**人工标注**的独立评价样本，分别包含应合并与不应合并
> 案例，并覆盖同名作品、翻拍、合集、名字缩写和同名人物等难例。说明抽样方式、
> 标注依据与分歧处理；**规则开发与评价样本分开**。

原来的 `s10_quality_checks` 用 `fixtures/fixture_manifest.csv` 当真值 —— 但那份
manifest 是夹具生成器自己写的，等于用「我们设定的答案」去考「我们写的规则」，
是循环论证。作业也要求"不能直接宣称代表全量准确率"。

这个脚本换一个**外部且独立**的真值来源：

    IMDb 官方数据集（datasets.imdbws.com）
        title.basics.tsv.gz   tconst / titleType / primaryTitle / startYear
        name.basics.tsv.gz    nconst / primaryName

判据：
    * 两个 Amazon 商品如果**都**能高置信度匹配到同一个 tconst，就标为「同一作品」；
    * 匹配到不同 tconst，就标为「不同作品」；
    * 人物同理，两个署名如果都唯一匹配到同一个 nconst，标为「同一人」。
    IMDb 的 ID 不是我们生成的，也不参与我们的合并规则，所以这是独立真值。

## 但"独立"不等于"人工"

脚本只做**预标注**：把 label 写进 `prefill_label` 列，同时留空的 `label` 列给人填。
工作流是：

    1. 跑本脚本 → 得到 `evidence/eval/eval_movies.csv` / `eval_persons.csv`
    2. 人工逐行看证据（标题/年份/ASIN/厂牌/演员交集/IMDb 条目），在 `label` 列填 1 或 0
    3. 跑 `--import` 把人工 label 写回 meta_eval_pair，并标记 label_source='human'
    4. s10 只认 label_source='human' 的对来算 P/R/F1

没填 label 的行不参与指标 —— 宁可样本少，也不能把预标注冒充人工标注。

## 抽样设计

    电影：正例 = 同 tconst 不同 ASIN（不同版本）
          负例 = 不同 tconst，按标题相似度分三层
                 难  sim ≥ 0.75（同名/翻拍/续集）
                 中  0.45 ≤ sim < 0.75
                 易  sim < 0.45
    人物：正例 = 同 nconst 不同写法（缩写/别名/目录式）
          负例 = 不同 nconst，按姓氏是否相同分三层
                 难  同姓（父子、兄妹、同姓巧合）
                 中  同首名
                 易  随机

每层按 50 正 + 50 负取，共 100 对；难例优先，不够才补易例，补了什么记在 stratum 里。

用法：
    python scripts/build_eval_set.py                      # 生成两份 CSV
    python scripts/build_eval_set.py --per-type 120       # 每类 120 对
    python scripts/build_eval_set.py --import             # 把人工 label 写回库
"""
from __future__ import annotations

import argparse
import csv
import gzip
import itertools
import os
import random
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

IMDB_DIR = os.path.join(ROOT, "data", "raw", "imdb")
OUT_DIR = os.path.join(ROOT, "evidence", "eval")

# 匹配 IMDb 时只认这些作品类型；tvSeries / videoGame 之类不参与电影合并评价
FILM_TYPES = {"movie", "tvMovie", "short"}

_EDITION_RE = re.compile(
    r"[\(\[]\s*(collector'?s?\s+edition|special\s+edition|deluxe\s+edition|"
    r"criterion\s+collection|anniversary\s+edition|remastered|unrated|"
    r"blu-?ray|dvd|4k|uhd|digital|widescreen|full\s*screen|"
    r"two-?disc|single-?disc|widescreen\s+edition)\s*[\)\]]", re.I)

_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")


def norm_title(s: str) -> str:
    """标题规范化：去重音、去版本词、还原目录式冠词、去标点。

    这一步和 s06 的规范化必须**分开实现**（而不是 import s06 的函数），
    否则评价集的构造会跟着合并规则的改动一起漂移，"规则与评价分离"就落空了。
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = _EDITION_RE.sub(" ", s)
    s = s.lower().replace("&", " and ")
    # "Inception, The" -> "the inception"
    m = re.match(r"^(.*?),\s*(the|a|an)\s*$", s)
    if m:
        s = f"{m.group(2)} {m.group(1)}"
    s = _PUNCT_RE.sub(" ", s)
    return " ".join(s.split())


def norm_person(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().replace(".", " ").replace(",", " ")
    s = _PUNCT_RE.sub(" ", s)
    return " ".join(s.split())


def title_similarity(a: str, b: str) -> float:
    """用 difflib 的字符级相似度，只用于**分层**，不参与合并判定。"""
    import difflib
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


# --------------------------------------------------------------------------- #
# IMDb 索引：流式扫描，只保留我们真正要查的键
# --------------------------------------------------------------------------- #

def build_title_index(wanted: set[str]) -> dict[str, list[tuple[str, int | None, str]]]:
    """扫描 title.basics，只保留 normalized title 在 wanted 里的行。

    为什么流式过滤而不是全量载入：
        title.basics 解压后约 1.1 GB / 1100 万行，全量建成 dict 要 3 GB 以上内存。
        我们只关心库里那几千个标题，边读边丢可以把内存压到几十 MB。
    """
    fp = os.path.join(IMDB_DIR, "title.basics.tsv.gz")
    if not os.path.exists(fp):
        raise SystemExit(f"缺少 {fp}，先下载 IMDb title.basics.tsv.gz")
    idx: dict[str, list[tuple[str, int | None, str]]] = defaultdict(list)
    n = 0
    with gzip.open(fp, "rt", encoding="utf-8", errors="replace", newline="") as f:
        header = f.readline().rstrip("\n").split("\t")
        col = {c: i for i, c in enumerate(header)}
        for line in f:
            n += 1
            parts = line.rstrip("\n").split("\t")
            if len(parts) < len(header):
                continue
            ttype = parts[col["titleType"]]
            if ttype not in FILM_TYPES:
                continue
            for key in (parts[col["primaryTitle"]], parts[col["originalTitle"]]):
                nt = norm_title(key)
                if nt and nt in wanted:
                    y = parts[col["startYear"]]
                    idx[nt].append((parts[col["tconst"]],
                                    int(y) if y.isdigit() else None, ttype))
                    break
    print(f"  title.basics 扫描 {n:,} 行，命中标题键 {len(idx):,} 个")
    return idx


def build_name_index(wanted: set[str]) -> dict[str, list[str]]:
    """扫描 name.basics，只保留 normalized name 在 wanted 里的 nconst。"""
    fp = os.path.join(IMDB_DIR, "name.basics.tsv.gz")
    if not os.path.exists(fp):
        raise SystemExit(f"缺少 {fp}，先下载 IMDb name.basics.tsv.gz")
    idx: dict[str, list[str]] = defaultdict(list)
    n = 0
    with gzip.open(fp, "rt", encoding="utf-8", errors="replace", newline="") as f:
        header = f.readline().rstrip("\n").split("\t")
        col = {c: i for i, c in enumerate(header)}
        for line in f:
            n += 1
            parts = line.rstrip("\n").split("\t")
            if len(parts) < len(header):
                continue
            nn = norm_person(parts[col["primaryName"]])
            if nn and nn in wanted:
                idx[nn].append(parts[col["nconst"]])
    print(f"  name.basics 扫描 {n:,} 行，命中姓名键 {len(idx):,} 个")
    return idx


# --------------------------------------------------------------------------- #
# 从库里取商品与署名
# --------------------------------------------------------------------------- #

def load_products(conn: sqlite3.Connection) -> dict[str, dict]:
    """取每个商品的标题、年份、厂牌、格式、演员/导演署名。"""
    conn.row_factory = sqlite3.Row
    out: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT product_id, movie_id, relation_type FROM product_movie"):
        d = out.setdefault(r["product_id"], {"movie_ids": [], "relation": set()})
        d["movie_ids"].append(r["movie_id"])
        d["relation"].add(r["relation_type"])

    # 只保留「有独立来源证据」的商品。
    #
    # 两个必须排除的东西：
    #   1. source_type='synthetic_fixture' —— 合成夹具是我们自己造的，
    #      用它标真值等于又回到循环论证（旧版 s10 就是这么做的）。
    #   2. 只有评论、没有任何商品元数据的商品 —— 它的标题是空的，无从比较。
    #
    # 什么算"独立证据"，两条路都算，但都不含夹具：
    #   A. 页面采集成功且有快照（live / offline_snapshot）
    #   B. 第三方离线快照里有这条 ASIN 的元数据（third_party，经 raw_source_record 关联）
    #
    # 为什么 B 也算：全量运行里实时采集被 Amazon 的访问限制卡在一百多条，
    # 绝大多数商品的元数据来自第三方快照。如果只认 A，评价集会小到没法用；
    # 而 B 的证据同样可回溯到具体文件和行号，并不是"没有证据"。
    # 真正决定标注对错的不是商品元数据来自哪，而是**真值来自 IMDb**，与我们的合并规则无关。
    has_evidence = {r[0] for r in conn.execute(
        "SELECT product_id FROM product_collection"
        " WHERE status='success' AND snapshot_path IS NOT NULL"
        "   AND source_type IN ('live','offline_snapshot')")}
    has_evidence |= {r[0] for r in conn.execute(
        "SELECT DISTINCT fa.entity_id"
        "  FROM field_assertion fa"
        "  JOIN raw_source_record rsr ON rsr.record_id = fa.source_record_id"
        " WHERE fa.entity_type='product' AND rsr.source_type='third_party'")}

    fields = ("title", "release_date", "studio", "format", "director", "cast")
    for pid in list(out):
        if pid not in has_evidence:
            del out[pid]
    for r in conn.execute(
            "SELECT entity_id, field_name, raw_value FROM field_assertion"
            " WHERE entity_type='product' AND field_name IN"
            " ('title','release_date','studio','format','director','cast')"):
        pid = r["entity_id"]
        if pid not in out:
            continue
        d = out[pid]
        if r["field_name"] in ("director", "cast"):
            d.setdefault(r["field_name"], []).append(r["raw_value"])
        else:
            d.setdefault(r["field_name"], r["raw_value"])

    # 标题：优先 title，其次任一断言里的标题
    for pid, d in out.items():
        d["title"] = d.get("title") or ""
        rd = d.get("release_date") or ""
        m = re.match(r"^(\d{4})", str(rd))
        d["year"] = int(m.group(1)) if m else None
    return {p: d for p, d in out.items() if d["title"]}


def match_tconst(products: dict[str, dict],
                 tindex: dict[str, list[tuple[str, int | None, str]]]) -> dict[str, str]:
    """把商品匹配到 tconst。只接受**唯一**且年份吻合的结果。

    这是评价集构造里最容易被质疑的一步：如果这里判错，真值就错了。
    所以规则收得很紧：
        * 规范化标题必须完全一致（不做模糊匹配）；
        * 同一标题在 IMDb 里有多个候选时，必须靠年份（±1）筛到只剩一个；
        * 仍然剩下多个就放弃，不猜。
    宁可少几对样本，也不要一个错真值。
    """
    out: dict[str, str] = {}
    stats = {"title_hit": 0, "unique": 0, "by_year": 0, "ambiguous": 0, "no_hit": 0}
    for pid, d in products.items():
        nt = norm_title(d["title"])
        cands = tindex.get(nt) if nt else None
        if not cands:
            stats["no_hit"] += 1
            continue
        stats["title_hit"] += 1
        # 去重（primaryTitle 与 originalTitle 可能同名导致重复登记）
        uniq = {(c[0], c[1]) for c in cands}
        if len({c[0] for c in uniq}) == 1:
            out[pid] = next(iter(uniq))[0]
            stats["unique"] += 1
            continue
        if d["year"]:
            near = [c for c in uniq if c[1] is not None and abs(c[1] - d["year"]) <= 1]
            if len({c[0] for c in near}) == 1:
                out[pid] = near[0][0]
                stats["by_year"] += 1
                continue
        stats["ambiguous"] += 1
    print(f"  商品→tconst：标题命中 {stats['title_hit']:,}，"
          f"唯一 {stats['unique']:,}，靠年份消歧 {stats['by_year']:,}，"
          f"仍歧义 {stats['ambiguous']:,}，无命中 {stats['no_hit']:,}")
    return out


# --------------------------------------------------------------------------- #
# 组对
# --------------------------------------------------------------------------- #

def build_movie_pairs(products: dict[str, dict], pid2tc: dict[str, str],
                      n_target: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    by_tc: dict[str, list[str]] = defaultdict(list)
    for pid, tc in pid2tc.items():
        by_tc[tc].append(pid)

    # 正例：同一 tconst 的两个不同 ASIN
    pos = []
    for tc, pids in by_tc.items():
        pids = sorted(pids)
        for i in range(len(pids)):
            for j in range(i + 1, len(pids)):
                a, b = pids[i], pids[j]
                pos.append({
                    "id_a": a, "id_b": b, "prefill_label": 1,
                    "stratum": "same_work_different_edition",
                    "evidence": (f"两者都唯一匹配 IMDb {tc}；"
                                 f"A={products[a]['title']!r}({products[a]['year']}) "
                                 f"B={products[b]['title']!r}({products[b]['year']})"),
                })

    # 负例：不同 tconst，按"难 → 易"分层
    #
    # 早期版本是全量枚举所有已知商品的两两组合：
    #     for i in range(len(known)):
    #         for j in range(i+1, len(known)):
    # 6,517 个商品 → 2,100 万对，而且每对都要跑一次 SequenceMatcher。
    # 实测卡了 5 分钟以上没有任何输出 —— 这是典型的"评价集脚本比主流程还慢"。
    #
    # 评价集只需要每层几十对，**不需要遍历所有对**。所以改成按语义分层定向抽样：
    #   难例：规范化标题**完全相同**但 IMDb 条目不同的（同名翻拍、续集、同名不同片）
    #         —— 这类对总量很小，可以精确枚举，而且正是最该被考到的
    #   中例：共享首个实词、但标题不完全相同的（续集、同系列）
    #   易例：随机对（标题毫不相干，用来确认匹配器不会乱合并）
    # 每层都设采样上限，脚本耗时从"不可用"降到秒级。
    known = sorted(pid2tc)
    hard: list[dict] = []
    mid: list[dict] = []
    easy: list[dict] = []

    def _neg(a: str, b: str, stratum_prefix: str) -> dict:
        sim = title_similarity(norm_title(products[a]["title"]),
                               norm_title(products[b]["title"]))
        return {"id_a": a, "id_b": b, "prefill_label": 0,
                "stratum": f"{stratum_prefix}({sim:.2f})",
                "evidence": (f"IMDb {pid2tc[a]} vs {pid2tc[b]}；"
                             f"A={products[a]['title']!r}({products[a]['year']}) "
                             f"B={products[b]['title']!r}({products[b]['year']})；"
                             f"标题相似度 {sim:.2f}")}

    by_title: dict[str, list[str]] = defaultdict(list)
    by_head: dict[str, list[str]] = defaultdict(list)
    for pid in known:
        nt = norm_title(products[pid]["title"])
        by_title[nt].append(pid)
        head = nt.split()[0] if nt.split() else ""
        if head:
            by_head[head].append(pid)

    for nt, pids in by_title.items():
        if len(pids) < 2:
            continue
        for a, b in itertools.combinations(sorted(pids), 2):
            if pid2tc[a] != pid2tc[b]:
                hard.append(_neg(a, b, "same_title_diff_imdb"))

    cap = max(600, n_target * 6)
    for head, pids in by_head.items():
        if len(pids) < 2 or len(mid) >= cap:
            continue
        for a, b in itertools.combinations(sorted(pids), 2):
            if len(mid) >= cap:
                break
            if pid2tc[a] != pid2tc[b] and norm_title(products[a]["title"]) != \
                    norm_title(products[b]["title"]):
                mid.append(_neg(a, b, "similar_title_mid"))

    tries = 0
    while len(easy) < cap and tries < cap * 40 and len(known) > 1:
        tries += 1
        a, b = rng.sample(known, 2)
        if pid2tc[a] != pid2tc[b]:
            easy.append(_neg(a, b, "similar_title_easy"))

    for lst in (pos, hard, mid, easy):
        rng.shuffle(lst)
    return balance(pos, [hard, mid, easy], n_target, rng)


def build_person_pairs(products: dict[str, dict], nindex: dict[str, list[str]],
                       n_target: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    raws: set[str] = set()
    for d in products.values():
        raws.update(d.get("cast") or [])
        raws.update(d.get("director") or [])

    # 每个署名唯一匹配一个 nconst 才用；多义的直接丢掉（不猜）
    raw2nc: dict[str, str] = {}
    for raw in raws:
        hits = nindex.get(norm_person(raw))
        if hits and len(set(hits)) == 1:
            raw2nc[raw] = hits[0]
    print(f"  署名 {len(raws):,} 个，唯一匹配 nconst {len(raw2nc):,} 个")

    by_nc: dict[str, list[str]] = defaultdict(list)
    for raw, nc in raw2nc.items():
        by_nc[nc].append(raw)

    pos = []
    for nc, variants in by_nc.items():
        vs = sorted(set(variants))
        for i in range(len(vs)):
            for j in range(i + 1, len(vs)):
                a, b = vs[i], vs[j]
                pos.append({
                    "id_a": a, "id_b": b, "prefill_label": 1,
                    "stratum": "same_person_alias",
                    "evidence": f"两种写法都唯一匹配 IMDb {nc}：{a!r} / {b!r}",
                })

    names = sorted(raw2nc)
    hard, mid, easy = [], [], []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            if raw2nc[a] == raw2nc[b]:
                continue
            na, nb = norm_person(a), norm_person(b)
            if not na or not nb:
                continue
            ta, tb = na.split(), nb.split()
            rec = {"id_a": a, "id_b": b, "prefill_label": 0,
                   "evidence": (f"IMDb {raw2nc[a]} vs {raw2nc[b]}；"
                                f"{a!r} / {b!r}")}
            if ta[-1] == tb[-1]:
                hard.append({**rec, "stratum": f"same_surname({ta[-1]})"})
            elif ta[0] == tb[0]:
                mid.append({**rec, "stratum": f"same_first_name({ta[0]})"})
            else:
                easy.append({**rec, "stratum": "random_different_person"})

    for lst in (pos, hard, mid, easy):
        rng.shuffle(lst)
    return balance(pos, [hard, mid, easy], n_target, rng)


def balance(pos: list[dict], neg_levels: list[list[dict]], n_target: int,
            rng: random.Random) -> list[dict]:
    """凑成正好 n_target 对，负例按「难 → 易」优先，正例占一半。"""
    half = n_target // 2
    chosen = pos[:half]
    need = n_target - len(chosen)
    for level in neg_levels:
        if need <= 0:
            break
        take = min(need, len(level))
        chosen += level[:take]
        need -= take
    if need > 0:                     # 负例不够，用正例补，保证总数
        chosen += pos[half:half + need]
    rng.shuffle(chosen)
    return chosen


# --------------------------------------------------------------------------- #
# 输出 / 导入
# --------------------------------------------------------------------------- #

CSV_FIELDS = ["pair_id", "entity_type", "id_a", "id_b", "title_a", "title_b",
              "year_a", "year_b", "stratum", "prefill_label", "label",
              "label_source", "reviewer_note", "evidence"]


def write_csv(path: str, etype: str, pairs: list[dict], products: dict[str, dict],
              seed: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for i, p in enumerate(pairs, 1):
            pid = f"EP-{etype[0].upper()}{i:04d}"
            a, b = p["id_a"], p["id_b"]
            da = products.get(a, {})
            db = products.get(b, {})
            w.writerow({
                "pair_id": pid, "entity_type": etype,
                "id_a": a, "id_b": b,
                "title_a": da.get("title", ""), "title_b": db.get("title", ""),
                "year_a": da.get("year") or "", "year_b": db.get("year") or "",
                "stratum": p["stratum"],
                "prefill_label": p["prefill_label"],
                "label": "",                       # ← 人工填写
                "label_source": "imdb_prefill_pending_human",
                "reviewer_note": "",
                "evidence": p["evidence"],
            })
    print(f"  写出 {path}（{len(pairs)} 对，label 列留空待人工填写）")


def do_import(db_path: str) -> int:
    """把 CSV 里人工填好的 label 写回 meta_eval_pair。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    total = 0
    for etype in ("movie", "person"):
        fp = os.path.join(OUT_DIR, f"eval_{etype}s.csv")
        if not os.path.exists(fp):
            print(f"  跳过 {etype}：{fp} 不存在")
            continue
        rows = list(csv.DictReader(open(fp, encoding="utf-8-sig")))
        filled = [r for r in rows if str(r.get("label", "")).strip() in ("0", "1")]
        print(f"  {etype}: {len(rows)} 对，已人工标注 {len(filled)} 对")
        if not filled:
            continue
        conn.execute("DELETE FROM meta_eval_pair WHERE entity_type=?", (etype,))
        for r in filled:
            pa = conn.execute(
                "SELECT 1 FROM product_movie WHERE product_id=?", (r["id_a"],)).fetchone()
            pred = 0
            if etype == "movie":
                q = conn.execute(
                    "SELECT (SELECT GROUP_CONCAT(movie_id) FROM product_movie WHERE product_id=?) a,"
                    "       (SELECT GROUP_CONCAT(movie_id) FROM product_movie WHERE product_id=?) b",
                    (r["id_a"], r["id_b"])).fetchone()
                pred = int(q["a"] is not None and q["a"] == q["b"])
            else:
                q = conn.execute(
                    "SELECT (SELECT person_id FROM person_name WHERE name_norm=? LIMIT 1) a,"
                    "       (SELECT person_id FROM person_name WHERE name_norm=? LIMIT 1) b",
                    (norm_person(r["id_a"]), norm_person(r["id_b"]))).fetchone()
                pred = int(q["a"] is not None and q["a"] == q["b"])
            conn.execute(
                "INSERT OR REPLACE INTO meta_eval_pair (pair_id,entity_type,id_a,id_b,"
                "predicted,label,sample_group,difficulty,note)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (r["pair_id"], etype, r["id_a"], r["id_b"], pred, int(r["label"]),
                 "eval", r["stratum"],
                 f"人工标注；IMDb 预填 {r['prefill_label']}；"
                 f"{r['evidence']}"[:500]))
            total += 1
        conn.commit()
    conn.close()
    print(f"  已写回 {total} 对（note 字段已标明「人工标注」，sample_group=eval）")
    print("  说明：meta_eval_pair 表没有 label_source 列，"
          "「哪些是人工标注」以 CSV 的 label 列是否填了 0/1 为准；"
          "s10 读的也是 CSV，不读这张表。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "db", "etl.sqlite3"))
    ap.add_argument("--per-type", type=int, default=100, help="每类目标对数（默认 100）")
    ap.add_argument("--seed", type=int, default=20261104)
    ap.add_argument("--import", dest="do_import_flag", action="store_true",
                    help="把 CSV 里的人工 label 写回库")
    args = ap.parse_args(argv)

    if args.do_import_flag:
        return do_import(args.db)

    if not os.path.exists(args.db):
        raise SystemExit(f"库不存在：{args.db}")
    conn = sqlite3.connect(args.db)
    print(f"库：{args.db}")
    products = load_products(conn)
    print(f"可核对商品 {len(products):,} 个（有成功页面快照 + 有标题）")
    if len(products) < 20:
        raise SystemExit("可用商品太少，先跑 s03/s04/s05 再生成评价集")

    # 只为库里出现过的标题建索引，避免全量载入
    wanted = {norm_title(d["title"]) for d in products.values()} - {""}
    print("构建 IMDb 标题索引…")
    tindex = build_title_index(wanted)
    pid2tc = match_tconst(products, tindex)

    print("构建 IMDb 姓名索引…")
    raws: set[str] = set()
    for d in products.values():
        raws.update(d.get("cast") or [])
        raws.update(d.get("director") or [])
    nindex = build_name_index({norm_person(x) for x in raws} - {""})

    print("组对…")
    mp = build_movie_pairs(products, pid2tc, args.per_type, args.seed)
    pp = build_person_pairs(products, nindex, args.per_type, args.seed)

    write_csv(os.path.join(OUT_DIR, "eval_movies.csv"), "movie", mp, products, args.seed)
    write_csv(os.path.join(OUT_DIR, "eval_persons.csv"), "person", pp, products, args.seed)

    print("\n下一步：打开两份 CSV，在 `label` 列填 1（应合并）或 0（不应合并），")
    print("       然后跑 `python scripts/build_eval_set.py --import` 写回库。")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
