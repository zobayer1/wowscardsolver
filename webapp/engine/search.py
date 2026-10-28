"""
Stage search: which cards go to which key point, and with which modifiers.

Plans are ranked by (agreed):
  1. P(stage goal met)  -- all must-win enemies beaten AND at least `min_wins` goal credits
  2. expected number of wins
  3. fewest modifier points spent
  4. lowest card cost (power lost from the HP each used card spends)

solve_stage:
  Phase 1  for every key point, every card arrangement (empty slots allowed) without
           modifiers, keeping the best arrangement of each card set; then the best splits of
           the cards across key points (each card used at most once), found by branch and
           bound.
  Phase 2  takes the top `keep` splits and tries every modifier combination with at most
           `max_mods` modifiers on the whole stage, limited by the inventory.

The results are the same as scoring every arrangement, split and variant with model.evaluate
and model.stage_summary (tests/test_search.py checks this); the search just avoids most of
that work. See docs/SOLVER.md.
"""

import heapq
import time
from collections import Counter
from dataclasses import dataclass
from itertools import combinations, permutations, product
from math import prod

from .model import (BUFF_OFFSET, DEBUFF_OFFSET, HP_PCT, MODIFIERS, NO_EFFECT_MODS, PARAMS,
                    evaluate,
                    round_half_up, stage_summary, summarize)


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


@dataclass(frozen=True)
class SearchLimits:
    """How much of the search space solve_stage explores."""
    max_mods: int = 2             # modifiers on the whole stage
    keep: int = 40                # best card splits that get the modifier search
    top: int = 3                  # plans returned
    max_combos: int = 400 ** 3    # trim the per-key-point pools above this many splits
    # Seconds. When set, the trim is widened (2x, 4x, ... until nothing is trimmed) while
    # the solve stays within about this time; None keeps the trim fixed (reproducible).
    time_budget: float | None = None
    # When above 0, plans after the first must each change at least this many card
    # assignments (which key point a card goes to, or none) from every earlier plan; each
    # such plan costs one more search. 0 returns the top plans as ranked.
    distinct: int = 0
    # When True (the web app), max_mods does not apply: each card may take one grey and one
    # green modifier, and modifiers are only used where they make something certain (see
    # _certain_mods). Otherwise up to max_mods per stage, whenever they raise the rank key.
    mods_for_certainty: bool = False


class _OutOfTime(Exception):
    """The search passed its deadline."""


# ---------------------------------------------------------------- one key point
class _SlotModel:  # pylint: disable=too-few-public-methods
    """Win chances of single cards at one key point, without modifiers.

    A card's chance in slot i depends only on itself and on what its neighbours do to slot i
    (buffs to the card, debuffs to the enemy). Neighbours are reduced to that effect, so
    the chance is computed once per (slot, left effect, card, right effect) and reused."""

    def __init__(self, avail, enemies, cond):
        self.avail, self.enemies, self.cond = avail, enemies, cond
        b_power = max(0, 100 + cond.get("B", 0))
        d_power = max(0, 100 + cond.get("D", 0))

        def effect(c, j, i):
            """(buff to F, S, M, debuff) that card c in slot j gives slot i, in hundredths."""
            bonus, deb = [0, 0, 0], 0
            for a in c.abilities:
                if a.kind == "buff":
                    if j != i and j + BUFF_OFFSET[a.dir] == i:
                        bonus[PARAMS.index(a.target)] += a.pct * b_power
                elif (j + DEBUFF_OFFSET[a.dir] == i and enemies[i] is not None
                      and a.target in enemies[i].vulns):
                    deb += a.pct * d_power
            return (*bonus, deb)

        none = (0, 0, 0, 0)
        # from_left[i][c]: effect of card c in slot i-1 on slot i (index -1: no card);
        # from_right likewise for slot i+1; own[i][c]: card c's own debuff on enemy i.
        self.from_left = [[effect(c, i - 1, i) for c in avail] + [none] for i in range(4)]
        self.from_right = [[effect(c, i + 1, i) for c in avail] + [none] for i in range(4)]
        self.own = [[effect(c, i, i)[3] for c in avail] for i in range(4)]
        self.cache = {}

    def winning_rolls(self, i, le, card, re):
        """Of the 21 possible rolls, how many win for avail[card] in slot i, given the
        effects le / re of its left and right neighbours (from_left / from_right)."""
        c, cond = self.avail[card], self.cond
        deb = le[3] + self.own[i][card] + re[3]
        e_pow = self.enemies[i].power_after(deb, cond.get("E", 0))
        power = 0
        for k, p in enumerate(PARAMS):
            power += max(0, round_half_up(
                getattr(c, p) * HP_PCT[c.hp] * (10000 + 100 * cond.get(p, 0) + le[k] + re[k]),
                1000000))
        return min(21, max(0, power + 11 - e_pow))


