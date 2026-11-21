import sqlite3,json,os
from pathlib import Path
from app.errors import DomainError

def connection():
    path=Path(os.environ.get('DB_PATH','data/records.db'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    if 'seq' not in [r[1] for r in db.execute('PRAGMA table_info(runs)')]:
        db.execute('ALTER TABLE runs ADD COLUMN seq INTEGER')
    db.execute('CREATE TABLE IF NOT EXISTS watermark (id INTEGER PRIMARY KEY CHECK(id=1), arrival_seq INTEGER NOT NULL, business_high TEXT, version INTEGER NOT NULL)')
    db.execute('INSERT OR IGNORE INTO watermark(id,arrival_seq,business_high,version) VALUES (1,0,NULL,1)')
    db.execute('CREATE TABLE IF NOT EXISTS watermark_proposal (version INTEGER PRIMARY KEY, arrival_seq INTEGER NOT NULL, business_high TEXT, run_id TEXT NOT NULL, proposed_at TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS backfill (bucket TEXT PRIMARY KEY, first_seq INTEGER NOT NULL, status TEXT NOT NULL DEFAULT "待补算", resolved_run TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS watermark_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, run_id TEXT, detail TEXT NOT NULL, at TEXT NOT NULL)')
    return db

def save(run_id, body):
    with connection() as db:
        db.execute('INSERT OR IGNORE INTO runs(id,body) VALUES (?,?)',(run_id,json.dumps(body,ensure_ascii=False)))
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
        seq=db.execute('SELECT seq FROM runs WHERE id=?',(run_id,)).fetchone()['seq']
    if seq is None:
        # 仅未占位的批次推进水位；重试同一批次不重复占位，水位不倒退。
        from app import watermark
        watermark.advance(run_id,[c['id'] for c in body.get('calculations',[])])
    return get(run_id)

def get(run_id):
    with connection() as db:r=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','批次不存在',404)
    body=json.loads(r['body']);body.update(run_id=r['id'],review_version=r['version'],decision=r['decision'],arrival_seq=r['seq']);return body

def history():
    with connection() as db:rows=db.execute("SELECT id,version,decision,seq FROM runs ORDER BY CASE WHEN id=(SELECT value FROM meta WHERE key='latest') THEN 0 ELSE 1 END,rowid DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]

def review(run_id, version, decision):
    if decision not in ('通过','退回'):raise DomainError('DECISION','复核状态无效')
    with connection() as db:
        row=db.execute('SELECT version,decision FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row:raise DomainError('NOT_FOUND','批次不存在',404)
        if row['decision']=='通过':raise DomainError('SEALED','已签认结果不能覆盖',409)
        cur=db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?',(decision,run_id,version))
        if cur.rowcount!=1:raise DomainError('STALE','复核版本已更新，请刷新',409)
    return get(run_id)
