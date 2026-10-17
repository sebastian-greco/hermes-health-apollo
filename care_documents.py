"""Private, scoped document intake and deterministic filing; no clinical inference.

The host supplies an authorized CareSession, private artifact root, configured
Drive folder and token provider. No patient, path or credential comes from tools.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tempfile
import uuid
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_BATCH_BYTES = 60 * 1024 * 1024
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
MAX_PAGES = 20
MAX_PIXELS = 20_000_000
MAX_BATCH_PIXELS = 80_000_000
MAX_RESPONSE_BYTES = 1024 * 1024
MIMES = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"}
ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")


class DocumentError(ValueError):
    """Safe error messages never include document content or transport secrets."""


class DriveError(DocumentError):
    pass


class UncertainUpload(DriveError):
    """The send might have succeeded. Reconcile before another send."""


@dataclass(frozen=True)
class DocumentInput:
    name: str
    mime_type: str
    content: bytes


@dataclass(frozen=True)
class PageEdit:
    source_index: int
    page_number: int = 1
    rotate: int = 0
    crop: tuple[int, int, int, int] | None = None
    deskew: bool = True


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def google_http_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> HTTPResponse:
    """Real bounded HTTPS transport; redirects never receive an OAuth token."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "www.googleapis.com" or parsed.fragment:
        raise DriveError("Drive transport destination rejected")
    request = Request(url, data=body, headers=dict(headers), method=method)
    try:
        response = build_opener(_NoRedirect).open(request, timeout=30)
    except HTTPError as exc:
        response = exc
    except (OSError, URLError):
        raise UncertainUpload("Drive response unavailable; reconciliation required") from None
    with response:
        data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise UncertainUpload("Drive response exceeds limit; reconciliation required")
        return HTTPResponse(response.status, dict(response.headers), data)


