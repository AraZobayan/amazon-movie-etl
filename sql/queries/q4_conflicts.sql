-- 验收查询 4：查询字段冲突与选择理由
-- 输入：:limit
-- 要点：一个字段有多条候选时，这里同时列出"选中的"和"被拒绝的"，
--       以及采用规则。作业要求"保留所有候选值及采用、拒绝或待确认状态"。
-- 候选通过 field_selection.candidate_ids_json 关联，**不依赖断言表与选择表字段名相同**
-- （断言是源级名 title/genre，选择是实体级名 canonical_title/genres）。
-- 早期版本按 field_name 直接 JOIN，导致 title/genres 的候选数恒为 0、
-- 被 HAVING 候选总数>1 全部过滤 —— 也就是"标题冲突永远查不到"。
SELECT
    fs.entity_id                                   AS 作品ID,
    m.canonical_title                              AS 作品,
    fs.field_name                                  AS 字段,
    CASE WHEN fs.selection_kind='multi'
         THEN '（多值并集，成员见候选）' ELSE fa_sel.normalized_value END AS 采用值,
    fa_sel.extractor_rule_id                       AS 采用规则,
    fs.decision_reason                             AS 选择理由,
    COUNT(fa_all.assertion_id)                     AS 候选总数,
    SUM(CASE WHEN fa_all.status='rejected' THEN 1 ELSE 0 END) AS 被拒绝数,
    GROUP_CONCAT(DISTINCT CASE WHEN fa_all.status='rejected'
                    THEN fa_all.normalized_value || '(' || fa_all.extractor_rule_id || ')'
                    END)                           AS 被拒绝的候选
FROM field_selection fs
JOIN movie m              ON m.movie_id = fs.entity_id
LEFT JOIN field_assertion fa_sel ON fa_sel.assertion_id = fs.selected_assertion_id
LEFT JOIN field_assertion fa_all
       ON fa_all.assertion_id IN (SELECT value FROM json_each(fs.candidate_ids_json))
WHERE fs.entity_type='movie'
GROUP BY fs.entity_id, fs.field_name
HAVING COUNT(fa_all.assertion_id) > 1
ORDER BY 候选总数 DESC, m.canonical_title
LIMIT :limit;
