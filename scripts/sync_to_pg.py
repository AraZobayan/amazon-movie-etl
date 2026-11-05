r"""把 SQLite 里的集成层 / 目标层同步到 PostgreSQL。

## 架构选择（这个脚本就是选择的落地）

本项目的分层：
    raw / staging  →  SQLite（`data/db/etl.sqlite3`）
        理由：这两层是「一次性大吞吐写、之后只读」的批处理产物。
        SQLite 无需运维、单文件、可以直接打包成证据；7.9M 条评论的装载
        用 SQLite 的批事务比走网络连 PG 快得多，也不会因为连接中断而白跑。

    integration / target  →  同时存在 SQLite 与 PostgreSQL
        理由：这一层是**要被反复查询、被血缘追溯、被验收 SQL 访问**的。
        PostgreSQL 的窗口函数、JSONB、GIN 索引、并发读在这类查询上明显更好，
        也是课件第 44 页「建议环境 PostgreSQL + Python/SQL」指向的场景。

所以不是「二选一」，而是「按层的访问特征分工」。这个脚本负责把后半段同步过去。

## 为什么用 CSV + psql \copy 而不是驱动

本机没有 psycopg2/psycopg，装一个是可行的，但会给复现多一个依赖。
`docker exec -i ... psql -c "\copy ... FROM STDIN"` 走的是容器里已有的 psql，
零额外依赖，而且 \copy 是 PostgreSQL 最快的批量装载路径（比逐条 INSERT 快一个量级）。

## 幂等性

每次同步先 TRUNCATE 目标表（CASCADE 处理外键），再按 schema 里的建表顺序装载
（schema_postgres.sql 的顺序天然是拓扑序：父表先于子表）。
所以重复执行结果一致，不会累积重复行。

用法：
    python scripts/sync_to_pg.py                    # 同步默认层
    python scripts/sync_to_pg.py --include-review   # 连 review/reviewer 一起同步（慢但完整）
    python scripts/sync_to_pg.py --verify-only      # 只比对行数，不装载
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "db", "etl.sqlite3")
SCHEMA = os.path.join(ROOT, "sql", "schema_postgres.sql")
TMP = os.path.join(ROOT, "data", "tmp_pg")
CONTAINER = "etl-postgres"
PGUSER, PGDB = "etl", "etl"

# 集成层 + 目标层 + 元数据（必同步）
CORE = [
    "meta_etl_run", "meta_etl_step", "meta_checkpoint", "meta_quality_issue",
    "meta_eval_pair", "meta_eval_metric",
    "raw_source_file", "raw_source_record",
    "int_pair_candidate", "int_merge_decision",
    "product", "product_collection", "product_movie",
    "movie", "movie_title",
    "person", "person_name", "movie_person",
    "field_assertion", "field_candidate_link", "field_selection",
]
# 大表：默认不同步，--include-review 才带上
BIG = ["reviewer", "review", "review_mapping", "stg_review", "stg_quarantine",
       "stg_product_page", "stg_classification", "raw_review"]


def psql(sql: str, stdin: bytes | None = None, timeout: int = 1800) -> subprocess.CompletedProcess:
    cmd = ["docker", "exec", "-i", CONTAINER, "psql", "-U", PGUSER, "-d", PGDB,
           "-v", "ON_ERROR_STOP=1", "-c", sql]
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    return subprocess.run(cmd, input=stdin, capture_output=True, timeout=timeout, env=env)


def table_order() -> list[str]:
    """按 schema_postgres.sql 里 CREATE TABLE 的先后顺序返回表名。

    这个顺序就是拓扑序：DDL 里子表引用父表，所以父表一定先出现。
    按它装载就不会撞外键。
    """
    with open(SCHEMA, encoding="utf-8") as f:
        return re.findall(r"^CREATE TABLE IF NOT EXISTS (\w+)", f.read(), re.M)


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def export_csv(conn: sqlite3.Connection, table: str, path: str) -> int:
    """把一张表导成 CSV。用 QUOTE_MINIMAL + 显式空串处理 NULL。

    注意 None 必须写成 PostgreSQL 认识的 NULL：CSV 里就是**空字段**。
    这里把 None 输出为空字符串、把空字符串也输出为空字符串会有歧义，
    所以统一：None -> 空，字符串原样（由 csv 模块加引号区分）。
    """
    cols = columns(conn, table)
    sql = f"SELECT {','.join('\"' + c + '\"' for c in cols)} FROM \"{table}\""
    n = 0
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        w.writerow(cols)
        cur = conn.execute(sql)
        while True:
            rows = cur.fetchmany(20000)
            if not rows:
                break
            for r in rows:
                w.writerow(["" if v is None else v for v in r])
                n += 1
    return n


def sync_table(conn: sqlite3.Connection, table: str, cols: list[str]) -> int:
    fp = os.path.join(TMP, f"{table}.csv")
    n = export_csv(conn, table, fp)
    if n == 0:
        return 0
    # 空文件只有表头时 \copy 也能正常结束，不会报错
    with open(fp, "rb") as f:
        data = f.read()
    collist = ",".join(f'"{c}"' for c in cols)
    sql = (f"\\copy {table} ({collist}) FROM STDIN WITH (FORMAT csv, HEADER true, "
           f"NULL '', ENCODING 'UTF8')")
    r = psql(sql, stdin=data)
    if r.returncode != 0:
        raise RuntimeError(f"{table} 装载失败：{r.stderr.decode('utf-8', 'replace')[:800]}")
    return n


def pg_counts(tables: list[str]) -> dict[str, int]:
    if not tables:
        return {}
    union = " UNION ALL ".join(
        f"SELECT '{t}' AS t, COUNT(*) AS n FROM {t}" for t in tables)
    r = psql(f"SELECT t, n FROM ({union}) x ORDER BY t")
    if r.returncode != 0:
        return {}
    out = {}
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 2 and parts[0] and parts[0] not in ("t", "t "):
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--include-review", action="store_true",
                    help="同时同步 review / reviewer 等大表（完整但慢）")
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        raise SystemExit(f"库不存在：{args.db}")
    order = table_order()
    targets = [t for t in order if t in (CORE + (BIG if args.include_review else []))]
    print(f"待同步 {len(targets)} 张表（schema 顺序）")

    conn = sqlite3.connect(args.db)
    lite = {}
    for t in targets:
        try:
            lite[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            lite[t] = -1
    print("SQLite 行数：" + ", ".join(f"{t}={n:,}" for t, n in list(lite.items())[:6]) + " …")

    if args.verify_only:
        pg = pg_counts(targets)
        bad = [(t, lite[t], pg.get(t)) for t in targets if lite[t] != pg.get(t)]
        print(f"\n不一致表 {len(bad)} 张" + ("" if not bad else "："))
        for t, a, b in bad:
            print(f"  {t}: sqlite={a:,} pg={b}")
        conn.close()
        return 0 if not bad else 1

    os.makedirs(TMP, exist_ok=True)
    t0 = time.time()

    # 先清空（CASCADE 处理外键），再按拓扑序装载 —— 重复执行结果一致
    print("清空 PG 目标表…")
    r = psql("TRUNCATE " + ", ".join(targets) + " CASCADE")
    if r.returncode != 0:
        raise SystemExit(f"TRUNCATE 失败：{r.stderr.decode('utf-8', 'replace')[:600]}")

    report = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "db": os.path.relpath(args.db, ROOT).replace("\\", "/"),
              "tables": {}}
    for t in targets:
        n = lite[t]
        if n <= 0:
            report["tables"][t] = {"sqlite": n, "pg": 0, "loaded": 0}
            continue
        st = time.time()
        got = sync_table(conn, t, columns(conn, t))
        el = time.time() - st
        print(f"  {t:24s} {got:>10,} 行  {el:6.1f}s")
        report["tables"][t] = {"sqlite": n, "loaded": got, "seconds": round(el, 1)}

    print("\n校验行数…")
    pg = pg_counts(targets)
    mismatched = []
    for t in targets:
        pn = pg.get(t, 0)
        report["tables"].setdefault(t, {})["pg"] = pn
        report["tables"][t]["ok"] = (report["tables"][t].get("sqlite", 0) == pn)
        if not report["tables"][t]["ok"]:
            mismatched.append(t)
        print(f"  {t:24s} sqlite={report['tables'][t].get('sqlite', 0):>10,}  pg={pn:>10,}"
              f"  {'OK' if report['tables'][t]['ok'] else '不一致'}")

    report["mismatched"] = mismatched
    report["elapsed_seconds"] = round(time.time() - t0, 1)
    report["note"] = ("raw/staging 留在 SQLite（大批量顺序写）；"
                      "integration/target 同步到 PostgreSQL（反复查询、血缘追溯、验收 SQL）。"
                      "按层的访问特征分工，不是二选一。")
    out = os.path.join(ROOT, "evidence", "sync_pg_report.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n用时 {report['elapsed_seconds']}s，报告 -> {out}")
    print("全部一致" if not mismatched else f"有 {len(mismatched)} 张表行数不一致：{mismatched}")
    conn.close()
    return 0 if not mismatched else 1


if __name__ == "__main__":
    sys.exit(main())
