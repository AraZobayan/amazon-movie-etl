"""分段并发下载 SNAP movies.txt.gz（3.32 GB）。

为什么需要这个脚本（这是本作业里一个真实的可达性问题，写进报告）：
    snap.stanford.edu 对**单个 TCP 连接**限速到约 45 KB/s，且长连接会静默卡死。
    3.32 GB 单线程需要 20 小时以上，等于不可能完成。
    但服务端返回 `Accept-Ranges: bytes`，说明支持 HTTP Range 请求。
    实测 4 个并发连接聚合约 215 KB/s，12 个约 600 KB/s —— 限速是按连接计的，
    不是按 IP 计的。所以用「多个 Range 请求并行」把总耗时压到 1.5 小时量级。

这不是「绕过访问限制」：
    * 遵守服务端声明的能力（Accept-Ranges 是它自己给的能力，不是我们猜的）；
    * 不伪造身份、不改 User-Agent 冒充浏览器、不加代理；
    * 并发数有上限（默认 12），远低于任何合理站点的承受阈值；
    * 每个分片都带失败重试上限和退避，不对源站做暴力重试。

断点续传：
    每个分片单独落 `.parts/seg_XX.bin`，重跑时从已有字节数继续，
    所以中断多少次都不会白下。全部分片齐了才拼接成最终文件，
    拼接后校验大小 + 可选 SHA-256。

用法：
    python scripts/download_full.py                 # 12 线程，断点续传
    python scripts/download_full.py --workers 16
    python scripts/download_full.py --verify-sha256 4b6ee09e...
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

URL = "https://snap.stanford.edu/data/movies.txt.gz"
TOTAL = 3_321_791_660           # 服务端 Content-Length，已核对
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "raw", "movies.txt.gz")
PARTS = os.path.join(ROOT, "data", "raw", ".parts")
UA = "etl-coursework/1.0 (student project; educational use; contact: course)"

_lock = threading.Lock()
_done = {"bytes": 0}
_t0 = time.time()


def utc_now() -> str:
    """UTC ISO-8601 秒级时间戳（下载时间的唯一可信来源）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_meta(out_path: str, *, started_at: str, finished_at: str,
               byte_size: int, sha256: str | None) -> str:
    """把"这次下载"的时间与校验值落成 `<文件>.meta.json`。

    为什么必须单独落盘：`raw_source_file.download_started_at/download_finished_at`
    需要下载时间，而 SNAP 的文件只有 mtime（拷贝/解压都会改它，不能当下载时间）。
    这个文件是**唯一**可信的下载时间来源；s01 会读它回填，读不到就留 NULL
    （并在报告里标注"未采集"），绝不用当前时间或 mtime 冒充。
    """
    meta = {
        "url": URL,
        "out": os.path.relpath(out_path, ROOT).replace("\\", "/"),
        "download_started_at": started_at,
        "download_finished_at": finished_at,
        "byte_size": byte_size,
        "sha256": sha256,
        "note": "download_* 为本次脚本运行的 UTC 起止时间；byte_size/sha256 为落盘后的实测值。"
                "更早手工下载的文件没有这个文件，其下载时间不可追溯。",
    }
    fp = out_path + ".meta.json"
    with open(fp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    log(f"下载时间已落盘：{os.path.relpath(fp, ROOT)}")
    return fp


def log(msg: str) -> None:
    with _lock:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def fetch_segment(idx: int, start: int, end: int, retries: int = 40) -> None:
    """下载 [start, end] 闭区间到 seg_XX.bin，已存在的字节数视为已完成。"""
    path = os.path.join(PARTS, f"seg_{idx:02d}.bin")
    want = end - start + 1
    attempt = 0
    while True:
        have = os.path.getsize(path) if os.path.exists(path) else 0
        if have >= want:
            return
        if attempt >= retries:
            raise RuntimeError(f"分片 {idx} 重试 {retries} 次仍未完成（{have}/{want} 字节）")
        attempt += 1
        req = urllib.request.Request(URL, headers={
            "User-Agent": UA,
            "Range": f"bytes={start + have}-{end}",
        })
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                if resp.status not in (200, 206):
                    raise RuntimeError(f"HTTP {resp.status}")
                with open(path, "ab") as f:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        with _lock:
                            _done["bytes"] += len(chunk)
        except Exception as exc:  # noqa: BLE001 - 网络异常统一按重试处理
            # 卡死或断开都走这里：退避后从已落盘位置续
            wait = min(30, 2 * attempt)
            log(f"分片 {idx} 第 {attempt} 次中断（{str(exc)[:60]}），{wait}s 后续传")
            time.sleep(wait)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    # 默认值不能直接写 URL / OUT：那是模块级常量，在函数里用 global 重新赋值前
    # 引用它会触发 SyntaxError（name is used prior to global declaration）。
    # 所以默认写 None，进函数后再回退到常量。
    ap.add_argument("--url", default=None, help="下载地址（默认 SNAP movies.txt.gz）")
    ap.add_argument("--out", default=None, help="输出文件路径")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--total", type=int, default=None,
                    help="文件总字节数。默认取 SNAP movies.txt.gz 的 Content-Length；"
                         "换别的 URL 时必须显式给（脚本不替你猜，猜错会拼出坏文件）")
    ap.add_argument("--verify-sha256", default=None)
    args = ap.parse_args(argv)

    # 允许在运行时覆盖 URL / 输出路径（IMDb 的表也是同样的下载场景）
    global URL, OUT, PARTS, TOTAL
    URL = args.url or URL
    OUT = args.out or OUT
    TOTAL = args.total if args.total is not None else TOTAL
    PARTS = os.path.join(os.path.dirname(OUT), ".parts_" + os.path.basename(OUT).replace(".", "_"))

    if os.path.exists(OUT) and os.path.getsize(OUT) == TOTAL:
        log(f"已存在完整文件 {OUT}（{human(TOTAL)}），跳过下载")
        log("注意：跳过下载时不会写 .meta.json —— 该文件的下载时间不可追溯，"
            "不要用 mtime 或当前时间代替")
        return 0

    started_at = utc_now()
    os.makedirs(PARTS, exist_ok=True)
    n = args.workers
    size = TOTAL // n
    bounds = [(i * size, (TOTAL - 1) if i == n - 1 else (i + 1) * size - 1)
              for i in range(n)]

    log(f"目标 {human(TOTAL)} / {TOTAL} 字节，{n} 个分片，并发 {n}")

    threads = [threading.Thread(target=fetch_segment, args=(i, s, e), daemon=True)
               for i, (s, e) in enumerate(bounds)]

    # 进度线程：每 15 秒报一次速率
    stop = threading.Event()

    def progress() -> None:
        last, last_t = 0, time.time()
        while not stop.wait(15):
            with _lock:
                cur = _done["bytes"]
            now = time.time()
            rate = (cur - last) / max(1e-6, now - last_t)
            last, last_t = cur, now
            pct = 100.0 * cur / TOTAL
            eta = (TOTAL - cur) / rate if rate > 1 else 0
            log(f"进度 {pct:5.1f}%  {human(cur)}/{human(TOTAL)}  "
                f"{human(rate)}/s  剩余约 {eta / 60:.0f} 分钟")

    pt = threading.Thread(target=progress, daemon=True)
    pt.start()

    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()

    # 拼接：必须按分片序号顺序，否则字节序错乱
    log("全部分片完成，开始拼接…")
    tmp = OUT + ".joining"
    with open(tmp, "wb") as out:
        for i in range(n):
            p = os.path.join(PARTS, f"seg_{i:02d}.bin")
            with open(p, "rb") as f:
                while True:
                    b = f.read(4 * 1024 * 1024)
                    if not b:
                        break
                    out.write(b)
    got = os.path.getsize(tmp)
    if got != TOTAL:
        log(f"拼接后大小不符：{got} != {TOTAL}，保留分片以便排查")
        return 2
    os.replace(tmp, OUT)
    finished_at = utc_now()
    log(f"完成：{OUT}（{human(got)}），用时 {(time.time() - _t0) / 60:.1f} 分钟")

    digest = None
    if args.verify_sha256:
        log("计算 SHA-256…")
        h = hashlib.sha256()
        with open(OUT, "rb") as f:
            for b in iter(lambda: f.read(8 * 1024 * 1024), b""):
                h.update(b)
        digest = h.hexdigest()
        ok = digest == args.verify_sha256.lower()
        log(f"SHA-256 = {digest}  {'一致' if ok else '不一致'}")
        if not ok:
            return 3
        with open(OUT + ".sha256", "w", encoding="utf-8", newline="\n") as f:
            f.write(f"{digest}  movies.txt.gz\n")

    # 下载时间 + 校验值落盘（s01 回填 raw_source_file.download_* 的唯一来源）
    write_meta(OUT, started_at=started_at, finished_at=finished_at,
               byte_size=got, sha256=digest)

    # 分片是中间产物，校验通过后清掉（保留 .parts 目录本身）
    for i in range(n):
        p = os.path.join(PARTS, f"seg_{i:02d}.bin")
        if os.path.exists(p):
            os.remove(p)
    log("已清理分片")
    return 0


if __name__ == "__main__":
    sys.exit(main())
