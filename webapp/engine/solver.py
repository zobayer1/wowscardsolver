"""
Key-point solver for the 4-vs-4 card game: the engine's entry module.

The battle model lives in model.py and the stage search in search.py; their public names
are re-exported here, so `from webapp.engine import solver` gives the whole engine. This
module adds the CSV readers (roster.csv for the roster import; board.csv, environment.csv and
modifiers.csv describe a stage, as in tests/data) and validate(), which checks the battle
model against numbers observed in the game.
"""

import csv
import os
from dataclasses import replace

from .model import (AA, AIR, BUFF_OFFSET, DEBUFF_OFFSET, DETECT, HP_PCT, LARGE, MODIFIERS,
                    PARAM_MODS, PARAMS, QUICK, SMOKE, VULNS, Ability, Card, Enemy, KeyPoint,
                    buff, debuff, evaluate, rank_key, round_half_up, stage_summary, summarize)
from .search import (SearchLimits, TopK, arrangements, best_by_subset, changed_cards,
                     modifier_variants, solve_stage)

__all__ = ["AA", "AIR", "BUFF_OFFSET", "DEBUFF_OFFSET", "DETECT", "HP_PCT", "LARGE",
           "MODIFIERS", "PARAM_MODS", "PARAMS", "QUICK", "SMOKE", "VULNS", "Ability", "Card",
           "Enemy", "KeyPoint", "SearchLimits", "TopK", "arrangements", "best_by_subset",
           "changed_cards",
           "buff", "debuff", "evaluate", "modifier_variants", "rank_key", "replace",
           "round_half_up", "solve_stage", "stage_summary", "summarize",
           "clean_rows", "parse_roster", "load_roster", "load_modifiers", "load_board",
           "load_environment", "cond_for", "load_stage", "validate", "EXAMPLE_ROSTER",
           "EXAMPLE_KEYPOINT"]


def _yes(v):
    return (v or "").strip().lower() in ("yes", "y", "true", "1", "x")


# ---------------------------------------------------------------- CSV input


PARAM_NAMES = {"firepower": "F", "survivability": "S", "maneuverability": "M",
               "f": "F", "s": "S", "m": "M"}


NOMOD_KEYS = {"no modifiers": {"grey", "green"}, "no grey modifiers": {"grey"},
              "no green modifiers": {"green"}}


ENV_KEYS = {"firepower": "F", "survivability": "S", "maneuverability": "M",
            "enemy power": "E", "buff power": "B", "debuff power": "D"}


def clean_rows(reader):
    """Rows of a csv.DictReader with keys and values stripped (extra unnamed cells dropped)."""
    for row in reader:
        yield {k.strip(): (v or "").strip() for k, v in row.items() if k is not None}


def _rows(path):
    """CSV rows as dicts with keys and values stripped."""
    with open(path, newline="", encoding="utf-8") as f:
        yield from clean_rows(csv.DictReader(f))


def _ability(row, n):
    """Ability n (1 or 2) of a roster row, or None if blank."""
    kind = row.get(f"ability{n}_type", "").lower()
    if not kind:
        return None
    where = f"{row['name']} ability {n}"
    target = row.get(f"ability{n}_target", "")
    direction = row.get(f"ability{n}_direction", "").lower()
    if kind == "buff":
        if target.lower() not in PARAM_NAMES:
            raise ValueError(f"{where}: unknown buff target {target!r}")
        target = PARAM_NAMES[target.lower()]
        directions = BUFF_OFFSET
    elif kind == "debuff":
        if target not in VULNS:
            raise ValueError(f"{where}: unknown vulnerability {target!r}")
        directions = DEBUFF_OFFSET
    else:
        raise ValueError(f"{where}: type must be buff or debuff, got {kind!r}")
    if direction not in directions:
        raise ValueError(f"{where}: {kind} direction must be one of "
                         f"{', '.join(directions)}, got {direction!r}")
    pct = int(row.get(f"ability{n}_pct") or (30 if kind == "buff" else 20))
    return Ability(kind, target, direction, pct)


def parse_roster(rows):
    """Cards from cleaned roster rows. Columns: name, firepower, survivability,
    maneuverability, [hp, used,] then ability1_* / ability2_* (type, target, direction, pct),
    [type]. Blank ability = none. Without a type column the type is None (unknown), so an
    import does not wipe types already set. Raises ValueError naming the bad row."""
    cards = []
    for line, row in enumerate(rows, start=2):
        try:
            if not row.get("name"):
                raise ValueError("name is missing")
            cards.append(Card(row["name"], int(row["firepower"]), int(row["survivability"]),
                              int(row["maneuverability"]),
                              tuple(a for a in (_ability(row, 1), _ability(row, 2)) if a),
                              hp=int(row.get("hp") or 3), used=_yes(row.get("used", "")),
                              type=row.get("type")))
        except KeyError as e:
            raise ValueError(f"line {line}: missing column {e.args[0]!r}") from e
        except ValueError as e:
            raise ValueError(f"line {line}: {e}") from e
    return cards


