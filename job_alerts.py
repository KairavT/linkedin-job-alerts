#!/usr/bin/env python3
"""LinkedIn job alerts.

Searches LinkedIn's public (logged-out) job listings for each [[search]] in
config.toml, keeps postings from the configured time window that haven't been
sent before, and delivers them by email and/or ntfy push notification.

    python job_alerts.py                # search, notify, remember what was sent
    python job_alerts.py --dry-run      # search and print only; nothing sent or saved
    python job_alerts.py --preview      # search and open the email in your browser instead
    python job_alerts.py --test-notify  # send a sample alert to check email/ntfy setup
    python job_alerts.py --lookback     # one-off catch-up over the past 48 hours (or --lookback 3d, etc.)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import smtplib
import sys
import time
import tomllib
import webbrowser
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
SEEN_FILE = ROOT / "seen_jobs.json"
STATE_FILE = ROOT / "state.json"  # when the last alert went out, for check-in emails
SEEN_RETENTION_DAYS = 60

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
JOB_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"
PAGE_SIZE = 10
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# LinkedIn's URL codes for the optional search filters.
EXPERIENCE = {"internship": "1", "entry": "2", "associate": "3", "mid-senior": "4", "director": "5", "executive": "6"}
JOB_TYPE = {"full-time": "F", "part-time": "P", "contract": "C", "temporary": "T", "internship": "I", "volunteer": "V", "other": "O"}
WORKPLACE = {"on-site": "1", "remote": "2", "hybrid": "3"}

NTFY_MAX_LINES = 15

# When the run has to wrap up, from max_run_minutes. LinkedIn's rate limiting can otherwise stretch
# a run out for hours, since every blocked request backs off for minutes. Measured with
# time.monotonic(), which stops while the computer sleeps: macOS can start a run during one of its
# short background wakes with the lid closed, and that sleep shouldn't use up the run's time.
RUN_DEADLINE: float | None = None


class OutOfTime(Exception):
    """The run reached max_run_minutes."""


def out_of_time() -> bool:
    return RUN_DEADLINE is not None and time.monotonic() > RUN_DEADLINE


class UnreadablePage(Exception):
    """LinkedIn sent a page, but no job listings could be read from it."""


# Job description pages loaded this run, and how many had no description we could find.
DESCRIPTION_PAGES = {"read": 0, "unreadable": 0}

# LinkedIn job IDs go up over time, roughly half a million a day. A reposted job keeps its original ID
# while LinkedIn lists it as just posted, so a "new" job whose ID is far below the newest ones is really
# an older posting, usually with a pile of applicants already. LinkedIn's public pages don't say so directly.
JOB_IDS_PER_DAY = 500_000
REPOST_MARGIN_DAYS = 2  # a genuinely new posting's ID can be a little older (drafted before it went up)
NEWEST_JOB_ID = {"id": 0}  # highest job ID seen this run (or saved from earlier runs)


@dataclass
class Job:
    id: str
    title: str
    company: str
    location: str
    posted: str  # LinkedIn's relative text, e.g. "3 hours ago"
    posted_date: date | None
    salary: str
    url: str
    search: str  # title of the [[search]] that found it
    logo: str = ""
    early_applicant: bool = False
    likely_repost: bool = False
    applicants: str = ""  # e.g. "Over 200 applicants", when the job's page was checked
    maybe_not_coop: bool = False  # only matched thanks to a phrase like "Students Welcome"
    term: str = ""         # e.g. "Winter 2027" or "Term not stated", when filtering by term
    term_ok: bool = False  # the posting says it's in the wanted term
    duplicate_ids: list[str] = field(default_factory=list)    # same job listed again (other cities, reposts)
    other_locations: list[str] = field(default_factory=list)


@dataclass
class Report:
    jobs: list[Job]
    searches: list[dict]
    settings: dict
    failures: list[str]  # searches that got nothing
    partial: list[str] = field(default_factory=list)  # searches that stopped part-way
    checkin_days: float | None = None  # set when this is a "still checking" email after a quiet stretch
    failed_runs: tuple[int, int] = (0, 0)  # for check-ins: (runs that didn't finish, runs) since the last email
    unreadable_searches: list[str] = field(default_factory=list)  # results pages we couldn't read
    unreadable_descriptions: bool = False  # job pages loaded but none had a readable description

    def groups(self) -> list[tuple[dict, list[Job]]]:
        """Each search with its new jobs, in config order."""
        return [(s, [j for j in self.jobs if j.search == s["title"]]) for s in self.searches]


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- config

def load_env(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():  # -sig: skip a Windows byte order mark
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value[:1] in ("'", '"'):
            value = value[1:].split(value[0], 1)[0]
        else:
            value = re.split(r"(?:^|\s)#", value, maxsplit=1)[0].strip()  # drop inline comments
        os.environ.setdefault(key.strip(), value)


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"{path} not found. Copy config.example.toml to config.toml and add your searches.")
    config = tomllib.loads(path.read_text(encoding="utf-8-sig"))  # -sig: skip a Windows byte order mark
    if not config.get("search"):
        sys.exit(f"{path}: add at least one [[search]] block")
    for search in config["search"]:
        if not search.get("title"):
            sys.exit(f"{path}: every [[search]] needs a title")
    return config


def as_list(value) -> list:
    """Settings that take a list also accept a single value: also = "AI Co-op" works like ["AI Co-op"]."""
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def synonym_groups(settings: dict) -> list[list[str]]:
    """`synonyms` is a list of groups; a flat list of words counts as one group."""
    groups = as_list(settings.get("synonyms"))
    if groups and all(isinstance(g, str) for g in groups):
        groups = [groups]
    return [as_list(g) for g in groups]


def option(search: dict, settings: dict, key: str, default):
    """Per-search value, falling back to [settings], then the default."""
    return search.get(key, settings.get(key, default))


def parse_window(value: str | int | float, name: str = "posted_within") -> timedelta:
    """'24h', '3d', '1w', or a bare number of hours."""
    if isinstance(value, (int, float)):
        return timedelta(hours=value)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([hdw])\s*", str(value).lower())
    if not m:
        sys.exit(f"{name} must look like '24h', '3d' or '1w', not {value!r}")
    amount, unit = float(m[1]), m[2]
    return timedelta(hours=amount * {"h": 1, "d": 24, "w": 168}[unit])


def build_params(search: dict, window: timedelta, keywords: str) -> dict:
    params = {
        "keywords": keywords,
        "location": search.get("location", ""),
        "f_TPR": f"r{int(window.total_seconds())}",
        # Most relevant first. f_TPR already limits results to the time window, and relevance
        # puts title matches ahead of jobs that only mention the words in their description.
        "sortBy": "R",
    }
    for key, codes, param in (
        ("experience", EXPERIENCE, "f_E"),
        ("job_type", JOB_TYPE, "f_JT"),
        ("workplace", WORKPLACE, "f_WT"),
    ):
        values = as_list(search.get(key))
        unknown = [v for v in values if v not in codes]
        if unknown:
            sys.exit(f"Unknown {key} {unknown} in search {search['title']!r}; choose from {list(codes)}")
        if values:
            params[param] = ",".join(codes[v] for v in values)
    return {k: v for k, v in params.items() if v}


# -------------------------------------------------------------------- title matching

# Words that mean the same thing in a job title. With similar_titles on, a search for
# "Software Engineer Co-op" also finds "Software Developer Intern", "SWE Internship", etc.
# The first four words of each group are also sent to LinkedIn as OR alternatives, so the
# common ones go first. Add your own groups with `synonyms` in config.toml.
SYNONYM_GROUPS = [
    ["intern", "internship", "co-op", "coop", "co op", "student", "placement", "stagiaire", "étudiant", "étudiante"],
    ["engineer", "developer", "engineering", "programmer", "dev", "développeur", "développeuse"],
    ["junior", "entry level", "new grad", "jr", "entry-level", "early career", "graduate", "new graduate"],
    ["senior", "sr"],
    ["frontend", "front end", "ui"],
    ["backend", "back end"],
    ["full stack", "fullstack"],
    ["machine learning", "ml", "apprentissage automatique"],
    ["artificial intelligence", "ai", "intelligence artificielle"],
    ["analyst", "analytics"],
    ["scientist", "science"],
    ["qa", "quality assurance", "test", "tester", "testing"],
    ["site reliability", "sre"],
    ["manager", "mgr"],
]
QUERY_TERMS = 4
# Abbreviations that stand for several title words at once.
EXPANSIONS = {"swe": "software engineer", "sde": "software engineer", "sdet": "software engineer test",
              "software development": "software engineer"}
# Title words that imply another: a "Backend Developer Co-op" is a software job without saying so.
IMPLIED = {"software": {"developer", "programmer", "frontend", "backend", "fullstack", "web", "mobile",
                        "ios", "android", "développeur", "développeuse", "logiciel"}}
STOPWORDS = {"a", "an", "and", "at", "for", "in", "of", "on", "or", "the", "to", "with"}
# Letters joined by "." or "&" stay one word: "node.js", and "AI&T" (assembly, integration and test)
# or "R&D" aren't read as "AI" or "R".
TOKEN_RE = re.compile(r"\.?[^\W_][\w+#]*(?:[.&]\w+)*")
# Phrases that only say who can apply, like "Students Welcome" or "Open to Students". A title can mention
# students this way without the job being a co-op or internship (say, a part-time gig), so a job that only
# matches a co-op search because of one is kept but labelled, rather than dropped.
WHO_CAN_APPLY_RE = re.compile(
    r"(?<!\w)(?:students?|interns?|co-?ops?|new grads?|graduates?)\s+(?:are\s+)?"
    r"(?:welcome|encouraged(?:\s+to\s+apply)?|eligible|preferred|wanted|may apply|can apply)(?!\w)"
    r"|(?<!\w)open\s+to\s+(?:students|new grads|graduates)(?!\w)", re.I)


def singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def alternation(words) -> re.Pattern:
    """Matches any of `words` as whole space-separated tokens, longest first."""
    words = sorted(words, key=len, reverse=True)
    return re.compile(r"(?<!\S)(" + "|".join(map(re.escape, words)) + r")(?!\S)") if words else re.compile(r"(?!)")


class TitleMatcher:
    """Reduces titles to sets of comparable words, optionally folding synonyms together."""

    def __init__(self, similar: bool = True, extra_groups: list[list[str]] = ()):
        self.similar = similar
        self.group_of: dict[str, str] = {}           # normalized word/phrase -> group key
        self.query_terms: dict[str, list[str]] = {}  # group key -> spellings to OR together for LinkedIn
        if similar:
            for builtin, group in [(True, g) for g in SYNONYM_GROUPS] + [(False, g) for g in extra_groups]:
                forms = [self._norm(w) for w in group]
                key = next((self.group_of[f] for f in forms if f in self.group_of), forms[0].replace(" ", ""))
                self.group_of.update((f, key) for f in forms)
                self.group_of[key] = key
                terms = self.query_terms.setdefault(key, [])
                terms += [w for w in (group[:QUERY_TERMS] if builtin else group) if w not in terms]
        self.expansion_re = alternation(EXPANSIONS if similar else [])
        self.phrase_re = alternation(f for f in self.group_of if " " in f)

    @staticmethod
    def _norm(text: str) -> str:
        return " ".join(TOKEN_RE.findall(re.sub(r"(?<=\w)-(?=\w)", "", text.lower())))

    def _tokens(self, text: str, split_hyphens: bool = False) -> list[str]:
        text = re.sub(r"(?<=\w)-(?=\w)", " " if split_hyphens else "", text.lower())  # co-op -> coop
        text = " ".join(TOKEN_RE.findall(text))
        text = self.expansion_re.sub(lambda m: EXPANSIONS[m[1]], text)
        text = self.phrase_re.sub(lambda m: self.group_of[m[1]], text)
        return text.split()

    def _canon(self, token: str) -> str:
        return self.group_of.get(token) or self.group_of.get(singular(token)) or singular(token)

    def concepts(self, title: str) -> set[str]:
        """What a search title asks for, e.g. 'Software Engineer Co-op' -> {software, engineer, intern}."""
        return {c for c in map(self._canon, self._tokens(title)) if c not in STOPWORDS}

    def words(self, job_title: str) -> set[str]:
        """What a job title offers. Hyphenated words count both joined and split."""
        tokens = set(self._tokens(job_title)) | set(self._tokens(job_title, split_hyphens=True))
        found = {self._canon(t) for t in tokens}
        if self.similar:
            plain = {singular(t) for t in tokens}
            found |= {word for word, triggers in IMPLIED.items() if plain & triggers}
        return found

    def query(self, titles: list[str]) -> str:
        """LinkedIn keywords: each title word OR'd with its synonyms, titles OR'd together."""
        if not self.similar:
            return titles[0] if len(titles) == 1 else " OR ".join(f"({t})" for t in titles)
        parts = []
        for title in titles:
            terms = []
            for token in dict.fromkeys(self._tokens(title)):
                key = self._canon(token)
                if key in STOPWORDS:
                    continue
                alts = [f'"{w}"' if re.search(r"\W", w) else w for w in self.query_terms.get(key, [token])]
                terms.append(alts[0] if len(alts) == 1 else f"({' OR '.join(alts)})")
            parts.append(" ".join(terms))
        parts = list(dict.fromkeys(parts))  # "Software Engineer" and "Software Developer" become one query
        return parts[0] if len(parts) == 1 else " OR ".join(f"({p})" for p in parts)


# -------------------------------------------------------------------------- scraping

def fetch(session: requests.Session, url: str, params: dict | None = None) -> str:
    """GET a LinkedIn page, backing off when LinkedIn rate-limits (it does, often)."""
    problem = ""
    for attempt in range(5):
        if out_of_time():
            raise OutOfTime("ran out of time")
        try:
            resp = session.get(url, params=params, timeout=30)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code not in (429, 500, 502, 503, 504):
                resp.raise_for_status()
            problem = f"HTTP {resp.status_code}"
        except requests.ConnectionError as e:
            problem = f"connection error ({e.__class__.__name__})"
        except requests.Timeout:
            problem = "timeout"
        wait = 15 * 2**attempt + random.uniform(0, 5)
        if RUN_DEADLINE is not None and time.monotonic() + wait > RUN_DEADLINE:
            raise OutOfTime(f"ran out of time after {problem}")
        log(f"  LinkedIn: {problem}; retrying in {wait:.0f}s")
        time.sleep(wait)
    raise RuntimeError(f"gave up after repeated {problem}")


def text_of(card, selector: str) -> str:
    el = card.select_one(selector)
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""


def parse_jobs(html: str, search_title: str) -> list[Job]:
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    for card in soup.select('[data-entity-urn^="urn:li:jobPosting:"]'):
        job_id = card["data-entity-urn"].rsplit(":", 1)[-1]
        title = text_of(card, ".base-search-card__title")
        if not job_id.isdigit() or not title:
            continue
        time_el = card.select_one("time")
        logo_el = card.select_one("img")
        badges = [b.get_text(" ", strip=True).lower() for b in card.select(".job-posting-benefits__text")]
        posted_date = None
        if time_el and time_el.get("datetime"):
            try:
                posted_date = date.fromisoformat(time_el["datetime"])
            except ValueError:
                pass
        jobs.append(Job(
            id=job_id,
            title=title,
            company=text_of(card, ".base-search-card__subtitle"),
            location=text_of(card, ".job-search-card__location"),
            posted=text_of(card, "time"),
            posted_date=posted_date,
            salary=text_of(card, ".job-search-card__salary-info"),
            url=f"https://www.linkedin.com/jobs/view/{job_id}/",
            search=search_title,
            logo=(logo_el.get("data-delayed-url") or logo_el.get("src") or "") if logo_el else "",
            early_applicant=any("early applicant" in b for b in badges),
        ))
    return jobs


def looks_unreadable(html: str) -> bool:
    """A results page with content but no job cards we recognise. A search with no results comes back
    nearly empty, so this points to a layout change (or a sign-in page) instead."""
    return "<li" in html or len(html.strip()) > 2000


def merge_duplicates(jobs: list[Job]) -> list[Job]:
    """LinkedIn lists a job once per city, and again when it's reposted. Keep one of each."""
    merged: dict[tuple[str, str], Job] = {}
    for job in jobs:
        title = re.sub(r"[\s–-]*\(?\b(?:en|fr)\)?\s*$", "", job.title.lower())  # "...-EN" English copies
        first = merged.setdefault((title, job.company.lower()), job)
        if first is not job:
            first.duplicate_ids += [job.id, *job.duplicate_ids]
            for location in (job.location, *job.other_locations):
                if location not in (first.location, *first.other_locations):
                    first.other_locations.append(location)
    return list(merged.values())


