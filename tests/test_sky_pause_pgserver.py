"""Real isolated PostgreSQL for atomic Sky pause generation writes."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest

from test_sky_delivery_pgserver import pg, ROOT
from gateway import sky_settings as settings

ZERO = settings.INITIAL_EPOCH
MIGRATION = ROOT/'supabase/migrations/20261024030000_sky_access_pause_epoch.sql'


def write(conn, enabled, epoch=None):
    return conn.execute('select public.sky_set_access(%s,%s)', (enabled, epoch or str(uuid4()))).fetchone()[0]


@pytest.fixture(scope='module')
def control(pg):
    conn = pg[0]
    conn.execute((ROOT/'supabase/migrations/20260920010000_create_app_settings.sql').read_text(encoding='utf8'))
    conn.execute("insert into app_settings(key,value) values('sky.enabled','true'::jsonb)")
    conn.execute(MIGRATION.read_text(encoding='utf8'))
    yield pg


def test_migration_preserves_old_boolean_and_is_repeatable(control):
    conn = control[0]
    assert conn.execute("select value from app_settings where key='sky.enabled'").fetchone()[0] == {'enabled': True, 'pause_epoch': ZERO}
    disabled = write(conn, False)
    conn.execute(MIGRATION.read_text(encoding='utf8'))
    assert conn.execute("select value from app_settings where key='sky.enabled'").fetchone()[0] == disabled
    assert conn.execute("select value from app_settings where key='eventide.inject_enabled'").fetchone()[0] is True
    conn.execute("update app_settings set value='false'::jsonb where key='sky.enabled'")
    conn.execute(MIGRATION.read_text(encoding='utf8'))
    assert conn.execute("select value from app_settings where key='sky.enabled'").fetchone()[0] == {'enabled': False, 'pause_epoch': ZERO}


def test_fast_aba_and_process_cache_reset_read_same_committed_epoch(control, monkeypatch):
    conn = control[0]
    def load(key): return conn.execute('select value from app_settings where key=%s', (key,)).fetchone()[0]
    class Client:
        def rpc(self, name, payload):
            assert name == 'sky_set_access'
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=write(conn, payload['p_enabled'], payload['p_pause_epoch'])))
    monkeypatch.setattr(settings.db, 'load_app_setting', load)
    monkeypatch.setattr(settings.db, 'get_client_status', lambda: {'elevated_active': True})
    monkeypatch.setattr(settings.db, 'get_client', Client)
    try:
        first = settings.save(True)
        paused = settings.save(False)
        resumed = settings.save(True)
        settings.reset_cache()
        assert settings.snapshot(refresh=True)['pause_epoch'] == paused['pause_epoch'] == resumed['pause_epoch'] != first['pause_epoch']
    finally: settings.reset_cache()


def test_enabler_queued_behind_uncommitted_disable_keeps_its_epoch(control):
    import psycopg
    conn, uri = control[:2]
    epoch = str(uuid4())
    with psycopg.connect(uri) as disabling:
        paused = write(disabling, False, epoch)  # Hold advisory + row lock until commit.
        with ThreadPoolExecutor(max_workers=1) as pool:
            def enable():
                with psycopg.connect(uri, autocommit=True) as other: return write(other, True)
            future = pool.submit(enable)
            disabling.commit()
            resumed = future.result(timeout=10)
    assert resumed == {'enabled': True, 'pause_epoch': paused['pause_epoch']} == {'enabled': True, 'pause_epoch': epoch}
    assert write(conn, True) == resumed  # Repeated enable never erases pause history.


def test_concurrent_disables_have_distinct_committed_epochs(control):
    import psycopg
    uri = control[1]
    def disable(_):
        with psycopg.connect(uri, autocommit=True) as other: return write(other, False)['pause_epoch']
    with ThreadPoolExecutor(max_workers=4) as pool: epochs = list(pool.map(disable, range(8)))
    assert len(set(epochs)) == 8
    assert write(control[0], True)['pause_epoch'] in epochs


def test_invalid_request_rolls_back_and_rpc_cannot_be_called_by_browser_roles(control):
    import psycopg
    conn = control[0]
    old = write(conn, False)
    for epoch in (ZERO, old['pause_epoch']):
        with pytest.raises(psycopg.errors.InvalidParameterValue): write(conn, False, epoch)
    assert write(conn, True)['pause_epoch'] == old['pause_epoch']
    for role in ('anon', 'authenticated'):
        assert conn.execute('select has_function_privilege(%s,%s,%s)', (role, 'public.sky_set_access(boolean,uuid)', 'EXECUTE')).fetchone()[0] is False
    assert conn.execute("select has_function_privilege('service_role','public.sky_set_access(boolean,uuid)','EXECUTE')").fetchone()[0] is True
    conn.execute('set role service_role')
    try: assert write(conn, True)['pause_epoch'] == old['pause_epoch']
    finally: conn.execute('reset role')
