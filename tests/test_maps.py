"""Map sandbox: maps, stages, key points, solving, recording and undoing battles."""
# pylint: disable=redefined-outer-name, missing-function-docstring

import re
import sqlite3

import pytest

from conftest import KEYPOINT_1, KEYPOINT_2, ROSTER_SIZE, flashes

from webapp import create_app


@pytest.fixture
def stage1(map1):
    """Map 1 with stage 1 open and key point 1 entered (Ency barred there)."""
    map1.post("/maps/1/stages/new")
    map1.post("/maps/stage/1/keypoints/new",
              data=dict(KEYPOINT_1, min_wins="3", enemy_power="5", no_green="on",
                        barred=["Ency"]))
    return map1


def record_plan(client, solution, keypoint_id=1, lose_slot=None, grey=None):
    """Record plan 1's placement at a key point; returns the form posted."""
    pk = next(k for k in solution(1)["plans"][0]["keypoints"] if k["keypoint_id"] == keypoint_id)
    form = {}
    for i, s in enumerate(pk["slots"], 1):
        if s["card"]:
            form[f"card{i}"] = str(s["map_card_id"])
            if i != lose_slot:
                form[f"won{i}"] = "on"
    if grey:
        form["grey1"] = grey
    client.post(f"/maps/keypoint/{keypoint_id}/record", data=form)
    return form


def test_new_map_is_a_snapshot_of_the_roster(loaded, query):
    r = loaded.post("/maps/new", data={"name": ""})
    assert r.headers["Location"].endswith("/maps/1")
    rows = query("SELECT * FROM map_card WHERE map_id = 1 AND hp = 3 AND used = 0")
    assert len(rows) == ROSTER_SIZE
    html = loaded.get("/maps/1").text
    assert "Map 1" in html and "Start a New Stage" in html and "Stage 1" not in html
    loaded.post("/roster/add", data={"name": "Late", "firepower": "1", "survivability": "1",
                                     "maneuverability": "1"})
    assert len(query("SELECT * FROM map_card WHERE map_id = 1")) == ROSTER_SIZE


def test_sync_roster_adds_new_cards_at_full_hp(map1, query):
    kiddo = query("SELECT id FROM map_card WHERE map_id = 1 AND name = 'Kiddo'")[0]["id"]
    map1.post("/maps/1/cards", data={f"hp_{kiddo}": "1", f"used_{kiddo}": "on"})
    map1.post("/roster/add", data={"name": "Late", "firepower": "50", "survivability": "50",
                                   "maneuverability": "50"})
    r = map1.post("/maps/1/sync", follow_redirects=True)
    assert "Added Late at 3 HP." in flashes(r.text)
    late = query("SELECT hp, used, firepower FROM map_card WHERE map_id = 1 AND name = 'Late'")
    assert late == [{"hp": 3, "used": 0, "firepower": 50}]
    assert query("SELECT hp, used FROM map_card WHERE id = ?", kiddo)[0] == {"hp": 1, "used": 1}
    r = map1.post("/maps/1/sync", follow_redirects=True)
    assert "The map is up to date with the roster." in flashes(r.text)
    assert len(query("SELECT * FROM map_card WHERE map_id = 1")) == ROSTER_SIZE + 1


def test_only_one_open_stage(map1):
    map1.post("/maps/1/stages/new")
    r = map1.post("/maps/1/stages/new", follow_redirects=True)
    assert "Finish stage 1 before" in r.text


def test_keypoint_ignores_unknown_barred_cards(map1, query):
    map1.post("/maps/1/stages/new")
    map1.post("/maps/stage/1/keypoints/new", data=dict(KEYPOINT_2, barred=["Ency", "Nope"]))
    assert query("SELECT barred FROM keypoint WHERE id = 1")[0]["barred"] == '["Ency"]'


def test_stage_has_no_environment_or_goal(stage1, query):
    html = stage1.get("/maps/1").text
    assert "Stage environment" not in html and "Wins needed (stage goal)" not in html
    assert "Wins 0" in html
    columns = {r["name"] for r in query("PRAGMA table_info(stage)")}
    assert not columns & {"wins_needed", "firepower", "barred", "all_keypoints"}


def test_keypoint_errors_keep_the_editor_open(map1):
    map1.post("/maps/1/stages/new")
    r = map1.post("/maps/stage/1/keypoints/new",
                  data={"power1": "abc", "vuln1_1": "AA Fire", "vuln2_1": "AA Fire"})
    assert "Slot 1 power must be a whole number." in flashes(r.text)
    assert "Slot 1: the two vulnerabilities are the same." in flashes(r.text)
    assert re.search(r'id="kp-editor-new"\s+data-view="add-kp"\s*>', r.text)
    assert 'id="add-kp" hidden' in r.text


