"""
Generic parser + query tool for IIT-B ASC course listings.

Two input formats, one parser:

* SingleFile saves of an ASC course page. That save is a *frameset*, and each
  frame's real HTML is stuffed into a `src="data:text/html,..."` URI. This tool
  digs the course table out of there.
* `asc-bundle` JSON produced by grab.js (pasted into the DevTools console of a
  logged-in ASC tab): every department's running-courses page plus every
  course's crsedetail.jsp page, fetched over the user's own session and saved
  as one file. Bundles additionally carry OFFICIAL per-course facts (credits,
  half-semester flag, description) that the listing table alone doesn't have.

--------------------------------------------------------------------------------
Design patterns used (asked for on purpose):

- Value Object          : `Course` is a frozen dataclass (immutable, comparable).
- Adapter               : `SingleFileSource` adapts SingleFile's frameset +
                          data-URI mess into plain inner-HTML documents.
- Factory Method        : `ASCParser.parse` builds `Course` objects from a row;
                          `load(path)` is the top-level factory for a catalog.
- Repository            : `CourseCatalog` is an in-memory collection with a
                          query API (find/filter), hiding storage details.
- Specification         : `Spec` predicates (`InSlot`, `CodeMatches`, `DeptIn`)
                          compose with `&` `|` `~` -> "not in slot" is just `~InSlot`.
- Fluent Interface      : `CourseCatalog` query methods return a new catalog,
                          so calls chain: `cat.where(...).exclude_slots(...)`.
- Facade                : `load()` hides Adapter + Parser + Repository wiring.
- Iterator              : `CourseCatalog` is iterable / sized / indexable.
--------------------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import html
import re
import sys
import urllib.parse
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from bs4 import BeautifulSoup, Tag

# bs4 backend: lxml (C, ~4x faster — vendored in ./pyodide/ and loaded by the
# webapp) when importable, else the stdlib parser. Same tree either way here:
# ASC pages are plain <table> soup without the edge cases the backends disagree on.
try:
    import lxml  # noqa: F401  (probe only; bs4 does the actual importing)
    SOUP_PARSER = "lxml"
except ImportError:            # pragma: no cover - depends on environment
    SOUP_PARSER = "html.parser"

# ----------------------------------------------------------------------------- 
# Script location (CWD-independent)
# -----------------------------------------------------------------------------
# Resolve the template, the default build outputs, and this module's OWN source
# relative to THIS file rather than the current working directory, so the tool
# works no matter where it's invoked from. The canonical command is run from the
# repo root as `python3 asc_parser.py --build` (or `--cdn`); source,
# build tooling and runtime all live together in this folder.
_HERE = Path(__file__).resolve()          # this file (used for self-embedding)
_SCRIPT_DIR = _HERE.parent                # this script's own folder (template + outputs)


# ----------------------------------------------------------------------------- 
# Value Objects
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Restriction:
    """One row of a course's ASC restriction table (an allow/deny rule)."""
    batch: str        # entry/batch year, e.g. "2024" or "ALL"
    dept: str         # department name, or "ALL"
    program: str      # e.g. "B.Tech.", or "ALL"
    allowed: bool     # True = Allowed, False = Deny
    overridable: bool


@dataclass(frozen=True)
class CourseDetail:
    """Official per-course facts from ASC's crsedetail.jsp 'COURSE INFO' page."""
    code: str
    credits: float | None = None     # "Total Credits" (institute-official)
    lecture: float | None = None     # weekly hours as listed
    tutorial: float | None = None
    practical: float | None = None
    selfstudy: float | None = None
    half_sem: bool | None = None     # "Half Semester" Y/N
    description: str = ""


@dataclass(frozen=True)
class Course:
    """One course-section row from the ASC table (immutable value object)."""
    code: str            # normalized, e.g. "CS 101"
    name: str
    slot: str            # canonical slot token, e.g. "7", "14", "A1"
    course_type: str     # Theory / Lab / ...
    category: str        # ASC tag: "Advanced Course." / "Interdisciplinary STEM Course."
    instructor: str
    venue: str
    division: str
    slot_detail: str = ""  # full slot cell (days/times), e.g. "7 Wed-7A-.. Fri-7B-.."
    restrictions: tuple[Restriction, ...] = ()  # eligibility rules (allow/deny)
    source: str = ""       # provenance: which uploaded file/dept page this came from

    @property
    def dept(self) -> str:
        """Department letters, e.g. 'CS' from 'CS 101' or 'CS6001'."""
        m = re.match(r"[A-Za-z]+", self.code.replace(" ", ""))
        return m.group(0) if m else ""

    @property
    def number(self) -> str:
        """Numeric part, e.g. '101' from 'CS 101', '6001' from 'CS6001'."""
        m = re.search(r"\d[\dA-Za-z]*$", self.code.replace(" ", ""))
        return m.group(0) if m else ""

    @property
    def slot_tokens(self) -> frozenset[str]:
        """Slot tokens, upper-cased, e.g. '7' -> {'7'}, 'L1' -> {'L1'}."""
        return frozenset(t.upper() for t in re.split(r"[^A-Za-z0-9]+", self.slot) if t)

    @property
    def instructor_short(self) -> str:
        """Instructor names without the 'I -'/'A -' status prefixes."""
        names = re.sub(r"\b[IA]\s*-\s*", "", self.instructor)
        return re.sub(r"\s+", " ", names).strip(" ,")

    @property
    def is_advanced(self) -> bool:
        """ASC-tagged 'Advanced Course.' (vs 'Interdisciplinary STEM Course.')."""
        return "advanced" in self.category.lower()

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "slot": self.slot,
            "slot_detail": self.slot_detail,
            "dept": self.dept,
            "number": self.number,
            "level": (self.number[:1] + "xx") if self.number[:1].isdigit() else "?",
            "type": self.course_type,
            "category": self.category,
            "advanced": self.is_advanced,
            "instructor": self.instructor_short,
            "venue": self.venue,
            "division": self.division,
            "source": self.source,
            "restrictions": [asdict(r) for r in self.restrictions],
            # schedule structure (see the "Schedule & credit heuristics" section)
            "meetings": parse_meetings(self.slot_detail, self.slot),
            "kind": slot_kind(self.slot),
            "unit_key": plannable_key(self.code, self.division),
        }

    def __str__(self) -> str:
        return f"{self.code:<10} slot {self.slot:<4} {self.course_type:<8} {self.name}"


