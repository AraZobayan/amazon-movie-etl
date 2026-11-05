"""按 Range 下载源文件的一个字节区间（切片），用于在低带宽环境下取得可复现的真实样本。

用法:
    python scripts/download_prefix.py --bytes 220000000 --out data/raw/movies.slice.gz

设计要点（见 docs 手册第 2 章）:
  * 使用 HTTP Range 请求，只取文件前 N 字节；
  * gzip 是流式格式，截断的 gzip 仍然可以解压出截断点之前的全部内容；
  * 记录切片区间与 SHA-256，作为"这份数据到底是什么"的证据。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
import urllib.request

URL = "https://snap.stanford.edu/data/movies.txt.gz"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bytes", type=int, default=220_000_000, help="要下载的字节数")
    ap.add_argument("--out", default="data/raw/movies.slice.gz")
    ap.add_argument("--url", default=URL)
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    if os.path.exists(args.out) and os.path.getsize(args.out) >= args.bytes:
        print(f"[skip] 已存在且足够大: {args.out} ({os.path.getsize(args.out)} bytes)")
        return 0

    req = urllib.request.Request(
        args.url,
        headers={"Range": f"bytes=0-{args.bytes - 1}", "User-Agent": "etl-coursework/1.0"},
    )
    t0 = time.time()
    h = hashlib.sha256()
    got = 0
    with urllib.request.urlopen(req, timeout=60) as r, open(args.out, "wb") as f:
        total = args.bytes
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            got += len(chunk)
            if got % (10 << 20) < (1 << 20):
                el = time.time() - t0
                print(
                    f"[dl] {got/1048576:7.1f} MB / {total/1048576:.0f} MB  "
                    f"{got/1048576/max(el,1e-9):.2f} MB/s  eta {(total-got)/1048576/max(got/1048576/max(el,1e-9),1e-9)/60:.1f} min",
                    flush=True,
                )
    print(f"[done] {got} bytes -> {args.out}")
    print(f"[sha256] {h.hexdigest()}")
    with open(args.out + ".sha256", "w", encoding="utf-8") as f:
        f.write(f"{h.hexdigest()}  {os.path.basename(args.out)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
