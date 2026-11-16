"""Answer setup decisions serialize on the existing call row, including SID binding."""
from datetime import datetime, timezone
from .answering import transition
from .openings import HELLO, FORMAT


def clock(db):
    return (db.execute('SELECT clock_timestamp() AS now').fetchone()['now'] if db is not None
            else datetime.now(timezone.utc))


class AnsweringStore:
    def answer_setup(self, id, sid, action, verdict=None):
        with self.connection() as db:
            row = self.locked(db, id)
            eligible = row['active'] and row['status_final'] == 'ligando' and not row['stream_sid']
            if row['sid'] is not None or eligible: self.bind_locked(db, row, sid)
            if not eligible: return 'hangup', None
            db.execute('INSERT INTO call_answering(call_id) VALUES (%s) ON CONFLICT DO NOTHING', (id,))
            state = db.execute('SELECT * FROM call_answering WHERE call_id=%s', (id,)).fetchone()
            before = dict(state)
            directive, outcome, human = transition(row, state, action, verdict, clock(db))
            digest = None
            if directive == 'hello':
                audio = db.execute("SELECT a.digest FROM call_openings p JOIN opening_audio a ON a.digest=p.hello "
                    "WHERE p.call_id=%s AND a.text=%s AND a.format=%s AND a.state='ready'",
                    (id,HELLO,FORMAT)).fetchone()
                if not audio:
                    self.outcome_locked(db,row,'falha_tecnica')
                    return 'hangup', None
                digest = audio['digest']
            if state != before:
                db.execute('UPDATE call_answering SET hello_issued_at=%s,verdict=%s,polls=%s WHERE call_id=%s',
                           (state['hello_issued_at'],state['verdict'],state['polls'],id))
            if outcome: self.outcome_locked(db,row,outcome)
            if human: self.human_locked(db,row)
            return directive, digest

    def hello_audio(self, digest):
        with self.connection() as db:
            row = db.execute("SELECT audio FROM opening_audio WHERE digest=%s AND text=%s "
                             "AND format=%s AND state='ready'", (digest,HELLO,FORMAT)).fetchone()
            if not row: raise LookupError('not found')
            return bytes(row['audio'])
