"""No live providers: exercise network boundaries with httpx MockTransport."""
import asyncio
import audioop
import base64
import threading
import unittest
import httpx
from app.config import Settings
from app.providers import Providers
from app.voice import Utterance, MAX_PCM
from app.security import signature_valid, public_url


class Adapters(unittest.IsolatedAsyncioTestCase):
    def provider(self, handler):
        p = Providers(Settings(account_sid='AC'+'2'*32, twilio_token='test-token',
                               public_base='https://voice.example/prefix', twilio_phone='+15551234567'))
        p.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler),
            auth=(p.s.account_sid, p.s.twilio_token), follow_redirects=False)
        return p

    async def test_recording_download_is_authenticated_and_redirects_rejected(self):
        seen = []
        def transport(request):
            seen.append(request)
            self.assertEqual(request.url.host, 'api.twilio.com')
            self.assertEqual(request.url.path, '/2010-04-01/Accounts/AC'+'2'*32+'/Recordings/RE'+'4'*32+'.wav')
            expected = base64.b64encode(('AC'+'2'*32+':test-token').encode()).decode()
            self.assertEqual(request.headers['authorization'], 'Basic ' + expected)
            return httpx.Response(302, headers={'Location': 'http://169.254.169.254/'})
        p = self.provider(transport)
        with self.assertRaises(httpx.HTTPStatusError): await p.download('RE'+'4'*32)
        self.assertEqual(len(seen), 1)
        with self.assertRaises(ValueError): await p.download('http://localhost/')
        self.assertEqual(len(seen), 1)

    async def test_outbound_amd_and_recording_contract(self):
        from urllib.parse import parse_qs
        def transport(request):
            form = parse_qs(request.content.decode())
            self.assertEqual(form['MachineDetection'], ['DetectMessageEnd'])
            self.assertEqual(form['AsyncAmd'], ['false'])
            self.assertEqual(form['Record'], ['true'])
            self.assertEqual(form['Url'], ['https://voice.example/prefix/twilio/voice/7'])
            self.assertEqual(form['RecordingStatusCallback'], ['https://voice.example/prefix/twilio/recording/7'])
            return httpx.Response(201, json={'sid': 'CA'+'1'*32})
        p = self.provider(transport)
        self.assertEqual(await p.dial({'id': 7}, '+15557654321'), 'CA'+'1'*32)

    async def test_local_inference_does_not_run_on_event_loop(self):
        p = Providers(Settings())
        loop_thread = threading.get_ident()
        entered = threading.Event()
        release = threading.Event()
        def transcribe(pcm):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            entered.set()
            if not release.wait(2): raise AssertionError('event loop did not advance')
            return 'fala local'
        p._transcribe = transcribe
        task = asyncio.create_task(p.transcribe(b'\0\0'*160))
        try:
            for _ in range(100):
                if entered.is_set(): break
                await asyncio.sleep(.005)
            self.assertTrue(entered.is_set())
        finally: release.set()
        self.assertEqual(await task, 'fala local')


class AudioAndSignatures(unittest.TestCase):
    def test_silence_boundary_and_memory_limit(self):
        utterance = Utterance()
        voiced = base64.b64encode(audioop.lin2ulaw(b'\x00\x20'*160, 2)).decode()
        silent = base64.b64encode(b'\xff'*160).decode()
        self.assertIsNone(utterance.feed(voiced))
        for _ in range(29): self.assertIsNone(utterance.feed(silent))
        self.assertTrue(utterance.feed(silent))
        self.assertEqual(len(utterance.buffer), 0)
        boundary_seen = False
        for _ in range(1100):
            boundary_seen = bool(utterance.feed(voiced)) or boundary_seen
            self.assertLessEqual(len(utterance.buffer), MAX_PCM)
        self.assertTrue(boundary_seen)
        with self.assertRaises(ValueError): utterance.feed('A'*5000)
        with self.assertRaises(ValueError): utterance.feed('???')

    def test_standard_hmac_vector_and_exact_query(self):
        # RFC 2202 test case 2, independently specified SHA1 digest.
        expected = base64.b64encode(bytes.fromhex('effcdf6ae5eb2fa2d27416d5f184df9c259a7c79')).decode()
        self.assertTrue(signature_valid('Jefe', 'what do ya want for nothing?', {}, expected))
        import hashlib
        import hmac
        form = {'CallSid': 'CA1234567890ABCDE', 'Digits': '1234'}
        url = 'https://mycompany.com/myapp.php?foo=1&bar=2'
        material = url + 'CallSidCA1234567890ABCDEDigits1234'
        sig = base64.b64encode(hmac.new(b'12345', material.encode(), hashlib.sha1).digest()).decode()
        self.assertTrue(signature_valid('12345', url, form, sig))
        self.assertFalse(signature_valid('12345', url.replace('foo=1&bar=2', 'bar=2&foo=1'), form, sig))
        self.assertEqual(public_url('https://host/prefix', '/twilio/status/1', b'x=1&y=%2F'),
                         'https://host/prefix/twilio/status/1?x=1&y=%2F')


if __name__ == '__main__': unittest.main()
