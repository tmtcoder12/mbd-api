# mbd-api Backend (API-only)

This repository contains the API backend extracted from `multi-bot-datastore`.  
It serves a restaurant-focused RAG chatbot over HTTP with:

- chunked NDJSON streaming responses
- signed widget token issuance + verification
- origin allowlist enforcement
- per-IP and per-session rate limiting
- Stripe-backed subscription gating for restaurant access
- optional Supabase-backed chat persistence + retrieval

## Project Files

- `rag-chatbot.py`: main server + request handling + RAG orchestration
- `supabase_store.py`: Supabase PostgREST/RPC adapter
- `requirements.txt`: Python dependencies
- `Dockerfile`: container build definition

## Runtime Overview

The server is built on Python’s `ThreadingHTTPServer` and exposes:

- `GET /healthz`
- `POST /api/stripe/webhook`
- `POST /api/widget-token`
- `POST /api/chat-stream`

High-level request flow:

1. Validate request body and headers (`Origin`, required fields).
2. Validate `restaurantId`, confirm the restaurant has an active Stripe subscription, and load restaurant security settings from Supabase.
3. Enforce origin allowlist for that restaurant.
4. Enforce rate limits (token issuance, IP, session).
5. For chat: verify `widgetToken` (HS256, `kid`, `rid`, `orig`, `exp`).
6. Resolve session language (`language` payload -> stored session language -> `eng`) and apply it to the restaurant system prompt.
7. Retrieve relevant chunks from Supabase pgvector.
8. Stream model deltas as NDJSON chunked transfer.
9. Optionally persist session/messages/sources to Supabase.

## API Endpoints

### `GET /healthz`

Returns service health.

Response:

```json
{ "ok": true }
```

### `POST /api/widget-token`

Issues a short-lived signed widget JWT bound to:

- `restaurantId` (`rid` claim)
- request `Origin` header (`orig` claim)

Required:

- `Origin` header
- JSON body:

```json
{
  "restaurantId": "11111111-1111-1111-1111-111111111111"
}
```

Success response:

```json
{
  "widgetToken": "<jwt>",
  "expiresAt": 1760000000
}
```

Requests are rejected with `403` when the restaurant does not have an active Stripe subscription in Supabase.

### `POST /api/stripe/webhook`

Receives Stripe webhook events for subscription lifecycle updates.

Supported event types:

- `checkout.session.completed`
- `customer.subscription.created`
- `customer.subscription.updated`
- `customer.subscription.deleted`

Requirements:

- `Stripe-Signature` header
- raw request body exactly as sent by Stripe

Integration contract:

- Stripe Payment Link URLs must be distributed with `client_reference_id=<restaurant_uuid>` appended
- `checkout.session.completed` uses that `client_reference_id` to map the Stripe checkout back to `public.restaurants.id`

The webhook persists Stripe identifiers and subscription status into Supabase so only restaurants with an `active` subscription can access chat endpoints.

### `POST /api/chat-stream`

Streams chatbot output as `application/x-ndjson` with `Transfer-Encoding: chunked`.

Required:

- `Origin` header
- JSON body:

```json
{
  "message": "What are your most popular dishes?",
  "restaurantId": "11111111-1111-1111-1111-111111111111",
  "sessionToken": "22222222-2222-2222-2222-222222222222",
  "widgetToken": "<jwt from /api/widget-token>",
  "language": "eng"
}
```

`sessionToken` is optional; if omitted, server generates one and sends it in first stream event.
`language` is optional; when omitted, the backend uses the stored session language or defaults to `eng`.

Streaming event types:

- `{"type":"session","sessionToken":"...","restaurantId":"...","generated":true|false}`
- `{"type":"delta","content":"..."}`
- `{"type":"images","images":[{"chunk_id":"...","title":"...","image_url":"...","score":0.0}]}` (optional, model-driven image decision)
- `{"type":"done"}`
- `{"type":"error","message":"..."}`

## Environment Variables

### Required

- `OPENAI_API_KEY`
- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `WIDGET_SIGNING_KEYS`  
  Format: JSON object (`{"v1":"secret1","v2":"secret2"}`) or CSV (`v1:secret1,v2:secret2`)
- `WIDGET_ACTIVE_KID` (must exist in `WIDGET_SIGNING_KEYS`)

### Retrieval / persistence

- `CHAT_PERSISTENCE=true|false` (default: `true`)
- `SUPABASE_URL` (required)
- `SUPABASE_SERVICE_ROLE_KEY` (required)

### Retrieval tuning

- `MIN_SCORE_DEFAULT` (default `0.0`)
- `QUERY_CLASSIFIER_MODEL` (default `gpt-5-mini`)

### Origin policy

- `ALLOW_LOCALHOST_ORIGINS=true|false` (default: `true`)

### Rate limiting defaults (used when per-restaurant settings are absent)

