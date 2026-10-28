-- Master roster: the cards you own. Map-scoped state (HP, used at a key point) lives in
-- per-map copies, not here.
CREATE TABLE IF NOT EXISTS card (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    NOT NULL UNIQUE,
    firepower           INTEGER NOT NULL CHECK (firepower >= 0),
    survivability       INTEGER NOT NULL CHECK (survivability >= 0),
    maneuverability     INTEGER NOT NULL CHECK (maneuverability >= 0),
    ability1_type       TEXT    CHECK (ability1_type IN ('buff', 'debuff')),
    ability1_target     TEXT,
    ability1_direction  TEXT,
    ability1_pct        INTEGER,
    ability2_type       TEXT    CHECK (ability2_type IN ('buff', 'debuff')),
    ability2_target     TEXT,
    ability2_direction  TEXT,
    ability2_pct        INTEGER,
    type                TEXT    NOT NULL DEFAULT ''   -- your own grouping, e.g. for bars
);

-- A map run. Its cards are a snapshot of the roster taken when the map starts, so later
-- roster edits do not change a map in progress.
CREATE TABLE IF NOT EXISTS map (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS map_card (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    map_id              INTEGER NOT NULL REFERENCES map(id) ON DELETE CASCADE,
    name                TEXT    NOT NULL,
    firepower           INTEGER NOT NULL,
    survivability       INTEGER NOT NULL,
    maneuverability     INTEGER NOT NULL,
    ability1_type       TEXT,
    ability1_target     TEXT,
    ability1_direction  TEXT,
    ability1_pct        INTEGER,
    ability2_type       TEXT,
    ability2_target     TEXT,
    ability2_direction  TEXT,
    ability2_pct        INTEGER,
    type                TEXT    NOT NULL DEFAULT '',
    hp                  INTEGER NOT NULL DEFAULT 3 CHECK (hp BETWEEN 0 AND 3),
    used                INTEGER NOT NULL DEFAULT 0,   -- fought in the current stage
    UNIQUE (map_id, name)
);

-- A key point's environment: conditions in percent (firepower .. debuff_power), barred card
-- names and types (JSON lists), and blocked modifier slots.
CREATE TABLE IF NOT EXISTS stage (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    map_id           INTEGER NOT NULL REFERENCES map(id) ON DELETE CASCADE,
    number           INTEGER NOT NULL,
    finished         INTEGER NOT NULL DEFAULT 0,
    solution         TEXT,                         -- JSON of the last solver run
    solution_stale   INTEGER NOT NULL DEFAULT 0,   -- inputs changed since that run
    UNIQUE (map_id, number)
);

CREATE TABLE IF NOT EXISTS keypoint (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_id         INTEGER NOT NULL REFERENCES stage(id) ON DELETE CASCADE,
    number           INTEGER NOT NULL,
    min_wins         INTEGER NOT NULL DEFAULT 0,
    firepower        INTEGER NOT NULL DEFAULT 0,
    survivability    INTEGER NOT NULL DEFAULT 0,
    maneuverability  INTEGER NOT NULL DEFAULT 0,
    enemy_power      INTEGER NOT NULL DEFAULT 0,
    buff_power       INTEGER NOT NULL DEFAULT 0,
    debuff_power     INTEGER NOT NULL DEFAULT 0,
    barred           TEXT    NOT NULL DEFAULT '[]',
    barred_types     TEXT    NOT NULL DEFAULT '[]',
    no_grey          INTEGER NOT NULL DEFAULT 0,
    no_green         INTEGER NOT NULL DEFAULT 0,
    played           INTEGER NOT NULL DEFAULT 0,
    UNIQUE (stage_id, number)
);

CREATE TABLE IF NOT EXISTS enemy (
    keypoint_id  INTEGER NOT NULL REFERENCES keypoint(id) ON DELETE CASCADE,
    slot         INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 4),
    power        INTEGER NOT NULL,
    vuln1        TEXT,
    vuln2        TEXT,
    must_win     INTEGER NOT NULL DEFAULT 0,
    boost        INTEGER NOT NULL DEFAULT 0,   -- the enemy's own power boost, %
    PRIMARY KEY (keypoint_id, slot)
);

-- One card's fight at a played key point. hp_before lets the battle be undone.
CREATE TABLE IF NOT EXISTS battle (
    keypoint_id  INTEGER NOT NULL REFERENCES keypoint(id) ON DELETE CASCADE,
    slot         INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 4),
    map_card_id  INTEGER NOT NULL REFERENCES map_card(id) ON DELETE CASCADE,
    grey_mod     TEXT,
    green_mod    TEXT,
    won          INTEGER NOT NULL DEFAULT 0,
    hp_before    INTEGER NOT NULL,
    PRIMARY KEY (keypoint_id, slot)
);
