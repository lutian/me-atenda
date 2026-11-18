# AI Outbound — painel local e agente de voz

Administre produtos, leads e auditoria pelo Streamlit. O backend FastAPI integra Twilio, RAG no PostgreSQL/pgvector, OpenAI (chat/embeddings/TTS) e **faster-whisper local** para STT. Não há campanhas nem loop de discagem: cada chamada exige lead e produto selecionados explicitamente.

## Passo a passo: do clone à primeira ligação

O sistema tem três serviços, todos em Docker:

- **postgres**: guarda os produtos, os leads, as chamadas, as transcrições e os áudios já gerados.
- **backend** (FastAPI): fala com a Twilio e a OpenAI, e roda o reconhecimento de voz (Whisper) na sua máquina.
- **frontend** (Streamlit): o painel em http://localhost:8501.

A Twilio precisa alcançar o backend pela internet para avisar que a ligação foi atendida e para enviar o áudio da conversa. Como o backend roda na sua máquina, o **ngrok** cria uma URL HTTPS pública que encaminha para ele.

```text
Telefone ⇄ Twilio ⇄ https://SEU-DOMINIO.ngrok-free.app ⇄ ngrok ⇄ backend (127.0.0.1:8001)
                                                                     ⇅
                              painel (localhost:8501) ⇄ backend ⇄ PostgreSQL / OpenAI
```

### 1. Instalar as ferramentas

| Ferramenta | Para quê | Como conferir |
|---|---|---|
| Git | clonar o repositório | `git --version` |
| Docker Engine + Docker Compose v2 | rodar os três serviços | `docker --version` e `docker compose version` |
| ngrok | expor o backend para a Twilio | `ngrok version` |

O Python não precisa estar instalado no host, porque tudo roda nos containers. Instale o Docker pelo guia oficial do seu sistema (no Windows, Docker Desktop com WSL2). Instale o ngrok pelo site https://ngrok.com/download. As portas locais **8001** (backend) e **8501** (painel) precisam estar livres.

```sh
git clone https://github.com/lutian/me-atenda.git
cd me-atenda
```

### 2. Criar a chave da OpenAI

A OpenAI faz o chat (respostas do agente), os embeddings (busca na descrição do produto) e a síntese de voz (TTS).

