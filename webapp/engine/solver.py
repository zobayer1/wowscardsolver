"""
Key-point solver for the 4-vs-4 card game.

Battle model (confirmed against in-game numbers):
  param  = round_half_up(base * (100 + buffs + modifiers + conditions - 10*HP lost)%)
           per parameter; HP loss is an additive -10% debuff per lost HP
  P      = F + S + M, roll range [P-10, P+10] (21 integer values)
  E      = round_half_up(enemy_base * (100 - sum of matching debuffs)%)
  win    = clamp(P + 11 - E, 0, 21) / 21      (ties go to you)

Ranking of arrangements (agreed):
  1. P(stage goal met)  -- all must-win enemies beaten AND at least `min_wins` goal credits
  2. expected reward points, minus the point value of modifiers consumed
  3. expected number of wins
  4. lowest card cost (power lost from the HP each used card spends)

Search (solve_stage):
  Phase 1  for every key point, brute-forces every card arrangement (empty slots allowed)
           without modifiers and keeps the best arrangement of each card set; then tries
           every split of the cards across key points (each card used at most once).
  Phase 2  takes the top `keep` splits and tries every modifier combination with at most
           `max_mods` modifiers on the whole stage, limited by the inventory.

Assumptions not yet confirmed in-game (marked ASSUMPTION below):
  - Focused: the card neither gives nor receives buffs, and its debuffs are off.
  - Unstoppable: battle conditions on the card's parameters and on the power of its own
    buffs/debuffs are ignored. It does not lift card bans (Barred), modifier bans or HP loss.
  - "Buff power" / "Debuff power" conditions scale every buff/debuff a card gives, adding to
    Teamwork / Specialization (+100%): 20% debuff at -50% -> 10%, with Specialization -> 30%.
  - An "Enemy power" condition adds to the same % pool as debuffs (e.g. +10% and one
    -20% debuff -> x0.90), and Unstoppable does not cancel it.
  - Fast Repair: +2 HP (cap 3) before the battle, then the battle costs 1 HP as usual.
  - Each modifier use is charged at its `value` column in modifiers.csv (points-equivalent),
    so a modifier is recommended only when it raises P(goal) or earns more than it costs.
  - A modifier is only considered when its `count` in modifiers.csv is above 0.
"""

import csv
import heapq
import os
import sys
from collections import Counter
from dataclasses import dataclass, replace
from itertools import combinations, permutations
from math import prod

PARAMS = ("F", "S", "M")
HP_PCT = {3: 100, 2: 90, 1: 80, 0: 0}

AA, SMOKE, DETECT, LARGE, QUICK, AIR = (
    "AA Fire", "Smoke Screen", "Detection", "Large Caliber", "Quick Fire", "Air Strike")
VULNS = (AA, SMOKE, DETECT, LARGE, QUICK, AIR)

DEBUFF_OFFSET = {"up-left": -1, "up": 0, "up-right": 1}
BUFF_OFFSET = {"left": -1, "right": 1}

# Built-in defaults: name -> (slot, point value of one use).
# The type, count and value actually used come from modifiers.csv.
MODIFIERS = {
    "Gunnery":         ("grey", 12),
    "Damage Control":  ("grey", 12),
    "Dodge":           ("grey", 12),
    "Teamwork":        ("grey", 12),
    "Effectiveness":   ("grey", 18),
    "Morale":          ("grey", 12),
    "Focused":         ("green", 15),
    "Specialization":  ("green", 15),
    "Rabid":           ("green", 15),
    "Fast Repair":     ("green", 15),
    "Unstoppable":     ("green", 15),
}
PARAM_MODS = {"Gunnery": {"F": 20}, "Damage Control": {"S": 20}, "Dodge": {"M": 20},
              "Focused": {"F": 40, "S": 40, "M": 40}}


def _yes(v):
    return (v or "").strip().lower() in ("yes", "y", "true", "1", "x")


def round_half_up(num, den):
    """Round num/den to nearest integer, halves up (integer math, no float error)."""
    return (2 * num + den) // (2 * den)


@dataclass(frozen=True)
class Ability:
    """A card's buff or debuff."""
    kind: str     # "buff" or "debuff"
    target: str   # buff: "F"/"S"/"M"; debuff: vulnerability name
    dir: str      # buff: left/right; debuff: up-left/up/up-right
    pct: int


