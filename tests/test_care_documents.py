from __future__ import annotations

import importlib
import io
import json
import sqlite3
import sys
import types
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module(name):
    sys.modules.setdefault("hermes_plugins", types.ModuleType("hermes_plugins"))
    package = sys.modules.setdefault("hermes_plugins.health_data", types.ModuleType("hermes_plugins.health_data"))
    package.__path__ = [str(ROOT)]
    return importlib.import_module("hermes_plugins.health_data." + name)


@pytest.fixture
def env(tmp_path):
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    pytest.importorskip("pypdfium2")
    docs = load_module("care_documents")
    core = load_module("care_store")
    session = core.CareStore(tmp_path / "apollo.db", scope_id="synthetic-adult").session(
        core.Authority("synthetic-actor", "synthetic-adult", frozenset({"read", "write", "admin"})))
    workflow = docs.DocumentWorkflow(session, runtime_root=tmp_path / "private-documents")
    return docs, core, session, workflow


def image_bytes(*, size=(600, 850), color="white", skew=0, exif=False):
    from PIL import Image, ImageDraw
    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    for y in range(120, 700, 45):
        draw.rectangle((100, y, 490, y + 6), fill="black")
        draw.text((100, y + 9), "SYNTHETIC DOCUMENT - Sample 123", fill="black")
    if skew:
        image = image.rotate(skew, expand=True, fillcolor="white")
    data = io.BytesIO()
    if exif:
        info = Image.Exif()
        info[274] = 6
        image.save(data, format="JPEG", exif=info)
    else:
        image.save(data, format="PNG")
    return data.getvalue()


def source(docs, **kwargs):
    return docs.DocumentInput("synthetic.png", "image/png", image_bytes(**kwargs))


class FakeDrive:
    def __init__(self, docs):
        self.docs = docs
        self.files = []
        self.calls = []
        self.timeout_after_create = False
        self.hidden = False
        self.fail_search = False
        self.redirect = False
        self.wrong_parent = False

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers.copy(), body))
        assert headers["Authorization"] == "Bearer synthetic-access"
        assert urlsplit(url).netloc == "www.googleapis.com"
        if self.redirect:
            return self.docs.HTTPResponse(302, {"Location": "https://example.invalid/"}, b"{}")
        if method == "GET":
            query = parse_qs(urlsplit(url).query)
            assert "'synthetic_folder' in parents" in query["q"][0]
            if self.fail_search:
                return self.docs.HTTPResponse(503, {}, b"{}")
            identity = query["q"][0].split("value='")[1].split("'")[0]
            matching = [f for f in self.files if f["appProperties"]["casita_upload_key"] == identity]
            return self.docs.HTTPResponse(200, {}, json.dumps({"files": [] if self.hidden else matching}).encode())
        assert urlsplit(url).path == "/upload/drive/v3/files"
        assert parse_qs(urlsplit(url).query)["uploadType"] == ["multipart"]
        message = BytesParser().parsebytes(("MIME-Version: 1.0\r\nContent-Type: " + headers["Content-Type"] + "\r\n\r\n").encode() + body)
        parts = message.get_payload()
        metadata = json.loads(parts[0].get_payload(decode=True))
        payload = parts[1].get_payload(decode=True)
        import hashlib
        assert metadata["appProperties"]["casita_sha256"] == hashlib.sha256(payload).hexdigest()
        assert metadata["parents"] == ["synthetic_folder"]
        item = {"id": "synthetic_file_" + str(len(self.files) + 1),
                "parents": ["wrong_folder"] if self.wrong_parent else metadata["parents"],
                "appProperties": metadata["appProperties"]}
        self.files.append(item)
        if self.timeout_after_create:
            self.timeout_after_create = False
            raise OSError("synthetic-access must not leak")
        return self.docs.HTTPResponse(200, {}, json.dumps(item).encode())


def bind_drive(env):
    docs, core, session, workflow = env
    fake = FakeDrive(docs)
    workflow.drive = docs.GoogleDriveAdapter(folder_id="synthetic_folder", token_provider=lambda: "synthetic-access", transport=fake)
    return fake


