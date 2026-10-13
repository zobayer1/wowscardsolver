# How the solver works

The solver lives in [`webapp/engine/solver.py`](../webapp/engine/solver.py). It has no web
dependencies: the web app and the command-line tool both call `solve_stage()` with the same
inputs.

Given your cards, the key points of a stage and your modifier stock, it suggests which cards
to place where, and with which modifiers, so that the stage goal is met as reliably as
possible.

## Inputs and output

```mermaid
flowchart LR
    subgraph Inputs
        R["Cards<br/>stats, abilities, HP, used, type"]
        K["Key points<br/>4 enemy slots each:<br/>power, vulnerabilities, reward, must-win"]
        E["Environment<br/>stage goal, wins needed per key point,<br/>% conditions, barred cards / types,<br/>blocked modifier slots"]
        M["Modifier stock<br/>count of each modifier"]
    end
    R & K & E & M --> S(["solve_stage()"])
    S --> P["Best plans (2 in the web app)<br/>card + modifiers per slot,<br/>win chance per battle,<br/>goal chance, expected reward and wins, HP cost"]
```

## The search

```mermaid
flowchart TD
    A["Usable cards<br/>HP > 0 and not used this stage"] --> B

    subgraph P1 ["Phase 1: cards only, no modifiers"]
        B["For each key point:<br/>try every placement of 0–4 cards<br/>(skipping barred cards and empty enemy slots)"]
        B --> C["Score each placement with the battle model"]
        C --> D["Keep the best placement for each distinct set of cards<br/>→ one pool of options per key point"]
        D --> T{"Pools too large to combine?<br/>(product above 400³)"}
        T -- yes --> T2["Keep only the best entries of each pool"]
        T -- no --> X
        T2 --> X["Combine one option from every pool,<br/>never using a card twice"]
        X --> Y["Score the whole stage:<br/>chance of meeting the stage goal<br/>and every per-key-point minimum"]
        Y --> Z["Keep the 40 best card splits"]
    end

    Z --> M1

    subgraph P2 ["Phase 2: modifiers"]
        M1["For each of the 40 splits:<br/>try every way to add 1–2 modifiers<br/>(one grey + one green per card,<br/>within stock, not in blocked slots)"]
        M1 --> M2["Re-score the key points that changed"]
    end

    M2 --> O["Rank all candidates and return the top plans"]
```

### How plans are ranked

Plans are compared on these, in order. A later item only breaks ties in the earlier ones.

1. **Chance of meeting the stage goal**, including every per-key-point minimum and every
   must-win enemy.
2. **Expected reward**, minus the point value of the modifiers the plan spends.
3. **Expected number of wins.**
4. **Lowest HP cost**: the card power lost to the 1 HP each placed card spends.

## The battle model

Each placement is scored with the game's numbers, checked against in-game observations
(`validate()` reproduces them on every command-line run and in the tests).

```mermaid
flowchart LR
    subgraph Card ["Your card in a slot"]
        C1["Each parameter F, S, M:<br/>base × (100% + buffs + modifiers<br/>+ conditions − 10% per lost HP),<br/>rounded"] --> C2["Power P = F + S + M<br/>roll is a whole number in P−10 … P+10"]
    end
    subgraph Enemy ["Enemy opposite it"]
        E1["base × (100% − matching debuffs<br/>+ enemy power condition),<br/>rounded"]
    end
    C2 & E1 --> W["Win chance =<br/>share of the 21 rolls ≥ enemy power<br/>(ties are wins)"]
```

- **Buffs** go to the neighbour on the left or right; **debuffs** hit the enemy diagonally
  left, straight ahead or diagonally right, and only if it has that vulnerability (or the card
  carries Rabid).
- **Buff power / Debuff power** conditions and Teamwork / Specialization scale each ability:
  a 20% debuff at −50% debuff power gives 10%, or 30% with Specialization.
- Win chances of the battles in a key point are combined into a distribution of wins, so the
  chance of "at least N wins" is exact rather than estimated. A Morale win counts twice.

## Why it can take minutes

The search is exhaustive, so its size grows quickly with the number of usable cards and key
points:

| Usable cards | Placements tried per key point (4 enemies) |
|---|---|
| 12 | 18,001 |
| 16 | 58,625 |
| 20 | 146,001 |
| 24 | 307,009 |

Combining several key points multiplies the options again; for example, 3 key points with 24
cards compare a few million card splits. Typical times on a desktop:

| Stage | Time |
|---|---|
| 1 key point | a few seconds |
| 2 key points, about 20 cards | under a minute |
| 3 key points, 20–24 cards | 2–5 minutes |

Fewer usable cards (barred, already used, or at 0 HP) make it faster. Solving one key point at
a time, while the stage is still marked incomplete, is also faster than solving all key points
together, but only the joint solve can trade cards between key points.

## Assumptions not yet confirmed in game

These are marked `ASSUMPTION` in the code:

- **Focused:** the card neither gives nor receives buffs, and its debuffs are off.
- **Unstoppable:** ignores the battle conditions on its own parameters and on the power of its
  own buffs and debuffs. It does not lift card bans, modifier bans or HP loss.
- **Buff / debuff power** conditions add to Teamwork / Specialization (×1.5 under −50%), rather
  than multiplying them.
- **Enemy power** conditions add to the same pool as debuffs.
- **Fast Repair:** +2 HP (up to 3) before the battle, which then costs 1 HP as usual.
