# Resposta após a abertura da Bia

A síntese da abertura não comprova que o restante da conversa funciona. A sequência ativa é reconhecimento local → embeddings → busca de contexto → resposta estruturada → síntese da resposta. Uma falha depois do reconhecimento pode encerrar a chamada como `falha_tecnica`.

## Compatibilidade do pedido de resposta

O adaptador Chat Completions usa `max_completion_tokens=300`, preservando o modelo configurado, mensagens, contexto e formato JSON. O antigo `max_tokens` pode ser rejeitado com HTTP 400 e `unsupported_parameter` por modelos atuais. A correção não troca o modelo nem adiciona tentativas automáticas.

Esse limite inclui tokens visíveis **e de raciocínio**; não significa 300 palavras disponíveis para a fala. O resultado ainda precisa conter JSON válido, texto não vazio e um resultado comercial permitido. Ausência de resposta não prova aceitação comercial. Não aumente o limite ou altere modelo/credenciais para contornar erro sem autorização do operador.

Referência: [documentação oficial de Chat Completions](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create).

## Testes sem custo de provedor

Na imagem backend, `python -B -m unittest discover -s tests -p 'test_reply.py' -v` usa o SDK real com transporte HTTP falso, sem rede paga. Verifica a serialização do parâmetro moderno com o modelo configurado no cenário e com o padrão da aplicação. O teste de WebSocket mantém a chamada ativa até observar uma segunda emissão de áudio e os turnos `assistant,user,assistant`; somente então envia `stop`. Valida também a ausência de desligamento prematuro e a liberação do slot apenas após callback terminal.

A suíte completa deve usar PostgreSQL descartável. Testes falsos não comprovam aceitação do provedor real: sondagens reais exigem autorização separada, dados fictícios e limites de requisições/tokens. Nunca publique corpo de erro, chave, URL configurada, telefone ou transcrição. Um teste da API de texto não comprova entrega de áudio pela operadora.
