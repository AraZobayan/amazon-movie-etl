"""公共基础设施：配置、日志、数据库连接、确定性 ID、文本规范化、运行登记。

这个模块被所有步骤脚本复用。它刻意只依赖 Python 标准库，任何人 clone 下来
装个 Python 3.10+ 就能跑（不装任何第三方包）。

设计要点（对应 docs/完成过程与知识手册.md 第 3 章）：
  * 确定性 ID：所有主键都由"内容"哈希得到，同一份输入重复运行会得到同样的
    主键，所以 `INSERT ... ON CONFLICT DO NOTHING` 天然幂等。
  * 运行登记：每次运行都写 meta_etl_run / meta_etl_step，出问题能定位到步骤。
  * 单一事实来源：路径、阈值、规则版本全部来自 config/config.json。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import re
import sqlite3
import string
import sys
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Sequence

# --------------------------------------------------------------------------- #
# 路径与配置
# --------------------------------------------------------------------------- #

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config", "config.json")

with open(CONFIG_PATH, encoding="utf-8") as _f:
    CFG: dict[str, Any] = json.load(_f)

TOOL_VERSION: str = CFG["tool_version"]
RULES_VERSION: str = CFG["rules_version"]

# Windows 控制台默认不是 UTF-8，中文日志会乱码；这里强制切一次。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - 某些环境不支持 reconfigure
        pass


def path(*parts: str) -> str:
    """把相对项目根目录的路径拼成绝对路径，并确保父目录存在。"""
    p = os.path.join(ROOT, *parts)
    parent = os.path.dirname(p)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    return p


def now_iso() -> str:
    """当前 UTC 时间，ISO8601，秒级精度。数据库里所有时间都存 UTC。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id(prefix: str = "RUN") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{prefix}-{stamp}-{suffix}"


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #


class Logger:
    """同时写控制台和 data/logs/ 文件的最小日志器。

    为什么不用 logging 模块？——为了让学生一眼看懂输出，也为了日志文件能和
    某一次 run_id 一一对应（答辩时要能拿出"这次运行的日志"）。
    """

    def __init__(self, name: str, run_id: str | None = None) -> None:
        self.name = name
        self.run_id = run_id or "norun"
        self.log_path = path("data", "logs", f"{self.run_id}__{name}.log")
        self._fh = open(self.log_path, "a", encoding="utf-8")

    def _emit(self, level: str, msg: str) -> None:
        line = f"[{now_iso()}] [{level:5s}] [{self.name}] {msg}"
        print(line, flush=True)
        self._fh.write(line + "\n")
        self._fh.flush()

    def info(self, msg: str) -> None:
        self._emit("INFO", msg)

    def warn(self, msg: str) -> None:
        self._emit("WARN", msg)

    def error(self, msg: str) -> None:
        self._emit("ERROR", msg)

    def close(self) -> None:
        self._fh.close()


