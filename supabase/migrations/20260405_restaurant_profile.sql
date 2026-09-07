ALTER TABLE public.restaurants
ADD COLUMN IF NOT EXISTS restaurant_profile jsonb;
