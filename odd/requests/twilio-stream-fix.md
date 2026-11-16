# Twilio media establishment correction

Human incident report: "cadastrei o produto, o lead e fiz e rodei o ligação. Recebi a ligação no telefone porém quando respondi fecho a ligação, preciso que valide o que aconteceu, não tenho informações de observabilidade dessa ligação".

The human answered "sim" to correcting WebSocket signature validation and the observability gap using local tests without placing another call. The real incident had human detection, successful signed callbacks, rejected WebSocket HTTP403, no bound stream or transcript, and a received recording.

This work unit adds only Twilio's documented same-path trailing-slash validation compatibility to the configured external handshake URL. HTTP callback validation, configured scheme/origin/prefix/raw query, signatures and SID binding remain mandatory; arbitrary Host, forwarded-header, scheme or query fallback is not intended. The actual provider signing URI and live resolution were not verified; no claim of a proven successful real call is made.

Unauthenticated handshake rejection must not mutate calls or write per-call records. Static rejection logging is bounded. A trusted signed terminal callback and locked stored human/no-stream state generate one idempotent media_stream_not_established audit event and a technical outcome where justified. Explicit guarded backfill may add the derived diagnostic for the old incident without rewriting its original outcome/history. No GET-side backfill, invented token/cost values, environment-file access, new paid calls or weakening of prior callback/stream/recording invariants is authorized.