# --------------------------------------------------------------------------- #
# 哈希与确定性 ID
# --------------------------------------------------------------------------- #


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(fp: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(fp, "rb") as f:
        while True:
            blk = f.read(chunk)
            if not blk:
                break
            h.update(blk)
    return h.hexdigest()


def det_id(prefix: str, *parts: Any, length: int = 16) -> str:
    """确定性 ID：前缀 + 内容哈希前 length 位。

    >>> det_id("M", "the matrix", 1999) == det_id("M", "the matrix", 1999)
    True

    为什么不用自增主键？
      * 自增 ID 每次运行都不一样 -> 无法幂等重跑（会重复插入）。
      * 确定性 ID 让"同一份输入 -> 同一份主键"，重复运行是纯粹的 upsert。
    风险：哈希碰撞。16 位十六进制 = 64 bit，按生日界，1 亿个 ID 的碰撞概率
    约 2.7e-4，对本作业规模（10^6 量级）足够安全。
    """
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    return f"{prefix}-{sha256_text(raw)[:length]}"


# --------------------------------------------------------------------------- #
# 数据库
# --------------------------------------------------------------------------- #


def connect(db_path: str | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    """打开 SQLite 连接。

    WAL 模式：读写不互相阻塞（批处理时一边写一边能查）。
    foreign_keys：SQLite 默认不检查外键！必须每个连接手动打开，
                  否则"外键有效性"这项验收根本无从谈起。
    """
    p = db_path or path(CFG["db"]["path"])
    if readonly:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)
    else:
        conn = sqlite3.connect(p, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # 页缓存 256MB（负值表示 KB）。默认只有 2MB —— 库长到几十 GB 之后，
        # 每插一条都要回读 B 树中间节点，命中率几乎为零，全量跑会慢一个数量级。
        # 这个设置只影响内存占用，不影响持久化语义。
        conn.execute("PRAGMA cache_size=-262144")
        # 排序/去重用的临时表放内存，避免落到磁盘临时文件。
        conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    # 每次连接都补一次列。_migrate 是幂等的（先 PRAGMA 查已有列，缺了才 ALTER），
    # 成本是每张表一次 PRAGMA，可以忽略。放在这里而不是只放在 init_db，
    # 是因为流水线里大多数步骤是直接 connect() 打开既有库的 ——
    # 只在 init_db 补列，那些步骤会撞上 "no such column"。
    if not readonly:
        _migrate(conn)
    return conn


def init_db(db_path: str | None = None, *, drop: bool = False) -> str:
    """建库建表。drop=True 时先删掉旧库（从头复现用）。"""
    p = db_path or path(CFG["db"]["path"])
    if drop:
        for suffix in ("", "-wal", "-shm"):
            fp = p + suffix
            if os.path.exists(fp):
                os.remove(fp)
    conn = connect(p)
    with open(path("sql", "schema_sqlite.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    _migrate(conn)
    conn.close()
    return p


# 增量列：CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，
# 老库（切片版跑过的）需要这一层才不用删库重来。
_ADDED_COLUMNS = {
    "product_collection": [
        ("canonical_url", "TEXT"),
        ("asin_on_page", "TEXT"),
        ("template_sig", "TEXT"),
    ],
    # 多值字段（genres/cast）取并集时 selected_assertion_id 必须为空，
    # 而单值字段的空值表示"有候选但全被拒绝"——两者语义完全不同，
    # 不加这一列，覆盖率查询就只能靠 rule_id 名字猜。
    "field_selection": [
        ("selection_kind", "TEXT"),
    ],
}


def _migrate(conn: sqlite3.Connection) -> None:
    """幂等补列。已存在则跳过，所以每次 init_db 调用都安全。"""
    for table, cols in _ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务：要么全成功，要么全回滚。

    isolation_level=None 时 sqlite3 不会自动开事务，必须自己 BEGIN。
    批量装载时"每批一个事务"是吞吐和可恢复性的平衡点：
    批内原子，批间可续跑。
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --------------------------------------------------------------------------- #
# 运行登记
# --------------------------------------------------------------------------- #


def start_run(conn: sqlite3.Connection, pipeline: str, params: dict[str, Any] | None = None) -> str:
    run_id = new_run_id()
    conn.execute(
        "INSERT INTO meta_etl_run (run_id, pipeline, tool_version, rules_version, started_at, status, params_json)"
        " VALUES (?,?,?,?,?,?,?)",
        (run_id, pipeline, TOOL_VERSION, RULES_VERSION, now_iso(), "running",
         json.dumps(params or {}, ensure_ascii=False)),
    )
    return run_id


def end_run(conn: sqlite3.Connection, run_id: str, status: str, note: str | None = None) -> None:
    conn.execute(
        "UPDATE meta_etl_run SET finished_at=?, status=?, note=? WHERE run_id=?",
        (now_iso(), status, note, run_id),
    )


@contextlib.contextmanager
def step(conn: sqlite3.Connection, run_id: str, name: str, seq: int,
         logger: Logger | None = None) -> Iterator[dict[str, Any]]:
    """记录一个步骤的开始/结束/耗时/计数/异常。

    用法：
        with step(conn, run_id, "ingest_reviews", 1, log) as st:
            st["counters"]["in"] = 123
    """
    counters: dict[str, Any] = {}
    conn.execute(
        "INSERT OR REPLACE INTO meta_etl_step (run_id, step_name, seq, status, started_at, counters_json)"
        " VALUES (?,?,?,?,?,?)",
        (run_id, name, seq, "running", now_iso(), "{}"),
    )
    if logger:
        logger.info(f"步骤开始: {name}")
    t0 = time.time()
    box = {"counters": counters}
    try:
        yield box
    except Exception as exc:
        conn.execute(
            "UPDATE meta_etl_step SET status=?, finished_at=?, counters_json=?, error=? WHERE run_id=? AND step_name=?",
            ("failed", now_iso(), json.dumps(counters, ensure_ascii=False), f"{type(exc).__name__}: {exc}",
             run_id, name),
        )
        if logger:
            logger.error(f"步骤失败: {name} -> {type(exc).__name__}: {exc}")
        raise
    else:
        conn.execute(
            "UPDATE meta_etl_step SET status=?, finished_at=?, counters_json=? WHERE run_id=? AND step_name=?",
            ("success", now_iso(), json.dumps(counters, ensure_ascii=False), run_id, name),
        )
        if logger:
            logger.info(f"步骤完成: {name}  {time.time()-t0:.1f}s  {json.dumps(counters, ensure_ascii=False)}")


# --------------------------------------------------------------------------- #
# 文本规范化
# --------------------------------------------------------------------------- #

# 商品标题里常见的"版本噪声"。注意：这里只用于"生成匹配候选"，
# 绝不用于"判定是不是电影"——那是分类任务，规则不同（作业说明第三部分明确警告）。
_EDITION_NOISE = [
    r"\(.*?\)", r"\[.*?\]", r"\{.*?\}",
    r"\b(blu-?ray|dvd|uhd|4k|2-?disc|special edition|collector'?s edition|"
    r"widescreen|full screen|remastered|anniversary edition|unrated|"
    r"director'?s cut|extended edition|the criterion collection)\b",
    r"\b(new|sealed|import|region \d|multi-?region)\b",
]

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def norm_text(s: str | None) -> str:
    """通用规范化：全角转半角(NFKC) -> 小写 -> 去标点 -> 合并空白。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.casefold()
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def norm_title(s: str | None) -> str:
    """标题规范化：在通用规范化基础上剥掉版本噪声，得到"可比较的作品名"。

    额外处理「冠词倒装」：图书馆/音像目录习惯把冠词挪到末尾
    （"The Matrix" 写成 "Matrix, The"）。这是**同一个标题的两种写法**，
    不还原的话会被当成两部不同作品，造成漏合并。
    """
    if not s:
        return ""
    # 先去变音符号（Amélie -> Amelie）。标题匹配要的是"同一个名字"，
    # 而不是"字形完全一样"，所以重音差异不该影响判断。
    t = unicodedata.normalize("NFKD", s)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = unicodedata.normalize("NFKC", t).casefold()
    for pat in _EDITION_NOISE:
        t = re.sub(pat, " ", t, flags=re.IGNORECASE)
    # 倒装还原必须在 norm_text 之前做：norm_text 会把逗号当标点去掉，
    # 一旦去掉就再也认不出 "matrix the" 里的倒装结构了。
    m = re.match(r"^(.*?),\s*(the|a|an)\s*$", t.strip())
    if m and m.group(1).strip():
        base, art = m.group(1).strip(), m.group(2)
        # 已经以同一个冠词开头就别再加一遍，否则会出现 "the the ..."
        t = base if re.match(rf"^{art}\b", base) else f"{art} {base}"
    return norm_text(t)


def norm_person_name(s: str | None) -> str:
    """人名规范化。

    处理三件事（作业任务 5 的难点）：
      1. "Last, First" -> "First Last"
      2. 中间名缩写 "j. r. r. tolkien" -> "jrr tolkien"（去点、去空格）
      3. 变音符号 "josé" -> "jose"（NFKD 去掉组合符）
    """
    if not s:
        return ""
    t = unicodedata.normalize("NFKD", s)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = unicodedata.normalize("NFKC", t).casefold().strip()
    if "," in t:  # "Last, First Middle"
        last, _, first = t.partition(",")
        t = f"{first.strip()} {last.strip()}"
    t = re.sub(r"\b([a-z])\.", r"\1", t)          # 缩写去点
    t = re.sub(r"\b([a-z])\s+(?=[a-z]\b)", r"\1", t)  # 单个字母粘连
    t = _PUNCT_RE.sub(" ", t)
    return _WS_RE.sub(" ", t).strip()


def title_tokens(t: str) -> set[str]:
    stop = {"the", "a", "an", "of", "and", "or", "in", "on", "to", "de", "la", "le", "el"}
    return {w for w in norm_title(t).split() if w and w not in stop}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def seq_ratio(a: str, b: str) -> float:
    """difflib 的序列相似度（0~1），对拼写差异比 Jaccard 更宽容。"""
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio()


def title_similarity(a: str, b: str) -> float:
    """标题相似度 = max(词集合 Jaccard, 字符序列相似度)。

    为什么取 max 而不是加权平均？
      因为两种算法各有所长：Jaccard 抗词序调换（"Matrix, The" vs "The Matrix"
      已被 norm_title 处理），SequenceMatcher 抗单复数/拼写差异。
      取 max 让"任一条强证据"就能抬高候选分，再由后续的年份/导演做否定。
    """
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return max(jaccard(title_tokens(a), title_tokens(b)), seq_ratio(na, nb))


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def chunked(it: Iterable[Any], size: int) -> Iterator[list[Any]]:
    buf: list[Any] = []
    for x in it:
        buf.append(x)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def to_int(v: Any) -> int | None:
    try:
        if v is None or v == "":
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


def to_float(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def write_json(fp: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def write_csv(fp: str, header: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
    import csv
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    n = 0
    with open(fp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)
            n += 1
    return n
