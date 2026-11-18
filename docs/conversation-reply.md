# Resposta após a abertura da Bia

A síntese da abertura não comprova que o restante da conversa funciona. A sequência ativa é reconhecimento local → embeddings → busca de contexto → resposta estruturada → síntese da resposta. Uma falha depois do reconhecimento pode encerrar a chamada como `falha_tecnica`.

## Compatibilidade do pedido de resposta

O adaptador Chat Completions usa `max_completion_tokens=300`, preservando o modelo configurado, mensagens, contexto e formato JSON. O antigo `max_tokens` pode ser rejeitado com HTTP 400 e `unsupported_parameter` por modelos atuais. A correção não troca o modelo nem adiciona tentativas automáticas.

Esse limite inclui tokens visíveis **e de raciocínio**; não significa 300 palavras disponíveis para a fala. O resultado ainda precisa conter JSON válido, texto não vazio e um resultado comercial permitido. Ausência de resposta não prova aceitação comercial. Não aumente o limite ou altere modelo/credenciais para contornar erro sem autorização do operador.

Referência: [documentação oficial de Chat Completions](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create).

## Testes sem custo de provedor

Na imagem backend, `python -B -m unittest discover -s tests -p 'test_reply.py' -v` usa o SDK real com transporte HTTP falso, sem rede paga. Verifica a serialização do parâmetro moderno com o modelo configurado no cenário e com o padrão da aplicação. O teste de WebSocket mantém a chamada ativa até observar uma segunda emissão de áudio e os turnos `assistant,user,assistant`; somente então envia `stop`. Valida também a ausência de desligamento prematuro e a liberação do slot apenas após callback terminal.

A suíte completa deve usar PostgreSQL descartável. Testes falsos não comprovam aceitação do provedor real: sondagens reais exigem autorização separada, dados fictícios e limites de requisições/tokens. Nunca publique corpo de erro, chave, URL configurada, telefone ou transcrição. Um teste da API de texto não comprova entrega de áudio pela operadora.

## Fala do lead durante uma resposta lenta

Enquanto a Bia processa ou fala, as falas do lead esperam numa fila de duas posições. Uma terceira fala não encerra mais a chamada: as falas pendentes são unidas em ordem, mantendo os últimos 20 segundos, e transcritas juntas. Antes, a chamada 7 caiu como `falha_tecnica` porque o lead repetiu "Olá?" durante uma pausa de cerca de 18 segundos.

Toda `falha_tecnica` do stream registra `media_stream_failed <tipo>` no log. Mensagens próprias de protocolo (`ValueError`, `TimeoutError`) aparecem com até 80 caracteres; erros de provedores registram só o tipo, porque podem conter URLs ou dados.

Na inicialização, o backend carrega o modelo Whisper em segundo plano. Assim, a primeira transcrição não paga cerca de 9 segundos de carga. Se a carga falhar, o log registra `whisper_warmup_failed <tipo>` e o modelo é carregado na primeira fala, como antes.

As respostas não reapresentam a Bia nem repetem o que a abertura já disse, a menos que o cliente pergunte.

## Voz e nome do agente

Em **Configurações → Agente de voz** o operador escolhe a voz do TTS e o nome do agente (padrão: Bia). A lista vem de `GET /settings/agent` e depende do modelo: `tts-1`/`tts-1-hd` aceitam alloy, ash, coral, echo, fable, onyx, nova, sage e shimmer; os modelos mais novos aceitam também ballad, verse, marin e cedar. Sem escolha salva, vale `OPENAI_TTS_VOICE`.

`PATCH /settings/agent` exige `agent_name` (1 a 40 caracteres) e `tts_voice` da lista. Cada chamada registra o nome e a voz no momento da reserva. Mudanças posteriores valem só para as próximas chamadas; a abertura, o prompt e as respostas ao vivo usam o registro da chamada. Como o áudio preparado é indexado por texto e voz, trocar nome ou voz gera áudios novos.

## Mensagens preparadas antes da ligação

Em **Leads → Disparo unitário**, depois de escolher o produto:

1. **Gerar texto com IA** (`POST /products/{id}/messages/draft`) sugere a apresentação a partir do nome e da descrição do produto. A despedida vem preenchida com "Entendi, obrigado pelo seu tempo.". Nada é salvo nem sintetizado nesse passo.
2. O operador revisa os dois textos e clica em **Salvar e gerar áudios** (`PUT /products/{id}/messages`, apresentação de 1 a 400 caracteres e despedida de 1 a 200). O backend salva os textos e sintetiza, com a voz do agente, "Alô?", a abertura, a apresentação e a despedida. Áudios que já existem no cache (`opening_audio`) são reaproveitados.
3. **Iniciar UMA ligação** só fica habilitado quando `GET /products/{id}/messages` responde `ready: true`.

Trocar a voz ou o nome do agente faz `ready` voltar a `false`, porque os áudios são indexados por texto e voz. Editar o produto apaga os textos, que precisam ser revisados de novo. Se a síntese falhar, os textos ficam salvos, `ready` continua `false` e vale a regra de reconciliação manual do cache. A API ainda aceita discar sem mensagens preparadas, para manter o contrato de `POST /calls`; nesse caso a chamada não tem apresentação nem despedida prontas.

## Respostas curtas e mensagens preparadas na conversa

O prompt pede uma única frase de até 20 palavras. Quando a chamada tem apresentação e despedida preparadas, o modelo pode responder `"script": "pitch"` ou `"script": "goodbye"` em vez de texto. Nesse caso a chamada toca o áudio gravado, sem esperar o TTS:

- `pitch`: oferecido uma vez só, quando a pessoa aceita conversar;
- `goodbye`: usado em recusa ou contato errado, com `sem_interesse` ou `numero_errado`; sem classificação vira `inconclusivo`. Depois dele a ligação é encerrada.

Um script não oferecido é ignorado, e aí a resposta precisa trazer texto próprio. Sem mensagens preparadas, o prompt manda usar sempre `script: null`.

## "Alô" no meio da conversa

Quando o lead fala só cumprimentos ou hesitações enquanto espera (alô, oi, olá, hello, hum, ah, eh, ei; até 6 palavras, sem diferenciar acentos ou maiúsculas), a fala fica na transcrição para auditoria, mas não recebe resposta e não vai para o LLM. Assim, cada "Alô?" dito durante uma espera não vira um turno novo respondido fora de hora. Uma frase com conteúdo, como "Oi, sim. Alô.", é respondida normalmente.