def block_list(search: dict, settings: dict, *keys: str) -> list[str]:
    """Everything listed under these keys, in [settings] and in the search (both apply)."""
    return [item for source in (settings, search) for key in keys for item in as_list(source.get(key))]


def mark_reposts(jobs: list[Job], window: timedelta) -> None:
    """Flag jobs whose ID says they were first posted well before the time frame: for the past 24 hours,
    an ID more than 3 days older than the newest ones."""
    newest = NEWEST_JOB_ID["id"]
    limit = window.total_seconds() / 86400 + REPOST_MARGIN_DAYS
    for job in jobs:
        job.likely_repost = newest > 0 and (newest - int(job.id)) / JOB_IDS_PER_DAY > limit


def run_search(session: requests.Session, search: dict, settings: dict) -> tuple[list[Job], str | None]:
    """Jobs matching the search, and why it stopped early (None if it didn't).
    Raises if not even the first page could be loaded."""
    window = parse_window(option(search, settings, "posted_within", "24h"))
    max_pages = int(option(search, settings, "max_pages", 10))
    matcher = TitleMatcher(option(search, settings, "similar_titles", True), synonym_groups(settings))
    titles = [search["title"], *as_list(search.get("also"))]
    params = build_params(search, window, matcher.query(titles))
    # LinkedIn's keyword search also matches descriptions and fuzzes heavily, so by default
    # keep only jobs whose title covers every word of one of the wanted titles.
    must_match = option(search, settings, "title_must_match", True)
    # (a title with no real words, like "the", would otherwise match every job)
    wanted = [c for c in map(matcher.concepts, titles) if c]
    # "exclude" is the old name for block_titles and still works.
    unwanted = [matcher.concepts(w) for w in block_list(search, settings, "block_titles", "exclude")]
    # Company names match as whole words, ignoring case: "Amazon" also blocks "Amazon Web Services (AWS)".
    companies = [c for c in block_list(search, settings, "block_companies") if c.strip()]
    blocked_company = re.compile("|".join(rf"(?<!\w){re.escape(c.strip().lower())}(?!\w)" for c in companies)) \
        if companies else None
    # LinkedIn's time filter is reliable but promoted posts sometimes slip through;
    # its dates are UTC calendar days, so allow a day of slack.
    oldest = (datetime.now(timezone.utc) - window).date() - timedelta(days=1)

    found: dict[str, Job] = {}
    scanned = blocked = 0
    stopped = None
    for page in range(max_pages):
        if page:
            time.sleep(random.uniform(4, 8))
        try:
            html = fetch(session, SEARCH_URL, {**params, "start": page * PAGE_SIZE})
        except (RuntimeError, OutOfTime, requests.RequestException) as e:
            if not page:
                raise
            stopped = f"stopped after page {page} of {max_pages}: {e}"
            break
        jobs = parse_jobs(html, search["title"])
        if not jobs:
            if looks_unreadable(html):
                if not page:
                    raise UnreadablePage("LinkedIn sent a results page with no job listings the script could read")
                stopped = f"stopped after page {page} of {max_pages}: a results page couldn't be read"
            break
        scanned += len(jobs)
        NEWEST_JOB_ID["id"] = max(NEWEST_JOB_ID["id"], *(int(j.id) for j in jobs))
        for job in jobs:
            if job.posted_date and job.posted_date < oldest:
                continue
            words = matcher.words(job.title)
            if must_match and not any(w <= words for w in wanted):
                continue
            if must_match and WHO_CAN_APPLY_RE.search(job.title):
                without = matcher.words(WHO_CAN_APPLY_RE.sub(" ", job.title))
                job.maybe_not_coop = not any(w <= without for w in wanted)
            if any(u and u <= words for u in unwanted) or (blocked_company and blocked_company.search(job.company.lower())):
                blocked += 1
                continue
            found.setdefault(job.id, job)
    log(f"  scanned {scanned} listings, {len(found)} match the title filters"
        + (f" ({blocked} more were blocked)" if blocked else ""))
    if stopped:
        log(f"  {stopped}; keeping what was found")
    return list(found.values()), stopped


