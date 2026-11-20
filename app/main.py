from fastapi import FastAPI, Request, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import os
import json
import mimetypes
import re
import sqlite3
import csv
import io
import time
from datetime import date, datetime
from decimal import Decimal
from typing import AsyncGenerator
from sqlalchemy import bindparam, text
try:
    import pandas as pd
except ModuleNotFoundError:
    pd = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)


def load_local_env():
    env_path = os.path.join(PROJECT_DIR, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if value:  # empty values (e.g. ANTHROPIC_API_KEY=) must not shadow real env vars
                os.environ.setdefault(key.strip(), value)


load_local_env()
try:
    from database import engine
    import llm
except ModuleNotFoundError:
    from app.database import engine
    from app import llm

app = FastAPI(title="Yogi by DataYog — Telecom KPI Analytics API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Templates directory - create 'templates' folder and put index.html there
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

SQL_ROW_LIMIT = 100
ALLOWED_TABLE = "kpi_multi_vendor.c_dashboard_kpis"
HISTORY_DB = os.path.join(BASE_DIR, "query_history.db")
KPI_DICTIONARY = {
    "cssr_bh": "Call Setup Success Rate (Busy Hour)",
    "cssr_daily": "Call Setup Success Rate (Daily)",
    "tch_availability_daily": "Network Availability",
    "tch_drops_bh": "TCH Drop Rate (Busy Hour)",
    "tch_drops_daily": "TCH Drop Rate (Daily)",
    "data_sr_bh": "Data Success Rate (Busy Hour)",
    "data_sr_daily": "Data Success Rate (Daily)",
    "ultilization": "Cell Utilization",
    "dl_volume_bh": "Download Volume (Busy Hour)",
    "dl_volume_daily": "Download Volume (Daily)",
    "cs_traffic_bh": "Circuit Switched Traffic (Busy Hour)",
    "cs_traffic_daily": "Circuit Switched Traffic (Daily)",
    "ul_volume_bh": "Upload Volume (Busy Hour)",
    "ul_volume_daily": "Upload Volume (Daily)",
}
ALLOWED_COLUMNS = {
    "date", "did", "site_name", "cell_name", "region", "vendor", "technology", "status",
    "cssr_bh", "cssr_daily",
    "data_sr_bh", "data_sr_daily",
    "tch_availability_daily",
    "tch_drops_bh", "tch_drops_daily",
    "tch_assignments_bh", "tch_assignments_daily",
    "cs_traffic_bh", "cs_traffic_daily",
    "dl_volume_bh", "dl_volume_daily",
    "ul_volume_bh", "ul_volume_daily",
    "ultilization", "normalized_utlilization", "configured_capacity",
}
SQL_IGNORED_WORDS = {
    "select", "from", "where", "and", "or", "not", "null", "is", "in", "like",
    "between", "case", "when", "then", "else", "end", "as", "on", "join",
    "inner", "left", "right", "full", "outer", "group", "by", "order", "limit",
    "offset", "desc", "asc", "distinct", "having", "with", "over", "partition",
    "current_date", "interval", "day", "month", "year", "quarter", "week",
    "hour", "minute", "second", "epoch", "dow", "doy",
    "sum", "avg", "min", "max", "count", "round", "date_trunc", "date_part",
    "coalesce", "cast", "extract", "nullif", "greatest", "least",
    "to_char", "to_date", "to_timestamp", "string_agg", "array_agg",
    "numeric", "float", "integer", "int", "bigint", "smallint", "double",
    "precision", "boolean", "bool", "text", "varchar", "timestamp", "timestamptz",
    "true", "false",
    "ilike", "similar", "escape", "lateral", "cross", "natural", "using",
    "except", "intersect", "union", "all", "exists", "any", "some",
    "rows", "range", "unbounded", "following", "preceding", "current", "row",
    "filter", "within",
}
# Strong terms = unambiguously telecom (reduce false positives)
STRONG_TELECOM_TERMS = {
    "cssr", "tch", "bh", "ultilization", "utilization",
    "cssr_bh", "cssr_daily", "tch_drops", "tch_availability",
    "data_sr", "data_sr_bh", "dl_volume", "cs_traffic",
    "degraded", "kpi", "availability",
}
# Weak terms = could appear in non-telecom context
TELECOM_QUERY_TERMS = {
    "site", "sites", "cell", "cells", "region", "vendor", "technology",
    "traffic", "volume", "drops", "trend", "comparison", "network",
    "voice", "dl", "lte", "gsm", "umts", "4g", "3g", "2g",
}

# Telecom knowledge base — answers KPI definition questions without DB
TELECOM_KB = {
    "cssr": "**CSSR (Call Setup Success Rate)** — Percentage of call setup attempts that successfully complete. Formula: Successful Seizures / Total Attempts × 100. **Higher is better.** Typical target: ≥ 95%.",
    "cssr_bh": "**CSSR_BH** — Call Setup Success Rate measured during the Busy Hour (peak traffic period). Most critical KPI for voice quality assessment.",
    "availability": "**Network Availability (tch_availability_daily)** — Percentage of time a cell is operational. Formula: Available Hours / Total Hours × 100. **Higher is better.** Target: ≥ 99%.",
    "tch_drops": "**TCH Drop Rate** — Percentage of established calls that are dropped before the subscriber ends them. **Lower is better.** Threshold: ≤ 1%.",
    "tch_drops_bh": "**TCH Drops BH** — TCH Drop Rate measured during the Busy Hour. Key indicator of radio link quality and coverage issues.",
    "data_sr": "**Data Success Rate** — Percentage of successful data sessions (GPRS/EDGE/LTE). Higher is better. Indicates data network quality.",
    "data_sr_bh": "**Data SR BH** — Data Success Rate during Busy Hour. Critical for measuring user data experience at peak times.",
    "ultilization": "**Cell Utilization** — Percentage of available cell capacity being used. High utilization (>80%) indicates congestion risk.",
    "utilization": "**Cell Utilization** — Same as ultilization. Percentage of available cell capacity being used. High values indicate need for capacity expansion.",
    "dl_volume": "**DL Volume** — Total data downloaded by users (Downlink Volume). Measured in MB/GB. Indicates data demand.",
    "cs_traffic": "**CS Traffic** — Circuit Switched Traffic, measured in Erlangs. Represents voice traffic load on the network.",
    "vendor": "In this system, **Vendor** refers to the network equipment manufacturer (e.g., Huawei, Ericsson, Nokia) for each cell or site.",
    "bh": "**BH (Busy Hour)** — The one-hour period in a day with the highest traffic load. KPIs measured during BH represent worst-case network conditions.",
    "kpi": "**KPI (Key Performance Indicator)** — Metrics used to evaluate telecom network performance. Main KPIs: CSSR, Availability, TCH Drops, Data SR, Utilization, DL Volume.",
}
FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|replace|grant|revoke|copy|call|do|execute|merge)\b",
    re.IGNORECASE,
)


