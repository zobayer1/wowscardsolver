"""
Battle model of the 4-vs-4 card game: one key point scored for a given placement of cards.

  param  = round_half_up(base * HP% * (100 + buffs + modifiers + conditions)%)
           per parameter, rounded once; HP% is 100 / 90 / 80 at 3 / 2 / 1 HP. Confirmed in
           game: Pulse (45/35/35) at 2 HP with +30% F and +30% S showed 116-136, i.e.
           53 + 41 + 32 = 126.
  P      = F + S + M, roll range [P-10, P+10] (21 integer values)
  E      = round_half_up(enemy_base * (100 - sum of matching debuffs + enemy boosts)%),
           boosts being the enemy's own boost and any "Enemy power" condition
  win    = clamp(P + 11 - E, 0, 21) / 21      (ties go to you)

Assumptions not yet confirmed in-game (marked ASSUMPTION below):
  - Focused: the card neither gives nor receives buffs, and its debuffs are off.
  - Unstoppable: battle conditions on the card's parameters and on the power of its own
    buffs/debuffs are ignored. It does not lift card bans (Barred), modifier bans or HP loss.
  - "Buff power" / "Debuff power" conditions scale every buff/debuff a card gives, adding to
    Teamwork / Specialization (+100%): 20% debuff at -50% -> 10%, with Specialization -> 30%.
  - An "Enemy power" condition adds to the same % pool as debuffs (e.g. +10% and one
    -20% debuff -> x0.90), and Unstoppable does not cancel it.
  - Fast Repair: +2 HP (cap 3) before the battle, then the battle costs 1 HP as usual.
  - Each modifier use is charged at its point value (MODIFIERS): a modifier is only
    recommended when it raises P(goal) or the expected wins.
"""

from collections import Counter
from dataclasses import dataclass

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
    "Effectiveness":   ("grey", 18),    # rewards only: never helps the ranking
    "Morale":          ("grey", 12),
    "Focused":         ("green", 15),
    "Specialization":  ("green", 15),
    "Rabid":           ("green", 15),
    "Fast Repair":     ("green", 15),
    "Unstoppable":     ("green", 15),
}


# Modifiers that cannot change goal chance, wins or HP cost; the search skips them.
NO_EFFECT_MODS = frozenset({"Effectiveness"})
PARAM_MODS = {"Gunnery": {"F": 20}, "Damage Control": {"S": 20}, "Dodge": {"M": 20},
              "Focused": {"F": 40, "S": 40, "M": 40}}


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
class Card:  # pylint: disable=too-many-instance-attributes  # a plain record
    """One of your cards, with base parameters at full HP."""
    name: str
    F: int  # pylint: disable=invalid-name  # parameter letters as shown in the game
    S: int  # pylint: disable=invalid-name
    M: int  # pylint: disable=invalid-name
    abilities: tuple
    hp: int = 3
    used: bool = False   # already deployed at a key point this stage
    type: str | None = None   # your own grouping, used to bar cards by type; None = unknown

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
    must_win: bool = False
    # This enemy's own power boost in %, e.g. 16. It adds to the same pool as your debuffs
    # (confirmed in game: base 108, +16% and a 20% debuff -> 108 x 96% = 104), so enter the
    # base power, not the boosted one.
    boost: int = 0

    def power_after(self, debuffs, cond_pct=0):
        """Power in battle, given the debuffs it takes (hundredths of a percent) and an
        enemy power condition (%)."""
        return round_half_up(
            self.power * max(0, 10000 - debuffs + 100 * (cond_pct + self.boost)), 10000)


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
        # HP scales the whole parameter, buffs included. Unstoppable ignores battle
        # conditions only, not HP loss.
        pct = ((0 if "Unstoppable" in mods else cond.get(p, 0))
               + sum(PARAM_MODS.get(name, {}).get(p, 0) for name in mods))
        params[p] = max(0, round_half_up(
            getattr(c, p) * HP_PCT[hp] * (10000 + 100 * pct + bonus[p]), 1000000))
    power = sum(params.values())
    return {"card": c, "params": params, "P": power, "E": e_pow,
            "p": 0.0 if e_pow is None else min(21, max(0, power + 11 - e_pow)) / 21,
            "weight": 2 if "Morale" in mods else 1,
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
        e_pow = None if enemy is None else enemy.power_after(debuffed[i], cond.get("E", 0))
        if c is None:
            out.append({"card": None, "params": None, "P": None, "E": e_pow, "p": 0.0,
                        "weight": 1, "cost": 0.0})
        else:
            out.append(_card_result(c, e_pow, mset[i], bonus[i], cond))
    return out


def summarize(slot_results, enemies, mods=None, min_wins=0):
    """Goal chance, expected wins, modifier spend and HP cost of one key point.
    The win distribution is a DP over goal credits; a must-win slot keeps only its win branch."""
    dist = {0: 1.0}
    exp_wins = 0.0
    for r, enemy in zip(slot_results, enemies):
        if enemy is None:
            continue
        p = r["p"]
        exp_wins += p
        new = Counter()
        for k, q in dist.items():
            new[k + r["weight"]] += q * p
            if not enemy.must_win:
                new[k] += q * (1 - p)
        dist = new
    spend = sum(MODIFIERS[n][1] for m in (mods or ()) for n in m)
    return {"p_goal": sum(q for k, q in dist.items() if k >= min_wins),
            "spend": spend, "exp_wins": exp_wins, "cost": sum(r["cost"] for r in slot_results),
            "dist": dict(dist)}


def rank_key(s):
    """Sort key for a summary: goal chance, expected wins, then the fewest modifier points
    spent, then the lowest HP cost."""
    return (round(s["p_goal"], 9), round(s["exp_wins"], 9),
            -round(s["spend"], 9), -round(s["cost"], 9))


# ---------------------------------------------------------------- a whole stage
@dataclass
class KeyPoint:
    """One key point of the stage and its environment."""
    name: str
    enemies: list
    cond: dict
    min_wins: int = 0   # wins that must come from this key point
    barred: frozenset = frozenset()   # card names not allowed here
    no_mods: frozenset = frozenset()  # modifier slot types blocked here ("grey"/"green")


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
    totals = {f: sum(sm[f] for sm in parts) for f in ("spend", "exp_wins", "cost")}
    return {"p_goal": sum(q for k, q in dist.items() if k >= min_wins), **totals}
