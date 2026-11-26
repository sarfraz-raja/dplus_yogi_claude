"""Date understanding for KPI questions.

All relative dates are anchored to the latest date that exists in the KPI table
(the "anchor"), because KPI data is loaded once a day and may lag behind the
calendar. "today"/"yesterday"/"latest" mean the newest loaded day; "last 7 days"
means the 7 loaded days ending at the anchor.
"""
import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
MONTHS["sept"] = 9

_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
              "eight": 8, "nine": 9, "ten": 10, "fourteen": 14, "fifteen": 15, "thirty": 30,
              "ek": 1, "do": 2, "teen": 3, "char": 4, "paanch": 5, "saat": 7, "das": 10}

_UNIT_DAYS = {"day": 1, "days": 1, "din": 1, "dino": 1, "dinon": 1,
              "week": 7, "weeks": 7, "hafte": 7, "hafta": 7, "hafton": 7, "wk": 7, "wks": 7,
              "month": 30, "months": 30, "mahine": 30, "mahina": 30, "mahino": 30}


@dataclass
class DateRange:
    start: date
    end: date
    label: str

    def days(self) -> int:
        return (self.end - self.start).days + 1


@dataclass
class DateIntent:
    range: DateRange | None = None
    previous: DateRange | None = None   # set when the question compares two periods
    explicit: bool = False              # the user named calendar dates (not relative)


def latest_day(today: date, data_max: date | None) -> date:
    """The day that 'today'/'yesterday' refer to."""
    if data_max is None:
        return today
    return min(today, data_max)