@app.get("/")
def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html"
    )


def check_database() -> str:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return "connected"
    except Exception:
        return "disconnected"


@app.get("/health")
async def health_check():
    """Reports Claude API and PostgreSQL status. Always HTTP 200 so the UI can render it."""
    llm_status = await llm.ping()
    db_status = check_database()
    healthy = llm_status["llm"] == "connected" and db_status == "connected"
    return JSONResponse({
        "status": "healthy" if healthy else "degraded",
        "service": "Yogi by DataYog",
        "db": db_status,
        **llm_status,
    })


YOGI_SYSTEM_PROMPT = """You are Yogi, an enterprise-grade AI assistant built by DataYog Analytics Pvt. Ltd.

=====================
CORE BEHAVIOR RULES
=====================
- Always give accurate, factual, and useful answers
- Do NOT guess or make up information
- If you are not sure, say:
"I don't have enough information. Please provide more details."
- Keep answers clear, short, and to the point
- Prefer step-by-step explanation when solving problems

=====================
LANGUAGE RULES
=====================
- Default response language: English
- Automatically detect user's language

Adaptive Behavior:
- If user writes in English → reply in English
- If user writes in Hinglish → reply in natural Hinglish
- If user writes in Hindi → reply in simple Hinglish

Guidelines for Hinglish:
- Use natural conversational Hinglish (not formal Hindi)
- Keep technical terms in English
- Example style: "Isme issue ho sakta hai..." instead of pure Hindi

=====================
IDENTITY RULE (STRICT)
=====================
If user asks anything about:
- creator
- developer
- owner
- company
- kisne banaya

You MUST reply EXACTLY:
"I am Yogi, developed by DataYog Analytics Pvt. Ltd."

Do not add anything else.

=====================
RESPONSE STYLE
=====================
- Professional but slightly friendly
- Avoid long paragraphs
- No unnecessary explanation
- No emojis in technical answers

=====================
EXAMPLES
=====================
User: "How to fix this API?"
Answer: "You can fix this by checking the request format and validating the endpoint."

User: "ye API kaam nahi kar rahi"
Answer: "Is API me issue ho sakta hai. Tum logs check karo aur request payload verify karo."

User: "database connect kaise kare?"
Answer: "Database connect karne ke liye tumhe connection string configure karni hogi..."
"""


