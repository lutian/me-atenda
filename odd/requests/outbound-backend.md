# Outbound backend work-unit request

Scope: initial backend service in commit 7645faf, based on odd/tasks/outbound-agent.md, T1/T5. The frontend, root Compose and operator walkthrough are a following integration-chain unit, not part of this backend commit. The reference project is read-only; environment artifacts are human-managed and excluded.

## Exact human requirements applicable to this unit

- "Endpoints para gerenciar produtos/serviços (com salvamento de embeddings no PostgreSQL para RAG)."
- "Endpoints para gerenciar a tabela de leads e disparar chamadas individuais via Twilio."
- "Configuração de Webhook da Twilio utilizando AMD (Answering Machine Detection) nativo para identificar caixas postais, PABX virtuais e números inválidos, mapeando os status: `sucesso`, `sem_interesse`, `caixa_postal`, `numero_errado`, `atendimento_automatico`, etc."
- "Engine de voz em tempo real (reaproveitando a estrutura de WebSocket/Streams existente) integrando STT, LLM com contexto RAG do produto e TTS."
- "Salvamento obrigatório do fluxo completo da conversa (transcrição JSON) e áudio da chamada no banco de dados ao término da ligação."
- "Para ouvir a resposta do cliente vamos utilizar whisper local que é open source."
- "tem que ter observabilidade de erros e custos das integrações, quantos tokens consome cada requisição e qual foi o total consumido (tokens e preço) de cada ligação"

## Accepted engineering and pricing decisions

REST administration uses server-side X-API-Key. Public Twilio HTTP and WebSocket upgrades validate signatures against the exact configured public URL and bind stored call/stream identifiers. Synchronous DetectMessageEnd gates human speech; unknown AMD is not evidence of IVR. PostgreSQL atomically permits one active call; callbacks are terminal-monotonic and duplicate-safe. Ambiguous dial creation retains its reservation; definitive rejected creation may release it. Accepted queued utterances drain and persist after completion without new sales speech. Local inference is bounded utterance processing off the event loop, not a promise of full-duplex recognition. RAG filters by product. Recording callback/download may lag; unavailable audio is explicitly pending/failed/absent, never fabricated, and fetches use authenticated fixed Twilio resources.

The human selected estimated_immediate_billed_separate: actual returned OpenAI usage, versioned configurable estimated USD, and separate provider-native billed Twilio connectivity. Cached tokens are a subset, TTS uses characters, local Whisper has no invented provider charge. Missing usage/rates remain unknown; subtotals with exclusions remain partial. Product ingestion is global rather than recharged to every call. Raw provider bodies, credentials, phone numbers and conversation content do not belong in error events/logs. Reconciliation is authenticated, explicit, SID-bound and idempotent; billed native currency is not added to USD estimates. Pricing configuration is optional and has no hardcoded live rate. Migrations are additive and preserve existing call/transcript/audio records.

## Operational boundary

Python 3.11 containers, FastAPI, PostgreSQL/pgvector, local faster-whisper, OpenAI chat/embeddings/TTS and Twilio are the selected stack. Live calls, paid provider tests, actual Whisper model accuracy and public tunnel interoperability were not authorized as verification actions. Offline provider doubles, actual disposable PostgreSQL checks and independent built-image verification were used; they do not establish final invoice completeness or live voice quality.
