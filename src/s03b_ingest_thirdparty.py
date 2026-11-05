"""任务 2 补充路径：接入第三方商品元数据（SNAP meta_Movies_and_TV）。

## 为什么需要这一步

实时采集在合规限速下只能拿到一百多条（Amazon 在约 100 次请求后开始返回验证码，
采集器按熔断规则主动停手，见 `evidence/s3_circuit_breaker.json`）。
而作业要的是 253,059 个 ASIN 的元数据。硬爬完需要 ~211 小时、~80 GB，
既不可行，也会把"不绕过访问限制"这条要求踩掉。

所以走作业允许的另一条路：**第三方补充数据**。原文：

> 允许使用 IMDb、豆瓣或其他允许使用的数据源补充；
> **离线快照、第三方补充与实时采集分别统计，不得混为同一成功指标**。

数据源：Stanford SNAP 随 movies.txt 一起发布的
`meta_Movies_and_TV.json.gz`（96 MB，同一批 ASIN 的商品元数据）。
覆盖 112,730 / 253,059 = 44.55% 的 ASIN。

## 这个文件的两个坑

1. **不是 JSON，是 Python repr。**
   每行形如 `{'asin': '0005019281', 'title': 'Ice Age', ...}`，
   用的是单引号。`json.loads` 会直接报错，必须 `ast.literal_eval`。
   为什么不用正则硬抠：title 里可能含引号、逗号、大括号，
   正则会切错字段。`literal_eval` 是唯一安全的解析方式（它只求值字面量，
   不执行代码，所以不存在安全问题）。

2. **它只有标题和类目，没有演职员。**
   所以这一步只能提供 `title` / `categories` / `salesRank` / `brand`，
   演职员仍然只能来自实时采集到的页面。
   这不是缺陷，是必须如实写进报告的数据边界：
   **第三方补的是"这是什么作品"，补不了"谁演的"。**

## 产出

* `raw_source_file` 一行（文件级证据：路径 + SHA-256 + 字节数）
* `raw_source_record` 每个 ASIN 一行（`source_type='third_party'`，
  locator 是行号，用于回溯"这个标题来自文件的第几行"）
* `field_assertion` 每个 ASIN 的 `title`（`entity_type='product'`）
* `stg_classification` 按 SNAP 的类目路径做初判
  （只判 movie / series / non_movie；`box_set` 和 `concert_other` 需要页面结构，这里判不了）

运行：python -m src.s03b_ingest_thirdparty
      python -m src.s03b_ingest_thirdparty --limit 5000   # 调试
"""
from __future__ import annotations

import argparse
import ast
import gzip
import json
import os
import re
import sys

from src import common as C

EXTRACTOR_VERSION = "snap-meta-v1"
RULE_TITLE = "R-EXT-SNAP-TITLE"
RULE_CATEGORY = "R-CLS-SNAP-CATEGORY"

# SNAP 类目路径的判定规则。**从叶子往根匹配**，第一个命中就返回。
#
# 为什么必须从叶子往根：
#   几乎所有路径都以 "Movies & TV" 开头，如果按根到叶匹配，
#   一条 `Movies & TV / Television / TV Shows` 会先命中根上的 "Movies"，
#   被判成电影 —— 实测第一版就是这么错的，126 条剧集全被标成 series 之外的错类。
#   叶子是最具体的，先看叶子才对。
#
# 为什么用正则词边界而不是 `in`：
#   `"tv" in "movies & tv"` 是 True。裸子串匹配会把 "Movies & TV" 里的 "tv"
#   当成电视剧证据。`\btv\b` 同样会命中 "Movies & TV"，
#   所以真正的办法是**先判叶子、再判祖先**，加上词边界只是第二道保险。
LEAF_RULES = [
    (r"\b(tv|television)\b|\btv (shows?|series|miniseries)\b|\bminiseries\b|\bseasons?\b",
     "series"),
    (r"\bmovies?\b|\bfilms?\b|\bcult movies\b|\banime\b|\bdocumentary\b|\bclassics\b",
     "movie"),
    (r"\b(cds?|vinyl|music|mp3|albums?|soundtracks?)\b", "non_movie"),
    (r"\b(books?|kindle|ebooks?|paperback|hardcover)\b", "non_movie"),
    (r"\b(video games?|software|electronics|toys|computers?)\b", "non_movie"),
]

# Amazon 的**根/聚合节点**：它们同时含 "movies" 和 "tv" 两个词，
# 描述的是"这个大类下有什么"，而不是"这条商品是什么"。
#
# 为什么必须显式排除：路径只有一段 `['Movies & TV']` 时（实测占比约 4%），
# 从叶子往根仍然会落在这一段上，`\btv\b` 命中后判成 series —— 这是错的，
# 因为 "Movies & TV" 是整棵树的根，不含任何类型信息。
# 这种"信息不足"必须老实返回 pending，交给后面的页面结构解析去判，
# 而不是靠根节点猜一个。宁可少判，不可错判。
ROOT_SEGMENTS = {
    "movies & tv", "movies and tv", "movies tv",
    "movies & tv shows", "movies and tv shows",
}