def _subset_keys(avail, enemies, cond):  # pylint: disable=too-many-locals  # hot loop: state kept in locals for speed
    """For every set of cards (0-4) that could go to this key point, a sort key and the card
    indices per slot (-1 = empty) of its best arrangement, without modifiers; best first.

    Ranks like rank_key(summarize(evaluate(...))) over every arrangement, but in exact
    integers: a win chance is always n/21, so the key is (product of n over must-win slots,
    sum of n, minus HP cost in hundredths of power)."""
    model = _SlotModel(avail, enemies, cond)
    from_left, from_right, cache = model.from_left, model.from_right, model.cache
    open_slots = [i for i in range(4) if enemies[i] is not None]
    must = [e is not None and e.must_win for e in enemies]
    cost = [c.base_total * (HP_PCT[c.hp] - HP_PCT[c.hp - 1]) for c in avail]
    best = {}
    for k in range(len(open_slots) + 1):  # pylint: disable=too-many-nested-blocks
        for positions in combinations(open_slots, k):
            for cards in permutations(range(len(avail)), k):
                at = [-1, -1, -1, -1]
                for pos, ci in zip(positions, cards):
                    at[pos] = ci
                goal, wins, cst = 1, 0, 0
                for i in open_slots:
                    ci = at[i]
                    if ci < 0:
                        if must[i]:
                            goal = 0            # an unopposed must-win enemy is a loss
                        continue
                    le = from_left[i][at[i - 1] if i else -1]
                    re = from_right[i][at[i + 1] if i < 3 else -1]
                    n = cache.get((i, le, ci, re))
                    if n is None:
                        n = cache[(i, le, ci, re)] = model.winning_rolls(i, le, ci, re)
                    if must[i]:
                        goal *= n
                    wins += n
                    cst += cost[ci]
                key = (goal, wins, -cst)
                sub = frozenset(cards)
                old = best.get(sub)
                if old is None or key > old[0]:
                    best[sub] = (key, at)
    keyed = list(best.values())
    keyed.sort(key=lambda e: e[0], reverse=True)
    return keyed


def _entry(avail, enemies, cond, at):
    """A pool entry (card names, slots, modifiers, slot results, summary) for an arrangement
    given as card indices per slot."""
    slots = [avail[ci] if ci >= 0 else None for ci in at]
    res = evaluate(slots, enemies, cond)
    return (frozenset(c.name for c in slots if c), slots, [()] * 4, res,
            summarize(res, enemies))


def best_by_subset(avail, enemies, cond):
    """For every set of cards (0-4) that could go to this key point, the best arrangement
    of that set, without modifiers; best first."""
    return [_entry(avail, enemies, cond, at) for _, at in _subset_keys(avail, enemies, cond)]


def _trim_pools(pools, max_combos):
    """Cut every pool to its best entries if the joint search would be too large."""
    cap = _caps([len(pl) for pl in pools], max_combos, widen=False)[0]
    return [pl[:cap] for pl in pools]


def _caps(sizes, max_combos, widen):
    """Entries kept per pool in each search round: the first fits max_combos splits (at
    least 50 per pool, or all of them if they fit); when widening, it then doubles until
    nothing is trimmed."""
    largest = max(sizes, default=0)
    if prod(sizes) <= max_combos:
        return [largest]
    root = round(max_combos ** (1 / len(sizes)))       # exact integer root: 400**3 -> 400
    while root ** len(sizes) > max_combos:
        root -= 1
    while (root + 1) ** len(sizes) <= max_combos:
        root += 1
    caps = [max(50, root)]
    while widen and caps[-1] < largest:
        caps.append(min(2 * caps[-1], largest))
    return caps


# ---------------------------------------------------------------- combining key points
def _wins_list(dist, need):
    """A win distribution as a list indexed by wins, with outcomes below `need` dropped."""
    out = [0.0] * (max(dist, default=0) + 1)
    for b, q in dist.items():
        if b >= need:
            out[b] += q
    return out


def _convolve(a, b):
    out = [0.0] * (len(a) + len(b) - 1)
    for i, p in enumerate(a):
        if p:
            for j, q in enumerate(b):
                out[i + j] += p * q
    return out


def _tail(dist):
    """tail[k] = chance of at least k wins."""
    tail, acc = [0.0] * len(dist), 0.0
    for k in range(len(dist) - 1, -1, -1):
        acc += dist[k]
        tail[k] = acc
    return tail


