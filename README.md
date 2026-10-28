WOWS Card Game Solver
=====================

An interactive solver for a 4-vs-4 card game: it plans which of your cards to send to each key
point of a map stage, and with which modifiers, to win as many battles as possible while
beating every must-win enemy and each key point's required wins. See the [game rules](./docs/GAME_RULES.md) and
[how the solver works](./docs/SOLVER.md).

It is a small Flask web app that tracks a whole map for you: HP and used cards.

## Features

- **Roster:** add, edit and remove cards, give them a type, and import or export them as
  `roster.csv`.
- **Maps:** each map works on its own copy of the roster, so HP and used cards are tracked per
  map. New roster cards can be synced in at any time.
- **Stages and key points:** enter up to 5 key points per stage, one at a time, each with
  its own required wins and battle conditions: % conditions, barred cards or card types,
  blocked modifier slots.
- **Solver:** suggests the best plan and a real alternative (one that assigns at least 3 cards
  differently); **Apply plan** records the battles, takes 1 HP from
  each card and marks the cards used.
- **Modifiers are not counted:** the solver assumes you can buy any modifier, and uses one
  only where it makes a battle (or a key point's requirements) a certain win.

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

1. **Roster:** import or enter your cards.
2. **New map** (top right) opens the map in a new tab with every card at 3 HP.
3. **Start a New Stage** and add the key points as they appear, each with its enemies,
   conditions and the wins it needs (if any).
   **If you cannot buy or use modifiers at a key point, tick No grey / No green modifiers
   there.** The solver then gets the best win chances from cards alone, and you can try the
   modifiers you do have by hand.
4. **Solve**, then **Apply plan** on the plan you play. Correct any battle that went
   differently by clicking its Won / Lost.
5. **Finish stage**: used marks clear and HP carries over into the next stage.

The roster CSV format is described in [docs/CSV_PARAMETERS.md](./docs/CSV_PARAMETERS.md).

## Tests

```
python -m pytest            # about a minute
```

Among them:
- `tests/test_search.py` checks the fast search against the plain exhaustive search on
  generated stages.
- `tests/test_csv_compat.py` builds the stage described by the CSV files in `tests/data`
  through the web app and checks that the solver gets the same inputs, and returns the same
  plans, as solving those files directly.

## Cautions

- **Solving usually takes a few seconds.** Hard stages may use up to a budget of about
  20 seconds; if the search had to stop before trying every option, the map page says so. The
  page waits until the solve finishes, so don't resubmit. See
  [how long a solve takes](./docs/SOLVER.md#how-long-a-solve-takes).
- **Local and single-user.** The app runs on Flask's development server with no login and a
  built-in development secret key. Keep it on your own machine. To change the key, put
  `SECRET_KEY = "..."` in `instance/config.py`.
- **Back up `instance/wowscard.db`** before big changes. Updates to the app migrate the
  database automatically when it starts.
- **Some modifier effects are assumptions** that have not been confirmed in the game. They are
  listed in [docs/SOLVER.md](./docs/SOLVER.md#assumptions-not-yet-confirmed-in-game).
- **Several tabs:** maps are independent, so each can be open in its own tab.

## Project layout

```
webapp/
  engine/model.py     battle model: scores a placement of cards at a key point
  engine/search.py    stage search (solve_stage)
  engine/solver.py    CSV readers and validate(); re-exports the engine
  sandbox.py          maps, stages, key points and battles
  maps.py, roster.py  web pages
  db.py, schema.sql   SQLite database and migrations
  templates/, static/ HTML and CSS
tests/                pytest suite; tests/data holds a complete CSV example
docs/                 game rules, CSV formats, solver overview
```

## License

[CC BY 2.0](./LICENSE). © 2026 Zobayer Hasan.
