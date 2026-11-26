"""Telecom analytics pipeline: question -> plan -> SQL -> rows -> analytics/chart/summary."""
import asyncio
import json
import logging
import re
import sqlite3
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

try:
    import pandas as pd
except ModuleNotFoundError:   # pragma: no cover
    pd = None

from . import llm
from .catalog import Catalog, get_catalog, norm
from .dates import latest_day, parse_dates
from .kpis import (DIMENSIONS, KEY_METRICS, LABEL_COLUMNS, METRICS, SOURCES, TABLE, THRESHOLDS,
                   column_label, metric_of_column)
from .planner import Plan, _find_metrics, parse_question, plan_from_json, plan_json_schema
from .sqlbuild import Resolved, build_sql, resolve_filters
from .sqlguard import execute_readonly, friendly_db_error, json_safe, validate_sql

log = logging.getLogger("yogi.analysis")

MAX_ROWS = 2000          # rows fetched for the answer
UI_ROWS = 1000           # rows sent to the browser
EXPORT_ROWS = 200_000    # rows in a CSV export
CHART_POINTS = 60


class AnalysisMessage(Exception):
    """A user-facing answer that is not a data table (unknown site, no data for dates, ...)."""

    def __init__(self, message: str, suggestions: list | None = None):
        super().__init__(message)
        self.message = message
        self.suggestions = suggestions or []


class NotDataQuestion(Exception):
    """The planner decided the question is not about the KPI data."""


# ====================================================================== result cache (CSV export)

_results: "OrderedDict[str, dict]" = OrderedDict()


def remember_result(result: dict) -> str:
    rid = uuid.uuid4().hex
    _results[rid] = {"sql": result["sql"], "question": result["question"], "created": time.time()}
    while len(_results) > 300:
        _results.popitem(last=False)
    return rid


def cached_result(rid: str) -> dict | None:
    return _results.get(rid)


# ====================================================================== planner prompt

def source_anchor(cat: Catalog, source_id: str) -> date:
    """The day 'today'/'yesterday' mean for a source: its newest complete day."""
    smax = cat.source_range(source_id)[1] if hasattr(cat, "source_range") else cat.date_max
    info = getattr(cat, "source_info", {}).get(source_id) or {}
    last_ts = info.get("last_ts")
    if smax and last_ts is not None and getattr(last_ts, "hour", 23) < 23 and smax > (info.get("date_min") or smax):
        smax = smax - timedelta(days=1)          # the newest day is still loading hour by hour
    return latest_day(date.today(), smax or cat.date_max)