def _chance_of(dist, tail, goal):
    """Chance that wins from `dist` plus wins from the distribution behind `tail` reach
    `goal`."""
    total = 0.0
    for a, q in enumerate(dist):
        if q:
            k = goal - a
            total += q * (tail[0] if k <= 0 else tail[k] if k < len(tail) else 0.0)
    return total


def _cannot_beat(bound, threshold):
    """True if no key at or below `bound` (componentwise) can rank above `threshold`."""
    return bound <= threshold


_CHECK = 32   # how often a scan of a key point's options checks whether it can stop


class _Level:  # pylint: disable=too-few-public-methods,too-many-instance-attributes  # a record
    """One key point's options, prepared for the joint search: per option its card mask,
    win distribution (minimum applied), modifier spend, expected wins and HP cost."""

    def __init__(self, pool, need, bit):
        self.items = []
        for e in pool:
            sm = e[4]
            mask = _mask(e[0], bit)
            dist = _wins_list(sm["dist"], need)
            self.items.append((mask, dist, _tail(dist), sm["spend"], sm["exp_wins"],
                               sm["cost"], e))
        width = max((len(it[1]) for it in self.items), default=1)
        # The best any option can do, for the bounds: the pointwise largest chance of at
        # least k wins (a distribution that stochastically dominates every option), the
        # largest expected wins, and the smallest modifier spend and HP cost.
        self.top_tail = [max((it[2][k] if k < len(it[2]) else 0.0) for it in self.items)
                         for k in range(width)] if self.items else [0.0]
        self.low_spend = min((it[3] for it in self.items), default=0.0)
        self.top_wins = max((it[4] for it in self.items), default=0.0)
        self.low_cost = min((it[5] for it in self.items), default=0.0)
        # The same among the options with the most expected wins: a split that only ties on
        # wins must use one of those here (wins move in steps of 1/21, far above rounding).
        top = [it for it in self.items if round(it[4], 9) == round(self.top_wins, 9)]
        self.low_spend_at_top = min((it[3] for it in top), default=0.0)
        self.low_cost_at_top = min((it[5] for it in top), default=0.0)
        self.suffixes = self._suffixes(width)

    def _suffixes(self, width):
        """The same bounds for the options from position j to the end, recorded at every
        _CHECK-th j: (top tail, top wins, low spend, low cost, low spend and low cost among
        the top-wins options)."""
        out = {}
        tail, wins = [0.0] * width, -1.0
        spend = cost = spend_top = cost_top = float("inf")
        for j in range(len(self.items) - 1, -1, -1):
            _, _, t, s, w, c, _ = self.items[j]
            tail = [max(a, t[k] if k < len(t) else 0.0) for k, a in enumerate(tail)]
            spend, cost = min(spend, s), min(cost, c)
            if round(w, 9) > round(wins, 9):
                wins, spend_top, cost_top = w, s, c
            elif round(w, 9) == round(wins, 9):
                spend_top, cost_top = min(spend_top, s), min(cost_top, c)
            if not j % _CHECK:
                out[j] = (tail, wins, spend, cost, spend_top, cost_top)
        return out