# ----------------------------------------------------------------------------- 
# Specification pattern (composable filters)
# -----------------------------------------------------------------------------
class Spec:
    """A predicate over Course that composes with & | ~ (Specification pattern)."""

    def is_satisfied(self, c: Course) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def __call__(self, c: Course) -> bool:
        return self.is_satisfied(c)

    def __and__(self, other: "Spec") -> "Spec":
        return _And(self, other)

    def __or__(self, other: "Spec") -> "Spec":
        return _Or(self, other)

    def __invert__(self) -> "Spec":
        return _Not(self)


class _Predicate(Spec):
    def __init__(self, fn: Callable[[Course], bool]):
        self._fn = fn

    def is_satisfied(self, c: Course) -> bool:
        return self._fn(c)


class _And(Spec):
    def __init__(self, a: Spec, b: Spec):
        self.a, self.b = a, b

    def is_satisfied(self, c: Course) -> bool:
        return self.a.is_satisfied(c) and self.b.is_satisfied(c)


class _Or(Spec):
    def __init__(self, a: Spec, b: Spec):
        self.a, self.b = a, b

    def is_satisfied(self, c: Course) -> bool:
        return self.a.is_satisfied(c) or self.b.is_satisfied(c)


class _Not(Spec):
    def __init__(self, a: Spec):
        self.a = a

    def is_satisfied(self, c: Course) -> bool:
        return not self.a.is_satisfied(c)


def _norm_slot(s) -> str:
    return str(s).strip().upper()


class InSlot(Spec):
    """Course runs in any of the given slots (int or str, case-insensitive)."""
    def __init__(self, *slots):
        self.slots = {_norm_slot(s) for s in slots}

    def is_satisfied(self, c: Course) -> bool:
        return bool(self.slots & c.slot_tokens)


class DeptIn(Spec):
    """Course department is one of the given (e.g. 'CS', 'ME')."""
    def __init__(self, *depts):
        self.depts = {d.strip().upper() for d in depts}

    def is_satisfied(self, c: Course) -> bool:
        return c.dept.upper() in self.depts


class CodeMatches(Spec):
    """Course code matches a regex (spaces optional, case-insensitive)."""
    def __init__(self, pattern: str):
        self.rx = re.compile(pattern, re.I)

    def is_satisfied(self, c: Course) -> bool:
        flat = c.code.replace(" ", "")
        return bool(self.rx.search(c.code) or self.rx.search(flat))


class NumberIn(Spec):
    """Course numeric part is one of the given (e.g. '101')."""
    def __init__(self, *numbers):
        self.numbers = {str(n).strip() for n in numbers}

    def is_satisfied(self, c: Course) -> bool:
        return c.number in self.numbers


# ----------------------------------------------------------------------------- 
# Repository + Fluent Interface + Iterator
# -----------------------------------------------------------------------------
class CourseCatalog:
    """In-memory repository of courses with a chainable query API."""

    def __init__(self, courses: Iterable[Course]):
        self._courses: list[Course] = list(courses)

    # -- Iterator / container protocol --
    def __iter__(self) -> Iterator[Course]:
        return iter(self._courses)

    def __len__(self) -> int:
        return len(self._courses)

    def __getitem__(self, i):
        return self._courses[i]

    # -- Fluent query methods (each returns a new catalog) --
    def where(self, spec: Spec) -> "CourseCatalog":
        return CourseCatalog(c for c in self._courses if spec(c))

    def in_slot(self, *slots) -> "CourseCatalog":
        return self.where(InSlot(*slots))

    def not_in_slot(self, *slots) -> "CourseCatalog":
        return self.where(~InSlot(*slots))

    def in_dept(self, *depts) -> "CourseCatalog":
        return self.where(DeptIn(*depts))

    def code_matches(self, pattern: str) -> "CourseCatalog":
        return self.where(CodeMatches(pattern))

    def numbers(self, *nums) -> "CourseCatalog":
        return self.where(NumberIn(*nums))

    def distinct(self, key: Callable[[Course], object] = lambda c: (c.code, c.slot)) -> "CourseCatalog":
        seen, out = set(), []
        for c in self._courses:
            k = key(c)
            if k not in seen:
                seen.add(k)
                out.append(c)
        return CourseCatalog(out)

    def sort(self, key: Callable[[Course], object] = lambda c: (c.dept, c.number)) -> "CourseCatalog":
        return CourseCatalog(sorted(self._courses, key=key))

    def slots(self) -> list[str]:
        """All distinct slot tokens present, sorted (numbers first)."""
        toks = {t for c in self._courses for t in c.slot_tokens}
        return sorted(toks, key=lambda t: (not t.isdigit(), int(t) if t.isdigit() else 0, t))

    def to_list(self) -> list[Course]:
        return list(self._courses)


# ----------------------------------------------------------------------------- 
# Adapter: SingleFile frameset -> inner HTML documents
# -----------------------------------------------------------------------------
class SingleFileSource:
    """Adapts a SingleFile-saved page into raw inner-HTML document strings.

    SingleFile saves framesets with each frame's HTML in a `data:text/html,...`
    URI. This yields the decoded HTML of every frame, plus the outer document,
    so the parser can scan all of them for the course table.
    """

    def __init__(self, raw_html: str):
        self._raw = raw_html

    @classmethod
    def from_path(cls, path: str | Path) -> "SingleFileSource":
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
        return cls(text)

    def documents(self) -> Iterator[str]:
        soup = BeautifulSoup(self._raw, SOUP_PARSER)
        frames = soup.find_all(["frame", "iframe"])
        found_frame = False
        for fr in frames:
            src = fr.get("src") or fr.get("srcdoc") or ""
            if src.startswith("data:text/html"):
                found_frame = True
                yield self._decode_data_uri(src)
        # Fallback: no data-URI frames -> the page itself holds the content.
        if not found_frame:
            yield self._raw

    @staticmethod
    def _decode_data_uri(uri: str) -> str:
        body = uri.split(",", 1)[1] if "," in uri else uri
        if ";base64" in uri.split(",", 1)[0]:
            import base64
            return base64.b64decode(body).decode("utf-8", "ignore")
        return urllib.parse.unquote(body)


