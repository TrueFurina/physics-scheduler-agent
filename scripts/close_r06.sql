-- =============================================================================
-- close_r06.sql —— R-06 收尾：播种管理员 + 收紧审批策略
--
-- 前置（已完成，2026-10-15 23:5x）：public.app_admins 已建表、已 ENABLE RLS、
--   已 GRANT SELECT TO authenticated、已有 app_admins_read_self 策略、
--   已 REVOKE truncate/trigger/references。
-- 本脚本只做剩余两步：③ 播种、④ 换策略。
--
-- ⚠️ 顺序强约束：③ 与 ④ **必须同批**。
--    若只做 ④（策略收紧而表为空），审批台会对**所有人**失效（含演示）。
--
-- 执行方式：一条一条发（平台 db_exec_sql 单次仅接受一条）。
--   mcp__genie-baas__workbuddy_cloudservice_db_exec_sql
--     { applicationId: "wbapp_C5QC1ilCbEStyirXoqSAoS", mode: "migrate", sql: "<单条>" }
--
-- 本方案已在回滚事务内预验证（scripts/probe_rls_r06.sql · PROBE2）：
--   非管理员 → 0 行；已播种管理员 → 1 行。
--
-- 回滚：见文件末尾。
-- =============================================================================


-- ---------------------------------------------------------------------------
-- ① 取真实 auth.uid()
--    原理：auth.uid() = JWT 的 sub 声明（已从 pg_proc 读到函数体），
--          而 auth.users.sub 就是该值。故下面这条查出的 sub **就是** auth.uid()。
--    要先完成一次真实登录，此行才会出现（现有唯一行是平台 internal 管理员，不可用）。
-- ---------------------------------------------------------------------------
select sub as real_auth_uid,
       coalesce(username, name) as nick,
       type,
       internal_user_type,
       created_from,
       last_login,
       created_at
  from auth.users
 where sub is not null
   and type is distinct from 'internal'
 order by created_at desc
 limit 5;

-- 交叉验算（推荐）：提交过一条调课申请后，下面查出的值应与上面的 sub 一致。
-- select owner_id, owner_name, created_at
--   from public.reschedule_requests order by created_at desc limit 3;


-- ---------------------------------------------------------------------------
-- ③ 播种管理员
--    写法 A（显式值，把 <real_auth_uid> 换成 ① 的结果）：
-- ---------------------------------------------------------------------------
-- insert into public.app_admins(user_id) values ('<real_auth_uid>')
--   on conflict (user_id) do nothing;

--    写法 B（免手抄，直接取"最近登录的非 internal 用户"）——推荐：
-- ---------------------------------------------------------------------------
-- insert into public.app_admins(user_id)
-- select sub from auth.users
--  where sub is not null
--    and type is distinct from 'internal'
--  order by coalesce(last_login, created_at) desc
--  limit 1
--   on conflict (user_id) do nothing;

-- 验收：应为 1（或你的教秘团队人数）
-- select count(*) as n_admins from public.app_admins;


-- ---------------------------------------------------------------------------
-- ④ 收紧审批策略（原有 INSERT / SELECT 策略不动，只重建 UPDATE）
-- ---------------------------------------------------------------------------
drop policy if exists rr_update_approve on public.reschedule_requests;

create policy rr_update_approve on public.reschedule_requests
  for update
  using      (exists (select 1 from public.app_admins a where a.user_id = auth.uid()))
  with check (exists (select 1 from public.app_admins a where a.user_id = auth.uid()));

-- 验收：非管理员登录 → 审批 UPDATE 命中 0 行；管理员 → 1 行
-- （回滚事务内已验证；也可用 scripts/probe_rls_r06.sql 复跑）


-- ---------------------------------------------------------------------------
-- 回滚（把策略退回 v3 状态）
-- ---------------------------------------------------------------------------
-- drop policy if exists rr_update_approve on public.reschedule_requests;
-- create policy rr_update_approve on public.reschedule_requests
--   for update
--   using      (auth.uid() is not null)
--   with check (auth.uid() is not null);
