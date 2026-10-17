from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def load_secret_scan():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "secret_scan.py"
    spec = importlib.util.spec_from_file_location("secret_scan", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["secret_scan"] = module
    spec.loader.exec_module(module)
    return module


def test_secret_scan_allows_obvious_test_placeholders(tmp_path):
    secret_scan = load_secret_scan()
    fixture = tmp_path / "fixture.py"
    fixture.write_text(
        "\n".join(
            [
                'client_secret = "client-secret"',
                'access_token = "secret-access"',
                'refresh_token = "old-refresh"',
                'email = "user@example.test"',
            ]
        ),
        encoding="utf-8",
    )

    assert secret_scan.scan([fixture]) == []


def test_secret_scan_blocks_credential_files_and_values(tmp_path):
    secret_scan = load_secret_scan()
    fixture = tmp_path / "client_secret_google.json"
    secret_value = "GOCSPX-" + "aBcDeFgHiJkLmNoPqRsTuVwXyZ123456789"
    fixture.write_text(
        f'{{"client_secret": "{secret_value}"}}',
        encoding="utf-8",
    )

    findings = secret_scan.scan([fixture])

    assert {finding.kind for finding in findings} == {
        "blocked_path",
        "sensitive_assignment",
    }


def test_secret_scan_blocks_personal_emails(tmp_path):
    secret_scan = load_secret_scan()
    fixture = tmp_path / "notes.md"
    address = "person" + "@" + "gmail.com"
    fixture.write_text(f"Contact me at {address}\n", encoding="utf-8")

    findings = secret_scan.scan([fixture])

    assert [finding.kind for finding in findings] == ["personal_email"]


def test_secret_scan_allows_public_project_contact(tmp_path):
    secret_scan = load_secret_scan()
    fixture = tmp_path / "README.md"
    fixture.write_text("Maintainer: RTK <apollo@ultima.inc>\n", encoding="utf-8")

    assert secret_scan.scan([fixture]) == []


def test_secret_scan_blocks_map_and_location_exports(tmp_path):
    secret_scan = load_secret_scan()
    fixtures = [
        tmp_path / "bundle.JS.MAP",
        tmp_path / "morning-run.GPX",
        tmp_path / "trail.KML",
        tmp_path / "ride.FIT",
        tmp_path / "workout.TCX",
        tmp_path / "offline-map.MBTILES",
        tmp_path / "LOCATION-HISTORY.JSON",
        tmp_path / "cycling-ROUTES.json",
        tmp_path / "Google-Takeout.JSON",
    ]
    for fixture in fixtures:
        if fixture.suffix.casefold() == ".mbtiles":
            fixture.write_bytes(b"SQLite format 3\0")
        else:
            fixture.write_text("{}", encoding="utf-8")

    findings = secret_scan.scan(fixtures)

    assert [finding.kind for finding in findings] == ["blocked_path"] * len(fixtures)


def test_secret_scan_blocks_private_workspace_paths(tmp_path):
    secret_scan = load_secret_scan()
    private_files = [
        tmp_path / "docs" / "plan.md",
        tmp_path / "docker" / "dev.Dockerfile",
        tmp_path / ".omx" / "state.json",
        tmp_path / "plans" / "release.md",
        tmp_path / ".context" / "handoff.md",
    ]
    for path in private_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private workspace note\n", encoding="utf-8")

    findings = secret_scan.scan(private_files)

    assert [finding.kind for finding in findings] == ["blocked_path"] * len(private_files)


def test_reviewed_care_source_docs_still_scan_contents(tmp_path, monkeypatch):
    scanner = load_secret_scan()
    monkeypatch.chdir(tmp_path)
    document = Path("docs/casita/INSTALL.md")
    document.parent.mkdir(parents=True)
    document.write_text("Synthetic setup guide\n")
    assert scanner.scan([document]) == []
    document.write_text("api_key = '" + "sk-" + "A" * 30 + "'\n")
    assert "openai_api_key" in {f.kind for f in scanner.scan([document])}
    other = Path("docs/casita/patient-private.md")
    other.write_text("private runtime data belongs elsewhere\n")
    assert "blocked_path" in {f.kind for f in scanner.scan([other])}