def buff(param, direction, pct=30):
    """A buff raising `param` of the neighbouring card on `direction`."""
    return Ability("buff", param, direction, pct)


def debuff(vuln, direction, pct=20):
    """A debuff on the enemy at `direction` that works if it has `vuln`."""
    return Ability("debuff", vuln, direction, pct)


@dataclass(frozen=True)
class Card:
    """One of your cards, with base parameters at full HP."""
    name: str
    F: int  # pylint: disable=invalid-name  # parameter letters as shown in the game
    S: int  # pylint: disable=invalid-name
    M: int  # pylint: disable=invalid-name
    abilities: tuple
    hp: int = 3
    used: bool = False   # already deployed at a key point this stage

    @property
    def base_total(self):
        """F + S + M at full HP."""
        return self.F + self.S + self.M

    def value_at(self, hp):
        """Total base power the card has at `hp`."""
        return self.base_total * HP_PCT[hp] / 100


@dataclass(frozen=True)
class Enemy:
    """One enemy card on the board."""
    power: int
    vulns: tuple
    reward: float = 0
    must_win: bool = False


# ---------------------------------------------------------------- one key point
def _effects(slots, enemies, mset, cond):
    """Buff each of your cards receives and debuff each enemy takes, in hundredths of a
    percent (a 20% debuff at half power is 1000), so scaled abilities stay exact."""
    bonus = [{p: 0 for p in PARAMS} for _ in range(4)]
    debuffed = [0] * 4
    for i, c in enumerate(slots):
        if c is None or "Focused" in mset[i]:          # ASSUMPTION: no outgoing effects
            continue
        # Ability power in %: Teamwork / Specialization add 100 (x2), and the "Buff power" /
        # "Debuff power" conditions add theirs, unless this card is Unstoppable.
        # ASSUMPTION: these add up (Specialization under -50% debuff power gives x1.5).
        own = {} if "Unstoppable" in mset[i] else cond
        b_power = max(0, 100 + 100 * ("Teamwork" in mset[i]) + own.get("B", 0))
        d_power = max(0, 100 + 100 * ("Specialization" in mset[i]) + own.get("D", 0))
        rabid = "Rabid" in mset[i]
        for a in c.abilities:
            if a.kind == "buff":
                j = i + BUFF_OFFSET[a.dir]
                if 0 <= j < 4 and slots[j] is not None and "Focused" not in mset[j]:
                    bonus[j][a.target] += a.pct * b_power
            else:
                j = i + DEBUFF_OFFSET[a.dir]
                if (0 <= j < 4 and enemies[j] is not None
                        and (rabid or a.target in enemies[j].vulns)):
                    debuffed[j] += a.pct * d_power
    return bonus, debuffed


def _card_result(c, e_pow, mods, bonus, cond):
    """Parameters, power, win chance and HP cost of one placed card.
    bonus: buffs received, in hundredths of a percent."""
    hp = min(3, c.hp + 2) if "Fast Repair" in mods else c.hp
    params = {}
    for p in PARAMS:
        # HP loss is a -10% debuff per lost HP, in the same additive pool.
        # Unstoppable ignores battle conditions only, not HP loss.
        pct = (-(100 - HP_PCT[hp])
               + (0 if "Unstoppable" in mods else cond.get(p, 0))
               + sum(PARAM_MODS.get(name, {}).get(p, 0) for name in mods))
        params[p] = max(0, round_half_up(getattr(c, p) * (10000 + 100 * pct + bonus[p]),
                                         10000))
    power = sum(params.values())
    return {"card": c, "params": params, "P": power, "E": e_pow,
            "p": 0.0 if e_pow is None else min(21, max(0, power + 11 - e_pow)) / 21,
            "weight": 2 if "Morale" in mods else 1,
            "reward_mult": 1.1 if "Effectiveness" in mods else 1.0,
            "cost": c.value_at(c.hp) - c.value_at(hp - 1),
            "hp_after": hp - 1}


