"""Server-side API boundary. Never pass the API key or upstream URLs to widgets."""
import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


MESSAGE_FIELDS = ('opening', 'pitch', 'goodbye')
RATE = re.compile(r'\d{1,18}(?:\.\d{1,18})?')
MODEL_NAME = re.compile(r'[A-Za-z0-9_.:/-]{1,120}')
KIND_RATES = {'text': ('input_per_million', 'cached_input_per_million', 'output_per_million'),
              'embedding': ('input_per_million',), 'audio': ('characters_per_million',)}


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

    def _request(self, method, path, payload=None, binary=False, timeout=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(self.base_url + path, data=data, method=method,
                          headers={"X-API-Key": self.api_key, "Content-Type": "application/json"})
        try:
            with self._http.open(request, timeout=timeout or self.timeout) as response:
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

    @staticmethod
    def _agent_profile(data):
        if not isinstance(data, dict): raise APIError('Backend returned an invalid agent profile.')
        name, voice, voices = data.get('agent_name'), data.get('tts_voice'), data.get('voices')
        if (not isinstance(name, str) or not isinstance(voice, str) or not isinstance(voices, list)
                or not all(isinstance(v, str) for v in voices)):
            raise APIError('Backend returned an invalid agent profile.')
        return {'agent_name': name, 'tts_voice': voice, 'voices': voices}

    def agent_profile(self):
        return self._agent_profile(self._request('GET', '/settings/agent'))

    def save_agent_profile(self, name, voice):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
            raise ValueError('Agent name must have 1 to 40 characters.')
        if not isinstance(voice, str) or not voice:
            raise ValueError('Select a voice.')
        return self._agent_profile(self._request('PATCH', '/settings/agent',
                                                 {'agent_name': name.strip(), 'tts_voice': voice}))

    @staticmethod
    def _pricing(data):
        keys = ('source', 'version', 'twilio')
        if not isinstance(data, dict) or any(k not in data for k in keys) or not isinstance(data['twilio'], dict):
            raise APIError('Backend returned invalid pricing.')
        return {k: data[k] for k in keys}

    def pricing_settings(self):
        return self._pricing(self._request('GET', '/settings/pricing'))

    @staticmethod
    def _rates(values):
        if not all(value is None or (isinstance(value, str) and RATE.fullmatch(value)) for value in values):
            raise ValueError('Rates must be non-negative numbers with a dot as decimal separator, e.g. 0.15.')

    def save_pricing_settings(self, twilio):
        self._rates(twilio.values())
        return self._pricing(self._request('PUT', '/settings/pricing', {'twilio': twilio}))

    @staticmethod
    def _model(data):
        if (not isinstance(data, dict) or type(data.get('id')) is not int or not isinstance(data.get('name'), str)
                or data.get('kind') not in KIND_RATES or not isinstance(data.get('costs'), dict)
                or type(data.get('active')) is not bool):
            raise APIError('Backend returned an invalid model.')
        return {k: data[k] for k in ('id', 'name', 'kind', 'costs', 'active')}

    @staticmethod
    def _checked_model(name, kind, costs):
        if not isinstance(name, str) or not MODEL_NAME.fullmatch(name.strip()):
            raise ValueError('Model name: 1 to 120 letters, digits, dot, dash, underscore, colon or slash.')
        if kind not in KIND_RATES: raise ValueError('Option must be text, audio or embedding.')
        if not set(costs) <= set(KIND_RATES[kind]): raise ValueError('These costs do not apply to this option.')
        APIClient._rates(costs.values())
        return {'name': name.strip(), 'kind': kind, 'costs': costs}

    def models(self):
        data = self._request('GET', '/models')
        if not isinstance(data, list): raise APIError('Backend returned invalid models.')
        return [self._model(item) for item in data]

    def create_model(self, name, kind, costs):
        return self._model(self._request('POST', '/models', self._checked_model(name, kind, costs)))

    def update_model(self, model_id, name, kind, costs):
        return self._model(self._request('PATCH', f'/models/{self._id(model_id)}',
                                         self._checked_model(name, kind, costs)))

    def delete_model(self, model_id):
        return self._request('DELETE', f'/models/{self._id(model_id)}')

    @staticmethod
    def _selection(data):
        if not isinstance(data, dict) or not all(
                isinstance(v, dict) and type(v.get('id')) is int and isinstance(v.get('name'), str) for v in data.values()):
            raise APIError('Backend returned an invalid model selection.')
        return data

    def model_selection(self):
        return self._selection(self._request('GET', '/settings/models'))

    def select_models(self, text, audio, embedding):
        return self._selection(self._request('PUT', '/settings/models', {
            'text': self._id(text), 'audio': self._id(audio), 'embedding': self._id(embedding)}))

    def update_embeddings(self, product_id):
        # Embeds the product description again with the model in use.
        return self._request('POST', f'/products/{self._id(product_id)}/embeddings', timeout=90)

    @staticmethod
    def _messages(data, status=True):
        valid = isinstance(data, dict) and all(
            k in data and (data[k] is None or isinstance(data[k], str)) for k in MESSAGE_FIELDS)
        if valid and status: valid = type(data.get('ready')) is bool
        if not valid: raise APIError('Backend returned invalid prepared messages.')
        return {k: data[k] for k in MESSAGE_FIELDS + (('ready',) if status else ())}

    def product_messages(self, product_id):
        return self._messages(self._request('GET', f'/products/{self._id(product_id)}/messages'))

    def draft_messages(self, product_id):
        return self._messages(self._request('POST', f'/products/{self._id(product_id)}/messages/draft'), False)

    def save_messages(self, product_id, opening, pitch, goodbye):
        product_id = self._id(product_id)
        if not isinstance(opening, str) or not 1 <= len(opening.strip()) <= 400:
            raise ValueError('Opening must have 1 to 400 characters.')
        if not isinstance(pitch, str) or not 1 <= len(pitch.strip()) <= 400:
            raise ValueError('Pitch must have 1 to 400 characters.')
        if not isinstance(goodbye, str) or not 1 <= len(goodbye.strip()) <= 200:
            raise ValueError('Goodbye must have 1 to 200 characters.')
        # Up to four speech syntheses run in this request.
        return self._messages(self._request('PUT', f'/products/{product_id}/messages',
                                            {'opening': opening.strip(), 'pitch': pitch.strip(),
                                             'goodbye': goodbye.strip()}, timeout=90))

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
