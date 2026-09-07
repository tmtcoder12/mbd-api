-- Idempotent local/demo bootstrap. Knowledge is loaded by the ingestion CLI.

insert into public.restaurants (id, slug, name, system_prompt)
values (
  '11111111-1111-4111-8111-555555555555',
  'cedar-and-salt',
  'Cedar & Salt',
  E'You are Cedar & Salt\'s restaurant assistant. Respond in {language}. Answer briefly and warmly using only the supplied restaurant context. Never invent prices, ingredients, allergens, hours, policies, or availability. When the context is insufficient, say you are not sure and suggest contacting the restaurant.'
)
on conflict (id) do update set
  slug = excluded.slug,
  name = excluded.name,
  system_prompt = excluded.system_prompt;

insert into public.restaurant_security_settings (restaurant_id)
values ('11111111-1111-4111-8111-555555555555')
on conflict (restaurant_id) do nothing;

insert into public.restaurant_allowed_origins (restaurant_id, origin)
values
  ('11111111-1111-4111-8111-555555555555', 'http://localhost:5173'),
  ('11111111-1111-4111-8111-555555555555', 'http://127.0.0.1:5173')
on conflict (restaurant_id, origin) do nothing;

insert into public.restaurant_subscriptions (restaurant_id, stripe_subscription_status)
values ('11111111-1111-4111-8111-555555555555', 'active')
on conflict (restaurant_id) do update set stripe_subscription_status = 'active';