def test_photo_batch_preserves_originals_order_provenance_and_apollo_lineage(env):
    docs, core, session, workflow = env
    first = source(docs)
    second = source(docs, size=(610, 860))
    job = workflow.intake([first, second], request_id="batch-1")
    job = workflow.prepare(job["job_id"], pages=[docs.PageEdit(1, rotate=90, crop=(0, 0, 800, 600), deskew=False),
                                               docs.PageEdit(0, deskew=False)])
    assert job["state"] == "prepared"
    manifest = json.loads(job["manifest_json"])
    assert [p["source_index"] for p in manifest["pages"]] == [1, 0]
    assert manifest["pages"][0]["width"] == 800
    assert manifest["pages"][0]["height"] == 600
    assert manifest["pages"][0]["transforms"]["rotate"] == 90
    assert workflow._read(job["sources"][0]["sha256"]) == first.content
    assert workflow._read(job["sources"][1]["sha256"]) == second.content
    with session.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM record_lineage WHERE canonical_table='care_document_jobs'").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM raw_records WHERE object_type='care_document'").fetchone()[0] == 2
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] >= 8
    with docs._pdfium().PdfDocument(workflow._read(job["derivative_sha"])) as pdf:
        assert len(pdf) == 2
    assert workflow.prepare(job["job_id"])["derivative_sha"] == job["derivative_sha"]
    assert all(p.stat().st_mode & 0o077 == 0 for p in workflow.root.iterdir())


def test_auto_deskew_corrects_lines_and_expands_without_cropping(env):
    docs, _, _, workflow = env
    original = source(docs, skew=3)
    job = workflow.prepare(workflow.intake([original], request_id="skew")["job_id"])
    page = json.loads(job["manifest_json"])["pages"][0]
    assert page["transforms"]["deskew_degrees"] == pytest.approx(-3, abs=0.5)
    from PIL import Image
    with Image.open(io.BytesIO(original.content)) as image:
        assert page["width"] >= image.width
        assert page["height"] >= image.height
    assert page["transforms"]["crop"] is None


def test_exif_orientation_and_transparency_are_deterministic(env):
    docs, _, _, workflow = env
    data = image_bytes(exif=True)
    job = workflow.intake([docs.DocumentInput("page.jpg", "image/jpeg", data)], request_id="exif")
    job = workflow.prepare(job["job_id"], pages=[docs.PageEdit(0, deskew=False)])
    page = json.loads(job["manifest_json"])["pages"][0]
    assert (page["width"], page["height"]) == (850, 600)


def test_multipage_pdf_intake_and_pdf_page_order(env):
    docs, _, _, workflow = env
    from PIL import Image
    one = Image.new("RGB", (300, 400), "white")
    two = Image.new("RGB", (400, 300), "white")
    stream = io.BytesIO()
    one.save(stream, format="PDF", save_all=True, append_images=[two], resolution=150)
    job = workflow.intake([docs.DocumentInput("source.pdf", "application/pdf", stream.getvalue())], request_id="pdf")
    assert job["sources"][0]["page_count"] == 2
    job = workflow.prepare(job["job_id"], pages=[docs.PageEdit(0, 2), docs.PageEdit(0, 1)])
    pages = json.loads(job["manifest_json"])["pages"]
    assert [p["source_page"] for p in pages] == [2, 1]
    assert pages[0]["width"] > pages[0]["height"]


def test_upload_real_http_shapes_original_and_derivative_receipts_and_replay(env):
    docs, _, session, workflow = env
    fake = bind_drive(env)
    job = workflow.prepare(workflow.intake([source(docs)], request_id="upload")["job_id"])
    uploaded = workflow.upload(job["job_id"])
    assert uploaded["state"] == "uploaded"
    assert len(fake.files) == 2
    assert [c[0] for c in fake.calls] == ["GET", "POST", "GET", "POST"]
    for upload in uploaded["uploads"]:
        assert upload["state"] == "uploaded"
        receipt = json.loads(upload["receipt_json"])
        assert receipt["folder_id"] == "synthetic_folder"
        assert receipt["sha256"] == upload["sha256"]
    restarted = docs.DocumentWorkflow(session, runtime_root=workflow.root.parent, drive=workflow.drive)
    assert restarted.upload(job["job_id"])["state"] == "uploaded"
    assert len(fake.files) == 2
    assert restarted.intake([source(docs)], request_id="upload")["job_id"] == job["job_id"]


def test_uncertain_send_reconciles_after_restart_without_duplicate(env):
    docs, _, session, workflow = env
    fake = bind_drive(env)
    fake.timeout_after_create = True
    job = workflow.prepare(workflow.intake([source(docs)], request_id="uncertain")["job_id"])
    with pytest.raises(docs.UncertainUpload, match="reconciliation") as error:
        workflow.upload(job["job_id"])
    assert "synthetic-access" not in str(error.value)
    assert len(fake.files) == 1
    restarted = docs.DocumentWorkflow(session, runtime_root=workflow.root.parent, drive=workflow.drive)
    assert restarted.upload(job["job_id"])["state"] == "uploaded"
    assert len(fake.files) == 2  # one derivative + one original, not two derivatives


