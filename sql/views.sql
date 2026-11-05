-- =============================================================================
--  视图层：把"血缘表里的选择结果"投影成可查询的宽表
-- -----------------------------------------------------------------------------
--  设计原则：**唯一事实来源是 field_assertion / field_selection**，
--  视图只负责把结果拼成人类和报表好用的形状，不引入第二份真相。
--  这样"改了断言 -> 视图自动反映"，不会出现"表里一个值、血缘里另一个值"。
-- =============================================================================

-- 作品类型（多值并集）
CREATE VIEW IF NOT EXISTS v_movie_genres AS
SELECT entity_id AS movie_id,
       GROUP_CONCAT(normalized_value, '|') AS genres,
       COUNT(*) AS genre_count
FROM field_assertion
WHERE entity_type='movie' AND field_name='genre' AND status='selected'
GROUP BY entity_id;

-- 作品主视图：把 release_date 从 field_selection 投影出来
CREATE VIEW IF NOT EXISTS v_movie AS
SELECT
    m.movie_id,
    m.canonical_title,
    COALESCE((SELECT fa.normalized_value FROM field_selection fs
               JOIN field_assertion fa ON fa.assertion_id = fs.selected_assertion_id
              WHERE fs.entity_type='movie' AND fs.entity_id=m.movie_id
                AND fs.field_name='release_date'), NULL)                AS release_date,
    m.release_year,
    m.release_precision,
    m.release_type,
    (SELECT g.genres FROM v_movie_genres g WHERE g.movie_id = m.movie_id) AS genres,
    (SELECT fa.normalized_value FROM field_selection fs
       JOIN field_assertion fa ON fa.assertion_id = fs.selected_assertion_id
      WHERE fs.entity_type='movie' AND fs.entity_id=m.movie_id
        AND fs.field_name='external_id')                              AS external_id,
    (SELECT COUNT(*) FROM product_movie pm WHERE pm.movie_id = m.movie_id) AS product_count,
    (SELECT COUNT(*) FROM movie_person mp WHERE mp.movie_id = m.movie_id)  AS person_count,
    m.edition_scope,
    m.status
FROM movie m;

-- 商品版本视图：ASIN + 采集状态 + 分类 + 版本信息
CREATE VIEW IF NOT EXISTS v_product_edition AS
SELECT
    p.product_id                                   AS product_id,
    c.status                                       AS collect_status,
    c.source_type                                  AS source_type,
    c.http_status                                  AS http_status,
    c.reason_code                                  AS reason_code,
    cl.category                                    AS category,
    cl.confidence                                  AS category_confidence,
    sp.title_raw                                   AS page_title,
    sp.format_raw                                  AS edition_format,
    sp.release_date_raw                            AS edition_release_date,
    sp.studio_raw                                  AS studio,
    p.review_count                                 AS review_count,
    (SELECT pm.movie_id FROM product_movie pm WHERE pm.product_id = p.product_id
      AND pm.relation_type <> 'box_set_member' LIMIT 1) AS movie_id,
    (SELECT COUNT(*) FROM product_movie pm WHERE pm.product_id = p.product_id) AS movie_count
FROM product p
LEFT JOIN product_collection c   ON c.product_id = p.product_id
LEFT JOIN stg_classification cl  ON cl.product_id = p.product_id
LEFT JOIN stg_product_page sp    ON sp.product_id = p.product_id;

-- 采集状态分布（作业要求：状态互斥且可核对）
CREATE VIEW IF NOT EXISTS v_collection_status AS
SELECT status, source_type, COUNT(*) AS n
FROM product_collection
GROUP BY status, source_type
ORDER BY n DESC;

-- 字段血缘：一条最终值 -> 断言 -> 原始证据
CREATE VIEW IF NOT EXISTS v_field_lineage AS
SELECT
    fs.entity_type,
    fs.entity_id,
    fs.field_name,
    fa.assertion_id          AS selected_assertion_id,
    fa.normalized_value      AS final_value,
    fa.raw_value             AS raw_value,
    fa.extractor_rule_id     AS rule_id,
    fa.extractor_version     AS rule_version,
    fa.confidence,
    sr.source_type,
    sr.url,
    sr.locator,
    sr.snapshot_path,
    sr.content_hash,
    sr.fetched_at,
    fs.decision_reason,
    fs.rule_id               AS selection_rule,
    fs.decided_at
FROM field_selection fs
LEFT JOIN field_assertion fa ON fa.assertion_id = fs.selected_assertion_id
LEFT JOIN raw_source_record sr ON sr.record_id = fa.source_record_id;

-- 来源构成审计：最终值有多少依赖"合成夹具"（这是本作业的诚实性自检）
CREATE VIEW IF NOT EXISTS v_provenance_audit AS
SELECT
    COALESCE(sr.source_type, 'NO_SOURCE') AS source_type,
    COUNT(*) AS selected_fields
FROM field_selection fs
LEFT JOIN field_assertion fa ON fa.assertion_id = fs.selected_assertion_id
LEFT JOIN raw_source_record sr ON sr.record_id = fa.source_record_id
GROUP BY COALESCE(sr.source_type, 'NO_SOURCE')
ORDER BY selected_fields DESC;

-- 作品-人物关系（带角色）
CREATE VIEW IF NOT EXISTS v_movie_person AS
SELECT mp.movie_id, m.canonical_title, mp.person_id, p.canonical_name,
       mp.role, mp.billing_order, sr.source_type
FROM movie_person mp
JOIN movie m  ON m.movie_id = mp.movie_id
JOIN person p ON p.person_id = mp.person_id
LEFT JOIN raw_source_record sr ON sr.record_id = mp.source_record_id;

-- 质量问题看板
CREATE VIEW IF NOT EXISTS v_quality_dashboard AS
SELECT issue_type, severity, COUNT(*) AS n
FROM meta_quality_issue
GROUP BY issue_type, severity
ORDER BY n DESC;

-- 合并决策看板（自动合并 / 待复核 / 不合并）
CREATE VIEW IF NOT EXISTS v_merge_dashboard AS
SELECT entity_type, action, rule_id, COUNT(*) AS n,
       ROUND(AVG(score), 3) AS avg_score
FROM int_merge_decision
GROUP BY entity_type, action, rule_id
ORDER BY entity_type, n DESC;
