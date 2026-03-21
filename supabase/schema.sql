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

create table if not exists daily_plans (
  id bigserial primary key,
  chat_id text not null,
  plan_date date not null,
  ordered_task_ids jsonb not null default '[]'::jsonb,
  rationale jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_tasks_chat_status_due on tasks (chat_id, status, due_at);
create index if not exists idx_tasks_chat_created on tasks (chat_id, created_at desc);
create index if not exists idx_daily_plans_chat_date on daily_plans (chat_id, plan_date desc);

create or replace function set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_user_profiles_updated_at on user_profiles;
create trigger trg_user_profiles_updated_at
before update on user_profiles
for each row execute procedure set_updated_at();

drop trigger if exists trg_tasks_updated_at on tasks;
create trigger trg_tasks_updated_at
before update on tasks
for each row execute procedure set_updated_at();
