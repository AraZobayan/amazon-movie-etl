-- 验收查询 5：按作品统计评论数量与平均分
-- 输入：:limit
-- 口径说明（作业要求必须解释）：
--   1) 只统计 relation_type='single' 的商品，合集成员不重复计数 ——
--      否则一部电影在 3 个合集里出现就会被算 3 次。
--   2) 评论以"逻辑评论"为单位（review 表），原始记录可能多于逻辑评论，
--      两者差异由 review_mapping 可查。
--   3) 平均分按逻辑评论算，未做加权，也未剔除无正文评论。
SELECT
    m.canonical_title                                   AS 作品,
    COUNT(DISTINCT pm.product_id)                       AS 计入的商品版本数,
    COUNT(DISTINCT r.review_id)                         AS 逻辑评论数,
    (SELECT COUNT(*) FROM review_mapping rm
      WHERE rm.review_id IN (SELECT review_id FROM review r2
                              WHERE r2.product_id IN (SELECT product_id FROM product_movie
                                                       WHERE movie_id=m.movie_id
                                                         AND relation_type='single'))) AS 原始记录数,
    ROUND(AVG(r.score), 3)                              AS 平均分,
    ROUND(1.0 * SUM(CASE WHEN r.score >= 4 THEN 1 ELSE 0 END) / COUNT(r.review_id), 3) AS 好评率
FROM movie m
JOIN product_movie pm ON pm.movie_id = m.movie_id AND pm.relation_type='single'
JOIN review r         ON r.product_id = pm.product_id
GROUP BY m.movie_id
HAVING 逻辑评论数 > 0
ORDER BY 逻辑评论数 DESC
LIMIT :limit;