# ----------------------------------------------------------------------------- 
# Parser (Factory Method for Course)
# -----------------------------------------------------------------------------
class ASCParser:
    """Turns ASC HTML into Course objects, mapping columns by header text.

    Header-driven mapping means it survives column reordering across semesters.
    """

    # header keyword -> logical field
    _FIELDS = {
        "course code": "code",
        "course name": "name",
        "course type": "course_type",
        "course content": "category",
        "instructor": "instructor",
        "venue": "venue",
        "slot": "slot",
        "division": "division",
        "restriction": "restrictions",
    }

    def parse(self, documents: Iterable[str]) -> list[Course]:
        courses: list[Course] = []
        for doc in documents:
            soup = BeautifulSoup(doc, SOUP_PARSER)
            courses.extend(self._parse_doc(soup))
        return courses

    def _parse_doc(self, soup: BeautifulSoup) -> list[Course]:
        rows = soup.find_all("tr")
        colmap = self._find_columns(rows)
        if not colmap or "slot" not in colmap:
            return []
        out = []
        for tr in rows:
            if not tr.find("a", href=re.compile("crsedetail")):
                continue  # only real course rows have the detail link
            cells = tr.find_all("td", recursive=False)
            if len(cells) <= colmap["slot"]:
                continue
            out.append(self._row_to_course(tr, cells, colmap))
        return out

    def _find_columns(self, rows: Sequence[Tag]) -> dict[str, int]:
        for tr in rows:
            cells = tr.find_all("td", recursive=False)
            texts = [self._text(td).lower() for td in cells]
            if any("course code" in t for t in texts):
                colmap: dict[str, int] = {}
                for i, t in enumerate(texts):
                    for kw, field in self._FIELDS.items():
                        if kw in t and field not in colmap:
                            colmap[field] = i
                return colmap
        return {}

    def _row_to_course(self, tr: Tag, cells: list[Tag], colmap: dict[str, int]) -> Course:
        def col(field: str) -> str:
            i = colmap.get(field, -1)
            return self._text(cells[i]) if 0 <= i < len(cells) else ""

        # Course code: prefer the detail-link text (cleanest), else the column.
        link = tr.find("a", href=re.compile("crsedetail"))
        code = self._norm_code(link.get_text() if link else col("code"))
        slot_cell = col("slot")
        ri = colmap.get("restrictions", -1)
        restrictions = self._parse_restrictions(cells[ri]) if 0 <= ri < len(cells) else ()
        return Course(
            code=code,
            name=col("name"),
            slot=self._first_slot(slot_cell),
            course_type=col("course_type"),
            category=col("category"),
            instructor=col("instructor"),
            venue=col("venue"),
            division=col("division"),
            slot_detail=re.sub(r"\s+", " ", slot_cell).strip(),
            restrictions=restrictions,
        )

    def _parse_restrictions(self, td: Tag) -> tuple[Restriction, ...]:
        """Parse the nested 'Restrictions' popup table (allow/deny rules)."""
        out: list[Restriction] = []
        for t in td.find_all("table"):
            rows = t.find_all("tr")
            if not rows:
                continue
            header = [self._text(x).lower() for x in rows[0].find_all(["td", "th"])]
            if not any("batch" in h for h in header):
                continue  # skip the title table; want the data table
            idx = {"batch": -1, "department": -1, "program": -1, "allowed": -1, "overridable": -1}
            for i, h in enumerate(header):
                for key in idx:
                    if key in h and idx[key] < 0:
                        idx[key] = i
            for r in rows[1:]:
                cells = [self._text(x) for x in r.find_all(["td", "th"])]
                if not any(cells):
                    continue
                get = lambda k: cells[idx[k]] if 0 <= idx[k] < len(cells) else ""
                out.append(Restriction(
                    batch=get("batch"),
                    dept=get("department"),
                    program=get("program"),
                    allowed=get("allowed").lower().startswith("allow"),
                    overridable=get("overridable").lower().startswith("overrid"),
                ))
        return tuple(out)

    @staticmethod
    def _text(node: Tag) -> str:
        return html.unescape(node.get_text(" ", strip=True)).replace("\xa0", " ").strip()

    @staticmethod
    def _norm_code(s: str) -> str:
        return re.sub(r"\s+", " ", html.unescape(s)).strip()

    @staticmethod
    def _first_slot(slot_cell: str) -> str:
        # slot cell looks like "7   Fri-7B-11:05.. Wed-7A-.." or "L   Mon-..";
        # the canonical slot is the leading token before the first day/time.
        head = re.split(r"[\s\-]+", slot_cell.strip(), maxsplit=1)[0]
        return head.upper()


# ----------------------------------------------------------------------------- 
# Grabber bundles (grab.js) + the official course-detail page
# -----------------------------------------------------------------------------
BUNDLE_KIND = "asc-bundle"   # the "kind" marker grab.js writes into its JSON


def _flat_code(code: str) -> str:
    """Course-code join key: 'CS 101' / 'cs101' -> 'CS101'."""
    return re.sub(r"\s+", "", code or "").upper()


def _maybe_bundle(text: str) -> dict | None:
    """Parse ``text`` as a grab.js bundle; ``None`` when it isn't one."""
    if not text.lstrip().startswith("{"):
        return None
    import json
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) and obj.get("kind") == BUNDLE_KIND else None


def parse_course_detail(code: str, html_text: str) -> CourseDetail:
    """Parse one crsedetail.jsp page: a two-column 'Fields | Content' table."""
    soup = BeautifulSoup(html_text, SOUP_PARSER)
    fields: dict[str, str] = {}
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) >= 2:
            k = ASCParser._text(cells[0]).lower().rstrip(":")
            if k and k not in fields:
                fields[k] = ASCParser._text(cells[1])

    def num(key: str) -> float | None:
        m = re.search(r"\d+(?:\.\d+)?", fields.get(key, ""))
        return float(m.group()) if m else None

    hs = fields.get("half semester", "").strip().upper()[:1]
    return CourseDetail(
        code=code,
        credits=num("total credits"),
        lecture=num("lecture"),
        tutorial=num("tutorial"),
        practical=num("practical"),
        selfstudy=num("selfstudy"),
        half_sem={"Y": True, "N": False}.get(hs),
        description=fields.get("description", ""),
    )


