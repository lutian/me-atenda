"""Offline public HTTP and operator presentation contracts."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from frontend.client import APIClient, APIError


class ObservabilityClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.requests = []
        cls.status = 200
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_request(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                cls.requests.append((self.command, self.path, self.headers.get("X-API-Key"), body))
                self.send_response(cls.status)
                if cls.status == 302:
                    self.send_header("Location", "/credential-leak")
                self.end_headers()
                payload = ([{"id": 1}] if self.path.startswith("/observability/events") else
                           {"status": "pending"} if self.command == "POST" else {"total_tokens": 7})
                self.wfile.write(json.dumps(payload if cls.status == 200 else {"secret": "NEVER DISPLAY"}).encode())
            do_GET = do_POST = handle_request
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.client = APIClient(f"http://127.0.0.1:{cls.server.server_port}", "server-only-key")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.requests.clear()
        type(self).status = 200

    def test_global_and_filtered_events_plain_array_and_limits(self):
        for kwargs, query in [({}, "limit=100"), ({"call_id": 7, "limit": 200}, "call_id=7&limit=200"),
                              ({"limit": 1}, "limit=1")]:
            self.assertEqual(self.client.observability_events(**kwargs), [{"id": 1}])
            self.assertEqual(self.requests[-1], ("GET", "/observability/events?" + query, "server-only-key", b""))

    def test_global_and_call_summary_objects(self):
        for call_id, path in [(None, "/observability/summary"), (7, "/observability/summary?call_id=7")]:
            self.assertEqual(self.client.observability_summary(call_id), {"total_tokens": 7})
            self.assertEqual(self.requests[-1], ("GET", path, "server-only-key", b""))

    def test_reconcile_is_single_explicit_post_without_price_or_body(self):
        self.assertEqual(self.client.reconcile_billing(7), {"status": "pending"})
        self.assertEqual(self.requests, [("POST", "/calls/7/billing/reconcile", "server-only-key", b"")])

    def test_invalid_ids_and_limits_make_no_requests(self):
        for value in [0, -1, True, "7", "https://evil"]:
            for action in [lambda: self.client.observability_events(value),
                           lambda: self.client.observability_summary(value),
                           lambda: self.client.reconcile_billing(value)]:
                with self.assertRaises(ValueError):
                    action()
        for value in [0, 201, True, "100", None]:
            with self.assertRaises(ValueError):
                self.client.observability_events(limit=value)
        self.assertEqual(self.requests, [])

    def test_new_endpoints_refuse_redirects_and_sanitize_http_errors(self):
        for status in [302, 401, 404, 503]:
            type(self).status = status
            for action in [self.client.observability_events, self.client.observability_summary,
                           lambda: self.client.reconcile_billing(7)]:
                self.requests.clear()
                with self.assertRaises(APIError) as error:
                    action()
                self.assertEqual(str(error.exception), f"API returned HTTP {status}.")
                self.assertEqual(len(self.requests), 1)


class PresentationTests(unittest.TestCase):
    def test_summary_known_tokens_partial_usd_and_native_billing_stay_separate(self):
        from frontend.observability import summary_lines
        lines = summary_lines({"request_count": 5, "error_count": 1, "input_tokens": 10,
                               "cached_input_tokens": 3, "output_tokens": 2, "total_tokens": 12,
                               "unknown_usage_events": 2, "estimated_cost_usd": "0.001200",
                               "cost_status": "partial", "excluded_costs": ["recording", "infra"],
                               "billed_connectivity": {"status": "available", "amount": "1.23",
                                                        "currency": "BRL", "updated_at": "2026-01-01T00:00:00Z"}})
        text = "\n".join(lines)
        for expected in ["5", "Erros: 1", "entrada=10", "cache=3", "saída=2", "total=12", "desconhecido: 2",
                         "Subtotal estimado USD: 0.001200 (partial)", "recording", "infra", "1.23 BRL", "available"]:
            self.assertIn(expected, text)
        self.assertNotIn("1.231200", text)
        self.assertIn("não somar", text)

    def test_unknown_no_usage_and_non_token_events_never_invent_zero(self):
        from frontend.observability import summary_lines, event_rows
        for status in ["unknown", "no_usage"]:
            self.assertIn(f"Subtotal estimado USD: desconhecido ({status})", "\n".join(summary_lines({"cost_status": status})))
        rows = event_rows([{"provider": "openai", "operation": "tts", "characters": 40,
                            "total_tokens": None, "cost_status": "unknown", "estimated_cost_usd": None},
                           {"provider": "local", "operation": "stt", "cost_status": "not_applicable"}])
        self.assertEqual(rows[0]["total_tokens"], "n/a")
        self.assertEqual(rows[0]["characters"], 40)
        self.assertEqual(rows[0]["estimated_cost_usd"], "desconhecido")
        self.assertEqual(rows[1]["estimated_cost_usd"], "n/a")

    def test_events_keep_utc_safe_ids_errors_elapsed_and_unknown_tokens(self):
        from frontend.observability import event_rows
        row = event_rows([{"created_at": "2026-01-01T03:00:00+03:00", "provider_request_id": "req_safe",
                           "status": "error", "elapsed_ms": 23, "operation": "chat", "error_code": "timeout",
                           "error_message": "<script>text only</script>", "cached_input_tokens": 2}])[0]
        self.assertEqual(row["created_at_utc"], "2026-01-01T00:00:00Z")
        for key, value in [("provider_request_id", "req_safe"), ("status", "error"), ("elapsed_ms", 23),
                           ("error_code", "timeout"), ("error_message", "<script>text only</script>"),
                           ("cached_input_tokens", 2), ("total_tokens", "desconhecido")]:
            self.assertEqual(row[key], value)

    def test_mixed_global_and_call_events_have_arrow_safe_display_types(self):
        from frontend.observability import render_audit
        class UI:
            rows = None
            def text(self, value): pass
            def caption(self, value): pass
            def subheader(self, value): pass
            def dataframe(self, rows, **kwargs): self.rows = rows
            def button(self, *args, **kwargs): return False
        class API:
            def observability_summary(self, call_id): return {"cost_status": "partial"}
            def observability_events(self, call_id, limit):
                return [{"id": "global", "call_id": None, "operation": "embed",
                         "provider": "openai", "elapsed_ms": 2, "input_tokens": 4},
                        {"id": "call", "call_id": 7, "operation": "tts",
                         "provider": "openai", "characters": 40, "elapsed_ms": None}]
        for scope in (None, 7):
            ui = UI()
            render_audit(ui, API(), scope)
            self.assertEqual([row["call_id"] for row in ui.rows], ["n/a", "7"])
            self.assertEqual(ui.rows[0]["input_tokens"], "4")
            self.assertEqual(ui.rows[1]["characters"], "40")
            self.assertTrue(all(isinstance(value, str) for row in ui.rows for value in row.values()))
            # The frontend image provides the actual serializers; host runs the type contract.
            import importlib.util
            if importlib.util.find_spec("pandas") and importlib.util.find_spec("pyarrow"):
                import pandas as pd
                import pyarrow as pa
                table = pa.Table.from_pandas(pd.DataFrame(ui.rows), preserve_index=False)
                self.assertEqual(table.column("call_id").to_pylist(), ["n/a", "7"])

    def test_render_read_only_until_explicit_reconcile_and_uses_text(self):
        from frontend.observability import render_audit
        class UI:
            clicked = False
            texts = []
            def text(self, value): self.texts.append(value)
            def caption(self, value): pass
            def subheader(self, value): pass
            def dataframe(self, *args, **kwargs): pass
            def button(self, *args, **kwargs): return self.clicked
        class API:
            reconciliations = []
            def observability_summary(self, call_id): return {"cost_status": "unknown"}
            def observability_events(self, call_id, limit): return []
            def reconcile_billing(self, call_id):
                self.reconciliations.append(call_id)
                return {"status": "pending"}
        ui, api = UI(), API()
        render_audit(ui, api, None)
        render_audit(ui, api, 7)
        self.assertEqual(api.reconciliations, [])
        ui.clicked = True
        render_audit(ui, api, 7)
        self.assertEqual(api.reconciliations, [7])
        self.assertIn("pending", "\n".join(ui.texts))
