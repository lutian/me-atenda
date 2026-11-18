"""Safe accounting primitives. No prompts, response bodies or exception text cross here."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
import json
import logging
import re
import time

CALL = ContextVar('accounting_call', default=None)
CURRENT = ContextVar('accounting_event', default=None)
LOG = logging.getLogger(__name__)
TOKEN_OPS = {'embed', 'answer'}
ERRORS = {'interrupted': 'Operation did not finalize', 'cancelled': 'Operation cancelled',
          'invalid_response': 'Invalid provider response', 'provider_error': 'Integration unavailable',
          'billing_invalid': 'Invalid connectivity charge', 'accounting_error': 'Accounting unavailable'}
EXCLUDED = ['recording', 'storage', 'streams', 'infrastructure', 'taxes',
            'connectivity estimate uses whole-minute ceiling; destination/carrier rates may differ']
RATE_KEYS = {'input_per_million', 'cached_input_per_million', 'output_per_million', 'characters_per_million'}
# Supported native billing units, not a currency conversion table.
CURRENCIES = {'USD', 'EUR', 'GBP', 'BRL', 'CAD', 'AUD', 'JPY', 'INR', 'MXN', 'CHF', 'ZAR', 'SGD', 'HKD', 'SEK', 'NOK', 'DKK', 'NZD', 'PLN'}


def decimal_string(value):
    return format(Decimal(value), 'f')


def pricing(raw):
    try:
        value = json.loads(raw) if raw else {}
        if not isinstance(value, dict): raise ValueError()
        if not value: return {}
        if set(value) - {'version', 'currency', 'models', 'twilio'}: raise ValueError()
        if (not isinstance(value.get('version'), str) or
                not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', value['version']) or value.get('currency') != 'USD'):
            raise ValueError()
        models, twilio = value.get('models', {}), value.get('twilio', {})
        if not isinstance(models, dict) or not isinstance(twilio, dict): raise ValueError()
        for model, rates in models.items():
            if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,120}', model): raise ValueError()
            validate_rates(rates, RATE_KEYS)
        validate_rates(twilio, {'voice_per_minute', 'amd_per_call'})
        return value
    except (ValueError, TypeError, InvalidOperation):
        raise ValueError('Invalid OBSERVABILITY_PRICING_JSON') from None


def validate_rates(rates, keys):
    if not isinstance(rates, dict) or set(rates) - keys: raise ValueError()
    for rate in rates.values():
        if not isinstance(rate, str) or not re.fullmatch(r'\d{1,18}(?:\.\d{1,18})?', rate): raise ValueError()
        if not Decimal(rate).is_finite() or Decimal(rate) < 0: raise ValueError()


@contextmanager
def call_scope(id):
    token = CALL.set(id)
    try: yield
    finally: CALL.reset(token)


def capture(response):
    """Capture usage immediately, before downstream schema/audio validation can fail."""
    event = CURRENT.get()
    if event is None: return
    event['response_received'] = True
    request_id = getattr(response, '_request_id', None)
    if isinstance(request_id, str) and re.fullmatch(r'req_[A-Za-z0-9_-]{1,100}', request_id):
        event['provider_request_id'] = request_id
    usage = getattr(response, 'usage', None)
    def count(obj, name):
        value = getattr(obj, name, None)
        return value if type(value) is int and 0 <= value <= 10**12 else None
    event['input_tokens'] = count(usage, 'prompt_tokens')
    event['total_tokens'] = count(usage, 'total_tokens')
    if event['operation'] == 'answer':
        event['output_tokens'] = count(usage, 'completion_tokens')
        cached = count(getattr(usage, 'prompt_tokens_details', None), 'cached_tokens')
        if cached is not None and event['input_tokens'] is not None and cached <= event['input_tokens']:
            event['cached_input_tokens'] = cached


def estimate(event):
    rates = event['pricing_snapshot'].get('rates', {})
    operation = event['operation']
    if operation in TOKEN_OPS:
        inputs, outputs, cached = (event[k] for k in ('input_tokens', 'output_tokens', 'cached_input_tokens'))
        if inputs is None or 'input_per_million' not in rates: return None
        amount = Decimal(inputs) * Decimal(rates['input_per_million'])
        if cached is not None and cached:
            if 'cached_input_per_million' not in rates: return None
            amount += Decimal(cached) * (Decimal(rates['cached_input_per_million']) - Decimal(rates['input_per_million']))
        # Missing cached subset does not mean zero cached tokens if a discounted rate differs.
        elif operation == 'answer' and cached is None and rates.get('cached_input_per_million', rates['input_per_million']) != rates['input_per_million']:
            return None
        if operation == 'answer':
            if outputs is None or 'output_per_million' not in rates: return None
            amount += Decimal(outputs) * Decimal(rates['output_per_million'])
        return decimal_string(amount / Decimal(1000000))
    if (operation == 'speak' and event.get('response_received') and
            event['characters'] is not None and 'characters_per_million' in rates):
        return decimal_string(Decimal(event['characters']) * Decimal(rates['characters_per_million']) / Decimal(1000000))
    return None


def instrument(operation, provider, model_attr=None):
    def decorate(fn):
        @wraps(fn)
        async def wrapped(self, *args, **kwargs):
            observer = getattr(self, 'observer', None)
            if observer is None: return await fn(self, *args, **kwargs)
            call_id = args[0]['id'] if operation == 'dial' else CALL.get()
            model = getattr(self.s, model_attr) if model_attr else None
            event = observer.event(call_id, provider, operation, model)
            if operation == 'speak': event['characters'] = len(args[0][:1800])
            # A durable interrupted marker precedes any external side effect.
            id = await asyncio.to_thread(observer.store.event_start, event)
            token = CURRENT.set(event)
            start = time.monotonic()
            try:
                result = await fn(self, *args, **kwargs)
                event.update(status='success', error_code=None, error_message=None)
                return result
            except asyncio.CancelledError:
                event.update(status='cancelled', error_code='cancelled', error_message=ERRORS['cancelled'])
                raise
            except Exception as exc:
                code = 'invalid_response' if isinstance(exc, (ValueError, KeyError, TypeError, IndexError)) else 'provider_error'
                event.update(status='error', error_code=code, error_message=ERRORS[code])
                raise
            finally:
                CURRENT.reset(token)
                event['elapsed_ms'] = max(0, int((time.monotonic() - start) * 1000))
                event['estimated_cost_usd'] = estimate(event)
                if event['estimated_cost_usd'] is not None: event['cost_status'] = 'estimated'
                # Never replace a definitive dial rejection/result with an accounting exception.
                # The durable interrupted marker remains visible if final persistence fails.
                async def persist():
                    try: await asyncio.to_thread(observer.store.event_finish, id, event)
                    except Exception:
                        LOG.error('accounting_finalize_failed event_id=%s status=%s elapsed_ms=%s input_tokens=%s output_tokens=%s '
                                  'cached_input_tokens=%s total_tokens=%s characters=%s estimated_cost_usd=%s',
                                  id, event['status'], event['elapsed_ms'], event['input_tokens'], event['output_tokens'], event['cached_input_tokens'],
                                  event['total_tokens'], event['characters'], event['estimated_cost_usd'])
                await asyncio.shield(persist())
        return wrapped
    return decorate


class Recorder:
    def __init__(self, store, settings):
        self.store = store
        self.prices = pricing(settings.observability_pricing_json)
        self.source = 'environment' if self.prices else 'none'

    def use(self, prices, source):
        self.prices, self.source = prices, source

    def event(self, call_id, provider, operation, model):
        rates = dict(self.prices.get('models', {}).get(model, {}))
        snapshot = {'version': self.prices.get('version'), 'currency': 'USD', 'rates': rates}
        return dict(call_id=call_id, provider=provider, operation=operation, model=model,
                    created_at=datetime.now(timezone.utc).isoformat(), elapsed_ms=0, status='error',
                    error_code='interrupted', error_message=ERRORS['interrupted'], provider_request_id=None,
                    input_tokens=None, output_tokens=None, cached_input_tokens=None, total_tokens=None,
                    characters=None, estimated_cost_usd=None,
                    cost_status='unknown' if operation in TOKEN_OPS | {'speak', 'dial'} else 'not_applicable',
                    pricing_snapshot=snapshot)


def native_charge(resource):
    price = resource.get('price')
    if price is None: return None, None
    currency = resource.get('price_unit')
    if not isinstance(currency, str) or currency.upper() not in CURRENCIES: raise ValueError('Invalid billing currency')
    if not isinstance(price, str) or not re.fullmatch(r'-?\d{1,18}(?:\.\d{1,18})?', price): raise ValueError('Invalid billing price')
    value = Decimal(price)
    # Twilio Call.price documents negative debit charges. Positive credits are NOT debits.
    if not value.is_finite() or value > 0: raise ValueError('Invalid billing charge sign')
    return decimal_string(-value if value else Decimal(0)), currency.upper()


def billed_empty(call_id):
    return dict(status='not_applicable' if call_id is None else 'pending', amount=None, currency=None,
                updated_at=None, error_code=None)


def summarize(events, call_id, billed=None):
    totals = {key: sum(e.get(key) or 0 for e in events) for key in
              ('input_tokens', 'output_tokens', 'cached_input_tokens', 'total_tokens')}
    known = [Decimal(e['estimated_cost_usd']) for e in events if e.get('estimated_cost_usd') is not None]
    unknown = sum(any(e.get(key) is None for key in
                      (('input_tokens', 'output_tokens', 'total_tokens') if e['operation'] == 'answer'
                       else ('input_tokens', 'total_tokens')))
                  for e in events if e['operation'] in TOKEN_OPS)
    # Telecom extras have no configured rates; a known subtotal is always partial.
    status = 'partial' if known else 'unknown' if events else 'no_usage'
    return dict(call_id=call_id, currency='USD', request_count=sum(e['operation'] != 'connectivity_estimate' for e in events),
                error_count=sum(e['status'] != 'success' for e in events), **totals,
                unknown_usage_events=unknown, estimated_cost_usd=decimal_string(sum(known)) if known else None,
                cost_status=status, excluded_costs=list(EXCLUDED),
                billed_connectivity=billed or billed_empty(call_id))
