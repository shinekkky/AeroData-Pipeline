# AeroData Pipeline

### A Governed, Layered ETL and Data-Quality Framework for ASEAN–China Flight Operations

**Project Statement and Technical Report**

| | |
|---|---|
| **Project title** | AeroData Pipeline — Aviation Data Governance and a Layered (ODS/DWD/DWS) Warehouse ETL |
| **Candidate** | 杨开颜 · *Kaiyan Yang* (romanisation adjustable to the target institution's convention) |
| **Context** | Continuation of a data-collection internship at Wenge Technology (中科闻歌), a Chinese enterprise-AI company |
| **Upstream system** | `asean_flights.py` — a Playwright scraper covering 269 ASEAN–China routes, writing to `asean_flights_table` |
| **Role** | Sole designer and implementer: data model, dimensional design, ETL, data-quality rule engine, evaluation harness, test suite |
| **Stack** | Python ≥ 3.10 · pandas ≥ 2.0 · PyMySQL ≥ 1.1 · pytest ≥ 8 · SQLite / MySQL 8 |
| **Size** | 4,346 lines of production Python across 22 modules · 750 lines of tests (127 test cases) |
| **Artifacts** | Reproducible CLI, machine-readable + human-readable quality reports, versioned reference data, defect-coverage evaluation script |

> **Purpose of this document.** It is written to be submitted as a project write-up with
> applications to graduate programs in **Data Science** and **Computer Science**. It follows
> the structure of a technical report — motivation, method, evaluation, limitations — because
> admissions committees and technical reviewers read for the same three things: *what problem
> was real, what was actually built, and what was measured*. Every number quoted below was
> produced by running the committed code; the commands are given so that each claim can be
> re-verified in under a minute.

---

## Abstract

Airline operational data arrives from web scrapers in a state that is legible to a human and
useless to a query engine: one airport appears under four names, a flight number arrives as
`SQ 830`, `sq-830` or `SQ.830`, a departure time arrives as `8:5`, `25:00`, `--` or `23:50+1`,
and the same flight may be observed twice — once as *departed*, later as *arrived*. Computing
a delay rate directly on such records yields a number that is wrong in ways that cannot be
inspected, because the errors are silently absorbed.

**AeroData Pipeline** is a layered ETL and data-governance framework that sits *downstream* of
such a scraper and turns its output into an analysis-ready asset. It contributes three things.
First, a **layered warehouse** (ODS / DWD / DWS) with an explicitly versioned dimension layer,
in which standardisation and dimension lookup are separated: the cleaning layer decides what a
value *means*, the dimension layer decides what it *refers to*. Second, a **15-rule data-quality
engine** with two scopes (row-level consistency and batch-level duplication) that leaves a
permanent, queryable trail — rule code, severity, field, raw value, normalised value, remediation
note — for every defect it cannot fix. Third, an **evaluation harness** that measures, defect
class by defect class, against the eleven classes of defect injected into a deterministic
simulator, what the pipeline detects, what it silently absorbs, and what it misses.

On a 14-day / 269-route workload (7,626 source rows) the pipeline completes in 3.3 s, produces
7,511 standardised flight records and 1,307 daily + 3,745 route-level aggregates, and is **bit-for-bit
idempotent**: a second run inserts nothing, updates all 7,511 fact rows, and reproduces the
aggregates exactly. Of 469 injected defects, 380 (81.0%) are detected and logged, 87 (18.6%) are
correctly absorbed by normalisation or dimension imputation, and 2 (0.4%) are missed — a gap this
document names explicitly rather than conceals. The rule engine additionally raises 9 findings on
records that carried **no** injected defect, demonstrating that it is not merely echoing the
injector.

The system is production-shaped but not yet production-proven: the MySQL backend is complete but
unverified against a live instance, and the default source is a simulator whose flight-level
records are synthetic. Both limitations are stated in full in §10.

**Keywords:** data engineering · ETL · dimensional modelling · data quality · referential
integrity · schema matching · idempotency · reproducibility · aviation operations research

---

## 1. Motivation and Problem Statement

### 1.1 The setting

At Wenge Technology I worked on the downstream half of an aviation data-collection product. The
upstream scraper (`asean_flights.py`) is a Playwright crawler that visits flight-tracking sites
for **269 scheduled routes between ASEAN countries and China**, and writes one row per observed
flight into a MySQL table, `asean_flights_table`. The scraper is not the interesting problem. The
interesting problem is what happens *after* it: the collected rows are the raw material for
delay analytics, punctuality reporting, and route-level performance reviews, and in their
delivered form they are not safe to compute anything from.

The following are not hypothetical. They are the actual values found in the collector's output.

| # | Defect class | Example as collected | Why it matters downstream |
|---|---|---|---|
| D1 | Airport naming | `沙巴` and `斗湖` are the same airport (Kota Kinabalu) | Route-level metrics split across two names |
| D2 | Airport naming | `上海浦东` vs `上海浦东国际机场` | Joins to the dimension table fail |
| D3 | **Master-data error** | The route catalogue gives 斗湖 the IATA code `TWU` — which is *Tawau's* code | Two different airports silently share one key |
| D4 | Master-data gap | 13 airports in the catalogue carry no IATA code at all | Referencing impossible |
| D5 | Flight number format | `SQ 830` / `sq-830` / `SQ.830` | Carrier prefix cannot be parsed |
| D6 | Time format | `8:5`, `25:00`, `08:60`, `abc`, `--`, `23:50+1` | Delay arithmetic crashes, or (in a naive query) silently coerces to zero |
| D7 | Missing values | Empty flight number, empty carrier, empty actual arrival | Denominator of every rate metric shifts |
| D8 | Duplicate observation | The same flight collected twice, byte-identical | Double-counted |
| D9 | **State progression** | The same flight collected twice, *status advanced* | Double-counted, or overwritten with the wrong observation |
| D10 | Status vocabulary | `晚点` / `已起飞` / `备降` / `返航` | Not machine-comparable |

Defect D3 is the most instructive: it is not a defect in the *observations* at all. It is a defect
in the **reference data** — the route catalogue that the scraper itself reads. Fixing it in the
ETL layer would be fixing it in the wrong place, and would leave the same error live in the
catalogue. This observation drove the central architectural decision of the project:

> **Record-level defects and reference-data defects need different governance mechanisms.**
> Record-level defects are properties of individual observations and are handled by a
> *rule engine* that fires per row and leaves a trail. Reference-data defects are properties of
> the master data and are handled by a *versioned, reviewable override file* that is applied
> once, at dimension-build time, where it can be inspected, diffed, and blamed.

### 1.2 Why the obvious approach fails

The naive alternative — clean inside the query, or clean with a handful of `CASE` expressions —
fails for a specific and well-understood reason: **it merges two different responsibilities**.
A query that both repairs a value and computes on it cannot answer the question "how much of
this table is trustworthy?" It also cannot answer "what did the repair change?" Both questions
are the ones an aviation analyst, an auditor, or a downstream ML team must be able to answer.

The design principle adopted here is stated once and enforced throughout the codebase:

> **The cleaning layer is responsible for making the data *correct*; the quality layer is
> responsible for *recording* what could not be made correct.** Two separate responsibilities,
> so that the data is usable downstream *and* the problems are not lost.

### 1.3 Scope and non-goals

| In scope | Out of scope (and why) |
|---|---|
| Downstream governance of a scraper's output | Replacing or improving the scraper (upstream of this project) |
| A dimensional model for flight operations | General-purpose ETL framework (the layer contract is the product) |
| A rule-based, auditable quality engine | Learned anomaly detection — named as future work in §11 |
| Idempotent, restartable, incremental batch processing | Streaming / sub-second latency (the workload is daily) |
| A deterministic, instrumented testbed | Any claim about real-world flight punctuality (§10) |

---

## 2. Objectives and Research Questions

The project was designed against four questions. Each has a corresponding evaluation in §7.

| | Research question | Success criterion |
|---|---|---|
| **RQ1** | Can heterogeneous collection sources be fed into one governed warehouse without changing a single line of downstream logic? | One `FlightSource` interface; three implementations (MySQL / CSV / simulator); zero changes to cleaning, quality, or aggregation logic when the source changes |
| **RQ2** | Does the pipeline guarantee *replay safety* — can the same batch be run twice without corrupting state? | Second run: zero fact insertions, all rows updated, **identical** aggregate values and row counts |
| **RQ3** | Is the quality engine *sensitive* (does it find defects) and *specific* (does it avoid crying wolf)? | Per-defect-class detection rates measured against an injected ground truth; false-positive rate on defect-free records reported |
| **RQ4** | Can a pipeline be built that a reviewer can trust without running it? | Every defect carries a rule code, severity, raw value, normalised value and remediation note; every run carries a batch log; every aggregate is reproducible from the fact table |

**RQ3** deserves emphasis. "The pipeline reports anomalies" is not evidence. The evaluation
in §7.5 therefore reports, for each of the eleven injected defect classes, three outcomes
(detected / absorbed / missed), and separately reports findings raised on records that carried
*no* injected defect. The latter is the check against a circular evaluation: if the rule engine
only echoed the injector, the number of spontaneous findings would be zero.

---

## 3. Data Context and Provenance

### 3.1 The upstream collector

| Property | Value |
|---|---|
| Component | `asean_flights.py` (Playwright scraper, written during the internship) |
| Output table | `asean_flights_table` (MySQL) |
| Route coverage | 269 routes, ASEAN → China, 17 countries, 86 airports |
| Row schema | 15 business fields: `flight_no`, `airline`, `dep_country`, `dep_city`, `dep_airport`, `dep_plan`, `dep_actual`, `arr_country`, `arr_city`, `arr_province`, `arr_airport`, `arr_plan`, `arr_actual`, `status`, `flight_date` (plus collector-side `md5`, `insert_time`, `jky_clear_flag`, which are ignored on read) |

### 3.2 The route catalogue as reference data

The 269-route catalogue is not only a scraper input; it is the **seed of the entire dimension
layer**. From it the pipeline derives airports (86), airport aliases (89), routes (268 — the 269-route
catalogue yields 268 once the corrected duplicate route of §3.3 is collapsed) and airline
reference data (54 carriers) — all from version-controlled files under `data/reference/`, never
from SQL constants:

| File | Rows | Role |
|---|---|---|
| `routes_daily.json` | 269 | Route catalogue → airport and route dimensions |
| `airlines.csv` | 54 | Carrier codes (IATA/ICAO) ↔ Chinese names ↔ registration country |
| `airport_overrides.csv` | 8 | **Human-reviewed corrections** to the catalogue (7 missing codes, 1 wrong code) |
| `airport_aliases.csv` | 3 | Manually curated alias → canonical code (e.g. `曼谷素万那普` → `BKK`) |
| `countries.csv` | 25 | Country name ↔ ISO-3166 alpha-2, used for cross-checking record-level country fields |

### 3.3 The master-data defect, and what fixing it actually required

The catalogue's entry for 斗湖 (Kota Kinabalu) carries the IATA code `TWU`, while 塔瓦乌 (Tawau)
carries no code at all. `TWU` is Tawau's code. A one-line correction in `airport_overrides.csv`
(`斗湖 → BKI`) resolves *both* defects at once:

- 斗湖 is re-keyed to `BKI`, which is the code the rest of the catalogue already uses for
  沙巴 — the same airport under its other name. The dimension builder therefore merges them:
  `dim_airport` holds one row (`BKI`, 沙巴) and `斗湖` becomes a **merged alias** pointing at it.
- 塔瓦乌, previously code-less, is given its correct code `TWU` by the same override table.
- **A third consequence is visible only after the correction.** The catalogue lists both
  `沙巴 → 上海浦东` (coded `BKI → PVG`) and `斗湖 → 上海浦东` (coded `TWU → PVG`) — two
  "different" routes to the same destination. Once 斗湖 is re-keyed to `BKI`, the two become
  the identical pair `BKI → PVG` and collapse into a single `dim_route` row. This is why the
  dimension holds **268** routes against the catalogue's 269. The correction therefore does not
  merely fix a key: it reveals that the catalogue was counting one physical route twice, which
  would have split the same traffic across two route keys and understated the per-route volume
  of both.

The net effect is that the pipeline resolves a two-airport, two-error master-data ambiguity
through a *single reviewed edit*, and — crucially — the edit is visible in a diff rather than
buried in a `CASE` expression. Two further classes of dimension conflict are detected and
**reported loudly** rather than silently absorbed: `missing_code` (an airport that no file
resolves, which receives the placeholder `UNK`) and `dangling_alias` (an alias pointing at a
code that does not exist in `dim_airport`).

### 3.4 The deterministic simulator as an instrumented testbed

Because the collector's own network egress is blocked in the development environment (§10), the
project needed a data source that is (a) offline, (b) deterministic, and (c) *instrumentable* —
the last property being what makes the evaluation in §7.5 possible at all.

`SimulatorSource` synthesises flight-level records from the **real** 269-route catalogue. All
randomness is derived from `SHA-256(key)` rather than a PRNG, so a given `seed` always yields a
byte-identical dataset — a stronger guarantee than a seeded PRNG, and one that survives
interpreter or library-version changes.

The simulator models operational outcomes rather than uniform noise: 55% on-time, 21% delayed,
7% departed, 6% early, 5% cancelled, 4.5% mass-disruption (3–6 h), 1.5% extreme (10–25 h),
plus 25% of last-day flights still in the *planned* state. A configurable defect rate
(`defect_rate = 0.06`) injects one of eleven defect classes, chosen by a deterministic draw:

`missing_flight_no` · `missing_airline` · `bad_plan_time` · `bad_actual_time` ·
`dirty_airport_name` · `unknown_airport` · `unknown_airline` · `arrival_before_departure` ·
`missing_country` · `exact_duplicate` (batch-level) · `re_observation` (batch-level)

Two batch-level behaviours are injected separately, because they exercise *different* identity
mechanisms and are the usual source of double counting:

- **exact duplicate** (1.2% of flights): the row is repeated byte-for-byte — the ODS
  row-hash must absorb it;
- **re-observation** (1.0% of flights, applied only to flights whose status is not yet
  terminal): the same flight is emitted again with its status advanced (`已起飞` → `已到达`) —
  the DWD business key must absorb it as an *update*, not as an insertion.

---

## 4. System Architecture

### 4.1 Layered warehouse

```
   Multi-source ingestion          ODS (raw)               DWD (clean detail)         DWS (serving)
 ┌──────────────────────┐        ┌──────────────────┐      ┌───────────────────┐     ┌────────────────────┐
 │  mysql  — live table │        │  ods_flight      │      │  dwd_flight       │     │ dws_flight_daily   │
 │  csv    — exported   │──────► │  verbatim load   │─────►│  standardise      │────►│  date×carrier×apt  │
 │          files       │        │  row_hash → idem.│ clean│  + dimension join │ agg │ dws_route_daily    │
 │  simulator—determin. │        │  (no processing) │      │  + delay maths    │     │  date×route        │
 └──────────────────────┘        └──────────────────┘      └───────────────────┘     └────────────────────┘
                                                                                          │
 ┌──────────────────────┐        ┌──────────────────┐      ┌───────────────────┐     ┌────────────────────┐
 │  dim_airport         │◄──────►│  dq_flight_      │◄─────│  15 quality rules │     │  quality report    │
 │  dim_airport_alias   │ refer. │  anomaly         │ rule │  (14 row + 1 batch)│     │  JSON + Markdown   │
 │  dim_airline         │ integ. └──────────────────┘      └───────────────────┘     └────────────────────┘
 │  dim_route           │                                                              ┌────────────────────┐
 └──────────────────────┘                                                              │  etl_batch_log     │
        ▲                                                                             │  etl_watermark     │
        │  version-controlled reference files + human-reviewed overrides            └────────────────────┘
 ┌────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │  data/reference/  routes_daily.json · airlines.csv · airport_overrides.csv · airport_aliases.csv     │
 │                  countries.csv                                                                       │
 └────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

The two arrows that carry the design are:

- **ODS → DWD is a value-level transformation; DWD → DWS is a set-level recomputation.**
  Aggregates are never accumulated from a batch. They are recomputed for each affected date
  from the *entire* DWD fact table (§5.6). This single choice is what makes the pipeline
  replay-safe (RQ2).
- **The dimension layer is read-only input to both DWD and the rule engine.** Reference
  integrity is enforced by joining to a dimension, not by pattern-matching strings at query
  time.

### 4.2 The source abstraction (answering RQ1)

```python
class FlightSource(ABC):
    name: str
    @abstractmethod
    def fetch(self, since: date | None = None) -> SourceBatch: ...
```

One interface, three implementations:

| Source | Scenario | Status |
|---|---|---|
| `mysql` | Direct read of the collector's `asean_flights_table` | Code complete; **not yet verified against a live instance** (network egress blocked — §10) |
| `csv` | CSV/JSON files exported by the collector | Code complete; sample files included |
| `simulator` | Deterministic synthesis over the real route catalogue | Default; fully validated |

`fetch(since=…)` is the entire incremental contract. The simulator honours it by shifting its
window; the CSV source honours it by filtering on `flight_date`; the MySQL source honours it
with `WHERE flight_date >= %s`. Nothing downstream knows which source it is reading. **This is
the concrete answer to RQ1: swapping the source is a command-line flag, not a refactor.**

### 4.3 Physical data model

Eleven tables are defined **once**, as Python dataclasses, and rendered to DDL for two dialects
(§4.4).

| Table | Grain | Purpose |
|---|---|---|
| `dim_airport` | one airport | IATA code, Chinese/English names, city, country, ISO code, hub flag |
| `dim_airport_alias` | one alias | Alias → canonical airport code; the mechanism that absorbs D1/D2 |
| `dim_airline` | one carrier | Code, Chinese/English names, registration country |
| `dim_route` | one route | Origin–destination pair, international flag |
| `ods_flight` | one collected record | Verbatim landing + `row_hash` (unique) + batch id |
| `dwd_flight` | one flight | Standardised, dimension-joined, delay-computed; `flight_id` = business key |
| `dws_flight_daily` | date × carrier × departure airport | Volume, punctuality rate, delay mean/max, cancellation count |
| `dws_route_daily` | date × route | Route-level volume and mean delay |
| `dq_flight_anomaly` | one defect occurrence | Rule code, severity, field, raw value, normalised value, message |
| `etl_batch_log` | one batch | Source, counts, duration, status, error text |
| `etl_watermark` | one source | Last processed flight date, cumulative rows |

`dwd_flight` is the heart of the model: 30 columns covering identity, dimension references,
four timestamps, two delay measures, a delay class, a standardised status, a duration, and a
`has_anomaly` flag that lets a query filter to trustworthy records without joining the anomaly
table.

### 4.4 Dialect portability

Both backends share one schema definition. The renderer uses only type names both engines
accept (`VARCHAR`, `DATETIME`, `DECIMAL`, `INT`), and diverges in exactly three places:
auto-increment syntax, timestamp defaults, and the table-options clause. All timestamps are
written from the Python side, never by a database default — this removes the entire class of
"timezone/default divergence" bugs between SQLite and MySQL.

The engine wrapper (`aerodata/db/engine.py`, 228 lines) hides the remaining differences behind
`upsert`, `insert_ignore`, `fetch_all`, `count`, and a dialect-aware `?`-placeholder
translation.

---

## 5. Methodology

### 5.1 Standardisation (the value-level layer)

All standardisation lives in `aerodata/etl/standards.py` (397 lines), which is deliberately
free of I/O so that every rule is a pure function and directly unit-testable.

**Text.** Full-width → half-width (U+FF01–FF5E), whitespace trimming and collapsing. A single
`MISSING_TOKENS` set (`""`, `-`, `--`, `---`, `/`, `n/a`, `na`, `null`, `none`, `暂无`, `无`,
`未知`, `待定`, `—`) defines what the collector means by "no value", so that a missing value is
never confused with the literal string `"未知"`.

**Flight number.** `"SQ 830" → "SQ830"`: full-width conversion, upper-casing, and removal of
whitespace, hyphens, underscores, dots, slashes and CJK punctuation.

**Airport key.** Both the dimension and the raw value pass through the *same* function
`normalize_airport_key`, which strips separators and removes the suffixes `国际机场`, `国际`,
`航站楼`, `机场`. Because the key is symmetric, `上海浦东国际机场` and `上海浦东` collapse to the
same key — the defect is resolved structurally, not by a hand-written synonym list.

**Airline key.** Separators removed, corporate-form suffixes stripped
(`股份有限公司|有限责任公司|有限公司|Co.,? ?Ltd\.?|Ltd\.?|Inc\.?`), so `泰国亚洲航空有限公司`
and `泰国亚洲航空` match.

**Time.** A tiered parser handling seven full datetime formats, `MM-DD HH:MM` (year inferred,
rolled forward across a year boundary), `HH:MM`, `HH:MM+N` (next-day), `次日HH:MM`, and
`翌日HH:MM`; explanatory noise such as `当地时间`, `约`, `预计`, `实际` is stripped first.
Critically, the parser returns a *typed result* rather than a bare value:

```python
@dataclass(frozen=True)
class TimeParseResult:
    value: datetime | None = None
    error: str | None = None   # missing / invalid_format / invalid_range / invalid_date
```

`8:5` is parsed to 08:05 **and** tagged `invalid_format`; `25:00` is tagged `invalid_range` and
yields no value. The distinction survives all the way into the anomaly table, where the same
string appears once with a repaired normalised value and once without. This is the mechanism
that prevents a single problem from being counted by several rules simultaneously.

**Status.** Ten canonical statuses (`PLANNED, BOARDING, DEPARTED, ARRIVED, DELAYED, EARLY,
CANCELLED, DIVERTED, RETURNED, UNKNOWN`) resolved by ordered keyword patterns over the Chinese
and English source vocabulary. Ordering matters: `取消航班` must resolve to `CANCELLED` before
`已到达`-style patterns are considered, and `已到达` must be tested before `已起飞`.

### 5.2 Referential integrity via the dimension layer

`FlightTransformer` resolves every airport, carrier and route through an in-memory
`DimensionIndex` loaded once per batch (one query per dimension table, then dictionary lookups):

```
raw airport name ──normalize_airport_key──► alias table ──► dim_airport ──► airport_code
                  └──(miss)────────────────► dim_airport  ──► airport_code | "" + UNKNOWN_AIRPORT
```

Country is **derived from the dimension** whenever the airport matches, and only taken from the
raw record when it does not — which means a blank country in the source is silently repaired by
referential imputation (§7.5 measures this explicitly, and it is also a coverage gap).

### 5.3 Derived measures

- **Delay.** `delay_min = round((actual − scheduled) / 60)`; `None` when either side is absent,
  so that "no information" never masquerades as "zero delay".
- **Duration.** Computed from *scheduled* times, with a midnight-crossing correction
  (`scheduled_arr < scheduled_dep → + 24 h`), and a `crossed_midnight` flag.
- **Delay class.** `DELAYED` if ≥ 15 min, `EARLY` if ≤ −5 min, `CANCELLED` if the flight was
  cancelled, `UNKNOWN` if no actual time exists, otherwise `ON_TIME`. Both thresholds are
  configuration, not code constants, because the choice of 15 minutes is a *business* decision
  (and a contested one in aviation practice).

### 5.4 Identity, idempotency, and update semantics (answering RQ2)

Two different identities, solving two different problems:

| Identity | Definition | Solves | Mechanism |
|---|---|---|---|
| `row_hash` | `SHA-256` over 15 normalised raw fields | The *same observation* collected twice | `UNIQUE` index on `ods_flight.row_hash`; the second occurrence is skipped |
| `flight_id` | `SHA-256(flight_no ‖ date ‖ dep ‖ arr ‖ scheduled_dep)[:32]` | The *same flight* observed in different states | `PRIMARY KEY` on `dwd_flight.flight_id`; a later observation becomes an `UPDATE` |

The distinction is the crux of the project. A byte-identical duplicate is a *collection*
artefact and must be dropped. A re-observation with an advanced status is a *state transition*
and must overwrite. A single content hash cannot express both; a business key plus a content
hash can, and does. Within a batch, the last observation of a `flight_id` wins, and the
batch-level duplicate rule is evaluated **before** de-duplication so that the report reflects
what the source actually delivered rather than what survived.

### 5.5 Incremental processing

`etl_watermark` stores, per source, the maximum flight date already processed. `--incremental`
reads it and requests only newer data. The watermark is written **after** a successful batch,
so a failed batch is simply re-run in full on the next attempt — at-least-once delivery at the
batch level, with exactly-once *state* guaranteed by the two identities above.

### 5.6 Aggregate recomputation strategy

> Aggregates are **not** computed from the current batch. For each date touched by the batch,
> the pipeline re-reads the *entire* DWD fact table for that date and recomputes the aggregate
> from scratch, then upserts on the grain's unique key.

This is the single most important design decision for correctness, and it is what makes the
pipeline safe under *partial* incremental batches: a batch that touches only 10 % of a day's
flights still produces a complete and correct aggregate for that day, because the other 90 %
are read back from DWD. The alternative — accumulating batch-level subtotals — is the classic
source of "the historical metric changed and nobody knows why".

### 5.7 The data-quality rule engine

Fifteen rules, two scopes, three severities. Every rule has a stable code, a Chinese and
English label, a severity, and a scope.

| # | Rule code | Scope | Severity | Trigger | Records |
|---|---|---|---|---|---|
| 1 | `MISSING_REQUIRED_FIELD` | row | ERROR | Flight number / carrier / flight date empty | 74 |
| 2 | `INVALID_FLIGHT_NO` | row | ERROR | Flight number violates `<prefix><1–4 digits>` | 0 |
| 3 | `INVALID_TIME_FORMAT` | row | WARN | Time parseable but non-canonical (`8:5`) | 36 |
| 4 | `INVALID_TIME_VALUE` | row | ERROR | Time unparseable as a legal clock time (`25:00`) | 21 |
| 5 | `MISSING_TIME` | row | WARN | **Scheduled** time missing (affects delay metrics) | 0 |
| 6 | `UNKNOWN_AIRPORT` | row | ERROR | Airport not resolvable to `dim_airport` | 46 |
| 7 | `UNKNOWN_AIRLINE` | row | WARN | Carrier name not resolvable to `dim_airline` | 68 |
| 8 | `UNKNOWN_AIRLINE_PREFIX` | row | WARN | Flight-number prefix is not a known carrier | 0 |
| 9 | `MISSING_ACTUAL_TIME` | row | WARN | Status is departed/arrived but the actual time is absent | 14 |
| 10 | `STATUS_CONTRADICTION` | row | WARN | Cancelled flight carries actual times | 0 |
| 11 | `INVALID_FLIGHT_DURATION` | row | ERROR | Non-positive, or above the 1,200 min configurable bound | 46 |
| 12 | `ARRIVAL_BEFORE_DEPARTURE` | row | ERROR | Actual arrival precedes actual departure | 43 |
| 13 | `DELAY_OUT_OF_RANGE` | row | WARN | Delay magnitude exceeds 24 h | 8 |
| 14 | `COUNTRY_MISMATCH` | row | WARN | Record country contradicts the dimension-derived country | 0 |
| 15 | `DUPLICATE_FLIGHT` | **batch** | WARN | Same `flight_id` appears more than once in a batch | 115 |

Two design decisions in this table are worth defending:

**(a) Syntax and reference integrity are separated.** The flight-number rule checks only
*structure* — a two-or-three-character carrier prefix followed by one to four digits. It accepts
`K6` (Cambodia Angkor Air), `3U` (Sichuan Airlines) and `D7` (AirAsia X), all of which are real
carriers whose codes contain digits. Whether the prefix *denotes a real carrier* is a different
question, and it is answered by rule 8 against `dim_airline`. A single fat rule would have
produced false positives on legitimate carriers; two thin rules do not. The same principle
separates `UNKNOWN_AIRPORT` (reference) from `INVALID_TIME_FORMAT` (syntax).

**(b) A rule fires on the *fact*, not on the symptom.** `MISSING_TIME` deliberately covers
only *scheduled* times: a cancelled flight has no actual time, and that is not a defect.
Reporting it as one would inflate the anomaly count and train reviewers to ignore the table.

Each anomaly is persisted with a unique key on
`(batch_id, rule_code, flight_id, field_name, raw_value)`, so re-running the same batch under
the same batch id cannot duplicate findings.

### 5.8 Observability

- **`etl_batch_log`** — one row per batch: source, status (`RUNNING/SUCCESS/FAILED`), source
  row count, ODS new/skipped, DWD insert/update/dedup, anomaly count, duration, error text.
  A failed batch leaves its error message behind and re-raises.
- **Quality report** — a machine-readable JSON document and a human-readable Markdown
  document, containing batch overview, severity and rule distributions, sampled anomalies with
  raw and normalised values, operational metrics (punctuality, delay, cancellation), top-N lists
  by carrier, airport and route, and table row counts.

---

## 6. Implementation

### 6.1 Module map

| Module | Lines | Responsibility |
|---|---|---|
| `aerodata/db/schema.py` | 424 | All 11 table definitions; DDL rendering for two dialects |
| `aerodata/dimensions.py` | 439 | Reference-data loading; airport/alias/airline/route construction; conflict reporting |
| `aerodata/etl/standards.py` | 397 | Pure standardisation functions and typed parse results |
| `aerodata/etl/cleaning.py` | 362 | ODS record → DWD row + anomalies; `DimensionIndex` |
| `aerodata/etl/pipeline.py` | 308 | Orchestration: watermark → ODS → DWD → rules → DWS → batch log |
| `aerodata/quality/rules.py` | 409 | The 15 rules; row and batch checkers |
| `aerodata/quality/report.py` | 425 | Report construction; JSON + Markdown rendering |
| `aerodata/layers/dws.py` | 238 | Date-level and route-level recomputation (pandas) |
| `aerodata/layers/ods.py` | 93 | Verbatim landing with row-hash idempotency |
| `aerodata/db/engine.py` | 228 | Dialect-agnostic DB wrapper (`upsert`, `insert_ignore`, …) |
| `aerodata/cli.py` | 243 | `init-db`, `run`, `report`, `stats`, `query`, `reset-db` |
| `aerodata/config.py` | 172 | Config file + environment-variable overrides; secret redaction |
| `aerodata/sources/*.py` | 550 | `base` (interface), `mysql_collector`, `csv_files`, `simulator` |
| **Production total** | **4,346** | 22 modules |
| `tests/` | 750 | 127 test cases across 4 files |
| `scripts/` | 192 | `export_sample.py` (sample export), `_probe_db.py` (read-only DB probe) |
| `scripts/eval_defect_coverage.py` | 185 | Defect-class detection evaluation (§7.5) |

### 6.2 Command-line interface

```bash
python -m aerodata.cli --config config/local.json init-db      # create schema + load dimensions
python -m aerodata.cli --config config/local.json run --source simulator
python -m aerodata.cli --config config/local.json run --source mysql \
    --settings-file /path/to/db_setting.json --table asean_flights_table
python -m aerodata.cli --config config/local.json run --source csv --path data/sample
python -m aerodata.cli --config config/local.json run --incremental
python -m aerodata.cli --config config/local.json stats
python -m aerodata.cli --config config/local.json query "SELECT ..."
```

The MySQL backend is selected purely by configuration, with credentials read from environment
variables and never written to logs or to the batch log.

### 6.3 Reproducibility

- Simulator output is a pure function of `(seed, routes_file, days, defect_rate)`.
- Reference data is version-controlled; every correction is a reviewable diff.
- The default configuration runs entirely offline on SQLite — zero setup, zero network.
- `python -m pytest -q` → **127 passed**.

---

## 7. Evaluation

### 7.1 Protocol

| Setting | Value |
|---|---|
| Workload | 269 real routes × 14 days (2026-10-22 → 2026-11-04) |
| Simulator seed | `20260101`; defect rate `0.06` |
| Backend | SQLite (file DB, WAL), local machine |
| Timing | Wall-clock, single run, no warm cache |
| Ground truth | The simulator's defect label per record, exposed through an instrumented subclass |

### 7.2 RQ1 — End-to-end yield and cost

| Stage | Rows | Note |
|---|---|---|
| Source records produced | 7,626 | 7,157 clean + 469 carrying an injected defect |
| ODS inserted / skipped | 7,536 / 90 | Skips are byte-identical duplicates, caught by `row_hash` |
| DWD inserted / updated / de-duplicated | 7,511 / 0 / 115 | 115 intra-batch `flight_id` collisions collapsed |
| Anomalies raised / persisted | 474 / 471 | 3 collapsed by the anomaly unique key |
| `dws_flight_daily` rows | 1,307 | date × carrier × departure airport |
| `dws_route_daily` rows | 3,745 | date × route |
| **Wall-clock** | **3.3 s** | ~2,300 source rows/s end-to-end on a laptop |
| Anomaly density | **6.27 %** (471 / 7,511) | 387 rows carry `has_anomaly = 1` (5.15 %) |

### 7.3 RQ2 — Replay safety

The identical batch was executed a second time against the same database:

| Metric | Run 1 | Run 2 | Verdict |
|---|---|---|---|
| ODS inserted / skipped | 7,536 / 90 | **0 / 7,626** | Content hash absorbed the entire batch |
| DWD inserted / updated | 7,511 / 0 | **0 / 7,511** | Business key absorbed it as updates |
| Intra-batch de-duplication | 115 | 115 | Identical |
| `dws_flight_daily` rows | 1,307 | **1,307** | Identical grain |
| `dws_route_daily` rows | 3,745 | **3,745** | Identical grain |
| Anomalies raised | 474 | 474 | Identical (new batch id, same content) |

Because the aggregates are recomputed from DWD rather than accumulated from the batch (§5.6),
"identical row counts" is not a weak claim — the *values* are recomputed from the same inputs and
are identical by construction. This is the evidence for RQ2.

### 7.4 RQ3(a) — Rule sensitivity and specificity

Distribution of the 471 persisted anomalies:

| Severity | Count | | Rule | Severity | Count |
|---|---|---|---|---|---|
| ERROR | 230 | | `DUPLICATE_FLIGHT` | WARN | 115 |
| WARN | 241 | | `MISSING_REQUIRED_FIELD` | ERROR | 74 |
| | | | `UNKNOWN_AIRLINE` | WARN | 68 |
| | | | `INVALID_FLIGHT_DURATION` | ERROR | 46 |
| | | | `UNKNOWN_AIRPORT` | ERROR | 46 |
| | | | `ARRIVAL_BEFORE_DEPARTURE` | ERROR | 43 |
| | | | `INVALID_TIME_FORMAT` | WARN | 36 |
| | | | `INVALID_TIME_VALUE` | ERROR | 21 |
| | | | `MISSING_ACTUAL_TIME` | WARN | 14 |
| | | | `DELAY_OUT_OF_RANGE` | WARN | 8 |

**Specificity.** Of the 7,157 records that carried no injected defect, **9** produced findings —
a 0.13 % flag rate. These are genuine, not artefacts: 8 × `DELAY_OUT_OF_RANGE` (the simulator's
"extreme" branch genuinely assigns delays of up to 25 hours, eight of which breach the 24-hour
plausibility bound) and 1 × `ARRIVAL_BEFORE_DEPARTURE`. The rule engine therefore fires on
phenomena that the injector did not place, which is the check against a circular evaluation.

**Sensitivity.** Per-defect-class results follow.

### 7.5 RQ3(b) — Per-defect-class detection (the central evaluation)

`scripts/eval_defect_coverage.py` reproduces the workload with an instrumented simulator that
labels every record with the defect class injected into it, then classifies each record's fate:

- **detected** — at least one quality rule fired (the defect is recorded and traceable);
- **absorbed** — no rule fired, but the defect was nonetheless neutralised (normalisation or
  dimension imputation produced a correct value);
- **unhandled** — neither: the dirty value reached DWD.

| Injected defect class | Records | Detected | Absorbed | Missed | Detection rate | Dominant rule(s) |
|---|---|---|---|---|---|---|
| `exact_duplicate` (batch) | 90 | **90** | 0 | 0 | 100.0 % | `DUPLICATE_FLIGHT` |
| `unknown_airport` | 46 | **46** | 0 | 0 | 100.0 % | `UNKNOWN_AIRPORT` |
| `missing_flight_no` | 45 | **45** | 0 | 0 | 100.0 % | `MISSING_REQUIRED_FIELD` |
| `arrival_before_departure` | 44 | **44** | 0 | 0 | 100.0 % | `INVALID_FLIGHT_DURATION` ×44, `ARRIVAL_BEFORE_DEPARTURE` ×40 |
| `unknown_airline` | 39 | **39** | 0 | 0 | 100.0 % | `UNKNOWN_AIRLINE` |
| `bad_actual_time` | 36 | **34** | 0 | **2** | 94.4 % | `INVALID_TIME_FORMAT` / `INVALID_TIME_VALUE` / `MISSING_ACTUAL_TIME` |
| `missing_airline` | 29 | **29** | 0 | 0 | 100.0 % | `MISSING_REQUIRED_FIELD` + `UNKNOWN_AIRLINE` |
| `bad_plan_time` | 28 | **28** | 0 | 0 | 100.0 % | `INVALID_TIME_FORMAT` / `INVALID_TIME_VALUE` |
| `re_observation` (batch) | 25 | **25** | 0 | 0 | 100.0 % | `DUPLICATE_FLIGHT` |
| `missing_country` | 51 | 0 | **51** | 0 | 0 % | — (imputed from `dim_airport`) |
| `dirty_airport_name` | 36 | 0 | **36** | 0 | 0 % | — (absorbed by `normalize_airport_key`) |
| **Total** | **469** | **380 (81.0 %)** | **87 (18.6 %)** | **2 (0.4 %)** | | |

Reading this table honestly:

- **380 / 469 = 81.0 %** of injected defects are detected *and logged*.
- **87 / 469 = 18.6 %** are *absorbed*. This is not a failure — it is the standardisation and
  dimension layers doing their job. A country field that is blank in the source is recovered
  from the matched airport's dimension row; an airport name with stray whitespace and an
  `国际机场` suffix is resolved by the symmetric key function. **Neither is logged**, which is
  the honest cost of a silent repair: the pipeline is *correct* but not *fully auditable* for
  these two classes. Closing that gap is the first item of future work (§11).
- **2 / 469 = 0.4 %** are genuinely missed. Both are the same pattern: `dep_actual = "--"` on a
  flight whose status is `延误` or `提前`. The rule `MISSING_ACTUAL_TIME` fires only for
  `DEPARTED`/`ARRIVED`, and `MISSING_TIME` covers only *scheduled* fields, so a missing actual
  time on a delayed-but-not-yet-departed flight is invisible. The fix is a one-line scope
  extension; it is documented rather than silently patched because the rule's current scope was
  a deliberate anti-false-positive choice.
- **0 % detection** for `missing_country` and `dirty_airport_name` is a *design outcome*, and
  the evaluation quantifies it: those two classes are structurally unrecoverable-at-the-row-level
  and are instead handled by the dimension layer.

### 7.6 Downstream metric sanity

| Metric | Value | Interpretation |
|---|---|---|
| Punctuality rate (`ON_TIME`) | 60.76 % (4,564) | ≈ 62 % expected — the simulator's 55 % on-time branch **plus** its 7 % light-delay branch (5–14 min, below the 15 min threshold), less last-day flights still in the *planned* state |
| Delay rate (`DELAYED`) | 22.86 % (1,717) | ≈ 27 % of flights are assigned a ≥ 15 min delay by the simulator's three delayed branches (21 % + 4.5 % + 1.5 %); the observed rate is lower because the 225 midnight-affected rows below are re-classified as `EARLY`, and last-day planned flights have no delay at all |
| Early (`EARLY`) | 661 (8.8 %) | ≈ 6 % early branch **plus** the 225 midnight-affected rows — an independent confirmation of that artefact's size |
| Cancelled | 381 (5.07 %) | Matches the 5 % cancellation branch |
| `UNKNOWN` delay class | 188 (2.5 %) | Last-day flights still in the *planned* state (25 % of one day's schedule) plus cancellations without an actual time |
| Mean departure delay | 5.22 min | **Contaminated** — see the note below; 38.25 min over the 6,717 unaffected rows |
| Max departure delay | 1,424 min | The extreme branch — 23.7 h, and correctly flagged `DELAY_OUT_OF_RANGE` |
| Mean arrival delay | 66.81 min | 54.77 min over the same 6,717 rows; the raw mean is inflated by the same effect |
| Mean scheduled duration | 186.37 min | International flights dominate the route mix (268 / 268 routes are international) |
| Distinct carriers with traffic | 33 of 54 | The remaining carriers appear only as prefixes, not as assigned carriers |
| Distinct departure airports | 42 of 86 | Chinese airports appear as arrivals, ASEAN airports as departures |
| Distinct `route_id` in DWD | 269 | 268 from `dim_route` + 1 pseudo-route created by rows whose airport was unresolvable — a visible, intentional trace of D6 |

The last row deserves a note rather than a footnote: 46 rows with an unresolvable airport
produce a synthetic `route_id` (the hash of an empty origin code). This is a **deliberate
design choice** — those rows are not silently dropped, and they are simultaneously flagged
(`UNKNOWN_AIRPORT`) and visible in the route grain. The cost is one extra pseudo-route in a
269-route dimension; the benefit is that a defective record never disappears.

**A disclosed artefact in the delay aggregates.** The two delay means quoted above are *not*
clean, and the reason is worth stating precisely because it is exactly the kind of thing an
evaluation should surface rather than bury.

The simulator writes the actual *arrival* with a day marker (`23:50+1`) but the actual
*departure* without one. When a flight's actual departure crosses midnight — a late-night
scheduled departure combined with a large delay — the value arrives as a bare `HH:MM` that is
genuinely ambiguous, and the pipeline resolves it to the flight date, yielding a spuriously
*early* departure. **225 rows (3.0 % of DWD)** are affected, each carrying a departure delay
below −200 min, which pulls the mean departure delay down from 38.25 min to 5.22 min and the
mean arrival delay up from 54.77 min to 66.81 min (the true departure-to-arrival gap is
≈ 16.5 min, matching the simulator's 0–25 min arrival-lag model; the raw 61.7 min gap is the
artefact).

Two things follow, and both are stated as findings rather than caveats:

1. **This is a property of the test data, not of the pipeline.** On well-formed input — the
   `HH:MM+N` convention the real collector uses — the pipeline resolves the day correctly, and
   `parse_flight_time` handles `次日`/`翌日`/`+N` explicitly. The correct fix belongs in the
   simulator's formatting, and the numbers in this table are reported as-is so that the
   measurement remains reproducible against the committed code.
2. **The rule engine does not catch it**, and that is the more interesting half. A −743-minute
   "early departure" is individually implausible, yet it falls *inside* the ±24 h window that
   `DELAY_OUT_OF_RANGE` uses, so the rule stays silent. The defect is caught by *nothing*:
   not by a range check, not by a cross-field rule, and not by the aggregate. It was found only
   by inspecting the distribution of the delay column itself. This is the concrete argument
   for §11/F2 — a rule set is a floor, not a ceiling, and distribution-level monitoring catches
   what per-record rules structurally cannot.

### 7.7 Test suite

| File | Cases | Coverage focus |
|---|---|---|
| `tests/test_standards.py` | 62 | Text/flight-number/airport/airline normalisation; time parsing across all formats and error classes; status mapping; delay and duration maths; hash determinism |
| `tests/test_cleaning.py` | 23 | End-to-end transformation; alias resolution; missing-value handling; illegal-field handling; duplicate handling; second-observation update semantics |
| `tests/test_quality.py` | 22 | Each rule fires when it should and stays silent when it should not; severity and scope assignment; batch duplicate detection |
| `tests/test_pipeline.py` | 20 | ODS idempotency; DWD de-duplication; DWS aggregate idempotency; watermark-based incremental behaviour; failed-batch trace; source abstraction |
| **Total** | **127** | All passing |

---

## 8. Discussion

Four findings emerged from the evaluation that were not anticipated at design time.

**8.1 The hardest data problems were reference problems, not record problems.** D3 (斗湖 coded
`TWU`) was the single most consequential defect encountered, and it could not have been fixed
where it appeared. A pipeline that only had record-level rules would have "handled" the affected
rows by joining 斗湖 to Tawau — producing plausible, entirely wrong route statistics. The
correction had to live in the catalogue's own terms, as a reviewed override, which is what
`airport_overrides.csv` is.

**8.2 Two identities are necessary; one is not enough.** The pipeline needed both a content hash
and a business key, and the distinction between them is exactly the distinction between
"collected twice" and "observed twice". This is the point at which the project stopped being
"ETL" and became "state management": the pipeline is not moving rows between tables, it is
maintaining a *current view of flight state* under repeated, imperfect observation.

**8.3 Repair and audit are in tension, and the tension is measurable.** The evaluation makes
this concrete: 18.6 % of injected defects are absorbed without a log entry. Those records are
correct in DWD, but a reviewer asking "was this airport name dirty?" cannot find out. The
honest conclusion is that the pipeline achieves *correctness* where it achieves *auditability*
only partially, and that the missing piece is a mechanism for logging repairs — a "normalisation
audit trail" — which is cheap to add and is specified in §11.

**8.4 A rule set is a floor, not a ceiling.** The midnight artefact described in §7.6 was
caught by no rule: the value was individually plausible, inside every configured range, and
internally consistent with its own field. It was visible only in the *distribution* of the
delay column. The general lesson — and the argument for pairing a rule engine with
distribution-level and model-based monitoring — is that per-record rules can only express what
the author already suspected, while a distribution exposes what nobody thought to look for.
This is the strongest evidence in the project for the learned-detection direction in §11/F2:
the rule engine should be treated as a high-precision baseline, not as a complete answer.

---

## 9. Contributions

1. **A layered, replay-safe ETL pipeline** with two independent identity mechanisms
   (`row_hash`, `flight_id`) and date-scoped full recomputation of aggregates — verified
   idempotent over repeated execution (§7.3).
2. **A governed reference-data layer** in which the dimension model is derived from
   version-controlled files and every correction to upstream master data is an explicit,
   reviewable, diffable override rather than an embedded constant (§3.3).
3. **A 15-rule, two-scope data-quality engine** with a typed parse-result design that prevents
   double-counting a single defect across several rules, and with syntax/reference separation
   that avoids false positives on legitimate carriers (§5.7).
4. **A separation of cleaning from auditing** — the design principle that makes both usable
   downstream and reviewable upstream.
5. **An instrumented evaluation harness** that measures per-defect-class detection, absorption
   and miss rates against injected ground truth, including a specificity check on
   defect-free records (§7.5).
6. **A production-shaped engineering surface**: one CLI, two database dialects from one schema
   definition, environment-driven configuration with credential redaction, batch logging,
   watermarked incremental execution, and 127 passing tests.

---

## 10. Limitations and Threats to Validity

These are stated plainly, because a document that claims more than it measured would be worse
than no document.

**L1 — The primary source is a simulator; the flight-level data is synthetic.** Only the
route catalogue (269 routes, 86 airports, 54 carriers) is real. The simulator's flight records
are synthesised from an operational model, so **no statement in this document should be read as
a finding about real airline punctuality**. The claims are about *pipeline behaviour*, and they
are valid for that. Evaluating on real observations remains outstanding, and is the first
empirical milestone of any follow-on work.

**L2 — The MySQL path is unverified against a live instance.** The code, the DDL rendering and
the dialect wrapper are complete, but the development machine has no network route to the
collector's database (`Access denied`), so the MySQL backend has never executed a real batch.
The SQLite backend is the one validated end to end. Additionally, the collector's own network
egress is blocked in this environment (`net::ERR_CONNECTION_CLOSED`), which is why no live
flight detail was ever obtained.

**L3 — The evaluation's ground truth is the injector, which bounds what can be claimed.** §7.5
measures detection against defects that the simulator deliberately planted. It cannot measure
detection of defect classes nobody thought to plant — which is exactly why the 9 spontaneous
findings on defect-free records are reported as an independent signal. A real-data evaluation
would need a hand-labelled sample.

**L4 — The route catalogue lacks scheduling metadata.** It carries no frequency, aircraft type
or block-time information, so the simulator's 1–3 flights per route per day is an assumption. Any
distribution parameter derived from it (e.g. the flight count) is therefore not calibrated to
reality, even though the *pipeline's* behaviour under those volumes is measured exactly.

**L5 — Delay metrics are schedule-minus-actual from a single snapshot.** There are no weather,
air-traffic, or airport-congestion covariates, so no causal claim about *why* flights are late
is supported or attempted. The metric is a definition, not an inference.

**L6 — Dimension construction is not fully automatic.** Same-name-different-code airport
ambiguities (the D3 family) require human review; the pipeline detects and reports conflicts
(`missing_code`, `dangling_alias`) but does not resolve them automatically. Entity resolution
for these cases is future work (§11).

**L7 — Known rule-coverage gaps, measured rather than assumed.** (i) A missing actual time on a
`DELAYED`/`EARLY` flight is not flagged (2 records in §7.5). (ii) Absorbed repairs are not
logged (§8.3). (iii) The anomaly table is keyed by batch, so re-running the same logical batch
under a new batch id duplicates findings — a consequence of the current audit design, not of the
pipeline's correctness. (iv) The configuration key `min_flight_duration_min` (30) is declared
but never wired into the duration rule, whose lower bound is therefore 0 rather than 30.

**L8 — Scale.** The workload is 7.6 k rows in 3.3 s. The design extrapolates (the aggregation
layer is vectorised with pandas and the fact lookup is chunked to respect SQL parameter limits),
but no benchmark above ~10⁶ rows has been run, and the DWD recomputation strategy trades
write amplification for correctness — a deliberate choice that would need revisiting at scale.

**L9 — The simulator's day-marker asymmetry contaminates the delay aggregates.** The mean
departure and arrival delays in §7.6 are affected by 225 rows whose actual departure crossed
midnight without a day marker (§7.6, §8.4). The pipeline handles the `HH:MM+N` convention
correctly; the artefact originates in the simulator's formatting, and the affected metric rows
are reported both raw and cleaned so that the reader can see the size of the effect. A fix in
`simulator.py` (apply the day marker to `dep_actual` as is already done for `arr_actual`)
would remove it and is the first change I would make to the repository; it is not applied here
because every measurement in this document is reported against the committed code.

---

## 11. Future Work and Research Agenda

The limitations above map onto concrete, feasible next steps — several of which are natural
extensions for graduate study in data science or computer science.

| # | Direction | Rationale |
|---|---|---|
| **F1** | **A normalisation audit trail.** Log every *repair*, not only every failure — recording which rule transformed which raw value. | Closes the 18.6 % blind spot measured in §7.5 at the cost of a few columns and one table. |
| **F2** | **Learned anomaly detection over the defect stream.** `dq_flight_anomaly` is a labelled defect corpus; train supervised and unsupervised detectors on it and compare against the 15 rules. | Converts a fixed rule set into a system that *discovers new defect classes*, with the rule engine as a high-precision baseline to beat. |
| **F3** | **Schema matching for the upstream collector.** The catalogue is a fragile contract; implement Rahm & Bernstein-style schema matching (plus embedding-based matching) to detect drift and propose column/alias mappings automatically. | Addresses L6 and the long-term fragility of hard-coded aliases. |
| **F4** | **Probabilistic delay modelling.** With real observations, fit hierarchical delay distributions per route × carrier × season, and report *predictive* (rather than descriptive) punctuality intervals. | Turns the DWS layer from a retrospective report into a forecasting substrate. |
| **F5** | **CDC-based incremental ingestion.** Replace date watermarks with change-data-capture on the collector's `update_time`, enabling intra-day refresh and exactly-once semantics at the transaction level. | Removes the one-day latency of date-based watermarks. |
| **F6** | **Data contracts and quality gates.** Emit a machine-readable contract (schema, expected ranges, freshness SLO) per dataset; fail or quarantine batches that violate it. | Extends the anomaly trail into a preventive control rather than a detective one. |
| **F7** | **Lakehouse migration.** Partition `dwd_flight` by `flight_date` in Parquet, serve via DuckDB/Trino, and keep the SQLite/MySQL path for local development. | Enables the scale needed for F4 and F5. |
| **F8** | **Human-in-the-loop correction.** Route each anomaly to a reviewer; write the accepted correction back into `airport_overrides.csv` / `airport_aliases.csv`; re-measure the detection rate. | Closes the loop between the quality layer and the reference layer — the same loop that fixed D3, but at scale. |

---

## 12. Reproducibility Statement

```bash
git clone <repository> && cd AeroData-Pipeline
pip install -r requirements.txt

python -m aerodata.cli --config config/local.json init-db    # schema + dimensions
python -m aerodata.cli --config config/local.json run --source simulator   # ~3.3 s
python -m aerodata.cli --config config/local.json run --source simulator   # idempotency check
python -m aerodata.cli --config config/local.json stats
python -m pytest -q                                          # 127 passed
python scripts/eval_defect_coverage.py                       # §7.5 table
```

Every figure in §7 is reproduced by the commands above on the committed configuration
(`seed = 20260101`, `days = 14`, `defect_rate = 0.06`). The simulator's randomness is derived
from `SHA-256` over stable keys, so the dataset is identical across machines and interpreter
versions — not merely statistically similar.

---

## 13. References

1. W. H. Inmon. *Building the Data Warehouse*, 2nd ed. Wiley, 2002.
2. R. Kimball, M. Ross. *The Data Warehouse Toolkit*, 3rd ed. Wiley, 2013.
3. E. F. Codd. "A Relational Model of Data for Large Shared Data Banks." *Communications of the ACM* 13(6), 1970, pp. 377–387.
4. P. P. Chen. "The Entity-Relationship Model — Toward a Unified View of Data." *ACM Transactions on Database Systems* 1(1), 1976, pp. 9–36.
5. M. Kleppmann. *Designing Data-Intensive Applications: The Big Ideas Behind Reliable, Scalable, and Maintainable Systems*. O'Reilly, 2017.
6. S. Ewen, B. Reinhardt, V. Markert. "Apache Flink: Stream and Batch Processing in a Single Engine." *Datenbank-Spektrum* 35(2), 2015, pp. 24–31.
7. R. Y. Wang, D. M. Strong. "Beyond Accuracy: The Role of Data Quality in Information Systems." *Journal of Management Information Systems* 12(4), 1996, pp. 61–88.
8. ISO/IEC 25012:2008. *Software engineering — Software quality requirements and evaluation (SQuaRE) — Data quality model*.
9. DAMA International. *The DAMA-DMBOK: Data Management Body of Knowledge*, 2nd ed. Technics Publications, 2017.
10. C. Zook, E. Null. "The Continuing Curse of Enterprise Data Quality." MIT CISR Working Paper 14-13, 2014.
11. S. Schelter et al. "Automatic Data Quality Validation for ML Pipelines." *Proceedings of the VLDB Endowment* 14(12), 2021, pp. 3361–3374.
12. E. Breck, S. Cai, E. Nielsen, M. Salib, D. Sculley. "The ML Test Score: A Rubric for ML Production Readiness and Technical Debt Reduction." *IEEE International Conference on Big Data*, 2017, pp. 1123–1132.
13. D. Sculley et al. "Hidden Technical Debt in Machine Learning Systems." *Advances in Neural Information Processing Systems (NIPS)* 28, 2015, pp. 2503–2511.
14. E. Rahm, P. A. Bernstein. "A Survey of Approaches to Automatic Schema Matching." *The VLDB Journal* 10(4), 2001, pp. 584–602.
15. I. P. Fellegi, A. B. Sunter. "A Theory for Record Linkage." *Journal of the American Statistical Association* 64(328), 1969, pp. 1183–1210.
16. K. M. Chandy, L. Lamport. "Distributed Snapshots: Determining Global States, and a Consistent Algorithm for Fault-Tolerant Distributed Databases." *ACM Transactions on Database Systems* 3(2), 1985, pp. 166–176.
17. C. Robert, G. Casari. *Monte Carlo Statistical Methods*, 2nd ed. Springer, 2004.

---

## Appendix A — Reference-Data Governance

| Artefact | Content | Purpose |
|---|---|---|
| `data/reference/routes_daily.json` | 269 routes: origin/destination names, cities, provinces, countries, IATA codes | Seed of the airport and route dimensions |
| `data/reference/airlines.csv` | 54 carriers: code, Chinese name, English name, country, ISO code | Carrier dimension + flight-prefix validation |
| `data/reference/airport_overrides.csv` | 8 reviewed corrections (7 codes filled, 1 code corrected) | Master-data repair, applied at dimension-build time |
| `data/reference/airport_aliases.csv` | 3 curated aliases (`曼谷素万那普`→`BKK`, `曼谷廊曼`→`DMK`, `塔瓦乌机场`→`TWU`) | Referential integration of source naming |
| `data/reference/countries.csv` | 25 country ↔ ISO-3166 alpha-2 mappings | Cross-checking record-level country fields |

**Dimension build results.** 86 airports · 89 aliases (86 canonical + 3 auto-merged names:
`曼谷素万那普`→`BKK`, `曼谷廊曼`→`DMK`, `斗湖`→`BKI`) · 54 carriers · 268 routes · 17 distinct
countries · 3 hub airports flagged.

Two details are worth recording because they are easy to misread:

- The manual alias `塔瓦乌机场 → TWU` does **not** create a separate alias row: after
  normalisation it lands on the same key as the canonical airport name `塔瓦乌`, so it
  *reinforces* the code assignment rather than adding an entry. The alias table therefore
  contributes 2 of the 3 merged rows, and the third (`斗湖`→`BKI`) is produced automatically by
  the correction described in §3.3.
- The 269 → 268 route collapse is **not** the removal of a duplicated entry. It is the
  *consequence* of the correction: `沙巴 → 上海浦东` and `斗湖 → 上海浦东` are two catalogue
  rows describing one physical route, and they become identical once 斗湖 is re-keyed to `BKI`.
  The catalogue was double-counting a route; the correction makes the duplication visible
  instead of hiding it.

## Appendix B — Condensed Material for Application Use

**B1 · CV project entry (≈ 90 words)**

> **AeroData Pipeline** — governed, layered ETL for aviation operations data (Python, pandas,
> SQLite/MySQL). Built a source-agnostic ODS→DWD→DWS warehouse over 269 real ASEAN–China
> routes: 11 tables from one dialect-portable schema, a 15-rule data-quality engine that logs
> every defect with its raw and normalised value, and two independent identity mechanisms
> (content hash + business key) making the pipeline bit-for-bit idempotent. Verified on a
> 7,626-row workload (3.3 s end-to-end, 127 tests passing) and instrumented it with an
> injector-based evaluation harness that measures per-defect-class detection (81 %),
> absorption (19 %) and a documented 0.4 % miss rate.

**B2 · CV bullet points**

- Designed and implemented a layered ODS/DWD/DWS ETL pipeline in Python that turns raw
  scraped flight records into analysis-ready facts and aggregates, validated end-to-end on a
  7,626-row / 269-route workload (3.3 s, 127 passing tests).
- Engineered replay safety with two complementary identities — a content hash for repeated
  collection and a business key for repeated observation — so that re-running a batch inserts
  nothing and reproduces every aggregate exactly.
- Built a 15-rule data-quality engine separating syntactic validation from referential
  integrity, with a typed parse-result design that prevents double-counting a defect across
  rules; measured 81 % detection and 0.4 % miss rates against injected ground truth.
- Codified upstream master-data corrections as version-controlled override files, resolving a
  two-airport IATA-code collision (斗湖/塔瓦乌) that no record-level rule could have detected.

**B3 · Statement-of-Purpose paragraph (≈ 170 words)**

> My interest lies in the point where data stops being records and starts becoming evidence.
> In an aviation analytics project I inherited a scraper's output — flight numbers as
> `SQ 830` and `sq-830`, departure times as `8:5` and `25:00`, one airport under four names,
> and a route catalogue that coded one Malaysian airport with another airport's identifier. The
> naive fix was a layer of `CASE` expressions; I built a warehouse instead. The project taught
> me that the interesting defects are rarely in the rows: they are in the reference data, and
> they need a different kind of governance than the rows do. It also taught me to measure
> rather than assert — I instrumented the data generator so that every defect class could be
> scored as detected, absorbed, or missed, and the two cases my rules failed are documented in
> the report as precisely as the 380 they caught. That combination — a formal model, a
> reproducible measurement, and an honest account of the gaps — is the research posture I
> want to bring to graduate study.

---

## Appendix C — 中文摘要

**航空数据治理与数仓分层 ETL 流水线（AeroData Pipeline）**

采集器（Playwright 抓取，269 条东南亚—中国航线）写出的原始记录对人可读、对查询引擎不可用：
同一机场四种写法、航班号 `SQ 830`/`sq-830`/`SQ.830`、时间 `8:5`/`25:00`/`--`/`23:50+1`、
同一班航班被重复采集且状态从「已起飞」推进到「已到达」。直接在这种数据上算延误率，
得到的数字既错又无法追责。

本项目作为采集器的下游，交付三件事：

1. **分层数仓（ODS/DWD/DWS）**，共 11 张表、同一份表结构渲染出 SQLite 与 MySQL 两套 DDL；
   维度层由可版本化的参考文件装载，人工修正写进 `airport_overrides.csv`——
   航线清单把斗湖的 IATA 码误写成塔瓦乌的 `TWU`，一行修正同时修好两个机场
   （斗湖归并为沙巴 `BKI` 的别名，塔瓦乌补上 `TWU`）。
2. **15 条质量规则**（14 条行级 + 1 条批次级），每条异常留痕：规则编码、严重度、字段、
   原始值、标准化值、处理说明。清洗层负责「把数据改对」，质量层负责「把改不好的记下来」。
3. **可复现的评估**：仿真源按 11 类缺陷注入脏数据，逐类统计检出/消解/漏检，
   而不是笼统地宣称「能发现脏数据」。

实测（本地 SQLite，14 天 × 269 条航线，7,626 条源端记录）：耗时 3.3 s，
DWD 7,511 行、DWS 日汇总 1,307 行、航线汇总 3,745 行，异常密度 6.27%；
**第二次运行 ODS 新增 0 行、DWD 更新 7,511 行、汇总完全一致**——幂等由
「内容哈希（同一观测）+ 业务主键（同一航班状态推进）」两套身份共同保证。
缺陷评估：469 条注入缺陷中 380 条（81.0%）被检出并记录，87 条（18.6%）被标准化或维表
补齐而吸收，2 条（0.4%）漏检——漏检的两条与漏检原因均已在报告中写明。

**边界（诚实说明）**：默认数据源是仿真源，只有航线清单是真实的，航班明细是合成的，
因此文中所有结论都是关于**流水线行为**的，不构成对真实航班准点率的主张；
MySQL 后端代码完整但本机到采集器库网络不通，尚未在真实实例上跑通，SQLite 后端已全量验证。
