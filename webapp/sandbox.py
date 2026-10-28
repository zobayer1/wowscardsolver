"""Map sandbox: per-map card copies, stages, key points, battles.

Each map has its own copy of the roster. A new map starts every card at full HP and unused;
finishing a stage clears the used flags but keeps HP. Recording a battle marks its cards
used. Modifiers are not counted: the solver assumes any of them can be bought, unless a key
point blocks grey or green ones.
"""

import json
from datetime import datetime
from math import prod

from .engine import solver

CARD_COLS = ("name", "firepower", "survivability", "maneuverability",
             *(f"ability{n}_{f}" for n in (1, 2) for f in ("type", "target", "direction", "pct")),
             "type")
ENV_FIELDS = ("firepower", "survivability", "maneuverability", "enemy_power", "buff_power",
              "debuff_power")
COND_KEYS = dict(zip(ENV_FIELDS, ("F", "S", "M", "E", "B", "D")))
MAX_KEYPOINTS = 5
# Two plans; the option trim widens while a solve stays within about 20 seconds.
# Plan 2 must change at least 3 card assignments from plan 1, to be a real alternative.
# Up to 5,000 of the best card splits get the modifier search, as time allows.
# Every modifier counts as available, one grey and one green per card; a modifier is only
# used where it is needed to make a battle, or a key point's requirements, certain.
SOLVE_LIMITS = solver.SearchLimits(top=2, time_budget=20, distinct=3, keep=5000,
                                   mods_for_certainty=True)
AVAILABLE_MODS = dict.fromkeys(solver.MODIFIERS, 1)
GREY_MODS = [n for n, (kind, _) in solver.MODIFIERS.items() if kind == "grey"]
GREEN_MODS = [n for n, (kind, _) in solver.MODIFIERS.items() if kind == "green"]


class SandboxError(ValueError):
    """An action that the current map state does not allow."""


# ---------------------------------------------------------------- maps
def create_map(db, name):
    """Start a map: copy the roster at full HP. Returns its id."""
    cols = ", ".join(CARD_COLS)
    with db:
        map_id = db.execute("INSERT INTO map (name) VALUES (?)", (name,)).lastrowid
        db.execute(f"INSERT INTO map_card (map_id, {cols}) SELECT ?, {cols} FROM card",
                   (map_id,))
    return map_id


def sync_roster(db, map_id):
    """Copy roster cards the map does not have yet (matched by name) into it, at full HP and
    unused, and bring card types up to date. Cards already in the map keep their HP, used
    flag and stats. Returns (names added, names whose type changed)."""
    cols = ", ".join(CARD_COLS)
    added = [r["name"] for r in db.execute(
        "SELECT name FROM card WHERE name NOT IN (SELECT name FROM map_card WHERE map_id = ?) "
        "ORDER BY name COLLATE NOCASE", (map_id,))]
    retyped = [r["name"] for r in db.execute(
        "SELECT m.name FROM map_card m JOIN card c ON c.name = m.name "
        "WHERE m.map_id = ? AND m.type != c.type ORDER BY m.name COLLATE NOCASE", (map_id,))]
    if added or retyped:
        with db:
            db.execute(f"INSERT INTO map_card (map_id, {cols}) SELECT ?, {cols} FROM card "
                       f"WHERE name NOT IN (SELECT name FROM map_card WHERE map_id = ?)",
                       (map_id, map_id))
            db.execute("UPDATE map_card SET type = (SELECT c.type FROM card c "
                       "WHERE c.name = map_card.name) WHERE map_id = ? "
                       "AND name IN (SELECT name FROM card)", (map_id,))
            _mark_stale(db, map_id=map_id)
    return added, retyped


def map_cards(db, map_id):
    """The map's card rows, by name."""
    return db.execute("SELECT * FROM map_card WHERE map_id = ? ORDER BY name COLLATE NOCASE",
                      (map_id,)).fetchall()


def set_card_state(db, map_id, state):
    """Overwrite HP and used flag of map cards ({map_card id: (hp, used)})."""
    with db:
        db.executemany("UPDATE map_card SET hp = ?, used = ? WHERE id = ? AND map_id = ?",
                       [(hp, int(used), cid, map_id) for cid, (hp, used) in state.items()])
        _mark_stale(db, map_id=map_id)


