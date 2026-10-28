"""Roster pages: add, edit, remove, import and export."""
# pylint: disable=missing-function-docstring

import csv
import io
import re

from conftest import DATA, ROSTER_SIZE, upload

from webapp.engine import solver

CARD = {"name": "Test O'Ship", "firepower": "40", "survivability": "30", "maneuverability": "50",
        "ability1_type": "buff", "ability1_target": "M", "ability1_direction": "right",
        "ability1_pct": "", "ability2_type": "debuff", "ability2_target": "Smoke Screen",
        "ability2_direction": "up-left", "ability2_pct": "25"}


def card_id(client):
    return re.search(r"/roster/(\d+)/edit", client.get("/roster/").text).group(1)


def test_empty_roster(client):
    r = client.get("/", follow_redirects=True)
    assert r.status_code == 200 and "The roster is empty" in r.text


def test_add_card(client):
    r = client.post("/roster/add", data=CARD, follow_redirects=True)
    assert "Added Test O&#39;Ship" in r.text
    assert "Maneuverability +30% → right" in r.text
    assert "Smoke Screen −25% ↖ up-left" in r.text
    assert '<td class="num total">120</td>' in r.text


def test_add_rejects_duplicates_and_bad_input(client):
    client.post("/roster/add", data=CARD)
    assert "already exists" in client.post("/roster/add", data=CARD).text
    bad = dict(CARD, name="X", firepower="-3", ability1_target="Detection", ability2_direction="")
    r = client.post("/roster/add", data=bad)
    assert "Firepower must be" in r.text
    assert "Ability 1: choose a target" in r.text
    assert "Ability 2: choose a direction" in r.text
    assert 'value="X"' in r.text  # the form keeps what was typed


def test_edit_card(client):
    client.post("/roster/add", data=CARD)
    cid = card_id(client)
    r = client.get(f"/roster/{cid}/edit")
    assert 'data-selected="Smoke Screen"' in r.text and 'value="25"' in r.text
    r = client.post(f"/roster/{cid}/edit", data=dict(CARD, firepower="45", ability2_type=""),
                    follow_redirects=True)
    assert "Saved" in r.text and '<td class="num total">125</td>' in r.text
    assert "Smoke Screen" not in r.text


def test_remove_card(client):
    client.post("/roster/add", data=CARD)
    r = client.post(f"/roster/{card_id(client)}/delete", follow_redirects=True)
    assert "Removed" in r.text and "The roster is empty" in r.text
    assert client.get("/roster/999/edit").status_code == 404


def test_import_adds_then_updates(client, roster_csv):
    assert f"Imported roster.csv: {ROSTER_SIZE} added, 0 updated" in upload(client, roster_csv).text
    assert f"0 added, {ROSTER_SIZE} updated" in upload(client, roster_csv).text


def test_export_round_trips_without_hp_and_used(loaded, roster_csv):
    r = loaded.get("/roster/export")
    assert r.headers["Content-Disposition"] == "attachment; filename=roster.csv"
    exported = r.data.decode()
    src = [{k: v for k, v in row.items() if k not in ("hp", "used")}
           for row in csv.DictReader(io.StringIO(roster_csv.decode()))]
    out = list(csv.DictReader(io.StringIO(exported)))
    assert "hp" not in out[0] and "used" not in out[0]
    assert src == out
    # the solver reads the export back as the same cards
    again = solver.parse_roster(solver.clean_rows(csv.DictReader(io.StringIO(exported))))
    base = solver.load_roster(DATA / "roster.csv")
    assert [(c.name, c.F, c.S, c.M, c.abilities) for c in again] == \
           [(c.name, c.F, c.S, c.M, c.abilities) for c in base]


def test_bad_import_changes_nothing(loaded):
    bad = (b"name,firepower,survivability,maneuverability,ability1_type,ability1_target,"
           b"ability1_direction\n"
           b"New,1,2,3,buff,Firepower,right\n"
           b"Broken,1,2,3,buf,Firepower,right\n")
    r = upload(loaded, bad, "bad.csv")
    assert ("Could not import bad.csv: line 3: Broken ability 1: "
            "type must be buff or debuff") in r.text
    assert '<td class="name">New</td>' not in r.text and f"{ROSTER_SIZE} cards" in r.text


def test_import_without_file(client):
    assert "Choose a CSV file" in client.post("/roster/import", follow_redirects=True).text


def test_types_import_export_and_form(loaded, query):
    types = dict((r["name"], r["type"]) for r in query("SELECT name, type FROM card"))
    assert types["Daunty"] == "flan" and types["KGV"] == "babby"
    exported = list(csv.DictReader(io.StringIO(loaded.get("/roster/export").data.decode())))
    assert list(exported[0])[-1] == "type" and exported[0]["type"] == types[exported[0]["name"]]
    html = loaded.get("/roster/").text
    assert '<span class="tag kind">doddy</span>' in html
    form = loaded.get(f"/roster/{card_id(loaded)}/edit").text
    assert '<datalist id="card-types">' in form and '<option value="cuzr">' in form


def test_import_without_type_column_keeps_types(loaded, query):
    no_type = b"name,firepower,survivability,maneuverability\nDaunty,36,20,55\n"
    assert "0 added, 1 updated" in upload(loaded, no_type).text
    row = query("SELECT firepower, type FROM card WHERE name = 'Daunty'")[0]
    assert row == {"firepower": 36, "type": "flan"}


def test_form_sets_and_clears_type(client, query):
    client.post("/roster/add", data=dict(CARD, type="  doddy "))
    assert query("SELECT type FROM card")[0]["type"] == "doddy"
    client.post(f"/roster/{card_id(client)}/edit", data=dict(CARD, type=""))
    assert query("SELECT type FROM card")[0]["type"] == ""


def test_modifiers_are_not_counted(loaded):
    assert "Modifiers" not in loaded.get("/roster/").text
    assert loaded.get("/maps/modifiers.json").status_code == 404
    assert loaded.post("/maps/modifiers", data={"mod_Rabid": "5"}).status_code == 404
