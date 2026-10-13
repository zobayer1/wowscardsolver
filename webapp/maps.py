"""Map pages: start a map, then add stages and key points, solve, and record battles."""

from flask import (Blueprint, abort, flash, jsonify, redirect, render_template, request,
                   url_for)

from . import sandbox
from .db import get_db
from .engine import solver
from .roster import describe_ability

bp = Blueprint("maps", __name__, url_prefix="/maps")

ENV_LABELS = {"firepower": "Firepower %", "survivability": "Survivability %",
              "maneuverability": "Maneuverability %", "enemy_power": "Enemy power %",
              "buff_power": "Buff power %", "debuff_power": "Debuff power %"}


# ---------------------------------------------------------------- helpers
def _get_map(map_id):
    row = get_db().execute("SELECT * FROM map WHERE id = ?", (map_id,)).fetchone()
    if row is None:
        abort(404, f"Map {map_id} not found.")
    return row


def _get_stage(stage_id):
    stage = sandbox.load_stage(get_db(), stage_id)
    if stage is None:
        abort(404, f"Stage {stage_id} not found.")
    return stage


def _stage_of_keypoint(keypoint_id):
    row = get_db().execute("SELECT stage_id FROM keypoint WHERE id = ?", (keypoint_id,)).fetchone()
    if row is None:
        abort(404, f"Key point {keypoint_id} not found.")
    return _get_stage(row["stage_id"])


def _to_map(stage_or_map_id, anchor=None):
    map_id = stage_or_map_id if isinstance(stage_or_map_id, int) else stage_or_map_id["map_id"]
    return redirect(url_for("maps.view", map_id=map_id) + (f"#{anchor}" if anchor else ""))


def _int(form, field, errors, label, minimum=None):
    """Whole number from a form field (blank = 0); problems are appended to errors."""
    text = form.get(field, "").strip()
    if not text:
        return 0
    try:
        value = int(text)
    except ValueError:
        errors.append(f"{label} must be a whole number.")
        return 0
    if minimum is not None and value < minimum:
        errors.append(f"{label} must be {minimum} or more.")
    return value


def parse_env(form, card_names):
    """Environment fields shared by stages and key points, and a list of errors."""
    errors = []
    env = {f: _int(form, f, errors, ENV_LABELS[f]) for f in sandbox.ENV_FIELDS}
    env["barred"] = [n for n in form.getlist("barred") if n in card_names]
    env["no_grey"] = int("no_grey" in form)
    env["no_green"] = int("no_green" in form)
    return env, errors


def parse_enemies(form):
    """Four enemy slots (None = empty) and a list of errors."""
    enemies, errors = [], []
    for slot in range(1, 5):
        if not form.get(f"power{slot}", "").strip():
            enemies.append(None)
            continue
        vulns = [form.get(f"vuln{n}_{slot}", "") or None for n in (1, 2)]
        for v in vulns:
            if v and v not in solver.VULNS:
                errors.append(f"Slot {slot}: unknown vulnerability {v!r}.")
        if vulns[0] and vulns[0] == vulns[1]:
            errors.append(f"Slot {slot}: the two vulnerabilities are the same.")
        try:
            reward = float(form.get(f"reward{slot}", "").strip() or 0)
        except ValueError:
            errors.append(f"Slot {slot}: reward must be a number.")
            reward = 0
        enemies.append({"power": _int(form, f"power{slot}", errors, f"Slot {slot} power", 1),
                        "vuln1": vulns[0], "vuln2": vulns[1], "reward": reward,
                        "must_win": int(f"must_win{slot}" in form)})
    if not any(enemies):
        errors.append("Enter at least one enemy.")
    return enemies, errors


def parse_battle(form, kp):
    """Battle entries for the slots that have a card."""
    entries = []
    for slot in range(1, 5):
        card_id = form.get(f"card{slot}", "")
        if not card_id or kp["enemies"][slot - 1] is None:
            continue
        entries.append({"slot": slot, "map_card_id": int(card_id),
                        "grey_mod": form.get(f"grey{slot}") or None,
                        "green_mod": form.get(f"green{slot}") or None,
                        "won": f"won{slot}" in form})
    return entries


def _flash_errors(errors):
    for e in errors:
        flash(e, "error")


# ---------------------------------------------------------------- maps
@bp.route("/")
def index():
    """All maps, newest first."""
    maps = get_db().execute(
        "SELECT m.*, (SELECT COUNT(*) FROM stage s WHERE s.map_id = m.id) AS stages, "
        "(SELECT COUNT(*) FROM map_card c WHERE c.map_id = m.id) AS cards "
        "FROM map m ORDER BY m.id DESC").fetchall()
    return render_template("maps/list.html", maps=maps)


@bp.route("/new", methods=("POST",))
def new():
    """Start a map from the current roster."""
    db = get_db()
    name = request.form.get("name", "").strip()
    if not name:
        n = db.execute("SELECT COUNT(*) FROM map").fetchone()[0] + 1
        name = f"Map {n}"
    map_id = sandbox.create_map(db, name)
    return _to_map(map_id)