def _mark_stale(db, map_id=None, stage_id=None):
    """Flag last solutions as out of date: one stage's, or one map's open stage."""
    if stage_id is not None:
        db.execute("UPDATE stage SET solution_stale = 1 WHERE id = ? AND solution IS NOT NULL",
                   (stage_id,))
    else:
        db.execute("UPDATE stage SET solution_stale = 1 "
                   "WHERE map_id = ? AND finished = 0 AND solution IS NOT NULL", (map_id,))


# ---------------------------------------------------------------- loading
def load_keypoints(db, stage_id):
    """Key points of a stage with enemies (list of 4, None = empty slot), battles and wins."""
    kps = [dict(r) for r in db.execute(
        "SELECT * FROM keypoint WHERE stage_id = ? ORDER BY number", (stage_id,))]
    for kp in kps:
        kp["barred"] = json.loads(kp["barred"])
        kp["barred_types"] = json.loads(kp["barred_types"])
        kp["enemies"] = [None] * 4
        for e in db.execute("SELECT * FROM enemy WHERE keypoint_id = ?", (kp["id"],)):
            kp["enemies"][e["slot"] - 1] = dict(e)
        kp["battles"] = [dict(b) for b in db.execute(
            "SELECT b.*, c.name AS card_name FROM battle b "
            "JOIN map_card c ON c.id = b.map_card_id WHERE b.keypoint_id = ? ORDER BY b.slot",
            (kp["id"],))]
        kp["wins"] = sum(2 if b["grey_mod"] == "Morale" else 1 for b in kp["battles"] if b["won"])
    return kps


def load_stage(db, stage_id):
    """One stage with its key points and derived state, or None."""
    row = db.execute("SELECT * FROM stage WHERE id = ?", (stage_id,)).fetchone()
    if row is None:
        return None
    st = dict(row)
    st["solution"] = json.loads(st["solution"]) if st["solution"] else None
    st["keypoints"] = load_keypoints(db, stage_id)
    st["wins"] = sum(kp["wins"] for kp in st["keypoints"])
    st["has_battles"] = any(kp["played"] for kp in st["keypoints"])
    return st


def load_stages(db, map_id):
    """All stages of a map, oldest first."""
    return [load_stage(db, r["id"]) for r in
            db.execute("SELECT id FROM stage WHERE map_id = ? ORDER BY number", (map_id,))]


# ---------------------------------------------------------------- stages and key points
def create_stage(db, map_id):
    """Open the next stage and clear every card's used flag (HP is kept). The previous
    stage must be finished."""
    last = db.execute("SELECT number, finished FROM stage WHERE map_id = ? "
                      "ORDER BY number DESC LIMIT 1", (map_id,)).fetchone()
    if last and not last["finished"]:
        raise SandboxError(f"Finish stage {last['number']} before starting the next one.")
    with db:
        db.execute("UPDATE map_card SET used = 0 WHERE map_id = ?", (map_id,))
        return db.execute("INSERT INTO stage (map_id, number) VALUES (?, ?)",
                          (map_id, last["number"] + 1 if last else 1)).lastrowid


def _check_open(stage):
    if stage["finished"]:
        raise SandboxError(f"Stage {stage['number']} is finished.")