class GoogleDriveAdapter:
    """Folder-bound Drive v3 HTTP adapter with search-before-send identity.

    ``token_provider`` must be host-managed, returning a current OAuth token;
    this module never reads token files, environment credentials or other apps.
    An injected transport must preserve the no-redirect and response-size rules.
    """

    def __init__(self, *, folder_id: str, token_provider: Callable[[], str], transport=None):
        if not isinstance(folder_id, str) or not ID.fullmatch(folder_id):
            raise DriveError("Invalid configured Drive folder")
        self.folder_id = folder_id
        self._token_provider = token_provider
        self._transport = transport or google_http_transport

    def _request(self, method, url, body=None, content_type=None, *, extra_headers=None, raw=False, resume=False):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "www.googleapis.com" or parsed.fragment:
            raise DriveError("Drive transport destination rejected")
        try:
            token = self._token_provider()
        except Exception:
            raise DriveError("Drive authorization unavailable") from None
        if not isinstance(token, str) or not token or len(token) > 8192 or any(c.isspace() for c in token):
            raise DriveError("Drive authorization unavailable")
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        headers.update(extra_headers or {})
        try:
            response = self._transport(method, url, headers, body)
        except Exception:
            raise UncertainUpload("Drive response unavailable; reconciliation required") from None
        if not isinstance(response, HTTPResponse) or len(response.body) > MAX_RESPONSE_BYTES:
            raise UncertainUpload("Invalid Drive response; reconciliation required")
        if response.status == 308 and resume and not any(k.lower() == "location" for k in response.headers):
            return response
        if 300 <= response.status < 400:
            raise DriveError("Drive redirect rejected")
        if response.status >= 500 or response.status in (408, 429):
            raise UncertainUpload("Drive response uncertain; reconciliation required")
        if not 200 <= response.status < 300:
            raise DriveError("Drive request rejected")
        if raw:
            return response
        try:
            data = json.loads(response.body)
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, TypeError):
            raise UncertainUpload("Invalid Drive receipt; reconciliation required") from None

    def _receipt(self, item, identity, sha256):
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not ID.fullmatch(item["id"]) or item.get("parents") != [self.folder_id]
                or not isinstance(item.get("appProperties"), dict)
                or item.get("appProperties", {}).get("casita_upload_key") != identity
                or item.get("appProperties", {}).get("casita_sha256") != sha256
                or item.get("trashed", False)):
            raise DriveError("Drive receipt identity or folder mismatch")
        return {"file_id": item["id"], "folder_id": self.folder_id,
                "upload_key": identity, "sha256": sha256, "recorded_at": _now()}

    def find(self, *, identity: str, sha256: str):
        if not SHA.fullmatch(identity) or not SHA.fullmatch(sha256):
            raise DriveError("Invalid upload identity")
        query = (f"'{self.folder_id}' in parents and trashed = false and "
                 f"appProperties has {{ key='casita_upload_key' and value='{identity}' }}")
        url = "https://www.googleapis.com/drive/v3/files?" + urlencode({
            "q": query, "spaces": "drive", "pageSize": 100,
            "fields": "nextPageToken,incompleteSearch,files(id,parents,appProperties,trashed)",
            "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"})
        data = self._request("GET", url)
        files = data.get("files")
        if not isinstance(files, list) or data.get("nextPageToken") or data.get("incompleteSearch"):
            raise DriveError("Drive search incomplete")
        if len(files) > 1:
            raise DriveError("Duplicate Drive identities require operator reconciliation")
        return self._receipt(files[0], identity, sha256) if files else None

    def create(self, *, identity: str, sha256: str, name: str, mime_type: str, content: bytes):
        if not SHA.fullmatch(identity) or hashlib.sha256(content).hexdigest() != sha256:
            raise DriveError("Upload artifact integrity mismatch")
        if len(content) > MAX_ARTIFACT_BYTES or mime_type not in MIMES:
            raise DriveError("Unsupported upload artifact")
        _name(name)
        metadata = {"name": name, "mimeType": mime_type, "parents": [self.folder_id],
                    "appProperties": {"casita_upload_key": identity, "casita_sha256": sha256}}
        boundary = "casita_" + uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
                + _json(metadata).encode() + f"\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n".encode()
                + content + f"\r\n--{boundary}--\r\n".encode())
        url = "https://www.googleapis.com/upload/drive/v3/files?" + urlencode({
            "uploadType": "multipart", "supportsAllDrives": "true",
            "fields": "id,parents,appProperties,trashed"})
        data = self._request("POST", url, body, "multipart/related; boundary=" + boundary)
        try:
            return self._receipt(data, identity, sha256)
        except DriveError:
            raise UncertainUpload("Drive receipt mismatch; reconciliation required") from None


    def begin_upload(self, *, identity: str, sha256: str, name: str, mime_type: str, size: int):
        """Create a resumable session; the caller persists URI before sending bytes."""
        _name(name)
        if (not SHA.fullmatch(identity) or not SHA.fullmatch(sha256) or mime_type not in MIMES
                or type(size) is not int or not 0 < size <= MAX_ARTIFACT_BYTES):
            raise DriveError("Invalid upload artifact")
        metadata = {"name": name, "mimeType": mime_type, "parents": [self.folder_id],
                    "appProperties": {"casita_upload_key": identity, "casita_sha256": sha256}}
        url = "https://www.googleapis.com/upload/drive/v3/files?" + urlencode({
            "uploadType": "resumable", "supportsAllDrives": "true",
            "fields": "id,parents,appProperties,trashed"})
        response = self._request("POST", url, _json(metadata).encode(), "application/json; charset=UTF-8",
                                 extra_headers={"X-Upload-Content-Type": mime_type,
                                                "X-Upload-Content-Length": str(size)}, raw=True)
        uri = next((v for k, v in response.headers.items() if k.lower() == "location"), None)
        self._validate_upload_uri(uri)
        return uri

    @staticmethod
    def _validate_upload_uri(uri):
        if not isinstance(uri, str) or len(uri) > 4096:
            raise UncertainUpload("Invalid resumable upload location")
        parsed = urlsplit(uri)
        query = parse_qs(parsed.query)
        if (parsed.scheme != "https" or parsed.netloc != "www.googleapis.com" or parsed.fragment
                or parsed.path != "/upload/drive/v3/files" or query.get("uploadType") != ["resumable"]
                or len(query.get("upload_id", [])) != 1 or not query["upload_id"][0]):
            raise UncertainUpload("Unsafe resumable upload location")

    def resume_upload(self, uri: str, *, identity: str, sha256: str, mime_type: str, content: bytes,
                      check_status: bool = True):
        self._validate_upload_uri(uri)
        if len(content) > MAX_ARTIFACT_BYTES or hashlib.sha256(content).hexdigest() != sha256:
            raise DriveError("Upload artifact integrity mismatch")
        offset = 0
        if check_status:
            status = self._request("PUT", uri, b"", extra_headers={"Content-Range": f"bytes */{len(content)}"},
                                   raw=True, resume=True)
            if status.status != 308:
                return self._decode_receipt(status, identity, sha256)
            received = next((v for k, v in status.headers.items() if k.lower() == "range"), None)
            if received is not None:
                match = re.fullmatch(r"bytes=0-([0-9]+)", received)
                if not match:
                    raise UncertainUpload("Invalid resumable upload progress")
                offset = int(match[1]) + 1
                if not 0 <= offset < len(content):
                    raise UncertainUpload("Invalid resumable upload progress")
        response = self._request("PUT", uri, content[offset:], mime_type,
                                 extra_headers={"Content-Range": f"bytes {offset}-{len(content)-1}/{len(content)}"},
                                 raw=True, resume=True)
        if response.status == 308:
            raise UncertainUpload("Drive upload incomplete; resume required")
        return self._decode_receipt(response, identity, sha256)

    def _decode_receipt(self, response, identity, sha256):
        try:
            return self._receipt(json.loads(response.body), identity, sha256)
        except (DriveError, ValueError, TypeError):
            raise UncertainUpload("Drive receipt mismatch; reconciliation required") from None


