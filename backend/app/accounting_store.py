"""Store-owned ledger transactions, separate from carrier lifecycle decisions."""
from datetime import datetime, timezone
from decimal import Decimal
import json
from .observability import decimal_string, summarize


class AccountingStore:
    def event_start(self, event):
        with self.connection() as db:
            return db.execute('INSERT INTO integration_events(call_id,payload) VALUES (%s,%s::jsonb) RETURNING id',
                              (event['call_id'], json.dumps(event))).fetchone()['id']

    def event_finish(self, id, event):
        with self.connection() as db:
            db.execute("UPDATE integration_events SET payload=%s::jsonb || "
                       "jsonb_build_object('pricing_snapshot',payload->'pricing_snapshot'),finalized=TRUE "
                       'WHERE id=%s AND NOT finalized', (json.dumps(event), id))

    def account_call(self, id, snapshot):
        with self.connection() as db:
            self.locked(db, id)
            db.execute('INSERT INTO call_accounting(call_id,pricing_snapshot) VALUES (%s,%s::jsonb) '
                       'ON CONFLICT (call_id) DO NOTHING', (id, json.dumps(snapshot)))

    @staticmethod
    def event_rows(rows):
        return [dict(row['payload'], id=row['id']) for row in rows]

    def observability_events(self, call_id=None, limit=100):
        with self.connection() as db:
            rows = db.execute('SELECT id,payload FROM integration_events '
                              'WHERE (%s::bigint IS NULL OR call_id=%s) ORDER BY id DESC LIMIT %s',
                              (call_id, call_id, limit)).fetchall()
            return self.event_rows(rows)

    def observability_summary(self, call_id=None):
        with self.connection() as db:
            # One snapshot for totals and bill; no history-list limit on aggregation.
            db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            rows = db.execute('SELECT id,payload FROM integration_events '
                              'WHERE (%s::bigint IS NULL OR call_id=%s)', (call_id, call_id)).fetchall()
            billed = None
            if call_id is not None:
                row = db.execute('SELECT billed_connectivity FROM call_accounting WHERE call_id=%s', (call_id,)).fetchone()
                if row: billed = row['billed_connectivity']
            return summarize(self.event_rows(rows), call_id, billed)

    def billing_save(self, id, sid, status, amount=None, currency=None, error_code=None):
        with self.connection() as db:
            row = self.locked(db, id)
            if not sid or row['sid'] != sid: raise ValueError('call SID mismatch')
            account = db.execute('SELECT * FROM call_accounting WHERE call_id=%s', (id,)).fetchone()
            prior = account['billed_connectivity'] if account else None
            # A transient failed/pending refresh cannot erase a validated available amount.
            if prior and prior['status'] == 'available' and status != 'available': return prior
            if prior and all(prior.get(key) == value for key, value in
                             {'status': status, 'amount': amount, 'currency': currency, 'error_code': error_code}.items()):
                return prior
            billed = dict(status=status, amount=amount, currency=currency,
                          updated_at=datetime.now(timezone.utc).isoformat(), error_code=error_code)
            db.execute('INSERT INTO call_accounting(call_id,pricing_snapshot,billed_connectivity) '
                       'VALUES (%s,%s::jsonb,%s::jsonb) ON CONFLICT (call_id) DO UPDATE '
                       'SET billed_connectivity=EXCLUDED.billed_connectivity',
                       (id, '{}', json.dumps(billed)))
            return billed

    @staticmethod
    def media_failure_locked(db, row):
        """One derived diagnostic per call, inside the caller's lifecycle transaction."""
        event = dict(call_id=row['id'], provider='twilio', operation='media_stream', model=None,
                     created_at=datetime.now(timezone.utc).isoformat(), elapsed_ms=0, status='error',
                     error_code='media_stream_not_established',
                     error_message='Human call ended without an established media stream',
                     provider_request_id=None, input_tokens=None, output_tokens=None,
                     cached_input_tokens=None, total_tokens=None, characters=None,
                     estimated_cost_usd=None, cost_status='not_applicable', pricing_snapshot={})
        inserted = db.execute('INSERT INTO integration_events(call_id,payload,finalized,stable_key) '
                              'VALUES (%s,%s::jsonb,TRUE,%s) ON CONFLICT (stable_key) DO NOTHING RETURNING id',
                              (row['id'], json.dumps(event), 'media_stream:' + str(row['id']))).fetchone()
        return inserted is not None

    @staticmethod
    def connectivity_locked(db, row):
        """Called only under Store.locked; repeated callbacks replace one stable estimate."""
        account = db.execute('SELECT pricing_snapshot FROM call_accounting WHERE call_id=%s', (row['id'],)).fetchone()
        if not account: return
        snapshot = account['pricing_snapshot']
        rates = snapshot.get('twilio', {})
        duration = row['duration_seconds']
        known = []
        # No answer/duration evidence means unknown, not a fabricated zero-dollar call.
        minutes = (duration + 59) // 60 if duration > 0 else None
        if minutes is not None and 'voice_per_minute' in rates:
            known.append(Decimal(minutes) * Decimal(rates['voice_per_minute']))
        if duration > 0 and 'amd_per_call' in rates: known.append(Decimal(rates['amd_per_call']))
        event = dict(call_id=row['id'], provider='twilio', operation='connectivity_estimate', model=None,
                     created_at=datetime.now(timezone.utc).isoformat(), elapsed_ms=0, status='success',
                     error_code=None, error_message=None, provider_request_id=None,
                     input_tokens=None, output_tokens=None, cached_input_tokens=None, total_tokens=None,
                     characters=None, estimated_cost_usd=decimal_string(sum(known)) if known else None,
                     cost_status='estimated' if known else 'unknown', pricing_snapshot=snapshot,
                     billed_minutes=minutes, duration_seconds=duration,
                     rounding_policy='ceil(duration_seconds / 60); AMD once when answered duration > 0',
                     excluded_costs=['recording', 'storage', 'streams', 'infrastructure', 'taxes'],
                     unknown_components=[key for key in ('voice_per_minute', 'amd_per_call') if key not in rates])
        db.execute('INSERT INTO integration_events(call_id,payload,finalized,stable_key) VALUES (%s,%s::jsonb,TRUE,%s) '
                   "ON CONFLICT (stable_key) DO UPDATE SET payload=EXCLUDED.payload || "
                   "jsonb_build_object('created_at',integration_events.payload->'created_at') "
                   "WHERE (integration_events.payload->>'duration_seconds')::integer < %s",
                   (row['id'], json.dumps(event), 'connectivity:' + str(row['id']), duration))
