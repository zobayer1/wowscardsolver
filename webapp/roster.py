"""Roster pages: list, add, edit and remove cards."""

import csv
import io
import sqlite3

from flask import (Blueprint, Response, abort, flash, redirect, render_template, request,
                   url_for)

from .db import get_db, import_cards
from .engine import solver

bp = Blueprint("roster", __name__, url_prefix="/roster")

PARAM_LABELS = {"F": "Firepower", "S": "Survivability", "M": "Maneuverability"}
DEFAULT_PCT = {"buff": 30, "debuff": 20}
DIRECTION_ARROWS = {"left": "←", "right": "→", "up-left": "↖", "up": "↑", "up-right": "↗"}

# Valid targets and directions per ability type, for the form and for validation.
ABILITY_CHOICES = {
    "buff": {"targets": [[k, v] for k, v in PARAM_LABELS.items()],
             "directions": list(solver.BUFF_OFFSET)},
    "debuff": {"targets": [[v, v] for v in solver.VULNS],
               "directions": list(solver.DEBUFF_OFFSET)},
}
STAT_FIELDS = ("firepower", "survivability", "maneuverability")
CSV_COLUMNS = ("name", *STAT_FIELDS,
               *(f"ability{n}_{f}" for n in (1, 2)
                 for f in ("type", "target", "direction", "pct")))


def describe_ability(card, n):
    """Short text for ability n of a card row, or "" if it has none."""
    kind = card[f"ability{n}_type"]
    if not kind:
        return ""
    target = card[f"ability{n}_target"]
    direction = card[f"ability{n}_direction"]
    sign = "+" if kind == "buff" else "−"
    return (f"{PARAM_LABELS.get(target, target)} {sign}{card[f'ability{n}_pct']}% "
            f"{DIRECTION_ARROWS.get(direction, '')} {direction}")


def parse_form(form):
    """Card column values from the submitted form, and a list of error messages."""
    errors = []
    row = {"name": form.get("name", "").strip()}
    if not row["name"]:
        errors.append("Name is required.")
    for field in STAT_FIELDS:
        try:
            row[field] = int(form.get(field, ""))
            if row[field] < 0:
                raise ValueError
        except ValueError:
            errors.append(f"{field.capitalize()} must be a whole number, 0 or more.")
            row[field] = form.get(field, "")
    for n in (1, 2):
        kind = form.get(f"ability{n}_type", "")
        target = form.get(f"ability{n}_target", "")
        direction = form.get(f"ability{n}_direction", "")
        pct_text = form.get(f"ability{n}_pct", "").strip()
        row.update({f"ability{n}_type": kind or None, f"ability{n}_target": target or None,
                    f"ability{n}_direction": direction or None, f"ability{n}_pct": None})
        if not kind:
            row[f"ability{n}_target"] = row[f"ability{n}_direction"] = None
            continue
        choices = ABILITY_CHOICES.get(kind)
        if choices is None:
            errors.append(f"Ability {n}: unknown type {kind!r}.")
            continue
        if target not in [t for t, _ in choices["targets"]]:
            errors.append(f"Ability {n}: choose a target for the {kind}.")
        if direction not in choices["directions"]:
            errors.append(f"Ability {n}: choose a direction for the {kind}.")
        try:
            row[f"ability{n}_pct"] = int(pct_text) if pct_text else DEFAULT_PCT[kind]
            if row[f"ability{n}_pct"] <= 0:
                raise ValueError
        except ValueError:
            errors.append(f"Ability {n}: strength must be a whole number above 0.")
            row[f"ability{n}_pct"] = pct_text
    return row, errors


def get_card(card_id):
    """The card row with this id, or a 404."""
    card = get_db().execute("SELECT * FROM card WHERE id = ?", (card_id,)).fetchone()
    if card is None:
        abort(404, f"Card {card_id} not found.")
    return card


def _render_form(card, title):
    return render_template("roster/form.html", card=card, title=title,
                           choices=ABILITY_CHOICES, default_pct=DEFAULT_PCT)


def _save(row, card_id=None):
    """Insert or update a card; returns an error message if the name is taken."""
    db = get_db()
    cols = list(row)
    try:
        if card_id is None:
            db.execute(f"INSERT INTO card ({', '.join(cols)}) "
                       f"VALUES ({', '.join('?' for _ in cols)})", [row[c] for c in cols])
        else:
            db.execute(f"UPDATE card SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?",
                       [row[c] for c in cols] + [card_id])
        db.commit()
    except sqlite3.IntegrityError:
        return f"A card named {row['name']!r} already exists."
    return None


@bp.route("/")
def index():
    """The roster table."""
    cards = get_db().execute("SELECT * FROM card ORDER BY name COLLATE NOCASE").fetchall()
    return render_template("roster/list.html", cards=cards, describe=describe_ability)


@bp.route("/add", methods=("GET", "POST"))
def add():
    """Add a card."""
    if request.method == "POST":
        row, errors = parse_form(request.form)
        if not errors:
            error = _save(row)
            if error is None:
                flash(f"Added {row['name']}.")
                return redirect(url_for("roster.index"))
            errors.append(error)
        for e in errors:
            flash(e, "error")
        return _render_form(row, "Add card")
    return _render_form({}, "Add card")


@bp.route("/<int:card_id>/edit", methods=("GET", "POST"))
def edit(card_id):
    """Edit a card."""
    card = get_card(card_id)
    if request.method == "POST":
        row, errors = parse_form(request.form)
        if not errors:
            error = _save(row, card_id)
            if error is None:
                flash(f"Saved {row['name']}.")
                return redirect(url_for("roster.index"))
            errors.append(error)
        for e in errors:
            flash(e, "error")
        return _render_form(row, f"Edit {card['name']}")
    return _render_form(dict(card), f"Edit {card['name']}")


@bp.route("/import", methods=("POST",))
def import_csv():
    """Add or update cards from an uploaded roster CSV; hp and used columns are ignored."""
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        flash("Choose a CSV file to import.", "error")
        return redirect(url_for("roster.index"))
    try:
        text = upload.read().decode("utf-8-sig")
        cards = solver.parse_roster(solver.clean_rows(csv.DictReader(io.StringIO(text))))
    except (UnicodeDecodeError, ValueError, csv.Error) as e:
        flash(f"Could not import {upload.filename}: {e}. Nothing was changed.", "error")
        return redirect(url_for("roster.index"))
    added, updated = import_cards(get_db(), cards)
    flash(f"Imported {upload.filename}: {added} added, {updated} updated.")
    return redirect(url_for("roster.index"))


@bp.route("/export")
def export_csv():
    """The roster as a CSV in the format the solver reads."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for card in get_db().execute("SELECT * FROM card ORDER BY id"):
        row = dict(card)
        for n in (1, 2):
            if row[f"ability{n}_type"] == "buff":
                row[f"ability{n}_target"] = PARAM_LABELS[row[f"ability{n}_target"]]
        writer.writerow(["" if row[c] is None else row[c] for c in CSV_COLUMNS])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=roster.csv"})


@bp.route("/<int:card_id>/delete", methods=("POST",))
def delete(card_id):
    """Remove a card from the roster."""
    card = get_card(card_id)
    db = get_db()
    db.execute("DELETE FROM card WHERE id = ?", (card_id,))
    db.commit()
    flash(f"Removed {card['name']}.")
    return redirect(url_for("roster.index"))
