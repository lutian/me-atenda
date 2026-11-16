"""Exercise real speech imports in a fresh process; adapter mocks cannot hide them."""
import os
import subprocess
import sys
import textwrap
import unittest


class SpeechRuntime(unittest.TestCase):
    def test_real_speech_dependencies_import_without_network_or_model_download(self):
        script = textwrap.dedent("""
            import socket
            def offline(*args, **kwargs):
                raise RuntimeError('speech import unexpectedly attempted network access')
            socket.socket.connect = offline
            socket.socket.connect_ex = offline
            import requests
            import ctranslate2
            from faster_whisper import WhisperModel
            assert callable(WhisperModel)
            print('speech-import-ok')
        """)
        result = subprocess.run(
            [sys.executable, '-I', '-B', '-c', script],
            env={'PATH': os.environ.get('PATH', ''), 'HF_HUB_OFFLINE': '1',
                 'HF_HUB_DISABLE_TELEMETRY': '1', 'TRANSFORMERS_OFFLINE': '1'},
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'speech-import-ok\n')
        self.assertEqual(result.stderr, '')
