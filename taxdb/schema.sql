-- Tax Database: unified schema.
-- 18 tables, 8 product views. Every substantive claim carries provenance.
-- SQLite. Apply as one file -- rate_change_event forward-references
-- ballot_measure, and a partial apply with PRAGMA foreign_keys=ON makes
-- every insert into rate_change_event fail.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================ L1 SPINE
-- Census GEOID is the join key for everything else.
-- state=2, county=5, place=7, mcd=10. School districts drop in later
-- on the same key without a migration.

CREATE TABLE IF NOT EXISTS jurisdiction (
    geoid           TEXT PRIMARY KEY,
    kind            TEXT NOT NULL
                    CHECK (kind IN ('state','county','place','mcd','school')),
    name            TEXT NOT NULL,
    state_usps      TEXT NOT NULL,
    state_fips      TEXT NOT NULL,
    county_fips     TEXT,
    parent_geoid    TEXT,
    lsad            TEXT,
    funcstat        TEXT,
    population      INTEGER,
    population_year INTEGER,
    land_sqmi       REAL,
    lat             REAL,
    lon             REAL,
    coterminous     INTEGER NOT NULL DEFAULT 0,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_juris_state ON jurisdiction(state_usps, kind);
CREATE INDEX IF NOT EXISTS idx_juris_pop   ON jurisdiction(population DESC);
CREATE INDEX IF NOT EXISTS idx_juris_parent ON jurisdiction(parent_geoid);

-- Census GID first-two-digits are sequential 01-51, not FIPS.
-- FIPS skips 03, 07, 14, 43, 52. AL/AK/AZ agree; everything after Arizona
-- silently shifts. Seeded on init. Unit-tested in tests/test_fips.py.
CREATE TABLE IF NOT EXISTS census_gid_crosswalk (
    fips_state      TEXT PRIMARY KEY,
    census_state    TEXT NOT NULL UNIQUE,
    usps            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL
);

-- ============================================================ L0 ARCHIVE / SOURCES

CREATE TABLE IF NOT EXISTS source (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_geoid     TEXT,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL,
    source_type     TEXT NOT NULL
                    CHECK (source_type IN (
                        'statute','agency_table','bulk_file',
                        'ordinance','portal','secondary')),
    authority_tier  INTEGER NOT NULL CHECK (authority_tier IN (1,2,3,4)),
    publisher       TEXT,
    verified        INTEGER NOT NULL DEFAULT 0,
    http_status     INTEGER,
    last_checked    TEXT,
    content_sha256  TEXT,
    content_changed INTEGER NOT NULL DEFAULT 0,
    notes           TEXT,
    UNIQUE(url, scope_geoid)
);

CREATE INDEX IF NOT EXISTS idx_source_scope ON source(scope_geoid);

-- Byte-level record of a fetch, so a finding survives the source site
-- being reorganized. Distinct from archive_file: this has no period label.
CREATE TABLE IF NOT EXISTS raw_document (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER REFERENCES source(id),
    url           TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    content_type  TEXT,
    byte_size     INTEGER,
    cache_path    TEXT,
    retrieved_at  TEXT NOT NULL,
    UNIQUE(sha256, url)
);

-- Local copy of Open US Law (or similar) statute text, for grep-then-read.
CREATE TABLE IF NOT EXISTS statute_section (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    state_usps          TEXT NOT NULL,
    snapshot            TEXT NOT NULL,
    citation            TEXT,
    section_title       TEXT,
    act_status          TEXT,
    text                TEXT,
    source_url          TEXT,
    last_amended_year   INTEGER,
    UNIQUE(state_usps, snapshot, citation)
);

CREATE INDEX IF NOT EXISTS idx_statute_state ON statute_section(state_usps);

-- Dated object store. Every rate file, every canvass PDF, every period,
-- forever. Deliberately NOT UNIQUE(sha256): a state that republishes a
-- byte-identical file in a quiet quarter must still be recordable, or
-- the diff engine cannot tell "unchanged" from "never fetched".
CREATE TABLE IF NOT EXISTS archive_file (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER REFERENCES source(id),
    adapter       TEXT NOT NULL,
    url           TEXT NOT NULL,
    period_label  TEXT NOT NULL,
    period_start  TEXT,
    period_end    TEXT,
    sha256        TEXT NOT NULL,
    byte_size     INTEGER,
    content_type  TEXT,
    store_path    TEXT NOT NULL,
    retrieved_at  TEXT NOT NULL,
    parsed_at     TEXT,
    parse_status  TEXT CHECK (parse_status IN ('pending','ok','failed','partial')
                              OR parse_status IS NULL),
    parse_note    TEXT,
    UNIQUE(adapter, period_label, url)
);

CREATE INDEX IF NOT EXISTS idx_af_sha    ON archive_file(sha256);
CREATE INDEX IF NOT EXISTS idx_af_period ON archive_file(adapter, period_label);

-- ============================================================ L2 RULES (per state)

CREATE TABLE IF NOT EXISTS state_profile (
    state_usps                  TEXT PRIMARY KEY,
    state_name                  TEXT NOT NULL,
    home_rule_doctrine          TEXT CHECK (home_rule_doctrine IN (
                                    'dillon','home_rule','mixed','unknown')
                                    OR home_rule_doctrine IS NULL),
    property_tax_limit_type     TEXT CHECK (property_tax_limit_type IN (
                                    'rate_cap','levy_growth_cap','assessment_cap',
                                    'combined','none','unknown')
                                    OR property_tax_limit_type IS NULL),
    property_tax_limit_summary  TEXT,
    local_sales_tax_allowed     TEXT,
    local_sales_tax_max         REAL,
    local_income_tax_allowed    TEXT,
    local_lodging_tax_allowed   TEXT,
    statute_root_url            TEXT,
    revenue_agency              TEXT,
    revenue_agency_url          TEXT,
    verified_by                 TEXT,
    verified_at                 TEXT,
    notes                       TEXT
);

-- What each state permits, at what cap, for each tax and jurisdiction kind.
CREATE TABLE IF NOT EXISTS authority_grant (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    state_usps         TEXT NOT NULL,
    jurisdiction_kind  TEXT,
    category           TEXT NOT NULL,
    instrument_code    TEXT NOT NULL,
    permitted          TEXT NOT NULL CHECK (permitted IN ('yes','no','conditional')),
    eligibility_note   TEXT,
    max_rate           REAL,
    max_rate_unit      TEXT,
    aggregate_cap_note TEXT,
    stacking_rule      TEXT,
    statute_cite       TEXT NOT NULL,
    source_id          INTEGER NOT NULL REFERENCES source(id),
    archive_file_id    INTEGER REFERENCES archive_file(id),
    extraction_method  TEXT NOT NULL CHECK (extraction_method IN (
                           'bulk_import','agent_research','manual','api')),
    effective_from     TEXT,
    effective_to       TEXT,
    confidence         TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
    verified_by        TEXT,
    verified_at        TEXT,
    notes              TEXT
);

CREATE INDEX IF NOT EXISTS idx_ag_lookup
    ON authority_grant(state_usps, instrument_code, jurisdiction_kind);

-- Dedup key for repeated research passes. Without it a nightly framework run
-- appends a fresh copy of the same cap every night and v_live_grant starts
-- picking winners by rowid. NULL kind and NULL effective_from are ordinary
-- values here, hence ifnull().
CREATE UNIQUE INDEX IF NOT EXISTS idx_ag_unique
    ON authority_grant(
        state_usps, ifnull(jurisdiction_kind, ''), category, instrument_code,
        ifnull(effective_from, ''));

-- Vote share a measure needs, and every structural constraint around
-- getting it on the ballot. Versioned: rules change (CA Upland 2017).
-- Human-verify 100% of this table.
CREATE TABLE IF NOT EXISTS threshold_rule (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    state_usps             TEXT NOT NULL,
    jurisdiction_kind      TEXT,
    measure_class          TEXT NOT NULL,
    instrument_code        TEXT,
    purpose_restriction    TEXT CHECK (purpose_restriction IN (
                               'general','special','either')
                               OR purpose_restriction IS NULL),
    threshold_value        REAL NOT NULL,
    threshold_basis        TEXT NOT NULL,
    threshold_note         TEXT,
    election_timing        TEXT,
    timing_note            TEXT,
    turnout_requirement    TEXT,
    sunset_required        TEXT,
    sunset_max_years       REAL,
    reimposition_allowed   TEXT,
    cooling_off_months     INTEGER,
    governing_body_vote    TEXT,
    petition_alternative   TEXT,
    ballot_language_rules  TEXT,
    statute_cite           TEXT NOT NULL,
    constitutional_cite    TEXT,
    effective_from         TEXT,
    effective_to           TEXT,
    source_id              INTEGER NOT NULL REFERENCES source(id),
    archive_file_id        INTEGER REFERENCES archive_file(id),
    extraction_method      TEXT NOT NULL CHECK (extraction_method IN (
                               'bulk_import','agent_research','manual','api')),
    verified_by            TEXT,
    confidence             TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
    verified_at            TEXT,
    notes                  TEXT
);

CREATE INDEX IF NOT EXISTS idx_thr_lookup
    ON threshold_rule(state_usps, measure_class, jurisdiction_kind);

-- Same reasoning as idx_ag_unique. purpose_restriction is part of the key:
-- general-purpose and special-purpose versions of one measure class carry
-- different thresholds in most states, and both are live at once.
CREATE UNIQUE INDEX IF NOT EXISTS idx_thr_unique
    ON threshold_rule(
        state_usps, ifnull(jurisdiction_kind, ''), measure_class,
        ifnull(instrument_code, ''), ifnull(purpose_restriction, ''),
        ifnull(effective_from, ''));

-- ============================================================ L3 FACTS (per jurisdiction)

-- One row per tax per jurisdiction. Covers levied, authorized-but-unused,
-- and barred outright. status is what makes an unused tax a finding
-- rather than a blank. Updates never overwrite: superseded_by points
-- at the replacement so rate history is preserved.
CREATE TABLE IF NOT EXISTS tax_instrument (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    geoid                    TEXT NOT NULL REFERENCES jurisdiction(geoid),
    category                 TEXT NOT NULL,
    instrument_code          TEXT NOT NULL,
    label                    TEXT,
    status                   TEXT NOT NULL CHECK (status IN (
                                 'levied','authorized_not_levied',
                                 'prohibited','repealed','unknown')),
    rate_value               REAL,
    rate_unit                TEXT,
    rate_basis               TEXT,
    cap_type                 TEXT,
    cap_value                REAL,
    cap_unit                 TEXT,
    cap_note                 TEXT,
    voter_approval_required  TEXT CHECK (voter_approval_required IN (
                                 'yes','no','conditional','unknown')
                                 OR voter_approval_required IS NULL),
    effective_date           TEXT,
    expiration_date          TEXT,
    fiscal_year              INTEGER,
    statute_cite             TEXT,
    source_id                INTEGER NOT NULL REFERENCES source(id),
    archive_file_id          INTEGER REFERENCES archive_file(id),
    confidence               TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
    extraction_method        TEXT NOT NULL CHECK (extraction_method IN (
                                 'bulk_import','agent_research','manual','api')),
    researcher               TEXT,
    verified_by              TEXT,
    verified_at              TEXT,
    retrieved_at             TEXT NOT NULL,
    superseded_by            INTEGER REFERENCES tax_instrument(id),
    notes                    TEXT,
    source_quote             TEXT
);

CREATE INDEX IF NOT EXISTS idx_ti_geoid    ON tax_instrument(geoid, category);
CREATE INDEX IF NOT EXISTS idx_ti_status   ON tax_instrument(status);
CREATE INDEX IF NOT EXISTS idx_ti_current  ON tax_instrument(superseded_by);
CREATE INDEX IF NOT EXISTS idx_ti_sunset   ON tax_instrument(expiration_date)
    WHERE superseded_by IS NULL AND status = 'levied';

CREATE UNIQUE INDEX IF NOT EXISTS idx_ti_live_unique
    ON tax_instrument(geoid, category, instrument_code)
    WHERE superseded_by IS NULL;

-- Synthesized by differencing consecutive archive_file periods.
-- How expired taxes are discovered in the 48 states that publish no sunset list.
-- expired vs abolished are kept distinct: one is a predecessor letting
-- revenue lapse, the other is voters killing it.
-- measure_id forward-references ballot_measure (created below).
CREATE TABLE IF NOT EXISTS rate_change_event (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    geoid             TEXT NOT NULL REFERENCES jurisdiction(geoid),
    category          TEXT NOT NULL,
    instrument_code   TEXT NOT NULL,
    change_type       TEXT NOT NULL CHECK (change_type IN (
                          'new','increase','decrease','expired','abolished',
                          'extended','renamed','boundary_change')),
    rate_before       REAL,
    rate_after        REAL,
    rate_unit         TEXT,
    effective_period  TEXT NOT NULL,
    detected_from     INTEGER REFERENCES archive_file(id),
    detected_against  INTEGER REFERENCES archive_file(id),
    measure_id        INTEGER,
    confidence        TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
    detected_at       TEXT NOT NULL,
    notes             TEXT,
    UNIQUE(geoid, category, instrument_code, change_type, effective_period)
);

CREATE INDEX IF NOT EXISTS idx_rce_geoid ON rate_change_event(geoid, category);
CREATE INDEX IF NOT EXISTS idx_rce_type  ON rate_change_event(change_type, effective_period);

-- ============================================================ L4 HISTORY

CREATE TABLE IF NOT EXISTS ballot_measure (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    geoid                 TEXT NOT NULL REFERENCES jurisdiction(geoid),
    election_date         TEXT NOT NULL,
    election_type         TEXT,
    measure_id_local      TEXT,
    official_title        TEXT,
    ballot_question       TEXT,
    full_text_url         TEXT,
    measure_class         TEXT NOT NULL,
    category              TEXT,
    instrument_code       TEXT,
    is_renewal            INTEGER,
    replaces_measure_id   INTEGER REFERENCES ballot_measure(id),
    rate_value            REAL,
    rate_unit             TEXT,
    rate_increment        REAL,
    principal_amount      REAL,
    duration_years        REAL,
    sunset_date           TEXT,
    purpose_type          TEXT,
    stated_purpose        TEXT,
    annual_revenue_est    REAL,
    oversight_provisions  TEXT,
    -- Denormalized on purpose: rules change, and you want what applied
    -- on THAT date, not today's matrix.
    threshold_required    REAL,
    threshold_basis       TEXT,
    threshold_rule_id     INTEGER REFERENCES threshold_rule(id),
    votes_yes             INTEGER,
    votes_no              INTEGER,
    votes_total           INTEGER,
    pct_yes               REAL,
    margin_vs_threshold   REAL,
    outcome               TEXT NOT NULL CHECK (outcome IN (
                              'passed','failed','withdrawn','pending',
                              'invalidated','unknown')),
    registered_voters     INTEGER,
    ballots_cast          INTEGER,
    turnout_pct           REAL,
    concurrent_measures   INTEGER,
    resulting_instrument_id INTEGER REFERENCES tax_instrument(id),
    resulting_change_id     INTEGER REFERENCES rate_change_event(id),
    source_id             INTEGER NOT NULL REFERENCES source(id),
    archive_file_id       INTEGER REFERENCES archive_file(id),
    confidence            TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
    extraction_method     TEXT NOT NULL CHECK (extraction_method IN (
                              'bulk_import','agent_research','manual','api')),
    researcher            TEXT,
    verified_by           TEXT,
    verified_at           TEXT,
    retrieved_at          TEXT NOT NULL,
    superseded_by         INTEGER REFERENCES ballot_measure(id),
    notes                 TEXT,
    UNIQUE(geoid, election_date, measure_id_local)
);

CREATE INDEX IF NOT EXISTS idx_bm_geoid   ON ballot_measure(geoid, election_date DESC);
CREATE INDEX IF NOT EXISTS idx_bm_date    ON ballot_measure(election_date DESC);
CREATE INDEX IF NOT EXISTS idx_bm_class   ON ballot_measure(measure_class, outcome);
CREATE INDEX IF NOT EXISTS idx_bm_margin  ON ballot_measure(margin_vs_threshold);

-- Close the forward FK now that ballot_measure exists.
-- SQLite cannot ALTER TABLE ADD CONSTRAINT; the FK is documented on
-- rate_change_event.measure_id and enforced in ingest/verify plus this
-- index used by the capture-gap view.
CREATE INDEX IF NOT EXISTS idx_rce_measure ON rate_change_event(measure_id);

CREATE TABLE IF NOT EXISTS measure_attempt_chain (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    geoid             TEXT NOT NULL REFERENCES jurisdiction(geoid),
    chain_key         TEXT NOT NULL,
    measure_id        INTEGER NOT NULL REFERENCES ballot_measure(id),
    attempt_number    INTEGER NOT NULL,
    months_since_prev INTEGER,
    changed_vs_prev   TEXT,
    notes             TEXT,
    UNIQUE(geoid, chain_key, attempt_number)
);

CREATE TABLE IF NOT EXISTS campaign_committee (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    measure_id      INTEGER NOT NULL REFERENCES ballot_measure(id),
    committee_name  TEXT NOT NULL,
    position        TEXT NOT NULL CHECK (position IN ('support','oppose','neutral')),
    filer_id        TEXT,
    total_raised    REAL,
    total_spent     REAL,
    top_donors      TEXT,
    consultants     TEXT,
    source_id       INTEGER NOT NULL REFERENCES source(id),
    retrieved_at    TEXT NOT NULL,
    notes           TEXT
);

-- ============================================================ L5 CAPACITY

CREATE TABLE IF NOT EXISTS revenue_base (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    geoid                 TEXT NOT NULL REFERENCES jurisdiction(geoid),
    fiscal_year           INTEGER NOT NULL,
    base_type             TEXT NOT NULL,
    base_value            REAL NOT NULL,
    base_unit             TEXT NOT NULL,
    is_estimated          INTEGER NOT NULL DEFAULT 0,
    estimation_method     TEXT,
    source_id             INTEGER NOT NULL REFERENCES source(id),
    archive_file_id       INTEGER REFERENCES archive_file(id),
    extraction_method     TEXT NOT NULL CHECK (extraction_method IN (
                              'bulk_import','agent_research','manual','api')),
    vintage               TEXT NOT NULL,
    is_current_vintage    INTEGER NOT NULL DEFAULT 1,
    confidence            TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
    verified_by           TEXT,
    verified_at           TEXT,
    retrieved_at          TEXT NOT NULL,
    notes                 TEXT,
    UNIQUE(geoid, fiscal_year, base_type, vintage)
);

CREATE TABLE IF NOT EXISTS yield_estimate (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    geoid               TEXT NOT NULL REFERENCES jurisdiction(geoid),
    category            TEXT NOT NULL,
    instrument_code     TEXT NOT NULL,
    increment_value     REAL NOT NULL,
    increment_unit      TEXT NOT NULL,
    annual_yield_usd    REAL NOT NULL,
    yield_per_capita    REAL,
    yield_low           REAL,
    yield_high          REAL,
    method              TEXT NOT NULL CHECK (method IN (
                            'official_estimate','base_x_rate',
                            'collections_scaled','peer_regression')),
    base_id             INTEGER REFERENCES revenue_base(id),
    fiscal_year         INTEGER,
    confidence          TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
    computed_at         TEXT NOT NULL,
    notes               TEXT
);

-- ============================================================ MULTI-SOURCE CLAIMS

-- Multiple sources per claim. Enforces the two-source rule for anything
-- that goes in front of a client. Keep source_id on each table as the
-- primary citation; use this for corroboration and conflicts.
CREATE TABLE IF NOT EXISTS claim_source (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_table     TEXT NOT NULL,
    claim_id        INTEGER NOT NULL,
    source_id       INTEGER NOT NULL REFERENCES source(id),
    archive_file_id INTEGER REFERENCES archive_file(id),
    role            TEXT NOT NULL CHECK (role IN (
                        'primary','corroborating','conflicting','superseded')),
    agrees          INTEGER,
    observed_value  TEXT,
    retrieved_at    TEXT NOT NULL,
    notes           TEXT,
    UNIQUE(claim_table, claim_id, source_id, role)
);

CREATE INDEX IF NOT EXISTS idx_cs_claim ON claim_source(claim_table, claim_id);

-- ============================================================ COVERAGE AND WORKFLOW

-- What we know and do not know, by place and date range.
-- The table that keeps a partial database from misleading anyone.
-- A Kansas city showing zero measures because Kansas is county-only
-- must never read the same as a Washington city that genuinely never tried.
CREATE TABLE IF NOT EXISTS coverage_assertion (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    domain             TEXT NOT NULL,
    scope_type         TEXT NOT NULL CHECK (scope_type IN ('state','county','place')),
    scope_geoid        TEXT NOT NULL,
    jurisdiction_kind  TEXT,
    period_start       TEXT NOT NULL,
    period_end         TEXT NOT NULL,
    completeness       TEXT NOT NULL CHECK (completeness IN (
                           'complete','substantial','partial','spot_checked','none')),
    basis              TEXT NOT NULL,
    known_exclusions   TEXT,
    measures_found     INTEGER,
    source_id          INTEGER REFERENCES source(id),
    asserted_by        TEXT NOT NULL,
    asserted_at        TEXT NOT NULL,
    expires_at         TEXT,
    notes              TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cov_unique
    ON coverage_assertion(
        domain, scope_type, scope_geoid,
        ifnull(jurisdiction_kind, ''), period_start, period_end);

CREATE INDEX IF NOT EXISTS idx_cov_scope ON coverage_assertion(domain, scope_geoid);

-- ~22,000 jurisdictions x 5 categories cannot be worked blind.
CREATE TABLE IF NOT EXISTS work_item (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    geoid         TEXT NOT NULL REFERENCES jurisdiction(geoid),
    category      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                      'pending','in_progress','needs_review',
                      'complete','no_data','blocked')),
    priority      INTEGER NOT NULL DEFAULT 0,
    batch         TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    claimed_at    TEXT,
    completed_at  TEXT,
    updated_at    TEXT,
    UNIQUE(geoid, category)
);

