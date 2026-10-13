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
              *((table, column, "INTEGER NOT NULL DEFAULT 0")
                for table in ("stage", "keypoint") for column in ("buff_power", "debuff_power"))]


def init_db():
    """Create any missing tables and columns, and a stock row for every modifier."""
    db = get_db()
    db.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
    for table, column, decl in MIGRATIONS:
        if column not in {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    _migrate_map_modifiers(db)
    db.executemany("INSERT OR IGNORE INTO modifier (name) VALUES (?)",
                   [(n,) for n in solver.MODIFIERS])
    db.commit()


def _migrate_map_modifiers(db):
    """Older databases kept a modifier stock per map. The latest map's stock becomes the
    shared one (if none exists yet), and the old table is dropped."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                      "AND name = 'map_modifier'").fetchone():
        return
    if not db.execute("SELECT 1 FROM modifier").fetchone():
        db.execute("INSERT INTO modifier (name, count) SELECT name, count FROM map_modifier "
                   "WHERE map_id = (SELECT MAX(map_id) FROM map_modifier)")
    db.execute("DROP TABLE map_modifier")


def card_to_row(card):
    """Column values for a solver.Card (HP and used are map state, so dropped)."""
    row = {"name": card.name, "firepower": card.F, "survivability": card.S,
           "maneuverability": card.M}
    for n in (1, 2):
        a = card.abilities[n - 1] if len(card.abilities) >= n else None
        row[f"ability{n}_type"] = a.kind if a else None
        row[f"ability{n}_target"] = a.target if a else None
        row[f"ability{n}_direction"] = a.dir if a else None
        row[f"ability{n}_pct"] = a.pct if a else None
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
