from __future__ import annotations

import base64
import importlib
import importlib.util
import io
import json
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(tmp_path):
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    pytest.importorskip("pypdfium2")
    package = types.ModuleType("care_extract_test_plugin")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
    extract = importlib.import_module(package.__name__ + ".care_extract")
    docs = importlib.import_module(package.__name__ + ".care_documents")
    core = importlib.import_module(package.__name__ + ".care_store")
    session = core.CareStore(tmp_path / "apollo.db", scope_id="synthetic-adult").session(
        core.Authority("synthetic-actor", "synthetic-adult", frozenset({"read", "write", "admin"})))
    workflow = docs.DocumentWorkflow(session, runtime_root=tmp_path / "documents")
    return extract, docs, core, session, workflow


def document(env, *, count=1, reverse=False, request_id="intake"):
    _, docs, _, _, workflow = env
    from PIL import Image, ImageDraw
    sources = []
    for index in range(count):
        image = Image.new("RGB", (600, 850), "white")
        ImageDraw.Draw(image).text((50, 100), f"SYNTHETIC PAGE {index + 1}. Sample measurement 123.", fill="black")
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        sources.append(docs.DocumentInput(f"synthetic-{index}.png", "image/png", stream.getvalue()))
    job = workflow.intake(sources, request_id=request_id)
    pages = [docs.PageEdit(index, deskew=False) for index in range(count)]
    return workflow.prepare(job["job_id"], pages=list(reversed(pages)) if reverse else pages)


def proposal(*, page=1, quote="Sample measurement 123.", kind="measurement"):
    return {"page": page, "kind": kind, "excerpt": "Synthetic excerpt for review", "quote": quote,
            "confidence": 0.9, "state": "unconfirmed", "data": {"reported_value": 123}}


def response(*items, unreadable=(), warnings=()):
    return {"proposals": list(items), "unreadable_pages": list(unreadable), "warnings": list(warnings)}


class Vision:
    def __init__(self, parsed):
        self.parsed = parsed
        self.calls = []
        self.side_effect = None

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if self.side_effect:
            self.side_effect()
        return types.SimpleNamespace(parsed=self.parsed)


def test_image_pdf_preview_host_vision_to_unconfirmed_source_proposals(env):
    extract, _, _, session, workflow = env
    job = document(env)
    vision = Vision(response(proposal()))
    receipt = extract.extract_document(workflow, vision, job["job_id"], request_id="extract-1")
    assert receipt["state"] == "unconfirmed"
    assert receipt["journal_entries_created"] == receipt["medication_plans_activated"] == 0
    assert receipt["source_ref"] == "document:" + job["job_id"]
    assert len(receipt["proposals"]) == 1
    call = vision.calls[0]
    blocks = call["input"]
    assert [b["type"] for b in blocks] == ["text", "text", "image"]
    image = blocks[-1]
    assert image["url"].startswith("data:image/png;base64,")
    preview = workflow.artifact(job["job_id"], page_number=1)
    assert base64.b64decode(image["url"].split(",", 1)[1]) == preview["content"]
    assert "provider" not in call and "model" not in call and "profile" not in call
    assert call["temperature"] == 0 and call["timeout"] == 60
    assert call["json_schema"] == extract.SCHEMA
    assert "immagini" in call["system_prompt"]
    persisted = workflow.get(job["job_id"])["proposals"]
    assert persisted[0]["state"] == "unconfirmed"
    payload = json.loads(persisted[0]["proposal_json"])
    assert payload["preview_sha256"] == preview["sha256"]
    assert payload["confidence"] == 0.9
    with session.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM care_entries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM care_document_extractions").fetchone()[0] == 1


def test_model_page_maps_to_original_source_after_reordering(env):
    extract, _, _, _, workflow = env
    job = document(env, count=2, reverse=True)
    receipt = extract.extract_document(workflow, Vision(response(proposal(page=1))), job["job_id"], request_id="order")
    assert receipt["proposals"][0]["source_index"] == 1
    assert receipt["proposals"][0]["source_page"] == 1
    saved = workflow.get(job["job_id"])["proposals"][0]
    assert saved["source_index"] == 1
    assert saved["page_number"] == 1


def test_replay_uses_durable_receipt_without_another_vision_call(env):
    extract, docs, _, session, workflow = env
    job = document(env)
    vision = Vision(response(proposal()))
    first = extract.extract_document(workflow, vision, job["job_id"], request_id="replay")
    restarted = docs.DocumentWorkflow(session, runtime_root=workflow.root.parent)
    second = extract.extract_document(restarted, vision, job["job_id"], request_id="replay")
    assert second == first
    assert len(vision.calls) == 1
    assert len(restarted.get(job["job_id"])["proposals"]) == 1


