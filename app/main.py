from fastapi import FastAPI, Request, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
import csv
import io
import json
import logging
import mimetypes
import os
import re
import sqlite3
import threading
from typing import AsyncGenerator

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
from app.database import engine  # noqa: E402
from app import analysis, llm  # noqa: E402
from app.analysis import AnalysisMessage, NotDataQuestion  # noqa: E402
from app.catalog import _catalog_ready, get_catalog, norm  # noqa: E402
from app.kpis import KPI_DICTIONARY, METRICS, SOURCES, TELECOM_KB  # noqa: E402
from app.planner import Plan, _find_metrics  # noqa: E402
from app.sqlguard import execute_readonly, friendly_db_error, json_safe, validate_sql  # noqa: E402,F401

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("yogi")

HISTORY_DB = os.path.join(BASE_DIR, "query_history.db")
analysis.HISTORY_DB = HISTORY_DB
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
MAX_HISTORY = 10


def _warm_catalog():
    try:
        get_catalog(engine)
    except Exception:
        log.exception("catalog warm-up failed (will retry on the first question)")


@asynccontextmanager
async def lifespan(_app):
    threading.Thread(target=_warm_catalog, daemon=True).start()
    yield


app = FastAPI(title="Yogi by DataYog — Telecom KPI Analytics API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


@app.get("/")
def home(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


def check_database() -> str:
    try:
        rows, _ = execute_readonly(engine, "SELECT 1 AS ok", 1, timeout="5s")
        return "connected" if rows else "disconnected"
    except Exception:
        return "disconnected"


@app.get("/health")
async def health_check():
    """Reports Claude API and PostgreSQL status. Always HTTP 200 so the UI can render it."""
    llm_status, db_status = await asyncio.gather(llm.ping(), asyncio.to_thread(check_database))
    healthy = llm_status["llm"] == "connected" and db_status == "connected"
    cat = _catalog_ready()
    return JSONResponse({
        "status": "healthy" if healthy else "degraded",
        "service": "Yogi by DataYog",
        "db": db_status,
        "catalog": "loaded" if cat else "loading",
        "data_range": f"{cat.date_min} to {cat.date_max}" if cat else None,
        **llm_status,
    })


# ====================================================================== prompts & canned answers

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
NETWORK DATA
=====================
You have the tool query_network_kpis. It queries the telecom KPI database (sites, cells, regions,
technologies 2G/3G/4G/5G, KPIs such as CSSR, availability, drop rate, data success rate, traffic,
data volume, down cells) and shows the user a table, a chart and a summary.
- Call it whenever the user asks about this network's data, performance, sites, cells, regions or KPI values,
  including follow-ups to an earlier data answer. Pass a complete standalone question that resolves
  references to earlier turns (e.g. "ab iske cells dikhao" -> "worst cells of site Zomba_Dam last 7 days").
- Never invent KPI values; get them with the tool.
- Do not call it for general knowledge, definitions or coding questions.

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

DATA_TOOL = {
    "name": "query_network_kpis",
    "description": ("Query the telecom network KPI database and show the user a table, chart and summary. "
                    "Use for any question about network data: sites, cells, regions, technologies, vendors, "
                    "KPIs (CSSR, availability, drop rate, data success rate, traffic, data volume, down cells), "
                    "rankings, trends and comparisons. Input: one complete standalone question."),
    "input_schema": {
        "type": "object",
        "properties": {"question": {"type": "string", "description": "Standalone question about the network data"}},
        "required": ["question"],
        "additionalProperties": False,
    },
    "eager_input_streaming": True,
}

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


# ====================================================================== routing

STRONG_TELECOM_TERMS = {
    "cssr", "tch", "bh", "ultilization", "utilization", "kpi", "kpis", "availability", "degraded",
    "degradation", "erlang", "erlangs", "outage", "outages", "bts", "nodeb", "enodeb", "gnodeb",
}
TELECOM_QUERY_TERMS = {
    "site", "sites", "cell", "cells", "region", "regions", "vendor", "vendors", "technology", "technologies",
    "traffic", "volume", "drops", "drop", "trend", "comparison", "network", "voice", "dl", "ul", "lte",
    "gsm", "umts", "2g", "3g", "4g", "5g", "tower", "towers", "sector", "sectors", "data", "huawei",
}
DATA_WORDS = {
    "show", "list", "give", "get", "fetch", "find", "check", "display", "report", "analysis", "analyse",
    "analyze", "analytics", "performance", "perform", "performing", "health", "status", "summary",
    "summarize", "overview", "trend", "trends", "compare", "comparison", "vs", "versus", "top", "worst",
    "best", "lowest", "highest", "bottom", "below", "above", "under", "less", "more", "greater", "count",
    "total", "average", "avg", "which", "kitna", "kitne", "kitni", "kaisa", "kaise", "kaisi", "dikhao",
    "dikha", "dikhaiye", "batao", "bata", "bataiye", "nikalo", "nikal", "chahiye", "sabse", "kharab",
    "achha", "accha", "wise", "breakdown", "distribution", "share", "split", "degraded", "rank", "ranking",
    "number", "value", "values", "level", "details", "detail", "how", "many", "kaun", "kaunse", "konsa",
    "today", "yesterday", "kal", "aaj", "last", "past", "week", "month", "daily", "weekly", "monthly",
    "days", "din", "hafte", "mahine", "date", "pichle", "pichhle", "latest", "current", "previous",
}
FOLLOW_UP_CUES = {
    "iske", "iska", "iski", "inke", "inka", "inki", "unke", "uska", "uski", "isme", "usme", "inme", "same",
    "also", "bhi", "only", "sirf", "ab", "now", "instead", "those", "these", "them", "it", "its", "that",
    "previous", "above", "more", "breakdown", "drill", "details", "detail", "chart", "graph", "table",
    "aur", "and", "what", "about", "for", "but", "phir", "fir", "wahi", "vahi", "isko", "usko", "inko",
}
KNOWLEDGE_TRIGGERS = ("what is", "what are", "what's", "define", "definition", "explain", "kya hai", "kya hota",
                      "kya hote", "matlab", "meaning", "full form", "stands for", "difference between",
                      "how is", "formula", "calculate", "calculated", "kise kehte")
OFF_TOPIC_WORDS = {
    "weather", "mausam", "food", "movie", "film", "sport", "cricket", "football", "recipe", "cook",
    "restaurant", "hotel", "travel", "flight", "train", "joke", "funny", "love", "relationship", "health",
    "medicine", "doctor", "news", "politics", "government", "stock", "price", "buy", "sell", "song", "gaana",
    "score", "match", "khana",
}
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\b(today|yesterday|aaj|kal|last|past|this|previous|pichle|pichhle)\s+"
                     r"(\d+\s+)?(day|days|week|weeks|month|months|din|hafte|mahine)\b|\b(today|yesterday)\b",
                     re.IGNORECASE)


def _catalog_or_none():
    return _catalog_ready()


def is_follow_up(question: str, history: list) -> bool:
    if not analysis.last_plan(history):
        return False
    words = norm(question).split()
    if len(words) <= 6:
        return True
    return bool(set(words) & FOLLOW_UP_CUES) and len(words) <= 14


def classify_question(question: str, history: list | None = None, cat=None) -> str:
    """
    Returns one of: 'telecom_db', 'telecom_knowledge', 'meta', 'general', 'off_topic'
    telecom_db        → needs DB query
    telecom_knowledge → KPI definition / telecom concept question
    meta              → what data exists (regions, dates, KPIs) - answered from the catalog
    general           → tech/coding/general — Claude (which can still call the data tool)
    off_topic         → completely unrelated — polite redirect
    """
    history = history or []
    cat = cat if cat is not None else _catalog_or_none()
    q = question.lower()
    qn = norm(question)
    words = set(qn.split())
    mentions = cat.extract(question) if cat else []
    named = {m.kind for m in mentions}
    has_entity = bool(named & {"site", "cell"})
    has_place = bool(named & {"region", "technology", "vendor"})
    unknown_with_context = any(m.kind == "unknown" for m in mentions) and bool(words & {"site", "cell", "sites", "cells", "tower"})
    metric_hit = bool(_find_metrics(qn))
    strong = bool(words & STRONG_TELECOM_TERMS) or metric_hit
    weak = bool(words & TELECOM_QUERY_TERMS)
    data_word = bool(words & DATA_WORDS) or bool(re.search(r"\b(how many|kitne|top \d+|\d+ se kam|below \d+)", q))
    has_date = bool(DATE_RE.search(question)) or bool(re.search(r"\d{4}-\d{2}-\d{2}", question))
    knowledge = any(t in q for t in KNOWLEDGE_TRIGGERS)

    if meta_answer(question, cat) is not None:
        return "meta"
    if knowledge and not has_entity and not has_date and (strong or weak) and \
            not (words & {"show", "list", "top", "worst", "best", "dikhao", "batao", "trend", "compare", "kitna", "kitne"}) \
            and not (named & {"region"}):
        return "telecom_knowledge"
    if has_entity or unknown_with_context:
        return "telecom_db"
    if strong and (data_word or has_date or has_place or weak or len(words) <= 5):
        return "telecom_db"
    if (weak or has_place) and (data_word or has_date) and (strong or has_place or len(words & TELECOM_QUERY_TERMS) >= 2
                                                          or words & {"degraded", "worst", "best", "top", "drops", "traffic", "volume"}):
        return "telecom_db"
    if has_place and (weak or strong):
        return "telecom_db"
    if weak and words & {"health", "performance", "report", "status", "overview", "summary", "analysis", "kpi", "kpis"}:
        return "telecom_db"     # "network ka overall health batao", "network performance report"
    if is_follow_up(question, history) and not (words & OFF_TOPIC_WORDS):
        return "telecom_db"
    if strong and not knowledge:
        return "telecom_db"
    if any(t in q for t in ["datayog", "company", "who made", "developer", "creator", "banaya", "kisne"]):
        return "general"
    if knowledge and (strong or weak):
        return "telecom_knowledge"
    if words & OFF_TOPIC_WORDS and not (strong or weak or has_place):
        return "off_topic"
    return "general"


def _telecom_words(question: str) -> bool:
    words = set(norm(question).split())
    return bool(words & (STRONG_TELECOM_TERMS | TELECOM_QUERY_TERMS)) or bool(_find_metrics(norm(question)))


def is_telecom_analytics_question(question: str) -> bool:
    return classify_question(question) == "telecom_db"


def get_kb_answer(question: str) -> str | None:
    qn = f" {norm(question)} "
    for key in sorted(TELECOM_KB, key=len, reverse=True):
        if f" {norm(key)} " in qn:
            return TELECOM_KB[key]
    return None


def meta_answer(question: str, cat) -> str | None:
    """Questions about what data exists, answered from the catalog without SQL."""
    if cat is None:
        return None
    q = question.lower()
    qn = norm(question)
    metric_words = [m for m in _find_metrics(qn)]
    date_question = re.search(r"\b(kab se|kab tak|date range|data range|latest date|last date|which dates|till when|"
                              r"up to which date|data kab|last updated|data freshness)\b", q)
    if metric_words and not date_question and not re.search(r"\b(which|what|kaun|konse|kaunse|list)\s+(kpis?|metrics)\b", q):
        return None
    if re.search(r"\b(date range|data range|kab se|kab tak|latest date|last date|data available|available data|"
                 r"which dates|kitne din ka data|data kab|till when|up to which date|how old is the data|"
                 r"data kitna purana|data freshness|last updated|data ki date)\b", q):
        return (f"The KPI table has data from **{cat.date_min}** to **{cat.date_max}** "
                f"({cat.date_count} days, {cat.row_count:,} cell-day rows).\n\n"
                f"Relative dates like *today*, *yesterday* and *last 7 days* are counted from **{cat.date_max}**, "
                "the latest loaded day.")
    if re.search(r"\b(which|what|list|kaun|kaunse|kaun se|konse|kitne|how many|all|sabhi|saare|sare)\b[\w\s]{0,20}\bregions?\b", q) \
            and not re.search(r"\b(worst|best|top|kpi|cssr|traffic|performance|compare|degraded|sites?|cells?)\b", q):
        counts = {}
        for s in cat.sites.values():
            counts[s["region"] or "Unknown"] = counts.get(s["region"] or "Unknown", 0) + 1
        lines = [f"- **{r}** — {counts.get(r, 0)} site names" for r in cat.regions]
        if cat.region_has_null:
            lines.append(f"- *Unknown* (no region recorded) — {counts.get('Unknown', 0)} site names")
        return "Regions in the KPI data:\n" + "\n".join(lines)
    if re.search(r"\b(which|what|list|kaun|kaunse|kaun se|konse|all)\b[\w\s]{0,20}\b(technologies|technology|techs|vendors?)\b", q) \
            and not re.search(r"\b(worst|best|top|kpi|cssr|traffic|performance|compare|degraded|sites?|cells?|by)\b", q):
        return (f"Technologies: **{', '.join(cat.technologies)}**\n\nVendors: **{', '.join(cat.vendors)}**")
    if re.search(r"\b(which|what|kaun|konse|kaunse|list)\s+(kpis?|metrics)\b|\bwhat data do you have\b|"
                 r"\bwhat can you (do|analy[sz]e)\b|\bkya kya (data|kpi)\b", q):
        lines = []
        for mid in ("cssr_bh", "data_sr_bh", "tch_availability_daily", "drop_rate_bh", "tch_drops_daily", "cs_traffic_daily",
                    "dl_volume_daily", "ul_volume_daily", "down_cells"):
            m = METRICS[mid]
            if cat.metric_available(mid):
                techs = ", ".join(sorted(cat.metric_techs(mid))) or "all"
                lines.append(f"- **{m.label}** — {m.description} ({techs})")
        missing = [METRICS[m].label for m in METRICS
                   if (METRICS[m].source or "daily") == "daily" and not cat.metric_available(m)]
        tail = f"\n\nNot available in the data: {', '.join(missing)}." if missing else ""
        extra = []
        for sid, src in SOURCES.items():
            if sid == "daily" or not cat.source_available(sid):
                continue
            smin, smax = cat.source_range(sid)
            names = [METRICS[m].label.split(" (")[0] for m in METRICS
                     if METRICS[m].source == sid and cat.metric_available(m)][:14]
            extra.append(f"- **{src.label}** ({smin} to {smax}): {', '.join(names)}")
        more = ("\n\n**Hourly and technology KPIs:**\n" + "\n".join(extra)) if extra else ""
        return (f"I can analyse these KPIs for any site, cell, region, technology or date "
                f"({cat.date_min} to {cat.date_max}):\n" + "\n".join(lines) + tail + more +
                "\n\nTry: *worst 10 cells in Lilongwe by drop rate last 7 days*, "
                "*LTE PRB utilization by region yesterday*, or *hourly traffic trend for Lilongwe yesterday*.")
    return None


# ====================================================================== request parsing

def _clean_history(raw) -> list:
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[-MAX_HISTORY:]:
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
            continue
        entry = {"role": item["role"], "content": str(item.get("content") or "")[:2000]}
        if item["role"] == "assistant" and isinstance(item.get("plan"), dict):
            plan = Plan.from_dict(item["plan"])
            if plan:
                entry["plan"] = plan.to_dict()
        out.append(entry)
    return out


def _claude_messages(history: list, prompt: str) -> list:
    messages = []
    for item in history:
        text = item["content"].strip() or "(table and chart shown)"
        if messages and messages[-1]["role"] == item["role"]:
            messages[-1]["content"] += "\n\n" + text
        else:
            messages.append({"role": item["role"], "content": text})
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    if messages and messages[-1]["role"] == "user":
        messages[-1]["content"] += "\n\n" + prompt
    else:
        messages.append({"role": "user", "content": prompt})
    return messages


async def _read_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    return body


def _sse(payload) -> str:
    if payload == "[DONE]":
        return "data: [DONE]\n\n"
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


LEGACY_ROWS = 100   # v1 clients always received at most 100 rows in telecom_data


def _client_payload(result: dict, v2: bool = True) -> dict:
    """telecom_data event. v1 keeps the original 8 keys with their original meaning; the rest are additions."""
    intent = result["intent"]
    if not v2 and intent == "threshold":
        intent = "ranking"      # v1 only ever sent ranking/trend/comparison/distribution/summary
    return {
        "rows": result["rows"][:analysis.UI_ROWS if v2 else LEGACY_ROWS],
        "row_count": result["row_count"],
        "truncated": result.get("truncated", False),
        "columns": result.get("columns", []),
        "chart_data": result.get("chart_data", {}),
        "chart_type": result["chart_type"],
        "intent": intent,
        "title": result.get("title"),
        "sql": result["sql"],
        "metrics": result["metrics"],
        "question": result["question"],
        "period": result.get("period"),
        "filters": result.get("filters", {}),
        "notes": result.get("notes", []),
        "followups": result.get("followups", []),
        "result_id": result.get("result_id"),
        "plan": result.get("plan"),
        "source": result.get("source"),
        "data_source": result.get("data_source"),
    }


# ====================================================================== streaming

async def stream_analysis(question: str, history: list, follow_up: bool, v2: bool = False) -> AsyncGenerator[str, None]:
    """v2: status events -> telecom_data (table/chart) -> summary streamed in chunks -> done.
    v1 (default, original contract): one summary chunk -> telecom_data -> done."""
    queue: asyncio.Queue = asyncio.Queue()

    async def status(text: str):
        await queue.put(text)

    ctx = analysis.Context(engine=engine, question=question, history=history, follow_up=follow_up,
                           status=status if v2 else None)
    task = asyncio.create_task(analysis.execute_analysis(ctx))
    if v2:
        yield _sse({"status": "Finding sites, cells and KPIs in your question..."})
    while not task.done():
        try:
            text = await asyncio.wait_for(queue.get(), timeout=0.25)
            yield _sse({"status": text})
        except asyncio.TimeoutError:
            continue
    try:
        result = task.result()
    except NotDataQuestion:
        async for event in stream_general(question, history, allow_tool=False, v2=v2):
            yield event
        return
    except AnalysisMessage as e:
        yield _sse({"chunk": e.message})
        if e.suggestions:
            yield _sse({"suggestions": e.suggestions})
        yield _sse({"done": True})
        yield _sse("[DONE]")
        return
    except Exception as e:
        log.exception("analysis failed")
        detail = getattr(e, "detail", None) or friendly_db_error(e)
        yield _sse({"chunk": f"Telecom analytics error: {detail}"})
        yield _sse({"done": True})
        yield _sse("[DONE]")
        return

    if not v2:
        result["summary"] = await analysis.write_summary(result)
        yield _sse({"chunk": result["summary"]})
        yield _sse({"telecom_data": _client_payload(result, v2=False)})
        yield _sse({"done": True})
        yield _sse("[DONE]")
        return

    yield _sse({"telecom_data": _client_payload(result)})
    yield _sse({"status": "Writing summary..."})
    wrote = False
    try:
        async for chunk in llm.stream_chat(analysis.SUMMARY_SYSTEM_PROMPT, analysis.summary_user_message(result),
                                           effort=llm.EFFORT_SUMMARY, max_tokens=4000, timeout=60.0):
            wrote = True
            yield _sse({"chunk": chunk})
    except Exception as e:
        log.warning("summary via Claude failed: %s", e)
        if not wrote:
            yield _sse({"chunk": analysis.build_local_summary(question, result["rows"], result["analytics"], result)})
    yield _sse({"done": True})
    yield _sse("[DONE]")


async def stream_general(prompt: str, history: list, allow_tool: bool = True, v2: bool = False) -> AsyncGenerator[str, None]:
    """General chat. Claude may hand a data question to the analytics pipeline via the tool."""
    messages = _claude_messages(history, prompt)
    tool_question = None
    if not llm.available() and allow_tool and _telecom_words(prompt):
        # Claude is down: a question that mentions the network still gets a data answer
        async for event in stream_analysis(prompt, history, follow_up=is_follow_up(prompt, history), v2=v2):
            yield event
        return
    wrote = False
    try:
        tools = [DATA_TOOL] if allow_tool else []
        if tools:
            async for event in llm.stream_chat_with_tools(YOGI_SYSTEM_PROMPT, messages, tools):
                if event[0] == "text":
                    wrote = True
                    yield _sse({"chunk": event[1]})
                elif event[0] == "tool" and event[1] == DATA_TOOL["name"]:
                    q = str(event[2].get("question") or "").strip()
                    if q:
                        tool_question = q[:500]
        else:
            async for chunk in llm.stream_chat(YOGI_SYSTEM_PROMPT, messages):
                wrote = True
                yield _sse({"chunk": chunk})
    except Exception as e:
        if not wrote and allow_tool and _telecom_words(prompt):
            async for event in stream_analysis(prompt, history, follow_up=is_follow_up(prompt, history), v2=v2):
                yield event
            return
        yield _sse({"error": llm.friendly_error(e)})
        if v2:
            yield _sse({"done": True})
            yield _sse("[DONE]")
        return
    if tool_question:
        async for event in stream_analysis(tool_question, history, follow_up=False, v2=v2):
            yield event
        return
    yield _sse({"done": True})
    yield _sse("[DONE]")


async def _single(events: list) -> AsyncGenerator[str, None]:
    for e in events:
        yield _sse(e)


@app.post("/chat/stream")
async def chat_stream(request: Request):
    """Server-Sent Events.

    Request: {"prompt": str, "history"?: [{role, content, plan?}], "events"?: "v2"}
    Default (v1, original contract): {chunk} ... {telecom_data} {done} [DONE]  (summary comes first, in one chunk)
    v2: {status}* {telecom_data} {chunk}* {suggestions}? {done} [DONE]  (table/chart first, summary streamed)
    Both: {error} on a chat failure; {suggestions} after an unmatched-name message.
    """
    body = await _read_body(request)
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")
    prompt = prompt[:2000]
    history = _clean_history(body.get("history"))
    v2 = str(body.get("events") or "").lower() == "v2"
    cat = _catalog_or_none()
    if cat is None:
        try:
            cat = await asyncio.to_thread(get_catalog, engine)
        except Exception:
            cat = None
    question_type = classify_question(prompt, history, cat)

    if question_type == "telecom_db":
        follow_up = is_follow_up(prompt, history)
        return StreamingResponse(stream_analysis(prompt, history, follow_up, v2=v2), media_type="text/event-stream",
                                 headers=SSE_HEADERS)
    if question_type == "meta":
        return StreamingResponse(_single([{"chunk": meta_answer(prompt, cat)}, {"done": True}, "[DONE]"]),
                                 media_type="text/event-stream", headers=SSE_HEADERS)
    if question_type == "off_topic":
        return StreamingResponse(_single([{"chunk": OFF_TOPIC_RESPONSE}, {"done": True}, "[DONE]"]),
                                 media_type="text/event-stream", headers=SSE_HEADERS)
    if question_type == "telecom_knowledge":
        kb_answer = get_kb_answer(prompt)
        if kb_answer:
            return StreamingResponse(_single([{"chunk": kb_answer}, {"done": True}, "[DONE]"]),
                                     media_type="text/event-stream", headers=SSE_HEADERS)
    return StreamingResponse(stream_general(prompt, history, v2=v2), media_type="text/event-stream", headers=SSE_HEADERS)


@app.post("/chat")
async def chat_no_stream(request: Request):
    """Non-streaming endpoint (fallback)."""
    try:
        body = await _read_body(request)
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            return JSONResponse({"response": "Error: Prompt is required"}, status_code=400)
        history = _clean_history(body.get("history"))
        cat = _catalog_or_none()
        question_type = classify_question(prompt, history, cat)
        if question_type == "off_topic":
            return JSONResponse({"response": OFF_TOPIC_RESPONSE})
        if question_type == "meta":
            return JSONResponse({"response": meta_answer(prompt, cat)})
        if question_type == "telecom_knowledge":
            kb_answer = get_kb_answer(prompt)
            if kb_answer:
                return JSONResponse({"response": kb_answer})
        if question_type == "telecom_db":
            try:
                result = await analysis.run_telecom_analysis(engine, prompt, history, is_follow_up(prompt, history))
            except AnalysisMessage as e:
                return JSONResponse({"response": e.message, "suggestions": e.suggestions})
            except NotDataQuestion:
                pass
            else:
                return JSONResponse({"response": format_telecom_chat_response(result),
                                     "data": _limit_rows(result, body.get("max_rows"))})
        answer = await llm.complete(YOGI_SYSTEM_PROMPT, _claude_messages(history, prompt), effort=llm.EFFORT_CHAT)
        return JSONResponse({"response": answer})
    except Exception as e:
        return JSONResponse({"response": f"Error: {llm.friendly_error(e)}"}, status_code=500)


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
        f"**Period:** {result.get('period')}",
        f"**Intent:** {result['intent']}",
        f"**Recommended chart:** {result['chart_type']}",
    ]
    if result.get("notes"):
        parts += ["", "**Notes:** " + " ".join(result["notes"])]
    if table:
        parts.extend(["", "**Top result rows:**", table])
    parts.extend(["", "**Generated SQL:**", f"```sql\n{result['sql']}\n```"])
    return "\n".join(parts)


