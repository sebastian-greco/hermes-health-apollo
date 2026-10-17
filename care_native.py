"""Pinned Hermes native child execution for the isolated health specialist.

Compatibility shim for Hermes d923a5eb487a17c0a68801bba9ef0f56c81040a8.
Construction/lineage follows its MIT-licensed tools/delegate_tool.py; execution,
interrupts, transcripts, progress, credentials and asynchronous delivery remain
Hermes-owned. No model loop or background executor is implemented here.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import suppress
import hashlib
import json
import re
import threading
from typing import Any, Callable
import uuid
import weakref


PINNED_HERMES = "d923a5eb487a17c0a68801bba9ef0f56c81040a8"
_LIVE = frozenset({"prepared", "running"})


@dataclass
class _Run:
    fingerprint: str
    child: Any = None
    state: str = "prepared"
    handle: dict | None = None
    cancelled: bool = False


def _bounded_text(value: Any, name: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value.encode("utf-8")) > limit:
        raise ValueError(f"Invalid or oversized {name}")
    return value


class NativeHealthRunner:
    """Host-only launcher. Callbacks and child identity are never model arguments.

    ``on_start(run_id, child)`` binds the exact native object before submission.
    ``on_finish(run_id, native_result)`` must return a bounded canonical receipt
    (at least a summary), derived from verified actions rather than child prose.
    The root service remains responsible for fresh actor/patient grants on every
    action. ``authorize_child`` adds exact-object/session/task identity checks.
    """

    def __init__(self, ctx=None, *, on_start: Callable, on_finish: Callable | None = None,
                 instructions: str, toolset: str = "casita_health_specialist", max_iterations: int = 12):
        self.ctx = ctx
        self.on_start, self.on_finish = on_start, on_finish
        self.instructions = _bounded_text(instructions, "instructions", 32_000)
        if toolset != "casita_health_specialist":
            raise ValueError("The native health catalog must be casita_health_specialist")
        if type(max_iterations) is not int or not 1 <= max_iterations <= 32:
            raise ValueError("Invalid child iteration limit")
        self.toolset, self.max_iterations = toolset, max_iterations
        self._lock = threading.RLock()
        self._runs: dict[str, _Run] = {}

    def dispatch(self, parent_agent, *, session_key: str, goal: str, context: str | None, run_id: str) -> dict:
        """Build a genuine child on the caller thread; return native async handle.

        A rejected launch never falls back to inline execution. The clinical
        context is absent from the native async ledger and parent progress.
        """
        _bounded_text(session_key, "session key", 512)
        _bounded_text(goal, "goal", 12_000)
        if context is not None:
            _bounded_text(context, "context", 64_000, empty=True)
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
            raise ValueError("Invalid run ID")
        if parent_agent is None or not getattr(parent_agent, "session_id", None):
            raise ValueError("Native parent session is required")
        fingerprint = hashlib.sha256(json.dumps([session_key, parent_agent.session_id, goal, context]).encode()).hexdigest()
        with self._lock:
            old = self._runs.get(run_id)
            if old:
                if old.fingerprint != fingerprint:
                    raise ValueError("Run ID reused for a different request")
                return dict(old.handle or {"status": old.state, "run_id": run_id})
            terminal = [key for key, value in self._runs.items() if value.state not in _LIVE | {"finishing"}]
            for key in terminal[:max(0, len(self._runs) - 255)]:
                self._runs.pop(key, None)
            record = self._runs[run_id] = _Run(fingerprint)

        # Capture native origin before constructing any AIAgent changes session
        # state. The native lane copies request/profile/secret ContextVars.
        from tools.delegate_tool_dispatch import _capture_origin
        from tools.delegate_tool_child_run import _detach_child
        from tools.async_delegation import dispatch_async_delegation
        origin_sid, origin_ui, owner_transport, owner_record, _history = _capture_origin()
        neutral_goal = f"Health request {run_id}"
        try:
            child = self._build(parent_agent, neutral_goal, context)
            with self._lock:
                record.child = child
            self.on_start(run_id, child)
            if not callable(getattr(child, "_care_authorize", None)):
                raise RuntimeError("Host did not bind fresh health authority")
            if record.cancelled or getattr(child, "_interrupt_requested", False):
                raise RuntimeError("Native parent was interrupted before launch")
            # Background children outlive their parent's ordinary turn teardown;
            # the native async registry owns /stop and session-end cancellation.
            _detach_child(parent_agent, child)
            handle = dispatch_async_delegation(
                goal=neutral_goal, context=None, toolsets=[self.toolset], role="leaf", model=child.model,
                session_key=session_key, parent_session_id=parent_agent.session_id,
                origin_ui_session_id=origin_ui, origin_session_id=origin_sid,
                runner=lambda: self._execute(run_id, record, parent_agent, goal, origin_ui,
                                             owner_transport, owner_record),
                interrupt_fn=lambda: self.cancel(run_id), progress_fn=lambda: self._progress(child),
                max_async_children=self._capacity(),
            )
            handle = {**handle, "run_id": run_id, "subagent_id": child._subagent_id,
                      "child_session_id": child.session_id}
            with self._lock:
                record.handle = handle
                if handle.get("status") != "dispatched":
                    record.state, record.cancelled = "rejected", True
                child._progress_identity_ref["delegation_id"] = handle.get("delegation_id", "")
            if handle.get("status") != "dispatched":
                with suppress(Exception):
                    child.close()
                with self._lock:
                    record.child = None
            return dict(handle)
        except Exception:
            with self._lock:
                record.state, record.cancelled = "rejected", True
                record.handle = {"status": "rejected", "run_id": run_id, "error": "Native health launch failed"}
            if record.child is not None:
                _detach_child(parent_agent, record.child)
                with suppress(Exception):
                    record.child.close()
                with self._lock:
                    record.child = None
            # Exception text can contain provider configuration or prompt text.
            return dict(record.handle)

    @staticmethod
    def _capacity() -> int:
        from tools.delegate_tool_config import _get_max_async_children
        return _get_max_async_children()

    @staticmethod
    def _progress(child) -> tuple:
        summary = child.get_activity_summary()
        tool = summary.get("current_tool")
        return ((summary.get("api_call_count", 0), tool, summary.get("last_activity_ts")),), bool(tool)

    def _build(self, parent, neutral_goal: str, context: str | None):
        from agent.delegation_context import delegated_child_context
        from hermes_cli.config import load_config_readonly
        from run_agent import AIAgent
        import model_tools
        from tools import delegate_tool as native
        from tools.delegate_tool_results import _CHILD_CONSTRUCTION_LOCK

        cfg = load_config_readonly()
        if native._get_worktree_isolation():
            raise ValueError("Health children require delegation.worktree_isolation disabled")
        if (cfg.get("context", {}) or {}).get("engine", "compressor") not in (None, "", "compressor"):
            raise ValueError("Health children require the built-in context engine")

        # Only this real child overrides prompt assembly. Invalidation and native
        # compaction rebuild the same own persona, never host plugin/house context.
        own_instructions = self.instructions

        class HealthAgent(AIAgent):
            def _build_system_prompt(self, system_message=None):
                self._cached_system_prompt_static = own_instructions
                return own_instructions

            def _authorize_inference(self):
                authorize = getattr(self, "_care_authorize", None)
                try:
                    if not callable(authorize) or authorize() is False:
                        raise PermissionError("No current host health authority")
                except Exception:
                    # Ordinary PermissionError is classified as a provider
                    # failure by the native retry ladder. Revoke and interrupt
                    # first so it never waits/retries a withdrawn health grant.
                    from tools.delegate_tool_child_run import _signal_child_stop
                    _signal_child_stop(self, "Health authority unavailable", tool_reason="Health authority unavailable")
                    raise InterruptedError("Health authority unavailable") from None

            def _interruptible_api_call(self, *args, **kwargs):
                self._authorize_inference()
                return super()._interruptible_api_call(*args, **kwargs)

            def _interruptible_streaming_api_call(self, *args, **kwargs):
                self._authorize_inference()
                return super()._interruptible_streaming_api_call(*args, **kwargs)

            def _compress_context(self, *args, **kwargs):
                # Native compression calls auxiliary_client directly, potentially
                # using a different configured provider and bypassing this turn's
                # authority callback. Stop this bounded run instead of exporting
                # clinical context through an unguarded auxiliary request.
                self._authorize_inference()
                from tools.delegate_tool_child_run import _signal_child_stop
                _signal_child_stop(self, "Health context budget exhausted", tool_reason="Health context budget exhausted")
                raise InterruptedError("Health child context budget exhausted")

        parent_key = getattr(parent, "api_key", None) or getattr(parent, "_client_kwargs", {}).get("api_key")
        rt = native._resolve_child_runtime(
            parent, {}, parent_key, model=None, override_provider=None, override_base_url=None,
            override_api_key=None, override_api_mode=None, override_acp_command=None,
            override_acp_args=None, routing_cfg={},
        )
        subagent_id = f"sa-health-{uuid.uuid4().hex[:12]}"
        parent_id = getattr(parent, "_subagent_id", None)
        depth = getattr(parent, "_delegate_depth", 0) + 1
        session_ref: dict[str, Any] = {}
        relay = native._build_child_progress_callback(
            0, neutral_goal, parent, 1, subagent_id=subagent_id, parent_id=parent_id,
            depth=max(0, depth - 1), model=rt["model"], toolsets=[self.toolset], session_ref=session_ref,
        )

        def progress(event_type, tool_name=None, preview=None, args=None, **kwargs):
            # Native events can include thinking, freeform output, tool arguments
            # and clinical previews. Relay identity/activity metadata only.
            if relay is None or str(event_type) in {"_thinking", "subagent.text"}:
                return
            metadata = {key: kwargs[key] for key in ("status", "duration_seconds", "api_calls", "cost_usd") if key in kwargs}
            relay(event_type, tool_name if tool_name == "health_action" else None,
                  preview="Health specialist", args=None, **metadata)

        progress._flush = getattr(relay, "_flush", lambda: None)
        db = native._open_child_session_db(parent)
        try:
            with _CHILD_CONSTRUCTION_LOCK:
                saved_tools = list(model_tools._last_resolved_tool_names)
                try:
                    with delegated_child_context():
                        child = HealthAgent(
                            **rt, max_iterations=self.max_iterations, prefill_messages=None,
                            enabled_toolsets=[self.toolset], disabled_toolsets=["delegation", "memory"],
                            quiet_mode=True, ephemeral_system_prompt=context or None,
                            platform="subagent", side_agent=True, skip_context_files=True,
                            load_soul_identity=False, skip_memory=True, skip_background_review=True,
                            clarify_callback=None, session_db=db, parent_session_id=parent.session_id,
                            request_overrides=dict(getattr(parent, "request_overrides", {}) or {}),
                            tool_progress_callback=progress, iteration_budget=None,
                        )
                        # Hermes' global tool-search preference can replace this
                        # finite catalog with discovery bridges. Select native
                        # raw schemas for this child only, without changing the
                        # parent's schemas or the profile's tool-search config.
                        child.tools = model_tools.get_tool_definitions(
                            enabled_toolsets=[self.toolset], disabled_toolsets=child.disabled_toolsets,
                            quiet_mode=True, skip_tool_search_assembly=True,
                        )
                        child.valid_tool_names = {tool["function"]["name"] for tool in child.tools}
                        child._skip_mcp_refresh = True
                finally:
                    model_tools._last_resolved_tool_names = saved_tools
        except BaseException:
            if db is not None:
                from hermes_state_registry import release_or_close
                release_or_close(db)
            raise
        child._owns_session_db = db is not None
        try:
            if not child.valid_tool_names or any(
                model_tools.get_toolset_for_tool(name) != self.toolset for name in child.valid_tool_names
            ):
                raise ValueError("Unexpected specialist tool catalog")
            child._delegate_saved_tool_names = saved_tools
            child._delegate_depth, child._delegate_role = depth, "leaf"
            child._subagent_id, child._parent_subagent_id = subagent_id, parent_id
            child._delegate_parent_ref = weakref.ref(parent)
            child._print_fn = getattr(parent, "_print_fn", None)
            session_ref["session_id"] = child.session_id
            child._progress_identity_ref = session_ref
            if getattr(child, "_session_init_model_config", None) is not None:
                child._session_init_model_config["_delegate_from"] = parent.session_id
            native._apply_child_cache_ttl(child)
            native._apply_child_compression_cap(child, {})
            pool = native._resolve_child_credential_pool(
                rt["provider"], parent, rt["base_url"], effective_requested_provider=rt.get("requested_provider"))
            if pool is not None:
                child._credential_pool = pool
            native._attach_child(parent, child)
            native._safe_progress(progress, "subagent.spawn_requested", preview=neutral_goal)
            from hermes_cli.lifecycle import invoke_hook
            invoke_hook("subagent_start", parent_session_id=parent.session_id,
                        parent_turn_id=getattr(parent, "_current_turn_id", "") or "",
                        parent_subagent_id=parent_id, child_session_id=child.session_id,
                        child_subagent_id=subagent_id, child_role="leaf", child_goal=neutral_goal)
            return child
        except BaseException:
            from tools.delegate_tool_child_run import _detach_child
            _detach_child(parent, child)
            child.close()
            raise

    def _execute(self, run_id, record, parent, goal, origin_ui, owner_transport, owner_record):
        from tools.delegate_tool import _run_single_child
        from tools.delegate_tool_results import (
            _apply_summary_budget, _fire_subagent_stop_hooks, _parent_finalization_lock, _rollup_children_cost,
        )
        child = record.child
        with self._lock:
            record.state = "running"
        try:
            raw = _run_single_child(0, goal, child, parent, owner_session_id=origin_ui or None,
                                    owner_transport=owner_transport, owner_session_record=owner_record)
        except Exception:
            # The SDK catches conversation errors; setup failures before its
            # finally block must also revoke actions and avoid an exception dump
            # being persisted by the native async crash-result fallback.
            from tools.delegate_tool_child_run import _detach_child
            _detach_child(parent, child)
            with suppress(Exception):
                child.close()
            raw = {"status": "error", "api_calls": 0, "duration_seconds": 0}
        with self._lock:
            record.state = "finishing"
        try:
            canonical = self.on_finish(run_id, raw) if self.on_finish is not None else None
            if canonical is None:
                canonical = {"summary": f"Health request {run_id} finished; inspect its verified receipts."}
            if not isinstance(canonical, dict):
                raise ValueError("Invalid canonical completion")
            summary = _bounded_text(canonical.get("summary"), "completion summary", 4_000)
            status = canonical.get("status", raw.get("status", "error"))
            if status == "cancelled":
                status = "interrupted"
            if status not in {"completed", "interrupted", "failed", "error", "unknown"}:
                status = "error"
        except Exception:
            status, summary = "error", f"Health request {run_id} ended; verified completion is unavailable."
        if record.cancelled:
            status = "interrupted"
        result = {"task_index": 0, "status": status, "summary": summary, "run_id": run_id,
                  "child_session_id": child.session_id, "subagent_id": child._subagent_id,
                  "api_calls": raw.get("api_calls", 0), "duration_seconds": raw.get("duration_seconds", 0),
                  "_child_role": "leaf", "_child_cost_usd": raw.get("_child_cost_usd", 0)}
        try:
            with _parent_finalization_lock(parent):
                _apply_summary_budget([result], parent)
                _rollup_children_cost(parent, _fire_subagent_stop_hooks([result], {0: child}, parent))
                # Intentionally omit native _notify_memory_manager: sensitive health
                # delegation receipts must not become general persistent memory.
        except Exception:
            # Canonical completion must still settle the native lane if an
            # observability hook fails. Never publish raw child result fields.
            result.pop("_child_role", None)
            result.pop("_child_cost_usd", None)
        with self._lock:
            record.state = status
            record.child = None
        return result

    def authorize_child(self, run_id: str, *, child=None, session_id=None, task_id=None) -> bool:
        """Validate HOST-derived current agent plus optional native session/task IDs."""
        with self._lock:
            record = self._runs.get(run_id)
            if not record or record.state not in _LIVE or record.cancelled or child is not record.child:
                return False
            if session_id is not None and session_id != record.child.session_id:
                return False
            if task_id is not None and task_id != record.child._subagent_id:
                return False
            return True

    def cancel(self, run_id: str, reason: str = "Health request cancelled") -> bool:
        from tools.delegate_tool_child_run import _signal_child_stop
        with self._lock:
            record = self._runs.get(run_id)
            if not record or record.state not in _LIVE:
                return False
            record.cancelled = True  # revoke actions before cooperative interruption
            child = record.child
        _signal_child_stop(child, "Health request cancelled", tool_reason="Health request cancelled")
        return True

    def status(self, run_id: str) -> dict | None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return None
            return {**(record.handle or {}), "run_id": run_id, "status": record.state,
                    "cancelled": record.cancelled}