1. Crie uma conta em https://platform.openai.com e adicione créditos em **Settings → Billing**. Sem saldo, as chamadas à API falham.
2. Em **API keys** (https://platform.openai.com/api-keys), clique em **Create new secret key**.
3. Copie a chave (começa com `sk-`). Ela aparece uma única vez; ela vai em `OPENAI_API_KEY`.

### 3. Obter as credenciais e o número da Twilio

A Twilio faz a ligação telefônica.

1. Crie uma conta em https://www.twilio.com/try-twilio e confirme e-mail e telefone.
2. No **Console** (https://console.twilio.com), em **Account Info**, copie:
   - **Account SID** (começa com `AC`) → `TWILIO_ACCOUNT_SID`;
   - **Auth Token** (clique em *Show* para ver) → `TWILIO_AUTH_TOKEN`. Ele também serve para conferir que os callbacks vieram mesmo da Twilio; trate-o como senha.
3. Compre um número com voz em **Phone Numbers → Manage → Buy a number**, marcando **Voice**. Copie-o no formato E.164 (por exemplo `+15555550100`) → `TWILIO_PHONE_NUMBER`.
4. Libere o país de destino em **Voice → Settings → Geo permissions**. Para ligar para celulares brasileiros, marque **Brazil**.
5. **Conta trial:** só liga para números verificados em **Phone Numbers → Manage → Verified Caller IDs**, e a Twilio toca um aviso de conta de teste antes da conversa. Para ligar para qualquer número, faça o upgrade da conta.

Não é preciso configurar nada no campo "A call comes in" do número: o backend envia as URLs de callback em cada ligação que cria.

### 4. Configurar o ngrok com um domínio fixo

1. Crie uma conta em https://dashboard.ngrok.com/signup.
2. Copie o authtoken em **Your Authtoken** e configure o agente local (substitua o placeholder):

   ```sh
   ngrok config add-authtoken SEU_AUTHTOKEN
   ```

3. Em **Domains** (https://dashboard.ngrok.com/domains), crie o domínio gratuito da conta, por exemplo `seu-nome.ngrok-free.app`. Com um domínio fixo a URL não muda a cada vez que o ngrok reinicia, então você configura `PUBLIC_BASE_URL` uma única vez.

O túnel só é iniciado no passo 7, depois que o backend estiver de pé.

### 5. Criar o arquivo `.env`

O Docker Compose lê o `.env` da raiz do projeto. Ele é ignorado pelo Git e **nunca** deve ser publicado.

```sh
cp .env.example .env
openssl rand -hex 24   # rode duas vezes: uma para a senha do banco, outra para a chave administrativa
```

Edite o `.env`:

| Variável | O que colocar |
|---|---|
| `POSTGRES_PASSWORD` | um dos valores gerados pelo `openssl` |
| `DATABASE_URL` | `postgresql+psycopg://outbound:MESMA_SENHA@postgres:5432/outbound`, com a mesma senha da linha anterior |
| `ADMIN_API_KEY` | o outro valor gerado; é a chave entre o painel e o backend |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` | valores do passo 3 |
| `PUBLIC_BASE_URL` | `https://seu-nome.ngrok-free.app`, o domínio do passo 4, sem barra no final |
| `OPENAI_API_KEY` | chave do passo 2 |
| `OPENAI_CHAT_MODEL` | modelo de chat da sua conta OpenAI (sem a variável, o Compose usa `gpt-4o-mini`) |
| `OPENAI_TTS_MODEL`, `OPENAI_TTS_VOICE` | `tts-1` e uma voz padrão (`alloy`, `coral`, `shimmer`…); a voz pode ser trocada depois no painel |
| `WHISPER_*` | deixe `base`, `pt`, `cpu`, `int8` |

A senha do banco vale a partir da primeira inicialização: mudá-la depois no `.env` não altera um banco que já existe.

### 6. Construir e subir os containers

```sh
docker compose up -d --build
docker compose ps
```

Espere os três serviços aparecerem como `healthy`. Na primeira vez isso demora mais: as imagens são construídas e o backend baixa o modelo Whisper (cerca de 150 MB), que fica guardado no volume `whisper_models`. A migração do banco roda sozinha ao iniciar. Confira:

```sh
curl http://localhost:8001/health        # {"status":"ok"}
curl http://localhost:8501/_stcore/health # ok
```

Se algo falhar, veja `docker compose logs --tail=100 backend frontend`. Um erro como `Set OPENAI_API_KEY in .env` indica uma variável faltando no `.env`.

### 7. Iniciar o túnel público

Em outro terminal, deixe o ngrok rodando com o seu domínio:

```sh
ngrok http --url=seu-nome.ngrok-free.app 8001
```

Confira de fora que a URL chega ao backend:

```sh
curl https://seu-nome.ngrok-free.app/health   # {"status":"ok"}
```

O retorno precisa ser esse JSON. Se vier uma página HTML do ngrok, a Twilio também não vai conseguir passar.

**Atenção:** assim o túnel expõe o backend inteiro. As rotas administrativas (`/products`, `/leads`, `/calls`…) exigem a `ADMIN_API_KEY`, mas ficam acessíveis na internet. Para testes curtos isso basta. Para deixar rodando, use o [proxy restrito](#3-restringir-as-rotas-antes-de-usar-callbacks), que só deixa passar os callbacks da Twilio.

Se a URL pública mudar, atualize `PUBLIC_BASE_URL` no `.env` e recrie o backend com `docker compose up -d --no-deps --force-recreate backend` (um `restart` não relê o `.env`).

### 8. Configurar o agente, cadastrar e ligar

Abra http://localhost:8501.

1. **Configurações → Agente de voz:** escolha a voz e o nome do agente (por exemplo "Bia") e clique em **Salvar agente**. Em **Configurações** também fica a duração máxima da chamada.
2. **Produtos → Cadastrar:** informe o nome e a descrição comercial. A descrição é a base que o agente usa para responder (gera embeddings, com custo pequeno).
3. **Leads:** cadastre nome e telefone em E.164 (`+5511999999999`), ou importe um JSON/CSV ([formatos](#leads-e-chamada-unitária)).
4. **Leads → Disparo unitário:** escolha o lead e o produto. Nos campos de mensagens:
   - **Gerar texto com IA** sugere a abertura e a apresentação; revise ou escreva à mão;
   - **Salvar e gerar áudios** grava os textos e gera os áudios ("Alô?", abertura, apresentação e despedida).

   Os textos e os áudios ficam salvos para as próximas ligações. Só é preciso gerar de novo quando você muda um texto, a voz ou o nome do agente.
5. Marque a confirmação de consentimento e clique em **Iniciar UMA ligação**. A conversa aparece em tempo real como chat: o lead à esquerda, o agente à direita.

### 9. Ver os resultados

- **Histórico:** status final, duração, gravação, a conversa em formato de chat e o download do JSON.
- **Observabilidade:** requisições, erros, tokens e custos estimados, no total ou por chamada.

Cada ligação real tem custo na Twilio e na OpenAI, e ligar para alguém exige base legal e consentimento ([limitações](#limitações-consentimento-e-privacidade)). Não há campanha nem nova tentativa automática: se o disparo der timeout, confira o histórico antes de tentar de novo.

### Parar e retomar

```sh
docker compose stop    # para os serviços, preservando os dados
docker compose start   # retoma
```

`docker compose down` remove os containers e mantém os volumes. Não use `down -v` se quiser manter banco e modelo.

## Variáveis de ambiente

Referência do `.env.example`. Os valores abaixo são placeholders.

```dotenv
POSTGRES_PASSWORD=CHANGE_ME_DATABASE_PASSWORD
DATABASE_URL=postgresql+psycopg://outbound:CHANGE_ME_DATABASE_PASSWORD@postgres:5432/outbound
ADMIN_API_KEY=CHANGE_ME_LONG_RANDOM_ADMIN_KEY
TWILIO_ACCOUNT_SID=CHANGE_ME_ACCOUNT_SID
TWILIO_AUTH_TOKEN=CHANGE_ME_AUTH_TOKEN
TWILIO_PHONE_NUMBER=+15555550100
PUBLIC_BASE_URL=https://your-public-backend.example.invalid
OPENAI_API_KEY=CHANGE_ME_OPENAI_KEY
OPENAI_CHAT_MODEL=gpt-4o-mini
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
OPENAI_TTS_MODEL=tts-1
OPENAI_TTS_VOICE=alloy
WHISPER_MODEL=base
WHISPER_LANGUAGE=pt
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
```

| Configuração | Regra |
|---|---|
| Banco | Mesmo segredo em `POSTGRES_PASSWORD` e `DATABASE_URL`; use senha aleatória URL-safe ou percent-encode na URL. Alterar as variáveis de configuração não muda a senha de um banco já inicializado. |
| API administrativa | Chave longa e aleatória compartilhada **apenas entre servidores** backend/frontend via `X-API-Key`; nunca inserir em navegador, links, query strings ou logs. |
| Twilio | Conta/número com Voice e destino autorizado; callbacks são gerados pelo backend e validados por assinatura. Não exponha o painel no túnel público. |
| OpenAI | Chat, embeddings e TTS usam rede e têm custo. Modelo de embeddings deve manter 1536 dimensões; não troque sem planejar a migração dos vetores. |
| Whisper | STT local em CPU/int8 por padrão; modelo `base`, idioma `pt`. O cache persistente fica em `/models/huggingface`. |

O frontend recebe somente `ADMIN_API_KEY` e a URL interna fixa `http://backend:8000`. Segredos da Twilio/OpenAI e acesso ao banco ficam fora dele. O painel serve áudio como bytes após buscá-lo no servidor, sem repassar a chave ou seguir `audio_url` arbitrária. Redirecionamentos HTTP autenticados são recusados.

## Conectar a Twilio pelo ngrok

O caminho é **Twilio → HTTPS/WSS público do ngrok → backend local na porta 8001 do host → porta 8000 do container**. Não aponte o túnel para o Streamlit (8501) nem para o PostgreSQL. O ngrok precisa estar instalado no host e associado à sua própria conta; não é necessário instalá-lo nos containers.

### 1. Autenticar o agente e conferir o backend

No painel da sua conta ngrok, obtenha o authtoken e configure o agente local. O valor abaixo é **somente um placeholder**: substitua-o localmente, sem publicar token, configuração do agente ou histórico do terminal. Esse token não é a chave da Twilio nem `ADMIN_API_KEY`.

```sh
ngrok config add-authtoken YOUR_NGROK_AUTHTOKEN
```

Se os containers ainda não estiverem ativos, com a configuração de ambiente já disponibilizada pelo operador (incluindo uma `PUBLIC_BASE_URL` HTTPS válida), execute `docker compose up --build -d`. Isso inicia os serviços, não liga para ninguém. Confira a saúde local, sem chamar Twilio/OpenAI:

```sh
curl --fail --show-error http://127.0.0.1:8001/health
```

Espere HTTP 200 com `{"status":"ok"}`. HTTP 503 indica problema no banco/esquema; resolva antes de continuar.

### 2. Teste rápido de conectividade (não é publicação segura)

Em outro terminal, mantenha o agente em primeiro plano:

```sh
ngrok http 8001
```

Copie a URL **HTTPS** exibida em `Forwarding`, por exemplo `https://your-domain.ngrok-free.app`. Em outro terminal, teste apenas a saúde, substituindo o domínio:

```sh
curl --fail --show-error https://your-domain.ngrok-free.app/health
```

Espere o mesmo JSON, não uma página HTML de aviso/login. Se a conta/plano ngrok apresentar uma página intermediária, resolva essa configuração antes de usar callbacks; não dependa de um clique no navegador ou de cabeçalhos especiais que a Twilio não envia. Esse GET não usa provedores pagos e **não comprova** assinaturas, WebSocket, áudio ou uma chamada real.

**Limite deste teste:** `ngrok http 8001` encaminha o backend inteiro, inclusive `/products`, `/leads` e `/calls`. Essas rotas exigem chave, mas não devem ficar expostas publicamente apenas por esse motivo. Faça somente a verificação de saúde e encerre esse túnel com **Ctrl+C**. Antes de usar callbacks, restrinja a entrada como abaixo; não compartilhe a chave administrativa nem acrescente autenticação interativa aos callbacks da Twilio.

### 3. Restringir as rotas antes de usar callbacks

Uma opção executável é colocar Nginx **no host**, entre o ngrok e o backend. Com Nginx já instalado, crie um diretório local para a configuração:

```sh
mkdir -p "$HOME/outbound-ngrok"
```

Salve o bloco abaixo em `$HOME/outbound-ngrok/nginx.conf` (fora do repositório). Ele só escuta em localhost:8002 (sem disputar a porta 8001 do backend), permite saúde, callbacks conhecidos e exclusivamente o áudio genérico de saudação, e bloqueia todo o restante; as assinaturas continuam sendo verificadas pelo backend.

```nginx
pid nginx.pid;
error_log stderr warn;
worker_processes 1;
events { worker_connections 128; }
http {
    access_log off;
    client_body_temp_path client_body_temp;
    proxy_temp_path proxy_temp;
    fastcgi_temp_path fastcgi_temp;
    uwsgi_temp_path uwsgi_temp;
    scgi_temp_path scgi_temp;
    server {
        listen 127.0.0.1:8002;
        client_max_body_size 32k;

        location = /health {
            limit_except GET { deny all; }
            proxy_pass http://127.0.0.1:8001;
        }
        location ~ ^/twilio/(voice|amd|gate|status|recording)/[1-9][0-9]*$ {
            limit_except POST { deny all; }
            proxy_pass http://127.0.0.1:8001;
        }
        location ~ ^/twilio/stream/[1-9][0-9]*$ {
            limit_except GET { deny all; }
            proxy_pass http://127.0.0.1:8001;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
            proxy_read_timeout 360s;
        }
        location ~ '^/media/hello/[0-9a-f]{64}\.wav$' {
            limit_except GET { deny all; }
            proxy_pass http://127.0.0.1:8001;
        }
        location / { return 404; }
    }
}
```

Se já usa a configuração anterior, acrescente `amd`, `gate` e a rota exata de saudação: bloquear essas rotas impede a nova abertura. Não libere `/media/` inteiro nem rotas administrativas. Preserve sua configuração e só aplique a alteração sem chamadas ativas ou callbacks pendentes. O exemplo não modifica automaticamente configurações pessoais fora do projeto.

Valide a configuração e inicie o proxy em primeiro plano; mantenha esse terminal aberto:

```sh
nginx -t -p "$HOME/outbound-ngrok/" -c nginx.conf
nginx -p "$HOME/outbound-ngrok/" -c nginx.conf -g 'daemon off;'
```

Em outro terminal, inicie **somente este** túnel, em vez do túnel direto para 8001:

```sh
ngrok http 8002
```

Copie a URL HTTPS atual do agente e repita o GET público de `/health`. Confira também o bloqueio, sem enviar chave nem criar chamada:

```sh
curl --show-error --output /dev/null --write-out '%{http_code}\n' https://your-domain.ngrok-free.app/products
```

Espere **404** do proxy, não 401 do backend; se retornar 401, provavelmente o túnel ainda aponta direto para 8001. O proxy preserva caminho/query e upgrade WebSocket, não cria prefixos nem redireciona URLs. Em implantação duradoura, acrescente monitoramento, limites de requisições, proteção dos dados e gestão de acesso; este exemplo de desenvolvimento não substitui uma revisão de segurança. O painel continua acessível **somente localmente** em http://localhost:8501 e o PostgreSQL continua sem porta pública.

### 4. Aplicar a URL pública ao backend

Com o túnel restrito ativo, ajuste `PUBLIC_BASE_URL` na configuração de ambiente gerenciada pelo operador, usando exatamente o domínio HTTPS exibido pelo ngrok, sem acrescentar `/twilio`, barra final, query ou fragmento:

```dotenv
PUBLIC_BASE_URL=https://your-domain.ngrok-free.app
```

Recrie o backend para carregar a variável atualizada; `docker compose restart` sozinho não aplica alterações nas variáveis fornecidas pelo Compose:

```sh
docker compose up -d --no-deps --force-recreate backend
docker compose ps
```

Aguarde a saúde local e pública novamente. Não é necessário mudar `BACKEND_URL` do frontend: ele continua em `http://backend:8000`. Configurar o túnel não exige disparar chamada; só faça um teste telefônico depois das verificações independentes e do consentimento descrito neste README.

### URLs e métodos que o backend configura automaticamente

Para uma chamada de ID inteiro `{id}`, o backend envia os callbacks na própria requisição de criação da chamada outbound à Twilio. **Não é necessário configurar “A call comes in” no console do número Twilio para este fluxo outbound**; esse campo trata chamadas recebidas e não substitui os callbacks por chamada.

| Uso | Método e URL pública |
|---|---|
| Saúde sem autenticação | `GET https://your-domain.ngrok-free.app/health` |
| TwiML inicial: saudação e redirecionamento | `POST https://your-domain.ngrok-free.app/twilio/voice/{id}` |
| Resultado assíncrono de AMD | `POST https://your-domain.ngrok-free.app/twilio/amd/{id}` |
| Espera limitada e autorização do stream | `POST https://your-domain.ngrok-free.app/twilio/gate/{id}` |
| Somente saudação genérica, sem dados pessoais | `GET https://your-domain.ngrok-free.app/media/hello/{digest}.wav` |
| Status terminal da operadora | `POST https://your-domain.ngrok-free.app/twilio/status/{id}` |
| Gravação concluída/ausente | `POST https://your-domain.ngrok-free.app/twilio/recording/{id}` |
| Media Stream bidirecional | WebSocket (`GET` de upgrade) `wss://your-domain.ngrok-free.app/twilio/stream/{id}` |

O AMD roda em paralelo (`AsyncAmd=true`): a resposta inicial emite `<Play>` do “Alô?” já preparado, sem esperar fala nem classificação. Após a saudação, o gate consulta a classificação autenticada; somente `human` permite `<Connect><Stream>` com URL **WSS** derivada de `PUBLIC_BASE_URL`. Não se configura esse stream manualmente no console. Todos os callbacks HTTP e o upgrade WebSocket exigem `X-Twilio-Signature`, validada com `TWILIO_AUTH_TOKEN`; não usam a chave administrativa. Apenas saúde e o WAV genérico permitem GET anônimo. O hash do áudio não autoriza consultar apresentações, gravações ou arquivos. Um POST sem assinatura é rejeitado com 403 e não é um teste completo de integração.

A validação reconstrói a URL a partir de `PUBLIC_BASE_URL`, caminho original e query original (incluindo ordem/codificação), não de `Host` ou `X-Forwarded-*`. No WebSocket, verificamos a URL **WSS efetivamente emitida** e, para compatibilidade, sua equivalente HTTPS no mesmo endpoint, ambas com/sem barra final. Origem configurada, prefixo, porta e query bruta continuam exatos; não há origem arbitrária nem confiança em headers encaminhados. As duas formas foram cobertas por testes de assinatura e handshake local; isso não substitui a validação de áudio com a operadora. Os callbacks HTTP continuam com validação exata, sem essa alternativa. Mudar domínio, caminho ou query no proxy pode invalidar assinaturas. Use o exemplo sem prefixo; se adotar prefixo em outra implantação, o proxy deve removê-lo antes do roteamento e `PUBLIC_BASE_URL` deve incluí-lo exatamente. Veja [backend/README.md](backend/README.md).

### Quando o ngrok reiniciar ou a URL mudar

Dependendo do domínio/plano, a URL pode mudar ao reiniciar o agente; sempre confira `Forwarding`. **Não troque túnel/configuração durante uma chamada ou enquanto callbacks de status/gravação estiverem pendentes.** Antes de encerrar, confirme o estado na operadora e no histórico: perder callbacks pode reter o slot ativo ou deixar uma gravação pendente.

Ao mudar a URL: mantenha o proxy restrito, reinicie `ngrok http 8002`, ajuste `PUBLIC_BASE_URL` na configuração gerenciada pelo operador, repita o comando de recriação do backend e os GETs de saúde/bloqueio acima. Novas chamadas usarão a URL nova automaticamente. Chamadas já criadas conservam os callbacks antigos na Twilio; mudar a configuração não os corrige retroativamente. Preserve o endereço anterior até a conclusão ou faça reconciliação operacional conforme o README do backend — não libere o slot nem repita a ligação às cegas. Encerre ngrok e Nginx com Ctrl+C quando não forem necessários.

## Uso do painel

### Produtos e serviços

Na seção **Produtos**, use **Cadastrar → Salvar cadastro**. Para alterações, escolha o registro em **Editar ou excluir → Salvar alterações**; a exclusão requer confirmação separada. Criação/edição pode gerar custo de embeddings, registrado globalmente, não replicado em cada ligação.

Cadastre **nome e descrição comercial**; edite ou exclua selecionando explicitamente um cadastro. O backend mantém embeddings/RAG. Registros usados no histórico podem ter exclusão recusada; o painel mostra o código HTTP sem expor detalhes internos. Revise a descrição para evitar promessas comerciais indevidas.

### Leads e chamada unitária

A tabela mostra os campos retornados pelo backend. Cadastro manual, edição e importação validam nome, telefone E.164 (`+` e 8–15 dígitos, sem espaços internos) e empresa opcional. Não há deduplicação automática; confirme o arquivo antes de importar novamente. Arquivos devem estar em UTF-8, ter até 2 MiB e conter somente estes campos:

```json
[
  {"name": "Contato autorizado", "phone": "+5511999999999", "company": "Empresa"}
]
```

```csv
name,phone,company
Contato autorizado,+5511999999999,Empresa
```

`company` omitida vira `""`. JSON deve ser uma lista não vazia; CSV exige cabeçalhos `name,phone` e aceita `company`. Campos extras, números JSON em vez de strings, linhas incompletas ou telefones inválidos rejeitam o arquivo inteiro **antes de enviar qualquer registro**. CSV é convertido no servidor Streamlit para a mesma lista JSON documentada em `POST /leads/import`. O backend limita lotes a 1000 leads; divida arquivos maiores. O preview permite revisar todos os dados antes do envio.

Para ligar, selecione **lead e produto**, confirme a base legal/consentimento e clique em **Iniciar UMA ligação**. O backend, não a UI, garante uma única chamada ativa com PostgreSQL. HTTP 409 indica conflito; não há retry automático. Se houver timeout ou HTTP 503, **consulte o histórico e a operadora antes de repetir**, pois a chamada pode ter sido criada e o slot continuar reservado. Não limpe o slot manualmente enquanto existir chamada ativa; veja recuperação no README do backend.

### Abertura imediata e áudio reutilizável

Os áudios das mensagens preparadas ("Alô?", abertura, apresentação e despedida) são gerados em **Salvar e gerar áudios**, antes da ligação. Os mesmos textos, modelo, voz e formato reutilizam o áudio persistido no PostgreSQL, inclusive após reiniciar o backend; mudar texto, modelo ou voz cria outra identidade. As mensagens ficam fixadas na reserva: uma edição posterior não muda o que aquela chamada ouvirá. Detalhes em [docs/conversation-reply.md](docs/conversation-reply.md).

Ao atender, a Twilio recebe a instrução de reproduzir **“Alô?” sem esperar o lead falar ou o AMD terminar**. Uma secretária também pode ouvir essa saudação, mas **a apresentação comercial continua restrita à classificação humana autenticada**. Depois do “Alô?”, o gate aguarda em intervalos de um segundo, com prazo de 35 segundos desde a primeira emissão e limite de consultas; sem confirmação humana, encerra sem apresentação. Respostas repetidas não repetem o “Alô?” nem reiniciam o prazo. Isso limita a emissão da instrução, não confirma que o áudio foi ouvido: rede, cache da operadora e AMD ainda podem falhar ou acrescentar atraso.

Preparações concorrentes são excluídas no banco; o pedido perdedor recebe conflito sem repetir síntese nem discar. Falhas de preparação não iniciam chamada nem deixam slot ativo. Uma preparação pendente/ambígua permanece bloqueada para evitar repetição automática de um TTS possivelmente cobrado: o operador precisa investigar e reconciliar explicitamente, sem apagar marcadores ou repetir às cegas. Se a discagem já começou, timeout continua sendo resultado incerto; confira histórico e operadora antes de qualquer repetição.

O custo real de preparação aparece **uma vez na observabilidade global**, antes de existir chamada. Reutilizar ou baixar o áudio não gera evento de TTS nem custo duplicado por conversa. Respostas durante a conversa continuam usando TTS normalmente. A gravação autenticada continua sendo a evidência de áudio; o sistema não inventa confirmação de reprodução nem turno de transcrição para o “Alô?”.

### Histórico e auditoria

Atualize a tabela para ver nome e telefone do lead, título do produto, `status_final`, `duration_seconds` e `recording_status`. A seleção combina referência da chamada, nome, telefone e produto; os detalhes também mostram esses campos legíveis, sem exigir IDs de cadastros. São **valores atuais dos cadastros vinculados**, não snapshots da época da ligação. Vínculos ausentes aparecem como lead, telefone ou produto indisponível, sem substituir por dados de outro cadastro.

Selecione uma chamada para buscar detalhes e ver **Observabilidade desta conversa** antes da gravação/transcrição: resumo e eventos consultam somente essa chamada e são atualizados ao trocar a seleção. Ingestão de produtos permanece na visão global, sem ser cobrada novamente na conversa. Abrir o histórico não disca, não reconcilia cobrança e não cria diagnósticos derivados. A consulta de cobrança continua exigindo clique explícito. Leia **todos os turnos** e use **Baixar transcrição JSON** para obter a transcrição. O botão **Carregar áudio autenticado** busca `GET /calls/{id}/audio` com chave no servidor e oferece player/download. Dados de transcrição são tratados como texto, nunca HTML.

Gravações podem estar `pending`, `available`, `failed` ou `absent`; gravação ausente não é inventada. Atualize depois do callback de gravação. Falhas de download exigem acompanhamento/retry seguro do callback pelo operador, conforme o backend. `sucesso` significa resultado comercial sustentado; `completed` da Twilio não basta. `inconclusivo` é válido quando faltam evidências. AMD desconhecido não prova PABX/IVR.

## Observabilidade: tokens, estimativas e cobrança

A seção **Observabilidade** mostra contagem global de requisições/erros e permite filtrar por chamada. **Histórico → chamada** incorpora a mesma auditoria. Use **Atualizar observabilidade** para consultar novamente; não há polling de cobrança nem ações pagas automáticas. A tabela apresenta eventos recentes (limite inicial 100, ajustável entre 1 e 200), enquanto os totais cobrem todo o escopo selecionado, não somente as linhas visíveis.

| Campo ou indicador | Como interpretar |
|---|---|
| Horário UTC, `elapsed_ms`, provedor/operação/modelo e `provider_request_id` | Localize a requisição com um ID seguro; `success`, `error` e `cancelled` não são status comerciais da ligação. |
| Entrada, saída e total de tokens | Soma apenas dos valores conhecidos retornados pelas integrações; cache é subconjunto da entrada, nunca some duas vezes. `unknown_usage_events` informa eventos com uso desconhecido. |
| TTS / STT | TTS usa caracteres; tokens são n/a. Whisper é local: não se inventa tarifa de provedor STT; CPU/infra continuam fora do subtotal. |
| `estimated_cost_usd` e `cost_status` | Estimativa USD por evento; resumo `estimated`, `partial`, `unknown` ou `no_usage`. Desconhecido é null, não zero; subtotal parcial não é fatura completa. |
| `excluded_costs` | Componentes excluídos/desconhecidos, como gravação, armazenamento, Streams, infraestrutura, impostos ou outros adicionais. Não presuma que uma lista curta garante cobertura total. |
| Conectividade faturada | Campo separado `billed_connectivity`, na moeda nativa do provedor, sem conversão cambial. Não some a cobrança à estimativa de conectividade; seria dupla contagem. |

### Tarifas opcionais e cálculo versionado

`OBSERVABILITY_PRICING_JSON` é opcional e repassada **somente ao backend** pelo Compose. Vazia/ausente deixa tarifas não configuradas: custos ficam null/desconhecidos ou parciais, mesmo quando tokens são conhecidos. Não há preços oficiais embutidos. O backend persiste um snapshot das tarifas aplicáveis por evento; alterar a versão/configuração afeta novos cálculos, não reprecifica o histórico. Não envie preços pelo navegador nem por webhook.

Schema: objeto com `version` (string), `currency` igual a `USD`, `models` indexado pelo **nome do modelo solicitado** e `twilio`. Tarifas são strings decimais finitas e não negativas. Modelos aceitam `input_per_million`, `cached_input_per_million`, `output_per_million` e/ou `characters_per_million`; Twilio aceita `voice_per_minute` e `amd_per_call`. Configure somente as tarifas que você confirmou e controle a versão.

**Exemplo estritamente hipotético — NÃO são preços oficiais atuais.** Referência inline para o ambiente do processo, não instrução para criar arquivo de segredos:

```sh
export OBSERVABILITY_PRICING_JSON='{"version":"exemplo-hipotetico-v1","currency":"USD","models":{"gpt-4o-mini":{"input_per_million":"2.00","cached_input_per_million":"1.00","output_per_million":"4.00"},"text-embedding-3-small":{"input_per_million":"0.50"},"tts-1":{"characters_per_million":"10.00"}},"twilio":{"voice_per_minute":"0.02","amd_per_call":"0.01"}}'
```

Com **1.000 tokens de entrada, dos quais 200 em cache**, e 100 de saída: `(800 × 2 + 200 × 1 + 100 × 4) / 1.000.000 = USD 0,0022`. TTS com 500 caracteres nessa tarifa hipotética acrescentaria USD 0,005, não 500 tokens. Esses valores ilustram somente componentes conhecidos. Telefonia depende de duração, política de arredondamento declarada pelo backend e limitações da tarifa; minutos estimados não provam o critério faturado da operadora. Consulte também [o contrato operacional do backend](backend/README.md). Não extrapole preço de gravação/Streams/infra ou impostos a partir da tarifa de voz.

Após o operador mudar a configuração, recrie o backend com `docker compose up -d --no-deps --force-recreate backend`. Não altere configuração/túnel durante chamadas ou callbacks pendentes.

### Consultar cobrança sem discar novamente

Na auditoria de uma chamada selecionada, o botão **Consultar cobrança de conectividade na Twilio** faz uma única consulta explícita da chamada já vinculada, não uma ligação. A API retorna `billed_connectivity` com status `pending`, `available`, `failed` ou `not_applicable`, valor/moeda nativos, horário UTC e código de erro seguro. A Twilio disponibiliza cobrança assincronamente: `pending` não significa grátis nem falha definitiva. Se necessário, consulte novamente **manualmente** mais tarde; repetir substitui a informação de conectividade em vez de acumular valores. A cobrança de conectividade não inclui toda a fatura. USD estimado e moeda do provedor permanecem separados, sem promessa de câmbio/impostos.

Contratos administrativos (autenticação `X-API-Key` apenas no servidor):

- `GET /observability/events?call_id=<id>&limit=100`: array direto; `call_id` omitido significa visão global; limite 1–200.
- `GET /observability/summary?call_id=<id>`: objeto de totais; omitir `call_id` dá resumo global, cobrança global n/a.
- `POST /calls/{id}/billing/reconcile`: retorna o objeto de conectividade, sem preços enviados pelo cliente.

O túnel público deve **bloquear essas rotas administrativas**, assim como produtos/leads/chamadas; somente callbacks Twilio, saúde e o WAV genérico de saudação são publicados pelo proxy do [tutorial ngrok](#conectar-a-twilio-pelo-ngrok).

### Falha de estabelecimento do canal de voz

**Observabilidade** e **Histórico → chamada** exibem o evento `twilio / media_stream`, código `media_stream_not_established`, quando um callback terminal autenticado confirma humano persistido sem stream vinculado. O encerramento libera a reserva e classifica uma chamada ainda `ligando` como `falha_tecnica`, sem sobrescrever resultados anteriores. Duplicatas não geram novos eventos. O diagnóstico não inventa fala, tokens ou cobrança; conectividade permanece separada.

No incidente original, havia humano detectado, nenhum stream/transcrição, seis segundos e resultado `inconclusivo`; callbacks HTTP tiveram sucesso, mas o WebSocket foi rejeitado antes da aceitação. Isso aponta a falha antes de STT/LLM/Bia, sem provar a causa exata da assinatura. O operador pode adicionar um único diagnóstico derivado pelo método restrito `Store.backfill_media_failure(call_id)` após verificação/deploy, preservando o resultado/histórico original; não há atualização automática nem escrita por GET. A normalização documentada da barra é uma correção de compatibilidade, **não prova de resolução da chamada real**: reteste telefônico exige autorização separada.

Rejeições não autenticadas do WebSocket não alteram banco nem status: geram somente aviso estático limitado por processo, sem tokens, assinaturas, headers, URLs ou dados pessoais. Assinaturas continuam obrigatórias. Veja [detalhes e limites do backend](backend/README.md#media-establishment-diagnostics-s22).

### Diagnóstico seguro

| Sintoma | Próximo passo seguro |
|---|---|
| 401 no painel | Confira a chave compartilhada somente nos processos servidores; nunca cole a chave no navegador, URL ou chamado de suporte. |
| 409 ao ligar | Há possível chamada ativa. Atualize o histórico e confira a operadora; não limpe o slot nem repita às cegas. |
| Timeout/503 no disparo | Resultado pode ser incerto; confira histórico/operadora antes de qualquer repetição. Saúde local não valida provedores reais. |
| Erro em embedding/chat/TTS | Veja operação, status, código sanitizado, UTC e request ID; confira modelo, credencial e conectividade com o operador, sem expor corpo de requisição/resposta. Uso pode ter sido consumido mesmo quando a validação de saída falha. |
| Tokens/custo desconhecidos | Diferencie falta de uso retornado, operação não baseada em tokens e falta de tarifa para aquele nome de modelo. Confira `unknown_usage_events`, versão e `excluded_costs`; não substitua null por zero. |
| Preço parcial ou cobrança pendente | Integrações têm latência, falhas e faturamento assíncrono reais. Espere a atualização da operadora e reconcilie explicitamente; não some valor faturado à estimativa. |
| `media_stream_not_established` / desligou sem fala | Consulte o evento da chamada em Observabilidade/Histórico, confira origem/prefixo/query, proxy WebSocket e assinatura; não repita a ligação sem autorização. Reteste real ainda é necessário. |
| Áudio pendente/ausente ou assinatura inválida | Confira callback de gravação, domínio HTTPS atual, caminho/query preservados e assinatura; siga a recuperação do backend, sem buscar URLs arbitrárias. |

Eventos e mensagens da UI são texto, nunca HTML bruto. Logs operacionais devem usar apenas códigos seguros, timestamps, status e IDs técnicos seguros: **não registrar** credenciais, headers de autenticação, corpos de requisição/resposta, telefone, transcrição ou contexto RAG. Revise/redija qualquer log local antes de compartilhar; a sanitização de eventos não autoriza publicar logs brutos de containers/proxies. Integração real, qualidade de voz e fatura final exigem verificação própria: testes offline não simulam todos esses efeitos.

## Infraestrutura e persistência

| Serviço | Exposição e dados |
|---|---|
| `postgres` | Imagem PostgreSQL 16 com pgvector; sem porta publicada; volume `postgres_data` persiste banco, transcrições, vetores e áudio BYTEA. |
| `backend` | Python 3.11, porta do host **127.0.0.1:8001** → porta interna **8000**; volume `whisper_models` persiste cache; health check HTTP `/health`. |
| `frontend` | Python 3.11, Streamlit **127.0.0.1:8501**; health check `/_stcore/health`. Sem banco/cache compartilhado de sessão. |

`docker compose stop` para parar e `docker compose start` para retomar. `docker compose down` remove containers/rede mas preserva os volumes nomeados; **não use remoção de volumes** se deseja preservar dados. Faça backup/restauração testada do PostgreSQL; o cache Whisper não substitui backup. A migração explícita, se necessária, é `docker compose run --rm backend python -m app.store` (mesma migração segura executada no startup).

A configuração não inclui TLS/proxy público, gestão corporativa de identidade ou autenticação própria no Streamlit. O bind localhost não substitui autenticação: em máquina multiusuário restrinja também o acesso local. Não publique 8501 nem conceda acesso ao painel a terceiros; ele representa privilégios administrativos. Se houver acesso remoto, adicione um gateway autenticado/TLS antes de disponibilizar. Mantenha CORS/XSRF padrão do Streamlit habilitados.

## Limitações, consentimento e privacidade

A primeira saudação é “Alô?”. A abertura, somente após AMD humano, é o texto revisado pelo operador para o produto. Sem mensagens salvas, usa o padrão: “Olá, meu nome é {agente}, sou Representante de {título do produto}, gostaria de apresentar para vocês o nosso produto. É com você que posso falar?”. O nome do agente vem das Configurações.

- **Antes de telefonar:** verifique leis de telemarketing, listas de bloqueio, horários e consentimento/base legal aplicável. Use apenas destinatários de teste autorizados. A nova abertura não identifica a IA nem avisa sobre gravação; a gravação continua habilitada e a Twilio pode iniciá-la ao atender. Cabe ao operador garantir os avisos de IA/gravação e o consentimento prévio quando exigidos, por processo adequado antes do uso. A pergunta inicial não comprova consentimento.
- **Dados pessoais:** nomes, telefones, transcrições e gravações ficam no banco e podem ser baixados por operadores. Restrinja acesso, proteja discos/backups, defina retenção e exclusão legais e evite colocar dados pessoais em logs/issues. Exclusão do cadastro não é garantia de eliminação do histórico.
- **Latência:** faster-whisper usa limites de silêncio/utterances, não reconhecimento full-duplex contínuo. Warmup/download, CPU e chamadas OpenAI/Twilio adicionam atraso; não há garantia de tempo real, barge-in ou cancelamento de eco. Dialetos, ruído e AMD podem produzir erros. Não use decisões inferidas como prova de consentimento.
- **Custos/rede:** Twilio e OpenAI são serviços pagos; Whisper local não elimina custos dos outros componentes. Modelos exigem download inicial; pré-carregue o volume para ambiente offline. Nunca copie segredos/estado do projeto de referência.

## Verificação offline e revisão

Sem instalações globais nem credenciais, da raiz:

```sh
python3 -B -m unittest discover -s frontend/tests -v
```

A suíte usa servidor HTTP local efêmero e valida contratos públicos do cliente, CRUD, importação, escolha explícita de produto, áudio autenticado, redirecionamentos recusados e erros sem segredos. Inclui os endpoints de observabilidade, limites/filtros, apresentação de uso desconhecido, separação de moedas/cobrança, reconciliação apenas explícita e configuração pública do Compose sem ler arquivos de ambiente. Os testes de histórico usam Streamlit AppTest com API falsa e duas chamadas para validar apresentação e troca do escopo da auditoria; são ignorados quando Streamlit não está instalado no host (execute-os na imagem frontend existente, sem rede). Não testa navegadores, provedores ou serviços reais. Os testes do backend e seus pré-requisitos estão em [backend/README.md](backend/README.md).

Para instalar o painel fora de Docker, crie um virtualenv local e instale `frontend/requirements.txt` nele; configure as variáveis somente no processo servidor e execute `streamlit run frontend/app.py`. O backend continua obrigatório.

### Conferir o reconhecimento de voz antes de uma ligação

A abertura da Bia usa síntese de voz; ouvi-la não comprova que o reconhecimento da sua resposta está pronto. A imagem backend inclui `requests` explicitamente porque o Whisper instalado precisa dessa biblioteca, embora não a declare como dependência. Depois de construir a imagem, confira os imports reais, sem rede nem download de modelo:

```sh
docker run --rm --network none --entrypoint python me-atenda-backend -B -m unittest discover -s tests -p test_speech_runtime.py -v
```

Esse teste não carrega pesos nem comprova transcrição. O modelo Whisper configurado também precisa estar instalado no volume `/models`: o download inicial é uma ação separada, somente com autorização do operador. Antes de testar outra ligação paga, valide o carregamento local e a transcrição de uma amostra sem chamar Twilio ou OpenAI. Falhas nessa etapa aparecem como `transcribe / local` na auditoria da conversa; a aplicação pode encerrar a ligação sem erro na operadora.

Ordem de revisão sugerida: (1) `frontend/client.py`, `frontend/imports.py` e testes; (2) `frontend/app.py` e fluxos de confirmação; (3) Compose, Dockerfiles e esta configuração. Integração real, segurança do proxy e serialização PostgreSQL são verificações independentes, não demonstradas pela suíte frontend.