def test_interrupted_proposal_application_reuses_validated_output_and_ids(env, monkeypatch):
    extract, _, _, _, workflow = env
    job = document(env)
    vision = Vision(response(proposal(), proposal(quote="Second synthetic quote", kind="note")))
    original = workflow.propose
    seen = []

    def interrupted(*args, **kwargs):
        seen.append(kwargs["request_id"])
        if len(seen) == 2:
            raise RuntimeError("synthetic process interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(workflow, "propose", interrupted)
    with pytest.raises(extract.ExtractionError, match="interrupted"):
        extract.extract_document(workflow, vision, job["job_id"], request_id="interrupted")
    assert len(workflow.get(job["job_id"])["proposals"]) == 1
    monkeypatch.setattr(workflow, "propose", original)
    receipt = extract.extract_document(workflow, vision, job["job_id"], request_id="interrupted")
    assert len(receipt["proposals"]) == 2
    assert len(vision.calls) == 1
    assert len(workflow.get(job["job_id"])["proposals"]) == 2


@pytest.mark.parametrize("output", [
    None,
    response(dict(proposal(), state="confirmed")),
    response(dict(proposal(), confidence=2)),
    response(dict(proposal(), confidence=float("nan"))),
    response(dict(proposal(), patient_id="foreign-patient")),
    response(dict(proposal(), page=2)),
    response(dict(proposal(), quote=" ")),
    response(dict(proposal(), data={"nested": {"secret": "not allowed"}})),
    response(proposal(), unreadable=[1]),
    response(unreadable=[2]),
    {"proposals": [], "unreadable_pages": []},
])
def test_invalid_model_output_never_creates_a_proposal(env, output):
    extract, _, _, session, workflow = env
    job = document(env)
    with pytest.raises(extract.ExtractionError, match="validation"):
        extract.extract_document(workflow, Vision(output), job["job_id"], request_id="invalid")
    assert workflow.get(job["job_id"])["proposals"] == []
    with session.connection() as conn:
        assert conn.execute("SELECT state FROM care_document_extractions").fetchone()[0] == "failed"


def test_unreadable_page_stays_missing_and_medication_proposal_never_activates(env):
    extract, _, _, session, workflow = env
    job = document(env, count=2)
    reminders = importlib.import_module(extract.__package__ + ".care_reminders")
    with session.connection(permission="write") as conn:
        reminders.ensure_reminder_schema(conn)
    receipt = extract.extract_document(workflow, Vision(response(proposal(kind="medication_instruction"), unreadable=[2],
                                                               warnings=["Synthetic unreadable page"])),
                                       job["job_id"], request_id="unreadable")
    assert receipt["unreadable_pages"] == [2]
    assert receipt["proposals"][0]["kind"] == "medication_instruction"
    assert receipt["proposals"][0]["state"] == "unconfirmed"
    with session.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM care_entries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM care_medication_plans").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM care_reminder_schedules").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM care_medication_administrations").fetchone()[0] == 0


def test_provider_without_vision_fails_safely_without_text_fallback(env):
    extract, _, _, _, workflow = env
    job = document(env)
    calls = []

    class NoVision:
        def complete_structured(self, **kwargs):
            calls.append(kwargs)
            raise NotImplementedError("secret-provider-error must not leak")

    with pytest.raises(extract.ExtractionError, match="Vision provider unavailable") as error:
        extract.extract_document(workflow, NoVision(), job["job_id"], request_id="no-vision")
    assert "secret-provider-error" not in str(error.value)
    assert len(calls) == 1
    assert workflow.get(job["job_id"])["proposals"] == []


def test_page_count_bytes_and_replay_selection_are_bounded(env, monkeypatch):
    extract, _, _, _, workflow = env
    job = document(env, count=6)
    vision = Vision(response())
    with pytest.raises(extract.ExtractionError, match="at most five"):
        extract.extract_document(workflow, vision, job["job_id"], request_id="six")
    assert not vision.calls
    receipt = extract.extract_document(workflow, vision, job["job_id"], request_id="selected", pages=[6, 2])
    assert receipt["pages"] == [6, 2]
    assert receipt["remaining_pages"] == [1, 3, 4, 5]
    with pytest.raises(extract.ExtractionError, match="different document or pages"):
        extract.extract_document(workflow, vision, job["job_id"], request_id="selected", pages=[2, 6])
    monkeypatch.setattr(extract, "MAX_PAGE_BYTES", 1)
    with pytest.raises(extract.ExtractionError, match="size limit"):
        extract.extract_document(workflow, vision, job["job_id"], request_id="too-big", pages=[1])
    assert len(vision.calls) == 1


