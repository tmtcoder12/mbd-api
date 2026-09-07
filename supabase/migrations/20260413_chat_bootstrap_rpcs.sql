CREATE OR REPLACE FUNCTION public.chat_access_context(
  p_restaurant_id uuid,
  p_origin text,
  p_origin_preallowed boolean DEFAULT false
)
RETURNS TABLE (
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
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $$
declare
  v_origin text := regexp_replace(lower(trim(coalesce(p_origin, ''))), '/+$', '');
begin
  return query
  select
    (r.id is not null) as restaurant_exists,
    case
      when r.id is null then false
      else public.restaurant_has_active_subscription(p_restaurant_id)
    end as subscription_active,
    case
      when coalesce(p_origin_preallowed, false) then true
      when r.id is null or v_origin = '' then false
      else exists (
        select 1
        from public.restaurant_allowed_origins rao
        where rao.restaurant_id = p_restaurant_id
          and rao.origin = v_origin
        limit 1
      )
    end as origin_allowed,
    coalesce(rss.ip_max_requests, 30) as ip_max_requests,
    coalesce(rss.ip_window_seconds, 60) as ip_window_seconds,
    coalesce(rss.session_max_requests, 45) as session_max_requests,
    coalesce(rss.session_window_seconds, 60) as session_window_seconds,
    coalesce(rss.token_max_age_seconds, 900) as token_max_age_seconds,
    coalesce(rss.token_issue_max_requests, 30) as token_issue_max_requests,
    coalesce(rss.token_issue_window_seconds, 60) as token_issue_window_seconds,
    r.system_prompt
  from (select 1) seed
  left join public.restaurants r on r.id = p_restaurant_id
  left join public.restaurant_security_settings rss on rss.restaurant_id = p_restaurant_id;
end;
$$;

ALTER FUNCTION public.chat_access_context(uuid, text, boolean) OWNER TO postgres;

GRANT ALL ON FUNCTION public.chat_access_context(uuid, text, boolean) TO anon;
GRANT ALL ON FUNCTION public.chat_access_context(uuid, text, boolean) TO authenticated;
GRANT ALL ON FUNCTION public.chat_access_context(uuid, text, boolean) TO service_role;


CREATE OR REPLACE FUNCTION public.chat_session_bootstrap(
  p_restaurant_id uuid,
  p_session_token text,
  p_client_meta jsonb DEFAULT '{}'::jsonb,
  p_language text DEFAULT NULL::text
)
RETURNS TABLE (
  session_id uuid,
  language text,
  last_response_id text,
  last_discussed_item_ids jsonb,
  last_candidate_item_ids jsonb,
  last_intent text,
  active_constraints jsonb
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $$
declare
  v_session_id uuid;
  v_language text;
begin
  insert into public.chat_sessions (
    restaurant_id,
    session_token,
    client_meta,
    last_activity_at,
    language
  )
  values (
    p_restaurant_id,
    p_session_token,
    coalesce(p_client_meta, '{}'::jsonb),
    now(),
    coalesce(nullif(lower(trim(p_language)), ''), 'eng')
  )
  on conflict (restaurant_id, session_token) do update
  set
    client_meta = coalesce(excluded.client_meta, public.chat_sessions.client_meta),
    last_activity_at = now(),
    language = case
      when nullif(lower(trim(p_language)), '') is not null then lower(trim(p_language))
      when nullif(lower(trim(public.chat_sessions.language)), '') ~ '^[a-z]{3}$' then lower(trim(public.chat_sessions.language))
      else 'eng'
    end
  returning public.chat_sessions.id, public.chat_sessions.language
  into v_session_id, v_language;

  return query
  select
    v_session_id as session_id,
    v_language as language,
    css.last_response_id,
    coalesce(css.last_discussed_item_ids, '[]'::jsonb) as last_discussed_item_ids,
    coalesce(css.last_candidate_item_ids, '[]'::jsonb) as last_candidate_item_ids,
    css.last_intent,
    coalesce(css.active_constraints, '{}'::jsonb) as active_constraints
  from (select 1) seed
  left join public.chat_session_state css on css.session_id = v_session_id;
end;
$$;

ALTER FUNCTION public.chat_session_bootstrap(uuid, text, jsonb, text) OWNER TO postgres;

GRANT ALL ON FUNCTION public.chat_session_bootstrap(uuid, text, jsonb, text) TO anon;
GRANT ALL ON FUNCTION public.chat_session_bootstrap(uuid, text, jsonb, text) TO authenticated;
GRANT ALL ON FUNCTION public.chat_session_bootstrap(uuid, text, jsonb, text) TO service_role;
