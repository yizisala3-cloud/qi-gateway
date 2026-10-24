"""Replay observed OCR variants, with fake game and gateway boundaries."""
import json
import threading

import pytest

from test_sky_companion import b, config, private_files, FakeDevice, screen, peer_line


VARIANTS = ['是一脚把你踢飞的创法（）', '是一脚把你踢飞的创法（‘）', '是一脚把你踢飞的创法（’）']


def message(text, sender='叶子'):
    return {'sender': sender, 'text': text}


def test_observed_emoticon_variants_trigger_only_once():
    tracker = b.Tracker()
    tracker.update([])
    emitted = []
    for text in VARIANTS:
        for _ in range(2):
            emitted.extend(tracker.update([message(text)]))
        tracker.update([])  # Delivery pauses/temporary OCR dropout.
    assert emitted == [message(VARIANTS[0])]


def test_baseline_emoticon_drift_does_not_become_a_new_message():
    tracker = b.Tracker()
    tracker.update([message(VARIANTS[0])])
    for variant in VARIANTS[1:]:
        for _ in range(2):
            assert tracker.update([message(variant)]) == []


@pytest.mark.parametrize('state', ['pending', 'inflight', 'handled'])
def test_inbox_rejects_same_event_in_every_lifecycle_state(state):
    inbox = b.Inbox()
    inbox.add([message(VARIANTS[0])], 1)
    if state != 'pending':
        inbox.take(timeout=0)
    if state == 'handled':
        inbox.acknowledge()
    inbox.add([message(VARIANTS[1]), message(VARIANTS[2])], 2)
    assert len(inbox.pending) + len(inbox.inflight) == (0 if state == 'handled' else 1)


def test_normalization_preserves_people_words_numbers_operators_and_emotions():
    messages = [message('同一句', sender='叶子'), message('同一句', sender='咕咕咕'),
                message('去雨林'), message('不去雨林'), message('1.2'), message('12'),
                message('1+2'), message('1-2'), message('喜欢你(❤)'), message('喜欢你(💔)'),
                message('好?'), message('好!'), message('到了(1)'), message('到了(2)')]
    tracker = b.Tracker()
    tracker.update([])
    tracker.update(messages)
    assert tracker.update(messages) == messages


@pytest.mark.parametrize('reply', ['知道啦。', None])
def test_ocr_variants_during_generation_do_not_create_a_second_reply(config, reply):
    started, duplicate_observed = threading.Event(), threading.Event()
    class Gateway:
        def __init__(self):
            self.calls, self.confirmations = 0, []
        def status(self):
            return {}
        def flush_deliveries(self):
            pass
        def generate(self, query, history):
            self.calls += 1
            started.set()
            assert duplicate_observed.wait(2)
            return reply
        def confirm_delivery(self, query, reply, **metadata):
            self.confirmations.append(metadata)
    gateway = Gateway()
    companion = b.Companion(config, FakeDevice(), gateway, reader=lambda _: None)
    def collect():
        companion.inbox.add([message(VARIANTS[0])], 1)
        assert started.wait(2)
        companion.inbox.add([message(VARIANTS[1]), message(VARIANTS[2])], 2)
        duplicate_observed.set()
    companion.collect = collect
    sent = []
    companion.send = lambda text: sent.append(text) or {'confirmed': True, 'send_ms': 0}
    acknowledge = companion.inbox.acknowledge
    def finish():
        acknowledge()
        companion.stop.set()
    companion.inbox.acknowledge = finish
    companion.run()
    assert gateway.calls == 1 and sent == ([] if reply is None else [reply])
    assert len(gateway.confirmations) == (0 if reply is None else 1)
    assert companion.inbox.pending == [] and companion.inbox.inflight == []
    decisions = list((b.PRIVATE / 'decisions').glob('*.json'))
    assert len(decisions) == 1
    decision = json.loads(decisions[0].read_text(encoding='utf-8'))
    assert decision['status'] == ('passed' if reply is None else 'completed')
    assert decision['trigger_kind'] == 'reply' and len(decision['event_ids']) == 1
    assert decision['sent'] is (reply is not None) and decision['handled']
    assert gateway.confirmations == ([] if reply is None else [{'delivery_id': decision['decision_id']}])


