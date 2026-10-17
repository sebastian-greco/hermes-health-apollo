"""Trusted orchestration of scoped Apollo journals and the isolated specialist.

No model argument supplies identity, a database path, credentials or a Drive
folder. Host mappings are re-read before every operation. SQLite transactions
never span inference or network I/O; a private per-request advisory lock and
durable prepared actions make interrupted work resumable.
"""
from __future__ import annotations

import contextlib
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Callable
from zoneinfo import ZoneInfo

from .care_store import Authority, AuthorizationError, CareError, CareStore, CareSession, ConflictError
from . import care_specialist, food


@dataclass(frozen=True)
class Origin:
    platform: str
    actor_id: str
    conversation_id: str
    message_id: str
    session_key: str
    text: str
    received_at: str
    attachment_refs: tuple[str, ...] = ()

    def __post_init__(self):
        for value in (self.platform, self.actor_id, self.conversation_id, self.message_id, self.session_key):
            if not isinstance(value, str) or not value or len(value) > 256:
                raise ValueError("Invalid trusted origin")
        if not isinstance(self.text, str) or len(self.text) > 16000:
            raise ValueError("Original message exceeds intake bound")
        if datetime.fromisoformat(self.received_at.replace("Z", "+00:00")).utcoffset() is None:
            raise ValueError("Trusted receipt time requires an offset")
        if (not isinstance(self.attachment_refs, tuple) or len(self.attachment_refs) > 30
                or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", x)
                       for x in self.attachment_refs)):
            raise ValueError("Invalid attachment references")

    @property
    def request_id(self):
        # Stable across process restarts and gateway redelivery.
        return hashlib.sha256(json.dumps([self.platform, self.actor_id,
            self.conversation_id, self.message_id]).encode()).hexdigest()

    @property
    def source_ref(self):
        return "message:" + self.request_id


@dataclass(frozen=True)
class Binding:
    platform: str
    actor_id: str
    conversation_id: str
    scope_id: str
    permissions: frozenset[str]

    def authority(self):
        return Authority(self.actor_id, self.scope_id, self.permissions)


class _FreshSession(CareSession):
    """Recheck the exact admitted route on every domain storage boundary."""
    def __init__(self, path, authority, current_binding):
        super().__init__(path, authority)
        self._current_binding = current_binding

    def require(self, permission):
        current = self._current_binding()
        if current.scope_id != self.authority.scope_id or permission not in current.permissions:
            raise AuthorizationError("Current route no longer grants this care operation")
        super().require(permission)


