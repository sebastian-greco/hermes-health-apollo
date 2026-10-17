from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

BLOCKED_ARTIFACT_PATTERNS = (
    ".context/*",
    ".omx/*",
    ".private/*",
    ".local/*",
    ".env",
    ".env.*",
    "docs/*",
    "docker/*",
    "plans/*",
    "scratch/*",
    "*/.context/*",
    "*/.omx/*",
    "*/.private/*",
    "*/.local/*",
    "*/docs/*",
    "*/docker/*",
    "*/plans/*",
    "*/scratch/*",
    "*.db",
    "*.db-*",
    "*.sqlite",
    "*.sqlite-*",
    "*.sqlite3",
    "*.sqlite3-*",
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
)

REQUIRED_PACKAGE_ASSETS = (
    "health_data_assets/plugin.yaml",
    "health_data_assets/skills/health-coach/SKILL.md",
    "health_data_assets/skills/health-visuals/SKILL.md",
    "health_data_assets/skills/health-visuals/references/cli_visual_patterns.md",
    "health_data_assets/skills/health-visuals/references/terminal_ascii_patterns.md",
    "health_data_assets/skills/health-visuals/references/ansi_visual_patterns.md",
    "health_data_assets/skills/health-visuals/references/ansi_color_implementation_plan.md",
    "health_data_assets/visuals/cli/README.md",
    "health_data_assets/visuals/cli/prompts/health_visual_brief.md",
    "health_data_assets/visuals/cli/prompts/interpretation_guardrails.md",
)


def run(command: list[str], *, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=True, text=True, capture_output=True)


def git_files(cwd: Path = ROOT) -> list[str]:
    output = run(["git", "ls-files", "-z"], cwd=cwd).stdout.encode()
    return [item.decode() for item in output.split(b"\0") if item]


def matches_any(path: str, patterns: tuple[str, ...] = BLOCKED_ARTIFACT_PATTERNS) -> str | None:
    normalized = path.replace("\\", "/").casefold()
    name = normalized.rsplit("/", 1)[-1]
    for pattern in patterns:
        folded_pattern = pattern.casefold()
        if fnmatch.fnmatch(name, folded_pattern) or fnmatch.fnmatch(normalized, folded_pattern):
            return pattern
    return None


def check_no_blocked_paths(paths: list[str], *, label: str) -> None:
    offenders = [(path, pattern) for path in paths if (pattern := matches_any(path))]
    if offenders:
        details = "\n".join(f"- {path} matches {pattern}" for path, pattern in offenders[:50])
        raise SystemExit(f"{label} contains private or location artifact paths:\n{details}")


def archive_names(path: Path) -> list[str]:
    if path.suffix == ".whl" or path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            names = []
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                parts = Path(member.name).parts
                names.append(Path(*parts[1:]).as_posix() if len(parts) > 1 else member.name)
            return names
    raise ValueError(f"Unsupported artifact type: {path}")


def check_package_artifacts() -> None:
    with tempfile.TemporaryDirectory(prefix="hermes-release-dist-") as raw_tmp:
        tmp = Path(raw_tmp)
        dist_dir = tmp / "dist"
        dist_dir.mkdir()
        run([sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", str(dist_dir)])

        artifacts = sorted(dist_dir.iterdir())
        if not artifacts:
            raise SystemExit("No package artifacts were built")

        for artifact in artifacts:
            names = archive_names(artifact)
            check_no_blocked_paths(names, label=artifact.name)
            if artifact.suffix == ".whl":
                missing = [asset for asset in REQUIRED_PACKAGE_ASSETS if asset not in names]
                if missing:
                    details = "\n".join(f"- {asset}" for asset in missing)
                    raise SystemExit(f"{artifact.name} is missing required package assets:\n{details}")


def check_snapshot_export() -> None:
    with tempfile.TemporaryDirectory(prefix="hermes-release-snapshot-") as raw_tmp:
        snapshot = Path(raw_tmp) / "snapshot"
        run(["sh", "scripts/create_public_snapshot.sh", str(snapshot)])
        run([sys.executable, "scripts/secret_scan.py", "--all-files"], cwd=snapshot)
        check_no_blocked_paths(git_files(snapshot), label="snapshot git index")


def check_tracked_paths() -> None:
    from secret_scan import REVIEWED_SOURCE_DOCS
    check_no_blocked_paths([p for p in git_files() if p not in REVIEWED_SOURCE_DOCS], label="tracked files")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run release safety checks for publishable artifacts.")
    parser.add_argument(
        "--skip-snapshot",
        action="store_true",
        help="Skip create_public_snapshot.sh validation. Useful before local changes are committed.",
    )
    args = parser.parse_args(argv)

    check_tracked_paths()
    check_package_artifacts()
    if not args.skip_snapshot:
        check_snapshot_export()
    print("Release safety checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
