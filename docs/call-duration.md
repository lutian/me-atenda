# Duração máxima das chamadas

O padrão para novas reservas é **180 segundos (3 minutos)**. A política fica no singleton `call_policy`, no PostgreSQL, e não em credenciais ou configurações pessoais. Atualizações não modificam chamadas já reservadas.

## Configuração no Streamlit

Abra **Configurações** no menu lateral. O campo **Duração máxima da ligação (segundos)** carrega o valor persistido; 180 significa3minutos. Altere e clique em **Salvar limite**. A página não salva automaticamente nem inicia chamadas ao renderizar, e mudanças só valem para novas reservas.

Se a leitura falhar, o formulário não aparece com um valor padrão inventado. Se a gravação falhar, não é exibida confirmação de sucesso nem ocorre nova tentativa automática. As chaves e o endereço do backend continuam exclusivamente no servidor Streamlit.

## Configuração autenticada

- `GET /settings/calls` retorna `{"max_duration_seconds":180}` inicialmente.
- `PATCH /settings/calls` recebe `{"max_duration_seconds":90}`, por exemplo.
- Ambas as rotas exigem a chave administrativa existente.
- O valor é um inteiro entre **1 e 14400 segundos**. Zero, negativo, booleano, texto, decimal, `null`, campo ausente e campos adicionais são rejeitados; não existe opção ilimitada.
- A faixa é a validação do aplicativo, não uma promessa de disponibilidade na conta da operadora. Restrições específicas da conta podem rejeitar a discagem; a aplicação não faz nova tentativa automática nem ignora o limite.

## Imposição na operadora

O valor é copiado para a chamada na transação de reserva, depois da preparação da voz. A criação REST da chamada envia esse snapshot em `TimeLimit`. Não é um temporizador por fala e não reinicia com cada resposta do lead. A operadora aplica o máximo mesmo se a interface fechar ou o stream/backend ficar indisponível.

`Timeout=30` continua sendo o tempo de toque, separado da duração da chamada. A preparação anterior à discagem não é tempo de conexão. O limite máximo não garante que uma chamada dure esse tempo: recusa, falha, silêncio ou término normal podem encerrá-la antes. A proteção temporal do stream acompanha o snapshot em vez de limitar silenciosamente configurações maiores aos antigos cinco minutos.

A [referência oficial da Twilio](https://www.twilio.com/docs/voice/api/call-resource) define `TimeLimit` como duração máxima em segundos e informa que restrições dependem da conta/configuração. Não foi feita chamada paga para medir o instante real de corte.

## Histórico, callbacks e migração

A migração `005_call_policy.sql` cria o padrão apenas uma vez e não sobrescreve valores salvos ao reaplicar. Chamadas antigas recebem snapshot desconhecido (`NULL`), sem inventar que já usavam 180 segundos; o código anterior enviava 600 segundos.

O callback terminal autenticado continua sendo o responsável por liberar a chamada ativa. Atingir o limite não comprova sucesso comercial, não dispensa gravação/consentimento e não autoriza liberação antecipada do slot. Saudações preparadas, voz Coral, assinaturas/SIDs, gravações autenticadas, contabilidade e drenagem de falas aceitas permanecem.

Testes de política/API, formulário da operadora e PostgreSQL usam provedores falsos e banco descartável. Nenhuma consulta de configuração ou renderização de formulário deve iniciar chamada, gerar áudio ou consultar cobrança na operadora.
