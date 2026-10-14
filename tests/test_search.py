"""The fast search must give the same results as the plain exhaustive search it replaced.

reference_* below is that exhaustive search: score every arrangement with evaluate() /
summarize(), try every split of the cards across key points, then every modifier variant
of the best splits."""
# pylint: disable=missing-function-docstring,too-many-locals,too-many-nested-blocks

import random
from collections import Counter
from itertools import combinations

import pytest

from webapp.engine import search, solver
from webapp.engine.solver import Card, Enemy, KeyPoint, buff, debuff


def reference_pool(avail, enemies, cond):
    best = {}
    for cards, slots in solver.arrangements(avail, enemies):
        res = solver.evaluate(slots, enemies, cond)
        sm = solver.summarize(res, enemies)
        key = solver.rank_key(sm)
        sub = frozenset(c.name for c in cards)
        if sub not in best or key > best[sub][0]:
            best[sub] = (key, slots, res, sm)
    pool = [(sub, slots, [()] * 4, res, sm) for sub, (_, slots, res, sm) in best.items()]
    pool.sort(key=lambda e: solver.rank_key(e[4]), reverse=True)
    return pool


def reference_combos(pools, used=frozenset(), idx=0):
    if idx == len(pools):
        yield []
        return
    for entry in pools[idx]:
        if not entry[0] & used:
            for rest in reference_combos(pools, used | entry[0], idx + 1):
                yield [entry] + rest


def reference_mods(chosen, n_kp, inventory):
    mods = [[[] for _ in range(4)] for _ in range(n_kp)]
    used = Counter()
    for ki, i, n in chosen:
        if any(solver.MODIFIERS[x][0] == solver.MODIFIERS[n][0] for x in mods[ki][i]):
            return None
        mods[ki][i].append(n)
        used[n] += 1
        if used[n] > inventory[n]:
            return None
    return [[tuple(x) for x in kp] for kp in mods]


def reference_solve(roster, keypoints, min_wins, inventory, limits, accept=None):  # pylint: disable=too-many-arguments,too-many-positional-arguments
    """The top plans; with `accept`, only card splits it accepts are considered."""
    avail = [c for c in roster if c.hp > 0 and not c.used]
    kp_mins = [kp.min_wins for kp in keypoints]
    pools = search._trim_pools(  # pylint: disable=protected-access
        [reference_pool([c for c in avail if c.name not in kp.barred], kp.enemies, kp.cond)
         for kp in keypoints], limits.max_combos)
    top = solver.TopK(limits.keep)
    for combo in reference_combos(pools):
        if accept and not accept(combo):
            continue
        st = solver.stage_summary([e[4] for e in combo], min_wins, kp_mins)
        top.push(solver.rank_key(st), (combo, st))
    if inventory and limits.max_mods > 0:
        names = [n for n, q in inventory.items() if q > 0]
        for _, _, (combo, _) in list(top.heap):
            options = [(ki, i, n) for ki, e in enumerate(combo)
                       for i, c in enumerate(e[1]) if c is not None for n in names
                       if solver.MODIFIERS[n][0] not in keypoints[ki].no_mods]
            for k in range(1, limits.max_mods + 1):
                for chosen in combinations(options, k):
                    mods = reference_mods(chosen, len(combo), inventory)
                    if mods is None:
                        continue
                    new = []
                    for e, kp, m in zip(combo, keypoints, mods):
                        if any(m):
                            res = solver.evaluate(e[1], kp.enemies, kp.cond, m)
                            e = (e[0], e[1], m, res, solver.summarize(res, kp.enemies, m))
                        new.append(e)
                    st = solver.stage_summary([e[4] for e in new], min_wins, kp_mins)
                    top.push(solver.rank_key(st), (new, st))
    return top.best(limits.top)


def outline(combo):
    """Cards and modifiers per slot of a plan, comparable across both searches."""
    return [[(c.name if c else None, tuple(m)) for c, m in zip(e[1], e[2])] for e in combo]


# ---------------------------------------------------------------- generated stages
VULNS = list(solver.VULNS)


def random_card(rng, name):
    abilities = []
    for _ in range(rng.choice((1, 2, 2))):
        if rng.random() < 0.5:
            abilities.append(buff(rng.choice("FSM"), rng.choice(("left", "right")),
                                  rng.choice((20, 30))))
        else:
            abilities.append(debuff(rng.choice(VULNS), rng.choice(("up-left", "up", "up-right")),
                                    rng.choice((20, 25))))
    return Card(name, rng.randint(25, 70), rng.randint(20, 70), rng.randint(15, 80),
                tuple(abilities), hp=rng.choice((3, 3, 2, 1)), type=rng.choice("ab"))