def _load_bundle(obj: dict) -> tuple[list["Course"], dict[str, CourseDetail], int | None]:
    """Expand a grab.js bundle into courses + official details + academic year.

    Each captured department page goes through the SAME parser as a SingleFile
    upload (``load_text`` falls back to the raw document when there are no
    data-URI frames). Details are keyed by flattened course code for joining.
    """
    courses: list[Course] = []
    for page in obj.get("pages", []):
        src = (page.get("dept") or "").split(",")[0].strip()
        courses.extend(load_text(page.get("html", ""), source=src))
    details: dict[str, CourseDetail] = {}
    for code, entry in (obj.get("details") or {}).items():
        html_text = (entry or {}).get("html", "")
        if html_text:
            details[_flat_code(code)] = parse_course_detail(code, html_text)
    m = re.match(r"(20\d{2})", str(obj.get("year", "")))
    year = int(m.group(1)) if m else _pick_year(
        _extract_academic_year(p.get("html", "")) for p in obj.get("pages", []))
    return courses, details, year


# ----------------------------------------------------------------------------- 
# Timetable metadata (IIT-B "New General Slot Pattern", Autumn 2026-27)
# -----------------------------------------------------------------------------
# slot -> {days, time}. Used by the front-end to show when each slot meets.
SLOT_INFO: dict[str, dict] = {
    "1":  {"days": ["Mon", "Tue", "Thu"], "time": "09:30–10:25"},
    "2":  {"days": ["Mon", "Tue", "Thu"], "time": "10:35–11:30"},
    "3":  {"days": ["Mon", "Tue", "Thu"], "time": "11:35–12:30"},
    "4":  {"days": ["Tue", "Thu"],        "time": "08:00–09:25"},
    "5":  {"days": ["Wed", "Fri"],        "time": "08:00–09:25"},
    "6":  {"days": ["Wed", "Fri"],        "time": "09:30–10:55"},
    "7":  {"days": ["Wed", "Fri"],        "time": "11:05–12:30"},
    "8":  {"days": ["Mon", "Thu"],        "time": "14:00–15:25"},
    "9":  {"days": ["Mon", "Thu"],        "time": "15:30–16:55"},
    "10": {"days": ["Tue", "Fri"],        "time": "14:00–15:25"},
    "11": {"days": ["Tue", "Fri"],        "time": "15:30–16:55"},
    "12": {"days": ["Mon", "Tue", "Thu"], "time": "17:05–18:00"},
    "L":  {"days": ["Mon", "Tue", "Wed", "Thu", "Fri"], "time": "lab block (see venue)"},
}

# Faithful weekly grid: per day, an ordered list of (time-label, slot) cells.
DAY_GRID: dict[str, list[list[str]]] = {
    "Mon": [["08:00–09:25", "XE"], ["09:30–10:25", "1"], ["10:35–11:30", "2"],
            ["11:35–12:30", "3"], ["14:00–15:25", "8"], ["15:30–16:55", "9"],
            ["17:05–18:00", "12"], ["14:00–17:00", "L"]],
    "Tue": [["08:00–09:25", "4"], ["09:30–10:25", "1"], ["10:35–11:30", "2"],
            ["11:35–12:30", "3"], ["14:00–15:25", "10"], ["15:30–16:55", "11"],
            ["17:05–18:00", "12"], ["14:00–17:00", "L"]],
    "Wed": [["08:00–09:25", "5"], ["09:30–10:55", "6"], ["11:05–12:30", "7"],
            ["14:00–17:00", "L"]],
    "Thu": [["08:00–09:25", "4"], ["09:30–10:25", "1"], ["10:35–11:30", "2"],
            ["11:35–12:30", "3"], ["14:00–15:25", "8"], ["15:30–16:55", "9"],
            ["17:05–18:00", "12"], ["14:00–17:00", "L"]],
    "Fri": [["08:00–09:25", "5"], ["09:30–10:55", "6"], ["11:05–12:30", "7"],
            ["14:00–15:25", "10"], ["15:30–16:55", "11"], ["14:00–17:00", "L"]],
}


# =============================================================================
# Schedule & credit heuristics  (MODULAR by design)
# -----------------------------------------------------------------------------
# Each uncertain assumption below is isolated in ONE small unit — a single
# function or table — carrying an ASSUMPTION note: what it assumes and how to
# change it. `parse_meetings` is the ONE reader of `slot_detail`; classification,
# L/T/P, credits and contact hours all build on the meeting dicts it returns, so
# a future Course->Section->Meeting refactor only has to move this one section.
# =============================================================================

# Weekday order used to sort meetings for display.
_WEEK_ORDER = {d: i for i, d in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])}

# ASSUMPTION: `slot_detail` packs meetings as repeated
#   "<slot> Day-Code-HH:MM:SS-HH:MM:SS-Room" segments; a room may be empty or
# contain hyphens/colons ("CC 201-SL-1", "Class Room : LA 202"). A meeting ends
# where the next "Day-Code-time" begins (or at end of string). To support a
# different ASC layout, change ONLY this regex and parse_meetings().
_MEETING_RE = re.compile(
    r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)-([0-9A-Za-z]+)-"
    r"(\d{1,2}:\d{2})(?::\d{2})?-(\d{1,2}:\d{2})(?::\d{2})?-"
    r"(.*?)(?=(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)-[0-9A-Za-z]+-\d{1,2}:\d{2}|$)",
    re.S,
)


def parse_meetings(slot_detail: str, slot: str = "") -> list[dict]:
    """THE single reader of `slot_detail` -> list of meeting dicts.

    Each meeting is ``{"day","code","start","end","room","slot","kind"}``. The
    meeting's *kind* is taken from the ROW's ``slot`` (the source of truth), NOT
    the inner code: an ``X`` row is a tutorial even when its inner code is numeric
    (e.g. MA 419's ``X Thu-3C-...``). Everything schedule-related (grid placement,
    clash checks, credits, contact hours) derives from these dicts, so this is the
    one function a future refactor must move.
    """
    kind = slot_kind(slot)
    out: list[dict] = []
    for day, code, start, end, room in _MEETING_RE.findall(slot_detail or ""):
        out.append({
            "day": day,
            "code": code.upper(),
            "start": _hhmm(start),
            "end": _hhmm(end),
            "room": _clean_room(room),
            "slot": slot,
            "kind": kind,
        })
    return out


