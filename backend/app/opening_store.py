"""Durable cache/pins. Unknown preparations never retry automatically.

A pending row can mean a paid response was lost, including process death. An operator
must reconcile usage and explicitly remove that pending identity before a manual POST
can retry it. This is exclusion, NOT exactly-once external payment. No public reset.
"""


class OpeningStore:
    def preparation_guard(self):
        db = self.connection()
        try:
            acquired = db.execute('SELECT pg_try_advisory_lock(743282) AS held').fetchone()['held']
            db.commit()
            if not acquired: raise ValueError('Speech preparation busy')
            return db  # Session lock survives commits and is released only by close.
        except BaseException:
            db.close()
            raise

    def opening_inputs(self, lead_id, product_id):
        with self.connection() as db:
            lead = db.execute('SELECT * FROM leads WHERE id=%s', (lead_id,)).fetchone()
            product = db.execute('SELECT * FROM products WHERE id=%s', (product_id,)).fetchone()
            if not lead or not product: raise LookupError('not found')
            if db.execute('SELECT id FROM calls WHERE active').fetchone():
                raise ValueError('another call is active')
            return product

    def opening_begin(self, digest, text, model, voice, format):
        with self.connection() as db:
            row = db.execute('SELECT * FROM opening_audio WHERE digest=%s', (digest,)).fetchone()
            if row:
                if row['state'] != 'ready':
                    raise RuntimeError('Unknown preparation; explicit operator reconciliation required')
                return bytes(row['audio'])
            db.execute('INSERT INTO opening_audio(digest,text,model,voice,format,event_floor) '
                       'VALUES (%s,%s,%s,%s,%s,(SELECT COALESCE(MAX(id),0) FROM integration_events))',
                       (digest,text,model,voice,format))

    def opening_save(self, digest, audio, ledger):
        if not isinstance(audio, bytes) or not 1 <= len(audio) <= 720000:
            raise ValueError('Invalid prepared audio')
        with self.connection() as db:
            row = db.execute('SELECT * FROM opening_audio WHERE digest=%s FOR UPDATE', (digest,)).fetchone()
            if ledger:
                event = db.execute("SELECT finalized,payload FROM integration_events WHERE id>%s "
                    "AND call_id IS NULL AND payload->>'operation'='speak' ORDER BY id DESC LIMIT 1",
                    (row['event_floor'],)).fetchone()
                if not event or not event['finalized'] or event['payload']['status'] != 'success':
                    raise RuntimeError('Preparation accounting unavailable')
            db.execute("UPDATE opening_audio SET audio=%s,state='ready' WHERE digest=%s AND state='pending'",
                       (audio,digest))

    def opening_intro(self, id):
        with self.connection() as db:
            row = db.execute("SELECT a.text,a.audio FROM call_openings p JOIN opening_audio a "
                "ON a.digest=p.intro WHERE p.call_id=%s AND a.state='ready'", (id,)).fetchone()
            if not row: raise ValueError('Prepared intro missing')
            return row['text'], bytes(row['audio'])

    def abort_undialed(self, id):
        """Only dispatch, before invoking dial, may use this proven-no-provider-call path."""
        with self.connection() as db:
            row = self.locked(db, id)
            if row['sid'] or row['stream_sid']: raise ValueError('Cannot abort a bound call')
            db.execute("UPDATE calls SET active=FALSE,status_final='falha_tecnica' WHERE id=%s", (id,))
            db.execute("UPDATE leads SET status='falha_tecnica' WHERE id=%s", (row['lead_id'],))
