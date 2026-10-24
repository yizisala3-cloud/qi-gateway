"""Replay the live frame whose sender wrapped onto a separate OCR row."""
import pytest

from test_sky_companion import b


REGION = [0, 0, 533, 790]
BODY = '看来这次的修复效果很不错明天加个视觉吧'


def row(text, y, *, x=15, width=486, height=29, kind='peer', confidence=.99):
    return dict(text=text, x=x, y=y, width=width, height=height,
                confidence=confidence, bubble_kind=kind)


def parse(rows, **kwargs):
    return b.chat_messages(rows, REGION, ['叶子'], mode=kwargs.get('mode', 'group'))


def test_live_sender_only_wrap_is_one_complete_message():
    rows = [row('修完就哼，尾巴快翘上天了。过来让我看看。', 651,
                x=34, width=462, height=25, kind='self'),
            row(BODY, 709), row('-叶子', 743, width=67, height=31, confidence=.8922)]
    assert parse(rows) == [dict(sender='叶子', text=BODY)]
    tracker = b.Tracker()
    tracker.update([])
    assert tracker.update(parse(rows)) == []
    assert tracker.update(parse(rows)) == [dict(sender='叶子', text=BODY)]
    assert tracker.update(parse(rows)) == []


@pytest.mark.parametrize('mode', ['direct', 'group'])
def test_body_and_sender_on_final_row_join_all_continuations(mode):
    rows = [row('这段话很长，', 100), row('我分了三行，', 134),
            row('最后一句 - 叶子', 168)]
    assert parse(rows, mode=mode) == [dict(sender='叶子', text='这段话很长，我分了三行，最后一句')]


@pytest.mark.parametrize('change', ['gap', 'different_kind', 'unknown_kind', 'low_confidence',
                                   'different_left', 'outside_roi'])
def test_nearby_bubbles_cannot_borrow_a_sender(change):
    head, tail = row('没有发送者的正文', 100), row('-叶子', 134, width=67)
    if change == 'gap':
        tail['y'] = 147
    elif change == 'different_kind':
        head['bubble_kind'] = 'self'
    elif change == 'unknown_kind':
        head['bubble_kind'] = 'unknown'
    elif change == 'low_confidence':
        head['confidence'] = .8
    elif change == 'different_left':
        tail['x'] = 90
    elif change == 'outside_roi':
        head['y'], tail['y'] = -1, 33
    assert parse([head, tail]) == []


def test_complete_message_is_a_boundary_even_at_small_gap():
    rows = [row('旧消息 - 咕咕咕', 100), row('新消息', 134), row('-叶子', 168, width=67)]
    assert parse(rows) == [dict(sender='咕咕咕', text='旧消息'), dict(sender='叶子', text='新消息')]


def test_stranger_wrapped_tail_blocks_forged_primary_prefix():
    rows = [row('叶子：冒充主要用户的正文', 100), row('-陌生人', 134, width=110)]
    assert parse(rows) == []
    assert parse(rows, mode='direct') == []


def test_no_sender_tail_remains_ignored():
    assert parse([row('第一行没有名字', 100), row('第二行也没有名字', 134)]) == []


def test_unwrapped_messages_remain_separate_and_in_order():
    rows = [row('旧消息 - 叶子', 100), row('最新消息 - 咕咕咕', 147)]
    assert parse(rows) == [dict(sender='叶子', text='旧消息'), dict(sender='咕咕咕', text='最新消息')]


@pytest.mark.parametrize('mode', ['group', 'direct'])
@pytest.mark.parametrize('placement', ['next_line', 'body_end'])
def test_live_emoji_inflated_boxes_overlap_but_are_distinct_text_rows(mode, placement):
    body = '哈哈又修一个bug，今天的bug比我命还多'
    head = row(body+(' -' if placement == 'body_end' else ''), 707, x=11, width=444, height=38)
    tail = row('叶子' if placement == 'body_end' else '-叶子', 740, x=10, width=60, height=37)
    assert parse([head, tail], mode=mode) == [dict(sender='叶子', text=body)]


