# How the solver works

The solver lives in [`webapp/engine`](../webapp/engine) and has no web dependencies:

| Module | What it does |
|---|---|
| [`model.py`](../webapp/engine/model.py) | The battle model: scores one placement of cards at a key point |
| [`search.py`](../webapp/engine/search.py) | The stage search, `solve_stage()` |
| [`solver.py`](../webapp/engine/solver.py) | CSV readers and `validate()`; re-exports the other two |

The web app calls `solve_stage()` with the stage you entered.

Given your cards and the key points of a stage, it suggests which cards
to place where, and with which modifiers, so that the key points' requirements are met as
reliably as possible and as many battles as possible are won
possible.

## Inputs and output

```mermaid
flowchart LR
    subgraph Inputs
        R["Cards<br/>stats, abilities, HP, used, type"]
        K["Key points<br/>4 enemy slots each:<br/>power, boost, vulnerabilities, must-win"]
        E["Per key point<br/>wins needed, % conditions,<br/>barred cards / types,<br/>blocked modifier slots"]
        M["Modifiers<br/>web app: any can be bought,<br/>CSV: a count of each"]
    end
    R & K & E & M --> S(["solve_stage()"])
    S --> P["Best plan + an alternative assigning<br/>at least 3 cards differently (web app)<br/>card + modifiers per slot,<br/>win chance per battle,<br/>requirements chance, expected wins, HP cost"]
```

## The search

```mermaid
flowchart TD
    A["Usable cards<br/>HP > 0 and not used this stage"] --> B

    subgraph P1 ["Phase 1: cards only, no modifiers"]
        B["For each key point:<br/>try every placement of 0–4 cards<br/>(skipping barred cards and empty enemy slots)"]
        B --> C["Score each placement with the battle model<br/>(win chances cached per card and neighbours)"]
        C --> D["Keep the best placement for each distinct set of cards<br/>→ one pool of options per key point"]
        D --> T{"Pools too large to combine?<br/>(product above 400³)"}
        T -- yes --> T2["Keep only the best 400 entries of each pool<br/>(widened while time allows, see below)"]
        T -- no --> X
        T2 --> X["Combine one option from every pool,<br/>never using a card twice"]
        X --> BB{"Could this partial split still<br/>beat the weakest one held?"}
        BB -- "no: skip it and everything under it" --> X
        BB -- yes --> Y["Score the whole stage:<br/>chance of beating every must-win enemy<br/>and meeting every key point minimum"]
        Y --> Z["Keep the best card splits<br/>(5,000 in the web app, 40 by default)"]
    end

    Z --> M1

    subgraph P2 ["Phase 2: modifiers"]
        M1["For each kept split, best first while time allows:<br/>try every useful way to add 1–2 modifiers<br/>(CSV: up to 2 per stage, within stock;<br/>web app: one grey + one green per card,<br/>each needed for a certain win;<br/>never in blocked slots)"]
        M1 --> M2["Re-score only the key points that changed<br/>(each key point + modifier set computed once)"]
    end

    M2 --> O["Rank all candidates and return the top plans"]
```

### How plans are ranked

Plans are compared on these, in order. A later item only breaks ties in the earlier ones.

1. **Chance of meeting the requirements**: beating every must-win enemy and winning at
   least each key point's required wins. There is no stage-wide goal; set required wins
   on the key points instead.
2. **Expected number of wins.**
3. **Fewest modifier points spent**, so a modifier is only used when it raises the requirements
   chance or the expected wins.
4. **Lowest HP cost**: the card power lost to the 1 HP each placed card spends.

Enemy rewards are not ranked: to make sure an enemy with a reward you want is beaten, mark
it must-win.

### The alternative plan

The second-best plan overall is nearly always the best plan with a trivial change: two cards
swapped within a key point, or one card replaced by an equal one. So the web app's plan 2 is
instead **the best plan that assigns at least 3 cards differently** from plan 1. A card counts
once whether it joins, leaves, or moves to another key point; replacing one card with another
counts as 2. Card positions within a key point and modifiers don't count.

