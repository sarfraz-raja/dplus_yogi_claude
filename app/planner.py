"""Query plans.

A ``Plan`` is a structured description of an analytics question: which KPIs, grouped
by what, filtered to which sites/cells/regions/technologies/vendors, over which dates,
ranked how. Claude produces plans (``plan_from_json``); when Claude is unavailable the
rule parser (``parse_question``) produces the same structure. SQL is then built from
the plan deterministically (see ``sqlbuild``), so every answer is safe and consistent.
"""
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from .catalog import Catalog, norm
from .dates import DateIntent, parse_dates, parse_iso
from .kpis import (DEGRADATION_METRICS, DIMENSIONS, KEY_METRICS, METRICS, SOURCE_KEY_METRICS, SOURCES, TRAFFIC_METRICS,
                   metric_source, translate_metric)

MAX_LIMIT = 1000
CHARTS = ("auto", "bar", "line", "pie", "table")
OPS = ("<", "<=", ">", ">=")


@dataclass
class Plan:
    metrics: list = field(default_factory=list)
    group_by: list = field(default_factory=list)
    sites: list = field(default_factory=list)
    cells: list = field(default_factory=list)
    regions: list = field(default_factory=list)
    technologies: list = field(default_factory=list)
    vendors: list = field(default_factory=list)
    date_from: date | None = None
    date_to: date | None = None
    compare_from: date | None = None
    compare_to: date | None = None
    thresholds: list = field(default_factory=list)      # [{"metric", "op", "value"}]
    order_by: str | None = None                          # metric id or dimension id
    order_direction: str | None = None                   # asc | desc
    limit: int | None = None
    chart: str = "auto"
    title: str = ""
    custom_sql: str | None = None
    unknown: list = field(default_factory=list)          # names the parser could not match
    source: str = "rules"                                # claude | rules (who made the plan)
    data_source: str = "daily"                           # which table: see kpis.SOURCES
    date_label: str = ""
    notes: list = field(default_factory=list)            # notes produced while normalising the plan

    # ---------------------------------------------------------------- helpers
    @property
    def compare(self) -> bool:
        return bool(self.compare_from and self.compare_to and self.date_from and self.date_to)

    def intent(self) -> str:
        if self.compare:
            return "comparison"
        if any(DIMENSIONS[g].time for g in self.group_by):
            return "trend"
        if self.chart == "pie":
            return "distribution"
        if self.thresholds:
            return "threshold"
        if self.order_by in METRICS and self.limit:
            return "ranking"
        multi = [v for v in (self.sites, self.cells, self.regions, self.technologies, self.vendors) if len(v) > 1]
        if multi or self.group_by and self.group_by[0] in {"vendor", "technology", "region"}:
            return "comparison"
        return "summary"

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("date_from", "date_to", "compare_from", "compare_to"):
            d[k] = d[k].isoformat() if d[k] else None
        d.pop("unknown", None)
        d.pop("notes", None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Plan | None":
        if not isinstance(d, dict):
            return None
        try:
            p = cls()
            p.metrics = [m for m in d.get("metrics") or [] if m in METRICS][:8]
            p.group_by = [g for g in d.get("group_by") or [] if g in DIMENSIONS][:3]
            for key in ("sites", "cells", "regions", "technologies", "vendors"):
                setattr(p, key, [str(v)[:120] for v in (d.get(key) or []) if str(v).strip()][:200])
            for key in ("date_from", "date_to", "compare_from", "compare_to"):
                setattr(p, key, parse_iso(d.get(key)))
            p.thresholds = _clean_thresholds(d.get("thresholds"))
            p.order_by = d.get("order_by") if d.get("order_by") in METRICS or d.get("order_by") in DIMENSIONS else None
            p.order_direction = d.get("order_direction") if d.get("order_direction") in ("asc", "desc") else None
            p.limit = _clean_limit(d.get("limit"))
            p.chart = d.get("chart") if d.get("chart") in CHARTS else "auto"
            p.title = str(d.get("title") or "")[:160]
            p.source = d.get("source") or "rules"
            p.data_source = d.get("data_source") if d.get("data_source") in SOURCES else "daily"
            p.date_label = str(d.get("date_label") or "")
            return p
        except Exception:
            return None


def _clean_limit(value) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(n, MAX_LIMIT)) if n > 0 else None


