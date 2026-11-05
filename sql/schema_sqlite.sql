-- =============================================================================
--  Amazon 电影评论与多源电影信息集成 —— 逻辑模型 DDL (SQLite 运行时版本)
-- -----------------------------------------------------------------------------
--  分层约定（对应作业说明第八部分"处理层次"）：
--    raw_*   原始层：原样落盘的数据与"证据定位"，不做任何清洗
--    stg_*   暂存层：结构化抽取值 + 异常/隔离记录
--    int_*   集成层：候选对、合并决策（实体消歧的中间结果）
--    无前缀  目标层：可查询的实体、关系、字段血缘
--    meta_*  元数据层：批次、步骤、检查点、质量问题、评价
--
--  设计铁律（本作业的核心，见 docs 手册第 5 章）：
--    1. 目标实体的"最终字段值"不直接落库，而是存一个指向 field_assertion
--       的指针（field_selection.selected_assertion_id）。
--    2. 所有主键都是"确定性 ID"（由内容哈希算出），因此重复运行得到同样的
--       ID —— 这是装载幂等（idempotency）的基础。
--    3. 原始记录永不删除、永不覆盖；一切清洗结果都是"新增一条断言"。
-- =============================================================================

PRAGMA foreign_keys = ON;

-- ============================ 元数据层 =======================================

-- 一次完整的 ETL 运行
CREATE TABLE IF NOT EXISTS meta_etl_run (
    run_id          TEXT PRIMARY KEY,       -- RUN-<时间戳>-<随机>
    pipeline        TEXT NOT NULL,          -- run_all / single step
    tool_version    TEXT NOT NULL,
    rules_version   TEXT NOT NULL,          -- 规则版本：规则一变，血缘要能回溯
    started_at      TEXT NOT NULL,          -- ISO8601 UTC
    finished_at     TEXT,
    status          TEXT NOT NULL CHECK (status IN ('running','success','failed','aborted')),
    params_json     TEXT,
    note            TEXT
);

-- 一次运行里的每个步骤（用于失败分支、断点恢复、耗时分析）
CREATE TABLE IF NOT EXISTS meta_etl_step (
    run_id          TEXT NOT NULL REFERENCES meta_etl_run(run_id),
    step_name       TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('pending','running','success','failed','skipped')),
    started_at      TEXT,
    finished_at     TEXT,
    counters_json   TEXT,                   -- {"in":x,"out":y,"quarantined":z}
    error           TEXT,
    PRIMARY KEY (run_id, step_name)
);

-- 断点续跑的游标：某个步骤处理到第几批、游标停在哪
CREATE TABLE IF NOT EXISTS meta_checkpoint (
    pipeline        TEXT NOT NULL,
    step_name       TEXT NOT NULL,
    batch_no        INTEGER NOT NULL,
    cursor_value    TEXT,                   -- 例如源文件字节偏移 / 记录序号
    rows_done       INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (pipeline, step_name, batch_no)
);

-- 质量问题（不是"报错"，而是"数据不对劲"的记录）
CREATE TABLE IF NOT EXISTS meta_quality_issue (
    issue_id        TEXT PRIMARY KEY,
    run_id          TEXT,
    entity_type     TEXT,
    entity_id       TEXT,
    field_name      TEXT,
    issue_type      TEXT NOT NULL,          -- score_out_of_range / helpful_exceeds_total / orphan_fk / ...
    severity        TEXT NOT NULL CHECK (severity IN ('info','warn','error')),
    detail          TEXT,
    created_at      TEXT NOT NULL
);

-- 实体匹配的人工评价集（开发样本与评价样本必须分开！）
CREATE TABLE IF NOT EXISTS meta_eval_pair (
    pair_id         TEXT PRIMARY KEY,
    entity_type     TEXT NOT NULL CHECK (entity_type IN ('movie','person')),
    id_a            TEXT NOT NULL,
    id_b            TEXT NOT NULL,
    predicted       INTEGER NOT NULL CHECK (predicted IN (0,1)),  -- 算法判定是否同一实体
    label           INTEGER CHECK (label IN (0,1)),               -- 人工标注：1=同一实体
    sample_group    TEXT NOT NULL CHECK (sample_group IN ('dev','eval')),
    difficulty      TEXT,                   -- 同名作品/翻拍/合集/缩写/同名人物
    note            TEXT
);

