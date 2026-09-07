-- Fresh-project schema for the MBD Restaurant RAG API.
-- Designed for Supabase Postgres 17 with pgvector.

create schema if not exists extensions;

do $$ begin
  create role anon nologin;
exception when duplicate_object then null;
end $$;
do $$ begin
  create role authenticated nologin;
exception when duplicate_object then null;
end $$;
do $$ begin
  create role service_role nologin bypassrls;
exception when duplicate_object then null;
end $$;

create extension if not exists pgcrypto with schema extensions;
create extension if not exists vector with schema extensions;

create table public.restaurants (
  id uuid primary key default extensions.gen_random_uuid(),
  slug text not null unique check (slug ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'),
  name text not null check (length(trim(name)) between 1 and 120),
  system_prompt text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.restaurant_allowed_origins (
  id uuid primary key default extensions.gen_random_uuid(),
  restaurant_id uuid not null references public.restaurants(id) on delete cascade,
  origin text not null,
  created_at timestamptz not null default now(),
  unique (restaurant_id, origin),
  check (origin = lower(rtrim(origin, '/')))
);

create table public.restaurant_security_settings (
  restaurant_id uuid primary key references public.restaurants(id) on delete cascade,
  ip_max_requests integer not null default 30 check (ip_max_requests > 0),
  ip_window_seconds integer not null default 60 check (ip_window_seconds > 0),
  session_max_requests integer not null default 45 check (session_max_requests > 0),
  session_window_seconds integer not null default 60 check (session_window_seconds > 0),
  token_issue_max_requests integer not null default 30 check (token_issue_max_requests > 0),
  token_issue_window_seconds integer not null default 60 check (token_issue_window_seconds > 0),
  token_max_age_seconds integer not null default 900 check (token_max_age_seconds between 60 and 7200),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.restaurant_subscriptions (
  restaurant_id uuid primary key references public.restaurants(id) on delete cascade,
  stripe_customer_id text unique,
  stripe_subscription_id text unique,
  stripe_payment_link_id text,
  stripe_checkout_session_id text,
  client_reference_id text,
  stripe_price_id text,
  stripe_product_id text,
  stripe_subscription_status text not null check (
    stripe_subscription_status in ('active','trialing','past_due','canceled','unpaid','incomplete','incomplete_expired','paused')
  ),
  current_period_start timestamptz,
  current_period_end timestamptz,
  cancel_at timestamptz,
  canceled_at timestamptz,
  ended_at timestamptz,
  last_checkout_completed_at timestamptz,
  last_synced_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.stripe_webhook_events (
  event_id text primary key,
  event_type text not null,
  stripe_created_at timestamptz,
  restaurant_id uuid references public.restaurants(id) on delete set null,
  stripe_customer_id text,
  stripe_subscription_id text,
  processing_status text not null default 'received' check (processing_status in ('received','processed','failed')),
  payload jsonb not null default '{}'::jsonb,
  error_message text,
  processed_at timestamptz,
  created_at timestamptz not null default now()
);

create table public.audit_events (
  id uuid primary key default extensions.gen_random_uuid(),
  restaurant_id uuid references public.restaurants(id) on delete set null,
  event_type text not null,
  actor text,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table public.chat_sessions (
  id uuid primary key default extensions.gen_random_uuid(),
  restaurant_id uuid not null references public.restaurants(id) on delete cascade,
  session_token text not null,
  client_meta jsonb not null default '{}'::jsonb,
  language text not null default 'eng' check (language ~ '^[a-z]{3}$'),
  created_at timestamptz not null default now(),
  last_activity_at timestamptz not null default now(),
  unique (restaurant_id, session_token)
);

create table public.chat_session_state (
  session_id uuid primary key references public.chat_sessions(id) on delete cascade,
  last_response_id text,
  last_discussed_item_ids jsonb not null default '[]'::jsonb,
  last_candidate_item_ids jsonb not null default '[]'::jsonb,
  last_intent text,
  active_constraints jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.chat_messages (
  id uuid primary key default extensions.gen_random_uuid(),
  session_id uuid not null references public.chat_sessions(id) on delete cascade,
  role text not null check (role in ('user','assistant','system')),
  content text not null,
  sources jsonb,
  latency_ms integer check (latency_ms is null or latency_ms >= 0),
  delivery_status text not null default 'complete' check (delivery_status in ('complete','error')),
  query_type text,
  created_at timestamptz not null default now()
);

create table public.ingest_runs (
  id uuid primary key default extensions.gen_random_uuid(),
  restaurant_id uuid not null references public.restaurants(id) on delete cascade,
  model text not null,
  source_name text not null,
  total_chunks integer not null check (total_chunks >= 0),
  embedded_chunks integer not null default 0 check (embedded_chunks >= 0),
  activated_chunks integer not null default 0 check (activated_chunks >= 0),
  pruned_chunks integer not null default 0 check (pruned_chunks >= 0),
  status text not null check (status in ('running','success','error')),
  error_message text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.knowledge_chunks (
  id uuid primary key,
  restaurant_id uuid not null references public.restaurants(id) on delete cascade,
  source_key text not null,
  external_id text not null,
  text text not null,
  type text,
  source_url text,
  page_path text,
  title text,
  meta_description text,
  image_url text,
  extra_metadata jsonb not null default '{}'::jsonb,
  content_hash text not null check (content_hash ~ '^[0-9a-f]{64}$'),
  embedding_model text not null,
  embedding extensions.vector(1536) not null,
  last_ingest_run_id uuid references public.ingest_runs(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (restaurant_id, source_key, external_id)
);

create table public.knowledge_chunk_staging (
  run_id uuid not null references public.ingest_runs(id) on delete cascade,
  id uuid not null,
  restaurant_id uuid not null references public.restaurants(id) on delete cascade,
  source_key text not null,
  external_id text not null,
  text text not null,
  type text,
  source_url text,
  page_path text,
  title text,
  meta_description text,
  image_url text,
  extra_metadata jsonb not null default '{}'::jsonb,
  content_hash text not null check (content_hash ~ '^[0-9a-f]{64}$'),
  embedding_model text not null,
  embedding extensions.vector(1536),
  created_at timestamptz not null default now(),
  primary key (run_id, source_key, external_id)
);

create index restaurant_allowed_origins_lookup_idx on public.restaurant_allowed_origins (restaurant_id, origin);
create index audit_events_restaurant_created_idx on public.audit_events (restaurant_id, created_at desc);
create index chat_sessions_restaurant_activity_idx on public.chat_sessions (restaurant_id, last_activity_at desc);
create index chat_messages_session_created_idx on public.chat_messages (session_id, created_at);
create index ingest_runs_restaurant_created_idx on public.ingest_runs (restaurant_id, created_at desc);
create index knowledge_chunks_restaurant_source_idx on public.knowledge_chunks (restaurant_id, source_key);
create index knowledge_chunks_embedding_hnsw_idx on public.knowledge_chunks using hnsw (embedding extensions.vector_cosine_ops);

create or replace function public.touch_updated_at() returns trigger
language plpgsql set search_path = public as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger restaurants_touch_updated_at before update on public.restaurants
for each row execute function public.touch_updated_at();
create trigger restaurant_security_touch_updated_at before update on public.restaurant_security_settings
for each row execute function public.touch_updated_at();
create trigger restaurant_subscriptions_touch_updated_at before update on public.restaurant_subscriptions
for each row execute function public.touch_updated_at();
create trigger chat_session_state_touch_updated_at before update on public.chat_session_state
for each row execute function public.touch_updated_at();
create trigger ingest_runs_touch_updated_at before update on public.ingest_runs
for each row execute function public.touch_updated_at();
create trigger knowledge_chunks_touch_updated_at before update on public.knowledge_chunks
for each row execute function public.touch_updated_at();

create or replace function public.restaurant_has_active_subscription(p_restaurant_id uuid) returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from public.restaurant_subscriptions rs
    where rs.restaurant_id = p_restaurant_id
      and (
        rs.stripe_subscription_status = 'active'
        or (
          rs.current_period_end is not null and rs.current_period_end > now()
          and (rs.ended_at is null or rs.ended_at > now())
        )
      )
  );
$$;

create or replace function public.upsert_restaurant(
  p_restaurant_id uuid,
  p_slug text default null,
  p_name text default null
) returns table (restaurant_id uuid)
language plpgsql security definer set search_path = public as $$
begin
  insert into public.restaurants (id, slug, name, system_prompt)
  values (
    p_restaurant_id,
    coalesce(nullif(trim(p_slug), ''), 'restaurant-' || left(p_restaurant_id::text, 8)),
    coalesce(nullif(trim(p_name), ''), 'Restaurant'),
    'You are a helpful restaurant assistant. Answer only from the supplied restaurant context.'
  )
  on conflict (id) do update set
    slug = coalesce(nullif(trim(p_slug), ''), public.restaurants.slug),
    name = coalesce(nullif(trim(p_name), ''), public.restaurants.name)
  returning id into restaurant_id;
  return next;
end;
$$;

create or replace function public.upsert_session(
  p_restaurant_id uuid,
  p_session_token text,
  p_client_meta jsonb default '{}'::jsonb,
  p_language text default null
) returns table (session_id uuid, language text)
language plpgsql security definer set search_path = public as $$
begin
  return query
  insert into public.chat_sessions (restaurant_id, session_token, client_meta, language)
  values (p_restaurant_id, p_session_token, coalesce(p_client_meta, '{}'::jsonb), coalesce(nullif(lower(trim(p_language)), ''), 'eng'))
  on conflict (restaurant_id, session_token) do update set
    client_meta = excluded.client_meta,
    last_activity_at = now(),
    language = coalesce(nullif(lower(trim(p_language)), ''), public.chat_sessions.language)
  returning id, public.chat_sessions.language;
end;
$$;

create or replace function public.chat_session_bootstrap(
  p_restaurant_id uuid,
  p_session_token text,
  p_client_meta jsonb default '{}'::jsonb,
  p_language text default null
) returns table (
  session_id uuid,
  language text,
  last_response_id text,
  last_discussed_item_ids jsonb,
  last_candidate_item_ids jsonb,
  last_intent text,
  active_constraints jsonb
)
language plpgsql security definer set search_path = public as $$
declare
  v_session_id uuid;
  v_language text;
begin
  select s.session_id, s.language into v_session_id, v_language
  from public.upsert_session(p_restaurant_id, p_session_token, p_client_meta, p_language) s;

  insert into public.chat_session_state (session_id) values (v_session_id)
  on conflict (session_id) do nothing;

  return query select
    v_session_id,
    v_language,
    css.last_response_id,
    css.last_discussed_item_ids,
    css.last_candidate_item_ids,
    css.last_intent,
    css.active_constraints
  from public.chat_session_state css where css.session_id = v_session_id;
end;
$$;

create or replace function public.chat_access_context(
  p_restaurant_id uuid,
  p_origin text,
  p_origin_preallowed boolean default false
) returns table (
  restaurant_exists boolean,
  subscription_active boolean,
  origin_allowed boolean,
  ip_max_requests integer,
  ip_window_seconds integer,
  session_max_requests integer,
  session_window_seconds integer,
  token_max_age_seconds integer,
  token_issue_max_requests integer,
  token_issue_window_seconds integer,
  system_prompt text
)
language sql stable security definer set search_path = public as $$
  select
    r.id is not null,
    public.restaurant_has_active_subscription(p_restaurant_id),
    coalesce(p_origin_preallowed, false) or exists (
      select 1 from public.restaurant_allowed_origins o
      where o.restaurant_id = p_restaurant_id and o.origin = lower(rtrim(trim(p_origin), '/'))
    ),
    coalesce(s.ip_max_requests, 30),
    coalesce(s.ip_window_seconds, 60),
    coalesce(s.session_max_requests, 45),
    coalesce(s.session_window_seconds, 60),
    coalesce(s.token_max_age_seconds, 900),
    coalesce(s.token_issue_max_requests, 30),
    coalesce(s.token_issue_window_seconds, 60),
    r.system_prompt
  from (select 1) seed
  left join public.restaurants r on r.id = p_restaurant_id
  left join public.restaurant_security_settings s on s.restaurant_id = r.id;
$$;

create or replace function public.match_chunks(
  p_restaurant_id uuid,
  query_embedding extensions.vector(1536),
  match_count integer default 8,
  min_score double precision default 0
) returns table (
  id uuid,
  text text,
  type text,
  source_url text,
  page_path text,
  title text,
  image_url text,
  extra_metadata jsonb,
  score double precision
)
language sql stable security definer set search_path = public, extensions as $$
  select
    kc.id, kc.text, kc.type, kc.source_url, kc.page_path, kc.title, kc.image_url, kc.extra_metadata,
    (1 - (kc.embedding <=> query_embedding))::double precision as score
  from public.knowledge_chunks kc
  where kc.restaurant_id = p_restaurant_id
    and (1 - (kc.embedding <=> query_embedding)) >= min_score
  order by kc.embedding <=> query_embedding
  limit greatest(1, least(match_count, 50));
$$;

create or replace function public.finalize_ingest_run(
  p_run_id uuid,
  p_prune boolean default false
) returns table (activated_chunks integer, pruned_chunks integer)
language plpgsql security definer set search_path = public, extensions as $$
declare
  v_restaurant_id uuid;
  v_activated integer := 0;
  v_pruned integer := 0;
begin
  select restaurant_id into v_restaurant_id
  from public.ingest_runs where id = p_run_id and status = 'running' for update;
  if v_restaurant_id is null then
    raise exception 'ingest run is missing or is not running';
  end if;
  if not exists (select 1 from public.knowledge_chunk_staging where run_id = p_run_id) then
    raise exception 'ingest run has no staged chunks';
  end if;
  if exists (
    select 1 from public.knowledge_chunk_staging
    where run_id = p_run_id and restaurant_id <> v_restaurant_id
  ) then
    raise exception 'staged chunks do not belong to the ingest run restaurant';
  end if;
  if exists (
    select 1
    from public.knowledge_chunk_staging s
    left join public.knowledge_chunks k
      on k.restaurant_id = s.restaurant_id and k.source_key = s.source_key and k.external_id = s.external_id
    where s.run_id = p_run_id
      and (k.id is null or k.content_hash <> s.content_hash or k.embedding_model <> s.embedding_model)
      and s.embedding is null
  ) then
    raise exception 'new or changed staged chunks are missing embeddings';
  end if;

  insert into public.knowledge_chunks (
    id, restaurant_id, source_key, external_id, text, type, source_url, page_path, title,
    meta_description, image_url, extra_metadata, content_hash, embedding_model, embedding, last_ingest_run_id
  )
  select
    s.id, s.restaurant_id, s.source_key, s.external_id, s.text, s.type, s.source_url, s.page_path, s.title,
    s.meta_description, s.image_url, s.extra_metadata, s.content_hash, s.embedding_model,
    coalesce(s.embedding, k.embedding), p_run_id
  from public.knowledge_chunk_staging s
  left join public.knowledge_chunks k
    on k.restaurant_id = s.restaurant_id and k.source_key = s.source_key and k.external_id = s.external_id
  where s.run_id = p_run_id
  on conflict (restaurant_id, source_key, external_id) do update set
    id = excluded.id,
    text = excluded.text,
    type = excluded.type,
    source_url = excluded.source_url,
    page_path = excluded.page_path,
    title = excluded.title,
    meta_description = excluded.meta_description,
    image_url = excluded.image_url,
    extra_metadata = excluded.extra_metadata,
    content_hash = excluded.content_hash,
    embedding_model = excluded.embedding_model,
    embedding = excluded.embedding,
    last_ingest_run_id = excluded.last_ingest_run_id;
  get diagnostics v_activated = row_count;

  if p_prune then
    delete from public.knowledge_chunks k
    where k.restaurant_id = v_restaurant_id
      and k.source_key in (select distinct source_key from public.knowledge_chunk_staging where run_id = p_run_id)
      and not exists (
        select 1 from public.knowledge_chunk_staging s
        where s.run_id = p_run_id and s.source_key = k.source_key and s.external_id = k.external_id
      );
    get diagnostics v_pruned = row_count;
  end if;

  update public.ingest_runs set
    status = 'success', activated_chunks = v_activated, pruned_chunks = v_pruned, error_message = null
  where id = p_run_id;
  delete from public.knowledge_chunk_staging where run_id = p_run_id;
  return query select v_activated, v_pruned;
exception when others then
  update public.ingest_runs set status = 'error', error_message = left(sqlerrm, 1000) where id = p_run_id;
  raise;
end;
$$;

alter table public.restaurants enable row level security;
alter table public.restaurant_allowed_origins enable row level security;
alter table public.restaurant_security_settings enable row level security;
alter table public.restaurant_subscriptions enable row level security;
alter table public.stripe_webhook_events enable row level security;
alter table public.audit_events enable row level security;
alter table public.chat_sessions enable row level security;
alter table public.chat_session_state enable row level security;
alter table public.chat_messages enable row level security;
alter table public.ingest_runs enable row level security;
alter table public.knowledge_chunks enable row level security;
alter table public.knowledge_chunk_staging enable row level security;

create policy service_role_all on public.restaurants to service_role using (true) with check (true);
create policy service_role_all on public.restaurant_allowed_origins to service_role using (true) with check (true);
create policy service_role_all on public.restaurant_security_settings to service_role using (true) with check (true);
create policy service_role_all on public.restaurant_subscriptions to service_role using (true) with check (true);
create policy service_role_all on public.stripe_webhook_events to service_role using (true) with check (true);
create policy service_role_all on public.audit_events to service_role using (true) with check (true);
create policy service_role_all on public.chat_sessions to service_role using (true) with check (true);
create policy service_role_all on public.chat_session_state to service_role using (true) with check (true);
create policy service_role_all on public.chat_messages to service_role using (true) with check (true);
create policy service_role_all on public.ingest_runs to service_role using (true) with check (true);
create policy service_role_all on public.knowledge_chunks to service_role using (true) with check (true);
create policy service_role_all on public.knowledge_chunk_staging to service_role using (true) with check (true);

revoke all on all tables in schema public from anon, authenticated;
revoke execute on function public.touch_updated_at() from public, anon, authenticated;
revoke execute on function public.restaurant_has_active_subscription(uuid) from public, anon, authenticated;
revoke execute on function public.upsert_restaurant(uuid, text, text) from public, anon, authenticated;
revoke execute on function public.upsert_session(uuid, text, jsonb, text) from public, anon, authenticated;
revoke execute on function public.chat_session_bootstrap(uuid, text, jsonb, text) from public, anon, authenticated;
revoke execute on function public.chat_access_context(uuid, text, boolean) from public, anon, authenticated;
revoke execute on function public.match_chunks(uuid, extensions.vector, integer, double precision) from public, anon, authenticated;
revoke execute on function public.finalize_ingest_run(uuid, boolean) from public, anon, authenticated;
grant usage on schema public to service_role;
grant all on all tables in schema public to service_role;
grant all on all sequences in schema public to service_role;
grant execute on function public.restaurant_has_active_subscription(uuid) to service_role;
grant execute on function public.upsert_restaurant(uuid, text, text) to service_role;
grant execute on function public.upsert_session(uuid, text, jsonb, text) to service_role;
grant execute on function public.chat_session_bootstrap(uuid, text, jsonb, text) to service_role;
grant execute on function public.chat_access_context(uuid, text, boolean) to service_role;
grant execute on function public.match_chunks(uuid, extensions.vector, integer, double precision) to service_role;
grant execute on function public.finalize_ingest_run(uuid, boolean) to service_role;