CREATE INDEX IF NOT EXISTS idx_work_queue ON work_item(status, priority DESC);
CREATE INDEX IF NOT EXISTS idx_work_batch ON work_item(batch);

CREATE TABLE IF NOT EXISTS run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    command     TEXT NOT NULL,
    args        TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    rows_in     INTEGER,
    rows_out    INTEGER,
    ok          INTEGER,
    message     TEXT
);

-- Revenue-measure class list used by product views.
-- LIKE 'tax%' would miss levy_override, de_bruce, assessment_district, bonds.
CREATE TABLE IF NOT EXISTS revenue_measure_class (
    measure_class TEXT PRIMARY KEY
);

INSERT OR IGNORE INTO revenue_measure_class VALUES
  ('tax_new'),('tax_increase'),('tax_extension'),('tax_increase_and_extension'),
  ('tax_renewal'),('bond_go'),('bond_revenue'),('levy_override'),('levy_renewal'),
  ('assessment_district'),('de_bruce'),('charter_fiscal');

-- ============================================================ PRODUCT VIEWS

CREATE VIEW IF NOT EXISTS v_current_tax AS
SELECT t.*, j.name AS jurisdiction_name, j.kind, j.state_usps, j.population,
       s.url AS source_url, s.name AS source_name, s.authority_tier