def load_roster(path):
    """Read cards from a roster CSV file (see parse_roster)."""
    return parse_roster(_rows(path))


def load_modifiers(path):
    """Read modifiers.csv (name, type, count, value[, effect]).
    Returns the inventory {name: count}; updates each modifier's point value."""
    inventory = {}
    for row in _rows(path):
        name = row["name"]
        assert name in MODIFIERS, f"unknown modifier in {path}: {name!r}"
        kind = row.get("type", "").lower() or MODIFIERS[name][0]
        assert kind == MODIFIERS[name][0], f"{name} is a {MODIFIERS[name][0]} modifier"
        value = float(row["value"]) if row.get("value") else MODIFIERS[name][1]
        MODIFIERS[name] = (kind, value)
        inventory[name] = int(row.get("count") or 0)
    return inventory


def load_board(path):
    """Read board.csv: [keypoint,] slot, power, vuln1, vuln2, must_win, [boost]. A reward
    column (older files) is ignored.
    Returns {keypoint name: [Enemy or None] * 4}, in file order. Blank power = empty slot."""
    boards = {}
    for row in _rows(path):
        kp = row.get("keypoint") or "1"
        board = boards.setdefault(kp, [None] * 4)
        slot = int(row["slot"])
        assert 1 <= slot <= 4, f"key point {kp}: slot must be 1-4, got {slot}"
        if not row.get("power"):
            continue
        vulns = tuple(v for v in (row.get("vuln1", ""), row.get("vuln2", "")) if v)
        for v in vulns:
            assert v in VULNS, f"key point {kp} slot {slot}: unknown vulnerability {v!r}"
        board[slot - 1] = Enemy(int(row["power"]), vulns, _yes(row.get("must_win", "")),
                                int(row.get("boost") or 0))
    return boards


def load_environment(path):
    """Read environment.csv: setting, [keypoint,] value.
    'Wins needed' with a blank keypoint is the stage goal; with a keypoint it is a minimum
    that must also be met at that key point. A condition with a blank keypoint applies to
    every key point; one with a keypoint applies only there (added on top).
    'Barred' rows name a card (in the value column) that cannot go to that key point;
    'Barred type' rows name a card type, barring every card of that type.
    'No modifiers' / 'No grey modifiers' / 'No green modifiers' (value yes) block modifier
    slots at that key point.
    Returns (stage wins needed, {keypoint or "*": cond dict}, {keypoint: wins needed},
             {keypoint or "*": set of barred card names},
             {keypoint or "*": set of blocked slot types},
             {keypoint or "*": set of barred card types})."""
    conds, min_wins, kp_mins, barred, nomods, barred_types = {}, 0, {}, {}, {}, {}
    for row in _rows(path):
        key, val = row["setting"].lower(), row.get("value") or "0"
        kp = row.get("keypoint") or "*"
        if key == "barred":
            barred.setdefault(kp, set()).add(row["value"])
        elif key == "barred type":
            barred_types.setdefault(kp, set()).add(row["value"])
        elif key in NOMOD_KEYS:
            if _yes(row.get("value", "")):
                nomods.setdefault(kp, set()).update(NOMOD_KEYS[key])
        elif key == "wins needed":
            if kp == "*":
                min_wins = int(val)
            else:
                kp_mins[kp] = int(val)
        else:
            assert key in ENV_KEYS, f"unknown setting in {path}: {row['setting']!r}"
            if float(val):
                c = conds.setdefault(kp, {})
                c[ENV_KEYS[key]] = c.get(ENV_KEYS[key], 0) + int(float(val))
    return min_wins, conds, kp_mins, barred, nomods, barred_types


def cond_for(conds, kp):
    """Battle conditions at key point `kp`: stage-wide ones plus its own."""
    out = dict(conds.get("*", {}))
    for k, v in conds.get(kp, {}).items():
        out[k] = out.get(k, 0) + v
    return out


def load_stage(board_path, env_path, roster):
    """Key points of the stage from the board and environment files, checked against the
    roster. Barred types become the names of the roster cards of those types.
    Returns (keypoints, stage wins needed)."""
    boards = load_board(board_path)
    env = load_environment(env_path) if os.path.exists(env_path) else (0, {}, {}, {}, {}, {})
    _check_environment(env, boards, roster)
    min_wins, conds, kp_mins, barred, nomods, barred_types = env
    keypoints = [KeyPoint(name, enemies, cond_for(conds, name), kp_mins.get(name, 0),
                          _barred_at(name, barred, barred_types, roster),
                          frozenset(nomods.get("*", set()) | nomods.get(name, set())))
                 for name, enemies in boards.items()]
    return keypoints, min_wins


