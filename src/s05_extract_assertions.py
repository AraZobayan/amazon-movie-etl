"""任务 5（上半）：字段抽取 —— 把页面解析结果变成"字段候选断言"。

核心思想（本作业拿 20 分的那块）：
    抽取的结果**不是**直接写进 movie 表，而是写进 field_assertion，
    每条断言都带着"它从哪条原始记录来的、由哪条规则抽的、置信度多少"。
    之后所有合并、补全、冲突处理，都是在操作这些断言，而不是覆盖值。

所以 field_assertion 是整条血缘链的"中间账本"：
    raw_source_record (页面快照/评论文件)  <-- 断言 --  movie 最终字段

字段语义靠"规则 ID"承载，例如：
    R-EXT-DATE-PHYSICAL     商品发售日期（DVD/蓝光上市日）—— 不是上映日！
    R-EXT-DATE-THEATRICAL   影院上映日期（来自第三方）
    R-EXT-TITLE-PAGE        页面标题（含版本噪声，原样保留）
这样一条断言自己就说明了"我这个日期到底是什么日期"。

运行：python -m src.s05_extract_assertions
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from src import common as C

EXTRACTOR_VERSION = "field-extractor-v2"
DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

# 真实 Amazon 页面的日期写法是 "January 1, 1993"，不是 ISO。
# 只写 ISO 正则的后果：125 条真实页面的发售日**一条都没进断言表**，
# 表面上"解析成功 905 页"，实际上字段全丢了 —— 这类静默丢弃最难发现，
# 因为它不报错，只是数字偏小。
_MONTH_FULL = ["january", "february", "march", "april", "may", "june", "july",
               "august", "september", "october", "november", "december"]
# 全名和前三字母都要能认：页面里既写 "December 25, 1999" 也写 "Dec 25, 1999"，
# 还有 "Sept." 这种四字母缩写。只收全名会漏掉一半。
MONTHS: dict[str, int] = {}
for _i, _m in enumerate(_MONTH_FULL, start=1):
    MONTHS[_m] = _i
    MONTHS[_m[:3]] = _i
MONTHS["sept"] = 9
DATE_RE_EN = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})")


def parse_release_date(raw: str | None) -> tuple[str, str] | None:
    """把页面上的发售日期文本解析成 (规范化值, 规则后缀)。

    返回值里的规范化值刻意**不总是完整日期**，因为完整日期会撒谎：

        Amazon 的 "Release date" 里，凡是只知道年份的商品，一律写成 1 月 1 日。
        实测 125 条真实发售日里，大部分是 "January 1, 1993" 这种形态。

    如果把 "January 1, 1993" 原样规范成 1993-01-01，下游按长度判精度
    （10 位 = day）就会得出"这一天精确到日"的结论 —— 这正是作业反复警告的
    "把只知道年份的数据补成 1 月 1 日"，只不过这次不是我们编的，是数据源编的。

    所以规则是：
        月=1 且 日=1  →  规范化成 "1993"（4 位），规则带 -YEARONLY 后缀
                        下游按长度反推精度得到 year，不需要额外字段
        其余          →  规范化成完整 ISO，精度 day
    """
    if not raw:
        return None
    m = DATE_RE.search(raw)
    if m:
        y, mo, d = m.group(1), m.group(2), m.group(3)
        if mo == "01" and d == "01":
            return y, "YEARONLY"
        return f"{y}-{mo}-{d}", ""
    m = DATE_RE_EN.search(raw)
    if m:
        mo = MONTHS.get(m.group(1).lower())
        if not mo:
            return None
        y, d = m.group(3), int(m.group(2))
        if mo == 1 and d == 1:
            return y, "YEARONLY"
        return f"{y}-{mo:02d}-{d:02d}", ""
    return None


def add_assertion(conn, run_id: str, *, entity_type: str, entity_id: str, field: str,
                  raw: str | None, norm: str | None, source_record_id: str,
                  rule: str, confidence: float, value_num: float | None = None,
                  status: str = "candidate") -> str:
    """写一条断言。ID 由 (实体, 字段, 来源, 规范化值) 决定 -> 天然去重、可幂等。"""
    aid = C.det_id("A", entity_type, entity_id, field, source_record_id, norm)
    conn.execute(
        "INSERT INTO field_assertion (assertion_id,entity_type,entity_id,field_name,raw_value,"
        "normalized_value,value_num,source_record_id,extractor_rule_id,extractor_version,"
        "confidence,status,run_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(assertion_id) DO NOTHING",
        (aid, entity_type, entity_id, field, raw, norm, value_num, source_record_id, rule,
         EXTRACTOR_VERSION, confidence, status, run_id, C.now_iso()))
    return aid


def split_people(s: str | None) -> list[str]:
    """把 "A, B, C" 拆成 ["A","B","C"]。

    坑：人名里本身可能有逗号（"Sammy Davis Jr." 没有，但 "Last, First" 有）。
    这里用启发式：如果拆分后某段很短（<=2 字符）或下一段以大写开头且前面是
    "Jr."/"Sr."，就不拆。生产环境应该用更严格的解析（或直接用结构化数据源）。
    """
    if not s:
        return []
    parts = [p.strip() for p in re.split(r",\s*", s) if p.strip()]
    merged: list[str] = []
    for p in parts:
        if merged and re.match(r"^(Jr\.?|Sr\.?|II|III|IV)$", p, re.I):
            merged[-1] = merged[-1] + ", " + p
        elif len(p) <= 2 and merged:
            merged[-1] = merged[-1] + ", " + p
        else:
            merged.append(p)
    return merged


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    conn = C.connect(args.db)
    log = C.Logger("s05_extract_assertions")
    run_id = C.start_run(conn, "s05_extract_assertions", {})
    log.run_id = run_id

    rows = conn.execute(
        "SELECT p.product_id, p.title_raw, p.release_date_raw, p.format_raw, p.studio_raw,"
        " p.byline_raw, p.source_record_id, c.category"
        " FROM stg_product_page p LEFT JOIN stg_classification c USING (product_id)"
        " WHERE p.parse_status IN ('ok','partial')").fetchall()
    log.info(f"待抽取字段的页面 {len(rows):,} 份")

    cnt = {"title": 0, "date": 0, "date_yearonly": 0, "format": 0, "studio": 0,
           "director": 0, "cast": 0}
    with C.step(conn, run_id, "extract", 1, log) as st:
        for r in rows:
            pid, rec = r["product_id"], r["source_record_id"]
            if not rec:
                continue

            if r["title_raw"]:
                add_assertion(conn, run_id, entity_type="product", entity_id=pid,
                              field="title", raw=r["title_raw"],
                              norm=C.norm_title(r["title_raw"]), source_record_id=rec,
                              rule="R-EXT-TITLE-PAGE", confidence=0.9)
                cnt["title"] += 1

            if r["release_date_raw"]:
                parsed = parse_release_date(r["release_date_raw"])
                if parsed:
                    iso, suffix = parsed
                    rule = "R-EXT-DATE-PHYSICAL" + (f"-{suffix}" if suffix else "")
                    add_assertion(conn, run_id, entity_type="product", entity_id=pid,
                                  field="release_date", raw=r["release_date_raw"], norm=iso,
                                  source_record_id=rec, rule=rule,
                                  confidence=0.55 if suffix else 0.6,
                                  value_num=float(iso[:4]))
                    cnt["date"] += 1
                    if suffix == "YEARONLY":
                        cnt["date_yearonly"] += 1

            if r["format_raw"]:
                add_assertion(conn, run_id, entity_type="product", entity_id=pid,
                              field="edition_format", raw=r["format_raw"],
                              norm=C.norm_text(r["format_raw"]), source_record_id=rec,
                              rule="R-EXT-FORMAT", confidence=0.9)
                cnt["format"] += 1

            if r["studio_raw"]:
                add_assertion(conn, run_id, entity_type="product", entity_id=pid,
                              field="studio", raw=r["studio_raw"],
                              norm=C.norm_text(r["studio_raw"]), source_record_id=rec,
                              rule="R-EXT-STUDIO", confidence=0.85)
                cnt["studio"] += 1

            # 演职员：一人一条断言，后面按人做消歧
            if r["byline_raw"]:
                d = re.search(r"Directed by:\s*([^|]+)", r["byline_raw"])
                if d:
                    for name in split_people(d.group(1)):
                        add_assertion(conn, run_id, entity_type="product", entity_id=pid,
                                      field="director", raw=name, norm=C.norm_person_name(name),
                                      source_record_id=rec, rule="R-EXT-BYLINE-DIRECTOR",
                                      confidence=0.85)
                        cnt["director"] += 1
                s = re.search(r"Starring:\s*([^|]+)", r["byline_raw"])
                if s:
                    for name in split_people(s.group(1)):
                        add_assertion(conn, run_id, entity_type="product", entity_id=pid,
                                      field="cast", raw=name, norm=C.norm_person_name(name),
                                      source_record_id=rec, rule="R-EXT-BYLINE-CAST",
                                      confidence=0.7)
                        cnt["cast"] += 1

        st["counters"].update(cnt)

    n = conn.execute("SELECT COUNT(*) c FROM field_assertion").fetchone()["c"]
    by_field = [dict(r) for r in conn.execute(
        "SELECT field_name, COUNT(*) n FROM field_assertion GROUP BY 1 ORDER BY n DESC")]
    C.write_json(C.path("evidence", "s05_assertions_summary.json"),
                 {"run_id": run_id, "extractor_version": EXTRACTOR_VERSION,
                  "assertions_total": n, "by_field": by_field})
    log.info(f"断言总数 {n:,}；分布 {[(d['field_name'], d['n']) for d in by_field]}")
    C.end_run(conn, run_id, "success", f"assertions={n}")
    conn.close()
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