def _limit_rows(result: dict, max_rows=None) -> dict:
    """JSON endpoints return at most 100 rows by default (as before); clients may ask for up to 2000."""
    try:
        n = int(max_rows) if max_rows is not None else LEGACY_ROWS
    except (TypeError, ValueError):
        n = LEGACY_ROWS
    n = max(1, min(n, analysis.MAX_ROWS))
    out = dict(result)
    out["rows"] = result["rows"][:n]
    return out


@app.post("/telecom/query")
async def telecom_query(request: Request):
    body = await _read_body(request)
    question = str(body.get("question") or body.get("prompt") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    history = _clean_history(body.get("history"))
    try:
        result = await analysis.run_telecom_analysis(engine, question, history, is_follow_up(question, history))
    except AnalysisMessage as e:
        return JSONResponse({"detail": e.message, "question": question, "message": e.message,
                             "suggestions": e.suggestions, "rows": [], "row_count": 0}, status_code=422)
    except NotDataQuestion:
        msg = "This is not a question about the KPI data."
        return JSONResponse({"detail": msg, "question": question, "message": msg,
                             "rows": [], "row_count": 0}, status_code=422)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("telecom query failed")
        raise HTTPException(status_code=500, detail=friendly_db_error(e))
    return JSONResponse(_limit_rows(result, body.get("max_rows")))


@app.get("/api/kpi-metadata")
async def kpi_metadata():
    return JSONResponse(KPI_DICTIONARY)


@app.get("/api/kpis")
async def kpi_registry():
    cat = _catalog_or_none()
    return JSONResponse([{
        "id": m.id, "label": m.label, "unit": m.unit, "better": m.better, "description": m.description,
        "source": m.source or "daily",
        "available": cat.metric_available(m.id) if cat else None,
        "technologies": sorted(cat.metric_techs(m.id)) if cat else None,
    } for m in METRICS.values()])


@app.get("/api/catalog")
async def catalog_info():
    try:
        cat = await asyncio.to_thread(get_catalog, engine)
    except Exception as e:
        raise HTTPException(status_code=503, detail=friendly_db_error(e))
    example_sites = [g["display"] for g in list(cat.groups.values()) if len(g["sites"]) >= 3][:6]
    return JSONResponse({
        "date_min": json_safe(cat.date_min), "date_max": json_safe(cat.date_max), "days": cat.date_count,
        "regions": cat.regions, "technologies": cat.technologies, "vendors": cat.vendors,
        "sites": len(cat.groups), "site_names": len(cat.sites), "cells": len(cat.cells),
        "example_sites": example_sites,
    })


@app.get("/api/suggest")
async def suggest(q: str = ""):
    cat = _catalog_or_none()
    if cat is None or len(q.strip()) < 2:
        return JSONResponse([])
    return JSONResponse(cat.autocomplete(q.strip()[:80]))


@app.get("/api/query-history")
async def query_history():
    analysis.init_query_history()
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
async def export_csv(question: str = "", id: str = ""):
    sql, name = None, question
    if id:
        cached = analysis.cached_result(id)
        if cached:
            sql, name = cached["sql"], cached["question"]
    if sql is None:
        if not question.strip():
            raise HTTPException(status_code=400, detail="question is required")
        try:
            result = await analysis.execute_analysis(analysis.Context(engine=engine, question=question.strip()))
        except AnalysisMessage as e:
            raise HTTPException(status_code=404, detail=e.message)
        sql = result["sql"]
    try:
        cat = await asyncio.to_thread(get_catalog, engine)
        validate_sql(sql, set(cat.columns))
        rows, _ = await asyncio.to_thread(execute_readonly, engine, sql.rsplit("LIMIT", 1)[0] + f" LIMIT {analysis.EXPORT_ROWS}"
                                          if re.search(r"\nLIMIT \d+$", sql) else sql, analysis.EXPORT_ROWS)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=friendly_db_error(e))
    if not rows:
        raise HTTPException(status_code=404, detail="No data found for this query")
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    filename = re.sub(r"[^a-zA-Z0-9_-]", "_", (name or "export").strip()[:40]) or "export"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
    )


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