def _check_environment(env, boards, roster):
    """Every key point, barred card and barred type the environment names must exist."""
    _, conds, kp_mins, barred, nomods, barred_types = env
    for k in [*kp_mins, *barred, *conds, *nomods, *barred_types]:
        assert k == "*" or k in boards, \
            f"environment names key point {k!r}, which is not in the board"
    names = {c.name for c in roster}
    for k, cards in barred.items():
        for c in cards:
            assert c in names, f"barred card {c!r} at key point {k} is not in the roster"
    types = {c.type for c in roster if c.type}
    for k, kinds in barred_types.items():
        for t in kinds:
            assert t in types, f"barred type {t!r} at key point {k}: no roster card has it"


def _barred_at(name, barred, barred_types, roster):
    """Card names barred at key point `name`, by name or by type, stage-wide or there."""
    kinds = barred_types.get("*", set()) | barred_types.get(name, set())
    return frozenset(barred.get("*", set()) | barred.get(name, set())
                     | {c.name for c in roster if c.type in kinds})


# ---------------------------------------------------------------- data
# Cards and key point from Example 1 in Rules.md, at full HP. Used by validate(), and as the
# roster when roster.csv is missing.


EXAMPLE_ROSTER = (
    Card("Daunty", 35, 20, 55, (debuff(AIR, "up"), buff("F", "right"))),
    Card("KGV", 60, 60, 35, (debuff(DETECT, "up-right"), buff("F", "left"))),
    Card("NM", 50, 50, 15, (debuff(LARGE, "up-right"), buff("F", "left"))),
    Card("Sandy", 35, 35, 70, (debuff(AA, "up"), debuff(SMOKE, "up-right"))),
    Card("Marble", 30, 30, 40, (debuff(SMOKE, "up"), debuff(QUICK, "up-right"))),
    Card("Faringar", 30, 30, 45, (debuff(AA, "up-left"), debuff(SMOKE, "up-right"))),
    Card("Fjord", 30, 20, 50, (debuff(SMOKE, "up-left"), buff("S", "left"))),
    Card("Vampire", 30, 20, 50, (debuff(QUICK, "up-right"), buff("M", "right"))),
    Card("Writer", 30, 30, 40, (debuff(SMOKE, "up-left"), buff("M", "left"))),
    Card("Kiddo", 45, 45, 60, (debuff(QUICK, "up-right"), buff("S", "right"))),
    Card("Hugee", 30, 30, 50, (debuff(QUICK, "up"), debuff(SMOKE, "up-right"))),
)


EXAMPLE_KEYPOINT = [
    Enemy(95, (SMOKE, QUICK)),
    Enemy(99, (LARGE, QUICK)),
    Enemy(105, (AA, SMOKE)),
    Enemy(104, (QUICK, AIR)),
]


# ---------------------------------------------------------------- validation


def validate():
    """Check the battle model against numbers observed in the game."""
    cards = {c.name: c for c in EXAMPLE_ROSTER}

    def check(names, expect_power, expect_enemy, cond=None):
        slots = [cards[n] if n else None for n in names]
        res = evaluate(slots, EXAMPLE_KEYPOINT, cond)
        assert [r["P"] for r in res] == expect_power, (names, [r["P"] for r in res])
        assert [r["E"] for r in res] == expect_enemy, (names, [r["E"] for r in res])
        return res

    check(["Vampire", "Writer", "Marble", "Fjord"], [115, 112, 109, 100], [76, 79, 63, 83])
    res = check(["Vampire", "Writer", "Fjord", "Marble"], [115, 121, 100, 100], [76, 79, 105, 104])
    assert round(res[2]["p"] * 100) == 29 and round(res[3]["p"] * 100) == 33
    res = check(["Vampire", "Fjord", "Writer", "Marble"], [106, 130, 100, 100], [76, 79, 105, 104])
    assert res[1]["params"]["M"] == 80
    res = evaluate([cards["Vampire"], cards["Sandy"], cards["Fjord"], None],
                   EXAMPLE_KEYPOINT, {"F": -24})
    assert res[1]["params"] == {"F": 27, "S": 46, "M": 91} and res[1]["P"] == 164
    assert round_half_up(30 * 100 * 106, 10000) == 32
    assert round_half_up(35 * 100 * 106, 10000) == 37
    assert round_half_up(96 * 80, 100) == 77
    # Fjord at 2 HP, no buffs: 27, 18, 45 -> 80-100
    fjord2 = replace(cards["Fjord"], hp=2)
    res = evaluate([None, None, None, fjord2], EXAMPLE_KEYPOINT)
    assert res[3]["params"] == {"F": 27, "S": 18, "M": 45} and res[3]["P"] == 90
    # HP loss is not a battle condition: Unstoppable drops the -24% F condition, keeps 90% HP
    res = evaluate([None, None, None, fjord2], EXAMPLE_KEYPOINT, {"F": -24},
                   [(), (), (), ("Unstoppable",)])
    assert res[3]["params"] == {"F": 27, "S": 18, "M": 45}