FROM tax_instrument t
JOIN jurisdiction j ON j.geoid = t.geoid
JOIN source s       ON s.id    = t.source_id
WHERE t.superseded_by IS NULL;

CREATE VIEW IF NOT EXISTS v_sunset_watch AS
SELECT j.state_usps, j.name, j.kind, j.population, j.geoid,
       t.category, t.instrument_code, t.rate_value, t.rate_unit,
       t.expiration_date,
       CAST(julianday(t.expiration_date) - julianday('now') AS INTEGER) AS days_out,
       (SELECT COUNT(*) FROM ballot_measure b
         WHERE b.geoid = t.geoid
           AND b.superseded_by IS NULL
           AND b.measure_class IN (SELECT measure_class FROM revenue_measure_class)
       ) AS prior_revenue_measures,
       (SELECT MAX(b.pct_yes) FROM ballot_measure b
         WHERE b.geoid = t.geoid AND b.outcome = 'passed'
           AND b.measure_class IN (SELECT measure_class FROM revenue_measure_class)
       ) AS best_prior_yes
FROM tax_instrument t
JOIN jurisdiction j ON j.geoid = t.geoid
WHERE t.superseded_by IS NULL
  AND t.status = 'levied'
  AND t.expiration_date IS NOT NULL
  AND julianday(t.expiration_date) IS NOT NULL
  AND julianday(t.expiration_date) - julianday('now') BETWEEN -180 AND 1095
