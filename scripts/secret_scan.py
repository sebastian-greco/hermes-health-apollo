from __future__ import annotations

import argparse
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable


MAX_TEXT_BYTES = 2_000_000
# Reviewed source documentation in this private derivative. Only path blocking
# is exempted; every content/credential check below still runs on these files.
REVIEWED_SOURCE_DOCS = frozenset("docs/casita/" + name for name in (
    "PLAN.md", "NEXT_SESSION_PROMPT.md", "CORE_API.md", "REUSE_MAP.md",
    "DOCUMENTS.md", "DRIVE_AUTH.md", "REMINDERS.md", "EGRESS.md",
    "EXTRACTION.md", "HERMES_COMPATIBILITY.md", "INSTALL.md", "STATUS.md", "REPORTS.md",
))
SKIP_ALL_FILE_DIRS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}


BLOCKED_PATH_PATTERNS = (
    ".context/*",
    ".local/*",
    ".omx/*",
    ".private/*",
    ".env",
    ".env.*",
    "*/.context/*",
    "*/.local/*",
    "*/.omx/*",
    "*/.private/*",
    "*/docker/*",
    "*/docs/*",
    "*/plans/*",
    "*/scratch/*",
    "*.db",
    "*.db-*",
    "*.sqlite",
    "*.sqlite-*",
    "*.sqlite3",
    "*.sqlite3-*",
    "*.pem",
    "*.key",
    "*.crt",
    "*.p12",
    "*.pfx",
    "*.log",
    "*.jsonl",
    "*.map",
    "*.geojson",
    "*.gpx",
    "*.kml",
    "*.kmz",
    "*.fit",
    "*.tcx",
    "*.mbtiles",
    "*.osm",
    "*.pbf",
    "*location*.json",
    "*locations*.json",
    "*route*.json",
    "*routes*.json",
    "*timeline*.json",
    "*takeout*.json",
    "*client_secret*.json",
    "*credential*.json",
    "*credentials*.json",
    "*service-account*.json",
    "*service_account*.json",
    "*token*.json",
    "docker/*",
    "docs/*",
    "plans/*",
    "scratch/*",
)


SECRET_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{30,})\b")),
    ("openai_api_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("oauth_callback_code", re.compile(r"\bcode=4/[A-Za-z0-9_-]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b")),
)


SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)['\"]?\b("
    r"access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|"
    r"api[_-]?key|secret[_-]?key|private[_-]?key|password"
    r")\b['\"]?\s*[:=]\s*['\"]([^'\"]{4,})['\"]"
)


LOCAL_PATH_PREFIXES = ("/" + "Users/", "/" + "home/", "C:" + "\\Users\\")
LOCAL_PATH_PATTERN = re.compile(
    "(?i)(" + "|".join(re.escape(prefix) + r"[^/\\ \t\"']+" for prefix in LOCAL_PATH_PREFIXES) + ")"
)


EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)


PLACEHOLDER_WORDS = (
    "access",
    "client",
    "dummy",
    "example",
    "fake",
    "fixture",
    "loopback",
    "old",
    "placeholder",
    "refresh",
    "secret",
    "test",
)


ALLOWED_EMAIL_DOMAINS = (
    "example.com",
    "example.org",
    "example.net",
    "example.test",
)


ALLOWED_EMAIL_ADDRESSES = {
    "apollo@ultima.inc",
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    detail: str

    def format(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where}: {self.kind}: {self.detail}"


def tracked_files() -> list[Path]:
    output = subprocess.check_output(["git", "ls-files", "-z"])
    return [Path(item.decode()) for item in output.split(b"\0") if item]


def all_repo_files() -> list[Path]:
    root = Path(".")
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_ALL_FILE_DIRS or part.endswith(".egg-info") for part in path.parts):
            continue
        files.append(path)
    return files


def is_placeholder(value: str) -> bool:
    normalized = value.lower()
    if len(value) < 24:
        return True
    return any(word in normalized for word in PLACEHOLDER_WORDS)


def is_text(data: bytes) -> bool:
    return b"\0" not in data[:4096]


def path_matches(patterns: Iterable[str], path: Path) -> str | None:
    normalized = path.as_posix().casefold()
    name = path.name.casefold()
    for pattern in patterns:
        folded_pattern = pattern.casefold()
        if fnmatch(name, folded_pattern) or fnmatch(normalized, folded_pattern):
            return pattern
    return None


def redact(value: str) -> str:
    value = value.strip()
    if len(value) <= 8:
        return "[REDACTED]"
    return f"{value[:4]}...[REDACTED]...{value[-4:]}"


def scan_path(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    blocked = path_matches(BLOCKED_PATH_PATTERNS, path)
    if blocked and path.as_posix() not in REVIEWED_SOURCE_DOCS:
        findings.append(
            Finding(
                path=path.as_posix(),
                line=0,
                kind="blocked_path",
                detail=f"matches {blocked}; keep private workspace artifacts, local secrets, DBs, logs, and tokens out of git",
            )
        )

    try:
        data = path.read_bytes()
    except OSError as exc:
        findings.append(Finding(path=path.as_posix(), line=0, kind="read_error", detail=str(exc)))
        return findings

    if len(data) > MAX_TEXT_BYTES or not is_text(data):
        return findings

    text = data.decode("utf-8", errors="replace")
    for line_number, line in enumerate(text.splitlines(), 1):
        for kind, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(
                    Finding(path.as_posix(), line_number, kind, pattern.sub("[REDACTED]", line.strip())[:160])
                )

        for match in SENSITIVE_ASSIGNMENT.finditer(line):
            value = match.group(2)
            if not is_placeholder(value):
                findings.append(
                    Finding(
                        path.as_posix(),
                        line_number,
                        "sensitive_assignment",
                        f"{match.group(1)}={redact(value)}",
                    )
                )

        local_path = LOCAL_PATH_PATTERN.search(line)
        if local_path:
            findings.append(
                Finding(
                    path.as_posix(),
                    line_number,
                    "personal_local_path",
                    LOCAL_PATH_PATTERN.sub("[LOCAL_PATH]", line.strip())[:160],
                )
            )

        for email_match in EMAIL_PATTERN.finditer(line):
            email = email_match.group(0)
            domain = email.rsplit("@", 1)[-1].lower()
            if email.lower() not in ALLOWED_EMAIL_ADDRESSES and domain not in ALLOWED_EMAIL_DOMAINS:
                findings.append(
                    Finding(
                        path.as_posix(),
                        line_number,
                        "personal_email",
                        EMAIL_PATTERN.sub("[EMAIL]", line.strip())[:160],
                    )
                )
                break

    return findings


def scan(paths: Iterable[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        findings.extend(scan_path(path))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail on committed secrets or private local artifacts.")
    parser.add_argument(
        "--all-files",
        action="store_true",
        help="Scan all files in the working tree instead of only tracked files.",
    )
    parser.add_argument("paths", nargs="*", help="Specific files or directories to scan.")
    args = parser.parse_args(argv)

    if args.paths:
        paths: list[Path] = []
        for raw in args.paths:
            path = Path(raw)
            if path.is_dir():
                paths.extend(item for item in path.rglob("*") if item.is_file())
            else:
                paths.append(path)
    elif args.all_files:
        paths = all_repo_files()
    else:
        paths = tracked_files()

    findings = scan(paths)
    if findings:
        print("Secret scan failed. Remove these files/values or replace them with safe fixtures:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding.format()}", file=sys.stderr)
        return 1

    print(f"Secret scan passed ({len(paths)} files scanned).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