def test_player_name_punctuation_is_never_removed_by_message_identity():
    messages = [message('在吗', sender='叶子'), message('在吗', sender='叶子.'),
                message('在吗', sender='叶·子')]
    tracker = b.Tracker()
    tracker.update([])
    tracker.update(messages)
    assert tracker.update(messages) == messages


def test_a_batch_cannot_generate_another_decision_after_restart(config):
    first = b.Companion(config, FakeDevice(), None, reader=lambda _: None)
    first.inbox.add([message('在吗')], 1)
    batch = first.inbox.take(timeout=0)
    query = b.conversation_query(batch['messages'], config)
    decision = first.begin_decision(batch, query)
    first.decision_state('unconfirmed')
    restarted = b.Companion(config, FakeDevice(), None, reader=lambda _: None)
    with pytest.raises(b.BridgeError, match='重复生成或发送'):
        restarted.begin_decision(batch, query)
    stored = json.loads((b.PRIVATE / 'decisions' / (decision['decision_id']+'.json')).read_text(encoding='utf-8'))
    assert stored['status'] == 'unconfirmed'


@pytest.mark.parametrize('winerror', [5, 32])
def test_transient_windows_json_replacement_is_retried_without_losing_old_state(monkeypatch, winerror):
    path = b.PRIVATE / 'state.json'
    b.save_json(path, {'old': True})
    original, calls = type(path).replace, []
    def replacing(temp, target):
        calls.append(str(temp))
        if len(calls) < 3:
            assert json.loads(path.read_text()) == {'old': True}
            error = PermissionError('temporary sharing conflict')
            error.winerror = winerror
            raise error
        return original(temp, target)
    monkeypatch.setattr(type(path), 'replace', replacing)
    b.save_json(path, {'new': True})
    assert len(calls) == 3 and json.loads(path.read_text()) == {'new': True}


def test_permanent_json_permission_failure_stops_and_preserves_previous_state(monkeypatch):
    path = b.PRIVATE / 'state.json'
    b.save_json(path, {'old': True})
    calls = []
    def failing(temp, target):
        calls.append(True)
        error = PermissionError('persistent denied')
        error.winerror = 5
        raise error
    monkeypatch.setattr(type(path), 'replace', failing)
    with pytest.raises(PermissionError):
        b.save_json(path, {'new': True})
    assert len(calls) == 4 and json.loads(path.read_text()) == {'old': True}


def test_submission_intent_is_saved_before_the_only_send_key(config):
    device = FakeDevice()
    companion = b.Companion(config, device, None,
                            reader=lambda _: screen(history=['知道啦。'] if device.keys else []))
    companion.inbox.add([message('在吗')], 1)
    batch = companion.inbox.take(timeout=0)
    intent = companion.begin_decision(batch, '叶子：在吗')
    pressed = device.adb
    def guarded(*args):
        stored = json.loads((b.PRIVATE / 'decisions' / (intent['decision_id']+'.json')).read_text(encoding='utf-8'))
        assert stored['status'] == 'submitting'
        pressed(*args)
    device.adb = guarded
    assert companion.send('知道啦。')['confirmed']
    assert len(device.keys) == 1


