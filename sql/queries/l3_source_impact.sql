-- 血缘查询 L3：输入原始页面/第三方源记录 ID，列出受影响的最终实体与字段
-- 输入：:record_id
-- 用途：当某个来源被判定不可信（例如发现夹具/错误页面）时，
--       可以立刻知道"哪些最终字段要重算" —— 这是血缘最实用的价值。
SELECT
    sr.record_id                                   AS 源记录ID,
    sr.source_type                                 AS 来源类型,
    COALESCE(sr.url, sr.locator)                   AS 证据定位,
    sr.content_hash                                AS 内容哈希,
    fa.entity_type                                 AS 实体类型,
    fa.entity_id                                   AS 实体ID,
    CASE WHEN fa.entity_type='movie'
         THEN (SELECT canonical_title FROM movie WHERE movie_id = fa.entity_id)
         WHEN fa.entity_type='product'
         THEN (SELECT title_raw FROM stg_product_page WHERE product_id = fa.entity_id)
         END                                       AS 实体名称,
    fa.field_name                                  AS 字段,
    fa.normalized_value                            AS 候选值,
    fa.status                                      AS 断言状态,
    CASE WHEN fs.selected_assertion_id = fa.assertion_id THEN '是' ELSE '否' END AS 是否被最终采用
FROM raw_source_record sr
JOIN field_assertion fa        ON fa.source_record_id = sr.record_id
LEFT JOIN field_selection fs   ON fs.entity_type = fa.entity_type
                              AND fs.entity_id  = fa.entity_id
                              AND fs.field_name = fa.field_name
WHERE sr.record_id = :record_id
ORDER BY 是否被最终采用 DESC, fa.field_name;
