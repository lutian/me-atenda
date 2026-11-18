"""All shared-state transitions live here, each in one PostgreSQL transaction."""
import json
import math
import os
from pathlib import Path
from .accounting_store import AccountingStore
from .opening_store import OpeningStore
from .answering_store import AnsweringStore


class Store(AnsweringStore, OpeningStore, AccountingStore):
    def __init__(self, url):
        self.url = url.replace('postgresql+psycopg://', 'postgresql://', 1)

    def connection(self):
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(self.url, row_factory=dict_row, connect_timeout=5)

    def health(self):
        with self.connection() as db:
            db.execute('SELECT id FROM calls LIMIT 1')
        return True

    def migrate(self):
        migrations = Path(__file__).parent.parent / 'migrations'
        with self.connection() as db:
            db.execute('SELECT pg_advisory_xact_lock(743281)')
            for path in sorted(migrations.glob('*.sql')):
                db.execute(path.read_text())

    @staticmethod
    def table(table):
        if table not in ('products', 'leads', 'calls'): raise ValueError('invalid table')
        return table

    def list(self, table):
        with self.connection() as db:
            # Exclude large BYTEA from history, but expose it only through audio endpoint.
            fields = '*' if table != 'calls' else self.call_fields()
            return db.execute(f'SELECT {fields} FROM {self.table(table)} ORDER BY id').fetchall()

    @staticmethod
    def call_fields():
        return ('id,lead_id,product_id,sid,stream_sid,active,status_final,duration_seconds,'
                'transcription_json,recording_status,recording_sid,(audio IS NOT NULL) AS has_audio')

    def get(self, table, id):
        with self.connection() as db:
            fields = '*' if table != 'calls' else self.call_fields()
            row = db.execute(f'SELECT {fields} FROM {self.table(table)} WHERE id=%s', (id,)).fetchone()
            if not row: raise LookupError('not found')
            return row

    def audio(self, id):
        with self.connection() as db:
            row = db.execute('SELECT audio FROM calls WHERE id=%s', (id,)).fetchone()
            if not row: raise LookupError('not found')
            return row['audio']

    @staticmethod
    def vector(values):
        if len(values) != 1536 or not all(math.isfinite(float(v)) for v in values):
            raise ValueError('embedding must have 1536 finite dimensions')
        return '[' + ','.join(str(float(v)) for v in values) + ']'

    def product(self, data, chunks, id=None):
        with self.connection() as db:
            if id is None:
                row = db.execute('INSERT INTO products(name,description) VALUES (%s,%s) RETURNING *',
                                 (data['name'], data['description'])).fetchone()
            else:
                row = db.execute('UPDATE products SET name=%s,description=%s WHERE id=%s RETURNING *',
                                 (data['name'], data['description'], id)).fetchone()
                if not row: raise LookupError('not found')
                db.execute('DELETE FROM product_chunks WHERE product_id=%s', (id,))
            for text, vector in chunks:
                db.execute('INSERT INTO product_chunks(product_id,content,embedding) VALUES (%s,%s,%s::vector)',
                           (row['id'], text, self.vector(vector)))
            return row

    @staticmethod
    def insert_lead(db, data):
        return db.execute('INSERT INTO leads(name,phone,company) VALUES (%s,%s,%s) RETURNING *',
                          (data['name'], data['phone'], data['company'])).fetchone()

    def lead(self, data, id=None):
        with self.connection() as db:
            if id is None: return self.insert_lead(db, data)
            row = db.execute('UPDATE leads SET name=%s,phone=%s,company=%s WHERE id=%s RETURNING *',
                             (data['name'], data['phone'], data['company'], id)).fetchone()
            if not row: raise LookupError('not found')
            return row

    def import_leads(self, rows):
        with self.connection() as db:
            return [self.insert_lead(db, data) for data in rows]

    def delete(self, table, id):
        from psycopg.errors import ForeignKeyViolation
        try:
            with self.connection() as db:
                if not db.execute(f'DELETE FROM {self.table(table)} WHERE id=%s RETURNING id', (id,)).fetchone():
                    raise LookupError('not found')
        except ForeignKeyViolation as exc: raise ValueError('record has call history') from exc

    def call_policy(self):
        with self.connection() as db:
            row = db.execute('SELECT max_duration_seconds FROM call_policy WHERE id=TRUE').fetchone()
            if not row: raise RuntimeError('Call policy missing')
            return row

    def save_call_policy(self, seconds):
        if type(seconds) is not int or not 1 <= seconds <= 14400:
            raise ValueError('Invalid maximum call duration')
        with self.connection() as db:
            row = db.execute('UPDATE call_policy SET max_duration_seconds=%s WHERE id=TRUE '
                             'RETURNING max_duration_seconds', (seconds,)).fetchone()
            if not row: raise RuntimeError('Call policy missing')
            return row

    def agent_profile(self):
        with self.connection() as db:
            row = db.execute('SELECT agent_name,tts_voice FROM call_policy WHERE id=TRUE').fetchone()
            if not row: raise RuntimeError('Call policy missing')
            return row

    def save_agent_profile(self, name, voice):
        if not isinstance(name, str) or not 1 <= len(name) <= 40: raise ValueError('Invalid agent name')
        with self.connection() as db:
            row = db.execute('UPDATE call_policy SET agent_name=%s,tts_voice=%s WHERE id=TRUE '
                             'RETURNING agent_name,tts_voice', (name, voice)).fetchone()
            if not row: raise RuntimeError('Call policy missing')
            return row

    def reserve(self, lead_id, product_id, opening=None, profile=None):
        from psycopg.errors import UniqueViolation, ForeignKeyViolation
        try:
            with self.connection() as db:
                if opening:
                    product = db.execute('SELECT name FROM products WHERE id=%s FOR UPDATE', (product_id,)).fetchone()
                    lead = db.execute('SELECT id FROM leads WHERE id=%s FOR UPDATE', (lead_id,)).fetchone()
                    if not product or not lead: raise LookupError('not found')
                    if product['name'] != opening[0]: raise ValueError('Spoken product changed; dial not attempted')
                policy = db.execute('SELECT max_duration_seconds,agent_name,tts_voice FROM call_policy '
                                    'WHERE id=TRUE FOR SHARE').fetchone()
                if not policy: raise RuntimeError('Call policy missing')
                # Prepared speech used this identity; a concurrent change must not mismatch it.
                if profile is not None and profile != {k: policy[k] for k in ('agent_name', 'tts_voice')}:
                    raise ValueError('Agent profile changed; dial not attempted')
                row = db.execute('INSERT INTO calls(lead_id,product_id,max_duration_seconds,agent_name,tts_voice) '
                                 'VALUES (%s,%s,%s,%s,%s) RETURNING *',
                                 (lead_id, product_id, policy['max_duration_seconds'],
                                  policy['agent_name'], policy['tts_voice'])).fetchone()
                db.execute("UPDATE leads SET status='ligando' WHERE id=%s", (lead_id,))
                if opening:
                    db.execute('INSERT INTO call_openings(call_id,hello,intro) VALUES (%s,%s,%s)',
                               (row['id'], opening[1], opening[2]))
                return row
        except UniqueViolation as exc: raise ValueError('another call is active') from exc
        except ForeignKeyViolation as exc: raise LookupError('lead or product not found') from exc

    @staticmethod
    def locked(db, id):
        row = db.execute('SELECT * FROM calls WHERE id=%s FOR UPDATE', (id,)).fetchone()
        if not row: raise LookupError('not found')
        return row

    @staticmethod
    def bind_locked(db, row, sid):
        if row['sid'] is not None and row['sid'] != sid: raise ValueError('call SID mismatch')
        if row['sid'] is None:
            db.execute('UPDATE calls SET sid=%s WHERE id=%s', (sid, row['id']))
            row['sid'] = sid
        return row

    def identify(self, id, sid):
        with self.connection() as db:
            return self.bind_locked(db, self.locked(db, id), sid)

    def bind(self, id, sid):
        return self.identify(id, sid)

    @staticmethod
    def outcome_locked(db, row, status):
        if row['active'] and row['status_final'] == 'ligando':
            db.execute('UPDATE calls SET status_final=%s WHERE id=%s', (status, row['id']))
            db.execute('UPDATE leads SET status=%s WHERE id=%s', (status, row['lead_id']))

    def outcome(self, id, status):
        with self.connection() as db:
            self.outcome_locked(db, self.locked(db, id), status)

    def finish(self, id, status, duration=0, terminal_sid=None):
        with self.connection() as db:
            row = self.locked(db, id)
            # Only authenticated carrier terminal callbacks supply terminal_sid.
            # Recheck SID and stream/human state under the same lock as release.
            if terminal_sid is not None:
                row = self.bind_locked(db, row, terminal_sid)
                if row.get('human') and row['stream_sid'] is None:
                    self.media_failure_locked(db, row)
                    if row['active'] and row['status_final'] == 'ligando': status = 'falha_tecnica'
            if row['active']:
                if row['status_final'] != 'ligando': status = row['status_final']
                row = db.execute('UPDATE calls SET active=FALSE,status_final=%s,duration_seconds=%s '
                                 'WHERE id=%s RETURNING *', (status, duration, id)).fetchone()
                db.execute('UPDATE leads SET status=%s WHERE id=%s', (status, row['lead_id']))
            elif duration > row['duration_seconds']:
                # Carrier completion supplies duration after an AMD/conversation outcome.
                db.execute('UPDATE calls SET duration_seconds=%s WHERE id=%s', (duration, id))
            self.connectivity_locked(db, dict(row, duration_seconds=max(duration, row['duration_seconds'])))
            return row

    def backfill_media_failure(self, call_id):
        """Explicit operator-only audit from trusted terminal state, never an outcome repair."""
        with self.connection() as db:
            row = self.locked(db, call_id)
            if row['active'] or not row['sid'] or not row.get('human') or row['stream_sid'] is not None:
                return False
            return self.media_failure_locked(db, row)

    @staticmethod
    def human_locked(db, row):
        if row['active'] and row['status_final'] == 'ligando':
            db.execute('UPDATE calls SET human=TRUE WHERE id=%s', (row['id'],))

    def human(self, id):
        with self.connection() as db:
            self.human_locked(db, self.locked(db, id))

    def claim_stream(self, id, sid, stream):
        with self.connection() as db:
            row = self.locked(db, id)
            if (not row['active'] or row['status_final'] != 'ligando' or not row['human'] or row['sid'] != sid
                    or row['stream_sid'] is not None):
                raise ValueError('stream not authorized or already bound')
            db.execute('UPDATE calls SET stream_sid=%s WHERE id=%s', (stream, id))
            return row

    def append(self, id, role, content):
        with self.connection() as db:
            row = self.locked(db, id)
            # Accepted final utterances can finish inference after carrier completion.
            db.execute('UPDATE calls SET transcription_json=transcription_json || %s::jsonb WHERE id=%s',
                       (json.dumps([{'role': role, 'content': content}]), id))

    def absent(self, id):
        with self.connection() as db:
            row = self.locked(db, id)
            if row['recording_status'] == 'pending':
                db.execute("UPDATE calls SET recording_status='absent' WHERE id=%s", (id,))

    def recording(self, id, sid, data=None, failed=False):
        with self.connection() as db:
            row = self.locked(db, id)
            if row['recording_sid'] and row['recording_sid'] != sid: raise ValueError('recording SID mismatch')
            if row['recording_status'] != 'available':
                db.execute('UPDATE calls SET recording_sid=%s,recording_status=%s,audio=%s WHERE id=%s',
                           (sid, 'failed' if failed else 'available', data, id))

    def retrieve(self, product_id, embedding):
        with self.connection() as db:
            rows = db.execute('SELECT content FROM product_chunks WHERE product_id=%s '
                              'ORDER BY embedding <=> %s::vector LIMIT 4',
                              (product_id, self.vector(embedding))).fetchall()
            return '\n'.join(row['content'] for row in rows)


if __name__ == '__main__':
    Store(os.environ['DATABASE_URL']).migrate()
    print('Migrations applied')
