"""Pre-dial work owns its session guard even after HTTP cancellation/timeout."""
import asyncio
import hashlib
import json

NOTICE = ('Olá, meu nome é {agent_name}, sou Representante de {product_name}, gostaria de apresentar '
          'para vocês o nosso produto. É com você que posso falar?')
HELLO = 'Alô?'
GOODBYE = 'Entendi, obrigado pelo seu tempo.'
FORMAT = 'raw-mulaw-8000-mono-v1'
PREP_SECONDS = 20


def identity(text, settings, voice=None):
    return hashlib.sha256(json.dumps([text,settings.tts_model,voice or settings.tts_voice,FORMAT],
                                    ensure_ascii=False).encode()).hexdigest()


def scripted_texts(product, profile, messages):
    """Hello, opening and (when confirmed) pitch/goodbye, in that fixed order."""
    # Reviewed opening when saved; the built-in template keeps calls without one working.
    opening = messages.get('opening') or NOTICE.format(agent_name=profile['agent_name'], product_name=product['name'])
    texts = [HELLO, opening]
    if messages['pitch']: texts += [messages['pitch'], messages['goodbye']]
    return texts


def voice_of(profile, settings): return profile['tts_voice'] or settings.tts_voice


async def synthesize(run, provider, settings, texts, voice, ledger, stop):
    keys = [identity(text, settings, voice) for text in texts]
    for key, text in zip(keys, texts):
        if stop.is_set(): raise RuntimeError('Preparation interrupted')
        audio = await run('opening_begin', key, text, settings.tts_model, voice, FORMAT)
        if audio is None:
            # Marker is durable BEFORE any external synthesis. No implicit retry on failure.
            try:
                audio = await provider.speak(text, voice)
                await run('opening_save', key, audio, ledger)
            except Exception: raise RuntimeError('Speech preparation unavailable') from None
    return keys


async def prepare_messages(db, provider, settings, product_id, opening, pitch, goodbye, ledger):
    """Operator button: persist confirmed texts, then synthesize every prepared message."""
    async def run(name, *args): return await asyncio.to_thread(getattr(db, name), *args)
    product = await run('get', 'products', product_id)
    guard = await run('preparation_guard')
    try:
        profile = dict(await run('agent_profile'))
        messages = await run('save_product_messages', product_id, opening, pitch, goodbye, profile['agent_name'])
        texts = scripted_texts(product, profile, messages)
        await synthesize(run, provider, settings, texts, voice_of(profile, settings), ledger, asyncio.Event())
    finally: await asyncio.to_thread(guard.close)


async def messages_status(db, settings, product_id):
    async def run(name, *args): return await asyncio.to_thread(getattr(db, name), *args)
    messages = dict(await run('product_messages', product_id))
    ready = False
    profile = dict(await run('agent_profile'))
    # The opening names the agent: a renamed agent needs a reviewed opening again.
    if messages['opening'] and messages['pitch'] and messages['agent_name'] == profile['agent_name']:
        product = await run('get', 'products', product_id)
        voice = voice_of(profile, settings)
        keys = [identity(text, settings, voice) for text in scripted_texts(product, profile, messages)]
        ready = await run('openings_ready', keys)
    return {'opening': messages['opening'], 'pitch': messages['pitch'], 'goodbye': messages['goodbye'],
            'ready': ready}


async def prepare(db, provider, settings, lead_id, product_id, prices, stop, ledger):
    async def run(name, *args): return await asyncio.to_thread(getattr(db, name), *args)
    guard = await run('preparation_guard')
    call = None
    try:
        product = await run('opening_inputs', lead_id, product_id)
        profile = dict(await run('agent_profile'))
        messages = await run('product_messages', product_id)
        texts = scripted_texts(product, profile, messages)
        keys = await synthesize(run, provider, settings, texts, voice_of(profile, settings), ledger, stop)
        if stop.is_set(): raise RuntimeError('Preparation interrupted')
        call = await run('reserve', lead_id, product_id, (product['name'], *keys), profile)
        try:
            await run('account_call', call['id'], prices)
            if stop.is_set(): raise RuntimeError('Preparation interrupted')
        except BaseException:
            await run('abort_undialed', call['id'])
            raise
        return call
    finally:
        # Owner is shielded: never close while a thread/provider can still pay or reserve.
        await asyncio.to_thread(guard.close)


async def prepared_call(db, provider, settings, lead_id, product_id, prices, ledger):
    stop = asyncio.Event()
    job = asyncio.create_task(prepare(db, provider, settings, lead_id, product_id, prices, stop, ledger))
    def observed(task):
        if not task.cancelled(): task.exception()
    job.add_done_callback(observed)
    async def abandon():
        stop.set()
        async def cleanup():
            try: call = await job
            except BaseException: return
            await asyncio.to_thread(db.abort_undialed, call['id'])
        cleanup_job = asyncio.create_task(cleanup())
        cleanup_job.add_done_callback(observed)
    try:
        ready, _ = await asyncio.wait([job], timeout=PREP_SECONDS)
        if not ready:
            await abandon()
            raise RuntimeError('Preparation timed out; operator reconciliation required')
        return job.result()
    except asyncio.CancelledError:
        await abandon()
        # Do not cancel the owner or its blocking database/provider work.
        raise
