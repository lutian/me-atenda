"""Bounded utterance streaming: 20s speech, 600ms silence, 5min session."""
import asyncio
import audioop
import base64
import binascii
import json
import logging
import re
import unicodedata
from starlette.websockets import WebSocketDisconnect

from .openings import NOTICE  # Preserve the existing public export.
MAX_MESSAGE = 8192
MAX_ULAW = 3200  # 400ms per frame, usual Twilio frame is 160 bytes.
MAX_PCM = 16000 * 2 * 20


class Utterance:
    def __init__(self):
        self.buffer = bytearray()
        self.silent = 0
        self.rate_state = None

    def feed(self, payload):
        if not isinstance(payload, str) or len(payload) > 4400: raise ValueError('oversized frame')
        try: raw = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc: raise ValueError('invalid audio') from exc
        if not raw or len(raw) > MAX_ULAW: raise ValueError('invalid frame size')
        pcm = audioop.ulaw2lin(raw, 2)
        voiced = audioop.rms(pcm, 2) > 350
        pcm16, self.rate_state = audioop.ratecv(pcm, 2, 1, 8000, 16000, self.rate_state)
        if voiced or self.buffer:
            if len(self.buffer) + len(pcm16) > MAX_PCM:
                result = bytes(self.buffer)
                self.buffer = bytearray(pcm16)
                self.silent = 0 if voiced else len(raw)
                return result
            self.buffer.extend(pcm16)
            self.silent = 0 if voiced else self.silent + len(raw)
            if self.silent >= 4800:
                result = bytes(self.buffer)
                self.buffer.clear(); self.silent = 0
                return result
        return None

    def flush(self):
        result = bytes(self.buffer)
        self.buffer.clear()
        return result


def enqueue(queue, pcm):
    """While the worker is busy, merge pending speech instead of failing the call.

    A lead repeating "Olá?" during a slow reply must not hang up the call; the merged
    utterance keeps arrival order and the newest MAX_PCM bytes. Synchronous: no await
    between taking and re-inserting, so the worker cannot interleave.
    """
    try:
        queue.put_nowait(pcm)
        return
    except asyncio.QueueFull: pass
    pending = []
    while not queue.empty():
        pending.append(queue.get_nowait())
        queue.task_done()
    queue.put_nowait(b''.join(pending + [pcm])[-MAX_PCM:])


# Greetings a lead repeats while waiting in silence ("Alô? Alô?"). Answering them snowballs.
FILLERS = {'alo', 'oi', 'ola', 'hello', 'hum', 'hm', 'humm', 'ah', 'eh', 'ei'}


def is_filler(text):
    """True when an utterance carries no content beyond greeting/hesitation words."""
    plain = unicodedata.normalize('NFKD', text or '').encode('ascii', 'ignore').decode().lower()
    words = re.findall(r'[a-z]+', plain)
    return bool(words) and len(words) <= 6 and all(word in FILLERS for word in words)


def failure_reason(exc):
    # Own protocol/timeout messages are static; provider errors may carry URLs or data.
    if type(exc) in (ValueError, TimeoutError) and str(exc): return f'{type(exc).__name__}: {str(exc)[:80]}'
    return type(exc).__name__


