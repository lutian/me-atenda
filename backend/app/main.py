"""Frozen admin API and publicly reachable, signed Twilio callbacks."""
import asyncio
import hmac
import logging
import re
import time
from typing import Optional
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, WebSocket
from pydantic import BaseModel, ConfigDict, Field
from .config import Settings
from .providers import CallCreationRejected, Providers
from .observability import Recorder, billed_empty, call_scope
from .security import public_url, signature_valid, websocket_signature_valid
from .store import Store
from .voice import session
from .openings import prepared_call
from .answering import VERDICTS, twiml, ulaw_wav


class Product(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=20000)


class ProductPatch(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, min_length=1, max_length=20000)


class Lead(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(pattern=r'^\+[1-9][0-9]{7,14}$')
    company: str = Field(default='', max_length=200)


class LeadPatch(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    phone: Optional[str] = Field(default=None, pattern=r'^\+[1-9][0-9]{7,14}$')
    company: Optional[str] = Field(default=None, max_length=200)


class CallPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    max_duration_seconds: int = Field(strict=True, ge=1, le=14400)


class Dial(BaseModel):
    model_config = ConfigDict(extra='forbid')
    lead_id: int = Field(gt=0)
    product_id: int = Field(gt=0)


def create_app(settings=None, store=None, providers=None):
    s = settings or Settings.environment()
    db = store or Store(s.database_url)
    provider = providers or Providers(s)
    recorder = Recorder(db, s)
    if isinstance(provider, Providers): provider.observer = recorder
    app = FastAPI(title='Outbound agent', docs_url=None, redoc_url=None, openapi_url=None)

    async def run(name, *args):
        try: return await asyncio.to_thread(getattr(db, name), *args)
        except LookupError: raise HTTPException(404, 'Not found')
        except ValueError as exc: raise HTTPException(409, str(exc))

    def admin(x_api_key: str = Header(default='')):
        if not s.admin_key or not hmac.compare_digest(x_api_key.encode(), s.admin_key.encode()):
            raise HTTPException(401, 'Invalid API key')

    def visible(row):
        return {key: row[key] for key in ('id', 'lead_id', 'product_id', 'status_final',
                'duration_seconds', 'transcription_json', 'recording_status')} | {
                'audio_url': f"/calls/{row['id']}/audio" if row['recording_status'] == 'available' else None}

    async def signed(request):
        body = await request.body()
        if len(body) > 32768: raise HTTPException(413, 'Callback too large')
        form = await request.form()
        url = public_url(s.public_base, request.scope.get('raw_path', b'').decode('ascii'),
                         request.scope.get('query_string', b''))
        if not signature_valid(s.twilio_token, url, form, request.headers.get('x-twilio-signature', '')):
            raise HTTPException(403, 'Invalid Twilio signature')
        return form

    def call_sid(form):
        sid = form.get('CallSid', '')
        if not re.fullmatch(r'CA[0-9a-fA-F]{32}', sid): raise HTTPException(422, 'Invalid CallSid')
        return sid

    async def identified(id, form):
        return await run('identify', id, call_sid(form))

    @app.get('/health')
    async def health():
        try:
            await run('health')
            return {'status': 'ok'}
        except Exception: raise HTTPException(503, 'Database unavailable or migrations missing')

    @app.get('/settings/calls', dependencies=[Depends(admin)])
    async def call_policy(): return await run('call_policy')

    @app.patch('/settings/calls', dependencies=[Depends(admin)])
    async def save_call_policy(data: CallPolicy):
        return await run('save_call_policy', data.max_duration_seconds)

    @app.get('/products', dependencies=[Depends(admin)])
    async def products(): return await run('list', 'products')

    async def save_product(data, id=None):
        text = data['name'] + '\n' + data['description']
        chunks = [text[i:i+1200] for i in range(0, len(text), 1000)]
        try: vectors = await provider.embed(chunks)
        except Exception: raise HTTPException(503, 'Embedding provider unavailable')
        if len(vectors) != len(chunks): raise HTTPException(503, 'Incomplete embeddings')
        return await run('product', data, list(zip(chunks, vectors)), id)

    @app.post('/products', status_code=201, dependencies=[Depends(admin)])
    async def product_create(data: Product): return await save_product(data.model_dump())

    @app.patch('/products/{id}', dependencies=[Depends(admin)])
    async def product_update(id: int, data: ProductPatch):
        old = await run('get', 'products', id)
        payload = data.model_dump(exclude_unset=True)
        if any(v is None for v in payload.values()): raise HTTPException(422, 'Null fields not allowed')
        merged = Product(**({k: old[k] for k in ('name', 'description')} | payload)).model_dump()
        return await save_product(merged, id)

    @app.delete('/products/{id}', status_code=204, dependencies=[Depends(admin)])
    async def product_delete(id: int):
        await run('delete', 'products', id)
        return Response(status_code=204)

    @app.get('/leads', dependencies=[Depends(admin)])
    async def leads(): return await run('list', 'leads')

    @app.post('/leads', status_code=201, dependencies=[Depends(admin)])
    async def lead_create(data: Lead): return await run('lead', data.model_dump())

    @app.post('/leads/import', status_code=201, dependencies=[Depends(admin)])
    async def lead_import(data: list[Lead]):
        if not 1 <= len(data) <= 1000: raise HTTPException(422, 'Import requires 1 to 1000 leads')
        return await run('import_leads', [item.model_dump() for item in data])

    @app.patch('/leads/{id}', dependencies=[Depends(admin)])
    async def lead_update(id: int, data: LeadPatch):
        old = await run('get', 'leads', id)
        payload = data.model_dump(exclude_unset=True)
        if any(v is None for v in payload.values()): raise HTTPException(422, 'Null fields not allowed')
        merged = Lead(**({k: old[k] for k in ('name', 'phone', 'company')} | payload))
        return await run('lead', merged.model_dump(), id)

    @app.delete('/leads/{id}', status_code=204, dependencies=[Depends(admin)])
    async def lead_delete(id: int):
        await run('delete', 'leads', id)
        return Response(status_code=204)

    @app.post('/calls', status_code=201, dependencies=[Depends(admin)])
    async def dial(data: Dial):
        try:
            call = await prepared_call(db, provider, s, data.lead_id, data.product_id,
                                       recorder.prices, isinstance(provider, Providers))
        except LookupError: raise HTTPException(404, 'Not found')
        except ValueError as exc: raise HTTPException(409, str(exc))
        except Exception:
            raise HTTPException(503, 'Speech preparation unavailable; dial not attempted; operator reconciliation required')
        try: lead = await run('get', 'leads', data.lead_id)
        except BaseException:
            await asyncio.shield(asyncio.to_thread(db.abort_undialed, call['id']))
            raise
        try:
            sid = await asyncio.wait_for(provider.dial(call, lead['phone']), timeout=8)
            await run('bind', call['id'], sid)
        except CallCreationRejected as exc:
            await run('finish', call['id'], exc.outcome)
            raise HTTPException(422 if exc.outcome == 'numero_errado' else 503,
                                'Twilio rejected call creation: ' + exc.outcome)
        except Exception:
            # Ambiguous timeout can mean Twilio accepted a call. Keep the DB slot held.
            await run('outcome', call['id'], 'falha_tecnica')
            raise HTTPException(503, 'Dial uncertain; active slot retained until terminal callback or reconciliation')
        return visible(await run('get', 'calls', call['id']))

    @app.get('/calls', dependencies=[Depends(admin)])
    async def calls(): return [visible(row) for row in await run('list', 'calls')]

    @app.get('/calls/{id}', dependencies=[Depends(admin)])
    async def call_detail(id: int): return visible(await run('get', 'calls', id))

    @app.get('/calls/{id}/audio', dependencies=[Depends(admin)])
    async def audio(id: int):
        row = await run('get', 'calls', id)
        if row['recording_status'] != 'available': raise HTTPException(404, 'Audio not available')
        content = await run('audio', id)
        return Response(content=bytes(content), media_type='audio/wav', headers={'Cache-Control': 'no-store'})

    @app.get('/observability/events', dependencies=[Depends(admin)])
    async def observability_events(call_id: Optional[int] = None,
                                   limit: int = Query(default=100, ge=1, le=200)):
        try: return await run('observability_events', call_id, limit)
        except HTTPException: raise
        except Exception: raise HTTPException(503, 'Accounting unavailable')

    @app.get('/observability/summary', dependencies=[Depends(admin)])
    async def observability_summary(call_id: Optional[int] = None):
        try: return await run('observability_summary', call_id)
        except HTTPException: raise
        except Exception: raise HTTPException(503, 'Accounting unavailable')

    @app.post('/calls/{id}/billing/reconcile', dependencies=[Depends(admin)])
    async def reconcile(id: int):
        row = await run('get', 'calls', id)
        sid = row['sid']
        if not sid: return billed_empty(id)
        try:
            with call_scope(id): amount, currency = await provider.billing(sid)
            status = 'pending' if amount is None else 'available'
            error_code = None
        except Exception:
            amount, currency, status, error_code = None, None, 'failed', 'provider_error'
        try: return await run('billing_save', id, sid, status, amount, currency, error_code)
        except Exception: raise HTTPException(503, 'Accounting unavailable')

    async def setup(id, request, action):
        form = await signed(request)
        sid = call_sid(form)
        verdict = form.get('AnsweredBy') if action == 'amd' else None
        if action == 'amd' and verdict not in VERDICTS: raise HTTPException(422, 'Invalid AMD verdict')
        directive, digest = await run('answer_setup', id, sid, action, verdict)
        if action == 'amd': return {'ok': True}
        return Response(twiml(directive,digest,s.public_base,id), media_type='application/xml')

    @app.post('/twilio/voice/{id}')
    async def voice(id: int, request: Request): return await setup(id, request, 'voice')

    @app.post('/twilio/amd/{id}')
    async def amd(id: int, request: Request): return await setup(id, request, 'amd')

    @app.post('/twilio/gate/{id}')
    async def gate(id: int, request: Request): return await setup(id, request, 'gate')

    @app.get('/media/hello/{digest}.wav')
    async def hello(digest: str, request: Request):
        if not re.fullmatch(r'[0-9a-f]{64}', digest) or request.scope.get('query_string'):
            raise HTTPException(404, 'Not found')
        audio = await run('hello_audio', digest)  # Restricted persisted text/format, never a provider call.
        headers = {'Cache-Control':'public, max-age=31536000, immutable', 'ETag':'"'+digest+'"'}
        if request.headers.get('if-none-match') == headers['ETag']: return Response(status_code=304, headers=headers)
        return Response(ulaw_wav(audio), media_type='audio/wav', headers=headers)

    @app.post('/twilio/status/{id}')
    async def status(id: int, request: Request):
        form = await signed(request)
        await identified(id, form)
        state = form.get('CallStatus')
        if state in ('completed', 'failed', 'busy', 'no-answer', 'canceled'):
            code = form.get('ErrorCode', '')
            outcome = ('numero_errado' if code in ('21211', '21214', '21614', '13224')
                       else 'inconclusivo' if state in ('completed', 'busy', 'no-answer', 'canceled')
                       else 'falha_tecnica')
            try: duration = int(form.get('CallDuration', '0'))
            except ValueError: raise HTTPException(422, 'Invalid duration')
            if not 0 <= duration <= 86400: raise HTTPException(422, 'Invalid duration')
            await run('finish', id, outcome, duration, form['CallSid'])
        return {'ok': True}

    @app.post('/twilio/recording/{id}')
    async def recording(id: int, request: Request):
        form = await signed(request)
        row = await identified(id, form)
        if row['recording_status'] == 'available': return {'ok': True}
        if form.get('RecordingStatus') == 'absent':
            await run('absent', id)
            return {'ok': True}
        sid = form.get('RecordingSid', '')
        if not re.fullmatch(r'RE[0-9a-fA-F]{32}', sid): raise HTTPException(422, 'Invalid RecordingSid')
        if form.get('RecordingStatus') != 'completed': raise HTTPException(422, 'Recording not complete')
        try:
            with call_scope(id): content = await provider.download(sid)
        except Exception:
            await run('recording', id, sid, None, True)
            raise HTTPException(503, 'Recording download failed; retry callback safely')
        await run('recording', id, sid, content)
        return {'ok': True}

    # One static warning per minute per app/process, no attacker-controlled labels
    # or per-call state. Rejected handshakes never touch the database.
    last_stream_warning = None

    @app.websocket('/twilio/stream/{id}')
    async def stream(websocket: WebSocket, id: int):
        nonlocal last_stream_warning
        try:
            valid = websocket_signature_valid(s.twilio_token, s.public_base,
                websocket.scope['raw_path'].decode('ascii'), websocket.scope.get('query_string', b''),
                websocket.headers.get('x-twilio-signature', ''))
        except (UnicodeError, ValueError):
            valid = False
        if not valid:
            now = time.monotonic()
            if last_stream_warning is None or now - last_stream_warning >= 60:
                logging.getLogger(__name__).warning('media_stream_signature_rejected')
                last_stream_warning = now
            await websocket.close(code=1008)
            return
        await websocket.accept()
        with call_scope(id): await session(websocket, id, db, provider)

    return app


# Factory keeps offline tests importable without production credentials.
# Uvicorn's documented `app.main:app` resolves this lazy ASGI application.
class Application:
    def __init__(self): self.instance = None
    async def __call__(self, scope, receive, send):
        if self.instance is None: self.instance = create_app()
        await self.instance(scope, receive, send)


app = Application()
