"""Durable dispatch receipts shared by Bridge adapters.

A committed claim precedes every side effect. Reusing a request ID only reads
its receipt; an uncertain operation is never replayed after a restart. Callers
persist hashes and safe result metadata, not prompts or attachment contents.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from typing import Any, Optional


REQUEST_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")


def delivery_identity(scope: str, payload: dict[str, Any]) -> tuple[str, str]:
    request_id = payload.get("requestId")
    if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
        raise ValueError("invalid_delivery_id")
    if not isinstance(payload.get("receiptOnly", False), bool):
        raise ValueError("invalid_receipt_only")
    message = payload.get("message")
    attachments = payload.get("attachmentIds", [])
    if (not isinstance(message, str) or len(message.strip()) > 20_000
            or "\0" in message or not isinstance(attachments, list)
            or len(attachments) > 4
            or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", value)
                   for value in attachments)
            or (not message.strip() and not attachments)):
        raise ValueError("invalid_delivery_message")
    content = [message.strip(), attachments]
    operation = [scope, content]
    if scope == "create":
        operation.append({key: payload.get(key) for key in
                          ("projectId", "model", "effort", "serviceTier")})
    def digest(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                         separators=(",", ":")).encode()).hexdigest()
    return digest(operation), digest(content)


def user_receipt_ids(thread: dict[str, Any], thread_id: str,
                     message: Optional[str] = None) -> set[str]:
    """Stable item identities; reconciliation requires an exact text message.

    Attachments intentionally need an observed successful dispatch response.
    A new attachment-only message must not be mistaken for an unrelated send.
    """
    if not isinstance(thread, dict) or thread.get("id") != thread_id:
        return set()
    expected = None
    if message is not None:
        expected = {message, message + "\n"}
        if "\\" not in message and "\r" not in message:
            serialized = message.replace("_", "\\_")
            expected.update((serialized, serialized + "\n"))
            if "\n" in message:
                hard_breaks = serialized.replace("\n", "\\\n")
                expected.update((hard_breaks, hard_breaks + "\n"))
    receipts: set[str] = set()
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return receipts
    for turn in turns:
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"]:
            continue
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if (not isinstance(item, dict) or item.get("type") != "userMessage"
                    or not isinstance(item.get("id"), str) or not item["id"]):
                continue
            if expected is not None:
                content = item.get("content")
                if not isinstance(content, list) or not content or any(
                    not isinstance(part, dict) or part.get("type") != "text"
                    or not isinstance(part.get("text"), str) for part in content
                ):
                    continue
                if "\n".join(part["text"] for part in content) not in expected:
                    continue
            receipts.add(json.dumps([turn["id"], item["id"]], separators=(",", ":")))
    return receipts


class DeliveryJournal:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for entry in (path.parent, path, Path(str(path) + "-journal"),
                      Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            if entry.exists() or entry.is_symlink():
                info = entry.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("delivery_reparse_point_refused")
        os.chmod(path.parent, 0o700)
        with closing(self.connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS deliveries (
                owner TEXT NOT NULL, request_id TEXT NOT NULL,
                scope TEXT NOT NULL, fingerprint TEXT NOT NULL, content_hash TEXT NOT NULL,
                thread_id TEXT, state TEXT NOT NULL, phase TEXT NOT NULL,
                baseline TEXT, status INTEGER, result TEXT, updated REAL NOT NULL,
                PRIMARY KEY (owner, request_id))""")
            db.execute("CREATE INDEX IF NOT EXISTS deliveries_pending ON deliveries (thread_id, state)")
        os.chmod(path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    @staticmethod
    def _record(row, *, dispatch: bool = False) -> dict[str, Any]:
        result = dict(row)
        result["dispatch"] = dispatch
        result["baseline"] = None if row["baseline"] is None else set(json.loads(row["baseline"]))
        result["result"] = None if row["result"] is None else json.loads(row["result"])
        return result

    def claim(self, owner: str, scope: str, payload: dict[str, Any],
              *, receipt_only: bool = False) -> Optional[dict[str, Any]]:
        fingerprint, content_hash = delivery_identity(scope, payload)
        request_id = payload["requestId"]
        with closing(self.connect()) as db, db:
            # A receipt-only lookup never allocates, even when the first POST
            # was lost before it reached this process.
            if not receipt_only:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM deliveries WHERE owner=? AND request_id=?",
                             (owner, request_id)).fetchone()
            if row:
                if row["phase"] == "abandoned_unseen":
                    # The original POST never supplied a payload to this DB.
                    # A local operator tombstone permanently reserves its ID,
                    # including against a delayed original HTTP request.
                    if row["scope"] != scope:
                        raise ValueError("delivery_id_reused_with_different_content")
                    return self._record(row)
                same_create_receipt = (row["scope"] == "create" and scope == row["thread_id"]
                                       and row["content_hash"] == content_hash)
                if row["fingerprint"] != fingerprint and not same_create_receipt:
                    raise ValueError("delivery_id_reused_with_different_content")
                return self._record(row)
            if receipt_only:
                return None
            if scope == "create":
                pending = db.execute("SELECT 1 FROM deliveries WHERE owner=? AND scope='create' "
                                     "AND state IN ('claimed','uncertain') LIMIT 1", (owner,)).fetchone()
            else:
                pending = db.execute("SELECT 1 FROM deliveries WHERE thread_id=? "
                                     "AND state IN ('claimed','uncertain') LIMIT 1", (scope,)).fetchone()
            if pending:
                raise ValueError("delivery_confirmation_pending")
            db.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?,'claimed','preflight',NULL,NULL,NULL,?)",
                       (owner, request_id, scope, fingerprint, content_hash,
                        None if scope == "create" else scope, time.time()))
            row = db.execute("SELECT * FROM deliveries WHERE owner=? AND request_id=?",
                             (owner, request_id)).fetchone()
            return self._record(row, dispatch=True)

    def get(self, owner: str, request_id: str) -> dict[str, Any]:
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM deliveries WHERE owner=? AND request_id=?",
                             (owner, request_id)).fetchone()
        if row is None:
            raise ValueError("unknown_delivery_id")
        return self._record(row)

    def advance(self, owner: str, request_id: str, phase: str, *,
                thread_id: Optional[str] = None, baseline: Optional[set[str]] = None) -> None:
        if phase not in {"creating", "created", "dispatching", "background_dispatching"}:
            raise ValueError("invalid_delivery_phase")
        with closing(self.connect()) as db, db:
            db.execute("UPDATE deliveries SET phase=?, thread_id=COALESCE(?,thread_id), "
                       "baseline=COALESCE(?,baseline), updated=? WHERE owner=? AND request_id=? "
                       "AND state='claimed'",
                       (phase, thread_id, None if baseline is None else json.dumps(sorted(baseline)),
                        time.time(), owner, request_id))

    def finish(self, owner: str, request_id: str, state: str, status: int,
               result: dict[str, Any]) -> dict[str, Any]:
        if state not in {"confirmed", "refused", "uncertain"}:
            raise ValueError("invalid_delivery_state")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT state FROM deliveries WHERE owner=? AND request_id=?",
                                 (owner, request_id)).fetchone()
            if current is not None and current["state"] == "uncertain" and state == "refused":
                raise ValueError("uncertain_delivery_requires_operator_resolution")
            # A simultaneous receipt poll may already have confirmed the send.
            # A late timeout must never downgrade that durable confirmation.
            db.execute("UPDATE deliveries SET state=?,status=?,result=?,updated=? "
                       "WHERE owner=? AND request_id=? AND state IN ('claimed','uncertain')",
                       (state, status, json.dumps(result, ensure_ascii=True, separators=(",", ":")),
                        time.time(), owner, request_id))
            row = db.execute("SELECT * FROM deliveries WHERE owner=? AND request_id=?",
                             (owner, request_id)).fetchone()
            if row is None:
                raise ValueError("unknown_delivery_id")
            return self._record(row)

    def abandon_unseen(self, owner: str, request_id: str, scope: str) -> dict[str, Any]:
        """Locally abandon a missing ID after stopping Bridge and checking Desktop.

        Unlike receiptOnly, this is an explicit operator mutation. It needs no
        prompt and cannot overwrite an existing claim: use resolve_pending for
        that. The permanent refused record also rejects a delayed first POST.
        """
        if (not isinstance(owner, str) or not owner or len(owner) > 256
                or any(ord(char) < 32 for char in owner)
                or not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id)):
            raise ValueError("invalid_delivery_id")
        if (not isinstance(scope, str) or not scope or scope.strip() != scope or len(scope) > 128
                or "/" in scope or "\\" in scope or any(ord(char) < 32 for char in scope)):
            raise ValueError("invalid_delivery_scope")
        result = {"ok": False, "error": "delivery_manually_refused", "retryAllowed": True}
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT 1 FROM deliveries WHERE owner=? AND request_id=?",
                                  (owner, request_id)).fetchone()
            if existing is not None:
                raise ValueError("delivery_already_exists")
            db.execute("INSERT INTO deliveries VALUES (?,?,?,'','',?,'refused','abandoned_unseen',"
                       "NULL,409,?,?)",
                       (owner, request_id, scope, None if scope == "create" else scope,
                        json.dumps(result, separators=(",", ":")), time.time()))
            row = db.execute("SELECT * FROM deliveries WHERE owner=? AND request_id=?",
                             (owner, request_id)).fetchone()
            return self._record(row)

    def resolve_pending(self, owner: str, request_id: str, outcome: str, *,
                        thread_id: Optional[str] = None) -> dict[str, Any]:
        """Local operator recovery, only after stopping Bridge and checking Desktop.

        ``refused`` means the operator abandons this attempt; it is not an
        automatic assertion that no side effect occurred. The old ID remains
        terminal and can never dispatch again. No remote route calls this.
        """
        if (not isinstance(owner, str) or not owner
                or not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id)):
            raise ValueError("invalid_delivery_id")
        if outcome not in {"confirmed", "refused"}:
            raise ValueError("invalid_delivery_resolution")
        if thread_id is not None and (
            not isinstance(thread_id, str) or not thread_id or len(thread_id) > 128
            or "/" in thread_id or "\\" in thread_id or any(ord(char) < 32 for char in thread_id)
        ):
            raise ValueError("invalid_thread_id")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM deliveries WHERE owner=? AND request_id=?",
                             (owner, request_id)).fetchone()
            if row is None:
                raise ValueError("unknown_delivery_id")
            if row["state"] not in {"claimed", "uncertain"}:
                raise ValueError("delivery_already_resolved")
            if row["thread_id"] and thread_id and row["thread_id"] != thread_id:
                raise ValueError("delivery_thread_identity_mismatch")
            resolved_thread = row["thread_id"] or thread_id
            if outcome == "confirmed" and not resolved_thread:
                raise ValueError("confirmed_delivery_requires_thread_id")
            if outcome == "confirmed":
                mode = "background" if row["phase"] == "background_dispatching" else "desktop"
                result: dict[str, Any] = {"ok": True, "mode": mode, "threadId": resolved_thread}
                if mode == "desktop":
                    result["desktop"] = {"ok": True, "confirmedBy": "operator"}
                else:
                    result["run"] = {"threadId": resolved_thread}
                status = 202
            else:
                result = {"ok": False, "error": "delivery_manually_refused", "retryAllowed": True}
                status = 409
            if row["scope"] == "create" and resolved_thread:
                result.update(threadCreated=True, threadId=resolved_thread)
            db.execute("UPDATE deliveries SET state=?,thread_id=?,status=?,result=?,updated=? "
                       "WHERE owner=? AND request_id=?",
                       (outcome, resolved_thread, status, json.dumps(result, separators=(",", ":")),
                        time.time(), owner, request_id))
            resolved = db.execute("SELECT * FROM deliveries WHERE owner=? AND request_id=?",
                                  (owner, request_id)).fetchone()
            return self._record(resolved)