-- 评价指标落库，便于报告引用
CREATE TABLE IF NOT EXISTS meta_eval_metric (
    metric_id       TEXT PRIMARY KEY,
    run_id          TEXT,
    entity_type     TEXT NOT NULL,
    sample_group    TEXT NOT NULL,
    tp              INTEGER NOT NULL,
    fp              INTEGER NOT NULL,
    fn              INTEGER NOT NULL,
    tn              INTEGER NOT NULL,
    precision       REAL,
    recall          REAL,
    f1              REAL,
    note            TEXT
);

-- ============================ 原始层 =========================================

-- 源文件登记：这份数据到底是什么（作业要求记录下载地址/时间/大小/SHA-256/版本）
CREATE TABLE IF NOT EXISTS raw_source_file (
    file_id         TEXT PRIMARY KEY,       -- F-<sha256 前16位>
    name            TEXT NOT NULL,
    url             TEXT,
    download_started_at  TEXT,
    download_finished_at TEXT,
    byte_size       INTEGER,
    sha256          TEXT NOT NULL,
    content_type    TEXT,
    range_start     INTEGER,                -- 切片起点（全量时为 0）
    range_end       INTEGER,                -- 切片终点（全量时为文件大小-1）
    note            TEXT
);

-- 原始证据定位：任何一条最终值，最终都要能走到这里
CREATE TABLE IF NOT EXISTS raw_source_record (
    record_id       TEXT PRIMARY KEY,       -- REC-<hash>
    source_type     TEXT NOT NULL CHECK (source_type IN
                        ('review_file','product_page','third_party','synthetic_fixture')),
    source_file_id  TEXT REFERENCES raw_source_file(file_id),
    locator         TEXT,                   -- 行号 / 记录序号 / URL 片段
    url             TEXT,                   -- 网页地址（页面类来源）
    snapshot_path   TEXT,                   -- 本地快照文件相对路径
    content_hash    TEXT,                   -- 快照内容的 SHA-256
    fetched_at      TEXT,                   -- 获取时间（与评论历史时间分开记录）
    note            TEXT
);

-- 原始评论：一字不改地保留，包括异常值
CREATE TABLE IF NOT EXISTS raw_review (
    source_review_id TEXT PRIMARY KEY,      -- SR-<hash(文件sha256:记录序号)>
    source_file_id  TEXT NOT NULL REFERENCES raw_source_file(file_id),
    record_seq      INTEGER NOT NULL,       -- 文件内第几条记录（从 1 开始）
    line_no         INTEGER,                -- 起始行号，便于人工回查
    product_id      TEXT,                   -- ASIN
    reviewer_id     TEXT,
    profile_name    TEXT,
    helpfulness_raw TEXT,                   -- "3/4" 原样保留
    score_raw       TEXT,
    review_time_raw TEXT,
    summary_raw     TEXT,
    review_text_raw TEXT,
    record_sha256   TEXT,                   -- 单条记录的哈希（防篡改/可校验）
    run_id          TEXT,
    is_valid        INTEGER NOT NULL DEFAULT 1,
    quarantine_reason TEXT
);

-- ============================ 暂存层 =========================================

-- 结构化后的评论（已解析、已校验，但还没去重）
CREATE TABLE IF NOT EXISTS stg_review (
    source_review_id TEXT PRIMARY KEY REFERENCES raw_review(source_review_id),
    product_id      TEXT,
    reviewer_id     TEXT,
    profile_name    TEXT,
    helpful_votes   INTEGER,
    total_votes     INTEGER,
    helpfulness_ratio REAL,                 -- 0/0 -> NULL（作业明确要求）
    score           REAL,                   -- 1..5，越界进隔离
    review_time_unix INTEGER,
    review_time_utc TEXT,
    summary         TEXT,
    review_text     TEXT,
    dedup_key       TEXT,                   -- 逻辑评论去重键
    review_id       TEXT,                   -- 指向目标层 review
    run_id          TEXT
);

-- 隔离区：解析/校验失败的记录，保留原文与原因
CREATE TABLE IF NOT EXISTS stg_quarantine (
    quarantine_id   TEXT PRIMARY KEY,
    source_review_id TEXT,
    field_name      TEXT,
    raw_value       TEXT,
    reason          TEXT NOT NULL,          -- score_out_of_range / missing_product_id / ...
    run_id          TEXT
);