def test_uncertain_empty_search_does_not_blindly_retry(env):
    docs, _, _, workflow = env
    fake = bind_drive(env)
    fake.timeout_after_create = True
    job = workflow.prepare(workflow.intake([source(docs)], request_id="uncertain-hidden")["job_id"])
    with pytest.raises(docs.UncertainUpload):
        workflow.upload(job["job_id"])
    fake.hidden = True
    with pytest.raises(docs.UncertainUpload, match="operator"):
        workflow.upload(job["job_id"])
    assert len(fake.files) == 1
    assert len([c for c in fake.calls if c[0] == "POST"]) == 1


def test_duplicate_receipts_and_folder_change_block_new_sends(env):
    docs, _, _, workflow = env
    fake = bind_drive(env)
    job = workflow.prepare(workflow.intake([source(docs)], request_id="duplicate")["job_id"])
    fake.timeout_after_create = True
    with pytest.raises(docs.UncertainUpload):
        workflow.upload(job["job_id"])
    fake.files.append(dict(fake.files[0], id="duplicate_file"))
    with pytest.raises(docs.DriveError, match="Duplicate"):
        workflow.upload(job["job_id"])
    assert len([c for c in fake.calls if c[0] == "POST"]) == 1
    workflow.drive = docs.GoogleDriveAdapter(folder_id="another_folder", token_provider=lambda: "synthetic-access", transport=fake)
    with pytest.raises(docs.DriveError, match="folder changed"):
        workflow.upload(job["job_id"])


def test_proposals_are_source_linked_unconfirmed_and_replay_safe(env):
    docs, _, session, workflow = env
    job = workflow.intake([source(docs)], request_id="proposal-source")
    args = dict(request_id="extract-1", source_index=0, page_number=1,
                quote="Synthetic instruction requiring review", proposal={"kind": "medication_instruction", "dose": "unverified"})
    proposal = workflow.propose(job["job_id"], **args)
    assert proposal["state"] == "unconfirmed"
    assert workflow.propose(job["job_id"], **args)["proposal_id"] == proposal["proposal_id"]
    with pytest.raises(docs.DocumentError, match="different content"):
        workflow.propose(job["job_id"], **dict(args, quote="different"))
    with session.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM care_entries").fetchone()[0] == 0


def test_links_validate_scope_and_kind(env):
    docs, _, session, workflow = env
    episode = session.create_case(kind="episode", title="Synthetic episode", request_id="case", source_ref="synthetic")
    job = workflow.intake([source(docs)], request_id="linked", episode_id=episode["case_id"])
    assert job["episode_id"] == episode["case_id"]
    with pytest.raises(docs.DocumentError, match="link unavailable"):
        workflow.intake([source(docs)], request_id="wrong-kind", visit_id=episode["case_id"])
    with pytest.raises(docs.DocumentError, match="link unavailable"):
        workflow.intake([source(docs)], request_id="foreign", episode_id="foreign-case")


def test_request_replay_rejects_changed_file(env):
    docs, _, _, workflow = env
    workflow.intake([source(docs)], request_id="replay")
    with pytest.raises(docs.DocumentError, match="different content"):
        workflow.intake([source(docs, size=(620, 840))], request_id="replay")


@pytest.mark.parametrize("name,mime,content", [
    ("../page.png", "image/png", b"no"),
    ("page.png", "image/svg+xml", b"<svg/>"),
    ("page.png", "image/png", b"%PDF-synthetic"),
    ("page.pdf", "application/pdf", b"not-pdf"),
    ("page.jpg", "image/jpeg", b""),
])
def test_unsafe_unsupported_or_mislabeled_sources_fail(env, name, mime, content):
    docs, _, _, workflow = env
    with pytest.raises(docs.DocumentError):
        workflow.intake([docs.DocumentInput(name, mime, content)], request_id="bad")
    assert not list(workflow.root.iterdir())