def _hhmm(t: str) -> str:
    """'11:35:00' or '9:05' -> 'HH:MM' (zero-padded hour)."""
    h, _, rest = t.partition(":")
    return f"{int(h):02d}:{rest[:2]}"


def _clean_room(room: str) -> str:
    """Tidy a venue string (collapse spaces, drop the 'Class Room :' label)."""
    room = re.sub(r"\s+", " ", room).strip(" -")
    return re.sub(r"^Class\s*Room\s*:\s*", "", room, flags=re.I).strip()


# ASSUMPTION: a course component's kind is named by the ROW's *slot* — "X" is a
# tutorial, "L" a lab/practical, a numeric slot a lecture. (Inner meeting codes
# are unreliable: MA 419's tutorial row is slot "X" but packs code "3C".) Change
# this ONE function if a department uses other slot conventions.
def slot_kind(slot: str) -> str:
    s = (slot or "").strip().upper()
    if s == "X":
        return "tutorial"
    if s == "L":
        return "lab"
    if s[:1].isdigit():
        return "lecture"
    return "other"


# ASSUMPTION: report weekly hours as whole numbers. 55-/85-min slot blocks sum to
# fractions (2.75, 2.83, 2.92); round to the nearest integer (half up). This is
# the ONE rounding rule for L, P and displayed contact hours — edit here.
def round_hours(hours: float) -> int:
    return int(hours + 0.5)


# ASSUMPTION: contact hours = summed span (end - start) of the distinct meetings.
def meeting_hours(m: dict) -> float:
    def mins(t: str) -> int:
        h, _, mm = t.partition(":")
        return int(h) * 60 + int(mm or 0)
    return max(0.0, (mins(m["end"]) - mins(m["start"])) / 60.0)


# ASSUMPTION: IIT-B credit rule  C = 2L + 2T + P, where (see course_credits)
#   L = rounded weekly LECTURE hours, T = 1 if any tutorial, P = rounded weekly
#   LAB hours. The formula lives in exactly ONE place — edit this expression.
def estimate_credits(lectures: int, tutorials: int, practicals: int) -> int:
    return 2 * lectures + 2 * tutorials + practicals


# ASSUMPTION: some course *types* carry fixed institute credits regardless of
# slots (usually the slot-less entries). One table + one detector, so the numbers
# are trivial to edit. Applied BEFORE the L-T-P formula (see course_credits).
COURSE_TYPE_CREDITS = {
    "seminar": 4,   # Seminar
    "rnd": 6,       # R&D / research / project work (BTP, stage projects, M.S. R&D)
}


def _type_credit_override(name: str, ctype: str, has_slots: bool) -> int | None:
    """Fixed credits for special course types, else None (fall back to formula)."""
    hay = f"{name} {ctype}".lower()
    if "seminar" in hay:
        return COURSE_TYPE_CREDITS["seminar"]
    if any(k in hay for k in ("r&d", "r & d", "rnd", "research")):
        return COURSE_TYPE_CREDITS["rnd"]
    # "project" is a common word -> only treat *slot-less* project entries as R&D
    if "project" in hay and not has_slots:
        return COURSE_TYPE_CREDITS["rnd"]
    return None


# ASSUMPTION: the plannable unit is (course code + division). Divisions differ
# in time, so each is planned independently. To plan per-course instead, change
# ONLY this function (e.g. ``return code``); the UI follows it.
def plannable_key(code: str, division: str) -> str:
    return f"{code} \u00b7 {division}" if division else code


def _section_hours(meetings: Iterable[dict], kind: str) -> float:
    """Weekly hours of ONE section: the summed duration of its distinct `kind`
    meetings (deduped by day+time so room-batch duplicates don't inflate it)."""
    seen, total = set(), 0.0
    for m in meetings:
        if m["kind"] != kind:
            continue
        k = (m["day"], m["start"], m["end"])
        if k not in seen:
            seen.add(k)
            total += meeting_hours(m)
    return total


# ASSUMPTION: "a representative section" = the MOST COMMON rounded weekly hours
# across a course's divisions (ties -> the smaller, conservative). Divisions of
# one course should match; when they don't (a division schedules an extra block),
# this stops a single outlier from inflating the whole course. Change this ONE
# function to prefer max/min/median instead.
def _representative_hours(per_division_hours: Iterable[float]) -> int:
    rounded = [round_hours(h) for h in per_division_hours if h > 0]
    if not rounded:
        return 0
    best, best_n = 0, -1
    for v in sorted(set(rounded)):        # ascending -> ties keep the smaller value
        n = rounded.count(v)
        if n > best_n:
            best, best_n = v, n
    return best


def course_credits(rows: Sequence[Course]) -> dict:
    """Credits are a COURSE property (division-invariant) — computed once per code.

    L = representative weekly LECTURE hours (rounded), T = 1 if the course has ANY
    tutorial (slot X), P = representative weekly LAB hours (rounded); credits =
    2L + 2T + P — unless a course-type override (Seminar/R&D) applies. Using
    rounded *hours* (not meeting counts), taken from a representative section,
    makes every division of a course share the SAME credits: MA 105 -> 8 for
    D1..D4, MA 419 -> 8.
    """
    # meetings grouped per division, so "a section" == one division's component
    by_div: dict[str, list[dict]] = {}
    for c in rows:
        by_div.setdefault(c.division, []).extend(parse_meetings(c.slot_detail, c.slot))
    L = _representative_hours(_section_hours(ms, "lecture") for ms in by_div.values())
    P = _representative_hours(_section_hours(ms, "lab") for ms in by_div.values())
    has_tut = any(m["kind"] == "tutorial" for ms in by_div.values() for m in ms)
    has_slots = any(ms for ms in by_div.values())
    T = 1 if has_tut else 0
    rep = rows[0]
    override = _type_credit_override(rep.name, rep.course_type, has_slots)
    credits = override if override is not None else estimate_credits(L, T, P)
    return {"L": L, "T": T, "P": P, "credits": credits,
            "ltp": "" if override is not None else f"{L}-{T}-{P}",
            "credit_src": "type" if override is not None else "formula"}


