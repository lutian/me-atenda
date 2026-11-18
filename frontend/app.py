"""Administrative UI: all backend traffic and authentication stays server-side."""
import json
import os

import streamlit as st

from client import APIClient, APIError
from imports import ImportValidationError, parse_leads
from chat import render_chat
from history import history_label, history_record
from observability import render_audit


st.set_page_config(page_title="Outbound — Administração", layout="wide")
st.title("Outbound — Administração")
st.caption("Uso local restrito a operadores autorizados. Sem campanhas ou discagem automática.")
try:
    api = APIClient(os.environ.get("BACKEND_URL", "http://backend:8000"),
                    os.environ.get("ADMIN_API_KEY", ""))
except ValueError:
    st.error("Configure BACKEND_URL e ADMIN_API_KEY no servidor Streamlit.")
    st.stop()


def select_record(label, records, key):
    labels = {row["id"]: f'{row["id"]} — {row.get("name", "")}' for row in records}
    return st.selectbox(label, list(labels), index=None, format_func=labels.get, key=key,
                        placeholder="Selecione explicitamente")


def changed(message):
    st.session_state["notice"] = message
    st.rerun()


LONG_FIELDS = ("description", "goal")
OPTIONAL_FIELDS = ("goal",)


def editor(resource, title, fields):
    """One CRUD flow reused by both product and lead sections."""
    st.header(title)
    records = api.list(resource)
    st.dataframe(records, use_container_width=True, hide_index=True)
    with st.expander("Cadastrar", expanded=not records):
        with st.form(f"new-{resource}", clear_on_submit=True):
            values = {field: (st.text_area(label) if field in LONG_FIELDS else st.text_input(label))
                      for field, label in fields}
            submit = st.form_submit_button("Salvar cadastro")
        if submit:
            if resource == "leads":
                values = parse_leads(json.dumps([values]), "manual.json")[0]
            elif not all(value.strip() for field, value in values.items() if field not in OPTIONAL_FIELDS):
                st.warning("Preencha nome e descrição.")
                return records
            api.create(resource, values)
            changed("Cadastro salvo.")
    selected = select_record("Editar ou excluir", records, f"edit-{resource}")
    if selected is not None:
        record = next(row for row in records if row["id"] == selected)
        with st.form(f"edit-{resource}-{selected}"):
            values = {field: (st.text_area(label, value=record.get(field) or "") if field in LONG_FIELDS
                              else st.text_input(label, value=record.get(field, "") or ""))
                      for field, label in fields}
            save = st.form_submit_button("Salvar alterações")
        if save:
            if resource == "leads":
                values = parse_leads(json.dumps([values]), "manual.json")[0]
            elif not all(value.strip() for field, value in values.items() if field not in OPTIONAL_FIELDS):
                st.warning("Preencha nome e descrição.")
                return records
            api.update(resource, selected, values)
            changed("Alterações salvas.")
        confirm = st.checkbox("Confirmo a exclusão deste cadastro", key=f"delete-confirm-{resource}-{selected}")
        if st.button("Excluir cadastro", disabled=not confirm, key=f"delete-{resource}-{selected}"):
            api.delete(resource, selected)
            changed("Cadastro excluído.")
    return records


GOODBYE = "Entendi, obrigado pelo seu tempo."


