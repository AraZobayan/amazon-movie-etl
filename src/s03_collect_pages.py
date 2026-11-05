"""任务 2（下半）：产品页面采集 + 状态机。

设计要点（对应作业说明第四部分任务 2、以及手册第 6 章）：

1. **先读 robots.txt 再发请求**。本仓库实测：amazon.com 的 robots.txt 允许
   `User-agent: *` 访问 `/dp/<ASIN>`，只禁了 `/dp/shipping/` 这类子路径。
   所以"合规"不等于"抓不到"，两件事要分开判断。

2. **状态机而不是布尔值**。作业要求状态区分：
   success / not_found / access_restricted / timeout / parse_failed / not_collected / pending
   并且"HTTP 200 的验证页或错误页不得标记为采集成功"。

3. **每种状态都要有证据**。失败也写 reason_code + reason_detail，
   原始响应（哪怕是 404 的错误页）照样落盘并算哈希。

4. **来源类型分开记**。source_type ∈ {live, offline_snapshot, third_party, synthetic_fixture}
   作业明确要求"离线快照、第三方补充与实时采集分别统计，不得混为同一成功指标"。

5. **断点续跑**。每采完一个产品立刻提交，进程被杀也不丢已完成的进度。

运行：
    python -m src.s03_collect_pages --live-sample 8      # 真实请求若干条
    python -m src.s03_collect_pages --offline-only       # 只跑离线快照（不联网）
    python -m src.s03_collect_pages --no-fixtures        # 只用真实来源，忽略夹具
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
import urllib.robotparser
from datetime import datetime, timedelta, timezone

from src import common as C

ROBOTS_PATH = "data/snapshots/robots.txt"
ROBOTS_META = "data/snapshots/robots.meta.json"
TERMINAL = ("success", "not_found", "access_restricted", "timeout", "parse_failed")


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #


def load_robots(log: C.Logger) -> tuple[urllib.robotparser.RobotFileParser, str]:
    """读取并缓存 robots.txt。返回 (解析器, 内容哈希)。

    缓存 7 天。缓存本身也是证据：它记录了"我们按什么规则决定要不要抓"。
    """
    fp = C.path(ROBOTS_PATH)
    meta_fp = C.path(ROBOTS_META)
    fresh = False
    if os.path.exists(fp) and os.path.exists(meta_fp):
        with open(meta_fp, encoding="utf-8") as f:
            meta = json.load(f)
        fetched = datetime.fromisoformat(meta["fetched_at"].replace("Z", "+00:00"))
        fresh = datetime.now(timezone.utc) - fetched < timedelta(days=7)

    if not fresh:
        url = "https://www.amazon.com/robots.txt"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": C.CFG["collect"]["user_agent"]})
            body = urllib.request.urlopen(req, timeout=20).read()
            with open(fp, "wb") as f:
                f.write(body)
            with open(meta_fp, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"url": url, "fetched_at": C.now_iso(), "byte_size": len(body),
                           "sha256": C.sha256_bytes(body)}, f, ensure_ascii=False, indent=2)
            log.info(f"robots.txt 已获取 {len(body)} 字节")
        except Exception as exc:
            log.warn(f"robots.txt 获取失败（{exc}），按「保守策略」处理：视为不允许抓取")
            rp = urllib.robotparser.RobotFileParser()
            rp.disallow_all = True
            return rp, ""

    with open(fp, encoding="utf-8", errors="replace") as f:
        txt = f.read()
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(txt.splitlines())
    return rp, C.sha256_text(txt)


# --------------------------------------------------------------------------- #
# 页面类型判别：HTTP 200 不等于"抓到了产品页"
# --------------------------------------------------------------------------- #

CAPTCHA_PAT = re.compile(r"api-services-support@amazon\.com|Enter the characters you see below|"
                         r"/errors/validateCaptcha|not a robot", re.I)
NOTFOUND_PAT = re.compile(r"Page Not Found|Looking for something\?|"
                          r"the Web address you entered is not a functioning page", re.I)
REDIRECT_PAT = re.compile(r"This item is no longer available|Showing a related item", re.I)

# --------------------------------------------------------------------------- #
# 页面身份核对：canonical 链接是 2026 年真实页面唯一可靠的"我是谁"证据
#
# 实测（2026-11 抓取 B000053VCW 等 9 个真实 ASIN）：
#   - "ASIN" 这个词在服务端 HTML 里出现 0 次，旧正则（"ASIN":"..." / name="ASIN"）
#     在真实页面上全部返回 None —— 那些标记只存在于教学夹具里。
#   - 真正的 ASIN 藏在 <link rel="canonical" href=".../dp/B000053VCW"/> 里。
#   - canonical 的路径不一定是 /dp/：实测 B00005JLF1 的 canonical 是
#     /clp/B00005JLF1（类目落地页），HTTP 200、318KB、标题看着完全正常，
#     但它不是产品详情页 —— 正是作业说的"HTTP 200 的错误页不得标记为成功"。
# --------------------------------------------------------------------------- #

CANONICAL_TAG_PAT = re.compile(r'<link[^>]+rel=["\']canonical["\'][^>]*>', re.I)
HREF_PAT = re.compile(r'href=["\']([^"\']+)["\']', re.I)
ASIN_IN_URL_PAT = re.compile(r"/(dp|gp/product|clp)/([A-Z0-9]{10})")

# 模板指纹只看"结构标记在不在"，不看内容，所以页面内容更新不会误报模板变化。
TEMPLATE_MARKERS = (
    "dp-container", "productTitle", "bylineInfo",
    "detailBullets_feature_div", "nav-subnav", "buybox",
)


def parse_canonical(body: str) -> str | None:
    """取 <link rel="canonical"> 的 href。"""
    m = CANONICAL_TAG_PAT.search(body)
    if not m:
        return None
    h = HREF_PAT.search(m.group(0))
    return h.group(1) if h else None


def page_identity(body: str) -> tuple[str | None, str, str | None]:
    """返回 (canonical_url, path_kind, asin_on_page)。

    path_kind:
      dp     —— canonical 是 /dp/<ASIN>，产品详情页
      clp    —— canonical 是 /clp/<ASIN>，类目落地页（不是产品页）
      gp     —— canonical 是 /gp/product/<ASIN>，旧版详情页
      other  —— 有 canonical 但认不出 ASIN
      legacy —— 没有 canonical，但命中旧版隐藏字段（兼容教学夹具）
      none   —— 什么都没有
    """
    can = parse_canonical(body)
    if can:
        m = ASIN_IN_URL_PAT.search(can)
        if m:
            return can, m.group(1), m.group(2)
        return can, "other", None
    for pat in (r'"ASIN"\s*:\s*"([A-Z0-9]{10})"',
                r'name="ASIN"\s+value="([A-Z0-9]{10})"',
                r'id="ASIN"\s+value="([A-Z0-9]{10})"'):
        m = re.search(pat, body)
        if m:
            return None, "legacy", m.group(1)
    return None, "none", None


def parse_asin_on_page(body: str, target: str | None = None) -> str | None:
    """页面自报的 ASIN（优先 canonical）。"""
    return page_identity(body)[2]


def template_signature(body: str) -> str:
    """模板指纹：命中的结构标记集合的短哈希，形如 tpl:ab12cd34ef56/4of6。"""
    hit = [m for m in TEMPLATE_MARKERS if m in body]
    return "tpl:" + C.sha256_text("|".join(hit))[:12] + f"/{len(hit)}of{len(TEMPLATE_MARKERS)}"


def classify_response(http_status: int, body: str, target_asin: str | None = None
                      ) -> tuple[str, str, str]:
    """把 (HTTP 状态码, 响应体, 目标 ASIN) 映射成 (业务状态, 原因码, 证据说明)。

    这是本步骤最容易写错的地方：只看状态码会把"验证页/落地页(200)"当成成功。
    """
    if http_status == 404:
        return "not_found", "http_404", "服务器返回 404"
    if http_status in (403, 429):
        return "access_restricted", f"http_{http_status}", "站点拒绝访问或限流"
    if http_status == 503:
        return "access_restricted", "http_503", "服务不可用（通常是反爬前置）"
    if http_status >= 400:
        return "parse_failed", f"http_{http_status}", f"未预期的 HTTP {http_status}"
    if CAPTCHA_PAT.search(body):
        return "access_restricted", "captcha_page", "返回验证码页，按作业要求不得标记为成功"
    if NOTFOUND_PAT.search(body):
        return "not_found", "soft_404", "HTTP 200 但内容是「页面不存在」"
    if len(body) < 2000:
        return "parse_failed", "suspiciously_small_body", f"响应体仅 {len(body)} 字节，不像产品页"

    can, kind, asin = page_identity(body)
    hits = [m for m in TEMPLATE_MARKERS if m in body]
    n_hits = len(hits)
    struct = f"结构标记 {n_hits}/{len(TEMPLATE_MARKERS)}"

    # ---- 结构证据 ----
    # 为什么不能只靠 URL 形态判断：实测 B00004VYPW 的 canonical 是
    #   https://www.amazon.com/clp/B00004VYPW
    # 而页面本身是 1.2 MB、6/6 结构标记齐全、<title> 为
    #   "Amazon.com: Face/Off : Movies & TV"
    # 的完整产品详情页。Amazon 对一批老 ASIN 的 canonical 就是用 /clp/ 形态的。
    # 早先按 "path 是 clp 就判失败" 的规则，会把这一整批真实产品页误判成落地页。
    # 所以判据改成两条都要：身份对得上（canonical 里有 ASIN）**且**结构像产品页。
    looks_like_pdp = ("productTitle" in body
                      and ("dp-container" in body or "bylineInfo" in body)
                      and n_hits >= 3)

    # 身份核对放在最前面：canonical 能解析出 ASIN 且与目标不符，是最硬的重定向证据，
    # 无论页面长什么样都不能算成功（作业要求核对返回页面与目标 ASIN 的对应关系）。
    if asin and target_asin and asin != target_asin:
        return ("parse_failed", "redirected_to_other_item",
                f"canonical 中的 ASIN={asin} 与目标 {target_asin} 不符（重定向到其它商品）")

    if looks_like_pdp:
        if asin:
            return ("success", "http_200_product_page",
                    f"canonical={can}（路径形态 {kind}），ASIN 与目标一致，{struct}")
        # 没有 canonical 但结构完整：成功，但原因码单列，
        # 让下游一眼看出这条的身份证据比 canonical 核对弱。
        return ("success", "http_200_product_page_title_verified",
                f"无 canonical 链接；{struct}，标题为产品页形态，身份仅由结构与标题佐证")

    # ---- 到这里说明页面结构不像产品页，才轮到「落地页 / 错误页」的解释 ----
    if kind == "legacy":
        return "success", "http_200_product_page", "旧版模板，ASIN 来自隐藏字段"
    if kind == "clp":
        return ("parse_failed", "landing_page_not_pdp",
                f"canonical={can} 指向类目页，且页面缺少产品页结构（{struct}）")
    if kind in ("dp", "gp"):
        return ("parse_failed", "pdp_url_without_structure",
                f"canonical={can} 形如产品页，但缺少产品页结构（{struct}），"
                f"疑似错误页或模板变更")
    if kind == "other":
        return ("parse_failed", "canonical_without_asin",
                f"有 canonical 但解析不出 ASIN：{can}")
    if REDIRECT_PAT.search(body):
        return "parse_failed", "redirected_to_other_item", "页面提示已跳转到相关商品"
    return ("parse_failed", "no_canonical_link",
            "HTTP 200 但既无 canonical 也无隐藏 ASIN，且缺少产品页结构"
            f"（{struct}），无法确认是目标产品页")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--live-sample", type=int, default=None, help="真实请求的条数上限")
    ap.add_argument("--offline-only", action="store_true")
    ap.add_argument("--no-fixtures", action="store_true", help="不使用合成夹具")
    ap.add_argument("--retry-failed", action="store_true", help="重试非成功状态")
    ap.add_argument("--reclassify-cache", action="store_true",
                    help="只用已落盘的快照重新分类，不联网。改了分类规则后用它回溯修正历史判定")
    args = ap.parse_args(argv)

    cfg = C.CFG["collect"]
    conn = C.connect(args.db)
    log = C.Logger("s03_collect_pages")
    run_id = C.start_run(conn, "s03_collect_pages", vars(args))
    log.run_id = run_id

    snap_dir = C.path("data", "snapshots", "pages")
    os.makedirs(snap_dir, exist_ok=True)

    rp, robots_hash = load_robots(log)
    ua = cfg["user_agent"]
    robots_record = C.det_id("REC", "robots", robots_hash)
    conn.execute(
        "INSERT OR IGNORE INTO raw_source_record (record_id,source_type,source_file_id,locator,url,"
        "snapshot_path,content_hash,fetched_at,note) VALUES (?,?,?,?,?,?,?,?,?)",
        (robots_record, "third_party", None, "robots.txt", "https://www.amazon.com/robots.txt",
         ROBOTS_PATH, robots_hash, C.now_iso(), "访问规则证据：决定是否允许抓取 /dp/"))

    # 取待处理清单：按评论数降序，优先处理"重要"产品
    where = "c.status NOT IN ('success','not_found','access_restricted','timeout','parse_failed')" \
        if not args.retry_failed else "1=1"
    todo = [dict(r) for r in conn.execute(
        f"SELECT c.product_id, c.original_url, c.status, p.review_count"
        f" FROM product_collection c LEFT JOIN product p USING (product_id)"
        f" WHERE {where} ORDER BY p.review_count DESC, c.product_id")]
    log.info(f"待处理产品 {len(todo):,} 个；robots 允许 /dp/ ? "
             f"{rp.can_fetch(ua, '/dp/' + (todo[0]['product_id'] if todo else 'X'))}")

    stats = {"live_attempted": 0, "live_success": 0, "live_cached": 0, "offline_loaded": 0,
             "not_collected": 0, "skipped_robots": 0}
    by_status: dict[str, int] = {}

    def record(pid: str, status: str, *, http_status=None, attempts=1, final_url=None,
               snapshot=None, content_hash=None, source_type=None, reason_code=None,
               reason_detail=None, canonical_url=None, asin_on_page=None,
               template_sig=None) -> None:
        conn.execute(
            "UPDATE product_collection SET status=?, http_status=?, attempts=?, fetched_at=?,"
            " final_url=COALESCE(?,final_url), snapshot_path=?, content_hash=?, source_type=?,"
            " reason_code=?, reason_detail=?, canonical_url=COALESCE(?,canonical_url),"
            " asin_on_page=COALESCE(?,asin_on_page), template_sig=COALESCE(?,template_sig),"
            " run_id=? WHERE product_id=?",
            (status, http_status, attempts, C.now_iso(), final_url, snapshot, content_hash,
             source_type, reason_code, reason_detail, canonical_url, asin_on_page,
             template_sig, run_id, pid))
        by_status[status] = by_status.get(status, 0) + 1

    # ---------------- 回溯分类：用已有快照重判，不联网 ----------------
    # 分类规则是会改的（比如发现 /clp/ 形态也可能是真产品页之后）。
    # 改完规则如果只能靠重抓来修正历史判定，既浪费请求又不可复现。
    # 这个分支只读 data/snapshots/pages/live/*.html，重新跑一遍 classify_response，
    # 把判定和原因码覆盖回 product_collection。快照本身就是证据，重判不动证据。
    if args.reclassify_cache:
        cand = [dict(r) for r in conn.execute(
            "SELECT product_id, status, reason_code FROM product_collection"
            " WHERE status IN ('parse_failed','not_found','access_restricted','success')"
            "   AND source_type='live'")]
        changed = 0
        log.info(f"回溯分类：待检查 {len(cand)} 个已有实时快照的产品")
        for item in cand:
            pid = item["product_id"]
            fp = os.path.join(snap_dir, "live", f"{pid}.html")
            if not os.path.exists(fp):
                continue
            with open(fp, encoding="utf-8", errors="replace") as f:
                body = f.read()
            st, code, detail = classify_response(200, body, pid)
            can_url, _, asin_seen = page_identity(body)
            if (st, code) != (item["status"], item["reason_code"]):
                changed += 1
                log.info(f"  {pid}: {item['status']}/{item['reason_code']} -> {st}/{code}")
            record(pid, st, http_status=200, attempts=1,
                   snapshot=os.path.relpath(fp, C.ROOT).replace("\\", "/"),
                   content_hash=C.sha256_text(body), source_type="live",
                   reason_code=code, reason_detail=f"回溯分类：{detail}",
                   canonical_url=can_url, asin_on_page=asin_seen,
                   template_sig=template_signature(body))
        log.info(f"回溯分类完成：{len(cand)} 个检查，{changed} 个判定发生变化")

    # ---------------- 第一轮：离线快照（不联网，可复现） ----------------
    #
    # 优先级规则：**真实采集过的快照，绝不能被合成夹具覆盖。**
    # 磁盘上 data/snapshots/live/ 里放的是真实 HTTP 响应，data/snapshots/ 里放的是
    # 合成夹具。两者可能对应同一个 ASIN —— 如果第一轮先把合成夹具写进库、
    # 第二轮又因为"已经 success 了"跳过，那这个商品的来源就会从 live 变成
    # synthetic_fixture，等于**用假数据顶替真数据**，这是最不能犯的错。
    # 所以第一轮先算出"哪些 ASIN 有真实快照"，这些一律留给第二轮处理。
    live_dir = os.path.join(snap_dir, "live")
    live_cached: set[str] = set()
    if os.path.isdir(live_dir):
        live_cached = {f[:-5] for f in os.listdir(live_dir) if f.endswith(".html")}
        log.info(f"检测到 {len(live_cached):,} 个真实采集快照（live/），"
                 f"这些 ASIN 不再用合成夹具覆盖")

    handled: set[str] = set()          # 第一轮已经定性的 ASIN，第二轮不再碰
    if not args.offline_only and not args.reclassify_cache:
        log.info("第一轮：装载离线快照（教学夹具）")
    for item in todo:
        pid = item["product_id"]
        if pid in live_cached:
            continue                   # 留给第二轮，用真实快照
        fp = os.path.join(snap_dir, f"{pid}.html")
        meta_fp = os.path.join(snap_dir, f"{pid}.meta.json")
        if args.no_fixtures or not os.path.exists(fp):
            continue
        handled.add(pid)
        with open(fp, "rb") as f:
            body_bytes = f.read()
        body = body_bytes.decode("utf-8", "replace")
        chash = C.sha256_bytes(body_bytes)
        rel = os.path.relpath(fp, C.ROOT).replace("\\", "/")

        meta = {}
        if os.path.exists(meta_fp):
            with open(meta_fp, encoding="utf-8") as f:
                meta = json.load(f)

        rec_id = C.det_id("REC", "page", pid, chash)
        conn.execute(
            "INSERT OR IGNORE INTO raw_source_record (record_id,source_type,source_file_id,locator,"
            "url,snapshot_path,content_hash,fetched_at,note) VALUES (?,?,?,?,?,?,?,?,?)",
            (rec_id, "synthetic_fixture", None, f"asin={pid}", item["original_url"], rel, chash,
             meta.get("generated_at") or C.now_iso(),
             "合成教学夹具（非真实 Amazon 页面），仅用于演示解析/合并/血缘链路"))

        kind = meta.get("page_kind", "product")
        if kind == "captcha":
            record(pid, "access_restricted", http_status=200, snapshot=rel, content_hash=chash,
                   source_type="synthetic_fixture", reason_code="captcha_page",
                   reason_detail="页面返回验证码，按作业要求不得标记为采集成功")
        elif kind == "not_found":
            record(pid, "not_found", http_status=404, snapshot=rel, content_hash=chash,
                   source_type="synthetic_fixture", reason_code="http_404",
                   reason_detail="页面不存在")
        elif kind == "redirect":
            record(pid, "parse_failed", http_status=200, snapshot=rel, content_hash=chash,
                   source_type="synthetic_fixture", reason_code="redirected_to_other_item",
                   reason_detail="重定向到其它商品，页面 ASIN 与目标不符")
        else:
            record(pid, "success", http_status=200, snapshot=rel, content_hash=chash,
                   source_type="synthetic_fixture", reason_code="offline_snapshot",
                   reason_detail="离线快照命中（合成夹具）")
            stats["offline_loaded"] += 1

    # ---------------- 第二轮：真实请求（受 robots 与限速约束） ----------------
    if not args.offline_only and not args.reclassify_cache:
        n_live = args.live_sample if args.live_sample is not None else int(cfg["live_sample"])
        delay = float(cfg["delay_seconds"])
        timeout = float(cfg["timeout_seconds"])
        retries = int(cfg["max_retries"])
        live_done = 0
        # 连续受阻熔断：一旦站点开始成片返回 403/429/验证码，继续发请求既拿不到数据，
        # 也是对这个站点的持续施压 —— 作业明确要求「遇到访问限制不得以绕过…作为完成手段」。
        # 所以设一个上限：连续 N 次 access_restricted 就停手，把原因和证据写清楚，
        # 剩下的 ASIN 老实标成 not_collected。这不是失败，这是正确的降级。
        max_consec_blocked = int(cfg.get("max_consecutive_blocked", 10))
        consec_blocked = 0
        blocked_evidence: list[dict] = []
        log.info(f"第二轮：真实请求最多 {n_live} 条，间隔 {delay}s，超时 {timeout}s，"
                 f"重试 {retries} 次，连续受阻熔断阈值 {max_consec_blocked}")

        # 有真实快照的 ASIN 排到最前面。原因很实际：熔断随时可能触发，
        # 一旦触发，后面排队的 ASIN 就再也轮不到了。而这批已经有快照的 ASIN
        # **不需要发任何请求**，把它们排在前面，能保证"已经花代价采到的数据"
        # 一定被完整登记进库，不会被一次熔断截断。
        live_order = ([it for it in todo if it["product_id"] in live_cached]
                      + [it for it in todo if it["product_id"] not in live_cached])
        if live_cached:
            log.info(f"第二轮顺序：{len(live_cached):,} 个已有真实快照的 ASIN 优先（不发请求）")

        for item in live_order:
            if live_done >= n_live:
                break
            pid = item["product_id"]
            # 第一轮已经从离线快照定性的商品，这一轮不再发请求。
            # 不跳过的后果：对同一个 ASIN 既读了快照又去抓一次 —— 既浪费采集预算，
            # 也是对一个已经给过我们数据的站点的无谓施压。
            if pid in handled:
                continue
            url = item["original_url"]
            path = "/dp/" + pid
            if cfg["respect_robots"] and not rp.can_fetch(ua, url):
                record(pid, "access_restricted", source_type="live", reason_code="robots_disallow",
                       reason_detail="robots.txt 不允许抓取该路径，按合规要求放弃")
                stats["skipped_robots"] += 1
                continue

            attempt = 0
            status, http_status, final_url, body, err = "timeout", None, url, "", None
            detail = ""
            # ★ 缓存优先（snapshot cache）：这个 ASIN 已有快照就不再发请求。
            #   两个理由：
            #   1) 对站点友好。同一个页面反复抓，是不礼貌也是没必要的。
            #   2) **让"重跑结果一致"成立**。真实页面内容会变（Amazon 会插推荐位、
            #      换 AB 测试文案），每次重抓都会产生新的 content_hash，
            #      于是 raw_source_record 每次多几行，级联到分类/断言/合并全都漂移，
            #      幂等性就永远无法验证。
            #   这也是真实采集系统的标准做法：**抓一次、存快照、后续复用**。
            cache_path = os.path.join(snap_dir, "live", f"{pid}.html")
            cached_hit = os.path.exists(cache_path)
            if cached_hit:
                with open(cache_path, encoding="utf-8") as f:
                    body = f.read()
                http_status, final_url = 200, url
                status, code, detail = classify_response(http_status, body, pid)
                err = f"{code}+snapshot_cache"
                stats["live_cached"] += 1
            else:
                while attempt <= retries:
                    attempt += 1
                    stats["network_requests"] = stats.get("network_requests", 0) + 1
                    try:
                        req = urllib.request.Request(url, headers={"User-Agent": ua,
                                                                   "Accept-Language": "en-US,en;q=0.8"})
                        resp = urllib.request.urlopen(req, timeout=timeout)
                        body = resp.read().decode("utf-8", "replace")
                        http_status, final_url = resp.status, resp.geturl()
                        status, code, detail = classify_response(http_status, body, pid)
                        err = code
                        break
                    except urllib.error.HTTPError as exc:
                        http_status = exc.code
                        body = exc.read().decode("utf-8", "replace")
                        final_url = url
                        status, err, detail = classify_response(http_status, body, pid)
                        if status in ("not_found", "access_restricted"):
                            break
                    except (socket.timeout, TimeoutError):
                        status, err = "timeout", "socket_timeout"
                    except urllib.error.URLError as exc:
                        status, err = "timeout", f"urlerror:{exc.reason}"
                    time.sleep(delay)

            stats["live_attempted"] += 1
            live_done += 1
            rel = None
            chash = None
            if body:
                rel_path = os.path.join(snap_dir, "live", f"{pid}.html")
                os.makedirs(os.path.dirname(rel_path), exist_ok=True)
                with open(rel_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(body)
                chash = C.sha256_text(body)
                rel = os.path.relpath(rel_path, C.ROOT).replace("\\", "/")
                rec_id = C.det_id("REC", "page", pid, chash)
                conn.execute(
                    "INSERT OR IGNORE INTO raw_source_record (record_id,source_type,source_file_id,"
                    "locator,url,snapshot_path,content_hash,fetched_at,note)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (rec_id, "product_page", None, f"asin={pid}", final_url, rel, chash,
                     C.now_iso(), f"实时采集 http_status={http_status}"))
            if status == "success":
                stats["live_success"] += 1
            can_url, _, asin_seen = page_identity(body) if body else (None, "none", None)
            tpl_sig = template_signature(body) if body else None
            record(pid, status, http_status=http_status, attempts=attempt, final_url=final_url,
                   snapshot=rel, content_hash=chash, source_type="live", reason_code=err,
                   reason_detail=f"实时采集：{detail or err}",
                   canonical_url=can_url, asin_on_page=asin_seen, template_sig=tpl_sig)
            log.info(f"  实时 {pid} -> {status} (HTTP {http_status}, 尝试 {attempt} 次)"
                     + (f" [{err}]" if status != "success" else ""))
            # 只有真的发了请求才需要间隔。命中快照缓存时一个包都没发，
            # 再 sleep 3 秒纯粹是浪费 —— 全量库里有上百条缓存，白等好几分钟。
            if not cached_hit:
                time.sleep(delay)

            # 熔断判定：只统计**本次真实请求**得到的拒绝，不把 404 算进去 ——
            # 404 说明这个商品下架了，是正常的业务结果，不代表我们在被限流。
            #
            # 也不能把「快照缓存里读出来的旧验证码页」算进去。
            # 这是实测踩到的坑：全量库里 data/snapshots/live/ 有 23 个历史验证码页，
            # 它们连在一起被读出来，凑够 10 个就触发熔断，而本次运行**一个网络请求都没发**。
            # 那样写出来的证据会谎称"站点本次成片拒绝访问"，是最不能出现的那种错误：
            # 把"我没发请求"说成"对方拒绝了我"。所以判据必须是 cached_hit 为假。
            if status == "access_restricted" and not cached_hit:
                consec_blocked += 1
                blocked_evidence.append({"product_id": pid, "http_status": http_status,
                                         "reason_code": err, "final_url": final_url})
                if consec_blocked >= max_consec_blocked:
                    log.warn(f"连续 {consec_blocked} 次访问受限（HTTP {http_status} / {err}），"
                             f"触发熔断：停止实时采集，剩余 ASIN 标为未采集。"
                             f"已采集 {live_done} 条。")
                    stats["circuit_broken"] = True
                    stats["circuit_broken_after"] = live_done
                    C.write_json(C.path("evidence", "s03_circuit_breaker.json"), {
                        "run_id": run_id,
                        "trigger": "consecutive_access_restricted",
                        "threshold": max_consec_blocked,
                        "observed": blocked_evidence[-max_consec_blocked:],
                        "live_attempted": live_done,
                        "network_requests": stats.get("network_requests", 0),
                        "note": "站点开始成片拒绝访问，主动停止。未使用任何绕过手段；"
                                "剩余 ASIN 保留在清单中，状态为 not_collected。",
                        "logged_at": C.now_iso(),
                    })
                    break
            elif not cached_hit:
                # 真实请求成功/404/解析失败都说明站点还在正常响应，计数清零
                consec_blocked = 0

        # 没触发熔断时也要留证据 —— 否则 evidence/ 目录里只剩一份"上一次被限流"的旧文件，
        # 读者会以为这一次也被拒绝了。证据的**缺失**同样是一种误导。
        if not stats.get("circuit_broken"):
            C.write_json(C.path("evidence", "s03_circuit_breaker.json"), {
                "run_id": run_id,
                "trigger": "consecutive_access_restricted",
                "threshold": max_consec_blocked,
                "observed": blocked_evidence[-max_consec_blocked:],
                "triggered": False,
                "live_attempted": live_done,
                "network_requests": stats.get("network_requests", 0),
                "max_consecutive_blocked_seen": consec_blocked,
                "note": "本次运行**未触发**熔断：没有任何一次连续 %d 个真实请求被判为访问受限。"
                        "observed 为空不代表没有检查，而代表本次真实请求里没有出现成片拒绝。"
                        % max_consec_blocked,
                "logged_at": C.now_iso(),
            })

    # ---------------- 第三轮：剩下的标为未采集 ----------------
    # 「未采集」必须区分两种完全不同的原因，否则统计会误导读者：
    #   budget_exhausted     —— 一次都没尝试（不在本次采集预算内）
    #   no_source_available  —— 尝试过，但既没有离线快照、实时页面也不可用
    # 判据用 attempts（该 ASIN 被尝试采集的次数），而不是猜预算边界，
    # 这样原因码永远和数据一致。
    conn.execute(
        "UPDATE product_collection SET status='not_collected', source_type=NULL,"
        " reason_code=CASE WHEN COALESCE(attempts,0)=0 THEN 'budget_exhausted'"
        "                   ELSE 'no_source_available' END,"
        " reason_detail=CASE WHEN COALESCE(attempts,0)=0"
        "                   THEN '不在本次采集预算内（清单保留该 ASIN，状态为未采集）'"
        "                   ELSE '已尝试采集，但无离线快照且实时页面不可用' END"
        " WHERE status IN ('pending','not_collected')")
    stats["not_collected"] = conn.execute(
        "SELECT COUNT(*) c FROM product_collection WHERE status='not_collected'").fetchone()["c"]
    stats["not_collected_budget"] = conn.execute(
        "SELECT COUNT(*) c FROM product_collection WHERE reason_code='budget_exhausted'").fetchone()["c"]
    stats["not_collected_attempted"] = conn.execute(
        "SELECT COUNT(*) c FROM product_collection WHERE reason_code='no_source_available'"
    ).fetchone()["c"]

    # ---------------- 统计：状态互斥性检查 ----------------
    rows = conn.execute(
        "SELECT status, source_type, COUNT(*) n FROM product_collection"
        " GROUP BY status, source_type ORDER BY n DESC").fetchall()
    total = conn.execute("SELECT COUNT(*) c FROM product_collection").fetchone()["c"]
    sum_status = sum(r["n"] for r in rows)
    detail = [{"status": r["status"], "source_type": r["source_type"], "count": r["n"]} for r in rows]

    summary = {
        "run_id": run_id,
        "products_total": total,
        "status_sum": sum_status,
        "status_partition_ok": total == sum_status,
        "status_by_source": detail,
        "robots_sha256": robots_hash,
        "robots_allows_dp": rp.can_fetch(ua, "/dp/"),
        "live": {"attempted": stats["live_attempted"], "success": stats["live_success"],
                 "cached_from_snapshot": stats.get("live_cached", 0),
                 "network_requests": stats.get("network_requests", 0)},
        "offline_loaded": stats["offline_loaded"],
        "not_collected": stats["not_collected"],
        "note": ("实时采集成功率与离线可用率必须分开看：source_type=synthetic_fixture 的"
                 "记录是合成教学夹具，不能计入真实采集成功率。"
                 "另注意 live.network_requests 与 live.attempted 的差别："
                 "后者包含「命中本地快照、一个请求都没发」的条数。"
                 "报告里说『采集了 N 条』时，必须说清其中多少来自本次网络请求。"),
    }
    C.write_json(C.path("evidence", "s03_collection_summary.json"), summary)
    log.info(f"清单共 {total:,} 个产品；状态之和 {sum_status:,} -> "
             f"{'互斥性检查通过' if total == sum_status else '不通过'}")
    for d in detail:
        log.info(f"  {d['status']:<18} source_type={str(d['source_type']):<20} {d['count']:,}")

    C.end_run(conn, run_id, "success", f"products={total}")
    conn.close()
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