def _dump(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _schema(conn):
    for statement in ("""
        CREATE TABLE IF NOT EXISTS care_ingress (
          request_id TEXT PRIMARY KEY, origin_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS care_ingress_sessions (
          request_id TEXT NOT NULL, session_key TEXT NOT NULL,
          PRIMARY KEY(request_id, session_key));
        CREATE TABLE IF NOT EXISTS care_dispatch (
          request_id TEXT PRIMARY KEY, source_hash TEXT NOT NULL, state_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS care_pending_confirmations (
          challenge TEXT PRIMARY KEY, actor_id TEXT NOT NULL, conversation_id TEXT NOT NULL, platform TEXT NOT NULL,
          plan_id TEXT NOT NULL, version INTEGER NOT NULL, expires_at TEXT NOT NULL,
          used_by TEXT);
        CREATE TABLE IF NOT EXISTS care_pending_manual (
          challenge TEXT PRIMARY KEY, actor_id TEXT NOT NULL, conversation_id TEXT NOT NULL, platform TEXT NOT NULL,
          payload_json TEXT NOT NULL, expires_at TEXT NOT NULL, used_by TEXT, result_json TEXT);
        CREATE TABLE IF NOT EXISTS care_plan_confirmation_receipts (
          challenge TEXT PRIMARY KEY, source_ref TEXT NOT NULL, confirmed_at TEXT NOT NULL);
        """).split(";"):
        if statement.strip():
            conn.execute(statement)


class CareRuntime:
    def __init__(self, root: Path, *, bindings: Callable[[], list[Binding]], llm,
                 documents: Callable | None = None, attachment_loader: Callable | None = None,
                 clock: Callable[[], datetime] | None = None, reminders_enabled=False,
                 on_change: Callable | None = None, timezone_name="UTC"):
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            raise ValueError("Runtime root cannot be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.bindings = bindings
        self.llm = llm
        self.documents = documents
        self.attachment_loader = attachment_loader
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timezone_name = ZoneInfo(timezone_name).key
        self.reminders_enabled = reminders_enabled
        self.delivery_available = lambda origin: self.reminders_enabled
        self.on_change = on_change or (lambda: None)
        self._admissions = OrderedDict()
        self._admission_lock = threading.RLock()

    def _binding(self, origin: Origin) -> Binding:
        found = [b for b in self.bindings() if (b.platform, b.actor_id, b.conversation_id) ==
                 (origin.platform, origin.actor_id, origin.conversation_id)]
        if len(found) != 1:
            raise AuthorizationError("No unique current route grant")
        found[0].authority()
        return found[0]

    def authority_current(self, authority):
        return any(b.actor_id == authority.actor_id and b.scope_id == authority.scope_id
                   and authority.permissions <= b.permissions for b in self.bindings())

    def _fresh(self, origin, scope_id):
        if self._binding(origin).scope_id != scope_id:
            raise AuthorizationError("Care route scope changed during the turn")
        return self.session(origin)

    def session(self, origin: Origin):
        binding = self._binding(origin)
        directory = self.root / hashlib.sha256(binding.scope_id.encode()).hexdigest()
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise AuthorizationError("Invalid scope directory")
        path = directory / "apollo.sqlite3"
        if path.is_symlink():
            raise AuthorizationError("Invalid database file")
        care = CareStore(path, scope_id=binding.scope_id)
        path.chmod(0o600)
        session = _FreshSession(care.db_path, binding.authority(), lambda: self._binding(origin))
        # Trusted default definition; configurable categories are added at runtime.
        admin = care.session(Authority("care-host", binding.scope_id, frozenset({"admin"})))
        admin.define_category("journal", label="Diario salute", schema={"type": "object"})
        with admin.connection(permission="admin") as conn:
            _schema(conn)
        return session

    def admit(self, origin: Origin):
        """Host-only entrypoint, called AFTER native authorization/admission."""
        binding = self._binding(origin)
        # Admission proves identity; it does not mean this is a health message.
        # Keep unselected general Casita messages only in bounded process memory.
        with self._admission_lock:
            old = self._admissions.get(origin.request_id)
            if old:
                if old[2] != binding.scope_id:
                    raise AuthorizationError("Care scope changed after message admission")
                if self._source_content(old[0]) != self._source_content(origin):
                    raise ConflictError("Admitted message identity changed")
                old[1].add(origin.session_key)
                self._admissions.move_to_end(origin.request_id)
            else:
                self._admissions[origin.request_id] = (origin, {origin.session_key}, binding.scope_id)
                while len(self._admissions) > 256:
                    self._admissions.popitem(last=False)
        return origin.request_id

    @staticmethod
    def _source_content(origin):
        return {k: v for k, v in asdict(origin).items() if k not in {"received_at", "session_key"}}

    def _persist_admission(self, origin):
        """Persist only messages actually delegated to care or explicitly confirmed."""
        session = self.session(origin)
        # Receipt storage is a host activity, not a patient modification grant.
        host = CareStore(session.db_path, scope_id=session.authority.scope_id).session(
            Authority("care-host", session.authority.scope_id, frozenset({"write"})))
        serialized = _dump(asdict(origin))
        with host.connection(permission="write") as conn:
            old = conn.execute("SELECT origin_json FROM care_ingress WHERE request_id=?", (origin.request_id,)).fetchone()
            if old:
                previous = json.loads(old[0])
                # Gateway retry may have a fresh receipt time/session key.
                incoming = json.loads(serialized)
                for key in ("received_at", "session_key"):
                    previous.pop(key, None)
                    incoming.pop(key, None)
                if incoming != previous:
                    raise ConflictError("Admitted message identity changed")
            else:
                conn.execute("INSERT INTO care_ingress VALUES (?, ?)", (origin.request_id, serialized))
            conn.execute("INSERT OR IGNORE INTO care_ingress_sessions VALUES (?, ?)", (origin.request_id, origin.session_key))
        return origin.request_id

    def admitted(self, *, platform, actor_id, conversation_id, message_id, session_key):
        probe = Origin(platform, actor_id, conversation_id, message_id, session_key, "", self.clock().isoformat())
        binding = self._binding(probe)
        with self._admission_lock:
            cached = self._admissions.get(probe.request_id)
            if cached and session_key in cached[1]:
                if binding.scope_id != cached[2]:
                    raise AuthorizationError("Care scope changed after message admission")
                return Origin(**{**asdict(cached[0]), "session_key": session_key})
        session = self.session(probe)
        with session.connection() as conn:
            row = conn.execute("SELECT origin_json FROM care_ingress WHERE request_id=?", (probe.request_id,)).fetchone()
            admitted_session = conn.execute("SELECT 1 FROM care_ingress_sessions WHERE request_id=? AND session_key=?",
                (probe.request_id, session_key)).fetchone()
        if not row:
            raise AuthorizationError("Message has no trusted admission receipt")
        values = json.loads(row[0])
        if not admitted_session:
            raise AuthorizationError("Admission belongs to another native session")
        values["attachment_refs"] = tuple(values["attachment_refs"])
        values["session_key"] = session_key
        return Origin(**values)

    @contextlib.contextmanager
    def _request_lock(self, origin):
        path = self.root / (origin.request_id + ".lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _host_connection(self, session):
        return CareStore(session.db_path, scope_id=session.authority.scope_id).session(
            Authority("care-host", session.authority.scope_id, frozenset({"write"}))).connection(permission="write")

    def consult(self, origin: Origin, *, request: str = "") -> dict:
        """Execute a bounded specialist turn; callers must supply admitted Origin."""
        if not isinstance(request, str) or len(request) > 1200:
            raise CareError("Invalid routing hint")
        # Trusted API users must also admit; no bypass for direct Python calls.
        original = self.admitted(platform=origin.platform, actor_id=origin.actor_id,
            conversation_id=origin.conversation_id, message_id=origin.message_id, session_key=origin.session_key)
        if original != origin:
            raise AuthorizationError("Origin differs from admitted event")
        self._persist_admission(origin)
        with self._request_lock(origin):
            session = self.session(origin)
            session.require("read")
            source_hash = hashlib.sha256(_dump({k: v for k, v in asdict(origin).items()
                if k not in {"session_key", "received_at"}}).encode()).hexdigest()
            with self._host_connection(session) as conn:
                old = conn.execute("SELECT source_hash, state_json FROM care_dispatch WHERE request_id=?", (origin.request_id,)).fetchone()
                if old and old[0] != source_hash:
                    raise ConflictError("Request source changed")
                state = json.loads(old[1]) if old else {"plans": [], "outputs": [], "next": 0, "done": False, "question": None}
                if not old:
                    conn.execute("INSERT INTO care_dispatch VALUES (?, ?, ?)", (origin.request_id, source_hash, _dump(state)))
            if state.get("result"):
                session.require("read")
                return {**state["result"], "replayed": True}
            for round_index in range(3):
                session = self._fresh(origin, session.authority.scope_id)
                if state["next"] == len(state["plans"]):
                    if state["done"]:
                        break
                    context = self._context(session, origin)
                    context.update(original_message=origin.text, received_at=origin.received_at,
                        timezone=self.timezone_name,
                        received_local=datetime.fromisoformat(origin.received_at.replace("Z", "+00:00")).astimezone(ZoneInfo(self.timezone_name)).isoformat(),
                        routing_hint=request, attachments=list(origin.attachment_refs),
                        results=state["outputs"], available_actions=list(care_specialist.ACTION_SCHEMAS))
                    if len(_dump(context).encode()) > 32000:
                        context["results"] = [self._result_reference(x) for x in state["outputs"]]
                        context["results_omitted"] = True
                    for optional in ("recent_entries", "cases", "categories", "previous_questions"):
                        while len(_dump(context).encode()) > 32000 and context.get(optional):
                            context[optional].pop(0)
                            context["context_omitted"] = True
                    if len(_dump(context).encode()) > 32000:
                        state.update(done=True, question="Il messaggio è troppo lungo per questo passaggio. Dividilo in due messaggi più brevi.")
                        break
                    plan = care_specialist.complete(self.llm, context)
                    session = self._fresh(origin, session.authority.scope_id)
                    session.require("read")
                    state["plans"].extend(plan["commands"])
                    state.update(done=plan["done"], question=plan["question"])
                    self._save(session, origin, state)
                while state["next"] < len(state["plans"]):
                    session = self._fresh(origin, session.authority.scope_id)
                    command = state["plans"][state["next"]]
                    op_id = origin.request_id + ":" + str(state["next"])
                    try:
                        args = self._resolve(command["args"], state["outputs"])
                        output = self._execute(session, origin, command["op"], args, op_id)
                        output = self._bound_output(session, origin, output, op_id)
                    except (ValueError, PermissionError, KeyError, TypeError) as exc:
                        # Don't echo account errors, file paths or external provider payloads.
                        output = {"status": "refused", "code": type(exc).__name__, "operation": command["op"]}
                        state["done"] = True
                        state["question"] = "Serve un chiarimento prima di proseguire; nessuna conferma di riuscita per questa azione."
                        state["outputs"].append(output)
                        state["next"] += 1
                        state["plans"] = state["plans"][:state["next"]]
                        self._save(session, origin, state)
                        break
                    state["outputs"].append(output)
                    state["next"] += 1
                    self._save(session, origin, state)
                if state["done"]:
                    break
            if not state["done"]:
                state["question"] = "Ho raggiunto il limite di questo passaggio. Posso continuare con una nuova richiesta."
            result = {"outcome": "needs_clarification" if state["question"] else "succeeded",
                      "results": state["outputs"], "question": state["question"], "replayed": False}
            if len(_dump(result).encode()) > 16000:
                full = self._bound_output(session, origin, result, origin.request_id + ":final")
                result.update(results=[self._result_reference(x) for x in state["outputs"]],
                              full_result=full, results_omitted=True)
            state["result"] = result
            self._save(session, origin, state)
            session.require("read")
            return result

    def _save(self, session, origin, state):
        with self._host_connection(session) as conn:
            conn.execute("UPDATE care_dispatch SET state_json=? WHERE request_id=?", (_dump(state), origin.request_id))
        self.on_change()

    def _context(self, session, origin):
        with session.connection() as conn:
            previous = conn.execute("SELECT d.state_json FROM care_dispatch d JOIN care_ingress i USING(request_id) "
                "WHERE d.request_id != ? ORDER BY d.rowid DESC LIMIT 3", (origin.request_id,)).fetchall()
        # Shared scope history is authorized; routing data is never model-editable.
        categories = [{k: c[k] for k in ("category_id", "version", "label", "schema", "observations")} for c in session.list_categories()]
        context = {"categories": categories[-30:], "cases": session.list_cases()[-20:],
                "recent_entries": session.list_entries(limit=20, latest=True),
                "previous_questions": [json.loads(r[0]).get("question") for r in previous if json.loads(r[0]).get("question")],
                "reminder_delivery_enabled": self.delivery_available(origin),
                "document_intake_available": self.documents is not None and self.attachment_loader is not None}
        # A bounded specialist context, with explicit omissions, is not a copy
        # of the full longitudinal record. Queries can retrieve older evidence.
        for key in ("recent_entries", "cases", "categories", "previous_questions"):
            while len(_dump(context).encode()) > 20000 and context[key]:
                context[key].pop(0)
                context["context_omitted"] = True
        return context

    def _bound_output(self, session, origin, output, op_id):
        encoded = _dump(output).encode()
        if len(encoded) <= 12000:
            return output
        # Large factual reports remain retrievable through a scoped host
        # artifact, not injected in full into Casita or another model round.
        directory = session.db_path.parent / "reports"
        directory.mkdir(exist_ok=True, mode=0o700)
        digest = hashlib.sha256(op_id.encode()).hexdigest()
        path = directory / (digest + ".json")
        fd, staging = tempfile.mkstemp(prefix=".care-report-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staging, path)
        finally:
            if os.path.exists(staging):
                os.unlink(staging)
        summary = {k: output[k] for k in ("entry_id", "case_id", "plan_id", "job_id", "id", "schedule_id",
                   "entry_count", "counts_by_category", "start", "end", "clinical_inference", "truncated")
                   if isinstance(output, dict) and k in output}
        if isinstance(output, dict) and "medication" in output:
            summary["reported_administration_count"] = output["medication"].get("reported_administrations", {}).get("total", 0)
        if isinstance(output, dict) and "documents" in output:
            summary["document_evidence_counts"] = {k: v["total"] for k, v in output["documents"].items()
                if isinstance(v, dict) and "total" in v}
        if isinstance(output, dict) and "gaps" in output:
            summary["days_without_exact_time_entries"] = len(output["gaps"].get("days_without_exact_time_entries", []))
        return {**summary, "artifact_ref": digest, "full_result_omitted": True,
                "artifact_bytes": len(encoded), "large_export": "report_artifact_chunks",
                "meaning": "Rapporto completo conservato localmente; nessuna conclusione clinica automatica."}

    @staticmethod
    def _result_reference(value):
        if not isinstance(value, dict):
            return {"result_omitted": True, "count": len(value) if isinstance(value, list) else None}
        return {k: v for k, v in value.items() if k in {"entry_id", "case_id", "plan_id", "job_id", "id",
            "schedule_id", "occurrence_id", "status", "revision", "version", "artifact_ref", "full_result_omitted",
            "entry_count", "start", "end", "confirmation"}}

    def report_artifact(self, origin, artifact_ref):
        if not re.fullmatch(r"[a-f0-9]{64}", artifact_ref):
            raise CareError("Invalid report reference")
        session = self.session(origin)
        session.require("read")
        path = session.db_path.parent / "reports" / (artifact_ref + ".json")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            content = handle.read(4_000_001)
        if len(content) > 4_000_000:
            raise CareError("Report exceeds artifact export bound")
        return content

    def report_artifact_chunks(self, origin, artifact_ref, *, chunk_bytes=1_000_000):
        """Host-only streaming export for reports beyond the bounded small API."""
        if not re.fullmatch(r"[a-f0-9]{64}", artifact_ref) or type(chunk_bytes) is not int or not 1 <= chunk_bytes <= 4_000_000:
            raise CareError("Invalid report export request")
        session = self.session(origin)
        session.require("read")
        path = session.db_path.parent / "reports" / (artifact_ref + ".json")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            while chunk := handle.read(chunk_bytes):
                session.require("read")
                yield chunk

    def _resolve(self, value, outputs):
        if isinstance(value, str) and re.fullmatch(r"\$\d+", value):
            index = int(value[1:])
            if index >= len(outputs):
                raise CareError("Invalid earlier result reference")
            out = outputs[index]
            for key in ("entry_id", "case_id", "plan_id", "job_id", "id", "schedule_id", "occurrence_id"):
                if isinstance(out, dict) and key in out:
                    return out[key]
            raise CareError("Earlier result has no reference")
        if isinstance(value, dict):
            return {k: self._resolve(v, outputs) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve(v, outputs) for v in value]
        return value

    def _reminders(self, session):
        from .care_reminders import CareReminderService
        return CareReminderService(session, clock=self.clock, authorize_current=self.authority_current)

    def _execute(self, session, origin, op, args, op_id):
        if op in {"append_entry", "create_case", "revise_entry", "update_case"}:
            return getattr(session, op)(**args, request_id=op_id, source_ref=origin.source_ref)
        if op == "report":
            from .care_reports import assemble_report
            return assemble_report(session, **args)
        if op in {"list_categories", "define_category", "get_entry", "list_entries", "get_case", "list_cases"}:
            return getattr(session, op)(**args)
        if op == "log_food":
            args = dict(args)
            case_id = args.pop("case_id", None)
            date.fromisoformat(args["day"])
            with session.apollo(permission="write"):
                result = food.log_food(**args, persist_narrative=True, request_id=op_id)
            if case_id:
                session.link_food(result["id"], case_id=case_id, source_ref=origin.source_ref)
            return result
        if op in {"get_plan", "list_upcoming", "cancel_plan", "acknowledge_reminder", "cancel_manual_reminder"}:
            return getattr(self._reminders(session), op)(**args)
        if op == "create_manual_reminder":
            return self._offer_manual(session, origin, args, op_id)
        if op == "record_administration":
            args = dict(args)
            args["occurred_at"] = datetime.fromisoformat(args["occurred_at"].replace("Z", "+00:00"))
            return self._reminders(session).record_administration(**args, source_ref=origin.source_ref, idempotency_key=op_id)
        if op == "propose_plan":
            from .care_reminders import ReminderSchedule
            args = dict(args)
            raw = args.pop("schedule")
            if set(raw) == {"at"}:
                schedule = ReminderSchedule.once(datetime.fromisoformat(raw["at"].replace("Z", "+00:00")))
            elif set(raw) <= {"times", "timezone", "start_date", "end_date"} and {"times", "timezone", "start_date"} <= set(raw):
                schedule = ReminderSchedule.daily(times=raw["times"], timezone=raw["timezone"],
                    start_date=date.fromisoformat(raw["start_date"]),
                    end_date=date.fromisoformat(raw["end_date"]) if raw.get("end_date") else None)
            else:
                raise CareError("Unsupported fixed plan schedule")
            # A deterministic plan ID makes crash replay safe without inventing a new plan.
            plan_id = hashlib.sha256(op_id.encode()).hexdigest()
            reminders = self._reminders(session)
            try:
                previous = reminders.get_plan(plan_id=plan_id)
            except ValueError:
                previous = None
            if previous is None:
                reminders.propose_plan(**args, schedule=schedule, source_ref=origin.source_ref, plan_id=plan_id)
            result = reminders.get_plan(plan_id=plan_id)
            return {**result, "confirmation": self._offer_confirmation(session, origin, plan_id)}
        if op in {"intake_documents", "prepare_document", "get_document", "upload_document", "propose_document_facts", "extract_document"}:
            if self.documents is None:
                raise CareError("Document service unavailable")
            workflow = self.documents(session)
            args = dict(args)
            if op == "intake_documents":
                refs = args.pop("attachment_refs")
                if (not refs or len(set(refs)) != len(refs) or self.attachment_loader is None
                        or not set(refs) <= set(origin.attachment_refs)):
                    raise AuthorizationError("No admitted attachments for this request")
                return workflow.intake([self.attachment_loader(origin, ref) for ref in refs], request_id=op_id, **args)
            if op == "prepare_document":
                from .care_documents import PageEdit
                if "pages" in args:
                    args["pages"] = [PageEdit(**p) for p in args["pages"]]
                return workflow.prepare(**args)
            if op == "get_document":
                return workflow.get(**args)
            if op == "extract_document":
                from .care_extract import extract_document
                return extract_document(workflow, self.llm, **args, request_id=op_id,
                    authorize_current=lambda authority: self._binding(origin).scope_id == authority.scope_id
                    and authority.permissions <= self._binding(origin).permissions)
            if op == "upload_document":
                return workflow.upload(**args)
            return workflow.propose(**args, request_id=op_id)
        raise CareError("Unsupported specialist action")

    def _offer_confirmation(self, session, origin, plan_id):
        from datetime import timedelta
        plan = self._reminders(session).get_plan(plan_id=plan_id)
        version = plan.get("current_version", plan.get("version"))
        challenge = hashlib.sha256(f"{origin.request_id}:{plan_id}:{version}".encode()).hexdigest()[:16]
        deadline = (self.clock() + timedelta(hours=1)).isoformat()
        with self._host_connection(session) as conn:
            conn.execute("INSERT OR IGNORE INTO care_pending_confirmations VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (challenge, origin.actor_id, origin.conversation_id, origin.platform, plan_id, version, deadline))
            deadline = conn.execute("SELECT expires_at FROM care_pending_confirmations WHERE challenge=?", (challenge,)).fetchone()[0]
        return {"command": "CONFERMO PIANO " + challenge, "expires_at": deadline,
                "meaning": "Conferma questi esatti farmaco, istruzioni e orari. Nessuna assunzione viene registrata."}

    def confirm_original(self, origin):
        """Native ingress-only explicit command. Never exposed to model actions."""
        match = re.fullmatch(r"CONFERMO (PIANO|PROMEMORIA) ([a-f0-9]{16})", origin.text.strip())
        if not match:
            return None
        self.admit(origin)
        self._persist_admission(origin)
        session = self.session(origin)
        if match[1] == "PROMEMORIA":
            return self._confirm_manual(session, origin, match[2])
        with self._host_connection(session) as conn:
            row = conn.execute("SELECT * FROM care_pending_confirmations WHERE challenge=?", (match[2],)).fetchone()
            receipt = conn.execute("SELECT source_ref, confirmed_at FROM care_plan_confirmation_receipts WHERE challenge=?", (match[2],)).fetchone()
        if (not row or row["actor_id"] != origin.actor_id or row["conversation_id"] != origin.conversation_id
                or row["platform"] != origin.platform
                or (not receipt and datetime.fromisoformat(row["expires_at"]) < self.clock())
                or (receipt and receipt["source_ref"] != origin.source_ref)
                or row["used_by"] not in (None, origin.message_id)):
            raise AuthorizationError("Confirmation is absent, expired or belongs to another actor")
        if not self.delivery_available(origin):
            raise CareError("Reminder delivery has not been enabled")
        with self._host_connection(session) as conn:
            conn.execute("INSERT OR IGNORE INTO care_plan_confirmation_receipts VALUES (?, ?, ?)",
                         (match[2], origin.source_ref, self.clock().isoformat()))
            receipt = conn.execute("SELECT source_ref, confirmed_at FROM care_plan_confirmation_receipts WHERE challenge=?", (match[2],)).fetchone()
        if receipt["source_ref"] != origin.source_ref:
            raise AuthorizationError("Another confirmation already owns this challenge")
        from .care_reminders import _mint_verified_confirmation
        confirmation = _mint_verified_confirmation(scope_id=session.authority.scope_id,
            actor_id=origin.actor_id, plan_id=row["plan_id"], version=row["version"],
            source_ref=receipt["source_ref"], confirmed_at=datetime.fromisoformat(receipt["confirmed_at"]))
        result = self._reminders(session).activate_plan(confirmation)
        with self._host_connection(session) as conn:
            conn.execute("UPDATE care_pending_confirmations SET used_by=? WHERE challenge=?",
                         (origin.message_id, match[2]))
        return result

    def _offer_manual(self, session, origin, args, op_id):
        from datetime import timedelta
        session.require("write")
        instant = datetime.fromisoformat(args["at"].replace("Z", "+00:00"))
        if instant.utcoffset() is None or instant <= self.clock():
            raise CareError("Reminder needs an exact future instant")
        challenge = hashlib.sha256((op_id + ":manual").encode()).hexdigest()[:16]
        with self._host_connection(session) as conn:
            conn.execute("INSERT OR IGNORE INTO care_pending_manual VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
                (challenge, origin.actor_id, origin.conversation_id, origin.platform, _dump(args),
                 (self.clock() + timedelta(hours=1)).isoformat()))
        return {"status": "proposed", **args, "confirmation": {"command": "CONFERMO PROMEMORIA " + challenge},
                "meaning": "Mostra testo e orario esatti; nessun promemoria è ancora attivo."}

    def _confirm_manual(self, session, origin, challenge):
        if not self.delivery_available(origin):
            raise CareError("Reminder delivery has not been enabled")
        with self._host_connection(session) as conn:
            row = conn.execute("SELECT * FROM care_pending_manual WHERE challenge=?", (challenge,)).fetchone()
        if (not row or (row["actor_id"], row["conversation_id"], row["platform"]) !=
                (origin.actor_id, origin.conversation_id, origin.platform)
                or datetime.fromisoformat(row["expires_at"]) < self.clock()
                or row["used_by"] not in (None, origin.message_id)):
            raise AuthorizationError("Invalid original reminder confirmation")
        if row["result_json"]:
            return json.loads(row["result_json"])
        args = json.loads(row["payload_json"])
        result = self._reminders(session).create_manual_reminder(message=args["message"],
            at=datetime.fromisoformat(args["at"].replace("Z", "+00:00")), request_ref="confirmed:" + challenge)
        with self._host_connection(session) as conn:
            conn.execute("UPDATE care_pending_manual SET used_by=?, result_json=? WHERE challenge=?",
                         (origin.message_id, _dump(result), challenge))
        return result
