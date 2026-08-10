from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    """返回当前 UTC ISO 时间字符串。"""
    return datetime.now(timezone.utc).isoformat()


def stable_hash(value: Any) -> str:
    """计算稳定的短哈希值。"""
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def ensure_dir(path: str | Path) -> Path:
    """确保目录存在并返回 Path 对象。"""
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def atomic_write_json(path: str | Path, data: Any) -> None:
    """以原子替换方式写入 UTF-8 JSON 文件。"""
    target = Path(path)
    ensure_dir(target.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)


SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*['\"]?[^'\"\s]+"),
]


def redact_text(text: str) -> str:
    """脱敏文本中的密钥和令牌片段。"""
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    redacted = re.sub(r"[A-Za-z]:\\(?:[^\\\s]+\\){2,}[^\\\s]+", "[LOCAL_PATH]", redacted)
    redacted = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", redacted)
    redacted = re.sub(r"\b1[3-9]\d{9}\b", "[PHONE]", redacted)
    return redacted


def is_relative_to(path: str | Path, root: str | Path) -> bool:
    """判断路径是否位于指定根目录内。"""
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False