-- 页面解析结果（快照 -> 结构化字段）
CREATE TABLE IF NOT EXISTS stg_product_page (
    snapshot_id     TEXT PRIMARY KEY,       -- SNP-<hash>
    product_id      TEXT NOT NULL,          -- 请求时的 ASIN
    source_record_id TEXT REFERENCES raw_source_record(record_id),
    asin_on_page    TEXT,                   -- 页面上真实出现的 ASIN（用于核对是否被重定向）
    page_kind       TEXT,                   -- product / captcha / not_found / error / redirect
    title_raw       TEXT,
    byline_raw      TEXT,                   -- 导演/主演行
    release_date_raw TEXT,
    format_raw      TEXT,                   -- DVD / Blu-ray / Prime Video ...
    studio_raw      TEXT,
    parse_status    TEXT CHECK (parse_status IN ('ok','partial','failed')),
    extractor_version TEXT
);

-- 产品分类结果（电影/非电影/合集/剧集/演唱会或其他/待确认）
CREATE TABLE IF NOT EXISTS stg_classification (
    product_id      TEXT PRIMARY KEY,
    category        TEXT NOT NULL CHECK (category IN
                        ('movie','non_movie','box_set','series','concert_other','pending')),
    basis           TEXT,                   -- 判定依据（人可读）
    evidence_json   TEXT,                   -- 命中的规则与字段
    confidence      REAL,
    rule_id         TEXT,
    run_id          TEXT
);

-- ============================ 集成层 =========================================

-- 候选对：实体消歧的中间结果（保留，用于解释"为什么合并/为什么不合并"）
CREATE TABLE IF NOT EXISTS int_pair_candidate (
    candidate_id    TEXT PRIMARY KEY,
    entity_type     TEXT NOT NULL CHECK (entity_type IN ('movie','person')),
    id_a            TEXT NOT NULL,
    id_b            TEXT NOT NULL,
    block_key       TEXT,                   -- 分块键（blocking），把 O(n^2) 降下来
    score           REAL NOT NULL,
    features_json   TEXT,                   -- {"title_sim":0.92,"year_diff":0,...}
    rules_version   TEXT,
    run_id          TEXT
);

-- 合并决策：自动合并 / 人工复核 / 不合并
CREATE TABLE IF NOT EXISTS int_merge_decision (
    decision_id     TEXT PRIMARY KEY,
    entity_type     TEXT NOT NULL,
    action          TEXT NOT NULL CHECK (action IN ('auto_merge','manual_review','no_merge')),
    id_a            TEXT NOT NULL,
    id_b            TEXT NOT NULL,
    cluster_key     TEXT,
    score           REAL,
    threshold_version TEXT,
    rule_id         TEXT,
    evidence_json   TEXT,
    run_id          TEXT,
    decided_at      TEXT
);

-- ============================ 目标层 =========================================

-- 产品（ASIN）：只做标识与汇总，不承载电影语义
CREATE TABLE IF NOT EXISTS product (
    product_id      TEXT PRIMARY KEY,       -- ASIN，天然主键
    first_run_id    TEXT,
    first_seen_at   TEXT,
    review_count    INTEGER NOT NULL DEFAULT 0,
    is_movie        INTEGER                      -- 由分类结果回填
);

-- 采集清单 + 状态机（作业要求：互斥状态数之和 = 不同 ASIN 总数）
CREATE TABLE IF NOT EXISTS product_collection (
    product_id      TEXT PRIMARY KEY REFERENCES product(product_id),
    original_url    TEXT,
    final_url       TEXT,
    status          TEXT NOT NULL CHECK (status IN
                        ('success','not_found','access_restricted','timeout',
                         'parse_failed','not_collected','pending')),
    http_status     INTEGER,
    attempts        INTEGER NOT NULL DEFAULT 0,
    fetched_at      TEXT,
    snapshot_path   TEXT,
    content_hash    TEXT,
    source_type     TEXT,                   -- live / offline_snapshot / third_party / synthetic_fixture
    reason_code     TEXT,
    reason_detail   TEXT,
    -- 以下三列是"HTTP 200 不等于采集成功"的判据与证据（对应作业说明任务 2 第 3 条）：
    canonical_url   TEXT,                   -- 页面自报的 canonical 链接，用来核对是否就是目标 ASIN
    asin_on_page    TEXT,                   -- 从 canonical 解析出的 ASIN，与 product_id 比对
    template_sig    TEXT,                   -- 页面模板指纹：结构标记集合的短哈希，用于发现模板变化
    run_id          TEXT
);

-- 评论用户（不因同名/改名而合并）
CREATE TABLE IF NOT EXISTS reviewer (
    reviewer_id     TEXT PRIMARY KEY,       -- review/userId，原始值
    profile_name_raw TEXT,
    first_seen_at   TEXT
);

