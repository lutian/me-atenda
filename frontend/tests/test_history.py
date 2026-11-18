"""History UI contracts with synthetic records and no backend/provider traffic."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

FRONTEND = Path(__file__).resolve().parents[1]
HAS_STREAMLIT = importlib.util.find_spec("streamlit") is not None
CALLS = [
    {"id": 11, "lead_id": 701, "product_id": 901, "status_final": "sucesso",
     "duration_seconds": 32, "recording_status": "available"},
    {"id": 22, "lead_id": 702, "product_id": 902, "status_final": "inconclusivo",
     "duration_seconds": 6, "recording_status": "pending"},
]
LEADS = [{"id": 702, "name": "Bruno Teste", "phone": "+5511888888888"},
         {"id": 701, "name": "Ana Teste", "phone": "+5511999999999"}]
PRODUCTS = [{"id": 902, "name": "Plano Beta"}, {"id": 901, "name": "Plano Alfa"}]


class FakeAPI:
    def __init__(self):
        self.requests = []
        self.leads = LEADS
        self.products = PRODUCTS

    def list(self, resource):
        self.requests.append(("list", resource))
        return {"calls": CALLS, "leads": self.leads, "products": self.products}[resource]

    def call(self, call_id):
        self.requests.append(("call", call_id))
        return dict(next(call for call in CALLS if call["id"] == call_id),
                    transcription_json=[{"role": "assistant", "content": f"Conversa {call_id}"}])

    def observability_summary(self, call_id=None):
        self.requests.append(("summary", call_id))
        return {"request_count": call_id, "cost_status": "partial",
                "estimated_cost_usd": "0.01", "billed_connectivity": {
                    "status": "available", "amount": "1.23", "currency": "BRL"}}

    def observability_events(self, call_id=None, limit=100):
        self.requests.append(("events", call_id, limit))
        return [{"call_id": call_id, "operation": f"chat_{call_id}", "provider": "openai"}]

    def audio(self, call_id):
        self.requests.append(("audio", call_id))
        return b"synthetic-audio", "audio/wav"

    def reconcile_billing(self, call_id):
        self.requests.append(("billing", call_id))
        return {"status": "pending"}

    def __getattr__(self, name):
        raise AssertionError(f"Unexpected API action: {name}")


@unittest.skipUnless(HAS_STREAMLIT, "Streamlit absent on host; run AppTest in frontend image")
class HistoryAppTests(unittest.TestCase):
    def setUp(self):
        from streamlit.testing.v1 import AppTest
        self.api = FakeAPI()
        self.paths = patch.object(sys, "path", [str(FRONTEND), *sys.path])
        self.paths.start()
        self.addCleanup(self.paths.stop)
        import client
        self.factory = patch.object(client, "APIClient", lambda *args: self.api)
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.app = AppTest.from_file(str(FRONTEND / "app.py"), default_timeout=5).run()
        self.api.requests.clear()
        self.app.sidebar.radio[0].set_value("Histórico").run()
        self.assertFalse(self.app.exception)

    def select(self, call_id):
        self.api.requests.clear()
        self.app.selectbox[0].set_value(call_id).run()
        self.assertFalse(self.app.exception)

    def assert_readonly(self):
        self.assertFalse([r for r in self.api.requests if r[0] not in
                          ("list", "call", "summary", "events")])

    def test_s1_readable_table_selection_and_details_without_n_plus_one(self):
        table = self.app.dataframe[0].value
        self.assertIn("Lead", table.columns)
        self.assertEqual(list(table["Lead"]), ["Ana Teste", "Bruno Teste"])
        self.assertEqual(list(table["Telefone"]), ["+5511999999999", "+5511888888888"])
        self.assertEqual(list(table["Produto"]), ["Plano Alfa", "Plano Beta"])
        self.assertEqual(self.app.selectbox[0].options,
                         ["Chamada 11 — Ana Teste — +5511999999999 — Plano Alfa",
                          "Chamada 22 — Bruno Teste — +5511888888888 — Plano Beta"])
        for call_id, name, phone, product in [(11, "Ana Teste", "+5511999999999", "Plano Alfa"),
                                             (22, "Bruno Teste", "+5511888888888", "Plano Beta")]:
            self.select(call_id)
            details = self.app.json[0].value
            for expected in [name, phone, product]:
                self.assertIn(expected, details)
            visible = str(self.app.dataframe[0].value) + details + str(self.app.selectbox[0].options)
            for forbidden in ["lead_id", "product_id", "701", "702", "901", "902"]:
                self.assertNotIn(forbidden, visible)
            for resource in ["calls", "leads", "products"]:
                self.assertEqual(self.api.requests.count(("list", resource)), 1)
            self.assert_readonly()

    def test_s2_scoped_audit_visible_before_media_and_refreshes_selection(self):
        self.assertFalse([r for r in self.api.requests if r[0] in ("summary", "events")])
        for call_id in [11, 22]:
            self.select(call_id)
            headings = [heading.value for heading in self.app.subheader]
            self.assertIn("Observabilidade desta conversa", headings)
            self.assertLess(headings.index("Observabilidade desta conversa"), headings.index("Gravação"))
            self.assertEqual([r for r in self.api.requests if r[0] in ("summary", "events")],
                             [("summary", call_id), ("events", call_id, 100)])
            self.assertEqual(list(self.app.dataframe[1].value["call_id"]), [str(call_id)])
            self.assertEqual(list(self.app.dataframe[1].value["operation"]), [f"chat_{call_id}"])
            text = "\n".join(element.value for element in self.app.text)
            self.assertIn(f"Requisições: {call_id}", text)
            self.assertIn("USD: 0.01 (partial)", text)
            self.assertIn("1.23 BRL", text)
            self.assert_readonly()

    def test_missing_joins_never_substitute_other_records(self):
        self.api.leads = [LEADS[0]]
        self.api.products = [PRODUCTS[0]]
        self.app.run()  # Refresh labels before making the explicit selection.
        self.select(11)
        table = self.app.dataframe[0].value
        self.assertEqual(table.iloc[0]["Lead"], "Lead indisponível")
        self.assertEqual(table.iloc[0]["Telefone"], "Telefone indisponível")
        self.assertEqual(table.iloc[0]["Produto"], "Produto indisponível")
        details = str(json.loads(self.app.json[0].value))
        for label in ["Lead indisponível", "Telefone indisponível", "Produto indisponível"]:
            self.assertIn(label, details)
            self.assertIn(label, self.app.selectbox[0].options[0])
        for forbidden in ["Bruno", "Beta", "701", "901"]:
            self.assertNotIn(forbidden, details)
        self.assert_readonly()

    def test_preserve_status_transcript_audio_and_explicit_only_billing(self):
        self.select(11)
        details = self.app.json[0].value
        for expected in ["sucesso", "32", "available"]:
            self.assertIn(expected, details)
        self.assertIn("Conversa 11", "\n".join(t.value for t in self.app.text))
        self.assertIn("Baixar transcrição JSON", [b.label for b in self.app.get("download_button")])
        self.assert_readonly()
        self.api.requests.clear()
        self.app.button(key="audio-11").click().run()
        self.assertIn(("audio", 11), self.api.requests)
        self.assertNotIn(("billing", 11), self.api.requests)
        self.assertIn("Baixar gravação", [b.label for b in self.app.get("download_button")])
        self.api.requests.clear()
        self.app.button(key="billing-11").click().run()
        self.assertIn(("billing", 11), self.api.requests)
        self.assertNotIn(("audio", 11), self.api.requests)
