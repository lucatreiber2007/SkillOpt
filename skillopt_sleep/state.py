"""SkillOpt-Sleep — persistent cross-night state.

state.json lives in ~/.skillopt-sleep and is the "long-term" store that
turns nightly episodes into durable competence (the Agent-Sleep paper's
short-term -> long-term transfer). It records:

  - night counter
  - last harvest timestamp per project (so each night only sees new data)
  - cross-night "slow/meta" memory (lessons that persisted across nights)
  - per-night history (scores, accept/reject) for trend reporting
"""
from __future__ import annotations

import copy
import json
import locale
import os
import tempfile
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional


def _now_iso(clock: Optional[float] = None) -> str:
    # caller passes a timestamp; we avoid importing time at module import
    import time as _t
    return _t.strftime("%Y-%m-%dT%H:%M:%S", _t.localtime(clock if clock is not None else _t.time()))


DEFAULT_STATE: Dict[str, Any] = {
    "version": 1,
    "night": 0,
    "last_harvest": {},     # project -> iso timestamp of last harvested record
    "slow_memory": "",      # cross-night consolidated lessons (meta-skill analogue)
    "history": [],          # list of per-night summaries
    "task_archive": [],     # capped list of past mined tasks (for associative recall)
    "last_model_key": "",   # "backend::model" string used in the last successful night (F16)
    "last_model_key_format": 1,  # v1=config text; v2=resolved backend/model
}


class SleepState:
    def __init__(self, path: str, data: Optional[Dict[str, Any]] = None) -> None:
        self.path = path
        # DEFAULT_STATE contains mutable lists and dicts. A shallow copy makes
        # independent projects in one Python process share history, harvest
        # cursors, and recalled tasks until they are persisted.
        self.data = data if data is not None else copy.deepcopy(DEFAULT_STATE)

    # io ---------------------------------------------------------------------
    @staticmethod
    @contextmanager
    def lock(path: str) -> Iterator[None]:
        """Refuse overlapping writers before they read or start model work."""
        lock_path = os.path.realpath(path) + ".lock"
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        locked = False
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                except OSError as error:
                    raise RuntimeError("Sleep state is busy; retry after the active cycle finishes") from error
            else:
                import fcntl
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    raise RuntimeError("Sleep state is busy; retry after the active cycle finishes") from error
            locked = True
            yield
        finally:
            try:
                if locked:
                    if os.name == "nt":
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    @classmethod
    def load(cls, path: str) -> "SleepState":
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            return cls(path, copy.deepcopy(DEFAULT_STATE))
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as utf8_error:
            # Earlier writers used the locale codec (for example GBK or cp1252);
            # the next save() rewrites the file as UTF-8.
            codec = locale.getpreferredencoding(False)
            try:
                text = raw.decode(codec)
            except (UnicodeDecodeError, LookupError):
                raise ValueError(
                    f"{path} is neither UTF-8 nor {codec}; it was left unchanged. "
                    "Convert it to UTF-8 or move it aside to start a fresh state."
                ) from utf8_error
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Sleep state must contain a JSON object")
        merged = copy.deepcopy(DEFAULT_STATE)
        merged.update(data)
        return cls(path, merged)

    def save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        descriptor, tmp = tempfile.mkstemp(prefix=".sleep-state-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    # accessors --------------------------------------------------------------
    @property
    def night(self) -> int:
        return int(self.data.get("night", 0))

    def last_harvest_for(self, project: str) -> Optional[str]:
        return self.data.get("last_harvest", {}).get(project)

    def set_last_harvest(self, project: str, iso_ts: str) -> None:
        self.data.setdefault("last_harvest", {})[project] = iso_ts

    @property
    def slow_memory(self) -> str:
        return str(self.data.get("slow_memory", ""))

    def set_slow_memory(self, content: str) -> None:
        self.data["slow_memory"] = content

    def begin_night(self, clock: Optional[float] = None) -> int:
        self.data["night"] = self.night + 1
        return self.night

    def record_night(self, summary: Dict[str, Any]) -> None:
        self.data.setdefault("history", []).append(summary)

    # ── task archive (associative-recall memory) ──────────────────────────
    def task_archive(self) -> list:
        """Past mined tasks as plain dicts (newest last)."""
        return list(self.data.get("task_archive", []))

    def add_to_archive(self, task_dicts: list, cap: int = 300) -> None:
        """Append tonight's tasks; keep only the most recent ``cap``."""
        arc = self.data.setdefault("task_archive", [])
        arc.extend(task_dicts)
        if len(arc) > cap:
            self.data["task_archive"] = arc[-cap:]

    # ── model-swap tracking (F16) ─────────────────────────────────────────
    @property
    def last_model_key(self) -> str:
        return str(self.data.get("last_model_key", ""))

    def set_last_model_key(self, key: str) -> None:
        self.data["last_model_key"] = key
        self.data["last_model_key_format"] = 2

    @property
    def last_model_key_format(self) -> int:
        try:
            return int(self.data.get("last_model_key_format", 1))
        except (TypeError, ValueError):
            return 1
