"""Verify the public Compose configuration without loading personal settings."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class VoiceConfiguration(unittest.TestCase):
    def test_coral_override_changes_only_backend_voice(self):
        docker = shutil.which('docker')
        self.assertIsNotNone(docker, 'Docker CLI is required for this configuration test')
        with tempfile.TemporaryDirectory(dir=ROOT / '.worktrees') as home:
            env = {'PATH': os.defpath, 'HOME': home}
            for key in ('POSTGRES_PASSWORD', 'DATABASE_URL', 'ADMIN_API_KEY',
                        'TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN',
                        'TWILIO_PHONE_NUMBER', 'PUBLIC_BASE_URL', 'OPENAI_API_KEY'):
                env[key] = 'synthetic-placeholder'
            def configuration(extra):
                result = subprocess.run(
                    [docker, 'compose', '--env-file', '/dev/null', *extra,
                     'config', '--format', 'json'], cwd=ROOT, env=env,
                    capture_output=True, timeout=10)
                self.assertTrue(result.returncode == 0, 'Compose configuration failed')
                self.assertTrue(result.stderr == b'', 'Unexpected Compose diagnostic')
                return json.loads(result.stdout)
            baseline = configuration(['-f', 'docker-compose.yml'])
            effective = configuration([])  # Exercise automatic override discovery.
        self.assertEqual(effective['services']['backend']['environment']['OPENAI_TTS_VOICE'], 'coral')
        normalized = copy.deepcopy(effective)
        normalized['services']['backend']['environment']['OPENAI_TTS_VOICE'] = baseline['services']['backend']['environment']['OPENAI_TTS_VOICE']
        self.assertTrue(normalized == baseline, 'Only the selected backend voice may change')


if __name__ == '__main__':
    unittest.main()