def planner_system_prompt(cat: Catalog, anchor: date) -> str:
    def line(m):
        return (f"  - {m.id}: {m.label} [{m.unit}; "
                f"{'higher is better' if m.better == 'higher' else 'lower is better' if m.better == 'lower' else 'neutral'}]"
                f"{'' if cat.metric_available(m.id) else ' (NO DATA - never use)'} - {m.description}")
    blocks = []
    for sid, src in SOURCES.items():
        if not (cat.source_available(sid) if hasattr(cat, "source_available") else sid == "daily"):
            continue
        smin, smax = cat.source_range(sid) if hasattr(cat, "source_range") else (cat.date_min, cat.date_max)
        dims = ", ".join(d for d in DIMENSIONS if src.supports(d))
        kpis = "\n".join(line(m) for m in METRICS.values() if (m.source or "daily") == sid)
        blocks.append(f'### data_source "{sid}": {src.label}\n{src.description}\n'
                      f"Dates: {smin} to {smax} (reference day {source_anchor(cat, sid)}). Granularity: {src.granularity}. "
                      f"group_by allowed: {dims}.\nKPIs:\n{kpis}")
    sources = "\n\n".join(blocks)
    return f"""You turn questions about a telecom network's KPI data into a JSON query plan.
The plan is executed by code against PostgreSQL. All data is Huawei. Pick ONE data_source per question.

DATA
{cat.summary_text()}
- Site names look like TN30040_3G_Zomba_Dam (code TN30040, technology 3G, physical site "Zomba_Dam"). One physical site has one site name per technology. Users usually say just "Zomba Dam" or the code.
- Cell names look like 3G_Zomba_Dam_U2100F1_1 or Alimenda_G900_1.

DATA SOURCES AND THEIR KPIS (use these ids; every metric in a plan must belong to its data_source)
{sources}

CHOOSING THE DATA SOURCE
- "daily" is the default for CSSR, data success rate, availability, TCH drop rate/drops, traffic, DL/UL volume, down cells, counts - including per-technology filters like "4G availability" or "2G drop rate".
- "hourly": the same KPIs per hour - use when the user says hourly, per hour, hour-wise, intraday, busy/peak hour, time of day, "kis time". Group by "hour" for a time series or "hour_of_day" for a 0-23 pattern.
- "lte" / "nr" / "umts" / "gsm": KPIs that exist only in the technology counters - RRC/S1/E-RAB setup, LTE CSSR/accessibility, E-RAB drop, VoLTE, handover, CSFB, PRB utilization, throughput, CQI, packet loss, users (LTE); SgNB addition, PSCell change (5G); voice CSSR/CDR, soft/IRAT HO, HSDPA, RTWP, Ec/No (3G); SDCCH, TCH blocking/assignment, RxQual, half-rate (2G). These sources are hourly: use "date" for daily trends, "hour"/"hour_of_day" for hourly.
- A technology source already contains only its technology; do not add a different technology filter.

HOW TO FILL THE PLAN
- is_data_question: false only when the question is not about this network's data at all (general chat, coding, definitions like "what is CSSR"). Follow-ups to a previous data answer ARE data questions.
- metrics: the KPIs asked for. "performance", "health", "analysis", "report", "KPIs" with no KPI named -> {", ".join(KEY_METRICS)}. "degraded"/"worst"/"problem" with no KPI -> cssr_bh, data_sr_bh, tch_availability_daily, drop_rate_bh. "drop rate"/"call drop" -> drop_rate_bh; "drops" (count) -> tch_drops_daily. "traffic" -> cs_traffic_daily. "data volume"/"payload" -> total_volume_daily. "down"/"outage"/"off air" cells -> down_cells. Counting cells/sites -> cell_count / site_count.
- Entity filters: put EVERY site, cell, region, technology and vendor the user names into sites / cells / regions / technologies / vendors. Copy names exactly as the user wrote them (or as listed under DETECTED ENTITIES). Never drop a name the user gave, even if you do not recognise it - the code validates names and tells the user if one does not exist. A place name that is not one of the regions is a site.
- "in Lilongwe", "Lilongwe region" -> regions. "North"/"South" are regions. "unknown region"/"no region" -> regions ["Unknown"]. 2G/3G/4G/5G/GSM/UMTS/LTE/NR -> technologies.
- group_by:
  * a ranking or list of entities ("worst cells", "top sites", "which regions") -> group by that entity (cell/site/region/technology/vendor);
  * "worst cells of site X" / "cells of X" -> filter sites [X], group_by ["cell"];
  * several entities compared ("Kasungu vs Balaka", "Lilongwe and Blantyre", "2G vs 3G") -> group by that entity type;
  * trend / daily / over time / "day wise" -> add "date" (or "week"/"month" for weekly/monthly);
  * analysis of ONE site or cell over several days -> ["date"]; over a single day -> ["site"] (or ["cell"]);
  * "by region and technology" etc. -> both;
  * a network-wide question with no grouping words -> ["region"]; "overall"/"total"/"network level" -> [] (one row).
- Dates: each data source has its own reference day (listed above) = its newest day. "today", "yesterday", "latest" -> that reference day only. "last N days" -> the N days ending on it. "this week" -> Monday of that week .. reference day. "this month" -> 1st of that month .. reference day. "last month" -> the whole previous calendar month. Month names -> that whole month (year {anchor.year} unless stated). No date mentioned -> both null (whole history). Dates are inclusive.
- Period comparison ("vs last week", "week over week", "compared to previous month", "April vs May", "degraded compared to last week"): date_from/date_to = the newer period, compare_from/compare_to = the older period, and do NOT group by a time dimension. Otherwise compare_from/compare_to are null.
- thresholds: "below 95%", "less than 99", "above 2%", "95 se kam" -> [{{metric, op, value}}] on the KPI it refers to; group by site (or cell if cells are mentioned) when no grouping is given.
- order_by / order_direction: for rankings. "worst"/"degraded"/"poor" = bad end: asc for higher-is-better KPIs, desc for lower-is-better (drop rate, drops, down cells). "best"/"good" = the opposite. "highest/top/most" = desc, "lowest/least" = asc for neutral KPIs (traffic, volume). In comparisons order_by the KPI to rank by its change. For trends leave order_by null.
- limit: the number the user asks for ("top 5" -> 5). Rankings of sites/cells with no number -> 10. Threshold lists ("sites below 95%") -> null so every breaching entity is listed. Otherwise null.
- chart: "pie" only when the user asks for share/distribution/contribution; "line" for trends; "table" when they ask for a table/raw rows; otherwise "auto".
- title: a short English title for the result.
- custom_sql: null in almost every case. Only when the question cannot be expressed with the fields above (e.g. top N per region with window functions, correlation, day-of-week patterns), write ONE read-only PostgreSQL SELECT over the chosen source's FROM clause only (daily: {TABLE}; hourly: kpi_multi_vendor.c_dashboard_kpis_hourly; technology sources: kpi_huawei.<gsm|umts|lte|nr>_main_kpis k LEFT JOIN gid.cells g ON g.did = k.did::int), using only real columns, aliasing every computed column with AS, never SELECT *, no comments, no semicolons. Still fill metrics/filters as well as you can.
- Follow-up questions ("ab iske cells dikhao", "same for 4G", "and last month?", "only Lilongwe"): start from the PREVIOUS PLAN and change only what the user changed.
"""


def planner_user_message(question: str, mentions, prev_plan: Plan | None, history_text: str) -> str:
    parts = []
    if history_text:
        parts.append("CONVERSATION SO FAR:\n" + history_text)
    if prev_plan:
        parts.append("PREVIOUS PLAN:\n" + json.dumps(prev_plan.to_dict(), ensure_ascii=False))
    detected = [f"- {m.kind}: \"{m.text}\" -> {m.display}" for m in mentions if m.kind != "unknown"]
    unknown = [f"- \"{m.text}\" (not found in the data; still put it in sites or cells)" for m in mentions if m.kind == "unknown"]
    if detected or unknown:
        parts.append("DETECTED ENTITIES (matched against the database):\n" + "\n".join(detected + unknown))
    parts.append("QUESTION:\n" + question)
    return "\n\n".join(parts)


