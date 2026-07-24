# Live Expectations & Risk Monitor — Taxonomy, Scoring, Schema (v1 spec)

Status: DESIGN — no code yet. Home: `portfolio_layer/expectations_monitor/`.
Scope: **investable_eligible names only** (currently 327), refreshed from each sealed Stage 1
`stocks_scores.csv`. Free/low-cost sources only; Gemini strictly free-tier, rules-first.

Core equation (posterior = prior + evidence):

```
LES_t = BaselinePoints                     (from the sealed Stage 1 run — the slow prior)
      + Σ decayed CompanyEventImpact        (company-originated events)
      + Σ decayed ExternalIntelImpact       (analyst / channel / industry)
      + MarketSignalPoints_t                (recomputed daily, no decay)
      + PeerReadThroughPoints_t             (relationship-weighted peer events)
EventImpact_i   = Direction × Severity × Credibility × Novelty × Relevance   (points, ±5·scale)
DecayedImpact_t = Impact_0 × 2^(−age_trading_days / half_life)               (unless until_replaced)
```

Components are ALWAYS stored separately (`les_snapshots`); the composite alone is never the record.

---

## 1. Reuse map — what already exists (do NOT rebuild)

| Monitor need | Existing asset (path) | How the monitor uses it |
|---|---|---|
| Universe + prior | `output/runs/<asof>/stocks_scores.csv` (sealed) | `investable_eligible=1` rows → universe; `within_sector_percentile` × `score_confidence` → BaselinePoints |
| Earnings event windows | `earnings_dates/37,38` + history CSV | `days_until` gates severity of pre-earnings signals; post-earnings triggers immediate review |
| SEC filing index & PIT discipline | sector `07_sync_*_sec_fundamentals` (`fact_sec_filing`), biotech `07_parse_sec_biotech_events.py` + `core/event_types.py` + parse-state pattern | EDGAR poller copies the parse-state/accession-dedup pattern; biotech taxonomy seeds the company-event taxonomy; worker tuning precedent (8 workers / 500 batch) |
| Issuer guidance events | biotech `19_parse_forward_guidance.py` (`company_forward_guidance`, PIT) | biotech guidance changes ingested directly as `guidance_*` events; other sectors detect via 8-K keywords (v1) |
| Insider activity | `SEC_FORM4_Runner/` + `helper_scripts/build_form4_buy_events_v1.py` | `insider_buy_cluster` / `insider_sale_cluster` events |
| Short interest / borrow | `market_positioning` (`short_interest_snapshots`, `ibkr_borrow_fee_rate_daily`) | `short_interest_spike`, `borrow_fee_spike` market events |
| Sector-relative returns | `risk_panel.sector_etf_map` (config.yaml: semis→SOXX, software→IGV, hardware→XLK, biotech→XBI, med→IHI, defense→XAR, machinery→XLI) + Stage 2 `prices_adjclose.csv`/`returns_panel.csv` | abnormal-return and relative-strength calculations |
| Price fetch plumbing | `risk/yahoo.py` (query1/query2, provenance) | market-signal builder extends the same fetch to keep volume/high/low for the 327 names (doubles as Phase 0 of the price-band engine) |
| API keys | env: `ALPHAVANTAGE_API_KEY`, `GEMINI_API_KEY`; `FMP_API_KEY` (used for splits) | AV news (secondary, quota-shared), Gemini classification (capped), FMP news/analyst endpoints (primary external intel — free-plan limits MUST be probed first) |
| Artifact conventions | `core/contracts.py` (write_csv/manifests/seals), `core/config.py`, validators | per-run export + manifest + validator follow house pattern |
| Feed-state bookkeeping | `market_positioning.market_positioning_feed_state` | same design for `feed_state` table |

**Genuinely new:** news/IR ingestion, analyst-action ingestion, event classifier, relationship map,
LES/state machine, alerts, monitor DB. Nothing else.

---

## 2. Event taxonomy (exact)

