"""Operator-only CLI for the local Casita health workspace.

This module is deliberately not registered as a model tool. It derives every
patient scope from the current host binding and never accepts a scope, actor,
database path, Drive folder, or token from command arguments.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import getpass
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
import uuid


MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_BATCH_BYTES = 60 * 1024 * 1024
ALLOWED_MIME = {"application/pdf", "image/jpeg", "image/png"}
REDIRECT_URI = "http://127.0.0.1:8765/"


class CareCLIError(ValueError):
    """Safe operator-facing error without source contents or credentials."""


def _emit(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _register(ctx, callback):
    try:
        ctx.register_cli_command(
            "health-care", "Manage the local Casita care workspace.",
            lambda parser: setup_parser(parser, callback),
            description="Manage local Casita care data, Google Drive, and reminders.",
        )
    except TypeError:
        # Older Hermes command registration accepts a zero-argument callback.
        ctx.register_cli_command("health-care", lambda: callback(argparse.Namespace(action="status")))


def register_cli(ctx, runtime, settings):
    """Register the standalone operator command with Hermes' native CLI."""
    def callback(args):
        try:
            result = dispatch(args, runtime=runtime, settings=settings)
        except Exception as exc:
            # Do not echo raw exception strings: transport errors can contain
            # callback URLs, paths, health data, or server response bodies.
            _emit({"ok": False, "error": _safe_error(exc)})
            return 2
        _emit({"ok": True, **result})
        return 0

    _register(ctx, callback)
    return callback


def setup_parser(parser, callback):
    parser.set_defaults(func=callback, action="status")
    subs = parser.add_subparsers(dest="action")
    subs.add_parser("status", help="Show safe local configuration status.").set_defaults(func=callback, action="status")

    report = subs.add_parser("report", help="Export a health report to a new private local file.")
    _binding_arg(report)
    report.add_argument("--output", required=True, help="Absolute path for a new report file; existing files are never replaced.")
    selectors = report.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--start", help="Inclusive ISO date or datetime; optionally pair with --end.")
    selectors.add_argument("--episode-id")
    selectors.add_argument("--since-visit-id")
    report.add_argument("--end", help="Exclusive ISO date or datetime; defaults to now.")
    report.add_argument("--issue-id", help="Further limit an episode/date/visit report to one issue.")
    report.set_defaults(func=callback)

    drive = subs.add_parser("drive", help="Manage the configured Google Drive connection.")
    drive_sub = drive.add_subparsers(dest="drive_action", required=True)
    for action in ("status", "begin", "finish", "verify"):
        cmd = drive_sub.add_parser(action)
        _binding_arg(cmd)
        cmd.set_defaults(func=callback)

    docs = subs.add_parser("documents", help="Intake local files into the authorized care record.")
    doc_sub = docs.add_subparsers(dest="document_action", required=True)
    intake = doc_sub.add_parser("intake", help="Import an explicit local PDF or image batch.")
    _binding_arg(intake)
    intake.add_argument("files", nargs="+", help="Local PDF/JPEG/PNG paths, up to the documented batch limit.")
    intake.add_argument("--request-id", help="Stable key for an intentional idempotent replay.")
    intake.add_argument("--prepare", action="store_true", help="Prepare a deterministic filing PDF after intake.")
    intake.add_argument("--upload", action="store_true", help="Upload the prepared result to the configured Drive folder.")
    intake.set_defaults(func=callback)
    prepare = doc_sub.add_parser("prepare", help="Prepare an already-intaken document job.")
    _binding_arg(prepare)
    prepare.add_argument("--job-id", required=True)
    prepare.set_defaults(func=callback)
    upload = doc_sub.add_parser("upload", help="Explicitly upload a previously prepared job.")
    _binding_arg(upload)
    upload.add_argument("--job-id", required=True)
    upload.set_defaults(func=callback)


def _binding_arg(parser):
    parser.add_argument("--binding", type=int, required=True,
                        help="1-based current trusted binding index shown by status.")