# ====================================================================== history helpers

def last_plan(history: list) -> Plan | None:
    for item in reversed(history or []):
        if item.get("role") == "assistant" and isinstance(item.get("plan"), dict):
            return Plan.from_dict(item["plan"])
    return None


def history_as_text(history: list, limit: int = 6) -> str:
    lines = []
    for item in (history or [])[-limit:]:
        role = "User" if item.get("role") == "user" else "Yogi"
        content = re.sub(r"\s+", " ", str(item.get("content") or ""))[:400]
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


# ====================================================================== plan enforcement

def enforce_question_entities(plan: Plan, mentions, cat: Catalog, question: str, anchor: date) -> list:
    """Make sure nothing the user named is silently dropped from Claude's plan."""
    notes = []
    resolved = resolve_filters(plan, cat)
    for m in mentions:
        if m.kind == "unknown":
            if not any(norm(m.value) in norm(v) or norm(v) in norm(m.value)
                       for v in plan.sites + plan.cells + plan.regions):
                plan.unknown.append(m.value)
            continue
        if m.kind == "site":
            match = cat.resolve_site(m.value)
            if match and not (match.site_names & resolved.site_names or match.cells & (resolved.site_cells | resolved.cells)) \
                    and not (match.how == "contains" and (plan.sites or plan.cells)):
                plan.sites.append(m.value)
        elif m.kind == "cell":
            match = cat.resolve_cell(m.value)
            if match and not (match.cells & (resolved.cells | resolved.site_cells)):
                plan.cells.append(m.value)
        elif m.kind == "region":
            if m.value == "Unknown":
                if not resolved.unknown_region:
                    plan.regions.append("Unknown")
            elif m.value not in resolved.regions:
                plan.regions.append(m.value)
        elif m.kind == "technology" and m.value not in resolved.technologies:
            plan.technologies.append(m.value)
        elif m.kind == "vendor" and m.value.lower() not in [v.lower() for v in resolved.vendors]:
            plan.vendors.append(m.value)
    if plan.date_from is None and plan.date_to is None:
        intent = parse_dates(question, source_anchor(cat, plan.data_source))
        if intent.range and intent.explicit:
            plan.date_from, plan.date_to = intent.range.start, intent.range.end
            notes.append(f"Period: {intent.range.label}.")
    return notes


# ====================================================================== analytics

def _round(v, nd=2):
    if isinstance(v, float):
        return round(v, nd)
    return v


def analyze_rows(rows: list, plan: Plan | None = None, metrics: list | None = None) -> dict:
    base = {"row_count": len(rows), "affected_regions": [], "affected_vendors": [], "top_entities": [],
            "worst_entities": [], "summary_metrics": {}}
    if not rows or pd is None:
        return base
    df = pd.DataFrame(rows)
    groups = plan.group_by if plan else []
    time_col = next((DIMENSIONS[g].alias for g in groups if DIMENSIONS[g].time and DIMENSIONS[g].alias in df), None)
    entity_col = next((DIMENSIONS[g].alias for g in groups if not DIMENSIONS[g].time and DIMENSIONS[g].alias in df), None)
    if entity_col is None:
        entity_col = next((c for c in ["cell_name", "site_name", "region", "technology", "vendor"] if c in df), None)
    numeric = [c for c in df.columns if c not in LABEL_COLUMNS and pd.api.types.is_numeric_dtype(df[c])]
    out = dict(base)
    out["affected_regions"] = sorted(df["region"].dropna().astype(str).unique().tolist())[:20] if "region" in df else []
    out["affected_vendors"] = sorted(df["vendor"].dropna().astype(str).unique().tolist())[:20] if "vendor" in df else []
    summary = {}
    for col in numeric[:12]:
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        if s.empty:
            summary[col] = {"min": None, "max": None, "avg": None, "label": column_label(col)}
            continue
        info = {"label": column_label(col), "min": _round(float(s.min())), "max": _round(float(s.max())),
                "avg": _round(float(s.mean()))}
        m = metric_of_column(col)
        if m:
            info["unit"] = m.unit
            info["better"] = m.better
        if entity_col and m and m.better and not col.endswith("_previous"):
            ranked = df[[entity_col, col]].dropna()
            if time_col:   # trend per entity: rank entities by their average over the period
                ranked = ranked.groupby(entity_col, as_index=False)[col].mean()
            if not ranked.empty:
                # the bad end is the low end for higher-is-better KPIs (values and changes alike)
                asc_bad = m.better == "higher"
                worst = ranked.sort_values(col, ascending=asc_bad).head(5)
                best = ranked.sort_values(col, ascending=not asc_bad).head(5)
                info["worst"] = [{entity_col: str(r[entity_col]), "value": _round(float(r[col]))} for _, r in worst.iterrows()]
                info["best"] = [{entity_col: str(r[entity_col]), "value": _round(float(r[col]))} for _, r in best.iterrows()]
        if m and m.id in THRESHOLDS and entity_col and not col.endswith(("_previous", "_change")):
            op, limit = THRESHOLDS[m.id]
            breaches = (s < limit) if op == "<" else (s > limit)
            info["breaches"] = {"rule": f"{op} {limit:g}", "count": int(breaches.sum()), "of": int(len(s))}
        if time_col and not col.endswith(("_previous", "_change")):
            per_time = df.groupby(time_col)[col].mean(numeric_only=True).dropna().sort_index()
            if len(per_time) >= 2:
                first, last = float(per_time.iloc[0]), float(per_time.iloc[-1])
                info["trend"] = {"first": {"at": str(per_time.index[0]), "value": _round(first)},
                                 "last": {"at": str(per_time.index[-1]), "value": _round(last)},
                                 "change": _round(last - first)}
                info["lowest_at"] = {"at": str(per_time.idxmin()), "value": _round(float(per_time.min()))}
                info["highest_at"] = {"at": str(per_time.idxmax()), "value": _round(float(per_time.max()))}
        summary[col] = info
    out["summary_metrics"] = summary
    # legacy keys, same shape as before: [{<entity column>: name, <metric column>: value}]
    # top = 5 highest values, worst = 5 lowest values
    first_metric = next((c for c in numeric if metric_of_column(c)), numeric[0] if numeric else None)
    if entity_col and first_metric:
        ranked = df[[entity_col, first_metric]].dropna().sort_values(first_metric, ascending=False)
        out["top_entities"] = [{k: json_safe(v) for k, v in r.items()} for r in ranked.head(5).to_dict("records")]
        out["worst_entities"] = [{k: json_safe(v) for k, v in r.items()}
                                 for r in ranked.tail(5).sort_values(first_metric).to_dict("records")]
    return out


