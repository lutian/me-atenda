# Backend: outbound voice agent

Python 3.11 is the supported runtime (uses stdlib `audioop`). Direct dependencies
are pinned in `requirements.txt`; no global install is needed. The Docker build
context is `backend/`. The image runs as UID 10001, applies the transactional,
idempotent initial migration and starts Uvicorn. Mount a writable model cache at
`/models`; faster-whisper downloads its open-source model on first use. For an
air-gapped deployment, preload that cache. CPU/int8 is the default.

## Configuration and startup

Set these environment variables through your local container orchestrator, never
commit credentials or copy credentials from the reference project:

- `DATABASE_URL`: `postgresql+psycopg://outbound:PASSWORD@postgres:5432/outbound`.
  PostgreSQL **with pgvector** is required; SQLite is not a supported fallback.
- `ADMIN_API_KEY`: long random server-side key. Send as `X-API-Key` for all REST
  resources including audio. Keep it out of browser JavaScript and public URLs.
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` (E.164).
- `PUBLIC_BASE_URL`: exact HTTPS public origin, optionally with a path prefix,
  no trailing slash, query or fragment. Example: `https://voice.example/agent`.
- `OPENAI_API_KEY`, `OPENAI_CHAT_MODEL=gpt-4o-mini`,
  `OPENAI_EMBEDDING_MODEL=text-embedding-3-small` (1536 dimensions),
  `OPENAI_TTS_MODEL=tts-1`, `OPENAI_TTS_VOICE=alloy`.
- `WHISPER_MODEL=base`, `WHISPER_LANGUAGE=pt`, `WHISPER_DEVICE=cpu`,
  `WHISPER_COMPUTE_TYPE=int8`.

Explicit migration: `python -m app.store` from `/app` (or `backend/` locally).
Runtime: `uvicorn app.main:app --host 0.0.0.0 --port 8000 --ws-max-size 8192 --ws-max-queue 4`.
`GET /health` is public and returns 503 until the database and schema are ready.
Interactive docs and OpenAPI are disabled on the public deployment.

Terminate TLS at your reverse proxy; forward WebSocket upgrade headers and strip
only the configured public prefix before routing to the backend. Preserve the
path encoding and raw query order. Do not rewrite callback trailing slashes.
Twilio signs the **HTTPS** WebSocket upgrade URL, not `wss://`. Signature checks
construct the exact URL from `PUBLIC_BASE_URL` and the raw routed path/query,
never from caller-controlled Host/forwarded headers. Configure proxy request
limits (32 KiB for callbacks, 2 MiB for REST), timeouts and rate limits too.

## Frozen API

All lists are plain JSON arrays and IDs are integers. Errors use FastAPI's JSON
`detail` envelope. Validation 422, missing IDs/audio 404, conflicts 409, provider
failures 503, invalid admin key 401, bad callback signature 403.

- `GET/POST /products`, `PATCH/DELETE /products/{id}`; input `{name,description}`.
  PATCH accepts a subset, not null values. Chunk embeddings are generated before
  an atomic transaction replaces product text and all vectors. Provider failure
  leaves existing product/chunks unchanged.
- `GET/POST /leads`, `PATCH/DELETE /leads/{id}`; input `{name,phone,company}`
  (`company` defaults to empty). E.164 phones only. Historical referenced records
  cannot be deleted. `POST /leads/import` accepts 1–1000 lead objects in a JSON
  array, validated completely before one atomic insert transaction.
- `POST /calls` with `{lead_id,product_id}` reserves the single active slot in
  PostgreSQL before contacting Twilio. HTTP 201 returns the call object.
- `GET /calls`, `GET /calls/{id}` return
  `{id,lead_id,product_id,status_final,duration_seconds,audio_url,transcription_json,recording_status}`.
  Turns are ordered `{role,content}` records; audio_url is null until available.
- `GET /calls/{id}/audio` returns authenticated WAV bytes (never a public static
  file). `recording_status` is `pending`, `available`, `failed` or `absent`.

Twilio URLs are generated automatically per call: `POST /twilio/voice/{id}`,
`POST /twilio/status/{id}`, `POST /twilio/recording/{id}`, and
`/twilio/stream/{id}` WebSocket. Do not manually change them in the console.
Calls use synchronous `DetectMessageEnd`, call recording and a 600-second carrier
limit. Only signed AMD `human` authorizes a media stream; machine outcomes hang
up as `caixa_postal`, fax maps to `numero_errado`, AMD unknown is **not** IVR.
Invalid-number carrier error codes map separately. Carrier `completed` by itself
means `inconclusivo`, never commercial success. Structured LLM classifications
are restricted to the documented statuses; explicit agreement is required for
`sucesso`, and explicit IVR evidence for `atendimento_automatico`.

## Integration observability (S18)

All three new endpoints require the same server-only `X-API-Key`:

- `GET /observability/events?call_id=1&limit=100`: plain array, newest first.
  `call_id` is optional (omission shows all scopes), `limit` defaults to 100 and
  must be 1–200. Events include provider, operation, model, timestamp, elapsed
  milliseconds, success/error/cancelled status, static sanitized errors, safe
  request ID, nullable input/output/cached/total tokens, characters, nullable
  decimal-string `estimated_cost_usd`, cost status and immutable pricing snapshot.
