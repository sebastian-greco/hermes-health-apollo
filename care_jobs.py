"""Durable native-child job lifecycle stored in Apollo's scoped SQLite DB.

This module deliberately owns no model runner. Hermes supplies the child run
and native cancellation callback; this module persists its source, scope,
delegation identity, bounded completion and replay/cancellation state.
"""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import re
import secrets

from .care_store import AuthorizationError, ConflictError, CareError


_JOB_ID = re.compile(r"[a-f0-9]{32}\Z")
_NATIVE_ID = re.compile(r"[A-Za-z0-9._:-]{1,256}\Z")
_OUTCOMES = {"completed", "awaiting_user", "failed", "unknown"}
_SUMMARY_LIMIT = 4000
_RESULT_LIMIT = 128 * 1024
_CONTEXT_LIMIT = 4000


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _time(runtime):
    value = runtime.clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise CareError("Job clock must return an aware datetime")
    return value.astimezone(UTC).isoformat()


def _ensure_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS care_jobs (
            job_id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            source_request_id TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            source_actor_id TEXT NOT NULL,
            source_platform TEXT NOT NULL,
            source_conversation_id TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            source_session_key TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN
              ('queued','running','awaiting_user','completed','cancel_requested','cancelled','failed','unknown')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            launch_id TEXT,
            native_delegation_id TEXT,
            child_session_id TEXT,
            attempt INTEGER NOT NULL DEFAULT 0,
            cancel_requested_at TEXT,
            finished_at TEXT,
            outcome TEXT,
            summary TEXT,
            result_json TEXT,
            completion_fingerprint TEXT,
            UNIQUE(scope_id, source_request_id)
        );
        CREATE INDEX IF NOT EXISTS idx_care_jobs_scope_state
          ON care_jobs(scope_id, state, updated_at);
        CREATE TABLE IF NOT EXISTS care_job_messages (
            job_id TEXT NOT NULL REFERENCES care_jobs(job_id),
            sequence INTEGER NOT NULL,
            source_request_id TEXT NOT NULL,
            session_key TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user','assistant')),
            created_at TEXT NOT NULL,
            PRIMARY KEY(job_id, sequence),
            UNIQUE(job_id, source_request_id)
        );
        CREATE TABLE IF NOT EXISTS care_job_events (
            job_id TEXT NOT NULL REFERENCES care_jobs(job_id),
            sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            question TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY(job_id, sequence)
        );
    """)


class CareJobs:
    """Lifecycle capability bound to the host's existing CareRuntime."""

    def __init__(self, runtime):
        self.runtime = runtime

    def _session(self, origin, permission="read"):
        # This resolves the current identity from the configured native binding;
        # no patient/scope argument is accepted by this API.
        self.runtime._binding(origin)
        session = self.runtime.session(origin)
        session.require(permission)
        return session

    def _ensure_schema(self, session):
        # Initialize additively via the trusted host, before opening a scoped
        # reader/writer transaction. Never open a second DB connection while
        # holding the SQLite write lock.
        with self.runtime._host_connection(session) as conn:
            _ensure_schema(conn)

    def _read(self, session, callback):
        self._ensure_schema(session)
        with session.connection(permission="read") as conn:
            return callback(conn)

    def _write(self, session, callback, *, permission="read"):
        self._ensure_schema(session)
        # Revalidate the exact requested capability immediately before every
        # host-owned write; a prior _session check can become stale.
        session.require(permission)
        with self.runtime._host_connection(session) as conn:
            return callback(conn)

    def _job(self, conn, session, job_id):
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise CareError("Invalid health job reference")
        row = conn.execute("SELECT * FROM care_jobs WHERE job_id=? AND scope_id=?",
                           (job_id, session.authority.scope_id)).fetchone()
        if row is None:
            raise CareError("Health job unavailable")
        return row

    def _authorize_source(self, row):
        """Require the original event's exact current route grant as well."""
        source = self.runtime.admitted(platform=row["source_platform"],
            actor_id=row["source_actor_id"], conversation_id=row["source_conversation_id"],
            message_id=row["source_message_id"], session_key=row["source_session_key"])
        if source.request_id != row["source_request_id"]:
            raise AuthorizationError("Health job source receipt changed")
        binding = self.runtime._binding(source)
        if binding.scope_id != row["scope_id"]:
            raise AuthorizationError("Health job source scope changed")
        if "read" not in binding.permissions:
            raise AuthorizationError("Original health job grant no longer allows reading")
        return source

    @staticmethod
    def _public(row, *, include_result=False):
        result = {
            "job_id": row["job_id"], "state": row["state"], "status": row["state"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "attempt": row["attempt"],
        }
        if row["summary"] is not None:
            result["summary"] = row["summary"]
        if row["outcome"] is not None:
            result["outcome"] = row["outcome"]
        if row["finished_at"] is not None:
            result["finished_at"] = row["finished_at"]
        if include_result and row["result_json"]:
            result["result"] = json.loads(row["result_json"])
        return result

    def admit(self, origin):
        """Persist a selected message and create one durable queued child job."""
        binding = self.runtime._binding(origin)
        session = self._session(origin, "read")
        self.runtime._persist_admission(origin)
        source_hash = hashlib.sha256(_json(self.runtime._source_content(origin)).encode()).hexdigest()
        now = _time(self.runtime)

        def create(conn):
            existing = conn.execute("SELECT * FROM care_jobs WHERE scope_id=? AND source_request_id=?",
                (binding.scope_id, origin.request_id)).fetchone()
            if existing:
                if existing["source_hash"] != source_hash:
                    raise ConflictError("Health job source changed")
                return {**self._public(existing), "duplicate": True}
            job_id = secrets.token_hex(16)
            conn.execute("""INSERT INTO care_jobs(job_id,scope_id,source_request_id,source_hash,
                source_actor_id,source_platform,source_conversation_id,source_message_id,
                source_session_key,state,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?)""",
                (job_id, binding.scope_id, origin.request_id, source_hash, origin.actor_id,
                 origin.platform, origin.conversation_id, origin.message_id, origin.session_key, now, now))
            conn.execute("INSERT INTO care_job_messages VALUES(?,?,?,?,'user',?)",
                         (job_id, 1, origin.request_id, origin.session_key, now))
            conn.execute("INSERT INTO care_job_events VALUES(?,?, 'accepted','queued', ?,NULL,?)",
                         (job_id, 1, "Richiesta salute accodata.", now))
            row = conn.execute("SELECT * FROM care_jobs WHERE job_id=?", (job_id,)).fetchone()
            return {**self._public(row), "duplicate": False}
        result = self._write(session, create)
        if result["duplicate"]:
            row = self._read(session, lambda conn: self._job(conn, session, result["job_id"]))
            self._authorize_source(row)
        return result

    def source(self, origin, job_id):
        """Host-only rehydration of the exact admitted user event for child setup."""
        session = self._session(origin, "read")
        row = self._read(session, lambda conn: self._job(conn, session, job_id))
        self._authorize_source(row)
        return self.runtime.admitted(platform=row["source_platform"], actor_id=row["source_actor_id"],
            conversation_id=row["source_conversation_id"], message_id=row["source_message_id"],
            session_key=row["source_session_key"])

    def claim_launch(self, origin, job_id, launch_id):
        """CAS queued->running before the Hermes child is started.

        If the host crashes after this transaction, recovery must mark it
        unknown rather than launch it again.
        """
        _identifier(launch_id, "launch_id")
        session = self._session(origin, "read")
        row_before = self._read(session, lambda conn: self._job(conn, session, job_id))
        self._authorize_source(row_before)
        now = _time(self.runtime)
        def claim(conn):
            row = self._job(conn, session, job_id)
            if row["state"] == "running" and row["launch_id"] == launch_id:
                return {**self._public(row), "replayed": True}
            if row["state"] != "queued":
                raise ConflictError("Health job is not launchable")
            conn.execute("""UPDATE care_jobs SET state='running',launch_id=?,native_delegation_id=NULL,
                child_session_id=NULL,attempt=attempt+1,updated_at=?,finished_at=NULL,outcome=NULL,
                summary=NULL,result_json=NULL,completion_fingerprint=NULL WHERE job_id=? AND state='queued'""",
                (launch_id, now, job_id))
            conn.execute("INSERT INTO care_job_events VALUES(?,?, 'launch_claimed','running', ?,NULL,?)",
                (job_id, self._next_event(conn, job_id), "Lo specialista salute è in esecuzione.", now))
            updated = self._job(conn, session, job_id)
            return {**self._public(updated), "replayed": False}
        return self._write(session, claim)

    def bind_child(self, origin, job_id, launch_id, *, child_run_id, child_session_id=None):
        """Bind Hermes on_start identity before launch returns to the parent."""
        _identifier(launch_id, "launch_id")
        _identifier(child_run_id, "child_run_id")
        if child_session_id is not None:
            _identifier(child_session_id, "child_session_id")
        session = self._session(origin, "read")
        row_before = self._read(session, lambda conn: self._job(conn, session, job_id))
        self._authorize_source(row_before)
        now = _time(self.runtime)
        def bind(conn):
            row = self._job(conn, session, job_id)
            if row["launch_id"] != launch_id or row["state"] not in {"running", "cancel_requested"}:
                raise ConflictError("Native child does not match the active health job")
            if row["native_delegation_id"] not in (None, child_run_id):
                raise ConflictError("Health job already has a different child")
            if row["child_session_id"] not in (None, child_session_id):
                raise ConflictError("Health job child session changed")
            conn.execute("UPDATE care_jobs SET native_delegation_id=?,child_session_id=?,updated_at=? WHERE job_id=?",
                         (child_run_id, child_session_id, now, job_id))
            updated = self._job(conn, session, job_id)
            return {**self._public(updated), "cancel_required": updated["state"] == "cancel_requested"}
        return self._write(session, bind)

    def authorize_child(self, origin, job_id, launch_id, *, child_run_id, child_session_id=None):
        """Fail closed unless both source and currently running child still match."""
        session = self._session(origin, "read")
        row = self._read(session, lambda conn: self._job(conn, session, job_id))
        self._authorize_source(row)
        if (row["state"] != "running" or row["launch_id"] != launch_id
                or row["native_delegation_id"] != child_run_id
                or (child_session_id is not None and row["child_session_id"] != child_session_id)):
            raise AuthorizationError("Health child is no longer authorized")
        return True

    def complete(self, origin, job_id, launch_id, *, outcome, summary, result=None,
                 question=None, child_run_id=None):
        """Persist a bounded child result; suppressed after cancellation/revocation."""
        if outcome not in _OUTCOMES:
            raise CareError("Invalid health job outcome")
        if question is None and outcome == "awaiting_user" and isinstance(result, dict):
            question = result.get("question")
        if not isinstance(summary, str) or len(summary) > _SUMMARY_LIMIT:
            raise CareError("Health job summary exceeds limit")
        if question is not None and (not isinstance(question, str) or len(question) > 1200):
            raise CareError("Health job question exceeds limit")
        if outcome == "awaiting_user" and not question:
            raise CareError("Awaiting-user outcomes require a question")
        if outcome != "awaiting_user" and question is not None:
            raise CareError("Only awaiting-user outcomes may include a question")
        result_json = None if result is None else _json(result)
        if result_json is not None and len(result_json.encode()) > _RESULT_LIMIT:
            raise CareError("Health job result exceeds limit")
        fingerprint = hashlib.sha256(_json({"outcome": outcome, "summary": summary,
            "result": result, "question": question}).encode()).hexdigest()
        session = self._session(origin, "read")
        row_before = self._read(session, lambda conn: self._job(conn, session, job_id))
        try:
            self._authorize_source(row_before)
        except (AuthorizationError, PermissionError):
            return {"published": False, "status": "suppressed_revoked"}
        now = _time(self.runtime)
        def finish(conn):
            row = self._job(conn, session, job_id)
            if row["state"] in {"cancel_requested", "cancelled"}:
                return {"published": False, "status": "suppressed_cancelled"}
            expected_child = child_run_id or row["native_delegation_id"]
            if row["state"] in _OUTCOMES and row["native_delegation_id"] == expected_child:
                if row["completion_fingerprint"] == fingerprint:
                    return {**self._public(row, include_result=True), "published": True, "replayed": True}
                raise ConflictError("Health child completion changed after commit")
            if (row["state"] != "running" or row["launch_id"] != launch_id
                    or (expected_child is not None and row["native_delegation_id"] != expected_child)):
                return {"published": False, "status": "suppressed_stale_child"}
            conn.execute("""UPDATE care_jobs SET state=?,updated_at=?,finished_at=?,outcome=?,summary=?,
                result_json=?,completion_fingerprint=? WHERE job_id=? AND state='running'""",
                (outcome, now, now, outcome, summary, result_json, fingerprint, job_id))
            self._append_event(conn, job_id, "child_completed", outcome, summary, question, now)
            updated = self._job(conn, session, job_id)
            return {**self._public(updated, include_result=True), "published": True, "replayed": False}
        result = self._write(session, finish)
        if result.get("published"):
            try:
                self._authorize_source(row_before)
                session.require("read")
            except (AuthorizationError, PermissionError):
                return {"published": False, "status": "suppressed_revoked"}
        return result

    def cancel(self, origin, job_id):
        """Request cancellation; caller must invoke native child cancellation."""
        session = self._session(origin, "write")
        row_before = self._read(session, lambda conn: self._job(conn, session, job_id))
        self._authorize_source(row_before)
        now = _time(self.runtime)
        def request(conn):
            row = self._job(conn, session, job_id)
            if row["state"] == "queued":
                conn.execute("UPDATE care_jobs SET state='cancelled',updated_at=?,finished_at=? WHERE job_id=?",
                             (now, now, job_id))
                self._append_event(conn, job_id, "cancelled_before_launch", "cancelled",
                                   "Richiesta annullata prima dell'avvio.", None, now)
            elif row["state"] in {"running", "cancel_requested"}:
                conn.execute("""UPDATE care_jobs SET state='cancel_requested',
                    cancel_requested_at=COALESCE(cancel_requested_at,?), updated_at=? WHERE job_id=?""",
                    (now, now, job_id))
                if row["state"] != "cancel_requested":
                    self._append_event(conn, job_id, "cancel_requested", "cancel_requested",
                                       "Annullamento richiesto; l'esito del lavoro è in sospeso.", None, now)
            # Awaiting-user work has no active side effect to interrupt.
            elif row["state"] == "awaiting_user":
                conn.execute("UPDATE care_jobs SET state='cancelled',updated_at=?,finished_at=? WHERE job_id=?",
                             (now, now, job_id))
                self._append_event(conn, job_id, "cancelled_awaiting_user", "cancelled",
                                   "Richiesta in attesa annullata.", None, now)
            updated = self._job(conn, session, job_id)
            return {**self._public(updated), "native_delegation_id": updated["native_delegation_id"],
                    "cancel_requested": updated["state"] == "cancel_requested"}
        return self._write(session, request, permission="write")

    def ack_cancelled(self, origin, job_id, launch_id, child_run_id=None):
        """Mark cancelled only after Hermes confirms the child is stopped."""
        session = self._session(origin, "read")
        row_before = self._read(session, lambda conn: self._job(conn, session, job_id))
        self._authorize_source(row_before)
        now = _time(self.runtime)
        def ack(conn):
            row = self._job(conn, session, job_id)
            expected_child = child_run_id or row["native_delegation_id"]
            if row["state"] == "cancelled" and row["native_delegation_id"] == expected_child:
                return {**self._public(row), "replayed": True}
            if (row["state"] != "cancel_requested" or row["launch_id"] != launch_id
                    or row["native_delegation_id"] != expected_child):
                raise ConflictError("Cancellation does not match active child")
            conn.execute("UPDATE care_jobs SET state='cancelled',updated_at=?,finished_at=? WHERE job_id=?",
                         (now, now, job_id))
            self._append_event(conn, job_id, "child_cancelled", "cancelled",
                               "Lavoro annullato.", None, now)
            return {**self._public(self._job(conn, session, job_id)), "replayed": False}
        return self._write(session, ack)

    def acknowledge_cancelled(self, origin, job_id, *, launch_id, native_delegation_id):
        return self.ack_cancelled(origin, job_id, launch_id, child_run_id=native_delegation_id)

    def interrupt(self, origin, job_id, *, launch_id, reason="host_restarted"):
        """Persist uncertainty for a child verified dead by the native host.

        This never relaunches the job. ``reason`` is a bounded enum so raw host
        exceptions/credentials cannot enter the health database.
        """
        if reason not in {"host_restarted", "child_lost", "launch_uncertain"}:
            raise CareError("Invalid interruption reason")
        session = self._session(origin, "read")
        row_before = self._read(session, lambda conn: self._job(conn, session, job_id))
        self._authorize_source(row_before)
        now = _time(self.runtime)
        def mark(conn):
            row = self._job(conn, session, job_id)
            if row["state"] == "unknown" and row["launch_id"] == launch_id:
                return {**self._public(row), "replayed": True}
            if row["state"] == "cancel_requested":
                # The child is confirmed gone by caller: cancellation is final.
                conn.execute("UPDATE care_jobs SET state='cancelled',updated_at=?,finished_at=? WHERE job_id=?",
                             (now, now, job_id))
                self._append_event(conn, job_id, "cancelled_after_child_exit", "cancelled",
                                   "Lavoro annullato dopo l'uscita dello specialista.", None, now)
            elif row["state"] == "running" and row["launch_id"] == launch_id:
                summary = "Esecuzione interrotta; verificare gli effetti prima di ripetere il lavoro."
                conn.execute("UPDATE care_jobs SET state='unknown',outcome='unknown',summary=?,updated_at=?,finished_at=? WHERE job_id=?",
                             (summary, now, now, job_id))
                self._append_event(conn, job_id, reason, "unknown", summary, None, now)
            else:
                raise ConflictError("Interruption does not match active child")
            return {**self._public(self._job(conn, session, job_id)), "replayed": False}
        return self._write(session, mark)

    def reconcile_scope(self, scope_id, *, active_child_ids):
        """Mark non-live active jobs unknown after native process recovery.

        This is a host-only lifecycle operation, not a model action. The caller
        supplies IDs observed from Hermes' current child registry. Queued jobs
        remain queued; running jobs absent from that authoritative set are
        never relaunched automatically.
        """
        if not isinstance(scope_id, str) or not scope_id or len(scope_id) > 128:
            raise CareError("Invalid care scope")
        if not isinstance(active_child_ids, (set, frozenset, tuple, list)) or len(active_child_ids) > 10000:
            raise CareError("Invalid native child registry")
        active = set(active_child_ids)
        if any(not isinstance(item, str) or not _NATIVE_ID.fullmatch(item) for item in active):
            raise CareError("Invalid native child identity")
        candidates = [b for b in self.runtime.bindings()
                      if b.scope_id == scope_id and "read" in b.permissions]
        if not candidates:
            raise AuthorizationError("No current host grant for care scope recovery")
        binding = candidates[0]
        from .care_runtime import Origin
        origin = Origin(binding.platform, binding.actor_id, binding.conversation_id,
            "care-jobs-recovery", "care-jobs-recovery", "", _time(self.runtime))
        session = self._session(origin, "read")
        now = _time(self.runtime)

        def reconcile(conn):
            rows = conn.execute("SELECT * FROM care_jobs WHERE scope_id=? AND state IN ('running','cancel_requested')",
                                (scope_id,)).fetchall()
            unknown = cancelled = retained = 0
            for row in rows:
                child_id = row["native_delegation_id"]
                if child_id is not None and child_id in active:
                    retained += 1
                    continue
                if row["state"] == "cancel_requested":
                    conn.execute("UPDATE care_jobs SET state='cancelled',updated_at=?,finished_at=? WHERE job_id=?",
                                 (now, now, row["job_id"]))
                    self._append_event(conn, row["job_id"], "recovery_cancelled", "cancelled",
                                       "Lavoro annullato dopo il riavvio.", None, now)
                    cancelled += 1
                else:
                    summary = "Esecuzione interrotta; verificare gli effetti prima di ripetere il lavoro."
                    conn.execute("UPDATE care_jobs SET state='unknown',outcome='unknown',summary=?,updated_at=?,finished_at=? WHERE job_id=?",
                                 (summary, now, now, row["job_id"]))
                    self._append_event(conn, row["job_id"], "host_restarted", "unknown", summary, None, now)
                    unknown += 1
            return {"unknown": unknown, "cancelled": cancelled, "retained": retained}
        return self._write(session, reconcile)

    def continue_job(self, origin, job_id):
        """Attach an explicitly received follow-up to an awaiting job."""
        session = self._session(origin, "write")
        binding = self.runtime._binding(origin)
        self.runtime._persist_admission(origin)
        row_before = self._read(session, lambda conn: self._job(conn, session, job_id))
        self._authorize_source(row_before)
        now = _time(self.runtime)
        def resume(conn):
            row = self._job(conn, session, job_id)
            if binding.scope_id != row["scope_id"]:
                raise AuthorizationError("Follow-up belongs to another care scope")
            if row["state"] != "awaiting_user":
                previous = conn.execute("SELECT 1 FROM care_job_messages WHERE job_id=? AND source_request_id=?",
                    (job_id, origin.request_id)).fetchone()
                if previous:
                    return {**self._public(row), "duplicate": True}
                raise ConflictError("Health job is not awaiting a follow-up")
            old = conn.execute("SELECT 1 FROM care_job_messages WHERE job_id=? AND source_request_id=?",
                               (job_id, origin.request_id)).fetchone()
            if old:
                return {**self._public(row), "duplicate": True}
            sequence = conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM care_job_messages WHERE job_id=?",
                                    (job_id,)).fetchone()[0]
            conn.execute("INSERT INTO care_job_messages VALUES(?,?,?,?, 'user',?)",
                         (job_id, sequence, origin.request_id, origin.session_key, now))
            conn.execute("""UPDATE care_jobs SET state='queued',updated_at=?,launch_id=NULL,
                native_delegation_id=NULL,child_session_id=NULL,outcome=NULL,summary=NULL,
                result_json=NULL,completion_fingerprint=NULL,finished_at=NULL WHERE job_id=?""",
                         (now, job_id))
            self._append_event(conn, job_id, "user_follow_up", "queued",
                               "L'utente ha risposto al chiarimento.", None, now)
            return {**self._public(self._job(conn, session, job_id)), "duplicate": False}
        return self._write(session, resume, permission="write")

    def input_messages(self, origin, job_id, *, max_messages=3, max_bytes=_CONTEXT_LIMIT):
        """Return only this health job's source messages, never Casita transcript."""
        if type(max_messages) is not int or not 1 <= max_messages <= 3:
            raise CareError("Health context is limited to three messages")
        if type(max_bytes) is not int or not 1 <= max_bytes <= _CONTEXT_LIMIT:
            raise CareError("Health context exceeds limit")
        session = self._session(origin, "read")
        row = self._read(session, lambda conn: self._job(conn, session, job_id))
        self._authorize_source(row)
        rows = self._read(session, lambda conn: conn.execute(
            "SELECT source_request_id,session_key FROM care_job_messages WHERE job_id=? AND role='user' ORDER BY sequence DESC LIMIT ?",
            (job_id, max_messages)).fetchall())
        messages = []
        budget = max_bytes
        for item in reversed(rows):
            source = self.runtime.admitted(platform=row["source_platform"], actor_id=row["source_actor_id"],
                conversation_id=row["source_conversation_id"],
                message_id=self._message_id_for_request(session, job_id, item["source_request_id"]),
                session_key=item["session_key"])
            payload = {"text": source.text, "received_at": source.received_at,
                       "attachments": list(source.attachment_refs)}
            encoded = _json(payload).encode()
            if len(encoded) > budget:
                # A source message is atomic; never truncate a symptom/dose/text.
                continue
            messages.append(payload)
            budget -= len(encoded)
        return messages

    def _message_id_for_request(self, session, job_id, request_id):
        value = self._read(session, lambda conn: conn.execute(
            "SELECT e.origin_json FROM care_ingress e WHERE e.request_id=?", (request_id,)).fetchone())
        if value is None:
            raise AuthorizationError("Health job source is unavailable")
        origin = json.loads(value[0])
        return origin["message_id"]

    def history(self, origin, job_id, *, limit=3, max_bytes=_CONTEXT_LIMIT):
        """Bounded job-only summaries/questions for a follow-up; no parent transcript."""
        if type(limit) is not int or not 1 <= limit <= 3 or type(max_bytes) is not int or not 1 <= max_bytes <= _CONTEXT_LIMIT:
            raise CareError("Health job history exceeds limit")
        session = self._session(origin, "read")
        row = self._read(session, lambda conn: self._job(conn, session, job_id))
        self._authorize_source(row)
        events = self._read(session, lambda conn: conn.execute(
            "SELECT event_type,status,summary,question,created_at FROM care_job_events WHERE job_id=? ORDER BY sequence DESC LIMIT ?",
            (job_id, limit)).fetchall())
        result, used = [], 0
        for item in reversed(events):
            payload = dict(item)
            encoded = _json(payload).encode()
            if used + len(encoded) > max_bytes:
                continue
            result.append(payload)
            used += len(encoded)
        return result

    def get(self, origin, job_id, *, include_result=True):
        session = self._session(origin, "read")
        row = self._read(session, lambda conn: self._job(conn, session, job_id))
        self._authorize_source(row)
        return self._public(row, include_result=include_result)

    def list(self, origin, *, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CareError("Health job list limit is invalid")
        session = self._session(origin, "read")
        rows = self._read(session, lambda conn: conn.execute(
            "SELECT * FROM care_jobs WHERE scope_id=? ORDER BY created_at DESC LIMIT ?",
            (session.authority.scope_id, limit)).fetchall())
        visible = []
        for row in rows:
            try:
                self._authorize_source(row)
            except (AuthorizationError, PermissionError):
                continue
            visible.append(self._public(row, include_result=True))
        return visible

    @staticmethod
    def _next_event(conn, job_id):
        return conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM care_job_events WHERE job_id=?",
                            (job_id,)).fetchone()[0]

    def _append_event(self, conn, job_id, event_type, status, summary, question, now):
        sequence = self._next_event(conn, job_id)
        conn.execute("INSERT INTO care_job_events VALUES(?,?,?,?,?,?,?)",
                     (job_id, sequence, event_type, status, summary[:_SUMMARY_LIMIT],
                      question[:1200] if question else None, now))


def _identifier(value, field):
    if not isinstance(value, str) or not _NATIVE_ID.fullmatch(value):
        raise CareError(f"Invalid {field}")