def evaluate(slots, enemies, cond=None, mods=None):
    """Score one arrangement. slots/enemies: length-4 lists, entries may be None.
    cond: battle conditions in %: "F"/"S"/"M" on your cards' parameters, "E" on enemy
          power, "B"/"D" on the power of your cards' buffs/debuffs, e.g. {"F": -24, "D": -50}.
    mods: length-4 list of tuples of modifier names per slot."""
    cond = cond or {}
    mset = [set(m) for m in (mods or [()] * 4)]
    bonus, debuffed = _effects(slots, enemies, mset, cond)
    out = []
    for i, (c, enemy) in enumerate(zip(slots, enemies)):
        e_pow = None if enemy is None else round_half_up(
            enemy.power * max(0, 10000 - debuffed[i] + 100 * cond.get("E", 0)), 10000)
        if c is None:
            out.append({"card": None, "params": None, "P": None, "E": e_pow, "p": 0.0,
                        "weight": 1, "reward_mult": 1.0, "cost": 0.0})
        else:
            out.append(_card_result(c, e_pow, mset[i], bonus[i], cond))
    return out


def summarize(slot_results, enemies, mods=None, min_wins=0):
    """Goal chance, expected reward and wins, modifier spend and HP cost of one key point.
    The win distribution is a DP over goal credits; a must-win slot keeps only its win branch."""
    dist = {0: 1.0}
    exp_reward = exp_wins = 0.0
    for r, enemy in zip(slot_results, enemies):
        if enemy is None:
            continue
        p = r["p"]
        exp_reward += p * enemy.reward * r["reward_mult"]
        exp_wins += p
        new = Counter()
        for k, q in dist.items():
            new[k + r["weight"]] += q * p
            if not enemy.must_win:
                new[k] += q * (1 - p)
        dist = new
    spend = sum(MODIFIERS[n][1] for m in (mods or ()) for n in m)
    return {"p_goal": sum(q for k, q in dist.items() if k >= min_wins),
            "exp_reward": exp_reward, "spend": spend, "net_reward": exp_reward - spend,
            "exp_wins": exp_wins, "cost": sum(r["cost"] for r in slot_results),
            "dist": dict(dist)}


def rank_key(s):
    """Sort key for a summary: goal chance, net reward, wins, then lowest HP cost."""
    return (round(s["p_goal"], 9), round(s["net_reward"], 9),
            round(s["exp_wins"], 9), -round(s["cost"], 9))


def arrangements(avail, enemies):
    """Every placement of 0-4 cards from `avail`; no card faces an empty slot.
    Yields (cards, slots)."""
    open_slots = [i for i in range(4) if enemies[i] is not None]
    for k in range(len(open_slots) + 1):
        for positions in combinations(open_slots, k):
            for cards in permutations(avail, k):
                slots = [None] * 4
                for pos, c in zip(positions, cards):
                    slots[pos] = c
                yield cards, slots


class TopK:
    """Keeps the `size` highest-ranked payloads pushed so far."""

    def __init__(self, size):
        self.size, self.heap, self.count = size, [], 0

    def push(self, key, payload):
        """Offer a payload ranked by `key`; ties go to the later one."""
        self.count += 1
        item = (key, self.count, payload)
        if len(self.heap) < self.size:
            heapq.heappush(self.heap, item)
        elif key > self.heap[0][0]:
            heapq.heapreplace(self.heap, item)

    def best(self, n):
        """The `n` best payloads, best first."""
        return [payload for _, _, payload in sorted(self.heap, reverse=True)[:n]]


# ---------------------------------------------------------------- stage (several key points)
@dataclass
class KeyPoint:
    """One key point of the stage and its environment."""
    name: str
    enemies: list
    cond: dict
    min_wins: int = 0   # wins that must come from this key point
    barred: frozenset = frozenset()   # card names not allowed here
    no_mods: frozenset = frozenset()  # modifier slot types blocked here ("grey"/"green")


@dataclass(frozen=True)
class SearchLimits:
    """How much of the search space solve_stage explores."""
    max_mods: int = 2             # modifiers on the whole stage
    keep: int = 40                # best card splits that get the modifier search
    top: int = 3                  # plans returned
    max_combos: int = 400 ** 3    # trim the per-key-point pools above this many splits


def best_by_subset(avail, enemies, cond):
    """For every set of cards (0-4) that could go to this key point, the best arrangement
    of that set, without modifiers."""
    best = {}
    for cards, slots in arrangements(avail, enemies):
        res = evaluate(slots, enemies, cond)
        sm = summarize(res, enemies)
        key = rank_key(sm)
        sub = frozenset(c.name for c in cards)
        if sub not in best or key > best[sub][0]:
            best[sub] = (key, slots, res, sm)
    pool = [(sub, slots, [()] * 4, res, sm) for sub, (_, slots, res, sm) in best.items()]
    pool.sort(key=lambda e: rank_key(e[4]), reverse=True)
    return pool