# ------------------------------------------------------------------------ work terms

# Co-op terms, Canadian style. With `term` set, a posting is kept if its title (or, when the
# title doesn't say, its description) says it starts in that term, and dropped if it names a
# different one.
TERM_START_MONTH = {"winter": 1, "summer": 5, "fall": 9}
SEASONS = {
    "winter": ["winter"], "hiver": ["winter"], "summer": ["summer"], "été": ["summer"],
    "fall": ["fall"], "autumn": ["fall"], "automne": ["fall"],
    # "Spring" is May-Aug at Canadian schools but Jan-May at most US ones, so it counts as both.
    "spring": ["winter", "summer"], "printemps": ["winter", "summer"],
}
MONTHS = {
    "january": 1, "jan": 1, "janvier": 1, "february": 2, "feb": 2, "février": 2, "march": 3, "mar": 3,
    "mars": 3, "april": 4, "apr": 4, "avril": 4, "may": 5, "mai": 5, "june": 6, "jun": 6, "juin": 6,
    "july": 7, "jul": 7, "juillet": 7, "august": 8, "aug": 8, "août": 8, "september": 9, "sept": 9,
    "sep": 9, "septembre": 9, "october": 10, "oct": 10, "octobre": 10, "november": 11, "nov": 11,
    "novembre": 11, "december": 12, "dec": 12, "décembre": 12,
}
AMBIGUOUS_MONTHS = {"may", "mai", "mar"}  # also ordinary words, so they need a day or year next to them

