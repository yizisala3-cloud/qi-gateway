"""Vision cadence, control messages and text/confirmed-delivery boundaries."""
import base64
from io import BytesIO
import json
import time

import httpx
from PIL import Image
import pytest

from test_sky_companion import b, config, private_files, FakeDevice
from tools.sky_companion import vision as v


def frame():
    return v.VisionFrame('data:image/jpeg;base64,test-image', time.time(), 1280, 720, 30, 'test-digest')


def vision_config(config):
    return {**config, 'vision': {'enabled': True, 'every_turns': 4, 'max_age_seconds': 60}}


def test_snapshot_is_resized_jpeg_and_metadata_never_contains_inline_image(tmp_path):
    source = tmp_path/'source.png'
    Image.new('RGB', (2560, 1440), 'blue').save(source)
    result = v.encode_frame(source, [2560, 1440], tmp_path/'last.jpg', captured_at=100)
    data = base64.b64decode(result.data_url.split(',', 1)[1])
    with Image.open(BytesIO(data)) as image:
        assert image.format == 'JPEG' and image.size == (1280, 720)
    assert result.byte_count == len(data) < 500_000
    assert result.captured_at == 100 and len(result.digest) == 64
    assert 'data:' not in json.dumps(result.metadata())
    assert 'base64' not in repr(result)
    with pytest.raises(ValueError, match='分辨率'):
        v.encode_frame(source, [1600, 900], tmp_path/'wrong.jpg')
    assert not (tmp_path/'wrong.jpg').exists()


@pytest.mark.parametrize('settings', [None, [], {'enabled':'yes'}, {'every_turns':True},
    {'every_turns':0}, {'every_turns':21}, {'max_age_seconds':9}, {'max_age_seconds':601}])
def test_invalid_vision_configuration_fails_before_start(settings):
    with pytest.raises(ValueError):
        v.VisionState(settings)


def test_periodic_updates_count_completed_turns_and_expiry():
    state = v.VisionState({'enabled':True})
    assert state.due(100)
    image = frame(); image.captured_at = 100
    state.accept(image, v.VisionReply('看到了', '草地上有三位玩家'))
    for _ in range(4):
        assert not state.due(110)
        state.complete()  # Reply and PASS both finish one incoming turn.
    assert state.due(110)
    state.accept(image, v.VisionReply(None, '草地上有三位玩家'))
    assert state.due(161) and state.context(161)['stale']
    assert state.context(161)['age_seconds'] == 61
    state.failed(161)
    assert not state.available(200) and not state.due(200)
    assert state.context(200)['status'] == 'temporarily_unavailable'
    assert state.due(221)
    assert not v.VisionState({}).due(1_000_000)


def gateway_with_response(config, content):
    requests = []
    def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices':[{'message':{'content':content}}]})
    return b.Gateway(config, transport=httpx.MockTransport(transport)), requests


@pytest.mark.parametrize('reply,expected', [('看见你的蓝披风了。','看见你的蓝披风了。'), ('PASS', None)])
def test_image_and_player_text_share_one_preview_but_history_stays_text(config, reply, expected):
    gateway, requests = gateway_with_response(config, json.dumps({'scene':'草地，蓝披风玩家在左边','reply':reply}))
    history = [{'role':'user','content':'旧消息'},{'role':'assistant','content':'知道了'}]
    try:
        result = gateway.generate('叶子：我穿什么？', history, image=frame(), visual_context={'status':'available'})
    finally:
        gateway.close()
    assert result == v.VisionReply(expected, '草地，蓝披风玩家在左边')
    messages = requests[0]['messages']
    assert messages[1:3] == history
    assert messages[-1]['content'][0] == {'type':'text','text':'叶子：我穿什么？'}
    assert messages[-1]['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,')
    assert sum('image_url' in json.dumps(message) for message in messages) == 1
    assert requests[0]['response_format'] == {'type':'json_object'}
    assert requests[0]['thinking'] == {'type':'enabled'}
    assert requests[0]['reasoning_effort'] == 'low' and requests[0]['max_tokens'] == 2048
    assert '具体可见事实' in messages[0]['content']
    assert '绝不能单独输出PASS' in messages[0]['content']
    assert '才输出控制词PASS' not in messages[0]['content']