def stage_summary(parts, min_wins, kp_mins=None):
    """parts: list of per-key-point summaries. Combines win distributions across key points.
    kp_mins: per-key-point minimum wins; outcomes below it count as failing the goal."""
    kp_mins = kp_mins or [0] * len(parts)
    dist = {0: 1.0}
    for sm, need in zip(parts, kp_mins):
        new = Counter()
        for a, p in dist.items():
            for b, q in sm["dist"].items():
                if b >= need:
                    new[a + b] += p * q
        dist = new
    totals = {f: sum(sm[f] for sm in parts) for f in ("exp_reward", "spend", "exp_wins", "cost")}
    return {"p_goal": sum(q for k, q in dist.items() if k >= min_wins),
            "net_reward": totals["exp_reward"] - totals["spend"], **totals}


def _combos(pools, used=frozenset(), idx=0):
    """Every choice of one entry per pool with no card used twice."""
    if idx == len(pools):
        yield []
        return
    for entry in pools[idx]:
        if entry[0] & used:
            continue
        for rest in _combos(pools, used | entry[0], idx + 1):
            yield [entry] + rest


def _trim_pools(pools, max_combos):
    """Cut every pool to its best entries if the joint search would be too large."""
    if prod(len(pl) for pl in pools) <= max_combos:
        return pools
    cap = max(50, int(max_combos ** (1 / len(pools))))
    return [pl[:cap] for pl in pools]


def _place_mods(chosen, n_kp, inventory):
    """Per-key-point, per-slot modifier tuples for `chosen` (key point, slot, name) triples,
    or None if they break the one-grey-one-green rule or exceed the inventory."""
    mods = [[[] for _ in range(4)] for _ in range(n_kp)]
    used = Counter()
    for ki, i, n in chosen:
        if any(MODIFIERS[x][0] == MODIFIERS[n][0] for x in mods[ki][i]):
            return None
        mods[ki][i].append(n)
        used[n] += 1
        if used[n] > inventory[n]:
            return None
    return [[tuple(x) for x in kp] for kp in mods]


def modifier_variants(combo, keypoints, inventory, max_mods):
    """Every way to put 1..max_mods modifiers on the cards of a stage plan, skipping slot
    types a key point blocks. Yields per-key-point lists of per-slot modifier tuples."""
    names = [n for n, q in inventory.items() if q > 0]
    options = [(ki, i, n) for ki, e in enumerate(combo)
               for i, c in enumerate(e[1]) if c is not None for n in names
               if MODIFIERS[n][0] not in keypoints[ki].no_mods]
    for k in range(1, max_mods + 1):
        for chosen in combinations(options, k):
            mods = _place_mods(chosen, len(combo), inventory)
            if mods is not None:
                yield mods


def _with_mods(entry, kp, mods):
    """Re-score one key point's plan entry with modifiers (unchanged if it has none)."""
    if not any(mods):
        return entry
    res = evaluate(entry[1], kp.enemies, kp.cond, mods)
    return (entry[0], entry[1], mods, res, summarize(res, kp.enemies, mods))


def solve_stage(roster, keypoints, min_wins=0, inventory=None, limits=None):
    """Split the available cards across all key points of a stage (each card at most once).
    Returns ([(combo, stage summary)] best first, number of card splits, number of plans)."""
    limits = limits or SearchLimits()
    avail = [c for c in roster if c.hp > 0 and not c.used]
    kp_mins = [kp.min_wins for kp in keypoints]
    pools = _trim_pools([best_by_subset([c for c in avail if c.name not in kp.barred],
                                        kp.enemies, kp.cond)
                         for kp in keypoints], limits.max_combos)

    top = TopK(limits.keep)
    for combo in _combos(pools):
        st = stage_summary([e[4] for e in combo], min_wins, kp_mins)
        top.push(rank_key(st), (combo, st))
    n_plans = top.count

    # Phase 2: modifiers (shared inventory, at most max_mods across the whole stage)
    if inventory and limits.max_mods > 0:
        for _, _, (combo, _) in list(top.heap):
            for mods in modifier_variants(combo, keypoints, inventory, limits.max_mods):
                new_combo = [_with_mods(e, kp, m) for e, kp, m in zip(combo, keypoints, mods)]
                st = stage_summary([e[4] for e in new_combo], min_wins, kp_mins)
                top.push(rank_key(st), (new_combo, st))
    return top.best(limits.top), n_plans, top.count


