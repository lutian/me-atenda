"""Environment configuration; never log credentials."""
from dataclasses import dataclass
import os
from urllib.parse import urlsplit


@dataclass
class Settings:
    database_url: str = ''
    admin_key: str = ''
    public_base: str = ''
    account_sid: str = ''
    twilio_token: str = ''
    twilio_phone: str = ''
    openai_key: str = ''
    chat_model: str = 'gpt-4o-mini'
    embedding_model: str = 'text-embedding-3-small'
    tts_model: str = 'tts-1'
    tts_voice: str = 'alloy'
    whisper_model: str = 'base'
    whisper_language: str = 'pt'
    whisper_device: str = 'cpu'
    whisper_compute_type: str = 'int8'
    observability_pricing_json: str = ''

    @classmethod
    def environment(cls):
        names = {'observability_pricing_json': 'OBSERVABILITY_PRICING_JSON',
                 'database_url': 'DATABASE_URL', 'admin_key': 'ADMIN_API_KEY',
                 'public_base': 'PUBLIC_BASE_URL', 'account_sid': 'TWILIO_ACCOUNT_SID',
                 'twilio_token': 'TWILIO_AUTH_TOKEN', 'twilio_phone': 'TWILIO_PHONE_NUMBER',
                 'openai_key': 'OPENAI_API_KEY', 'chat_model': 'OPENAI_CHAT_MODEL',
                 'embedding_model': 'OPENAI_EMBEDDING_MODEL', 'tts_model': 'OPENAI_TTS_MODEL',
                 'tts_voice': 'OPENAI_TTS_VOICE', 'whisper_model': 'WHISPER_MODEL',
                 'whisper_language': 'WHISPER_LANGUAGE', 'whisper_device': 'WHISPER_DEVICE',
                 'whisper_compute_type': 'WHISPER_COMPUTE_TYPE'}
        value = cls(**{key: os.environ[name] for key, name in names.items() if name in os.environ})
        from .observability import pricing
        pricing(value.observability_pricing_json)
        value.public_base = value.public_base.rstrip('/')
        parsed = urlsplit(value.public_base)
        if (parsed.scheme != 'https' or not parsed.netloc or parsed.query or parsed.fragment
                or parsed.username or parsed.password):
            raise ValueError('PUBLIC_BASE_URL must be an HTTPS origin with optional path prefix')
        if not value.admin_key or not value.twilio_token:
            raise ValueError('ADMIN_API_KEY and TWILIO_AUTH_TOKEN are required')
        return value