async def session(ws, id, store, providers):
    """Stop accepting before draining; carrier completion never stops the STT consumer."""
    bound = None
    stream = None
    queue = asyncio.Queue(maxsize=2)
    utterance = Utterance()
    done = asyncio.Event()
    disconnected = False
    draining = False
    failure = None

    async def db(method, *args): return await asyncio.to_thread(getattr(store, method), *args)

    async def can_speak():
        if disconnected or draining or done.is_set(): return False
        return (await db('get', 'calls', id))['active']

    async def output(text, audio=None):
        if not await can_speak(): return
        await db('append', id, 'assistant', text)
        if audio is None: audio = await providers.speak(text, bound.get('tts_voice'))
        if not await can_speak(): return
        for offset in range(0, len(audio), 160):
            if disconnected or draining or done.is_set(): return
            await ws.send_json({'event': 'media', 'streamSid': stream,
                                'media': {'payload': base64.b64encode(audio[offset:offset+160]).decode()}})
            await asyncio.sleep(0.02)

    def padded(audio):
        # Complete the last cached frame with mu-law silence; live replies are unchanged.
        return audio + b'\xff' * (-len(audio) % 160)

    async def conversation():
        nonlocal draining
        text, audio = await db('opening_intro', id)
        await output(text, padded(audio))
        prepared = await db('opening_scripts', id)
        pitched = False
        while True:
            pcm = await queue.get()
            try:
                text = await providers.transcribe(pcm)
                if not text: continue
                await db('append', id, 'user', text)
                # Completion disables selling, not transcription of accepted audio.
                if not await can_speak(): continue
                # Kept in the transcript for audit; never answered (see FILLERS).
                if is_filler(text): continue
                embedding = (await providers.embed([text]))[0]
                context = await db('retrieve', bound['product_id'], embedding)
                current = await db('get', 'calls', id)
                if not current['active'] or not await can_speak(): continue
                offered = [k for k in ('pitch', 'goodbye') if prepared[k] and not (k == 'pitch' and pitched)]
                history = [turn for turn in current['transcription_json']
                           if not (turn['role'] == 'user' and is_filler(turn['content']))]
                result = await providers.answer(history, context,
                                                bound.get('agent_name') or 'Bia', offered)
                if not await can_speak(): continue
                script = result.get('script')
                if script in offered:
                    # Prepared audio: no live synthesis latency.
                    pitched = pitched or script == 'pitch'
                    await output(prepared[script][0], padded(prepared[script][1]))
                else: await output(result['text'])
                if result['outcome']:
                    await db('outcome', id, result['outcome'])
                    draining = True
                    done.set()
                    await providers.hangup(bound['sid'])
            finally:
                queue.task_done()

    async def drain():
        nonlocal draining
        draining = True
        async def finish_accepted():
            final = utterance.flush()
            if final: await queue.put(final)
            # No sentinel: a full queue cannot strand a producer after completion.
            await queue.join()
        await asyncio.wait_for(finish_accepted(), timeout=45)

    worker = None
    receive = None
    ended = None
    try:
        # A stream cannot keep the pre-start phase alive with endless connected messages.
        start_deadline = asyncio.get_running_loop().time() + 10
        while bound is None:
            remaining = start_deadline - asyncio.get_running_loop().time()
            if remaining <= 0: raise TimeoutError('start timeout')
            raw = await asyncio.wait_for(ws.receive_text(), timeout=remaining)
            if len(raw) > MAX_MESSAGE: raise ValueError('oversized message')
            data = json.loads(raw)
            if data.get('event') == 'connected': continue
            if data.get('event') != 'start': raise ValueError('start required')
            start = data['start']; stream = data.get('streamSid')
            if (not isinstance(stream, str) or not re.fullmatch(r'MZ[0-9a-fA-F]{32}', stream)
                    or start.get('streamSid') != stream
                    or start.get('mediaFormat') != {'encoding': 'audio/x-mulaw', 'sampleRate': 8000, 'channels': 1}):
                raise ValueError('invalid stream start')
            sid = start.get('callSid', '')
            if not re.fullmatch(r'CA[0-9a-fA-F]{32}', sid): raise ValueError('invalid call SID')
            bound = await db('claim_stream', id, sid, stream)
        worker = asyncio.create_task(conversation())
        ended = asyncio.create_task(done.wait())
        last_sequence = 0
        # Carrier TimeLimit is authoritative; this stream safeguard must not
        # silently cap a larger configured snapshot at the old five minutes.
        deadline = asyncio.get_running_loop().time() + (bound.get('max_duration_seconds') or 300)
        while not done.is_set():
            if asyncio.get_running_loop().time() >= deadline: raise TimeoutError('session timeout')
            receive = asyncio.create_task(ws.receive_text())
            remaining = max(0.01, deadline - asyncio.get_running_loop().time())
            ready, _ = await asyncio.wait([receive, worker, ended], timeout=min(30, remaining),
                                           return_when=asyncio.FIRST_COMPLETED)
            if worker in ready: worker.result()
            if ended in ready: break
            if receive not in ready: raise TimeoutError('idle or session timeout')
            raw = receive.result()
            if len(raw) > MAX_MESSAGE: raise ValueError('oversized message')
            data = json.loads(raw)
            if data.get('streamSid') != stream: raise ValueError('stream SID mismatch')
            sequence = int(data.get('sequenceNumber', 0))
            if sequence <= last_sequence: raise ValueError('nonmonotonic sequence')
            last_sequence = sequence
            event = data.get('event')
            if event == 'stop':
                if data.get('stop', {}).get('callSid') != bound['sid']: raise ValueError('stop SID mismatch')
                break
            if event == 'media':
                if data.get('media', {}).get('track') != 'inbound': raise ValueError('unexpected media track')
                pcm = utterance.feed(data['media']['payload'])
                if pcm: enqueue(queue, pcm)
            elif event not in ('mark',): raise ValueError('unexpected event')
        await drain()
    except WebSocketDisconnect:
        disconnected = True
        if bound and worker and not worker.done():
            try: await drain()
            except Exception as exc: failure = exc
    except (ValueError, KeyError, TypeError, TimeoutError, asyncio.TimeoutError) as exc:
        failure = exc
    except Exception as exc:
        failure = exc
    finally:
        done.set()
        # Receiver and wakeup tasks must not survive stop/disconnect/cancellation.
        for task in (receive, ended):
            if task and not task.done(): task.cancel()
        await asyncio.gather(*(task for task in (receive, ended) if task), return_exceptions=True)
        if worker:
            if not worker.done(): worker.cancel()
            results = await asyncio.gather(worker, return_exceptions=True)
            if results and isinstance(results[0], Exception): failure = results[0]
        if bound:
            # Never release a slot here; another call can already own it after a callback.
            if failure:
                logging.getLogger(__name__).warning('media_stream_failed %s', failure_reason(failure))
                await db('outcome', id, 'falha_tecnica')
            try: await providers.hangup(bound['sid'])
            except Exception: pass
        try: await ws.close(code=1011 if failure else 1000)
        except (RuntimeError, WebSocketDisconnect): pass