def _best_splits(pools, min_wins, kp_mins, keep, deadline=None, avoid=(), min_change=0):  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    """The `keep` best ways to choose one entry per pool without using a card twice, ranked
    like rank_key(stage_summary(...)). Branch and bound: once `keep` splits are held, a
    partial choice is dropped when even the best completion it could have (from what the
    remaining pools can offer at most) cannot rank above the weakest split held.
    Raises _OutOfTime once time.perf_counter() passes `deadline`.
    avoid: plans (per key point, the set of card names) a split must differ from by at
    least `min_change` card assignments; closer splits are skipped.
    Returns (TopK of entry lists, number of complete splits scored)."""
    bit = {}
    levels = [_Level(pool, need, bit) for pool, need in zip(pools, kp_mins)]
    avoid = [[_mask(names, bit) for names in plan] for plan in avoid]
    n = len(levels)
    # Suffix bounds: what levels L.. can add at most, combined.
    s_tail, s_spend, s_wins, s_cost = [None] * (n + 1), [0.0] * (n + 1), [0.0] * (n + 1), \
        [0.0] * (n + 1)
    s_spend_top, s_cost_top = [0.0] * (n + 1), [0.0] * (n + 1)
    s_dists = [None] * n + [[1.0]]
    s_tail[n] = [1.0]
    for lv in range(n - 1, -1, -1):
        s_dist = s_dists[lv] = _convolve(_from_tail(levels[lv].top_tail), s_dists[lv + 1])
        s_tail[lv] = _tail(s_dist)
        s_spend[lv] = levels[lv].low_spend + s_spend[lv + 1]
        s_wins[lv] = levels[lv].top_wins + s_wins[lv + 1]
        s_cost[lv] = levels[lv].low_cost + s_cost[lv + 1]
        s_spend_top[lv] = levels[lv].low_spend_at_top + s_spend_top[lv + 1]
        s_cost_top[lv] = levels[lv].low_cost_at_top + s_cost_top[lv + 1]
    # Stopping points: at option j of level lv, the best that options j.. there, together
    # with everything at the deeper levels, could add.
    stops = [{j: (_tail(_convolve(_from_tail(t), s_dists[lv + 1])), w + s_wins[lv + 1],
                  s + s_spend[lv + 1], c + s_cost[lv + 1], st + s_spend_top[lv + 1],
                  ct + s_cost_top[lv + 1])
              for j, (t, w, s, c, st, ct) in levels[lv].suffixes.items()}
             for lv in range(n)]

    best = TopK(keep)
    scored = steps = 0
    heap = best.heap

    def search(lv, used, dist, spend, wins, cost, chosen):  # chosen: [(mask, entry)]  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
        nonlocal scored, steps
        last = lv == n - 1
        for j, (mask, d, tail, s, w, c, e) in enumerate(levels[lv].items):
            if j in stops[lv] and len(heap) >= keep and _cannot_beat(
                    _bound(dist, spend, wins, cost, stops[lv][j], min_wins, heap[0][0]),
                    heap[0][0]):
                break            # no option from here on can beat the weakest split held
            if mask & used:
                continue
            steps += 1
            if deadline is not None and not steps & 0xFFF and time.perf_counter() > deadline:
                raise _OutOfTime
            ns, nw, nc = spend + s, wins + w, cost + c
            if last:
                if avoid and _too_close([m for m, _ in chosen] + [mask], avoid, min_change):
                    continue
                scored += 1
                key = (round(_chance_of(dist, tail, min_wins), 9), round(nw, 9),
                       -round(ns, 9), -round(nc, 9))
                if len(heap) < keep or key > heap[0][0]:
                    best.push(key, [x for _, x in chosen] + [e])
                continue
            nd = _convolve(dist, d)
            if len(heap) >= keep and _cannot_beat(
                    _bound(nd, ns, nw, nc, (s_tail[lv + 1], s_wins[lv + 1], s_spend[lv + 1],
                                            s_cost[lv + 1], s_spend_top[lv + 1],
                                            s_cost_top[lv + 1]), min_wins, heap[0][0]),
                    heap[0][0]):
                continue
            search(lv + 1, used | mask, nd, ns, nw, nc, chosen + [(mask, e)])

    if n:
        search(0, 0, [1.0], 0.0, 0.0, 0.0, [])
    return best, scored


def _from_tail(tail):
    """The distribution whose chance of at least k wins is tail[k]."""
    return [t - (tail[k + 1] if k + 1 < len(tail) else 0.0) for k, t in enumerate(tail)]


def _bound(dist, spend, wins, cost, rest, min_wins, weakest):  # pylint: disable=too-many-arguments,too-many-positional-arguments
    """Upper bound on the rank key of any completion of a partial split (its win
    distribution, spend, wins and cost so far), given `rest` = (tail, wins, spend, cost,
    spend and cost among top-wins options) of what the remaining choices can add. Only a
    tie with `weakest` on goal chance and wins can still win on spend and cost, and such a
    tie needs a most-wins option at every remaining choice."""
    tail, more_wins, low_spend, low_cost, spend_top, cost_top = rest
    # The 1e-12 margins cover float error (~1e-14) but stay below the 9-decimal rounding, so
    # an exact tie is still seen as a tie.
    top = (round(_chance_of(dist, tail, min_wins) + 1e-12, 9),
           round(wins + more_wins + 1e-12, 9))
    tie = top == weakest[:2]
    return (*top, -round(spend + (spend_top if tie else low_spend) - 1e-12, 9),
            -round(cost + (cost_top if tie else low_cost) - 1e-12, 9))


def _mask(names, bit):
    """Bit mask of card names, with bits assigned by `bit` ({name: bit})."""
    mask = 0
    for name in names:
        mask |= bit.setdefault(name, 1 << len(bit))
    return mask


def _too_close(masks, avoid, min_change):
    """True if the split (card mask per key point) changes fewer than `min_change` card
    assignments from any plan in `avoid`. A card counts once whether it is added,
    dropped or moved to another key point."""
    for plan in avoid:
        changed = 0
        for mine, theirs in zip(masks, plan):
            changed |= mine ^ theirs
        if changed.bit_count() < min_change:
            return True
    return False


