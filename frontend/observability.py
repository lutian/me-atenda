"""Text-only observability presentation; no provider calls or price arithmetic."""
from datetime import datetime, timezone


def known(value, missing="desconhecido"):
    return missing if value is None else value


def utc(value):
    if not value:
        return "desconhecido"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return "desconhecido (timestamp sem fuso)"
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError):
        return "desconhecido (timestamp inválido)"


def billing_line(billing):
    return (f'Conectividade faturada: {billing.get("status", "pending")} — '
            f'{known(billing.get("amount"))} {known(billing.get("currency"))}; '
            f'UTC: {utc(billing.get("updated_at"))}; erro: {known(billing.get("error_code"), "nenhum")}. '
            'Cobrança separada: não somar à estimativa; não é a fatura completa.')


def summary_lines(summary):
    return [
        f'Requisições: {known(summary.get("request_count"))} | Erros: {known(summary.get("error_count"))}',
        ('Tokens conhecidos: '
         f'entrada={known(summary.get("input_tokens"))}, cache={known(summary.get("cached_input_tokens"))} '
         f'(subconjunto da entrada), saída={known(summary.get("output_tokens"))}, '
         f'total={known(summary.get("total_tokens"))}.'),
        f'Eventos com uso desconhecido: {known(summary.get("unknown_usage_events"))}. '
        'Totais somam somente uso conhecido, não representam tokens de telefonia/TTS.',
        f'Subtotal estimado USD: {known(summary.get("estimated_cost_usd"))} '
        f'({summary.get("cost_status", "unknown")}). Não é uma fatura.',
        'Custos excluídos: ' + ', '.join(summary.get("excluded_costs") or ["nenhum informado; não implica cobertura completa"]),
        billing_line(summary.get("billed_connectivity") or {}),
    ]


def event_rows(events):
    rows = []
    for event in events:
        operation = str(event.get("operation", "")).lower()
        non_token = (event.get("characters") is not None or event.get("provider") in ("local", "twilio")
                     or any(part in operation for part in ("tts", "stt", "speech", "transcrib")))
        row = {key: known(event.get(key), "n/a") for key in (
            "id", "call_id", "provider", "operation", "model", "elapsed_ms", "status", "error_code",
            "error_message", "provider_request_id", "characters", "cost_status")}
        row["created_at_utc"] = utc(event.get("created_at"))
        for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
            row[key] = known(event.get(key), "n/a" if non_token else "desconhecido")
        row["estimated_cost_usd"] = known(event.get("estimated_cost_usd"),
                                          "n/a" if event.get("cost_status") == "not_applicable" else "desconhecido")
        rows.append(row)
    return rows


def render_audit(ui, api, call_id=None, limit=100):
    ui.subheader("Consumo e eventos de integrações")
    for line in summary_lines(api.observability_summary(call_id)):
        ui.text(line)
    ui.caption("Cálculo versionado no backend: uso real e tarifas persistidas por evento; mudanças de tarifas não reescrevem o histórico. "
               "STT local não tem cobrança de provedor inventada; TTS usa caracteres, não tokens. Infraestrutura não é gratuita por isso.")
    # Display-only text keeps missing-value labels and numbers Arrow-compatible.
    rows = [{key: str(value) for key, value in row.items()}
            for row in event_rows(api.observability_events(call_id, limit))]
    ui.dataframe(rows, hide_index=True, use_container_width=True)
    ui.caption("Eventos mais recentes dentro do limite; os totais abrangem todo o escopo, não apenas esta tabela. "
               "Erros são sanitizados; request IDs seguros e horários UTC auxiliam a investigação.")
    if call_id is not None:
        ui.caption("Reconciliação consulta somente a chamada já vinculada na Twilio, sem discar. "
                   "A cobrança é assíncrona; pendente pode exigir nova consulta manual posterior. Sem retry automático.")
        if ui.button("Consultar cobrança de conectividade na Twilio", key=f"billing-{call_id}"):
            ui.text(billing_line(api.reconcile_billing(call_id)))
