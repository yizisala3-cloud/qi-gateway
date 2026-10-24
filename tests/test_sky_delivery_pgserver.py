"""Opt-in real PostgreSQL: immutable legacy ownership and retry atomicity."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

ROOT=Path(__file__).resolve().parents[1]
STAMP=datetime(2026,10,24,12,tzinfo=timezone.utc)


def publish(conn,identity,author='first-owner',vm=10,query='question',text='answer',stamp=STAMP):
    return conn.execute('select public.sky_publish_chat_delivery(%s,%s,%s,%s,%s,%s)',
                        (identity,author,vm,query,text,stamp)).fetchone()[0]


@pytest.fixture(scope='module')
def pg(request,tmp_path_factory):
    if not request.config.getoption('--db'): pytest.skip('use --db for isolated PostgreSQL')
    import pgserver,psycopg
    server=pgserver.get_server(tmp_path_factory.mktemp('sky-pg'),cleanup_mode='stop')
    conn=None
    try:
        conn=psycopg.connect(server.get_uri(),autocommit=True)
        conn.execute('''create role anon nologin; create role authenticated nologin;
            create role service_role nologin bypassrls;
            grant usage on schema public to anon,authenticated,service_role;
            create table public.chat_messages(id bigint generated always as identity primary key,
                assistant_id text not null,conversation_id text not null,role text not null,
                content text not null,created_at timestamp without time zone not null);
            insert into public.chat_messages(assistant_id,conversation_id,role,content,created_at)
                values('legacy','existing','user','existing immutable row',now());''')
        conn.execute((ROOT/'supabase/migrations/20261023010000_sky_confirmed_chat_delivery.sql').read_text(encoding='utf8'))
        legacy=str(uuid4()); old=publish(conn,legacy,author='legacy-owner')
        old_hash=conn.execute('select payload_hash from sky_chat_deliveries where delivery_id=%s',(legacy,)).fetchone()[0]
        migration=(ROOT/'supabase/migrations/20261024010000_sky_retry_original_author.sql').read_text(encoding='utf8')
        conn.execute(migration);conn.execute(migration)
        yield conn,server.get_uri(),legacy,old,old_hash
    finally:
        if conn is not None: conn.close()
        server.cleanup()


def test_legacy_receipt_survives_upgrade_and_discovered_author_change(pg):
    conn,_,identity,old,old_hash=pg
    replay=publish(conn,identity,author='other-client')
    assert replay['duplicate'] and replay['assistant_message_id']==old['assistant_message_id']
    assert conn.execute('select assistant_id from chat_messages where id=%s',(old['assistant_message_id'],)).fetchone()[0]=='legacy-owner'
    assert conn.execute('select payload_hash from sky_chat_deliveries where delivery_id=%s',(identity,)).fetchone()[0]==old_hash


@pytest.mark.parametrize('change',[{'query':'changed'},{'text':'changed'},{'vm':11},{'stamp':STAMP+timedelta(seconds=1)}])
def test_real_client_payload_changes_remain_conflicts(pg,change):
    import psycopg
    conn=pg[0];identity=str(uuid4());first=publish(conn,identity)
    with pytest.raises(psycopg.errors.InvalidParameterValue): publish(conn,identity,author='other-client',**change)
    assert publish(conn,identity)['assistant_message_id']==first['assistant_message_id']


def test_fresh_server_envelope_after_response_loss_uses_original_database_owner(pg,monkeypatch):
    from gateway import sky_delivery as sd
    conn=pg[0];authors=iter(['first-process','restarted-process'])
    monkeypatch.setattr(sd,'resolve_assistant_id',lambda:next(authors))
    monkeypatch.setattr(sd.db,'get_client_status',lambda:{'elevated_active':True})
    class Client:
        def rpc(self,name,payload):
            assert name=='sky_publish_chat_delivery'
            def execute():
                result=conn.execute('select public.sky_publish_chat_delivery(%s,%s,%s,%s,%s,%s)',
                    tuple(payload[k] for k in ('p_delivery_id','p_assistant_id','p_vm_index',
                                               'p_user_text','p_assistant_text','p_confirmed_at'))).fetchone()[0]
                return SimpleNamespace(data=result)
            return SimpleNamespace(execute=execute)
    monkeypatch.setattr(sd.db,'get_client',lambda:Client())
    body={'delivery_id':str(uuid4()),'vm_index':10,'query':'在吗','text':'在呢','confirmed':True,'confirmed_at':STAMP.isoformat()}
    first=sd.publish_delivery(sd.validate_delivery(body))
    replay=sd.publish_delivery(sd.validate_delivery(body))  # New dict, no in-memory identity cache.
    assert replay['duplicate'] and replay['user_message_id']==first['user_message_id']
    assert conn.execute('select assistant_id from chat_messages where id=%s',(first['assistant_message_id'],)).fetchone()[0]=='first-process'


def test_concurrent_different_authors_create_one_pair(pg):
    import psycopg
    conn,uri=pg[:2];identity=str(uuid4())
    before=conn.execute('select count(*) from chat_messages').fetchone()[0]
    def call(author):
        with psycopg.connect(uri,autocommit=True) as other: return publish(other,identity,author=author)
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(call,['client-a','client-b']))
    assert {result['duplicate'] for result in results}=={True,False}
    assert len({result['assistant_message_id'] for result in results})==1
    assert conn.execute('select count(*) from chat_messages').fetchone()[0]==before+2


def test_original_atomicity_permissions_and_old_row_checks_still_hold(pg):
    pg[0].execute((ROOT/'tests/sky_delivery_postgres.sql').read_text(encoding='utf8'))
