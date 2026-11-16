"""At-most-once hello issuance, not playback acknowledgement; no inference here."""
import struct
from xml.sax.saxutils import escape

VERDICTS = {'human','fax','unknown','machine_start','machine_end_beep',
            'machine_end_silence','machine_end_other'}


def transition(row, state, action, verdict, now):
    """Pure decision; caller owns call lock, binding and persistence."""
    if action == 'amd' and state['verdict'] is not None: return 'noop', None, False
    issued = state['hello_issued_at']
    if issued is not None and ((now-issued).total_seconds() >= 35 or state['polls'] >= 35):
        return 'hangup', 'inconclusivo', False
    if action == 'amd':
        state['verdict'] = verdict
        outcome = ('caixa_postal' if verdict.startswith('machine') else
                   'numero_errado' if verdict == 'fax' else 'inconclusivo')
        return 'noop', None if verdict == 'human' else outcome, verdict == 'human'
    if action == 'voice':
        if issued is None:
            state['hello_issued_at'] = now
            return 'hello', None, False
        return 'redirect', None, False
    if issued is None: return 'hangup', None, False
    if row['human']: return 'connect', None, False
    state['polls'] += 1
    return ('hangup','inconclusivo',False) if state['polls'] >= 35 else ('wait',None,False)


def twiml(action, digest, base, id):
    redirect = '<Redirect method="POST">'+escape(base+f'/twilio/gate/{id}')+'</Redirect>'
    if action == 'hello': body = '<Play>'+escape(base+f'/media/hello/{digest}.wav')+'</Play>'+redirect
    elif action == 'redirect': body = redirect
    elif action == 'wait': body = '<Pause length="1"/>'+redirect
    elif action == 'connect':
        url = base.replace('https://','wss://',1)+f'/twilio/stream/{id}'
        body = '<Connect><Stream url="'+escape(url, {'"':'&quot;'})+'"/></Connect>'
    else: body = '<Hangup/>'
    return '<Response>'+body+'</Response>'


def ulaw_wav(audio):
    """Wrap existing mono 8kHz mu-law bytes; no synthesis or lossy conversion."""
    fmt = struct.pack('<HHIIHHH', 7,1,8000,8000,1,8,0)
    body = (b'WAVEfmt '+struct.pack('<I',len(fmt))+fmt+
            b'fact'+struct.pack('<II',4,len(audio))+
            b'data'+struct.pack('<I',len(audio))+audio+b'\0'*(len(audio)%2))
    return b'RIFF'+struct.pack('<I',len(body))+body