def _clean_thresholds(items) -> list:
    out = []
    for t in items or []:
        if not isinstance(t, dict):
            continue
        metric, op = t.get("metric"), t.get("op")
        try:
            value = float(t.get("value"))
        except (TypeError, ValueError):
            continue
        if metric in METRICS and op in OPS:
            out.append({"metric": metric, "op": op, "value": value})
    return out[:4]


# ====================================================================== Claude plans

def plan_json_schema() -> dict:
    nullable_date = {"anyOf": [{"type": "string", "format": "date"}, {"type": "null"}]}
    str_list = {"type": "array", "items": {"type": "string"}}
    metric_ids = list(METRICS)
    dims = list(DIMENSIONS)
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "is_data_question": {"type": "boolean"},
            "metrics": {"type": "array", "items": {"type": "string", "enum": metric_ids}},
            "group_by": {"type": "array", "items": {"type": "string", "enum": dims}},
            "sites": str_list,
            "cells": str_list,
            "regions": str_list,
            "technologies": str_list,
            "vendors": str_list,
            "date_from": nullable_date,
            "date_to": nullable_date,
            "compare_from": nullable_date,
            "compare_to": nullable_date,
            "thresholds": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {"metric": {"type": "string", "enum": metric_ids},
                               "op": {"type": "string", "enum": list(OPS)},
                               "value": {"type": "number"}},
                "required": ["metric", "op", "value"]}},
            "order_by": {"anyOf": [{"type": "string", "enum": metric_ids + dims}, {"type": "null"}]},
            "order_direction": {"anyOf": [{"type": "string", "enum": ["asc", "desc"]}, {"type": "null"}]},
            "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "chart": {"type": "string", "enum": list(CHARTS)},
            "title": {"type": "string"},
            "data_source": {"type": "string", "enum": list(SOURCES)},
            "custom_sql": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["is_data_question", "metrics", "group_by", "sites", "cells", "regions", "technologies",
                     "vendors", "data_source", "date_from", "date_to", "compare_from", "compare_to", "thresholds", "order_by",
                     "order_direction", "limit", "chart", "title", "custom_sql"],
    }


def plan_from_json(data: dict) -> Plan | None:
    """Validate Claude's plan. Returns None when it is not a data question."""
    if not isinstance(data, dict) or not data.get("is_data_question", True):
        return None
    plan = Plan.from_dict({**data, "source": "claude"})
    if plan is None:
        raise ValueError("invalid plan")
    custom = data.get("custom_sql")
    plan.custom_sql = custom.strip() if isinstance(custom, str) and custom.strip() else None
    if plan.date_from and plan.date_to and plan.date_from > plan.date_to:
        plan.date_from, plan.date_to = plan.date_to, plan.date_from
    if plan.compare_from and plan.compare_to and plan.compare_from > plan.compare_to:
        plan.compare_from, plan.compare_to = plan.compare_to, plan.compare_from
    if not plan.metrics and not plan.custom_sql:
        plan.metrics = list(SOURCE_KEY_METRICS.get(plan.data_source, KEY_METRICS))
    plan.notes = normalize_source(plan)
    return plan