def ensure_document_schema(conn):
    """Add document state inside the initialized, authorized Apollo database."""
    statements = [
        """CREATE TABLE IF NOT EXISTS care_document_jobs (
          job_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
          actor_id TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES health_sources(source_id),
          state TEXT NOT NULL, created_at TEXT NOT NULL, episode_id TEXT, visit_id TEXT,
          plan_json TEXT, manifest_json TEXT, derivative_sha TEXT, last_error TEXT, UNIQUE(actor_id,request_id))""",
        """CREATE TABLE IF NOT EXISTS care_document_sources (
          job_id TEXT NOT NULL REFERENCES care_document_jobs(job_id), source_index INTEGER NOT NULL,
          name TEXT NOT NULL, mime_type TEXT NOT NULL, sha256 TEXT NOT NULL,
          page_count INTEGER NOT NULL, byte_count INTEGER NOT NULL, raw_record_id TEXT NOT NULL,
          PRIMARY KEY(job_id,source_index))""",
        """CREATE TABLE IF NOT EXISTS care_document_uploads (
          job_id TEXT NOT NULL REFERENCES care_document_jobs(job_id), item_key TEXT NOT NULL,
          sha256 TEXT NOT NULL, mime_type TEXT NOT NULL, name TEXT NOT NULL,
          upload_key TEXT NOT NULL UNIQUE, folder_id TEXT, state TEXT NOT NULL,
          receipt_json TEXT, last_error TEXT, upload_session_uri TEXT, PRIMARY KEY(job_id,item_key))""",
        """CREATE TABLE IF NOT EXISTS care_document_proposals (
          proposal_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES care_document_jobs(job_id),
          request_id TEXT NOT NULL, source_index INTEGER NOT NULL, page_number INTEGER NOT NULL,
          quote TEXT NOT NULL, proposal_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'unconfirmed',
          created_at TEXT NOT NULL, UNIQUE(job_id,request_id))""",
    ]
    for sql in statements:
        conn.execute(sql)