Plan 1 is found as usual. Plan 2 comes from a second search over the same options that skips
every split within 3 changes of plan 1, followed by its own modifier search, so it is the
true best plan under that rule rather than the best of a few near-copies. The page shows how
many cards differ. The threshold is `distinct=3` in the web app's `SOLVE_LIMITS`
([`webapp/sandbox.py`](../webapp/sandbox.py)); the second search costs about one more search
round.

## The battle model

Each placement is scored with the game's numbers, checked against in-game observations
(`validate()` and the tests reproduce them).

```mermaid
flowchart LR
    subgraph Card ["Your card in a slot"]
        C1["Each parameter F, S, M:<br/>base × HP factor (100 / 90 / 80%)<br/>× (100% + buffs + modifiers + conditions),<br/>rounded once"] --> C2["Power P = F + S + M<br/>roll is a whole number in P−10 … P+10"]
    end
    subgraph Enemy ["Enemy opposite it"]
        E1["base × (100% − matching debuffs<br/>+ its own boost + enemy power condition),<br/>rounded"]
    end
    C2 & E1 --> W["Win chance =<br/>share of the 21 rolls ≥ enemy power<br/>(ties are wins)"]
```

- **HP** scales the whole parameter, buffs included: at 2 HP a card with a +30% buff gets
  90% × 130%, not 120% (confirmed in game).
- **Buffs** go to the neighbour on the left or right; **debuffs** hit the enemy diagonally
  left, straight ahead or diagonally right, and only if it has that vulnerability (or the card
  carries Rabid).
- **Buff power / Debuff power** conditions and Teamwork / Specialization scale each ability:
  a 20% debuff at −50% debuff power gives 10%, or 30% with Specialization.
- Win chances of the battles in a key point are combined into a distribution of wins, so the
  chance of "at least N wins" is exact rather than estimated. A Morale win counts twice.

## How long a solve takes

The search space grows quickly with the number of usable cards:

| Usable cards | Placements per key point (4 enemies) |
|---|---|
| 12 | 18,001 |
| 16 | 58,625 |
| 20 | 146,001 |
| 24 | 307,009 |

Combining key points multiplies that again: 3 key points with 24 cards have billions of
possible card splits. These keep the solve to seconds, without changing which plans it finds:

1. **Cheap placement scoring.** A card's win chance in a slot depends only on the card and
   what its two neighbours do to that slot, so each chance is computed once and reused, and
   placements are ranked with whole numbers (a chance is always n out of 21). The full
   battle model only runs for the placements that are kept.
2. **Branch and bound.** Once 40 splits are held, a partial split is dropped when even its
   best possible completion cannot beat the weakest of them. "Best possible" combines, for
   the key points still to choose, the highest chance of each win count, the most expected
   wins, and the lowest modifier spend and HP cost that any of their options offers. That is
   an upper bound, so nothing that would have made the top 40 is dropped.
3. **Ties are bounded tightly.** Many plans tie on requirements chance and wins (for example when
   every battle is a certain win) and differ only in HP cost. A split can only tie on wins by
   taking a most-wins option at every remaining key point, so for ties the cost bound uses
   the cheapest *most-wins* option, not the cheapest option overall (which would be the
   empty one).
4. **Stopping a scan early.** Each key point's options are sorted best first. Every 32
   options the search checks the best that any of the remaining ones could still achieve,
   and stops scanning that list once none of them could beat the weakest split held.
5. **Only useful modifiers.** A modifier costs points, and spend ranks before HP cost, so
   it can only improve a plan by raising the requirements chance or the expected wins, which means
   raising the chance of some battle that is not yet certain. Each modifier can only reach
   certain battles: Gunnery, Damage Control, Dodge and Focused its own card's (Fast Repair
   too, if the card is damaged); Teamwork the neighbours its card buffs; Specialization the
   enemies its card's debuffs hit, Rabid those they miss; Unstoppable whatever the key
   point's conditions weaken (so it, and Teamwork against a "Buff power −100%" condition,
   are tried exactly where they matter); Morale the required wins, while it is not certain. A
   modifier whose reachable battles are all certain wins is not tried. This applies when a
   search only needs its single best plan, as in the web app; a modifier whose value is set
   to 0 turns it off.
