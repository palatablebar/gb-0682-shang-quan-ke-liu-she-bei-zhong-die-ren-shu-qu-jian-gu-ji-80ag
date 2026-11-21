import pytest
from app import store, watermark
from app.errors import DomainError
from app.workflow import run

HEADER = 'id,bucket,version,kind,left,right,count,unit\n'
A09 = HEADER + 'a1,2026-11-16T09:00:00+08:00,1,zone,东门,,120,人\n'  # UTC 01:00
A10 = HEADER + 'a2,2026-11-16T10:00:00+08:00,1,zone,东门,,121,人\n'  # UTC 02:00
A08 = HEADER + 'a3,2026-11-16T08:00:00+08:00,1,zone,东门,,119,人\n'  # UTC 00:00 补传
A08V2 = HEADER + 'a4,2026-11-16T08:00:00+08:00,1,zone,东门,,130,人\n'  # 重算 UTC 00:00

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'w.db'))
    return store

def save_csv(text):
    state = run(text.encode(), {})
    return store.save(state['run_id'], state)

def test_first_batch_sets_watermark(db):
    save_csv(A09)
    s = watermark.snapshot()
    assert s['arrival_seq'] == 1 and s['next_seq'] == 2
    assert s['business_high'] == '2026-11-16T01:00:00+00:00' and s['pending'] == []

def test_retry_does_not_regress(db):
    save_csv(A09)
    save_csv(A09)  # 同一批次重试：幂等键相同
    s = watermark.snapshot()
    assert s['arrival_seq'] == 1 and s['next_seq'] == 2

def test_late_batch_registers_backfill(db):
    save_csv(A09); save_csv(A10)
    save_csv(A08)  # 迟到补传：业务时间早于当前业务水位
    s = watermark.snapshot()
    assert s['arrival_seq'] == 3  # 到达水位不跳过迟到批次
    assert s['business_high'] == '2026-11-16T02:00:00+00:00'  # 业务水位不倒退
    assert [p['bucket'] for p in s['pending']] == ['2026-11-16T00:00:00+00:00']

def test_recalculation_resolves_backfill(db):
    save_csv(A09); save_csv(A10); save_csv(A08)
    save_csv(A08V2)  # 覆盖迟到时段的重算批次
    s = watermark.snapshot()
    assert s['pending'] == []
    assert s['resolved'][0]['status'] == '已补算'
    assert s['resolved'][0]['bucket'] == '2026-11-16T00:00:00+00:00'

def test_sealed_review_cannot_be_overwritten(db):
    rid = save_csv(A09)['run_id']
    store.review(rid, 1, '通过')
    with pytest.raises(DomainError) as e:
        store.review(rid, 2, '退回')
    assert e.value.code == 'SEALED' and e.value.status == 409
    assert store.get(rid)['decision'] == '通过'

def test_failed_confirm_keeps_old_and_logs(db, monkeypatch):
    save_csv(A09)
    before = watermark.snapshot()
    def boom(*args):
        raise DomainError('STALE', '水位已被更新', 409)
    monkeypatch.setattr(watermark, '_confirm', boom)
    with pytest.raises(DomainError):
        save_csv(A10)
    after = watermark.snapshot()
    assert after['arrival_seq'] == before['arrival_seq']  # 确认失败保留旧版
    assert after['proposal'] is not None  # 提案留痕
    assert any(l['event'] == '确认失败' for l in after['log'])  # 过程不丢记录

def test_retry_after_failed_confirm_heals(db, monkeypatch):
    save_csv(A09)
    real = watermark._confirm
    def boom(*args):
        raise DomainError('STALE', '水位已被更新', 409)
    monkeypatch.setattr(watermark, '_confirm', boom)
    with pytest.raises(DomainError):
        save_csv(A10)
    monkeypatch.setattr(watermark, '_confirm', real)
    save_csv(A10)  # 同一批次重试：补齐水位，不重复占位
    s = watermark.snapshot()
    assert s['arrival_seq'] == 2 and s['business_high'] == '2026-11-16T02:00:00+00:00'

def test_watermark_page_shows_next_and_pending(client):
    client.post('/api/runs', json={'csv': A09})  # 业务时间早于种子批次水位
    page = client.get('/m8_watermark').data.decode()
    assert '下一批续跑位置' in page and '待补算' in page
    assert '2026-11-16T01:00:00+00:00' in page

def test_watermark_api(client):
    s = client.get('/api/m8_watermark').get_json()
    assert s['watermark']['next_seq'] == s['watermark']['arrival_seq'] + 1
    assert s['watermark']['business_high'] == '2026-11-16T12:00:00+00:00'
