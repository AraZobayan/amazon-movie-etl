-- 验收查询 1：查询一部作品的所有商品版本
-- 输入：:movie_id
-- 要点：同一个 movie_id 下挂着多个 ASIN（DVD / 蓝光 / 特别版 / 合集成员），
--       每个版本保留自己的采集状态与版本字段 —— 这正是"作品 ≠ 商品"的体现。
SELECT
    m.canonical_title                     AS 作品,
    pm.product_id                         AS ASIN,
    pm.relation_type                       AS 关系类型,
    ROUND(pm.confidence, 3)                AS 匹配置信度,
    pc.status                              AS 采集状态,
    pc.source_type                         AS 来源类型,
    sp.format_raw                          AS 版本载体,
    sp.release_date_raw                    AS 商品发售日,
    sp.studio_raw                          AS 发行商,
    p.review_count                         AS 评论数,
    cl.category                            AS 产品分类
FROM product_movie pm
JOIN movie m                    ON m.movie_id  = pm.movie_id
JOIN product p                  ON p.product_id = pm.product_id
LEFT JOIN product_collection pc ON pc.product_id = pm.product_id
LEFT JOIN stg_product_page sp   ON sp.product_id = pm.product_id
LEFT JOIN stg_classification cl ON cl.product_id = pm.product_id
WHERE pm.movie_id = :movie_id
ORDER BY p.review_count DESC, pm.product_id;