Direction is assigned by the classifier (sign −1..+1); the table gives DEFAULT severity, credibility,
half-life (trading days), decay mode, and whether the type can support a `thesis_break_flag`.
Novelty default 1.0 (first in cluster); repeat of same `event_type`+ticker within 20 td → 0.3.
Relevance default 1.0 for the subject ticker; peer-propagated events use the relationship weight.

### 2.1 company_filing / company_announcement (credibility 0.90–1.00)

| event_type | sev | cred | half-life | decay | thesis-break |
|---|---|---|---|---|---|
| guidance_cut | 4.5 | 1.00 | — | until_replaced | eligible (repeated) |
| guidance_raise | 3.5 | 1.00 | — | until_replaced | — |
| guidance_affirmed | 1.5 | 1.00 | — | until_replaced | — |
| preannounce_negative | 4.5 | 1.00 | — | until_replaced | eligible |
| preannounce_positive | 3.5 | 1.00 | — | until_replaced | — |
| earnings_miss | 3.0 | 1.00 | 20 | half_life | — |
| earnings_beat | 2.5 | 1.00 | 20 | half_life | — |
| customer_loss_or_contract_cancellation | 4.5 | 0.95 | 120 | half_life | eligible |
| customer_win_or_major_contract | 3.0 | 0.95 | 60 | half_life | — |
| mna_target (holding being acquired) | 4.0 | 1.00 | 60 | half_life | — |
| mna_acquirer / divestiture | 2.5 | 1.00 | 60 | half_life | — |
| executive_departure_ceo_cfo | 3.0 | 1.00 | 120 | half_life | eligible (credibility failure) |
| executive_departure_other / hire | 1.5 | 1.00 | 60 | half_life | — |
| product_launch | 2.0 | 0.95 | 40 | half_life | — |
| product_delay_or_recall | 3.0 | 0.95 | 60 | half_life | eligible (displacement) |
| regulatory_action_adverse | 4.0 | 1.00 | event_specific (default 90) | half_life | eligible |
| regulatory_clearance_positive | 3.0 | 1.00 | 60 | half_life | — |
| litigation_development | 2.5 | 0.90 | event_specific (default 60) | half_life | eligible |
| accounting_restatement_or_material_weakness | 5.0 | 1.00 | 180 | half_life | **always** |
| auditor_change_adverse | 4.0 | 1.00 | 120 | half_life | eligible |
| balance_sheet_distress (covenant/going-concern) | 5.0 | 1.00 | 180 | half_life | **always** |
| financing_dilutive | 2.5 | 1.00 | 40 | half_life | — |
| buyback_or_dividend_action | 1.5 | 1.00 | 40 | half_life | — |
| insider_buy_cluster / insider_sale_cluster | 1.5 | 0.90 | 30 | half_life | — |

### 2.2 external_intel (credibility 0.50–0.80)

| event_type | sev | cred | half-life | decay | thesis-break |
|---|---|---|---|---|---|
| analyst_downgrade / analyst_upgrade | 2.0 | 0.75 | 15 | half_life | — |
| price_target_change (either direction) | 1.0 | 0.70 | 10 | half_life | — |
| estimate_revision_down / _up (needs FMP) | 2.5 | 0.75 | 30 | half_life | — |
| channel_check_negative / _positive | 3.5 | 0.70 | 30 | half_life | — |
| churn_or_pricing_pressure_report | 4.5 | 0.65 | 90 | half_life | eligible (structural churn) |
| industry_survey_or_market_share | 2.5 | 0.65 | 40 | half_life | — |
| security_incident_or_negative_review | 2.5 | 0.70 | 30 | half_life | — |
| competitor_commentary | 2.0 | 0.60 | 20 | half_life | — |

Churn scores worse than a price-target cut BY CONSTRUCTION (sev 4.5×90td vs 1.0×10td): churn
changes the thesis; a target change may only re-mark valuation.

### 2.3 peer_readthrough (relevance = relationship weight, always < 1)