def normalize_source(plan: Plan) -> list:
    """One query reads one source: make the plan's KPIs and dimensions fit its data_source."""
    notes = []
    if plan.data_source not in SOURCES:
        plan.data_source = "daily"
    owners = {metric_source(m) for m in plan.metrics if m in METRICS}
    tech_owners = owners - {"daily", "hourly"}
    if tech_owners and plan.data_source not in tech_owners:
        # a technology-specific KPI decides the source ("LTE PRB utilization" -> lte)
        plan.data_source = sorted(tech_owners)[0] if len(tech_owners) == 1 else \
            max(tech_owners, key=lambda s: sum(metric_source(m) == s for m in plan.metrics))
    elif not tech_owners and plan.data_source == "daily" and owners == {"hourly"}:
        plan.data_source = "hourly"
    if plan.data_source == "daily" and any(g in ("hour", "hour_of_day") for g in plan.group_by):
        plan.data_source = "hourly"
    src = SOURCES[plan.data_source]
    metrics, dropped = [], []
    for m in plan.metrics:
        t = translate_metric(m, plan.data_source)
        if t and t not in metrics:
            metrics.append(t)
        elif not t:
            dropped.append(METRICS[m].label if m in METRICS else m)
    if dropped:
        notes.append(f"{', '.join(dropped)} is not in the {src.label} data, so it was left out "
                     "(one question reads one data source; ask it separately).")
    plan.metrics = metrics or list(SOURCE_KEY_METRICS.get(plan.data_source, KEY_METRICS))
    plan.thresholds = [dict(t, metric=translate_metric(t["metric"], plan.data_source)) for t in plan.thresholds
                       if translate_metric(t["metric"], plan.data_source)]
    if plan.order_by in METRICS:
        plan.order_by = translate_metric(plan.order_by, plan.data_source) or (plan.metrics[0] if plan.order_direction else None)
    unsupported = [g for g in plan.group_by if not src.supports(g)]
    if unsupported:
        plan.group_by = [g for g in plan.group_by if src.supports(g)]
        notes.append(f"{src.label} cannot be grouped by {', '.join(DIMENSIONS[g].label for g in unsupported)}.")
    return notes


# ====================================================================== rule parser

_ALIASES = sorted(((alias, mid) for mid, m in METRICS.items() for alias in m.aliases),
                  key=lambda x: len(x[0]), reverse=True)

_DIM_WORDS = {
    "region": ("region", "regions", "zone", "zones", "circle", "circles"),
    "technology": ("technology", "technologies", "tech", "techs", "rat", "layer", "layers"),
    "vendor": ("vendor", "vendors", "oem"),
    "site": ("site", "sites", "tower", "towers", "bts", "location", "locations"),
    "cell": ("cell", "cells", "sector", "sectors"),
    "date": ("date", "dates", "day", "days", "daily", "din"),
    "week": ("week", "weeks", "weekly"),
    "month": ("month", "months", "monthly"),
    "status": ("status",),
}
_WORST = r"\b(worst|degraded|degrading|degradation|poor|poorly|bad|kharab|weak|problematic|problem|issue|issues|underperforming|under performing|bottom|lowest performing|failing|critical)\b"
_BEST = r"\b(best|good|top performing|best performing|healthy|achha|accha|achhe|strong|excellent)\b"
_LOW = r"\b(lowest|least|minimum|min|kam se kam|sabse kam|smallest)\b"
_HIGH = r"\b(highest|most|maximum|max|sabse zyada|sabse jyada|largest|biggest|top|peak|heaviest|busiest)\b"
_RANK = r"\b(top|worst|best|lowest|highest|bottom|least|most|maximum|minimum|degraded|poor|bad|kharab|achha|sabse|rank|ranking|which|kaun|kaunse|kaun se|list)\b"
_HOURLY = r"\b(hourly|per hour|hour wise|hourwise|by hour|each hour|every hour|har ghante|ghante|ghanta|busy hour analysis|peak hour|hour of day|hour of the day|which hour|kis time|kis ghante|time of day|intraday|hours|hour by hour)\b"
_HOUR_OF_DAY = r"\b(hour of day|hour of the day|which hour|kis time|kis ghante|time of day|peak hour|busy hour analysis|daily pattern)\b"
_TREND = r"\b(trend|trends|over time|day wise|daywise|date wise|datewise|daily|timeline|time series|history|historical|graph|progress|pattern|day by day|har din)\b"
_OVERALL = r"\b(overall|total|entire network|whole network|network level|network wide|networkwide|pure network|poore network|all network|summary of network)\b"


def _find_metrics(qn: str) -> list[tuple[int, str]]:
    cleaned = re.sub(r"\b(drill down|break down|breakdown|shut down|step down|band \d+)\b", " ", qn)
    text = f" {cleaned} "
    found = []
    for alias, mid in _ALIASES:
        pattern = f" {alias} "
        start = text.find(pattern)
        while start != -1:
            found.append((start, mid))
            text = text[:start] + " " + "#" * len(alias) + " " + text[start + len(pattern):]
            start = text.find(pattern)
    found.sort()
    seen, ordered = set(), []
    for pos, mid in found:
        if mid not in seen:
            seen.add(mid)
            ordered.append((pos, mid))
    return ordered


