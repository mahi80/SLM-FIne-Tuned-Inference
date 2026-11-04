"""Hash-chained audit log.

Every record commits to the one before it:

    hash = sha256(canonical_json(record without "hash"))

so removing, reordering or editing a record breaks the chain from that point
on. This is tamper *evidence*, not tamper prevention: a writer with filesystem
access can rewrite the whole chain. Ship the head hash to a separate system
(syslog, object-lock bucket) if the requirement is tamper resistance.

One process owns the file. The gateway runs a single uvicorn worker, and the
lock below serialises concurrent requests inside it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64

logger = logging.getLogger("slm.audit")


def _digest(record: dict) -> str:
    canonical = {key: value for key, value in record.items() if key != "hash"}
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _walk(lines: list[str]) -> tuple[int, str, str | None]:
    previous = GENESIS
    expected = 1
    for number, line in enumerate(lines, 1):
        if not line.strip():
            return expected - 1, previous, f"line {number}: blank line"
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            return expected - 1, previous, f"line {number}: invalid JSON ({exc.msg})"
        if not isinstance(record, dict):
            return expected - 1, previous, f"line {number}: record is not an object"
        if record.get("seq") != expected:
            return expected - 1, previous, f"line {number}: seq {record.get('seq')} != {expected}"
        if record.get("prev") != previous:
            return expected - 1, previous, f"line {number}: prev does not match previous hash"
        if record.get("hash") != _digest(record):
            return expected - 1, previous, f"line {number}: hash mismatch"
        previous = record["hash"]
        expected += 1
    return expected - 1, previous, None


class AuditLog:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0
        self._prev = GENESIS
        self._recover()
        self._file = self.path.open("a", encoding="utf-8", buffering=1)

    def _recover(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        raw = self.path.read_bytes()
        end = raw.rfind(b"\n")
        if end == -1:
            logger.warning("audit log %s: dropping incomplete first record", self.path)
            with self.path.open("r+b") as handle:
                handle.truncate(0)
            return
        if end != len(raw) - 1:
            # A crash between write() and the trailing newline can leave a torn
            # tail. Drop only that tail; anything earlier must still verify.
            logger.warning("audit log %s: dropping torn tail of %d bytes", self.path, len(raw) - end - 1)
            with self.path.open("r+b") as handle:
                handle.truncate(end + 1)
            raw = raw[: end + 1]
        lines = raw.decode("utf-8").splitlines()
        count, last, error = _walk(lines)
        if error is not None:
            raise RuntimeError(f"audit chain broken: {error}")
        self._seq = count
        self._prev = last
        if count:
            logger.info("audit log %s: recovered %d records", self.path, count)

    def append(self, payload: dict) -> dict:
        reserved = {"seq", "ts", "prev", "hash"} & payload.keys()
        if reserved:
            raise ValueError(f"audit payload uses reserved keys: {sorted(reserved)}")
        with self._lock:
            record = {"seq": self._seq + 1, "ts": _utc_now(), "prev": self._prev, **payload}
            record["hash"] = _digest(record)
            line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
            size = os.fstat(self._file.fileno()).st_size
            try:
                self._file.write(line)
                self._file.flush()
                os.fsync(self._file.fileno())
            except OSError:
                # Leave no half-written record behind; the caller decides
                # whether a request may proceed without an audit entry.
                os.ftruncate(self._file.fileno(), size)
                raise
            self._seq = record["seq"]
            self._prev = record["hash"]
            return record

    def verify(self) -> dict:
        with self._lock:
            self._file.flush()
            if not self.path.exists():
                if self._seq:
                    return {"ok": False, "records": 0, "error": "audit file is missing"}
                return {"ok": True, "records": 0, "last_seq": 0, "last_hash": GENESIS}
            lines = self.path.read_text(encoding="utf-8").splitlines()
            count, last, error = _walk(lines)
            if error is not None:
                return {"ok": False, "records": count, "error": error}
            if count != self._seq or last != self._prev:
                return {
                    "ok": False,
                    "records": count,
                    "error": (
                        f"file head (seq {count}) does not match in-memory head "
                        f"(seq {self._seq})"
                    ),
                }
            return {"ok": True, "records": count, "last_seq": count, "last_hash": last}

    def close(self) -> None:
        with self._lock:
            self._file.close()