def test_size_pixel_limits_and_page_plan_are_enforced(env, monkeypatch):
    docs, _, _, workflow = env
    original = source(docs)
    monkeypatch.setattr(docs, "MAX_FILE_BYTES", 10)
    with pytest.raises(docs.DocumentError, match="oversized"):
        workflow.intake([original], request_id="large")
    monkeypatch.setattr(docs, "MAX_FILE_BYTES", 25 * 1024 * 1024)
    monkeypatch.setattr(docs, "MAX_PIXELS", 100)
    with pytest.raises(docs.DocumentError, match="pixel limit"):
        workflow.intake([original], request_id="pixels")
    monkeypatch.setattr(docs, "MAX_PIXELS", 20_000_000)
    job = workflow.intake([original], request_id="valid")
    with pytest.raises(docs.DocumentError, match="every source page"):
        workflow.prepare(job["job_id"], pages=[docs.PageEdit(0), docs.PageEdit(0)])
    with pytest.raises(docs.DocumentError, match="Invalid page transform"):
        workflow.prepare(job["job_id"], pages=[docs.PageEdit(0, rotate=45)])


def test_authorization_scope_marker_and_symlink_checks(env, tmp_path):
    docs, core, session, workflow = env
    with pytest.raises(PermissionError):
        docs.DocumentWorkflow(object(), runtime_root=tmp_path / "untrusted")
    reader = core.CareStore(session.db_path, scope_id="synthetic-adult").session(
        core.Authority("reader", "synthetic-adult", frozenset({"read"})))
    readonly = docs.DocumentWorkflow(reader, runtime_root=workflow.root.parent)
    with pytest.raises(PermissionError):
        readonly.intake([source(docs)], request_id="reader-write")
    linked = tmp_path / "linked"
    linked.symlink_to(workflow.root.parent, target_is_directory=True)
    with pytest.raises(docs.DocumentError, match="Unsafe"):
        docs.DocumentWorkflow(session, runtime_root=linked)
    with session.connection(permission="write") as conn:
        conn.execute("UPDATE care_scope SET scope_id='different-scope'")
    with pytest.raises(PermissionError, match="scope binding changed"):
        workflow.intake([source(docs)], request_id="revoked")
    assert not list(workflow.root.iterdir())


def test_artifact_corruption_and_symlink_cannot_be_uploaded(env, tmp_path):
    docs, _, _, workflow = env
    fake = bind_drive(env)
    job = workflow.prepare(workflow.intake([source(docs)], request_id="tamper")["job_id"])
    path = workflow.root / job["derivative_sha"]
    path.unlink()
    external = tmp_path / "outside"
    external.write_bytes(b"synthetic")
    path.symlink_to(external)
    with pytest.raises(docs.DocumentError, match="Artifact unavailable"):
        workflow.upload(job["job_id"])
    assert not fake.calls


def test_redirect_and_wrong_folder_receipts_never_follow_or_resend(env):
    docs, _, _, workflow = env
    fake = bind_drive(env)
    job = workflow.prepare(workflow.intake([source(docs)], request_id="redirect")["job_id"])
    fake.redirect = True
    with pytest.raises(docs.DriveError, match="redirect rejected"):
        workflow.upload(job["job_id"])
    assert len(fake.calls) == 1
    fake.redirect = False
    fake.wrong_parent = True
    with pytest.raises(docs.UncertainUpload, match="receipt mismatch"):
        workflow.upload(job["job_id"])
    with pytest.raises(docs.DriveError, match="folder mismatch"):
        workflow.upload(job["job_id"])
    assert len(fake.files) == 1


def test_preparing_crash_resumes_same_plan(env):
    docs, _, session, workflow = env
    job = workflow.intake([source(docs)], request_id="crash")
    plan = docs._json([docs.asdict(docs.PageEdit(0, rotate=180, deskew=False))])
    with session.connection(permission="write") as conn:
        conn.execute("UPDATE care_document_jobs SET state='preparing',plan_json=? WHERE job_id=?", (plan, job["job_id"]))
    restarted = docs.DocumentWorkflow(session, runtime_root=workflow.root.parent)
    assert restarted.prepare(job["job_id"])["state"] == "prepared"


