DROP FUNCTION IF EXISTS public.match_chunks(uuid, public.vector, integer, double precision);

CREATE OR REPLACE FUNCTION public.match_chunks(
    p_restaurant_id uuid,
    query_embedding public.vector,
    match_count integer DEFAULT 8,
    min_score double precision DEFAULT 0
) RETURNS TABLE(
    id uuid,
    text text,
    type text,
    source_url text,
    page_path text,
    title text,
    score double precision,
    image_url text,
    extra_metadata jsonb
)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
  select
    kc.id,
    kc.text,
    kc.type,
    kc.source_url,
    kc.page_path,
    kc.title,
    (1 - (kc.embedding <=> query_embedding))::float as score,
    null::text as image_url,
    coalesce(kc.extra_metadata, '{}'::jsonb) as extra_metadata
  from public.knowledge_chunks kc
  where kc.restaurant_id = p_restaurant_id
    and (1 - (kc.embedding <=> query_embedding)) >= coalesce(min_score, 0)
  order by kc.embedding <=> query_embedding
  limit greatest(match_count, 1);
$$;

ALTER FUNCTION public.match_chunks(uuid, public.vector, integer, double precision) OWNER TO postgres;

GRANT ALL ON FUNCTION public.match_chunks(uuid, public.vector, integer, double precision) TO anon;
GRANT ALL ON FUNCTION public.match_chunks(uuid, public.vector, integer, double precision) TO authenticated;
GRANT ALL ON FUNCTION public.match_chunks(uuid, public.vector, integer, double precision) TO service_role;
