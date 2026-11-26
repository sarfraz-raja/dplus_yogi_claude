"""Plan -> SQL.

Entities in the plan are resolved against the catalog first. Anything that cannot be
matched is returned as "missing" (with suggestions) so the caller can tell the user,
instead of running a query that silently ignores the filter.
"""
import re
from dataclasses import dataclass, field
from datetime import date

from .catalog import Catalog
from .kpis import DIMENSIONS, METRICS, SOURCES, TABLE
from .planner import Plan

DEFAULT_MAX_ROWS = 2000


@dataclass
class Resolved:
    site_names: set = field(default_factory=set)
    site_cells: set = field(default_factory=set)
    cells: set = field(default_factory=set)
    regions: list = field(default_factory=list)
    unknown_region: bool = False
    technologies: list = field(default_factory=list)
    vendors: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    missing: list = field(default_factory=list)       # [{"kind", "value", "suggestions"}]
    labels: dict = field(default_factory=dict)        # filter chips for the UI


def lit(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def lit_list(values) -> str:
    return ", ".join(lit(v) for v in sorted(values))


def resolve_filters(plan: Plan, cat: Catalog) -> Resolved:
    r = Resolved()
    site_labels, cell_labels = [], []

    def add_site(value: str) -> bool:
        match = cat.resolve_site(value)
        if not match:
            return False
        r.site_names |= match.site_names
        r.site_cells |= match.cells
        site_labels.append(match.display)
        if match.how == "fuzzy":
            r.notes.append(f"Interpreted site '{value}' as **{match.display}**.")
        elif match.how == "contains":
            r.notes.append(f"'{value}' matched {len(match.site_names)} site names"
                           + (f" and {len(match.cells)} unmapped cells" if match.cells else "") + ".")
        return True

    def add_cell(value: str) -> bool:
        match = cat.resolve_cell(value)
        if not match:
            return False
        r.cells |= match.cells
        cell_labels.append(match.display)
        if match.how == "fuzzy":
            r.notes.append(f"Interpreted cell '{value}' as **{match.display}**.")
        elif match.how in {"sectors", "prefix"} and len(match.cells) > 1:
            r.notes.append(f"'{value}' matched {len(match.cells)} cells.")
        return True

    for value in plan.sites:
        if not add_site(value) and not add_cell(value):
            r.missing.append({"kind": "site", "value": value, "suggestions": cat.suggest(value, "site")})
    for value in plan.cells:
        if not add_cell(value) and not add_site(value):
            r.missing.append({"kind": "cell", "value": value, "suggestions": cat.suggest(value, "any")})
    for value in plan.regions:
        try:
            region = cat.resolve_region(value)
            if region is None:
                r.unknown_region = True
            elif region not in r.regions:
                r.regions.append(region)
        except KeyError:
            if not add_site(value):   # "in Mzuzu" is a place (site), not a region
                r.missing.append({"kind": "region", "value": value, "suggestions": list(cat.regions)})
    for value in plan.technologies:
        try:
            tech = cat.resolve_technology(value)
            if tech not in r.technologies:
                r.technologies.append(tech)
        except KeyError:
            r.missing.append({"kind": "technology", "value": value, "suggestions": list(cat.technologies)})
    for value in plan.vendors:
        try:
            vendor = cat.resolve_vendor(value)
            if vendor not in r.vendors:
                r.vendors.append(vendor)
        except KeyError:
            r.missing.append({"kind": "vendor", "value": value, "suggestions": list(cat.vendors)})
    for value in plan.unknown:
        if not any(m["value"] == value for m in r.missing):
            r.missing.append({"kind": "site or cell", "value": value, "suggestions": cat.suggest(value, "any")})

    if site_labels:
        r.labels["Sites"] = site_labels
    if cell_labels:
        r.labels["Cells"] = cell_labels
    if r.regions or r.unknown_region:
        r.labels["Regions"] = r.regions + (["Unknown"] if r.unknown_region else [])
    if r.technologies:
        r.labels["Technology"] = r.technologies
    if r.vendors:
        r.labels["Vendor"] = r.vendors
    return r


_AGG_RE = re.compile(r"\b(AVG|SUM|COUNT|MIN|MAX)\s*\(", re.IGNORECASE)


def with_filter(expr: str, condition: str) -> str:
    """Add FILTER (WHERE ...) to every aggregate call in a metric expression."""
    out, i = [], 0
    for m in _AGG_RE.finditer(expr):
        if m.start() < i:
            continue
        depth, j = 0, m.end() - 1
        while j < len(expr):
            if expr[j] == "(":
                depth += 1
            elif expr[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append(expr[i:j + 1])
        out.append(f" FILTER (WHERE {condition})")
        i = j + 1
    out.append(expr[i:])
    return "".join(out)


@dataclass
class BuiltQuery:
    sql: str
    metrics: list
    notes: list
    order_column: str | None
    limit: int


def coverage_notes(plan: Plan, r: Resolved, cat: Catalog) -> tuple[list, list]:
    """Drop KPIs without data; explain KPIs that only exist for some technologies."""
    notes, metrics = [], []
    for mid in plan.metrics:
        m = METRICS[mid]
        if not cat.metric_available(mid):
            notes.append(f"{m.label} has no data in the {SOURCES[plan.data_source].label} table, so it was left out.")
            continue
        techs = cat.metric_techs(mid) if cat.loaded_at else set()
        if r.technologies and techs and not (techs & set(r.technologies)):
            notes.append(f"{m.label} is only reported for {', '.join(sorted(techs))}, "
                         f"not for {', '.join(r.technologies)}, so it was left out.")
            continue
        metrics.append(mid)
    partial = [mid for mid in metrics if cat.loaded_at and not SOURCES[plan.data_source].technology
               and cat.metric_techs(mid) and set(cat.technologies) - cat.metric_techs(mid) and not r.technologies]
    if partial:
        notes.append("Coverage: " + "; ".join(
            f"{METRICS[mid].label} is reported for {', '.join(sorted(cat.metric_techs(mid)))} only"
            for mid in partial[:3]) + ".")
    return metrics, notes


def build_sql(plan: Plan, r: Resolved, cat: Catalog, max_rows: int = DEFAULT_MAX_ROWS) -> BuiltQuery:
    src = SOURCES.get(plan.data_source, SOURCES["daily"])
    c = src.cols
    dexpr = src.date_expr
    if src.technology and r.technologies and src.technology not in r.technologies:
        raise ValueError(f"{src.label} only covers {src.technology}, not {', '.join(r.technologies)}.")
    metrics, notes = coverage_notes(plan, r, cat)
    if not metrics:
        raise ValueError("None of the requested KPIs has data for this selection. " + " ".join(notes))
    select, group_exprs = [], []
    groups = [g for g in plan.group_by if g in DIMENSIONS and src.supports(g)]
    for g in groups:
        d = DIMENSIONS[g]
        expr = src.dim_sql(g)
        select.append(f"{expr} AS {d.alias}")
        if not expr.startswith("'"):        # a constant (technology of a per-technology table) is not grouped
            group_exprs.append(expr)
    if "cell" in groups:
        if "site" not in groups:
            select.append(f"MAX({c['site_name']}) AS site_name")
        if "region" not in groups:
            select.append(f"MAX({c['region']}) AS region")
        if "technology" not in groups:
            select.append(f"MAX({src.tech_expr()}) AS technology")
    elif "site" in groups:
        if "region" not in groups:
            select.append(f"MAX({c['region']}) AS region")
        if "technology" not in groups:
            select.append(f"STRING_AGG(DISTINCT {src.tech_expr()}, '/') AS technology")

    compare = plan.compare
    if compare:
        cur = f"{dexpr} BETWEEN {lit(plan.date_from)} AND {lit(plan.date_to)}"
        prev = f"{dexpr} BETWEEN {lit(plan.compare_from)} AND {lit(plan.compare_to)}"
        for mid in metrics:
            expr = METRICS[mid].sql
            cur_e, prev_e = with_filter(expr, cur), with_filter(expr, prev)
            select += [f"{cur_e} AS {mid}_current", f"{prev_e} AS {mid}_previous",
                       f"({cur_e}) - ({prev_e}) AS {mid}_change"]
    else:
        select += [f"{METRICS[mid].sql} AS {mid}" for mid in metrics]

    where = []
    if compare:
        start = min(plan.date_from, plan.compare_from)
        end = max(plan.date_to, plan.compare_to)
        where.append(f"{dexpr} BETWEEN {lit(start)} AND {lit(end)}")
    elif plan.date_from or plan.date_to:
        smin, smax = cat.source_range(src.id) if hasattr(cat, "source_range") else (cat.date_min, cat.date_max)
        start = plan.date_from or smin or date(2000, 1, 1)
        end = plan.date_to or smax or date.today()
        where.append(f"{dexpr} BETWEEN {lit(start)} AND {lit(end)}" if start != end else f"{dexpr} = {lit(start)}")
    entity = []
    if r.site_names:
        entity.append(f"{c['site_name']} IN ({lit_list(r.site_names)})")
    if r.site_cells:
        entity.append(f"{c['cell_name']} IN ({lit_list(r.site_cells)})")
    if r.cells:
        entity.append(f"{c['cell_name']} IN ({lit_list(r.cells)})")
    if entity:
        where.append("(" + " OR ".join(entity) + ")" if len(entity) > 1 else entity[0])
    if r.regions or r.unknown_region:
        parts = [f"{c['region']} IN ({lit_list(r.regions)})"] if r.regions else []
        if r.unknown_region:
            parts.append(f"{c['region']} IS NULL")
        where.append("(" + " OR ".join(parts) + ")" if len(parts) > 1 else parts[0])
    if r.technologies and not src.technology:
        where.append(f"{c['technology']} IN ({lit_list(r.technologies)})")
    if r.vendors and c.get("vendor"):
        where.append(f"{c['vendor']} IN ({lit_list(r.vendors)})")

    having = []
    for t in plan.thresholds:
        mid = t["metric"]
        if mid not in METRICS or not cat.metric_available(mid):
            continue
        expr = METRICS[mid].sql
        if compare:
            expr = with_filter(expr, f"{dexpr} BETWEEN {lit(plan.date_from)} AND {lit(plan.date_to)}")
        value = float(t["value"])
        having.append(f"{expr} {t['op']} {value:g}")
        if mid not in metrics:
            notes.append(f"Filtered on {METRICS[mid].label} {t['op']} {value:g}.")
    if not groups and having:
        having = []
        notes.append("Thresholds apply to sites/cells; showing the overall value instead.")

    order, order_column = [], None
    ob = plan.order_by
    direction_hint = plan.order_direction
    if ob is None and plan.thresholds and plan.thresholds[0]["metric"] in metrics:
        ob = plan.thresholds[0]["metric"]          # list breaching entities worst first
        direction_hint = "desc" if METRICS[ob].better == "lower" else "asc"
    if ob in METRICS and ob not in metrics:
        covered = [m for m in metrics if METRICS[m].better]
        ob = covered[0] if covered else metrics[0]
        if plan.order_by != ob:
            notes.append(f"Ranked by {METRICS[ob].label} because {METRICS[plan.order_by].label} has no data here.")
    if ob in METRICS and groups:
        order_column = f"{ob}_change" if compare else ob
        direction = (direction_hint or ("asc" if METRICS[ob].better == "higher" else "desc")).upper()
        order.append(f"{order_column} {direction} NULLS LAST")
    elif ob in DIMENSIONS and ob in groups:
        order.append(f"{DIMENSIONS[ob].alias} {(direction_hint or 'asc').upper()}")
    for g in groups:
        alias = DIMENSIONS[g].alias
        if not any(o.startswith(alias + " ") for o in order):
            order.append(alias)

    limit = min(plan.limit or max_rows, max_rows)
    sql = "SELECT " + ",\n       ".join(select) + f"\nFROM {src.from_sql}"
    if where:
        sql += "\nWHERE " + "\n  AND ".join(where)
    if group_exprs:
        sql += "\nGROUP BY " + ", ".join(group_exprs)
    if having:
        sql += "\nHAVING " + "\n   AND ".join(having)
    if order and groups:
        sql += "\nORDER BY " + ", ".join(order)
    sql += f"\nLIMIT {limit}"
    return BuiltQuery(sql=sql, metrics=metrics, notes=notes, order_column=order_column, limit=limit)