ORDER BY days_out;

-- Exactly one live, most-specific grant per (state, kind, instrument).
CREATE VIEW IF NOT EXISTS v_live_grant AS
SELECT * FROM authority_grant a
WHERE a.permitted = 'yes'
  AND (a.effective_from IS NULL OR a.effective_from <= date('now'))
  AND (a.effective_to   IS NULL OR a.effective_to   >= date('now'))
  AND a.id = (
    SELECT a2.id FROM authority_grant a2
    WHERE a2.state_usps = a.state_usps
      AND a2.instrument_code = a.instrument_code
      AND a2.permitted = 'yes'
      AND (a2.jurisdiction_kind = a.jurisdiction_kind
           OR (a2.jurisdiction_kind IS NULL AND a.jurisdiction_kind IS NULL))
      AND (a2.effective_to IS NULL OR a2.effective_to >= date('now'))
    ORDER BY COALESCE(a2.effective_from,'0000') DESC, a2.id DESC
    LIMIT 1
  );

-- Exactly one live, most-specific threshold per
-- (state, kind, measure_class, purpose). Mirrors v_live_grant.
CREATE VIEW IF NOT EXISTS v_live_threshold AS
SELECT * FROM threshold_rule t
WHERE (t.effective_from IS NULL OR t.effective_from <= date('now'))
  AND (t.effective_to   IS NULL OR t.effective_to   >= date('now'))
  AND t.id = (
    SELECT t2.id FROM threshold_rule t2
    WHERE t2.state_usps = t.state_usps
      AND t2.measure_class = t.measure_class
      AND (t2.jurisdiction_kind = t.jurisdiction_kind
           OR (t2.jurisdiction_kind IS NULL AND t.jurisdiction_kind IS NULL))
      AND (t2.purpose_restriction = t.purpose_restriction
           OR (t2.purpose_restriction IS NULL AND t.purpose_restriction IS NULL))
      AND (t2.effective_to IS NULL OR t2.effective_to >= date('now'))
    ORDER BY COALESCE(t2.effective_from,'0000') DESC, t2.id DESC
    LIMIT 1
  );

