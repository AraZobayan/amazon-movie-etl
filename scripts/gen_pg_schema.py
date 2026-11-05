r"""把 SQLite 版 DDL 机械转换成 PostgreSQL 版 DDL，保证两份结构永不漂移。

为什么要有这个脚本：
    一个模型两套数据库是常见需求（本地跑 SQLite、生产用 PostgreSQL）。
    手写两份 DDL 必然会漂移（改了 A 忘了 B）。这里用"单一事实来源 + 机械转换"
    的办法：schema_sqlite.sql 是唯一事实来源，PG 版由它生成。

用法:
    python scripts/gen_pg_schema.py            # 生成 sql/schema_postgres.sql

注意：改完 schema_sqlite.sql 必须重跑一次本脚本，否则 PG 版会落后。
落后的表现不是静默丢数据，而是 `sync_to_pg.py` 的 `\copy` 直接报列不存在 ——
宁可报错，也不要悄悄少同步一列。
"""
from __future__ import annotations

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "sql", "schema_sqlite.sql")
DST = os.path.join(ROOT, "sql", "schema_postgres.sql")

TYPE_MAP = [
    (r"\bREAL\b", "DOUBLE PRECISION"),
    (r"\bINTEGER\b", "INTEGER"),
    (r"\bTEXT\b", "TEXT"),
]


def convert(sql: str) -> str:
    out_lines = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("PRAGMA"):
            continue
        new = line
        for pat, rep in TYPE_MAP:
            new = re.sub(pat, rep, new)
        out_lines.append(new)
    body = "\n".join(out_lines)
    header = (
        "-- =============================================================================\n"
        "--  自动生成，请勿手改。事实来源：sql/schema_sqlite.sql\n"
        "--  生成命令：python scripts/gen_pg_schema.py\n"
        "--  PostgreSQL 版本：长度/精度/时区等生产级细节（timestamptz、varchar 长度、\n"
        "--  分区、并行装载）在 docs/项目报告.md 的「上生产环境要改什么」一节说明。\n"
        "-- =============================================================================\n\n"
    )
    return header + body


def main() -> int:
    with open(SRC, encoding="utf-8") as f:
        sql = f.read()
    with open(DST, "w", encoding="utf-8", newline="\n") as f:
        f.write(convert(sql))
    print(f"[ok] {DST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
