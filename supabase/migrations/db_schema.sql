


SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;


COMMENT ON SCHEMA "public" IS 'standard public schema';



CREATE EXTENSION IF NOT EXISTS "pg_graphql" WITH SCHEMA "graphql";






CREATE EXTENSION IF NOT EXISTS "pg_stat_statements" WITH SCHEMA "extensions";






CREATE EXTENSION IF NOT EXISTS "pgcrypto" WITH SCHEMA "extensions";






CREATE EXTENSION IF NOT EXISTS "supabase_vault" WITH SCHEMA "vault";






CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA "extensions";






CREATE EXTENSION IF NOT EXISTS "vector" WITH SCHEMA "public";






CREATE OR REPLACE FUNCTION "public"."delete_knowledge_chunk"("p_chunk_id" "text", "p_reason" "text" DEFAULT NULL::"text") RETURNS "void"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
declare
  v_actor uuid;
  v_chunk public.knowledge_chunks%rowtype;
  v_image_url text;
  v_image_path text;
  v_deleted_storage_rows int;
begin
  if nullif(trim(coalesce(p_chunk_id, '')), '') is null then
    raise exception 'p_chunk_id is required';
  end if;

  v_actor := auth.uid();
  if v_actor is null then
    raise exception 'unauthorized';
  end if;

  select *
  into v_chunk
  from public.knowledge_chunks kc
  where kc.id = p_chunk_id::uuid;

  if not found then
    raise exception 'knowledge chunk not found';
  end if;

  if not public.user_has_restaurant_role(v_chunk.restaurant_id, array['owner', 'manager']) then
    raise exception 'forbidden';
  end if;

  insert into public.deleted_knowledge_chunks (
    original_chunk_id,
    restaurant_id,
    deleted_by,
    deleted_at,
    delete_reason,
    text,
    type,
    source_url,
    page_path,
    title,
    meta_description,
    image_url,
    extra_metadata,
    content_hash,
    embedding
  )
  values (
    v_chunk.id::text,
    v_chunk.restaurant_id,
    v_actor,
    now(),
    nullif(trim(coalesce(p_reason, '')), ''),
    v_chunk.text,
    v_chunk.type,
    v_chunk.source_url,
    v_chunk.page_path,
    v_chunk.title,
    v_chunk.meta_description,
    v_chunk.image_url,
    coalesce(v_chunk.extra_metadata, '{}'::jsonb),
    v_chunk.content_hash,
    v_chunk.embedding
  );

  delete from public.knowledge_chunks
  where id = v_chunk.id;
end;
$$;


ALTER FUNCTION "public"."delete_knowledge_chunk"("p_chunk_id" "text", "p_reason" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."handle_new_user"() RETURNS "trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
begin
  insert into public.profiles (id, email)
  values (new.id, new.email)
  on conflict (id) do update
    set email = excluded.email;
  return new;
end;
$$;


ALTER FUNCTION "public"."handle_new_user"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."match_chunks"("p_restaurant_id" "uuid", "query_embedding" "public"."vector", "match_count" integer DEFAULT 8, "min_score" double precision DEFAULT 0) RETURNS TABLE("id" "uuid", "text" "text", "type" "text", "source_url" "text", "page_path" "text", "title" "text", "score" double precision)
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
  select
    kc.id,
    kc.text,
    kc.type,
    kc.source_url,
    kc.page_path,
    kc.title,
    (1 - (kc.embedding <=> query_embedding))::float as score
  from public.knowledge_chunks kc
  where kc.restaurant_id = p_restaurant_id
    and (1 - (kc.embedding <=> query_embedding)) >= coalesce(min_score, 0)
  order by kc.embedding <=> query_embedding
  limit greatest(match_count, 1);
$$;


ALTER FUNCTION "public"."match_chunks"("p_restaurant_id" "uuid", "query_embedding" "public"."vector", "match_count" integer, "min_score" double precision) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."normalize_origin_text"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $_$
begin
  new.origin := lower(trim(new.origin));
  new.origin := regexp_replace(new.origin, '/+$', '');
  if new.origin !~ '^https?://[a-z0-9.-]+(:[0-9]{1,5})?$' then
    raise exception 'invalid origin format';
  end if;
  return new;
end;
$_$;


ALTER FUNCTION "public"."normalize_origin_text"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."touch_last_activity"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
begin
  update public.chat_sessions
    set last_activity_at = now()
    where id = new.session_id;
  return new;
end;
$$;


ALTER FUNCTION "public"."touch_last_activity"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."touch_updated_at"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
begin
  new.updated_at = now();
  return new;
end;
$$;


ALTER FUNCTION "public"."touch_updated_at"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."upsert_restaurant"("p_restaurant_id" "uuid", "p_slug" "text" DEFAULT NULL::"text", "p_name" "text" DEFAULT NULL::"text") RETURNS TABLE("restaurant_id" "uuid")
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
declare
  v_id uuid;
begin
  if p_restaurant_id is null then
    raise exception 'p_restaurant_id is required';
  end if;

  insert into public.restaurants (id, slug, name)
  values (p_restaurant_id, nullif(trim(p_slug), ''), nullif(trim(p_name), ''))
  on conflict (id)
  do update set
    slug = coalesce(excluded.slug, public.restaurants.slug),
    name = coalesce(excluded.name, public.restaurants.name)
  returning id into v_id;

  return query select v_id;
end;
$$;


ALTER FUNCTION "public"."upsert_restaurant"("p_restaurant_id" "uuid", "p_slug" "text", "p_name" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."upsert_session"("p_restaurant_id" "uuid", "p_session_token" "text", "p_client_meta" "jsonb" DEFAULT '{}'::"jsonb") RETURNS TABLE("session_id" "uuid")
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
declare
  v_id uuid;
begin
  if p_restaurant_id is null then
    raise exception 'p_restaurant_id is required';
  end if;

  insert into public.chat_sessions(restaurant_id, session_token, client_meta)
  values (p_restaurant_id, p_session_token, coalesce(p_client_meta, '{}'::jsonb))
  on conflict (restaurant_id, session_token)
  do update set
    client_meta = public.chat_sessions.client_meta || excluded.client_meta,
    last_activity_at = now()
  returning id into v_id;

  return query select v_id;
end;
$$;


ALTER FUNCTION "public"."upsert_session"("p_restaurant_id" "uuid", "p_session_token" "text", "p_client_meta" "jsonb") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."upsert_session"("p_restaurant_id" "uuid", "p_session_token" "text", "p_client_meta" "jsonb" DEFAULT '{}'::"jsonb", "p_language" "text" DEFAULT NULL::"text") RETURNS TABLE("session_id" "uuid", "language" "text")
    LANGUAGE "plpgsql"
    AS $_$
begin
  return query
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
  returning public.chat_sessions.id, public.chat_sessions.language;
end;
$_$;


ALTER FUNCTION "public"."upsert_session"("p_restaurant_id" "uuid", "p_session_token" "text", "p_client_meta" "jsonb", "p_language" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."user_has_restaurant_role"("p_restaurant_id" "uuid", "p_roles" "text"[]) RETURNS boolean
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
  select exists (
    select 1
    from public.restaurant_users ru
    where ru.restaurant_id = p_restaurant_id
      and ru.user_id = auth.uid()
      and ru.role = any(p_roles)
  );
$$;


ALTER FUNCTION "public"."user_has_restaurant_role"("p_restaurant_id" "uuid", "p_roles" "text"[]) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."user_manages_restaurant"("p_restaurant_id" "uuid") RETURNS boolean
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
  select public.user_has_restaurant_role(p_restaurant_id, array['owner', 'manager']);
$$;


ALTER FUNCTION "public"."user_manages_restaurant"("p_restaurant_id" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."user_owns_restaurant"("p_restaurant_id" "uuid") RETURNS boolean
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
  select public.user_has_restaurant_role(p_restaurant_id, array['owner']);
$$;


ALTER FUNCTION "public"."user_owns_restaurant"("p_restaurant_id" "uuid") OWNER TO "postgres";

SET default_tablespace = '';

SET default_table_access_method = "heap";


CREATE TABLE IF NOT EXISTS "public"."audit_events" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "restaurant_id" "uuid",
    "event_type" "text" NOT NULL,
    "actor" "text",
    "details" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."audit_events" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."chat_messages" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "session_id" "uuid" NOT NULL,
    "role" "text" NOT NULL,
    "content" "text" NOT NULL,
    "sources" "jsonb",
    "latency_ms" integer,
    "delivery_status" "text" DEFAULT 'complete'::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "query_type" "text",
    CONSTRAINT "chat_messages_delivery_status_check" CHECK (("delivery_status" = ANY (ARRAY['complete'::"text", 'error'::"text"]))),
    CONSTRAINT "chat_messages_role_check" CHECK (("role" = ANY (ARRAY['user'::"text", 'assistant'::"text", 'system'::"text"])))
);


ALTER TABLE "public"."chat_messages" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."chat_session_state" (
    "session_id" "uuid" NOT NULL,
    "last_response_id" "text",
    "last_discussed_item_ids" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "last_candidate_item_ids" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "last_intent" "text",
    "active_constraints" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."chat_session_state" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."chat_sessions" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "restaurant_id" "uuid" NOT NULL,
    "session_token" "text" NOT NULL,
    "client_meta" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "last_activity_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "language" "text"
);


ALTER TABLE "public"."chat_sessions" OWNER TO "postgres";


COMMENT ON COLUMN "public"."chat_sessions"."language" IS 'language of the user session';



CREATE TABLE IF NOT EXISTS "public"."deleted_knowledge_chunks" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "original_chunk_id" "text" NOT NULL,
    "restaurant_id" "uuid" NOT NULL,
    "deleted_by" "uuid" NOT NULL,
    "deleted_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "delete_reason" "text",
    "text" "text" NOT NULL,
    "type" "text",
    "source_url" "text",
    "page_path" "text",
    "title" "text",
    "meta_description" "text",
    "extra_metadata" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "content_hash" "text",
    "embedding" "public"."vector"(1536) NOT NULL,
    "image_url" "text"
);


ALTER TABLE "public"."deleted_knowledge_chunks" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."ingest_runs" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "restaurant_id" "uuid" NOT NULL,
    "model" "text" NOT NULL,
    "source_name" "text" NOT NULL,
    "total_chunks" integer NOT NULL,
    "embedded_chunks" integer DEFAULT 0 NOT NULL,
    "status" "text" NOT NULL,
    "error_message" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "ingest_runs_status_check" CHECK (("status" = ANY (ARRAY['running'::"text", 'success'::"text", 'error'::"text"])))
);


ALTER TABLE "public"."ingest_runs" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."knowledge_chunks" (
    "id" "uuid" NOT NULL,
    "restaurant_id" "uuid" NOT NULL,
    "text" "text" NOT NULL,
    "type" "text",
    "source_url" "text",
    "page_path" "text",
    "title" "text",
    "meta_description" "text",
    "extra_metadata" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "content_hash" "text",
    "embedding" "public"."vector"(1536) NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "image_url" "text"
);


ALTER TABLE "public"."knowledge_chunks" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."profiles" (
    "id" "uuid" NOT NULL,
    "email" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."profiles" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."restaurant_allowed_origins" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "restaurant_id" "uuid" NOT NULL,
    "origin" "text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."restaurant_allowed_origins" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."restaurant_security_settings" (
    "restaurant_id" "uuid" NOT NULL,
    "ip_max_requests" integer DEFAULT 30 NOT NULL,
    "ip_window_seconds" integer DEFAULT 60 NOT NULL,
    "session_max_requests" integer DEFAULT 45 NOT NULL,
    "session_window_seconds" integer DEFAULT 60 NOT NULL,
    "token_max_age_seconds" integer DEFAULT 900 NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "token_issue_max_requests" integer DEFAULT 30 NOT NULL,
    "token_issue_window_seconds" integer DEFAULT 60 NOT NULL,
    CONSTRAINT "restaurant_security_settings_ip_max_requests_check" CHECK (("ip_max_requests" > 0)),
    CONSTRAINT "restaurant_security_settings_ip_window_seconds_check" CHECK (("ip_window_seconds" > 0)),
    CONSTRAINT "restaurant_security_settings_session_max_requests_check" CHECK (("session_max_requests" > 0)),
    CONSTRAINT "restaurant_security_settings_session_window_seconds_check" CHECK (("session_window_seconds" > 0)),
    CONSTRAINT "restaurant_security_settings_token_issue_max_requests_check" CHECK (("token_issue_max_requests" > 0)),
    CONSTRAINT "restaurant_security_settings_token_issue_window_seconds_check" CHECK (("token_issue_window_seconds" > 0)),
    CONSTRAINT "restaurant_security_settings_token_max_age_seconds_check" CHECK ((("token_max_age_seconds" >= 60) AND ("token_max_age_seconds" <= 7200)))
);


ALTER TABLE "public"."restaurant_security_settings" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."restaurant_users" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "restaurant_id" "uuid" NOT NULL,
    "user_id" "uuid" NOT NULL,
    "role" "text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "restaurant_users_role_check" CHECK (("role" = ANY (ARRAY['owner'::"text", 'manager'::"text", 'staff'::"text"])))
);


