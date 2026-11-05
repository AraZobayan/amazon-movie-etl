-- 血缘查询 L1：输入 movie_id 和字段名，返回最终值、采用/未采用候选、原始证据、选择理由
-- 输入：:movie_id, :field_name
-- 这是作业"必须演示的血缘查询"第 1 条。一条 SQL 回答四个问题：
--   是什么？ 从哪来？ 还有哪些候选？ 为什么选它？
--
-- 字段名有两个层次，不能混用（早期版本靠"两边同名"隐式关联，结果对 title/genre 恒 0 行）：
--   * 断言表 field_assertion 用**源级字段名**：title / genre / release_date / external_id
--   * 选择表 field_selection 用**实体级字段名**：canonical_title / genres / …
-- 所以这里先做一次显式映射，再用 candidate_ids_json 关联候选。
-- 多值字段（genres）没有"被选中的那一条断言"，成员存在 candidate_ids_json 里，
-- 用 json_each 展开，每个成员一行（selection_kind='multi'）。
WITH sel AS (
    SELECT * FROM field_selection
     WHERE entity_type = 'movie' AND entity_id = :movie_id
       AND field_name = CASE :field_name
                            WHEN 'title' THEN 'canonical_title'
                            WHEN 'genre' THEN 'genres'
                            ELSE :field_name END
)
SELECT
    fs.field_name                                   AS 字段,
    fa_sel.normalized_value                         AS 最终值,
    fa_sel.raw_value                                AS 原始值,
    fa_sel.extractor_rule_id                        AS 抽取规则,
    fa_sel.extractor_version                        AS 规则版本,
    fa_sel.confidence                               AS 置信度,
    sr.source_type                                  AS 来源类型,
    COALESCE(sr.url, sr.locator)                    AS 证据定位,
    sr.snapshot_path                                AS 快照路径,
    sr.content_hash                                 AS 内容哈希,
    sr.fetched_at                                   AS 获取时间,
    fs.decision_reason                              AS 选择理由,
    fs.rule_id                                      AS 选择规则,
    fs.decided_at                                   AS 决策时间
FROM sel fs
JOIN field_assertion fa_sel
  ON fa_sel.assertion_id = fs.selected_assertion_id
  OR (fs.selection_kind = 'multi'
      AND fa_sel.assertion_id IN (SELECT value FROM json_each(fs.candidate_ids_json)))
JOIN raw_source_record sr ON sr.record_id = fa_sel.source_record_id
ORDER BY 最终值;