@pytest.mark.parametrize('old_reply', ['旧草稿', None])
def test_new_message_during_generation_is_merged_before_any_send(config, old_reply):
    config['chat_mode'] = 'group'
    first, latest = message('你好'), message('又卡了吗')
    class Gateway:
        def __init__(self):
            self.queries, self.commits = [], []
        def status(self):
            return {}
        def flush_deliveries(self):
            pass
        def generate(self, query, history):
            self.queries.append(json.loads(query))
            if len(self.queries) == 1:
                companion.inbox.add([latest], 2)
                return old_reply
            return '没卡，你这两句都看到了。'
        def confirm_delivery(self, query, reply, **metadata):
            self.commits.append((json.loads(query), reply))
    gateway = Gateway()
    companion = b.Companion(config, FakeDevice(), gateway, reader=lambda _: None)
    companion.collect = lambda: companion.inbox.add([first], 1)
    sent = []
    def send(reply):
        sent.append(reply)
        companion.stop.set()
        return {'confirmed': True, 'send_ms': 0}
    companion.send = send
    companion.run()
    assert sent == ['没卡，你这两句都看到了。']
    assert len(gateway.queries) == 2 and gateway.queries[-1]['messages'] == [first, latest]
    assert gateway.queries[-1]['latest_message'] == latest
    assert len(gateway.commits) == 1 and gateway.commits[0][0]['messages'] == [first, latest]
    assert companion.inbox.pending == [] and companion.inbox.inflight == []
    records = [json.loads(p.read_text(encoding='utf-8')) for p in (b.PRIVATE/'decisions').glob('*.json')]
    assert sorted(d['status'] for d in records) == ['completed', 'superseded']
    assert len(companion.history) == 2  # Only the combined conversation enters history.


def test_continuous_arrivals_have_a_bounded_number_of_draft_refreshes(config):
    config['chat_mode'] = 'group'
    class Gateway:
        def __init__(self):
            self.queries = []
        def status(self): return {}
        def flush_deliveries(self): pass
        def generate(self, query, history):
            self.queries.append(json.loads(query))
            companion.inbox.add([message(f'后续{len(self.queries)}')], len(self.queries)+1)
            return '合并后的回复'
        def confirm_delivery(self, *args, **kwargs): pass
    gateway = Gateway()
    companion = b.Companion(config, FakeDevice(), gateway, reader=lambda _: None)
    companion.collect = lambda: companion.inbox.add([message('最早')], 1)
    sent = []
    def send(text):
        sent.append(text)
        companion.stop.set()
        return {'confirmed': True, 'send_ms': 0}
    companion.send = send
    companion.run()
    assert len(gateway.queries) == b.MAX_DRAFT_REFRESHES+1
    assert sent == ['合并后的回复']
    assert gateway.queries[-1]['latest_message'] == message('后续2')
    assert [item['message'] for item in companion.inbox.pending] == [message('后续3')]
    assert not companion.inbox.inflight


def test_arrivals_after_send_begins_remain_for_the_next_turn(config):
    class Gateway:
        def status(self): return {}
        def flush_deliveries(self): pass
        def generate(self, *args): return '正在发的回复'
        def confirm_delivery(self, *args, **kwargs): pass
    companion = b.Companion(config, FakeDevice(), Gateway(), reader=lambda _: None)
    companion.collect = lambda: companion.inbox.add([message('之前')], 1)
    def send(text):
        companion.inbox.add([message('发送期间来了')], 2)
        companion.stop.set()
        return {'confirmed': True, 'send_ms': 0}
    companion.send = send
    companion.run()
    assert [item['message'] for item in companion.inbox.pending] == [message('发送期间来了')]
    assert not companion.inbox.inflight


def test_merge_never_builds_an_oversized_query_or_drops_its_pending_events(config):
    config['chat_mode'] = 'group'
    inbox = b.Inbox(config)
    # latest_message repeats the tail in the actual wire envelope. Each input
    # must fit on its own before testing rejection of their combined request.
    inbox.add([message('甲'*9000)], 1)
    original = inbox.take(timeout=0)
    assert len(b.conversation_query(original['messages'], config)) <= 20000
    inbox.add([message('乙'*9000)], 2)
    assert inbox.merge_pending(config) is None
    assert [item['event_id'] for item in inbox.inflight] == original['event_ids']
    assert len(inbox.pending) == 1 and len(inbox.inflight) == 1


def test_prompt_prioritizes_latest_message_and_requests_one_combined_reply(config):
    import httpx
    config['chat_mode'] = 'group'
    bodies = []
    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '那就去霞谷。'}}]})
    gateway = b.Gateway(config, transport=httpx.MockTransport(respond))
    messages = [message('去雨林'), message('改主意了，去霞谷')]
    try:
        assert gateway.generate(b.conversation_query(messages, config)) == '那就去霞谷。'
    finally:
        gateway.close()
    prompt = bodies[0]['messages'][0]['content']
    assert '先回应最后一条最新消息' in prompt and '只合并成一条' in prompt
    assert '新发言最高' not in prompt
    assert json.loads(bodies[0]['messages'][-1]['content'])['latest_message'] == messages[-1]