@bp.route("/<int:map_id>/delete", methods=("POST",))
def delete(map_id):
    """Delete a map and everything recorded in it."""
    m = _get_map(map_id)
    db = get_db()
    with db:
        db.execute("DELETE FROM map WHERE id = ?", (map_id,))
    flash(f"Deleted {m['name']}.")
    return redirect(url_for("maps.index"))


@bp.route("/<int:map_id>")
def view(map_id):
    """The map page: cards, modifiers and stages."""
    return _render_map(map_id)


def _render_map(map_id, draft=None):
    """The map page. draft: {"kp_id": key point id or None for a new one, "kp": entered
    values} to reopen a key point form that failed validation."""
    db = get_db()
    m = _get_map(map_id)
    stages = sandbox.load_stages(db, map_id)
    current = stages[-1] if stages and not stages[-1]["finished"] else None
    return render_template(
        "maps/map.html", map=m, cards=sandbox.map_cards(db, map_id), stages=stages,
        current=current, draft=draft, stock=sandbox.modifier_stock(db),
        modifiers=sandbox.modifier_counts(db),
        grey_mods=sandbox.GREY_MODS, green_mods=sandbox.GREEN_MODS, vuln_names=solver.VULNS,
        env_labels=ENV_LABELS, describe=describe_ability,
        max_keypoints=sandbox.MAX_KEYPOINTS,
        goal=sandbox.stage_goal(current, [k for k in current["keypoints"] if not k["played"]])
        if current else 0)


@bp.route("/<int:map_id>/cards", methods=("POST",))
def set_cards(map_id):
    """Correct card HP and used flags by hand."""
    _get_map(map_id)
    state = {}
    for c in sandbox.map_cards(get_db(), map_id):
        hp = request.form.get(f"hp_{c['id']}")
        if hp in ("0", "1", "2", "3"):
            state[c["id"]] = (int(hp), f"used_{c['id']}" in request.form)
    sandbox.set_card_state(get_db(), map_id, state)
    flash("Cards saved.")
    return _to_map(map_id, "cards")


@bp.route("/<int:map_id>/sync", methods=("POST",))
def sync_roster(map_id):
    """Bring cards added to the roster since the map started into the map."""
    _get_map(map_id)
    added = sandbox.sync_roster(get_db(), map_id)
    flash(f"Added {', '.join(added)} at 3 HP." if added else
          "The map already has every roster card.")
    return _to_map(map_id, "cards")


@bp.route("/<int:map_id>/modifiers", methods=("POST",))
def set_modifiers(map_id):
    """Save the shared modifier stock, unless it changed since the page was loaded."""
    _get_map(map_id)
    errors = []
    counts = {n: _int(request.form, f"mod_{n}", errors, n, minimum=0) for n in solver.MODIFIERS}
    versions = {}
    for n in solver.MODIFIERS:
        try:
            versions[n] = int(request.form.get(f"ver_{n}", ""))
        except ValueError:
            versions[n] = None
    if not errors:
        try:
            changed = sandbox.set_modifier_counts(get_db(), counts, versions)
            flash(f"Modifier stock saved: {', '.join(f'{n} {c}' for n, c in changed.items())}."
                  if changed else "Modifier stock unchanged.")
        except sandbox.SandboxError as e:
            errors.append(str(e))
    _flash_errors(errors)
    return _to_map(map_id, "modifiers")


@bp.route("/modifiers.json")
def modifiers_json():
    """The shared modifier stock with versions, for the Reload counts button."""
    return jsonify(sandbox.modifier_stock(get_db()))


# ---------------------------------------------------------------- stages
@bp.route("/<int:map_id>/stages/new", methods=("POST",))
def new_stage(map_id):
    """Open the next stage."""
    _get_map(map_id)
    try:
        sandbox.create_stage(get_db(), map_id)
    except sandbox.SandboxError as e:
        flash(str(e), "error")
    return _to_map(map_id, "current-stage")


def _stage_action(stage_id, action, message):
    stage = _get_stage(stage_id)
    try:
        action(get_db(), stage)
        if message:
            flash(message.format(n=stage["number"]))
    except sandbox.SandboxError as e:
        flash(str(e), "error")
    return _to_map(stage, f"stage-{stage['number']}")


@bp.route("/stage/<int:stage_id>/env", methods=("POST",))
def stage_env(stage_id):
    """Save the stage's environment."""
    stage = _get_stage(stage_id)
    names = {c["name"] for c in sandbox.map_cards(get_db(), stage["map_id"])}
    env, errors = parse_env(request.form, names)
    env["wins_needed"] = _int(request.form, "wins_needed", errors, "Wins needed", minimum=0)
    env["all_keypoints"] = int("all_keypoints" in request.form)
    if errors:
        _flash_errors(errors)
        return _to_map(stage, "current-stage")
    return _stage_action(stage_id, lambda db, s: sandbox.update_stage_env(db, s, env),
                         "Stage {n} settings saved.")