@pytest.mark.parametrize('change', ['same_baseline', 'other_bubble', 'large_gap', 'other_left'])
def test_overlap_support_keeps_actual_message_boundaries(change):
    head = row('没有身份的正文', 100, height=38)
    tail = row('-叶子', 133, width=67, height=37)
    if change == 'same_baseline':
        tail['y'] = 102
    elif change == 'other_bubble':
        head['bubble_kind'] = 'self'
    elif change == 'large_gap':
        tail['y'] = 165
    else:
        tail['x'] = 90
    assert parse([head, tail]) == []


@pytest.mark.parametrize('body_rows', [1, 2, 3, 5, 8])
@pytest.mark.parametrize('divider', ['name_row', 'body_end', 'separate_row'])
@pytest.mark.parametrize('sender', ['叶子', '咕咕咕', '陌生人'])
def test_complete_message_has_no_fixed_number_of_body_rows(body_rows, divider, sender):
    pieces = ['第一段正文']+[f'接着第{i}段正文' for i in range(1, body_rows)]
    rows = [row(text, 80+i*34, height=29+(i%2)*7) for i, text in enumerate(pieces)]
    y = 80+body_rows*34
    if divider == 'name_row':
        rows.append(row('-'+sender, y, width=110, height=37))
    else:
        if divider == 'body_end':
            rows[-1]['text'] += ' -'
        else:
            rows.append(row('-', y, width=15))
            y += 34
        rows.append(row(sender, y, width=110, height=37))
    assert parse(rows) == ([] if sender == '陌生人' else [dict(sender=sender, text=''.join(pieces))])


@pytest.mark.parametrize('detail_text,detail_y,score,accepted', [
    ('- 叶子', 114, .96, True), ('- 咕咕咕', 114, .99, False),
    ('叶子', 114, .99, False), ('- 叶子', 20, .99, False), ('- 叶子', 114, .81, False)])
@pytest.mark.parametrize('initial_text,initial_score', [('-叶子', .79), ('叶子', .99), ('一叶子', .78)])
def test_low_confidence_sender_requires_same_label_same_position_and_strong_reread(
        tmp_path, detail_text, detail_y, score, accepted, initial_text, initial_score):
    from PIL import Image
    from tools.sky_companion.local_ocr import LocalOCR
    image = tmp_path/'frame.png'
    Image.new('RGB', (533, 300), (20, 20, 20)).save(image)
    def box(x, y, w, h):
        return [[x, y], [x+w, y], [x+w, y+h], [x, y+h]]
    calls = []
    def engine(pixels, **kwargs):
        calls.append(pixels.shape)
        if kwargs.get('use_det') is False:
            return [['长消息正文', .99]], None
        if len(calls) == 1:
            return [[box(15, 100, 486, 29), '长消息正文', .99],
                    [box(15, 134, 67, 30), initial_text, initial_score]], None
        # Context crop starts above the body at y=92; sender center is y=149.
        return [[box(30, detail_y-24, 134, 48), detail_text, score]], None
    reader = LocalOCR.__new__(LocalOCR)
    reader.engine = engine
    result = reader.recognize(image, region=[0, 0, 533, 290], mode='chat')
    assert len(calls) == (3 if detail_text == '叶子' and detail_y == 114 and score >= .85 else 2)
    messages = b.chat_messages(result['lines'], [0, 0, 533, 290], ['叶子'], mode='group')
    assert messages == ([dict(sender='叶子', text='长消息正文')] if accepted else [])