| event_type | sev | cred | half-life |
|---|---|---|---|
| peer_results_negative / _positive | inherit source × weight | 0.80 | 20 |
| customer_capex_cut / _raise | 3.0 | 0.80 | 60 |
| supplier_constraint | 2.0 | 0.75 | 40 |
| competitor_pricing_action | 3.0 | 0.70 | 60 |

### 2.4 market_signal (credibility 1.00 — measured, but low severity unless corroborated; recomputed daily, `recompute_daily` = no stored decay)

| event_type | sev | half-life / mode |
|---|---|---|
| abnormal_return_1d (|z| ≥ 2 vs sector ETF) | 2.0 | 4 td |
| earnings_gap_down_unrecovered / failed_rebound | 2.0 | 10 td |
| volume_anomaly (volume z ≥ 3, no news matched) | 1.0 | 5 td |
| rel_weakness_5d_20d (both windows underperform sector) | 1.5 | recompute_daily |
| new_52w_low | 1.5 | recompute_daily |
| below_ma50_and_ma200 | 1.0 | recompute_daily |
| volatility_expansion (realized vol 2× 60d norm) | 1.0 | 10 td |
| short_interest_spike / borrow_fee_spike | 1.5 | 20 td |

---

## 3. Scoring parameters

```
points_per_unit          = 4.0     # EventImpact × 4 → LES points (max single event ≈ ±20)
baseline_scale           = 0.6     # BaselinePoints = clip((within_sector_percentile − 50) × 0.6, ±30) × score_confidence
market_component_cap     = ±15     # sum of §2.4, clipped
peer_component_cap       = ±10
company+external caps    = none individually; LES_total clipped to ±100
novelty_repeat_window_td = 20 ; novelty_repeat_value = 0.3
pre_earnings_gate        = within 5 td of next_earnings_date (from earnings_dates artifact):
                           market-signal severities ×0.5 (moves near prints are expected noise)
```

CHKP worked example (spec §Event scoring): channel check −1.0×4.0(sev overridden from 3.5)×0.75×0.9×1.0
= −2.70 impact → −10.8 LES points, decaying with 30-td half-life; plus escalation rule R2 fires.

## 4. States and asymmetric transitions

| State | LES range | Portfolio action |
|---|---|---|
| green | ≥ +20 | eligible to add |
| stable | −10 … +20 | hold; add only with valuation support |
| watch | −25 … −10 | suspend additions; thesis review |
| deteriorating | −45 … −25 | consider reduction/replacement |
| broken | < −45 **AND** ≥1 event with `thesis_break_flag` confirmed | exit |

Rules:
- **Downgrades are immediate** on score cross OR escalation rule (below), whichever first.
- **Broken requires hard evidence**: score alone can never set `broken` — needs a confirmed
  thesis-break event (repeated guidance cuts = 2 `guidance_cut` events within 2 quarters counts).
- **Upgrades require dwell or confirmation** (anti-first-rebound):
  - watch→stable: LES ≥ −5 for 10 consecutive td, OR a company confirmation event
    (`guidance_affirmed`/`guidance_raise`/`earnings_beat`).
  - deteriorating→watch: inflection event required (`guidance_raise`, `estimate_revision_up`,
    `customer_win_or_major_contract`) AND LES ≥ −20 for 5 consecutive td.
  - broken→anything: manual only (`state_transitions.approved_by = 'manual'`).

Escalation rules (fire regardless of score; recorded in `state_transitions.rule_id`):

| id | trigger | action |
|---|---|---|
| R1 | any `guidance_cut` | ≥ watch immediately |
| R2 | `channel_check_negative` sev≥3.5 or `churn_or_pricing_pressure_report` | ≥ watch; sev≥4.5 → deteriorating review alert |
| R3 | 2 `analyst_downgrade` with same driver tag within 20 td, cred ≥0.7 | deteriorating review alert |
| R4 | negative event (any category) same-day as sector-relative return z ≤ −2 | immediate review alert |
| R5 | `rel_weakness_5d_20d` AND `estimate_revision_down` both active | suspend adds (≥ watch) |
| R6 | any negative material event on a Tier 1/holding name | `rerank_tier1` alert (immediate, not next monthly) |

