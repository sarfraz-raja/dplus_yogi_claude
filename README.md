# Yogi by DataYog — Telecom KPI Analytics

Chat assistant for telecom network KPIs, powered by Claude API (Sonnet 5.5) and PostgreSQL.

## Structure

```
app/
  main.py        API routes + telecom logic
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

Open http://localhost:8000

## Config

Set `CLAUDE_MODEL` in `.env` to change the model (default `claude-sonnet-5-5`).