@bp.route("/stage/<int:stage_id>/solve", methods=("POST",))
def solve(stage_id):
    """Run the solver for the stage's unplayed key points."""
    return _stage_action(stage_id, sandbox.solve, None)


@bp.route("/stage/<int:stage_id>/apply/<int:plan_index>", methods=("POST",))
def apply_plan(stage_id, plan_index):
    """Record a whole solver plan: HP, used flags and modifiers update at once."""
    stage = _get_stage(stage_id)
    try:
        numbers = sandbox.apply_plan(get_db(), stage, plan_index)
        flash(f"Applied plan {plan_index + 1} to key point{'s' if len(numbers) > 1 else ''} "
              f"{', '.join(map(str, numbers))}. HP, used cards and modifiers updated. "
              f"Click a slot's result to correct a win or loss.")
    except sandbox.SandboxError as e:
        flash(str(e), "error")
    return _to_map(stage, "current-stage")


@bp.route("/stage/<int:stage_id>/finish", methods=("POST",))
def finish_stage(stage_id):
    """Close the stage."""
    return _stage_action(stage_id, sandbox.finish_stage, "Stage {n} finished.")


@bp.route("/stage/<int:stage_id>/reopen", methods=("POST",))
def reopen_stage(stage_id):
    """Reopen the latest stage."""
    return _stage_action(stage_id, sandbox.reopen_stage, "Stage {n} reopened.")


@bp.route("/stage/<int:stage_id>/delete", methods=("POST",))
def delete_stage(stage_id):
    """Delete an open stage with no recorded battles."""
    return _stage_action(stage_id, sandbox.delete_stage, "Stage {n} deleted.")


# ---------------------------------------------------------------- key points
def _save_keypoint(stage, keypoint_id=None):
    """Save the posted key point form. On errors, show the map again with the form open
    and the entered values kept."""
    names = {c["name"] for c in sandbox.map_cards(get_db(), stage["map_id"])}
    env, errors = parse_env(request.form, names)
    env["min_wins"] = _int(request.form, "min_wins", errors, "Wins needed here", minimum=0)
    enemies, enemy_errors = parse_enemies(request.form)
    errors += enemy_errors
    if not errors:
        try:
            kid = sandbox.save_keypoint(get_db(), stage, env, enemies, keypoint_id)
            flash("Key point saved.")
            return _to_map(stage, f"kp-{kid}")
        except sandbox.SandboxError as e:
            errors.append(str(e))
    _flash_errors(errors)
    return _render_map(stage["map_id"],
                       draft={"kp_id": keypoint_id, "kp": dict(env, enemies=enemies)})


@bp.route("/stage/<int:stage_id>/keypoints/new", methods=("POST",))
def new_keypoint(stage_id):
    """Add a key point to a stage."""
    return _save_keypoint(_get_stage(stage_id))


@bp.route("/keypoint/<int:keypoint_id>/edit", methods=("POST",))
def edit_keypoint(keypoint_id):
    """Change an unplayed key point."""
    return _save_keypoint(_stage_of_keypoint(keypoint_id), keypoint_id)


@bp.route("/keypoint/<int:keypoint_id>/delete", methods=("POST",))
def delete_keypoint(keypoint_id):
    """Delete an unplayed key point."""
    stage = _stage_of_keypoint(keypoint_id)
    try:
        sandbox.delete_keypoint(get_db(), stage, keypoint_id)
        flash("Key point deleted.")
    except sandbox.SandboxError as e:
        flash(str(e), "error")
    return _to_map(stage, "current-stage")


@bp.route("/keypoint/<int:keypoint_id>/record", methods=("POST",))
def record(keypoint_id):
    """Record the battle fought at a key point."""
    stage = _stage_of_keypoint(keypoint_id)
    kp = next(k for k in stage["keypoints"] if k["id"] == keypoint_id)
    try:
        sandbox.record_battle(get_db(), stage, keypoint_id, parse_battle(request.form, kp))
        flash(f"Recorded key point {kp['number']}. Card HP and modifier stock updated.")
    except sandbox.SandboxError as e:
        flash(str(e), "error")
    return _to_map(stage, f"kp-{keypoint_id}")


@bp.route("/keypoint/<int:keypoint_id>/slot/<int:slot>/toggle", methods=("POST",))
def toggle_won(keypoint_id, slot):
    """Flip a recorded battle between won and lost."""
    stage = _stage_of_keypoint(keypoint_id)
    try:
        sandbox.toggle_won(get_db(), stage, keypoint_id, slot)
    except sandbox.SandboxError as e:
        flash(str(e), "error")
    return _to_map(stage, f"kp-{keypoint_id}")


@bp.route("/keypoint/<int:keypoint_id>/undo", methods=("POST",))
def undo(keypoint_id):
    """Undo a recorded battle."""
    stage = _stage_of_keypoint(keypoint_id)
    try:
        sandbox.undo_battle(get_db(), stage, keypoint_id)
        flash("Battle undone. Card HP and modifier stock restored.")
    except sandbox.SandboxError as e:
        flash(str(e), "error")
    return _to_map(stage, f"kp-{keypoint_id}")
