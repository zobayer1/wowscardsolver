"""Shared fixtures: a Flask app on a temporary database, with a fixed test roster."""
# pylint: disable=redefined-outer-name  # pytest fixtures are injected by parameter name

import io
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from webapp import create_app, sandbox
from webapp.db import get_db

DATA = Path(__file__).parent / "data"
# Number of cards in the test roster.
ROSTER_SIZE = (DATA / "roster.csv").read_text(encoding="utf-8").strip().count("\n")

# Modifier stock every test map starts with.
STOCK = {"Gunnery": 2, "Damage Control": 3, "Dodge": 3, "Teamwork": 0, "Effectiveness": 3,
         "Morale": 0, "Focused": 3, "Specialization": 3, "Rabid": 7, "Fast Repair": 8,
         "Unstoppable": 4}

# A full key point: four enemies with vulnerabilities.
KEYPOINT_1 = {
    "power1": "115", "vuln1_1": "AA Fire", "vuln2_1": "Smoke Screen",
    "power2": "107", "vuln1_2": "Smoke Screen", "vuln2_2": "Quick Fire",
    "power3": "110", "vuln1_3": "Detection", "vuln2_3": "Air Strike",
    "power4": "105", "vuln1_4": "Quick Fire", "vuln2_4": "Air Strike",
}
# Three enemies (slot 3 empty), one of them a must-win.
KEYPOINT_2 = {
    "power1": "94", "vuln1_1": "Smoke Screen", "vuln2_1": "Quick Fire",
    "power2": "184", "vuln1_2": "Smoke Screen", "vuln2_2": "Large Caliber",
    "must_win2": "on", "power4": "170", "vuln1_4": "AA Fire", "vuln2_4": "Smoke Screen",
}


@pytest.fixture(autouse=True)
def fixed_search(monkeypatch):
    """Solve with a fixed option trim in tests (no time budget), so results and run time do
    not depend on the machine."""
    monkeypatch.setattr(sandbox, "SOLVE_LIMITS",
                        replace(sandbox.SOLVE_LIMITS, time_budget=None))


@pytest.fixture
def app(tmp_path):
    """The app on an empty temporary database."""
    return create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.db")})


@pytest.fixture
def client(app):
    """A test client on the empty database."""
    return app.test_client()


@pytest.fixture
def query(app):
    """Run SQL against the test database; returns rows as dicts."""
    def run(sql, *args):
        with app.app_context():
            return [dict(r) for r in get_db().execute(sql, args)]
    return run


@pytest.fixture
def roster_csv():
    """Bytes of the test roster (ROSTER_SIZE cards)."""
    return (DATA / "roster.csv").read_bytes()


def upload(client, data, filename="roster.csv"):
    """POST a roster CSV to the import route."""
    return client.post("/roster/import", data={"file": (io.BytesIO(data), filename)},
                       content_type="multipart/form-data", follow_redirects=True)


@pytest.fixture
def loaded(client, roster_csv):
    """A client whose roster holds the test cards."""
    upload(client, roster_csv)
    return client


def set_stock(client, counts):
    """Save modifier counts through the map page form, with the current versions."""
    stock = client.get("/maps/modifiers.json").get_json()
    data = {f"ver_{n}": str(s["version"]) for n, s in stock.items()}
    data.update({f"mod_{n}": str(counts.get(n, s["count"])) for n, s in stock.items()})
    return client.post("/maps/modifiers", data=data, follow_redirects=True)


@pytest.fixture
def map1(loaded):
    """A client with map 1 started and the STOCK modifier counts."""
    loaded.post("/maps/new", data={"name": "Test map"})
    set_stock(loaded, STOCK)
    return loaded


@pytest.fixture
def solution(query):
    """The stored solver output of a stage."""
    def get(stage_id):
        return json.loads(query("SELECT solution FROM stage WHERE id = ?", stage_id)[0]["solution"])
    return get


def flashes(html):
    """Flash message texts on a page."""
    return re.findall(r'class="flash[^"]*">([^<]+)<', html)
