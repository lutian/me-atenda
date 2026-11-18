# Model catalog and per-model costs
Objective: operator-managed AI models (text, audio, embeddings) with costs, selectable per option in Settings. Branch: main (local commits; push is the user's decision). Strategy: ask-on-risk. Runner: backend unittest (+ disposable PostgreSQL), frontend unittest in the frontend image.

## Specs
S1. "os modelos deve ser possível cadastrar eles, porque todo dia sai um novo modelo, assim que tem que ter uma tabela de modelos e outra de custos por modelo" — two tables: models and costs per model.
S2. "primero consigo cadastrar modelos novos (e modificar ou apagar) e a qual opção pertence (textos, áudios, embeddings)" — CRUD of models with kind text | audio | embedding. Assumption: the model in use for an option cannot be deleted nor change kind.
S3. "depois para cada opção (mensagens em texto, áudios e embeddings) poder escolher o modelo e definir os custos" — Settings selects the active model per option; costs are edited per model. Twilio rates stay in Settings (previous feature).
S4. "crie as duas tabelas e depois preencha as informações dos modelos atuais e adicione o modelo gpt-6.2-sol com os preços" — seed gpt-6.1-sol, gpt-6.2-sol, text-embedding-3-small, tts-1 with official prices (developers.openai.com/api/docs/pricing, read 2026-11-18): gpt-6.x-sol input 2.00 / cached 0.10 / output 10.00 per 1M tokens; text-embedding-3-small 0.02 per 1M tokens; tts-1 15.00 per 1M characters.
S5. "trocar sem recalcular, os produtos que não tem os embeddings atualizados ficam indisponíveis até atualizar os embeddings (seria um novo botão que só aparece quando precisa regerar os embeddings) mas acho que o produto tem que ter um campo a mais para atrelar o modelo de embeddings" — products store their embedding model; outdated products cannot be drafted nor dialed; an "Atualizar embeddings" button appears only for them.

## Tasks
T1 | S1-S4 | catalog + costs tables, CRUD API, active selection, runtime switch, pricing from catalog, seed | inline | done (RED→GREEN, PG) | c5c86ed
T2 | S5 | product embedding model binding, outdated guard, re-embed endpoint | inline | done (RED→GREEN, PG) | d7e1444
T3 | S2,S3,S5 | frontend: models section, selection, costs, products button, dispatch block; README/docs | inline | done (RED→GREEN) | 285a106

## Log
L1 (2026-11-18, user, verbatim): "os modelos deve ser possível cadastrar eles, porque todo dia sai um novo modelo, assim que tem que ter uma tabela de modelos e outra de custos por modelo, então primero consigo cadastrar modelos novos (e modificar ou apagar) e a qual opção pertence (textos, áudios, embeddings) e depois para cada opção (mensagens em texto, áudios e embeddings) poder escolher o modelo e definir os custos. crie as duas tabelas e depois preencha as informações dos modelos atuais e adicione o modelo gpt-6.2-sol com os preços"
L2 (2026-11-18, user answer, verbatim): "trocar sem recalcular, os produtos que não tem os embeddings atualizados ficam indisponíveis até atualizar os embeddings (seria um novo botão que só aparece quando precisa regerar os embeddings) mas acho que o produto tem que ter um campo a mais para atrelar o modelo de embeddings"
L3 Design: ai_models(id, name unique, kind) + model_costs(model_id PK FK cascade, NUMERIC rates) + active_models(kind PK, model_id FK restrict). Env models are only the initial selection (inserted at startup when a kind has no selection). Selection mutates the shared Settings so Providers use it on the next request. Recorder prices = catalog costs + Twilio rates. products.embedding_model_id FK (SET NULL on delete); migration backfills existing products with text-embedding-3-small, the only embedding model ever supported.
L4 (2026-11-18) T1-T3 implemented RED→GREEN; frontend Settings: models table, register/edit/delete, selection per option, Twilio rates; Products: "Atualizar embeddings" only for outdated products; Leads blocks outdated products.