6. **Many splits get modifiers.** Modifiers are tried on the best splits found without
   them, and a split that is mediocre without modifiers can be the best with them. The web
   app keeps up to 5,000 splits for this (instead of 40) and works through them best first
   while the time budget allows; the map page says if it had to stop early. On a hard stage
   with a −100% buff power key point this found 11.95 expected wins at HP cost 198, where 40
   splits gave 11 wins at HP cost 269.5.
7. **Modifiers for certainty (web app).** The web app does not count modifiers: it assumes
   any can be bought, and each card can take one grey and one green, as in the game. They
   are kept for what is otherwise hard to reach: **each** modifier used must be needed to
   make something certain, meaning more battles won at 100%, or the key point's
   requirements (must-win enemies, required wins) met at 100%. A modifier that only lifts a
   battle from 60% to 80% is not suggested. Key points never share modifiers, so each key
   point plan is worked out once and reused by every split that contains it:
   - for each battle below 100%, the fewest (then cheapest) modifiers that reach it and
     make it certain, trying up to 3 together and keeping the 3 cheapest ways;
   - every mix of those ways across the key point's battles, with any modifier that is not
     needed dropped;
   - Morale on certain wins where that makes the required wins certain.

   [`tests/test_search.py`](../tests/test_search.py) checks this against trying every
   grey and green on every card. Where you cannot buy or use modifiers, tick No grey / No
   green modifiers on the key point: the solver then maximizes the chances with cards
   alone.
8. **Reused modifier results.** A key point's result with a given set of modifiers is
   computed once and shared by every split and variant that contains it.

[`tests/test_search.py`](../tests/test_search.py) checks that the results match the plain
exhaustive search on generated stages, including close contests won with modifiers.

### Trimming the options, and the time budget

If combining every option would mean more than 400³ splits, each key point's options are
first cut to the ones it ranks highest on its own (400 each with 3 key points, 8,000 each
with 2). That can hide the best plan: a split that is best for the whole stage may use an
option that looks weak for its own key point, for example one that leaves strong cards free
for a harder key point. On 30 generated 3-key-point stages this happened once (measured with
an earlier ranking), with a slightly worse plan than the true best.

So the web app uses a **time budget of about 20 seconds**: after the first, trimmed round,
the search is repeated with twice as many options each time (up to all of them) while the
next round is expected to finish within the first 20% of the budget. Plan 1's modifier
search then runs until 50% of it, and the alternative plan's search until 95%. Each round includes everything the previous one searched, so it can
only find better plans; a round that runs over is abandoned for the last complete one. The
first round always completes, so a very hard stage can take somewhat longer than the budget.

With the bounds above, most stages reach every option well within the budget. The map page
shows how far the search got: *searched every option*, or *best N options per key point (time
limit)*. Tests and scripts that call `solve_stage()` without a budget keep the fixed trim, so
their results never depend on the machine.

### Times

On a desktop, with the web app's settings (plan 1 and an alternative):

| Stage | Time | Options searched |
|---|---|---|
| Map 10, stage 1: 29 cards, 2 key points | 4 s | all (104 s and 8,000 per key point before the tie bound) |
| The example in `tests/data`: 24 cards, 3 key points | 3 s | all |
| A hard generated close contest: 24 cards, 3 key points | 6 s | all (13 s at 400 per key point before) |

The very first version of the search took about 200 s on the example at 400 options. Fewer
usable cards (barred, already used, or at 0 HP) make every solve faster.

## Assumptions not yet confirmed in game

These are marked `ASSUMPTION` in the code:

- **Focused:** the card neither gives nor receives buffs, and its debuffs are off.
- **Unstoppable:** ignores the battle conditions on its own parameters and on the power of its
  own buffs and debuffs. It does not lift card bans, modifier bans or HP loss.
- **Buff / debuff power** conditions add to Teamwork / Specialization (×1.5 under −50%), rather
  than multiplying them.
- **Enemy power** conditions add to the same pool as debuffs, like an enemy's own boost does
  (that part is confirmed in game: base 108, +16% and a 20% debuff gave 104).
- **Fast Repair:** +2 HP (up to 3) before the battle, which then costs 1 HP as usual.