def _iso(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def month_range(year: int, month: int) -> DateRange:
    last = calendar.monthrange(year, month)[1]
    return DateRange(date(year, month, 1), date(year, month, last), f"{calendar.month_name[month]} {year}")


def _find_calendar_dates(q: str, anchor: date) -> list[date]:
    """ISO dates, dd-mm-yyyy, '5 May', 'May 5', '5th May 2026'."""
    found: list[tuple[int, date]] = []
    for m in re.finditer(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", q):
        d = _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            found.append((m.start(), d))
    for m in re.finditer(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b", q):
        d = _iso(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if d:
            found.append((m.start(), d))
    month_names = "|".join(sorted(MONTHS, key=len, reverse=True))
    for m in re.finditer(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({month_names})\b(?:,?\s+(\d{{4}}))?", q):
        year = int(m.group(3)) if m.group(3) else anchor.year
        d = _iso(year, MONTHS[m.group(2)], int(m.group(1)))
        if d:
            found.append((m.start(), d))
    for m in re.finditer(rf"\b({month_names})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", q):
        year = int(m.group(3)) if m.group(3) else anchor.year
        d = _iso(year, MONTHS[m.group(1)], int(m.group(2)))
        if d:
            found.append((m.start(), d))
    found.sort()
    out: list[date] = []
    for _, d in found:
        if d not in out:
            out.append(d)
    return out


def _find_months(q: str, anchor: date) -> list[DateRange]:
    """Whole months named without a day: 'April', 'march 2026', 'in may'."""
    month_names = "|".join(sorted(MONTHS, key=len, reverse=True))
    ranges = []
    for m in re.finditer(rf"\b({month_names})\b(?:\s+(\d{{4}}))?", q):
        # skip 'may' used as a verb, and month names that belong to a day-date ("5 may")
        before = q[max(0, m.start() - 4):m.start()]
        after = q[m.end():m.end() + 4]
        if re.search(r"\d\s*(?:st|nd|rd|th)?\s*(?:of\s+)?$", before) or re.match(r"\s+\d{1,2}\b(?!\d)", after):
            continue
        word = m.group(1)
        if word == "may" and not m.group(2) and not re.search(r"\b(in|of|during|for|vs|versus|and|month|se|tak|ka|ke|ki|me|mein)\s+may\b|\bmay\s+(month|vs|versus|and|me|mein|ka|ke|ki|data)\b|^may\b", q):
            continue
        year = int(m.group(2)) if m.group(2) else anchor.year
        month = MONTHS[word]
        if not m.group(2) and month > anchor.month and year == anchor.year:
            year -= 1   # "december" asked in May means last December
        ranges.append(month_range(year, month))
    unique = []
    for r in ranges:
        if all(r.start != u.start for u in unique):
            unique.append(r)
    return unique


def _last_n(q: str) -> int | None:
    m = re.search(r"\b(?:last|past|previous|pichle|pichhle|pichley|pichhley|recent|next)\s+(\d+|[a-z]+)\s+(day|days|din|dino|dinon|week|weeks|hafte|hafta|hafton|wk|wks|month|months|mahine|mahina|mahino)\b", q)
    if not m:
        m = re.search(r"\b(\d+)\s+(day|days|din|dino|dinon|week|weeks|hafte|hafta|month|months|mahine|mahina)\b", q)
    if not m:
        bare = re.search(r"\b(?:last|past|pichle|pichhle)\s+(\d{1,3})\b(?!\s*(?:%|percent|sites?|cells?|regions?|rows?))", q)
        if bare:
            return max(1, min(int(bare.group(1)), 3660))
        return None
    raw = m.group(1)
    n = int(raw) if raw.isdigit() else _NUM_WORDS.get(raw)
    if not n:
        return None
    return max(1, min(n * _UNIT_DAYS[m.group(2)], 3660))


def parse_dates(question: str, anchor: date) -> DateIntent:
    """Read the date range (and an optional comparison period) from a question."""
    q = question.lower()
    compare_words = bool(re.search(r"\b(vs|versus|compared?\s+(to|with)|compare|comparison|over\s+previous|wow|week\s+over\s+week|day\s+over\s+day|mom|month\s+over\s+month|change|changed|improve[ds]?|degrad\w*|than)\b", q))

    # 1. Calendar dates
    dates = _find_calendar_dates(q, anchor)
    if len(dates) >= 2:
        if compare_words and re.search(r"\b(vs|versus|compared?|comparison)\b", q):
            a, b = sorted(dates[:2])
            return DateIntent(range=DateRange(b, b, b.isoformat()), previous=DateRange(a, a, a.isoformat()), explicit=True)
        a, b = sorted(dates[:2])
        return DateIntent(range=DateRange(a, b, f"{a.isoformat()} to {b.isoformat()}"), explicit=True)
    if len(dates) == 1:
        d = dates[0]
        if re.search(r"\b(since|from|after|se)\b", q) and not re.search(r"\b(to|till|until|tak)\b", q):
            return DateIntent(range=DateRange(d, anchor, f"{d.isoformat()} to {anchor.isoformat()}"), explicit=True)
        if re.search(r"\b(until|till|before|upto|up to|tak)\b", q):
            return DateIntent(range=DateRange(date(2000, 1, 1), d, f"up to {d.isoformat()}"), explicit=True)
        return DateIntent(range=DateRange(d, d, d.isoformat()), explicit=True)

    # 2. Named months
    months = _find_months(q, anchor)
    if len(months) >= 2:
        a, b = sorted(months[:2], key=lambda r: r.start)
        if compare_words:
            return DateIntent(range=b, previous=a, explicit=True)
        return DateIntent(range=DateRange(a.start, b.end, f"{a.label} to {b.label}"), explicit=True)
    if len(months) == 1:
        intent = DateIntent(range=months[0], explicit=True)
        if compare_words and re.search(r"\b(previous|last|prior|pichle|pichhle)\s+month\b", q):
            prev_end = months[0].start - timedelta(days=1)
            intent.previous = month_range(prev_end.year, prev_end.month)
        return intent

    # 3. Relative phrases
    rng: DateRange | None = None
    n = _last_n(q)
    if n:
        rng = DateRange(anchor - timedelta(days=n - 1), anchor, f"last {n} days")
    elif re.search(r"\b(today|aaj|latest|current day|abhi|recent day|most recent)\b", q):
        rng = DateRange(anchor, anchor, f"latest day ({anchor.isoformat()})")
    elif re.search(r"\b(yesterday|kal|last day|previous day)\b", q):
        rng = DateRange(anchor, anchor, f"latest day ({anchor.isoformat()})")
    elif re.search(r"\b(day before yesterday|parso)\b", q):
        d = anchor - timedelta(days=1)
        rng = DateRange(d, d, d.isoformat())
    elif re.search(r"\b(this|current|is)\s+(week|hafte|hafta)\b", q):
        start = anchor - timedelta(days=anchor.weekday())
        rng = DateRange(start, anchor, "this week")
    elif re.search(r"\b(last|previous|pichle|pichhle|past)\s+(week|hafte|hafta)\b", q) or re.search(r"\bweek\b", q) and not re.search(r"\b(weekly|week wise|weekwise|per week|by week|each week|week over week)\b", q):
        rng = DateRange(anchor - timedelta(days=6), anchor, "last 7 days")
    elif re.search(r"\b(this|current|is)\s+(month|mahine|mahina)\b", q) or re.search(r"\bmtd\b", q):
        rng = DateRange(anchor.replace(day=1), anchor, "this month")
    elif re.search(r"\b(last|previous|pichle|pichhle|past)\s+(month|mahine|mahina)\b", q):
        prev_end = anchor.replace(day=1) - timedelta(days=1)
        rng = month_range(prev_end.year, prev_end.month)
    elif re.search(r"\bfortnight\b", q):
        rng = DateRange(anchor - timedelta(days=13), anchor, "last 14 days")

    intent = DateIntent(range=rng)
    # Period-over-period comparison ("vs last week", "week over week", "compared to previous period")
    wants_previous = bool(re.search(
        r"\b(vs|versus|compared?\s+(to|with)|than)\s+(the\s+)?(previous|last|prior|pichle|pichhle)\b"
        r"|\bover\s+(the\s+)?previous\b|\bweek\s+over\s+week\b|\bwow\b|\bday\s+over\s+day\b|\bdod\b"
        r"|\bmonth\s+over\s+month\b|\bmom\b|\bprevious\s+period\b", q))
    if wants_previous:
        if rng is None:
            if re.search(r"\bday\s+over\s+day\b|\bdod\b", q):
                rng = DateRange(anchor, anchor, f"latest day ({anchor.isoformat()})")
            elif re.search(r"\bmonth\s+over\s+month\b|\bmom\b", q):
                rng = DateRange(anchor - timedelta(days=29), anchor, "last 30 days")
            else:
                rng = DateRange(anchor - timedelta(days=6), anchor, "last 7 days")
            intent.range = rng
        length = rng.days()
        intent.previous = DateRange(rng.start - timedelta(days=length), rng.start - timedelta(days=1), "previous period")
    return intent


def parse_iso(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    m = re.match(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})", str(value))
    return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