ALTER TABLE "public"."restaurant_users" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."restaurants" (
    "id" "uuid" NOT NULL,
    "slug" "text",
    "name" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "system_prompt" "text" DEFAULT 'You are a helpful restaurant website assistant. Answer using only the provided restaurant knowledge base.'::"text"
);


ALTER TABLE "public"."restaurants" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."user_auth_events" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "uuid" NOT NULL,
    "event_type" "text" NOT NULL,
    "client_info" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "user_auth_events_event_type_check" CHECK (("event_type" = ANY (ARRAY['login'::"text", 'logout'::"text"])))
);


ALTER TABLE "public"."user_auth_events" OWNER TO "postgres";


ALTER TABLE ONLY "public"."audit_events"
    ADD CONSTRAINT "audit_events_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."chat_messages"
    ADD CONSTRAINT "chat_messages_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."chat_session_state"
    ADD CONSTRAINT "chat_session_state_pkey" PRIMARY KEY ("session_id");



ALTER TABLE ONLY "public"."chat_sessions"
    ADD CONSTRAINT "chat_sessions_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."chat_sessions"
    ADD CONSTRAINT "chat_sessions_restaurant_id_session_token_key" UNIQUE ("restaurant_id", "session_token");



ALTER TABLE ONLY "public"."deleted_knowledge_chunks"
    ADD CONSTRAINT "deleted_knowledge_chunks_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."ingest_runs"
    ADD CONSTRAINT "ingest_runs_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."knowledge_chunks"
    ADD CONSTRAINT "knowledge_chunks_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."profiles"
    ADD CONSTRAINT "profiles_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."restaurant_allowed_origins"
    ADD CONSTRAINT "restaurant_allowed_origins_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."restaurant_allowed_origins"
    ADD CONSTRAINT "restaurant_allowed_origins_restaurant_id_origin_key" UNIQUE ("restaurant_id", "origin");



ALTER TABLE ONLY "public"."restaurant_security_settings"
    ADD CONSTRAINT "restaurant_security_settings_pkey" PRIMARY KEY ("restaurant_id");



ALTER TABLE ONLY "public"."restaurant_users"
    ADD CONSTRAINT "restaurant_users_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."restaurant_users"
    ADD CONSTRAINT "restaurant_users_restaurant_id_user_id_key" UNIQUE ("restaurant_id", "user_id");



ALTER TABLE ONLY "public"."restaurants"
    ADD CONSTRAINT "restaurants_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."restaurants"
    ADD CONSTRAINT "restaurants_slug_key" UNIQUE ("slug");



ALTER TABLE ONLY "public"."user_auth_events"
    ADD CONSTRAINT "user_auth_events_pkey" PRIMARY KEY ("id");



CREATE INDEX "audit_events_restaurant_id_created_at_idx" ON "public"."audit_events" USING "btree" ("restaurant_id", "created_at" DESC);



CREATE INDEX "chat_messages_session_id_created_at_idx" ON "public"."chat_messages" USING "btree" ("session_id", "created_at" DESC);



CREATE INDEX "chat_session_state_last_intent_idx" ON "public"."chat_session_state" USING "btree" ("last_intent");



CREATE INDEX "chat_session_state_updated_at_idx" ON "public"."chat_session_state" USING "btree" ("updated_at" DESC);



CREATE INDEX "chat_sessions_restaurant_last_activity_idx" ON "public"."chat_sessions" USING "btree" ("restaurant_id", "last_activity_at" DESC);



CREATE INDEX "deleted_knowledge_chunks_deleted_by_idx" ON "public"."deleted_knowledge_chunks" USING "btree" ("deleted_by");



CREATE INDEX "deleted_knowledge_chunks_original_chunk_id_idx" ON "public"."deleted_knowledge_chunks" USING "btree" ("original_chunk_id");



CREATE INDEX "deleted_knowledge_chunks_restaurant_deleted_at_idx" ON "public"."deleted_knowledge_chunks" USING "btree" ("restaurant_id", "deleted_at" DESC);



CREATE INDEX "ingest_runs_restaurant_created_at_idx" ON "public"."ingest_runs" USING "btree" ("restaurant_id", "created_at" DESC);



CREATE INDEX "knowledge_chunks_embedding_ivfflat_idx" ON "public"."knowledge_chunks" USING "ivfflat" ("embedding" "public"."vector_cosine_ops") WITH ("lists"='100');



CREATE INDEX "knowledge_chunks_restaurant_created_at_idx" ON "public"."knowledge_chunks" USING "btree" ("restaurant_id", "created_at" DESC);



CREATE INDEX "knowledge_chunks_restaurant_idx" ON "public"."knowledge_chunks" USING "btree" ("restaurant_id");



CREATE INDEX "knowledge_chunks_source_url_idx" ON "public"."knowledge_chunks" USING "btree" ("source_url");



CREATE INDEX "profiles_email_idx" ON "public"."profiles" USING "btree" ("email");



CREATE INDEX "restaurant_allowed_origins_restaurant_id_origin_idx" ON "public"."restaurant_allowed_origins" USING "btree" ("restaurant_id", "origin");



