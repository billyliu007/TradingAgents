# CLAUDE.md — TradingAgents API Wrapper

Guidance for AI assistants working in this repository.

---

## Repository Overview

**TradingAgents API Wrapper** is a FastAPI HTTP service that wraps the [TradingAgents](https://github.com/TauricResearch/TradingAgents) multi-agent LLM financial analysis framework. It exposes a REST + WebSocket API and serves a single-page dark-themed web UI (`service/static/index.html`).

The core analysis is performed by a LangGraph agent graph (`TradingAgentsGraph`) that orchestrates multiple AI personas (analysts, researchers, traders, risk managers) to produce a structured trading recommendation and PDF report.

---

## Directory Structure

```
TradingAgentsAPIWrapper/
├── service/                    # FastAPI web service
│   ├── app.py                  # Main application: routes, job queue, WebSocket, PDF
│   ├── pdf_export.py           # PDF report generation (fpdf2)
│   ├── static/
│   │   └── index.html          # Single-page web UI (all CSS+JS inline)
│   └── fonts/                  # DejaVuSans TTF fonts for PDF
├── tradingagents/              # Core agent framework
│   ├── default_config.py       # DEFAULT_CONFIG dict (all tuneable knobs)
│   ├── graph/
│   │   ├── trading_graph.py    # TradingAgentsGraph — main entry point
│   │   ├── setup.py            # Graph node/edge construction
│   │   ├── conditional_logic.py
│   │   ├── propagation.py
│   │   ├── reflection.py
│   │   └── signal_processing.py
│   ├── agents/
│   │   ├── analysts/           # market, fundamentals, news, social_media
│   │   ├── researchers/        # bull, bear
│   │   ├── managers/           # research_manager, portfolio_manager
│   │   ├── risk_mgmt/          # aggressive, conservative, neutral debators
│   │   ├── trader/             # trader
│   │   └── utils/              # AgentState, tools, memory
│   ├── dataflows/              # Data vendor adapters
│   │   ├── interface.py        # Public tool interface
│   │   ├── y_finance.py        # yfinance adapter (default)
│   │   ├── alpha_vantage*.py   # Alpha Vantage adapters
│   │   └── config.py           # Vendor config switcher
│   └── llm_clients/            # LLM provider abstraction
│       ├── factory.py          # create_llm_client()
│       ├── openai_client.py
│       ├── anthropic_client.py
│       └── google_client.py
├── cli/                        # Typer CLI (tradingagents command)
│   ├── main.py
│   └── utils.py
├── tests/                      # Pytest tests
├── pyproject.toml              # Package metadata + dependencies
├── render.yaml                 # Render.com deployment blueprint
├── .env.example                # Environment variable template
└── CLAUDE.md                   # This file
```

---

## Key Entry Points

| Entry Point | Purpose |
|---|---|
| `service/app.py` | FastAPI app — import with `from service.app import app` |
| `uvicorn service.app:app` | Start the HTTP server |
| `tradingagents` CLI | `tradingagents-service` starts the service; `tradingagents` is the CLI |
| `TradingAgentsGraph` | Core graph in `tradingagents/graph/trading_graph.py` |

---

## Service Architecture (`service/app.py`)

### Job Lifecycle

1. Client `POST /api/jobs` → creates a job dict in `_jobs`, submits to `ThreadPoolExecutor`
2. Background worker `_run_analysis_job()` calls `_execute_analysis()`
3. `_execute_analysis()` instantiates `TradingAgentsGraph`, runs `.propagate()`, generates PDF
4. Events are emitted via `_emit_event()` → stored in `job["event_log"]`
5. Client connects `WebSocket /ws/job/{job_id}` → receives replayed + live events

### In-Memory Job Store

```python
_jobs: dict[str, dict]  # job_id → job dict (lost on server restart)
MAX_JOBS_STORE = 100    # oldest jobs pruned when limit exceeded
```

Job dict keys: `status`, `ticker`, `created_at`, `completed_at`, `decision`, `pdf_filename`, `pdf_download_url`, `error`, `event_log`, `error_notes`, `cancel_event`.

### WebSocket Event Types

| Event `type` | When emitted |
|---|---|
| `ping` | Keepalive (ignore) |
| `data_fetched` | Tool returns data (fields: `analyst`, `tool`, `label`, `preview`) |
| `data_error` | Tool raises exception |
| `analyst_complete` | An analyst agent finishes (fields: `analyst`, `content`, `title`) |
| `debate_message` | Bull/bear debate turn (fields: `role`, `content`) |
| `risk_message` | Risk review turn (fields: `role`, `content`) |
| `job_complete` | Analysis done (fields: `decision`, `pdf_filename`, `pdf_download_url`) |
| `job_cancelled` | User cancelled |
| `job_failed` | Unhandled exception |

### REST API Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Serve `index.html` |
| `GET` | `/api/options` | Default config values for UI |
| `GET` | `/api/logs?limit=N` | Server log buffer (max 1000 lines) |
| `DELETE` | `/api/logs` | Clear log buffer |
| `POST` | `/api/jobs` | Submit analysis job |
| `GET` | `/api/jobs/{job_id}` | Poll job status |
| `POST` | `/api/jobs/{job_id}/cancel` | Cancel running job |
| `GET` | `/api/jobs` | List last 20 jobs |
| `GET` | `/api/exports` | List PDF exports |
| `GET` | `/api/exports/download/{filename}` | Download a PDF |
| `POST` | `/api/exports/zip` | Download selected PDFs as zip |
| `GET` | `/api/exports/all.zip` | Download all PDFs as zip |
| `WebSocket` | `/ws/job/{job_id}` | Live event stream |

### Security Notes

- `_safe_export_basename()` validates filenames before filesystem access (path traversal prevention)
- `_resolve_export_file()` enforces that resolved paths stay within `_exports_dir()`
- Never remove these checks

---

## Agent Framework (`tradingagents/`)

### Agent Pipeline Order

```
Analysts (parallel):         market → fundamentals → social → news
                                          ↓
Researchers (debate):            bull ↔ bear (N rounds)
                                          ↓
Research Manager:             synthesises researcher debate
                                          ↓
Trader:                       proposes a trade plan
                                          ↓
Risk Managers (debate):   aggressive ↔ conservative ↔ neutral (N rounds)
                                          ↓
Portfolio Manager:            final BUY/SELL/HOLD decision
```

### Configuration (`tradingagents/default_config.py`)

All behaviour is controlled by `DEFAULT_CONFIG`. The service merges user-supplied request fields into this dict when constructing `TradingAgentsGraph`. Key fields:

```python
{
    "llm_provider":          "openai",       # openai | anthropic | google | xai | openrouter
    "deep_think_llm":        "gpt-5.2",      # Model for complex reasoning
    "quick_think_llm":       "gpt-5-mini",   # Model for faster tasks
    "backend_url":           "https://api.openai.com/v1",
    "max_debate_rounds":     1,              # Bull/bear debate iterations
    "max_risk_discuss_rounds": 1,            # Risk team debate iterations
    "data_vendors": {                        # Per-category data source
        "core_stock_apis":      "yfinance",  # or "alpha_vantage"
        "technical_indicators": "yfinance",
        "fundamental_data":     "yfinance",
        "news_data":            "yfinance",
    },
}
```

### LLM Client Factory

`create_llm_client(provider, model, config)` in `tradingagents/llm_clients/factory.py` returns a LangChain-compatible LLM. Supported providers: `openai`, `anthropic`, `google`, `xai`, `openrouter`.

### Data Vendors

Tools in `tradingagents/agents/utils/agent_utils.py` route calls through `tradingagents/dataflows/interface.py` which dispatches to the configured vendor. Default is **yfinance** (no API key needed). **Alpha Vantage** requires `ALPHA_VANTAGE_API_KEY`.

---

## Frontend (`service/static/index.html`)

Single HTML file with all CSS and JavaScript inline (~700 lines total). No build step, no bundler.

### Layout

- **Desktop (>1200px)**: 3-column grid — Form | Chat Feed | Data Feed
- **Tablet (768–1200px)**: 2-column — Form | Chat Feed (Data Feed hidden)
- **Mobile (≤767px)**: Tab-based navigation with bottom tab bar

### Mobile Tab Navigation

The tab bar (`<nav class="mobile-tabs">`) has 3 tabs: Setup (📋), Research (💬), Data (⚡). JS in an IIFE at the bottom of the file manages tab switching via `.mobile-active` class. Red dot badges appear on inactive tabs when new content arrives (via `MutationObserver`).

### Key JavaScript Patterns

- `TEAM` object maps agent role keys → `{name, role, avatar, color}` (names are randomised on load)
- `addChatMsg(role, content)` renders a chat bubble in the feed
- `addDataEntry(event)` renders a data card in the data feed
- `connectWS(jobId)` opens the WebSocket and dispatches all event types
- `setProgress(pct, label)` updates the progress bar (only moves forward — never decreases)
- Local storage keys `ta_job_v2` and `ta_ticker_v2` persist the active job across refreshes

### Frontend Conventions

- Keep all CSS/JS inline — no external files or build tools
- Use CSS custom properties (`--blue`, `--green`, etc.) for all colours
- Never shrink touch targets below 44px height on mobile
- `font-size: 16px` on inputs prevents iOS auto-zoom
- All user-generated content must pass through `escHtml()` before insertion

---

## Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | One of these | LLM provider key |
| `ANTHROPIC_API_KEY` | ↑ | |
| `GOOGLE_API_KEY` | ↑ | |
| `XAI_API_KEY` | ↑ | |
| `OPENROUTER_API_KEY` | ↑ | |
| `ALPHA_VANTAGE_API_KEY` | No | Needed only if using Alpha Vantage data vendor |
| `TRADINGAGENTS_RESULTS_DIR` | No | Where raw agent results are saved (default: `./results`) |
| `TRADINGAGENTS_EXPORTS_DIR` | No | Where PDF exports are saved (default: `./exports`) |
| `TRADINGAGENTS_SERVICE_HOST` | No | Uvicorn bind host (default: `0.0.0.0`) |

Copy `.env.example` to `.env` and fill in at least one LLM provider key.

---

## Development Workflow

### Running the service locally

```bash
# Install dependencies
pip install -e .

# Copy env template and fill in keys
cp .env.example .env

# Start server (default port 8000)
uvicorn service.app:app --reload
# Open http://localhost:8000
```

### Running tests

```bash
pytest tests/
```

### Deployment (Render.com)

`render.yaml` is a Render Blueprint. Push to `main` branch, then create a new Blueprint in the Render dashboard pointing to the repo. Set secret env vars (API keys) in the Render dashboard — they are marked `sync: false` in the YAML.

---

## Key Conventions

1. **One LLM key required at runtime** — the service will error if no provider key is set.
2. **Jobs are in-memory only** — server restart loses all pending/running jobs. PDFs on disk survive restarts; clients should save the PDF download URL.
3. **PDF filenames are validated** — `_safe_export_basename()` and `_resolve_export_file()` in `app.py` prevent path traversal. Do not weaken these checks.
4. **Progress only moves forward** — `setProgress()` uses `Math.max(progress, pct)`, never steps backward. UI convention to maintain.
5. **Agent role keys are fixed strings**: `market`, `fundamentals`, `social`, `news`, `bull`, `bear`, `research_manager`, `trader`, `aggressive`, `conservative`, `neutral`, `portfolio`. These are used as CSS class names, TEAM keys, WebSocket event fields, and analyst selection values — keep them consistent across all layers.
6. **yfinance is the default data vendor** — requires no API key. Alpha Vantage is an opt-in override per data category.
7. **Thread safety** — `_jobs` and `_log_buffer` are protected by `_jobs_lock` and `_log_lock` respectively. Always acquire the lock when reading or writing these.
8. **Cancel via `ThreadEvent`** — `cancel_event.set()` signals the background worker; the graph checks it at checkpoints and raises `InterruptedError`.

---

## What Was Removed

- **Google Drive integration** (removed): `service/gdrive.py` deleted; `gdrive_upload_pdf` import, `gdrive_url` fields, and the upload try/except block removed from `app.py`; `GDRIVE_*` env vars removed from `render.yaml` and `.env.example`; `google-api-python-client` removed from `pyproject.toml`.
