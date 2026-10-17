"""Native health delegation and child-only Apollo action boundary.

Hermes owns the model loop, child session and completion queue. This module owns
the selected medical source, scoped job receipts and authority for each action.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import threading
import uuid
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator

from .care_jobs import CareJobs
from .care_specialist import ACTION_SCHEMAS, NATIVE_INSTRUCTIONS
from .care_store import AuthorizationError, ConflictError


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _interrupted(child):
    value = getattr(child, "is_interrupted", True)
    return bool(value() if callable(value) else value)


ACTION_PARAMETERS = {"oneOf": [
    {"type": "object", "properties": {"action_id": {"type": "string", "pattern": "^[a-zA-Z0-9_-]{1,48}$"},
        "op": {"const": op}, "args": schema},
     "required": ["action_id", "op", "args"], "additionalProperties": False}
    for op, schema in ACTION_SCHEMAS.items()]}
FINISH_PARAMETERS = {"type": "object", "properties": {
    "question": {"type": "string", "maxLength": 1200}}, "additionalProperties": False}
READ_OPERATIONS = {"list_categories", "get_entry", "list_entries", "get_case", "list_cases",
                   "report", "get_plan", "list_upcoming", "get_document"}


class HealthDispatcher:
    def __init__(self, ctx, runtime, *, runner=None):
        self.ctx, self.runtime = ctx, runtime
        self.jobs = CareJobs(runtime)
        self._runner = runner
        self._launches = {}
        self._children = {}
        self._cancelled = set()
        self._lock = threading.RLock()

    @property
    def runner(self):
        if self._runner is None:
            from .care_native import NativeHealthRunner
            self._runner = NativeHealthRunner(self.ctx, on_start=self._started,
                on_finish=self._finished, instructions=NATIVE_INSTRUCTIONS)
        return self._runner

    def _started(self, job_id, child):
        with self._lock:
            origin, launch_id = self._launches[job_id]
            self.jobs.bind_child(origin, job_id, launch_id,
                child_run_id=child._subagent_id, child_session_id=child.session_id)
            self._children[id(child)] = (child, job_id, origin, launch_id)
            child._care_authorize = lambda: self._child(child)

    def _child(self, child):
        with self._lock:
            value = self._children.get(id(child))
        if value is None or value[0] is not child:
            raise AuthorizationError("Not the host-created health specialist")
        _, job_id, origin, launch_id = value
        if job_id in self._cancelled:
            raise AuthorizationError("Health specialist cancelled")
        self.jobs.authorize_child(origin, job_id, launch_id,
            child_run_id=child._subagent_id, child_session_id=child.session_id)
        if _interrupted(child):
            raise AuthorizationError("Health specialist interrupted")
        return job_id, origin, launch_id

    def _session(self, child):
        job_id, origin, launch_id = self._child(child)
        session = self.runtime.session(origin)
        original_require = session.require
        def require(permission):
            # Do not open another Apollo write/migration connection inside an
            # existing action transaction. Durable state was checked at entry;
            # local cancellation and native interruption guard every boundary.
            if job_id in self._cancelled or _interrupted(child):
                raise AuthorizationError("Health specialist interrupted")
            original_require(permission)
        session.require = require
        return job_id, origin, launch_id, session

    @staticmethod
    def _schema(connection):
        connection.execute("""CREATE TABLE IF NOT EXISTS care_job_actions (
            job_id TEXT NOT NULL, action_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            operation TEXT NOT NULL, state TEXT NOT NULL, result_json TEXT,
            PRIMARY KEY(job_id,action_id))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS care_job_finishes (
            job_id TEXT PRIMARY KEY, question TEXT)""")

    def dispatch(self, origin, request, *, parent_agent):
        receipt = self.jobs.admit(origin)
        job_id = receipt["job_id"]
        if receipt["duplicate"]:
            return {**receipt, "outcome": "existing", "result": self.status(origin, job_id)}
        launch_id = uuid.uuid4().hex
        self.jobs.claim_launch(origin, job_id, launch_id)
        with self._lock:
            self._launches[job_id] = (origin, launch_id)
        session = self.runtime.session(origin)
        context = self.runtime._context(session, origin)
        context.update(original_message=origin.text, received_at=origin.received_at,
            timezone=self.runtime.timezone_name,
            received_local=datetime.fromisoformat(origin.received_at.replace("Z", "+00:00")).astimezone(
                ZoneInfo(self.runtime.timezone_name)).isoformat(),
            routing_hint=request, attachments=list(origin.attachment_refs))
        previous = self.jobs.list(origin, limit=5)
        context["previous_health_requests"] = []
        previous_budget = 4000
        for row in previous:
            if row["job_id"] == job_id:
                continue
            item = {"state": row["state"], "question": row.get("result", {}).get("question"),
                    "original_message": self.jobs.source(origin, row["job_id"]).text}
            size = len(_dump(item).encode())
            if size <= previous_budget:
                context["previous_health_requests"].append(item)
                previous_budget -= size
        for key in ("recent_entries", "cases", "categories", "previous_questions", "previous_health_requests"):
            while len(_dump(context).encode()) > 32000 and context.get(key):
                context[key].pop(0)
                context["context_omitted"] = True
        if len(_dump(context).encode()) > 32000:
            self.jobs.complete(origin, job_id, launch_id, outcome="awaiting_user",
                summary="Messaggio troppo lungo", result={"question": "Dividi il messaggio in due parti più brevi."},
                question="Dividi il messaggio in due parti più brevi.")
            with self._lock:
                self._launches.pop(job_id, None)
            return self.status(origin, job_id)
        try:
            submitted = self.runner.dispatch(parent_agent, session_key=origin.session_key,
                goal="Gestisci la richiesta salute ammessa dall'host. Usa gli strumenti health_* e termina con health_finish.",
                context=_dump(context), run_id=job_id)
        except Exception:
            # A constructor/SDK failure is never evidence of clinical success.
            self.jobs.complete(origin, job_id, launch_id, outcome="unknown",
                summary="Avvio non completato", result={"code": "native_start_unavailable"})
            with self._lock:
                self._launches.pop(job_id, None)
            raise
        if submitted.get("status") != "dispatched":
            self.jobs.complete(origin, job_id, launch_id, outcome="failed",
                summary="Specialista occupato", result={"code": "native_dispatch_rejected"})
            with self._lock:
                self._launches.pop(job_id, None)
                for key, value in list(self._children.items()):
                    if value[1] == job_id:
                        self._children.pop(key, None)
            return {"outcome": "busy", "job_id": job_id, "state": "failed"}
        return {"outcome": "delegated", "job_id": job_id, "state": "running",
                "delegation_id": submitted["delegation_id"],
                "message": "Lo specialista salute è al lavoro; il risultato tornerà qui tramite Casita."}

    def action(self, args, *, parent_agent=None, **kwargs):
        try:
            Draft202012Validator(ACTION_PARAMETERS).validate(args)
            if len(_dump(args).encode()) > 16000:
                raise ValueError("Action exceeds bound")
            job_id, origin, _, session = self._session(parent_agent)
            fingerprint = hashlib.sha256(_dump({"op": args["op"], "args": args["args"]}).encode()).hexdigest()
            with self.runtime._request_lock(origin):
                self._child(parent_agent)
                with self.runtime._host_connection(session) as connection:
                    self._schema(connection)
                    if connection.execute("SELECT 1 FROM care_job_finishes WHERE job_id=?", (job_id,)).fetchone():
                        raise AuthorizationError("Specialist already finished")
                    old = connection.execute("SELECT fingerprint,state,result_json FROM care_job_actions WHERE job_id=? AND action_id=?",
                        (job_id, args["action_id"])).fetchone()
                    if old:
                        if old[0] != fingerprint:
                            raise ConflictError("Action ID reused with different input")
                        if old[1] != "done":
                            raise ConflictError("Uncertain action requires reconciliation")
                    else:
                        count = connection.execute("SELECT COUNT(*) FROM care_job_actions WHERE job_id=?", (job_id,)).fetchone()[0]
                        uncertain = connection.execute("SELECT 1 FROM care_job_actions WHERE job_id=? AND state!='done' LIMIT 1", (job_id,)).fetchone()
                        if count >= 64 or (uncertain and args["op"] not in READ_OPERATIONS):
                            raise ConflictError("Job action limit or unresolved side effect")
                        connection.execute("INSERT INTO care_job_actions VALUES (?,?,?,?,?,NULL)",
                            (job_id, args["action_id"], fingerprint, args["op"], "prepared"))
                if old:
                    session.require("read")
                    return old[2]
                op_id = job_id + ":native:" + args["action_id"]
                result = self.runtime._execute(session, origin, args["op"], args["args"], op_id)
                result = self.runtime._bound_output(session, origin, result, op_id)
                session.require("read")
                with self.runtime._host_connection(session) as connection:
                    connection.execute("UPDATE care_job_actions SET state='done', result_json=? WHERE job_id=? AND action_id=?",
                        (_dump(result), job_id, args["action_id"]))
                self.runtime.on_change()
                return _dump(result)
        except Exception:
            return _dump({"status": "refused", "code": "health_action_not_completed",
                          "message": "Azione non confermata. Non dichiararla riuscita e non ripeterla con un altro action_id."})

    def finish(self, args, *, parent_agent=None, **kwargs):
        try:
            Draft202012Validator(FINISH_PARAMETERS).validate(args)
            job_id, origin, _, session = self._session(parent_agent)
            with self.runtime._request_lock(origin), self.runtime._host_connection(session) as connection:
                self._schema(connection)
                connection.execute("INSERT OR IGNORE INTO care_job_finishes VALUES (?,?)", (job_id, args.get("question")))
            return _dump({"status": "finished", "message": "Termina il turno. Casita riceverà solo le ricevute verificate."})
        except Exception:
            return _dump({"status": "refused", "code": "health_finish_refused"})

    def _receipts(self, origin, job_id):
        session = self.runtime.session(origin)
        session.require("read")
        with session.connection() as connection:
            # A newly admitted job may have no action table yet.
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='care_job_actions'").fetchone()
            rows = connection.execute("SELECT operation,state,result_json FROM care_job_actions WHERE job_id=? ORDER BY rowid", (job_id,)).fetchall() if exists else []
            finished = connection.execute("SELECT question FROM care_job_finishes WHERE job_id=?", (job_id,)).fetchone() if exists else None
        return [{"operation": row[0], "state": row[1],
                 "result": json.loads(row[2]) if row[2] else {"status": "unknown"}} for row in rows], finished

    def _finished(self, job_id, native_result):
        origin, launch_id = self._launches[job_id]
        try:
            state = self.jobs.get(origin, job_id)["state"]
            if native_result.get("status") == "interrupted" and state == "running":
                self.jobs.cancel(origin, job_id)
                state = "cancel_requested"
            if state in {"cancel_requested", "cancelled"}:
                self.jobs.ack_cancelled(origin, job_id, launch_id)
                return {"status": "cancelled", "summary": "Attività salute annullata. Nessun risultato tardivo da pubblicare."}
            receipts, finished = self._receipts(origin, job_id)
            question = finished[0] if finished else None
            outcome = "awaiting_user" if question else "completed"
            if finished is None or any(r["state"] != "done" for r in receipts):
                outcome = "unknown"
            if native_result.get("status") not in {"completed", "success", "succeeded"}:
                outcome = "unknown"
            result = {"receipts": receipts, "question": question,
                      "verified_finish": finished is not None, "clinical_inference": False}
            if len(_dump(result).encode()) > 15000:
                full = self.runtime._bound_output(self.runtime.session(origin), origin, result, job_id + ":native-final")
                result = {"full_result": full, "question": question, "receipts_omitted": True}
            committed = self.jobs.complete(origin, job_id, launch_id, outcome=outcome,
                summary="Richiesta salute: " + outcome, result=result,
                question=question if outcome == "awaiting_user" else None)
            if not committed.get("published"):
                if committed.get("status") == "suppressed_cancelled":
                    self.jobs.ack_cancelled(origin, job_id, launch_id)
                return {"status": "error", "summary": "Risultato salute soppresso: autorizzazione o stato cambiato."}
            # Never publish the model's free-form claim or full child transcript.
            # The native queue can outlive a permission grant. Carry no diary,
            # question, confirmation token or report in its durable wakeup.
            # Parent status retrieval rechecks the CURRENT host-bound route.
            summary = {"health_job_id": job_id, "state": outcome,
                       "instruction": "Usa consult_health action=status con questo job_id prima di rispondere. Riferisci solo ricevute confermate; se l'accesso è negato non pubblicare dettagli."}
            return {"status": "completed" if outcome in {"completed", "awaiting_user"} else "error", "summary": _dump(summary)}
        except Exception:
            # Revoked origin or stale generation: no clinical result enters a
            # parent wakeup, even if the child happened to finish successfully.
            return {"status": "error", "summary": "Risultato salute non pubblicabile: autorizzazione o stato cambiato. Nessuna riuscita confermata."}
        finally:
            with self._lock:
                for key, value in list(self._children.items()):
                    if value[1] == job_id:
                        self._children.pop(key, None)
                self._launches.pop(job_id, None)
                self._cancelled.discard(job_id)

    def status(self, origin, job_id):
        result = self.jobs.get(origin, job_id)
        receipts, finished = self._receipts(origin, job_id)
        # No raw route, source message or child transcript returns to Casita.
        public = {k: result[k] for k in ("job_id", "state")}
        public.update(receipts=receipts, question=finished[0] if finished else result.get("result", {}).get("question"))
        if len(_dump(public).encode()) > 16000:
            public = {**public, "receipts": [self.runtime._result_reference(r["result"]) for r in receipts], "receipts_omitted": True}
        self.jobs.get(origin, job_id)  # current reader AND original source, after assembly
        return public

    def cancel(self, origin, job_id):
        result = self.jobs.cancel(origin, job_id)
        with self._lock:
            if job_id in self._launches:
                self._cancelled.add(job_id)
        if self._runner is not None:
            self.runner.cancel(job_id, reason="Richiesta della persona autorizzata")
        return {"job_id": job_id, "state": result["state"]}

    def close(self):
        with self._lock:
            active = list(self._launches.items())
        for job_id, (origin, launch_id) in active:
            with self._lock:
                self._cancelled.add(job_id)
            try:
                state = self.jobs.get(origin, job_id)["state"]
                if state in {"queued", "running"}:
                    self.jobs.cancel(origin, job_id)
            except Exception:
                pass
            finally:
                if self._runner is not None:
                    self.runner.cancel(job_id, reason="Health plugin unloading")