# ---------------------------------------------------------------- output
def _kp_header(kp):
    """One line describing a key point's settings."""
    parts = [f" Key point {kp.name}"]
    if kp.cond:
        parts.append(f"conditions {kp.cond}")
    if kp.min_wins:
        parts.append(f"(needs {kp.min_wins} wins here)")
    if kp.barred:
        parts.append(f"barred: {', '.join(sorted(kp.barred))}")
    if kp.no_mods >= {"grey", "green"}:
        parts.append("no modifiers")
    elif kp.no_mods:
        parts.append(f"no {next(iter(kp.no_mods))} modifiers")
    return "  ".join(parts)


def _print_order(keypoints, combo):
    """Suggest which key point to play first: the one least likely to be swept."""
    sure = [(prod(r["p"] for r in e[3] if r["card"] is not None), kp.name)
            for kp, e in zip(keypoints, combo)]
    if all(p >= 1.0 for p, _ in sure):
        print(" ORDER: every placed battle is a certain win, so any order works.")
    else:
        order = " -> ".join(name for _, name in sorted(sure))
        print(f" ORDER: {order}  (riskiest first; re-run for the rest after seeing its result)")


def show(mods, res, s, title=""):
    """Print one key point's arrangement and its summary."""
    if title:
        print(title)
    for i, r in enumerate(res):
        if r["card"] is None:
            vs = f"  vs E={r['E']}" if r["E"] is not None else "  (no enemy)"
            print(f"  slot {i+1}: (empty){vs}")
            continue
        pr = r["params"]
        m = f"  [{', '.join(mods[i])}]" if mods[i] else ""
        print(f"  slot {i+1}: {r['card'].name:<9} F{pr['F']:>3} S{pr['S']:>3} M{pr['M']:>3}"
              f"  P={r['P']:>3} ({r['P']-10}-{r['P']+10})  vs E={r['E']:>3}"
              f"  win {r['p']*100:5.1f}%{m}")
    print(f"  P(goal)={s['p_goal']*100:.1f}%  E[reward]={s['exp_reward']:.2f}"
          f"  modifier spend={s['spend']}  E[wins]={s['exp_wins']:.2f}  cost={s['cost']:.1f}\n")


def show_stage(keypoints, combo, st, min_wins, title=""):
    """Print one stage plan, key point by key point."""
    if title:
        print(title)
    for kp, (_, _, mods, res, sm) in zip(keypoints, combo):
        print(_kp_header(kp))
        show(mods, res, sm)
    print(f" STAGE: P(goal {min_wins} wins)={st['p_goal']*100:.1f}%"
          f"  E[reward]={st['exp_reward']:.2f}  modifier spend={st['spend']}"
          f"  E[wins]={st['exp_wins']:.2f}  cost={st['cost']:.1f}")
    if len(keypoints) > 1:
        _print_order(keypoints, combo)
    print()


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
    maneuverability, [hp, used,] then ability1_* / ability2_* (type, target, direction, pct).
    Blank ability = none. Raises ValueError naming the bad row."""
    cards = []
    for line, row in enumerate(rows, start=2):
        try:
            if not row.get("name"):
                raise ValueError("name is missing")
            cards.append(Card(row["name"], int(row["firepower"]), int(row["survivability"]),
                              int(row["maneuverability"]),
                              tuple(a for a in (_ability(row, 1), _ability(row, 2)) if a),
                              hp=int(row.get("hp") or 3), used=_yes(row.get("used", ""))))
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
    """Read board.csv: [keypoint,] slot, power, vuln1, vuln2, reward, must_win.
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
        board[slot - 1] = Enemy(int(row["power"]), vulns,
                                float(row.get("reward") or 0),
                                _yes(row.get("must_win", "")))
    return boards