def test_model_can_request_look_only_when_enabled(config):
    gateway, requests = gateway_with_response(config, 'LOOK')
    try:
        with pytest.raises(v.VisionRequested):
            gateway.generate('我换了衣服，看看？', visual_context={'summary':'旧画面','stale':True}, allow_look=True)
        with pytest.raises(b.BridgeError, match='控制词'):
            gateway.generate('继续', visual_context={'status':'temporarily_unavailable'}, allow_look=False)
    finally:
        gateway.close()
    assert all(isinstance(request['messages'][-1]['content'], str) for request in requests)
    assert all(request['thinking'] == {'type':'disabled'} for request in requests)
    assert all('response_format' not in request for request in requests)


def test_ordinary_text_without_vision_preserves_existing_protocol(config):
    gateway, requests = gateway_with_response(config, '在呢')
    try:
        assert gateway.generate('在吗') == '在呢'
    finally:
        gateway.close()
    assert 'thinking' not in requests[0] and 'response_format' not in requests[0]
    assert '才输出控制词PASS' in requests[0]['messages'][0]['content']


@pytest.mark.parametrize('content,finish_reason', [('', 'stop'), (None,'stop'),
    ('```json\n{"scene":"草地","reply":"嗯"}\n```','stop'),
    ('{"scene":"草地","reply":"嗯"}', 'length')])
