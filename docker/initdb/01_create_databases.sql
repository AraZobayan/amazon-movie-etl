-- 一次建两个库：etl 放作业的数据仓库，airflow 放 Airflow 自己的元数据库。
-- 两者必须分开：Airflow 升级会改自己的 schema，不能和业务表混在一个库里。
CREATE USER etl WITH PASSWORD 'etl';
CREATE DATABASE etl OWNER etl;

CREATE USER airflow WITH PASSWORD 'airflow';
CREATE DATABASE airflow OWNER airflow;

-- 作业用到的扩展：uuid 生成、文本相似度（实体匹配的候选打分可以用）
\connect etl
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
