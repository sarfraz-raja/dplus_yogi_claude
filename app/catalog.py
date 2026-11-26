"""Data catalog + entity resolver.

Loads (and caches) what actually exists in the KPI table: date range, regions,
technologies, vendors, every site and cell, which KPI columns carry data and for
which technologies. Questions are matched against these real names, so a filter
is never guessed and an unknown name is reported instead of silently ignored.

Site naming in this network: ``TN30040_3G_Zomba_Dam`` = code TN30040, technology
3G, physical site "Zomba Dam". One physical site therefore has one site_name per
technology; some cells have no site_name, so they are tied to the physical site
through their own name (``3G_Zomba_Dam_U2100F1_1``).
"""
import difflib
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import text

from .kpis import METRICS, SOURCES, TABLE

log = logging.getLogger("yogi.catalog")

CATALOG_TTL_SECONDS = 30 * 60
MAX_PHRASE_TOKENS = 10

SITE_RE = re.compile(r"^(TN\d+)_(?:(?:2G|3G|4G|5G)_)?(.+)$", re.IGNORECASE)
SITE_TECH_RE = re.compile(r"^TN\d+_([2-5]G)_", re.IGNORECASE)
CELL_RE = re.compile(r"^(?:[2-5]G_)?(.+?)_(?:[GULN]\d{3,4}[A-Z0-9]*)(?:_\d+)*$", re.IGNORECASE)
CELL_SECTOR_RE = re.compile(r"(?:_\d+)+$")
CELL_TECH_PREFIX_RE = re.compile(r"^[2-5]G_", re.IGNORECASE)

TECH_SYNONYMS = {"2g": "2G", "gsm": "2G", "3g": "3G", "umts": "3G", "wcdma": "3G", "4g": "4G",
                 "lte": "4G", "5g": "5G", "nr": "5G"}
KNOWN_VENDORS = {"huawei", "ericsson", "nokia", "zte", "samsung", "cisco"}
UNKNOWN_REGION_WORDS = {"unknown", "null", "none", "unmapped", "blank", "missing", "no region", "without region"}
REGION_ALIASES = {"northern": "north", "southern": "south", "northern region": "north", "southern region": "south"}

ENTITY_CONTEXT_WORDS = {"site", "sites", "cell", "cells", "tower", "towers", "bts", "node", "location", "sector"}

# Words that must never be read as a site name on their own (a few sites have
# single-word names that are also ordinary words).
COMMON_WORDS = set("""
a an the of in on at to for from by with and or not no yes is are was were be been being am do does did
done have has had this that these those it its they them their there here what which who whom whose
when where why how all any each every some many much more most less least few other another same such
only own so than too very can could will would should may might must shall just also into over under
about above below between after before during up down out off again further then once both either
neither per via vs versus top bottom best worst good bad poor high low highest lowest higher lower
show list give get fetch find check display report analysis analyse analyze analytics performance
perform performing health status summary summarize overview trend trends compare comparison
daily weekly monthly day days week weeks month months year today yesterday latest last past previous
next current recent total average avg sum count number rate ratio share split mix distribution
breakdown wise kpi kpis network data voice call calls traffic volume drop drops degraded degradation
availability available region regions technology tech vendor vendors site sites cell cells tower
towers node sector band busy hour bh dl ul upload download success setup north south east west central
main town city market hospital school college police station road hill hills river lake bridge
mission boma depot camp farm estate park plaza mall hotel lodge church university airport stadium
junction corner centre center house office factory industrial area zone sector point view
dam police court prison hall club garden village new old big small upper lower top inner outer
hai hain ka ke ki ko me mein se par pe aur bhi kya kaise kitna kitne kitni sabse wala wali wale
dikhao batao nikalo karo kar do de chahiye mujhe hume humein iska iske uska uske inka inke ab abhi
aaj kal pichle pichhle din hafte mahine saare sare sab koi kuch achha accha kharab band chalu
please pls thanks thank hello hi hey ok okay sir madam level levels name names detail details
info information wise basis based related specific particular individual single multiple several
various combined together overall whole entire full complete related value values metric metrics
compared compare comparing having where whose while within without against among near around located
named called id code codes wala wale wali waala waale jinka jinki jiska jo jaha jahan isliye kyun kyunki
issue issues problem problems performing working worked shown showing see view analyse analysed impact
impacted affected affecting caused cause reason reasons improve improved improvement change changed
changes increase increased decrease decreased declined decline drop dropped rise rising falling
sabhi saari saara sara kaunsi kaunsa kaunse konsi konsa konse kaun kon kahan kidhar kab kaisa kaisi
jinki jinke jinka unki unka unke inki uski isliye wahan yahan dono teeno sirf bas thoda zyada jyada
kam bahut bohot sabse kharab kharaab acchi achhi bura buri wali dikhaiye bataiye chahte chahta chahti
dekhna dekho dekhiye karo kariye karna hona hai hain tha thi the raha rahi rahe gaya gayi
""".split())
# Every word of every KPI phrase ("call", "setup", "success", "rate", ...) is also a common word.
COMMON_WORDS |= {tok for m in METRICS.values() for alias in m.aliases for tok in alias.split()}