def load_environment(path):
    """Read environment.csv: setting, [keypoint,] value.
    'Wins needed' with a blank keypoint is the stage goal; with a keypoint it is a minimum
    that must also be met at that key point. A condition with a blank keypoint applies to
    every key point; one with a keypoint applies only there (added on top).
    'Barred' rows name a card (in the value column) that cannot go to that key point.
    'No modifiers' / 'No grey modifiers' / 'No green modifiers' (value yes) block modifier
    slots at that key point.
    Returns (stage wins needed, {keypoint or "*": cond dict}, {keypoint: wins needed},
             {keypoint or "*": set of barred card names},
             {keypoint or "*": set of blocked slot types})."""
    conds, min_wins, kp_mins, barred, nomods = {}, 0, {}, {}, {}
    for row in _rows(path):
        key, val = row["setting"].lower(), row.get("value") or "0"
        kp = row.get("keypoint") or "*"
        if key == "barred":
            barred.setdefault(kp, set()).add(row["value"])
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
    return min_wins, conds, kp_mins, barred, nomods


def cond_for(conds, kp):
    """Battle conditions at key point `kp`: stage-wide ones plus its own."""
    out = dict(conds.get("*", {}))
    for k, v in conds.get(kp, {}).items():
        out[k] = out.get(k, 0) + v
    return out


def load_stage(board_path, env_path, roster):
    """Key points of the stage from the board and environment files, checked against the
    roster. Returns (keypoints, stage wins needed)."""
    boards = load_board(board_path)
    min_wins, conds, kp_mins, barred, nomods = (
        load_environment(env_path) if os.path.exists(env_path) else (0, {}, {}, {}, {}))
    for k in [*kp_mins, *barred, *conds, *nomods]:
        assert k == "*" or k in boards, \
            f"environment names key point {k!r}, which is not in the board"
    names = {c.name for c in roster}
    for k, cards in barred.items():
        for c in cards:
            assert c in names, f"barred card {c!r} at key point {k} is not in the roster"
    keypoints = [KeyPoint(name, enemies, cond_for(conds, name), kp_mins.get(name, 0),
                          frozenset(barred.get("*", set()) | barred.get(name, set())),
                          frozenset(nomods.get("*", set()) | nomods.get(name, set())))
                 for name, enemies in boards.items()]
    return keypoints, min_wins


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
    Enemy(95, (SMOKE, QUICK), 6),
    Enemy(99, (LARGE, QUICK), 6),
    Enemy(105, (AA, SMOKE), 7),
    Enemy(104, (QUICK, AIR), 6),
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
    # HP loss is not a battle condition: Unstoppable drops the -24% F condition, keeps -10% HP
    res = evaluate([None, None, None, fjord2], EXAMPLE_KEYPOINT, {"F": -24},
                   [(), (), (), ("Unstoppable",)])
    assert res[3]["params"] == {"F": 27, "S": 18, "M": 45}
    print("Validation: all in-game observations reproduced.\n")


def _load_if_exists(path, loader, default):
    return loader(path) if os.path.exists(path) else default


def main(argv):
    """Solve the stage in board.csv / environment.csv (or the files named in argv).
    roster.csv and modifiers.csv are read from the current directory."""
    validate()
    board_path = argv[1] if len(argv) > 1 else "board.csv"
    env_path = argv[2] if len(argv) > 2 else "environment.csv"
    roster = _load_if_exists("roster.csv", load_roster, list(EXAMPLE_ROSTER))
    inventory = _load_if_exists("modifiers.csv", load_modifiers, {})
    keypoints, min_wins = load_stage(board_path, env_path, roster)

    print(f"Board: {board_path}   Key points: {', '.join(kp.name for kp in keypoints)}")
    extra = "".join(f", {kp.min_wins} at key point {kp.name}" for kp in keypoints if kp.min_wins)
    print(f"Stage wins needed: {min_wins}{extra}")
    print(f"Cards: {len([c for c in roster if c.hp > 0 and not c.used])} usable   "
          f"Modifiers: {', '.join(f'{k} x{v}' for k, v in inventory.items() if v) or 'none'}\n")

    best, n_plans, n_tot = solve_stage(roster, keypoints, min_wins=min_wins, inventory=inventory)
    print(f"Compared {n_plans:,} card splits, {n_tot:,} incl. modifier variants\n")
    for rank, (combo, st) in enumerate(best, 1):
        show_stage(keypoints, combo, st, min_wins, title=f"===== Plan #{rank} =====")


if __name__ == "__main__":
    main(sys.argv)
