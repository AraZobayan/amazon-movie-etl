-- 血缘查询 L4：输入 person_id，查询原始署名、别名归并过程与参与作品
-- 输入：:person_id
-- 三段结果合成一个答案：
--   1) 这个人有哪些署名写法（person_name）
--   2) 每条署名来自哪条原始记录（source_record_id -> raw_source_record）
--   3) 参与过哪些作品、什么角色（movie_person）
SELECT
    p.person_id                                   AS 人物ID,
    p.canonical_name                              AS 规范名,
    pn.name                                       AS 署名写法,
    pn.name_type                                  AS 写法类型,
    sr.source_type                                AS 署名来源类型,
    COALESCE(sr.url, sr.locator)                  AS 署名证据定位,
    m.canonical_title                             AS 参与作品,
    m.release_year                                AS 上映年,
    mp.role                                       AS 角色,
    mp.billing_order                              AS 署名顺序
FROM person p
JOIN person_name pn            ON pn.person_id = p.person_id
LEFT JOIN raw_source_record sr ON sr.record_id = pn.source_record_id
LEFT JOIN movie_person mp      ON mp.person_id = p.person_id
LEFT JOIN movie m              ON m.movie_id  = mp.movie_id
WHERE p.person_id = :person_id
ORDER BY pn.name_type, m.release_year;