class DocumentWorkflow:
    def __init__(self, session, *, runtime_root: Path, drive: GoogleDriveAdapter | None = None):
        from .care_store import CareSession
        if not isinstance(session, CareSession):
            raise PermissionError("An authorized care session is required")
        writer = "write" in session.authority.permissions
        session.require("write" if writer else "read")
        self.session = session
        self.drive = drive
        if not isinstance(runtime_root, Path) or not runtime_root.is_absolute():
            raise DocumentError("A trusted absolute private artifact root is required")
        if runtime_root.resolve().is_relative_to(Path(__file__).resolve().parent):
            raise DocumentError("Medical artifacts must remain outside the plugin source tree")
        # Scope hashes, never model paths, partition a host-owned runtime directory.
        self.root = runtime_root / hashlib.sha256(session.authority.scope_id.encode()).hexdigest()
        with session.connection(permission="write" if writer else "read") as conn:
            if writer:
                ensure_document_schema(conn)
            elif not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='care_document_jobs'").fetchone():
                raise DocumentError("Document storage requires initialization by an authorized writer")
        self._check_root(create=writer)

    def _check_root(self, *, create=False):
        parts = list(reversed([self.root, *self.root.parents]))
        for path in parts:
            if path.exists() or path.is_symlink():
                if path.is_symlink() or not path.is_dir():
                    raise DocumentError("Unsafe artifact directory")
            elif create:
                path.mkdir(mode=0o700)
            else:
                raise DocumentError("Artifact directory unavailable")
        info = self.root.stat()
        if info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise DocumentError("Artifact directory must be private and host owned")

    def _put(self, data: bytes):
        if len(data) > MAX_ARTIFACT_BYTES:
            raise DocumentError("Artifact exceeds size limit")
        self._check_root()
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / digest
        if path.exists() or path.is_symlink():
            if self._read(digest) != data:
                raise DocumentError("Artifact integrity mismatch")
            return digest
        fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            # Hard-link publishes without replacing a competing immutable object.
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                self._read(digest)
            _fsync_directory(self.root)
        finally:
            os.unlink(temporary)
        return digest

    def _read(self, digest):
        if not isinstance(digest, str) or not SHA.fullmatch(digest):
            raise DocumentError("Invalid artifact identity")
        self._check_root()
        try:
            fd = os.open(self.root / digest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_BYTES:
                    raise DocumentError("Unsafe artifact")
                data = source.read(MAX_ARTIFACT_BYTES + 1)
        except OSError:
            raise DocumentError("Artifact unavailable") from None
        if len(data) > MAX_ARTIFACT_BYTES or hashlib.sha256(data).hexdigest() != digest:
            raise DocumentError("Artifact integrity mismatch")
        return data

    def intake(self, sources: Sequence[DocumentInput], *, request_id: str,
               episode_id: str | None = None, visit_id: str | None = None):
        self.session.require("write")
        _request_id(request_id)
        if not isinstance(sources, (list, tuple)) or not 1 <= len(sources) <= MAX_PAGES:
            raise DocumentError("Document batch must contain 1 to 20 files")
        total = 0
        rows = []
        for index, source in enumerate(sources):
            if not isinstance(source, DocumentInput):
                raise DocumentError("Invalid source")
            _name(source.name)
            if (source.mime_type not in MIMES or not isinstance(source.content, bytes)
                    or not 0 < len(source.content) <= MAX_FILE_BYTES):
                raise DocumentError("Unsupported or oversized source")
            total += len(source.content)
            if total > MAX_BATCH_BYTES:
                raise DocumentError("Document batch exceeds size limit")
            count = _inspect(source.content, source.mime_type)
            rows.append({"source_index": index, "name": source.name, "mime_type": source.mime_type,
                         "sha256": hashlib.sha256(source.content).hexdigest(),
                         "page_count": count, "byte_count": len(source.content)})
        if sum(r["page_count"] for r in rows) > MAX_PAGES:
            raise DocumentError("Document batch exceeds page limit")
        fingerprint = hashlib.sha256(_json({"sources": rows, "episode_id": episode_id,
                                             "visit_id": visit_id}).encode()).hexdigest()
        with self.session.connection(permission="write") as conn:
            self._validate_links(conn, episode_id, visit_id)
            old = conn.execute("SELECT * FROM care_document_jobs WHERE actor_id=? AND request_id=?", (self.session.authority.actor_id, request_id)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise DocumentError("Request replay has different content")
                return self._job(conn, old["job_id"])
            from . import sync_control
            source_id = sync_control.ensure_source(conn, source_slug="care_documents", provider="manual",
                                                  connection_name="Care documents", status="manual", sync_mode="manual")
            job_id = "doc_" + uuid.uuid4().hex
            conn.execute("""INSERT INTO care_document_jobs(job_id,request_id,fingerprint,actor_id,
                         source_id,state,created_at,episode_id,visit_id) VALUES(?,?,?,?,?,'received',?,?,?)""",
                         (job_id, request_id, fingerprint, self.session.authority.actor_id,
                          source_id, _now(), episode_id, visit_id))
            for source, row in zip(sources, rows):
                self._put(source.content)
                raw_id = "docraw_" + uuid.uuid4().hex
                conn.execute("""INSERT INTO raw_records(raw_record_id,source_id,provider,object_type,
                    external_id,payload_json,source_record_hash,extracted_at,privacy_tier)
                    VALUES(?,?,'manual','care_document',?,?,?,?,'sensitive')""",
                    (raw_id, source_id, f"{job_id}:{row['source_index']}", _json(row), row["sha256"], _now()))
                conn.execute("""INSERT INTO care_document_sources VALUES(?,?,?,?,?,?,?,?)""",
                             (job_id, row["source_index"], row["name"], row["mime_type"], row["sha256"],
                              row["page_count"], row["byte_count"], raw_id))
                conn.execute("""INSERT INTO record_lineage(lineage_id,canonical_table,canonical_id,
                             raw_record_id,normalization_version,lineage_role,created_at) VALUES(?,?,?,?,?,?,?)""",
                             (uuid.uuid4().hex, "care_document_jobs", job_id, raw_id, "document-v1", "source", _now()))
            return self._job(conn, job_id)

    @staticmethod
    def _validate_links(conn, episode_id, visit_id):
        for value, kind in [(episode_id, "episode"), (visit_id, "visit")]:
            if value is not None:
                if not isinstance(value, str) or not value or len(value) > 200:
                    raise DocumentError("Invalid document link")
                if not conn.execute("SELECT 1 FROM care_cases WHERE case_id=? AND kind=?", (value, kind)).fetchone():
                    raise DocumentError("Document link unavailable")

    def get(self, job_id: str):
        with self.session.connection(permission="read") as conn:
            return self._job(conn, job_id)

    def _job(self, conn, job_id):
        row = conn.execute("SELECT * FROM care_document_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise DocumentError("Document job unavailable")
        result = dict(row)
        result["sources"] = [dict(r) for r in conn.execute(
            "SELECT * FROM care_document_sources WHERE job_id=? ORDER BY source_index", (job_id,))]
        result["uploads"] = [{k: r[k] for k in r.keys() if k != "upload_session_uri"} for r in conn.execute(
            "SELECT * FROM care_document_uploads WHERE job_id=? ORDER BY item_key", (job_id,))]
        result["source_ref"] = "document:" + job_id
        result["proposals"] = [dict(r) for r in conn.execute(
            "SELECT * FROM care_document_proposals WHERE job_id=? ORDER BY created_at,proposal_id", (job_id,))]
        return result

    def artifact(self, job_id: str, *, source_index: int | None = None, page_number: int | None = None):
        """Authorized bytes for host media transport; no arbitrary path/digest input."""
        with self.session.connection(permission="read") as conn:
            job = self._job(conn, job_id)
            if source_index is not None and page_number is not None:
                raise DocumentError("Select an original or an output page")
            if source_index is not None:
                if type(source_index) is not int or not 0 <= source_index < len(job["sources"]):
                    raise DocumentError("Original source unavailable")
                source = job["sources"][source_index]
                return {"content": self._read(source["sha256"]), "mime_type": source["mime_type"],
                        "sha256": source["sha256"]}
            if page_number is not None:
                if not job["manifest_json"]:
                    raise DocumentError("Document has not been prepared")
                pages = json.loads(job["manifest_json"])["pages"]
                if type(page_number) is not int or not 1 <= page_number <= len(pages):
                    raise DocumentError("Output page unavailable")
                digest = pages[page_number - 1]["page_sha256"]
                return {"content": self._read(digest), "mime_type": "image/png", "sha256": digest}
            if not job["derivative_sha"]:
                raise DocumentError("Document has not been prepared")
            return {"content": self._read(job["derivative_sha"]), "mime_type": "application/pdf",
                    "sha256": job["derivative_sha"]}

    def prepare(self, job_id: str, *, pages: Sequence[PageEdit] | None = None):
        """Create immutable ordered PDF and provenance. Explicit crops need review.

        Defaults preserve every page and margin; EXIF orientation and small-angle
        deskew use only deterministic pixels. No OCR or invented text is used.
        """
        with self.session.connection(permission="write") as conn:
            job = self._job(conn, job_id)
            default = [PageEdit(row["source_index"], n) for row in job["sources"]
                       for n in range(1, row["page_count"] + 1)]
            selected = ([PageEdit(**p) for p in json.loads(job["plan_json"])]
                        if pages is None and job["plan_json"] else default if pages is None else pages)
            if not isinstance(selected, (tuple, list)) or not 1 <= len(selected) <= MAX_PAGES:
                raise DocumentError("Invalid page plan")
            if any(not isinstance(p, PageEdit) for p in selected):
                raise DocumentError("Invalid page plan")
            if sorted((p.source_index, p.page_number) for p in selected) != sorted(
                    (p.source_index, p.page_number) for p in default):
                raise DocumentError("Page plan must preserve every source page exactly once")
            for edit in selected:
                if type(edit.source_index) is not int or type(edit.page_number) is not int:
                    raise DocumentError("Invalid source page identity")
                if type(edit.rotate) is not int or edit.rotate not in (0, 90, 180, 270) or type(edit.deskew) is not bool:
                    raise DocumentError("Invalid page transform")
                if edit.crop is not None and (not isinstance(edit.crop, (list, tuple)) or len(edit.crop) != 4
                                             or any(type(v) is not int for v in edit.crop)):
                    raise DocumentError("Invalid crop")
            plan_json = _json([asdict(p) for p in selected])
            if job["plan_json"] is not None and job["plan_json"] != plan_json:
                raise DocumentError("Prepared document plan is immutable; create a new intake request")
            if job["derivative_sha"]:
                self._read(job["derivative_sha"])
                return job
            conn.execute("UPDATE care_document_jobs SET state='preparing',plan_json=?,last_error=NULL WHERE job_id=?",
                         (plan_json, job_id))
        images = []
        provenance = []
        total_pixels = 0
        try:
            for edit in selected:
                row = job["sources"][edit.source_index]
                image = _load_page(self._read(row["sha256"]), row["mime_type"], edit.page_number)
                total_pixels += image.width * image.height
                if total_pixels > MAX_BATCH_PIXELS:
                    raise DocumentError("Rendered batch exceeds pixel limit")
                transformed, applied = _transform(image, edit)
                images.append(transformed)
                page_buf = _BoundedBytes()
                transformed.save(page_buf, format="PNG")
                page_sha = self._put(page_buf.getvalue())
                provenance.append({"output_page": len(images), "source_sha256": row["sha256"],
                    "source_index": edit.source_index, "source_page": edit.page_number,
                    "raw_record_id": row["raw_record_id"], "transforms": applied,
                    "page_sha256": page_sha, "width": transformed.width, "height": transformed.height})
            pdf = _BoundedBytes()
            images[0].save(pdf, format="PDF", save_all=True, append_images=images[1:], resolution=150.0,
                           quality=95, subsampling=0, creationDate="", modDate="", title="Care document")
            derivative_sha = self._put(pdf.getvalue())
            manifest = {"version": 1, "job_id": job_id, "derivative_sha256": derivative_sha,
                        "method": "deterministic_raster_v1", "pages": provenance,
                        "extraction_status": "not_performed", "originals_preserved": True}
            manifest_json = _json(manifest)
            self._put(manifest_json.encode())
            with self.session.connection(permission="write") as conn:
                conn.execute("""UPDATE care_document_jobs SET state='prepared',manifest_json=?,derivative_sha=?,
                               last_error=NULL WHERE job_id=?""", (manifest_json, derivative_sha, job_id))
                items = [("derivative", derivative_sha, "application/pdf", f"{job_id}.pdf")]
                items += [(f"original_{r['source_index']:03}", r["sha256"], r["mime_type"],
                           f"{job_id}_original_{r['source_index'] + 1}.{MIMES[r['mime_type']]}") for r in job["sources"]]
                for key, digest, mime, name in items:
                    identity = hashlib.sha256(_json([self.session.authority.scope_id, job_id, key, digest]).encode()).hexdigest()
                    conn.execute("""INSERT OR IGNORE INTO care_document_uploads(job_id,item_key,sha256,mime_type,
                                 name,upload_key,state) VALUES(?,?,?,?,?,?,'pending')""",
                                 (job_id, key, digest, mime, name, identity))
                return self._job(conn, job_id)
        except Exception:
            with self.session.connection(permission="write") as conn:
                conn.execute("UPDATE care_document_jobs SET last_error='preparation_failed' WHERE job_id=?", (job_id,))
            raise
        finally:
            for image in images:
                image.close()

    def upload(self, job_id: str, *, retry_uncertain: bool = False):
        """Resume/reconcile all artifacts. Uncertain empty searches never auto-resend.

        ``retry_uncertain=True`` is for a host/operator who accepts duplicate risk
        after reconciliation, not an exactly-once guarantee or a model default.
        """
        self.session.require("write")
        if self.drive is None:
            raise DriveError("Host Drive connection is not configured")
        job = self.get(job_id)
        if not job["derivative_sha"]:
            raise DocumentError("Document must be prepared before upload")
        for item in job["uploads"]:
            with self.session.connection(permission="write") as conn:
                current = conn.execute("SELECT * FROM care_document_uploads WHERE job_id=? AND item_key=?",
                                       (job_id, item["item_key"])).fetchone()
                if current["folder_id"] and current["folder_id"] != self.drive.folder_id:
                    raise DriveError("Configured folder changed; operator reconciliation required")
                if current["state"] == "uploaded":
                    continue
                uncertain = current["state"] in ("uploading", "uncertain")
                upload_uri = current["upload_session_uri"]
                conn.execute("""UPDATE care_document_uploads SET state='uploading',folder_id=?
                             WHERE job_id=? AND item_key=?""", (self.drive.folder_id, job_id, item["item_key"]))
            try:
                content = self._read(item["sha256"])
                receipt = self.drive.find(identity=item["upload_key"], sha256=item["sha256"])
                if receipt is None and upload_uri:
                    receipt = self.drive.resume_upload(upload_uri, identity=item["upload_key"], sha256=item["sha256"],
                                                       mime_type=item["mime_type"], content=content)
                if receipt is None and uncertain and not retry_uncertain:
                    raise UncertainUpload("No matching receipt yet; operator reconciliation required")
                if receipt is None and len(content) > 5 * 1024 * 1024:
                    upload_uri = self.drive.begin_upload(identity=item["upload_key"], sha256=item["sha256"],
                                                         name=item["name"], mime_type=item["mime_type"], size=len(content))
                    with self.session.connection(permission="write") as conn:
                        conn.execute("UPDATE care_document_uploads SET upload_session_uri=? WHERE job_id=? AND item_key=?",
                                     (upload_uri, job_id, item["item_key"]))
                    receipt = self.drive.resume_upload(upload_uri, identity=item["upload_key"], sha256=item["sha256"],
                                                       mime_type=item["mime_type"], content=content, check_status=False)
                if receipt is None:
                    receipt = self.drive.create(identity=item["upload_key"], sha256=item["sha256"],
                                                name=item["name"], mime_type=item["mime_type"], content=content)
            except (DriveError, DocumentError) as exc:
                error = "uncertain" if isinstance(exc, UncertainUpload) else "rejected"
                with self.session.connection(permission="write") as conn:
                    conn.execute("""UPDATE care_document_uploads SET state=?,last_error=?
                                 WHERE job_id=? AND item_key=?""", (error, error, job_id, item["item_key"]))
                    conn.execute("UPDATE care_document_jobs SET state=?,last_error=? WHERE job_id=?",
                                 (error, error, job_id))
                raise
            with self.session.connection(permission="write") as conn:
                conn.execute("""UPDATE care_document_uploads SET state='uploaded',receipt_json=?,last_error=NULL
                             WHERE job_id=? AND item_key=?""", (_json(receipt), job_id, item["item_key"]))
        with self.session.connection(permission="write") as conn:
            conn.execute("UPDATE care_document_jobs SET state='uploaded',last_error=NULL WHERE job_id=?", (job_id,))
            return self._job(conn, job_id)

    def propose(self, job_id: str, *, request_id: str, source_index: int, page_number: int,
                quote: str, proposal: dict):
        """Save unconfirmed extraction evidence, never activate a medication plan.

        The host may run OCR/extraction separately. Quotes remain unverified until
        reviewed against the linked original. This API cannot confirm proposals.
        """
        _request_id(request_id)
        if (not isinstance(quote, str) or not quote.strip() or len(quote) > 8000
                or not isinstance(proposal, dict)):
            raise DocumentError("Invalid extraction proposal")
        try:
            payload = _json(proposal)
        except (ValueError, TypeError, RecursionError):
            raise DocumentError("Invalid extraction proposal") from None
        if len(payload.encode()) > 16000 or _depth(proposal) > 8:
            raise DocumentError("Extraction proposal exceeds bounds")
        with self.session.connection(permission="write") as conn:
            job = self._job(conn, job_id)
            if (type(source_index) is not int or type(page_number) is not int
                    or not 0 <= source_index < len(job["sources"])
                    or not 1 <= page_number <= job["sources"][source_index]["page_count"]):
                raise DocumentError("Proposal source page unavailable")
            old = conn.execute("SELECT * FROM care_document_proposals WHERE job_id=? AND request_id=?",
                               (job_id, request_id)).fetchone()
            if old:
                if (old["proposal_json"], old["quote"], old["source_index"], old["page_number"]) != (
                        payload, quote, source_index, page_number):
                    raise DocumentError("Proposal replay has different content")
                return dict(old)
            proposal_id = "proposal_" + uuid.uuid4().hex
            conn.execute("""INSERT INTO care_document_proposals(proposal_id,job_id,request_id,source_index,
                         page_number,quote,proposal_json,state,created_at) VALUES(?,?,?,?,?,?,?,'unconfirmed',?)""",
                         (proposal_id, job_id, request_id, source_index, page_number, quote, payload, _now()))
            return dict(conn.execute("SELECT * FROM care_document_proposals WHERE proposal_id=?", (proposal_id,)).fetchone())


class _BoundedBytes(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_ARTIFACT_BYTES:
            raise DocumentError("Artifact exceeds size limit")
        return super().write(data)


def _inspect(data, mime):
    if mime == "application/pdf":
        if not data.startswith(b"%PDF-"):
            raise DocumentError("Source does not match declared PDF type")
        pdfium = _pdfium()
        try:
            with pdfium.PdfDocument(data) as pdf:
                count = len(pdf)
                if not 1 <= count <= MAX_PAGES:
                    raise DocumentError("PDF exceeds page limit")
                for page in pdf:
                    try:
                        width, height = page.get_size()
                        if width <= 0 or height <= 0 or width * height * (150 / 72) ** 2 > MAX_PIXELS:
                            raise DocumentError("PDF page exceeds pixel limit")
                    finally:
                        page.close()
                return count
        except DocumentError:
            raise
        except Exception:
            raise DocumentError("PDF is unreadable or password protected") from None
    Image, _ = _pillow()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                expected = "PNG" if mime == "image/png" else "JPEG"
                if image.format != expected or getattr(image, "n_frames", 1) != 1:
                    raise DocumentError("Source does not match supported image type")
                if image.width * image.height > MAX_PIXELS:
                    raise DocumentError("Image exceeds pixel limit")
                image.verify()
                return 1
    except DocumentError:
        raise
    except Exception:
        raise DocumentError("Image is unreadable or exceeds bounds") from None


def _load_page(data, mime, number):
    Image, ImageOps = _pillow()
    if mime == "application/pdf":
        with _pdfium().PdfDocument(data) as pdf:
            page = pdf[number - 1]
            try:
                bitmap = page.render(scale=150 / 72)
                try:
                    image = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
            finally:
                page.close()
            return image
    with Image.open(io.BytesIO(data)) as source:
        orientation = source.getexif().get(274, 1)
        image = ImageOps.exif_transpose(source).convert("RGBA")
        white = Image.new("RGBA", image.size, "white")
        white.alpha_composite(image)
        image.close()
        result = white.convert("RGB")
        white.close()
        result.info["original_exif_orientation"] = orientation
        return result


def _transform(image, edit):
    Image, _ = _pillow()
    applied = {"exif_orientation": image.info.get("original_exif_orientation"), "rotate": edit.rotate, "crop": edit.crop, "deskew_degrees": 0.0}
    if edit.rotate:
        rotated = image.rotate(edit.rotate, expand=True, fillcolor="white")
        image.close()
        image = rotated
    if edit.crop:
        left, top, right, bottom = edit.crop
        if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
            image.close()
            raise DocumentError("Crop exceeds oriented page bounds")
        cropped = image.crop(edit.crop)
        image.close()
        image = cropped
    if edit.deskew:
        angle = _deskew_angle(image)
        if angle:
            rotated = image.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
            image.close()
            image = rotated
            applied["deskew_degrees"] = angle
    if image.width * image.height > MAX_PIXELS:
        image.close()
        raise DocumentError("Transformed page exceeds pixel limit")
    return image, applied


def _deskew_angle(image):
    """Conservative line-projection deskew, no margin cropping or text generation."""
    try:
        import numpy as np
    except ImportError:
        raise DocumentError("Install the documents optional dependencies") from None
    sample = image.convert("L")
    sample.thumbnail((900, 900))
    pixels = np.asarray(sample)
    # Empty photographs/low-contrast pages get no guessed rotation.
    ink = pixels < 180
    if int(ink.sum()) < 100:
        sample.close()
        return 0.0
    mask = _pillow()[0].fromarray((~ink * 255).astype("uint8"))
    angles = [n / 4 for n in range(-20, 21)]
    scores = []
    for angle in angles:
        rotated = mask.rotate(angle, expand=True, fillcolor=255)
        projection = (np.asarray(rotated) < 128).sum(axis=1).astype(float)
        scores.append(float(np.var(projection)))
        rotated.close()
    sample.close()
    mask.close()
    best = max(range(len(scores)), key=scores.__getitem__)
    baseline = scores[20]
    # Avoid uncertain extremes and tiny gains that needlessly resample text.
    if best in (0, 40) or abs(angles[best]) < 0.5 or scores[best] < baseline * 1.15:
        return 0.0
    return angles[best]


def _pillow():
    try:
        from PIL import Image, ImageOps
        return Image, ImageOps
    except ImportError:
        raise DocumentError("Install the documents optional dependencies") from None


def _pdfium():
    try:
        import pypdfium2
        return pypdfium2
    except ImportError:
        raise DocumentError("Install the documents optional dependencies") from None


def _name(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 200 or value in (".", "..")
            or any(c in value for c in ("/", "\\")) or any(ord(c) < 32 for c in value)):
        raise DocumentError("Unsafe source name")


def _request_id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or any(ord(c) < 32 for c in value):
        raise DocumentError("Invalid request identity")


def _depth(value):
    if isinstance(value, dict):
        return 1 + max((_depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_depth(v) for v in value), default=0)
    return 0


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