CREATE VIEW IF NOT EXISTS v_headroom AS
SELECT j.geoid, j.name, j.state_usps, j.kind, j.population,
       g.id AS grant_id, g.category, g.instrument_code,
       g.max_rate, g.max_rate_unit,
       COALESCE(lev.levied_rate, 0)                  AS levied_rate,
       g.max_rate - COALESCE(lev.levied_rate, 0)     AS headroom,
       g.stacking_rule, g.statute_cite
FROM jurisdiction j
JOIN v_live_grant g
  ON g.state_usps = j.state_usps
 AND (g.jurisdiction_kind = j.kind
      OR (g.jurisdiction_kind IS NULL
          AND NOT EXISTS (SELECT 1 FROM v_live_grant g2
                          WHERE g2.state_usps = j.state_usps
                            AND g2.instrument_code = g.instrument_code
                            AND g2.jurisdiction_kind = j.kind)))
LEFT JOIN (
    SELECT geoid, instrument_code, SUM(rate_value) AS levied_rate
    FROM tax_instrument
    WHERE superseded_by IS NULL AND status = 'levied'
    GROUP BY geoid, instrument_code
) lev ON lev.geoid = j.geoid AND lev.instrument_code = g.instrument_code;

CREATE VIEW IF NOT EXISTS v_near_miss AS
SELECT j.state_usps, j.name, j.population, b.election_date, b.measure_id_local,
       b.measure_class, b.rate_value, b.rate_unit,
       b.pct_yes, b.threshold_required, b.margin_vs_threshold,
       b.stated_purpose, b.turnout_pct