def random_keypoint(rng, name, cards):
    enemies = []
    for _ in range(4):
        if rng.random() < 0.15:
            enemies.append(None)
            continue
        power = rng.randint(90, 175)
        vulns = tuple(rng.sample(VULNS, rng.choice((0, 1, 2))))
        rng.choice((0, 1, 8, 11.5, 15))      # was the reward; keeps the seeds' stages
        enemies.append(Enemy(power, vulns, rng.random() < 0.1))
    cond = {k: rng.choice((-50, -12, 8)) for k in rng.sample("FSMEBD", rng.choice((0, 1, 2)))}
    return KeyPoint(name, enemies, cond, rng.choice((0, 0, 1, 2)),
                    frozenset(rng.sample([c.name for c in cards], rng.choice((0, 0, 1)))),
                    frozenset(rng.sample(["grey", "green"], rng.choice((0, 0, 1)))))


def random_stage(seed, n_cards, n_kps):
    rng = random.Random(seed)
    cards = [random_card(rng, f"C{i}") for i in range(n_cards)]
    if rng.random() < 0.3:
        cards[0] = solver.replace(cards[0], used=True)
    keypoints = [random_keypoint(rng, str(k + 1), cards) for k in range(n_kps)]
    goal = rng.randint(0, 3 * n_kps)
    inventory = {n: rng.choice((0, 1, 2)) for n in solver.MODIFIERS}
    return cards, keypoints, goal, inventory


def close_stage(seed, n_cards, n_kps):
    """Enemies about as strong as the cards, so goal chances are fractional and modifiers
    make the best plans."""
    rng = random.Random(seed)
    cards = [random_card(rng, f"C{i}") for i in range(n_cards)]
    keypoints = []
    for k in range(n_kps):
        enemies = []
        for _ in range(4):
            power = rng.randint(115, 150)
            vulns = tuple(rng.sample(VULNS, rng.choice((1, 2))))
            rng.choice((5, 10, 15))          # was the reward; keeps the seeds' stages
            enemies.append(Enemy(power, vulns, rng.random() < 0.15))
        keypoints.append(KeyPoint(str(k + 1), enemies, {}, rng.choice((0, 1))))
    goal = rng.randint(n_kps * 2, n_kps * 3)
    inventory = {n: rng.choice((1, 2)) for n in solver.MODIFIERS}
    return cards, keypoints, goal, inventory


@pytest.mark.parametrize("seed", [0, 14, 24, 28, 37, 45, 49])
def test_close_contests_match_the_exhaustive_search(seed):
    cards, keypoints, goal, inventory = close_stage(seed, 7, 3)
    limits = solver.SearchLimits(top=5, keep=12)
    fast, _, _ = solver.solve_stage(cards, keypoints, goal, inventory, limits)
    slow = reference_solve(cards, keypoints, goal, inventory, limits)
    assert [solver.rank_key(st) for _, st in fast] == [solver.rank_key(st) for _, st in slow]
    assert [outline(c) for c, _ in fast] == [outline(c) for c, _ in slow]
    assert 0 < fast[0][1]["p_goal"] < 1                     # a real contest
    assert any(m for e in fast[0][0] for m in e[2])         # won with modifiers


@pytest.mark.parametrize("seed", range(6))
def test_pools_match_the_exhaustive_scoring(seed):
    cards, keypoints, _, _ = random_stage(seed, 8, 1)
    kp = keypoints[0]
    fast = solver.best_by_subset(cards, kp.enemies, kp.cond)
    slow = reference_pool(cards, kp.enemies, kp.cond)
    assert [solver.rank_key(e[4]) for e in fast] == [solver.rank_key(e[4]) for e in slow]
    assert {e[0]: outline([e]) for e in fast} == {e[0]: outline([e]) for e in slow}


@pytest.mark.parametrize("seed, n_cards, n_kps, max_combos", [
    (1, 7, 1, 400 ** 3), (2, 7, 2, 400 ** 3), (3, 6, 3, 400 ** 3), (4, 8, 2, 400 ** 3),
    (5, 6, 3, 400 ** 3), (6, 8, 2, 60 ** 2), (7, 7, 3, 50 ** 3), (8, 9, 2, 400 ** 3),
])
def test_solve_stage_matches_the_exhaustive_search(seed, n_cards, n_kps, max_combos):
    cards, keypoints, goal, inventory = random_stage(seed, n_cards, n_kps)
    limits = solver.SearchLimits(top=5, keep=12, max_combos=max_combos)
    fast, _, _ = solver.solve_stage(cards, keypoints, goal, inventory, limits)
    slow = reference_solve(cards, keypoints, goal, inventory, limits)
    assert [solver.rank_key(st) for _, st in fast] == [solver.rank_key(st) for _, st in slow]
    assert [outline(c) for c, _ in fast] == [outline(c) for c, _ in slow]