def changed_cards(plan_a, plan_b):
    """Names of the cards assigned differently (another key point, or used in only one)
    by two plans, given as lists of pool entries per key point."""
    where_a = {name: k for k, e in enumerate(plan_a) for name in e[0]}
    where_b = {name: k for k, e in enumerate(plan_b) for name in e[0]}
    return {n for n in where_a.keys() | where_b.keys() if where_a.get(n) != where_b.get(n)}


# ---------------------------------------------------------------- modifiers
def _slot_changes(chosen, inventory):
    """{key point: {slot: [names]}} for `chosen` (key point, slot, name) triples, or None if
    they break the one-grey-one-green rule or exceed the inventory."""
    changes, used = {}, Counter()
    for ki, i, n in chosen:
        slot = changes.setdefault(ki, {}).setdefault(i, [])
        if any(MODIFIERS[x][0] == MODIFIERS[n][0] for x in slot):
            return None
        slot.append(n)
        used[n] += 1
        if used[n] > inventory[n]:
            return None
    return changes


def modifier_variants(combo, keypoints, inventory, max_mods, allowed=None):  # pylint: disable=too-many-locals
    """Every way to put 1..max_mods modifiers on the cards of a stage plan, skipping slot
    types a key point blocks (and, if given, (key point, slot, modifier) choices not in
    `allowed`). Yields {key point index: per-slot modifier tuples} for the key points
    that get modifiers."""
    names = [n for n, q in inventory.items() if q > 0 and n not in NO_EFFECT_MODS]
    options = [(ki, i, n) for ki, e in enumerate(combo)
               for i, c in enumerate(e[1]) if c is not None for n in names
               if MODIFIERS[n][0] not in keypoints[ki].no_mods
               and (allowed is None or (ki, i, n) in allowed)]

    def slots_of(changes):
        return {ki: tuple(tuple(slots.get(i, ())) for i in range(4))
                for ki, slots in changes.items()}

    if max_mods >= 1:
        for ki, i, n in options:       # one modifier: always allowed (its count is above 0)
            yield {ki: tuple((n,) if s == i else () for s in range(4))}
    if max_mods >= 2:                  # two: the common case, checked without building dicts
        for (k1, i1, n1), (k2, i2, n2) in combinations(options, 2):
            if (k1, i1) == (k2, i2) and MODIFIERS[n1][0] == MODIFIERS[n2][0]:
                continue               # one grey and one green per card
            if n1 == n2 and inventory[n1] < 2:
                continue
            if k1 != k2:
                yield slots_of({k1: {i1: [n1]}, k2: {i2: [n2]}})
            elif i1 != i2:
                yield slots_of({k1: {i1: [n1], i2: [n2]}})
            else:
                yield slots_of({k1: {i1: [n1, n2]}})
    for k in range(3, max_mods + 1):
        for chosen in combinations(options, k):
            changes = _slot_changes(chosen, inventory)
            if changes is not None:
                yield slots_of(changes)


def _with_mods(entry, kp, mods):
    """Re-score one key point's plan entry with modifiers (unchanged if it has none)."""
    if not any(mods):
        return entry
    res = evaluate(entry[1], kp.enemies, kp.cond, mods)
    return (entry[0], entry[1], mods, res, summarize(res, kp.enemies, mods))


def _part_stats(sm, need):
    """A key point summary as (win list with the minimum applied, spend, wins, cost), the
    pieces _stage_key combines."""
    return _wins_list(sm["dist"], need), sm["spend"], sm["exp_wins"], sm["cost"]


def _stage_key(stats, min_wins):
    """rank_key of stage_summary(...) for key points given as _part_stats tuples."""
    dist, spend, wins, cost = [1.0], 0, 0, 0
    for wl, s, w, c in stats:
        dist = _convolve(dist, wl)
        spend, wins, cost = spend + s, wins + w, cost + c
    goal = sum(dist[min_wins:]) if min_wins > 0 else sum(dist)
    return (round(goal, 9), round(wins, 9), -round(spend, 9), -round(cost, 9))