def prepared_messages(product_id):
    """Review the drafted pitch and goodbye, then generate every prepared audio before dialing."""
    st.markdown("**Mensagens preparadas**")
    st.caption("Alô, abertura, apresentação (se houver interesse) e despedida (sem interesse ou contato errado) "
               "ficam gravadas antes da ligação, sem esperar o TTS durante a conversa. O \"Alô?\" é único e "
               "compartilhado por todos os produtos.")
    status = api.product_messages(product_id)
    opening_key, pitch_key, goodbye_key = f"opening-{product_id}", f"pitch-{product_id}", f"goodbye-{product_id}"
    st.session_state.setdefault(opening_key, status["opening"] or "")
    st.session_state.setdefault(pitch_key, status["pitch"] or "")
    st.session_state.setdefault(goodbye_key, status["goodbye"] or GOODBYE)
    if st.button("Gerar texto com IA", key=f"draft-{product_id}"):
        draft = api.draft_messages(product_id)
        st.session_state[opening_key], st.session_state[pitch_key] = draft["opening"], draft["pitch"]
        st.session_state[goodbye_key] = draft["goodbye"]
        st.rerun()
    opening = st.text_area("Abertura (após o \"Alô?\"; inclui o nome do agente)", key=opening_key, max_chars=400)
    pitch = st.text_area("Apresentação (caso a pessoa tenha interesse)", key=pitch_key, max_chars=400)
    goodbye = st.text_area("Despedida (sem interesse ou contato errado)", key=goodbye_key, max_chars=200)
    if st.button("Salvar e gerar áudios", key=f"prepare-{product_id}"):
        try: api.save_messages(product_id, opening, pitch, goodbye)
        except ValueError as exc:
            st.error(str(exc))
            return False
        changed("Mensagens salvas e áudios gerados.")
    if status["ready"]:
        st.success("Áudios prontos para a ligação.")
    else:
        st.info("Revise os textos e clique em \"Salvar e gerar áudios\" antes de ligar.")
    return status["ready"]


def leads_page():
    leads = editor("leads", "Leads (fila)", [("name", "Nome"), ("phone", "Telefone E.164 (+5511999999999)"),
                                             ("company", "Empresa (opcional)")])
    st.subheader("Importar JSON ou CSV")
    st.caption("Campos: name, phone, company (opcional). Todos os registros são validados antes de enviar.")
    upload = st.file_uploader("Arquivo de leads", type=["json", "csv"])
    if upload is not None:
        if upload.size > 2 * 1024 * 1024:
            st.warning("Limite de importação: 2 MiB.")
        else:
            try:
                rows = parse_leads(upload.getvalue().decode("utf-8-sig"), upload.name)
                st.dataframe(rows, hide_index=True, use_container_width=True)
                if st.button(f"Importar {len(rows)} leads"):
                    api.import_leads(rows)
                    changed("Leads importados. Retire o arquivo antes de uma nova importação.")
            except UnicodeError:
                st.warning("Use arquivo UTF-8.")
    st.subheader("Disparo unitário")
    st.warning("A ligação real tem custo. Confirme consentimento/base legal e aviso de gravação antes do disparo.")
    products = api.list("products")
    lead_id = select_record("Lead da chamada", leads, "dispatch-lead")
    product_id = select_record("Produto ofertado", products, "dispatch-product")
    product = next((p for p in products if p["id"] == product_id), None)
    if product and product.get("embeddings_current") is False:
        st.error("Este produto tem embeddings de outro modelo. Atualize os embeddings em Produtos antes de ligar.")
        ready = False
    else:
        ready = prepared_messages(product_id) if product_id else False
    consent = st.checkbox("Conferi autorização/base legal, horário permitido e aviso de gravação.")
    if st.button("Iniciar UMA ligação", disabled=not (lead_id and product_id and consent and ready)):
        result = api.dispatch(lead_id, product_id)
        st.session_state["live_call"], st.session_state["live_active"] = result["id"], True
        st.success(f'Chamada solicitada: {result["id"]}. Consulte o histórico antes de novo disparo.')
    st.caption("O backend serializa chamadas. Sem repetição automática; em timeout, consulte o histórico antes de tentar novamente.")
    live_conversation()


LIVE_SECONDS = 2


def live_conversation():
    """Conversation of the last dispatched call, refreshed while the call is in progress."""
    call_id = st.session_state.get("live_call")
    if not call_id:
        return

    @st.fragment(run_every=LIVE_SECONDS if st.session_state.get("live_active") else None)
    def panel():
        st.subheader(f"Conversa em tempo real — chamada {call_id}")
        try: call = api.call(call_id)
        except APIError as exc:
            # Fragment reruns bypass the page-level handler; keep polling after a transient failure.
            st.warning(f"Não foi possível atualizar a conversa: {exc}")
            return
        render_chat(st, call.get("transcription_json"))
        if call.get("status_final") == "ligando":
            st.caption(f"Em andamento; atualiza a cada {LIVE_SECONDS} segundos.")
        else:
            st.info(f'Chamada encerrada: {call.get("status_final")}.')
            if st.session_state.get("live_active"):
                st.session_state["live_active"] = False  # Stop polling a finished call.
                st.rerun()

    panel()