## 5. Database schema (new SQLite: `db/expectations_monitor.sqlite`)

Separate DB from `portfolio_layer.sqlite`: this store is continuously appended by pollers and must
not contend with run-scoped pipeline writes. All timestamps UTC ISO-8601; PIT columns everywhere.

```sql
CREATE TABLE monitor_universe (          -- refreshed from each sealed Stage 1 run
  run_as_of TEXT NOT NULL, ticker TEXT NOT NULL,
  source_pipeline TEXT, sector TEXT, industry TEXT,
  tier TEXT,                             -- holding | tier1 | tier2 | candidate | other (config-mapped)
  baseline_points REAL NOT NULL, baseline_inputs_json TEXT,
  updated_at TEXT NOT NULL, PRIMARY KEY (run_as_of, ticker));

CREATE TABLE ticker_relationships (
  ticker TEXT NOT NULL, related TEXT NOT NULL,       -- ticker, ETF, or factor slug
  relation_type TEXT NOT NULL,                       -- peer|competitor|customer|supplier|sector_etf|commodity|macro_factor
  weight REAL NOT NULL CHECK (weight > 0 AND weight <= 1),
  direction_hint TEXT DEFAULT 'aligned',             -- aligned | inverse | ambiguous
  source TEXT NOT NULL,                              -- auto_industry | manual | config_sector_etf
  valid_from TEXT, valid_to TEXT, note TEXT,
  PRIMARY KEY (ticker, related, relation_type));

CREATE TABLE raw_items (                 -- ingest log; nothing is classified without a raw item
  item_id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,                  -- sec_edgar|fmp_news|fmp_press|fmp_analyst|av_news|ir_rss|form4|market_calc|manual
  source_uid TEXT,                       -- accession no. / article id / url hash
  ticker_hint TEXT, published_at_utc TEXT, fetched_at_utc TEXT NOT NULL,
  title TEXT, summary TEXT, url TEXT, payload_json TEXT,
  content_sha256 TEXT NOT NULL UNIQUE,   -- exact dedup
  dedup_cluster_id INTEGER,              -- near-dup: normalized-title Jaccard ≥ 0.6, same ticker, 48h
  status TEXT NOT NULL DEFAULT 'new');   -- new|classified|duplicate|irrelevant|error

CREATE TABLE event_taxonomy (            -- seeded from §2; versioned via taxonomy_version
  event_type TEXT PRIMARY KEY, category TEXT NOT NULL,
  default_severity REAL, default_credibility REAL,
  default_half_life_td INTEGER, decay_mode TEXT NOT NULL,   -- half_life|until_replaced|recompute_daily
  thesis_break_eligible INTEGER NOT NULL DEFAULT 0,
  escalation_rule TEXT, description TEXT, taxonomy_version TEXT NOT NULL);

CREATE TABLE events (
  event_id INTEGER PRIMARY KEY, ticker TEXT NOT NULL,
  event_type TEXT NOT NULL REFERENCES event_taxonomy(event_type),
  category TEXT NOT NULL, event_date TEXT NOT NULL, detected_at_utc TEXT NOT NULL,
  direction REAL NOT NULL CHECK (direction BETWEEN -1 AND 1),
  severity REAL NOT NULL, credibility REAL NOT NULL,
  novelty REAL NOT NULL, relevance REAL NOT NULL,
  impact_0 REAL NOT NULL,                -- direction×severity×credibility×novelty×relevance
  half_life_td INTEGER, decay_mode TEXT NOT NULL,
  replaced_by_event_id INTEGER,          -- until_replaced chain (e.g. new guidance supersedes old)
  driver_tag TEXT,                       -- shared-driver key for R3 (e.g. 'firewall_demand')
  origin_ticker TEXT,                    -- for peer_readthrough: where the event actually happened
  source_item_ids TEXT NOT NULL,         -- JSON list into raw_items
  classifier TEXT NOT NULL,              -- rule|llm|manual
  classifier_version TEXT, rationale_text TEXT NOT NULL,
  material_flag INTEGER NOT NULL DEFAULT 0, thesis_break_flag INTEGER NOT NULL DEFAULT 0,
  review_status TEXT NOT NULL DEFAULT 'auto');  -- auto|pending_review|confirmed|dismissed

CREATE TABLE market_signals_daily (
  ticker TEXT NOT NULL, asof_date TEXT NOT NULL,
  abnormal_ret_1d_z REAL, rel_ret_5d REAL, rel_ret_20d REAL,
  volume_z REAL, realized_vol_ratio REAL,
  below_ma50 INTEGER, below_ma200 INTEGER, new_52w_low INTEGER,
  gap_state TEXT, market_component_points REAL NOT NULL, inputs_json TEXT,
  PRIMARY KEY (ticker, asof_date));

CREATE TABLE les_snapshots (             -- components ALWAYS visible; composite never stored alone
  ticker TEXT NOT NULL, asof_ts TEXT NOT NULL, run_as_of TEXT NOT NULL,
  baseline_points REAL NOT NULL, company_event_points REAL NOT NULL,
  external_intel_points REAL NOT NULL, market_points REAL NOT NULL,
  peer_readthrough_points REAL NOT NULL, les_total REAL NOT NULL,
  state TEXT NOT NULL, prior_state TEXT, state_changed INTEGER NOT NULL DEFAULT 0,
  escalation_flags_json TEXT, top_contributors_json TEXT NOT NULL,  -- [{event_id, points}, ...]
  PRIMARY KEY (ticker, asof_ts));

CREATE TABLE state_transitions (         -- the audit trail for every state change
  transition_id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, transition_ts TEXT NOT NULL,
  from_state TEXT NOT NULL, to_state TEXT NOT NULL,
  trigger TEXT NOT NULL,                 -- score_cross|escalation|upgrade_dwell|upgrade_confirmation|manual
  rule_id TEXT, evidence_event_ids TEXT, dwell_days_met INTEGER,
  approved_by TEXT NOT NULL DEFAULT 'auto', note TEXT);

CREATE TABLE alerts (
  alert_id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, created_at TEXT NOT NULL,
  alert_type TEXT NOT NULL,              -- state_change|escalation|review_required|rerank_tier1|suspend_starter
  severity TEXT NOT NULL, message TEXT NOT NULL,
  rule_id TEXT, event_ids TEXT, acknowledged_at TEXT);

CREATE TABLE feed_state (                -- mirrors market_positioning pattern
  source TEXT PRIMARY KEY, cursor TEXT, etag TEXT,
  last_success_utc TEXT, last_error TEXT, error_streak INTEGER NOT NULL DEFAULT 0);
```

