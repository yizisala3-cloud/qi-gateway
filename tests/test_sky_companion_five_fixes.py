"""Current fixes: receipt durability, stable chronology, identities and budgets."""
import json
from uuid import uuid4

import httpx
import pytest

from test_sky_companion import b, config, private_files, peer_line, FakeDevice, screen


@pytest.mark.parametrize('name', ['叶·子','阿祁·叶子','A-B','A—B','小·阿·祁','叶子.'])
def test_names_and_punctuation_in_body_keep_distinct_identity(config, name):
    raw = 'A-B 和 C·D 都来了 - '+name
    assert b.chat_messages([peer_line(raw)],config['chat_region'],['叶子'],mode='group') == [
        {'sender':name,'text':'A-B 和 C·D 都来了'}]
    if name != '叶子':
        assert b.chat_messages([peer_line('叶子：你好 - '+name)],config['chat_region'],['叶子']) == []


def test_ambiguous_unspaced_name_does_not_gain_primary_identity(config):
    assert b.chat_messages([peer_line('你好-阿祁-叶子')],config['chat_region'],['叶子'],mode='group') == []


def test_multiline_symbol_name_is_not_shortened_to_primary(config):
    rows=[peer_line('第一行正文',y=100,height=20),peer_line('第二行正文 -',y=123,height=20),
          peer_line('阿祁·叶子',y=146,height=20)]
    assert b.chat_messages(rows,config['chat_region'],['叶子'],mode='group') == [
        {'sender':'阿祁·叶子','text':'第一行正文第二行正文'}]


@pytest.mark.parametrize('frames', [[], [0], [1], [0,2], [2], [0,1,2]])
def test_missing_rows_preserve_order_transitively(config,frames):
    values=[{'sender':'叶子','text':str(i)} for i in range(4)]
    inbox=b.Inbox(config)
    inbox.observe_order(values[:2]); inbox.observe_order(values[1:3])
    inbox.add(values,1)
    inbox.observe_order([values[i] for i in frames])
    assert inbox.take(timeout=0)['messages'] == values


def test_conflicting_frame_cannot_reverse_established_order(config):
    values=[{'sender':'叶子','text':str(i)} for i in range(3)]
    inbox=b.Inbox(config); inbox.observe_order(values); inbox.add(values,1)
    inbox.observe_order(values[::-1])
    assert inbox.take(timeout=0)['messages']==values


@pytest.mark.parametrize('mode',['group','direct'])
def test_long_queue_drains_exactly_once_in_actual_wire_budget(config,mode):
    config.update(chat_mode=mode,primary_user='主'*30)
    messages=[{'sender':'玩家甲','text':f'{i}:'+('你"好\\'*40)} for i in range(401)]
    inbox=b.Inbox(config); inbox.add(messages,1)
    received=[]; ids=[]; batches=0
    while batch:=inbox.take(timeout=0):
        assert len(b.conversation_query(batch['messages'],config)) <= 20000
        received.extend(batch['messages']); ids.extend(batch['event_ids']); batches+=1
        inbox.acknowledge()
    assert received==messages and len(set(ids))==401 and batches>1
    assert all(event['state']=='handled' for event in inbox.events.values())


def test_oversize_single_is_preserved_separately_and_does_not_block_normal(config):
    config['chat_mode']='group'
    huge={'sender':'叶子','text':'大'*14000}
    normal={'sender':'叶子','text':'在吗'}
    inbox=b.Inbox(config); inbox.add([huge,normal],1)
    assert inbox.take(timeout=0)['messages']==[normal]
    rejected=list((b.PRIVATE/'rejected_messages').glob('*.json'))
    assert len(rejected)==1 and json.loads(rejected[0].read_text(encoding='utf8'))['message']==huge
    assert inbox.events[b.message_identity(huge)]['state']=='rejected_oversize'
    inbox.acknowledge(); assert inbox.take(timeout=0) is None


