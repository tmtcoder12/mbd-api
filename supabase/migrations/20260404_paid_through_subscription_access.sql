CREATE OR REPLACE FUNCTION public.restaurant_has_active_subscription(p_restaurant_id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public'
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


CREATE OR REPLACE FUNCTION public.user_can_access_active_restaurant(p_restaurant_id uuid, p_roles text[]) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
  select public.user_has_restaurant_role(p_restaurant_id, p_roles)
     and public.restaurant_has_active_subscription(p_restaurant_id);
$$;