def _add_modifiers(top, keypoints, inventory, limits, min_wins, kp_mins, only_best=False,  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
                   deadline=None):
    """Phase 2: try every modifier variant (shared inventory, at most limits.max_mods on the
    whole stage) of the splits held in `top`, pushing the good ones into it. Each key
    point's result with a given set of modifiers is computed once. With a `deadline`, the
    splits are taken best first and the search stops when time.perf_counter() passes it.
    With limits.mods_for_certainty, each key point gets its own modifiers instead (see
    _certain_mods), and each split is pushed once with them.
    Returns (variants scored, splits whose modifier search was completed)."""
    entries, n, done = {}, 0, 0
    names = frozenset(nm for nm, q in inventory.items() if q > 0 and nm not in NO_EFFECT_MODS)
    splits = sorted(top.heap, reverse=True) if deadline is not None else list(top.heap)
    for _, _, combo in splits:
        if deadline is not None and time.perf_counter() > deadline:
            break
        done += 1
        if limits.mods_for_certainty:
            new_combo, stats = [], []
            for ki, (e, kp, need) in enumerate(zip(combo, keypoints, kp_mins)):
                key = (ki, id(e))
                if key not in entries:
                    entries[key] = _certain_mods(e, kp, need, names)
                    n += 1
                new_combo.append(entries[key])
                stats.append(_part_stats(entries[key][4], need))
            key = _stage_key(stats, min_wins)
            if len(top.heap) < limits.keep or key > top.heap[0][0]:
                top.push(key, new_combo)
            continue
        base = [_part_stats(e[4], need) for e, need in zip(combo, kp_mins)]
        allowed = _useful_modifiers(combo, keypoints, base, min_wins) if only_best else None
        if allowed is not None and not allowed:
            continue
        for changes in modifier_variants(combo, keypoints, inventory, limits.max_mods,
                                         allowed):
            new_combo, stats = list(combo), list(base)
            for ki, mods in changes.items():
                key = (ki, id(combo[ki]), mods)
                hit = entries.get(key)
                if hit is None:
                    e = _with_mods(combo[ki], keypoints[ki], mods)
                    hit = entries[key] = (e, _part_stats(e[4], kp_mins[ki]))
                new_combo[ki], stats[ki] = hit
            n += 1
            key = _stage_key(stats, min_wins)
            if len(top.heap) < limits.keep or key > top.heap[0][0]:
                top.push(key, new_combo)
    return n, done


# ---------------------------------------------------------------- modifiers for certainty
_PER_BATTLE = 3      # most modifiers tried together to make one battle certain
_WAYS = 3            # cheapest ways kept per battle


def _mod_spend(assign):
    return sum(MODIFIERS[name][1] for _, name in assign)


def _fits(assign):
    """True if no card gets two modifiers of one kind (one grey and one green per card)."""
    seen = set()
    for i, name in assign:
        if (i, MODIFIERS[name][0]) in seen:
            return False
        seen.add((i, MODIFIERS[name][0]))
    return True


def _certain_mods(entry, kp, need, names):  # pylint: disable=too-many-locals
    """One key point's plan entry with the modifiers that make the most of it certain: every
    battle certain that some modifiers can make certain, then the key point's requirements
    (Morale on certain wins). Each card takes at most one grey and one green, and every
    modifier kept is needed: without it fewer battles, or not the requirements, would be
    certain. Among such choices the best by rank (requirements chance, expected wins,
    spend, cost) is returned; the entry itself if no modifier makes anything certain."""
    cards = entry[1]
    blocked = kp.no_mods
    scored = {}

    def score(assign):
        """(entry, rank key, certainty) of the key point with these (slot, name) pairs."""
        hit = scored.get(assign)
        if hit is None:
            mods = tuple(tuple(name for s, name in sorted(
                assign, key=lambda a: MODIFIERS[a[1]][0] != "grey") if s == i)
                for i in range(4))
            e = _with_mods(entry, kp, mods)
            key = _stage_key([_part_stats(e[4], need)], 0)
            sure = sum(1 for r in e[3] if r["card"] is not None and r["p"] >= 1)
            hit = scored[assign] = (e, key, (key[0] >= 1, sure))
        return hit

    def usable(name):
        return name in names and MODIFIERS[name][0] not in blocked

    base = score(frozenset())
    # The cheapest ways (fewest modifiers, then fewest points) to make each open battle certain.
    ways = []
    for j, r in enumerate(entry[3]):
        if r["card"] is None or r["p"] >= 1:
            continue
        only_j = [s == j for s in range(4)]
        options = [(i, name) for i, c in enumerate(cards) if c is not None
                   for name in _useful_on(c, i, kp, only_j, False) if usable(name)]
        found = []
        for size in range(1, _PER_BATTLE + 1):
            for chosen in combinations(options, size):
                if _fits(chosen) and score(frozenset(chosen))[0][3][j]["p"] >= 1:
                    found.append(frozenset(chosen))
            if found:
                break
        if found:
            ways.append(sorted(found, key=_mod_spend)[:_WAYS])

    # Every mix of those ways (or none) per battle, trimmed to the modifiers it needs.
    candidates = {frozenset()}
    for pick in product(*[[frozenset()] + w for w in ways]):
        union = frozenset().union(*pick)
        if union and _fits(union):
            candidates.add(_trim(union, score))
    if need and usable("Morale"):
        candidates |= _with_morale(candidates, score)
    best = max(candidates, key=lambda a: score(a)[1])
    return score(best)[0] if score(best)[2] > base[2] else entry


