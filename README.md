WOWS Card Game Solver
=====================

An interactive solver for a 4-vs-4 card game: it plans which of your cards to send to each key
point of a map stage, and with which modifiers, so that you meet the stage goal as reliably as
possible. See the [game rules](./docs/GAME_RULES.md) and
[how the solver works](./docs/SOLVER.md).

It comes as a small Flask web app that tracks a whole map for you (HP, used cards and the
modifier stock), plus the original command-line solver that reads CSV files.

## Features

- **Roster:** add, edit and remove cards, give them a type, and import or export them as
  `roster.csv`.
- **Maps:** each map works on its own copy of the roster, so HP and used cards are tracked per
  map. New roster cards can be synced in at any time.
- **Stages and key points:** enter up to 5 key points per stage, one at a time, with the
  stage goal and battle conditions: % conditions, barred cards or card types, blocked
  modifier slots.
- **Solver:** suggests the two best plans; **Apply plan** records the battles, takes 1 HP from
  each card, marks the cards used and spends the modifiers.
- **Modifier stock:** shared by all maps, with a version on every count, so saving from an
  out-of-date page is refused.

## Requirements

- Python 3.10 or newer (developed on 3.13)
- Flask 3.1 (installed below)

## Installation

```
git clone <this repository>
cd wowscard
python -m venv .venv
```

Activate the environment:

| Shell | Command |
|---|---|
| Windows PowerShell | `.venv\Scripts\Activate.ps1` |
| Windows cmd | `.venv\Scripts\activate.bat` |
| macOS / Linux | `source .venv/bin/activate` |

Then install the dependencies (the `-dev` file adds pytest for the tests):

```
pip install -r requirements-dev.txt
```

## Running the web app

```
flask --app webapp run --debug
```

Open <http://127.0.0.1:5000>. The database is created on first start at
`instance/wowscard.db`. That folder is git-ignored, so your data stays local.

To load an existing roster, use **Import CSV** on the Roster page, or from the command line:

```
flask --app webapp import-roster path/to/roster.csv
```

### A typical map

1. **Roster:** import or enter your cards and set the modifier counts in the panel on the
   right.
2. **New map** (top right) opens the map in a new tab with every card at 3 HP.
3. **Start a New Stage**, set the stage goal and conditions, and add the key points as they
   appear. Tick **All key points of this stage are entered** once you have them all.
4. **Solve**, then **Apply plan** on the plan you play. Correct any battle that went
   differently by clicking its Won / Lost.
5. **Finish stage**: used marks clear and HP carries over into the next stage.

## Command-line solver

The original CSV workflow still works and gives the same results as the web app. It reads
`roster.csv`, `modifiers.csv`, `board.csv` and `environment.csv` from the current folder:

```
python -m webapp.engine.solver
python -m webapp.engine.solver my_board.csv my_environment.csv
```

The file formats are described in [docs/CSV_PARAMETERS.md](./docs/CSV_PARAMETERS.md). The
files in [`tests/data`](./tests/data) are a complete example. To solve it, run the solver from
that folder with the project folder on `PYTHONPATH`:

```
cd tests/data
PYTHONPATH=../.. python -m webapp.engine.solver                         # macOS / Linux
$env:PYTHONPATH = "../.."; python -m webapp.engine.solver               # Windows PowerShell
```

## Tests

```
python -m pytest            # the regular suite, about 2 minutes
python -m pytest -m slow    # full-size solver runs, several minutes
```

The slow test builds the stage in `tests/data` through the web app and checks that the
solver gets the same inputs, and returns the same plans, as the command-line CSV workflow.

## Cautions

- **Solving can take minutes.** The search is exhaustive: one key point takes seconds, but
  three key points with 20–24 cards can take 2–5 minutes. The page waits until the solve
  finishes, so don't resubmit. Barred, used or 0 HP cards make it faster. See
  [why it can take minutes](./docs/SOLVER.md#why-it-can-take-minutes).
- **Local and single-user.** The app runs on Flask's development server with no login and a
  built-in development secret key. Keep it on your own machine. To change the key, put
  `SECRET_KEY = "..."` in `instance/config.py`.
- **Back up `instance/wowscard.db`** before big changes. Updates to the app migrate the
  database automatically when it starts.
- **Some modifier effects are assumptions** that have not been confirmed in the game. They are
  listed in [docs/SOLVER.md](./docs/SOLVER.md#assumptions-not-yet-confirmed-in-game).
- **Several tabs:** maps are independent, but the modifier stock is shared. After another tab
  uses or changes modifiers, use **Reload counts** before saving the stock.

## Project layout

```
webapp/
  engine/solver.py    battle model, search, CSV loaders and the command-line entry point
  sandbox.py          maps, stages, key points, battles and the modifier stock
  maps.py, roster.py  web pages
  db.py, schema.sql   SQLite database and migrations
  templates/, static/ HTML and CSS
tests/                pytest suite; tests/data holds a complete CSV example
docs/                 game rules, CSV formats, solver overview
```

## License

[CC BY 2.0](./LICENSE). © 2026 Zobayer Hasan.
