"""第十部分：把作业要求的 9 个查询跑起来并导出结果。

5 个"验收查询" + 4 个"血缘查询"，SQL 都存在 sql/queries/*.sql 里
（单独成文件是为了方便教师现场执行、也方便答辩时改参数重跑）。

本脚本负责：
  * 从库里挑出有代表性的参数（不是硬编码）；
  * 执行查询、导出 CSV 到 evidence/queries/；
  * 生成一份 Markdown 摘要 evidence/queries/查询结果.md，可直接贴进报告。

运行：python -m src.s11_queries
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys

from src import common as C

QUERIES = [
    ("q1", "q1_versions_of_movie.sql", "验收1 作品的所有商品版本", "movie_id"),
    ("q2", "q2_director_works.sql", "验收2 某导演的作品及来源", "person_name"),
    ("q3", "q3_missing_release.sql", "验收3 缺失与推断的上映信息", None),
    ("q4", "q4_conflicts.sql", "验收4 字段冲突与选择理由", None),
    ("q5", "q5_reviews_by_movie.sql", "验收5 按作品统计评论数与平均分", None),
    ("l1", "l1_field_lineage.sql", "血缘1 字段最终值 + 证据 + 理由", "movie_id"),
    ("l1b", "l1b_candidates.sql", "血缘1b 该字段的全部候选（含未采用）", "movie_id"),
    ("l2", "l2_asin_trace.sql", "血缘2 ASIN -> 版本/作品/匹配状态", "product_id"),
    ("l3", "l3_source_impact.sql", "血缘3 源记录 -> 受影响实体与字段", "record_id"),
    ("l4", "l4_person_lineage.sql", "血缘4 人物 -> 署名/别名/作品", "person_id"),
]


def pick_params(conn) -> dict:
    """挑出有代表性的示例参数。"""
    p: dict = {"limit": 12, "field_name": "release_date"}

    # 优先挑「真的有上映日期最终值」且商品版本最多的作品。
    # 为什么不能只按版本数排：那样可能选中一部只有 physical 发售日、
    # 上映日期最终值为 NULL 的作品，于是旗舰血缘查询 l1 会返回 0 行 ——
    # 查询没错，是样例参数没代表性。演示用的样例必须自己先满足演示条件。
    row = conn.execute("""
        SELECT pm.movie_id, COUNT(*) n
          FROM product_movie pm
          JOIN field_selection fs
            ON fs.entity_type='movie' AND fs.entity_id=pm.movie_id
           AND fs.field_name='release_date' AND fs.selected_assertion_id IS NOT NULL
         GROUP BY pm.movie_id
         ORDER BY n DESC, pm.movie_id LIMIT 1""").fetchone()
    if not row:      # 兜底：全库都没有选中的上映日期时，退回版本数最多的一部
        row = conn.execute("""
            SELECT pm.movie_id, COUNT(*) n FROM product_movie pm
            GROUP BY pm.movie_id ORDER BY n DESC, pm.movie_id LIMIT 1""").fetchone()
    p["movie_id"] = row["movie_id"] if row else None

    row = conn.execute("""
        SELECT p.canonical_name_norm FROM person p
        JOIN movie_person mp ON mp.person_id=p.person_id
        GROUP BY p.person_id ORDER BY COUNT(*) DESC, p.person_id LIMIT 1""").fetchone()
    p["person_name"] = ("%" + row["canonical_name_norm"].split()[-1] + "%") if row else "%"

    row = conn.execute("""
        SELECT product_id FROM product_movie
        ORDER BY (SELECT review_count FROM product WHERE product_id=product_movie.product_id) DESC
        LIMIT 1""").fetchone()
    p["product_id"] = row["product_id"] if row else None

    # 源记录示例参数：优先取「真的被最终采用过」的第三方来源，
    # 这样 L3 查询能展示「一个来源影响到了哪些最终字段」；
    # 若没有任何第三方来源被采用（例如补全全部失败），退一步取「有断言的最大来源」，
    # 保证查询仍可执行、仍能演示血缘链路。
    p["record_id"] = None
    for sql in (
        """SELECT fa.source_record_id AS rid FROM field_selection fs
             JOIN field_assertion fa ON fa.assertion_id=fs.selected_assertion_id
            WHERE fs.entity_type='movie' AND fs.field_name='release_date'
              AND fa.source_record_id LIKE 'REC-%' LIMIT 1""",
        """SELECT fa.source_record_id AS rid FROM field_assertion fa
            WHERE fa.source_record_id LIKE 'REC-%'
            GROUP BY fa.source_record_id ORDER BY COUNT(*) DESC LIMIT 1""",
        """SELECT fa.source_record_id AS rid FROM field_assertion fa
            GROUP BY fa.source_record_id ORDER BY COUNT(*) DESC LIMIT 1""",
    ):
        row = conn.execute(sql).fetchone()
        if row and row["rid"]:
            p["record_id"] = row["rid"]
            break

    row = conn.execute("""
        SELECT person_id FROM movie_person GROUP BY person_id
        ORDER BY COUNT(*) DESC, person_id LIMIT 1""").fetchone()
    p["person_id"] = row["person_id"] if row else None

    # L2 的另一个输入口：原始评论记录 ID。演示时保持 NULL（走 ASIN 那条路），
    # 但把它放进参数表，答辩现场换成任意 SR-… 即可演示"从原始记录反查"。
    p["source_review_id"] = None
    return p


_PLACEHOLDER_RE = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")

# 允许为 NULL 的占位符：L2 的 :source_review_id 是"二选一"输入，
# 演示时用 ASIN 那条路，评论记录 ID 传 NULL（SQL 里用 COALESCE 取非空的那个）。
OPTIONAL_PARAMS = {"source_review_id"}


def sql_placeholders(sql: str) -> set[str]:
    """从 SQL 文本里提取所有 :name 形式的绑定参数名。

    先去掉 -- 行注释，避免注释里提到的参数被误当成真的占位符。
    """
    body = "\n".join(line.split("--")[0] for line in sql.splitlines())
    return set(_PLACEHOLDER_RE.findall(body))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    conn = C.connect(args.db, readonly=True)
    log = C.Logger("s11_queries")
    params = pick_params(conn)
    log.info(f"示例参数: { {k: v for k, v in params.items() if v} }")

    out_dir = C.path("evidence", "queries")
    os.makedirs(out_dir, exist_ok=True)
    md = ["# 查询结果（自动生成）", "",
          f"数据库：`{C.CFG['db']['path']}`　生成时间：{C.now_iso()}", "",
          "示例参数：", "", "```json",
          __import__("json").dumps({k: v for k, v in params.items() if v}, ensure_ascii=False, indent=2),
          "```", ""]

    ok = 0
    for key, fname, title, need in QUERIES:
        sql_fp = C.path("sql", "queries", fname)
        with open(sql_fp, encoding="utf-8") as f:
            sql = f.read()
        # 参数绑定直接从 SQL 里"读"出来，而不是靠人工在 QUERIES 里声明。
        # 原因：一个查询可能用到多个占位符（l1 同时要 movie_id 和 field_name），
        # 靠声明去维护迟早会漏；从 SQL 扫一遍则永远不会漂移。
        used = sql_placeholders(sql)
        missing = sorted(k for k in used
                         if params.get(k) is None and k not in OPTIONAL_PARAMS)
        if missing:
            log.warn(f"{key} 跳过：缺少示例参数 {missing}（该查询需要它才能演示）")
            continue
        use = {k: params.get(k) for k in used}
        try:
            cur = conn.execute(sql, use)
            rows = [dict(r) for r in cur.fetchall()]
            cols = [d[0] for d in cur.description] if cur.description else []
        except Exception as exc:
            log.error(f"{key} 执行失败: {exc}")
            md += [f"## {title}", "", f"**执行失败**：`{exc}`", ""]
            continue

        ok += 1
        csv_fp = os.path.join(out_dir, f"{key}.csv")
        C.write_csv(csv_fp, cols, ([r[c] for c in cols] for r in rows))
        md += [f"## {title}", "",
               f"`sql/queries/{fname}`　返回 **{len(rows)}** 行", ""]
        if rows:
            md += ["| " + " | ".join(cols) + " |",
                   "|" + "|".join(["---"] * len(cols)) + "|"]
            for r in rows[:10]:
                md.append("| " + " | ".join("" if r[c] is None else str(r[c])[:40] for c in cols) + " |")
            if len(rows) > 10:
                md.append(f"| … | 共 {len(rows)} 行，完整结果见 `{key}.csv` |" + " |" * (len(cols) - 2))
        md.append("")
        log.info(f"{key} {title}: {len(rows)} 行 -> {csv_fp}")

    with open(os.path.join(out_dir, "查询结果.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(md))
    log.info(f"共 {ok}/{len(QUERIES)} 个查询成功，摘要 -> {os.path.join(out_dir, '查询结果.md')}")
    conn.close()
    log.close()
    return 0 if ok == len(QUERIES) else 1


if __name__ == "__main__":
    sys.exit(main())