# ====================================================================== chart

# Validated categorical palette (fixed order). Dark steps: the app's default theme.
SERIES_COLORS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9", "#e66767"]


def decorate_chart(chart: dict) -> dict:
    """Add the Chart.js colour fields every dataset carried before (API clients render chart_data directly)."""
    if not chart:
        return chart
    for i, ds in enumerate(chart.get("datasets", [])):
        color = SERIES_COLORS[ds.get("slot", i) % len(SERIES_COLORS)]
        if chart.get("type") == "pie":
            ds["backgroundColor"] = [SERIES_COLORS[j % len(SERIES_COLORS)] for j in range(len(chart.get("labels", [])))]
            ds["borderColor"] = "#1a1a19"
            ds["borderWidth"] = 2
            continue
        ds["backgroundColor"] = color + "99"
        ds["borderColor"] = color
        ds["borderWidth"] = 2
        if chart.get("type") == "line":
            ds["fill"] = False
            ds["tension"] = 0.3
    return chart


def build_chart(rows: list, plan: Plan, metrics: list) -> dict:
    """Chart.js-ready config. One y-axis only: KPIs with a different unit stay in the table."""
    return decorate_chart(_build_chart(rows, plan, metrics))


def _build_chart(rows: list, plan: Plan, metrics: list) -> dict:
    if not rows or plan.chart == "table":
        return {}
    keys = list(rows[0].keys())
    groups = [g for g in plan.group_by if DIMENSIONS[g].alias in keys]
    time_dims = [g for g in groups if DIMENSIONS[g].time]
    cat_dims = [g for g in groups if not DIMENSIONS[g].time]
    if not groups:
        return {}
    if plan.compare:
        first = metrics[0]
        cols = [f"{first}_current", f"{first}_previous"]
        label_col = DIMENSIONS[cat_dims[0]].alias if cat_dims else None
        if not label_col:
            return {}
        display = rows[:30]
        return {
            "type": "bar", "unit": METRICS[first].unit, "title": METRICS[first].label,
            "labels": [_label(r, cat_dims) for r in display],
            "datasets": [{"label": "Current period" if c.endswith("_current") else "Previous period",
                          "slot": i, "data": [_num(r.get(c)) for r in display]} for i, c in enumerate(cols)],
        }
    unit = METRICS[metrics[0]].unit
    same_unit = [m for m in metrics if METRICS[m].unit == unit][:4]
    if time_dims:
        t_alias = DIMENSIONS[time_dims[0]].alias
        if cat_dims:
            series_alias = DIMENSIONS[cat_dims[0]].alias
            m = metrics[0]
            totals: dict = {}
            for r in rows:
                totals[str(r.get(series_alias))] = totals.get(str(r.get(series_alias)), 0) + 1
            series = sorted(totals, key=lambda s: (-totals[s], s))[:8]
            series.sort()
            labels = sorted({str(r.get(t_alias)) for r in rows})[-CHART_POINTS * 3:]
            lookup = {(str(r.get(t_alias)), str(r.get(series_alias))): r.get(m) for r in rows}
            return {
                "type": "line", "unit": METRICS[m].unit, "title": METRICS[m].label, "labels": labels,
                "datasets": [{"label": s, "slot": i, "data": [_num(lookup.get((t, s))) for t in labels]}
                             for i, s in enumerate(series)],
                "note": f"Showing {len(series)} of {len(totals)} series" if len(totals) > len(series) else "",
            }
        ordered = sorted(rows, key=lambda r: str(r.get(t_alias)))
        return {
            "type": "line", "unit": unit, "title": " · ".join(METRICS[m].label for m in same_unit),
            "labels": [str(r.get(t_alias)) for r in ordered],
            "datasets": [{"label": METRICS[m].label, "slot": i, "data": [_num(r.get(m)) for r in ordered]}
                         for i, m in enumerate(same_unit)],
        }
    display = rows[:CHART_POINTS if len(cat_dims) == 1 else 40]
    labels = [_label(r, cat_dims) for r in display]
    if plan.chart == "pie" and len(display) <= len(SERIES_COLORS) and unit not in ("%",):
        m = metrics[0]
        return {"type": "pie", "unit": unit, "title": METRICS[m].label, "labels": labels,
                "datasets": [{"label": METRICS[m].label, "slot": 0, "data": [_num(r.get(m)) for r in display]}]}
    return {
        "type": "bar", "unit": unit, "title": " · ".join(METRICS[m].label for m in same_unit),
        "labels": labels,
        "datasets": [{"label": METRICS[m].label, "slot": i, "data": [_num(r.get(m)) for r in display]}
                     for i, m in enumerate(same_unit)],
        "note": f"Chart shows the first {len(display)} of {len(rows)} rows" if len(rows) > len(display) else "",
    }


