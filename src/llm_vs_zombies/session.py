"""Append-only client audit records, separate from native simulation events."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SessionTrace:
    """Flush each JSONL record before returning; refuse concurrent writers.

    A stale ``.lock`` after a crashed process must be inspected and removed by
    the operator. Incomplete JSONL tails are rejected, never silently repaired.
    ``durable=True`` adds fsync; ordinary flush is not a power-loss guarantee.
    """

    def __init__(self, path: str | Path, *, durable: bool = False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        self._mutex = threading.RLock()
        self._durable = durable
        self._closed = False
        self._lock_fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(self._lock_fd, str(os.getpid()).encode("ascii"))
            self._seq = 0
            if self.path.exists():
                with self.path.open("rb") as source:
                    for line in source:
                        if not line.endswith(b"\n"):
                            raise ValueError("session trace has an incomplete tail")
                        record = json.loads(line)
                        if record.get("schema") != 1 or record.get("seq") != self._seq:
                            raise ValueError("session trace schema or sequence mismatch")
                        self._seq += 1
            self._file = self.path.open("a", encoding="utf-8", newline="\n")
        except BaseException:
            os.close(self._lock_fd)
            self._lock_path.unlink()
            raise

    def emit(self, kind: str, data: Any) -> dict[str, Any]:
        with self._mutex:
            if self._closed:
                raise ValueError("session trace is closed")
            record = {"schema": 1, "seq": self._seq,
                      "recorded_at": datetime.now(timezone.utc).isoformat(),
                      "kind": kind, "data": data}
            # Serialize before writing so unserializable values cannot corrupt a line.
            encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            self._file.write(encoded + "\n")
            self._file.flush()
            if self._durable:
                os.fsync(self._file.fileno())
            self._seq += 1
            return json.loads(encoded)

    def close(self) -> None:
        with self._mutex:
            if not self._closed:
                try:
                    self._file.close()
                finally:
                    os.close(self._lock_fd)
                    self._lock_path.unlink()
                    self._closed = True

    def __enter__(self) -> SessionTrace:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
