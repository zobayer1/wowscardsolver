# CSV files

| File | What it describes | Used by |
|---|---|---|
| `roster.csv` | Your cards | **Import CSV** / **Export CSV** on the Roster page |
| `modifiers.csv` | A modifier stock | the example stage in `tests/data` |
| `board.csv` | Enemy cards at each key point of a stage | the example stage in `tests/data` |
| `environment.csv` | Required wins, battle conditions and restrictions | the example stage in `tests/data` |

In the web app you enter stages, key points and modifier counts on the map page; only the
roster is exchanged as a CSV file. The other three files describe a complete stage for the
tests (`tests/test_csv_compat.py` checks that entering it through the web app gives the solver
the same inputs and plans). Their columns mirror the fields of the map page.

General rules for all files:

- The first row is the header. Column order does not matter, but the names must match.
- Spaces around values are ignored.
- Yes/no columns accept `yes`, `y`, `true`, `1` or `x` as yes. Anything else, including blank, is no.
- Card names, modifier names and vulnerability names are **case-sensitive** and must be spelled exactly as listed here.
- A typo in a name, direction or key point stops the import with an error that names the problem.

---

## roster.csv

One row per card you own.

| Column | Required | Values | Meaning |
|---|---|---|---|
| `name` | yes | text | Card name. Must be unique. Used by `Barred` in `environment.csv`. |
| `firepower` | yes | whole number | **Base** firepower at full HP, before any buffs. |
| `survivability` | yes | whole number | Base survivability. |
| `maneuverability` | yes | whole number | Base maneuverability. |
| `hp` | no (default 3) | `3`, `2`, `1`, `0` | Current HP, for test stages. **Import CSV** ignores it: maps track HP themselves. |
| `used` | no (default no) | yes / no | Card already placed this stage, for test stages. **Import CSV** ignores it. |
| `ability1_type` | no | `buff`, `debuff` | First ability. Leave all four `ability1_*` columns blank if the card has none. |
| `ability1_target` | with type | see below | For a buff: the parameter it raises. For a debuff: the vulnerability it hits. |
| `ability1_direction` | with type | see below | Where the ability lands. |
| `ability1_pct` | no (default 30 for buff, 20 for debuff) | whole number | Strength in percent, written without the % sign. |
| `ability2_*` | no | same as above | Second ability. |
| `type` | no | text | Your own grouping of cards (e.g. `doddy`). Used by `Barred type` in `environment.csv`. Leave the column out of an import to keep the types already set in the web app. |

**Enter base stats**, as the card has them at full HP, not the reduced numbers the game shows for a damaged card.

**Buff targets:** `Firepower`, `Survivability`, `Maneuverability` (or `F`, `S`, `M`).

**Buff directions:** `left`, `right`. The buff goes to the neighboring card on that side. It does not wrap around the board.

**Debuff targets (vulnerabilities):** `AA Fire`, `Smoke Screen`, `Detection`, `Large Caliber`, `Quick Fire`, `Air Strike`.

**Debuff directions:** `up-left`, `up`, `up-right`. These mean the enemy diagonally left, straight ahead, or diagonally right. A debuff only works if that enemy has the matching vulnerability, and it does not wrap around.

Example:

```
name,firepower,survivability,maneuverability,hp,used,ability1_type,ability1_target,ability1_direction,ability1_pct,ability2_type,ability2_target,ability2_direction,ability2_pct,type
Fjord,30,20,50,2,no,debuff,Smoke Screen,up-left,20,buff,Survivability,left,30,doddy
Sandy,35,35,70,3,no,debuff,AA Fire,up,20,debuff,Smoke Screen,up-right,20,cuzr
```

---

## modifiers.csv

One row per modifier type. The solver only recommends a modifier whose `count` is above 0.

| Column | Required | Values | Meaning |
|---|---|---|---|
| `name` | yes | see list below | Modifier name. |
| `type` | no | `grey`, `green` | Slot type. Checked against the built-in type if filled in. |
| `count` | yes | whole number | How many you have. To let the solver use a grey modifier you plan to buy, enter the number you would buy. |
| `value` | no | number | Points-equivalent charged each time one is used. Raise it to make the solver more reluctant to spend that modifier. |
| `effect` | no | text | Description for your reference. Ignored by the solver. |

A modifier is only used when it raises the chance of meeting the requirements (must-win enemies, required wins) or the expected number of wins. Between otherwise equal plans, the one spending fewer modifier points (by `value`) wins.

| Name | Type | Default value | Effect |
|---|---|---|---|
| `Gunnery` | grey | 12 | Firepower +20% |
| `Damage Control` | grey | 12 | Survivability +20% |
| `Dodge` | grey | 12 | Maneuverability +20% |
| `Teamwork` | grey | 12 | This card's buffs are doubled |
| `Effectiveness` | grey | 18 | Reward from this battle +10%. The solver does not rank by reward, so it never recommends this one. |
| `Morale` | grey | 12 | A win counts as 2 toward required wins |
| `Focused` | green | 15 | All parameters +40%; this card gives and receives no buffs, and its debuffs are off |
| `Specialization` | green | 15 | This card's debuffs are doubled |
| `Rabid` | green | 15 | This card's debuffs work without a matching vulnerability |
| `Fast Repair` | green | 15 | +2 HP (max 3) before the battle |
| `Unstoppable` | green | 15 | Battle conditions on parameters, buffs and debuffs are ignored for this card. Card bans, modifier bans and HP loss still apply. |