def _dedupe_meetings(meetings: Iterable[dict]) -> list[dict]:
    """Collapse meetings sharing (day, slot, code, start, end), keeping every room.

    Keying on the row ``slot`` too keeps a lecture and a same-timed tutorial
    distinct, while still folding the many room-batch tutorial rows (e.g. MA 105's
    ~9 ``X`` rows) into a single meeting that lists its rooms.
    """
    by_key: dict[tuple, dict] = {}
    for m in meetings:
        k = (m["day"], m.get("slot", ""), m["code"], m["start"], m["end"])
        d = by_key.get(k)
        if d is None:
            d = {"day": m["day"], "code": m["code"], "start": m["start"], "end": m["end"],
                 "slot": m.get("slot", ""), "kind": m["kind"], "rooms": []}
            by_key[k] = d
        if m["room"] and m["room"] not in d["rooms"]:
            d["rooms"].append(m["room"])
    return sorted(by_key.values(), key=lambda x: (_WEEK_ORDER.get(x["day"], 9), x["start"]))


def build_units(courses: Sequence[Course]) -> dict[str, dict]:
    """Group flat rows into plannable units (course+division) and summarise each.

    THE one place meetings are aggregated across a unit's rows, so the front-end
    renders one entry per division instead of the ~40 duplicated MA 105 rows.
    Credits come from course_credits (division-invariant), so every division of a
    course shows the same credits + L-T-P triple.
    """
    # credits are a COURSE property -> compute once per code, share across divisions
    by_code: dict[str, list[Course]] = {}
    for c in courses:
        by_code.setdefault(c.code, []).append(c)
    cred_by_code = {code: course_credits(rows) for code, rows in by_code.items()}

    groups: dict[str, list[Course]] = {}
    for c in courses:
        groups.setdefault(plannable_key(c.code, c.division), []).append(c)
    units: dict[str, dict] = {}
    for key, rows in groups.items():
        meetings = _dedupe_meetings(m for c in rows for m in parse_meetings(c.slot_detail, c.slot))
        cc = cred_by_code[rows[0].code]
        rep = rows[0].as_dict()
        units[key] = {
            "key": key,
            "code": rep["code"], "name": rep["name"], "dept": rep["dept"],
            "number": rep["number"], "level": rep["level"], "type": rep["type"],
            "category": rep["category"], "advanced": rep["advanced"],
            "instructor": rep["instructor"], "source": rep["source"],
            "division": rep["division"], "restrictions": rep["restrictions"],
            "slots": sorted({c.slot for c in rows if c.slot}),
            "meetings": meetings,
            "L": cc["L"], "T": cc["T"], "P": cc["P"], "ltp": cc["ltp"],
            "est_credits": cc["credits"], "credit_src": cc["credit_src"],
            "contact_hours": round_hours(sum(meeting_hours(m) for m in meetings)),
            "sections": len(rows),
        }
    return units


# -----------------------------------------------------------------------------
# Academic year (data-driven base year for the front-end)
# -----------------------------------------------------------------------------
def _extract_academic_year(raw_html: str) -> int | None:
    """Best-effort academic *base* year (entry year of the incoming 1st-years).

    ASC labels a year range as e.g. "2026-2027"; the base year is the first
    number (2026). Sources are tried in order of reliability:

    1. the ASC form's selected year option: ``<option selected value=2026>``
    2. the "Running courses for year 2026-2027" banner
    3. any ``20XX-20YY`` range near the top of the page
    4. the SingleFile "saved date" comment (weak: the calendar save year)

    Returns ``None`` when nothing plausible is found. Both the outer document
    and any decoded SingleFile data-URI frames are searched.
    """
    docs = list(SingleFileSource(raw_html).documents())
    combined = "\n".join([raw_html, *docs])
    # 1. the form's selected <option> (tolerate either attribute order / quoting)
    for pat in (r"<option[^>]*\bselected\b[^>]*\bvalue\s*=\s*[\"']?(20\d{2})",
                r"<option[^>]*\bvalue\s*=\s*[\"']?(20\d{2})[\"']?[^>]*\bselected\b"):
        m = re.search(pat, combined, re.I)
        if m:
            return int(m.group(1))
    # 2. the "... for year 2026-2027 ..." banner
    m = re.search(r"year\s+(20\d{2})\s*[-\u2013\u2014]\s*20\d{2}", combined, re.I)
    if m:
        return int(m.group(1))
    # 3. a 20XX-20YY range near the top (avoid batch years deep in restriction tables)
    near_top = raw_html[:6000] + ("\n" + docs[0][:6000] if docs else "")
    m = re.search(r"(20\d{2})\s*[-\u2013\u2014/]\s*20\d{2}", near_top)
    if m:
        return int(m.group(1))
    # 4. SingleFile "saved date: ... 2026 ..." comment (last resort)
    m = re.search(r"saved date:[^<\n]*?(20\d{2})", combined, re.I)
    if m:
        return int(m.group(1))
    return None


def _pick_year(years: Iterable[int | None]) -> int | None:
    """Reconcile academic years across pages: most-common, ties -> latest year."""
    from collections import Counter
    vals = [y for y in years if y is not None]
    if not vals:
        return None
    counts = Counter(vals)
    top = max(counts.values())
    return max(y for y, c in counts.items() if c == top)


# -----------------------------------------------------------------------------
# Exporters
# -----------------------------------------------------------------------------
def _payload(catalog: CourseCatalog, meta: dict | None = None,
             details: dict[str, CourseDetail] | None = None) -> dict:
    """The canonical front-end payload: courses + plannable units + metadata.

    ``courses`` stays a flat per-row list (unchanged shape + new schedule fields)
    for back-compat; ``units`` groups those rows into plannable (course+division)
    units with aggregated meetings and estimated credits — the UI renders those
    so uncertain heuristics stay in this one file. ``meta`` carries page-level
    facts, e.g. ``{"academic_year": 2026}`` (``None`` when unknown). ``details``
    (from a grab.js bundle) adds OFFICIAL facts per course: ``official_credits``
    (+ ``credit_src="official"``), an official L-T-P, ``half_sem`` and
    ``description``. Estimates stay in ``est_credits`` untouched; the UI prefers
    official when present.
    """
    courses = list(catalog)
    units = build_units(courses)
    course_dicts = []
    for c in courses:
        d = c.as_dict()
        u = units.get(d["unit_key"])
        if u:  # surface the unit's (division-invariant) credit/contact estimate on each row too
            d["est_credits"] = u["est_credits"]
            d["contact_hours"] = u["contact_hours"]
            d["ltp"] = u["ltp"]
            d["credit_src"] = u["credit_src"]
        course_dicts.append(d)
    if details:
        def _fmt(x: float | None) -> str:
            return str(int(x)) if x and float(x).is_integer() else str(x or 0)

        def _apply(d: dict) -> None:
            cd = details.get(_flat_code(d["code"]))
            if not cd:
                return
            if cd.credits is not None:
                d["official_credits"] = int(cd.credits) if cd.credits.is_integer() else cd.credits
                d["credit_src"] = "official"
            if any(x is not None for x in (cd.lecture, cd.tutorial, cd.practical)):
                d["ltp"] = f"{_fmt(cd.lecture)}-{_fmt(cd.tutorial)}-{_fmt(cd.practical)}"
            if cd.half_sem is not None:
                d["half_sem"] = cd.half_sem
            if cd.description:
                d["description"] = cd.description
        for u in units.values():
            _apply(u)
        for d in course_dicts:
            _apply(d)
    return {
        "courses": course_dicts,
        "units": units,
        "slot_info": SLOT_INFO,
        "day_grid": DAY_GRID,
        "meta": meta or {"academic_year": None},
    }


