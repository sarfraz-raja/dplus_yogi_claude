"""SQL safety: validation of generated SQL and read-only execution.

Two independent layers protect the database:
1. ``validate_sql`` - only one SELECT/WITH statement over the KPI table, only known
   columns, no dangerous functions or keywords, no comments.
2. ``execute_readonly`` - every query runs inside a READ ONLY transaction with a
   statement timeout, and at most ``max_rows`` rows are fetched (server-side cursor).
"""
import re
from datetime import date, datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import text

from .kpis import SOURCES, TABLE, TABLE_NAME, TABLE_SCHEMA

# Every table a query may read: the KPI sources plus the cell master table they join.
ALLOWED_TABLES = sorted({t for s in SOURCES.values() for t in s.tables}, key=len, reverse=True)
_PLACEHOLDERS = {f"__tbl{i}__": t for i, t in enumerate(ALLOWED_TABLES)}
_TABLE_PARTS = {p for t in ALLOWED_TABLES for p in t.split('.')}

STATEMENT_TIMEOUT = "45s"

FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|replace|grant|revoke|copy|call|do|execute|merge|"
    r"vacuum|analyze|cluster|reindex|refresh|listen|notify|unlisten|lock|prepare|deallocate|discard|"
    r"reset|set|show|begin|commit|rollback|savepoint|release|security|into|import|load|checkpoint)\b",
    re.IGNORECASE,
)
FORBIDDEN_FUNCTION = re.compile(
    r"^(pg_\w+|dblink\w*|lo_\w+|set_config|current_setting|query_to_xml\w*|table_to_xml\w*|"
    r"cursor_to_xml\w*|xpath\w*|txid_\w+|inet_\w+|version|current_user|session_user|user|"
    r"current_database|current_schema\w*|has_\w+_privilege|binary_upgrade\w*|gen_random\w*|"
    r"nextval|setval|currval|lastval|row_security\w*|format_type|to_regclass|to_regproc\w*|"
    r"generate_series|unnest|json_populate\w*|jsonb_populate\w*|read\w*|write\w*|sleep\w*)$",
    re.IGNORECASE,
)

SQL_KEYWORDS = {
    "select", "from", "where", "and", "or", "not", "null", "is", "in", "like", "ilike", "between",
    "case", "when", "then", "else", "end", "as", "on", "join", "inner", "left", "right", "full", "outer",
    "cross", "natural", "using", "group", "by", "order", "limit", "offset", "desc", "asc", "distinct",
    "having", "with", "over", "partition", "rows", "range", "groups", "unbounded", "following", "preceding",
    "current", "row", "filter", "within", "nulls", "first", "last", "true", "false", "all", "any", "some",
    "exists", "union", "intersect", "except", "similar", "escape", "lateral", "interval", "day", "days",
    "month", "months", "year", "years", "week", "weeks", "hour", "hours", "minute", "minutes", "second",
    "seconds", "quarter", "epoch", "dow", "doy", "isodow", "isoyear", "current_date", "current_timestamp",
    "localtimestamp", "timestamp", "timestamptz", "date", "time", "zone", "at", "numeric", "decimal",
    "float", "real", "double", "precision", "integer", "int", "bigint", "smallint", "text", "varchar",
    "char", "character", "varying", "boolean", "bool", "array", "fetch", "next", "only", "ties", "values",
    "asymmetric", "symmetric", "collate", "unknown", "for", "both", "leading", "trailing", "placing",
    "century", "decade", "millennium", "isoyear", "week_of_year",
}


def json_safe(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float) and value != value:   # NaN
        return None
    return value


def _strip_literals(sql: str) -> str:
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def extract_sql(response: str) -> str:
    response = (response or "").strip()
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", response, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        response = fenced.group(1).strip()
    match = re.search(r"\b(with|select)\b", response, flags=re.IGNORECASE)
    if not match:
        raise HTTPException(status_code=400, detail="Claude did not return SQL")
    return response[match.start():].strip().rstrip(";").strip()