def _safe_error(exc):
    # Prefer a stable class-level code over an exception message.
    name = type(exc).__name__
    safe = {
        "AuthorizationError": "authorization_denied", "PermissionError": "authorization_denied",
        "CareError": "care_operation_failed", "ConflictError": "care_conflict",
        "DriveAuthError": "drive_operation_failed", "DriveError": "drive_operation_failed",
        "DocumentError": "document_operation_failed", "FileNotFoundError": "input_unavailable",
        "IsADirectoryError": "input_unavailable", "ValueError": "invalid_input",
        "OSError": "local_io_failed", "TypeError": "invalid_input",
    }
    return safe.get(name, "operation_failed")


def _current_config(settings):
    value = settings() if callable(settings) else settings
    if not isinstance(value, dict) or value.get("enabled") is not True:
        raise CareCLIError("Care mode is not enabled")
    return value


def _select(runtime, settings, number, permission):
    if type(number) is not int or number < 1:
        raise CareCLIError("Choose a valid current binding index")
    bindings = runtime.bindings()
    config = _current_config(settings)
    if len(bindings) != len(config.get("bindings", [])) or number > len(bindings):
        raise CareCLIError("Binding configuration changed; run status again")
    binding = bindings[number - 1]
    if permission not in binding.permissions:
        raise PermissionError("Binding does not grant this operation")
    from .care_runtime import Origin
    origin = Origin(binding.platform, binding.actor_id, binding.conversation_id,
                    "operator-cli", "operator-cli", "", datetime.now(UTC).isoformat())
    session = runtime.session(origin)  # re-resolves the current trusted host binding
    if session.authority.scope_id != binding.scope_id or runtime._binding(origin) != binding:
        raise PermissionError("Binding changed during operator selection")
    session.require(permission)
    return binding, session, config


def status(*, runtime, settings):
    config = _current_config(settings)
    bindings = runtime.bindings()
    drive = config.get("drive", {})
    reminders = config.get("reminders", {})
    return {
        "care_enabled": True,
        "binding_count": len(bindings),
        "bindings": [{"index": index, "platform": b.platform,
                      "read": "read" in b.permissions,
                      "write": "write" in b.permissions,
                      "admin": "admin" in b.permissions}
                     for index, b in enumerate(bindings, 1)],
        "drive_configured_binding_count": sum(1 for b in bindings if b.scope_id in drive),
        "reminders_enabled": runtime.reminders_enabled is True,
        "reminder_route_count": len(reminders.get("routes", [])) if isinstance(reminders, dict) else 0,
        "ha_url_configured": bool(isinstance(reminders, dict) and reminders.get("ha_url")),
        "data_directory_ready": runtime.root.is_dir(),
    }


def dispatch(args, *, runtime, settings):
    action = getattr(args, "action", "status") or "status"
    if action == "status":
        return status(runtime=runtime, settings=settings)
    if action == "report":
        return _report(args, runtime, settings)
    if action == "drive":
        return _drive(args, runtime, settings)
    if action == "documents":
        return _documents(args, runtime, settings)
    raise CareCLIError("Unknown operator action")


def _report(args, runtime, settings):
    _, session, _ = _select(runtime, settings, args.binding, "read")
    from .care_reports import assemble_report
    report = assemble_report(session, start=args.start, end=args.end, episode_id=args.episode_id,
                            since_visit_id=args.since_visit_id, issue_id=args.issue_id)
    output = _write_private_new(args.output, json.dumps(report, ensure_ascii=False, indent=2).encode())
    return {"report_written": True, "entry_count": report.get("entry_count", 0),
            "output": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}


def _drive(args, runtime, settings):
    binding, session, config = _select(runtime, settings, args.binding, "admin" if args.drive_action in ("begin", "finish", "verify") else "read")
    drive_config = config.get("drive", {}).get(binding.scope_id)
    if not isinstance(drive_config, dict):
        raise CareCLIError("No Drive connection is configured for this binding")
    if args.drive_action == "status" and "write" not in session.authority.permissions:
        return {"drive": {"configured": True, "connection_status": "operator_check_required"}}
    from .care_drive_auth import DriveOAuth
    auth = DriveOAuth.from_config(session, drive_config, runtime_root=runtime.root)
    if args.drive_action == "status":
        return {"drive": auth.status()}
    if args.drive_action == "begin":
        return auth.begin(redirect_uri=REDIRECT_URI)
    if args.drive_action == "finish":
        if not sys.stdin.isatty():
            raise CareCLIError("Run this operation in an interactive terminal")
        callback = getpass.getpass("Paste the complete Google redirect URL (input hidden): ")
        result = auth.finish(callback)
        return {"drive": {"connected": result.get("connected") is True,
                          "scope_mode": result.get("scope_mode"), "folder_verified": False}}
    if args.drive_action == "verify":
        return {"drive": auth.verify_folder()}
    raise CareCLIError("Unknown Drive action")


