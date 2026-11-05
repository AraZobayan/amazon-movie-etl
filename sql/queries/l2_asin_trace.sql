-- 血缘查询 L2：输入 ASIN 或原始评论记录 ID，返回其对应的版本、作品及匹配状态
-- 输入：:product_id（ASIN）或 :source_review_id（raw_review.source_review_id），二选一
-- 作业第九部分必答血缘查询 2 的两个输入口都在这里：
--   * 传 :product_id  → 直接用；
--   * 传 :source_review_id → 走 raw_review 主键点查（source_review_id 是 PK，
--     无需全表扫描）拿到 product_id。**刻意不经过 review_mapping**：
--     该表主键是 (review_id, source_review_id)，按 source_review_id 反查没有索引，
--     在 791 万行上会全表扫描（该索引缺口记在报告的已知局限里）。
-- 合并决策按 (action, rule_id) 聚合：一个商品可能被多条决策扇出成几十行，
-- 不聚合会让"一个 ASIN 的传记"看不出重点。
WITH target AS (
    SELECT COALESCE(
             :product_id,
             (SELECT r.product_id FROM raw_review r
               WHERE r.source_review_id = :source_review_id)
           ) AS product_id
), md_agg AS (
    SELECT id_a AS product_id, action, rule_id, evidence_json
      FROM int_merge_decision
     WHERE entity_type='movie' AND id_a = (SELECT product_id FROM target)
    UNION ALL
    SELECT id_b AS product_id, action, rule_id, evidence_json
      FROM int_merge_decision
     WHERE entity_type='movie' AND id_b = (SELECT product_id FROM target)
), md AS (
    SELECT product_id,
           GROUP_CONCAT(DISTINCT action)   AS action,
           GROUP_CONCAT(DISTINCT rule_id)  AS rule_id,
           COUNT(*)                        AS decisions,
           MIN(evidence_json)              AS evidence_json
      FROM md_agg GROUP BY product_id
)
SELECT
    p.product_id                                  AS ASIN,
    :source_review_id                             AS 原始评论记录ID,
    pc.status                                     AS 采集状态,
    pc.source_type                                AS 来源类型,
    pc.reason_code                                AS 状态原因,
    sp.title_raw                                  AS 页面标题,
    sp.format_raw                                 AS 版本载体,
    sp.release_date_raw                           AS 商品发售日,
    cl.category                                   AS 产品分类,
    cl.basis                                      AS 分类依据,
    pm.movie_id                                   AS 作品ID,
    m.canonical_title                             AS 作品,
    pm.relation_type                              AS 关系类型,
    ROUND(pm.confidence, 3)                       AS 匹配置信度,
    md.action                                     AS 合并决策,
    md.rule_id                                    AS 决策规则,
    md.decisions                                  AS 合并决策数,
    md.evidence_json                              AS 决策证据
FROM target t
JOIN product p                  ON p.product_id = t.product_id
LEFT JOIN product_collection pc ON pc.product_id = p.product_id
LEFT JOIN stg_product_page sp   ON sp.product_id = p.product_id
LEFT JOIN stg_classification cl ON cl.product_id = p.product_id
LEFT JOIN product_movie pm      ON pm.product_id = p.product_id
LEFT JOIN movie m               ON m.movie_id = pm.movie_id
LEFT JOIN md                    ON md.product_id = p.product_id
ORDER BY pm.relation_type;
