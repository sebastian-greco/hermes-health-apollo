"""Narrative and quantified meal coverage in Apollo daily query aggregates."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_module(name):
    package_name = "apollo_care_food_query_tests"
    sys.modules.setdefault("apollo_care_food_query_parent", types.ModuleType("apollo_care_food_query_parent"))
    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        package.__path__ = [str(ROOT)]
        sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(f"{package_name}.{name}", ROOT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def modules(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return load_module("store"), load_module("query")


def _seed(store):
    store.initialize()
    with sqlite3.connect(store.database_path()) as conn:
        conn.executemany(
            "INSERT INTO oura_daily(day, readiness_score, sleep_score, stress_high_seconds) VALUES (?, 70, 70, ?)",
            [("2026-10-11", 100), ("2026-10-12", 200), ("2026-10-13", 300), ("2026-10-14", 400)],
        )
        rows = [
            # Narrative-only meal: no item estimate was recorded.
            ("narrative", "2026-10-11", "Pasta with vegetables", None),
            # Mixed day: partial calories must not be presented as a daily total.
            ("mixed-known", "2026-10-12", "Soup", json.dumps({"items": [], "total_estimated_calories": 320})),
            ("mixed-unknown", "2026-10-12", "Fruit", None),
            # Legacy quantified rows retain their previous summed value.
            ("known-a", "2026-10-13", "Lunch", json.dumps({"items": [], "total_estimated_calories": 450})),
            ("known-b", "2026-10-13", "Dinner", json.dumps({"items": [], "total_estimated_calories": 600})),
        ]
        conn.executemany(
            "INSERT INTO food_logs(id, day, logged_at, description, items_json) VALUES (?, ?, ?, ?, ?)",
            [(identity, day, day + "T12:00:00Z", description, payload)
             for identity, day, description, payload in rows],
        )


def test_date_range_distinguishes_missing_partial_complete_and_absent_food(modules):
    store, query = modules
    _seed(store)

    result = query.health_query({"query_type": "date_range", "start": "2026-10-11", "end": "2026-10-14"})
    rows = {row["day"]: row for row in result["days"]}

    assert rows["2026-10-11"]["food_total_estimated_calories"] is None
    assert rows["2026-10-12"]["food_total_estimated_calories"] is None
    assert rows["2026-10-13"]["food_total_estimated_calories"] == 1050
    # Preserve the legacy no-meals contract.
    assert rows["2026-10-14"]["food_total_estimated_calories"] == 0


def test_stress_days_uses_the_same_unknown_coverage_rules(modules):
    store, query = modules
    _seed(store)

    result = query.health_query({"query_type": "stress_days", "limit": 4})
    rows = {row["day"]: row["food_total_estimated_calories"] for row in result["days"]}

    assert rows == {
        "2026-10-14": 0,
        "2026-10-13": 1050,
        "2026-10-12": None,
        "2026-10-11": None,
    }
