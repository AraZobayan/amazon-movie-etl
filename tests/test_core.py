"""单元测试：把"最容易出错、最值得讲"的纯函数钉死。

运行：python -m unittest discover -s tests -v

为什么这些函数值得单独测？
    它们是整条链的地基：标题规范化错了，分块和打分全错；
    人名规范化错了，人物会成批误合并；确定性 ID 错了，幂等就没了。
"""
from __future__ import annotations

import unittest

from src import common as C
from src.s01_ingest_reviews import parse_helpfulness
from src.s04_parse_and_classify import classify, parse_page
from src.s06_match_movies import DSU, clean_title, score_pair, title_relation
from src.s07_resolve_persons import abbrev_kind, is_initials_of, pair_score, suffix_of, surname
from src.s10_quality_checks import prf


class TestNormalization(unittest.TestCase):
    def test_norm_title_strips_edition_noise(self):
        # 注意期望值：冠词倒装要还原成 "the matrix"，
        # 而不是留着 "matrix the" —— 否则 "Matrix, The" 会被当成另一部作品
        cases = {
            "The Matrix (DVD)": "the matrix",
            "The Matrix [Blu-ray]": "the matrix",
            "The Matrix (2-Disc Special Edition)": "the matrix",
            "Matrix, The": "the matrix",
            "Amélie": "amelie",
        }
        for raw, want in cases.items():
            self.assertEqual(C.norm_title(raw), want, raw)

    def test_norm_title_keeps_real_words(self):
        # "Widescreen" 是噪声，但 "Widescreen" 之外的实词必须留下
        self.assertEqual(C.norm_title("True Grit (Widescreen Edition)"), "true grit")

    def test_norm_person_name_last_first(self):
        self.assertEqual(C.norm_person_name("Reeves, Keanu"), "keanu reeves")

    def test_norm_person_name_initials_and_diacritics(self):
        self.assertEqual(C.norm_person_name("J. R. R. Tolkien"), "jrr tolkien")
        self.assertEqual(C.norm_person_name("Toshirô Mifune"), "toshiro mifune")
        self.assertEqual(C.norm_person_name("Jean-Pierre Jeunet"), "jean pierre jeunet")

    def test_det_id_is_deterministic(self):
        a = C.det_id("M", "the matrix", 1999)
        b = C.det_id("M", "the matrix", 1999)
        c = C.det_id("M", "the matrix", 2003)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(a.startswith("M-"))


class TestSimilarity(unittest.TestCase):
    def test_identical_title(self):
        self.assertEqual(C.title_similarity("The Matrix", "The Matrix (DVD)"), 1.0)

    def test_sequel_is_prefix(self):
        self.assertEqual(title_relation("the matrix", "the matrix reloaded"), "prefix")

    def test_similarity_symmetric(self):
        x, y = C.title_similarity("True Grit", "True Grit"), C.title_similarity("True Grit", "True Grit")
        self.assertEqual(x, y)

    def test_clean_title(self):
        self.assertEqual(clean_title("The Matrix (2-Disc Special Edition)"), "The Matrix")
        self.assertEqual(clean_title("Kill Bill: Vol. 1 [Blu-ray]"), "Kill Bill: Vol. 1")


