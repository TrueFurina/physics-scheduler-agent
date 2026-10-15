-- =============================================================================
-- probe_rls_r06.sql —— RLS / R-06 实证探针（可回滚，零残留）
--
-- 用途：在**不改变任何持久状态**的前提下，实测三件事：
--   1) auth.uid() 是否等于 JWT 的 sub 声明
--   2) R-06 是否成立（任意已登录用户能否改写他人工单）
--   3) R-06 的修复策略是否真的能挡住非管理员（上线前预验证）
--
-- 前提：`auth.uid()` 的定义（已从 pg_proc 读出，2026-10-15）为
--   select coalesce(
--     nullif(current_setting('request.jwt.claim.sub', true), ''),
--     (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
--   )::text
-- 即 uid = JWT 的 sub。
--
-- 执行方式：**一次只发一条语句**（平台 db_exec_sql 单次仅接受一条）。
--   mcp__genie-baas__workbuddy_cloudservice_db_exec_sql
--     { applicationId: "wbapp_C5QC1ilCbEStyirXoqSAoS", mode: "migrate", sql: "<下面任一块>" }
--
-- 回滚机制：每块最后以 `raise exception` 借异常把观测值带出，
--   而异常会**回滚整个 DO 块**（含 INSERT/UPDATE 与块内 DDL），故零残留。
--   注意 sequence 不回滚（表内 id 序列会前进几格，无副作用）。
--
-- 实测结果（2026-10-15 23:4x）：
--   PROBE1  uid_A=__probe_user_A__  uid_now=__probe_user_B__  owner_id=__probe_user_A__
--           rows_updated_by_NON_ADMIN_B=1        ← R-06 成立
--   PROBE2  default_owner_id=__probe_other__     ← owner_id 默认值 = auth.uid() 生效
--           nonadmin_update=0  admin_update=1    ← 修复方案有效
--           consult_own_read=1  consult_other_read=0  ← consult_logs 按用户隔离
--   残留核查 rr_rows=0 / cl_rows=0 / admins=0 / rr_policies=4
-- =============================================================================


-- ---------------------------------------------------------------------------
-- PROBE 1 · R-06 实证：A 提交申请，B（另一个已登录的非管理员）尝试审批
-- 期望：rows_updated_by_NON_ADMIN_B = 1  ⇒ 说明权限缺口真实存在
-- ---------------------------------------------------------------------------
do $$
declare
  v_a     text   := '__probe_user_A__';
  v_b     text   := '__probe_user_B__';
  v_id    bigint;
  v_n     int;
  v_uid_a text;
  v_owner text;
begin
  perform set_config('role', 'authenticated', true);

  -- A 会话
  perform set_config('request.jwt.claims',
                     json_build_object('sub', v_a, 'role', 'authenticated')::text, true);
  v_uid_a := auth.uid();

  insert into public.reschedule_requests
    (teacher, course_name, class_name, original_time, target_week, reason, status, owner_id)
  values
    ('__probe__', '__probe__', '__probe__', '__probe__', 1, '__probe__', 'pending', v_a)
  returning id, owner_id into v_id, v_owner;

  -- B 会话：换一个 uid，尝试改 A 的工单
  perform set_config('request.jwt.claims',
                     json_build_object('sub', v_b, 'role', 'authenticated')::text, true);

  update public.reschedule_requests
     set status = 'approved', admin_comment = '__probe__', decided_by = v_b
   where id = v_id;
  get diagnostics v_n = row_count;

  raise exception 'PROBE_RESULT uid_A=% uid_now=% new_id=% owner_id=% rows_updated_by_NON_ADMIN_B=%',
    v_uid_a, auth.uid(), v_id, v_owner, v_n;
end $$;


-- ---------------------------------------------------------------------------
-- PROBE 2 · 修复预验证：同一事务内换上目标策略 + 播种管理员，再看谁改得动
-- 期望：nonadmin_update = 0，admin_update = 1
--       （同时验证 owner_id 默认值与 consult_logs 隔离）
-- ---------------------------------------------------------------------------
do $$
declare
  v_admin text   := '__probe_admin__';
  v_other text   := '__probe_other__';
  v_id    bigint;
  v_cid   bigint;
  v_own   text;
  v_n_other int;
  v_n_admin int;
  v_c_other int;
  v_c_admin int;
begin
  -- 前置（当前高权限角色）：播种 + 换策略；全部在事务内，最后随 raise 回滚
  insert into public.app_admins(user_id) values (v_admin) on conflict do nothing;

  execute 'drop policy rr_update_approve on public.reschedule_requests';
  execute $pol$
    create policy rr_update_approve on public.reschedule_requests
      for update
      using      (exists (select 1 from public.app_admins a where a.user_id = auth.uid()))
      with check (exists (select 1 from public.app_admins a where a.user_id = auth.uid()))
  $pol$;

  perform set_config('role', 'authenticated', true);

  -- 非管理员提交申请（**故意不传 owner_id**，验证 DEFAULT auth.uid()）
  perform set_config('request.jwt.claims',
                     json_build_object('sub', v_other, 'role', 'authenticated')::text, true);
  insert into public.reschedule_requests
    (teacher, course_name, class_name, original_time, target_week, reason)
  values
    ('__probe__', '__probe__', '__probe__', '__probe__', 1, '__probe__')
  returning id, owner_id into v_id, v_own;

  -- 非管理员尝试审批自己那条 → 期望 0
  update public.reschedule_requests set status = 'approved' where id = v_id;
  get diagnostics v_n_other = row_count;

  -- consult_logs 隔离：写一条自己的，自己读得到
  insert into public.consult_logs(module, question, answer, owner_name)
  values ('course_select', '__probe__', '__probe__', '__probe__')
  returning id into v_cid;
  select count(*) into v_c_other from public.consult_logs where id = v_cid;

  -- 切成管理员 → 期望 1；且读不到别人的 consult_log
  perform set_config('request.jwt.claims',
                     json_build_object('sub', v_admin, 'role', 'authenticated')::text, true);
  update public.reschedule_requests set status = 'approved' where id = v_id;
  get diagnostics v_n_admin = row_count;
  select count(*) into v_c_admin from public.consult_logs where id = v_cid;

  raise exception 'PROBE2 default_owner_id=% nonadmin_update=% admin_update=% consult_own_read=% consult_other_read=%',
    v_own, v_n_other, v_n_admin, v_c_other, v_c_admin;
end $$;


-- ---------------------------------------------------------------------------
-- 残留核查（只读，任意时刻可跑）
-- ---------------------------------------------------------------------------
-- select (select count(*) from public.reschedule_requests) as rr_rows,
--        (select count(*) from public.consult_logs)        as cl_rows,
--        (select count(*) from public.app_admins)          as admins,
--        (select count(*) from pg_policies
--          where schemaname = 'public' and tablename = 'reschedule_requests') as rr_policies;
