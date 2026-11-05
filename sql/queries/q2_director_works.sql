-- 验收查询 2：查询某位导演的作品及其来源
-- 输入：:person_name（规范化姓名，或原始署名的一部分）
-- 要点：作品 -> 人物 的关系本身带来源（source_record_id -> raw_source_record），
--       所以"这条导演署名是哪来的"可以直接答出来，而不是只有一句"来自商品页"。
SELECT
    p.canonical_name                       AS 人物,
    m.canonical_title                      AS 作品,
    m.release_year                         AS 上映年,
    mp.role                                AS 角色,
    mp.billing_order                       AS 署名顺序,
    sr.source_type                         AS 来源类型,
    COALESCE(sr.url, sr.locator)           AS 来源定位,
    sr.content_hash                        AS 内容哈希,
    sr.fetched_at                          AS 获取时间
FROM movie_person mp
JOIN person p                  ON p.person_id = mp.person_id
JOIN movie m                   ON m.movie_id  = mp.movie_id
LEFT JOIN raw_source_record sr ON sr.record_id = mp.source_record_id
WHERE p.canonical_name_norm LIKE :person_name
ORDER BY m.release_year, m.canonical_title;