def history_page():
    st.header("Histórico e auditoria")
    st.button("Atualizar histórico")  # Every interaction reruns and fetches fresh data.
    calls = api.list("calls")
    leads = {lead["id"]: lead for lead in api.list("leads")}
    products = {product["id"]: product for product in api.list("products")}
    records = [history_record(call, leads, products) for call in calls]
    st.dataframe(records, hide_index=True, use_container_width=True)
    st.caption("Nome, telefone e produto são valores atuais dos cadastros vinculados, não snapshots da época da chamada.")
    labels = {record["id"]: history_label(record) for record in records
              if type(record["id"]) is int and record["id"] > 0}
    call_id = st.selectbox("Chamada para auditoria", list(labels), index=None, format_func=labels.get)
    if call_id is None:
        return
    call = api.call(call_id)
    st.write(history_record(call, leads, products))
    st.caption("sucesso é resultado comercial; conclusão da operadora não comprova sucesso. inconclusivo indica ausência de evidência.")
    st.subheader("Observabilidade desta conversa")
    st.caption(f"Auditoria da chamada {call_id}. Ingestão de produtos pertence à visão global, não ao custo desta conversa.")
    render_audit(st, api, call_id)
    st.subheader("Gravação")
    st.caption("A gravação pode ficar pendente ou falhar após o fim da ligação; nem toda chamada produz áudio.")
    if st.button("Carregar áudio autenticado", key=f"audio-{call_id}"):
        content, content_type = api.audio(call_id)
        if content:
            st.audio(content, format=content_type)
            st.download_button("Baixar gravação", content, file_name=f"call-{call_id}.audio", mime=content_type)
        else:
            st.info("Áudio indisponível. Atualize o histórico para conferir recording_status.")
    st.subheader("Transcrição completa")
    transcript = call.get("transcription_json") or []
    # Text rendering only: never interpret model/lead content as HTML.
    render_chat(st, transcript)
    with st.expander("JSON da transcrição"):
        st.json(transcript)
    st.download_button("Baixar transcrição JSON", json.dumps(transcript, ensure_ascii=False, indent=2),
                       file_name=f"call-{call_id}.json", mime="application/json")


def observability_page():
    st.header("Observabilidade")
    st.caption("Visão global inclui ingestão de produtos; esses eventos não são cobrados novamente em cada chamada.")
    calls = api.list("calls")
    call_id = st.selectbox("Filtrar por chamada (vazio = global)", [call["id"] for call in calls], index=None)
    limit = st.number_input("Limite de eventos recentes", min_value=1, max_value=200, value=100, step=1)
    st.button("Atualizar observabilidade")
    render_audit(st, api, call_id, limit)


def settings_page():
    st.header("Configurações de chamadas")
    current = api.call_policy()
    st.caption("Padrão inicial: 180 segundos (3 minutos). O limite é imposto pela operadora e não reinicia a cada fala.")
    st.caption("Alterações valem somente para novas reservas, não para chamadas já em andamento.")
    with st.form("call-policy"):
        seconds = st.number_input("Duração máxima da ligação (segundos)", min_value=1,
                                  max_value=14400, value=current["max_duration_seconds"], step=1)
        st.caption("Faixa do aplicativo: 1 a 14400 segundos. O tempo de toque é separado.")
        save = st.form_submit_button("Salvar limite")
    if save:
        api.save_call_policy(seconds)
        changed("Limite de duração salvo. Será aplicado às próximas chamadas.")
    st.subheader("Agente de voz")
    agent = api.agent_profile()
    st.caption("Escolha um nome coerente com a voz (por exemplo, um nome masculino para uma voz masculina).")
    st.caption("Mudar nome ou voz exige gerar as mensagens preparadas de novo antes da próxima ligação.")
    with st.form("agent-profile"):
        voices = agent["voices"]
        voice = st.selectbox("Voz do TTS", voices, key="agent-voice",
                             index=voices.index(agent["tts_voice"]) if agent["tts_voice"] in voices else None)
        name = st.text_input("Nome do agente", value=agent["agent_name"], max_chars=40, key="agent-name")
        save_agent = st.form_submit_button("Salvar agente")
    if save_agent:
        try: api.save_agent_profile(name, voice)
        except ValueError as exc:
            st.error(str(exc))
            return
        changed("Agente salvo. Será aplicado às próximas chamadas.")
    pricing_form()