def _documents(args, runtime, settings):
    action = args.document_action
    permission = "read" if action == "status" else "write"
    binding, session, _ = _select(runtime, settings, args.binding, permission)
    factory = runtime.documents
    if not callable(factory):
        raise CareCLIError("Document intake is not configured by the host")
    workflow = factory(session)
    if action == "intake":
        if args.upload and not args.prepare:
            raise CareCLIError("Upload requires an explicit prepare step")
        sources = _load_sources(args.files)
        request_id = args.request_id or ("cli:" + uuid.uuid4().hex)
        job = workflow.intake(sources, request_id=request_id)
        result = {"job_id": job["job_id"], "state": job["state"], "source_count": len(job["sources"])}
        if args.prepare:
            job = workflow.prepare(job["job_id"])
            result.update({"prepared": True, "state": job["state"]})
        if args.upload:
            job = workflow.upload(job["job_id"])
            result.update({"uploaded": job["state"] == "uploaded", "state": job["state"]})
        return result
    if action == "prepare":
        job = workflow.prepare(args.job_id)
        return {"job_id": job["job_id"], "prepared": True, "state": job["state"]}
    if action == "upload":
        job = workflow.upload(args.job_id)
        return {"job_id": job["job_id"], "uploaded": job["state"] == "uploaded", "state": job["state"]}
    raise CareCLIError("Unknown document action")


def _load_sources(paths):
    from .care_documents import DocumentInput
    if not 1 <= len(paths) <= 20:
        raise CareCLIError("Choose between 1 and 20 source files")
    total = 0
    sources = []
    for value in paths:
        path = Path(value)
        if not path.is_absolute() or path.is_symlink():
            raise CareCLIError("Input paths must be absolute regular files")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                raise CareCLIError("Input is not a supported bounded regular file")
            total += info.st_size
            if total > MAX_BATCH_BYTES:
                raise CareCLIError("Document batch exceeds the size limit")
            chunks = []
            remaining = MAX_FILE_BYTES + 1
            while remaining:
                block = os.read(fd, min(1024 * 1024, remaining))
                if not block:
                    break
                chunks.append(block)
                remaining -= len(block)
            content = b"".join(chunks)
            if len(content) != info.st_size:
                raise CareCLIError("Input changed while being read")
        finally:
            os.close(fd)
        mime, _ = mimetypes.guess_type(path.name)
        if path.suffix.lower() in (".jpg", ".jpeg"):
            mime = "image/jpeg"
        if mime not in ALLOWED_MIME:
            raise CareCLIError("Only PDF, JPEG, and PNG files are supported")
        sources.append(DocumentInput(name=path.name[:180], mime_type=mime, content=content))
    return sources


def _write_private_new(value, content):
    target = Path(value)
    if not target.is_absolute() or target.exists() or target.is_symlink():
        raise CareCLIError("Report output must be an absolute path to a new file")
    parent = target.parent
    if not parent.is_dir() or parent.is_symlink():
        raise CareCLIError("Report output directory must already exist and be trusted")
    # The file is created exclusively at mode 0600. A temporary private file is
    # published with hard-link semantics, so a racing path is never overwritten.
    fd, temporary = tempfile.mkstemp(prefix=".casita-report-", dir=parent)
    temp_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.link(temp_path, target, follow_symlinks=False)
        dir_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except FileExistsError:
        raise CareCLIError("Report output already exists") from None
    finally:
        temp_path.unlink(missing_ok=True)
    return target
