"""Small on-disk store for what the assistant does on its own.

Holds which calls it has already followed up, which threads it has drafted
replies for, actions waiting for the rep's approval, and the updates (briefs,
post-call follow-ups) it has produced. Everything lives under var/, which is
not committed. A file lock keeps the scheduled job and the web app from
overwriting each other.
"""

from __future__ import annotations

import fcntl
import json
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import ROOT

VAR = ROOT / "var"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, directory: Path = VAR) -> None:
        self.dir = Path(directory)
        self.updates_dir = self.dir / "updates"
        self.updates_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = self.dir / "state.json"
        self._lock_file = self.dir / ".lock"

    # -- state -------------------------------------------------------------
    @contextmanager
    def _locked(self):
        with self._lock_file.open("w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _read(self) -> dict:
        if not self._state_file.exists():
            return {}
        try:
            return json.loads(self._state_file.read_text())
        except json.JSONDecodeError:
            return {}

    def read(self) -> dict:
        with self._locked():
            return self._read()

    def change(self, fn: Callable[[dict], object]):
        """Read-modify-write under the lock. `fn` mutates the state dict; its return value is passed through."""
        with self._locked():
            state = self._read()
            result = fn(state)
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=1))
            tmp.replace(self._state_file)
            return result

    # -- drafted threads (so the same nudge is not drafted every morning) ---
    def record_draft(self, thread_id: str) -> None:
        self.change(lambda s: s.setdefault("drafted_threads", {}).__setitem__(thread_id, _now()))

    def drafted_threads(self) -> dict[str, str]:
        return self.read().get("drafted_threads", {})

    # -- actions waiting for approval --------------------------------------
    def queue_action(self, kind: str, preview: str, action: dict, source: str) -> str:
        item = {"id": uuid.uuid4().hex[:12], "kind": kind, "preview": preview, "action": action, "source": source, "created": _now()}
        self.change(lambda s: s.setdefault("pending", []).append(item))
        return item["id"]

    def pending(self) -> list[dict]:
        return self.read().get("pending", [])

    def take_pending(self, action_id: str) -> dict | None:
        """Remove and return one pending action (so it can only be decided once)."""

        def take(state: dict):
            items = state.get("pending", [])
            for i, item in enumerate(items):
                if item["id"] == action_id:
                    return items.pop(i)
            return None

        return self.change(take)

    # -- updates -----------------------------------------------------------
    def add_update(self, kind: str, title: str, body: str, **meta) -> dict:
        update = {"id": f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}", "kind": kind, "title": title,
                  "created": _now(), "read": False, "body": body, **meta}
        (self.updates_dir / f"{update['id']}.json").write_text(json.dumps(update, indent=1))
        return update

    def list_updates(self, limit: int = 30) -> list[dict]:
        out = []
        for path in sorted(self.updates_dir.glob("*.json"), reverse=True)[:limit]:
            try:
                item = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            item.pop("body", None)
            out.append(item)
        return out

    def get_update(self, update_id: str, mark_read: bool = False) -> dict | None:
        path = self.updates_dir / f"{Path(update_id).name}.json"
        if not path.exists():
            return None
        item = json.loads(path.read_text())
        if mark_read and not item.get("read"):
            item["read"] = True
            path.write_text(json.dumps(item, indent=1))
        return item