- `GET /observability/summary?call_id=1`: all matching events, not just the first
  history page. Returns known token totals, `unknown_usage_events`, request/error
  counts, USD estimate/subtotal, excluded components and separate native-currency
  `billed_connectivity`. Without `call_id`, billed connectivity is not applicable.
- `POST /calls/1/billing/reconcile`: explicitly queries the **stored** call SID
  using the server's Twilio account. Does not dial, accept a URL/price from the
  caller, follow redirects, or perform automatic retries. Returns the connectivity
  billing object (`pending`, `available`, `failed`, or `not_applicable`). Missing
  call is 404; unbound SID is pending. Provider failures are sanitized; an available
  prior amount survives a failed/pending refresh. Repeat identical results are
  idempotent. Billed connectivity replaces the previous billed record, never adds
  itself to an estimate. Twilio prices arrive asynchronously and cover connectivity
  only, not the complete bill. Validated negative Twilio debit amounts become
  nonnegative charges; positive credits or invalid currency/signs are rejected,
  never converted using arbitrary absolute values. There is no FX conversion.

Set optional `OBSERVABILITY_PRICING_JSON` through operator-managed configuration.
Empty/absent means **unknown costs**, never zero. Invalid config fails startup
with a static message. Example below is a schema illustration with deliberately
fictional rates, **not provider pricing**:

```json
{"version":"operator-v1","currency":"USD","models":{"gpt-4o-mini":{"input_per_million":"1.00","cached_input_per_million":"0.50","output_per_million":"2.00"},"text-embedding-3-small":{"input_per_million":"0.10"},"tts-1":{"characters_per_million":"3.00"}},"twilio":{"voice_per_minute":"0.10","amd_per_call":"0.02"}}
```

Only nonnegative finite decimal **strings** are accepted. Models use their exact
configured names; no mutable live rate is hardcoded. OpenAI retries are disabled
so each observed attempt is accounted once. Usage is captured before chat JSON
validation; malformed output still retains actual returned usage. Cached input
is a subset of prompt input, never added to total tokens. Missing usage remains
null on events; integer summary totals sum only known values and disclose missing
usage. Optional missing cached usage remains null; a discounted cached rate with
unknown cached coverage prevents a fabricated exact estimate. `tts-1` remains
configured (no silent model migration) and uses input character estimates only
once a provider response has arrived, not invented chat tokens. Local Whisper
records time/failure/cancellation but no invented provider USD or tokens. Product
embeddings are global; utterance/query embeddings and other call work are call
scoped, so product ingestion is not charged again on each call.

Twilio rates freeze at call reservation before dialing, including the configured
version. Terminal signed callbacks maintain one stable connectivity estimate per
call, not one charge per callback. Voice units are **ceil(duration_seconds / 60)**
whole minutes; AMD is one configured per-call unit when positive answered duration
is available. No positive duration evidence means unknown, not a zero-dollar bill.
Destination/carrier billing rules may differ; this is an estimate, not a tariff
engine. Missing voice/AMD rates remain unknown components. Recording, storage,
Streams, infrastructure and taxes are explicitly excluded/unknown, never silently
priced. A known subtotal is therefore labelled **partial**, not a complete bill.
Estimates are USD only; reconciled validated native currency remains separate.
Known OpenAI token totals are not a total for TTS/telecom/local STT.

The additive `002_observability.sql` migration preserves existing001 data and
invariants; the migration runner applies ordered SQL files under the existing
transaction/advisory lock and is safe to reapply. Ledger requests first persist
an interrupted marker before network/inference work; finalization is idempotent.
A crash/cancellation before finalization leaves visible `interrupted` evidence,
not an invented success. Final persistence failure retains that marker and emits
only a static `accounting_finalize_failed` message with event ID and safe numeric
usage/cost fields, never exception text/bodies/phones/credentials. It does not
replace a definitive dial result/rejection with accounting uncertainty. Failure
to freeze accounting before dialing returns 503 without dialing and conservatively
retains the reservation for operator reconciliation. Events never store prompts,
transcripts, phone numbers or token headers. Restrict ledger access/retention as
operational metadata; do not enable provider HTTP debug/body logging.

## Lifecycle, recovery and limits

A partial unique PostgreSQL index serializes active calls across processes.
Row locks protect SID binding, terminal outcomes, recording updates and ordered
transcript appends. An AMD/LLM outcome or WebSocket loss does **not** release the
slot; only terminal carrier status or definitive rejected creation does. Late ringing/duplicate callbacks never
regress terminal outcomes. The first stream is exclusively bound to the stored
call SID and stream SID, after the human gate. Reconnects are intentionally denied.