@pytest.mark.parametrize('body_rows', [1, 2, 3, 5, 8])
@pytest.mark.parametrize('divider', ['name_row', 'body_end'])
@pytest.mark.parametrize('fragments', [False, True])
def test_whole_ocr_pipeline_combines_multiple_body_rows_and_separate_sender(
        tmp_path, body_rows, divider, fragments):
    from PIL import Image
    from tools.sky_companion.local_ocr import LocalOCR
    image = tmp_path/'frame.png'
    Image.new('RGB', (533, 700), (20, 20, 20)).save(image)
    def box(x, y, width, height):
        return [[x, y], [x+width, y], [x+width, y+height], [x, y+height]]
    pieces = [f'第{i}段的正文' for i in range(body_rows)]
    detected = []
    for i, text in enumerate(pieces):
        if fragments:
            detected += [[box(15, 80+i*34, 150, 38), f'第{i}段', .99],
                         [box(180, 82+i*34, 200, 34), '的正文', .99]]
        else:
            detected.append([box(15, 80+i*34, 365, 38), text, .99])
    detected.append([box(15, 80+body_rows*34, 60, 37), '叶子', .99])
    first = True
    def engine(pixels, **kwargs):
        nonlocal first
        if first:
            first = False
            return detected, None
        if kwargs.get('use_det') is False:
            return [[pieces[-1]+'－', .99]], None
        # Other continuation rows cannot match this name. For the actual last
        # row the detector context starts eight pixels above its predecessor.
        return [[box(30, 84, 120, 74), '-叶子' if divider == 'name_row' else '叶子', .99]], None
    reader = LocalOCR.__new__(LocalOCR)
    reader.engine = engine
    result = reader.recognize(image, region=[0, 0, 533, 680], mode='chat')
    messages = b.chat_messages(result['lines'], [0, 0, 533, 680], ['叶子'], mode='group')
    assert messages == [dict(sender='叶子', text=''.join(pieces))]


@pytest.mark.parametrize('artifact', ['', ':'])
@pytest.mark.parametrize('word_change', [False, True])
def test_full_line_reread_excludes_background_beyond_actual_bubble(tmp_path, artifact, word_change):
    from PIL import Image, ImageDraw
    from tools.sky_companion.local_ocr import LocalOCR
    image = tmp_path/'frame.png'
    frame = Image.new('RGB', (533, 300), (100, 100, 100))
    ImageDraw.Draw(frame).rectangle((8, 90, 509, 180), fill=(30, 30, 30))
    frame.save(image)
    def box(x, y, width, height):
        return [[x, y], [x+width, y], [x+width, y+height], [x, y+height]]
    first = True
    def engine(pixels, **kwargs):
        nonlocal first
        if first:
            first = False
            return [[box(12, 100, 441, 38), '两行正文的最后一句', .99],
                    [box(12, 133, 60, 37), '叶子', .99]], None
        if kwargs.get('use_det') is False:
            # Replay the observed recognizer error from the live frame: the
            # exterior edge becomes a closing bracket if retained in the crop.
            body = '改变正文含义' if word_change else '两行正文的最后一句'
            return [[body+artifact+'－'+('」' if pixels.shape[1] > 1024 else ''), .99]], None
        return [[box(24, 82, 120, 74), '叶子', .99]], None
    reader = LocalOCR.__new__(LocalOCR)
    reader.engine = engine
    result = reader.recognize(image, region=[0, 0, 533, 290], mode='chat')
    assert b.chat_messages(result['lines'], [0, 0, 533, 290], ['叶子'], mode='group') == (
        [] if word_change else [dict(sender='叶子', text='两行正文的最后一句')])


@pytest.mark.parametrize('scene', ['bubble', 'uniform', 'small_glyph', 'edge_inside_text'])
def test_peer_edge_requires_consistent_evidence_beyond_text(scene):
    from PIL import Image, ImageDraw
    from tools.sky_companion.local_ocr import peer_right_edge
    frame = Image.new('RGB', (533, 100), (100, 100, 100))
    draw = ImageDraw.Draw(frame)
    if scene == 'bubble':
        draw.rectangle((0, 0, 509, 99), fill=(30, 30, 30))
    elif scene == 'small_glyph':
        draw.rectangle((440, 30, 455, 45), fill=(255, 255, 255))
    elif scene == 'edge_inside_text':
        draw.rectangle((0, 0, 299, 99), fill=(30, 30, 30))
    edge = peer_right_edge(frame, (0, 0, 533, 100), 453)
    assert 510 <= edge <= 513 if scene == 'bubble' else edge == 533