def save_keypoint(db, stage, env, enemies, keypoint_id=None):
    """Add a key point (or replace an unplayed one's settings and enemies).
    enemies: list of 4 dicts or None. Returns the key point id."""
    _check_open(stage)
    env = dict(env, barred=json.dumps(env["barred"]),
               barred_types=json.dumps(env["barred_types"]))
    with db:
        if keypoint_id is None:
            if len(stage["keypoints"]) >= MAX_KEYPOINTS:
                raise SandboxError(f"A stage has at most {MAX_KEYPOINTS} key points.")
            number = max((kp["number"] for kp in stage["keypoints"]), default=0) + 1
            keypoint_id = db.execute(
                f"INSERT INTO keypoint (stage_id, number, {', '.join(env)}) "
                f"VALUES (?, ?, {', '.join('?' for _ in env)})",
                [stage["id"], number, *env.values()]).lastrowid
        else:
            kp = _keypoint(stage, keypoint_id)
            if kp["played"]:
                raise SandboxError("Undo the recorded battle before editing this key point.")
            db.execute(f"UPDATE keypoint SET {', '.join(f'{k} = ?' for k in env)} WHERE id = ?",
                       [*env.values(), keypoint_id])
            db.execute("DELETE FROM enemy WHERE keypoint_id = ?", (keypoint_id,))
        db.executemany(
            "INSERT INTO enemy (keypoint_id, slot, power, vuln1, vuln2, must_win, boost) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(keypoint_id, i + 1, e["power"], e["vuln1"], e["vuln2"], e["must_win"], e["boost"])
             for i, e in enumerate(enemies) if e is not None])
        _mark_stale(db, stage_id=stage["id"])
    return keypoint_id


def delete_keypoint(db, stage, keypoint_id):
    """Remove an unplayed key point."""
    _check_open(stage)
    if _keypoint(stage, keypoint_id)["played"]:
        raise SandboxError("Undo the recorded battle before deleting this key point.")
    with db:
        db.execute("DELETE FROM keypoint WHERE id = ?", (keypoint_id,))
        _mark_stale(db, stage_id=stage["id"])


def delete_stage(db, stage):
    """Remove an open stage that has no recorded battles."""
    _check_open(stage)
    if stage["has_battles"]:
        raise SandboxError("Undo the recorded battles before deleting this stage.")
    with db:
        db.execute("DELETE FROM stage WHERE id = ?", (stage["id"],))


def finish_stage(db, stage):
    """Close the stage and clear every card's used flag; HP carries over."""
    _check_open(stage)
    with db:
        db.execute("UPDATE stage SET finished = 1 WHERE id = ?", (stage["id"],))
        db.execute("UPDATE map_card SET used = 0 WHERE map_id = ?", (stage["map_id"],))


def reopen_stage(db, stage):
    """Reopen the latest stage, if no later stage exists; the cards that fought in it are
    marked used again."""
    later = db.execute("SELECT 1 FROM stage WHERE map_id = ? AND number > ?",
                       (stage["map_id"], stage["number"])).fetchone()
    if later:
        raise SandboxError("Only the latest stage can be reopened.")
    with db:
        db.execute("UPDATE stage SET finished = 0 WHERE id = ?", (stage["id"],))
        db.execute("UPDATE map_card SET used = 1 WHERE id IN (SELECT b.map_card_id "
                   "FROM battle b JOIN keypoint k ON k.id = b.keypoint_id WHERE k.stage_id = ?)",
                   (stage["id"],))


def _keypoint(stage, keypoint_id):
    for kp in stage["keypoints"]:
        if kp["id"] == keypoint_id:
            return kp
    raise SandboxError("That key point is not in this stage.")


# ---------------------------------------------------------------- battles
def record_battle(db, stage, keypoint_id, entries):
    """Record the battle at a key point. entries: list of dicts with slot, map_card_id,
    grey_mod, green_mod, won. Each card loses 1 HP (after Fast Repair)."""
    _check_open(stage)
    kp = _keypoint(stage, keypoint_id)
    _validate_battle(db, stage, kp, entries)
    with db:
        _write_battle(db, stage, keypoint_id, entries)


def apply_plan(db, stage, plan_index):
    """Record every key point of one of the stored solver plans at once. A battle counts as
    won when the plan gives it at least a 50% chance; flip it with toggle_won afterwards.
    Returns the key point numbers recorded."""
    _check_open(stage)
    plans = (stage["solution"] or {}).get("plans", [])
    if not 0 <= plan_index < len(plans):
        raise SandboxError("That plan is no longer available. Solve again.")
    todo = []
    for pk in plans[plan_index]["keypoints"]:
        kp = _keypoint(stage, pk["keypoint_id"])
        if kp["played"]:
            raise SandboxError(f"Key point {kp['number']} is already recorded. Solve again.")
        entries = [{"slot": i, "map_card_id": s["map_card_id"],
                    "grey_mod": next((m for m in s["mods"] if m in GREY_MODS), None),
                    "green_mod": next((m for m in s["mods"] if m in GREEN_MODS), None),
                    "won": s["p"] >= 0.5}
                   for i, s in enumerate(pk["slots"], 1) if s["card"]]
        _validate_battle(db, stage, kp, entries)
        todo.append((kp, entries))
    with db:
        for kp, entries in todo:
            _write_battle(db, stage, kp["id"], entries)
    return [kp["number"] for kp, _ in todo]