def _dim_words_outside_entities(qn: str, mentions) -> list[tuple[int, str]]:
    """Dimension nouns in the question (not those that are part of an entity name)."""
    text = f" {qn} "
    for m in mentions:
        if m.text:
            text = text.replace(f" {m.text} ", " " + "#" * len(m.text) + " ", 1)
    hits = []
    for dim, words in _DIM_WORDS.items():
        for w in words:
            for m in re.finditer(rf"(?<= ){re.escape(w)}(?= )", text):
                hits.append((m.start(), dim))
    hits.sort()
    return hits


def _thresholds(q: str, metric_positions: list, default_metric: str | None) -> list:
    out = []
    patterns = [
        (r"(?:below|under|less than|lower than|beneath|<=?|se kam|se neeche|kam)\s*(\d+(?:\.\d+)?)\s*%?", "<"),
        (r"(?:above|over|greater than|more than|higher than|exceeding|exceeds|>=?|se zyada|se jyada|se upar|zyada)\s*(\d+(?:\.\d+)?)\s*%?", ">"),
        (r"(\d+(?:\.\d+)?)\s*%?\s*(?:se kam|se neeche|or less|or below|and below)", "<"),
        (r"(\d+(?:\.\d+)?)\s*%?\s*(?:se zyada|se jyada|se upar|or more|or above|and above|plus)", ">"),
    ]
    used_spans = []
    for pattern, op in patterns:
        for m in re.finditer(pattern, q):
            if any(m.start() < e and s < m.end() for s, e in used_spans):
                continue
            # skip "top 10", "last 7 days", dates
            tail = q[m.end():m.end() + 8]
            if "%" not in m.group(0) and re.match(r"\s*(day|days|din|week|weeks|month|months|sites?|cells?|regions?|rows?)\b", tail):
                continue
            value = float(m.group(1))
            before = [mid for pos, mid in metric_positions if pos <= m.start() + 1]
            metric = before[-1] if before else (metric_positions[0][1] if metric_positions else default_metric)
            if metric:
                used_spans.append((m.start(), m.end()))
                out.append({"metric": metric, "op": op, "value": value})
    return out[:3]