def export_json(catalog: CourseCatalog, meta: dict | None = None,
                details: dict[str, CourseDetail] | None = None) -> str:
    """Serialize a catalog to JSON (courses + slot/grid + page metadata)."""
    import json
    return json.dumps(_payload(catalog, meta, details), ensure_ascii=False, indent=1)


def _js_backtick(src: str) -> str:
    """Escape text for embedding inside a JS template literal (backticks).

    Lossless — the JS engine restores the exact bytes. We escape backslashes,
    backticks and ``${`` (template-literal syntax), and ``</`` so the embedded
    source can't prematurely close the surrounding <script> tag.
    """
    return (src.replace("\\", "\\\\")
               .replace("`", "\\`")
               .replace("${", "\\${")
               .replace("</", "<\\/"))


def build_site(catalog: CourseCatalog, out_html: str | Path,
               template: str | Path | None = None,
               meta: dict | None = None, cdn: bool = False,
               details: dict[str, CourseDetail] | None = None) -> Path:
    """Render the self-contained front-end HTML.

    The page runs THIS parser in the browser via Pyodide, so we embed the full
    text of this module (single source of truth) for it to import at runtime.
    ``catalog`` is inlined as the initial dataset; an empty catalog is fine — the
    user drag-drops ASC .html pages in the page and they're parsed live.
    ``meta`` (e.g. ``{"academic_year": 2026}``) is inlined so the UI can derive
    its base year from the page rather than a hardcoded constant.

    ``cdn=False`` (default) keeps the fully-offline build that boots Pyodide from
    the vendored ``./pyodide/`` folder. ``cdn=True`` flips the ONE ``FORCE_CDN``
    switch in the template so the page loads Pyodide + bs4 from the CDN/PyPI at
    runtime — a single emailable file that needs no local folder (but needs net).
    """
    import json
    # Template sits alongside this script (template.html), resolved via
    # __file__ so the build is independent of the current working directory.
    tpl_path = Path(template) if template else _SCRIPT_DIR / "template.html"
    html_tpl = Path(tpl_path).read_text(encoding="utf-8")
    # initial dataset as JSON; guard against premature </script>
    blob = json.dumps(_payload(catalog, meta, details), ensure_ascii=False).replace("</", "<\\/")
    # embed this exact module (via __file__) so Pyodide is the one-and-only parser
    parser_lit = "`" + _js_backtick(_HERE.read_text(encoding="utf-8")) + "`"
    # embed grab.js so the page can offer a copy-to-clipboard grabber
    grab_path = _SCRIPT_DIR / "grab.js"
    grab_lit = ("`" + _js_backtick(grab_path.read_text(encoding="utf-8")) + "`"
                if grab_path.exists() else '""')
    if cdn:  # hard-select the template's existing CDN code path (skip local files)
        html_tpl = html_tpl.replace("/*__FORCE_CDN__*/false", "/*__FORCE_CDN__*/true")
    # ORDER MATTERS: this module's source contains every marker string above
    # (right here, in this function), so embedding it must come LAST — a global
    # replace done after it would also rewrite the embedded copy of the parser.
    out = (html_tpl
           .replace("/*__DATA__*/null", blob)
           .replace('/*__GRABJS__*/""', grab_lit)
           .replace('/*__ASCPY__*/""', parser_lit))
    out_path = Path(out_html)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out, encoding="utf-8")
    return out_path


# ----------------------------------------------------------------------------- 
# Facade
# -----------------------------------------------------------------------------
def load(path: str | Path) -> CourseCatalog:
    """Load one SingleFile ASC page from disk into a queryable CourseCatalog."""
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    return load_text(text, source=Path(path).stem)


def load_text(text: str, source: str = "") -> CourseCatalog:
    """Load one SingleFile ASC page from a raw HTML string (no disk access).

    Same parser as :func:`load`, but takes the HTML directly — this is what the
    in-browser (Pyodide) build calls on uploaded files. ``source`` is the tag to
    fall back on when the department can't be inferred (e.g. the file-name stem).
    """
    docs = SingleFileSource(text).documents()
    courses = ASCParser().parse(docs)
    tag = _dominant_dept(courses, source)
    return CourseCatalog(replace(c, source=tag) for c in courses)


def load_many(paths: Iterable[str | Path]) -> CourseCatalog:
    """Load several ASC pages (e.g. one per department) into one merged catalog."""
    merged: list[Course] = []
    for p in paths:
        merged.extend(load(p))
    return CourseCatalog(merged)


def _load_pairs(pairs: Iterable[tuple[str, str]],
                ) -> tuple[CourseCatalog, dict[str, CourseDetail], dict]:
    """Load ``(filename, text)`` pairs — SingleFile pages and/or grab.js bundles.

    THE shared input path for the browser (``parse_payload``) and the CLI: each
    text is sniffed (``_maybe_bundle``); bundles expand into their captured
    department pages + official course details, everything else parses as an
    ASC page. Returns (merged catalog, details-by-code, meta).
    """
    merged: list[Course] = []
    details: dict[str, CourseDetail] = {}
    years: list[int | None] = []
    meta: dict = {}
    for name, text in pairs:
        bundle = _maybe_bundle(text)
        if bundle is not None:
            b_courses, b_details, b_year = _load_bundle(bundle)
            merged.extend(b_courses)
            details.update(b_details)
            years.append(b_year)
            meta["kind"] = "bundle"
            if bundle.get("semester"):
                meta["semester"] = str(bundle["semester"])
        else:
            merged.extend(load_text(text, source=Path(name).stem if name else ""))
            years.append(_extract_academic_year(text))
    meta["academic_year"] = _pick_year(years)
    return CourseCatalog(merged), details, meta