def norm(value) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


def site_base(site_name: str) -> str:
    m = SITE_RE.match(site_name)
    return m.group(2) if m else site_name


def site_code(site_name: str) -> str | None:
    m = SITE_RE.match(site_name)
    return m.group(1).upper() if m else None


def cell_base(cell_name: str) -> str | None:
    m = CELL_RE.match(cell_name)
    return m.group(1) if m else None


@dataclass
class SiteMatch:
    """A resolved site reference: one or more site_names plus cells without a site_name."""
    query: str
    display: str
    site_names: set = field(default_factory=set)
    cells: set = field(default_factory=set)
    how: str = "exact"      # exact | code | name | contains | fuzzy

    def size(self) -> int:
        return len(self.site_names) + len(self.cells)


@dataclass
class CellMatch:
    query: str
    display: str
    cells: set = field(default_factory=set)
    how: str = "exact"


@dataclass
class Mention:
    kind: str           # site | cell | region | technology | vendor | unknown
    value: str          # canonical value (site group key, cell name, region...)
    text: str           # the words in the question
    display: str = ""


class Catalog:
    def __init__(self):
        self.loaded_at = 0.0
        self.columns: dict[str, str] = {}
        self.row_count = 0
        self.date_min: date | None = None
        self.date_max: date | None = None
        self.date_count = 0
        self.regions: list[str] = []
        self.region_has_null = False
        self.technologies: list[str] = []
        self.vendors: list[str] = []
        self.populated: set[str] = set()                 # columns with any data (recent window)
        self.coverage: dict[str, set] = {}               # column -> technologies with data
        self.sites: dict[str, dict] = {}                 # site_name -> {region, technology, cells, base, code}
        self.cells: dict[str, dict] = {}                 # cell_name -> {site_name, region, technology}
        self.groups: dict[str, dict] = {}                # base key -> {display, sites:set, cells:set}
        self.codes: dict[str, set] = {}                  # code -> site_names
        self.cell_groups: dict[str, set] = {}            # cell name without sector (norm) -> cells
        self.index: dict[str, list] = {}                 # normalised phrase -> [(priority, kind, value)]
        self._site_norm: dict[str, str] = {}
        self._cell_norm: dict[str, str] = {}
        self._cell_noprefix: dict[str, set] = {}

    # ------------------------------------------------------------------ loading
    def load(self, engine) -> "Catalog":
        started = time.perf_counter()
        with engine.connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text("SET LOCAL statement_timeout = '120s'"))
            schema, table = TABLE.split(".")
            cols = conn.execute(text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = :t ORDER BY ordinal_position"),
                {"s": schema, "t": table}).all()
            self.columns = {c.column_name: c.data_type for c in cols}
            r = conn.execute(text(f"SELECT MIN(date) AS dmin, MAX(date) AS dmax, COUNT(*) AS n, "
                                  f"COUNT(DISTINCT date) AS nd FROM {TABLE}")).one()
            self.date_min, self.date_max, self.row_count, self.date_count = r.dmin, r.dmax, r.n, r.nd
            dims = conn.execute(text(f"SELECT region, technology, vendor FROM {TABLE} GROUP BY 1, 2, 3")).all()
            self.regions = sorted({d.region for d in dims if d.region})
            self.region_has_null = any(d.region is None for d in dims)
            self.technologies = sorted({d.technology for d in dims if d.technology})
            self.vendors = sorted({d.vendor for d in dims if d.vendor})
            sites = conn.execute(text(
                f"SELECT site_name, MAX(region) AS region, MAX(technology) AS technology, "
                f"COUNT(DISTINCT cell_name) AS cells FROM {TABLE} WHERE site_name IS NOT NULL GROUP BY site_name")).all()
            cells = conn.execute(text(
                f"SELECT cell_name, MAX(site_name) AS site_name, MAX(region) AS region, MAX(technology) AS technology "
                f"FROM {TABLE} WHERE cell_name IS NOT NULL GROUP BY cell_name")).all()
            metric_cols = sorted({c for m in METRICS.values() for c in m.columns
                                  if c in self.columns and (m.source or "daily") == "daily"})
            if self.date_max:
                counts = ", ".join(f'COUNT("{c}") AS "{c}"' for c in metric_cols)
                cov = conn.execute(text(
                    f"SELECT technology, {counts} FROM {TABLE} WHERE date >= CAST(:d AS date) - 13 GROUP BY technology"),
                    {"d": self.date_max}).mappings().all()
            else:
                cov = []
        self.coverage = {c: set() for c in metric_cols}
        for row in cov:
            for c in metric_cols:
                if row[c]:
                    self.coverage[c].add(row["technology"] or "Unknown")
        self.populated = {c for c, techs in self.coverage.items() if techs}
        self.table_columns = {TABLE: dict(self.columns)}
        self.source_info = {"daily": {"date_min": self.date_min, "date_max": self.date_max, "last_ts": None,
                                      "populated": set(self.populated), "ok": True}}
        gid_cells = self._load_sources(engine)
        self._build_entities(list(sites) + gid_cells["sites"], list(cells) + gid_cells["cells"])
        self.loaded_at = time.time()
        log.info("catalog loaded in %.1fs: %d sites, %d cells, %s..%s", time.perf_counter() - started,
                 len(self.sites), len(self.cells), self.date_min, self.date_max)
        return self

    def _load_sources(self, engine) -> dict:
        """Columns, date range and populated KPI columns of every extra source; names from gid.cells."""
        out = {"sites": [], "cells": []}
        tables = sorted({t for s in SOURCES.values() for t in s.tables} - {TABLE})
        for table in tables:
            try:
                with engine.connect() as conn:
                    conn.execute(text("SET TRANSACTION READ ONLY"))
                    schema, name = table.split(".")
                    cols = conn.execute(text(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_schema = :s AND table_name = :t ORDER BY ordinal_position"), {"s": schema, "t": name}).all()
                    self.table_columns[table] = {c.column_name: c.data_type for c in cols}
            except Exception:
                log.exception("could not read columns of %s", table)
        for sid, src in SOURCES.items():
            if sid == "daily":
                continue
            info = {"date_min": None, "date_max": None, "last_ts": None, "populated": set(), "ok": False}
            self.source_info[sid] = info
            main = src.tables[0]
            if main not in self.table_columns:
                continue
            raw_time = src.time_col.split(".")[-1]
            try:
                with engine.connect() as conn:
                    conn.execute(text("SET TRANSACTION READ ONLY"))
                    conn.execute(text("SET LOCAL statement_timeout = '120s'"))
                    r = conn.execute(text(f"SELECT MIN({raw_time}) AS a, MAX({raw_time}) AS b FROM {main}")).one()
                    info["date_min"] = r.a.date() if r.a else None
                    info["date_max"] = r.b.date() if r.b else None
                    info["last_ts"] = r.b
                    mcols = sorted({c for m in METRICS.values() if m.source == sid for c in m.columns
                                    if c in self.table_columns[main]})
                    if r.b and mcols:
                        counts = ", ".join(f'COUNT("{c}") AS "{c}"' for c in mcols)
                        row = conn.execute(text(
                            f"SELECT {counts} FROM {main} WHERE {raw_time} >= CAST(:d AS timestamp) - INTERVAL '1 day'"),
                            {"d": r.b}).mappings().one()
                        info["populated"] = {c for c in mcols if row[c]}
                    info["ok"] = True
            except Exception:
                log.exception("could not profile source %s", sid)
        if "gid.cells" in self.table_columns:
            try:
                with engine.connect() as conn:
                    conn.execute(text("SET TRANSACTION READ ONLY"))
                    out["sites"] = conn.execute(text(
                        "SELECT site_name, MAX(region) AS region, MAX(technology) AS technology, "
                        "COUNT(DISTINCT cell_name) AS cells FROM gid.cells WHERE site_name IS NOT NULL GROUP BY site_name")).all()
                    out["cells"] = conn.execute(text(
                        "SELECT cell_name, MAX(site_name) AS site_name, MAX(region) AS region, MAX(technology) AS technology "
                        "FROM gid.cells WHERE cell_name IS NOT NULL GROUP BY cell_name")).all()
            except Exception:
                log.exception("could not read gid.cells names")
        return out

    def allowed_columns(self) -> set:
        cols = set()
        for c in getattr(self, "table_columns", {}).values():
            cols |= set(c)
        return cols or set(self.columns)

    def source_range(self, source_id: str) -> tuple:
        info = getattr(self, "source_info", {}).get(source_id)
        if not info:
            return self.date_min, self.date_max
        return info["date_min"], info["date_max"]

    def source_available(self, source_id: str) -> bool:
        if source_id == "daily":
            return True
        info = getattr(self, "source_info", {}).get(source_id)
        return bool(info and info["ok"] and info["date_max"])

    def _add_phrase(self, phrase: str, priority: int, kind: str, value: str):
        if not phrase:
            return
        bucket = self.index.setdefault(phrase, [])
        if (kind, value) not in [(k, v) for _, k, v in bucket]:
            bucket.append((priority, kind, value))

    def _build_entities(self, sites, cells):
        self.sites, self.cells, self.groups, self.codes, self.cell_groups = {}, {}, {}, {}, {}
        self.index, self._site_norm, self._cell_norm, self._cell_noprefix = {}, {}, {}, {}
        for s in sites:
            base = site_base(s.site_name)
            code = site_code(s.site_name)
            key = norm(base)
            self.sites[s.site_name] = {"region": s.region, "technology": s.technology, "cells": s.cells,
                                       "base": base, "code": code}
            group = self.groups.setdefault(key, {"display": base, "sites": set(), "cells": set()})
            group["sites"].add(s.site_name)
            if code:
                self.codes.setdefault(code, set()).add(s.site_name)
            self._site_norm[norm(s.site_name)] = s.site_name
        for c in cells:
            self.cells[c.cell_name] = {"site_name": c.site_name, "region": c.region, "technology": c.technology}
            self._cell_norm[norm(c.cell_name)] = c.cell_name
            no_sector = norm(CELL_SECTOR_RE.sub("", c.cell_name))
            self.cell_groups.setdefault(no_sector, set()).add(c.cell_name)
            no_sector_prefix = norm(CELL_SECTOR_RE.sub("", CELL_TECH_PREFIX_RE.sub("", c.cell_name)))
            if no_sector_prefix != no_sector:
                self.cell_groups.setdefault(no_sector_prefix, set()).add(c.cell_name)
            no_prefix = norm(CELL_TECH_PREFIX_RE.sub("", c.cell_name))
            self._cell_noprefix.setdefault(no_prefix, set()).add(c.cell_name)
            if not c.site_name:
                base = cell_base(c.cell_name)
                if base:
                    key = norm(base)
                    group = self.groups.setdefault(key, {"display": base, "sites": set(), "cells": set()})
                    group["cells"].add(c.cell_name)
        # phrase index (lower priority number wins)
        for region in self.regions:
            self._add_phrase(norm(region), 1, "region", region)
        for alias, target in REGION_ALIASES.items():
            match = next((r for r in self.regions if norm(r) == target), None)
            if match:
                self._add_phrase(alias, 1, "region", match)
        if self.region_has_null:
            for phrase in ("unknown region", "no region", "without region", "region unknown",
                           "unmapped region", "region missing", "missing region", "null region"):
                self._add_phrase(phrase, 1, "region", "Unknown")
        for word, tech in TECH_SYNONYMS.items():
            self._add_phrase(word, 1, "technology", tech)
        for vendor in KNOWN_VENDORS | {v.lower() for v in self.vendors}:
            self._add_phrase(norm(vendor), 1, "vendor", vendor)
        for site_name in self.sites:
            self._add_phrase(norm(site_name), 2, "site_exact", site_name)
        for cell_name in self.cells:
            self._add_phrase(norm(cell_name), 2, "cell", cell_name)
        for code in self.codes:
            self._add_phrase(norm(code), 3, "code", code)
            digits = code[2:]
            if len(digits) >= 5:
                self._add_phrase(digits, 6, "code", code)
        for key in self.groups:
            self._add_phrase(key, 4, "site", key)
        for key, members in self.cell_groups.items():
            if len(members) > 1:
                self._add_phrase(key, 5, "cell_group", key)
        for key, members in self._cell_noprefix.items():
            if len(members) == 1:
                self._add_phrase(key, 5, "cell", next(iter(members)))

    def is_stale(self) -> bool:
        return not self.loaded_at or (time.time() - self.loaded_at) > CATALOG_TTL_SECONDS

    # ------------------------------------------------------------------ facts
    def metric_available(self, metric_id: str) -> bool:
        m = METRICS[metric_id]
        if not self.loaded_at:
            return True
        source = m.source or "daily"
        if source == "daily":
            return all(c in self.populated for c in m.columns)
        info = getattr(self, "source_info", {}).get(source)
        return bool(info and info["ok"] and all(c in info["populated"] for c in m.columns))

    def metric_techs(self, metric_id: str) -> set:
        m = METRICS[metric_id]
        src = SOURCES.get(m.source or "daily")
        if src and src.technology:
            return {src.technology}
        if (m.source or "daily") != "daily":
            m = METRICS.get(m.concept) or m
        techs = None
        for c in m.columns:
            cov = self.coverage.get(c, set())
            techs = set(cov) if techs is None else techs & cov
        return techs or set()

    def group_display(self, key: str) -> str:
        return self.groups[key]["display"] if key in self.groups else key

    # ------------------------------------------------------------------ extraction
    def extract(self, question: str) -> list[Mention]:
        """Find every site/cell/region/technology/vendor named in the question."""
        tokens = norm(question).split()
        mentions: list[Mention] = []
        run: list[tuple[int, str]] = []   # consecutive unmatched name-like tokens

        def flush():
            if run:
                mentions.extend(self._resolve_run(tokens, run))
                run.clear()

        i = 0
        while i < len(tokens):
            matched = False
            for n in range(min(MAX_PHRASE_TOKENS, len(tokens) - i), 0, -1):
                phrase = " ".join(tokens[i:i + n])
                candidates = self.index.get(phrase)
                if not candidates:
                    continue
                if n == 1 and phrase in COMMON_WORDS:
                    candidates = [c for c in candidates if c[1] in {"region", "technology", "vendor"}
                                  or self._has_entity_context(tokens, i)]
                    if not candidates:
                        continue
                if n == 1 and phrase.isdigit():
                    candidates = [c for c in candidates if self._has_entity_context(tokens, i)]
                    if not candidates:
                        continue
                # A region word followed by "site"/"cell" is a site name, not a region ("site Balaka").
                candidates.sort(key=lambda c: c[0])
                if candidates[0][1] == "region" and self._has_entity_context(tokens, i, before_only=True):
                    site_like = [c for c in candidates if c[1] in {"site", "site_exact", "code"}]
                    if site_like:
                        candidates = site_like
                _, kind, value = candidates[0]
                flush()
                mentions.append(self._mention(kind, value, phrase))
                i += n
                matched = True
                break
            if not matched:
                tok = tokens[i]
                if tok.isdigit() and i > 0 and not run and tokens[i - 1] in COMMON_WORDS and (
                        not mentions or mentions[-1].text.split()[-1] != tokens[i - 1]):
                    # "area 25" style partial names: a common word followed by a number
                    phrase = f"{tokens[i - 1]} {tok}"
                    pattern = re.compile(rf"(^| ){re.escape(phrase)}( |$)")
                    if any(pattern.search(k) for k in self.groups):
                        mentions.append(Mention("site", phrase, phrase, f"sites matching '{phrase}'"))
                        i += 1
                        continue
                if tok not in COMMON_WORDS and not tok.isdigit() and len(tok) >= 2:
                    run.append((i, tok))
                else:
                    flush()
                i += 1
        flush()
        return mentions

    def _resolve_run(self, tokens: list, run: list) -> list[Mention]:
        """Unmatched words: partial site names ("zomba"), typos ("zomba dm") or unknown ids ("ll001")."""
        start = run[0][0]
        words = [t for _, t in run]
        # a name usually follows the singular word ("site Kasungu"); after "sites" comes a verb ("sites compared")
        context = start > 0 and tokens[start - 1] in {"site", "cell", "tower", "bts", "node"} or (
            run[-1][0] + 1 < len(tokens) and tokens[run[-1][0] + 1] in {"site", "sites", "cell", "cells", "tower"})
        phrase = " ".join(words)
        strong = context or len(words) >= 2 or any(self._looks_like_identifier(w) for w in words)
        # partial physical-site name (every word of the run is a word of some site name)
        if all(len(w) >= 4 or w.isalnum() and re.search(r"\d", w) for w in words):
            pattern = re.compile(rf"(^| ){re.escape(phrase)}( |$)")
            if any(pattern.search(k) for k in self.groups):
                return [Mention("site", phrase, phrase, f"sites matching '{phrase}'")]
        if strong:
            match = self.resolve_site(phrase)
            if match and match.how in {"name", "fuzzy", "exact", "code"}:
                return [Mention("site", phrase, phrase, match.display)]
            cell = self.resolve_cell(phrase)
            if cell:
                return [Mention("cell", phrase, phrase, cell.display)]
        unknown = [w for w in words if self._looks_like_identifier(w)]
        if context and not unknown and difflib.get_close_matches(phrase, list(self.groups), n=1, cutoff=0.72):
            # an ordinary word next to "site" is only flagged when it looks like a misspelt site name;
            # Claude's plan is the second safety net for names that are not in the data at all
            unknown = [phrase]
        return [Mention("unknown", u, u, u) for u in unknown]

    def _has_entity_context(self, tokens, i, before_only=False) -> bool:
        before = tokens[i - 1] if i > 0 else ""
        after = tokens[i + 1] if i + 1 < len(tokens) else ""
        if before in ENTITY_CONTEXT_WORDS:
            return True
        return not before_only and after in {"site", "sites", "cell", "cells", "tower"}

    @staticmethod
    def _looks_like_identifier(tok: str) -> bool:
        if not (re.search(r"[a-z]", tok) and re.search(r"\d", tok)):
            return False
        if re.fullmatch(r"\d+(st|nd|rd|th|d|w|m|h|hr|hrs|days?|g|x|k|mb|gb|tb|pct|percent)", tok):
            return False
        if re.fullmatch(r"(top|worst|best|last|bottom|first|q|h|w|l|g|u|n)\d+", tok):
            return False
        if re.fullmatch(r"[gulnf]\d{3,4}\w*", tok):      # band names like l1800, u2100f1
            return False
        if re.fullmatch(r"(19|20)\d\d\w{0,3}", tok):     # years
            return False
        return True

    def _mention(self, kind: str, value: str, phrase: str) -> Mention:
        if kind == "site_exact":
            return Mention("site", value, phrase, value)
        if kind == "code":
            return Mention("site", value, phrase, value)
        if kind == "site":
            return Mention("site", value, phrase, self.group_display(value))
        if kind == "cell_group":
            return Mention("cell", value, phrase, value)
        if kind == "vendor":
            real = next((v for v in self.vendors if v.lower() == value.lower()), value.title())
            return Mention("vendor", real, phrase, real)
        return Mention(kind, value, phrase, value)

    # ------------------------------------------------------------------ resolution
    def resolve_site(self, value: str) -> SiteMatch | None:
        raw = str(value).strip()
        n = norm(raw)
        if not n:
            return None
        if n in self._site_norm:
            name = self._site_norm[n]
            return SiteMatch(raw, name, {name}, set(), "exact")
        code_key = n.replace(" ", "").upper()
        if code_key in self.codes or ("TN" + code_key) in self.codes:
            code = code_key if code_key in self.codes else "TN" + code_key
            names = set(self.codes[code])
            cells = set()
            for name in names:
                cells |= self.groups.get(norm(self.sites[name]["base"]), {}).get("cells", set())
            return SiteMatch(raw, code, names, cells, "code")
        # strip technology words and filler ("Zomba Dam 3G site")
        tokens = n.split()
        techs = {TECH_SYNONYMS[t] for t in tokens if t in TECH_SYNONYMS}
        core = " ".join(t for t in tokens if t not in TECH_SYNONYMS and t not in {"site", "sites", "tower", "bts"})
        if core in self.groups:
            return self._group_match(raw, core, techs, "name")
        if core in self.codes or core.upper() in self.codes:
            return self.resolve_site(core.upper())
        # contains: physical-site names that contain the words, e.g. "Zomba" -> Zomba_Dam, Zomba_Police...
        if core and len(core) >= 3:
            pattern = re.compile(rf"(^| ){re.escape(core)}( |$)")
            keys = [k for k in self.groups if pattern.search(k)]
            if keys:
                match = SiteMatch(raw, f"sites matching '{raw}'", set(), set(), "contains")
                for k in keys:
                    match.site_names |= self._filter_tech(self.groups[k]["sites"], techs)
                    if not techs:
                        match.cells |= self.groups[k]["cells"]
                if match.size():
                    return match
        # fuzzy (typos)
        close = difflib.get_close_matches(core or n, list(self.groups), n=3, cutoff=0.86)
        if close:
            return self._group_match(raw, close[0], techs, "fuzzy")
        return None

    def _filter_tech(self, names: set, techs: set) -> set:
        if not techs:
            return set(names)
        return {s for s in names if (self.sites.get(s, {}).get("technology") or "") in techs}

    def _group_match(self, raw: str, key: str, techs: set, how: str) -> SiteMatch | None:
        group = self.groups[key]
        names = self._filter_tech(group["sites"], techs)
        cells = set(group["cells"]) if not techs else {
            c for c in group["cells"] if (self.cells.get(c, {}).get("technology") or "") in techs}
        if not names and not cells:
            return None
        return SiteMatch(raw, group["display"], names, cells, how)

    def resolve_cell(self, value: str) -> CellMatch | None:
        raw = str(value).strip()
        n = norm(raw)
        if not n:
            return None
        if n in self._cell_norm:
            name = self._cell_norm[n]
            return CellMatch(raw, name, {name}, "exact")
        if n in self.cell_groups:
            members = sorted(self.cell_groups[n])
            display = os.path.commonprefix(members).rstrip("_") if len(members) > 1 else members[0]
            return CellMatch(raw, display or raw, set(members), "sectors")
        if n in self._cell_noprefix:
            members = self._cell_noprefix[n]
            return CellMatch(raw, raw if len(members) > 1 else next(iter(members)), set(members), "exact")
        prefixed = [c for k, c in self._cell_norm.items() if k.startswith(n + " ")]
        if prefixed and len(prefixed) <= 500:
            return CellMatch(raw, f"cells starting with '{raw}'", set(prefixed), "prefix")
        first = n.split()[0]
        pool = [k for k in self._cell_norm if k.split()[0] == first or first in k.split()[:2]] or list(self._cell_norm)
        close = difflib.get_close_matches(n, pool, n=3, cutoff=0.9)
        if close:
            name = self._cell_norm[close[0]]
            return CellMatch(raw, name, {name}, "fuzzy")
        return None

    def resolve_region(self, value: str):
        """Returns the region name, None for 'Unknown region', or raises KeyError."""
        n = norm(value)
        n = REGION_ALIASES.get(n, n)
        n = re.sub(r"\s*(region|regions|zone|area)$", "", n).strip() or n
        if n in UNKNOWN_REGION_WORDS:
            return None
        for r in self.regions:
            if norm(r) == n:
                return r
        close = difflib.get_close_matches(n, [norm(r) for r in self.regions], n=1, cutoff=0.75)
        if close:
            return next(r for r in self.regions if norm(r) == close[0])
        raise KeyError(value)

    def resolve_technology(self, value: str) -> str:
        n = norm(value).replace(" ", "")
        if n in TECH_SYNONYMS:
            return TECH_SYNONYMS[n]
        for t in self.technologies:
            if norm(t).replace(" ", "") == n:
                return t
        raise KeyError(value)

    def resolve_vendor(self, value: str) -> str:
        n = norm(value)
        for v in self.vendors:
            if norm(v) == n:
                return v
        raise KeyError(value)

    # ------------------------------------------------------------------ suggestions
    def suggest(self, value: str, kind: str = "site", limit: int = 5) -> list[str]:
        n = norm(value)
        if not n:
            return []
        if kind == "region":
            return list(self.regions)
        if kind == "technology":
            return list(self.technologies)
        if kind == "vendor":
            return list(self.vendors)
        out: list[str] = []
        if kind in {"site", "any"}:
            keys = list(self.groups)
            out += [self.groups[k]["display"] for k in difflib.get_close_matches(n, keys, n=limit, cutoff=0.5)]
            out += [self.groups[k]["display"] for k in keys if n in k and self.groups[k]["display"] not in out][:limit]
            out += [s for k, s in self._site_norm.items() if n.replace(" ", "") in k.replace(" ", "") and s not in out][:limit]
        if kind in {"cell", "any"}:
            keys = list(self._cell_norm)
            first = n.split()[0]
            pool = [k for k in keys if first in k] or keys
            out += [self._cell_norm[k] for k in difflib.get_close_matches(n, pool, n=limit, cutoff=0.5)]
        unique = []
        for s in out:
            if s not in unique:
                unique.append(s)
        return unique[:limit]

    def autocomplete(self, prefix: str, limit: int = 10) -> list[dict]:
        n = norm(prefix)
        if len(n) < 2:
            return []
        results: list[dict] = []

        def add(label, kind, detail=""):
            if len(results) < limit and all(r["label"] != label for r in results):
                results.append({"label": label, "kind": kind, "detail": detail})

        for r in self.regions:
            if norm(r).startswith(n):
                add(r, "region", "Region")
        for key, g in self.groups.items():
            if key.startswith(n):
                techs = sorted({self.sites[s]["technology"] for s in g["sites"] if self.sites[s]["technology"]})
                add(g["display"], "site", "Site · " + "/".join(techs) if techs else "Site")
        for k, s in self._site_norm.items():
            if k.startswith(n) or k.replace(" ", "").startswith(n.replace(" ", "")):
                add(s, "site", f"Site · {self.sites[s]['technology'] or ''} · {self.sites[s]['region'] or 'Unknown'}")
        if len(results) < limit:
            for key, g in self.groups.items():
                if f" {n}" in f" {key}" and not key.startswith(n):
                    add(g["display"], "site", "Site")
        if len(results) < limit:
            for k, c in self._cell_norm.items():
                if k.startswith(n) or f" {n}" in f" {k}":
                    add(c, "cell", f"Cell · {self.cells[c]['technology'] or ''}")
        return results

    def summary_text(self) -> str:
        """Compact description of the data for the planner prompt."""
        unavailable = [m.label for mid, m in METRICS.items() if not self.metric_available(mid)]
        coverage = []
        for mid in ("cssr_bh", "data_sr_bh", "tch_availability_daily", "drop_rate_bh", "cs_traffic_daily", "dl_volume_daily"):
            if self.metric_available(mid):
                coverage.append(f"{METRICS[mid].label}: {', '.join(sorted(self.metric_techs(mid))) or 'none'}")
        return (
            f"- Daily dashboard covers {self.date_min} to {self.date_max} ({self.date_count} days, {self.row_count:,} cell-day rows)\n"
            f"- Regions: {', '.join(self.regions)}" + (" (some cells have no region -> 'Unknown')" if self.region_has_null else "") + "\n"
            f"- Technologies: {', '.join(self.technologies)}\n"
            f"- Vendors: {', '.join(self.vendors)}\n"
            f"- {len(self.groups)} physical sites, {len(self.sites)} site names, {len(self.cells)} cells\n"
            f"- KPI coverage by technology: {'; '.join(coverage)}\n"
            + (f"- KPIs with NO data at all (do not use): {', '.join(unavailable)}\n" if unavailable else "")
        )


_catalog: Catalog | None = None
_lock = threading.Lock()
_refreshing = False


def get_catalog(engine, force: bool = False) -> Catalog:
    """Cached catalog. Reloads synchronously the first time, in the background when stale."""
    global _catalog, _refreshing, _engine_for_refresh
    _engine_for_refresh = engine
    if _catalog is not None and not force:
        if _catalog.is_stale() and not _refreshing:
            _refreshing = True

            def refresh():
                global _catalog, _refreshing
                try:
                    _catalog = Catalog().load(engine)
                except Exception:
                    log.exception("catalog refresh failed; keeping the previous one")
                finally:
                    _refreshing = False

            threading.Thread(target=refresh, daemon=True).start()
        return _catalog
    with _lock:
        if _catalog is None or force:
            _catalog = Catalog().load(engine)
        return _catalog


def _catalog_ready() -> Catalog | None:
    """The cached catalog if it has been loaded (never blocks); triggers a background refresh when stale."""
    if _catalog is None:
        return None
    if _catalog.is_stale() and not _refreshing:
        try:
            return get_catalog_engine_refresh()
        except Exception:
            return _catalog
    return _catalog


_engine_for_refresh = None


def get_catalog_engine_refresh() -> Catalog | None:
    if _engine_for_refresh is not None:
        return get_catalog(_engine_for_refresh)
    return _catalog


def set_catalog(catalog: Catalog | None):
    """Used by tests to inject a catalog built from fixture rows."""
    global _catalog
    _catalog = catalog
