"""生成"离线产品页面快照"教学夹具（fixtures）。

============================ 重要声明 ============================
这些快照**不是真实的 Amazon 页面**，是脚本合成的教学夹具，用于在
"实时采集不可行"的条件下演示页面解析、分类、实体合并、补全与血缘。

* 每份 HTML 里都有 `<!-- PROVENANCE: synthetic_fixture ... -->` 标记；
* 每份页面都有同名 .meta.json 旁注 `synthetic: true`；
* 入库时 source_type 记为 `synthetic_fixture`，可与真实来源区分；
* `evidence/provenance_audit.json` 会统计"有多少最终字段依赖合成来源"。

真正上线运行时应把 collect.mode 改成 live，这些夹具不会被使用。
=================================================================

它同时承担第二个职责：给出**评价集的真值**。因为作品归属是生成时决定的，
所以哪些 ASIN 属于同一部作品、哪些只是标题相似，脚本自己知道——
这就是 meta_eval_pair 里 label 的来源（见 s11 与手册第 9 章对"循环论证"的讨论）。

运行：python -m fixtures.make_offline_snapshots --max-products 1200
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import common as C

GENERATOR_VERSION = "fixture-v1.0.0"

# --------------------------------------------------------------------------- #
# 作品目录（真实电影，用于生成可理解的夹具；权威字段在 s08 由 Wikidata 覆盖）
# key, 标题, 上映年, 导演, 主演, 类型, 制片厂
# --------------------------------------------------------------------------- #
WORKS: list[dict] = [
    dict(key="matrix_1999", title="The Matrix", year=1999,
         directors=["Lana Wachowski", "Lilly Wachowski"],
         cast=["Keanu Reeves", "Laurence Fishburne", "Carrie-Anne Moss"],
         genres=["science fiction", "action"], studio="Warner Home Video"),
    dict(key="matrix_reloaded_2003", title="The Matrix Reloaded", year=2003,
         directors=["Lana Wachowski", "Lilly Wachowski"],
         cast=["Keanu Reeves", "Laurence Fishburne", "Hugo Weaving"],
         genres=["science fiction", "action"], studio="Warner Home Video"),
    dict(key="matrix_revolutions_2003", title="The Matrix Revolutions", year=2003,
         directors=["Lana Wachowski", "Lilly Wachowski"],
         cast=["Keanu Reeves", "Carrie-Anne Moss", "Hugo Weaving"],
         genres=["science fiction", "action"], studio="Warner Home Video"),
    dict(key="true_grit_1969", title="True Grit", year=1969,
         directors=["Henry Hathaway"],
         cast=["John Wayne", "Kim Darby", "Glen Campbell"],
         genres=["western", "drama"], studio="Paramount"),
    dict(key="true_grit_2010", title="True Grit", year=2010,
         directors=["Joel Coen", "Ethan Coen"],
         cast=["Jeff Bridges", "Hailee Steinfeld", "Matt Damon"],
         genres=["western", "drama"], studio="Paramount"),
    dict(key="oceans_eleven_1960", title="Ocean's Eleven", year=1960,
         directors=["Lewis Milestone"],
         cast=["Frank Sinatra", "Dean Martin", "Sammy Davis Jr."],
         genres=["crime", "comedy"], studio="Warner Home Video"),
    dict(key="oceans_eleven_2001", title="Ocean's Eleven", year=2001,
         directors=["Steven Soderbergh"],
         cast=["George Clooney", "Brad Pitt", "Julia Roberts"],
         genres=["crime", "comedy"], studio="Warner Home Video"),
    dict(key="dragon_tattoo_2009", title="The Girl with the Dragon Tattoo", year=2009,
         directors=["Niels Arden Oplev"],
         cast=["Michael Nyqvist", "Noomi Rapace"],
         genres=["thriller", "drama"], studio="Music Box Films"),
    dict(key="dragon_tattoo_2011", title="The Girl with the Dragon Tattoo", year=2011,
         directors=["David Fincher"],
         cast=["Daniel Craig", "Rooney Mara"],
         genres=["thriller", "drama"], studio="Sony Pictures"),
    dict(key="blade_runner_1982", title="Blade Runner", year=1982,
         directors=["Ridley Scott"],
         cast=["Harrison Ford", "Rutger Hauer", "Sean Young"],
         genres=["science fiction", "thriller"], studio="Warner Home Video"),
    dict(key="blade_runner_2049_2017", title="Blade Runner 2049", year=2017,
         directors=["Denis Villeneuve"],
         cast=["Ryan Gosling", "Harrison Ford"],
         genres=["science fiction", "thriller"], studio="Warner Home Video"),
    dict(key="alien_1979", title="Alien", year=1979,
         directors=["Ridley Scott"],
         cast=["Sigourney Weaver", "Tom Skerritt"],
         genres=["science fiction", "horror"], studio="20th Century Fox"),
    dict(key="aliens_1986", title="Aliens", year=1986,
         directors=["James Cameron"],
         cast=["Sigourney Weaver", "Michael Biehn"],
         genres=["science fiction", "action"], studio="20th Century Fox"),
    dict(key="lotr_fellowship_2001", title="The Lord of the Rings: The Fellowship of the Ring", year=2001,
         directors=["Peter Jackson"],
         cast=["Elijah Wood", "Ian McKellen", "Viggo Mortensen"],
         genres=["fantasy", "adventure"], studio="New Line Home Video"),
    dict(key="lotr_two_towers_2002", title="The Lord of the Rings: The Two Towers", year=2002,
         directors=["Peter Jackson"],
         cast=["Elijah Wood", "Ian McKellen", "Viggo Mortensen"],
         genres=["fantasy", "adventure"], studio="New Line Home Video"),
    dict(key="lotr_return_2003", title="The Lord of the Rings: The Return of the King", year=2003,
         directors=["Peter Jackson"],
         cast=["Elijah Wood", "Ian McKellen", "Viggo Mortensen"],
         genres=["fantasy", "adventure"], studio="New Line Home Video"),
    dict(key="inception_2010", title="Inception", year=2010,
         directors=["Christopher Nolan"],
         cast=["Leonardo DiCaprio", "Joseph Gordon-Levitt", "Elliot Page"],
         genres=["science fiction", "thriller"], studio="Warner Home Video"),
    dict(key="interstellar_2014", title="Interstellar", year=2014,
         directors=["Christopher Nolan"],
         cast=["Matthew McConaughey", "Anne Hathaway", "Jessica Chastain"],
         genres=["science fiction", "drama"], studio="Paramount"),
    dict(key="dark_knight_2008", title="The Dark Knight", year=2008,
         directors=["Christopher Nolan"],
         cast=["Christian Bale", "Heath Ledger", "Aaron Eckhart"],
         genres=["action", "crime"], studio="Warner Home Video"),
    dict(key="batman_begins_2005", title="Batman Begins", year=2005,
         directors=["Christopher Nolan"],
         cast=["Christian Bale", "Liam Neeson", "Michael Caine"],
         genres=["action", "crime"], studio="Warner Home Video"),
    dict(key="pulp_fiction_1994", title="Pulp Fiction", year=1994,
         directors=["Quentin Tarantino"],
         cast=["John Travolta", "Samuel L. Jackson", "Uma Thurman"],
         genres=["crime", "drama"], studio="Miramax"),
    dict(key="kill_bill_1_2003", title="Kill Bill: Vol. 1", year=2003,
         directors=["Quentin Tarantino"],
         cast=["Uma Thurman", "Lucy Liu", "Vivica A. Fox"],
         genres=["action", "thriller"], studio="Miramax"),
    dict(key="kill_bill_2_2004", title="Kill Bill: Vol. 2", year=2004,
         directors=["Quentin Tarantino"],
         cast=["Uma Thurman", "David Carradine"],
         genres=["action", "thriller"], studio="Miramax"),
    dict(key="spirited_away_2001", title="Spirited Away", year=2001,
         directors=["Hayao Miyazaki"],
         cast=["Rumi Hiiragi", "Miyu Irino"],
         genres=["animation", "fantasy"], studio="Studio Ghibli"),
    dict(key="mononoke_1997", title="Princess Mononoke", year=1997,
         directors=["Hayao Miyazaki"],
         cast=["Yoji Matsuda", "Yuriko Ishida"],
         genres=["animation", "fantasy"], studio="Studio Ghibli"),
    dict(key="seven_samurai_1954", title="Seven Samurai", year=1954,
         directors=["Akira Kurosawa"],
         cast=["Toshiro Mifune", "Takashi Shimura"],
         genres=["drama", "action"], studio="Criterion Collection"),
    dict(key="rashomon_1950", title="Rashomon", year=1950,
         directors=["Akira Kurosawa"],
         cast=["Toshiro Mifune", "Machiko Kyo"],
         genres=["drama", "crime"], studio="Criterion Collection"),
    dict(key="amelie_2001", title="Amélie", year=2001,
         directors=["Jean-Pierre Jeunet"],
         cast=["Audrey Tautou", "Mathieu Kassovitz"],
         genres=["comedy", "romance"], studio="Miramax"),
    dict(key="cinema_paradiso_1988", title="Cinema Paradiso", year=1988,
         directors=["Giuseppe Tornatore"],
         cast=["Philippe Noiret", "Salvatore Cascio"],
         genres=["drama", "romance"], studio="Arrow Video"),
    dict(key="shawshank_1994", title="The Shawshank Redemption", year=1994,
         directors=["Frank Darabont"],
         cast=["Tim Robbins", "Morgan Freeman"],
         genres=["drama"], studio="Warner Home Video"),
    dict(key="godfather_1972", title="The Godfather", year=1972,
         directors=["Francis Ford Coppola"],
         cast=["Marlon Brando", "Al Pacino", "James Caan"],
         genres=["crime", "drama"], studio="Paramount"),
    dict(key="godfather_2_1974", title="The Godfather Part II", year=1974,
         directors=["Francis Ford Coppola"],
         cast=["Al Pacino", "Robert De Niro", "Robert Duvall"],
         genres=["crime", "drama"], studio="Paramount"),
    dict(key="apocalypse_now_1979", title="Apocalypse Now", year=1979,
         directors=["Francis Ford Coppola"],
         cast=["Martin Sheen", "Marlon Brando", "Robert Duvall"],
         genres=["war", "drama"], studio="Lionsgate"),
    dict(key="casablanca_1942", title="Casablanca", year=1942,
         directors=["Michael Curtiz"],
         cast=["Humphrey Bogart", "Ingrid Bergman"],
         genres=["romance", "drama"], studio="Warner Home Video"),
    dict(key="taxi_driver_1976", title="Taxi Driver", year=1976,
         directors=["Martin Scorsese"],
         cast=["Robert De Niro", "Jodie Foster"],
         genres=["drama", "crime"], studio="Sony Pictures"),
    dict(key="goodfellas_1990", title="Goodfellas", year=1990,
         directors=["Martin Scorsese"],
         cast=["Robert De Niro", "Ray Liotta", "Joe Pesci"],
         genres=["crime", "drama"], studio="Warner Home Video"),
    dict(key="space_jam_1996", title="Space Jam", year=1996,
         directors=["Joe Pytka"],
         cast=["Michael Jordan", "Bugs Bunny", "Bill Murray"],
         genres=["animation", "comedy"], studio="Warner Home Video"),
    dict(key="creed_2015", title="Creed", year=2015,
         directors=["Ryan Coogler"],
         cast=["Michael B. Jordan", "Sylvester Stallone"],
         genres=["drama", "sport"], studio="Warner Home Video"),
    dict(key="rocky_1976", title="Rocky", year=1976,
         directors=["John G. Avildsen"],
         cast=["Sylvester Stallone", "Talia Shire"],
         genres=["drama", "sport"], studio="MGM"),
    dict(key="silence_lambs_1991", title="The Silence of the Lambs", year=1991,
         directors=["Jonathan Demme"],
         cast=["Jodie Foster", "Anthony Hopkins"],
         genres=["thriller", "crime"], studio="Criterion Collection"),
]

WORK_BY_KEY = {w["key"]: w for w in WORKS}

# 版本噪声写法：模拟真实商品标题的混乱程度
EDITION_STYLES = [
    ("DVD", "{t} (DVD)", "DVD"),
    ("Blu-ray", "{t} [Blu-ray]", "Blu-ray"),
    ("DVD2", "{t} (2-Disc Special Edition)", "DVD"),
    ("WS", "{t} (Widescreen Edition)", "DVD"),
    ("CE", "{t} (Collector's Edition) [Blu-ray]", "Blu-ray"),
    ("INV", "{t}, The (DVD)", "DVD"),
    ("PRIME", "{t}", "Prime Video"),
]

# 真实存在的人名写法差异（用于任务 5 的别名/消歧难例）
# 这一组是「同一个人的不同名字」（真正的改名/艺名），不是拼写差异。
CREDIT_VARIANTS = {
    "Lana Wachowski": ["Larry Wachowski", "Lana Wachowski"],
    "Jean-Pierre Jeunet": ["Jean Pierre Jeunet", "Jean-Pierre Jeunet"],
    "Toshiro Mifune": ["Toshirô Mifune", "Toshiro Mifune"],
    "Samuel L. Jackson": ["Samuel L Jackson", "Samuel L. Jackson"],
}

# 同姓但**不同人**的真实演员家庭。
# 它们存在的意义：给「人物消歧」提供真正难的负例——
# 只看姓氏或只看字符串相似度会把父子/兄妹判成同一个人，
# 而正确的做法要靠「是否共同出演同一部作品」这类结构性证据。
FAMILY_POOL = [
    ("Michael Douglas", "Kirk Douglas"),
    ("Jake Gyllenhaal", "Maggie Gyllenhaal"),
    ("Ben Affleck", "Casey Affleck"),
    ("Luke Wilson", "Owen Wilson"),
    ("John Cusack", "Joan Cusack"),
    ("Jason Schwartzman", "Robert Schwartzman"),
    ("Chris Hemsworth", "Liam Hemsworth"),
    ("Elle Fanning", "Dakota Fanning"),
    ("Kate Mara", "Rooney Mara"),
    ("Sean Penn", "Chris Penn"),
    ("Jeff Bridges", "Beau Bridges"),
    ("Ralph Fiennes", "Joseph Fiennes"),
    ("Jeremy Irons", "Max Irons"),
    ("Martin Sheen", "Charlie Sheen"),
    ("Donald Sutherland", "Kiefer Sutherland"),
    ("Stellan Skarsgård", "Alexander Skarsgård"),
    ("Anjelica Huston", "John Huston"),
    ("Peter Fonda", "Bridget Fonda"),
    ("Vanessa Redgrave", "Lynn Redgrave"),
    ("David Carradine", "Keith Carradine"),
    ("Bill Murray", "Brian Doyle-Murray"),
    ("Sofia Coppola", "Roman Coppola"),
    ("Jason Reitman", "Ivan Reitman"),
    ("Bryce Dallas Howard", "Ron Howard"),
    ("Colin Hanks", "Tom Hanks"),
    ("Zosia Mamet", "David Mamet"),
    ("Rashida Jones", "Quincy Jones"),
    ("Charlie Day", "Jordan Day"),
    ("Domhnall Gleeson", "Brendan Gleeson"),
    ("Jack Quaid", "Randy Quaid"),
]


def credit_forms(name: str) -> list[str]:
    """给一个人名生成若干「真实世界里确实会出现」的写法。

    为什么需要这个：
        Amazon 的商品页上，同一个人可能写成
        「John Smith」「Smith, John」「J. Smith」「John Smith」……
        这些写法在字符串层面完全不同，但对人来说是同一个人。
        实体消歧任务的第一道关就是「把这些写法归到同一个实体」，
        所以夹具必须真的产出这些写法，否则评价集没有正例可测。
    """
    forms = {name}
    clean = name.replace(".", "").replace("-", " ").strip()
    parts = clean.split()
    if len(parts) >= 2:
        first, last = parts[0], parts[-1]
        forms.add(f"{last}, {first}")                       # 图书馆/目录式
        forms.add(f"{first[0]}. {last}")                    # 首字母缩写
        if len(parts) >= 3:
            forms.add(f"{first} {last}")                    # 去掉中间名
            forms.add(f"{last}, {first} {parts[1]}")        # 目录式带中间名
    # 注意：这里**故意不**随机加变音符号。
    # 早先版本会把任意名字里的 o 换成 ô（造出 "Jacksôn" 这种不存在的写法），
    # 结果"正例"全变成了只差一个重音的 trivial 例子，测不出真本事。
    # 真实的变音差异只在少数名字上出现，已写进 CREDIT_VARIANTS（Toshirô Mifune）。
    return sorted(forms)


# 标题的拼写/重音差异（同一作品，不同商品页写法不同）
TITLE_VARIANTS = {
    "Amélie": ["Amelie"],
}

NON_MOVIE_TEMPLATES = [
    ("{t}: Original Motion Picture Soundtrack (Audio CD)", "soundtrack CD"),
    ("{t} Movie Poster 27x40 Reprint", "poster"),
    ("{t}: The Official Novelization (Paperback)", "book"),
    ("{t} - Video Game (PlayStation 2)", "video game"),
]

BOX_SETS = [
    ("The Matrix Collection (The Matrix / The Matrix Reloaded / The Matrix Revolutions) [Blu-ray]",
     ["matrix_1999", "matrix_reloaded_2003", "matrix_revolutions_2003"]),
    ("The Lord of the Rings Trilogy (Extended Edition) [Blu-ray]",
     ["lotr_fellowship_2001", "lotr_two_towers_2002", "lotr_return_2003"]),
    ("The Godfather Collection (The Godfather / The Godfather Part II) [DVD]",
     ["godfather_1972", "godfather_2_1974"]),
    ("Kill Bill Vol. 1 & 2 Double Feature [DVD]",
     ["kill_bill_1_2003", "kill_bill_2_2004"]),
]

SERIES_ITEMS = [
    "Star Trek: The Next Generation - The Complete First Season (DVD)",
    "The Wire: The Complete Series (DVD)",
]
CONCERT_ITEMS = [
    "Metallica: S&M - Live in Concert (DVD)",
    "Pink Floyd: Pulse - Live at Earls Court (DVD)",
]


def html_page(spec: dict) -> str:
    """生成一份"看起来像商品页"的 HTML，但带明显的合成标记。"""
    w = spec.get("work") or {}
    title = spec["title"]
    lines = [
        "<!doctype html>",
        "<html><head><meta charset=\"utf-8\">",
        f"<title>Amazon.com: {title}</title></head><body>",
        "<!-- PROVENANCE: synthetic_fixture; generated by fixtures/make_offline_snapshots.py;",
        "     THIS IS NOT REAL AMAZON CONTENT. For coursework demonstration only. -->",
        f"<div id=\"productTitle\">{title}</div>",
    ]
    if spec["page_kind"] == "product":
        byline = []
        dirs = spec.get("directors_list") or w.get("directors") or []
        cast = spec.get("cast_list") or w.get("cast") or []
        if dirs:
            byline.append("Directed by: " + ", ".join(dirs))
        if cast:
            byline.append("Starring: " + ", ".join(cast))
        lines += [
            f"<div id=\"byline\">{' | '.join(byline)}</div>",
            "<div id=\"productDetails\"><ul>",
            f"<li><b>Format:</b> {spec.get('format','DVD')}</li>",
            f"<li><b>Release date:</b> {spec.get('dvd_date','')}</li>",
            f"<li><b>Studio:</b> {w.get('studio','')}</li>",
            f"<li><b>ASIN:</b> {spec['asin']}</li>",
            "</ul></div>",
            f"<div id=\"productDescription\">{w.get('genres',[''])[0].title()} title.</div>",
        ]
    elif spec["page_kind"] == "captcha":
        lines.append("<div id=\"captcha\">Enter the characters you see below. "
                     "Sorry, we just need to make sure you're not a robot.</div>")
    elif spec["page_kind"] == "not_found":
        lines.append("<div id=\"notFound\">Page Not Found. We're sorry. "
                     "The Web address you entered is not a functioning page on our site.</div>")
    elif spec["page_kind"] == "redirect":
        lines.append("<div id=\"redirectNotice\">This item is no longer available. "
                     "Showing a related item instead.</div>")
        lines.append("<div id=\"productTitle\">Generic DVD Lot (10 Movies)</div>")
    lines.append("</body></html>")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--max-products", type=int, default=None)
    ap.add_argument("--out-dir", default="data/snapshots/pages")
    args = ap.parse_args(argv)

    conn = C.connect(args.db)
    log = C.Logger("make_fixtures")
    out_dir = C.path(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    limit = args.max_products or int(C.CFG["collect"]["max_products"])
    asins = [r["product_id"] for r in conn.execute(
        "SELECT product_id FROM product ORDER BY review_count DESC, product_id LIMIT ?", (limit,))]
    if not asins:
        log.error("product 表为空，请先跑 s01")
        return 2
    log.info(f"准备为 {len(asins)} 个 ASIN 生成离线快照夹具")

    # 1) 组装"产品规格"清单：作品版本 -> 非电影 -> 合集 -> 剧集 -> 演唱会
    specs: list[dict] = []
    rng = random.Random(20261103)  # 固定种子 -> 可复现
    credit_map: dict[str, str] = {}   # 原始署名 -> 真实人物（评价集真值）

    def maybe_variant(name: str) -> str:
        """按一定概率把一个规范人名替换成它在商品页上的另一种写法。

        替换后仍然登记 credit_map[写法] = 规范名，
        这就是评价集里「正例」的真值来源。
        """
        pool = list(credit_forms(name))
        if name in CREDIT_VARIANTS:
            pool += CREDIT_VARIANTS[name]
        pool = sorted(set(pool))
        # ★ 一个写法只能属于一个人，否则真值本身就是歧义的。
        #   反例：Wachowski 姐妹的首字母都是 L，"L. Wachowski" 对 Lana 和 Lilly 都成立。
        #   如果两边都生成这个写法，credit_map 会被后写的那次覆盖，
        #   评价集里就会出现「真值说是同一人、但实际上无法判定」的伪正例。
        #   现实里页面也不会对同姓同首字母的两个人用同一个缩写，所以直接跳过这种写法。
        pool = [v for v in pool if v == name or credit_map.get(v, name) == name]
        if len(pool) > 1 and rng.random() < 0.55:
            v = rng.choice(pool)
            credit_map[v] = name
            return v
        credit_map.setdefault(name, name)
        return name

    for w in WORKS:
        # 同一部作品在电商平台上会有很多在售版本（不同碟数/包装/发行批次），
        # 这里用「版本样式 × 再版标记」组合出约 21 个 SKU，
        # 目的是让「同一作品的不同商品版本」这个正例有足够样本。
        for style_key, tpl, fmt in EDITION_STYLES:
            # 「倒装」写法只对以冠词开头的标题成立（"The Matrix" -> "Matrix, The"）。
            # 对 "Aliens" 硬套会造出 "Aliens, The" 这种现实中不存在的标题，
            # 反而制造出一个假的"不同作品"，污染后续消歧。
            if style_key == "INV" and not re.match(r"^(the|a|an)\s", w["title"], re.I):
                continue
            for reissue in ("", " (Reissue)", " (Anniversary Edition)"):
                # 同一作品的不同商品页，标题写法/演职员写法可能不同 —— 这是消歧的真实难点
                title_src = w["title"]
                if title_src in TITLE_VARIANTS and rng.random() < 0.5:
                    title_src = rng.choice(TITLE_VARIANTS[title_src])
                title = tpl.format(t=title_src) + reissue
                # 商品页上的"发售日期"故意取影院上映后 6~14 个月（真实世界的 DVD 窗口期），
                # 这样后面与 Wikidata 的影院上映日期天然冲突 —— 这是任务 6/7 的考点。
                dvd = date(w["year"], 1, 1) + timedelta(days=rng.randint(180, 420))
                specs.append(dict(work_key=w["key"], work=w, title=title, format=fmt,
                                  dvd_date=dvd.isoformat(), expected_category="movie",
                                  members=[w["key"]], page_kind="product", style=style_key,
                                  directors_list=[maybe_variant(d) for d in w["directors"]],
                                  cast_list=[maybe_variant(c) for c in w["cast"]]))

    # 3) 注入「同姓不同人」的真实演员家庭，制造人物消歧的难负例。
    #    做法：把 FAMILY_POOL 摊平后随机分给一部分作品，各追加一名演员。
    #    这样语料里会出现「Michael Douglas」和「Kirk Douglas」分别在两部作品里，
    #    姓氏相同、字符串相似，但**不是同一个人**——只看姓氏的合并策略会在这里出错。
    fam_pool = [n for pair in FAMILY_POOL for n in pair]
    rng.shuffle(fam_pool)
    by_work_specs: dict[str, list[dict]] = {}
    for sp in specs:
        if sp["work_key"]:
            by_work_specs.setdefault(sp["work_key"], []).append(sp)
    work_keys = list(by_work_specs)
    rng.shuffle(work_keys)
    for key, extra in zip(work_keys, fam_pool):
        for sp in by_work_specs[key]:
            sp["cast_list"] = list(sp["cast_list"]) + [maybe_variant(extra)]

    for w in rng.sample(WORKS, 10):
        tpl, _kind = rng.choice(NON_MOVIE_TEMPLATES)
        specs.append(dict(work_key=None, work=w, title=tpl.format(t=w["title"]),
                          format="", dvd_date="", expected_category="non_movie",
                          members=[], page_kind="product", style="NONMOVIE"))

    for title, members in BOX_SETS:
        specs.append(dict(work_key=None, work=WORK_BY_KEY[members[0]], title=title,
                          format="Blu-ray", dvd_date=f"{max(WORK_BY_KEY[m]['year'] for m in members)}-11-15",
                          expected_category="box_set", members=members,
                          page_kind="product", style="BOXSET"))

    for title in SERIES_ITEMS:
        specs.append(dict(work_key=None, work=WORK_BY_KEY["matrix_1999"], title=title,
                          format="DVD", dvd_date="2002-07-02", expected_category="series",
                          members=[], page_kind="product", style="SERIES"))

    for title in CONCERT_ITEMS:
        specs.append(dict(work_key=None, work=WORK_BY_KEY["rocky_1976"], title=title,
                          format="DVD", dvd_date="2001-05-08", expected_category="concert_other",
                          members=[], page_kind="product", style="CONCERT"))

    # 2) 把 ASIN 分配给规格（不足则循环，多余则留空 -> 后面记为未采集）
    rng.shuffle(specs)
    manifest_rows: list[dict] = []
    n_pages = 0
    for i, asin in enumerate(asins):
        if i >= len(specs):
            break
        spec = dict(specs[i])
        spec["asin"] = asin
        # 8% 的页面模拟失败（验证码/不存在/重定向），让采集状态机有真实分布
        roll = rng.random()
        if roll < 0.04:
            spec["page_kind"] = "captcha"
        elif roll < 0.07:
            spec["page_kind"] = "not_found"
        elif roll < 0.08:
            spec["page_kind"] = "redirect"

        html = html_page(spec)
        fp = os.path.join(out_dir, f"{asin}.html")
        with open(fp, "w", encoding="utf-8", newline="\n") as f:
            f.write(html)
        content_hash = C.sha256_text(html)
        meta = {
            "asin": asin,
            "page_kind": spec["page_kind"],
            "expected_category": spec["expected_category"],
            "truth_work_key": spec["work_key"] or (spec["members"][0] if len(spec["members"]) == 1 else None),
            "truth_members": spec["members"],
            "title_on_page": spec["title"] if spec["page_kind"] == "product" else None,
            "synthetic": True,
            "generator_version": GENERATOR_VERSION,
            "generated_at": C.now_iso(),
            "content_hash": content_hash,
        }
        with open(os.path.join(out_dir, f"{asin}.meta.json"), "w", encoding="utf-8", newline="\n") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        manifest_rows.append(meta)
        n_pages += 1

    C.write_json(C.path("fixtures", "fixture_manifest.json"), {
        "generator_version": GENERATOR_VERSION,
        "generated_at": C.now_iso(),
        "works": len(WORKS),
        "products_with_snapshot": n_pages,
        "products_total": len(asins),
        "disclaimer": "合成教学夹具，非真实 Amazon 页面",
    })
    C.write_csv(C.path("fixtures", "fixture_manifest.csv"),
                ["asin", "page_kind", "expected_category", "truth_work_key", "truth_members", "content_hash"],
                ([r["asin"], r["page_kind"], r["expected_category"], r["truth_work_key"],
                  "|".join(r["truth_members"]), r["content_hash"]] for r in manifest_rows))
    # 人物真值：原始署名 -> 真实人物（用于任务 5 的评价集）
    C.write_json(C.path("fixtures", "person_truth.json"),
                 {"credit_to_person": credit_map,
                  "note": "由夹具生成规则决定，仅对合成夹具成立"})
    log.info(f"已生成 {n_pages} 份快照 -> {out_dir}")
    log.info(f"清单 -> {C.path('fixtures','fixture_manifest.csv')}")
    conn.close()
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
