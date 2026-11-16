# Saudação imediata e voz preparada

## O que muda

Um disparo explícito prepara “Alô?” e a apresentação da Bia **antes da discagem**. O cache persistente no PostgreSQL usa texto exato, modelo TTS, voz e formato versionado. Alterar a descrição não invalida a fala; mudar um desses dados falados cria outra identidade. A apresentação e o áudio ficam fixados na reserva da chamada, mesmo se o produto for editado depois.

Ao atender, a Twilio executa o áudio genérico sem aguardar fala do lead nem AMD. `AsyncAmd=true` mantém `DetectMessageEnd` em paralelo. Uma máquina pode ouvir “Alô?”, mas somente o resultado humano autenticado libera a apresentação comercial inalterada:

> Olá, meu nome é Bia, sou Representante de {título do produto}, gostaria de apresentar para vocês o nosso produto. É com você que posso falar?

O gate espera em passos de um segundo, com prazo de 35 segundos desde a emissão inicial e limite finito de consultas. Retentativas não reiniciam o prazo nem repetem o “Alô?”. Máquina, fax, desconhecido e expiração não recebem apresentação. Apenas o callback terminal libera a chamada ativa.

## Exposição pública mínima

| Rota | Controle |
|---|---|
| `POST /twilio/voice/{id}` | Assinatura Twilio e SID; emite saudação e redireciona. |
| `POST /twilio/amd/{id}` | Assinatura e SID; primeira classificação válida prevalece. |
| `POST /twilio/gate/{id}` | Assinatura e SID; espera limitada ou stream humano. |
| `GET /media/hello/{digest}.wav` | Anônimo, exclusivamente áudio pronto cujo texto é “Alô?”, digest válido, WAV imutável. |

Mantenha também os callbacks de status/gravação e o upgrade WSS já existentes. O proxy precisa permitir **essas rotas exatas**, preservando origem/prefixo/query e o upgrade. A configuração anterior, limitada a `voice/status/recording/stream`, bloqueia a nova função. O exemplo Nginx completo está no README raiz. Não libere `/media/` inteiro ou rotas administrativas.

A saudação genérica não contém dados pessoais e não exige a chave administrativa. Hashes de apresentação, itens pendentes, gravações, arquivos ou caminhos arbitrários continuam inacessíveis por essa rota. As gravações permanecem autenticadas. Não adicione login interativo aos callbacks nem headers que a Twilio não envia.

## Custos, falhas e evidência

- Preparação nova usa o TTS existente e registra consumo **global**, antes da chamada; primeiro disparo pode demorar antes de tocar. Não se repete cobrança/evento por reutilizar ou baixar o áudio. Respostas da conversa continuam usando TTS normalmente.
- A exclusão no PostgreSQL impede preparação concorrente; pedido perdedor recebe conflito sem discar. A restrição de uma chamada ativa permanece.
- Falha antes da discagem não deixa slot ativo. Preparação ambígua conserva marcador pendente, impedindo repetição automática de TTS possivelmente cobrado. Exige investigação/reconciliação explícita; não apague marcadores nem repita às cegas.
- Após começar a discagem, timeout pode significar chamada criada: confira histórico e operadora antes de repetir. Não libere slot manualmente sem provar encerramento.
- “Alô?” é emissão de instrução, **não confirmação de reprodução**; não se inventa turno ou recibo de entrega. Use a gravação autenticada para evidência. Rede/cache/AMD podem atrasar ou falhar; não há garantia de latência zero ou classificação perfeita.
- O cache de fala fica no banco, não no volume Whisper. Migrações aditivas `003` e `004` preservam histórico/gravações; valide backup/restauração e ausência de chamadas ativas antes de atualizar. Não remova volumes.
- A saudação/apresentação não substitui avisos de IA/gravação ou consentimento/base legal. A gravação continua habilitada e o operador continua responsável pelo cumprimento das regras aplicáveis.

## Verificação sem cobrança

Use somente PostgreSQL descartável, provedores falsos e rede bloqueada para integrações externas. A suíte backend cobre preparação/cache, produto editado, cancelamento, concorrência entre apps, callbacks assinados, limites de espera, bytes WAV, migrações, WSS local e drenagem de falas aceitas. Isso não dispara chamada, não mede entrega real e não prova qualidade de voz com a operadora. Teste pago continua sendo decisão do operador.