def _label(row: dict, dims: list) -> str:
    return " · ".join(str(row.get(DIMENSIONS[d].alias, "")) for d in dims) or "All"


def _num(v):
    return round(v, 3) if isinstance(v, float) else v


# ====================================================================== follow-up suggestions

def follow_ups(plan: Plan, r: Resolved, cat: Catalog, metrics: list) -> list:
    out = []
    kpi = METRICS[metrics[0]].label.split(" (")[0] if metrics else "KPIs"
    period = "last 7 days"
    sites = r.labels.get("Sites", [])
    if sites and "cell" not in plan.group_by:
        out.append(f"Worst cells of {sites[0]} by {kpi} {period}")
    if "date" not in plan.group_by and not plan.compare:
        target = sites[0] if sites else (r.regions[0] if r.regions else "network")
        out.append(f"Daily {kpi} trend for {target} {period}")
    if not plan.compare:
        out.append(f"Compare {kpi} with previous period" + (f" for {sites[0]}" if sites else ""))
    if "technology" not in plan.group_by and not r.technologies:
        out.append(f"{kpi} by technology" + (f" for {sites[0]}" if sites else f" in {r.regions[0]}" if r.regions else ""))
    if not sites and "site" not in plan.group_by and "cell" not in plan.group_by:
        where = f" in {r.regions[0]}" if r.regions else ""
        out.append(f"Top 10 degraded sites{where} {period}")
    return out[:4]


# ====================================================================== summary

SUMMARY_SYSTEM_PROMPT = """You are Yogi, DataYog's telecom network analytics assistant.
Summarise KPI query results for network operations engineers.
- Use only the numbers given. Never invent values, sites or causes; you may name likely causes as things to check.
- Reply in the user's language: English question -> English; Hinglish/Hindi -> natural Hinglish (technical terms in English).
- Use markdown with exactly these bold headings, each on its own line: **Overview:**, **Key Findings:**, **Recommendation:**
- Overview: 1-2 sentences - what was analysed (KPIs, entities, period) and the headline.
- Key Findings: 3-5 short bullets with entity names and numbers (2 decimals, with units). Mention threshold breaches (CSSR/Data SR < 95%, Availability < 99%, Drop Rate > 2%) and trend direction when present.
- Recommendation: 1-3 concrete NOC actions (e.g. check alarms, transmission, radio/interference, parameter changes, capacity) tied to the findings.
- If NOTES say a KPI has no data for some technology, or a name was interpreted, say so briefly.
- If there are no rows, say no matching data was found and suggest how to adjust the question.
- Keep it under 180 words. No tables (the user already sees the table and chart)."""


def summary_user_message(result: dict) -> str:
    rows = result["rows"][:150]
    compact = [{k: _round(v) for k, v in row.items()} for row in rows]
    text = json.dumps(compact, ensure_ascii=False, default=str)
    while len(text) > 14000 and len(compact) > 20:
        compact = compact[: int(len(compact) * 0.7)]
        text = json.dumps(compact, ensure_ascii=False, default=str)
    return "\n".join([
        f"QUESTION: {result['question']}",
        f"DATA SOURCE: {result.get('data_source') or 'Daily KPI dashboard'}",
        f"PERIOD: {result.get('period') or 'all available data'}",
        f"FILTERS: {json.dumps(result.get('filters') or {}, ensure_ascii=False)}",
        "KPIS: " + "; ".join(f"{c['key']} = {c['label']} ({c.get('unit') or ''}, {c.get('better') or 'neutral'})"
                             for c in result.get("columns", []) if c.get("metric")),
        f"NOTES: {' '.join(result.get('notes') or []) or 'none'}",
        f"ROW COUNT: {result['row_count']}" + (" (truncated)" if result.get("truncated") else ""),
        f"ANALYTICS: {json.dumps(result.get('analytics', {}), ensure_ascii=False, default=str)[:6000]}",
        f"ROWS ({len(compact)} of {result['row_count']}; ANALYTICS covers all rows - never mention sampling): {text}",
    ])