def parse_question(question: str, catalog: Catalog, anchor: date,
                   previous: Plan | None = None, follow_up: bool = False) -> Plan:
    q = question.lower()
    qn = norm(question)
    mentions = catalog.extract(question)
    plan = Plan(source="rules")

    for m in mentions:
        target = {"site": plan.sites, "cell": plan.cells, "region": plan.regions,
                  "technology": plan.technologies, "vendor": plan.vendors, "unknown": plan.unknown}[m.kind]
        if m.value not in target:
            target.append(m.value)

    # ---- metrics
    metric_hits = _find_metrics(qn)
    metrics = [mid for _, mid in metric_hits]
    metrics_explicit = bool(metrics)
    worst = bool(re.search(_WORST, q))
    best = bool(re.search(_BEST, q))
    if not metrics:
        if re.search(r"\b(how many|kitne|kitni|number of|count of|count)\b", q) and re.search(r"\bcells?\b", q):
            metrics = ["cell_count"]
        elif re.search(r"\b(how many|kitne|kitni|number of|count of|count)\b", q) and re.search(r"\b(sites?|towers?)\b", q):
            metrics = ["site_count"]
        elif worst or best:
            metrics = list(DEGRADATION_METRICS)
        elif re.search(r"\b(usage|load|busy)\b", q):
            metrics = list(TRAFFIC_METRICS)
        else:
            metrics = list(KEY_METRICS)
    # ---- data source: a technology-specific KPI picks its table; "hourly" picks the hourly view
    hourly_words = bool(re.search(_HOURLY, q))
    tech_hits = [metric_source(m) for m in metrics if metrics_explicit and metric_source(m) not in ("daily", "hourly")]
    if tech_hits:
        plan.data_source = max(set(tech_hits), key=tech_hits.count)
    elif hourly_words:
        plan.data_source = "hourly"
    if plan.data_source != "daily" and catalog.loaded_at:
        src_min, src_max = catalog.source_range(plan.data_source)
        info = getattr(catalog, "source_info", {}).get(plan.data_source) or {}
        last_ts = info.get("last_ts")
        if src_max and last_ts is not None and getattr(last_ts, "hour", 23) < 23 and src_min and src_max > src_min:
            src_max = src_max - timedelta(days=1)      # newest day still loading: use the last complete day
        if src_max:
            anchor = min(date.today(), src_max)
    techs = {t for t in plan.technologies}
    if techs and not metrics_explicit and plan.data_source == "daily":
        metrics = [m for m in metrics if not catalog.loaded_at or catalog.metric_techs(m) & techs] or ["data_sr_bh", "tch_availability_daily"]

    # ---- dates
    intent: DateIntent = parse_dates(question, anchor)
    if intent.range:
        plan.date_from, plan.date_to, plan.date_label = intent.range.start, intent.range.end, intent.range.label
    if intent.previous:
        plan.compare_from, plan.compare_to = intent.previous.start, intent.previous.end

    # ---- grouping
    group: list[str] = []
    dim_hits = _dim_words_outside_entities(qn, mentions)
    for dim, words in _DIM_WORDS.items():
        alt = "|".join(re.escape(w) for w in words)
        if re.search(rf"\b(by|per|each|every|across|har|all|sabhi|saare|sare|for every|for each)\s+({alt})\b", q) or \
                re.search(rf"\b({alt})\s*-?\s*(wise|level|basis|breakdown|split|ke hisab se|ke according)\b", q):
            if dim not in group:
                group.append(dim)
    if re.search(_TREND, q) and not any(DIMENSIONS[g].time for g in group):
        group.insert(0, "week" if re.search(r"\bweekly\b", q) else "month" if re.search(r"\bmonthly\b", q) else "date")
    if plan.data_source != "daily" and hourly_words:
        group = [g for g in group if g != "date"]
        hour_dim = "hour_of_day" if re.search(_HOUR_OF_DAY, q) else "hour"
        if hour_dim not in group:
            group.insert(0, hour_dim)
    ranking = bool(re.search(_RANK, q)) or worst or best
    nouns = [d for _, d in dim_hits if d in {"cell", "site", "region", "technology", "vendor"}]
    if ranking and nouns and not any(g in {"cell", "site", "region", "technology", "vendor"} for g in group):
        # the ranked noun: cells beat sites beat regions ("worst cells of site X")
        for candidate in ("cell", "site", "region", "technology", "vendor"):
            if candidate in nouns:
                if candidate == "site" and len(plan.sites) == 1 and "sites" not in qn.split():
                    continue
                if candidate == "cell" and len(plan.cells) == 1 and "cells" not in qn.split():
                    continue
                group.append(candidate)
                break
    # several entities of one kind -> compare them side by side
    for kind, dim in (("cells", "cell"), ("sites", "site"), ("regions", "region"),
                      ("technologies", "technology"), ("vendors", "vendor")):
        if len(getattr(plan, kind)) > 1 and dim not in group and not (dim == "site" and "cell" in group):
            group.append(dim)
    counting = metrics in (["cell_count"], ["site_count"])
    if "cell" in nouns and "cell" not in group and not plan.cells and (plan.sites or plan.regions) and not counting and \
            re.search(r"\b(cells|cell wise|cell level|sectors|har cell|all cells|each cell)\b", qn):
        group.append("cell")
    plan.thresholds = _thresholds(q, metric_hits, metrics[0] if metrics else None)
    if not group:
        single_entity = len(plan.sites) + len(plan.cells) == 1
        multi_day = plan.date_from is None or plan.date_to is None or plan.date_from != plan.date_to
        if plan.thresholds:
            group = ["cell"] if "cell" in nouns else ["site"]
        elif single_entity and multi_day and not re.search(_OVERALL, q):
            group = ["date"]
        elif single_entity:
            group = ["cell"] if plan.cells or "cell" in nouns else ["site"]
        elif re.search(_OVERALL, q) or metrics in (["cell_count"], ["site_count"]):
            group = []
        elif plan.regions:
            group = ["region"]
        elif nouns:
            group = [nouns[0]]
        elif plan.technologies:
            group = ["technology"]
        else:
            group = ["region"]
    plan.group_by = group[:3]
    if plan.compare:
        plan.group_by = [g for g in plan.group_by if not DIMENSIONS[g].time]

    # ---- ordering / limit
    first = plan.thresholds[0]["metric"] if plan.thresholds else metrics[0]
    better = METRICS[first].better
    direction = None
    if worst:
        direction = "desc" if better == "lower" else "asc"
    elif best:
        direction = "asc" if better == "lower" else "desc"
    elif re.search(_LOW, q):
        direction = "asc"
    elif re.search(_HIGH, q):
        direction = "desc"
    if plan.compare and (worst or re.search(r"\b(degrad\w*|drop(ped)?|declin\w*|worsen\w*|girawat)\b", q)):
        direction = "desc" if better == "lower" else "asc"
    elif plan.compare and re.search(r"\b(improv\w*|better|gain\w*|sudhar)\b", q):
        direction = "asc" if better == "lower" else "desc"
    if direction is None and plan.thresholds and better:
        direction = "desc" if better == "lower" else "asc"     # breaching entities, worst first
    if direction:
        plan.order_by, plan.order_direction = first, direction
    m = re.search(r"\b(?:top|worst|best|bottom|first|lowest|highest|show|list|get|fetch|give)\s+(\d{1,4})\b", q) or \
        re.search(r"(?<![-/.\d])\b(\d{1,4})\s+(?:worst|best|top|degraded|poor|bad|sites?|cells?|regions?|towers?|sectors?)\b", q) or \
        re.search(r"\b(?:sabse kharab|sabse achhe|sabse acche|sabse achha|kharab|achhe|acche|bottom)\s+(\d{1,4})\b", q)
    if m:
        plan.limit = _clean_limit(m.group(1))
    elif direction and not plan.thresholds and any(g in {"site", "cell"} for g in plan.group_by):
        plan.limit = 10

    # ---- chart
    if re.search(r"\b(pie|share|distribution|contribution|proportion|percentage share|hissa)\b", q):
        plan.chart = "pie"
    elif re.search(r"\b(line chart|line graph)\b", q):
        plan.chart = "line"
    elif re.search(r"\b(bar chart|bar graph|histogram)\b", q):
        plan.chart = "bar"
    elif re.search(r"\b(table|tabular|raw data|rows)\b", q):
        plan.chart = "table"

    plan.metrics = metrics[:8]
    if plan.order_by and plan.order_by not in plan.metrics:
        plan.metrics.insert(0, plan.order_by)

    if follow_up and previous:
        plan = _merge_follow_up(plan, previous, q, metrics_explicit, mentions)
    plan.title = question.strip()[:160]
    plan.notes = normalize_source(plan)
    return plan


