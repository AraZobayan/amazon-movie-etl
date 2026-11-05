"""任务 3：页面解析 + 产品分类（原始层 -> 暂存层）。

两件事：
  1. 把 success 状态的页面快照解析成结构化字段，写入 stg_product_page；
  2. 按规则把产品分成 电影/非电影/合集/剧集/演唱会或其他/待确认，写入 stg_classification。

分类为什么要单独一个字段存"依据"？
    作业原文：不能仅凭标题含有 DVD 或 Blu-ray 就认定其为电影。
    所以每条分类都带 basis（人可读理由）、evidence_json（命中的规则与字段）、
    confidence（置信度）。答辩时被问"这个为什么算电影"，能当场答出来。

运行：python -m src.s04_parse_and_classify
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from src import common as C

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

# 夹具结构（简化的"教学版 Amazon 页面"）
#
# 标题的闭合标签不能写死成 </div>：真实 Amazon 页面是
#     <h1 id="title"><span id="productTitle" ...>  Much Ado About Nothing  </span></h1>
# 夹具是 <div id="productTitle">...</div>。只写 </div> 的后果是
# **真实页面全部抽不到标题** —— 203 个真实页面因此被判成 partial/pending，
# 一个都没进实体层。这是"正则只对着自己造的假数据调"的典型翻车。
RE_TITLE = re.compile(r'id="productTitle"[^>]*>(.*?)</(?:div|span|h1)>', re.S)
RE_BYLINE = re.compile(r'id="byline[^"]*"[^>]*>(.*?)</div>', re.S)
RE_FORMAT = re.compile(r"<b>Format:</b>\s*([^<]+)")
RE_DATE = re.compile(r"<b>Release date:</b>\s*([^<]*)")
RE_STUDIO = re.compile(r"<b>Studio:</b>\s*([^<]*)")
RE_ASIN = re.compile(r"<b>ASIN:</b>\s*([A-Z0-9]{8,14})")
RE_DIRECTOR = re.compile(r"Directed by:\s*([^|<]+)")
RE_STARRING = re.compile(r"Starring:\s*([^|<]+)")

# 真实页面：详情区（detailBullets）是"标签 → 冒号 → 值"的三段式，
# 但中间夹着 U+200E/U+200F 这些双向控制符和大量空白。
# 做法不是写一个更复杂的正则，而是先把 HTML 拆成"token 序列"，再按位置取下一项——
# 结构化的东西用结构化的方式解析，比在字符串上打补丁可靠。
BIDI_RE = re.compile(r"[\u200e\u200f\u202a-\u202e]")
SEP = "\x01"


def _detail_tokens(html: str) -> list[str]:
    """把详情区拆成 token 序列。返回形如 ['MPAA rating', ':', 'PG', 'Studio', ':', 'Universal']"""
    i = html.find("detailBullets")
    if i < 0:
        return []
    seg = html[i:i + 12000]
    flat = TAG_RE.sub(SEP, seg)
    flat = _unescape(flat)
    flat = BIDI_RE.sub(" ", flat)
    return [t.strip() for t in flat.split(SEP) if t.strip()]


def _unescape(s: str) -> str:
    import html as _h
    return _h.unescape(s)


def _detail_value(tokens: list[str], *keys: str) -> str | None:
    """在 token 序列里找 `键 → 值`，返回第一个命中的值。

    注意键和冒号经常在**同一个 token 里**（`'Release date\\n   :'`），
    因为中间那些空白和双向控制符把标签边界吃掉了。
    所以不能假设"键的下一个 token 就是冒号"，要先把 token 自身按冒号劈开。
    """
    for i, tok in enumerate(tokens):
        raw = tok.strip()
        if ":" in raw:
            key, _, inline = raw.partition(":")
            key, inline = key.strip().lower(), inline.strip()
        else:
            key, inline = raw.lower(), ""
        if key not in keys:
            continue
        if inline:
            return inline
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if nxt == ":":
            return tokens[i + 2] if i + 2 < len(tokens) else None
        if nxt.startswith(":"):
            v = nxt[1:].strip()
            return v or None
        return nxt or None
    return None


def parse_detail_bullets(html: str) -> dict:
    """从真实页面的"商品详情"区抽字段。

    只抽本项目真正用得上的五项：发售日、介质、厂牌、导演、演员。
    抽不到的返回 None —— 不猜、不用相邻字段顶替。
    """
    tok = _detail_tokens(html)
    if not tok:
        return {}
    return {
        "release_date_raw": _detail_value(tok, "release date", "release date:"),
        "format_raw": _detail_value(tok, "media format", "format"),
        "studio_raw": _detail_value(tok, "studio", "production company"),
        "directors_raw": _detail_value(tok, "director", "directed by"),
        "starring_raw": _detail_value(tok, "actors", "starring", "actor"),
    }


def clean(s: str | None) -> str:
    if not s:
        return ""
    # 必须先反转义再剥标签：
    #   真实页面里 `To Have &amp; Have Not` 的 & 是转义过的，
    #   不反转义就会把 "&amp;" 当成标题的一部分存进库，
    #   于是下游的标题规范化、相似度、合并全都在错误的字符串上做。
    #   反转义放在剥标签之前也更安全 —— 剥标签用的 < > 本身不会被转义成实体。
    return WS_RE.sub(" ", TAG_RE.sub(" ", _unescape(s))).strip()


def parse_page(html: str, asin: str) -> dict:
    """从 HTML 里抽字段。

    说明：这里的正则针对的是本项目夹具的 HTML 结构（以及一个"简化版 Amazon 商品页"
    结构）。真实 Amazon 页面结构会变，工程上的做法是把选择器集中到
    `extractors/` 目录并带版本号（extractor_version），页面结构一变就升版本，
    这样血缘里能区分"这条字段是哪一版解析器抽的"。
    """
    out = {
        "asin_on_page": None, "title_raw": None, "byline_raw": None,
        "release_date_raw": None, "format_raw": None, "studio_raw": None,
        "directors_raw": None, "starring_raw": None,
    }
    m = RE_TITLE.search(html)
    if m:
        out["title_raw"] = clean(m.group(1)) or None
    m = RE_BYLINE.search(html)
    if m:
        out["byline_raw"] = clean(m.group(1))
        d = RE_DIRECTOR.search(out["byline_raw"] or "")
        if d:
            out["directors_raw"] = d.group(1).strip()
        s = RE_STARRING.search(out["byline_raw"] or "")
        if s:
            out["starring_raw"] = s.group(1).strip()
    for key, rx in (("format_raw", RE_FORMAT), ("release_date_raw", RE_DATE),
                    ("studio_raw", RE_STUDIO), ("asin_on_page", RE_ASIN)):
        m = rx.search(html)
        if m:
            v = clean(m.group(1))
            out[key] = v or None

    # ---------- 真实页面的兜底路径 ----------
    # 上面那组 <b>键:</b> 正则只存在于教学夹具里，真实 Amazon 页面没有。
    # 缺了兜底，真实页面会"标题抽到、ASIN 抽不到"，被下面的 asin_ok 判成 partial，
    # 分类因为没标题而落到 pending —— 采了 200 多个真页面，实体层一个都没多。
    if not out["asin_on_page"]:
        # 复用采集阶段同一个身份函数：采集时说"这页是 B000X 的"，解析时必须用同一个判据，
        # 否则两处标准不一致，会互相"打脸"（采集判成功、解析判不匹配）。
        try:
            from src.s03_collect_pages import page_identity
            _, _, asin_seen = page_identity(html)
            out["asin_on_page"] = asin_seen
        except Exception:  # noqa: BLE001 - 兜底失败不应中断解析
            pass
    if not (out["format_raw"] and out["release_date_raw"] and out["studio_raw"]):
        detail = parse_detail_bullets(html)
        for k, v in detail.items():
            if v and not out.get(k):
                out[k] = v
    return out


def classify(title: str | None, page: dict, asin: str) -> tuple[str, str, dict, float, str]:
    """规则分类，返回 (category, basis, evidence, confidence, rule_id)。

    规则顺序很重要：先排除非电影，再识别合集/剧集/演唱会，最后才认定电影。
    这样"标题里带 DVD 的音乐 CD"不会因为 DVD 而被判成电影。
    """
    cfg = C.CFG["classify"]
    t = (title or "").lower()
    ev: dict = {"title": title, "format": page.get("format_raw")}
    hits: dict[str, list[str]] = {}

    for name in ("non_movie_keywords", "box_set_keywords", "series_keywords", "concert_keywords"):
        hits[name] = [k for k in cfg[name] if k in t]
    ev["keyword_hits"] = hits

    if hits["non_movie_keywords"]:
        return ("non_movie", f"标题命中非电影关键词: {', '.join(hits['non_movie_keywords'])}",
                ev, 0.92, "R-CLS-NONMOVIE")
    if hits["concert_keywords"]:
        return ("concert_other", f"标题命中演出/演唱会关键词: {', '.join(hits['concert_keywords'])}",
                ev, 0.85, "R-CLS-CONCERT")
    if hits["series_keywords"]:
        return ("series", f"标题命中剧集关键词: {', '.join(hits['series_keywords'])}",
                ev, 0.8, "R-CLS-SERIES")
    if hits["box_set_keywords"]:
        return ("box_set", f"标题命中合集关键词: {', '.join(hits['box_set_keywords'])}",
                ev, 0.8, "R-CLS-BOXSET")
    # 有导演/主演信息 + 有发售日期 -> 电影（这是"证据"，不是"标题里有没有 DVD"）
    if page.get("directors_raw") or page.get("starring_raw"):
        return ("movie", "页面提供导演/主演署名，判定为电影作品载体", ev, 0.9, "R-CLS-MOVIE-CREW")
    if page.get("format_raw") and page.get("release_date_raw"):
        return ("pending", "有格式与发售日期但缺少演职员署名，证据不足，保留待确认",
                ev, 0.4, "R-CLS-PENDING")
    return ("pending", "页面信息不足以判定", ev, 0.2, "R-CLS-PENDING-EMPTY")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    conn = C.connect(args.db)
    log = C.Logger("s04_parse_and_classify")
    run_id = C.start_run(conn, "s04_parse_and_classify", {})
    log.run_id = run_id
    extractor_version = "page-extractor-v2"

    rows = conn.execute(
        "SELECT product_id, snapshot_path, content_hash, source_type, final_url"
        " FROM product_collection WHERE status='success' AND snapshot_path IS NOT NULL").fetchall()
    log.info(f"待解析成功页面 {len(rows):,} 份")

    stat = {"pages": 0, "ok": 0, "partial": 0, "failed": 0}
    cls_count: dict[str, int] = {}

    with C.step(conn, run_id, "parse_and_classify", 1, log) as st:
        for r in rows:
            pid = r["product_id"]
            fp = C.path(r["snapshot_path"])
            try:
                with open(fp, encoding="utf-8", errors="replace") as f:
                    html = f.read()
            except OSError as exc:
                log.warn(f"读取快照失败 {pid}: {exc}")
                stat["failed"] += 1
                continue
            stat["pages"] += 1
            p = parse_page(html, pid)

            # 核对"返回的页面是不是目标 ASIN"——作业明确要求做这个核对
            asin_ok = (p["asin_on_page"] == pid)
            parse_status = "ok" if (p["title_raw"] and asin_ok) else (
                "partial" if p["title_raw"] else "failed")
            if not asin_ok:
                parse_status = "partial"
            stat[parse_status] += 1

            snap_id = C.det_id("SNP", pid, r["content_hash"])
            rec_id = C.det_id("REC", "page", pid, r["content_hash"])
            conn.execute(
                "INSERT OR REPLACE INTO stg_product_page (snapshot_id,product_id,source_record_id,"
                "asin_on_page,page_kind,title_raw,byline_raw,release_date_raw,format_raw,studio_raw,"
                "parse_status,extractor_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (snap_id, pid, rec_id, p["asin_on_page"],
                 "product" if asin_ok else "asin_mismatch",
                 p["title_raw"], p["byline_raw"], p["release_date_raw"], p["format_raw"],
                 p["studio_raw"], parse_status, extractor_version))

            category, basis, ev, conf, rule = classify(p["title_raw"], p, pid)
            ev["asin_match"] = asin_ok
            conn.execute(
                "INSERT OR REPLACE INTO stg_classification (product_id,category,basis,evidence_json,"
                "confidence,rule_id,run_id) VALUES (?,?,?,?,?,?,?)",
                (pid, category, basis, json.dumps(ev, ensure_ascii=False), conf, rule, run_id))
            cls_count[category] = cls_count.get(category, 0) + 1

        st["counters"].update({"pages": stat["pages"], "ok": stat["ok"],
                               "partial": stat["partial"], "failed": stat["failed"],
                               "classification": cls_count})

    # 回填 product.is_movie（三态：1 电影 / 0 明确非电影 / NULL 待确认）
    conn.execute("""
        UPDATE product SET is_movie = (
            SELECT CASE WHEN c.category='movie' THEN 1
                        WHEN c.category IN ('non_movie','box_set','series','concert_other') THEN 0
                        ELSE NULL END
            FROM stg_classification c WHERE c.product_id = product.product_id)
    """)

    total_products = conn.execute("SELECT COUNT(*) c FROM product").fetchone()["c"]
    # 全表分布：s03b 已经按 SNAP 类目路径给「无页面」的商品做过初判，
    # 所以 stg_classification 的总行数远大于本步骤解析的页面数。
    # 只报本步骤的 cls_count 会让读者以为全库只有几百个商品被分类 —— 必须两个口径都报。
    table_dist = {r["category"]: r["n"] for r in conn.execute(
        "SELECT category, COUNT(*) n FROM stg_classification GROUP BY category")}
    table_total = sum(table_dist.values())
    summary = {
        "run_id": run_id,
        "extractor_version": extractor_version,
        "pages_parsed": stat["pages"],
        "parse_status": {"ok": stat["ok"], "partial": stat["partial"], "failed": stat["failed"]},
        "classification_counts_this_run": cls_count,
        "classification_counts_table": table_dist,
        "classification_rows_total": table_total,
        "products_total": total_products,
        "coverage": {
            "parsed_over_all_products": round(stat["pages"] / max(total_products, 1), 4),
            "classified_over_all_products": round(table_total / max(total_products, 1), 4),
            "movie_over_parsed": round(cls_count.get("movie", 0) / max(stat["pages"], 1), 4),
            "movie_over_all_products": round(table_dist.get("movie", 0) / max(total_products, 1), 4),
        },
        "note": ("两个口径必须分开读：classification_counts_this_run 只含本次解析的页面；"
                 "classification_counts_table 是全表分布，其中无页面的商品由 s03b 按 SNAP "
                 "类目路径初判（basis=third_party_category），证据强度弱于页面解析。"
                 "没有页面也没有类目的商品不进表 —— 不能凭空给类别。"),
    }
    C.write_json(C.path("evidence", "s04_parse_classify_summary.json"), summary)
    log.info(f"解析 {stat['pages']:,} 页（ok={stat['ok']}, partial={stat['partial']}, "
             f"failed={stat['failed']}）；本次分类 {cls_count}")
    log.info(f"全表分类分布（含 s03b 类目初判）{table_dist}，共 {table_total:,} 行")

    C.end_run(conn, run_id, "success", json.dumps(cls_count, ensure_ascii=False))
    conn.close()
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
