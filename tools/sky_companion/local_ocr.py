"""Persistent local OCR worker. Screenshots and bundled models stay on this PC."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import struct
import time
import math
import re

from PIL import Image

if __package__:
    from .layout import merge_rows, peer_continues, join_text
else:
    from layout import merge_rows, peer_continues, join_text

def raw_size(data):
    if len(data) < 16:
        raise ValueError("Android原始截图头部不完整")
    width, height, pixel_format, _ = struct.unpack("<IIII", data[:16])
    if not 0 < width <= 8192 or not 0 < height <= 8192 or pixel_format not in (1, 2):
        raise ValueError("Android原始截图尺寸或像素格式不受支持")
    if len(data) != 16 + width * height * 4:
        raise ValueError("Android原始截图数据长度不匹配")
    return width, height


def open_frame(path: Path):
    if path.suffix == ".rgba":
        data = path.read_bytes()
        size = raw_size(data)
        return Image.frombytes("RGBA", size, data[16:]).convert("RGB")
    with Image.open(path) as source:
        return source.convert("RGB")


def bubble_kind(source, bounds):
    """Sky renders self text on a light bubble, peers on a dark bubble."""
    import numpy as np
    x1, y1, x2, y2 = bounds
    patch = source.crop((max(0, math.floor(x1)), max(0, math.floor(y1)),
                         min(source.width, math.ceil(x2)), min(source.height, math.ceil(y2))))
    pixels = np.asarray(patch)
    if pixels.size == 0:
        return "unknown"
    background = float(np.median(pixels.mean(axis=2)))
    return "peer" if background < 120 else "self" if background >= 150 else "unknown"


def peer_right_edge(source, bounds, content_right):
    """Find a consistent dark-bubble edge beyond all recognized text.

    A whole-line recognizer must include small punctuation beyond its detector
    box, while excluding the game scene outside the bubble. Require an upward
    luminance step across most of the bubble's height. Without visual evidence,
    retain the calibrated ROI instead of inventing a narrower boundary.
    """
    import numpy as np
    left, top, right, bottom = bounds
    pixels = np.asarray(source.crop(bounds)).mean(axis=2)
    best = None
    for x in range(max(3, math.ceil(content_right-left)+1), pixels.shape[1]-3):
        change = np.median(pixels[:, x:x+3], axis=1)-np.median(pixels[:, x-3:x], axis=1)
        consistent = float(np.mean(change >= 12))
        if consistent >= .6:
            evidence = (float(np.median(change)), consistent)
            if best is None or evidence > best[0]:
                best = (evidence, x)
    return min(right, left+best[1]+2) if best else right


class LocalOCR:
    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR
        # Limit detector upscaling on a small chat/editor crop. Native OCR models
        # are loaded once per worker, instead of once for every screenshot.
        # Single-thread sessions avoid three model pools spinning between
        # screenshots. The measured chat crop still fits the 1-second poll.
        self.engine = RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1,
                               det_limit_type="max", det_limit_side_len=960)

    def recognize(self, path: Path, *, region=None, mode="all", save_png=False):
        import numpy as np
        if mode not in ("all", "chat", "editor", "proof"):
            raise ValueError("无效OCR模式")
        started = time.perf_counter()
        lines = []
        with open_frame(path) as source:
            width, height = source.size
            if save_png:
                source.save(path.with_name("screen.png"))
            jobs = []
            if mode != "editor":
                bounds = region or [0, 0, width, height-100]
                jobs.append((bounds, 1))
            if mode in ("all", "editor"):
                jobs.append(([12, height-60, width-12, height-12], 2))
            for bounds, scale in jobs:
                x1, y1, x2, y2 = bounds
                if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
                    raise ValueError("OCR区域超出截图")
                crop = source.crop(tuple(bounds))
                if scale != 1:
                    crop = crop.resize((crop.width*scale, crop.height*scale), Image.Resampling.LANCZOS)
                result, _ = self.engine(np.asarray(crop), use_cls=False)
                for box, text, score in sorted(result or [], key=lambda item: min(p[1] for p in item[0])):
                    left, top = min(p[0] for p in box)/scale + x1, min(p[1] for p in box)/scale + y1
                    right, bottom = max(p[0] for p in box)/scale + x1, max(p[1] for p in box)/scale + y1
                    kind = bubble_kind(source, (left, top, right, bottom))
                    # A detector may split the preceding body around an emoji.
                    # Rebuild that row before finding its continuation. Weak
                    # fragments stay separate and cannot lower a strong body's
                    # confidence or supply an unverified sender identity.
                    strong = merge_rows([item for item in lines if item["confidence"] >= .85])
                    weak = [item for item in lines if item["confidence"] < .85]
                    lines = sorted(strong+weak, key=lambda item: (item["y"], item["x"]))
                    previous = next((item for item in reversed(lines) if item["confidence"] >= .85), None)
                    current = dict(x=left, y=top, height=bottom-top, bubble_kind=kind)
                    wrapped_tail = (peer_continues(previous, current)
                                    and not re.fullmatch(r".+[-－—–·]\s*[^\r\n:：]{1,24}", previous["text"]))
                    label_pattern = r"\s*[-－—–·]\s*[^\r\n:：]{1,24}"
                    if (mode != "editor" and kind == "peer" and len(text.strip()) <= 25
                            and (wrapped_tail or (score < .85 and re.fullmatch(label_pattern, text)))):
                        # A wrapped sender-only row can lose confidence when
                        # detached from its body, or miss its separator. Re-read at 2x with
                        # the preceding bubble for detector context. Accept only
                        # the same complete label at the same physical position;
                        # never lower the threshold or guess a configured name.
                        cx1, cy1 = x1, max(y1, math.floor(top)-math.ceil(2*(bottom-top)))
                        cx2, cy2 = x2, min(y2, math.ceil(bottom)+8)
                        if wrapped_tail:
                            cy1 = max(y1, math.floor(previous["y"])-8)
                            cx2 = peer_right_edge(source, (cx1, cy1, x2, cy2),
                                                 max(right, previous["x"]+previous["width"]))
                        detail = source.crop((cx1, cy1, cx2, cy2))
                        # Keep the bright chat glyphs and remove the animated
                        # background that can hide a short leading separator.
                        glyphs = np.asarray(detail).min(axis=2) > 180
                        detail = Image.fromarray(glyphs.astype(np.uint8)*255).convert("RGB")
                        detail = detail.resize((detail.width*2, detail.height*2), Image.Resampling.BICUBIC)
                        refined, _ = self.engine(np.asarray(detail), use_cls=False)
                        matches, bare_matches = [], []
                        for refined_box, refined_text, refined_score in refined or []:
                            ry = sum(p[1] for p in refined_box)/len(refined_box)/2+cy1
                            rx1 = min(p[0] for p in refined_box)/2+cx1
                            rx2 = max(p[0] for p in refined_box)/2+cx1
                            overlap = max(0, min(right, rx2)-max(left, rx1))
                            def label_key(value, *, uncertain=False):
                                separators = r"[-－—–·一]" if uncertain else r"[-－—–·]"
                                return re.sub(r"\s+", "", re.sub(r"^\s*"+separators, "", value))
                            if (refined_score >= .85
                                    and label_key(refined_text) == label_key(text, uncertain=score < .85)
                                    and abs(ry-(top+bottom)/2) <= (bottom-top)/2
                                    and overlap >= .5*min(right-left, rx2-rx1)):
                                if re.fullmatch(label_pattern, refined_text):
                                    matches.append((refined_text, refined_score))
                                elif re.fullmatch(r"[^\r\n:：\s－—–·-]{1,24}", refined_text):
                                    bare_matches.append((refined_text, refined_score))
                        if len(matches) == 1:
                            text, score = matches[0]
                            if wrapped_tail:
                                # Use the same clean read for its body too. Mixing
                                # raw/background-confused text with a clean label
                                # otherwise makes one wrapped bubble change IDs.
                                body = []
                                for rb, rt, rs in refined or []:
                                    ry = sum(p[1] for p in rb)/len(rb)/2+cy1
                                    if abs(ry-previous["y"]-previous["height"]/2) <= previous["height"]/2:
                                        body.append((min(p[0] for p in rb)/2+cx1,
                                                     max(p[0] for p in rb)/2+cx1, rt, rs))
                                if (body and min(part[3] for part in body) >= .85
                                        and max(part[1] for part in body)-min(part[0] for part in body)
                                            >= .8*previous["width"]):
                                    previous.update(**join_text([{'text':part[2]} for part in sorted(body)]),
                                                    confidence=min(part[3] for part in body))
                        elif wrapped_tail and len(bare_matches) == 1:
                            # The divider may be at the END of the preceding
                            # body row, beyond an emoji and outside its detector
                            # box. Recognize that full-width line without another
                            # detector pass, which otherwise discards punctuation.
                            split = (previous["y"]+previous["height"]/2+(top+bottom)/2)/2
                            row_top = max(y1, math.floor(previous["y"])-3)
                            row_bottom = min(y2, math.floor(split))
                            complete = source.crop((x1, row_top, cx2, row_bottom))
                            complete = complete.resize((complete.width*2, complete.height*2), Image.Resampling.BICUBIC)
                            recognized, _ = self.engine(np.asarray(complete), use_det=False, use_cls=False)
                            if recognized and len(recognized) == 1 and recognized[0][1] >= .85:
                                divider = re.fullmatch(r"(.*)([-－—–·])\s*", recognized[0][0])
                                words = lambda value: "".join(c for c in value if c.isalnum())
                                if (divider and words(previous["text"])
                                        and words(divider[1]) == words(previous["text"])):
                                    # This pass proves the small divider; retain
                                    # the detected body rather than importing
                                    # punctuation hallucinated beside an emoji.
                                    trimmed = previous['text'].rstrip()
                                    joined = join_text([{'text':trimmed, 'synthetic_spaces':[
                                        index for index in previous.get('synthetic_spaces', ()) if index < len(trimmed)]},
                                        {'text':divider[2]}])
                                    previous.update(**joined,
                                                    confidence=min(previous["confidence"], float(recognized[0][1])))
                                    text, score = bare_matches[0]
                    if mode == "proof" and kind == "self":
                        # Full-screen detector downscaling confused 叶/吐 in a
                        # short, already-sent bubble. Re-read its native crop at
                        # 2x, without relaxing short-text character matching.
                        cx1, cy1 = max(x1, math.floor(left)-12), max(y1, math.floor(top)-8)
                        cx2, cy2 = min(x2, math.ceil(right)+12), min(y2, math.ceil(bottom)+12)
                        detail = source.crop((cx1, cy1, cx2, cy2))
                        detail = detail.resize((detail.width*2, detail.height*2), Image.Resampling.LANCZOS)
                        refined, _ = self.engine(np.asarray(detail), use_cls=False)
                        if refined and len(refined) == 1 and refined[0][2] >= 0.85:
                            _, text, score = refined[0]
                    lines.append({"text": text, "confidence": float(score), "x": left,
                                  "y": top, "width": right-left, "height": bottom-top,
                                  "bubble_kind": kind})
        return {"width": width, "height": height, "lines": sorted(lines, key=lambda v: (v["y"], v["x"])),
                "ocr_ms": round((time.perf_counter()-started)*1000, 1)}


def serve():
    engine = LocalOCR()
    sys.stdout.write('{"ready":true}\n')
    sys.stdout.flush()
    for request in sys.stdin:
        data = json.loads(request)
        result = engine.recognize(Path(data["path"]), region=data.get("region"),
                                  mode=data.get("mode", "all"), save_png=data.get("save_png", False))
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Local RapidOCR worker")
    parser.add_argument("image", type=Path, nargs="?")
    parser.add_argument("--server", action="store_true")
    args = parser.parse_args()
    # JSON is the worker protocol; the parent logs failures and worker stderr.
    if args.server:
        serve()
    elif args.image:
        sys.stdout.write(json.dumps(LocalOCR().recognize(args.image, mode="proof"), ensure_ascii=False) + "\n")
    else:
        parser.error("请提供截图路径或 --server")
