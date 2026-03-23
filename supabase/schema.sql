create table if not exists user_profiles (
  chat_id text primary key,
  timezone text not null default 'Asia/Hong_Kong',
  wake_time time,
  focus_window text,
  break_pref text,
  max_daily_tasks int not null default 6,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists tasks (
  id bigserial primary key,
  chat_id text not null,
  list_id bigint,
  task_no int,
  title text not null,
  due_at timestamptz,
  priority smallint not null default 2 check (priority between 1 and 3),
  status text not null default 'open' check (status in ('open', 'done')),
  effort_min int check (effort_min is null or effort_min > 0),
  energy_need text not null default 'medium' check (energy_need in ('low', 'medium', 'high')),
  source_text text,
  source_message_id text unique,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz
);

alter table tasks add column if not exists list_id bigint;
alter table tasks add column if not exists task_no int;

create table if not exists daily_plans (
  id bigserial primary key,
  chat_id text not null,
  plan_date date not null,
  ordered_task_ids jsonb not null default '[]'::jsonb,
  rationale jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists whitelist_contacts (
  sender_id text primary key,
  label text,
  created_at timestamptz not null default now()
);

-- Legacy one-to-one mapping table (kept for backward compatibility)
create table if not exists task_list_bindings (
  chat_id text primary key,
  list_chat_id text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists task_lists (
  id bigserial primary key,
  name text not null default 'Default',
  owner_chat_id text not null,
  scope_chat_id text not null unique,
  is_archived boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists task_list_members (
  id bigserial primary key,
  chat_id text not null,
  list_id bigint not null,
  role text not null default 'member' check (role in ('owner', 'member')),
  is_default boolean not null default false,
  created_at timestamptz not null default now(),
  unique (chat_id, list_id)
);

create table if not exists admin_users (
  id bigserial primary key,
  email text not null unique,
  display_name text,
  password_hash text not null,
  status text not null default 'active' check (status in ('active', 'disabled')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  last_login_at timestamptz
);

create index if not exists idx_tasks_chat_status_due on tasks (chat_id, status, due_at);
create index if not exists idx_tasks_chat_created on tasks (chat_id, created_at desc);
create index if not exists idx_tasks_list_status_due on tasks (list_id, status, due_at);
create index if not exists idx_daily_plans_chat_date on daily_plans (chat_id, plan_date desc);
create unique index if not exists idx_tasks_chat_task_no on tasks (chat_id, task_no);
create unique index if not exists idx_tasks_list_task_no on tasks (list_id, task_no) where list_id is not null;
create index if not exists idx_task_list_bindings_list_chat on task_list_bindings (list_chat_id);
create index if not exists idx_task_lists_owner on task_lists (owner_chat_id, created_at desc);
create index if not exists idx_task_list_members_chat on task_list_members (chat_id);
create index if not exists idx_task_list_members_list on task_list_members (list_id);
create index if not exists idx_task_list_members_chat_default on task_list_members (chat_id, is_default);
create unique index if not exists idx_admin_users_email_lower on admin_users (lower(email));

create or replace function set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create or replace function assign_task_no()
returns trigger as $$
declare
  next_no int;
begin
  if new.task_no is not null then
    return new;
  end if;

  if new.list_id is not null then
    select coalesce(max(task_no), 0) + 1
      into next_no
    from tasks
    where list_id = new.list_id;
  else
    select coalesce(max(task_no), 0) + 1
      into next_no
    from tasks
    where chat_id = new.chat_id;
  end if;

  new.task_no = next_no;
  return new;
end;
$$ language plpgsql;

-- Foreign keys guarded by catalog checks for idempotent re-run
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'fk_task_list_members_list_id'
  ) THEN
    ALTER TABLE task_list_members
      ADD CONSTRAINT fk_task_list_members_list_id
      FOREIGN KEY (list_id) REFERENCES task_lists(id) ON DELETE CASCADE;
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'fk_tasks_list_id'
  ) THEN
    ALTER TABLE tasks
      ADD CONSTRAINT fk_tasks_list_id
      FOREIGN KEY (list_id) REFERENCES task_lists(id) ON DELETE SET NULL;
  END IF;
END $$;

-- Backfill task_lists and memberships from existing chats
insert into task_lists (name, owner_chat_id, scope_chat_id)
select 'Default', x.chat_id, x.chat_id
from (
  select distinct chat_id from tasks where chat_id is not null and chat_id <> ''
  union
  select distinct chat_id from user_profiles where chat_id is not null and chat_id <> ''
  union
  select distinct chat_id from task_list_bindings where chat_id is not null and chat_id <> ''
  union
  select distinct list_chat_id as chat_id from task_list_bindings where list_chat_id is not null and list_chat_id <> ''
) x
on conflict (scope_chat_id) do nothing;

insert into task_list_members (chat_id, list_id, role, is_default)
select tl.owner_chat_id, tl.id, 'owner', true
from task_lists tl
where not exists (
  select 1
  from task_list_members m
  where m.chat_id = tl.owner_chat_id
    and m.list_id = tl.id
)
on conflict (chat_id, list_id) do nothing;

-- Ensure each chat has at least one default membership
with ranked as (
  select
    m.id,
    m.chat_id,
    m.is_default,
    row_number() over (partition by m.chat_id order by m.is_default desc, m.created_at asc, m.id asc) as rn,
    max(case when m.is_default then 1 else 0 end) over (partition by m.chat_id) as has_default
  from task_list_members m
)
update task_list_members m
set is_default = case
  when r.has_default = 0 and r.rn = 1 then true
  else m.is_default
end
from ranked r
where m.id = r.id;

-- Backfill tasks.list_id by matching scope_chat_id
update tasks t
set list_id = tl.id
from task_lists tl
where t.list_id is null
  and tl.scope_chat_id = t.chat_id;

-- Fill missing task_no with per-list (or fallback per-chat) sequence
update tasks t
set task_no = x.task_no
from (
  select
    id,
    row_number() over (partition by coalesce(list_id::text, chat_id) order by created_at, id) as task_no
  from tasks
) as x
where t.id = x.id
  and t.task_no is null;

drop trigger if exists trg_user_profiles_updated_at on user_profiles;
create trigger trg_user_profiles_updated_at
before update on user_profiles
for each row execute procedure set_updated_at();

drop trigger if exists trg_tasks_updated_at on tasks;
create trigger trg_tasks_updated_at
before update on tasks
for each row execute procedure set_updated_at();

drop trigger if exists trg_tasks_assign_task_no on tasks;
create trigger trg_tasks_assign_task_no
before insert on tasks
for each row execute procedure assign_task_no();

drop trigger if exists trg_task_list_bindings_updated_at on task_list_bindings;
create trigger trg_task_list_bindings_updated_at
before update on task_list_bindings
for each row execute procedure set_updated_at();

drop trigger if exists trg_task_lists_updated_at on task_lists;
create trigger trg_task_lists_updated_at
before update on task_lists
for each row execute procedure set_updated_at();

drop trigger if exists trg_admin_users_updated_at on admin_users;
create trigger trg_admin_users_updated_at
before update on admin_users
for each row execute procedure set_updated_at();
