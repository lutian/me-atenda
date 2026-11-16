"""Provider-free media protocol tests through WebSocket and REST interfaces."""
import asyncio
import audioop
import base64
import hashlib
import hmac
import unittest
from unittest.mock import patch
from starlette.websockets import WebSocketDisconnect
import test_api as fixtures


class Media(unittest.TestCase):
    setUp = fixtures.API.setUp
    seed = fixtures.API.seed
    callback = fixtures.API.callback
    post = fixtures.API.post
    def test_stream_human_audio_and_final_turns(self):
        self.seed()
        self.callback('/twilio/voice/1', {'CallSid': 'CA'+'1'*32, 'AnsweredBy': 'human'})
        url = self.settings.public_base + '/twilio/stream/1'
        sig = base64.b64encode(hmac.new(b'test-token', url.encode(), hashlib.sha1).digest()).decode()
        stream = 'MZ'+'5'*32
        start = {'event': 'start', 'streamSid': stream, 'start': {
            'streamSid': stream, 'callSid': 'CA'+'1'*32,
            'mediaFormat': {'encoding': 'audio/x-mulaw', 'sampleRate': 8000, 'channels': 1}}}
        with self.client.websocket_connect('/twilio/stream/1', headers={'X-Twilio-Signature': sig}) as ws:
            ws.send_json(start)
            frame = ws.receive_json()
            self.assertEqual(frame['streamSid'], stream)
            self.assertEqual(len(base64.b64decode(frame['media']['payload'])), 160)
            # Deterministic voiced PCM followed by stop flushes the pending utterance.
            voice = audioop.lin2ulaw(b'\x00\x20' * 160, 2)
            ws.send_json({'event': 'media', 'sequenceNumber': '2', 'streamSid': stream,
                          'media': {'track': 'inbound', 'payload': base64.b64encode(voice).decode()}})
            ws.send_json({'event': 'stop', 'sequenceNumber': '3', 'streamSid': stream,
                          'stop': {'callSid': 'CA'+'1'*32}})
            with self.assertRaises(WebSocketDisconnect) as closed: ws.receive_json()
            self.assertEqual(closed.exception.code, 1000)
        rows = self.client.get('/calls/1', headers=self.headers).json()['transcription_json']
        self.assertEqual([r['role'] for r in rows], ['assistant', 'user'])
        self.assertEqual(rows[1]['content'], 'Tenho interesse')

    def test_stream_requires_human_gate_and_bound_sid(self):
        self.seed()
        url = self.settings.public_base + '/twilio/stream/1'
        sig = base64.b64encode(hmac.new(b'test-token', url.encode(), hashlib.sha1).digest()).decode()
        for sid in ['CA'+'1'*32, 'CA'+'9'*32]:
            with self.client.websocket_connect('/twilio/stream/1', headers={'X-Twilio-Signature': sig}) as ws:
                stream = 'MZ'+'5'*32
                ws.send_json({'event': 'start', 'streamSid': stream, 'start': {
                    'streamSid': stream, 'callSid': sid,
                    'mediaFormat': {'encoding': 'audio/x-mulaw', 'sampleRate': 8000, 'channels': 1}}})
                with self.assertRaises(WebSocketDisconnect): ws.receive_json()
        rows = self.client.get('/calls/1', headers=self.headers).json()['transcription_json']
        self.assertEqual(rows, [])

    def test_oversized_audio_terminates_without_releasing_slot(self):
        self.seed()
        self.callback('/twilio/voice/1', {'CallSid': 'CA'+'1'*32, 'AnsweredBy': 'human'})
        url = self.settings.public_base + '/twilio/stream/1'
        sig = base64.b64encode(hmac.new(b'test-token', url.encode(), hashlib.sha1).digest()).decode()
        stream = 'MZ'+'5'*32
        with self.client.websocket_connect('/twilio/stream/1', headers={'X-Twilio-Signature': sig}) as ws:
            ws.send_json({'event': 'start', 'streamSid': stream, 'start': {
                'streamSid': stream, 'callSid': 'CA'+'1'*32,
                'mediaFormat': {'encoding': 'audio/x-mulaw', 'sampleRate': 8000, 'channels': 1}}})
            ws.receive_json()
            ws.send_json({'event': 'media', 'sequenceNumber': '2', 'streamSid': stream,
                          'media': {'track': 'inbound', 'payload': 'A' * 5000}})
            with self.assertRaises(WebSocketDisconnect): ws.receive_json()
        self.assertEqual(self.post('/calls', {'lead_id': 1, 'product_id': 1}).status_code, 409)
        self.assertEqual(self.client.get('/calls/1', headers=self.headers).json()['status_final'], 'falha_tecnica')


if __name__ == '__main__': unittest.main()