def _merge_follow_up(new: Plan, prev: Plan, q: str, metrics_explicit: bool, mentions) -> Plan:
    """'ab iske cells dikhao', 'same for 4G', 'and last month?' -> reuse the previous plan."""
    reset = bool(re.search(_OVERALL + r"|\b(all sites|all regions|sab|saare|sare|everything|pure)\b", q))
    for key in ("sites", "cells", "regions", "technologies", "vendors"):
        if not getattr(new, key) and not reset:
            setattr(new, key, list(getattr(prev, key)))
    if new.date_from is None and prev.date_from:
        new.date_from, new.date_to, new.date_label = prev.date_from, prev.date_to, prev.date_label
        if not new.compare_from and prev.compare_from:
            new.compare_from, new.compare_to = prev.compare_from, prev.compare_to
    if not metrics_explicit and prev.metrics:
        new.metrics = list(prev.metrics)
        if new.order_by and new.order_by not in METRICS:
            new.order_by = None
        if new.order_by in METRICS and new.order_by not in new.metrics:
            new.order_by = new.metrics[0]
    explicit_group = bool(re.search(r"\b(by|per|wise|each|har|breakdown|trend|daily|weekly|monthly|cells|sites|regions)\b", q))
    if not explicit_group:
        new.group_by = list(prev.group_by)
        if not new.order_by:
            new.order_by, new.order_direction = prev.order_by, prev.order_direction
        if not new.limit:
            new.limit = prev.limit
    if new.compare:
        new.group_by = [g for g in new.group_by if not DIMENSIONS[g].time]
    return new