Each card can carry one grey and one green modifier. By default the solver uses at most 2 modifiers per stage.

---

## board.csv

One row per enemy slot. All key points of the stage go in the same file.

| Column | Required | Values | Meaning |
|---|---|---|---|
| `keypoint` | no (default `1`) | text, e.g. `1`, `2`, `3` | Which key point this enemy belongs to. Must match the labels used in `environment.csv`. |
| `slot` | yes | `1`–`4` | Position from left to right. |
| `power` | yes | whole number | Enemy power as shown **before** you place cards, and before its own boost (see `boost`). Leave blank for an empty slot; no card will be placed opposite it. |
| `vuln1` | no | vulnerability name | First vulnerability (same names as debuff targets above). |
| `vuln2` | no | vulnerability name | Second vulnerability. Leave blank if there is only one. |
| `must_win` | no (default no) | yes / no | The stage fails if this enemy is not defeated (e.g. a primary target, or one whose reward you want). |
| `boost` | no (default 0) | whole number | The enemy's own power boost in percent, e.g. `16`. The game adds it to the same pool as your debuffs, so give the power before the boost: 108 with `boost` 16 and a 20% debuff is 108 × 96% = 104, not 125 × 80% = 100. |

Older files with a `reward` column still load; the column is ignored.

Example (two key points):

```
keypoint,slot,power,vuln1,vuln2,must_win,boost
1,1,115,AA Fire,Smoke Screen,no,
1,2,107,Smoke Screen,Quick Fire,no,
1,3,110,Detection,Air Strike,no,
1,4,105,Quick Fire,Air Strike,no,
2,1,94,Smoke Screen,Quick Fire,no,
2,2,108,Smoke Screen,Large Caliber,no,16
2,3,119,Detection,Large Caliber,no,
2,4,120,AA Fire,Smoke Screen,yes,
```

---

## environment.csv

One row per setting. Columns: `setting`, `keypoint`, `value`.

- A blank `keypoint` means the whole stage (or every key point).
- A filled `keypoint` limits the row to that key point. It must exist in `board.csv`.
- Setting names are not case-sensitive.

| Setting | `keypoint` | `value` | Meaning |
|---|---|---|---|
| `Wins needed` | blank | whole number | Stage goal across all key points. The web app has no stage goal (it maximizes wins), so the compatibility test ignores this row. |
| `Wins needed` | key point | whole number | Minimum wins that must also come from this key point (e.g. to clear a condition it generates). |
| `Firepower` | blank or key point | whole number | Battle condition on your cards, in percent. Negative for a penalty (`-12`), positive for a bonus. |
| `Survivability` | blank or key point | whole number | Same, for survivability. |
| `Maneuverability` | blank or key point | whole number | Same, for maneuverability. |
| `Enemy power` | blank or key point | whole number | Condition that strengthens enemies, in percent (`8` for +8%). |
| `Buff power` | blank or key point | whole number | Condition on the strength of your cards' buffs, in percent (`-50` halves them). |
| `Debuff power` | blank or key point | whole number | Condition on the strength of your cards' debuffs, in percent (`-50` turns a 20% debuff into 10%). |
| `Barred` | blank or key point | card name | This card may not be placed at that key point (or anywhere, if blank). One row per card. |
| `Barred type` | blank or key point | card type | No card of this type may be placed there. One row per type. |
| `No modifiers` | blank or key point | yes | No modifier slots can be used there. |
| `No grey modifiers` | blank or key point | yes | Grey slot blocked there. |
| `No green modifiers` | blank or key point | yes | Green slot blocked there. |

A condition with a blank key point and another for a specific key point are added together at that key point. A setting at `0`, or a missing row, has no effect.

Example:

```
setting,keypoint,value
Wins needed,,8
Wins needed,1,4
Firepower,2,-12
Maneuverability,2,-12
Barred,3,KGV
No modifiers,2,yes
```

---

## How the numbers are used

- **Your card's parameter:** base × HP factor × (100% + buffs + modifiers + conditions), rounded once to the nearest whole number (halves up). The HP factor is 100% / 90% / 80% at 3 / 2 / 1 HP and scales the buffs too: Pulse (45 / 35 / 35) at 2 HP with +30% firepower and +30% survivability is 53 + 41 + 32 = 126, as the game shows.
- **Card power:** firepower + survivability + maneuverability. The battle roll is a whole number from power − 10 to power + 10.
- **Enemy power:** base × (100% − matching debuffs + its own boost + enemy power condition), rounded to the nearest whole number.
- **Buff and debuff strength:** each ability's % × (100% + Teamwork or Specialization 100% + buff or debuff power condition). With `Debuff power` at −50%, a 20% debuff becomes 10%, or 30% with Specialization. Unstoppable ignores these conditions for its card's own abilities.
- **Win chance:** the share of the 21 possible rolls that are at least the enemy's power. Ties are wins.

Plans are ranked by:
1. Chance of beating every must-win enemy and meeting each key point's required wins.
2. Expected number of wins.
3. Fewest modifier points spent.
4. Lowest card cost, i.e. the least power lost to HP damage.

Rewards are not part of the ranking: to make sure an enemy is beaten for its reward, mark it `must_win`.
