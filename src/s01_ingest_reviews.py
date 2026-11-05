"""任务 1：全量评论抽取（原始层 -> 暂存层）。

输入：data/raw/movies.txt.gz（本仓库用可复现切片 movies.slice.gz）
输出：
    raw_source_file   源文件登记（URL / 时间 / 大小 / SHA-256 / 切片区间）
    raw_review        一字不改的原始记录
    stg_review        解析校验后的结构化记录
    stg_quarantine    隔离记录（保留原因）
    review / reviewer / review_mapping / product   目标层第一批实体

运行：
    python -m src.s01_ingest_reviews
    python -m src.s01_ingest_reviews --limit 50000     # 只跑前 5 万条（调试）
    python -m src.s01_ingest_reviews --resume          # 从检查点续跑

核心知识点见 docs/完成过程与知识手册.md 第 4 章：
  gzip 流式解压、截断文件处理、块状记录格式解析、批量事务、幂等主键、
  隔离区（quarantine）思想、统计核对恒等式。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import zlib
from datetime import datetime, timezone
from typing import Iterator

from src import common as C

# SNAP 文件的字段前缀（顺序就是文件里的顺序，但解析时不依赖顺序）
FIELDS = {
    "product/productId": "product_id",
    "review/userId": "reviewer_id",
    "review/profileName": "profile_name",
    "review/helpfulness": "helpfulness_raw",
    "review/score": "score_raw",
    "review/time": "review_time_raw",
    "review/summary": "summary_raw",
    "review/text": "review_text_raw",
}

ASIN_RE = None  # 延迟编译，见 _valid_asin


def iter_gz_lines(gz_path: str, logger: C.Logger) -> Iterator[str]:
    """流式读取 gzip 文本行。

    为什么不用 gzip.open()：
      本仓库的数据是"源文件前 N 字节"的切片，gzip 尾部 CRC/长度字段被截掉了，
      gzip 模块读到末尾会抛 EOFError，导致前面已解压的内容也拿不到。
      这里用 zlib.decompressobj(wbits=16+MAX_WBITS) 手动解压：能解多少给多少，
      截断点处抛 zlib.error 就正常收尾，并记录"数据在何处被截断"。

    这同时也演示了"流式处理"：任何时刻内存里只有一个 1MB 块 + 一行残留，
    与文件总大小无关。
    """
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    buf = b""
    consumed = 0
    with open(gz_path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            consumed += len(chunk)
            try:
                data = d.decompress(chunk)
            except zlib.error as exc:
                logger.warn(f"gzip 流在 {consumed} 字节处结束（{exc}）——按切片处理，已解压内容保留")
                data = b""
            if not data:
                break
            buf += data
            *lines, buf = buf.split(b"\n")
            for ln in lines:
                yield ln.decode("utf-8", errors="replace")
    if buf:
        yield buf.decode("utf-8", errors="replace")


def iter_records(lines: Iterator[str]) -> Iterator[tuple[int, int, dict[str, str]]]:
    """把"字段: 值"的块状文本切成一条条记录。

    返回 (记录序号, 起始行号, 字段字典)。
    格式长这样（字段之间用换行，记录之间用空行）：
        product/productId: B003AI2VGA
        review/userId: A141HP4LYPWMSR
        ...
        review/text: I loved this movie.
        <空行>

    两个坑：
      1. 值里本身可能含冒号（"review/text: 8:30 开始"），所以只能按"已知前缀 + 冒号"
         匹配，不能按第一个冒号硬切。
      2. 极少数记录的值里有裸换行（不是新字段），此时该行不匹配任何前缀，
         要接到上一个字段后面，而不是丢掉。
    """
    seq = 0
    start_line = 0
    line_no = 0
    cur: dict[str, str] = {}
    last_key: str | None = None

    for raw in lines:
        line_no += 1
        if not raw.strip():
            if cur:
                seq += 1
                yield seq, start_line, cur
                cur, last_key = {}, None
            continue

        matched = False
        for prefix, name in FIELDS.items():
            if raw.startswith(prefix + ":"):
                if not cur:
                    start_line = line_no
                cur[name] = raw[len(prefix) + 1:].strip()
                last_key = name
                matched = True
                break
        if not matched and last_key and cur:
            # 续行：拼到上一个字段
            cur[last_key] = (cur[last_key] + " " + raw.strip()).strip()

    if cur:
        seq += 1
        yield seq, start_line, cur


def _valid_asin(s: str | None) -> bool:
    """ASIN 是 Amazon 的 10 位商品编号。这里只做"看起来像"的宽松校验，
    真正严格的校验放在分类阶段（因为历史上也有非 10 位的编号）。"""
    if not s:
        return False
    s = s.strip()
    return 8 <= len(s) <= 14 and s.isalnum()


def parse_helpfulness(raw: str | None) -> tuple[int | None, int | None, str | None]:
    """"3/4" -> (3, 4, None)；异常时返回原因。

    作业要求：Helpfulness 的分子不得大于分母，票数不得为负。
    不满足条件的**保留原值**并记录异常，不能悄悄改成 0。
    """
    if raw is None or raw == "":
        return None, None, "missing_helpfulness"
    parts = raw.split("/")
    if len(parts) != 2:
        return None, None, "malformed_helpfulness"
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return None, None, "malformed_helpfulness"
    if a < 0 or b < 0:
        return a, b, "negative_votes"
    if a > b:
        return a, b, "helpful_exceeds_total"
    return a, b, None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条记录（调试用）")
    ap.add_argument("--resume", action="store_true", help="从 meta_checkpoint 续跑")
    ap.add_argument("--reset", action="store_true", help="先清空本次要写的表")
    ap.add_argument("--file", default=None,
                    help="源文件路径（相对项目根或绝对路径）。默认读 config.source.raw_file；"
                         "跑全量时用 config.source.raw_file_full 指向的完整 movies.txt.gz")
    ap.add_argument("--full", action="store_true",
                    help="等价于 --file <config.source.raw_file_full>，并清掉 is_slice 标记")
    ap.add_argument("--summary", default=None,
                    help="汇总 JSON 的输出路径（相对项目根或绝对路径）。"
                         "默认 evidence/s01_ingest_summary.json。"
                         "有界/调试运行必须改这个路径，否则会把全量证据覆盖成小规模口径。")
    args = ap.parse_args(argv)

    db = C.init_db(args.db)
    conn = C.connect(db)
    log = C.Logger("s01_ingest_reviews")

    src_cfg = C.CFG["source"]
    raw_rel = args.file or src_cfg["raw_file"]
    if args.full:
        raw_rel = src_cfg["raw_file_full"]
    raw_abs = raw_rel if os.path.isabs(raw_rel) else C.path(raw_rel)
    if not os.path.exists(raw_abs):
        log.error(f"找不到源文件 {raw_abs}，请先运行 scripts/download_prefix.py 或 "
                  f"scripts/download_full.py")
        return 2
    # 是不是切片，不靠人声明，靠字节数比对官方全量大小。
    # 这样即使忘了传 --full，落库的 is_slice 也不会说谎。
    full_size = int(src_cfg.get("full_byte_size") or 0)
    is_slice = bool(full_size and os.path.getsize(raw_abs) < full_size)

    run_id = C.start_run(conn, "s01_ingest_reviews",
                         {"file": raw_rel, "limit": args.limit, "resume": args.resume})
    log.run_id = run_id
    log.info(f"run_id={run_id}  db={db}  file={raw_rel} ({os.path.getsize(raw_abs)/1048576:.1f} MB)")

    if args.reset:
        for t in ("review_mapping", "review", "reviewer", "stg_review", "stg_quarantine",
                  "raw_review", "raw_source_record", "raw_source_file", "product"):
            conn.execute(f"DELETE FROM {t}")
        log.warn("已清空原始层/暂存层与首批目标表")

    # ---------- 1. 源文件登记：先证明"这份数据是什么" ----------
    t0 = time.time()
    file_sha = C.sha256_file(raw_abs)
    file_id = f"F-{file_sha[:16]}"
    size = os.path.getsize(raw_abs)

    # 下载时间：只认 download_full.py 落盘的 <文件>.meta.json（UTC 起止）。
    # 没有就留 NULL 并在 note 里写明"未采集"—— 绝不用当前时间或文件 mtime 冒充：
    # mtime 会被拷贝/解压改掉，它不是下载时间。
    dl_start = dl_end = None
    meta_fp = raw_abs + ".meta.json"
    if os.path.exists(meta_fp):
        try:
            with open(meta_fp, encoding="utf-8") as f:
                meta = json.load(f)
            if (meta.get("byte_size") in (None, size)
                    and meta.get("sha256") in (None, file_sha)):
                dl_start = meta.get("download_started_at")
                dl_end = meta.get("download_finished_at")
            else:
                log.warn(f"{os.path.basename(meta_fp)} 与当前文件的字节数/SHA-256 不符，下载时间不回填")
        except (OSError, ValueError) as exc:
            log.warn(f"读 {meta_fp} 失败（{exc}），下载时间按未采集处理")
    else:
        log.warn(f"没有 {os.path.basename(meta_fp)}：该源文件的下载时间未采集，"
                 f"raw_source_file.download_* 留 NULL")

    conn.execute(
        "INSERT INTO raw_source_file (file_id,name,url,download_started_at,download_finished_at,"
        "byte_size,sha256,content_type,range_start,range_end,note) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(file_id) DO NOTHING",
        (file_id, os.path.basename(raw_rel), src_cfg["url"], dl_start, dl_end, size, file_sha,
         "application/gzip", 0, size - 1,
         (f"切片 {size} 字节 / 全量 {src_cfg['full_byte_size']} 字节；"
          f"官方统计 {src_cfg['official_review_count']} 条评论")
         if is_slice else
         (f"全量源文件 {size} 字节；官方统计 {src_cfg['official_review_count']} 条评论，"
          f"range 0..{size - 1} 覆盖整个文件")),
    )
    log.info(f"源文件 SHA-256={file_sha[:16]}…  size={size}  ({time.time()-t0:.1f}s)")

    # 源文件本身也是一条"原始证据"（可用于"输入 ASIN -> 受影响字段"的反查）
    file_record_id = C.det_id("REC", "review_file", file_id)
    conn.execute(
        "INSERT OR IGNORE INTO raw_source_record (record_id,source_type,source_file_id,locator,"
        "url,snapshot_path,content_hash,fetched_at,note) VALUES (?,?,?,?,?,?,?,?,?)",
        (file_record_id, "review_file", file_id, f"bytes 0-{size-1}", src_cfg["url"],
         raw_rel, file_sha, C.now_iso(), "SNAP Amazon movie reviews（切片）"),
    )

    # ---------- 2. 断点续跑 ----------
    start_seq = 0
    if args.resume:
        row = conn.execute(
            "SELECT cursor_value, rows_done FROM meta_checkpoint"
            " WHERE pipeline=? AND step_name=? ORDER BY batch_no DESC LIMIT 1",
            ("s01_ingest_reviews", "parse"), ).fetchone()
        if row and row["cursor_value"]:
            start_seq = int(row["cursor_value"])
            log.info(f"从检查点续跑：跳过前 {start_seq} 条记录")

    # ---------- 3. 主循环：解析 + 分批装载 ----------
    batch_rows: list[tuple] = []
    batch_stg: list[tuple] = []
    batch_quar: list[tuple] = []
    batch_reviews: dict[str, list] = {}
    batch_mapping: list[tuple] = []
    batch_products: set[str] = set()
    batch_reviewers: dict[str, str] = {}

    batch_size = int(C.CFG["batch"]["reviews_per_batch"])
    commit_every = int(C.CFG["batch"]["commit_every_batches"])

    stats = {
        "records_read": 0, "valid": 0, "quarantined": 0,
        "records_skipped_resume": 0, "reviews_new": 0, "reviews_merged": 0,
        "products": set(), "reviewers": set(), "batches": 0,
    }
    reason_counter: dict[str, int] = {}

    def flush(force: bool = False) -> None:
        nonlocal batch_rows, batch_stg, batch_quar, batch_reviews, batch_mapping
        nonlocal batch_products, batch_reviewers
        if not batch_rows and not force:
            return
        with C.transaction(conn):
            conn.executemany(
                "INSERT INTO raw_review (source_review_id,source_file_id,record_seq,line_no,product_id,"
                "reviewer_id,profile_name,helpfulness_raw,score_raw,review_time_raw,summary_raw,"
                "review_text_raw,record_sha256,run_id,is_valid,quarantine_reason)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_review_id) DO NOTHING",
                batch_rows)
            conn.executemany(
                "INSERT INTO stg_review (source_review_id,product_id,reviewer_id,profile_name,"
                "helpful_votes,total_votes,helpfulness_ratio,score,review_time_unix,review_time_utc,"
                "summary,review_text,dedup_key,review_id,run_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_review_id) DO NOTHING",
                batch_stg)
            conn.executemany(
                "INSERT INTO stg_quarantine (quarantine_id,source_review_id,field_name,raw_value,reason,run_id)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT(quarantine_id) DO NOTHING",
                batch_quar)
            conn.executemany(
                "INSERT INTO product (product_id,first_run_id,first_seen_at,review_count) VALUES (?,?,?,0)"
                " ON CONFLICT(product_id) DO NOTHING",
                [(p, run_id, C.now_iso()) for p in batch_products])
            conn.executemany(
                "INSERT INTO reviewer (reviewer_id,profile_name_raw,first_seen_at) VALUES (?,?,?)"
                " ON CONFLICT(reviewer_id) DO NOTHING",
                [(r, n, C.now_iso()) for r, n in batch_reviewers.items()])
            conn.executemany(
                "INSERT INTO review (review_id,product_id,reviewer_id,review_time_utc,score,"
                "helpful_votes,total_votes,helpfulness_ratio,summary,review_text,dedup_key,raw_count,run_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?) ON CONFLICT(review_id) DO NOTHING",
                batch_reviews.values())
            conn.executemany(
                "INSERT INTO review_mapping (review_id,source_review_id) VALUES (?,?)"
                " ON CONFLICT DO NOTHING", batch_mapping)

        batch_rows, batch_stg, batch_quar, batch_mapping = [], [], [], []
        batch_reviews, batch_products, batch_reviewers = {}, set(), {}

    t1 = time.time()
    last_log = t1
    for seq, line_no, rec in iter_records(iter_gz_lines(raw_abs, log)):
        if seq <= start_seq:
            stats["records_skipped_resume"] += 1
            continue
        if args.limit and stats["records_read"] >= args.limit:
            break
        stats["records_read"] += 1

        pid = (rec.get("product_id") or "").strip()
        uid = (rec.get("reviewer_id") or "").strip()
        pname = (rec.get("profile_name") or "").strip()
        score = C.to_float(rec.get("score_raw"))
        t_unix = C.to_int(rec.get("review_time_raw"))
        hv, tv, hv_reason = parse_helpfulness(rec.get("helpfulness_raw"))

        # --- 校验：不通过就进隔离区（原始记录仍然入库） ---
        reasons: list[str] = []
        if not _valid_asin(pid):
            reasons.append("missing_or_bad_product_id")
        if score is None or not (1.0 <= score <= 5.0):
            reasons.append("score_out_of_range")
        if t_unix is None or t_unix <= 0:
            reasons.append("bad_review_time")
        if hv_reason in ("negative_votes", "helpful_exceeds_total", "malformed_helpfulness"):
            reasons.append(hv_reason)
        if not uid:
            reasons.append("missing_reviewer_id")

        sid = C.det_id("SR", file_sha, seq)
        rec_sha = C.sha256_text(json.dumps(rec, ensure_ascii=False, sort_keys=True))
        is_valid = 0 if reasons else 1
        reason = ",".join(reasons) if reasons else None

        batch_rows.append((sid, file_id, seq, line_no, pid or None, uid or None, pname or None,
                           rec.get("helpfulness_raw"), rec.get("score_raw"), rec.get("review_time_raw"),
                           rec.get("summary_raw"), rec.get("review_text_raw"), rec_sha,
                           run_id, is_valid, reason))

        if reasons:
            stats["quarantined"] += 1
            for r in reasons:
                reason_counter[r] = reason_counter.get(r, 0) + 1
            batch_quar.append((C.det_id("Q", sid, reasons[0]), sid, reasons[0],
                               str(rec.get("score_raw") or rec.get("product_id") or ""), reason, run_id))
        else:
            stats["valid"] += 1
            ratio = (hv / tv) if (hv is not None and tv) else None
            utc = datetime.fromtimestamp(t_unix, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            text = rec.get("review_text_raw") or ""
            dedup_key = C.sha256_text(f"{pid}|{uid}|{t_unix}|{score}|{text}")
            rid = C.det_id("R", dedup_key)
            batch_stg.append((sid, pid, uid, pname or None, hv, tv, ratio, score, t_unix, utc,
                              rec.get("summary_raw"), text, dedup_key, rid, run_id))
            if rid not in batch_reviews:
                batch_reviews[rid] = (rid, pid, uid, utc, score, hv, tv, ratio,
                                      rec.get("summary_raw"), text, dedup_key, run_id)
                stats["reviews_new"] += 1
            else:
                stats["reviews_merged"] += 1
            batch_mapping.append((rid, sid))
            batch_products.add(pid)
            stats["products"].add(pid)
            stats["reviewers"].add(uid)
            if uid not in batch_reviewers:
                batch_reviewers[uid] = pname or None

        if len(batch_rows) >= batch_size:
            flush()
            stats["batches"] += 1
            if stats["batches"] % commit_every == 0:
                conn.execute(
                    "INSERT OR REPLACE INTO meta_checkpoint (pipeline,step_name,batch_no,cursor_value,"
                    "rows_done,updated_at) VALUES (?,?,?,?,?,?)",
                    ("s01_ingest_reviews", "parse", stats["batches"], seq,
                     stats["records_read"], C.now_iso()))
            if time.time() - last_log > 5:
                el = time.time() - t1
                log.info(f"进度 {stats['records_read']:>9,} 条  "
                         f"{stats['records_read']/max(el,1e-9):,.0f} 条/s  "
                         f"批次 {stats['batches']}  隔离 {stats['quarantined']:,}")
                last_log = time.time()

    flush(force=True)

    # ---------- 4. 收尾：raw_count 回填 + 统计核对 ----------
    log.info("回填 review.raw_count …")
    conn.execute("""
        UPDATE review SET raw_count = (
            SELECT COUNT(*) FROM review_mapping m WHERE m.review_id = review.review_id)
        WHERE raw_count <> (SELECT COUNT(*) FROM review_mapping m WHERE m.review_id = review.review_id)
    """)
    conn.execute("""
        UPDATE product SET review_count = (
            SELECT COUNT(*) FROM review r WHERE r.product_id = product.product_id)
    """)
    conn.execute("""
        UPDATE product SET is_movie = (
            SELECT CASE WHEN c.category='movie' THEN 1
                        WHEN c.category IN ('non_movie','box_set','series','concert_other') THEN 0
                        ELSE NULL END
            FROM stg_classification c WHERE c.product_id = product.product_id)
    """)

    q = conn.execute
    n_raw = q("SELECT COUNT(*) c FROM raw_review").fetchone()["c"]
    n_valid = q("SELECT COUNT(*) c FROM raw_review WHERE is_valid=1").fetchone()["c"]
    n_quar = q("SELECT COUNT(*) c FROM raw_review WHERE is_valid=0").fetchone()["c"]
    n_qrows = q("SELECT COUNT(*) c FROM stg_quarantine").fetchone()["c"]
    n_stg = q("SELECT COUNT(*) c FROM stg_review").fetchone()["c"]
    n_review = q("SELECT COUNT(*) c FROM review").fetchone()["c"]
    n_prod = q("SELECT COUNT(*) c FROM product").fetchone()["c"]
    n_user = q("SELECT COUNT(*) c FROM reviewer").fetchone()["c"]
    n_multi = q("SELECT COUNT(*) c FROM review WHERE raw_count>1").fetchone()["c"]

    elapsed = time.time() - t1
    summary = {
        "run_id": run_id,
        "source_file": raw_rel,
        "source_sha256": file_sha,
        "source_bytes": size,
        "source_download_started_at": dl_start,
        "source_download_finished_at": dl_end,
        "source_download_time_source": ("<file>.meta.json" if dl_start
                                        else "未采集（没有 .meta.json；不用 mtime/当前时间冒充）"),
        "is_slice": is_slice,
        "elapsed_seconds": round(elapsed, 1),
        "throughput_records_per_sec": round(stats["records_read"] / max(elapsed, 1e-9)),
        "records_read": n_raw,
        # records_read 是**累计**行数（resume 运行会包含上一次已落库的部分），
        # 而 throughput 用的是**本次运行**读取条数 —— 两者口径不同，必须分开命名，
        # 否则 400,000 / 75.7s = 5,284 与 throughput 2,640 会对不上（踩过）。
        "records_read_this_run": stats["records_read"],
        "valid_records": n_valid,
        "quarantined_records": n_quar,
        "quarantine_rows": n_qrows,
        "identity_check": {
            "formula": "读取记录数 = 有效记录数 + 隔离记录数",
            "left": n_raw, "right": n_valid + n_quar, "ok": n_raw == n_valid + n_quar,
        },
        "stg_review_rows": n_stg,
        "logical_reviews": n_review,
        "logical_reviews_merged_from_multiple_raw": n_multi,
        "dedup_removed": n_valid - n_review,
        "distinct_products": n_prod,
        "distinct_reviewers": n_user,
        "quarantine_reasons": reason_counter,
        "official_stats": {
            "reviews": src_cfg["official_review_count"],
            "products": src_cfg["official_product_count"],
            "users": src_cfg["official_user_count"],
        },
        "note": ("本工作区跑的是源文件前 %d 字节的可复现切片，因此评论/产品/用户数必然小于官方统计。"
                 "这不是数据错误，而是输入范围不同；全量运行见 README。" % size
                 if is_slice else
                 "全量运行：输入为完整源文件（%d 字节），记录数应与官方统计一致。" % size),
    }
    summary_path = (args.summary if args.summary
                    else C.path("evidence", "s01_ingest_summary.json"))
    if not os.path.isabs(summary_path):
        summary_path = C.path(summary_path)
    C.write_json(summary_path, summary)
    log.info("统计核对: 读取=%d  有效=%d  隔离=%d  -> %s"
             % (n_raw, n_valid, n_quar, "通过" if n_raw == n_valid + n_quar else "不通过"))
    log.info(f"逻辑评论 {n_review:,} 条（去重合并 {n_multi:,} 条由多条原始记录产生）")
    log.info(f"不同产品 {n_prod:,}  不同用户 {n_user:,}  用时 {elapsed:.1f}s")

    C.end_run(conn, run_id, "success", f"records={n_raw}")
    conn.close()
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
