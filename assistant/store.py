"""Small JSON files under data/: chat history, seen message ids, runtime state."""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def ensure_private_dir(path: os.PathLike) -> Path:
    """Create the folder if needed and make it accessible only to the owner (0700)."""
    folder = Path(path)
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    return folder


class JsonFile:
    """A JSON document written atomically (temp file 0600, then os.replace)."""

    def __init__(self, path: os.PathLike, default: Any) -> None:
        self.path = Path(path)
        self.default = default

    def read(self) -> Any:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return copy.deepcopy(self.default)
        except (OSError, ValueError) as exc:
            log.warning("Could not read %s (%s); starting with an empty value", self.path, exc)
            return copy.deepcopy(self.default)

    def write(self, data: Any) -> None:
        ensure_private_dir(self.path.parent)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            os.chmod(tmp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise


class HistoryStore:
    """Recent messages, kept separately for every business connection and chat."""

    def __init__(self, path: os.PathLike, limit: int) -> None:
        self.file = JsonFile(path, {})
        self.limit = limit
        data = self.file.read()
        self.data: Dict[str, List[Dict[str, Any]]] = data if isinstance(data, dict) else {}

    @staticmethod
    def key(connection_id: str, chat_id: int) -> str:
        return f"{connection_id}:{chat_id}"

    def get(self, connection_id: str, chat_id: int) -> List[Dict[str, Any]]:
        return [dict(item) for item in self.data.get(self.key(connection_id, chat_id), [])]

    def add(
        self,
        connection_id: str,
        chat_id: int,
        *,
        role: str,
        name: str,
        text: str,
        lang: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> None:
        items = self.data.setdefault(self.key(connection_id, chat_id), [])
        items.append(
            {"role": role, "name": name, "text": text, "lang": lang, "ts": time.time() if ts is None else ts}
        )
        del items[: max(0, len(items) - self.limit)]
        self.file.write(self.data)

    def last_language(self, connection_id: str, chat_id: int) -> Optional[str]:
        for item in reversed(self.data.get(self.key(connection_id, chat_id), [])):
            if item.get("role") == "other" and item.get("lang"):
                return item["lang"]
        return None

    def chat_count(self) -> int:
        return len(self.data)


class SeenMessages:
    """Remembers recent message keys on disk so duplicates are caught after a restart too."""

    def __init__(self, path: os.PathLike, capacity: int = 5000) -> None:
        self.file = JsonFile(path, [])
        self.capacity = capacity
        stored = self.file.read()
        self.keys: "OrderedDict[str, None]" = OrderedDict(
            (str(key), None) for key in (stored if isinstance(stored, list) else [])
        )

    def first_time(self, key: str) -> bool:
        if key in self.keys:
            return False
        self.keys[key] = None
        while len(self.keys) > self.capacity:
            self.keys.popitem(last=False)
        self.file.write(list(self.keys))
        return True


class RuntimeState:
    """Pause flag and usage-limit deadline, kept across restarts."""

    def __init__(self, path: os.PathLike) -> None:
        self.file = JsonFile(path, {})
        data = self.file.read()
        self.data: Dict[str, Any] = data if isinstance(data, dict) else {}

    @property
    def paused(self) -> bool:
        return bool(self.data.get("paused", False))

    @paused.setter
    def paused(self, value: bool) -> None:
        self.data["paused"] = bool(value)
        self.file.write(self.data)

    @property
    def limit_until(self) -> Optional[float]:
        value = self.data.get("limit_until")
        return float(value) if isinstance(value, (int, float)) else None

    @limit_until.setter
    def limit_until(self, value: Optional[float]) -> None:
        self.data["limit_until"] = value
        self.file.write(self.data)