_SEASON = "|".join(sorted(SEASONS, key=len, reverse=True))
_SEASONS = rf"(?:{_SEASON})(?:\s*(?:/|,|&|\bor\b|\band\b|\bou\b|\bet\b)\s*(?:{_SEASON}))*"
_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
_YEAR = r"20\d\d|['’]\d\d"
_TERM_WORD = (r"(?:work[\s-]+)?(?:terms?|semesters?|sessions?|co-?ops?|internships?|interns?|students?|"
              r"placements?|cohorts?|stagiaires?|stages?)(?!\w)")
_DAY = r"(?:\s+(\d{1,2})(?!\d)(?:st|nd|rd|th|er)?)?,?"  # (?!\d): don't read the "20" of "2027" as a day
_START = r"(?:start\w*|begin\w*|commenc\w*|available|from|début\w*|à partir)"
_TO = r"(?:-|–|—|to|through|thru|until|till|à|au|jusqu['’]au)"

SEASON_RE = re.compile(  # "Winter 2027", "winter term", "Summer 16-week co-op", "Fall 2026 or Winter 2027"
    rf"(?<!\w)({_SEASONS})(?!\w)(?:[\s,]*({_YEAR})(?!\d))?"
    rf"(\s+(?:\d[\w-]*\s+|full-time\s+|paid\s+)?{_TERM_WORD})?(?:[\s,]*({_YEAR})(?!\d))?")
YEAR_SEASON_RE = re.compile(rf"(?<![\w/])({_YEAR})\s+({_SEASONS})(?!\w)")  # "2027 Winter Co-op"
FRENCH_SEASON_RE = re.compile(  # "stage d'été 2027", "session d'hiver"
    rf"(?<!\w)(?:stage|session|trimestre)s?\s+(?:d['’]|de\s+l['’]|de\s+)({_SEASON})(?!\w)(?:[\s,]*({_YEAR})(?!\d))?")
RANGE_RE = re.compile(  # "January to April 2027", "Jan-Apr 2027", "janvier à avril 2027"
    rf"(?<!\w)({_MONTH})(?!\w)\.?{_DAY}(?:\s+({_YEAR}))?\s*{_TO}\s*({_MONTH})(?!\w)\.?{_DAY}(?:\s+({_YEAR}))?")
START_RE = re.compile(  # "Start date: January 5, 2027", "starting in May 2027"
    rf"(?<!\w){_START}(?!\w)[^.\n]{{0,25}}?(?<!\w)({_MONTH})(?!\w)\.?{_DAY}(?:\s+({_YEAR}))?")
NUMERIC_START_RE = re.compile(rf"(?<!\w){_START}(?!\w)[^.\n\d]{{0,20}}(\d{{1,2}})[/.](\d{{1,2}})[/.](20\d\d)")
ISO_START_RE = re.compile(rf"(?<!\w){_START}(?!\w)[^.\n\d]{{0,20}}(20\d\d)-(\d{{1,2}})-(\d{{1,2}})")
TITLE_MONTH_RE = re.compile(rf"(?<!\w)({_MONTH})(?!\w)\.?\s+({_YEAR})")
TITLE_TERM_WORD_RE = re.compile(rf"(?<!\w){_TERM_WORD}")
# Dates right after these are about school, not the work term ("graduating in Fall 2027").
NOT_THE_TERM_RE = re.compile(r"graduat|diplôm|return\w*\s+to\s+(?:school|stud|class|univ)|back to school|"
                             r"retour aux études|complete your degree")


def month_term(month: int) -> str:
    return "winter" if month <= 4 else "summer" if month <= 8 else "fall"


def resolve_year(month: int, year: str | None, today: date) -> int:
    """An explicit year, or the next time `month` comes around."""
    if year:
        y = int(year.strip("'’"))
        return y + 2000 if y < 100 else y
    return today.year if month >= today.month else today.year + 1


def find_terms(text: str, today: date, in_title: bool = False) -> set[tuple[str, int]]:
    """Work terms a piece of text says a job starts in, e.g. {("winter", 2027)}."""
    text = text.lower()
    found: set[tuple[str, int]] = set()

    def about_school(m: re.Match) -> bool:
        return NOT_THE_TERM_RE.search(text[max(0, m.start() - 50):m.start()]) is not None

    def add_seasons(words: str, year: str | None) -> None:
        for word in re.findall(_SEASON, words):
            for season in SEASONS[word]:
                found.add((season, resolve_year(TERM_START_MONTH[season], year, today)))

    def add_month(month: int, year: str | None) -> None:
        found.add((month_term(month), resolve_year(month, year, today)))

    # A bare season only counts with a year or a word like "term" or "co-op" next to it, so
    # "a terrace open in the summer and fall" doesn't. Titles get more benefit of the doubt.
    loose_title = in_title and TITLE_TERM_WORD_RE.search(text)
    for m in SEASON_RE.finditer(text):
        year = m[2] or m[4]
        if about_school(m) or not (year or m[3] or (loose_title and m[1] != "été")):
            continue
        add_seasons(m[1], year)
    for m in YEAR_SEASON_RE.finditer(text):
        if not about_school(m):
            add_seasons(m[2], m[1])
    for m in FRENCH_SEASON_RE.finditer(text):
        if not about_school(m):
            add_seasons(m[1], m[2])

    for m in RANGE_RE.finditer(text):
        if about_school(m):
            continue
        start, end = MONTHS[m[1]], MONTHS[m[4]]
        year = m[3]
        if not year and m[6]:  # "September to April 2027" starts in 2026
            end_year = resolve_year(end, m[6], today)
            year = str(end_year if start <= end else end_year - 1)
        add_month(start, year)
    for m in START_RE.finditer(text):
        if about_school(m) or (m[1] in AMBIGUOUS_MONTHS and not (m[2] or m[3])):
            continue
        add_month(MONTHS[m[1]], m[3])
    for m in NUMERIC_START_RE.finditer(text):  # day/month order varies, so take the reading that's upcoming
        a, b, year = int(m[1]), int(m[2]), int(m[3])
        readings = sorted(d for d in (safe_date(year, a, b), safe_date(year, b, a))
                          if d and d >= today - timedelta(days=31))
        if readings:
            add_month(readings[0].month, str(readings[0].year))
    for m in ISO_START_RE.finditer(text):
        if safe_date(int(m[1]), int(m[2]), int(m[3])):
            add_month(int(m[2]), m[1])
    if in_title:
        for m in TITLE_MONTH_RE.finditer(text):
            if not (m[1] in AMBIGUOUS_MONTHS and m[2].startswith("'")):
                add_month(MONTHS[m[1]], m[2])
    return found


def safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_terms(value) -> set[tuple[str, int]]:
    """config `term`: "Winter 2027", or a list of them."""
    if not value:
        return set()
    terms = set()
    for v in [value] if isinstance(value, str) else value:
        m = re.fullmatch(r"\s*(winter|spring|summer|fall|autumn)\s+(20\d\d)\s*", str(v).lower())
        if not m:
            sys.exit(f"term must look like 'Winter 2027', 'Summer 2027' or 'Fall 2027', not {v!r}")
        terms.add(({"spring": "summer", "autumn": "fall"}.get(m[1], m[1]), int(m[2])))
    return terms


