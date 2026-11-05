# 合并 / 不合并实例（自动导出）

数据库：`data/db/etl.sqlite3`　生成时间：2026-11-05T10:39:42Z

作业说明第六部分要求的四类实例。每行都带 `decision_id` / `movie_id` / `person_id`，
可以回库复核，不是手写的说明文字。

## 1 电影：跨版本合并（同一作品，多个 ASIN）

判据：标题归一后相同或近似 + 演职员重合 + 年份不冲突，
分数 ≥ 0.86 自动合并。载体（DVD / Blu-ray / Prime Video）与发售日**不同**恰恰是要合并的理由——
它们是同一部作品的不同发行版本。

### 实例 M1　The Godfather　`M-c1a82e928b1f1b66`

- 商品版本数：**20**；载体：Blu-ray、DVD、Prime Video

| ASIN | 页面标题 | 载体 | 商品发售日 | 发行商 | 评论数 | 采集 |
|---|---|---|---|---|---|---|
| B0032LO8DE | The Godfather [Blu-ray] (Anniversary Edition) | Blu-ray | 1972-08-29 | Paramount | 509 | success |
| B000BI5KV0 | The Godfather (DVD) (Reissue) | DVD | 1972-10-28 | Paramount | 455 | success |
| B000NQRV4O | The Godfather [Blu-ray] (Reissue) | Blu-ray | 1972-11-08 | Paramount | 438 | success |
| B001FB55HQ | The Godfather (Reissue) | Prime Video | 1972-11-11 | Paramount | 402 | success |
| B000GETWF4 | The Godfather (2-Disc Special Edition) (Annive | DVD | 1972-12-28 | Paramount | 375 | success |
| B000127M62 | The Godfather (Anniversary Edition) | Prime Video | 1972-08-08 | Paramount | 328 | success |

- 合并证据：`R-MRG-SAME-TITLE-SAME-CREW` 分数 1.0（`D-17df8d49dfdc6e6e`）—— 标题相似度 1.0；关系 equal；导演相似度 1.0；演员重合度 1.0；年份相同
- 合并的 ASIN 对：B00004RVJO ↔ B001FB55HQ

### 实例 M2　The Matrix Revolutions　`M-1b0e7cdc7002fd2a`

- 商品版本数：**20**；载体：Blu-ray、DVD、Prime Video

| ASIN | 页面标题 | 载体 | 商品发售日 | 发行商 | 评论数 | 采集 |
|---|---|---|---|---|---|---|
| B0014ERKO0 | The Matrix Revolutions [Blu-ray] (Reissue) | Blu-ray | 2003-10-01 | Warner Home Video | 515 | success |
| B0002ZUHD8 | The Matrix Revolutions (Reissue) | Prime Video | 2004-02-17 | Warner Home Video | 496 | success |
| B00004RUNT | The Matrix Revolutions, The (DVD) | DVD | 2003-11-29 | Warner Home Video | 476 | success |
| B0033BISWK | The Matrix Revolutions [Blu-ray] | Blu-ray | 2003-07-14 | Warner Home Video | 473 | success |
| B002OHDRF2 | The Matrix Revolutions (Collector's Edition) [ | Blu-ray | 2003-09-04 | Warner Home Video | 402 | success |
| B00005MHNI | The Matrix Revolutions (Widescreen Edition) (A | DVD | 2004-01-02 | Warner Home Video | 373 | success |

- 合并证据：`R-MRG-SAME-TITLE-SAME-CREW` 分数 1.0（`D-7ad4b9c9827ff5d9`）—— 标题相似度 1.0；关系 equal；导演相似度 1.0；演员重合度 1.0；年份相同
- 合并的 ASIN 对：B00004CIQV ↔ B0033BISWK

### 实例 M3　The Godfather Part II　`M-b8642e901fdc9734`

- 商品版本数：**19**；载体：Blu-ray、DVD、Prime Video

| ASIN | 页面标题 | 载体 | 商品发售日 | 发行商 | 评论数 | 采集 |
|---|---|---|---|---|---|---|
| B007XF4J66 | The Godfather Part II (Widescreen Edition) (Re | DVD | 1974-10-05 | Paramount | 476 | success |
| B0068THAGY | The Godfather Part II | Prime Video | 1974-08-10 | Paramount | 467 | success |
| B000VDDWEC | The Godfather Part II (Anniversary Edition) | Prime Video | 1974-08-27 | Paramount | 445 | success |
| B002P8ZOUQ | The Godfather Part II, The (DVD) (Anniversary  | DVD | 1974-07-04 | Paramount | 377 | success |
| B004TRJRD6 | The Godfather Part II (Collector's Edition) [B | Blu-ray | 1974-09-30 | Paramount | 377 | success |
| B0001EYTK2 | The Godfather Part II (DVD) | DVD | 1974-12-20 | Paramount | 371 | success |

- 合并证据：`R-MRG-SAME-TITLE-SAME-CREW` 分数 1.0（`D-3d18ef5723e3b1ca`）—— 标题相似度 1.0；关系 equal；导演相似度 1.0；演员重合度 1.0；年份相同
- 合并的 ASIN 对：B00006AGH1 ↔ B000CNE0U6

### 实例 M4　The Lord of the Rings: The Return of the King　`M-9a4cf98926c105c1`

- 商品版本数：**19**；载体：Blu-ray、DVD、Prime Video

| ASIN | 页面标题 | 载体 | 商品发售日 | 发行商 | 评论数 | 采集 |
|---|---|---|---|---|---|---|
| B005CA4SJW | The Lord of the Rings: The Return of the King  | Blu-ray | 2004-02-07 | New Line Home Vide | 606 | success |
| B0006Z2NJU | The Lord of the Rings: The Return of the King  | Blu-ray | 2003-08-20 | New Line Home Vide | 523 | success |
| B0030MM68O | The Lord of the Rings: The Return of the King  | Blu-ray | 2003-09-22 | New Line Home Vide | 514 | success |
| 6303953425 | The Lord of the Rings: The Return of the King  | DVD | 2003-12-18 | New Line Home Vide | 475 | success |
| B0000640ZP | The Lord of the Rings: The Return of the King, | DVD | 2004-01-07 | New Line Home Vide | 381 | success |
| B007JRTV52 | The Lord of the Rings: The Return of the King, | DVD | 2004-02-08 | New Line Home Vide | 379 | success |

- 合并证据：`R-MRG-SAME-TITLE-SAME-CREW` 分数 1.0（`D-64e84ee44154df45`）—— 标题相似度 1.0；关系 equal；导演相似度 1.0；演员重合度 1.0；年份差 1
- 合并的 ASIN 对：B0000640ZP ↔ B0006Z2NJU

### 实例 M5　The Silence of the Lambs　`M-06d9a640ceaf39a3`

- 商品版本数：**19**；载体：Blu-ray、DVD、Prime Video

| ASIN | 页面标题 | 载体 | 商品发售日 | 发行商 | 评论数 | 采集 |
|---|---|---|---|---|---|---|
| B000067JG4 | The Silence of the Lambs (2-Disc Special Editi | DVD | 1991-11-03 | Criterion Collecti | 643 | success |
| B000NQQ4ME | The Silence of the Lambs [Blu-ray] | Blu-ray | 1991-11-10 | Criterion Collecti | 586 | success |
| B001G7Q0Z0 | The Silence of the Lambs (Collector's Edition) | Blu-ray | 1991-07-01 | Criterion Collecti | 472 | success |
| B003ZY3050 | The Silence of the Lambs (Widescreen Edition)  | DVD | 1992-01-10 | Criterion Collecti | 397 | success |
| B005LAJ22Q | The Silence of the Lambs (2-Disc Special Editi | DVD | 1991-11-15 | Criterion Collecti | 382 | success |
| B000065K25 | The Silence of the Lambs (DVD) | DVD | 1991-10-31 | Criterion Collecti | 318 | success |

- 合并证据：`R-MRG-SAME-TITLE-SAME-CREW` 分数 1.0（`D-7a2b91b0da3f1be6`）—— 标题相似度 1.0；关系 equal；导演相似度 1.0；演员重合度 1.0；年份相同
- 合并的 ASIN 对：B00004CXXA ↔ B000HOMU98

### 实例 M6　The Lord of the Rings: The Two Towers　`M-c21f9f6a32947cec`

- 商品版本数：**18**；载体：Blu-ray、DVD、Prime Video

| ASIN | 页面标题 | 载体 | 商品发售日 | 发行商 | 评论数 | 采集 |
|---|---|---|---|---|---|---|
| B000KKQNRO | The Lord of the Rings: The Two Towers (DVD) (R | DVD | 2002-11-26 | New Line Home Vide | 608 | success |
| B002SEQ8ZC | The Lord of the Rings: The Two Towers (Anniver | Prime Video | 2002-12-15 | New Line Home Vide | 397 | success |
| B00006LLJ4 | The Lord of the Rings: The Two Towers (Widescr | DVD | 2003-01-16 | New Line Home Vide | 366 | success |
| B000055XP5 | The Lord of the Rings: The Two Towers, The (DV | DVD | 2002-10-29 | New Line Home Vide | 366 | success |
| 6303566669 | The Lord of the Rings: The Two Towers (Reissue | Prime Video | 2002-10-13 | New Line Home Vide | 360 | success |
| B00004RYIR | The Lord of the Rings: The Two Towers (2-Disc  | DVD | 2002-07-16 | New Line Home Vide | 312 | success |

- 合并证据：`R-MRG-SAME-TITLE-SAME-CREW` 分数 1.0（`D-23760788df325495`）—— 标题相似度 1.0；关系 equal；导演相似度 1.0；演员重合度 1.0；年份差 1
- 合并的 ASIN 对：B00004RYIR ↔ B0013Y1GEQ

## 2 电影：易混淆但未合并（对照组）

这些对的标题非常像，但**不足以判定同一作品**，所以停在 `manual_review` 或 `no_merge`。
不合并的依据是：导演/演员不重合、年份冲突、或标题只是词面重叠。

| # | ASIN A / 标题 | ASIN B / 标题 | 分数 | 判定 | 规则 | 证据 |
|---|---|---|---|---|---|---|
| 1 | B002N14GPE<br>Kill Bill: Vol. 2 (2-Disc Special Editio | B003ELMR9E<br>Kill Bill: Vol. 1 [Blu-ray] (Reissue) | 0.8583 | manual_review | `R-MRG-MID-SCORE` | 标题相似度 0.9333；关系 overlap；导演相似度 1.0；演员重合度 0.1667；年份差 1 |
| 2 | B004EPYZQ2<br>The Lord of the Rings Trilogy (Extended  | B004J04KXU<br>The Lord of the Rings: The Two Towers (2 | 0.8367 | manual_review | `R-MRG-MID-SCORE` | 标题相似度 0.7385；关系 overlap；导演相似度 1.0；演员重合度 0.75；年份差 1 |
| 3 | B00013F2J0<br>magical shopping arcade abenobashi vol 2 | B000ADKWNW<br>magical shopping arcade abenobashi vols  | 0.6199 | no_merge | `R-MRG-LOW-SCORE` | 标题相似度 0.9398；关系 overlap；导演相似度 0.0；演员重合度 0.0；年份未知 |
| 4 | 6304400179<br>street fighter ii volume 2 vhs | B000059H9H<br>street fighter ii vol 2 | 0.6194 | no_merge | `R-MRG-LOW-SCORE` | 标题相似度 0.9388；关系 overlap；导演相似度 0.0；演员重合度 0.0；年份未知 |
| 5 | 6304014457<br>the cabinet of dr caligari vhs | B0009X759C<br>the cabinet of caligari | 0.6194 | no_merge | `R-MRG-LOW-SCORE` | 标题相似度 0.9388；关系 overlap；导演相似度 0.0；演员重合度 0.0；年份未知 |
| 6 | 6302230519<br>slumber party massacre 3 vhs | 6305976392<br>slumber party massacre ii vhs | 0.6194 | no_merge | `R-MRG-LOW-SCORE` | 标题相似度 0.9388；关系 overlap；导演相似度 0.0；演员重合度 0.0；年份未知 |

## 3 人物：别名合并（同一人的不同署名）

判据：缩写展开一致（`K. Reeves` → `Keanu Reeves`）、目录式写法、
且参演作品重合。姓相同**不足以**合并——见下一节。

### 实例 P1　Toshiro Mifune　`P-84bac9f8e15d0635`

- 归并的署名：`T. Mifune`、`Toshiro Mifune`、`Toshirô Mifune`
- 参与作品：Complete Adventures of Indiana Jones、Rashomon、Seven Samurai、Pitch Black
- 合并证据：`R-PER-INITIALS` 分数 0.9（`D-16de1bf55e9cf64a`）—— 姓名关系 initials_first_abbrev；共现作品 2 部

### 实例 P2　Al Pacino　`P-f29ff566a1c7c586`

- 归并的署名：`A. Pacino`、`Al Pacino`、`Pacino, Al`
- 参与作品：The Godfather、The Godfather Part II、Buffy the Vampire Slayer - The Complete Second Season、Evanescence - Anywhere But Home
- 合并证据：`R-PER-INITIALS` 分数 0.9（`D-d5dd642495fdb57f`）—— 姓名关系 initials_first_abbrev；共现作品 2 部

### 实例 P3　James Cameron　`P-00003f6e07827f27`

- 归并的署名：`J. Cameron`、`James Cameron`
- 参与作品：Aliens
- 合并证据：`R-PER-INITIALS` 分数 0.9（`D-9da70e1f339f9713`）—— 姓名关系 initials_first_abbrev；共现作品 1 部

### 实例 P4　Matt Damon　`P-013d21cd66243f90`

- 归并的署名：`M. Damon`、`Matt Damon`
- 参与作品：True Grit、Closer
- 合并证据：`R-PER-INITIALS` 分数 0.9（`D-dae75c1339e69068`）—— 姓名关系 initials_first_abbrev；共现作品 1 部

### 实例 P5　Marlon Brando　`P-0360bd8cef6e99bf`

- 归并的署名：`M. Brando`、`Marlon Brando`
- 参与作品：The Godfather、Apocalypse Now、Team America: World Police、The Godfather Collection
- 合并证据：`R-PER-INITIALS` 分数 0.9（`D-be083510792dd557`）—— 姓名关系 initials_first_abbrev；共现作品 2 部

### 实例 P6　Robert Duvall　`P-05db91d47e0aa05a`

- 归并的署名：`R. Duvall`、`Robert Duvall`
- 参与作品：Apocalypse Now、The Godfather Part II、Buffy the Vampire Slayer - The Complete Second Season、Evanescence - Anywhere But Home
- 合并证据：`R-PER-INITIALS` 分数 0.9（`D-23e713103a8a2662`）—— 姓名关系 initials_first_abbrev；共现作品 2 部

## 4 人物：同名 / 近似名但未合并（对照组）

作业原文：**「姓名相同不代表同一人；名字缩写相同更不能直接合并。」**
下面这些对停在待复核或明确不合并，各自保留独立 `person_id`。

| # | 署名 A | 署名 B | 分数 | 判定 | 规则 | 证据 | 当前实体 | 复核判断 |
|---|---|---|---|---|---|---|---|---|
| 1 | `francis coppola` | `roman coppola` | 0.55 | manual_review | `R-PER-SURNAME-ALIAS` | 姓名关系 same_surname | P-af9c4aa101fc1feb / P-2259e9c99718380d | 同姓、名字不同 → 规则判为两个人 |
| 2 | `lana wachowski` | `larry wachowski` | 0.55 | manual_review | `R-PER-SURNAME-ALIAS` | 姓名关系 same_surname；共现作品 2 部 | P-2df863b2a965d391 / P-a7e0cc7439b55b53 | 同姓、名字不同 → 规则判为两个人 |
| 3 | `bill murray` | `brian murray` | 0.55 | manual_review | `R-PER-SURNAME-ALIAS` | 姓名关系 same_surname | P-d81a07af6671ea7d / P-1a4d2467593de99b | 同姓、名字不同 → 规则判为两个人 |
| 4 | `bridget fonda` | `peter fonda` | 0.55 | manual_review | `R-PER-SURNAME-ALIAS` | 姓名关系 same_surname | P-c5138848f499cf58 / P-543cc5c79d8dd3e8 | 同姓、名字不同 → 规则判为两个人 |
| 5 | `b murray` | `brian murray` | 0.9 | manual_review | `R-PER-INITIALS-AMBIGUOUS` | 姓名关系 initials_first_abbrev | P-f6e7d9759d9da02f / P-1a4d2467593de99b | 缩写可展开成多人 → 待复核，未合并 |
| 6 | `l wachowski` | `lilly wachowski` | 0.9 | manual_review | `R-PER-INITIALS-AMBIGUOUS` | 姓名关系 initials_first_abbrev；共现作品 2 部；共现页面 3 个 | P-20e6f53a6fd757a0 / P-ad47f09b127eca35 | 缩写可展开成多人 → 待复核，未合并 |

> `manual_review` 表示规则不敢自动合并，等人工复核；`no_merge` 表示证据明确不支持合并。
> 两者都**不会**产生合并，因此不会污染实体表。
>
> 这张表说明的是**规则做了什么决定**，不是「这两个署名一定属于不同的人」。
> 表里仍有个别对实际上是同一人（例如改名、仅署名姓氏的职员表写法），
> 这类漏合并是已知缺陷，见报告第 9 节「局限」。