def _write_battle(db, stage, keypoint_id, entries):
    """Insert a validated battle and apply HP loss and used flags."""
    cards = {c["id"]: c for c in map_cards(db, stage["map_id"])}
    for e in entries:
        hp = cards[e["map_card_id"]]["hp"]
        hp_after = max(0, (min(3, hp + 2) if e["green_mod"] == "Fast Repair" else hp) - 1)
        db.execute("INSERT INTO battle (keypoint_id, slot, map_card_id, grey_mod, green_mod, "
                   "won, hp_before) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (keypoint_id, e["slot"], e["map_card_id"], e["grey_mod"], e["green_mod"],
                    int(e["won"]), hp))
        db.execute("UPDATE map_card SET hp = ?, used = 1 WHERE id = ?",
                   (hp_after, e["map_card_id"]))
    db.execute("UPDATE keypoint SET played = 1 WHERE id = ?", (keypoint_id,))
    _mark_stale(db, stage_id=stage["id"])


def toggle_won(db, stage, keypoint_id, slot):
    """Flip one recorded battle between won and lost."""
    _check_open(stage)
    kp = _keypoint(stage, keypoint_id)
    if not any(b["slot"] == slot for b in kp["battles"]):
        raise SandboxError("No card fought in that slot.")
    with db:
        db.execute("UPDATE battle SET won = 1 - won WHERE keypoint_id = ? AND slot = ?",
                   (keypoint_id, slot))
        _mark_stale(db, stage_id=stage["id"])


def undo_battle(db, stage, keypoint_id):
    """Reverse a recorded battle: restore HP and used flags; reopen the key point."""
    _check_open(stage)
    kp = _keypoint(stage, keypoint_id)
    with db:
        for b in kp["battles"]:
            db.execute("UPDATE map_card SET hp = ?, used = 0 WHERE id = ?",
                       (b["hp_before"], b["map_card_id"]))
        db.execute("DELETE FROM battle WHERE keypoint_id = ?", (keypoint_id,))
        db.execute("UPDATE keypoint SET played = 0 WHERE id = ?", (keypoint_id,))
        _mark_stale(db, stage_id=stage["id"])


def _validate_battle(db, stage, kp, entries):
    """Raise SandboxError listing everything wrong with a battle entry."""
    errors = []
    if kp["played"]:
        errors.append("This key point already has a recorded battle.")
    if not entries:
        errors.append("Place at least one card.")
    cards = {c["id"]: c for c in map_cards(db, stage["map_id"])}
    rules = {"enemies": kp["enemies"],
             "barred": barred_names(kp, cards.values()),
             "grey": kp["no_grey"], "green": kp["no_green"]}
    seen = set()
    for e in entries:
        errors += _entry_errors(e, cards.get(e["map_card_id"]), rules, seen)
    if errors:
        raise SandboxError(" ".join(errors))


def _entry_errors(e, card, rules, seen):
    """Problems with one slot of a battle entry; adds the card to `seen`."""
    where = f"Slot {e['slot']}"
    errors = []
    if rules["enemies"][e["slot"] - 1] is None:
        errors.append(f"{where} has no enemy.")
    if card is None:
        return errors + [f"{where}: unknown card."]
    if card["id"] in seen:
        errors.append(f"{card['name']} is placed twice.")
    seen.add(card["id"])
    if card["used"]:
        errors.append(f"{card['name']} is already used in this stage.")
    if card["hp"] <= 0:
        errors.append(f"{card['name']} has no HP left.")
    if card["name"] in rules["barred"]:
        errors.append(f"{card['name']} is barred here.")
    errors += [f"{where}: {kind} modifiers are blocked here."
               for kind in ("grey", "green") if e[f"{kind}_mod"] and rules[kind]]
    return errors


