-- PostgreSQL 初始化。
--
-- 这里只做「数据库级」的一次性准备，不建业务表——业务表一律交给 Alembic 迁移，
-- 否则会出现「init.sql 和迁移脚本各建一套、字段不一致」的经典问题。

-- 全文检索用的扩展（阶段 3 的 tsvector 检索会用到）
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 生成 UUID 的能力（备用；当前主键是 BIGSERIAL）
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 说明：多租户的行级隔离由应用层的 do_orm_execute 钩子实现，
-- 这里**不使用** PostgreSQL 的 RLS（Row Level Security）。
-- 原因：RLS 需要每个请求 SET LOCAL app.tenant_id，与连接池复用有微妙的交互，
-- 且会让「漏设上下文」从「查到全量」变成「查到零行」——后者更隐蔽、更难排查。
-- 应用层钩子 + repository 守卫的组合，错误暴露得更直接。
