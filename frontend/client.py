"""Server-side API boundary. Never pass the API key or upstream URLs to widgets."""
import json
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class APIError(Exception):
    """Safe operator-facing error, without upstream bodies or credentials."""


class APIClient:
    def __init__(self, base_url, api_key, timeout=30):
        if not api_key or not base_url.startswith(("http://", "https://")):
            raise ValueError("Backend URL and ADMIN_API_KEY must be configured.")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._http = build_opener(NoRedirect)

    @staticmethod
    def _id(value):
        if type(value) is not int or value <= 0:
            raise ValueError("Select a valid integer ID.")
        return value

    @staticmethod
    def _resource(value):
        if value not in ("products", "leads", "calls"):
            raise ValueError("Unknown resource.")
        return value

    def _request(self, method, path, payload=None, binary=False):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(self.base_url + path, data=data, method=method,
                          headers={"X-API-Key": self.api_key, "Content-Type": "application/json"})
        try:
            with self._http.open(request, timeout=self.timeout) as response:
                content = response.read()
                if binary:
                    return content, response.headers.get_content_type()
                return json.loads(content) if content else None
        except HTTPError as exc:
            code = exc.code
            exc.close()
            hint = " Another call may be active; refresh history." if code == 409 else ""
            raise APIError(f"API returned HTTP {code}.{hint}") from None
        except (URLError, TimeoutError, OSError):
            raise APIError("Backend unavailable or timed out. Refresh history before retrying a write or call.") from None
        except (ValueError, UnicodeError):
            raise APIError("Backend returned an invalid response.") from None

    @staticmethod
    def _call_policy(data):
        value = data.get('max_duration_seconds') if isinstance(data, dict) else None
        if type(value) is not int or not 1 <= value <= 14400:
            raise APIError('Backend returned an invalid call policy.')
        return {'max_duration_seconds': value}

    def call_policy(self):
        return self._call_policy(self._request('GET', '/settings/calls'))

    def save_call_policy(self, seconds):
        if type(seconds) is not int or not 1 <= seconds <= 14400:
            raise ValueError('Maximum call duration must be an integer from 1 to 14400 seconds.')
        return self._call_policy(self._request('PATCH', '/settings/calls',
                                             {'max_duration_seconds': seconds}))

    def list(self, resource):
        return self._request("GET", "/" + self._resource(resource))

    def create(self, resource, payload):
        return self._request("POST", "/" + self._resource(resource), payload)

    def update(self, resource, item_id, payload):
        return self._request("PATCH", f"/{self._resource(resource)}/{self._id(item_id)}", payload)

    def delete(self, resource, item_id):
        return self._request("DELETE", f"/{self._resource(resource)}/{self._id(item_id)}")

    def import_leads(self, leads):
        return self._request("POST", "/leads/import", leads)

    def dispatch(self, lead_id, product_id):
        return self._request("POST", "/calls", {"lead_id": self._id(lead_id), "product_id": self._id(product_id)})

    def call(self, call_id):
        return self._request("GET", f"/calls/{self._id(call_id)}")

    def observability_events(self, call_id=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("Event limit must be an integer from 1 to 200.")
        query = f"call_id={self._id(call_id)}&" if call_id is not None else ""
        return self._request("GET", f"/observability/events?{query}limit={limit}")

    def observability_summary(self, call_id=None):
        query = f"?call_id={self._id(call_id)}" if call_id is not None else ""
        return self._request("GET", "/observability/summary" + query)

    def reconcile_billing(self, call_id):
        return self._request("POST", f"/calls/{self._id(call_id)}/billing/reconcile")

    def audio(self, call_id):
        # Ignore audio_url from API data: use the fixed authenticated endpoint only.
        return self._request("GET", f"/calls/{self._id(call_id)}/audio", binary=True)
