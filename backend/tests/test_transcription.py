"""Speech recognition: OpenAI transcription first, local Whisper as fallback; cost per audio minute."""
import asyncio
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from app.config import Settings
from app.observability import Recorder
from app.providers import Providers
from test_observability import Ledger

PCM = b'\x00\x01' * 32000  # 2 seconds of 16 kHz mono 16-bit audio.


class Transcription(unittest.IsolatedAsyncioTestCase):
    def provider(self, create, prices=None):
        s = Settings(observability_pricing_json=json.dumps(prices) if prices else '')
        p = Providers(s)
        p._openai = NS(audio=NS(transcriptions=NS(create=create)))
        self.local = []
        def local(pcm):
            self.local.append(len(pcm))
            return 'texto local'
        p._transcribe = local
        self.ledger = Ledger()
        p.observer = Recorder(self.ledger, s)
        return p

    async def test_openai_transcribes_with_the_selected_model(self):
        create = AsyncMock(return_value=NS(text='  Sim, pode falar.  '))
        p = self.provider(create)
        self.assertEqual(await p.transcribe(PCM), 'Sim, pode falar.')
        kwargs = create.await_args.kwargs
        self.assertEqual((kwargs['model'], kwargs['language']), ('whisper-1', 'pt'))
        name, wav, mime = kwargs['file']
        self.assertEqual((name, mime), ('speech.wav', 'audio/wav'))
        self.assertTrue(wav.startswith(b'RIFF') and b'WAVE' in wav[:12])
        self.assertEqual(int.from_bytes(wav[24:28], 'little'), 16000)  # Sample rate.
        self.assertLessEqual(kwargs['timeout'], 15)
        self.assertEqual(self.local, [])
        p.s.transcribe_model = 'gpt-4o-mini-transcribe'
        await p.transcribe(PCM)
        self.assertEqual(create.await_args.kwargs['model'], 'gpt-4o-mini-transcribe')

    async def test_local_whisper_is_the_fallback_when_openai_fails(self):
        p = self.provider(AsyncMock(side_effect=RuntimeError('openai offline')))
        with self.assertLogs('app.providers', 'WARNING') as logs:
            self.assertEqual(await p.transcribe(PCM), 'texto local')
        self.assertEqual(self.local, [len(PCM)])
        self.assertIn('transcription_fallback_local RuntimeError', logs.output[0])
        self.assertNotIn('offline', logs.output[0])
        self.assertEqual([(e['provider'], e['status']) for e in self.ledger.rows], [('openai', 'error'), ('local', 'success')])

    async def test_cancellation_does_not_fall_back(self):
        p = self.provider(AsyncMock(side_effect=asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError): await p.transcribe(PCM)
        self.assertEqual(self.local, [])

    async def test_openai_transcription_is_priced_per_audio_minute(self):
        prices = {'version': 't', 'currency': 'USD', 'models': {'whisper-1': {'per_minute': '0.006'}}}
        p = self.provider(AsyncMock(return_value=NS(text='Oi')), prices)
        await p.transcribe(PCM)
        event = self.ledger.rows[0]
        self.assertEqual((event['provider'], event['model'], event['audio_seconds']), ('openai', 'whisper-1', 2.0))
        self.assertEqual((event['estimated_cost_usd'], event['cost_status']), ('0.0002', 'estimated'))


if __name__ == '__main__': unittest.main()
