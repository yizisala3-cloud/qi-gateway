"""Bounded, occasional game screenshots; never a background polling model."""
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import base64
import time

from PIL import Image

if __package__:
    from .local_ocr import open_frame
else:
    from local_ocr import open_frame


class VisionRequested(Exception):
    """A text-only decision requests one current screenshot, never game text."""


@dataclass
class VisionReply:
    reply: str | None
    scene: str


@dataclass
class VisionFrame:
    data_url: str = field(repr=False)
    captured_at: float
    width: int
    height: int
    byte_count: int
    digest: str

    def metadata(self):
        return dict(captured_at=self.captured_at, width=self.width, height=self.height,
                    byte_count=self.byte_count, sha256=self.digest)


def encode_frame(path: Path, expected_size, output: Path, *, captured_at=None):
    with open_frame(path) as source:
        if list(source.size) != list(expected_size):
            raise ValueError('视觉截图分辨率与设备配置不匹配')
        source.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
        buffer = BytesIO()
        source.save(buffer, format='JPEG', quality=75, optimize=True)
        data = buffer.getvalue()
        if len(data) > 500_000:
            raise ValueError('视觉截图压缩后超过500KB，未上传')
        output.write_bytes(data)
        return VisionFrame('data:image/jpeg;base64,'+base64.b64encode(data).decode('ascii'),
                           time.time() if captured_at is None else captured_at,
                           source.width, source.height, len(data), sha256(data).hexdigest())


class VisionState:
    def __init__(self, config):
        if not isinstance(config, dict):
            raise ValueError('vision 必须为配置对象')
        self.enabled = config.get('enabled', False)
        self.every_turns = config.get('every_turns', 4)
        self.max_age = config.get('max_age_seconds', 60)
        if type(self.enabled) is not bool:
            raise ValueError('vision.enabled 必须为布尔值')
        if type(self.every_turns) is not int or not 1 <= self.every_turns <= 20:
            raise ValueError('vision.every_turns 须为1至20的整数')
        if type(self.max_age) is not int or not 10 <= self.max_age <= 600:
            raise ValueError('vision.max_age_seconds 须为10至600的整数')
        self.summary = ''
        self.observed_at = 0
        self.turns = 0
        self.retry_after = 0

    def available(self, now):
        return self.enabled and now >= self.retry_after

    def due(self, now):
        return self.available(now) and (not self.summary or self.turns >= self.every_turns
                                       or now-self.observed_at >= self.max_age)

    def context(self, now):
        return dict(status='available' if self.available(now) else 'temporarily_unavailable',
                    summary=self.summary, observed_at=self.observed_at or None,
                    age_seconds=round(max(0, now-self.observed_at), 1) if self.observed_at else None,
                    stale=not self.summary or now-self.observed_at >= self.max_age)

    def accept(self, frame, result):
        self.summary, self.observed_at = result.scene, frame.captured_at
        self.turns = 0
        self.retry_after = 0

    def failed(self, now):
        # No second image request in this decision. Text remains usable, and
        # later decisions wait a minute before trying the failed vision path.
        self.retry_after = now+60

    def complete(self):
        if self.enabled:
            self.turns += 1