def test_keypoint_is_shown_with_its_conditions(stage1):
    html = stage1.get("/maps/1").text
    assert "Key point 1" in html and "Enemy power +5%" in html and "no green modifiers" in html
    assert "needs 3 wins here" in html
    assert 'name="power4" type="number" min="1" value="105"' in html  # editor prefilled


def test_solving_maximizes_wins_without_a_stage_goal(stage1, solution):
    r = stage1.post("/maps/stage/1/solve", follow_redirects=True)
    assert "Plan 1" in r.text and "Plan 2" in r.text and "Plan 3" not in r.text
    sol = solution(1)
    assert "goal" not in sol and len(sol["plans"]) == 2
    assert sol["plans"][0]["exp_wins"] == 4.0          # every enemy beaten
    assert all(s["card"] != "Ency" for s in sol["plans"][0]["keypoints"][0]["slots"])


def test_recording_takes_hp_and_marks_used(stage1, query, solution):
    stage1.post("/maps/stage/1/solve")
    form = record_plan(stage1, solution, lose_slot=4, grey="Gunnery")
    html = stage1.get("/maps/1").text
    assert "played · 3 wins" in html
    for i in range(1, 5):
        row = query("SELECT hp, used FROM map_card WHERE id = ?", int(form[f"card{i}"]))[0]
        assert row == {"hp": 2, "used": 1}
    assert len(query("SELECT * FROM map_card WHERE map_id = 1 AND used = 1")) == 4
    assert query("SELECT grey_mod FROM battle WHERE slot = 1")[0]["grey_mod"] == "Gunnery"
    assert query("SELECT solution_stale FROM stage WHERE id = 1")[0]["solution_stale"] == 1


def test_record_rejects_used_and_unknown_cards(stage1, solution):
    stage1.post("/maps/stage/1/solve")
    form = record_plan(stage1, solution)
    stage1.post("/maps/stage/1/keypoints/new", data=KEYPOINT_2)
    r = stage1.post("/maps/keypoint/2/record", data={"card1": form["card1"]}, follow_redirects=True)
    assert "is already used in this stage" in r.text
    r = stage1.post("/maps/keypoint/2/record", data={"card1": "999"}, follow_redirects=True)
    assert "unknown card" in r.text


def test_fast_repair_and_undo(stage1, query):
    stage1.post("/maps/stage/1/keypoints/new", data=KEYPOINT_2)
    card = query("SELECT id FROM map_card WHERE map_id = 1 ORDER BY id LIMIT 1")[0]["id"]
    stage1.post("/maps/1/cards", data={f"hp_{c['id']}": "2" if c["id"] == card else "3"
                                       for c in query("SELECT id FROM map_card WHERE map_id = 1")})
    r = stage1.post("/maps/keypoint/2/record",
                    data={"card1": str(card), "green1": "Fast Repair", "won1": "on"},
                    follow_redirects=True)
    assert "Recorded key point 2" in r.text
    assert query("SELECT hp, used FROM map_card WHERE id = ?", card)[0] == {"hp": 2, "used": 1}
    r = stage1.post("/maps/keypoint/2/undo", follow_redirects=True)
    assert "Battle undone" in r.text
    assert query("SELECT hp, used FROM map_card WHERE id = ?", card)[0] == {"hp": 2, "used": 0}


def test_played_keypoint_cannot_be_edited(stage1, solution):
    stage1.post("/maps/stage/1/solve")
    record_plan(stage1, solution)
    r = stage1.post("/maps/keypoint/1/edit", data=KEYPOINT_1, follow_redirects=True)
    assert "Undo the recorded battle before editing" in r.text


def test_solving_the_rest_skips_used_cards(stage1, solution):
    stage1.post("/maps/stage/1/solve")
    form = record_plan(stage1, solution, lose_slot=4)
    stage1.post("/maps/stage/1/keypoints/new", data=dict(KEYPOINT_2, barred=["Ency"]))
    r = stage1.post("/maps/stage/1/solve", follow_redirects=True)
    sol = solution(1)
    assert len(sol["plans"][0]["keypoints"]) == 1
    used = {int(form[f"card{i}"]) for i in range(1, 5)}
    assert not used & {s.get("map_card_id") for s in sol["plans"][0]["keypoints"][0]["slots"]}
    assert "Use for key point 2" in r.text and "Use for key point 1" not in r.text