def parse_payload(pairs: Iterable[tuple[str, str]]) -> dict:
    """Parse ``(filename, text)`` pairs into the front-end payload dict.

    Single browser-facing entry point: it runs the exact same parser as the CLI
    and returns the same shape ``build_site`` inlines
    (``{"courses": [...], "slot_info": ..., "day_grid": ..., "meta": ...}``),
    each course tagged by its file's dominant dept (fallback: the file-name
    stem). Accepts SingleFile .html saves and grab.js .json bundles alike.
    ``meta.academic_year`` is derived from the page(s)/bundle, reconciled
    across files.
    """
    catalog, details, meta = _load_pairs(pairs)
    return _payload(catalog, meta, details)


def _dominant_dept(courses: Sequence[Course], fallback: str = "") -> str:
    """Most common department among ``courses`` (``fallback`` if none present)."""
    depts = [c.dept for c in courses if c.dept]
    if depts:
        return max(set(depts), key=depts.count)
    return fallback


# ----------------------------------------------------------------------------- 
# CLI
# -----------------------------------------------------------------------------
def _print_table(catalog: CourseCatalog) -> None:
    rows = catalog.to_list()
    if not rows:
        print("(no matching courses)")
        return
    for c in rows:
        instr = (c.instructor[:30] + "…") if len(c.instructor) > 31 else c.instructor
        print(f"{c.code:<10} {c.slot:<5} {c.course_type:<8} {c.name[:45]:<45} {instr}")
    print(f"\n{len(rows)} course(s).")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Query ASC course dumps (SingleFile .html or grab.js .json).")
    ap.add_argument("files", nargs="*",
                    help="Zero or more SingleFile-saved ASC .html pages and/or grab.js .json bundles")
    ap.add_argument("--in-slot", nargs="+", metavar="S", help="keep courses running in these slots")
    ap.add_argument("--not-in-slot", nargs="+", metavar="S", help="drop courses running in these slots")
    ap.add_argument("--dept", nargs="+", metavar="D", help="keep only these departments, e.g. CS ME")
    ap.add_argument("--code", metavar="REGEX", help="keep codes matching regex, e.g. 'CS.?1' ")
    ap.add_argument("--number", nargs="+", metavar="N", help="keep these course numbers, e.g. 101 215")
    ap.add_argument("--distinct", action="store_true", help="collapse duplicate sections")
    ap.add_argument("--list-slots", action="store_true", help="just list all slots present and exit")
    ap.add_argument("--json", metavar="OUT", nargs="?", const="-",
                    help="export filtered courses as JSON (to file, or stdout if omitted)")
    ap.add_argument("--build", metavar="OUT", nargs="?", const="",
                    help="build the OFFLINE front-end HTML and exit; OUT is optional and "
                         "defaults alongside this script (<script_dir>/timetable.html)")
    ap.add_argument("--cdn", action="store_true",
                    help="build the ONLINE 'share' HTML (Pyodide from CDN, needs internet); "
                         "with no explicit --build OUT, writes <script_dir>/timetable-share.html")
    args = ap.parse_args(argv)

    cat, details, meta = _load_pairs(
        (str(f), Path(f).read_text(encoding="utf-8", errors="ignore")) for f in args.files)

    if args.list_slots:
        print("Slots present:", ", ".join(cat.slots()))
        return 0

    if args.build is not None or args.cdn:
        # Defaults live ALONGSIDE this script (CWD-independent), so the canonical
        #   python3 asc_parser.py --build   -> <script_dir>/timetable.html
        #   python3 asc_parser.py --cdn     -> <script_dir>/timetable-share.html
        # work from anywhere; an explicit `--build OUT` always wins.
        if args.build:                       # explicit OUT path given
            out_name = args.build
        elif args.cdn:                       # --cdn with no explicit OUT -> share file
            out_name = str(_SCRIPT_DIR / "timetable-share.html")
        else:                                # bare --build -> offline default
            out_name = str(_SCRIPT_DIR / "timetable.html")
        out = build_site(cat, out_name, meta=meta, cdn=args.cdn, details=details)
        kind = "ONLINE share build" if args.cdn else "offline build"
        print(f"Built front-end ({kind}): {out.resolve()}  ({len(cat)} courses, "
              f"academic year: {meta['academic_year'] or 'n/a'})")
        if args.cdn:
            # Self-contained single file: loads Pyodide from the CDN and installs
            # bs4 from PyPI at runtime -> works by double-click, but NEEDS INTERNET.
            print("  NOTE: this share file needs INTERNET (loads Pyodide/bs4 from the CDN). "
                  "Email/share the single .html — no ./pyodide/ folder required.")
        else:
            # Pyodide is vendored for offline use, but browsers block file:// module
            # fetches, so a plain double-click falls back to the CDN (needs internet).
            # For true offline, serve the folder over http -- serve.py does
            # this (and opens the page for you).
            print("  Offline: python3 serve.py   (serves it + opens your browser)")
            print(f"           or: cd {out.resolve().parent} && python3 -m http.server 8000 "
                  f"then open http://localhost:8000/{out.name}")
        return 0

    if args.in_slot:
        cat = cat.in_slot(*args.in_slot)
    if args.not_in_slot:
        cat = cat.not_in_slot(*args.not_in_slot)
    if args.dept:
        cat = cat.in_dept(*args.dept)
    if args.code:
        cat = cat.code_matches(args.code)
    if args.number:
        cat = cat.numbers(*args.number)
    if args.distinct:
        cat = cat.distinct()

    if args.json is not None:
        payload = export_json(cat, meta, details)
        if args.json == "-":
            print(payload)
        else:
            Path(args.json).write_text(payload, encoding="utf-8")
            print(f"Wrote {len(cat)} courses to {args.json}")
        return 0

    _print_table(cat.sort())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
