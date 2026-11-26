# Yogi by DataYog — Telecom KPI Analytics

Chat assistant for telecom network KPIs, powered by Claude API (Sonnet 5.5) and PostgreSQL.
Ask about any site, cell, region, technology or vendor — one or many, combined with any KPI,
date range, ranking, threshold or period comparison — in English or Hinglish.

## How a question is answered

```
question ──► router (main.py) ──► catalog: find real site/cell/region names (catalog.py)
                                   │
                                   ▼
                    Claude query plan (structured JSON)  ── fallback ──► rule parser (planner.py)
                                   │
                                   ▼
          resolve names against the DB (unknown name ⇒ "did you mean" answer, never a silent drop)
                                   │
                                   ▼
          SQL built from the plan (sqlbuild.py) ──► guard + READ ONLY execution (sqlguard.py)
                                   │
                                   ▼
          analytics, chart, follow-up suggestions, summary (analysis.py)
```

## Data sources (all Huawei)

| Source | Table | Granularity | KPIs |
|---|---|---|---|
| `daily` (default) | `kpi_multi_vendor.c_dashboard_kpis` | cell / day | CSSR, data SR, availability, TCH drops & drop rate, traffic, DL/UL volume, down cells |
| `hourly` | `kpi_multi_vendor.c_dashboard_kpis_hourly` | cell / hour | the same KPIs per hour (hourly trends, hour-of-day patterns) |
| `gsm` | `kpi_huawei.gsm_main_kpis` + `gid.cells` | cell / hour | SDCCH congestion/drop, TCH blocking/drop/assignment, RxQual, half-rate, data setup |
| `umts` | `kpi_huawei.umts_main_kpis` + `gid.cells` | cell / hour | voice CSSR/CDR, PS setup, soft/IRAT HO, HSDPA/HSUPA, RTWP, Ec/No |
| `lte` | `kpi_huawei.lte_main_kpis` + `gid.cells` | cell / hour | RRC/S1/E-RAB SR, LTE CSSR, E-RAB drop, VoLTE, HO, CSFB, PRB utilization, throughput, volume, CQI, packet loss, users, availability |
| `nr` | `kpi_huawei.nr_main_kpis` + `gid.cells` | cell / hour | SgNB addition SR, abnormal release, PSCell change, availability, users |

Per-technology tables carry only a cell id (`did`); names come from `gid.cells`. Formulas are in
`app/kpis.py` (standard counter formulas — review with the RF team). The planner picks one source per
question; relative dates use that source's newest complete day.

## Structure

```
app/
  main.py        API routes, routing, SSE streaming, meta answers
  analysis.py    pipeline: plan -> SQL -> rows -> analytics / chart / summary
  catalog.py     cached data catalog + entity resolver (sites, cells, regions, ...)
  planner.py     query plan, Claude plan schema, rule-based parser (offline fallback)
  sqlbuild.py    plan -> SQL, entity resolution, KPI coverage notes
  sqlguard.py    SQL validation + read-only execution
  kpis.py        KPI / dimension registry, KPI knowledge base
  dates.py       relative / calendar date parsing (anchored to the latest data day)
  llm.py         Claude API calls
  database.py    PostgreSQL connection
  templates/     Chat UI
  uploads/       Logo
```

## Setup

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env    # fill SQL_* and ANTHROPIC_API_KEY
```

## Run

```bash
venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 (or `http://localhost:8000/?q=your+question` to ask on load).

## API

| Endpoint | Purpose |
|---|---|
| `POST /chat/stream` | SSE chat. Body `{prompt, history?, events?}`. Without `events` the original (v1) event contract is kept; `"events": "v2"` streams status, data first, then the summary. |
| `POST /chat` | Non-streaming chat (`{response, data?}`) |
| `POST /telecom/query` | Analytics JSON (`{question, history?, max_rows?}`) |
| `GET /api/export/csv?id=…` or `?question=…` | CSV of a result |
| `GET /api/suggest?q=…` | Site / cell / region name autocomplete |
| `GET /api/catalog` | Date range, regions, technologies, counts |
| `GET /api/kpis` | KPI registry with data availability |
| `GET /api/kpi-metadata`, `GET /api/query-history`, `GET /health` | as before |

## Safety

- Queries run in a `READ ONLY` transaction with a 45 s statement timeout; at most 2000 rows are fetched.
- Generated SQL may only read `kpi_multi_vendor.c_dashboard_kpis` and its columns; no comments,
  multiple statements, writes or system functions.
- Works without Claude: the rule parser answers data questions when the API is unavailable.

## Config

Set `CLAUDE_MODEL` in `.env` to change the model (default `claude-sonnet-5-5`).

## Tests (local, not committed)

```bash
BASE_URL=http://127.0.0.1:8000 venv/bin/python tests/e2e_check.py    # API + v1 contract
BASE_URL=http://127.0.0.1:8000 venv/bin/python tests/e2e_matrix.py   # 51 question combinations
```