def test_partial_merge_retains_rest_and_ids(config):
    config['chat_mode']='group'
    first={'sender':'叶子','text':'先说一句'}
    later=[{'sender':'玩家甲','text':str(i)+':'+'好'*80} for i in range(400)]
    inbox=b.Inbox(config); inbox.add([first],1); original=inbox.take(timeout=0)
    inbox.add(later,2); before={entry['event_id'] for entry in inbox.pending}
    merged=inbox.merge_pending(config)
    assert merged and len(b.conversation_query(merged['messages'],config))<=20000 and inbox.pending
    assert set(merged['event_ids'])|{entry['event_id'] for entry in inbox.pending} == before|set(original['event_ids'])


def fake_gateway(config,requests):
    def respond(request):
        requests.append(request.url.path)
        if request.url.path=='/status': return httpx.Response(200,json={'sky_delivery_protocol':1})
        if request.url.path.endswith('chat/completions'):
            return httpx.Response(200,json={'choices':[{'message':{'content':'在呢。'}}]})
        payload=json.loads(request.content)
        return httpx.Response(200,json={'delivery_id':payload['delivery_id'],'user_message_id':1,'assistant_message_id':2})
    return b.Gateway(config,transport=httpx.MockTransport(respond))


@pytest.mark.parametrize('failure',['diagnostics','outbox','decision'])
def test_confirmed_fact_recovers_from_independent_file_failures(config,monkeypatch,failure):
    requests=[]; gateway=fake_gateway(config,requests); device=FakeDevice()
    companion=b.Companion(config,device,gateway,reader=lambda _:screen(history=['在呢。'] if device.keys else []))
    companion.collect=lambda:None
    companion.inbox.add([{'sender':'叶子','text':'在吗'}],1)
    original_save=b.save_json
    def save(path,value):
        if device.keys:
            if failure=='outbox' and path.parent.name=='deliveries':
                raise PermissionError('outbox locked')
            if failure=='decision' and path.parent.name=='decisions' and value.get('sent') is True:
                raise PermissionError('decision locked')
            if failure=='diagnostics' and path.name in ('last_send.json','last_send_attempt.json','last_reply.json','last_latency.json'):
                raise PermissionError('diagnostic locked')
        original_save(path,value)
    monkeypatch.setattr(b,'save_json',save)
    def snapshot(): raise PermissionError('screen.png locked')
    monkeypatch.setattr(companion,'save_snapshot',snapshot)
    acknowledge=companion.inbox.acknowledge
    def finish(): acknowledge(); companion.stop.set()
    monkeypatch.setattr(companion.inbox,'acknowledge',finish)
    try:
        if failure=='diagnostics': companion.run()
        else:
            with pytest.raises(PermissionError): companion.run()
        assert len(device.keys)==1
        monkeypatch.setattr(b,'save_json',original_save)
        gateway.flush_deliveries(); gateway.flush_deliveries()
    finally: gateway.close()
    assert requests.count('/v1/sky/deliveries')==1
    outbox=list((b.PRIVATE/'deliveries').glob('*.json'))
    assert len(outbox)==1 and json.loads(outbox[0].read_text(encoding='utf8'))['persisted']
    decision=json.loads(next((b.PRIVATE/'decisions').glob('*.json')).read_text(encoding='utf8'))
    assert decision['status']=='completed' and decision['sent'] is True


def test_unconfirmed_submission_cannot_be_reconstructed_from_decision(config):
    identity=str(uuid4()); requests=[]
    b.save_json(b.PRIVATE/'decisions'/f'{identity}.json',{'decision_id':identity,'status':'awaiting_receipt',
                'query':'在吗','text':'在呢','sent':False})
    gateway=fake_gateway(config,requests)
    try: gateway.flush_deliveries()
    finally: gateway.close()
    assert requests==[] and not list((b.PRIVATE/'deliveries').glob('*.json'))
