"""Compatibility: the stage described by the CSV files in tests/data, entered through the web
UI, must give the solver exactly the same inputs and the same best plans as the command-line
CSV workflow."""
# pylint: disable=redefined-outer-name, missing-function-docstring, protected-access, too-many-locals

import pytest

from conftest import DATA, upload

from webapp import sandbox
from webapp.db import get_db
from webapp.engine import solver

COND_FIELDS = {"F": "firepower", "S": "survivability", "M": "maneuverability",
               "E": "enemy_power", "B": "buff_power", "D": "debuff_power"}
TOP = 2


@pytest.fixture(scope="module")
def csv_stage():
    """Roster and key points as the CSV loader reads them."""
    roster = solver.load_roster(DATA / "roster.csv")
    keypoints, _ = solver.load_stage(DATA / "board.csv", DATA / "environment.csv", roster)
    return roster, keypoints


def env_form(cond, barred, no_mods):
    """Environment form fields for a condition dict, barred names and blocked slot types."""
    form = {COND_FIELDS[k]: str(v) for k, v in cond.items()}
    form["barred"] = sorted(barred)
    form.update({f"no_{kind}": "on" for kind in no_mods})
    return form


def enemy_form(enemies):
    form = {}
    for slot, e in enumerate(enemies, 1):
        if e is None:
            continue
        form[f"power{slot}"] = str(e.power)
        for n, v in enumerate(e.vulns, 1):
            form[f"vuln{n}_{slot}"] = v
        if e.must_win:
            form[f"must_win{slot}"] = "on"
    return form


@pytest.fixture
def ui_stage(client, csv_stage, query):
    """The same game entered through the web routes, as a user would."""
    roster, keypoints = csv_stage
    upload(client, (DATA / "roster.csv").read_bytes())
    client.post("/maps/new", data={"name": "From CSV"})
    ids = {r["name"]: r["id"] for r in query("SELECT id, name FROM map_card WHERE map_id = 1")}
    client.post("/maps/1/stages/new")
    cards = {f"hp_{ids[c.name]}": str(c.hp) for c in roster}
    cards.update({f"used_{ids[c.name]}": "on" for c in roster if c.used})
    client.post("/maps/1/cards", data=cards)  # after starting the stage, which clears used

    # Stages have no settings of their own: everything (including what environment.csv sets
    # for the whole stage) goes on each key point.
    for kp in keypoints:
        form = {**enemy_form(kp.enemies), **env_form(kp.cond, kp.barred, kp.no_mods)}
        if kp.min_wins:
            form["min_wins"] = str(kp.min_wins)
        r = client.post("/maps/stage/1/keypoints/new", data=form)
        assert r.status_code == 302, r.text
    return client


def test_ui_gives_the_solver_the_same_inputs(ui_stage, csv_stage):
    roster, keypoints = csv_stage
    with ui_stage.application.app_context():
        db = get_db()
        stage = sandbox.load_stage(db, 1)
        ui_roster = [sandbox.to_solver_card(c, used=bool(c["used"]))
                     for c in sandbox.map_cards(db, 1)]
        cards = sandbox.map_cards(db, 1)
        ui_keypoints = [sandbox.solver_keypoint(kp, cards) for kp in stage["keypoints"]]
    assert sorted(ui_roster, key=lambda c: c.name) == sorted(roster, key=lambda c: c.name)
    assert len(ui_keypoints) == len(keypoints)
    for ui, kp in zip(ui_keypoints, keypoints):
        assert ui.enemies == kp.enemies
        assert ui.cond == kp.cond
        assert ui.min_wins == kp.min_wins
        assert ui.barred == kp.barred
        assert ui.no_mods == kp.no_mods


def test_ui_and_csv_give_the_same_best_plans(ui_stage, csv_stage, solution):
    roster, keypoints = csv_stage
    # The web app has no stage goal (it maximizes wins) and does not count modifiers, so the
    # CSV's stage goal and modifier counts are not used.
    # The map lists cards by name, so solve the CSV roster in that order too: equally ranked
    # plans are then broken the same way and the placements match exactly.
    by_name = sorted(roster, key=lambda c: c.name.lower())
    best, _, _ = solver.solve_stage(by_name, keypoints, 0, sandbox.AVAILABLE_MODS,
                                    sandbox.SOLVE_LIMITS)   # the web app's limits (fixed trim)
    ui_stage.post("/maps/stage/1/solve")
    plans = solution(1)["plans"]
    assert len(plans) == len(best) == TOP
    for plan, (combo, st) in zip(plans, best):
        for key in ("p_goal", "spend", "exp_wins", "cost"):
            assert plan[key] == pytest.approx(st[key]), key
        for pk, (_, slots, mods, _, _) in zip(plan["keypoints"], combo):
            assert [s["card"] for s in pk["slots"]] == [c.name if c else None for c in slots]
            assert [s.get("mods", []) for s in pk["slots"] if s["card"]] == \
                   [list(m) for c, m in zip(slots, mods) if c]
