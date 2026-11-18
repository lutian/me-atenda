import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from frontend.client import APIClient, APIError
from frontend.imports import ImportValidationError, parse_leads


class ImportTests(unittest.TestCase):
    def test_json_preserves_documented_fields(self):
        leads = [{"name": "Ana", "phone": "+5511999999999", "company": "ACME"}]
        self.assertEqual(parse_leads(json.dumps(leads), "leads.json"), leads)

    def test_csv_converts_to_json_objects(self):
        self.assertEqual(parse_leads("name,phone,company\nAna,+5511999999999,ACME\n", "leads.csv"),
                         [{"name": "Ana", "phone": "+5511999999999", "company": "ACME"}])

    def test_phone_boundaries_and_whitespace_normalization(self):
        for phone in ["+12345678", "+123456789012345"]:
            self.assertEqual(parse_leads(json.dumps([{"name": " Ana ", "phone": phone, "company": " ACME "}]), "a.json"),
                             [{"name": "Ana", "phone": phone, "company": "ACME"}])
        for phone in ["+1234567", "+1234567890123456"]:
            with self.assertRaises(ImportValidationError):
                parse_leads(json.dumps([{"name": "Ana", "phone": phone}]), "a.json")

    def test_optional_company_defaults_empty(self):
        self.assertEqual(parse_leads('[{"name":"Ana","phone":"+5511999999999"}]', "a.json")[0]["company"], "")

    def test_rejects_invalid_import_atomically(self):
        cases = ['{}', '[]', '[1]', '[{"name":"Ana"}]',
                 '[{"name":"Ana","phone":123}]',
                 '[{"name":" ","phone":"+5511999999999"}]',
                 '[{"name":"Ana","phone":"11999999999"}]',
                 '[{"name":"Ana","phone":"+0123"}]',
                 '[{"name":"Ana","phone":"+5511999999999","extra":1}]',
                 '[{"name":"Ana","phone":"+5511999999999"},{"name":"Bad"}]',
                 'not json']
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ImportValidationError):
                parse_leads(text, "a.json")

    def test_rejects_wrong_csv_schema_and_type(self):
        for text, filename in [("name,phone,other\nAna,+5511999999999,x", "a.csv"),
                               ("name,phone\nAna,+5511999999999,extra", "a.csv"),
                               ("anything", "a.txt")]:
            with self.subTest(filename=filename, text=text), self.assertRaises(ImportValidationError):
                parse_leads(text, filename)


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_request(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                cls.requests.append((self.command, self.path, self.headers.get("X-API-Key"), body))
                if self.path == "/calls/302":
                    self.send_response(302)
                    self.send_header("Location", "/calls/2")
                    self.end_headers()
                elif self.path == "/calls/409":
                    self.send_response(409)
                    self.end_headers()
                    self.wfile.write(b'{"detail":"active call"}')
                elif self.path.endswith("/audio"):
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.end_headers()
                    self.wfile.write(b"RIFF-audio")
                else:
                    self.send_response(204 if self.command == "DELETE" else 200)
                    self.end_headers()
                    if self.command != "DELETE":
                        self.wfile.write(b'[]' if self.command == "GET" else b'{"id":1}')

            do_GET = do_POST = do_PATCH = do_DELETE = handle_request
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

    def test_crud_uses_exact_paths_auth_and_fields(self):
        for resource, payload in [("products", {"name": "P", "description": "D"}),
                                  ("leads", {"name": "A", "phone": "+5511999999999", "company": "C"})]:
            self.assertEqual(self.client.list(resource), [])
            self.assertEqual(self.client.create(resource, payload), {"id": 1})
            self.client.update(resource, 1, payload)
            self.assertIsNone(self.client.delete(resource, 1))
            for method, path, key, body in self.requests[-4:]:
                self.assertEqual(key, "server-only-key")
                self.assertEqual(path, f"/{resource}" + ("/1" if method in ("PATCH", "DELETE") else ""))
                if method in ("POST", "PATCH"):
                    self.assertEqual(json.loads(body), payload)

    def test_dispatch_requires_product_and_sends_one_request(self):
        self.assertEqual(self.client.dispatch(3, 5), {"id": 1})
        self.assertEqual(len(self.requests), 1)
        method, path, key, body = self.requests[0]
        self.assertEqual((method, path, key), ("POST", "/calls", "server-only-key"))
        self.assertEqual(json.loads(body), {"lead_id": 3, "product_id": 5})

    def test_import_sends_array_without_envelope(self):
        leads = [{"name": "A", "phone": "+5511999999999", "company": ""}]
        self.client.import_leads(leads)
        self.assertEqual(self.requests[0][0:2], ("POST", "/leads/import"))
        self.assertEqual(json.loads(self.requests[0][3]), leads)

    def test_history_and_authenticated_audio(self):
        self.assertEqual(self.client.list("calls"), [])
        self.client.call(2)
        self.assertEqual(self.requests[-1][1], "/calls/2")
        self.assertEqual(self.client.audio(2), (b"RIFF-audio", "audio/wav"))
        self.assertEqual(self.requests[-1][1:3], ("/calls/2/audio", "server-only-key"))

    def test_errors_do_not_expose_credentials_or_response_body(self):
        with self.assertRaises(APIError) as raised:
            self.client.call(409)
        self.assertEqual(str(raised.exception), "API returned HTTP 409. Another call may be active; refresh history.")
        self.assertNotIn("server-only-key", str(raised.exception))

    def test_redirects_are_not_followed_with_admin_credentials(self):
        with self.assertRaises(APIError) as raised:
            self.client.call(302)
        self.assertEqual(str(raised.exception), "API returned HTTP 302.")
        self.assertEqual(len(self.requests), 1)

    def test_invalid_import_makes_no_requests(self):
        with self.assertRaises(ImportValidationError):
            self.client.import_leads(parse_leads('[{"name":"A","phone":"+5511999999999"},{"name":"Bad"}]', "a.json"))
        self.assertEqual(self.requests, [])

    def test_invalid_inputs_make_no_requests(self):
        for action in [lambda: self.client.dispatch(1, None), lambda: self.client.call("https://evil"),
                       lambda: self.client.list("../secret"), lambda: self.client.audio(True)]:
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