@pytest.mark.parametrize('arrival_stage', ['keyboard', 'typing', 'fresh_screen', 'one_read_before_preparing'])
def test_message_received_while_preparing_is_merged_before_enter(config, arrival_stage):
    config['chat_mode'] = 'group'
    first, latest = message('叶子大王又回来了'), message('哼哼')
    old, combined = '旧草稿不应该发出', '哼，叶子大王回来还挺神气。'
    injected, screens = False, 0
    class Gateway:
        def __init__(self): self.queries, self.commits = [], []
        def status(self): return {}
        def flush_deliveries(self): pass
        def generate(self, query, history):
            self.queries.append(json.loads(query))
            return old if len(self.queries) == 1 else combined
        def confirm_delivery(self, query, reply, **metadata): self.commits.append(reply)
    device, gateway = FakeDevice(), Gateway()
    def reader(_):
        nonlocal injected, screens
        screens += 1
        if arrival_stage == 'keyboard' and not injected:
            companion.inbox.add([latest], 2)
            injected = True
        frame = screen(history=[combined] if device.keys else [])
        if arrival_stage in ('fresh_screen', 'one_read_before_preparing'):
            frame['lines'] += [peer_line(first['text']+'-叶子', y=200), peer_line('哼哼-叶子', y=250)]
        return frame
    companion = b.Companion(config, device, gateway, reader=reader)
    companion.tracker.update([first])
    if arrival_stage == 'one_read_before_preparing':
        companion.tracker.update([first, latest])  # Seen once, as in the actual 12:25 trace.
    companion.collect = lambda: companion.inbox.add([first], 1)
    original_input = device.input_text
    def input_text(text):
        nonlocal injected
        original_input(text)
        if arrival_stage == 'typing' and not injected:
            companion.inbox.add([latest], 2)
            injected = True
    device.input_text = input_text
    original_ack = companion.inbox.acknowledge
    def finish():
        original_ack()
        companion.stop.set()
    companion.inbox.acknowledge = finish
    companion.run()
    assert len(device.keys) == 1 and gateway.commits == [combined]
    assert gateway.queries[-1]['messages'] == [first, latest]
    assert device.typed == ([combined] if arrival_stage == 'keyboard' else [old, combined])
    assert not device.draft and not companion.inbox.pending and not companion.inbox.inflight


def test_screen_order_wins_when_older_text_stabilizes_late(config):
    config['chat_mode'] = 'group'
    old, latest = message('你好'), message('又卡了吗')
    inbox = b.Inbox()
    inbox.observe_order([old, latest])
    inbox.add([latest], 1)
    inbox.add([old], 2)  # Recognition order is reversed; chat chronology is not.
    batch = inbox.take(timeout=0)
    assert batch['messages'] == [old, latest]
    assert json.loads(b.conversation_query(batch['messages'], config))['latest_message'] == latest


def test_native_clear_only_targets_the_exact_owned_draft(config):
    device = b.Device(config)
    draft, selected = ['程序刚输入的草稿'], []
    def field():
        return {'bounds': [12,828,1588,888], 'text':draft[0], 'empty':not draft[0]}
    class UI:
        def __call__(self, **selector):
            selected.append(selector)
            return self
        def clear_text(self, timeout): draft[0] = ''
    device.ui, device.input_field = UI(), field
    device.clear_owned_draft('程序刚输入的草稿')
    assert selected == [{'className':'android.widget.EditText','packageName':config['package'],
                         'focused':True,'text':'程序刚输入的草稿'}]
    draft[0] = '用户改过的文字'
    with pytest.raises(b.BridgeError, match='草稿归属已变化'):
        device.clear_owned_draft('程序刚输入的草稿')
    assert draft[0] == '用户改过的文字' and len(selected) == 1