-- 逻辑评论（去重后的一条"观影评价"）
CREATE TABLE IF NOT EXISTS review (
    review_id       TEXT PRIMARY KEY,       -- R-<hash(dedup_key)>
    product_id      TEXT NOT NULL REFERENCES product(product_id),
    reviewer_id     TEXT,
    review_time_utc TEXT,
    score           REAL,
    helpful_votes   INTEGER,
    total_votes     INTEGER,
    helpfulness_ratio REAL,
    summary         TEXT,
    review_text     TEXT,
    dedup_key       TEXT NOT NULL,
    raw_count       INTEGER NOT NULL DEFAULT 1,  -- 这条逻辑评论由几条原始记录合并而来
    run_id          TEXT
);

-- 逻辑评论 ↔ 原始记录（多对一：原始记录全部保留）
CREATE TABLE IF NOT EXISTS review_mapping (
    review_id       TEXT NOT NULL REFERENCES review(review_id),
    source_review_id TEXT NOT NULL REFERENCES raw_review(source_review_id),
    PRIMARY KEY (review_id, source_review_id)
);

-- 电影作品（合并后的实体）—— 注意：不是 ASIN！
CREATE TABLE IF NOT EXISTS movie (
    movie_id        TEXT PRIMARY KEY,       -- M-<hash(规范化标题|年份)>
    canonical_title TEXT,
    original_title  TEXT,
    release_year    INTEGER,
    release_date    TEXT,                   -- YYYY-MM-DD，仅在精确到日时填写
    release_precision TEXT CHECK (release_precision IN ('day','month','year','unknown')),
    release_region  TEXT,
    release_type    TEXT,                   -- theatrical / premiere / festival
    edition_scope   TEXT,                   -- 归并层次说明（剧场版/DVD/蓝光/修复版…）
    status          TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','pending_review','merged_away')),
    created_run_id  TEXT
);

-- 作品标题的多语言/别名（原始标题必须保留）
CREATE TABLE IF NOT EXISTS movie_title (
    movie_id        TEXT NOT NULL REFERENCES movie(movie_id),
    title           TEXT NOT NULL,
    lang            TEXT,
    title_type      TEXT NOT NULL CHECK (title_type IN ('original','canonical','alias')),
    source_record_id TEXT REFERENCES raw_source_record(record_id),
    PRIMARY KEY (movie_id, title, title_type)
);

-- 商品 ↔ 作品：多对多（合集就是靠这张表表达的）
CREATE TABLE IF NOT EXISTS product_movie (
    product_id      TEXT NOT NULL REFERENCES product(product_id),
    movie_id        TEXT NOT NULL REFERENCES movie(movie_id),
    relation_type   TEXT NOT NULL CHECK (relation_type IN
                        ('single','box_set_member','series_season','unresolved')),
    match_method    TEXT,                   -- title_year_director / external_id / manual
    confidence      REAL,
    decision_id     TEXT REFERENCES int_merge_decision(decision_id),
    source_record_id TEXT REFERENCES raw_source_record(record_id),
    PRIMARY KEY (product_id, movie_id, relation_type)
);

-- 人物实体
CREATE TABLE IF NOT EXISTS person (
    person_id       TEXT PRIMARY KEY,       -- P-<hash(规范名)>
    canonical_name  TEXT NOT NULL,
    canonical_name_norm TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','pending_review','merged_away')),
    created_run_id  TEXT
);

-- 人物姓名与别名（含原始署名）
CREATE TABLE IF NOT EXISTS person_name (
    person_id       TEXT NOT NULL REFERENCES person(person_id),
    name            TEXT NOT NULL,
    name_norm       TEXT NOT NULL,
    name_type       TEXT NOT NULL CHECK (name_type IN ('canonical','alias','raw_credit','abbrev')),
    source_record_id TEXT REFERENCES raw_source_record(record_id),
    PRIMARY KEY (person_id, name, name_type)
);

-- 作品 ↔ 人物：角色、署名顺序、来源
CREATE TABLE IF NOT EXISTS movie_person (
    movie_id        TEXT NOT NULL REFERENCES movie(movie_id),
    person_id       TEXT NOT NULL REFERENCES person(person_id),
    role            TEXT NOT NULL CHECK (role IN ('director','star','cast','writer','producer')),
    billing_order   INTEGER,                -- 来源中的署名顺序（不解释成"主演排名"）
    source_record_id TEXT REFERENCES raw_source_record(record_id),
    PRIMARY KEY (movie_id, person_id, role)
);

-- ------------------------- 血缘三件套 ----------------------------------------