CREATE INDEX "restaurant_users_restaurant_id_idx" ON "public"."restaurant_users" USING "btree" ("restaurant_id");



CREATE INDEX "restaurant_users_restaurant_role_idx" ON "public"."restaurant_users" USING "btree" ("restaurant_id", "role");



CREATE INDEX "restaurant_users_user_id_idx" ON "public"."restaurant_users" USING "btree" ("user_id");



CREATE INDEX "user_auth_events_user_id_idx" ON "public"."user_auth_events" USING "btree" ("user_id", "created_at" DESC);



CREATE OR REPLACE TRIGGER "trg_normalize_origin_text" BEFORE INSERT OR UPDATE ON "public"."restaurant_allowed_origins" FOR EACH ROW EXECUTE FUNCTION "public"."normalize_origin_text"();



CREATE OR REPLACE TRIGGER "trg_touch_chat_session_state_updated_at" BEFORE UPDATE ON "public"."chat_session_state" FOR EACH ROW EXECUTE FUNCTION "public"."touch_updated_at"();



CREATE OR REPLACE TRIGGER "trg_touch_ingest_updated_at" BEFORE UPDATE ON "public"."ingest_runs" FOR EACH ROW EXECUTE FUNCTION "public"."touch_updated_at"();



CREATE OR REPLACE TRIGGER "trg_touch_last_activity" AFTER INSERT ON "public"."chat_messages" FOR EACH ROW EXECUTE FUNCTION "public"."touch_last_activity"();



CREATE OR REPLACE TRIGGER "trg_touch_restaurant_security_settings_updated_at" BEFORE UPDATE ON "public"."restaurant_security_settings" FOR EACH ROW EXECUTE FUNCTION "public"."touch_updated_at"();



