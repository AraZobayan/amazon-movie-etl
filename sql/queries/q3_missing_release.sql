-- 验收查询 3：查询缺失与仅年份的上映信息
-- 无输入
-- 要点：把"缺失"和"只有年份"分开呈现：
--   * 缺失：release_date 为 NULL，但候选里可能有商品发售日（语义不同，不能顶替）
--   * 仅年份：release_precision='year'，release_date 保持 NULL —— 绝不补成当年 1 月 1 日
-- 列名口径：第 5 列统计的是"商品发售日（PHYSICAL）候选的条数"，**不是**"只有发售日的作品数"——
--   有影院日期候选的作品也会计入（实测 215 部有 PHYSICAL 候选，其中只有 176 部没有 THEATRICAL 候选）。
--   所以列名叫"商品发售日候选数"，不叫"仅有发售日候选数"。
-- 说明（重要）：本项目**不启用时间推断**，因此没有"推断值"这一类。
--   field_assertion.status 的 CHECK 只有 candidate/selected/rejected/conflict/pending，
--   没有 'inferred'（见 sql/schema_sqlite.sql 的 field_assertion 定义）。
--   早期版本的注释写成"按 status='inferred' 统计"，与 DDL 矛盾，已删除。
--   同样，'pending' 状态本项目从不写入，待确认候选数恒为 0（如实保留该列以便对照）。
SELECT
    m.movie_id                                                  AS 作品ID,
    m.canonical_title                                           AS 作品,
    m.release_precision                                         AS 精度,
    m.release_type                                              AS 日期语义,
    (SELECT COUNT(*) FROM field_assertion fa
      WHERE fa.entity_type='movie' AND fa.entity_id=m.movie_id
        AND fa.field_name='release_date' AND fa.extractor_rule_id LIKE '%PHYSICAL%') AS 商品发售日候选数,
    (SELECT fa.normalized_value FROM field_assertion fa
      WHERE fa.entity_type='movie' AND fa.entity_id=m.movie_id
        AND fa.field_name='release_date' AND fa.extractor_rule_id LIKE '%PHYSICAL%'
      ORDER BY fa.normalized_value LIMIT 1)                     AS 商品发售日示例,
    (SELECT COUNT(*) FROM field_assertion fa
      WHERE fa.entity_type='movie' AND fa.entity_id=m.movie_id
        AND fa.field_name='release_date' AND fa.status='pending')  AS 待确认候选数,
    CASE WHEN m.release_precision='year' THEN '仅年份（未补 1 月 1 日）'
         WHEN m.release_date IS NULL THEN '缺失'
         ELSE '已核实' END                                       AS 状态分类
FROM movie m
WHERE m.release_date IS NULL OR m.release_precision <> 'day'
ORDER BY 商品发售日候选数 DESC, m.canonical_title
LIMIT :limit;
