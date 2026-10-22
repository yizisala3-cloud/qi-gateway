"""A private, file-backed sticker catalogue. IDs never become arbitrary URLs/paths."""
from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path

log = logging.getLogger('gateway.qqbot.stickers')

class StickerCatalog:
    MAX_BYTES = 8 * 1024 * 1024

    def __init__(self, manifest=""):
        self.entries = {}
        self.root = None
        if not manifest:
            return
        try:
            path = Path(manifest).resolve(strict=True)
            if path.stat().st_size > 262144:
                raise ValueError("表情目录过大")
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            log.warning('QQ表情目录不可读，使用文字服务 type=%s',type(exc).__name__,exc_info=exc)
            return
        if not isinstance(data,dict) or data.get("version") != 1 or not isinstance(data.get("stickers"),list):
            raise ValueError("表情目录格式错误")
        if len(data["stickers"]) > 200:
            raise ValueError("单个表情库最多200项")
        self.root = path.parent
        seen = set()
        for entry in data["stickers"]:
            if not isinstance(entry,dict):
                raise ValueError("表情条目必须是对象")
            key, description = entry.get("id"), entry.get("description")
            if not isinstance(key,str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}",key) or key in seen:
                raise ValueError("表情编号无效或重复")
            seen.add(key)
            if not isinstance(description,str) or not 1 <= len(description.strip()) <= 200:
                raise ValueError("表情必须有1至200字符的含义说明")
            tags = entry.get("tags",[])
            if not isinstance(tags,list) or len(tags)>12 or any(not isinstance(t,str) or len(t)>40 for t in tags):
                raise ValueError("表情标签格式错误")
            file = entry.get("file")
            if not isinstance(file,str) or not file or Path(file).is_absolute():
                raise ValueError("表情文件必须是目录内的相对路径")
            self.entries[key] = {"id":key,"description":description.strip(),"tags":tags,"file":file}
            try:
                self._read(key)
            except OSError as exc:
                del self.entries[key]
                log.warning('QQ跳过不可读表情 id=%s type=%s',key,type(exc).__name__,exc_info=exc)

    def _read(self, key):
        path = (self.root / self.entries[key]["file"]).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("表情文件越出素材目录")
        if path.stat().st_size > self.MAX_BYTES:
            raise ValueError("表情文件超过8MiB")
        data = path.read_bytes()
        valid = (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")
                 or data.startswith((b"GIF87a",b"GIF89a"))
                 or (data[:4]==b"RIFF" and data[8:12]==b"WEBP"))
        if not valid or len(data)>self.MAX_BYTES:
            raise ValueError("表情仅支持PNG/JPEG/GIF/WebP")
        return data

    def choices(self):
        return [{k:v for k,v in e.items() if k!="file"} for e in self.entries.values()]

    def segment(self, key):
        entry = self.entries[key]
        return {"type":"image","data":{"file":"base64://"+base64.b64encode(self._read(key)).decode("ascii"),
                 "sub_type":1,"summary":"[表情包] "+entry["description"]}}

    def summary(self, key):
        return f"[表情包 {key}：{self.entries[key]['description']}]"
