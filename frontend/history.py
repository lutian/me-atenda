"""Readable history using current registry values, never call-time snapshots."""


def history_record(call, leads_by_id, products_by_id):
    lead = leads_by_id.get(call.get("lead_id")) or {}
    product = products_by_id.get(call.get("product_id")) or {}
    return {
        "id": call.get("id"),
        "Lead": lead.get("name") or "Lead indisponível",
        "Telefone": lead.get("phone") or "Telefone indisponível",
        "Produto": product.get("name") or "Produto indisponível",
        **{key: call.get(key) for key in ("status_final", "duration_seconds", "recording_status")},
    }


def history_label(record):
    return (f'Chamada {record["id"]} — {record["Lead"]} — '
            f'{record["Telefone"]} — {record["Produto"]}')
