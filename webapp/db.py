"""SQLite connection handling and database CLI commands."""

import sqlite3
from pathlib import Path

import click
from flask import current_app, g

from .engine import solver


def get_db():
    """The request's database connection, opened on first use."""
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    """Close the request's connection, if one was opened."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Columns added after a table was first created: (table, column, declaration).
MIGRATIONS = [("map_card", "used", "INTEGER NOT NULL DEFAULT 0"),
              *(("keypoint", column, "INTEGER NOT NULL DEFAULT 0")
                for column in ("buff_power", "debuff_power")),
              ("card", "type", "TEXT NOT NULL DEFAULT ''"),
              ("map_card", "type", "TEXT NOT NULL DEFAULT ''"),
              ("keypoint", "barred_types", "TEXT NOT NULL DEFAULT '[]'"),
              ("enemy", "boost", "INTEGER NOT NULL DEFAULT 0")]


# Columns no longer used: (table, column). Dropped when present. Stages used to have a wins
# goal and their own environment; everything is now set per key point.
DROPPED = [("enemy", "reward"),
           *(("stage", column) for column in (
               "wins_needed", "firepower", "survivability", "maneuverability", "enemy_power",
               "buff_power", "debuff_power", "barred", "barred_types", "no_grey", "no_green",
               "all_keypoints"))]
# Tables no longer used. Modifiers used to be counted (per map, then shared); the solver now
# assumes any modifier can be bought.
DROPPED_TABLES = ("map_modifier", "modifier")


def init_db():
    """Create any missing tables and columns; drop what is no longer used."""
    db = get_db()
    db.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
    for table, column, decl in MIGRATIONS:
        if column not in {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    for table, column in DROPPED:
        if column in {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    for table in DROPPED_TABLES:
        db.execute(f"DROP TABLE IF EXISTS {table}")
    db.commit()


def card_to_row(card):
    """Column values for a solver.Card (HP and used are map state, so dropped). The type is
    left out when unknown (None), so existing types are kept."""
    row = {"name": card.name, "firepower": card.F, "survivability": card.S,
           "maneuverability": card.M}
    for n in (1, 2):
        a = card.abilities[n - 1] if len(card.abilities) >= n else None
        row[f"ability{n}_type"] = a.kind if a else None
        row[f"ability{n}_target"] = a.target if a else None
        row[f"ability{n}_direction"] = a.dir if a else None
        row[f"ability{n}_pct"] = a.pct if a else None
    if card.type is not None:
        row["type"] = card.type
    return row


def upsert_card(db, row):
    """Insert a card, or overwrite the one with the same name."""
    cols = list(row)
    db.execute(
        f"INSERT INTO card ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
        f"ON CONFLICT(name) DO UPDATE SET "
        f"{', '.join(f'{c} = excluded.{c}' for c in cols if c != 'name')}",
        [row[c] for c in cols])


def import_cards(db, cards):
    """Add or update solver.Cards by name in one transaction. Returns (added, updated)."""
    existing = {r["name"] for r in db.execute("SELECT name FROM card")}
    with db:
        for card in cards:
            upsert_card(db, card_to_row(card))
    updated = sum(1 for c in cards if c.name in existing)
    return len(cards) - updated, updated


@click.command("init-db")
def init_db_command():
    """Create the database tables."""
    init_db()
    click.echo(f"Initialized {current_app.config['DATABASE']}")


@click.command("import-roster")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
def import_roster_command(path):
    """Add or update cards from a roster CSV (same format the solver reads)."""
    init_db()
    added, updated = import_cards(get_db(), solver.load_roster(path))
    click.echo(f"Imported {path}: {added} added, {updated} updated")


def init_app(app):
    """Register teardown and CLI commands, and make sure the tables exist."""
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
    app.cli.add_command(import_roster_command)
    with app.app_context():
        init_db()