class TestScoring(unittest.TestCase):
    def mk(self, title, year, dirs, cast):
        return {"title": title, "title_norm": C.norm_title(title), "year": year,
                "directors": {C.norm_person_name(d) for d in dirs},
                "cast": {C.norm_person_name(c) for c in cast}}

    def test_same_title_same_crew_merges(self):
        a = self.mk("The Matrix (DVD)", 1999, ["Lana Wachowski"], ["Keanu Reeves"])
        b = self.mk("The Matrix [Blu-ray]", 2004, ["Lana Wachowski"], ["Keanu Reeves"])
        s, f = score_pair(a, b)
        self.assertGreaterEqual(s, 0.86, f)

    def test_sequel_does_not_merge(self):
        a = self.mk("The Matrix", 1999, ["Lana Wachowski"], ["Keanu Reeves"])
        b = self.mk("The Matrix Reloaded", 2003, ["Lana Wachowski"], ["Keanu Reeves"])
        s, f = score_pair(a, b)
        self.assertLess(s, 0.62, f)
        self.assertEqual(f["title_relation"], "prefix")

    def test_remake_does_not_merge(self):
        a = self.mk("True Grit", 1969, ["Henry Hathaway"], ["John Wayne"])
        b = self.mk("True Grit", 2010, ["Joel Coen", "Ethan Coen"], ["Jeff Bridges"])
        s, f = score_pair(a, b)
        self.assertLess(s, 0.62, f)


class TestPersonRules(unittest.TestCase):
    def test_initials_detection(self):
        # 一个词压缩多个词
        self.assertTrue(is_initials_of("jrr tolkien", "john ronald reuel tolkien"))
        # 首名缩写（等长，也要能识别——这是最容易被写错的一类）
        self.assertTrue(is_initials_of("h hathaway", "henry hathaway"))
        # 省略了单字母中间名
        self.assertTrue(is_initials_of("s jackson", "samuel l jackson"))
        # 结构上兼容，但"是否同一人"要靠决策规则，不能靠这个函数
        self.assertTrue(is_initials_of("michael jordan", "michael b jordan"))
        # 真的不同
        self.assertFalse(is_initials_of("henry hathaway", "michael hathaway"))

    def test_bare_surname_is_not_an_abbreviation(self):
        # 亚马逊演职员表经常只写姓氏，那是信息缺失，不是缩写。
        # 如果不挡，"coppola" 会吃掉 "f coppola" 里的 f，
        # 把 Francis Coppola 和 Sofia Coppola(S. Coppola) 合并成一个实体。
        self.assertFalse(is_initials_of("coppola", "f coppola"))
        self.assertFalse(is_initials_of("clooney", "george clooney"))
        self.assertFalse(is_initials_of("mara", "kate mara"))
        # 但"纯缩写"（首字母串，且覆盖整个名字）仍要能识别
        self.assertTrue(is_initials_of("jrt", "john ronald tolkien"))
        # 而只覆盖名字一部分的缩写串不算（缺姓氏，无法确认身份）
        self.assertFalse(is_initials_of("jrr", "john ronald reuel tolkien"))

    def test_first_name_must_not_be_skipped(self):
        # "跳过单字母"只能跳中间名，不能跳首名。
        # 否则 "luke wilson" vs "o wilson" 会被判成缩写（跳过 o），
        # 把 Luke Wilson 和 Owen Wilson 这对兄弟合并。
        self.assertFalse(is_initials_of("luke wilson", "o wilson"))
        self.assertFalse(is_initials_of("michael douglas", "k douglas"))
        # 中间名单字母仍然可以跳
        self.assertTrue(is_initials_of("s jackson", "samuel l jackson"))

    def test_abbrev_kind_distinguishes_middle_name(self):
        # 首名被缩写 -> 可信
        self.assertEqual(abbrev_kind("h hathaway", "henry hathaway"), "first_abbrev")
        # 只有中间名不同 -> 同名不同人的典型形态，必须交人工
        self.assertEqual(abbrev_kind("michael jordan", "michael b jordan"), "middle_only")

    def test_surname(self):
        self.assertEqual(surname("sammy davis jr"), "davis")
        self.assertEqual(surname("keanu reeves"), "reeves")

    def test_same_surname_is_not_enough(self):
        # 同姓、名完全不同（父子/兄妹）-> 只是 same_surname，分数远不够合并
        score, rel = pair_score("michael douglas", "kirk douglas")
        self.assertEqual(rel, "same_surname")
        self.assertLess(score, 0.9)

    def test_suffix_is_a_distinguishing_signal(self):
        # 后缀是父子唯一的区分信号，不能被当噪声剥掉
        self.assertEqual(suffix_of("dean martin jr"), "jr")
        self.assertEqual(suffix_of("dean martin"), "")
        # 结构上 "d martin jr" 和 "dean martin" 兼容（首名缩写），
        # 所以决策必须靠后缀差异把它们拦下来（R-PER-SUFFIX-DIFF）
        self.assertNotEqual(suffix_of("d martin jr"), suffix_of("dean martin"))

    def test_middle_name_difference_is_not_auto_merge(self):
        # 首名、姓都相同，只差一个中间名首字母 -> 关系是 middle_only，
        # 这类必须交人工，因为 Michael Jordan / Michael B. Jordan 就是两个人
        score, rel = pair_score("michael jordan", "michael b jordan")
        self.assertEqual(rel, "initials_middle_only")


