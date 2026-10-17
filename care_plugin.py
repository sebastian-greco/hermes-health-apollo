"""Hermes 0.22.2 native hook/tool bridge for opt-in Casita care mode."""
from __future__ import annotations

from datetime import UTC, datetime
import json
from contextvars import copy_context
import threading
from jsonschema.exceptions import ValidationError

from .care_runtime import Binding, CareRuntime, Origin


PARAMETERS = {"type": "object", "properties": {
    "request": {"type": "string", "maxLength": 1200,
                "description": "Breve indicazione per lo specialista; userà il messaggio originale autorizzato."},
    "action": {"enum": ["consult", "status", "cancel"], "default": "consult"},
    "job_id": {"type": "string", "maxLength": 128}},
    "required": ["request"], "additionalProperties": False}

active_runtime = None  # profile-scoped native module; diagnostic host handle only
active_dispatcher = None


def enabled(ctx):
    getter = getattr(ctx, "get_config", None)
    return callable(getter) and getter("care.enabled", False) is True


def register(ctx):
    global active_runtime, active_dispatcher
    def settings():
        value = ctx.get_config("care", {})
        return value if isinstance(value, dict) else {}

    def bindings():
        config = settings()
        if config.get("enabled") is not True:
            return []
        routes = config.get("bindings", [])
        if not isinstance(routes, list) or len(routes) > 100:
            raise ValueError("Invalid trusted care bindings")
        result = []
        for route in routes:
            if not isinstance(route, dict) or set(route) != {
                    "platform", "actor_id", "conversation_id", "scope_id", "permissions"}:
                raise ValueError("Invalid trusted care binding")
            if not isinstance(route["permissions"], list):
                raise ValueError("Invalid trusted care permissions")
            result.append(Binding(**{**route, "permissions": frozenset(route["permissions"])}))
            result[-1].authority()
        return result

    def document_factory(session):
        from .care_documents import DocumentWorkflow, GoogleDriveAdapter
        scoped = settings().get("drive", {}).get(session.authority.scope_id)
        drive = None
        if scoped:
            from .care_drive_auth import DriveOAuth
            # The host-managed protected client file is operator configured.
            # It is never included in specialist context or tool arguments.
            auth = DriveOAuth.from_config(session, scoped, runtime_root=runtime.root)
            def token_provider():
                # Recheck this exact original route for each upload/reconcile
                # request, including multi-file or resumable transfers.
                session.require("write")
                current = settings().get("drive", {}).get(session.authority.scope_id)
                if current != scoped:
                    raise PermissionError("Drive binding changed")
                return auth.token_provider()
            def drive_current():
                session.require("read")
                session.require("write")
                return settings().get("drive", {}).get(session.authority.scope_id) == scoped
            drive = GoogleDriveAdapter(folder_id=scoped["folder_id"], token_provider=token_provider,
                                      authorize_current=drive_current)
        return DocumentWorkflow(session, runtime_root=runtime.root / "documents", drive=drive)

    runtime = CareRuntime(ctx.state.data_dir / "care", bindings=bindings, llm=ctx.llm,
                          documents=document_factory, timezone_name=settings().get("timezone", "Europe/Rome"))
    active_runtime = runtime
    workers = []
    wakes = []
    scope_workers = {}
    runtime.worker_failures = set()

    def delivery_available(origin):
        try:
            scope_id = runtime._binding(origin).scope_id
            item = scope_workers.get(scope_id)
            return bool(runtime.reminders_enabled and item and item[0].is_alive()
                        and route_current(item[1].actor_id, scope_id, item[1]))
        except (ValueError, PermissionError):
            return False

    runtime.delivery_available = delivery_available

    def changed():
        for wake in wakes:
            wake.set()

    runtime.on_change = changed

    def stop():
        runtime.reminders_enabled = False
        for thread, stop_event, wake in workers:
            stop_event.set()
            wake.set()
        for thread, _, _ in workers:
            if thread.ident is not None:
                thread.join(timeout=20)

    reminder_config = settings().get("reminders", {})
    if reminder_config.get("enabled") is True:
        expected_ha_url = reminder_config.get("ha_url")
        from .care_egress import HAConversationEgress, TrustedConversation
        from .care_reminders import CareReminderService
        from agent.secret_scope import get_scoped_secret

        def token_provider():
            return get_scoped_secret("WAHA_HA_TOKEN", "")

        def route_current(actor, scope, route):
            current = settings().get("reminders", {})
            if (current.get("enabled") is not True or current.get("ha_url") != expected_ha_url):
                return False
            candidate = {"actor_id": route.actor_id, "scope_id": route.scope_id,
                         "config_entry_id": route.config_entry_id, "conversation_id": route.conversation_id}
            matches = [r for r in current.get("routes", []) if isinstance(r, dict) and
                       {k: r.get(k) for k in candidate} == candidate]
            return (len(matches) == 1 and
                any(b.platform == "waha" and b.actor_id == actor and b.scope_id == scope
                    and b.conversation_id == matches[0].get("chat_id") and {"read", "write"} <= b.permissions
                    for b in bindings()))

        # One worker per scope. A shared scope has one configured egress route,
        # avoiding duplicate caregiver sends for the same occurrence ledger.
        scopes = set()
        try:
            for raw_route in reminder_config.get("routes", []):
                route = TrustedConversation(**{k: v for k, v in raw_route.items() if k != "chat_id"})
                if route.scope_id in scopes or not route_current(route.actor_id, route.scope_id, route):
                    raise ValueError("Invalid or duplicate reminder route")
                scopes.add(route.scope_id)
                origin = Origin("waha", route.actor_id, raw_route["chat_id"], "worker", "worker", "",
                                datetime.now(UTC).isoformat())
                session = runtime.session(origin)
                service = CareReminderService(session, authorize_current=lambda authority, route=route:
                    route_current(authority.actor_id, authority.scope_id, route))
                egress = HAConversationEgress(ha_url=reminder_config["ha_url"], token_provider=token_provider,
                    route=route, authorize_current=route_current)
                stop_event, wake = threading.Event(), threading.Event()
                worker_context = copy_context()
                def run_worker(service=service, stop_event=stop_event, wake=wake,
                               egress=egress, worker_context=worker_context, scope_id=route.scope_id):
                    try:
                        worker_context.run(service.run_forever, stop_event, wake, egress)
                    except Exception:
                        # Record safe health only; no thread traceback of account
                        # responses or patient records in the shared app log.
                        runtime.worker_failures.add(scope_id)
                thread = threading.Thread(target=run_worker, daemon=True, name="casita-care-reminders")
                wakes.append(wake)
                workers.append((thread, stop_event, wake))
                scope_workers[route.scope_id] = (thread, route)
            for thread, _, _ in workers:
                thread.start()
            runtime.reminders_enabled = bool(workers)
        except Exception:
            stop()
            raise
        ctx.on_unload(stop)
    # Deliberately not a generic file-path loader: the current WAHA adapter is
    # text-only. A trusted media host must supply bounded byte attachments.

    async def admitted(session_key="", platform="", source=None, message_id="", text="", **kwargs):
        if not isinstance(source, dict):
            return None
        try:
            origin = Origin(platform=platform, actor_id=source.get("user_id", ""),
                conversation_id=source.get("chat_id", ""), message_id=message_id,
                session_key=session_key, text=text, received_at=datetime.now(UTC).isoformat())
            runtime.admit(origin)
            if origin.text.strip().startswith(("CONFERMO PIANO ", "CONFERMO PROMEMORIA ")):
                result = runtime.confirm_original(origin)
                if result:
                    changed()
                    return {"action": "handled", "reply": "Confermato: promemoria attivato."}
        except (ValueError, PermissionError, KeyError, TypeError, ValidationError):
            # Failed receipt -> later consult fails closed. Never leak raw user
            # text, token transport errors or local paths into Hermes logs.
            if text.strip().startswith(("CONFERMO PIANO ", "CONFERMO PROMEMORIA ")):
                return {"action": "handled", "reply": "Il piano non è stato attivato: conferma scaduta, permesso mancante o servizio promemoria non attivo."}
        return None

    from .care_dispatch import HealthDispatcher, FINISH_PARAMETERS
    from .care_specialist import ACTION_SCHEMAS
    dispatcher = HealthDispatcher(ctx, runtime)
    # A newly registered plugin owns no surviving child capabilities. Native
    # runs do not resume after process/plugin reload; retain committed receipts
    # and mark abandoned work uncertain instead of silently launching it again.
    for scope_id in {binding.scope_id for binding in bindings() if "read" in binding.permissions}:
        dispatcher.jobs.reconcile_scope(scope_id, active_child_ids=())
    active_dispatcher = dispatcher
    ctx.on_unload(dispatcher.close)

    def handler(args, *, parent_agent=None, **kwargs):
        try:
            from agent.subagent_lifecycle import get_active_subagent_parent
            parent_agent = get_active_subagent_parent()
            from jsonschema import Draft202012Validator
            Draft202012Validator(PARAMETERS).validate(args)
            from gateway import session_context as sc
            from agent.delegation_context import is_delegated_child_process_context
            # Require explicitly bound ContextVars, not os.environ fallback.
            if (is_delegated_child_process_context() or not sc.session_context_engaged()
                    or any(var.get() is sc._UNSET for var in
                           (sc._SESSION_KEY, sc._SESSION_PLATFORM, sc._SESSION_USER_ID, sc._SESSION_CHAT_ID))):
                raise PermissionError("No admitted parent turn")
            action = args.get("action", "consult")
            job_id = args.get("job_id")
            if action == "status":
                # Internal native completion turns deliberately have no human
                # admission event. A read may use the host-bound current route,
                # with fresh scope checks; it cannot create a job or mutate data.
                if parent_agent is None:
                    raise PermissionError("No native parent for care status")
                origin = Origin(platform=sc.get_session_env("HERMES_SESSION_PLATFORM"),
                    actor_id=sc.get_session_env("HERMES_SESSION_USER_ID"),
                    conversation_id=sc.get_session_env("HERMES_SESSION_CHAT_ID"),
                    message_id="care-status", text="", received_at=datetime.now(UTC).isoformat(),
                    session_key=sc.get_session_env("HERMES_SESSION_KEY"))
                runtime.session(origin).require("read")
            else:
                if sc._SESSION_MESSAGE_ID.get() is sc._UNSET:
                    raise PermissionError("No admitted human message")
                origin = runtime.admitted(platform=sc.get_session_env("HERMES_SESSION_PLATFORM"),
                    actor_id=sc.get_session_env("HERMES_SESSION_USER_ID"),
                    conversation_id=sc.get_session_env("HERMES_SESSION_CHAT_ID"),
                    message_id=sc.get_session_env("HERMES_SESSION_MESSAGE_ID"),
                    session_key=sc.get_session_env("HERMES_SESSION_KEY"))
            if action == "status":
                result = dispatcher.status(origin, job_id) if job_id else {
                    "jobs": [{k: row[k] for k in ("job_id", "state")} for row in dispatcher.jobs.list(origin, limit=10)]}
            elif action == "cancel":
                if not job_id:
                    raise ValueError("Cancellation requires a job")
                result = dispatcher.cancel(origin, job_id)
            else:
                if parent_agent is None or job_id:
                    raise PermissionError("No native parent agent")
                result = dispatcher.dispatch(origin, args["request"], parent_agent=parent_agent)
            return json.dumps(result, ensure_ascii=False)
        except (ValueError, PermissionError, KeyError, TypeError, ValidationError):
            return json.dumps({"outcome": "refused", "code": "care_request_refused",
                               "message": "Richiesta salute non completata; serve verificare contesto o autorizzazione."})
        except Exception:
            return json.dumps({"outcome": "unavailable", "code": "care_temporarily_unavailable",
                               "message": "Lo specialista non ha completato la richiesta. I dati già salvati restano disponibili."})

    ctx.register_tool(name="consult_health", toolset="casita_health", handler=handler,
        schema={"name": "consult_health", "description":
            "Delega allo specialista salute: diari, sintomi, episodi, pasti, documenti e riepiloghi. "
            "Usa il messaggio originale del turno; non trasferire qui la cronologia di Casita. "
            "Lavora in background e restituisce subito un riferimento: resta disponibile e riferisci il risultato quando arriva. "
            "Status legge risultati/domande o elenca attività; cancel interrompe un job. "
            "Comunica solo risultati salvati/confermati e chiarimenti restituiti.",
            "parameters": PARAMETERS})
    for operation, arguments in ACTION_SCHEMAS.items():
        def native_action(args, *, parent_agent=None, _operation=operation, **kwargs):
            from agent.subagent_lifecycle import get_active_subagent_parent
            parent_agent = get_active_subagent_parent()
            if not isinstance(args, dict) or set(args) != {"action_id", "args"}:
                return json.dumps({"status": "refused", "code": "health_action_not_completed"})
            return dispatcher.action({**args, "op": _operation}, parent_agent=parent_agent)
        name = "health_" + operation
        ctx.register_tool(name=name, toolset="casita_health_specialist", handler=native_action,
            schema={"name": name, "description": "Azione Apollo dello specialista: " + operation + ". Usa un action_id stabile per evitare duplicati.",
                    "parameters": {"type": "object", "properties": {
                        "action_id": {"type": "string", "pattern": "^[a-zA-Z0-9_-]{1,48}$"}, "args": arguments},
                        "required": ["action_id", "args"], "additionalProperties": False}})
    def native_finish(args, **kwargs):
        from agent.subagent_lifecycle import get_active_subagent_parent
        return dispatcher.finish(args, parent_agent=get_active_subagent_parent())
    ctx.register_tool(name="health_finish", toolset="casita_health_specialist", handler=native_finish,
        schema={"name": "health_finish", "description": "Termina la richiesta e consegna le ricevute a Casita.",
                "parameters": FINISH_PARAMETERS})
    ctx.register_hook("post_gateway_admission", admitted)
    from .care_cli import register_cli
    register_cli(ctx, runtime, settings)
    # Intentionally explicit diagnostic/integration handle, never a model tool.
    return runtime