def test_empty_fenced_and_truncated_image_answer_cannot_become_observation(config, content, finish_reason, caplog):
    def transport(_):
        return httpx.Response(200, json={'choices':[{'finish_reason':finish_reason,
            'message':{'content':content,'reasoning_content':'private reasoning'}}],
            'usage':{'completion_tokens':1024}})
    gateway = b.Gateway(config, transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(b.BridgeError):
            gateway.generate('看见什么', image=frame())
    finally:
        gateway.close()
    assert 'metadata=' in caplog.text and 'completion_tokens' in caplog.text
    assert 'private reasoning' not in caplog.text


@pytest.mark.parametrize('content', ['不是JSON', '{}', '[]', '{"scene":"", "reply":"好"}',
    '{"scene":"天空", "reply":null}', '{"scene":"天空", "reply":"LOOK"}',
    json.dumps({'scene':'天空', 'reply':'太长'*30}), json.dumps({'scene':'天空', 'reply':'第一行\n第二行'}),
    json.dumps({'scene':'x'*801, 'reply':'好'}), json.dumps({'scene':'天空','reply':'好','other':'隐藏内容'})])
def test_bad_visual_protocol_is_never_game_text(config, content):
    gateway, _ = gateway_with_response(config, content)
    try:
        with pytest.raises(b.BridgeError):
            gateway.generate('看一下', image=frame())
    finally:
        gateway.close()


def test_four_turns_reuse_scene_without_uploading_each_time(config, monkeypatch):
    class Gateway:
        calls = []
        def generate(self, query, history, **kwargs):
            self.calls.append(kwargs)
            return v.VisionReply('嗯', '草地上三个玩家') if kwargs.get('image') else '嗯'
    gateway = Gateway()
    companion = b.Companion(vision_config(config), FakeDevice(), gateway, reader=lambda _:None)
    captures = []
    monkeypatch.setattr(companion, 'capture_visual', lambda: captures.append(frame()) or captures[-1])
    for _ in range(9):
        assert companion.generate_reply('叶子：在吗') == '嗯'
        companion.vision.complete()
    assert len(captures) == 3
    assert [index for index, call in enumerate(gateway.calls) if call.get('image')] == [0,4,8]
    assert 'data:' not in json.dumps([call['visual_context'] for call in gateway.calls])


def test_model_requested_capture_is_bounded_and_keeps_natural_reply(config, monkeypatch):
    class Gateway:
        calls = []
        def generate(self, query, history, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get('image'):
                return v.VisionReply('看到了，你换蓝披风了。', '玩家刚换成蓝色披风')
            raise v.VisionRequested()
    gateway = Gateway()
    companion = b.Companion(vision_config(config), FakeDevice(), gateway, reader=lambda _:None)
    companion.vision.accept(frame(), v.VisionReply(None, '旧装扮'))
    monkeypatch.setattr(companion, 'capture_visual', frame)
    assert companion.generate_reply('叶子：看我新衣服') == '看到了，你换蓝披风了。'
    assert len(gateway.calls) == 2 and gateway.calls[0]['allow_look']
    saved = json.loads((b.PRIVATE/'last_vision.json').read_text(encoding='utf8'))
    assert saved['reason'] == 'model_requested' and saved['summary'] == '玩家刚换成蓝色披风'
    assert 'base64' not in json.dumps(saved)


def test_vision_failure_keeps_text_and_backoff_does_not_retry_image(config, monkeypatch):
    class Gateway:
        calls = []
        def generate(self, query, history, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get('image'):
                raise b.BridgeError('视觉服务503')
            assert not kwargs['allow_look']
            assert kwargs['visual_context']['status'] == 'temporarily_unavailable'
            return '现在看不到，文字还在。'
    gateway = Gateway()
    companion = b.Companion(vision_config(config), FakeDevice(), gateway, reader=lambda _:None)
    captures = []
    monkeypatch.setattr(companion, 'capture_visual', lambda: captures.append(frame()) or captures[-1])
    assert companion.generate_reply('看我') == '现在看不到，文字还在。'
    assert companion.generate_reply('还能说话吗') == '现在看不到，文字还在。'
    assert len(captures) == 1 and len(gateway.calls) == 3
    assert json.loads((b.PRIVATE/'last_vision.json').read_text(encoding='utf8'))['status'] == 'failed'


def test_device_guard_failure_is_not_treated_as_recoverable_vision_api_failure(config, monkeypatch):
    companion = b.Companion(vision_config(config), FakeDevice(), None, reader=lambda _:None)
    def wrong_device():
        raise b.BridgeError('指定游戏不在前台')
    monkeypatch.setattr(companion, 'capture_visual', wrong_device)
    with pytest.raises(b.BridgeError, match='不在前台'):
        companion.generate_reply('看我')
    assert not (b.PRIVATE/'last_vision.json').exists()


@pytest.mark.parametrize('reply', ['你在草地上。', None])
def test_vision_run_never_persists_control_json_or_images_as_game_chat(config, monkeypatch, reply):
    class Gateway:
        confirmed = []
        def status(self): return {}
        def flush_deliveries(self): pass
        def generate(self, query, history, **kwargs):
            assert kwargs.get('image')
            return v.VisionReply(reply, '云和草地')
        def confirm_delivery(self, query, text, **kwargs):
            self.confirmed.append((query,text))
    gateway = Gateway()
    companion = b.Companion(vision_config(config), FakeDevice(), gateway, reader=lambda _:None)
    monkeypatch.setattr(companion, 'capture_visual', frame)
    monkeypatch.setattr(companion, 'collect', lambda:None)
    companion.inbox.add([{'sender':'叶子','text':'看看我在哪'}], time.monotonic())
    original_acknowledge = companion.inbox.acknowledge
    def acknowledge():
        original_acknowledge(); companion.stop.set()
    monkeypatch.setattr(companion.inbox, 'acknowledge', acknowledge)
    sent = []
    monkeypatch.setattr(companion, 'send', lambda text: sent.append(text) or {'sent':True,'confirmed':True})
    companion.run()
    assert sent == ([] if reply is None else [reply])
    assert len(gateway.confirmed) == (0 if reply is None else 1)
    assert companion.vision.turns == 1
    assert 'base64' not in json.dumps(companion.history)
    assert 'scene' not in json.dumps(gateway.confirmed)