def test_cross_patient_job_and_revoked_grant_never_reach_vision_or_persist(env, tmp_path):
    extract, docs, core, session, workflow = env
    job = document(env)
    foreign = core.CareStore(tmp_path / "foreign.db", scope_id="foreign-scope").session(
        core.Authority("synthetic-actor", "foreign-scope", frozenset({"read", "write"})))
    other = docs.DocumentWorkflow(foreign, runtime_root=tmp_path / "foreign-documents")
    vision = Vision(response(proposal()))
    with pytest.raises(ValueError, match="job unavailable"):
        extract.extract_document(other, vision, job["job_id"], request_id="foreign")
    with pytest.raises(PermissionError, match="Current care grant"):
        extract.extract_document(workflow, vision, job["job_id"], request_id="revoked", authorize_current=lambda authority: False)
    assert not vision.calls
    authorized = [True]
    vision.side_effect = lambda: authorized.__setitem__(0, False)
    with pytest.raises(PermissionError, match="Current care grant"):
        extract.extract_document(workflow, vision, job["job_id"], request_id="revoked-during-inference",
                                 authorize_current=lambda authority: authorized[0])
    assert len(vision.calls) == 1
    assert workflow.get(job["job_id"])["proposals"] == []


def test_same_request_cannot_run_concurrent_inferences(env):
    extract, _, _, _, workflow = env
    job = document(env)
    vision = Vision(response())
    errors = []

    def concurrent():
        with pytest.raises(extract.ExtractionError, match="already running") as error:
            extract.extract_document(workflow, Vision(response()), job["job_id"], request_id="concurrent")
        errors.append(str(error.value))

    vision.side_effect = concurrent
    extract.extract_document(workflow, vision, job["job_id"], request_id="concurrent")
    assert errors


def test_pinned_hermes_facade_accepts_actual_image_blocks_without_network(env):
    """Optional source-contract check; no Hermes installation or provider needed."""
    source_root = os.environ.get("HERMES_SOURCE")
    if not source_root:
        pytest.skip("Set HERMES_SOURCE to the reviewed Hermes 0.22.2 source tree")
    path = Path(source_root) / "agent/plugin_llm.py"
    assert path.exists()
    name = "synthetic_pinned_plugin_llm"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    extract, _, _, _, workflow = env
    job = document(env)
    calls = []

    def native_call(**kwargs):
        calls.append(kwargs)
        content = kwargs["messages"][-1]["content"]
        image = [part for part in content if part["type"] == "image_url"]
        assert len(image) == 1
        assert image[0]["image_url"]["url"].startswith("data:image/png;base64,")
        reply = types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(
            content=json.dumps(response(proposal()))))], model="synthetic-vision", usage=None)
        return "synthetic-provider", "synthetic-vision", reply

    llm = module.make_plugin_llm_for_test(plugin_id="synthetic-care", policy=module._TrustPolicy(plugin_id="synthetic-care"),
                                        sync_caller=native_call)
    receipt = extract.extract_document(workflow, llm, job["job_id"], request_id="native-facade")
    assert len(receipt["proposals"]) == 1
    assert len(calls) == 1


def test_total_preview_and_per_proposal_bytes_are_checked_before_inference_or_writes(env, monkeypatch):
    extract, _, _, _, workflow = env
    job = document(env, count=2)
    vision = Vision(response())
    monkeypatch.setattr(extract, "MAX_IMAGE_BYTES", len(workflow.artifact(job["job_id"], page_number=1)["content"]))
    with pytest.raises(extract.ExtractionError, match="batch size limit"):
        extract.extract_document(workflow, vision, job["job_id"], request_id="batch-byte-limit")
    assert not vision.calls
    monkeypatch.setattr(extract, "MAX_IMAGE_BYTES", 20 * 1024 * 1024)
    big = dict(proposal(), data={"value_" + str(n): "x" * 1000 for n in range(20)})
    with pytest.raises(extract.ExtractionError, match="validation"):
        extract.extract_document(workflow, Vision(response(big)), job["job_id"], request_id="proposal-bytes")
    assert workflow.get(job["job_id"])["proposals"] == []