KIND_LABELS = {"text": "Textos (mensagens e respostas)", "audio": "Áudios (voz do agente)",
               "embedding": "Embeddings (busca na descrição)",
               "transcription": "Transcrição (fala do lead)"}
RATE_LABELS = {"input_per_million": "Entrada, por 1 milhão de tokens",
               "cached_input_per_million": "Entrada em cache, por 1 milhão de tokens",
               "output_per_million": "Saída, por 1 milhão de tokens",
               "characters_per_million": "Por 1 milhão de caracteres",
               "per_minute": "Por minuto de áudio"}
KIND_RATES = {"text": ("input_per_million", "cached_input_per_million", "output_per_million"),
              "embedding": ("input_per_million",), "audio": ("characters_per_million",),
              "transcription": ("per_minute",)}
TWILIO_FIELDS = [("voice_per_minute", "Ligação, por minuto"), ("amd_per_call", "Detecção de secretária (AMD), por chamada")]


def cost_inputs(kind, saved, prefix):
    """Text fields for the rates of one option; empty means no price (unknown cost)."""
    values = {rate: st.text_input(RATE_LABELS[rate], value=saved.get(rate) or "", key=f"{prefix}{rate}")
              for rate in KIND_RATES[kind]}
    return {rate: value.strip() or None for rate, value in values.items()}


def pricing_form():
    models_section()
    twilio_form()


def models_section():
    st.subheader("Modelos de IA")
    st.caption("Cadastre os modelos de cada opção com o custo em USD (ponto como separador decimal, ex.: 0.15). "
               "Preços da OpenAI: https://openai.com/api/pricing. Novos custos valem para as próximas requisições; "
               "eventos antigos mantêm o preço da época.")
    models = api.models()
    st.dataframe([{"Modelo": m["name"], "Opção": KIND_LABELS[m["kind"]], "Em uso": "sim" if m["active"] else "",
                   **{RATE_LABELS[k]: v or "" for k, v in m["costs"].items()}} for m in models],
                 hide_index=True, use_container_width=True)
    selection_form(models)
    with st.expander("Cadastrar modelo"):
        kind = st.selectbox("Opção", list(KIND_LABELS), format_func=KIND_LABELS.get, key="new-model-kind")
        with st.form("new-model"):
            name = st.text_input("Nome do modelo (como na API, ex.: gpt-6.2-sol)", key="new-model-name")
            costs = cost_inputs(kind, {}, "new-model-cost-")
            create = st.form_submit_button("Cadastrar modelo")
        if create:
            try: api.create_model(name, kind, {k: v for k, v in costs.items() if v is not None})
            except ValueError as exc:
                st.error(str(exc))
                return
            changed("Modelo cadastrado.")
    by_id = {m["id"]: m for m in models}
    selected = st.selectbox("Editar ou excluir modelo", list(by_id), index=None, key="edit-model",
                            format_func=lambda id: f'{by_id[id]["name"]} — {KIND_LABELS[by_id[id]["kind"]]}',
                            placeholder="Selecione explicitamente")
    if selected is None:
        return
    model = by_id[selected]
    with st.form(f"edit-model-{selected}"):
        name = st.text_input("Nome do modelo", value=model["name"], key=f"edit-model-name-{selected}")
        costs = cost_inputs(model["kind"], model["costs"], f"edit-model-cost-{selected}-")
        save = st.form_submit_button("Salvar modelo")
    if save:
        try: api.update_model(selected, name, model["kind"], costs)
        except ValueError as exc:
            st.error(str(exc))
            return
        changed("Modelo salvo.")
    if model["active"]:
        st.caption("Este modelo está em uso: escolha outro modelo para a opção antes de excluí-lo.")
    confirm = st.checkbox("Confirmo a exclusão deste modelo", key=f"delete-model-confirm-{selected}")
    if st.button("Excluir modelo", disabled=not confirm or model["active"], key=f"delete-model-{selected}"):
        api.delete_model(selected)
        changed("Modelo excluído.")