Per-run export (house convention, for pipeline joins):
`output/runs/<asof>/expectations_monitor/expectations_state.csv`
(ticker, les components, state, active escalations, top 3 contributors + rationale) + manifest + validator.

## 6. Data sources and quota budget (free tiers only)

| Source | Endpoint(s) | Budget / cadence | Notes |
|---|---|---|---|
| SEC EDGAR (free, no key) | `getcurrent` RSS filtered by our CIK set every 15 min (RTH); `data.sec.gov/submissions/CIK*.json` etag-cached hourly | ≤10 req/s allowed; we use ~1/min | Authoritative 8-K/6-K/10-Q/10-K/S-* events. CIK map from existing sector syncs |
| FMP (key exists) | stock-news, press-releases, grades (analyst actions), price-target, analyst-estimates | free plan ~250 req/day — **probe first (Phase 1 step 0)** | Primary external-intel feed; batch by ticker list where supported |
| Alpha Vantage (key exists) | NEWS_SENTIMENT, tickers= comma-batched | ≤ ~20 req/day (25/day quota SHARED with earnings_dates sync: 1/day) | Secondary; we classify ourselves, never accept provider sentiment as final |
| Gemini (key exists, FREE tier only) | REST generateContent (pattern already in earnings_dates/37) | hard cap ~100 classifications/day, 6.5 s pacing | ONLY for ambiguous material items; rules classify first |
| Form 4 | existing `SEC_FORM4_Runner` outputs | daily | insider cluster events |
| market_positioning DB | short interest, borrow fee | daily (already synced by sectors) | spike events |
| Prices/volume | extend `risk/yahoo.py` fetch pattern to keep OHLCV for the 327 names | EOD daily + on-demand intraday for holdings (Phase 2: IBKR via existing ib_insync patterns) | This IS Phase 0 of the price-band engine — one shared OHLCV snapshot |
| IR RSS | per-company feeds | Phase 2, holdings + Tier 1 only | maintenance-heavy; seed list manual |