def validate_sql(sql: str, allowed_columns: set) -> str:
    """Reject anything that is not a single safe read-only query over the KPI table."""
    if not sql or not sql.strip():
        raise HTTPException(status_code=400, detail="SQL generation returned empty query")
    compact = re.sub(r"\s+", " ", sql).strip().rstrip(";").strip()
    if "--" in compact or "/*" in compact or "*/" in compact:
        raise HTTPException(status_code=400, detail="SQL comments are not allowed")
    no_literals = _strip_literals(compact)
    if ";" in no_literals:
        raise HTTPException(status_code=400, detail="Only one SQL statement is allowed")
    if "$" in no_literals or "\\" in no_literals:
        raise HTTPException(status_code=400, detail="Dollar quoting / escapes are not allowed")
    if not re.match(r"^\(?\s*(select|with)\b", no_literals, flags=re.IGNORECASE):
        raise HTTPException(status_code=400, detail="Only SELECT queries are allowed")
    if FORBIDDEN_SQL.search(no_literals):
        raise HTTPException(status_code=400, detail="Unsafe SQL keyword found")
    if re.search(r"\bselect\s+(distinct\s+)?\*", no_literals, re.IGNORECASE) or re.search(r"\b\w+\.\*", no_literals):
        raise HTTPException(status_code=400, detail="SELECT * is not allowed")

    columns = {c.lower() for c in allowed_columns}
    quoted = re.findall(r'"([^"]*)"', no_literals)
    for q in quoted:
        if q.lower() not in columns and q.lower() not in _TABLE_PARTS:
            raise HTTPException(status_code=400, detail=f"Column not allowed: {q}")
    work = re.sub(r'"([^"]*)"', lambda m: m.group(1).lower(), no_literals)
    # FROM inside EXTRACT(dow FROM date) / SUBSTRING(x FROM 1) / TRIM(BOTH 'x' FROM y) is not a table
    work = re.sub(r"\b(extract|substring|trim|overlay|position)\s*\(([^()]*?)\bfrom\b", r"\1(\2,", work, flags=re.IGNORECASE)
    for ph, table in _PLACEHOLDERS.items():
        schema, name = table.split(".")
        work = re.sub(rf"\b{re.escape(schema)}\s*\.\s*{re.escape(name)}\b", f" {ph} ", work, flags=re.IGNORECASE)
    placeholders = set(_PLACEHOLDERS)

    cte_names = {m.lower() for m in re.findall(r"(?:\bwith|,)\s*(?:recursive\s+)?([a-zA-Z_]\w*)\s+as\s*\(", work, re.IGNORECASE)}
    if re.search(r"\bwith\s+recursive\b", work, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="Recursive queries are not allowed")
    # gid.cells may only be joined to a KPI table, never read on its own
    if not any(ph in work for ph, table in _PLACEHOLDERS.items() if table != "gid.cells"):
        raise HTTPException(status_code=400, detail="SQL must reference a KPI table")

    # FROM / JOIN targets: the KPI table, a CTE, or a sub-query
    for m in re.finditer(r"\b(?:from|join)\s+(?!\()([a-zA-Z_][\w.]*)", work, re.IGNORECASE):
        target = m.group(1).lower()
        if target not in placeholders and target not in cte_names:
            raise HTTPException(status_code=400, detail=f"Table not allowed: {m.group(1)}")
    # comma-joined tables after FROM
    for m in re.finditer(r"\bfrom\s+[^()]*?(?=\bwhere\b|\bgroup\b|\border\b|\blimit\b|\bhaving\b|\)|$)", work, re.IGNORECASE):
        for part in m.group(0)[4:].split(",")[1:]:
            name = part.strip().split(" ")[0].lower()
            if name and name not in placeholders and name not in cte_names and not name.startswith("("):
                raise HTTPException(status_code=400, detail=f"Table not allowed: {name}")

    aliases = set(cte_names)
    for m in re.finditer(r"\b(?:" + "|".join(map(re.escape, placeholders | (cte_names or {"__none__"}))) + r")\s+(?:as\s+)?([a-zA-Z_]\w*)", work, re.IGNORECASE):
        alias = m.group(1).lower()
        if alias not in SQL_KEYWORDS:
            aliases.add(alias)
    for m in re.finditer(r"\)\s+(?:as\s+)?([a-zA-Z_]\w*)", work, re.IGNORECASE):
        alias = m.group(1).lower()
        if alias not in SQL_KEYWORDS:
            aliases.add(alias)
    aliases |= {a.lower() for a in re.findall(r"\bas\s+([a-zA-Z_]\w*)", work, re.IGNORECASE)}

    # dotted references: alias.column
    for prefix, column in re.findall(r"\b([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)\b", work):
        if prefix.lower() not in aliases and prefix.lower() not in _TABLE_PARTS:
            raise HTTPException(status_code=400, detail=f"Unknown table alias: {prefix}")
        if column.lower() not in columns and column.lower() not in aliases:
            raise HTTPException(status_code=400, detail=f"Column not allowed: {column}")
    work = re.sub(r"\b[a-zA-Z_]\w*\.[a-zA-Z_]\w*\b", " ", work)

    # every remaining identifier must be a column, keyword, alias, function or type
    for m in re.finditer(r"(::\s*)?\b([a-zA-Z_]\w*)\b(\s*\()?", work):
        cast, word, call = m.group(1), m.group(2), m.group(3)
        lw = word.lower()
        if lw in placeholders:
            continue
        if call:
            if FORBIDDEN_FUNCTION.match(lw):
                raise HTTPException(status_code=400, detail=f"Function not allowed: {word}")
            continue
        if cast:
            continue
        if lw in SQL_KEYWORDS or lw in columns or lw in aliases:
            continue
        raise HTTPException(status_code=400, detail=f"Column not allowed: {word}")
    return compact


def _escape_colons(sql: str) -> str:
    """sqlalchemy.text() treats ':name' as a bind parameter; escape colons inside string literals."""
    return re.sub(r"'(?:[^']|'')*'", lambda m: m.group(0).replace(":", r"\:"), sql)


def execute_readonly(engine, sql: str, max_rows: int, timeout: str = STATEMENT_TIMEOUT) -> tuple[list, bool]:
    """Run a query read-only. Returns (rows, truncated)."""
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text(f"SET LOCAL statement_timeout = '{timeout}'"))
        result = conn.execution_options(stream_results=True, max_row_buffer=1000).execute(text(_escape_colons(sql)))
        fetched = result.mappings().fetchmany(max_rows + 1)
        result.close()
        conn.rollback()
    truncated = len(fetched) > max_rows
    rows = [{key: json_safe(value) for key, value in row.items()} for row in fetched[:max_rows]]
    return rows, truncated


def friendly_db_error(error: Exception) -> str:
    msg = str(getattr(error, "orig", error)).strip().splitlines()[0] if str(error) else type(error).__name__
    if "statement timeout" in msg or "canceling statement" in msg:
        return "The query took too long and was stopped. Try a shorter date range or fewer sites."
    if "could not connect" in msg or "Connection refused" in msg or "timeout expired" in msg:
        return "Cannot reach the KPI database right now. Please try again shortly."
    return msg[:300]