def selection_form(models):
    st.markdown("**Modelo em uso por opção**")
    st.caption("Trocar o modelo de áudios exige gerar de novo os áudios das mensagens preparadas. Trocar o modelo "
               "de embeddings deixa os produtos indisponíveis até clicar em \"Atualizar embeddings\" em Produtos.")
    current = {kind: chosen["id"] for kind, chosen in api.model_selection().items()}
    names = {m["id"]: m["name"] for m in models}
    with st.form("model-selection"):
        chosen = {}
        for kind, label in KIND_LABELS.items():
            options = [m["id"] for m in models if m["kind"] == kind]
            chosen[kind] = st.selectbox(label, options, format_func=names.get, key=f"active-{kind}",
                                        index=options.index(current[kind]) if current.get(kind) in options else None)
        save = st.form_submit_button("Salvar modelos em uso")
    if save:
        if None in chosen.values():
            st.error("Escolha um modelo para cada opção.")
            return
        api.select_models(chosen["text"], chosen["audio"], chosen["embedding"], chosen["transcription"])
        changed("Modelos em uso salvos. Valem para as próximas requisições.")


def twilio_form():
    st.subheader("Custos da Twilio (USD)")
    prices = api.pricing_settings()
    source = {"settings": "salvos neste painel", "environment": "variável OBSERVABILITY_PRICING_JSON",
              "none": "nenhum valor configurado"}.get(prices["source"], prices["source"])
    st.caption(f"Valores em vigor: {source}. Tarifa de chamadas de saída para o país do lead em "
               "https://www.twilio.com/en-us/voice/pricing. O valor realmente cobrado de cada ligação aparece em "
               "Histórico, no botão de consulta de cobrança.")
    with st.form("pricing"):
        twilio = {rate: st.text_input(label, value=prices["twilio"].get(rate, ""), key=f"price-twilio-{rate}")
                  for rate, label in TWILIO_FIELDS}
        save = st.form_submit_button("Salvar custos da Twilio")
    if save:
        try: api.save_pricing_settings({k: v.strip() for k, v in twilio.items() if v.strip()})
        except ValueError as exc:
            st.error(str(exc))
            return
        changed("Custos da Twilio salvos. Valem para as próximas ligações.")


def outdated_embeddings(products):
    """Only products whose vectors came from another embedding model get the update button."""
    for product in products:
        if product.get("embeddings_current") is not False:
            continue
        st.warning(f'"{product["name"]}" usa embeddings de {product.get("embedding_model") or "um modelo excluído"}. '
                   "Fica indisponível para mensagens e ligações até atualizar.")
        if st.button("Atualizar embeddings", key=f'embeddings-{product["id"]}'):
            api.update_embeddings(product["id"])
            changed(f'Embeddings de "{product["name"]}" atualizados.')


if "notice" in st.session_state:
    st.success(st.session_state.pop("notice"))
page = st.sidebar.radio("Seção", ["Produtos", "Leads", "Histórico", "Observabilidade", "Configurações"])
try:
    if page == "Produtos":
        products = editor("products", "Produtos e serviços", [("name", "Nome"), ("description", "Descrição comercial"),
                                                               ("goal", "Objetivo da ligação (opcional)")])
        outdated_embeddings(products)
        st.caption("Objetivo: o que a ligação precisa conseguir (vender, obter dados do lead, agendar uma reunião). "
                   "Ele orienta os textos gerados e as respostas do agente, e a chamada só conta como sucesso "
                   "quando o lead aceita esse objetivo.")
    elif page == "Leads":
        leads_page()
    elif page == "Histórico":
        history_page()
    elif page == "Observabilidade":
        observability_page()
    else:
        settings_page()
except (APIError, ImportValidationError) as exc:
    st.error(str(exc))