def test_finish_clears_used_and_keeps_hp(stage1, query, solution):
    stage1.post("/maps/stage/1/solve")
    form = record_plan(stage1, solution)
    stage1.post("/maps/stage/1/finish")
    html = stage1.get("/maps/1").text
    assert "Start a New Stage" in html and "Reopen" in html
    assert not query("SELECT * FROM map_card WHERE map_id = 1 AND used = 1")
    assert query("SELECT hp FROM map_card WHERE id = ?", int(form["card1"]))[0]["hp"] == 2
    # reopening marks the stage's cards used again; finishing clears them again
    stage1.post("/maps/stage/1/reopen")
    used = {r["id"] for r in query("SELECT id FROM map_card WHERE map_id = 1 AND used = 1")}
    assert used == {int(form[f"card{i}"]) for i in range(1, 5)}
    stage1.post("/maps/stage/1/finish")
    stage1.post("/maps/1/stages/new")
    assert "Stage 2" in stage1.get("/maps/1").text
    assert not query("SELECT * FROM map_card WHERE map_id = 1 AND used = 1")
    assert query("SELECT hp FROM map_card WHERE id = ?", int(form["card1"]))[0]["hp"] == 2


def test_maps_are_independent(stage1, query, solution):
    stage1.post("/maps/new", data={"name": "Second"})
    stage1.post("/maps/2/stages/new")
    stage1.post("/maps/stage/2/keypoints/new", data=KEYPOINT_2)
    stage1.post("/maps/stage/2/solve")
    stage1.post("/maps/stage/1/solve")
    record_plan(stage1, solution, grey="Gunnery")
    rows = query("SELECT * FROM map_card WHERE map_id = 2 AND hp = 3 AND used = 0")
    assert len(rows) == ROSTER_SIZE
    assert query("SELECT solution_stale FROM stage WHERE id = 2")[0]["solution_stale"] == 0


def test_manual_card_edits(map1, query):
    cid = query("SELECT id FROM map_card WHERE map_id = 1")[0]["id"]
    map1.post("/maps/1/cards", data={f"hp_{cid}": "1", f"used_{cid}": "on"})
    assert query("SELECT hp, used FROM map_card WHERE id = ?", cid)[0] == {"hp": 1, "used": 1}
    rows = query("SELECT * FROM map_card WHERE map_id = 1 AND hp = 3 AND used = 0")
    assert len(rows) == ROSTER_SIZE - 1


def test_maps_list_and_delete(map1, query):
    assert "Test map" in map1.get("/maps/").text
    map1.post("/maps/1/delete")
    assert not query("SELECT * FROM map_card WHERE map_id = 1")
    assert "No maps yet" in map1.get("/maps/").text
    assert map1.get("/maps/99").status_code == 404


def test_debuff_power_condition_reaches_the_solver(map1):
    map1.post("/maps/1/stages/new")
    map1.post("/maps/stage/1/keypoints/new",
              data=dict(KEYPOINT_2, debuff_power="-50", buff_power="-20"))
    html = map1.get("/maps/1").text
    assert "Buff power -20% · Debuff power -50%" in html  # key point summary
    assert 'name="buff_power" type="number" value="-20"' in html  # key point editor
    assert 'name="debuff_power" type="number" value="-50"' in html
    with map1.application.app_context():
        from webapp import sandbox  # pylint: disable=import-outside-toplevel
        from webapp.db import get_db  # pylint: disable=import-outside-toplevel
        stage = sandbox.load_stage(get_db(), 1)
        kp = sandbox.solver_keypoint(stage["keypoints"][0], sandbox.map_cards(get_db(), 1))
    assert kp.cond == {"B": -20, "D": -50}


def test_keypoint_editor_tabs_across_the_slots(map1):
    map1.post("/maps/1/stages/new")
    html = map1.get("/maps/1").text
    editor = html[html.index('id="kp-editor-new"'):]
    editor = editor[:editor.index("kp-conditions")]
    names = re.findall(r'name="(\w+)"', editor)
    rows = ["power{}", "boost{}", "vuln1_{}", "vuln2_{}", "must_win{}"]
    assert names == [row.format(slot) for row in rows for slot in range(1, 5)]


def test_old_modifier_stock_tables_are_dropped(tmp_path):
    path = str(tmp_path / "old.db")
    create_app({"TESTING": True, "DATABASE": path})
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE modifier (name TEXT PRIMARY KEY, count INTEGER, version INTEGER)")
    db.execute("CREATE TABLE map_modifier (map_id INTEGER, name TEXT, count INTEGER)")
    db.commit()
    db.close()
    create_app({"TESTING": True, "DATABASE": path})  # runs the migration
    db = sqlite3.connect(path)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    db.close()
    assert not tables & {"modifier", "map_modifier"}


