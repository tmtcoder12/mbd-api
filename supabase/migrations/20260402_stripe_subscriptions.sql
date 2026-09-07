CREATE TABLE IF NOT EXISTS public.restaurant_subscriptions (
    restaurant_id uuid PRIMARY KEY REFERENCES public.restaurants(id) ON DELETE CASCADE,
    stripe_customer_id text,
    stripe_subscription_id text,
    stripe_payment_link_id text,
    stripe_checkout_session_id text,
    client_reference_id text,
    stripe_price_id text,
    stripe_product_id text,
    stripe_subscription_status text NOT NULL,
    current_period_start timestamp with time zone,
    current_period_end timestamp with time zone,
    cancel_at timestamp with time zone,
    canceled_at timestamp with time zone,
    ended_at timestamp with time zone,
    last_checkout_completed_at timestamp with time zone,
    last_synced_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT restaurant_subscriptions_status_check CHECK (
        stripe_subscription_status = ANY (
            ARRAY[
                'active'::text,
                'trialing'::text,
                'past_due'::text,
                'canceled'::text,
                'unpaid'::text,
                'incomplete'::text,
                'incomplete_expired'::text,
                'paused'::text
            ]
        )
    )
);

ALTER TABLE public.restaurant_subscriptions OWNER TO postgres;

CREATE UNIQUE INDEX IF NOT EXISTS restaurant_subscriptions_stripe_customer_id_idx
    ON public.restaurant_subscriptions USING btree (stripe_customer_id)
    WHERE stripe_customer_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS restaurant_subscriptions_stripe_subscription_id_idx
    ON public.restaurant_subscriptions USING btree (stripe_subscription_id)
    WHERE stripe_subscription_id IS NOT NULL;

CREATE OR REPLACE TRIGGER trg_touch_restaurant_subscriptions_updated_at
    BEFORE UPDATE ON public.restaurant_subscriptions
    FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

CREATE TABLE IF NOT EXISTS public.stripe_webhook_events (
    event_id text PRIMARY KEY,
    event_type text NOT NULL,
    stripe_created_at timestamp with time zone,
    restaurant_id uuid REFERENCES public.restaurants(id) ON DELETE SET NULL,
    stripe_customer_id text,
    stripe_subscription_id text,
    processing_status text DEFAULT 'received'::text NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    error_message text,
    processed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT stripe_webhook_events_processing_status_check CHECK (
        processing_status = ANY (
            ARRAY[
                'received'::text,
                'processed'::text,
                'failed'::text
            ]
        )
    )
);

ALTER TABLE public.stripe_webhook_events OWNER TO postgres;

CREATE OR REPLACE FUNCTION public.restaurant_has_active_subscription(p_restaurant_id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
  select exists (
    select 1
    from public.restaurant_subscriptions rs
    where rs.restaurant_id = p_restaurant_id
      and rs.stripe_subscription_status = 'active'
  );
$$;

ALTER FUNCTION public.restaurant_has_active_subscription(p_restaurant_id uuid) OWNER TO postgres;

CREATE OR REPLACE FUNCTION public.user_can_access_active_restaurant(p_restaurant_id uuid, p_roles text[]) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
  select public.user_has_restaurant_role(p_restaurant_id, p_roles)
     and public.restaurant_has_active_subscription(p_restaurant_id);
$$;

ALTER FUNCTION public.user_can_access_active_restaurant(p_restaurant_id uuid, p_roles text[]) OWNER TO postgres;

ALTER TABLE public.restaurant_subscriptions ENABLE ROW LEVEL SECURITY;

CREATE POLICY restaurant_subscriptions_select_manager ON public.restaurant_subscriptions
    FOR SELECT TO authenticated
    USING (public.user_has_restaurant_role(restaurant_id, ARRAY['owner'::text, 'manager'::text]));

CREATE POLICY service_role_restaurant_subscriptions_all ON public.restaurant_subscriptions
    USING ((auth.role() = 'service_role'::text))
    WITH CHECK ((auth.role() = 'service_role'::text));

ALTER TABLE public.stripe_webhook_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY service_role_stripe_webhook_events_all ON public.stripe_webhook_events
    USING ((auth.role() = 'service_role'::text))
    WITH CHECK ((auth.role() = 'service_role'::text));

GRANT ALL ON FUNCTION public.restaurant_has_active_subscription(p_restaurant_id uuid) TO anon;
GRANT ALL ON FUNCTION public.restaurant_has_active_subscription(p_restaurant_id uuid) TO authenticated;
GRANT ALL ON FUNCTION public.restaurant_has_active_subscription(p_restaurant_id uuid) TO service_role;

GRANT ALL ON FUNCTION public.user_can_access_active_restaurant(p_restaurant_id uuid, p_roles text[]) TO anon;
GRANT ALL ON FUNCTION public.user_can_access_active_restaurant(p_restaurant_id uuid, p_roles text[]) TO authenticated;
GRANT ALL ON FUNCTION public.user_can_access_active_restaurant(p_restaurant_id uuid, p_roles text[]) TO service_role;

GRANT ALL ON TABLE public.restaurant_subscriptions TO anon;
GRANT ALL ON TABLE public.restaurant_subscriptions TO authenticated;
GRANT ALL ON TABLE public.restaurant_subscriptions TO service_role;

GRANT ALL ON TABLE public.stripe_webhook_events TO service_role;
