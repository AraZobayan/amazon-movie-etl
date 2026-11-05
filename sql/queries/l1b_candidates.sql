-- 血缘查询 L1b：同一字段的"全部候选"（含未采用的），支撑"未采用候选仍可查看"
-- 输入：:movie_id, :field_name
-- :field_name 可传源级名（title/genre）或实体级名（canonical_title/genres）：
-- 断言表用源级名、选择表用实体级名，两者是不同层次，这里做显式映射。
-- "是否采用"用 candidate_ids_json 判定，因此单值字段与多值字段（genres）都成立。
WITH sel AS (
    SELECT * FROM field_selection
     WHERE entity_type = 'movie' AND entity_id = :movie_id
       AND field_name = CASE :field_name
                            WHEN 'title' THEN 'canonical_title'
                            WHEN 'genre' THEN 'genres'
                            ELSE :field_name END
)
SELECT
    fa.assertion_id                     AS 断言ID,
    fa.status                           AS 状态,
    fa.normalized_value                 AS 候选值,
    fa.raw_value                        AS 原始值,
    fa.extractor_rule_id                AS 规则,
    ROUND(fa.confidence, 3)             AS 置信度,
    sr.source_type                      AS 来源类型,
    COALESCE(sr.url, sr.locator)        AS 证据定位,
    CASE WHEN EXISTS (
             SELECT 1 FROM sel fs
              WHERE fs.selected_assertion_id = fa.assertion_id
                 OR (fs.selection_kind = 'multi'
                     AND fa.assertion_id IN (SELECT value FROM json_each(fs.candidate_ids_json)))
         ) THEN '采用' ELSE '未采用' END AS 是否采用
FROM field_assertion fa
JOIN raw_source_record sr ON sr.record_id = fa.source_record_id
WHERE fa.entity_type='movie' AND fa.entity_id = :movie_id
  AND fa.field_name = CASE :field_name
                          WHEN 'canonical_title' THEN 'title'
                          WHEN 'genres' THEN 'genre'
                          ELSE :field_name END
ORDER BY 是否采用 DESC, fa.confidence DESC;