def term_label(term: tuple[str, int]) -> str:
    return f"{term[0].capitalize()} {term[1]}"


JOB_PAGES: dict[str, BeautifulSoup | None] = {}  # job pages loaded this run, so none is loaded twice


def fetch_job_page(session: requests.Session, job_id: str) -> BeautifulSoup | None:
    """A job's own page (description, applicant count), or None if it couldn't be loaded."""
    if job_id not in JOB_PAGES:
        time.sleep(random.uniform(3, 6))
        try:
            JOB_PAGES[job_id] = BeautifulSoup(fetch(session, JOB_URL.format(job_id)), "html.parser")
        except OutOfTime:
            raise
        except Exception as e:
            log(f"  couldn't load the page of job {job_id}: {e}")
            JOB_PAGES[job_id] = None
    return JOB_PAGES[job_id]


def fetch_description(session: requests.Session, job_id: str) -> str | None:
    page = fetch_job_page(session, job_id)
    if page is None:
        return None
    el = page.select_one(".show-more-less-html__markup, .description__text")
    DESCRIPTION_PAGES["read" if el else "unreadable"] += 1
    return el.get_text(" ", strip=True) if el else ""


def parse_applicants(text: str) -> tuple[int, str] | None:
    """LinkedIn's applicant caption as (a number to compare, a short label), e.g.
    "Over 200 applicants" -> (201, "Over 200 applicants"), "Be among the first 25 applicants" ->
    (24, "Under 25 applicants"), "114 applicants" -> (114, "114 applicants")."""
    t = text.lower().replace(",", "")
    if m := re.search(r"over (\d+)", t):
        return int(m[1]) + 1, f"Over {m[1]} applicants"
    if m := re.search(r"first (\d+)", t):
        return int(m[1]) - 1, f"Under {m[1]} applicants"
    if m := re.search(r"(\d+) applicant", t):
        return int(m[1]), f"{m[1]} applicant{'' if m[1] == '1' else 's'}"
    return None


def applicant_count(session: requests.Session, job: Job) -> int | None:
    """How many people have applied, from the job's page (None if it can't be read)."""
    page = fetch_job_page(session, job.id)
    el = page.select_one(".num-applicants__caption") if page else None
    parsed = parse_applicants(el.get_text(" ", strip=True)) if el else None
    if parsed is None:
        return None
    job.applicants = parsed[1]
    return parsed[0]


def filter_reposts(session: requests.Session, jobs: list[Job], min_applicants: int) -> tuple[list[Job], list[Job], int]:
    """Split off likely reposts that already have at least `min_applicants` applicants, checked on the
    job's page (0 = skip every likely repost without checking). Likely reposts with fewer, or whose
    count can't be read, are kept and labelled: often they were drafted days before going up, and are
    as fresh as any other posting. The third value counts likely reposts left for the next run because
    the run ran out of time."""
    kept, skipped, unchecked = [], [], 0
    for job in jobs:
        if not job.likely_repost:
            kept.append(job)
        elif min_applicants <= 0:
            skipped.append(job)
        elif unchecked:
            unchecked += 1
        else:
            try:
                count = applicant_count(session, job)
            except OutOfTime:
                unchecked += 1
                continue
            (skipped if count is not None and count >= min_applicants else kept).append(job)
    reposts_kept = sum(j.likely_repost for j in kept)
    if skipped or reposts_kept or unchecked:
        log(f"  likely reposts: {len(skipped)} skipped, {reposts_kept} kept"
            + (f" (fewer than {min_applicants} applicants)" if min_applicants > 0 else "")
            + (f", {unchecked} left for the next run (out of time)" if unchecked else ""))
    return kept, skipped, unchecked


def filter_by_term(session: requests.Session, jobs: list[Job], wanted: set[tuple[str, int]],
                   require: bool) -> tuple[list[Job], list[Job], int]:
    """Split jobs into (kept, dropped) by work term, reading descriptions only when titles don't say.
    The third value counts jobs left unchecked because the run ran out of time; they're in neither
    list, so they aren't sent or remembered and the next run checks them."""
    today = date.today()
    kept, dropped, unstated, unchecked = [], [], 0, 0
    for job in jobs:
        terms = find_terms(job.title, today, in_title=True)
        if not terms:
            if unchecked:
                unchecked += 1
                continue
            try:
                terms = find_terms(fetch_description(session, job.id) or "", today)
            except OutOfTime:
                unchecked += 1
                continue
        if terms & wanted:
            job.term, job.term_ok = term_label(min(terms & wanted)), True
            kept.append(job)
        elif terms or require:
            dropped.append(job)
        else:
            job.term = "Term not stated"
            kept.append(job)
            unstated += 1
    label = " or ".join(term_label(t) for t in sorted(wanted))
    log(f"  {len(kept) - unstated} for {label}, {unstated} don't say when, {len(dropped)} dropped"
        + (f", {unchecked} left for the next run (out of time)" if unchecked else ""))
    return kept, dropped, unchecked


# ---------------------------------------------------------------------------- state