Refresh schedule v1 (honest for one Windows box; spec's 15–30 min tiers are Phase 2 with IBKR):
EDGAR 15 min RTH · FMP news hourly · analyst endpoints 2×/day · market signals EOD (+ on-demand)
· LES/state recompute after every poll batch and always EOD · immediate review after earnings
(from earnings_dates), abnormal move, analyst action, SEC filing, or material peer event.

## 7. Module layout (script numbers 39–45 are free)

```
expectations_monitor/
  monitor_common.py                    # taxonomy constants, decay math, state machine, DB DDL
  39_sync_monitor_universe.py          # sealed run -> monitor_universe + baseline; auto peer seeding
  40_poll_event_feeds.py               # EDGAR/FMP/AV/Form4 -> raw_items (dedup, feed_state); --loop or one-shot
  41_classify_events.py                # rules first, capped Gemini for ambiguous material items -> events
  42_build_market_signals.py           # OHLCV snapshot -> market_signals_daily (+ market events)
  43_update_expectations_state.py      # decay + LES + states + escalations + alerts + per-run CSV export
  44_validate_expectations_monitor.py  # seal/coverage/audit-trail gates (house validator pattern)
  data/relationship_overrides.csv      # manual peer/customer/supplier map (auto peers from industry)
```

Orchestration: joins the pipeline as a second SOFT group (`monitor`) after `earnings` — WARN-only,
never blocks. The pollers additionally run standalone on a scheduled task between pipeline runs.

## 8. Integration contracts

1. **Starter-price suspension** (future levels engine): any name with state ∈ {watch, deteriorating,
   broken} → entry bands `inactive/thesis_suspended`. Contract: read `expectations_state.csv` of the
   same run; never reach into the monitor DB.
2. **Tier 1 rerank (R6)**: alert only in v1 (`rerank_tier1`); automated rerank is a later decision.
3. **earnings_dates**: `days_until ≤ 5` gates market-signal severity (§3) and post-report triggers review.
4. **Stage 1 is read-only**: the monitor NEVER feeds back into sector scores or the optimizer.
   Like exits/payout it is shadow/advisory until a Stage 11-style validation promotes any of it.

## 9. Phasing

- **Phase 1 (build next):** step 0 = probe FMP free-plan endpoints/limits with the existing key and
  record results in this spec; then 39, 40 (EDGAR+FMP+Form4), 41 (rules only), 42 (EOD), 43, 44,
  orchestration soft-group, alerts to log/CSV.
- **Phase 2:** Gemini-assisted classification (capped), AV news secondary feed, IR RSS for holdings,
  intraday holdings cadence via IBKR, peer read-through automation beyond same-industry.
- **Phase 3:** levels-engine integration (suspension), Tier rerank automation, notification channel.

## 10. Open decisions

1. ~~Folder spelling~~ — RESOLVED 2026-07-22: renamed to `expectations_monitor` (owner decision).
2. FMP free-plan reality (limits/endpoints) — resolved by Phase 1 step 0 probe.
3. Alert delivery in v1: log + alerts table + per-run CSV, or also desktop/email notification?
4. Tier definitions for `monitor_universe.tier`: holdings from ledger; Tier 1/2 mapping source
   (optimizer target book? manual list?) needs one decision.
   Recommended default (config-driven, no manual list): holding (ledger `holding_state`)
   > tier1 (in current final target book; fallback top-quartile investable by `final_score`)
   > tier2 (`rating` >= buy) > candidate (remaining investable).