- `RATE_LIMIT_REQUESTS_PER_MINUTE` (IP limit, default: `30`)
- `RATE_LIMIT_WINDOW_SECONDS` (IP window, default: `60`)
- `SESSION_RATE_LIMIT_REQUESTS_PER_MINUTE` (default: `45`)
- `SESSION_RATE_LIMIT_WINDOW_SECONDS` (default: `60`)
- `TOKEN_ISSUE_RATE_LIMIT_REQUESTS_PER_MINUTE` (default: `30`)
- `TOKEN_ISSUE_RATE_LIMIT_WINDOW_SECONDS` (default: `60`)
- `WIDGET_TOKEN_MAX_AGE_SECONDS` (default: `900`)

### Optional distributed limiter

- `RATE_LIMIT_REDIS_URL`  
  If set, Redis sliding-window limiter is used; otherwise in-memory limiter is used.

### Optional query cache (Redis)

- `QUERY_CACHE_ENABLED` (default: `true` when a Redis URL is available)
- `QUERY_CACHE_REDIS_URL` (defaults to `RATE_LIMIT_REDIS_URL` when omitted)
- `QUERY_CACHE_TTL_SECONDS` (default: `900`)
- `QUERY_CACHE_NAMESPACE` (default: `qcache:v1`)
- `QUERY_CACHE_SEMANTIC_THRESHOLD` (default: `0.8`)
- `QUERY_CACHE_SEMANTIC_MAX_CANDIDATES` (default: `200`)
- `QUERY_CACHE_REQUIRE_RESTAURANT_RELEVANCE` (default: `true`)
- `QUERY_CACHE_CLASSIFIER_MODEL` (default: `gpt-5-nano`)
- `QUERY_CACHE_CLASSIFIER_TIMEOUT_MS` (default: `250`)

When query caching is enabled, eligible requests use staged matching:
exact query -> normalized query -> semantic similarity -> normal LLM flow.

Eligibility excludes follow-up/reference-style turns. On cache misses, store decisions are gated by an LLM query classifier (`CACHEABLE` vs `NOT_CACHEABLE`).

## Retrieval Backend

Retrieval is Supabase-only and uses RPC `match_chunks` via `supabase_store.py`.  
`restaurantId` is required on each chat request.

## Supabase Integration

`supabase_store.py` calls PostgREST and RPC endpoints for:

- origin + restaurant validation
- active subscription validation
- security settings lookup
- Stripe subscription upsert + lookup
- Stripe webhook idempotency logging
- audit events
- session upsert
- session state persistence (`chat_session_state`)
- chat message persistence
- async query classification writeback (`chat_messages.query_type`)
- vector retrieval (`match_chunks`)
- ingest run/chunk upserts (utility methods)

Expected backend tables/RPC include (at minimum):

- `restaurants`
- `restaurant_subscriptions`
- `restaurant_allowed_origins`
- `restaurant_security_settings`
- `stripe_webhook_events`
- `audit_events`
- `chat_sessions`
- `chat_session_state`
- `chat_messages`
- SQL function: `restaurant_has_active_subscription`
- SQL function: `user_can_access_active_restaurant`
- RPC: `upsert_session`, `match_chunks`

## Migrations

Apply migrations:

```bash
psql "$DATABASE_URL" -f supabase/migrations/20260402_stripe_subscriptions.sql
psql "$DATABASE_URL" -f migrations/20260317_chat_session_state.sql
psql "$DATABASE_URL" -f migrations/20260328_chat_session_language.sql
```

## Run Locally

Install:

```bash
pip install -r requirements.txt
```

Run server:

```bash
python rag-chatbot.py serve 8 8000
```

Health check:

```bash
curl http://localhost:8000/healthz
```

## Security Notes

- Chat calls are blocked without valid `widgetToken`.
- Tokens are origin-bound and restaurant-bound.
- Expiry is enforced and capped by max age.
- Origin allowlist is enforced per restaurant.
- Audit events are written on origin/rate-limit violations when Supabase is configured.

## Operational Notes

- Streaming format is NDJSON over chunked HTTP/1.1.
- Request IDs are returned in `X-Request-Id`.
- Debug timing logs are enabled (`DEBUG_TIMINGS = True` in code).
- The `Dockerfile` currently starts `backend/rag-chatbot.py`; in this repo layout, the script is at repo root (`rag-chatbot.py`), so update container command if needed.


## Example Usage: 

Health check: curl -i https://mbd-api.onrender.com/healthz

Request Widget Token: 
curl -sS -X POST https://mbd-api.onrender.com/api/widget-token \
  -H "Content-Type: application/json" \
  -H "Origin: http://localhost:8000" \ 
  -d "{\"restaurantId\":\"$RID\"}"

NOTE: In testing and dev, the origin will be local host. In production, that will be the restaurants website. 

Call Chat Stream: 

WIDGET_TOKEN="<paste token from step 2>"

curl -N -X POST https://mbd-api.onrender.com/api/chat-stream \
  -H "Content-Type: application/json" \
  -H "Origin: $ORIGIN" \
  -d "{
    \"message\":\"What are your most popular dishes?\",
    \"restaurantId\":\"$RID\",
    \"widgetToken\":\"$WIDGET_TOKEN\"
  }"