class ResumableDrive(FakeDrive):
    def __init__(self, docs):
        super().__init__(docs)
        self.sessions = {}
        self.interrupt_once = True
        self.resume_ranges = []
        self.unsafe_location = None

    def __call__(self, method, url, headers, body):
        query = parse_qs(urlsplit(url).query)
        if method == "POST" and query.get("uploadType") == ["resumable"]:
            self.calls.append((method, url, headers.copy(), body))
            metadata = json.loads(body)
            uri = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=synthetic_session_" + str(len(self.sessions))
            self.sessions[uri] = {"metadata": metadata, "content": b"", "complete": None}
            assert int(headers["X-Upload-Content-Length"]) > 5 * 1024 * 1024
            return self.docs.HTTPResponse(200, {"Location": self.unsafe_location or uri}, b"")
        if method == "PUT":
            self.calls.append((method, url, headers.copy(), body))
            upload = self.sessions[url]
            progress = headers["Content-Range"]
            self.resume_ranges.append(progress)
            if progress.startswith("bytes */"):
                if upload["complete"]:
                    return self.docs.HTTPResponse(200, {}, json.dumps(upload["complete"]).encode())
                received = len(upload["content"])
                return self.docs.HTTPResponse(308, {"Range": f"bytes=0-{received-1}"} if received else {}, b"")
            offset = int(progress.split(" ")[1].split("-")[0])
            assert offset == len(upload["content"])
            if self.interrupt_once:
                self.interrupt_once = False
                upload["content"] += body[:len(body) // 2]
                raise OSError("synthetic partial network interruption")
            upload["content"] += body
            import hashlib
            assert hashlib.sha256(upload["content"]).hexdigest() == upload["metadata"]["appProperties"]["casita_sha256"]
            item = {"id": "synthetic_resumable_" + str(len(self.files)),
                    "parents": upload["metadata"]["parents"], "appProperties": upload["metadata"]["appProperties"]}
            self.files.append(item)
            upload["complete"] = item
            return self.docs.HTTPResponse(200, {}, json.dumps(item).encode())
        return super().__call__(method, url, headers, body)


def large_source(docs):
    import numpy as np
    from PIL import Image
    values = np.random.default_rng(16).integers(0, 256, (1500, 1500, 3), dtype=np.uint8)
    stream = io.BytesIO()
    Image.fromarray(values).save(stream, format="PNG")
    assert len(stream.getvalue()) > 5 * 1024 * 1024
    return docs.DocumentInput("synthetic-noise.png", "image/png", stream.getvalue())


def test_large_file_resumable_progress_survives_restart(env):
    docs, _, session, workflow = env
    fake = ResumableDrive(docs)
    workflow.drive = docs.GoogleDriveAdapter(folder_id="synthetic_folder", token_provider=lambda: "synthetic-access", transport=fake)
    job = workflow.prepare(workflow.intake([large_source(docs)], request_id="large-resumable")["job_id"],
                           pages=[docs.PageEdit(0, deskew=False)])
    with pytest.raises(docs.UncertainUpload):
        workflow.upload(job["job_id"])
    assert len(fake.sessions) == 1
    assert "upload_session_uri" not in workflow.get(job["job_id"])["uploads"][0]
    restarted = docs.DocumentWorkflow(session, runtime_root=workflow.root.parent, drive=workflow.drive)
    uploaded = restarted.upload(job["job_id"])
    assert uploaded["state"] == "uploaded"
    assert len(fake.files) == 2
    assert any(r.startswith("bytes */") for r in fake.resume_ranges)
    assert any(not r.startswith("bytes */") and not r.startswith("bytes 0-") for r in fake.resume_ranges)


def test_resumable_session_location_cannot_redirect_credentials(env):
    docs, _, _, workflow = env
    fake = ResumableDrive(docs)
    fake.unsafe_location = "https://example.invalid/upload?uploadType=resumable&upload_id=synthetic"
    drive = docs.GoogleDriveAdapter(folder_id="synthetic_folder", token_provider=lambda: "synthetic-access", transport=fake)
    with pytest.raises(docs.UncertainUpload, match="Unsafe"):
        drive.begin_upload(identity="a" * 64, sha256="b" * 64, name="synthetic.pdf", mime_type="application/pdf", size=6 * 1024 * 1024)
    assert len(fake.calls) == 1
    with pytest.raises(docs.DriveError, match="destination rejected"):
        docs.google_http_transport("GET", "https://example.invalid/", {}, None)


def test_artifact_reader_requires_owned_job_and_cannot_read_arbitrary_digest(env):
    docs, _, _, workflow = env
    original = source(docs)
    job = workflow.prepare(workflow.intake([original], request_id="preview")["job_id"])
    assert workflow.artifact(job["job_id"], source_index=0)["content"] == original.content
    assert workflow.artifact(job["job_id"], page_number=1)["mime_type"] == "image/png"
    assert workflow.artifact(job["job_id"])["content"].startswith(b"%PDF")
    with pytest.raises(docs.DocumentError, match="job unavailable"):
        workflow.artifact("foreign-job")
    with pytest.raises(docs.DocumentError, match="Output page unavailable"):
        workflow.artifact(job["job_id"], page_number=0)