def _with_morale(candidates, score):
    """Morale on 1 or 2 certain wins (with a free grey slot) of each candidate whose key
    point requirements are not certain yet, where that makes them certain."""
    more = set()
    for assign in candidates:
        e, _, (req, _) = score(assign)
        if req:
            continue
        free = [i for i, r in enumerate(e[3]) if r["card"] is not None and r["p"] >= 1
                and not any(s == i and MODIFIERS[nm][0] == "grey" for s, nm in assign)]
        for size in (1, 2):
            for slots in combinations(free, size):
                extra = assign | {(i, "Morale") for i in slots}
                if score(extra)[2][0]:
                    more.add(_trim(extra, score))
    return more


def _trim(assign, score):
    """Drop modifiers (most expensive first) whose removal keeps the same certainty."""
    level = score(assign)[2]
    trimmed = True
    while trimmed:
        trimmed = False
        for item in sorted(assign, key=lambda a: -MODIFIERS[a[1]][1]):
            if score(assign - {item})[2] >= level:
                assign, trimmed = assign - {item}, True
                break
    return assign


# What each modifier can change: the card's own battle, the battles its buffs or debuffs
# reach, or (Morale) only the goal.
_OWN_MODS = frozenset({"Gunnery", "Damage Control", "Dodge", "Focused", "Fast Repair"})


def _useful_modifiers(combo, keypoints, base, min_wins):
    """The (key point, slot, modifier) choices that could make this split rank higher, for
    when only the best plan matters. Modifiers cost points and spend ranks before HP cost,
    so a variant only beats its split by raising the goal chance or the expected wins,
    which needs some battle it can reach to be less than certain:
      Gunnery, Damage Control, Dodge, Focused: the card's own battle (Fast Repair: also only
        if the card is damaged);
      Teamwork: a neighbour the card buffs; Specialization: an enemy its debuffs hit;
        Rabid: an enemy its debuffs miss for lack of the vulnerability;
      Unstoppable: its own battle under a parameter condition, or what its buffs / debuffs
        reach under a buff / debuff power condition;
      Morale: any won-able battle, while the goal is not certain.
    Returns None (everything) if some modifier costs nothing."""
    if min(v for _, v in MODIFIERS.values()) <= 0:
        return None
    goal_open = _stage_key(base, min_wins)[0] < 1
    allowed = set()
    for ki, (e, kp) in enumerate(zip(combo, keypoints)):
        open_ = [r["card"] is not None and r["p"] < 1 for r in e[3]]
        for i, c in enumerate(e[1]):
            if c is not None:
                morale = goal_open and e[3][i]["p"] > 0
                allowed.update((ki, i, name) for name in _useful_on(c, i, kp, open_, morale))
    return allowed


def _useful_on(c, i, kp, open_, morale):
    """Modifiers that could raise a battle's chance when put on card c in slot i of key
    point kp, given which slots hold a battle that is not yet certain (open_)."""
    buffs = [i + BUFF_OFFSET[a.dir] for a in c.abilities
             if a.kind == "buff" and 0 <= i + BUFF_OFFSET[a.dir] < 4]
    hits, misses = [], []
    for a in c.abilities:
        j = i + DEBUFF_OFFSET[a.dir] if a.kind == "debuff" else -1
        if 0 <= j < 4 and kp.enemies[j] is not None:
            (hits if a.target in kp.enemies[j].vulns else misses).append(j)
    reach = {
        "Teamwork": any(open_[j] for j in buffs),
        "Specialization": any(open_[j] for j in hits),
        "Rabid": any(open_[j] for j in misses),
        "Morale": morale,
        "Unstoppable": bool((open_[i] and any(kp.cond.get(p) for p in PARAMS))
                            or (kp.cond.get("B") and any(open_[j] for j in buffs))
                            or (kp.cond.get("D") and any(open_[j] for j in hits))),
    }
    for name in MODIFIERS:
        if name in _OWN_MODS:
            if open_[i] and (name != "Fast Repair" or c.hp < 3):
                yield name
        elif reach.get(name):
            yield name


