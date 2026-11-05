"""任务 6：补全上映信息（第三方数据源 = Wikidata）。

为什么用 Wikidata：
  * 公开 REST / SPARQL 接口，返回结构化 JSON，可程序化核对；
  * 每条结果都能给出实体 URI（QID），等于给了「证据地址」，可回溯；
  * 作业说明允许「IMDb、豆瓣或其他允许使用的数据源」。

本步骤严格区分两种日期语义（作业反复强调的点）：
    商品发售日期  release_type = physical   （来自商品页，"Release date" 其实是 DVD 上市日）
    影院上映日期  release_type = theatrical （来自 Wikidata 的 P577 首次上映）
两者分别保存、绝不互相覆盖；也绝不把「只知道年份」补成 1 月 1 日。

关于访问路径（本文件为什么有两套接口）：
    首选 SPARQL 端点 query.wikidata.org，一次批量查多部作品最省请求。
    但 2026 年 wdqs 处于故障限流状态（1 请求/分钟），实测返回 HTTP 429。
    于是降级到 www.wikidata.org 的 MediaWiki REST API（wbsearchentities +
    wbgetentities）——同一份权威数据、同一批 QID，只是访问方式不同。
    这本身就是 ETL 里「数据源可达性降级」的真实案例，已写入 evidence 摘要。

流程（每一步都留证据）：
    1. 用作品规范标题做实体搜索，得到同名候选（可能包含同名专辑、剧集、小说）；
    2. 过滤：P31 必须是 Q11424（电影），这是硬性条件；
    3. 用「年份接近度」在同名候选里挑唯一一部（重拍片靠这一步分开）；
    4. 把原始响应存成快照 + SHA-256 + 请求 URL，登记为 third_party 来源；
    5. 把上映日期/类型/导演/主演写成 movie 级断言（不直接改 movie 表）。

运行：python -m src.s08_enrich_release
      python -m src.s08_enrich_release --offline   # 只用缓存，答辩断网可复现
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

from src import common as C

WD_API = "https://www.wikidata.org/w/api.php"
SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
EXTRACTOR_VERSION = "wikidata-rest-v2"

# 单次 HTTP 超时。为什么是 20 秒而不是 60 秒：
#   这一步是「补全」，不是正确性前提。补不到就该尽快放弃、写 NULL、让下游继续。
#   60 秒 × 3 次重试 × 3 个标题变体 = 单部作品最坏要挂 9 分钟，
#   几十部作品就足以让整条链看起来像死掉（实测被这一步卡住过一次）。
HTTP_TIMEOUT = 20


class Throttle:
    """自适应请求间隔（AutoThrottle 式）。

    为什么不能用一个固定间隔：
        第一次跑这一步时把 delay 写死成 1.2s，结果是「每分钟约 100 个请求」——
        前 10 分钟正常，之后开始连续 429，每个 429 要退避 8s+16s，
        于是 1500s 的预算里只跑完 172 部（其中还失败一半）。
        把 delay 直接改成 5s 能躲开限流，但站点恢复之后仍然要白等 5s。
    固定值无论取多少都是错的：站点能承受的速率随时间变化，客户端只能跟着反馈走。
    Scrapy 的 AutoThrottle 就是这个思路（按延迟动态算并发），这里用一个更朴素的版本：

        429 一次        → delay × 2（封顶 max_delay）
        连续 N 次成功   → delay − 0.2（不低于 min_delay）

    乘性增、线性减（AIMD）：增得快才能在挨罚后立刻收手，
    减得慢才不会在限流边缘反复抖动。这跟 TCP 拥塞控制的思路是同一个。
    """

    def __init__(self, min_delay: float = 0.6, max_delay: float = 8.0,
                 recover_after: int = 6) -> None:
        self.min_delay = float(min_delay)
        self.max_delay = float(max_delay)
        self.delay = float(min_delay)
        self.recover_after = int(recover_after)
        self.ok_streak = 0
        self.events: list[dict] = []      # 每次调整都留痕，写进 evidence

    def wait(self) -> None:
        if self.delay > 0:
            time.sleep(self.delay)

    def on_success(self) -> None:
        self.ok_streak += 1
        if self.ok_streak >= self.recover_after and self.delay > self.min_delay:
            self.delay = max(self.min_delay, round(self.delay - 0.2, 2))
            self.ok_streak = 0
            self.events.append({"at": C.now_iso(), "event": "decay",
                                "delay": self.delay})

    def on_rate_limited(self) -> float:
        """遇到 429：立刻把间隔翻倍，并返回本次应当退避的秒数。"""
        self.ok_streak = 0
        self.delay = min(self.max_delay, round(self.delay * 2, 2))
        self.events.append({"at": C.now_iso(), "event": "throttle_up",
                            "delay": self.delay})
        return self.delay


# 进程内单例。_http_json 在调用栈深处，逐层传参只会让签名更脏；
# 限速本来就是「整个进程对同一个站点的全局状态」，用模块级单例反而更贴合语义。
THROTTLE = Throttle()

# 电影类型（P31 = instance of 的值）
QID_FILM = "Q11424"
QID_ANIMATED_FILM = "Q202866"

# Wikidata 属性
P_INSTANCE_OF = "P31"
P_PUBLICATION_DATE = "P577"
P_IMDB = "P345"
P_DIRECTOR = "P57"
P_GENRE = "P136"
P_CAST = "P161"

# P577 的 precision 字段含义：9=年 10=月 11=日
PRECISION_MAP = {9: "year", 10: "month", 11: "day", 12: "hour", 13: "minute", 14: "second"}

# 描述里出现这些词，说明这一条可能是影视作品（用于粗筛，最终仍以 P31 为准）
FILM_HINT_RE = re.compile(r"\b(film|movie|animated|documentary|feature)\b", re.I)

# 描述开头通常写着年份，例如 "2010 film directed by Ethan and Joel Coen"
DESC_YEAR_RE = re.compile(r"^\s*(\d{4})\b")


# --------------------------------------------------------------------------- #
# 一、HTTP 层：搜索 / 取实体 / 批量取标签，全部带磁盘缓存
# --------------------------------------------------------------------------- #

def _rel(p: str) -> str:
    """把绝对路径转成相对项目根目录的路径，便于证据文件跨机器复现。"""
    try:
        return os.path.relpath(p, C.ROOT).replace("\\", "/")
    except ValueError:
        return p.replace("\\", "/")


def _http_json(url: str, cache_fp: str, meta_fp: str, log: C.Logger,
               retries: int = 3) -> tuple[dict | None, dict]:
    """GET 一个返回 JSON 的 URL，结果落盘缓存。

    缓存不只是为了「快」：它让整个流程**可离线复现**——
    答辩时网络不通，也能用同一份快照跑出完全一样的结果（作业的「可复现」要求）。
    """
    if os.path.exists(cache_fp) and os.path.exists(meta_fp):
        with open(cache_fp, encoding="utf-8") as f:
            data = json.load(f)
        with open(meta_fp, encoding="utf-8") as f:
            meta = json.load(f)
        meta["cached"] = True
        meta["cache_path"] = _rel(cache_fp)
        return data, meta

    req = urllib.request.Request(url, headers={
        "User-Agent": "etl-coursework/1.0 (student project; educational use)",
        "Accept": "application/json"})

    last_err = None
    for attempt in range(retries):
        # 每次**真实请求**前先等当前的自适应间隔。
        # 早期版本是在「每部作品」循环末尾 sleep 一次，一部作品要发 2~3 个请求，
        # 于是实际速率是配置值的 2~3 倍 —— 这正是上一版被 429 打死的原因。
        # 限速的对象是请求，不是作品。
        THROTTLE.wait()
        try:
            body = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT).read()
            THROTTLE.on_success()
            break
        except Exception as exc:  # noqa: BLE001 - 网络异常统一处理
            last_err = exc
            msg = str(exc)
            # 429 是限流：先调高全局间隔，再按新间隔退避重试（有界，不无限重试）
            wait = THROTTLE.on_rate_limited() if "429" in msg else 1.5 * (attempt + 1)
            if attempt < retries - 1:
                log.warn(f"请求失败（{msg[:80]}），{wait:.1f}s 后重试 {attempt + 1}/{retries - 1}")
                time.sleep(wait)
    else:
        return None, {"ok": False, "error": str(last_err), "url": url}

    with open(cache_fp, "wb") as f:
        f.write(body)
    meta = {"url": url, "fetched_at": C.now_iso(), "sha256": C.sha256_bytes(body),
            "byte_size": len(body), "ok": True, "cached": False,
            "cache_path": _rel(cache_fp)}
    with open(meta_fp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    try:
        return json.loads(body.decode("utf-8")), meta
    except Exception as exc:  # noqa: BLE001
        return None, {"ok": False, "error": f"json decode: {exc}", "url": url}


def wb_search(term: str, cache_dir: str, log: C.Logger, limit: int = 8) -> tuple[list[dict], dict]:
    """按标题搜实体。返回 [{id,label,description}]，只保留粗筛像影视的。"""
    params = {"action": "wbsearchentities", "format": "json", "language": "en",
              "uselang": "en", "type": "item", "limit": str(limit), "search": term}
    url = WD_API + "?" + urllib.parse.urlencode(params)
    key = C.sha256_text("wbsearch|" + term.lower())[:16]
    data, meta = _http_json(url, os.path.join(cache_dir, f"search_{key}.json"),
                            os.path.join(cache_dir, f"search_{key}.meta.json"), log)
    if not data:
        return [], meta
    hits = data.get("search", []) or []
    return [{"id": h["id"], "label": h.get("label", ""),
             "description": h.get("description", "")} for h in hits], meta


def wb_entities(qids: list[str], cache_dir: str, log: C.Logger) -> tuple[dict, dict]:
    """批量取实体的 claims + 英文标签（一次最多 50 个 QID）。"""
    if not qids:
        return {}, {"ok": True, "cached": True, "sha256": ""}
    params = {"action": "wbgetentities", "format": "json", "props": "claims|labels",
              "languages": "en", "ids": "|".join(sorted(set(qids))[:50])}
    url = WD_API + "?" + urllib.parse.urlencode(params)
    key = C.sha256_text("wbent|" + params["ids"])[:16]
    data, meta = _http_json(url, os.path.join(cache_dir, f"ent_{key}.json"),
                            os.path.join(cache_dir, f"ent_{key}.meta.json"), log)
    if not data:
        return {}, meta
    return data.get("entities", {}) or {}, meta


def claim_qids(entity: dict, prop: str) -> list[str]:
    """取某个属性指向的实体 QID 列表（跳过已被弃用的 rank=deprecated 值）。"""
    out = []
    for c in (entity.get("claims", {}) or {}).get(prop, []) or []:
        if c.get("rank") == "deprecated":
            continue
        dv = (c.get("mainsnak", {}) or {}).get("datavalue", {}) or {}
        v = dv.get("value")
        if isinstance(v, dict) and "id" in v:
            out.append(v["id"])
    return out


def claim_strings(entity: dict, prop: str) -> list[str]:
    """取某个属性的字符串值（例如 P345 = IMDb ID）。"""
    out = []
    for c in (entity.get("claims", {}) or {}).get(prop, []) or []:
        if c.get("rank") == "deprecated":
            continue
        v = ((c.get("mainsnak", {}) or {}).get("datavalue", {}) or {}).get("value")
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    return out


def claim_dates(entity: dict, prop: str) -> list[tuple[str, str]]:
    """取时间型属性，返回 [(ISO 值, 精度名)]。

    这里的关键是 **precision**：Wikidata 可能只精确到年（precision=9）。
    我们保留这个精度，绝不把 "1969 年" 补成 "1969-01-01"。
    """
    out = []
    for c in (entity.get("claims", {}) or {}).get(prop, []) or []:
        if c.get("rank") == "deprecated":
            continue
        dv = (c.get("mainsnak", {}) or {}).get("datavalue", {}) or {}
        v = dv.get("value")
        if not isinstance(v, dict) or "time" not in v:
            continue
        raw = v["time"]                       # 形如 +1969-06-11T00:00:00Z
        prec = PRECISION_MAP.get(int(v.get("precision", 11)), "day")
        m = re.match(r"^[+-]?(\d{4})-(\d{2})-(\d{2})", raw)
        if not m:
            continue
        y, mo, d = m.group(1), m.group(2), m.group(3)
        if prec == "year":
            iso = y
        elif prec == "month":
            iso = f"{y}-{mo}"
        else:
            iso = f"{y}-{mo}-{d}"
        out.append((iso, prec))
    return out


# --------------------------------------------------------------------------- #
# 二、标题变体：规范化标题常把冠词挪到末尾，搜索前要还原
# --------------------------------------------------------------------------- #

_EDITION_RE = re.compile(
    r"\s*[\(\[]\s*(collector'?s?\s+edition|special\s+edition|deluxe\s+edition|"
    r"criterion\s+collection|anniversary\s+edition|remastered|unrated|"
    r"blu-?ray|dvd|4k|uhd|digital|widescreen|full\s*screen)\s*[\)\]]",
    re.I)


def title_variants(title: str) -> list[str]:
    """生成用于检索的标题变体，按「最可能命中」排序。

    图书/音像行业习惯把冠词挪到末尾（「Inception, The」），
    Wikidata 的标签却是「Inception」或「The Inception」。
    所以这里把三种形态都试一遍：原样 / 冠词还原 / 去冠词。
    """
    t = _EDITION_RE.sub("", title or "").strip()
    variants = [t]

    m = re.match(r"^(.*?),\s*(The|A|An)$", t, re.I)
    if m:
        variants.append(f"{m.group(2)} {m.group(1)}".strip())   # 冠词还原到前面
        variants.append(m.group(1).strip())                      # 去掉冠词

    m2 = re.match(r"^(The|A|An)\s+(.*)$", t, re.I)
    if m2:
        variants.append(m2.group(2).strip())

    # 副标题去掉再试一次（"Kill Bill: Vol. 2" -> "Kill Bill"）
    if ":" in t:
        variants.append(t.split(":", 1)[0].strip())

    seen, out = set(), []
    for v in variants:
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out


# --------------------------------------------------------------------------- #
# 三、候选打分：先确认是电影，再看年份是否对得上
# --------------------------------------------------------------------------- #

def build_candidate(qid: str, hit: dict, entity: dict | None,
                    labels: dict[str, str]) -> dict:
    """把一个搜索结果整理成统一结构的候选。"""
    ent = entity or {}
    p31 = claim_qids(ent, P_INSTANCE_OF)
    is_film = QID_FILM in p31 or QID_ANIMATED_FILM in p31

    dates = claim_dates(ent, P_PUBLICATION_DATE)
    # 优先取精度最高的那条（day > month > year）
    order = {"day": 0, "month": 1, "year": 2}
    dates.sort(key=lambda x: order.get(x[1], 3))
    date_iso, date_prec = (dates[0] if dates else (None, None))

    desc = hit.get("description", "") or ""
    year = int(date_iso[:4]) if date_iso and date_iso[:4].isdigit() else None
    year_from_desc = int(m.group(1)) if (m := DESC_YEAR_RE.match(desc)) else None

    return {
        "qid": qid,
        "label": (ent.get("labels", {}).get("en", {}) or {}).get("value") or hit.get("label", ""),
        "description": desc,
        "is_film": is_film,
        "p31": p31,
        "date_iso": date_iso,
        "date_precision": date_prec,
        "year": year or year_from_desc,
        "year_source": "P577" if year else ("description" if year_from_desc else None),
        "imdb": (claim_strings(ent, P_IMDB) or [None])[0],
        "directors": claim_qids(ent, P_DIRECTOR),
        "genres": claim_qids(ent, P_GENRE),
        "cast": claim_qids(ent, P_CAST),
    }


def score_candidate(cand: dict, query_title: str, year_hint: int | None) -> tuple[float, dict]:
    """给候选打分。分数只是「排个序 + 卡阈值」，理由要写清楚，便于解释。"""
    reasons = []
    if not cand["is_film"]:
        return -1.0, {"rejected": "P31 不是电影", "p31": cand["p31"]}

    score = 0.40
    reasons.append("P31=电影(+0.40)")

    if year_hint and cand["year"]:
        diff = abs(cand["year"] - year_hint)
        bonus = {0: 0.50, 1: 0.40, 2: 0.25}.get(diff, 0.05 if diff <= 4 else -0.50)
        score += bonus
        reasons.append(f"年份 {cand['year']} vs 线索 {year_hint}，差 {diff} 年({bonus:+.2f})")
    elif year_hint and not cand["year"]:
        reasons.append("候选无年份，无法核对(-0.00)")
    else:
        reasons.append("无年份线索，仅靠标题(+0.00)")

    if C.norm_title(cand["label"]) == C.norm_title(query_title):
        score += 0.10
        reasons.append("标题规范化后完全一致(+0.10)")

    return round(score, 3), {"reasons": reasons, "year_diff":
                             abs(cand["year"] - year_hint) if (year_hint and cand["year"]) else None}


def pick_best(cands: list[dict], query_title: str, year_hint: int | None,
              threshold: float) -> tuple[dict | None, dict]:
    """从候选里挑唯一一部。返回 (选中候选, 决策说明)。

    决策说明要能回答「为什么是它、为什么不是别人」——
    这正是作业里「选择理由」要求的东西。
    """
    scored = []
    for c in cands:
        s, why = score_candidate(c, query_title, year_hint)
        scored.append((s, c, why))
    scored.sort(key=lambda x: (-x[0], x[1]["year"] or 9999))

    detail = {
        "query_title": query_title,
        "year_hint": year_hint,
        "threshold": threshold,
        "considered": len(cands),
        "ranking": [{"qid": c["qid"], "label": c["label"], "year": c["year"],
                     "score": s, "why": w} for s, c, w in scored[:6]],
    }
    if not scored or scored[0][0] < threshold:
        detail["decision"] = "reject_all"
        detail["reason"] = (f"最高分 {scored[0][0] if scored else 0:.2f} < 阈值 {threshold:.2f}"
                            "，宁可留空也不猜")
        return None, detail

    best_score, best, _ = scored[0]
    # 平票保护：第二名同分则视为歧义，交人工
    if len(scored) > 1 and abs(scored[1][0] - best_score) < 1e-9:
        detail["decision"] = "ambiguous"
        detail["reason"] = f"前两名同分 {best_score:.2f}，无法区分"
        return None, detail

    detail["decision"] = "accept"
    detail["chosen"] = best["qid"]
    detail["reason"] = (f"最高分 {best_score:.2f} ≥ 阈值 {threshold:.2f}，"
                        f"领先第二名 {best_score - (scored[1][0] if len(scored) > 1 else 0):.2f}")
    return best, detail


# --------------------------------------------------------------------------- #
# 四、主流程
# --------------------------------------------------------------------------- #

def year_hint_of(conn, movie_id: str) -> tuple[int | None, str | None]:
    """作品的年份线索：优先用商品页发售日的年份（弱信号）。

    为什么允许用 physical 日期做线索？因为它只是「帮我在同名候选里挑人」，
    不会被写进 release_date 字段。真正入库时 s09 只接受 theatrical 语义。
    线索与写入语义分离，是这套设计的关键。
    """
    r = conn.execute(
        "SELECT MIN(CAST(substr(normalized_value,1,4) AS INTEGER)) AS y,"
        "       MIN(extractor_rule_id) AS rule"
        "  FROM field_assertion"
        " WHERE entity_type='movie' AND entity_id=? AND field_name='release_date'"
        "   AND substr(normalized_value,1,4) GLOB '[0-9][0-9][0-9][0-9]'",
        (movie_id,)).fetchone()
    if r and r["y"]:
        return int(r["y"]), f"physical_date({r['rule']})"
    return None, None


def search_candidates(title: str, year_hint: int | None, cache_dir: str,
                      log: C.Logger) -> tuple[list[dict], dict]:
    """按标题变体依次检索，返回 (候选实体列表, 过程信息)。

    为什么要「依次」而不是「全部」：
        大部分作品第一个变体就能命中，全试会浪费 2/3 的请求；
        但只试第一个又会漏（例如「Aliens, The」在「The Aliens」上撞到
        另一部同名电影，真正的「Aliens」要退到第三个变体才出现）。
        所以策略是：命中后先用**描述里的年份**预判对不对，
        不对就继续试下一个变体，直到有变体的年份和线索吻合。
    注意：这里只用搜索结果里的 description 做预筛，不额外发请求。
    """
    variants = title_variants(title)
    collected: list[dict] = []
    info = {"variants_tried": [], "variant_used": None, "network_errors": 0, "requests": 0}

    for v in variants:
        hits, meta = wb_search(v, cache_dir, log)
        info["requests"] += 1
        if not meta.get("ok", False):
            info["network_errors"] += 1
            continue
        info["variants_tried"].append(v)
        filmish = [h for h in hits if FILM_HINT_RE.search(h.get("description") or "")]
        if not filmish:
            continue
        collected.extend(filmish)
        # 用描述年份预判：有一条落在 ±2 年内，就认为这个变体是对的
        if year_hint:
            for h in filmish:
                m = DESC_YEAR_RE.match(h.get("description") or "")
                if m and abs(int(m.group(1)) - year_hint) <= 2:
                    info["variant_used"] = v
                    return filmish, info
        elif len(filmish) == 1:
            info["variant_used"] = v
            return filmish, info

    # 所有变体都没给出「年份吻合」的候选：把收集到的全部交给后续打分
    seen, merged = set(), []
    for h in collected:
        if h["id"] not in seen:
            seen.add(h["id"])
            merged.append(h)
    info["variant_used"] = info["variants_tried"][0] if info["variants_tried"] else None
    return merged, info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--limit", type=int, default=None, help="最多查询多少部作品（控制耗时）")
    ap.add_argument("--offline", action="store_true", help="只用缓存，不联网")
    ap.add_argument("--budget-seconds", type=float, default=None,
                    help="本步骤的时间预算（秒）。超预算就停下并如实记录「已处理 N 部、剩余 M 部未处理」，"
                         "而不是让整条流水线挂在补全上。默认取 config.enrich.budget_seconds（1200s）。")
    ap.add_argument("--max-movies", type=int, default=None,
                    help="最多补全多少部作品（按重要性排序后取前 N）。全量库有几万个 movie 实体，"
                         "逐个查 Wikidata 是不可能的，所以只补影响面最大的那批。"
                         "默认取 config.enrich.max_movies。")
    ap.add_argument("--order", choices=["importance", "reviews", "title"], default="importance",
                    help="补全顺序。importance=按关联商品版本数降序（先补影响面大的），"
                         "reviews=按关联商品评论总数降序（先补「有知名度」的），"
                         "title=按标题字母序（便于逐条复现某个作品）")
    args = ap.parse_args(argv)

    cfg = C.CFG["enrich"]
    threshold = float(cfg.get("accept_threshold", 0.70))
    # 限量与预算都允许 config 兜底：统一运行时（s12_run_all）不该被迫传一堆参数，
    # 而"到底补多少部"属于运行策略，放在 config 里比写死在命令行更合适。
    if args.budget_seconds is None:
        args.budget_seconds = float(cfg.get("budget_seconds", 1200.0))
    if args.max_movies is None and cfg.get("max_movies"):
        args.max_movies = int(cfg["max_movies"])
    conn = C.connect(args.db)
    log = C.Logger("s08_enrich_release")

    # 用 config 里的 delay_seconds 作为**起始**间隔，之后由 Throttle 自行调整。
    # config 里的值从此不再是"最终速率"，而是"乐观起点"——这需要在报告里说明，
    # 否则读者会以为实际速率等于配置值。
    THROTTLE.min_delay = float(cfg.get("delay_seconds", 0.6))
    THROTTLE.delay = THROTTLE.min_delay
    THROTTLE.max_delay = float(cfg.get("max_delay_seconds", 8.0))

    run_id = C.start_run(conn, "s08_enrich_release", {"limit": args.limit, "offline": args.offline,
                                                     "max_movies": args.max_movies,
                                                     "order": args.order})
    log.run_id = run_id
    cache_dir = C.path(cfg["cache_dir"])
    os.makedirs(cache_dir, exist_ok=True)

    # 为什么按「关联商品版本数」排序，而不是按标题字母序：
    #   全量库有数万个 movie 实体，绝大多数只有一个商品版本（长尾）。
    #   按字母序取前 N 部，等于随机抽了一批长尾作品，补全覆盖率再高也代表不了整体。
    #   按版本数降序，补的是"被最多商品引用"的作品 —— 同样 1000 次查询，
    #   覆盖的商品数从「几百」变成「上万」，报告里才讲得清"补全覆盖了 X% 的商品"。
    #
    # 但这个推理**实测被推翻了**（手册 12.30）：
    #   版本数多的作品，恰恰是"同一部冷门片被反复以 DVD/蓝光/多碟装重发"的那批，
    #   它们在 Wikidata 里往往没有条目；而 Wikidata 收录的是"有百科关注度"的作品。
    #   所以 importance 与"可补全性"负相关：按它排序 = 把补全预算花在最不可能补全的作品上。
    #   于是新增 reviews 排序：按关联商品的评论总数降序，用"有多少人评论过"当知名度代理。
    if args.order == "importance":
        order_sql = ("ORDER BY (SELECT COUNT(*) FROM product_movie pm WHERE pm.movie_id=m.movie_id)"
                     " DESC, m.canonical_title")
    elif args.order == "reviews":
        order_sql = ("ORDER BY (SELECT COALESCE(SUM(p.review_count),0) FROM product_movie pm"
                     " JOIN product p ON p.product_id=pm.product_id"
                     " WHERE pm.movie_id=m.movie_id) DESC, m.canonical_title")
    else:
        order_sql = "ORDER BY m.canonical_title"
    movies = [dict(r) for r in conn.execute(
        f"SELECT m.movie_id, m.canonical_title,"
        f"       (SELECT COUNT(*) FROM product_movie pm WHERE pm.movie_id=m.movie_id) AS n_products"
        f"  FROM movie m {order_sql}")]
    total_movies = len(movies)
    limit = args.limit if args.limit is not None else len(movies)
    if args.max_movies is not None:
        limit = min(limit, args.max_movies)
    movies = movies[:limit]
    log.info(f"待补全作品 {len(movies)} 部（全库 {total_movies:,} 部，上限 {limit}，"
             f"排序={args.order}，接受阈值 {threshold}）")

    stat = {"queried": 0, "matched": 0, "not_found": 0, "rejected": 0, "ambiguous": 0,
            "source_unavailable": 0, "cached": 0, "assertions": 0, "requests": 0,
            "precision": {}, "search_variant_hits": {},
            "budget_exhausted": False, "processed": 0, "skipped_over_budget": 0}
    t_start = time.time()

    with C.step(conn, run_id, "enrich", 1, log) as st:
        for mv in movies:
            # 预算闸门：到点就停。已处理的写库结果全部保留（每部作品独立提交），
            # 下次重跑会因为缓存命中而直接跳过，所以「停下来」不损失任何进度。
            if time.time() - t_start > args.budget_seconds:
                remaining = len(movies) - stat["processed"]
                stat["budget_exhausted"] = True
                stat["skipped_over_budget"] = remaining
                log.warn(f"时间预算 {args.budget_seconds:.0f}s 用尽：已处理 {stat['processed']} 部，"
                         f"剩余 {remaining} 部留待下次（缓存已保留，重跑会命中缓存）")
                break

            mid, title = mv["movie_id"], mv["canonical_title"]
            if not title:
                continue
            stat["processed"] += 1

            hint, hint_src = year_hint_of(conn, mid)

            # ---- 1. 搜索：按标题变体检索，拿到「像影视」的候选 ----
            hits, sinfo = search_candidates(title, hint, cache_dir, log)
            stat["requests"] += sinfo["requests"]
            stat["search_variant_hits"][sinfo["variant_used"] or "-"] = \
                stat["search_variant_hits"].get(sinfo["variant_used"] or "-", 0) + 1
            stat["queried"] += 1

            # 网络全失败 ≠ 数据不存在。必须分开记，否则质量报告会骗人。
            if not hits and sinfo["network_errors"] == sinfo["requests"] > 0:
                stat["source_unavailable"] += 1
                conn.execute(
                    "INSERT INTO meta_quality_issue (issue_id,run_id,entity_type,entity_id,field_name,"
                    "issue_type,severity,detail,created_at) VALUES (?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(issue_id) DO NOTHING",
                    (C.det_id("QI", "enrich", mid, "unavailable"), run_id, "movie", mid,
                     "release_date", "enrichment_source_unavailable", "warn",
                     json.dumps({"title": title, "variants": title_variants(title),
                                 "network_errors": sinfo["network_errors"]},
                                ensure_ascii=False), C.now_iso()))
                continue

            # ---- 2. 取候选实体的 claims（一次批量，最多 50 个 QID） ----
            cands: list[dict] = []
            emeta: dict = {}
            if hits:
                ents, emeta = wb_entities([h["id"] for h in hits], cache_dir, log)
                stat["requests"] += 1
                if emeta.get("cached"):
                    stat["cached"] += 1
                for h in hits:
                    cands.append(build_candidate(h["id"], h, ents.get(h["id"]), {}))

            if args.offline and not cands:
                continue

            best, decision = pick_best(cands, title, hint, threshold)
            decision["variants_tried"] = sinfo["variants_tried"]
            decision["variant_used"] = sinfo["variant_used"]

            # ---- 3. 没选中：登记质量问题，绝不猜 ----
            if not best:
                if decision["decision"] == "ambiguous":
                    stat["ambiguous"] += 1
                    sev, itype = "warn", "enrichment_ambiguous"
                elif cands:
                    stat["rejected"] += 1
                    sev, itype = "info", "enrichment_rejected"
                else:
                    stat["not_found"] += 1
                    sev, itype = "info", "enrichment_not_found"
                conn.execute(
                    "INSERT INTO meta_quality_issue (issue_id,run_id,entity_type,entity_id,field_name,"
                    "issue_type,severity,detail,created_at) VALUES (?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(issue_id) DO NOTHING",
                    (C.det_id("QI", "enrich", mid, decision["decision"]), run_id, "movie", mid,
                     "release_date", itype, sev,
                     json.dumps({"title": title, "year_hint": hint, "decision": decision},
                                ensure_ascii=False), C.now_iso()))
                continue

            stat["matched"] += 1

            # ---- 4. 补全被引用实体的标签（导演/类型/主演） ----
            ref_qids = best["directors"] + best["genres"] + best["cast"]
            labels: dict[str, str] = {}
            if ref_qids:
                ents2, _ = wb_entities(ref_qids, cache_dir, log)
                stat["requests"] += 1
                for q, e in ents2.items():
                    lab = (e.get("labels", {}).get("en", {}) or {}).get("value")
                    if lab:
                        labels[q] = lab

            # ---- 5. 登记来源记录（第三方，带快照 + 哈希 + URL） ----
            # 快照路径必须用「这次真正取到该实体的那个响应文件」。
            # 早先这里按单个 QID 重算文件名，但 wbgetentities 是**批量**接口：
            # 一次请求 8 个候选只落一个文件，文件名是 QID 列表的哈希，
            # 单个 QID 永远对不上 → snapshot_path 恒为 NULL，等于第三方来源没有证据。
            # 现在改为直接取本次请求的缓存路径，并如实说明该文件包含整批候选。
            rel_path = emeta.get("cache_path") if hits else None
            rec_id = C.det_id("REC", "wikidata", best["qid"])
            note = (f"Wikidata 实体 {best['qid']}（{best['label']}）；{decision['reason']}；"
                    f"年份线索={hint}({hint_src})；检索词={sinfo['variant_used']}；"
                    f"快照为批量查询响应，同批含 {len(hits)} 个候选实体")
            conn.execute(
                "INSERT OR IGNORE INTO raw_source_record (record_id,source_type,source_file_id,"
                "locator,url,snapshot_path,content_hash,fetched_at,note) VALUES (?,?,?,?,?,?,?,?,?)",
                (rec_id, "third_party", None, best["qid"],
                 f"https://www.wikidata.org/wiki/{best['qid']}", rel_path,
                 emeta.get("sha256"), emeta.get("fetched_at") or C.now_iso(), note))
            # 已存在的记录补一次快照路径与哈希。
            # 条件写成「路径空 或 哈希空」而不是只判路径：旧版本跑出来的记录里，
            # 有的路径已经写进去了但 content_hash 还是 NULL（当时没传这个字段）。
            # 证据链要求「定位 + 哈希」两者齐全，缺一个就不算可验证。
            if rel_path:
                conn.execute(
                    "UPDATE raw_source_record SET"
                    " snapshot_path=COALESCE(NULLIF(snapshot_path,''), ?),"
                    " content_hash=COALESCE(content_hash, ?)"
                    " WHERE record_id=? AND (snapshot_path IS NULL OR snapshot_path=''"
                    "                        OR content_hash IS NULL)",
                    (rel_path, emeta.get("sha256"), rec_id))

            def add(field: str, raw: str | None, norm: str | None, rule: str,
                    conf: float, num: float | None = None) -> None:
                nonlocal stat
                aid = C.det_id("A", "movie", mid, field, rec_id, norm)
                cur = conn.execute(
                    "INSERT INTO field_assertion (assertion_id,entity_type,entity_id,field_name,"
                    "raw_value,normalized_value,value_num,source_record_id,extractor_rule_id,"
                    "extractor_version,confidence,status,run_id,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(assertion_id) DO NOTHING",
                    (aid, "movie", mid, field, raw, norm, num, rec_id, rule,
                     EXTRACTOR_VERSION, conf, "candidate", run_id, C.now_iso()))
                if cur.rowcount:
                    stat["assertions"] += 1

            # 上映日期（theatrical）—— 保留原始精度
            if best["date_iso"]:
                prec = best["date_precision"] or "day"
                stat["precision"][prec] = stat["precision"].get(prec, 0) + 1
                add("release_date", best["date_iso"], best["date_iso"],
                    "R-EXT-DATE-THEATRICAL", 0.9,
                    float(best["date_iso"][:4]))
            # 外部标识 —— 支撑「先匹配作品再抽字段」
            add("external_id", best["qid"], best["qid"], "R-EXT-WD-QID", 0.95)
            if best["imdb"]:
                add("external_id", best["imdb"], best["imdb"], "R-EXT-IMDB-ID", 0.9)
            for q in best["genres"]:
                if q in labels:
                    add("genre", labels[q], C.norm_text(labels[q]), "R-EXT-WD-GENRE", 0.85)
            for q in best["directors"]:
                if q in labels:
                    add("director", labels[q], C.norm_person_name(labels[q]),
                        "R-EXT-WD-DIRECTOR", 0.85)
            for q in best["cast"]:
                if q in labels:
                    add("cast", labels[q], C.norm_person_name(labels[q]),
                        "R-EXT-WD-CAST", 0.7)

            # 这里**不再** sleep：等待已经下沉到 _http_json 的每一次真实请求之前。
            # 两处都等等于把间隔算了两次，而且按作品等待会让速率随「每部作品几个请求」
            # 而漂移 —— 限速的对象是请求。

        st["counters"].update(stat)

    summary = {
        "run_id": run_id,
        "extractor_version": EXTRACTOR_VERSION,
        "endpoint": WD_API,
        "sparql_endpoint_status": "HTTP 429（wdqs 故障限流），已降级到 MediaWiki REST API",
        "threshold": threshold,
        "budget_seconds": args.budget_seconds,
        "selection": {
            "order": args.order,
            "total_movies_in_db": total_movies,
            "selected": len(movies),
            "coverage_note": (
                "全库 %d 部作品，本次按「%s」选取前 %d 部补全。"
                "未选中的作品不是补全失败，而是**没有查询**，报告中必须与 not_found 分开表述。"
                % (total_movies, "商品版本数降序" if args.order == "importance" else "标题字母序",
                   len(movies))),
        },
        "elapsed_seconds": round(time.time() - t_start, 1),
        "throttle": {
            "policy": "429 → 间隔 ×2（封顶 max_delay）；连续 %d 次成功 → 间隔 −0.2s（不低于起始值）"
                      % THROTTLE.recover_after,
            "start_delay": THROTTLE.min_delay,
            "final_delay": THROTTLE.delay,
            "max_delay": THROTTLE.max_delay,
            "adjustments": len(THROTTLE.events),
            "events": THROTTLE.events[:40],
            "note": "config.enrich.delay_seconds 是**起始**间隔而非最终速率；"
                    "实际速率由站点反馈决定。全部调整记录在此，可核对限流发生的时间点。",
        },
        **stat,
        "note": "Wikidata 的 P577 是首次上映/发行日期；商品页的 Release date 是 DVD 发售日，"
                "两者语义不同，分别存为不同断言，由 s09 做冲突决策。"
                "date_precision 保留 Wikidata 的原始精度（day/month/year），"
                "年份级数据不会被补成 1 月 1 日。",
    }
    C.write_json(C.path("evidence", "s08_enrich_summary.json"), summary)
    log.info(f"查询 {stat['queried']} 部：匹配 {stat['matched']}，拒绝 {stat['rejected']}，"
             f"歧义 {stat['ambiguous']}，未找到 {stat['not_found']}；"
             f"新增断言 {stat['assertions']}；HTTP 请求 {stat['requests']}（缓存命中 {stat['cached']}）")
    if stat["precision"]:
        log.info(f"日期精度分布: {stat['precision']}")
    log.info(f"自适应限速：起始 {THROTTLE.min_delay}s → 结束 {THROTTLE.delay}s，"
             f"共调整 {len(THROTTLE.events)} 次（429 抬高 {sum(1 for e in THROTTLE.events if e['event']=='throttle_up')} 次）")

    C.end_run(conn, run_id, "success", json.dumps(stat, ensure_ascii=False))
    conn.close()
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