A definitive Twilio 4xx creation rejection with a valid error envelope releases
its reservation through `Store.finish`. Invalid-number codes (including 21211)
map to `numero_errado`, with HTTP 422 and detail
`Twilio rejected call creation: numero_errado`. Other definitive rejections map
to `falha_tecnica` and HTTP 503. Uncertain responses (including timeouts, 408,
malformed error envelopes or binding failures after acceptance) retain the slot.
A timeout during dialing can mean Twilio accepted the call, so the API returns
503 and conservatively retains the slot. Missing terminal callbacks can also
retain it. Before any operator database repair, verify that the corresponding
Twilio call is terminal (or that no call was created), reconcile its SID/status,
and use the same transactional state helper `Store.finish`; never clear a slot
while a carrier call may still be active. There is no automatic redial/campaign
loop and no unsafe automatic timeout release.

Recording callbacks are asynchronous, so history may show `pending` after hangup.
Only fixed `api.twilio.com` account/recording-SID paths are fetched with Basic
Auth; callback `RecordingUrl` is ignored, redirects denied, WAV validated, and
32 MiB maximum enforced. Failure is `failed` + HTTP 503, not a fabricated audio
link. Replay the original valid signed callback to retry after resolving the
provider failure; terminal recordings are idempotent. `absent` means Twilio
produced none. Missing callbacks remain pending and require operator follow-up.
Recordings beyond the cap deliberately fail rather than fill database memory.

Inbound mu-law/8kHz becomes PCM/16kHz for **local faster-whisper**, not OpenAI STT.
Silence/RMS boundaries collect utterances (600ms silence, 20s maximum), inference
runs off the event loop, queue capacity is two utterances, and overload fails
closed with a carrier hangup. Streams have 30s idle and 5min absolute limits;
individual frames and ASGI WebSocket queues are bounded. RAG embeds each user
utterance and retrieves the nearest chunks filtered by the selected product.
Assistant speech is OpenAI PCM/24kHz converted to raw mu-law/8kHz and paced in
20ms frames. Turns are persisted incrementally before subsequent provider work.
This is **utterance streaming**, not full-duplex recognition; model warmup, CPU
inference and network providers add latency. There is no barge-in/echo cancellation.
Stop and disconnect share a 45-second drain of all accepted utterances, even
when carrier completion has already made the old call inactive. Completion
suppresses further sales replies, not transcription. Queue completion accounting
replaces stop sentinels, so a full queue cannot strand sentinel insertion. Cleanup
updates only the bound old call; it never releases a newer call's reservation.
Final accepted utterances on a normal stop are drained with a bounded wait;
unreceived audio or an inference failure cannot be represented as invented text.
Whisper work already executing cannot be forcibly interrupted on socket loss;
its serialization lock remains held until completion.

## Offline tests and missing environmental proof

From the repository root, without credentials or live providers:

```
cd backend && python3 -B -m unittest discover -s tests -p test_api.py -v
cd backend && python3 -B -m unittest discover -s tests -p test_stream.py -v
cd backend && python3 -B -m unittest discover -s tests -p test_adapters.py -v
cd backend && python3 -B -m unittest discover -s tests -p test_postgres.py -v
cd backend && python3 -B -m unittest discover -s tests -p test_corrections.py -v
cd backend && python3 -B -m unittest discover -s tests -p 'test_observability*.py' -v
cd backend && python3 -B -m unittest discover -s tests -v
```

Use the image's pinned environment for reproducibility. Tests never call Twilio,
OpenAI or download Whisper models. `test_postgres.py` requires an explicitly
supplied **disposable** `TEST_DATABASE_URL` and installed psycopg. It creates a
unique test schema, removes only that schema in teardown, and exercises concurrent
public API requests against actual PostgreSQL/pgvector. Without that URL it skips:
**mocks do not prove PostgreSQL locking**. The migration needs extension privileges
on first application. `test_observability_postgres.py` additionally checks ledger
restart persistence/aggregation, an existing001-only schema upgrade and migration
reapplication, immutable rate snapshots, concurrent callback estimate deduplication
and concurrent idempotent native billing reconciliation. These are skipped without
`TEST_DATABASE_URL`; the parent owns actual container/database execution and image
builds. Offline tests do not prove Twilio interoperability, voice
quality, local model accuracy, Docker integration or live recording delivery.

## Consent, privacy and operational safety

Obtain lawful permission and check local telemarketing, do-not-call and recording
consent rules **before dialing**. Use only authorized test recipients. The opening
identifies the AI and announces recording, but Twilio recording starts at answer:
this notice alone does not satisfy all jurisdictions' prior-consent rules. Configure
a lawful consent process before using this system. Refusals should end the call.
Transcripts/audio contain personal information: restrict database access, encrypt
storage/backups, define retention/deletion, and do not expose PostgreSQL publicly.
No live call or paid-provider validation is implied by the offline tests.

Reference patterns inspected read-only: `flig.tech/backend/app/routers/voice.py`
(outbound Twilio callbacks/TwiML) and `services/voice_service.py` (AsyncOpenAI and
Basic-auth recording retrieval). That source had Record/Play rather than media
WebSockets/AMD; the new bounded stream protocol replaces its Gemini/Record flow.
No reference credentials, state, audio or database content were copied.