def test_barred_types_reach_the_solver_and_block_recording(map1, query):
    types = {r["name"]: r["type"] for r in query("SELECT name, type FROM map_card")}
    flan = sorted(n for n, t in types.items() if t == "flan")
    map1.post("/maps/1/stages/new")
    map1.post("/maps/stage/1/keypoints/new",
              data=dict(KEYPOINT_2, barred_types=["babby", "flan", "nope"]))
    html = map1.get("/maps/1").text
    assert "barred types: babby, flan" in html
    assert 'name="barred_types" value="babby"\n         checked' in html
    with map1.application.app_context():
        from webapp import sandbox  # pylint: disable=import-outside-toplevel
        from webapp.db import get_db  # pylint: disable=import-outside-toplevel
        stage = sandbox.load_stage(get_db(), 1)
        assert stage["keypoints"][0]["barred_types"] == ["babby", "flan"]
        kp = sandbox.solver_keypoint(stage["keypoints"][0], sandbox.map_cards(get_db(), 1))
    assert kp.barred == {n for n, t in types.items() if t in ("babby", "flan")}
    card = query("SELECT id FROM map_card WHERE name = ?", flan[0])[0]["id"]
    r = map1.post("/maps/keypoint/1/record", data={"card1": str(card)}, follow_redirects=True)
    assert f"{flan[0]} is barred here." in r.text


def test_sync_roster_updates_types(map1, query):
    map1.post(f"/roster/{query('SELECT id FROM card WHERE name = ?', 'Daunty')[0]['id']}/edit",
              data={"name": "Daunty", "firepower": "35", "survivability": "20",
                    "maneuverability": "55", "type": "cuzr"})
    r = map1.post("/maps/1/sync", follow_redirects=True)
    assert "Updated the type of Daunty." in flashes(r.text)
    assert query("SELECT type FROM map_card WHERE name = 'Daunty'")[0]["type"] == "cuzr"


def test_barred_type_locks_its_cards_in_the_list(map1, query):
    flan = sorted(r["name"] for r in query("SELECT name FROM map_card WHERE type = 'flan'"))
    map1.post("/maps/1/stages/new")
    map1.post("/maps/stage/1/keypoints/new",
              data=dict(KEYPOINT_2, barred_types="flan", barred=["KGV"]))
    assert query("SELECT barred FROM keypoint WHERE id = 1")[0]["barred"] == '["KGV"]'
    html = map1.get("/maps/1").text
    stage_form = html[html.index('id="kp-editor-1"'):]
    stage_form = stage_form[:stage_form.index("Save key point")]
    for name in flan:
        box = re.search(rf'<input type="checkbox" name="barred" value="{name}"[^>]*>', stage_form)
        tag = box.group(0)
        assert "checked" in tag and "disabled" in tag and 'data-own="0"' in tag
    kgv = re.search(r'<input type="checkbox" name="barred" value="KGV"[^>]*>', stage_form).group(0)
    assert "checked" in kgv and "disabled" not in kgv
    assert f"({len(flan) + 1})" in stage_form


def test_solution_records_how_far_the_search_got(map1, solution):
    map1.post("/maps/1/stages/new")
    map1.post("/maps/stage/1/keypoints/new", data=KEYPOINT_2)
    map1.post("/maps/stage/1/solve")
    sol = solution(1)
    assert sol["exact"] is True and sol["options"] > 0
    assert "searched every option" in map1.get("/maps/1").text


def test_enemy_boost_is_saved_and_shown(map1, query):
    map1.post("/maps/1/stages/new")
    map1.post("/maps/stage/1/keypoints/new", data=dict(KEYPOINT_2, boost2="16"))
    assert query("SELECT slot, boost FROM enemy ORDER BY slot") == [
        {"slot": 1, "boost": 0}, {"slot": 2, "boost": 16}, {"slot": 4, "boost": 0}]
    html = map1.get("/maps/1").text
    assert '184 <span class="boost"' in html and "+16%" in html
    assert 'name="boost2" type="number" value="16"' in html
    with map1.application.app_context():
        from webapp import sandbox  # pylint: disable=import-outside-toplevel
        from webapp.db import get_db  # pylint: disable=import-outside-toplevel
        stage = sandbox.load_stage(get_db(), 1)
        kp = sandbox.solver_keypoint(stage["keypoints"][0], sandbox.map_cards(get_db(), 1))
    assert kp.enemies[1].boost == 16


def test_new_stage_opens_the_form_for_key_point_1(map1):
    map1.post("/maps/1/stages/new")
    html = map1.get("/maps/1").text
    assert '<div class="add-kp" id="add-kp" hidden>' in html
    assert '<h3 class="editor-title" >Key point 1</h3>' in html
    assert re.search(r'id="kp-editor-new"\s+data-view="add-kp"\s+>', html)
    map1.post("/maps/stage/1/keypoints/new", data=KEYPOINT_1)
    html = map1.get("/maps/1").text           # later key points wait for the button
    assert '<div class="add-kp" id="add-kp" >' in html
    assert '<h3 class="editor-title" hidden>Key point 2</h3>' in html
    assert re.search(r'id="kp-editor-new"\s+data-view="add-kp"\s+hidden>', html)
