"""Shared geometry for OCR and message assembly; independent of line count."""
import re


def sender_suffix(text, *, synthetic_spaces=(), separator=None):
    """Use a visible label divider, never a punctuation mark inside a name.

    Spaces around a dash are stronger evidence than an internal middle dot.
    Without spacing, accept one unambiguous dash (or one dot); an ambiguous
    sequence cannot safely grant another player's identity or reply priority.
    """
    if any(type(index) is not int or not 0 <= index < len(text) or not text[index].isspace()
           for index in synthetic_spaces):
        return None
    artificial = set(synthetic_spaces)
    marks = list(re.finditer(r"[-－—–·]", text))
    if separator is not None:
        if type(separator) is not int or not 0 <= separator < len(text) or text[separator] not in '-－—–·':
            return None
        body, name = text[:separator].strip(), text[separator+1:].strip()
        return (body, name) if body and re.fullmatch(r"[^\r\n:：]{1,24}", name) else None
    def space(index):
        return 0 <= index < len(text) and text[index].isspace() and index not in artificial
    spaced = [mark for mark in marks if
              space(mark.start()-1) or space(mark.end())]
    dashes = [mark for mark in marks if mark[0] != '·']
    spaced_dashes = [mark for mark in spaced if mark[0] != '·']
    complete = [mark for mark in spaced_dashes if space(mark.start()-1) and space(mark.end())]
    candidates = complete or spaced_dashes or spaced
    if len(candidates) > 1:
        return None
    divider = candidates[0] if candidates else (dashes[0] if len(dashes) == 1 else
                                                  marks[0] if len(marks) == 1 else None)
    if divider is None:
        return None
    body, name = text[:divider.start()].strip(), text[divider.end():].strip()
    if not body or not re.fullmatch(r"[^\r\n:：]{1,24}", name):
        return None
    return body, name


def join_text(parts):
    """Keep character provenance when layout assembly inserts whitespace."""
    text, spaces, dividers = '', [], []
    for part in parts:
        if text:
            spaces.append(len(text))
            text += ' '
        offset = len(text)
        text += part['text']
        spaces.extend(offset+index for index in part.get('synthetic_spaces', ()))
        divider = part.get('sender_separator')
        if divider is not None:
            dividers.append(offset+divider if divider >= 0 else -1)
    return dict(text=text, synthetic_spaces=spaces,
                sender_separator=dividers[0] if len(dividers) == 1 else -1 if dividers else None)


def merge_rows(lines):
    """Assemble same-baseline fragments before reasoning about vertical rows."""
    rows = []
    for line in sorted(lines, key=lambda value: value['x']):
        target = None
        for row in rows:
            centers = abs(line['y']+line['height']/2-row['y']-row['height']/2)
            gap = line['x']-row['x']-row['width']
            if (line.get('bubble_kind') == row.get('bubble_kind')
                    and centers <= min(line['height'], row['height'])*.45 and 0 <= gap <= 50):
                target = row
                break
        if target is None:
            rows.append(dict(line))
        else:
            bottom = max(target['y']+target['height'], line['y']+line['height'])
            target.update(**join_text([target, line]),
                          width=line['x']+line['width']-target['x'], y=min(target['y'], line['y']))
            target['height'] = bottom-target['y']
    return sorted(rows, key=lambda value: (value['y'], value['x']))


def peer_continues(previous, current):
    if previous is None or previous.get('bubble_kind') != 'peer' or current.get('bubble_kind') != 'peer':
        return False
    first, second = previous['height'], current['height']
    small = min(first, second)
    if small <= 0 or abs(previous['x']-current['x']) > .5*small:
        return False
    # Emoji and bold sender glyphs inflate OCR boxes, which may overlap even
    # though their text baselines differ. Compare centers instead of requiring
    # a positive gap. Same-baseline fragments and normal inter-bubble gaps fail.
    distance = current['y']+second/2-previous['y']-first/2
    return .65*small <= distance <= (first+second)/2+.4*small