@app.post("/chat/stream")
async def chat_stream(request: Request):
    """
    Streaming endpoint that returns Server-Sent Events (SSE) with Claude streaming.
    Implements retry logic on client side, but backend also handles graceful errors.
    """
    try:
        body = await request.json()
        prompt = body.get("prompt", "").strip()

        if not prompt:
            raise HTTPException(status_code=400, detail="Prompt is required")

        question_type = classify_question(prompt)

        if question_type == "telecom_db":
            async def generate_telecom_stream() -> AsyncGenerator[str, None]:
                try:
                    result = await run_telecom_analysis(prompt)
                    yield f"data: {json.dumps({'chunk': result['summary']})}\n\n"
                    telecom_payload = json.dumps({
                        "telecom_data": {
                            "rows": result["rows"][:100],
                            "row_count": result["row_count"],
                            "chart_data": result.get("chart_data", {}),
                            "chart_type": result["chart_type"],
                            "intent": result["intent"],
                            "sql": result["sql"],
                            "metrics": result["metrics"],
                            "question": result["question"],
                        }
                    })
                    yield f"data: {telecom_payload}\n\n"
                    yield f"data: {json.dumps({'done': True})}\n\n"
                    yield "data: [DONE]\n\n"
                except Exception as e:
                    detail = friendly_error_detail(e)
                    yield f"data: {json.dumps({'chunk': f'Telecom analytics error: {detail}'})}\n\n"
                    yield f"data: {json.dumps({'done': True})}\n\n"
                    yield "data: [DONE]\n\n"

            return StreamingResponse(
                generate_telecom_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
            )

        if question_type == "off_topic":
            async def generate_off_topic_stream() -> AsyncGenerator[str, None]:
                yield f"data: {json.dumps({'chunk': OFF_TOPIC_RESPONSE})}\n\n"
                yield f"data: {json.dumps({'done': True})}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                generate_off_topic_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
            )

        if question_type == "telecom_knowledge":
            kb_answer = get_kb_answer(prompt)
            if kb_answer:
                async def generate_kb_stream() -> AsyncGenerator[str, None]:
                    yield f"data: {json.dumps({'chunk': kb_answer})}\n\n"
                    yield f"data: {json.dumps({'done': True})}\n\n"
                    yield "data: [DONE]\n\n"

                return StreamingResponse(
                    generate_kb_stream(),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
                )
            # No KB match — fall through to Claude general chat below

        async def generate_stream() -> AsyncGenerator[str, None]:
            try:
                async for chunk in llm.stream_chat(YOGI_SYSTEM_PROMPT, prompt):
                    yield f"data: {json.dumps({'chunk': chunk})}\n\n"
                yield f"data: {json.dumps({'done': True})}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': llm.friendly_error(e)})}\n\n"

        return StreamingResponse(
            generate_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/chat")
async def chat_no_stream(request: Request):
    """Legacy non-streaming endpoint (fallback)"""
    try:
        body = await request.json()
        prompt = body.get("prompt", "").strip()

        question_type = classify_question(prompt)
        if question_type == "off_topic":
            return JSONResponse({"response": OFF_TOPIC_RESPONSE})
        if question_type == "telecom_knowledge":
            kb_answer = get_kb_answer(prompt)
            if kb_answer:
                return JSONResponse({"response": kb_answer})
        if question_type == "telecom_db":
            result = await run_telecom_analysis(prompt)
            return JSONResponse({"response": format_telecom_chat_response(result), "data": result})

        answer = await llm.complete(YOGI_SYSTEM_PROMPT, prompt, effort=llm.EFFORT_CHAT)
        return JSONResponse({"response": answer})
    except Exception as e:
        return JSONResponse({"response": f"Error: {llm.friendly_error(e)}"}, status_code=500)


def json_safe(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def init_query_history():
    with sqlite3.connect(HISTORY_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS query_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                generated_sql TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                execution_time REAL NOT NULL,
                created_at TEXT NOT NULL
            )
        """)


def save_query_history(question: str, sql: str, row_count: int, execution_time: float):
    init_query_history()
    with sqlite3.connect(HISTORY_DB) as conn:
        conn.execute(
            """
            INSERT INTO query_history
            (question, generated_sql, row_count, execution_time, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (question, sql, row_count, execution_time, datetime.utcnow().isoformat()),
        )


def get_postgres_schema() -> str:
    query = text("""
        SELECT table_schema, table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'kpi_multi_vendor'
          AND table_name = 'c_dashboard_kpis'
          AND column_name IN :columns
        ORDER BY table_schema, table_name, ordinal_position
    """).bindparams(bindparam("columns", expanding=True))
    with engine.connect() as conn:
        rows = conn.execute(query, {"columns": list(ALLOWED_COLUMNS)}).all()
    columns = [f"{row.column_name} {row.data_type}" for row in rows]
    return f"- {ALLOWED_TABLE}: {', '.join(columns)}" if columns else ""


def get_available_kpi_columns() -> set:
    query = text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'kpi_multi_vendor'
          AND table_name = 'c_dashboard_kpis'
          AND column_name IN :columns
    """).bindparams(bindparam("columns", expanding=True))
    with engine.connect() as conn:
        rows = conn.execute(query, {"columns": list(ALLOWED_COLUMNS)}).all()
    return {row.column_name.lower() for row in rows}


def detect_intent(question: str) -> str:
    q = question.lower()
    # ranking check before trend so "worst sites this month" → ranking, not trend
    if any(word in q for word in ["top", "highest", "best", "worst", "lowest", "degraded", "below"]):
        return "ranking"
    if any(word in q for word in ["trend", "last 30", "daily", "over time", "monthly", "weekly"]):
        return "trend"
    if any(word in q for word in ["compare", "comparison", "vs", "vendor"]):
        return "comparison"
    if any(word in q for word in ["distribution", "share", "split", "mix"]):
        return "distribution"
    if any(word in q for word in ["month", "last 7", "last 30", "week"]):
        return "trend"
    return "summary"


def classify_question(question: str) -> str:
    """
    Returns one of: 'telecom_db', 'telecom_knowledge', 'general', 'off_topic'
    telecom_db       → needs DB query
    telecom_knowledge → KPI definition / telecom concept question
    general          → tech/coding/general — route to Claude
    off_topic        → completely unrelated — polite redirect
    """
    q = question.lower()
    words = set(re.findall(r"[a-zA-Z_]+", q))

    has_specific_date = bool(re.search(r"\d{4}-\d{2}-\d{2}", question))
    has_strong_term = bool(words & STRONG_TELECOM_TERMS) or any(kpi.lower() in q for kpi in KPI_DICTIONARY)
    has_weak_term = bool(words & TELECOM_QUERY_TERMS)
    analysis_words = {
        "top", "worst", "best", "lowest", "highest", "degraded", "below",
        "trend", "compare", "comparison", "show", "find", "list",
        "yesterday", "today", "month", "days", "last", "week",
        "get", "fetch", "check", "report",
    }
    has_analysis = bool(words & analysis_words)
    strong_analysis = bool(words & {"degraded", "worst", "cssr", "availability", "yesterday", "last", "drops", "tch"})

    # 1. Telecom DB: needs data from database
    if has_specific_date and (has_strong_term or has_weak_term):
        return "telecom_db"
    if has_strong_term and has_analysis:
        return "telecom_db"
    if has_weak_term and has_analysis and (strong_analysis or len(words & TELECOM_QUERY_TERMS) >= 2):
        return "telecom_db"

    # 2. Telecom knowledge: KPI definition questions (no DB needed)
    knowledge_triggers = {"what is", "what are", "define", "explain", "kya hai", "kya hota", "meaning", "full form", "stands for", "difference between"}
    has_knowledge_intent = any(t in q for t in knowledge_triggers)
    if has_knowledge_intent and (has_strong_term or has_weak_term):
        return "telecom_knowledge"

    # 3. DataYog / company questions → Claude general
    if any(t in q for t in ["datayog", "company", "who made", "developer", "creator", "banaya", "kisne"]):
        return "general"

    # 4. General tech/programming → Claude general
    tech_words = {
        "python", "javascript", "java", "code", "program", "function", "class",
        "api", "error", "bug", "debug", "algorithm", "database", "sql", "html",
        "css", "framework", "library", "tutorial", "how to", "install", "docker",
        "machine learning", "ai", "model", "training",
    }
    if bool(words & tech_words) or any(t in q for t in ["how to", "what is the", "how does"]):
        return "general"

    # 5. Completely off-topic (weather, food, movies, sports, etc.)
    off_topic_words = {
        "weather", "food", "movie", "film", "sport", "cricket", "football",
        "recipe", "cook", "restaurant", "hotel", "travel", "flight", "train",
        "joke", "funny", "love", "relationship", "health", "medicine", "doctor",
        "news", "politics", "government", "stock", "share", "price", "buy", "sell",
    }
    if bool(words & off_topic_words):
        return "off_topic"

    return "general"


def is_telecom_analytics_question(question: str) -> bool:
    return classify_question(question) == "telecom_db"


OFF_TOPIC_RESPONSE = """I'm **Yogi by DataYog** — your intelligent guide for network operations, specialized in **Telecom Network KPI Analytics**.

I can help you with:
- Network performance analysis (CSSR, Availability, TCH Drops, Data SR)
- Site and Cell degradation analysis
- Vendor comparison reports
- Traffic trends and KPI insights
- Date-specific KPI queries from the database

Please ask me about your telecom network data. Example queries:
- *"Top 10 degraded sites yesterday"*
- *"Vendor comparison by CSSR"*
- *"Availability below 95% last 7 days"*"""


def get_kb_answer(question: str) -> str | None:
    q = question.lower()
    for key, answer in TELECOM_KB.items():
        if key in q:
            return answer
    return None


def extract_limit(question: str, default: int = 10) -> int:
    """Extract LIMIT N from question like 'top 5', 'worst 20', 'show 50'."""
    m = re.search(r'\b(?:top|worst|best|show|get|fetch|list)\s+(\d+)\b', question.lower())
    if m:
        return min(int(m.group(1)), SQL_ROW_LIMIT)
    return default


def recommend_chart(intent: str, row_count: int) -> str:
    if intent in {"ranking", "comparison"}:
        return "bar"
    if intent == "trend":
        return "line"
    if intent == "distribution":
        return "pie"
    return "table" if row_count <= 10 else "bar"


_NON_METRIC_COLS = {
    "date", "site_name", "cell_name", "region", "vendor", "technology",
    "did", "status", "latitude", "longitude", "operator",
}
_CHART_COLORS = ["#6366f1", "#0ea5e9", "#10b981", "#f59e0b", "#f43f5e"]


def build_chart_data(rows: list, chart_type: str) -> dict:
    if not rows or chart_type == "table":
        return {}
    first = rows[0]
    keys = list(first.keys())
    if chart_type == "line":
        label_col = "date" if "date" in keys else next(
            (c for c in ["region", "vendor", "site_name"] if c in keys), None
        )
    else:
        label_col = next(
            (c for c in ["region", "vendor", "site_name", "cell_name", "technology", "date"] if c in keys),
            None
        )
    value_cols = [
        k for k in keys
        if k.lower() not in _NON_METRIC_COLS
        and isinstance(first.get(k), (int, float))
    ]
    if not label_col or not value_cols:
        return {}
    display = rows[:50]
    datasets = []
    for i, col in enumerate(value_cols[:4]):
        color = _CHART_COLORS[i % len(_CHART_COLORS)]
        ds = {
            "label": KPI_DICTIONARY.get(col, col.replace("_", " ").title()),
            "data": [row.get(col) for row in display],
            "backgroundColor": color + "99",
            "borderColor": color,
            "borderWidth": 2,
        }
        if chart_type == "line":
            ds["fill"] = False
            ds["tension"] = 0.3
        datasets.append(ds)
    return {
        "type": chart_type,
        "labels": [str(row.get(label_col, "")) for row in display],
        "datasets": datasets,
    }


def kpi_prompt_block() -> str:
    return "\n".join(f"- {name}: {label}" for name, label in KPI_DICTIONARY.items())


def telecom_examples_block() -> str:
    return f"""
Question: Top 10 degraded sites yesterday
SQL: SELECT site_name, region, vendor, AVG(cssr_bh) AS cssr_bh, AVG(data_sr_bh) AS data_sr_bh FROM {ALLOWED_TABLE} WHERE date = CURRENT_DATE - INTERVAL '1 day' GROUP BY site_name, region, vendor ORDER BY cssr_bh ASC, data_sr_bh ASC LIMIT 10

Question: Top 10 degraded sites yesterday in Lilongwe
SQL: SELECT site_name, region, vendor, AVG(cssr_bh) AS cssr_bh, AVG(data_sr_bh) AS data_sr_bh FROM {ALLOWED_TABLE} WHERE date = CURRENT_DATE - INTERVAL '1 day' AND region ILIKE '%Lilongwe%' GROUP BY site_name, region, vendor ORDER BY cssr_bh ASC, data_sr_bh ASC LIMIT 10

Question: Worst cells last 7 days
SQL: SELECT cell_name, site_name, region, AVG(cssr_bh) AS cssr_bh, AVG(data_sr_bh) AS data_sr_bh FROM {ALLOWED_TABLE} WHERE date >= CURRENT_DATE - INTERVAL '7 day' GROUP BY cell_name, site_name, region ORDER BY cssr_bh ASC, data_sr_bh ASC LIMIT 10

Question: Worst region by CSSR
SQL: SELECT region, AVG(cssr_bh) AS cssr_bh FROM {ALLOWED_TABLE} GROUP BY region ORDER BY cssr_bh ASC LIMIT 10

Question: Availability below 95%
SQL: SELECT site_name, cell_name, region, vendor, AVG(tch_availability_daily) AS tch_availability_daily FROM {ALLOWED_TABLE} GROUP BY site_name, cell_name, region, vendor HAVING AVG(tch_availability_daily) < 95 ORDER BY tch_availability_daily ASC LIMIT 100

Question: Highest utilization sites
SQL: SELECT site_name, region, vendor, AVG(ultilization) AS ultilization FROM {ALLOWED_TABLE} GROUP BY site_name, region, vendor ORDER BY ultilization DESC LIMIT 10

Question: Vendor comparison
SQL: SELECT vendor, AVG(cssr_bh) AS cssr_bh, AVG(data_sr_bh) AS data_sr_bh, SUM(cs_traffic_daily) AS cs_traffic_daily, AVG(tch_availability_daily) AS tch_availability_daily FROM {ALLOWED_TABLE} GROUP BY vendor ORDER BY vendor LIMIT 100

Question: Traffic trend last 30 days
SQL: SELECT date, SUM(cs_traffic_daily) AS cs_traffic_daily FROM {ALLOWED_TABLE} WHERE date >= CURRENT_DATE - INTERVAL '30 day' GROUP BY date ORDER BY date LIMIT 100

Question: 2026-03-22 get CSSR
SQL: SELECT region, vendor, AVG(cssr_bh) AS cssr_bh, AVG(data_sr_bh) AS data_sr_bh, AVG(tch_availability_daily) AS tch_availability_daily FROM {ALLOWED_TABLE} WHERE date = '2026-03-22' GROUP BY region, vendor ORDER BY region LIMIT 100

Question: CSSR on 2026-03-15
SQL: SELECT region, vendor, AVG(cssr_bh) AS cssr_bh FROM {ALLOWED_TABLE} WHERE date = '2026-03-15' GROUP BY region, vendor ORDER BY region LIMIT 100
""".strip()


def build_rule_based_sql(question: str, available_columns: set) -> str | None:
    q = question.lower()
    has = available_columns.__contains__
    date_filter = ""
    specific_date_match = re.search(r"(\d{4}-\d{2}-\d{2})", question)
    if specific_date_match:
        date_filter = f"WHERE date = '{specific_date_match.group(1)}'"
    elif "yesterday" in q:
        date_filter = "WHERE date = CURRENT_DATE - INTERVAL '1 day'"
    elif "today" in q:
        date_filter = "WHERE date = CURRENT_DATE"
    elif "this week" in q or "current week" in q:
        date_filter = "WHERE date >= DATE_TRUNC('week', CURRENT_DATE)"
    elif "this month" in q or "current month" in q:
        date_filter = "WHERE date >= DATE_TRUNC('month', CURRENT_DATE)"
    elif "last 7" in q or "7 day" in q or ("week" in q and "this" not in q):
        date_filter = "WHERE date >= CURRENT_DATE - INTERVAL '7 day'"
    elif "last 30" in q or "30 day" in q:
        date_filter = "WHERE date >= CURRENT_DATE - INTERVAL '30 day'"
    elif "last month" in q or "previous month" in q:
        date_filter = "WHERE date >= DATE_TRUNC('month', CURRENT_DATE - INTERVAL '1 month') AND date < DATE_TRUNC('month', CURRENT_DATE)"
    # Extract location filter (city/region name after "in <Proper Noun>")
    location_condition = ""
    location_match = re.search(r'\bin\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)', question)
    if location_match:
        location = re.sub(r"[^a-zA-Z0-9 ]", "", location_match.group(1).strip())
        if location:
            location_condition = f"region ILIKE '%{location}%'"

    # Build combined WHERE clause
    where_parts = []
    if date_filter:
        where_parts.append(date_filter[len("WHERE "):])
    if location_condition:
        where_parts.append(location_condition)
    df = " WHERE " + " AND ".join(where_parts) if where_parts else ""
    lim = extract_limit(question)

    # plain KPI summary for a specific date (e.g., "2026-03-22 get CSSR")
    if specific_date_match and not any(word in q for word in ["worst", "best", "top", "trend", "compare", "vendor", "region"]):
        metrics = []
        if has("cssr_bh"):
            metrics.append("AVG(cssr_bh) AS cssr_bh")
        if has("data_sr_bh"):
            metrics.append("AVG(data_sr_bh) AS data_sr_bh")
        if has("tch_availability_daily"):
            metrics.append("AVG(tch_availability_daily) AS tch_availability_daily")
        if has("cs_traffic_daily"):
            metrics.append("SUM(cs_traffic_daily) AS cs_traffic_daily")
        if metrics:
            return f"SELECT region, vendor, {', '.join(metrics)} FROM {ALLOWED_TABLE}{df} GROUP BY region, vendor ORDER BY region LIMIT {SQL_ROW_LIMIT}"

    # availability below threshold
    if "availability" in q and any(word in q for word in ["below", "less", "under", "low"]) and has("tch_availability_daily"):
        threshold_match = re.search(r"(\d+(?:\.\d+)?)\s*%?", q)
        threshold = float(threshold_match.group(1)) if threshold_match else 95.0
        return f"SELECT site_name, cell_name, region, vendor, AVG(tch_availability_daily) AS tch_availability_daily FROM {ALLOWED_TABLE}{df} GROUP BY site_name, cell_name, region, vendor HAVING AVG(tch_availability_daily) < {threshold} ORDER BY tch_availability_daily ASC LIMIT 100"

    # best/top performing sites by CSSR
    if any(word in q for word in ["best", "top", "highest", "good"]) and "site" in q and has("cssr_bh"):
        if not any(word in q for word in ["degraded", "worst", "lowest"]):
            metric = ", AVG(data_sr_bh) AS data_sr_bh" if has("data_sr_bh") else ""
            order = "cssr_bh DESC, data_sr_bh DESC" if has("data_sr_bh") else "cssr_bh DESC"
            return f"SELECT site_name, region, vendor, AVG(cssr_bh) AS cssr_bh{metric} FROM {ALLOWED_TABLE}{df} GROUP BY site_name, region, vendor ORDER BY {order} LIMIT {lim}"

    if ("degraded" in q or "worst" in q or "lowest" in q) and "site" in q and has("cssr_bh"):
        metric = ", AVG(data_sr_bh) AS data_sr_bh" if has("data_sr_bh") else ""
        order = "cssr_bh ASC, data_sr_bh ASC" if has("data_sr_bh") else "cssr_bh ASC"
        return f"SELECT site_name, region, vendor, AVG(cssr_bh) AS cssr_bh{metric} FROM {ALLOWED_TABLE}{df} GROUP BY site_name, region, vendor ORDER BY {order} LIMIT {lim}"
    if "worst" in q and "cell" in q and has("cssr_bh"):
        metric = ", AVG(data_sr_bh) AS data_sr_bh" if has("data_sr_bh") else ""
        return f"SELECT cell_name, site_name, region, AVG(cssr_bh) AS cssr_bh{metric} FROM {ALLOWED_TABLE}{df} GROUP BY cell_name, site_name, region ORDER BY cssr_bh ASC LIMIT {lim}"
    if "vendor" in q and any(word in q for word in ["worst", "lowest", "degraded", "performing"]) and has("cssr_bh"):
        metrics = ["AVG(cssr_bh) AS cssr_bh"]
        order = "cssr_bh ASC"
        if has("data_sr_bh"):
            metrics.append("AVG(data_sr_bh) AS data_sr_bh")
            order += ", data_sr_bh ASC"
        if has("cs_traffic_daily"):
            metrics.append("SUM(cs_traffic_daily) AS cs_traffic_daily")
        return f"SELECT vendor, {', '.join(metrics)} FROM {ALLOWED_TABLE}{df} GROUP BY vendor ORDER BY {order} LIMIT {lim}"
    if "region" in q and any(word in q for word in ["worst", "lowest", "degraded", "performing"]) and has("cssr_bh"):
        metric = ", AVG(data_sr_bh) AS data_sr_bh" if has("data_sr_bh") else ""
        order = "cssr_bh ASC, data_sr_bh ASC" if has("data_sr_bh") else "cssr_bh ASC"
        return f"SELECT region, AVG(cssr_bh) AS cssr_bh{metric} FROM {ALLOWED_TABLE}{df} GROUP BY region ORDER BY {order} LIMIT {lim}"

    # Region comparison / all-regions summary
    if "region" in q and any(w in q for w in ["compare", "comparison", "all region", "by region", "region wise", "regionwise", "each region"]):
        metrics = []
        if has("tch_availability_daily"):
            metrics.append("AVG(tch_availability_daily) AS tch_availability_daily")
        if has("cssr_bh"):
            metrics.append("AVG(cssr_bh) AS cssr_bh")
        if has("data_sr_bh"):
            metrics.append("AVG(data_sr_bh) AS data_sr_bh")
        if has("cs_traffic_daily"):
            metrics.append("SUM(cs_traffic_daily) AS cs_traffic_daily")
        if metrics:
            order_col = metrics[0].split(" AS ")[-1]
            return f"SELECT region, {', '.join(metrics)} FROM {ALLOWED_TABLE}{df} GROUP BY region ORDER BY {order_col} DESC LIMIT 100"

    # General availability summary (no threshold — just show availability by region/vendor)
    if "availability" in q and has("tch_availability_daily"):
        if "vendor" in q:
            extra = ", AVG(cssr_bh) AS cssr_bh" if has("cssr_bh") else ""
            return f"SELECT vendor, AVG(tch_availability_daily) AS tch_availability_daily{extra} FROM {ALLOWED_TABLE}{df} GROUP BY vendor ORDER BY tch_availability_daily DESC LIMIT 100"
        extra = ", AVG(cssr_bh) AS cssr_bh" if has("cssr_bh") else ""
        return f"SELECT region, vendor, AVG(tch_availability_daily) AS tch_availability_daily{extra} FROM {ALLOWED_TABLE}{df} GROUP BY region, vendor ORDER BY tch_availability_daily DESC LIMIT 100"

    # General CSSR summary (no site/cell/worst/best — aggregate by region or vendor)
    if any(w in q for w in ["cssr", "call setup", "call success"]) and has("cssr_bh"):
        if not any(w in q for w in ["site", "cell", "worst", "best", "top", "degraded"]):
            if "vendor" in q:
                return f"SELECT vendor, AVG(cssr_bh) AS cssr_bh FROM {ALLOWED_TABLE}{df} GROUP BY vendor ORDER BY cssr_bh DESC LIMIT 100"
            return f"SELECT region, vendor, AVG(cssr_bh) AS cssr_bh FROM {ALLOWED_TABLE}{df} GROUP BY region, vendor ORDER BY cssr_bh DESC LIMIT 100"

    # TCH drops summary
    if any(w in q for w in ["tch drop", "tch_drop", "drop rate", "drops"]) and has("tch_drops_bh"):
        group_by = "vendor" if "vendor" in q else ("region" if "region" in q else "site_name, region, vendor")
        direction = "ASC" if any(w in q for w in ["worst", "highest", "most"]) else "DESC"
        return f"SELECT {group_by}, AVG(tch_drops_bh) AS tch_drops_bh FROM {ALLOWED_TABLE}{df} GROUP BY {group_by} ORDER BY tch_drops_bh {direction} LIMIT {lim}"

    if ("traffic" in q or "utilization" in q) and "site" in q:
        if "utilization" in q and has("ultilization"):
            direction = "ASC" if any(word in q for word in ["lowest", "worst", "low"]) else "DESC"
            return f"SELECT site_name, region, vendor, AVG(ultilization) AS ultilization FROM {ALLOWED_TABLE}{df} GROUP BY site_name, region, vendor ORDER BY ultilization {direction} LIMIT {lim}"
        if "traffic" in q and has("cs_traffic_daily"):
            direction = "ASC" if any(word in q for word in ["lowest", "worst", "low"]) else "DESC"
            return f"SELECT site_name, region, vendor, SUM(cs_traffic_daily) AS cs_traffic_daily FROM {ALLOWED_TABLE}{df} GROUP BY site_name, region, vendor ORDER BY cs_traffic_daily {direction} LIMIT {lim}"
    if "vendor" in q and ("compare" in q or "comparison" in q):
        metrics = ["AVG(cssr_bh) AS cssr_bh"] if has("cssr_bh") else []
        if has("data_sr_bh"):
            metrics.append("AVG(data_sr_bh) AS data_sr_bh")
        if has("cs_traffic_daily"):
            metrics.append("SUM(cs_traffic_daily) AS cs_traffic_daily")
        if has("tch_availability_daily"):
            metrics.append("AVG(tch_availability_daily) AS tch_availability_daily")
        return f"SELECT vendor, {', '.join(metrics)} FROM {ALLOWED_TABLE} GROUP BY vendor ORDER BY vendor LIMIT 100" if metrics else None
    if "trend" in q and has("date"):
        metric = "SUM(cs_traffic_daily) AS cs_traffic_daily" if has("cs_traffic_daily") else "AVG(cssr_bh) AS cssr_bh"
        return f"SELECT date, {metric} FROM {ALLOWED_TABLE} WHERE date >= CURRENT_DATE - INTERVAL '30 day' GROUP BY date ORDER BY date LIMIT 100"
    return None


def build_telecom_prompt(question: str, schema: str) -> tuple[str, str]:
    """Returns (system, user). The system part is identical for every question, so Claude caches it."""
    return build_telecom_system_prompt(schema), f"USER QUESTION:\n{question}"


def build_telecom_system_prompt(schema: str) -> str:
    return f"""
You are a PostgreSQL SQL generator for telecom analytics.
Use only table {ALLOWED_TABLE}. Write one read-only SELECT query.
Use only the allowed columns shown in the schema. Return SQL only.
Return SQL only. No markdown. No explanation.
Always include LIMIT {SQL_ROW_LIMIT} unless the query is an aggregate returning few rows.

KPI DICTIONARY:
{kpi_prompt_block()}

RANKING DIRECTION RULES:
- "worst", "degraded", "lowest", "below threshold" → ORDER BY metric ASC (lower is worse for CSSR, availability)
- "best", "top", "highest", "good performing" → ORDER BY metric DESC (higher is better for CSSR, availability)
- For traffic/utilization "top/highest" → ORDER BY metric DESC

DATE RULES:
- today = CURRENT_DATE
- yesterday = CURRENT_DATE - INTERVAL '1 day'
- last 7 days = date >= CURRENT_DATE - INTERVAL '7 day'
- last 30 days = date >= CURRENT_DATE - INTERVAL '30 day'
- this month = date >= DATE_TRUNC('month', CURRENT_DATE)
- previous month = date >= DATE_TRUNC('month', CURRENT_DATE - INTERVAL '1 month') AND date < DATE_TRUNC('month', CURRENT_DATE)

TELECOM SQL EXAMPLES:
{telecom_examples_block()}

SCHEMA:
{schema}

Put the single SQL query in the "sql" field of your JSON answer.
""".strip()


def extract_sql(response: str) -> str:
    response = response.strip()
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", response, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        response = fenced.group(1).strip()
    match = re.search(r"\b(with|select)\b", response, flags=re.IGNORECASE)
    if not match:
        raise HTTPException(status_code=400, detail="Claude did not return SQL")
    return response[match.start():].strip().rstrip(";")


def validate_sql(sql: str, available_columns: set | None = None) -> str:
    if not sql:
        raise HTTPException(status_code=400, detail="SQL generation returned empty query")
    allowed_columns = ALLOWED_COLUMNS if available_columns is None else (ALLOWED_COLUMNS & available_columns)
    compact = re.sub(r"\s+", " ", sql).strip()
    if ";" in compact:
        raise HTTPException(status_code=400, detail="Only one SQL statement is allowed")
    if not re.match(r"^(select|with)\b", compact, flags=re.IGNORECASE):
        raise HTTPException(status_code=400, detail="Only SELECT queries are allowed")
    if FORBIDDEN_SQL.search(compact):
        raise HTTPException(status_code=400, detail="Unsafe SQL keyword found")
    if re.search(r"\bselect\s+\*", compact, re.IGNORECASE) or re.search(r"\b[a-zA-Z_]\w*\.\*", compact):
        raise HTTPException(status_code=400, detail="SELECT * is not allowed")
    # Extract CTE names (WITH cte_name AS (...)) so they're valid table references
    cte_names = {m.lower() for m in re.findall(r'\b(\w+)\s+as\s*\(', compact, re.IGNORECASE)}
    table_refs = re.findall(r"\b(?:from|join)\s+([a-zA-Z_][\w.]*)(?:\s+(?:as\s+)?([a-zA-Z_]\w*))?", compact, re.IGNORECASE)
    if not table_refs:
        raise HTTPException(status_code=400, detail="SQL must reference the KPI table")
    aliases = set()
    has_allowed_table = False
    for table_name, alias in table_refs:
        tn = table_name.lower()
        if tn == ALLOWED_TABLE:
            has_allowed_table = True
        elif tn not in cte_names:
            raise HTTPException(status_code=400, detail=f"Table not allowed: {table_name}")
        if alias and alias.lower() not in SQL_IGNORED_WORDS:
            aliases.add(alias.lower())
    if not has_allowed_table and not cte_names:
        raise HTTPException(status_code=400, detail="SQL must reference the KPI table")
    sql_without_literals = re.sub(r"'[^']*'", "", compact)
    selected_aliases = set(re.findall(r"\bas\s+([a-zA-Z_]\w*)", sql_without_literals, re.IGNORECASE))
    sql_for_columns = re.sub(re.escape(ALLOWED_TABLE), "", sql_without_literals, flags=re.IGNORECASE)
    dotted_columns = re.findall(r"\b([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)\b", sql_for_columns)
    for prefix, column in dotted_columns:
        if prefix.lower() not in aliases and prefix.lower() not in cte_names and prefix.lower() != "c_dashboard_kpis":
            raise HTTPException(status_code=400, detail=f"Unknown table alias: {prefix}")
        if column.lower() not in allowed_columns:
            raise HTTPException(status_code=400, detail=f"Column not allowed: {column}")
    cleaned = re.sub(r"\b[a-zA-Z_]\w*\.[a-zA-Z_]\w*\b", "", sql_for_columns)
    identifiers = set(re.findall(r"\b[a-zA-Z_]\w*\b", cleaned))
    ignored = SQL_IGNORED_WORDS | aliases | cte_names | {a.lower() for a in selected_aliases} | {"kpi_multi_vendor", "c_dashboard_kpis"}
    unknown = {
        word for word in identifiers
        if word.lower() not in ignored
        and word.lower() not in allowed_columns
        and not word.isdigit()
    }
    if unknown:
        raise HTTPException(status_code=400, detail=f"Column not allowed: {sorted(unknown)[0]}")
    if not re.search(r"\blimit\s+\d+\b", compact, flags=re.IGNORECASE):
        compact += f" LIMIT {SQL_ROW_LIMIT}"
    return compact


def execute_sql(sql: str):
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        rows = []
        for row in result.mappings().all():
            rows.append({key: json_safe(value) for key, value in row.items()})
        return rows


def rewrite_to_latest_available_date(sql: str) -> str:
    latest_date_expr = f"(SELECT MAX(date) FROM {ALLOWED_TABLE})"
    replacements = {
        "date = CURRENT_DATE - INTERVAL '1 day'": f"date = {latest_date_expr}",
        "date = CURRENT_DATE": f"date = {latest_date_expr}",
        "date >= CURRENT_DATE - INTERVAL '7 day'": f"date >= {latest_date_expr} - INTERVAL '7 day'",
        "date >= CURRENT_DATE - INTERVAL '30 day'": f"date >= {latest_date_expr} - INTERVAL '30 day'",
        "date >= DATE_TRUNC('month', CURRENT_DATE)": f"date >= DATE_TRUNC('month', {latest_date_expr})",
    }
    rewritten = sql
    for old, new in replacements.items():
        rewritten = re.sub(re.escape(old), new, rewritten, flags=re.IGNORECASE)
    return rewritten


def analyze_rows(rows: list) -> dict:
    if pd is None:
        raise HTTPException(status_code=500, detail="pandas is required for analytics processing")
    if not rows:
        return {
            "row_count": 0,
            "affected_regions": [],
            "affected_vendors": [],
            "top_entities": [],
            "worst_entities": [],
            "summary_metrics": {},
        }
    df = pd.DataFrame(rows)
    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    entity_col = next((col for col in ["site_name", "cell_name", "region", "vendor"] if col in df.columns), None)
    metric_col = next((col for col in numeric_cols if col not in {"row_count"}), None)
    top_entities = []
    worst_entities = []
    if entity_col and metric_col:
        ranked = df[[entity_col, metric_col]].dropna().sort_values(metric_col, ascending=False)
        top_entities = ranked.head(5).to_dict("records")
        worst_entities = ranked.tail(5).sort_values(metric_col).to_dict("records")
    return {
        "row_count": int(len(df)),
        "affected_regions": sorted(df["region"].dropna().astype(str).unique().tolist()) if "region" in df else [],
        "affected_vendors": sorted(df["vendor"].dropna().astype(str).unique().tolist()) if "vendor" in df else [],
        "top_entities": top_entities,
        "worst_entities": worst_entities,
        "summary_metrics": {
            col: {
                "min": json_safe(df[col].min()),
                "max": json_safe(df[col].max()),
                "avg": json_safe(df[col].mean()),
            }
            for col in numeric_cols[:5]
        },
    }


async def summarize_result(question: str, sql: str, rows: list, analytics: dict) -> str:
    sample = json.dumps(rows[:50], ensure_ascii=False)
    analytics_json = json.dumps(analytics, ensure_ascii=False)
    user = f"""
Question: {question}
SQL: {sql}
Rows JSON: {sample}
Analytics JSON: {analytics_json}
""".strip()
    return await llm.complete(SUMMARY_SYSTEM_PROMPT, user, effort=llm.EFFORT_SUMMARY, timeout=60.0)


SUMMARY_SYSTEM_PROMPT = """
You are Yogi, DataYog's telecom analytics AI. Summarize the database query results below.
Keep the answer factual and business-friendly.
If rows are empty, say no matching data was found.
Return exactly these sections:
Overview:
Key Findings:
Recommendation:
""".strip()


def build_local_summary(question: str, rows: list, analytics: dict) -> str:
    if not rows:
        return (
            "Overview:\nNo matching KPI data was found for this question.\n\n"
            "Key Findings:\nThe query returned 0 rows.\n\n"
            "Recommendation:\nCheck whether the requested date range and KPI columns exist in the database."
        )
    regions = ", ".join(analytics.get("affected_regions", [])[:5]) or "not included in this aggregate"
    vendors = ", ".join(analytics.get("affected_vendors", [])[:5]) or "not included in this aggregate"
    first = rows[0]
    entity = first.get("site_name") or first.get("cell_name") or first.get("region") or first.get("vendor") or "top entity"
    metric_parts = [
        f"{key}={round(value, 3) if isinstance(value, float) else value}"
        for key, value in first.items()
        if isinstance(value, (int, float)) and key != "row_count"
    ]
    metric_line = f" Main metrics: {', '.join(metric_parts[:4])}." if metric_parts else ""
    data_note = analytics.get("data_note")
    note_line = f"\n\nData Note:\n{data_note}" if data_note else ""
    return (
        f"Overview:\nAnalyzed {len(rows)} KPI rows for: {question}.\n\n"
        f"Key Findings:\nTop impacted entity is {entity}.{metric_line} Affected regions: {regions}. Vendors: {vendors}.\n\n"
        f"Recommendation:\nPrioritize the worst ranked entities and check congestion, radio, transmission, and recent alarm history.{note_line}"
    )


def friendly_error_detail(error: Exception) -> str:
    detail = getattr(error, "detail", None) or llm.friendly_error(error)
    if not detail:
        detail = "Unable to complete telecom analysis. Check PostgreSQL connectivity, KPI table data, and Claude API status."
    return str(detail)


async def run_telecom_analysis(question: str) -> dict:
    total_start = time.perf_counter()
    intent = detect_intent(question)
    schema = get_postgres_schema()
    if not schema:
        raise HTTPException(status_code=500, detail=f"No columns found for {ALLOWED_TABLE}")
    available_columns = get_available_kpi_columns()

    generation_start = time.perf_counter()
    generation_error = None
    rule_sql = build_rule_based_sql(question, available_columns)
    used_rule_sql = bool(rule_sql)
    sql = validate_sql(rule_sql, available_columns) if rule_sql else None
    if not sql:
        for attempt in range(2):
            try:
                retry_context = ""
                if attempt > 0 and generation_error:
                    retry_context = f"\nPrevious attempt failed validation: {getattr(generation_error, 'detail', str(generation_error))}. Fix and return valid SQL only."
                system_prompt, user_prompt = build_telecom_prompt(question, schema)
                generated = await llm.generate_sql(system_prompt, user_prompt + retry_context)
                sql = validate_sql(extract_sql(generated), available_columns)
                used_rule_sql = False
                break
            except Exception as e:
                generation_error = e
        if not sql:
            fallback_sql = build_rule_based_sql(question, available_columns)
            if fallback_sql:
                sql = validate_sql(fallback_sql, available_columns)
                used_rule_sql = True
    sql_generation_ms = round((time.perf_counter() - generation_start) * 1000, 2)
    if not sql:
        raise generation_error or HTTPException(status_code=400, detail="Unable to generate SQL")

    execution_start = time.perf_counter()
    rows = execute_sql(sql)
    data_note = ""
    if not rows and "current_date" in sql.lower():
        latest_sql = validate_sql(rewrite_to_latest_available_date(sql), available_columns)
        if latest_sql != sql:
            latest_rows = execute_sql(latest_sql)
            if latest_rows:
                sql = latest_sql
                rows = latest_rows
                data_note = "No rows matched CURRENT_DATE filter, so analysis used the latest available date in the KPI table."
    query_execution_ms = round((time.perf_counter() - execution_start) * 1000, 2)
    analytics = analyze_rows(rows)
    if data_note:
        analytics["data_note"] = data_note
    chart_type = recommend_chart(intent, len(rows))
    chart_data = build_chart_data(rows, chart_type)
    if used_rule_sql:
        summary = build_local_summary(question, rows, analytics)
    else:
        try:
            summary = await summarize_result(question, sql, rows, analytics)
        except Exception:
            summary = build_local_summary(question, rows, analytics)
    total_ms = round((time.perf_counter() - total_start) * 1000, 2)
    save_query_history(question, sql, len(rows), query_execution_ms)
    return {
        "question": question,
        "intent": intent,
        "sql": sql,
        "row_count": len(rows),
        "chart_type": chart_type,
        "chart_data": chart_data,
        "analytics": analytics,
        "summary": summary,
        "rows": rows,
        "metrics": {
            "sql_generation_ms": sql_generation_ms,
            "query_execution_ms": query_execution_ms,
            "total_ms": total_ms,
        },
    }


def markdown_table(rows: list, limit: int = 10) -> str:
    if not rows:
        return ""
    columns = list(rows[0].keys())[:6]
    header = "| " + " | ".join(columns) + " |"
    divider = "| " + " | ".join(["---"] * len(columns)) + " |"
    lines = [header, divider]
    for row in rows[:limit]:
        values = [str(row.get(col, ""))[:80] for col in columns]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def format_telecom_chat_response(result: dict) -> str:
    table = markdown_table(result["rows"])
    parts = [
        result["summary"],
        "",
        f"**Rows analyzed:** {result['row_count']}",
        f"**Intent:** {result['intent']}",
        f"**Recommended chart:** {result['chart_type']}",
    ]
    if table:
        parts.extend(["", "**Top result rows:**", table])
    parts.extend(["", "**Generated SQL:**", f"```sql\n{result['sql']}\n```"])
    return "\n".join(parts)


@app.post("/telecom/query")
async def telecom_query(request: Request):
    try:
        body = await request.json()
        question = body.get("question") or body.get("prompt") or ""
        question = question.strip()
        if not question:
            raise HTTPException(status_code=400, detail="question is required")

        return JSONResponse(await run_telecom_analysis(question))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/kpi-metadata")
async def kpi_metadata():
    return JSONResponse(KPI_DICTIONARY)


@app.get("/api/query-history")
async def query_history():
    init_query_history()
    with sqlite3.connect(HISTORY_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT question, generated_sql, row_count, execution_time, created_at
            FROM query_history
            ORDER BY created_at DESC
            LIMIT 100
        """).fetchall()
    return JSONResponse([dict(row) for row in rows])


@app.get("/api/export/csv")
async def export_csv(question: str):
    if not question.strip():
        raise HTTPException(status_code=400, detail="question is required")
    result = await run_telecom_analysis(question.strip())
    rows = result["rows"]
    if not rows:
        raise HTTPException(status_code=404, detail="No data found for this query")
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    filename = re.sub(r"[^a-zA-Z0-9_-]", "_", question.strip()[:40])
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
    )



UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")


@app.get("/uploads/{filename}")
def download_file(filename: str):
    safe_name = os.path.basename(filename)  # block ../ path traversal
    file_path = os.path.join(UPLOADS_DIR, safe_name)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    media_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    return FileResponse(
        path=file_path,
        filename=safe_name,
        media_type=media_type,
        content_disposition_type="inline",
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