-- 1) 候选值：某个实体的某个字段，从某条原始记录里抽出来的一个候选
CREATE TABLE IF NOT EXISTS field_assertion (
    assertion_id    TEXT PRIMARY KEY,       -- A-<hash(实体|字段|来源|规范化值)>
    entity_type     TEXT NOT NULL CHECK (entity_type IN ('movie','person','product','review')),
    entity_id       TEXT NOT NULL,
    field_name      TEXT NOT NULL,
    raw_value       TEXT,                   -- 原样（页面上怎么写就怎么存）
    normalized_value TEXT,                  -- 规范化后的值
    value_num       REAL,                   -- 数值型便于比较/排序
    source_record_id TEXT NOT NULL REFERENCES raw_source_record(record_id),
    extractor_rule_id TEXT NOT NULL,        -- 哪条抽取规则
    extractor_version TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 0.5,
    status          TEXT NOT NULL CHECK (status IN
                        ('candidate','selected','rejected','conflict','pending')),
    run_id          TEXT,
    created_at      TEXT NOT NULL
);

-- 2) 处理过程：候选之间的父子关系（规范化、推断、合并都能重建）
CREATE TABLE IF NOT EXISTS field_candidate_link (
    parent_assertion_id TEXT NOT NULL REFERENCES field_assertion(assertion_id),
    child_assertion_id  TEXT NOT NULL REFERENCES field_assertion(assertion_id),
    operation       TEXT NOT NULL CHECK (operation IN
                        ('normalize','infer','merge','translate','manual_fix')),
    rule_id         TEXT,
    note            TEXT,
    PRIMARY KEY (parent_assertion_id, child_assertion_id)
);

-- 3) 最终选择：为什么采用这个值
CREATE TABLE IF NOT EXISTS field_selection (
    entity_type     TEXT NOT NULL,
    entity_id       TEXT NOT NULL,
    field_name      TEXT NOT NULL,
    selected_assertion_id TEXT REFERENCES field_assertion(assertion_id),
    candidate_ids_json TEXT,                -- 全部候选（含被拒绝的）
    decision_reason TEXT NOT NULL,          -- 人可读的采用理由
    rule_id         TEXT NOT NULL,
    -- 这一列是为了消除一个真实的歧义：
    --   genres / cast 这类多值字段取并集，"选中的值"不止一个，
    --   所以 selected_assertion_id 只能留空，成员记在 candidate_ids_json 里；
    --   而单值字段的 selected_assertion_id 为空，含义是"有候选但全被规则拒绝"。
    -- 两者在 SQL 里长得一模一样，覆盖率会算错、质量检查会误报。
    -- 显式记下选择方式，查询才不需要靠 rule_id 名字猜。
    selection_kind  TEXT CHECK (selection_kind IN ('single','multi')),
    run_id          TEXT,
    decided_at      TEXT NOT NULL,
    PRIMARY KEY (entity_type, entity_id, field_name)
);

-- ============================ 索引 ===========================================
CREATE INDEX IF NOT EXISTS idx_raw_review_product   ON raw_review(product_id);
CREATE INDEX IF NOT EXISTS idx_raw_review_valid     ON raw_review(is_valid);
CREATE INDEX IF NOT EXISTS idx_stg_review_product   ON stg_review(product_id);
CREATE INDEX IF NOT EXISTS idx_stg_review_dedup     ON stg_review(dedup_key);
CREATE INDEX IF NOT EXISTS idx_review_product       ON review(product_id);
CREATE INDEX IF NOT EXISTS idx_review_reviewer      ON review(reviewer_id);
CREATE INDEX IF NOT EXISTS idx_collection_status    ON product_collection(status);
CREATE INDEX IF NOT EXISTS idx_class_category       ON stg_classification(category);
CREATE INDEX IF NOT EXISTS idx_product_movie_movie  ON product_movie(movie_id);
CREATE INDEX IF NOT EXISTS idx_movie_title          ON movie(canonical_title);
CREATE INDEX IF NOT EXISTS idx_movie_year           ON movie(release_year);
CREATE INDEX IF NOT EXISTS idx_movie_person_person  ON movie_person(person_id);
CREATE INDEX IF NOT EXISTS idx_assertion_entity     ON field_assertion(entity_type, entity_id, field_name);
CREATE INDEX IF NOT EXISTS idx_assertion_status     ON field_assertion(status);
CREATE INDEX IF NOT EXISTS idx_assertion_source     ON field_assertion(source_record_id);
CREATE INDEX IF NOT EXISTS idx_pair_entity          ON int_pair_candidate(entity_type, id_a);
CREATE INDEX IF NOT EXISTS idx_issue_type           ON meta_quality_issue(issue_type);