def solve_stage(roster, keypoints, min_wins=0, inventory=None, limits=None, report=None):  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    """Split the available cards across all key points of a stage (each card at most once).
    Returns ([(combo, stage summary)] best first, number of card splits scored, number of
    plans scored including modifier variants). With limits.distinct, plans after the first
    are the best that change at least that many card assignments from every earlier plan.
    If `report` is a dict, it receives how far the search got: "options" (entries kept per
    key point), "exact" (nothing trimmed), "rounds", and for plan 1's modifier search
    "modified" (splits searched) and "candidates" (splits held).
    With a time budget, widening the trim may use the first 20% of it (40% without
    alternatives), plan 1's modifier search runs to 50% (95%), and the alternative plans'
    searches to 95%."""
    limits = limits or SearchLimits()
    start = time.perf_counter()
    budget = limits.time_budget
    alternatives = limits.distinct > 0 and limits.top > 1

    def by(share):
        return None if budget is None else start + share * budget

    avail = [c for c in roster if c.hp > 0 and not c.used]
    kp_mins = [kp.min_wins for kp in keypoints]

    # Phase 1: best card placements per key point, then the best splits across key points.
    keyed = []
    for kp in keypoints:
        cards = [c for c in avail if c.name not in kp.barred]
        keyed.append((cards, _subset_keys(cards, kp.enemies, kp.cond)))
    top, n_plans, cap, rounds, pools = _widening_search(
        keyed, keypoints, min_wins, kp_mins, limits, by(0.2 if alternatives else 0.4))
    use_mods = bool(inventory) and limits.max_mods > 0
    n_total = n_plans
    candidates, modified = len(top.heap), 0
    if use_mods:
        # Only plan 1 comes from this search when alternatives are searched separately.
        variants, modified = _add_modifiers(
            top, keypoints, inventory, limits, min_wins, kp_mins,
            only_best=limits.distinct > 0 or limits.top == 1,
            deadline=by(0.5 if alternatives else 0.95))
        n_total += variants

    if limits.distinct > 0:
        # Each further plan: search the same options again, skipping splits too close to the
        # plans already chosen (modifiers never change which cards go where).
        combos = top.best(1)
        while combos and len(combos) < limits.top:
            alt, scored = _best_splits(pools, min_wins, kp_mins, limits.keep,
                                       avoid=[[e[0] for e in c] for c in combos],
                                       min_change=limits.distinct)
            n_plans += scored
            n_total += scored
            if not alt.heap:
                break
            if use_mods:
                n_total += _add_modifiers(alt, keypoints, inventory, limits, min_wins, kp_mins,
                                          only_best=True, deadline=by(0.95))[0]
            combos.append(alt.best(1)[0])
    else:
        combos = top.best(limits.top)

    if report is not None:
        report.update(options=cap, rounds=rounds,
                      exact=cap >= max((len(k) for _, k in keyed), default=0),
                      modified=modified, candidates=candidates)
    best = []
    for combo in combos:
        best.append((combo, stage_summary([e[4] for e in combo], min_wins, kp_mins)))
    return best, n_plans, n_total


def _widening_search(keyed, keypoints, min_wins, kp_mins, limits, deadline):  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    """Best splits over the key points' options, first trimmed to fit limits.max_combos.
    With a `deadline` (from the time budget), the trim is then widened round by round while
    the next round is expected to finish by it; a round that overruns anyway is abandoned
    for the previous one. Each round searches a superset of the last, so it can only find
    better splits.
    Returns (TopK, splits scored, entries kept per key point, rounds completed, the final
    round's pools)."""
    caps = _caps([len(k) for _, k in keyed], limits.max_combos, widen=deadline is not None)
    pools = [[] for _ in keyed]
    result, times = None, []
    for cap in caps:
        if result is not None:
            # The next round costs about as much more as the last one did over the one before.
            growth = times[-1] / times[-2] if len(times) > 1 and times[-2] > 0 else 3.0
            if time.perf_counter() + times[-1] * max(growth, 1.5) > deadline:
                break
        began = time.perf_counter()
        for pool, (cards, entries), kp in zip(pools, keyed, keypoints):
            pool.extend(_entry(cards, kp.enemies, kp.cond, at)
                        for _, at in entries[len(pool):cap])
        try:
            top, scored = _best_splits([p[:cap] for p in pools], min_wins, kp_mins, limits.keep,
                                       deadline if result is not None else None)
        except _OutOfTime:
            break
        result = (top, scored, cap, [p[:cap] for p in pools])
        times.append(time.perf_counter() - began)
    top, scored, cap, final_pools = result
    return top, scored, cap, len(times), final_pools