ALTER TABLE ONLY "public"."audit_events"
    ADD CONSTRAINT "audit_events_restaurant_id_fkey" FOREIGN KEY ("restaurant_id") REFERENCES "public"."restaurants"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."chat_messages"
    ADD CONSTRAINT "chat_messages_session_id_fkey" FOREIGN KEY ("session_id") REFERENCES "public"."chat_sessions"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."chat_session_state"
    ADD CONSTRAINT "chat_session_state_session_id_fkey" FOREIGN KEY ("session_id") REFERENCES "public"."chat_sessions"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."chat_sessions"
    ADD CONSTRAINT "chat_sessions_restaurant_id_fkey" FOREIGN KEY ("restaurant_id") REFERENCES "public"."restaurants"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."deleted_knowledge_chunks"
    ADD CONSTRAINT "deleted_knowledge_chunks_deleted_by_fkey" FOREIGN KEY ("deleted_by") REFERENCES "public"."profiles"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."deleted_knowledge_chunks"
    ADD CONSTRAINT "deleted_knowledge_chunks_restaurant_id_fkey" FOREIGN KEY ("restaurant_id") REFERENCES "public"."restaurants"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."ingest_runs"
    ADD CONSTRAINT "ingest_runs_restaurant_id_fkey" FOREIGN KEY ("restaurant_id") REFERENCES "public"."restaurants"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."knowledge_chunks"
    ADD CONSTRAINT "knowledge_chunks_restaurant_id_fkey" FOREIGN KEY ("restaurant_id") REFERENCES "public"."restaurants"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."profiles"
    ADD CONSTRAINT "profiles_id_fkey" FOREIGN KEY ("id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."restaurant_allowed_origins"
    ADD CONSTRAINT "restaurant_allowed_origins_restaurant_id_fkey" FOREIGN KEY ("restaurant_id") REFERENCES "public"."restaurants"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."restaurant_security_settings"
    ADD CONSTRAINT "restaurant_security_settings_restaurant_id_fkey" FOREIGN KEY ("restaurant_id") REFERENCES "public"."restaurants"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."restaurant_users"
    ADD CONSTRAINT "restaurant_users_restaurant_id_fkey" FOREIGN KEY ("restaurant_id") REFERENCES "public"."restaurants"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."restaurant_users"
    ADD CONSTRAINT "restaurant_users_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "public"."profiles"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."user_auth_events"
    ADD CONSTRAINT "user_auth_events_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "public"."profiles"("id") ON DELETE CASCADE;



ALTER TABLE "public"."audit_events" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "audit_events_select_manager" ON "public"."audit_events" FOR SELECT TO "authenticated" USING ((("restaurant_id" IS NOT NULL) AND "public"."user_has_restaurant_role"("restaurant_id", ARRAY['owner'::"text", 'manager'::"text"])));



ALTER TABLE "public"."chat_messages" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "chat_messages_select_manager" ON "public"."chat_messages" FOR SELECT TO "authenticated" USING ((EXISTS ( SELECT 1
   FROM "public"."chat_sessions" "cs"
  WHERE (("cs"."id" = "chat_messages"."session_id") AND "public"."user_has_restaurant_role"("cs"."restaurant_id", ARRAY['owner'::"text", 'manager'::"text"])))));



ALTER TABLE "public"."chat_session_state" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "chat_session_state_select_manager" ON "public"."chat_session_state" FOR SELECT TO "authenticated" USING ((EXISTS ( SELECT 1
   FROM "public"."chat_sessions" "cs"
  WHERE (("cs"."id" = "chat_session_state"."session_id") AND "public"."user_has_restaurant_role"("cs"."restaurant_id", ARRAY['owner'::"text", 'manager'::"text"])))));



ALTER TABLE "public"."chat_sessions" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "chat_sessions_select_manager" ON "public"."chat_sessions" FOR SELECT TO "authenticated" USING ("public"."user_has_restaurant_role"("restaurant_id", ARRAY['owner'::"text", 'manager'::"text"]));



ALTER TABLE "public"."deleted_knowledge_chunks" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "deleted_knowledge_chunks_select_member" ON "public"."deleted_knowledge_chunks" FOR SELECT TO "authenticated" USING ("public"."user_has_restaurant_role"("restaurant_id", ARRAY['owner'::"text", 'manager'::"text", 'staff'::"text"]));



ALTER TABLE "public"."ingest_runs" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "ingest_runs_insert_manager" ON "public"."ingest_runs" FOR INSERT TO "authenticated" WITH CHECK ("public"."user_has_restaurant_role"("restaurant_id", ARRAY['owner'::"text", 'manager'::"text"]));



CREATE POLICY "ingest_runs_select_manager" ON "public"."ingest_runs" FOR SELECT TO "authenticated" USING ("public"."user_has_restaurant_role"("restaurant_id", ARRAY['owner'::"text", 'manager'::"text"]));



CREATE POLICY "ingest_runs_update_manager" ON "public"."ingest_runs" FOR UPDATE TO "authenticated" USING ("public"."user_has_restaurant_role"("restaurant_id", ARRAY['owner'::"text", 'manager'::"text"])) WITH CHECK ("public"."user_has_restaurant_role"("restaurant_id", ARRAY['owner'::"text", 'manager'::"text"]));



ALTER TABLE "public"."knowledge_chunks" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "knowledge_chunks_delete_manager" ON "public"."knowledge_chunks" FOR DELETE TO "authenticated" USING ("public"."user_has_restaurant_role"("restaurant_id", ARRAY['owner'::"text", 'manager'::"text"]));



CREATE POLICY "knowledge_chunks_insert_manager" ON "public"."knowledge_chunks" FOR INSERT TO "authenticated" WITH CHECK ("public"."user_has_restaurant_role"("restaurant_id", ARRAY['owner'::"text", 'manager'::"text"]));



CREATE POLICY "knowledge_chunks_select_member" ON "public"."knowledge_chunks" FOR SELECT TO "authenticated" USING ("public"."user_has_restaurant_role"("restaurant_id", ARRAY['owner'::"text", 'manager'::"text", 'staff'::"text"]));



CREATE POLICY "knowledge_chunks_update_manager" ON "public"."knowledge_chunks" FOR UPDATE TO "authenticated" USING ("public"."user_has_restaurant_role"("restaurant_id", ARRAY['owner'::"text", 'manager'::"text"])) WITH CHECK ("public"."user_has_restaurant_role"("restaurant_id", ARRAY['owner'::"text", 'manager'::"text"]));



ALTER TABLE "public"."profiles" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "profiles_select_own" ON "public"."profiles" FOR SELECT TO "authenticated" USING (("id" = "auth"."uid"()));



CREATE POLICY "profiles_update_own" ON "public"."profiles" FOR UPDATE TO "authenticated" USING (("id" = "auth"."uid"())) WITH CHECK (("id" = "auth"."uid"()));



ALTER TABLE "public"."restaurant_allowed_origins" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "restaurant_allowed_origins_delete_owner" ON "public"."restaurant_allowed_origins" FOR DELETE TO "authenticated" USING ("public"."user_owns_restaurant"("restaurant_id"));



CREATE POLICY "restaurant_allowed_origins_insert_owner" ON "public"."restaurant_allowed_origins" FOR INSERT TO "authenticated" WITH CHECK ("public"."user_owns_restaurant"("restaurant_id"));



CREATE POLICY "restaurant_allowed_origins_select_manager" ON "public"."restaurant_allowed_origins" FOR SELECT TO "authenticated" USING ("public"."user_has_restaurant_role"("restaurant_id", ARRAY['owner'::"text", 'manager'::"text"]));



CREATE POLICY "restaurant_allowed_origins_update_owner" ON "public"."restaurant_allowed_origins" FOR UPDATE TO "authenticated" USING ("public"."user_owns_restaurant"("restaurant_id")) WITH CHECK ("public"."user_owns_restaurant"("restaurant_id"));



ALTER TABLE "public"."restaurant_security_settings" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "restaurant_security_settings_insert_owner" ON "public"."restaurant_security_settings" FOR INSERT TO "authenticated" WITH CHECK ("public"."user_owns_restaurant"("restaurant_id"));



CREATE POLICY "restaurant_security_settings_select_manager" ON "public"."restaurant_security_settings" FOR SELECT TO "authenticated" USING ("public"."user_has_restaurant_role"("restaurant_id", ARRAY['owner'::"text", 'manager'::"text"]));



CREATE POLICY "restaurant_security_settings_update_owner" ON "public"."restaurant_security_settings" FOR UPDATE TO "authenticated" USING ("public"."user_owns_restaurant"("restaurant_id")) WITH CHECK ("public"."user_owns_restaurant"("restaurant_id"));



ALTER TABLE "public"."restaurant_users" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "restaurant_users_delete_owner" ON "public"."restaurant_users" FOR DELETE TO "authenticated" USING ("public"."user_owns_restaurant"("restaurant_id"));



CREATE POLICY "restaurant_users_insert_owner" ON "public"."restaurant_users" FOR INSERT TO "authenticated" WITH CHECK ("public"."user_owns_restaurant"("restaurant_id"));



CREATE POLICY "restaurant_users_select_own" ON "public"."restaurant_users" FOR SELECT TO "authenticated" USING (("user_id" = "auth"."uid"()));



CREATE POLICY "restaurant_users_select_owner" ON "public"."restaurant_users" FOR SELECT TO "authenticated" USING ("public"."user_owns_restaurant"("restaurant_id"));



CREATE POLICY "restaurant_users_update_owner" ON "public"."restaurant_users" FOR UPDATE TO "authenticated" USING ("public"."user_owns_restaurant"("restaurant_id")) WITH CHECK ("public"."user_owns_restaurant"("restaurant_id"));



ALTER TABLE "public"."restaurants" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "restaurants_select_member" ON "public"."restaurants" FOR SELECT TO "authenticated" USING ("public"."user_has_restaurant_role"("id", ARRAY['owner'::"text", 'manager'::"text", 'staff'::"text"]));



CREATE POLICY "restaurants_update_owner" ON "public"."restaurants" FOR UPDATE TO "authenticated" USING ("public"."user_owns_restaurant"("id")) WITH CHECK ("public"."user_owns_restaurant"("id"));



CREATE POLICY "service_role_audit_events_all" ON "public"."audit_events" USING (("auth"."role"() = 'service_role'::"text")) WITH CHECK (("auth"."role"() = 'service_role'::"text"));



CREATE POLICY "service_role_chat_messages_all" ON "public"."chat_messages" USING (("auth"."role"() = 'service_role'::"text")) WITH CHECK (("auth"."role"() = 'service_role'::"text"));



CREATE POLICY "service_role_chat_session_state_all" ON "public"."chat_session_state" USING (("auth"."role"() = 'service_role'::"text")) WITH CHECK (("auth"."role"() = 'service_role'::"text"));



CREATE POLICY "service_role_chat_sessions_all" ON "public"."chat_sessions" USING (("auth"."role"() = 'service_role'::"text")) WITH CHECK (("auth"."role"() = 'service_role'::"text"));



CREATE POLICY "service_role_deleted_knowledge_chunks_all" ON "public"."deleted_knowledge_chunks" USING (("auth"."role"() = 'service_role'::"text")) WITH CHECK (("auth"."role"() = 'service_role'::"text"));



CREATE POLICY "service_role_ingest_runs_all" ON "public"."ingest_runs" USING (("auth"."role"() = 'service_role'::"text")) WITH CHECK (("auth"."role"() = 'service_role'::"text"));



CREATE POLICY "service_role_knowledge_chunks_all" ON "public"."knowledge_chunks" USING (("auth"."role"() = 'service_role'::"text")) WITH CHECK (("auth"."role"() = 'service_role'::"text"));



CREATE POLICY "service_role_profiles_all" ON "public"."profiles" USING (("auth"."role"() = 'service_role'::"text")) WITH CHECK (("auth"."role"() = 'service_role'::"text"));



CREATE POLICY "service_role_restaurant_allowed_origins_all" ON "public"."restaurant_allowed_origins" USING (("auth"."role"() = 'service_role'::"text")) WITH CHECK (("auth"."role"() = 'service_role'::"text"));



CREATE POLICY "service_role_restaurant_security_settings_all" ON "public"."restaurant_security_settings" USING (("auth"."role"() = 'service_role'::"text")) WITH CHECK (("auth"."role"() = 'service_role'::"text"));



CREATE POLICY "service_role_restaurant_users_all" ON "public"."restaurant_users" USING (("auth"."role"() = 'service_role'::"text")) WITH CHECK (("auth"."role"() = 'service_role'::"text"));



CREATE POLICY "service_role_restaurants_all" ON "public"."restaurants" USING (("auth"."role"() = 'service_role'::"text")) WITH CHECK (("auth"."role"() = 'service_role'::"text"));



ALTER TABLE "public"."user_auth_events" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "user_auth_events_insert_own" ON "public"."user_auth_events" FOR INSERT TO "authenticated" WITH CHECK (("user_id" = "auth"."uid"()));



CREATE POLICY "user_auth_events_select_own" ON "public"."user_auth_events" FOR SELECT TO "authenticated" USING (("user_id" = "auth"."uid"()));





ALTER PUBLICATION "supabase_realtime" OWNER TO "postgres";


GRANT USAGE ON SCHEMA "public" TO "postgres";
GRANT USAGE ON SCHEMA "public" TO "anon";
GRANT USAGE ON SCHEMA "public" TO "authenticated";
GRANT USAGE ON SCHEMA "public" TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_in"("cstring", "oid", integer) TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_in"("cstring", "oid", integer) TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_in"("cstring", "oid", integer) TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_in"("cstring", "oid", integer) TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_out"("public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_out"("public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_out"("public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_out"("public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_recv"("internal", "oid", integer) TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_recv"("internal", "oid", integer) TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_recv"("internal", "oid", integer) TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_recv"("internal", "oid", integer) TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_send"("public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_send"("public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_send"("public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_send"("public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_typmod_in"("cstring"[]) TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_typmod_in"("cstring"[]) TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_typmod_in"("cstring"[]) TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_typmod_in"("cstring"[]) TO "service_role";



GRANT ALL ON FUNCTION "public"."sparsevec_in"("cstring", "oid", integer) TO "postgres";
GRANT ALL ON FUNCTION "public"."sparsevec_in"("cstring", "oid", integer) TO "anon";
GRANT ALL ON FUNCTION "public"."sparsevec_in"("cstring", "oid", integer) TO "authenticated";
GRANT ALL ON FUNCTION "public"."sparsevec_in"("cstring", "oid", integer) TO "service_role";



GRANT ALL ON FUNCTION "public"."sparsevec_out"("public"."sparsevec") TO "postgres";
GRANT ALL ON FUNCTION "public"."sparsevec_out"("public"."sparsevec") TO "anon";
GRANT ALL ON FUNCTION "public"."sparsevec_out"("public"."sparsevec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."sparsevec_out"("public"."sparsevec") TO "service_role";



GRANT ALL ON FUNCTION "public"."sparsevec_recv"("internal", "oid", integer) TO "postgres";
GRANT ALL ON FUNCTION "public"."sparsevec_recv"("internal", "oid", integer) TO "anon";
GRANT ALL ON FUNCTION "public"."sparsevec_recv"("internal", "oid", integer) TO "authenticated";
GRANT ALL ON FUNCTION "public"."sparsevec_recv"("internal", "oid", integer) TO "service_role";



GRANT ALL ON FUNCTION "public"."sparsevec_send"("public"."sparsevec") TO "postgres";
GRANT ALL ON FUNCTION "public"."sparsevec_send"("public"."sparsevec") TO "anon";
GRANT ALL ON FUNCTION "public"."sparsevec_send"("public"."sparsevec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."sparsevec_send"("public"."sparsevec") TO "service_role";



GRANT ALL ON FUNCTION "public"."sparsevec_typmod_in"("cstring"[]) TO "postgres";
GRANT ALL ON FUNCTION "public"."sparsevec_typmod_in"("cstring"[]) TO "anon";
GRANT ALL ON FUNCTION "public"."sparsevec_typmod_in"("cstring"[]) TO "authenticated";
GRANT ALL ON FUNCTION "public"."sparsevec_typmod_in"("cstring"[]) TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_in"("cstring", "oid", integer) TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_in"("cstring", "oid", integer) TO "anon";
GRANT ALL ON FUNCTION "public"."vector_in"("cstring", "oid", integer) TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_in"("cstring", "oid", integer) TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_out"("public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_out"("public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."vector_out"("public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_out"("public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_recv"("internal", "oid", integer) TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_recv"("internal", "oid", integer) TO "anon";
GRANT ALL ON FUNCTION "public"."vector_recv"("internal", "oid", integer) TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_recv"("internal", "oid", integer) TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_send"("public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_send"("public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."vector_send"("public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_send"("public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_typmod_in"("cstring"[]) TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_typmod_in"("cstring"[]) TO "anon";
GRANT ALL ON FUNCTION "public"."vector_typmod_in"("cstring"[]) TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_typmod_in"("cstring"[]) TO "service_role";



GRANT ALL ON FUNCTION "public"."array_to_halfvec"(real[], integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."array_to_halfvec"(real[], integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."array_to_halfvec"(real[], integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."array_to_halfvec"(real[], integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."array_to_sparsevec"(real[], integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."array_to_sparsevec"(real[], integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."array_to_sparsevec"(real[], integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."array_to_sparsevec"(real[], integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."array_to_vector"(real[], integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."array_to_vector"(real[], integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."array_to_vector"(real[], integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."array_to_vector"(real[], integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."array_to_halfvec"(double precision[], integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."array_to_halfvec"(double precision[], integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."array_to_halfvec"(double precision[], integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."array_to_halfvec"(double precision[], integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."array_to_sparsevec"(double precision[], integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."array_to_sparsevec"(double precision[], integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."array_to_sparsevec"(double precision[], integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."array_to_sparsevec"(double precision[], integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."array_to_vector"(double precision[], integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."array_to_vector"(double precision[], integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."array_to_vector"(double precision[], integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."array_to_vector"(double precision[], integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."array_to_halfvec"(integer[], integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."array_to_halfvec"(integer[], integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."array_to_halfvec"(integer[], integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."array_to_halfvec"(integer[], integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."array_to_sparsevec"(integer[], integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."array_to_sparsevec"(integer[], integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."array_to_sparsevec"(integer[], integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."array_to_sparsevec"(integer[], integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."array_to_vector"(integer[], integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."array_to_vector"(integer[], integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."array_to_vector"(integer[], integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."array_to_vector"(integer[], integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."array_to_halfvec"(numeric[], integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."array_to_halfvec"(numeric[], integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."array_to_halfvec"(numeric[], integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."array_to_halfvec"(numeric[], integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."array_to_sparsevec"(numeric[], integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."array_to_sparsevec"(numeric[], integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."array_to_sparsevec"(numeric[], integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."array_to_sparsevec"(numeric[], integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."array_to_vector"(numeric[], integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."array_to_vector"(numeric[], integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."array_to_vector"(numeric[], integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."array_to_vector"(numeric[], integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_to_float4"("public"."halfvec", integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_to_float4"("public"."halfvec", integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_to_float4"("public"."halfvec", integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_to_float4"("public"."halfvec", integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec"("public"."halfvec", integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec"("public"."halfvec", integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec"("public"."halfvec", integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec"("public"."halfvec", integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_to_sparsevec"("public"."halfvec", integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_to_sparsevec"("public"."halfvec", integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_to_sparsevec"("public"."halfvec", integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_to_sparsevec"("public"."halfvec", integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_to_vector"("public"."halfvec", integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_to_vector"("public"."halfvec", integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_to_vector"("public"."halfvec", integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_to_vector"("public"."halfvec", integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."sparsevec_to_halfvec"("public"."sparsevec", integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."sparsevec_to_halfvec"("public"."sparsevec", integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."sparsevec_to_halfvec"("public"."sparsevec", integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."sparsevec_to_halfvec"("public"."sparsevec", integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."sparsevec"("public"."sparsevec", integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."sparsevec"("public"."sparsevec", integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."sparsevec"("public"."sparsevec", integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."sparsevec"("public"."sparsevec", integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."sparsevec_to_vector"("public"."sparsevec", integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."sparsevec_to_vector"("public"."sparsevec", integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."sparsevec_to_vector"("public"."sparsevec", integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."sparsevec_to_vector"("public"."sparsevec", integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_to_float4"("public"."vector", integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_to_float4"("public"."vector", integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."vector_to_float4"("public"."vector", integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_to_float4"("public"."vector", integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_to_halfvec"("public"."vector", integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_to_halfvec"("public"."vector", integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."vector_to_halfvec"("public"."vector", integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_to_halfvec"("public"."vector", integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_to_sparsevec"("public"."vector", integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_to_sparsevec"("public"."vector", integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."vector_to_sparsevec"("public"."vector", integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_to_sparsevec"("public"."vector", integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."vector"("public"."vector", integer, boolean) TO "postgres";
GRANT ALL ON FUNCTION "public"."vector"("public"."vector", integer, boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."vector"("public"."vector", integer, boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector"("public"."vector", integer, boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."binary_quantize"("public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."binary_quantize"("public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."binary_quantize"("public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."binary_quantize"("public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."binary_quantize"("public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."binary_quantize"("public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."binary_quantize"("public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."binary_quantize"("public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."cosine_distance"("public"."halfvec", "public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."cosine_distance"("public"."halfvec", "public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."cosine_distance"("public"."halfvec", "public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."cosine_distance"("public"."halfvec", "public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."cosine_distance"("public"."sparsevec", "public"."sparsevec") TO "postgres";
GRANT ALL ON FUNCTION "public"."cosine_distance"("public"."sparsevec", "public"."sparsevec") TO "anon";
GRANT ALL ON FUNCTION "public"."cosine_distance"("public"."sparsevec", "public"."sparsevec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."cosine_distance"("public"."sparsevec", "public"."sparsevec") TO "service_role";



GRANT ALL ON FUNCTION "public"."cosine_distance"("public"."vector", "public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."cosine_distance"("public"."vector", "public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."cosine_distance"("public"."vector", "public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."cosine_distance"("public"."vector", "public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."delete_knowledge_chunk"("p_chunk_id" "text", "p_reason" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."delete_knowledge_chunk"("p_chunk_id" "text", "p_reason" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_accum"(double precision[], "public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_accum"(double precision[], "public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_accum"(double precision[], "public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_accum"(double precision[], "public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_add"("public"."halfvec", "public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_add"("public"."halfvec", "public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_add"("public"."halfvec", "public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_add"("public"."halfvec", "public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_avg"(double precision[]) TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_avg"(double precision[]) TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_avg"(double precision[]) TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_avg"(double precision[]) TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_cmp"("public"."halfvec", "public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_cmp"("public"."halfvec", "public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_cmp"("public"."halfvec", "public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_cmp"("public"."halfvec", "public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_combine"(double precision[], double precision[]) TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_combine"(double precision[], double precision[]) TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_combine"(double precision[], double precision[]) TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_combine"(double precision[], double precision[]) TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_concat"("public"."halfvec", "public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_concat"("public"."halfvec", "public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_concat"("public"."halfvec", "public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_concat"("public"."halfvec", "public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_eq"("public"."halfvec", "public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_eq"("public"."halfvec", "public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_eq"("public"."halfvec", "public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_eq"("public"."halfvec", "public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_ge"("public"."halfvec", "public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_ge"("public"."halfvec", "public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_ge"("public"."halfvec", "public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_ge"("public"."halfvec", "public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_gt"("public"."halfvec", "public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_gt"("public"."halfvec", "public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_gt"("public"."halfvec", "public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_gt"("public"."halfvec", "public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_l2_squared_distance"("public"."halfvec", "public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_l2_squared_distance"("public"."halfvec", "public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_l2_squared_distance"("public"."halfvec", "public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_l2_squared_distance"("public"."halfvec", "public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_le"("public"."halfvec", "public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_le"("public"."halfvec", "public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_le"("public"."halfvec", "public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_le"("public"."halfvec", "public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_lt"("public"."halfvec", "public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_lt"("public"."halfvec", "public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_lt"("public"."halfvec", "public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_lt"("public"."halfvec", "public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_mul"("public"."halfvec", "public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_mul"("public"."halfvec", "public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_mul"("public"."halfvec", "public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_mul"("public"."halfvec", "public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_ne"("public"."halfvec", "public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_ne"("public"."halfvec", "public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_ne"("public"."halfvec", "public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_ne"("public"."halfvec", "public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_negative_inner_product"("public"."halfvec", "public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_negative_inner_product"("public"."halfvec", "public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_negative_inner_product"("public"."halfvec", "public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_negative_inner_product"("public"."halfvec", "public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_spherical_distance"("public"."halfvec", "public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_spherical_distance"("public"."halfvec", "public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_spherical_distance"("public"."halfvec", "public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_spherical_distance"("public"."halfvec", "public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."halfvec_sub"("public"."halfvec", "public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."halfvec_sub"("public"."halfvec", "public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."halfvec_sub"("public"."halfvec", "public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."halfvec_sub"("public"."halfvec", "public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."hamming_distance"(bit, bit) TO "postgres";
GRANT ALL ON FUNCTION "public"."hamming_distance"(bit, bit) TO "anon";
GRANT ALL ON FUNCTION "public"."hamming_distance"(bit, bit) TO "authenticated";
GRANT ALL ON FUNCTION "public"."hamming_distance"(bit, bit) TO "service_role";



GRANT ALL ON FUNCTION "public"."handle_new_user"() TO "anon";
GRANT ALL ON FUNCTION "public"."handle_new_user"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."handle_new_user"() TO "service_role";



GRANT ALL ON FUNCTION "public"."hnsw_bit_support"("internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."hnsw_bit_support"("internal") TO "anon";
GRANT ALL ON FUNCTION "public"."hnsw_bit_support"("internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."hnsw_bit_support"("internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."hnsw_halfvec_support"("internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."hnsw_halfvec_support"("internal") TO "anon";
GRANT ALL ON FUNCTION "public"."hnsw_halfvec_support"("internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."hnsw_halfvec_support"("internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."hnsw_sparsevec_support"("internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."hnsw_sparsevec_support"("internal") TO "anon";
GRANT ALL ON FUNCTION "public"."hnsw_sparsevec_support"("internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."hnsw_sparsevec_support"("internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."hnswhandler"("internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."hnswhandler"("internal") TO "anon";
GRANT ALL ON FUNCTION "public"."hnswhandler"("internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."hnswhandler"("internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."inner_product"("public"."halfvec", "public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."inner_product"("public"."halfvec", "public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."inner_product"("public"."halfvec", "public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."inner_product"("public"."halfvec", "public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."inner_product"("public"."sparsevec", "public"."sparsevec") TO "postgres";
GRANT ALL ON FUNCTION "public"."inner_product"("public"."sparsevec", "public"."sparsevec") TO "anon";
GRANT ALL ON FUNCTION "public"."inner_product"("public"."sparsevec", "public"."sparsevec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."inner_product"("public"."sparsevec", "public"."sparsevec") TO "service_role";



GRANT ALL ON FUNCTION "public"."inner_product"("public"."vector", "public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."inner_product"("public"."vector", "public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."inner_product"("public"."vector", "public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."inner_product"("public"."vector", "public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."ivfflat_bit_support"("internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."ivfflat_bit_support"("internal") TO "anon";
GRANT ALL ON FUNCTION "public"."ivfflat_bit_support"("internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."ivfflat_bit_support"("internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."ivfflat_halfvec_support"("internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."ivfflat_halfvec_support"("internal") TO "anon";
GRANT ALL ON FUNCTION "public"."ivfflat_halfvec_support"("internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."ivfflat_halfvec_support"("internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."ivfflathandler"("internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."ivfflathandler"("internal") TO "anon";
GRANT ALL ON FUNCTION "public"."ivfflathandler"("internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."ivfflathandler"("internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."jaccard_distance"(bit, bit) TO "postgres";
GRANT ALL ON FUNCTION "public"."jaccard_distance"(bit, bit) TO "anon";
GRANT ALL ON FUNCTION "public"."jaccard_distance"(bit, bit) TO "authenticated";
GRANT ALL ON FUNCTION "public"."jaccard_distance"(bit, bit) TO "service_role";



GRANT ALL ON FUNCTION "public"."l1_distance"("public"."halfvec", "public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."l1_distance"("public"."halfvec", "public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."l1_distance"("public"."halfvec", "public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."l1_distance"("public"."halfvec", "public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."l1_distance"("public"."sparsevec", "public"."sparsevec") TO "postgres";
GRANT ALL ON FUNCTION "public"."l1_distance"("public"."sparsevec", "public"."sparsevec") TO "anon";
GRANT ALL ON FUNCTION "public"."l1_distance"("public"."sparsevec", "public"."sparsevec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."l1_distance"("public"."sparsevec", "public"."sparsevec") TO "service_role";



GRANT ALL ON FUNCTION "public"."l1_distance"("public"."vector", "public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."l1_distance"("public"."vector", "public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."l1_distance"("public"."vector", "public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."l1_distance"("public"."vector", "public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."l2_distance"("public"."halfvec", "public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."l2_distance"("public"."halfvec", "public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."l2_distance"("public"."halfvec", "public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."l2_distance"("public"."halfvec", "public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."l2_distance"("public"."sparsevec", "public"."sparsevec") TO "postgres";
GRANT ALL ON FUNCTION "public"."l2_distance"("public"."sparsevec", "public"."sparsevec") TO "anon";
GRANT ALL ON FUNCTION "public"."l2_distance"("public"."sparsevec", "public"."sparsevec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."l2_distance"("public"."sparsevec", "public"."sparsevec") TO "service_role";



GRANT ALL ON FUNCTION "public"."l2_distance"("public"."vector", "public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."l2_distance"("public"."vector", "public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."l2_distance"("public"."vector", "public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."l2_distance"("public"."vector", "public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."l2_norm"("public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."l2_norm"("public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."l2_norm"("public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."l2_norm"("public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."l2_norm"("public"."sparsevec") TO "postgres";
GRANT ALL ON FUNCTION "public"."l2_norm"("public"."sparsevec") TO "anon";
GRANT ALL ON FUNCTION "public"."l2_norm"("public"."sparsevec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."l2_norm"("public"."sparsevec") TO "service_role";



GRANT ALL ON FUNCTION "public"."l2_normalize"("public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."l2_normalize"("public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."l2_normalize"("public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."l2_normalize"("public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."l2_normalize"("public"."sparsevec") TO "postgres";
GRANT ALL ON FUNCTION "public"."l2_normalize"("public"."sparsevec") TO "anon";
GRANT ALL ON FUNCTION "public"."l2_normalize"("public"."sparsevec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."l2_normalize"("public"."sparsevec") TO "service_role";



GRANT ALL ON FUNCTION "public"."l2_normalize"("public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."l2_normalize"("public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."l2_normalize"("public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."l2_normalize"("public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."match_chunks"("p_restaurant_id" "uuid", "query_embedding" "public"."vector", "match_count" integer, "min_score" double precision) TO "service_role";



GRANT ALL ON FUNCTION "public"."normalize_origin_text"() TO "anon";
GRANT ALL ON FUNCTION "public"."normalize_origin_text"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."normalize_origin_text"() TO "service_role";



GRANT ALL ON FUNCTION "public"."sparsevec_cmp"("public"."sparsevec", "public"."sparsevec") TO "postgres";
GRANT ALL ON FUNCTION "public"."sparsevec_cmp"("public"."sparsevec", "public"."sparsevec") TO "anon";
GRANT ALL ON FUNCTION "public"."sparsevec_cmp"("public"."sparsevec", "public"."sparsevec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."sparsevec_cmp"("public"."sparsevec", "public"."sparsevec") TO "service_role";



GRANT ALL ON FUNCTION "public"."sparsevec_eq"("public"."sparsevec", "public"."sparsevec") TO "postgres";
GRANT ALL ON FUNCTION "public"."sparsevec_eq"("public"."sparsevec", "public"."sparsevec") TO "anon";
GRANT ALL ON FUNCTION "public"."sparsevec_eq"("public"."sparsevec", "public"."sparsevec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."sparsevec_eq"("public"."sparsevec", "public"."sparsevec") TO "service_role";



GRANT ALL ON FUNCTION "public"."sparsevec_ge"("public"."sparsevec", "public"."sparsevec") TO "postgres";
GRANT ALL ON FUNCTION "public"."sparsevec_ge"("public"."sparsevec", "public"."sparsevec") TO "anon";
GRANT ALL ON FUNCTION "public"."sparsevec_ge"("public"."sparsevec", "public"."sparsevec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."sparsevec_ge"("public"."sparsevec", "public"."sparsevec") TO "service_role";



GRANT ALL ON FUNCTION "public"."sparsevec_gt"("public"."sparsevec", "public"."sparsevec") TO "postgres";
GRANT ALL ON FUNCTION "public"."sparsevec_gt"("public"."sparsevec", "public"."sparsevec") TO "anon";
GRANT ALL ON FUNCTION "public"."sparsevec_gt"("public"."sparsevec", "public"."sparsevec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."sparsevec_gt"("public"."sparsevec", "public"."sparsevec") TO "service_role";



GRANT ALL ON FUNCTION "public"."sparsevec_l2_squared_distance"("public"."sparsevec", "public"."sparsevec") TO "postgres";
GRANT ALL ON FUNCTION "public"."sparsevec_l2_squared_distance"("public"."sparsevec", "public"."sparsevec") TO "anon";
GRANT ALL ON FUNCTION "public"."sparsevec_l2_squared_distance"("public"."sparsevec", "public"."sparsevec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."sparsevec_l2_squared_distance"("public"."sparsevec", "public"."sparsevec") TO "service_role";



GRANT ALL ON FUNCTION "public"."sparsevec_le"("public"."sparsevec", "public"."sparsevec") TO "postgres";
GRANT ALL ON FUNCTION "public"."sparsevec_le"("public"."sparsevec", "public"."sparsevec") TO "anon";
GRANT ALL ON FUNCTION "public"."sparsevec_le"("public"."sparsevec", "public"."sparsevec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."sparsevec_le"("public"."sparsevec", "public"."sparsevec") TO "service_role";



GRANT ALL ON FUNCTION "public"."sparsevec_lt"("public"."sparsevec", "public"."sparsevec") TO "postgres";
GRANT ALL ON FUNCTION "public"."sparsevec_lt"("public"."sparsevec", "public"."sparsevec") TO "anon";
GRANT ALL ON FUNCTION "public"."sparsevec_lt"("public"."sparsevec", "public"."sparsevec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."sparsevec_lt"("public"."sparsevec", "public"."sparsevec") TO "service_role";



GRANT ALL ON FUNCTION "public"."sparsevec_ne"("public"."sparsevec", "public"."sparsevec") TO "postgres";
GRANT ALL ON FUNCTION "public"."sparsevec_ne"("public"."sparsevec", "public"."sparsevec") TO "anon";
GRANT ALL ON FUNCTION "public"."sparsevec_ne"("public"."sparsevec", "public"."sparsevec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."sparsevec_ne"("public"."sparsevec", "public"."sparsevec") TO "service_role";



GRANT ALL ON FUNCTION "public"."sparsevec_negative_inner_product"("public"."sparsevec", "public"."sparsevec") TO "postgres";
GRANT ALL ON FUNCTION "public"."sparsevec_negative_inner_product"("public"."sparsevec", "public"."sparsevec") TO "anon";
GRANT ALL ON FUNCTION "public"."sparsevec_negative_inner_product"("public"."sparsevec", "public"."sparsevec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."sparsevec_negative_inner_product"("public"."sparsevec", "public"."sparsevec") TO "service_role";



GRANT ALL ON FUNCTION "public"."subvector"("public"."halfvec", integer, integer) TO "postgres";
GRANT ALL ON FUNCTION "public"."subvector"("public"."halfvec", integer, integer) TO "anon";
GRANT ALL ON FUNCTION "public"."subvector"("public"."halfvec", integer, integer) TO "authenticated";
GRANT ALL ON FUNCTION "public"."subvector"("public"."halfvec", integer, integer) TO "service_role";



GRANT ALL ON FUNCTION "public"."subvector"("public"."vector", integer, integer) TO "postgres";
GRANT ALL ON FUNCTION "public"."subvector"("public"."vector", integer, integer) TO "anon";
GRANT ALL ON FUNCTION "public"."subvector"("public"."vector", integer, integer) TO "authenticated";
GRANT ALL ON FUNCTION "public"."subvector"("public"."vector", integer, integer) TO "service_role";



GRANT ALL ON FUNCTION "public"."touch_last_activity"() TO "anon";
GRANT ALL ON FUNCTION "public"."touch_last_activity"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."touch_last_activity"() TO "service_role";



GRANT ALL ON FUNCTION "public"."touch_updated_at"() TO "anon";
GRANT ALL ON FUNCTION "public"."touch_updated_at"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."touch_updated_at"() TO "service_role";



GRANT ALL ON FUNCTION "public"."upsert_restaurant"("p_restaurant_id" "uuid", "p_slug" "text", "p_name" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."upsert_session"("p_restaurant_id" "uuid", "p_session_token" "text", "p_client_meta" "jsonb") TO "service_role";



GRANT ALL ON FUNCTION "public"."upsert_session"("p_restaurant_id" "uuid", "p_session_token" "text", "p_client_meta" "jsonb", "p_language" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."upsert_session"("p_restaurant_id" "uuid", "p_session_token" "text", "p_client_meta" "jsonb", "p_language" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."upsert_session"("p_restaurant_id" "uuid", "p_session_token" "text", "p_client_meta" "jsonb", "p_language" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."user_has_restaurant_role"("p_restaurant_id" "uuid", "p_roles" "text"[]) TO "anon";
GRANT ALL ON FUNCTION "public"."user_has_restaurant_role"("p_restaurant_id" "uuid", "p_roles" "text"[]) TO "authenticated";
GRANT ALL ON FUNCTION "public"."user_has_restaurant_role"("p_restaurant_id" "uuid", "p_roles" "text"[]) TO "service_role";



GRANT ALL ON FUNCTION "public"."user_manages_restaurant"("p_restaurant_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."user_manages_restaurant"("p_restaurant_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."user_manages_restaurant"("p_restaurant_id" "uuid") TO "service_role";



GRANT ALL ON FUNCTION "public"."user_owns_restaurant"("p_restaurant_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."user_owns_restaurant"("p_restaurant_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."user_owns_restaurant"("p_restaurant_id" "uuid") TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_accum"(double precision[], "public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_accum"(double precision[], "public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."vector_accum"(double precision[], "public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_accum"(double precision[], "public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_add"("public"."vector", "public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_add"("public"."vector", "public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."vector_add"("public"."vector", "public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_add"("public"."vector", "public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_avg"(double precision[]) TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_avg"(double precision[]) TO "anon";
GRANT ALL ON FUNCTION "public"."vector_avg"(double precision[]) TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_avg"(double precision[]) TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_cmp"("public"."vector", "public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_cmp"("public"."vector", "public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."vector_cmp"("public"."vector", "public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_cmp"("public"."vector", "public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_combine"(double precision[], double precision[]) TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_combine"(double precision[], double precision[]) TO "anon";
GRANT ALL ON FUNCTION "public"."vector_combine"(double precision[], double precision[]) TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_combine"(double precision[], double precision[]) TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_concat"("public"."vector", "public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_concat"("public"."vector", "public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."vector_concat"("public"."vector", "public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_concat"("public"."vector", "public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_dims"("public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_dims"("public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."vector_dims"("public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_dims"("public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_dims"("public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_dims"("public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."vector_dims"("public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_dims"("public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_eq"("public"."vector", "public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_eq"("public"."vector", "public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."vector_eq"("public"."vector", "public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_eq"("public"."vector", "public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_ge"("public"."vector", "public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_ge"("public"."vector", "public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."vector_ge"("public"."vector", "public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_ge"("public"."vector", "public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_gt"("public"."vector", "public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_gt"("public"."vector", "public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."vector_gt"("public"."vector", "public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_gt"("public"."vector", "public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_l2_squared_distance"("public"."vector", "public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_l2_squared_distance"("public"."vector", "public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."vector_l2_squared_distance"("public"."vector", "public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_l2_squared_distance"("public"."vector", "public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_le"("public"."vector", "public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_le"("public"."vector", "public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."vector_le"("public"."vector", "public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_le"("public"."vector", "public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_lt"("public"."vector", "public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_lt"("public"."vector", "public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."vector_lt"("public"."vector", "public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_lt"("public"."vector", "public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_mul"("public"."vector", "public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_mul"("public"."vector", "public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."vector_mul"("public"."vector", "public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_mul"("public"."vector", "public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_ne"("public"."vector", "public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_ne"("public"."vector", "public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."vector_ne"("public"."vector", "public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_ne"("public"."vector", "public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_negative_inner_product"("public"."vector", "public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_negative_inner_product"("public"."vector", "public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."vector_negative_inner_product"("public"."vector", "public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_negative_inner_product"("public"."vector", "public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_norm"("public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_norm"("public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."vector_norm"("public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_norm"("public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_spherical_distance"("public"."vector", "public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_spherical_distance"("public"."vector", "public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."vector_spherical_distance"("public"."vector", "public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_spherical_distance"("public"."vector", "public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."vector_sub"("public"."vector", "public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."vector_sub"("public"."vector", "public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."vector_sub"("public"."vector", "public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."vector_sub"("public"."vector", "public"."vector") TO "service_role";












GRANT ALL ON FUNCTION "public"."avg"("public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."avg"("public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."avg"("public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."avg"("public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."avg"("public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."avg"("public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."avg"("public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."avg"("public"."vector") TO "service_role";



GRANT ALL ON FUNCTION "public"."sum"("public"."halfvec") TO "postgres";
GRANT ALL ON FUNCTION "public"."sum"("public"."halfvec") TO "anon";
GRANT ALL ON FUNCTION "public"."sum"("public"."halfvec") TO "authenticated";
GRANT ALL ON FUNCTION "public"."sum"("public"."halfvec") TO "service_role";



GRANT ALL ON FUNCTION "public"."sum"("public"."vector") TO "postgres";
GRANT ALL ON FUNCTION "public"."sum"("public"."vector") TO "anon";
GRANT ALL ON FUNCTION "public"."sum"("public"."vector") TO "authenticated";
GRANT ALL ON FUNCTION "public"."sum"("public"."vector") TO "service_role";









GRANT ALL ON TABLE "public"."audit_events" TO "anon";
GRANT ALL ON TABLE "public"."audit_events" TO "authenticated";
GRANT ALL ON TABLE "public"."audit_events" TO "service_role";



GRANT ALL ON TABLE "public"."chat_messages" TO "anon";
GRANT ALL ON TABLE "public"."chat_messages" TO "authenticated";
GRANT ALL ON TABLE "public"."chat_messages" TO "service_role";



GRANT ALL ON TABLE "public"."chat_session_state" TO "anon";
GRANT ALL ON TABLE "public"."chat_session_state" TO "authenticated";
GRANT ALL ON TABLE "public"."chat_session_state" TO "service_role";



GRANT ALL ON TABLE "public"."chat_sessions" TO "anon";
GRANT ALL ON TABLE "public"."chat_sessions" TO "authenticated";
GRANT ALL ON TABLE "public"."chat_sessions" TO "service_role";



GRANT ALL ON TABLE "public"."deleted_knowledge_chunks" TO "anon";
GRANT ALL ON TABLE "public"."deleted_knowledge_chunks" TO "authenticated";
GRANT ALL ON TABLE "public"."deleted_knowledge_chunks" TO "service_role";



GRANT ALL ON TABLE "public"."ingest_runs" TO "anon";
GRANT ALL ON TABLE "public"."ingest_runs" TO "authenticated";
GRANT ALL ON TABLE "public"."ingest_runs" TO "service_role";



GRANT ALL ON TABLE "public"."knowledge_chunks" TO "anon";
GRANT SELECT,REFERENCES,TRIGGER,TRUNCATE,MAINTAIN ON TABLE "public"."knowledge_chunks" TO "authenticated";
GRANT ALL ON TABLE "public"."knowledge_chunks" TO "service_role";



GRANT ALL ON TABLE "public"."profiles" TO "anon";
GRANT ALL ON TABLE "public"."profiles" TO "authenticated";
GRANT ALL ON TABLE "public"."profiles" TO "service_role";



GRANT ALL ON TABLE "public"."restaurant_allowed_origins" TO "anon";
GRANT ALL ON TABLE "public"."restaurant_allowed_origins" TO "authenticated";
GRANT ALL ON TABLE "public"."restaurant_allowed_origins" TO "service_role";



GRANT ALL ON TABLE "public"."restaurant_security_settings" TO "anon";
GRANT ALL ON TABLE "public"."restaurant_security_settings" TO "authenticated";
GRANT ALL ON TABLE "public"."restaurant_security_settings" TO "service_role";



GRANT ALL ON TABLE "public"."restaurant_users" TO "anon";
GRANT ALL ON TABLE "public"."restaurant_users" TO "authenticated";
GRANT ALL ON TABLE "public"."restaurant_users" TO "service_role";



GRANT ALL ON TABLE "public"."restaurants" TO "anon";
GRANT ALL ON TABLE "public"."restaurants" TO "authenticated";
GRANT ALL ON TABLE "public"."restaurants" TO "service_role";



GRANT ALL ON TABLE "public"."user_auth_events" TO "anon";
GRANT ALL ON TABLE "public"."user_auth_events" TO "authenticated";
GRANT ALL ON TABLE "public"."user_auth_events" TO "service_role";









ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "service_role";






ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "service_role";






ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "service_role";




CREATE TABLE IF NOT EXISTS "public"."restaurant_subscriptions" (
    "restaurant_id" "uuid" NOT NULL,
    "stripe_customer_id" "text",
    "stripe_subscription_id" "text",
    "stripe_payment_link_id" "text",
    "stripe_checkout_session_id" "text",
    "client_reference_id" "text",
    "stripe_price_id" "text",
    "stripe_product_id" "text",
    "stripe_subscription_status" "text" NOT NULL,
    "current_period_start" timestamp with time zone,
    "current_period_end" timestamp with time zone,
    "cancel_at" timestamp with time zone,
    "canceled_at" timestamp with time zone,
    "ended_at" timestamp with time zone,
    "last_checkout_completed_at" timestamp with time zone,
    "last_synced_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "restaurant_subscriptions_pkey" PRIMARY KEY ("restaurant_id"),
    CONSTRAINT "restaurant_subscriptions_restaurant_id_fkey" FOREIGN KEY ("restaurant_id") REFERENCES "public"."restaurants"("id") ON DELETE CASCADE,
    CONSTRAINT "restaurant_subscriptions_status_check" CHECK (("stripe_subscription_status" = ANY (ARRAY['active'::"text", 'trialing'::"text", 'past_due'::"text", 'canceled'::"text", 'unpaid'::"text", 'incomplete'::"text", 'incomplete_expired'::"text", 'paused'::"text"])))
);


ALTER TABLE "public"."restaurant_subscriptions" OWNER TO "postgres";


CREATE UNIQUE INDEX "restaurant_subscriptions_stripe_customer_id_idx" ON "public"."restaurant_subscriptions" USING "btree" ("stripe_customer_id") WHERE ("stripe_customer_id" IS NOT NULL);


CREATE UNIQUE INDEX "restaurant_subscriptions_stripe_subscription_id_idx" ON "public"."restaurant_subscriptions" USING "btree" ("stripe_subscription_id") WHERE ("stripe_subscription_id" IS NOT NULL);


CREATE OR REPLACE TRIGGER "trg_touch_restaurant_subscriptions_updated_at" BEFORE UPDATE ON "public"."restaurant_subscriptions" FOR EACH ROW EXECUTE FUNCTION "public"."touch_updated_at"();


CREATE TABLE IF NOT EXISTS "public"."stripe_webhook_events" (
    "event_id" "text" NOT NULL,
    "event_type" "text" NOT NULL,
    "stripe_created_at" timestamp with time zone,
    "restaurant_id" "uuid",
    "stripe_customer_id" "text",
    "stripe_subscription_id" "text",
    "processing_status" "text" DEFAULT 'received'::"text" NOT NULL,
    "payload" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "error_message" "text",
    "processed_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "stripe_webhook_events_pkey" PRIMARY KEY ("event_id"),
    CONSTRAINT "stripe_webhook_events_processing_status_check" CHECK (("processing_status" = ANY (ARRAY['received'::"text", 'processed'::"text", 'failed'::"text"]))),
    CONSTRAINT "stripe_webhook_events_restaurant_id_fkey" FOREIGN KEY ("restaurant_id") REFERENCES "public"."restaurants"("id") ON DELETE SET NULL
);


ALTER TABLE "public"."stripe_webhook_events" OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."restaurant_has_active_subscription"("p_restaurant_id" "uuid") RETURNS boolean
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
  select exists (
    select 1
    from public.restaurant_subscriptions rs
    where rs.restaurant_id = p_restaurant_id
      and (
        rs.stripe_subscription_status = 'active'
        or (
          rs.current_period_end is not null
          and rs.current_period_end > now()
          and (rs.ended_at is null or rs.ended_at > now())
        )
      )
  );
$$;


ALTER FUNCTION "public"."restaurant_has_active_subscription"("p_restaurant_id" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."user_can_access_active_restaurant"("p_restaurant_id" "uuid", "p_roles" "text"[]) RETURNS boolean
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
  select public.user_has_restaurant_role(p_restaurant_id, p_roles)
     and public.restaurant_has_active_subscription(p_restaurant_id);
$$;


ALTER FUNCTION "public"."user_can_access_active_restaurant"("p_restaurant_id" "uuid", "p_roles" "text"[]) OWNER TO "postgres";


ALTER TABLE "public"."restaurant_subscriptions" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "restaurant_subscriptions_select_manager" ON "public"."restaurant_subscriptions" FOR SELECT TO "authenticated" USING ("public"."user_has_restaurant_role"("restaurant_id", ARRAY['owner'::"text", 'manager'::"text"]));


CREATE POLICY "service_role_restaurant_subscriptions_all" ON "public"."restaurant_subscriptions" USING (("auth"."role"() = 'service_role'::"text")) WITH CHECK (("auth"."role"() = 'service_role'::"text"));


ALTER TABLE "public"."stripe_webhook_events" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "service_role_stripe_webhook_events_all" ON "public"."stripe_webhook_events" USING (("auth"."role"() = 'service_role'::"text")) WITH CHECK (("auth"."role"() = 'service_role'::"text"));


GRANT ALL ON FUNCTION "public"."restaurant_has_active_subscription"("p_restaurant_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."restaurant_has_active_subscription"("p_restaurant_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."restaurant_has_active_subscription"("p_restaurant_id" "uuid") TO "service_role";


GRANT ALL ON FUNCTION "public"."user_can_access_active_restaurant"("p_restaurant_id" "uuid", "p_roles" "text"[]) TO "anon";
GRANT ALL ON FUNCTION "public"."user_can_access_active_restaurant"("p_restaurant_id" "uuid", "p_roles" "text"[]) TO "authenticated";
GRANT ALL ON FUNCTION "public"."user_can_access_active_restaurant"("p_restaurant_id" "uuid", "p_roles" "text"[]) TO "service_role";


GRANT ALL ON TABLE "public"."restaurant_subscriptions" TO "anon";
GRANT ALL ON TABLE "public"."restaurant_subscriptions" TO "authenticated";
GRANT ALL ON TABLE "public"."restaurant_subscriptions" TO "service_role";


GRANT ALL ON TABLE "public"."stripe_webhook_events" TO "service_role";


drop extension if exists "pg_net";

revoke delete on table "public"."knowledge_chunks" from "authenticated";

revoke insert on table "public"."knowledge_chunks" from "authenticated";

revoke update on table "public"."knowledge_chunks" from "authenticated";

CREATE TRIGGER on_auth_user_created AFTER INSERT ON auth.users FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();