class TestDSU(unittest.TestCase):
    def test_transitivity(self):
        d = DSU()
        d.union("a", "b")
        d.union("b", "c")
        self.assertEqual(d.find("a"), d.find("c"))
        g = d.groups()
        self.assertEqual(len(g), 1)
        self.assertEqual(sorted(next(iter(g.values()))), ["a", "b", "c"])

    def test_separate_components(self):
        d = DSU()
        d.union("a", "b")
        d.union("c", "d")
        self.assertEqual(len(d.groups()), 2)


class TestParsing(unittest.TestCase):
    def test_helpfulness_normal(self):
        self.assertEqual(parse_helpfulness("3/4"), (3, 4, None))

    def test_helpfulness_bad(self):
        self.assertEqual(parse_helpfulness("5/4")[2], "helpful_exceeds_total")
        self.assertEqual(parse_helpfulness("-1/4")[2], "negative_votes")
        self.assertEqual(parse_helpfulness("abc")[2], "malformed_helpfulness")

    def test_parse_page(self):
        html = ('<div id="productTitle">The Matrix (DVD)</div>'
                '<div id="byline">Directed by: Lana Wachowski | Starring: Keanu Reeves</div>'
                '<li><b>Format:</b> DVD</li><li><b>Release date:</b> 2000-03-15</li>'
                '<li><b>Studio:</b> Warner</li><li><b>ASIN:</b> B000123456</li>')
        p = parse_page(html, "B000123456")
        self.assertEqual(p["title_raw"], "The Matrix (DVD)")
        self.assertEqual(p["release_date_raw"], "2000-03-15")
        self.assertEqual(p["directors_raw"], "Lana Wachowski")
        self.assertEqual(p["asin_on_page"], "B000123456")

    def test_classify_music_cd_is_not_movie(self):
        cat, basis, _ev, _c, _r = classify(
            "The Matrix: Original Motion Picture Soundtrack (Audio CD)", {}, "X1")
        self.assertEqual(cat, "non_movie")

    def test_classify_box_set(self):
        cat, *_ = classify("The Matrix Collection (The Matrix / Reloaded) [Blu-ray]", {}, "X2")
        self.assertEqual(cat, "box_set")

    def test_classify_movie_by_crew_evidence(self):
        cat, *_ = classify("The Matrix (DVD)",
                           {"directors_raw": "Lana Wachowski", "format_raw": "DVD"}, "X3")
        self.assertEqual(cat, "movie")


class TestMetrics(unittest.TestCase):
    def test_prf(self):
        p, r, f = prf(8, 2, 4)
        # prf 内部会四舍五入到 4 位，所以比较时也要按 4 位比
        self.assertAlmostEqual(p, 0.8)
        self.assertAlmostEqual(r, round(8 / 12, 4))
        self.assertAlmostEqual(f, 2 * 0.8 * (8 / 12) / (0.8 + 8 / 12), places=4)

    def test_prf_zero_denominator(self):
        self.assertEqual(prf(0, 0, 0), (None, None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
