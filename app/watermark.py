import json
from datetime import datetime, timezone
from app.errors import DomainError
from app import store

def _dt(bucket):
    d = datetime.fromisoformat(str(bucket).replace('Z', '+00:00'))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

def _now():
    return datetime.now(timezone.utc).isoformat()

def late(buckets, high):
    return [b for b in buckets if high and _dt(b) <= _dt(high)]

def _log(event, run_id, detail):
    # 日志独立提交，不随确认事务回滚：确认过程不丢记录。
    with store.connection() as db:
        db.execute('INSERT INTO watermark_log(event,run_id,detail,at) VALUES (?,?,?,?)',
                   (event, run_id, json.dumps(detail, ensure_ascii=False, sort_keys=True), _now()))

def snapshot():
    with store.connection() as db:
        w = db.execute('SELECT * FROM watermark WHERE id=1').fetchone()
        pending = db.execute("SELECT bucket,first_seq,status,resolved_run FROM backfill WHERE status='待补算' ORDER BY bucket").fetchall()
        resolved = db.execute("SELECT bucket,first_seq,status,resolved_run FROM backfill WHERE status='已补算' ORDER BY bucket DESC LIMIT 5").fetchall()
        proposal = db.execute('SELECT * FROM watermark_proposal ORDER BY version DESC LIMIT 1').fetchone()
        log = db.execute('SELECT * FROM watermark_log ORDER BY seq DESC LIMIT 15').fetchall()
    return {'arrival_seq': w['arrival_seq'], 'next_seq': w['arrival_seq'] + 1,
            'business_high': w['business_high'], 'version': w['version'],
            'pending': [dict(r) for r in pending], 'resolved': [dict(r) for r in resolved],
            'proposal': dict(proposal) if proposal else None, 'log': [dict(r) for r in log]}

def advance(run_id, buckets, attempts=3):
    # 两阶段推进：提案先落库，再单事务确认。确认失败保留旧版、留痕后按最新水位重新提案。
    buckets = sorted(set(buckets), key=_dt)
    for _ in range(attempts):
        with store.connection() as db:
            w = db.execute('SELECT * FROM watermark WHERE id=1').fetchone()
            waiting = {r['bucket'] for r in db.execute("SELECT bucket FROM backfill WHERE status='待补算'")}
        high = w['business_high']
        arrived = late(buckets, high)
        late_new = [b for b in arrived if b not in waiting]
        resolving = [b for b in arrived if b in waiting]
        candidates = ([high] if high else []) + buckets
        new_high = max(candidates, key=_dt) if candidates else high
        proposal = {'version': w['version'] + 1, 'arrival_seq': w['arrival_seq'] + 1,
                    'business_high': new_high, 'run_id': run_id, 'proposed_at': _now()}
        _log('提案', run_id, proposal)
        with store.connection() as db:
            db.execute('INSERT OR REPLACE INTO watermark_proposal(version,arrival_seq,business_high,run_id,proposed_at) VALUES (?,?,?,?,?)',
                       (proposal['version'], proposal['arrival_seq'], proposal['business_high'], run_id, proposal['proposed_at']))
        try:
            _confirm(proposal, late_new, resolving)
            return snapshot()
        except DomainError:
            _log('确认失败', run_id, proposal)
    raise DomainError('STALE', '水位确认冲突，请重试', 409)

def _confirm(proposal, late_new, resolving):
    # 单事务确认：任一失败整体回滚，已确认水位保持旧版。
    v = proposal['version']
    run_id = proposal['run_id']
    with store.connection() as db:
        cur = db.execute('UPDATE watermark SET arrival_seq=?, business_high=?, version=? WHERE id=1 AND version=?',
                         (proposal['arrival_seq'], proposal['business_high'], v, v - 1))
        if cur.rowcount != 1:
            raise DomainError('STALE', '水位已被更新', 409)
        db.execute('DELETE FROM watermark_proposal WHERE version<=?', (v,))
        for b in late_new:
            db.execute("INSERT INTO backfill(bucket,first_seq) VALUES (?,?) ON CONFLICT(bucket) DO NOTHING", (b, proposal['arrival_seq']))
        for b in resolving:
            db.execute("UPDATE backfill SET status='已补算',resolved_run=? WHERE bucket=? AND status='待补算'", (run_id, b))
        db.execute('UPDATE runs SET seq=? WHERE id=?', (proposal['arrival_seq'], run_id))
        db.execute('INSERT INTO watermark_log(event,run_id,detail,at) VALUES (?,?,?,?)',
                   ('确认', run_id, json.dumps({'version': v, 'late': late_new, 'resolved': resolving}, ensure_ascii=False), _now()))