def test_bounding_skips_most_splits():
    cards, keypoints, goal, inventory = random_stage(11, 9, 3)
    limits = solver.SearchLimits(top=2, keep=10)
    _, scored, _ = solver.solve_stage(cards, keypoints, goal, inventory, limits)
    pools = [reference_pool([c for c in cards if c.hp > 0 and not c.used
                             and c.name not in kp.barred], kp.enemies, kp.cond)
             for kp in keypoints]
    assert scored < sum(1 for _ in reference_combos(pools))


def test_time_budget_widens_to_the_exact_search():
    cards, keypoints, goal, inventory = close_stage(41, 9, 3)
    start = solver.SearchLimits(top=5, keep=12, max_combos=60 ** 3)   # trims at first
    report = {}
    fast, _, _ = solver.solve_stage(cards, keypoints, goal, inventory,
                                    solver.replace(start, time_budget=600), report=report)
    assert report["exact"] and report["rounds"] > 1
    exact = reference_solve(cards, keypoints, goal, inventory,
                            solver.replace(start, max_combos=10 ** 12))
    assert [solver.rank_key(st) for _, st in fast] == [solver.rank_key(st) for _, st in exact]
    assert [outline(c) for c, _ in fast] == [outline(c) for c, _ in exact]


def test_tiny_time_budget_keeps_the_first_round():
    cards, keypoints, goal, inventory = close_stage(41, 9, 3)
    fixed = solver.SearchLimits(top=5, keep=12, max_combos=60 ** 3)
    report = {}
    rushed, _, _ = solver.solve_stage(cards, keypoints, goal, inventory,
                                      solver.replace(fixed, time_budget=1e-6), report=report)
    assert report == {"options": 60, "rounds": 1, "exact": False}
    plain, _, _ = solver.solve_stage(cards, keypoints, goal, inventory, fixed)
    assert [outline(c) for c, _ in rushed] == [outline(c) for c, _ in plain]


def reference_distinct(cards, keypoints, goal, inventory, limits):
    """Plan 1 is the best plan; each further plan is the best one changing at least
    limits.distinct card assignments from every earlier plan."""
    one = solver.replace(limits, top=1, distinct=0)
    plans = reference_solve(cards, keypoints, goal, inventory, one)
    while plans and len(plans) < limits.top:
        earlier = [combo for combo, _ in plans]
        more = reference_solve(cards, keypoints, goal, inventory, one, accept=lambda c: all(
            len(search.changed_cards(c, p)) >= limits.distinct for p in earlier))
        if not more:
            break
        plans.append(more[0])
    return plans


@pytest.mark.parametrize("make, seed", [(close_stage, 0), (close_stage, 24), (close_stage, 37),
                                        (random_stage, 3), (random_stage, 7), (random_stage, 8)])
def test_distinct_alternatives_match_the_exhaustive_search(make, seed):
    cards, keypoints, goal, inventory = (make(seed, 7, 3) if make is close_stage
                                         else make(seed, 8, 2))
    limits = solver.SearchLimits(top=3, keep=12, distinct=3)
    fast, _, _ = solver.solve_stage(cards, keypoints, goal, inventory, limits)
    slow = reference_distinct(cards, keypoints, goal, inventory, limits)
    assert [solver.rank_key(st) for _, st in fast] == [solver.rank_key(st) for _, st in slow]
    assert [outline(c) for c, _ in fast] == [outline(c) for c, _ in slow]
    assert len(fast) >= 2
    for i, (combo, _) in enumerate(fast):
        for earlier, _ in fast[:i]:
            assert len(search.changed_cards(combo, earlier)) >= 3


def test_first_plan_is_unchanged_by_asking_for_alternatives():
    cards, keypoints, goal, inventory = close_stage(0, 7, 3)
    plain, _, _ = solver.solve_stage(cards, keypoints, goal, inventory,
                                     solver.SearchLimits(top=2, keep=12))
    distinct, _, _ = solver.solve_stage(cards, keypoints, goal, inventory,
                                        solver.SearchLimits(top=2, keep=12, distinct=3))
    assert outline(plain[0][0]) == outline(distinct[0][0])
    assert len(search.changed_cards(plain[1][0], plain[0][0])) < 3   # the near-copy it replaces
