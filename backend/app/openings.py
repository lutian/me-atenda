"""Pre-dial work owns its session guard even after HTTP cancellation/timeout."""
import asyncio
import hashlib
import json

NOTICE = ('Olá, meu nome é {agent_name}, sou Representante de {product_name}, gostaria de apresentar '
          'para vocês o nosso produto. É com você que posso falar?')
HELLO = 'Alô?'
FORMAT = 'raw-mulaw-8000-mono-v1'
PREP_SECONDS = 20


def identity(text, settings, voice=None):
    return hashlib.sha256(json.dumps([text,settings.tts_model,voice or settings.tts_voice,FORMAT],
                                    ensure_ascii=False).encode()).hexdigest()


async def prepare(db, provider, settings, lead_id, product_id, prices, stop, ledger):
    async def run(name, *args): return await asyncio.to_thread(getattr(db, name), *args)
    guard = await run('preparation_guard')
    call = None
    try:
        product = await run('opening_inputs', lead_id, product_id)
        profile = dict(await run('agent_profile'))
        voice = profile['tts_voice'] or settings.tts_voice
        texts = [HELLO, NOTICE.format(agent_name=profile['agent_name'], product_name=product['name'])]
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