def read_json(path: Path, what: str) -> dict:
    """A saved state file, or {} if it's missing or damaged. A damaged file (cut off mid-write, or
    edited by hand) shouldn't stop every future run."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        problem = "it isn't a JSON object"
    except ValueError as e:
        problem = str(e)
    log(f"{path.name} couldn't be read ({problem}); starting over with a fresh {what}")
    return {}


def load_seen() -> dict[str, str]:
    seen = read_json(SEEN_FILE, "list of sent jobs")
    cutoff = (date.today() - timedelta(days=SEEN_RETENTION_DAYS)).isoformat()
    return {job_id: day for job_id, day in seen.items() if isinstance(day, str) and day >= cutoff}


def save_seen(seen: dict[str, str]) -> None:
    tmp = SEEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen, indent=0, sort_keys=True), encoding="utf-8")
    tmp.replace(SEEN_FILE)


def load_state() -> dict:
    return read_json(STATE_FILE, "check-in history")


def state_number(state: dict, key: str) -> int:
    """A count or job ID from state.json, or 0 if it's missing or isn't a number (say, after a hand edit)."""
    try:
        return int(state.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def state_time(state: dict, key: str, default: datetime) -> datetime:
    """A time from state.json in local time, or `default` if it's missing or isn't a time."""
    try:
        when = datetime.fromisoformat(state[key])
    except (KeyError, TypeError, ValueError):
        return default
    return when.astimezone().replace(tzinfo=None) if when.tzinfo else when


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# -------------------------------------------------------------------------- output

# Email palette (light). Dark-mode overrides live in EMAIL_CSS, keyed by class name.
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
PAGE, CARD, RULE = "#f4f2ee", "#ffffff", "#ebe9e4"
INK, MUTED, FAINT, ACCENT = "#1b1b1f", "#5f5f66", "#8b8b93", "#0a66c2"
AVATAR_COLORS = [("#e7eefa", "#2856a3"), ("#e6f3ea", "#22703f"), ("#f8ece2", "#9a4f17"),
                 ("#efe8f8", "#5e3c99"), ("#f9e7ea", "#a3304a"), ("#e3f2f3", "#1f6b70")]
EMAIL_CSS = """
body { margin:0; padding:0; -webkit-text-size-adjust:100%; }
a { text-decoration:none; }
@media (max-width:620px) {
  .pad { padding-left:20px !important; padding-right:20px !important; }
  .h1 { font-size:24px !important; line-height:30px !important; }
}
@media (prefers-color-scheme: dark) {
  .page { background:#111113 !important; }
  .card { background:#1c1c1f !important; border-color:#2b2b30 !important; }
  .rule { border-color:#2b2b30 !important; }
  .ink { color:#f1f1f3 !important; }
  .muted { color:#b3b3ba !important; }
  .faint { color:#85858d !important; }
  .accent { color:#70b5f9 !important; }
  .pill { background:#2a2a2f !important; color:#d4d4da !important; }
  .pill-good { background:#173b25 !important; color:#82dca3 !important; }
  .warn { background:#3a2c12 !important; border-color:#5c4519 !important; color:#f3cd84 !important; }
  .logo { border-color:#2b2b30 !important; }
}
"""
LABELS = {"entry": "Entry level", "mid-senior": "Mid-senior", "on-site": "On-site",
          "full-time": "Full-time", "part-time": "Part-time"}


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else many or word + 's'}"


def describe_window(window: timedelta) -> str:
    hours = window.total_seconds() / 3600
    for unit, size in (("week", 168), ("day", 24)):
        if hours % size == 0 and (hours > 24 or size == 168):
            n = int(hours // size)
            return f"past {unit}" if n == 1 else f"past {n} {unit}s"
    return f"past {hours:g} hour{'' if hours == 1 else 's'}"


def describe_search(search: dict, settings: dict) -> str:
    """e.g. 'Canada · Entry level · Remote · past 24 hours'"""
    parts = [search.get("location") or "Any location"]
    for key in ("experience", "job_type", "workplace"):
        if values := as_list(search.get(key)):
            parts.append(", ".join(LABELS.get(v, v.capitalize()) for v in values))
    parts.append(describe_window(parse_window(option(search, settings, "posted_within", "24h"))))
    if also := as_list(search.get("also")):
        parts.append("also " + ", ".join(also))
    if option(search, settings, "similar_titles", True):
        parts.append("similar titles")
    if not option(search, settings, "include_reposts", True):
        need = int(option(search, settings, "repost_applicants", 100))
        parts.append(f"no reposts with {need}+ applicants" if need > 0 else "no reposts")
    if wanted := parse_terms(option(search, settings, "term", None)):
        parts.append(" or ".join(term_label(t) for t in sorted(wanted)) + " only")
    return " · ".join(parts)


def days(n: float) -> str:
    return f"{n:g} day{'' if n == 1 else 's'}"


def checkin_note(r: Report) -> str:
    note = (f"Nothing new has matched your searches in the last {days(r.checkin_days)}. This is a check-in so "
            "you know the alerts are still running; if even these stop arriving, look at logs/job_alerts.log.")
    failed, runs = r.failed_runs
    if failed:
        note += (f" {failed} of the {plural(runs, 'run')} in that time couldn't finish all their searches, usually "
                 "because LinkedIn was rate-limiting, so some postings may have been missed.")
    return note


def layout_note(r: Report) -> str:
    what = []
    if r.unreadable_searches:
        what.append(f"the search results for {', '.join(r.unreadable_searches)}")
    if r.unreadable_descriptions:
        what.append("the job descriptions used for the term check")
    return (f"LinkedIn sent pages the script couldn't read: {' and '.join(what)}. Either LinkedIn changed its "
            "page layout or it showed a sign-in page instead of results. If this keeps happening, the parsing in "
            "job_alerts.py (parse_jobs and fetch_description) needs updating, and jobs will be missing until then.")


def nothing_ran_ok(r: Report) -> bool:
    """No jobs, and that's because searches failed or couldn't be read, not because nothing matched."""
    return not r.jobs and not r.checkin_days and bool(r.failures or r.unreadable_searches)


def subject(r: Report) -> str:
    if not r.jobs:
        if r.unreadable_searches or r.unreadable_descriptions:
            return "LinkedIn job alerts: couldn't read LinkedIn's pages"
        if r.checkin_days:
            return f"Still checking: no new LinkedIn jobs in {days(r.checkin_days)}"
        if r.failures:
            return "LinkedIn job alerts: search failed"
        return "No new LinkedIn jobs today"
    counts = ", ".join(f"{s['title']} ({len(g)})" for s, g in r.groups() if g)
    return f"{plural(len(r.jobs), 'new LinkedIn job')}: {counts}"


def preheader(r: Report) -> str:
    """The preview line inbox lists show under the subject."""
    companies = list(dict.fromkeys(j.company for j in r.jobs if j.company))
    if not companies:
        return ""
    more = f" and {len(companies) - 3} more" if len(companies) > 3 else ""
    return ", ".join(companies[:3]) + more


def failure_note(r: Report) -> str:
    problems = []
    if r.failures:
        problems.append(f"couldn't search {', '.join(r.failures)}")
    if r.partial:
        problems.append(f"only got partway through {', '.join(r.partial)}")
    return (f"This run {' and '.join(problems)}, usually because LinkedIn was rate-limiting. "
            "Anything missed will show up in the next run.")


def render_text(r: Report) -> str:
    lines = []
    for search, group in r.groups():
        if not group:
            continue
        lines += [f"== {search['title']} ({len(group)}) ==", ""]
        for j in group:
            lines.append(j.title)
            lines.append("  " + " · ".join(filter(None, [j.company, where(j), j.posted, j.term, j.salary,
                                                          "may not be a co-op or internship" if j.maybe_not_coop else "",
                                                          "likely repost" if j.likely_repost else "",
                                                          j.applicants])))
            lines += [f"  {j.url}", ""]
    if not r.jobs and not nothing_ran_ok(r):
        lines.append(checkin_note(r) if r.checkin_days else "No new postings matched your searches.")
    if r.unreadable_searches or r.unreadable_descriptions:
        lines += ["", layout_note(r)]
    if r.failures or r.partial:
        lines += ["", failure_note(r)]
    return "\n".join(lines).rstrip() + "\n"


def where(job: Job) -> str:
    n = len(job.other_locations)
    return job.location + (f" (+{n} more location{'s' if n > 1 else ''})" if n else "")


def logo_html(job: Job) -> str:
    box = "display:block;width:44px;height:44px;border-radius:10px;"
    initial = escape((job.company or "?")[0].upper())
    if job.logo:
        return (f'<img class="logo" src="{escape(job.logo)}" width="44" height="44" alt="{initial}" '
                f'style="{box}border:1px solid {RULE};background:#fff">')
    bg, fg = AVATAR_COLORS[sum(map(ord, job.company)) % len(AVATAR_COLORS)]
    return (f'<div style="{box}background:{bg};color:{fg};font-size:18px;font-weight:700;'
            f'line-height:44px;text-align:center">{initial}</div>')


def pill_html(text: str, good: bool = False) -> str:
    cls, bg, fg = ("pill-good", "#e5f4ea", "#1d7a3e") if good else ("pill", "#f1f0ec", MUTED)
    return (f'<span class="{cls}" style="display:inline-block;margin:8px 6px 0 0;padding:3px 9px;'
            f'border-radius:999px;background:{bg};color:{fg};font-size:12px;font-weight:600;'
            f'line-height:16px">{escape(text)}</span>')


def job_html(job: Job) -> str:
    place = " · ".join(filter(None, [where(job), job.posted]))
    pills = (pill_html(job.term, good=job.term_ok) if job.term else "") + \
            (pill_html("Early applicant", good=True) if job.early_applicant else "") + \
            (pill_html("May not be a co-op or internship") if job.maybe_not_coop else "") + \
            (pill_html("Likely repost") if job.likely_repost else "") + \
            (pill_html(job.applicants) if job.applicants else "") + \
            (pill_html(job.salary) if job.salary else "")
    return (
        f'<tr><td class="rule" style="padding:16px 0;border-top:1px solid {RULE}">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td width="46" valign="top" style="width:46px;padding-right:14px">{logo_html(job)}</td>'
        f'<td valign="top">'
        f'<a class="accent" href="{escape(job.url)}" style="color:{ACCENT};font-size:15px;font-weight:600;'
        f'line-height:21px">{escape(job.title)}</a>'
        f'<div class="ink" style="color:{INK};font-size:14px;line-height:20px;margin-top:2px">{escape(job.company)}</div>'
        f'<div class="faint" style="color:{FAINT};font-size:13px;line-height:18px;margin-top:1px">{escape(place)}</div>'
        f'{pills}</td></tr></table></td></tr>'
    )


def section_html(search: dict, jobs: list[Job], settings: dict) -> str:
    return (
        f'<tr><td class="pad" style="padding:28px 32px 0">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td style="padding-bottom:14px">'
        f'<span class="ink" style="color:{INK};font-size:17px;font-weight:700;line-height:24px">{escape(search["title"])}</span>'
        f'&nbsp;{pill_html(str(len(jobs))).replace("margin:8px 6px 0 0", "margin:0 0 0 4px;vertical-align:2px")}'
        f'<div class="faint" style="color:{FAINT};font-size:13px;line-height:18px;margin-top:3px">'
        f'{escape(describe_search(search, settings))}</div>'
        f'</td></tr>{"".join(job_html(j) for j in jobs)}</table></td></tr>'
    )


def render_html(r: Report) -> str:
    n = len(r.jobs)
    groups = r.groups()
    if n:
        headline = plural(n, "new job")
    elif r.unreadable_searches:
        headline = "Couldn't read LinkedIn"
    elif r.checkin_days:
        headline = "Still checking"
    elif r.failures:
        headline = "Searches didn't finish"
    else:
        headline = "Nothing new today"
    sub = f"{datetime.now():%A, %B} {datetime.now().day} · {plural(len(r.searches), 'search', 'searches')}"
    body = [
        f'<tr><td class="pad rule" style="padding:32px 32px 22px;border-bottom:1px solid {RULE}">'
        f'<div class="accent" style="color:{ACCENT};font-size:12px;font-weight:700;letter-spacing:.08em;'
        f'text-transform:uppercase">LinkedIn job alerts</div>'
        f'<div class="ink h1" style="color:{INK};font-size:28px;font-weight:700;line-height:34px;margin-top:8px;'
        f'letter-spacing:-.01em">{headline}</div>'
        f'<div class="muted" style="color:{MUTED};font-size:14px;line-height:20px;margin-top:4px">{escape(sub)}</div>'
        f'</td></tr>'
    ]
    for note, show in ((layout_note, r.unreadable_searches or r.unreadable_descriptions),
                       (failure_note, r.failures or r.partial)):
        if not show:
            continue
        body.append(
            f'<tr><td class="pad" style="padding:20px 32px 0"><div class="warn" style="background:#fdf5e6;'
            f'border:1px solid #f0dcb4;border-radius:10px;padding:12px 14px;color:#7a4b06;font-size:13px;'
            f'line-height:19px">{escape(note(r))}</div></td></tr>'
        )
    body += [section_html(s, g, r.settings) for s, g in groups if g]
    quiet = [s["title"] for s, g in groups
             if not g and s["title"] not in r.failures + r.partial + r.unreadable_searches]
    if not n and not nothing_ran_ok(r):
        note = checkin_note(r) if r.checkin_days else "No new postings matched your searches since the last alert."
    elif quiet:
        note = f"Nothing new for {', '.join(quiet)}."
    else:
        note = ""
    if note:
        body.append(f'<tr><td class="pad muted" style="padding:24px 32px 0;color:{MUTED};font-size:14px;'
                    f'line-height:20px">{escape(note)}</td></tr>')
    body.append('<tr><td style="height:28px;line-height:28px;font-size:0">&nbsp;</td></tr>')

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark">'
        f'<title>{escape(subject(r))}</title><style>{EMAIL_CSS}</style></head>'
        f'<body class="page" style="margin:0;padding:0;background:{PAGE}">'
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0">{escape(preheader(r))}</div>'
        f'<table role="presentation" class="page" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{PAGE}"><tr><td align="center" style="padding:24px 12px 32px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="max-width:600px;font-family:{FONT}">'
        f'<tr><td class="card" style="background:{CARD};border:1px solid {RULE};border-radius:14px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{"".join(body)}</table>'
        f'</td></tr>'
        f'<tr><td class="faint" style="padding:20px 24px 0;text-align:center;color:{FAINT};font-size:12px;line-height:18px">'
        f'Sent by your LinkedIn job alerts script at {datetime.now().strftime("%I:%M %p").lstrip("0")}.<br>'
        f'Change what you get in config.toml.</td></tr>'
        f'</table></td></tr></table></body></html>'
    )


def send_email(r: Report) -> None:
    user = os.environ["SMTP_USER"]
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or 587)

    msg = EmailMessage()
    msg["Subject"] = subject(r)
    msg["From"] = os.environ.get("EMAIL_FROM") or user
    msg["To"] = os.environ.get("EMAIL_TO") or user
    msg.set_content(render_text(r))
    msg.add_alternative(render_html(r), subtype="html")

    smtp_cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
    with smtp_cls(host, port, timeout=30) as smtp:
        if port != 465:
            smtp.starttls()
        try:
            smtp.login(user, os.environ["SMTP_PASSWORD"])
        except smtplib.SMTPAuthenticationError as e:
            raise RuntimeError("SMTP login failed. For Gmail, SMTP_PASSWORD must be an app password "
                               "(myaccount.google.com/apppasswords), not your normal password.") from e
        smtp.send_message(msg)


def send_ntfy(r: Report) -> None:
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    lines = [f"• {j.title} — {j.company}" for j in r.jobs[:NTFY_MAX_LINES]]
    if len(r.jobs) > NTFY_MAX_LINES:
        lines.append(f"…and {len(r.jobs) - NTFY_MAX_LINES} more")
    if not r.jobs and not nothing_ran_ok(r):
        lines.append(checkin_note(r) if r.checkin_days else "No new postings matched your searches.")
    if r.unreadable_searches or r.unreadable_descriptions:
        lines.append(layout_note(r))
    if r.failures or r.partial:
        lines.append(failure_note(r))
    # Title/click go in query params rather than headers so non-ASCII text is safe.
    params = {"title": subject(r), "tags": "briefcase"}
    if len(r.jobs) == 1:
        params["click"] = r.jobs[0].url
    resp = requests.post(f"{server}/{os.environ['NTFY_TOPIC']}", params=params,
                         data="\n".join(lines).encode(), timeout=30)
    resp.raise_for_status()


def notify(r: Report) -> bool:
    """Send through every configured channel. True if at least one succeeded."""
    channels = []
    if os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"):
        channels.append(("email", send_email))
    if os.environ.get("NTFY_TOPIC"):
        channels.append(("ntfy", send_ntfy))
    if not channels:
        missing = "SMTP_PASSWORD is empty" if os.environ.get("SMTP_USER") else "no email or ntfy settings"
        log(f"Nothing sent ({missing} in .env). Found:")
        print(render_text(r), flush=True)
        return False

    delivered = False
    for name, send in channels:
        try:
            send(r)
            log(f"Sent {name} notification")
            delivered = True
        except Exception as e:
            log(f"{name} notification failed: {e}")
    return delivered


def write_preview(r: Report) -> None:
    path = ROOT / "preview.html"
    path.write_text(render_html(r), encoding="utf-8")
    log(f"Wrote {path}")
    webbrowser.open(path.as_uri())


# ----------------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    parser.add_argument("--dry-run", action="store_true", help="print results; don't notify or update seen_jobs.json")
    parser.add_argument("--preview", action="store_true",
                        help="like --dry-run, but open the email in your browser instead of printing it")
    parser.add_argument("--test-notify", action="store_true", help="send a sample alert without searching")
    # Short by default: the point is spotting jobs early, and older postings are less worth applying to.
    parser.add_argument("--log", metavar="FILE",
                        help="append output to this file (relative paths are from the project folder), for "
                             "schedulers that don't save output themselves, like Windows Task Scheduler")
    parser.add_argument("--lookback", metavar="WINDOW", nargs="?", const="48h",
                        help="one-off catch-up: search this far back instead of posted_within (48h if no window is given)")
    args = parser.parse_args()

    # Job titles and company names can be in any language. Windows consoles and log files default to
    # a narrower encoding than UTF-8, so printing one could crash the run.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args.log:
        log_file = ROOT / args.log
        log_file.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = log_file.open("a", encoding="utf-8", errors="replace", buffering=1)

    load_env(ROOT / ".env")

    if args.test_notify:
        sample = Job(id="0", title="Software Engineer (sample alert)", company="Example Co", location="Remote",
                     posted="1 hour ago", posted_date=None, salary="$120K/yr - $150K/yr",
                     url="https://www.linkedin.com/jobs/", search="Test alert", early_applicant=True,
                     term="Winter 2027", term_ok=True)
        return 0 if notify(Report([sample], [{"title": "Test alert"}], {}, [])) else 1

    config = load_config(args.config)
    settings = config.get("settings", {})
    searches = config["search"]
    seen = load_seen()
    state = load_state()
    NEWEST_JOB_ID["id"] = state_number(state, "newest_job_id")
    if args.lookback:
        window = parse_window(args.lookback, "--lookback")
        settings = {**settings, "posted_within": args.lookback}
        searches = [{**s, "posted_within": args.lookback} for s in searches]
        log(f"Catch-up run over the {describe_window(window)}")
    # Catch-up runs aren't time-limited: whatever they didn't get to would fall outside the next
    # normal run's window and never be checked.
    global RUN_DEADLINE
    RUN_DEADLINE = None if args.lookback else time.monotonic() + float(settings.get("max_run_minutes", 30)) * 60

    session = requests.Session()
    session.headers.update(HEADERS)
    DESCRIPTION_PAGES.update(read=0, unreadable=0)
    JOB_PAGES.clear()
    unreadable = []
    new: dict[str, Job] = {}
    skipped: set[str] = set()  # wrong term; remembered so their descriptions aren't fetched again
    failures, partial = [], []
    for i, search in enumerate(searches):
        if out_of_time():
            log(f"Out of time; {search['title']!r} waits for the next run")
            failures.append(search["title"])
            continue
        if i:
            time.sleep(random.uniform(5, 10))
        log(f"Searching {search['title']!r} in {search.get('location') or 'any location'}")
        try:
            jobs, stopped = run_search(session, search, settings)
        except UnreadablePage as e:
            log(f"  {e}; LinkedIn may have changed its page layout")
            unreadable.append(search["title"])
            continue
        except Exception as e:
            log(f"  search failed: {e}")
            failures.append(search["title"])
            continue
        if stopped:
            partial.append(search["title"])
        fresh = merge_duplicates([j for j in jobs if j.id not in seen and j.id not in new and j.id not in skipped])
        mark_reposts(fresh, parse_window(option(search, settings, "posted_within", "24h")))
        if not option(search, settings, "include_reposts", True):
            fresh, reposts, unchecked_reposts = filter_reposts(
                session, fresh, int(option(search, settings, "repost_applicants", 100)))
            skipped.update(job_id for j in reposts for job_id in (j.id, *j.duplicate_ids))
            if unchecked_reposts and search["title"] not in partial:
                partial.append(search["title"])
        wanted_terms = parse_terms(option(search, settings, "term", None))
        if wanted_terms and fresh:
            try:
                fresh, dropped, unchecked = filter_by_term(session, fresh, wanted_terms,
                                                           option(search, settings, "require_term", False))
            except Exception as e:
                log(f"  term check failed: {e}")
                failures.append(search["title"])
                continue
            skipped.update(job_id for j in dropped for job_id in (j.id, *j.duplicate_ids))
            if unchecked and search["title"] not in partial:
                partial.append(search["title"])
        log(f"  {len(fresh)} new since last run")
        new.update((j.id, j) for j in fresh)

    unreadable_descriptions = DESCRIPTION_PAGES["read"] == 0 and DESCRIPTION_PAGES["unreadable"] >= 3
    if unreadable_descriptions:
        log(f"None of the {DESCRIPTION_PAGES['unreadable']} job descriptions loaded could be read; "
            "LinkedIn may have changed its page layout")
    report = Report(merge_duplicates(list(new.values())), searches, settings, failures, partial,
                    unreadable_searches=unreadable, unreadable_descriptions=unreadable_descriptions)
    if args.preview:
        write_preview(report)
        return 0
    if args.dry_run:
        print("\n" + render_text(report), flush=True)
        return 0

    state["newest_job_id"] = NEWEST_JOB_ID["id"]
    now = datetime.now()
    last_alert = state_time(state, "last_alert", now)  # first run: start counting from now
    checkin_days = float(settings.get("checkin_days", 3))
    run_failed = bool(failures or partial)
    state["runs_since_alert"] = state_number(state, "runs_since_alert") + 1
    state["failed_runs_since_alert"] = state_number(state, "failed_runs_since_alert") + run_failed
    # Searches that didn't finish don't cause an email on their own: the next run's time frame overlaps
    # this one, so it picks up whatever was missed. They're mentioned when an email goes out anyway,
    # and the check-in email says if runs keep failing.
    sending = bool(report.jobs or unreadable or unreadable_descriptions or settings.get("send_when_empty", False))
    if not sending and checkin_days and now - last_alert >= timedelta(days=checkin_days):
        report.checkin_days = checkin_days
        report.failed_runs = (state["failed_runs_since_alert"], state["runs_since_alert"])
        sending = True
    if sending:
        if not notify(report):
            log("No notification went through; not marking jobs as seen so they're retried next run")
            return 1
        last_alert = now
        state.update(runs_since_alert=0, failed_runs_since_alert=0)
    elif run_failed:
        log("Nothing new; some searches didn't finish, so the next run will pick them up (no email for that alone)")
    else:
        log("Nothing new; no notification sent")
    state["last_alert"] = last_alert.isoformat(timespec="seconds")
    save_state(state)

    today = date.today().isoformat()
    seen.update((i, today) for j in report.jobs for i in (j.id, *j.duplicate_ids))
    seen.update((job_id, today) for job_id in skipped)
    save_seen(seen)
    return 1 if len(failures) + len(unreadable) == len(searches) else 0


if __name__ == "__main__":
    sys.exit(main())
