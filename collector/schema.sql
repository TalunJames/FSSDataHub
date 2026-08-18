-- Collector tables. Applied on app startup; separate from the taxdb spine
-- so the CLI stays usable without the web stack.

CREATE TABLE IF NOT EXISTS collector_setting (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT
);

-- mode is deliberately unconstrained: the worker records burst, continuous,
-- schedule, manual_url, seed, plan, fetch, cog, and statutes runs, and the
-- list grows. A CHECK here once broke every bulk-adapter run.
CREATE TABLE IF NOT EXISTS crawl_run (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    mode                TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN (
                            'running','ok','stopped','failed')),
    provider            TEXT,
    filter_states       TEXT,
    items_claimed       INTEGER NOT NULL DEFAULT 0,
    pages_fetched       INTEGER NOT NULL DEFAULT 0,
    findings_written    INTEGER NOT NULL DEFAULT 0,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    message             TEXT
);

CREATE INDEX IF NOT EXISTS idx_cr_started ON crawl_run(started_at DESC);

CREATE TABLE IF NOT EXISTS crawl_page (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            INTEGER REFERENCES crawl_run(id),
    geoid             TEXT,
    category          TEXT,
    url               TEXT NOT NULL,
    final_url         TEXT,
    http_status       INTEGER,
    content_type      TEXT,
    sha256            TEXT,
    byte_size         INTEGER,
    archive_file_id   INTEGER,
    robots_allowed    INTEGER NOT NULL DEFAULT 1,
    title             TEXT,
    text_chars        INTEGER,
    error             TEXT,
    fetched_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cp_run ON crawl_page(run_id);
CREATE INDEX IF NOT EXISTS idx_cp_geoid ON crawl_page(geoid, category);
CREATE INDEX IF NOT EXISTS idx_cp_url ON crawl_page(url);

CREATE TABLE IF NOT EXISTS crawl_extract (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER REFERENCES crawl_run(id),
    geoid           TEXT,
    category        TEXT,
    provider        TEXT,
    model           TEXT,
    raw_response    TEXT,
    parsed_ok       INTEGER,
    error           TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ce_run ON crawl_extract(run_id);

CREATE TABLE IF NOT EXISTS intake_item (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    geoid               TEXT,
    category            TEXT,
    kind                TEXT NOT NULL CHECK (kind IN ('url','pdf','image')),
    url                 TEXT,
    filename            TEXT,
    store_path          TEXT,
    content_type        TEXT,
    sha256              TEXT,
    byte_size           INTEGER,
    note                TEXT,
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','running','ok','failed')),
    findings_written    INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    created_at          TEXT NOT NULL,
    finished_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_intake_status ON intake_item(status, id);
CREATE INDEX IF NOT EXISTS idx_intake_geoid ON intake_item(geoid, category);

-- Second-checker verdicts. One row per checked (geoid, category) pass;
-- 'flag' rows carry a JSON array of {code, instrument_code, reason}.
CREATE TABLE IF NOT EXISTS check_result (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER REFERENCES crawl_run(id),
    geoid           TEXT NOT NULL,
    category        TEXT NOT NULL,
    verdict         TEXT NOT NULL CHECK (verdict IN ('pass','flag','error')),
    flags           TEXT,
    provider        TEXT,
    model           TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_check_geo ON check_result(geoid, category, id);
CREATE INDEX IF NOT EXISTS idx_check_verdict ON check_result(verdict, id);

CREATE TABLE IF NOT EXISTS interview_answer (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    geoid           TEXT NOT NULL,
    category        TEXT NOT NULL,
    question_id     TEXT NOT NULL,
    action          TEXT NOT NULL CHECK (action IN (
                        'answered','skipped','unknown','skip_rest')),
    payload         TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ia_lookup
    ON interview_answer(geoid, category, created_at);