def _fmt(v, unit=""):
    if v is None:
        return "n/a"
    if isinstance(v, (int, float)):
        text = f"{v:,.2f}" if isinstance(v, float) else f"{v:,}"
        return f"{text}%" if unit == "%" else f"{text} {unit}".strip() if unit in ("Erl",) else text
    return str(v)


def build_local_summary(question: str, rows: list, analytics: dict, result: dict | None = None) -> str:
    """Data-driven summary used when Claude is unavailable."""
    notes = (result or {}).get("notes") or []
    note_line = ("\n\n**Notes**\n" + "\n".join(f"- {n}" for n in notes)) if notes else ""
    if not rows:
        return ("**Overview:**\nNo matching KPI data was found for this question.\n\n"
                "**Key Findings:**\n- The query returned 0 rows.\n\n"
                "**Recommendation:**\n- Check the site/cell names and the date range (data covers "
                f"{(result or {}).get('data_range', 'the loaded period')})." + note_line)
    period = (result or {}).get("period") or "all available data"
    findings = []
    for col, info in list((analytics.get("summary_metrics") or {}).items())[:4]:
        unit = info.get("unit", "")
        line = f"- **{info['label']}**: avg {_fmt(info.get('avg'), unit)} (min {_fmt(info.get('min'), unit)}, max {_fmt(info.get('max'), unit)})"
        if info.get("worst"):
            w = info["worst"][0]
            name = next(v for k, v in w.items() if k != "value")
            line += f"; worst: {name} ({_fmt(w['value'], unit)})"
        if info.get("trend"):
            t = info["trend"]
            line += f"; {t['first']['at']} → {t['last']['at']}: {_fmt(t['first']['value'], unit)} → {_fmt(t['last']['value'], unit)}"
        if info.get("breaches") and info["breaches"]["count"]:
            b = info["breaches"]
            line += f"; {b['count']} of {b['of']} breach {b['rule']}"
        findings.append(line)
    if not findings:
        findings = ["- See the table for the values returned."]
    return (f"**Overview:**\nAnalysed {len(rows)} result rows for: {question} ({period}).\n\n"
            "**Key Findings:**\n" + "\n".join(findings) + "\n\n"
            "**Recommendation:**\n- Prioritise the worst-ranked entities and check alarms, transmission, "
            "radio/interference and recent changes." + note_line)


# ====================================================================== query history (SQLite)

