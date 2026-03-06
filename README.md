# mbd-api Backend (API-only)

This repository contains the API backend extracted from `multi-bot-datastore`.  
It serves a restaurant-focused RAG chatbot over HTTP with:

- chunked NDJSON streaming responses
- signed widget token issuance + verification
- origin allowlist enforcement
- per-IP and per-session rate limiting
- optional Supabase-backed chat persistence + retrieval

## Project Files

- `rag-chatbot.py`: main server + request handling + RAG orchestration
- `supabase_store.py`: Supabase PostgREST/RPC adapter
- `requirements.txt`: Python dependencies
- `Dockerfile`: container build definition

## Runtime Overview

The server is built on Python’s `ThreadingHTTPServer` and exposes:

- `GET /healthz`
- `POST /api/widget-token`
- `POST /api/chat-stream`

High-level request flow:

1. Validate request body and headers (`Origin`, required fields).
2. Validate `restaurantId`, load restaurant security settings from Supabase.
3. Enforce origin allowlist for that restaurant.
4. Enforce rate limits (token issuance, IP, session).
5. For chat: verify `widgetToken` (HS256, `kid`, `rid`, `orig`, `exp`).
6. Retrieve relevant chunks from Supabase pgvector.
7. Stream model deltas as NDJSON chunked transfer.
8. Optionally persist session/messages/sources to Supabase.

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
  "widgetToken": "<jwt from /api/widget-token>"
}
```

`sessionToken` is optional; if omitted, server generates one and sends it in first stream event.

Streaming event types:

- `{"type":"session","sessionToken":"...","restaurantId":"...","generated":true|false}`
- `{"type":"delta","content":"..."}`
- `{"type":"done"}`
- `{"type":"error","message":"..."}`

## Environment Variables

### Required

- `OPENAI_API_KEY`
- `WIDGET_SIGNING_KEYS`  
  Format: JSON object (`{"v1":"secret1","v2":"secret2"}`) or CSV (`v1:secret1,v2:secret2`)
- `WIDGET_ACTIVE_KID` (must exist in `WIDGET_SIGNING_KEYS`)

### Retrieval / persistence

- `CHAT_PERSISTENCE=true|false` (default: `true`)
- `SUPABASE_URL` (required)
- `SUPABASE_SERVICE_ROLE_KEY` (required)

### Retrieval tuning

- `MIN_SCORE_DEFAULT` (default `0.0`)

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

## Retrieval Backend

Retrieval is Supabase-only and uses RPC `match_chunks` via `supabase_store.py`.  
`restaurantId` is required on each chat request.

## Supabase Integration

`supabase_store.py` calls PostgREST and RPC endpoints for:

- origin + restaurant validation
- security settings lookup
- audit events
- session upsert
- chat message persistence
- vector retrieval (`match_chunks`)
- ingest run/chunk upserts (utility methods)

Expected backend tables/RPC include (at minimum):

- `restaurants`
- `restaurant_allowed_origins`
- `restaurant_security_settings`
- `audit_events`
- `chat_messages`
- RPC: `upsert_session`, `match_chunks`

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