FROM ballot_measure b
JOIN jurisdiction j ON j.geoid = b.geoid
WHERE b.outcome = 'failed'
  AND b.superseded_by IS NULL
  AND b.measure_class IN (SELECT measure_class FROM revenue_measure_class)
  AND b.margin_vs_threshold BETWEEN -8.0 AND 0.0
  AND b.election_date >= date('now', '-6 years')
ORDER BY b.margin_vs_threshold DESC;

CREATE VIEW IF NOT EXISTS v_measure_capture_gap AS
SELECT j.state_usps, j.name, j.geoid, j.population,
       e.category, e.instrument_code, e.change_type,
       e.rate_before, e.rate_after, e.effective_period
FROM rate_change_event e
JOIN jurisdiction j ON j.geoid = e.geoid
WHERE e.measure_id IS NULL
  AND e.change_type IN ('new','increase','extended')
  AND EXISTS (
      SELECT 1 FROM v_live_grant g
      WHERE g.state_usps = j.state_usps
        AND g.instrument_code = e.instrument_code
  )
ORDER BY j.state_usps, e.effective_period DESC;

CREATE VIEW IF NOT EXISTS v_coverage AS
SELECT j.state_usps, j.kind, w.category, w.status, COUNT(*) AS n,
       SUM(COALESCE(j.population, 0)) AS pop
FROM work_item w
JOIN jurisdiction j ON j.geoid = w.geoid
GROUP BY j.state_usps, j.kind, w.category, w.status;