HISTORY_DB = None   # set by main


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
    try:
        init_query_history()
        with sqlite3.connect(HISTORY_DB) as conn:
            conn.execute(
                "INSERT INTO query_history (question, generated_sql, row_count, execution_time, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (question, sql, row_count, execution_time, datetime.utcnow().isoformat()))
    except Exception:
        log.exception("could not save query history")


# ====================================================================== pipeline

@dataclass
class Context:
    engine: object
    question: str
    history: list = field(default_factory=list)
    follow_up: bool = False
    status: object = None        # async callable(str) for progress messages


async def _status(ctx: Context, text: str):
    if ctx.status:
        try:
            await ctx.status(text)
        except Exception:
            pass


async def make_plan(ctx: Context, cat: Catalog, anchor: date, mentions) -> tuple[Plan, list, float]:
    """Claude plan, falling back to the rule parser. Returns (plan, notes, ms)."""
    started = time.perf_counter()
    prev = last_plan(ctx.history)
    notes: list = []
    plan = None
    if llm.available():
        await _status(ctx, "Understanding your question...")
        try:
            data = await llm.structured(
                planner_system_prompt(cat, anchor),
                planner_user_message(ctx.question, mentions, prev, history_as_text(ctx.history)),
                plan_json_schema(), effort=llm.EFFORT_PLAN, timeout=90.0)
            plan = plan_from_json(data)
            if plan is None:
                if ctx.follow_up or mentions and any(m.kind in {"site", "cell"} for m in mentions):
                    plan = None   # the router saw data signals; let the rule parser try
                else:
                    raise NotDataQuestion()
            else:
                notes += plan.notes
                notes += enforce_question_entities(plan, mentions, cat, ctx.question, anchor)
        except NotDataQuestion:
            raise
        except Exception as e:
            log.warning("Claude planner failed, using rule parser: %s", e)
            plan = None
    if plan is None:
        plan = parse_question(ctx.question, cat, anchor, previous=prev, follow_up=ctx.follow_up)
        notes += plan.notes
    # KPIs the user named that have no data must be reported, never silently swapped for another KPI
    requested = [mid for _, mid in _find_metrics(norm(ctx.question))]
    empty = [mid for mid in requested if not cat.metric_available(mid)]
    if empty:
        labels = ", ".join(METRICS[m].label for m in empty)
        if len(empty) == len(requested):
            raise AnalysisMessage(
                f"**{labels}** has no data in the KPI table (the column is empty for every cell), "
                "so I can't rank or analyse it.\n\nAvailable KPIs: CSSR, Data Success Rate, Availability, "
                "TCH Drop Rate / Drops, CS Traffic, DL/UL Volume and Down Cells.",
                ["Top 10 sites by traffic last 7 days", "Top 10 sites by DL volume last 7 days",
                 "Worst 10 sites by availability last 7 days"])
        notes.append(f"{labels} has no data in the KPI table, so it was left out.")
        plan.metrics = [m for m in plan.metrics if m not in empty] or [m for m in requested if m not in empty]
    return plan, notes, round((time.perf_counter() - started) * 1000, 2)


def _date_checks(plan: Plan, cat: Catalog) -> list:
    notes = []
    dmin, dmax = cat.source_range(plan.data_source) if hasattr(cat, "source_range") else (cat.date_min, cat.date_max)
    if not dmin or not dmax:
        return notes
    lo, hi = plan.date_from, plan.date_to
    if plan.compare:
        lo, hi = min(plan.date_from, plan.compare_from), max(plan.date_to, plan.compare_to)
    if lo and hi and (hi < dmin or lo > dmax):
        raise AnalysisMessage(
            f"There is no KPI data for **{lo} to {hi}**. The database has data from "
            f"**{dmin}** to **{dmax}**.",
            [f"Same question for the last 7 days", f"Data for {dmax}"])
    if lo and lo < dmin:
        notes.append(f"Data starts on {dmin}; earlier days are not available.")
    if hi and hi > dmax:
        notes.append(f"Data ends on {dmax}; later days are not loaded yet.")
    if plan.compare:
        def days(a, b):
            a, b = max(a, dmin), min(b, dmax)
            return max(0, (b - a).days + 1)
        cur, prev = days(plan.date_from, plan.date_to), days(plan.compare_from, plan.compare_to)
        if cur != prev and any(METRICS[m].unit in ("Erl", "MB", "count", "cells") for m in plan.metrics if m in METRICS):
            notes.append(f"The current period has {cur} days of data and the previous period {prev} days, "
                         "so totals (traffic, volume, counts) are not like-for-like; compare averages or equal periods.")
    return notes


def drop_empty_rows(rows: list, plan: Plan, metrics: list) -> tuple[list, int]:
    """For site/cell lists, hide rows where none of the requested KPIs has a value (e.g. CSSR for 4G)."""
    if not rows or not metrics or any(DIMENSIONS[g].time for g in plan.group_by) or plan.compare:
        return rows, 0
    if not any(g in {"site", "cell"} for g in plan.group_by):
        return rows, 0
    keep = [r for r in rows if any(r.get(m) is not None for m in metrics)]
    return keep, len(rows) - len(keep)


def _missing_message(missing: list, cat: Catalog) -> AnalysisMessage:
    lines, suggestions = [], []
    for m in missing:
        kind, value = m["kind"], m["value"]
        if kind == "vendor":
            lines.append(f"- Vendor **{value}** is not in the data. Available: {', '.join(cat.vendors)}.")
        elif kind == "region":
            lines.append(f"- Region **{value}** was not found. Regions: {', '.join(cat.regions)}.")
        elif kind == "technology":
            lines.append(f"- Technology **{value}** was not found. Available: {', '.join(cat.technologies)}.")
        else:
            sug = m.get("suggestions") or []
            lines.append(f"- No {kind} named **{value}** was found" + (f". Did you mean: {', '.join(sug[:5])}?" if sug else "."))
            suggestions += sug[:3]
    example = next((g for g in cat.groups.values() if len(g["sites"]) >= 2), None)
    code = next((cat.sites[s]["code"] for s in (example or {}).get("sites", []) if cat.sites[s]["code"]), None)
    tail = "\n\nTip: use a site name" + (f" like **{example['display']}**" if example else "") + \
           (f", a site code like **{code}**" if code else "") + \
           ", or an exact cell name. Start typing in the box to see matching names."
    return AnalysisMessage("I couldn't match some names in your question:\n" + "\n".join(lines) + tail, suggestions)


async def execute_analysis(ctx: Context) -> dict:
    """Plan + run the query. Returns the result dict (summary not yet written)."""
    total_start = time.perf_counter()
    try:
        cat = await asyncio.to_thread(get_catalog, ctx.engine)
    except Exception as e:
        raise AnalysisMessage("Cannot reach the KPI database right now: " + friendly_db_error(e))
    anchor = latest_day(date.today(), cat.date_max)
    mentions = cat.extract(ctx.question)
    plan, notes, plan_ms = await make_plan(ctx, cat, anchor, mentions)

    resolved = resolve_filters(plan, cat)
    if resolved.missing:
        raise _missing_message(resolved.missing, cat)
    notes = resolved.notes + notes + _date_checks(plan, cat)

    await _status(ctx, "Running query on the KPI database...")
    source = plan.source
    sql = None
    rows, truncated = [], False
    metrics_used = []
    exec_start = time.perf_counter()
    if plan.custom_sql:
        try:
            sql = validate_sql(plan.custom_sql, cat.allowed_columns() or _static_columns())
            rows, truncated = await asyncio.to_thread(execute_readonly, ctx.engine, sql, MAX_ROWS)
            source = "claude-sql"
            metrics_used = [m for m in plan.metrics if m in METRICS]
        except Exception as e:
            log.warning("custom SQL rejected/failed (%s); using the plan instead", getattr(e, "detail", e))
            sql = None
            plan.custom_sql = None
    if sql is None:
        try:
            built = build_sql(plan, resolved, cat, MAX_ROWS)
        except ValueError as e:
            raise AnalysisMessage(str(e) + "\n\nAvailable KPIs include CSSR, Data Success Rate, Availability, "
                                  "TCH Drop Rate, CS Traffic, DL/UL Volume and Down Cells.",
                                  ["Top 10 sites by traffic last 7 days", "Top 10 sites by DL volume last 7 days",
                                   "Worst 10 sites by availability last 7 days"])
        notes += built.notes
        metrics_used = built.metrics
        sql = built.sql
        validate_sql(sql, cat.allowed_columns() or _static_columns())
        try:
            rows, truncated = await asyncio.to_thread(execute_readonly, ctx.engine, sql, MAX_ROWS)
        except Exception as e:
            raise AnalysisMessage("The database query failed: " + friendly_db_error(e))
    query_ms = round((time.perf_counter() - exec_start) * 1000, 2)
    rows, hidden = drop_empty_rows(rows, plan, metrics_used)
    if hidden:
        notes.append(f"{hidden} rows with no value for the requested KPIs were hidden "
                     "(for example CSSR and drop rate are not reported for 4G/5G).")
    if truncated:
        notes.append(f"Showing the first {MAX_ROWS} rows; use Export CSV for the full result.")

    columns = []
    for key in (rows[0].keys() if rows else []):
        m = metric_of_column(key)
        columns.append({"key": key, "label": column_label(key), "unit": m.unit if m else None,
                        "better": m.better if m else None, "metric": bool(m),
                        "kind": "change" if key.endswith("_change") else "value"})
    period = _period_label(plan, cat)
    analytics = analyze_rows(rows, plan, metrics_used)
    chart_data = build_chart(rows, plan, metrics_used) if metrics_used and not plan.custom_sql else _generic_chart(rows)
    chart_type = chart_data.get("type", "table") if chart_data else "table"
    result = {
        "question": ctx.question,
        "intent": plan.intent(),
        "title": plan.title or ctx.question,
        "sql": sql,
        "row_count": len(rows),
        "truncated": truncated,
        "rows": rows,
        "columns": columns,
        "chart_type": chart_type,
        "chart_data": chart_data,
        "analytics": analytics,
        "period": period,
        "data_range": "{} to {}".format(*(cat.source_range(plan.data_source) if hasattr(cat, "source_range") else (cat.date_min, cat.date_max))),
        "data_source": SOURCES[plan.data_source].label,
        "filters": resolved.labels,
        "notes": _dedupe(notes),
        "plan": plan.to_dict(),
        "source": source,
        "followups": follow_ups(plan, resolved, cat, metrics_used) if metrics_used else [],
        "metrics": {"sql_generation_ms": plan_ms, "query_execution_ms": query_ms,
                    "total_ms": round((time.perf_counter() - total_start) * 1000, 2)},
    }
    result["result_id"] = remember_result(result)
    save_query_history(ctx.question, sql, len(rows), query_ms)
    return result


def _static_columns() -> set:
    return {c for m in METRICS.values() for c in m.columns} | {"date", "did", "site_name", "cell_name", "region",
                                                                 "vendor", "technology", "status", "latitude", "longitude"}


def _generic_chart(rows: list) -> dict:
    """Chart for custom SQL results: first label column x first numeric columns."""
    if not rows:
        return {}
    keys = list(rows[0].keys())
    label = next((k for k in keys if not isinstance(rows[0][k], (int, float)) or k in LABEL_COLUMNS), None)
    values = [k for k in keys if k != label and isinstance(rows[0][k], (int, float)) and k not in LABEL_COLUMNS][:3]
    if not label or not values:
        return {}
    display = rows[:CHART_POINTS]
    kind = "line" if label in ("date", "week", "month") else "bar"
    return decorate_chart({"type": kind, "unit": "", "title": "", "labels": [str(r.get(label)) for r in display],
            "datasets": [{"label": column_label(v), "slot": i, "data": [_num(r.get(v)) for r in display]}
                         for i, v in enumerate(values)]})


def _period_label(plan: Plan, cat: Catalog) -> str:
    dmin, dmax = cat.source_range(plan.data_source) if hasattr(cat, "source_range") else (cat.date_min, cat.date_max)
    if plan.compare:
        return f"{plan.date_from} to {plan.date_to} vs {plan.compare_from} to {plan.compare_to}"
    if plan.date_from and plan.date_to:
        return str(plan.date_from) if plan.date_from == plan.date_to else f"{plan.date_from} to {plan.date_to}"
    if plan.date_from:
        return f"{plan.date_from} to {dmax}"
    if plan.date_to:
        return f"{dmin} to {plan.date_to}"
    return f"{dmin} to {dmax} (all data)"


def _dedupe(items: list) -> list:
    out = []
    for i in items:
        if i and i not in out:
            out.append(i)
    return out


async def write_summary(result: dict) -> str:
    try:
        return await llm.complete(SUMMARY_SYSTEM_PROMPT, summary_user_message(result),
                                  effort=llm.EFFORT_SUMMARY, timeout=60.0)
    except Exception:
        return build_local_summary(result["question"], result["rows"], result["analytics"], result)


async def run_telecom_analysis(engine, question: str, history: list | None = None, follow_up: bool = False) -> dict:
    """Non-streaming: full result including the summary."""
    result = await execute_analysis(Context(engine=engine, question=question, history=history or [], follow_up=follow_up))
    result["summary"] = await write_summary(result)
    return result