def classify_categories(cats: list[str]) -> tuple[str, str, float]:
    """按 SNAP 的类目路径判定分类，返回 (category, basis, confidence)。

    置信度为什么不是 1.0：类目是 Amazon 自己的上架路径，
    不是"这是不是电影"的权威判断。它可能把演唱会 DVD 归到 Movies 下。
    所以这一步给的只是**初判**，页面结构解析（s04）优先级更高，
    `_flush` 里的 ON CONFLICT 只在已有分类是 pending 时才覆盖。
    """
    if not cats:
        return "pending", "SNAP 无类目路径", 0.2
    path = " / ".join(cats)
    n = len(cats)
    # 从叶子往根走。用下标倒序而不是 reversed()+index()：
    # 路径里若出现重复段（如 `Movies & TV / Movies / Movies`），
    # `cats.index(comp)` 永远返回第一次出现的下标，深度就算错了。
    for i in range(n - 1, -1, -1):
        comp = cats[i]
        low = comp.lower().strip()
        if low in ROOT_SEGMENTS:
            # 根节点不提供类型证据，跳过继续往上看 —— 但其实已经是最后一段了，
            # 所以这里等价于"信息不足"。
            continue
        for pat, cat in LEAF_RULES:
            if re.search(pat, low):
                # 命中越靠近叶子的层，说明越具体，置信度越高
                conf = 0.80 if i == n - 1 else 0.65
                return cat, f"SNAP 类目路径「{path}」在「{comp}」上命中「{pat}」", conf
    if n == 1:
        return ("pending",
                f"SNAP 类目路径「{path}」只有根节点，不含类型信息", 0.2)
    return "pending", f"SNAP 类目路径「{path}」无法判定", 0.3


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--file", default=None, help="meta_Movies_and_TV.json.gz 路径")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少行（0=全部）")
    args = ap.parse_args(argv)

    cfg = C.CFG.get("third_party", {})
    fp = args.file or cfg.get("snap_meta_file", "_tmp/meta_Movies_and_TV.json.gz")
    fp = fp if os.path.isabs(fp) else os.path.join(C.ROOT, fp)
    if not os.path.exists(fp):
        raise SystemExit(f"找不到第三方元数据文件：{fp}\n"
                         f"下载：curl -o _tmp/meta_Movies_and_TV.json.gz "
                         f"https://snap.stanford.edu/data/amazon/productGraph/categoryFiles/"
                         f"meta_Movies_and_TV.json.gz")

    conn = C.connect(args.db)
    log = C.Logger("s03b_ingest_thirdparty")
    run_id = C.start_run(conn, "s03b_ingest_thirdparty",
                         {"file": os.path.relpath(fp, C.ROOT), "limit": args.limit})
    log.run_id = run_id

    # ---- 文件级证据：字节数 + SHA-256 + 修改时间，全部落库 ----
    size = os.path.getsize(fp)
    log.info(f"计算 SHA-256（{size / 1048576:.1f} MB）…")
    sha = C.sha256_file(fp)
    rel = os.path.relpath(fp, C.ROOT).replace("\\", "/")
    file_id = C.det_id("SF", "snap_meta", sha)
    # raw_source_file 的列是「文件属性」而不是「来源类型」——
    # 来源类型在 raw_source_record 上（同一份文件里的行可以有不同的来源语义）。
    # 这里如实写 name/url/字节数/SHA-256/切片范围（全量时 range 为 0..size-1）。
    conn.execute(
        "INSERT OR IGNORE INTO raw_source_file (file_id,name,url,download_started_at,"
        "download_finished_at,byte_size,sha256,content_type,range_start,range_end,note)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (file_id, "meta_Movies_and_TV.json.gz",
         "https://snap.stanford.edu/data/amazon/productGraph/categoryFiles/"
         "meta_Movies_and_TV.json.gz",
         None, C.now_iso(), size, sha, "application/gzip", 0, size - 1,
         "SNAP 随 movies.txt 发布的同批 ASIN 商品元数据；"
         "格式为 Python repr（单引号），必须用 ast.literal_eval 解析；"
         "只含标题/类目/品牌，无演职员"))
    log.info(f"文件证据：{rel}  {size:,} 字节  sha256={sha[:16]}…")

    stat = {"lines": 0, "parsed": 0, "with_title": 0, "parse_error": 0,
            "no_asin": 0, "not_in_list": 0, "assertions": 0, "classified": 0,
            "by_category": {}}
    batch: list[tuple] = []
    recs: list[tuple] = []
    asserts: list[tuple] = []
    classes: list[tuple] = []

    with C.step(conn, run_id, "ingest_third_party", 1, log) as st:
        with gzip.open(fp, "rt", encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                stat["lines"] += 1
                if args.limit and stat["lines"] > args.limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    # 关键：不是 json.loads —— 这个文件是 Python repr
                    obj = ast.literal_eval(line)
                except (ValueError, SyntaxError):
                    stat["parse_error"] += 1
                    continue
                if not isinstance(obj, dict):
                    stat["parse_error"] += 1
                    continue
                stat["parsed"] += 1
                asin = (obj.get("asin") or "").strip()
                if not asin:
                    stat["no_asin"] += 1
                    continue
                # 只处理采集清单里的 ASIN（清单来自评论文件，是作业的锚点）
                if not conn.execute("SELECT 1 FROM product WHERE product_id=?",
                                    (asin,)).fetchone():
                    stat["not_in_list"] += 1
                    continue

                title = obj.get("title")
                title = title.strip() if isinstance(title, str) else None
                cats = obj.get("categories") or []
                if isinstance(cats, list) and cats and isinstance(cats[0], list):
                    cats = [str(x) for x in cats[0]]
                elif not isinstance(cats, list):
                    cats = []

                rec_id = C.det_id("REC", "snap_meta", asin)
                recs.append((rec_id, "third_party", file_id, f"line={lineno}",
                             f"https://www.amazon.com/dp/{asin}", rel, sha, C.now_iso(),
                             f"SNAP meta 第 {lineno} 行；ASIN={asin}"))
                if title:
                    stat["with_title"] += 1
                    asserts.append((
                        C.det_id("A", "product", asin, "title", rec_id, title),
                        "product", asin, "title", title, C.norm_text(title), None,
                        rec_id, RULE_TITLE, EXTRACTOR_VERSION, 0.80, "candidate",
                        run_id, C.now_iso()))
                    stat["assertions"] += 1

                cat, basis, conf = classify_categories(cats)
                classes.append((asin, cat, basis,
                                json.dumps({"categories": cats, "source": "snap_meta"},
                                           ensure_ascii=False),
                                conf, RULE_CATEGORY, run_id))
                stat["by_category"][cat] = stat["by_category"].get(cat, 0) + 1
                stat["classified"] += 1

                # 分批提交：内存可控，中断可续（确定性 ID + OR IGNORE）
                if len(recs) >= 20000:
                    _flush(conn, recs, asserts, classes)
                    recs, asserts, classes = [], [], []
        _flush(conn, recs, asserts, classes)
        st["counters"].update(stat)

    # 覆盖率：这一步到底补上了多少 ASIN
    total = conn.execute("SELECT COUNT(*) c FROM product").fetchone()["c"]
    covered = conn.execute(
        "SELECT COUNT(DISTINCT entity_id) c FROM field_assertion"
        " WHERE entity_type='product' AND field_name='title'"
        "   AND extractor_rule_id=?", (RULE_TITLE,)).fetchone()["c"]
    summary = {
        "run_id": run_id,
        "file": rel, "file_bytes": size, "file_sha256": sha,
        "source_type": "third_party",
        "extractor_version": EXTRACTOR_VERSION,
        **stat,
        "products_total": total,
        "products_with_thirdparty_title": covered,
        "coverage_pct": round(100.0 * covered / total, 2) if total else 0.0,
        "boundary": "SNAP meta 只提供标题/类目/销量排名/品牌，没有演职员。"
                    "演职员仍只能来自实时采集的页面，两类来源分开统计。",
        "note": "离线快照 / 第三方补充 / 实时采集三者按 source_type 分开统计，"
                "不合并为同一个成功指标（作业要求）。",
    }
    C.write_json(C.path("evidence", "s03b_thirdparty_summary.json"), summary)
    log.info(f"扫描 {stat['lines']:,} 行：解析成功 {stat['parsed']:,}，"
             f"有标题 {stat['with_title']:,}，写入断言 {stat['assertions']:,}，"
             f"分类 {stat['classified']:,}，解析失败 {stat['parse_error']:,}")
    log.info(f"第三方标题覆盖 {covered:,} / {total:,} 个 ASIN = {summary['coverage_pct']}%")
    log.info(f"分类分布: {stat['by_category']}")
    C.end_run(conn, run_id, "success", json.dumps(summary, ensure_ascii=False))
    conn.close()
    log.close()
    return 0


def _flush(conn, recs, asserts, classes) -> None:
    if recs:
        conn.executemany(
            "INSERT OR IGNORE INTO raw_source_record (record_id,source_type,source_file_id,"
            "locator,url,snapshot_path,content_hash,fetched_at,note)"
            " VALUES (?,?,?,?,?,?,?,?,?)", recs)
    if asserts:
        conn.executemany(
            "INSERT OR IGNORE INTO field_assertion (assertion_id,entity_type,entity_id,field_name,"
            "raw_value,normalized_value,value_num,source_record_id,extractor_rule_id,"
            "extractor_version,confidence,status,run_id,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", asserts)
    if classes:
        # 页面解析（s04）的判定优先级更高：这里用 ON CONFLICT 只在
        # 还没有分类、或已有分类是 pending 时才覆盖。
        conn.executemany(
            "INSERT INTO stg_classification (product_id,category,basis,evidence_json,"
            "confidence,rule_id,run_id) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(product_id) DO UPDATE SET"
            "   category=excluded.category, basis=excluded.basis,"
            "   evidence_json=excluded.evidence_json, confidence=excluded.confidence,"
            "   rule_id=excluded.rule_id, run_id=excluded.run_id"
            " WHERE stg_classification.category='pending'", classes)
    conn.commit()


if __name__ == "__main__":
    sys.exit(main())