# ---------------------------------------------------------------- solving
def to_solver_card(row, used=False):
    """solver.Card for a map card row."""
    abilities = tuple(solver.Ability(row[f"ability{n}_type"], row[f"ability{n}_target"],
                                     row[f"ability{n}_direction"], row[f"ability{n}_pct"])
                      for n in (1, 2) if row[f"ability{n}_type"])
    return solver.Card(row["name"], row["firepower"], row["survivability"],
                       row["maneuverability"], abilities, hp=row["hp"], used=used,
                       type=row["type"])


def barred_names(kp, cards):
    """Names of the map cards barred at a key point: by name, or of a type barred there."""
    types = set(kp["barred_types"])
    return frozenset(kp["barred"]) | {c["name"] for c in cards if c["type"] in types}


def solver_keypoint(kp, cards):
    """solver.KeyPoint for a key point, given the map's card rows."""
    enemies = [None if e is None else
               solver.Enemy(e["power"], tuple(v for v in (e["vuln1"], e["vuln2"]) if v),
                            bool(e["must_win"]), e["boost"])
               for e in kp["enemies"]]
    cond = {key: kp[field] for field, key in COND_KEYS.items() if kp[field]}
    no_mods = frozenset(kind for kind in ("grey", "green") if kp[f"no_{kind}"])
    return solver.KeyPoint(str(kp["number"]), enemies, cond, kp["min_wins"],
                           barred_names(kp, cards), no_mods)


def solve(db, stage):
    """Run the solver on the stage's unplayed key points and store the two best plans."""
    _check_open(stage)
    open_kps = [kp for kp in stage["keypoints"] if not kp["played"]]
    if not open_kps:
        raise SandboxError("There are no unplayed key points to solve.")
    cards = map_cards(db, stage["map_id"])
    roster = [to_solver_card(c, used=bool(c["used"])) for c in cards]
    # No stage goal: the ranking already maximizes wins, after must-win enemies and each key
    # point's own minimum.
    search = {}
    best, n_plans, n_total = solver.solve_stage(
        roster, [solver_keypoint(kp, cards) for kp in open_kps], 0,
        AVAILABLE_MODS, SOLVE_LIMITS, report=search)
    ids = {c["name"]: c["id"] for c in cards}
    solution = {"compared": n_plans,
                "variants": n_total, "options": search["options"], "exact": search["exact"],
                "modified": search["modified"], "candidates": search["candidates"],
                "solved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "plans": [_plan_json(combo, st, open_kps, ids, best[0][0] if i else None)
                          for i, (combo, st) in enumerate(best)]}
    with db:
        db.execute("UPDATE stage SET solution = ?, solution_stale = 0 WHERE id = ?",
                   (json.dumps(solution), stage["id"]))
    return solution


SUMMARY_KEYS = ("spend", "exp_wins", "cost")


def _plan_json(combo, st, open_kps, ids, first=None):
    """A plan for the page; `first` (plan 1) adds which cards this plan assigns
    differently."""
    keypoints = []
    for kp, (_, _, mods, res, sm) in zip(open_kps, combo):
        slots = []
        for i, r in enumerate(res):
            c = r["card"]
            slots.append({"card": None, "E": r["E"]} if c is None else
                         {"card": c.name, "map_card_id": ids[c.name], "mods": list(mods[i]),
                          **r["params"], "P": r["P"], "E": r["E"], "p": r["p"]})
        keypoints.append({"keypoint_id": kp["id"], "number": kp["number"], "slots": slots,
                          "p_all": prod(s["p"] for s in slots if s["card"]),
                          **{k: sm[k] for k in SUMMARY_KEYS}})
    risky = sorted(keypoints, key=lambda k: k["p_all"])
    return {"p_goal": st["p_goal"], **{k: st[k] for k in SUMMARY_KEYS},
            "keypoints": keypoints,
            "changed": sorted(solver.changed_cards(combo, first)) if first else [],
            "order": None if all(k["p_all"] >= 1 for k in keypoints) or len(keypoints) < 2
            else [k["number"] for k in risky]}
