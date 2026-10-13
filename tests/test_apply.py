"""Applying a whole solver plan and correcting results."""
# pylint: disable=redefined-outer-name, missing-function-docstring

import json

import pytest

from conftest import KEYPOINT_1, KEYPOINT_2, set_stock


# Barred to keep the two-key-point search small (9 cards left) so the tests stay fast.
BARRED = ["Avenger", "Clip", "Ency", "Hellcat", "Jhonny", "KGV", "Kiddo", "Narly", "NM",
          "Sandy", "Trump"]


@pytest.fixture
def solved(map1):
    """Map 1, stage 1 complete with key points 1 and 2 and solved."""
    map1.post("/maps/1/stages/new")
    map1.post("/maps/stage/1/env",
              data={"wins_needed": "4", "all_keypoints": "on", "barred": BARRED})
    map1.post("/maps/stage/1/keypoints/new", data=KEYPOINT_1)
    map1.post("/maps/stage/1/keypoints/new", data=KEYPOINT_2)
    map1.post("/maps/stage/1/solve")
    return map1


def placed(solution, plan=0):
    return [s for pk in solution(1)["plans"][plan]["keypoints"] for s in pk["slots"] if s["card"]]


def test_plans_offer_apply_buttons(solved):
    html = solved.get("/maps/1").text
    assert "Apply plan 1" in html and "Apply plan 2" in html


def test_apply_records_every_keypoint(solved, query, solution):
    cards = placed(solution)
    r = solved.post("/maps/stage/1/apply/0", follow_redirects=True)
    assert "Applied plan 1 to key points 1, 2" in r.text
    state = {c["id"]: c for c in query("SELECT id, hp, used FROM map_card WHERE map_id = 1")}
    for s in cards:
        assert state[s["map_card_id"]] == {"id": s["map_card_id"], "hp": 2, "used": 1}
    assert sum(c["used"] for c in state.values()) == len(cards)
    wins = query("SELECT SUM(won) AS w FROM battle")[0]["w"]
    assert wins == sum(1 for s in cards if s["p"] >= 0.5)
    assert "Apply plan" not in r.text


def test_apply_twice_is_refused(solved, query, solution):
    solved.post("/maps/stage/1/apply/0")
    r = solved.post("/maps/stage/1/apply/1", follow_redirects=True)
    assert "already recorded" in r.text
    assert query("SELECT COUNT(*) AS n FROM battle")[0]["n"] == len(placed(solution))


def test_unknown_plan_is_refused(solved):
    r = solved.post("/maps/stage/1/apply/5", follow_redirects=True)
    assert "no longer available" in r.text


def test_toggle_won(solved, query):
    solved.post("/maps/stage/1/apply/0")
    b = query("SELECT keypoint_id, slot, won FROM battle ORDER BY keypoint_id, slot")[0]
    solved.post(f"/maps/keypoint/{b['keypoint_id']}/slot/{b['slot']}/toggle")
    after = query("SELECT won FROM battle WHERE keypoint_id = ? AND slot = ?",
                  b["keypoint_id"], b["slot"])
    assert after[0]["won"] == 1 - b["won"]
    r = solved.post("/maps/keypoint/2/slot/3/toggle", follow_redirects=True)  # empty slot
    assert "No card fought in that slot" in r.text


def test_next_stage_resets_used_and_keeps_applied_hp(solved, query, solution):
    cards = placed(solution)
    solved.post("/maps/stage/1/apply/0")
    solved.post("/maps/stage/1/finish")
    solved.post("/maps/1/stages/new")
    state = {c["id"]: c for c in query("SELECT id, hp, used FROM map_card WHERE map_id = 1")}
    assert not any(c["used"] for c in state.values())
    assert all(state[s["map_card_id"]]["hp"] == 2 for s in cards)
    r = solved.post("/maps/keypoint/1/slot/1/toggle", follow_redirects=True)
    assert "is finished" in r.text


def test_apply_refuses_a_plan_beyond_the_modifier_stock(solved, query):
    # Make plan 1 use a Gunnery, then run the stock out: nothing may be recorded.
    sol = json.loads(query("SELECT solution FROM stage WHERE id = 1")[0]["solution"])
    first = next(s for s in sol["plans"][0]["keypoints"][0]["slots"] if s["card"])
    first["mods"] = ["Gunnery"]
    with solved.application.app_context():
        from webapp.db import get_db  # pylint: disable=import-outside-toplevel
        get_db().execute("UPDATE stage SET solution = ? WHERE id = 1", (json.dumps(sol),))
        get_db().commit()
    set_stock(solved, {"Gunnery": 0})
    r = solved.post("/maps/stage/1/apply/0", follow_redirects=True)
    assert "Only 0 Gunnery left" in r.text
    assert query("SELECT COUNT(*) AS n FROM battle")[0]["n"] == 0
