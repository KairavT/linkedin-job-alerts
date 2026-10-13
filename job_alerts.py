#!/usr/bin/env python3
"""LinkedIn job alerts.

Searches LinkedIn's public (logged-out) job listings for each [[search]] in
config.toml, keeps postings from the configured time window that haven't been
sent before, and delivers them by email and/or ntfy push notification.

    python job_alerts.py                # search, notify, remember what was sent
    python job_alerts.py --dry-run      # search and print only; nothing sent or saved
    python job_alerts.py --preview      # search and open the email in your browser instead
    python job_alerts.py --test-notify  # send a sample alert to check email/ntfy setup
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
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
SEEN_FILE = ROOT / "seen_jobs.json"
SEEN_RETENTION_DAYS = 60

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
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


@dataclass
class Report:
    jobs: list[Job]
    searches: list[dict]
    settings: dict
    failures: list[str]

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
    for line in path.read_text().splitlines():
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
    with path.open("rb") as f:
        config = tomllib.load(f)
    if not config.get("search"):
        sys.exit(f"{path}: add at least one [[search]] block")
    for search in config["search"]:
        if not search.get("title"):
            sys.exit(f"{path}: every [[search]] needs a title")
    return config


def option(search: dict, settings: dict, key: str, default):
    """Per-search value, falling back to [settings], then the default."""
    return search.get(key, settings.get(key, default))


def parse_window(value: str | int | float) -> timedelta:
    """'24h', '3d', '1w', or a bare number of hours."""
    if isinstance(value, (int, float)):
        return timedelta(hours=value)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([hdw])\s*", str(value).lower())
    if not m:
        sys.exit(f"posted_within must look like '24h', '3d' or '1w', not {value!r}")
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
        values = search.get(key) or []
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
TOKEN_RE = re.compile(r"\.?[^\W_][\w+#]*(?:\.\w+)*")


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

def fetch_page(session: requests.Session, params: dict, start: int) -> str:
    """One page of results, backing off when LinkedIn rate-limits (it does, often)."""
    problem = ""
    for attempt in range(5):
        try:
            resp = session.get(SEARCH_URL, params={**params, "start": start}, timeout=30)
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


def run_search(session: requests.Session, search: dict, settings: dict) -> list[Job]:
    window = parse_window(option(search, settings, "posted_within", "24h"))
    max_pages = int(option(search, settings, "max_pages", 10))
    matcher = TitleMatcher(option(search, settings, "similar_titles", True), settings.get("synonyms", []))
    titles = [search["title"], *search.get("also", [])]
    params = build_params(search, window, matcher.query(titles))
    # LinkedIn's keyword search also matches descriptions and fuzzes heavily, so by default
    # keep only jobs whose title covers every word of one of the wanted titles.
    must_match = option(search, settings, "title_must_match", True)
    wanted = [matcher.concepts(t) for t in titles]
    unwanted = [matcher.concepts(w) for w in [*settings.get("exclude", []), *search.get("exclude", [])]]
    # LinkedIn's time filter is reliable but promoted posts sometimes slip through;
    # its dates are UTC calendar days, so allow a day of slack.
    oldest = (datetime.now(timezone.utc) - window).date() - timedelta(days=1)

    found: dict[str, Job] = {}
    scanned = 0
    for page in range(max_pages):
        if page:
            time.sleep(random.uniform(4, 8))
        jobs = parse_jobs(fetch_page(session, params, page * PAGE_SIZE), search["title"])
        if not jobs:
            break
        scanned += len(jobs)
        for job in jobs:
            if job.posted_date and job.posted_date < oldest:
                continue
            words = matcher.words(job.title)
            if must_match and not any(w <= words for w in wanted):
                continue
            if any(u and u <= words for u in unwanted):
                continue
            found.setdefault(job.id, job)
    log(f"  scanned {scanned} listings, {len(found)} match the title filters")
    return list(found.values())


# ---------------------------------------------------------------------------- state

def load_seen() -> dict[str, str]:
    if not SEEN_FILE.exists():
        return {}
    seen = json.loads(SEEN_FILE.read_text())
    cutoff = (date.today() - timedelta(days=SEEN_RETENTION_DAYS)).isoformat()
    return {job_id: day for job_id, day in seen.items() if day >= cutoff}


def save_seen(seen: dict[str, str]) -> None:
    tmp = SEEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen, indent=0, sort_keys=True))
    tmp.replace(SEEN_FILE)


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
        if search.get(key):
            parts.append(", ".join(LABELS.get(v, v.capitalize()) for v in search[key]))
    parts.append(describe_window(parse_window(option(search, settings, "posted_within", "24h"))))
    if search.get("also"):
        parts.append("also " + ", ".join(search["also"]))
    if option(search, settings, "similar_titles", True):
        parts.append("similar titles")
    return " · ".join(parts)


def subject(r: Report) -> str:
    if not r.jobs:
        return "LinkedIn job alerts: search failed" if r.failures else "No new LinkedIn jobs today"
    counts = ", ".join(f"{s['title']} ({len(g)})" for s, g in r.groups() if g)
    return f"{plural(len(r.jobs), 'new LinkedIn job')}: {counts}"


def preheader(r: Report) -> str:
    """The preview line inbox lists show under the subject."""
    companies = list(dict.fromkeys(j.company for j in r.jobs if j.company))
    if not companies:
        return ""
    more = f" and {len(companies) - 3} more" if len(companies) > 3 else ""
    return ", ".join(companies[:3]) + more


def failure_note(failures: list[str]) -> str:
    return (f"Couldn't finish searching: {', '.join(failures)}. LinkedIn was probably rate-limiting; "
            "anything missed will show up in the next run.")


def render_text(r: Report) -> str:
    lines = []
    for search, group in r.groups():
        if not group:
            continue
        lines += [f"== {search['title']} ({len(group)}) ==", ""]
        for j in group:
            lines.append(j.title)
            lines.append("  " + " · ".join(filter(None, [j.company, j.location, j.posted, j.salary])))
            lines += [f"  {j.url}", ""]
    if not r.jobs:
        lines.append("No new postings matched your searches.")
    if r.failures:
        lines += ["", failure_note(r.failures)]
    return "\n".join(lines).rstrip() + "\n"


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
    where = " · ".join(filter(None, [job.location, job.posted]))
    pills = (pill_html("Early applicant", good=True) if job.early_applicant else "") + \
            (pill_html(job.salary) if job.salary else "")
    return (
        f'<tr><td class="rule" style="padding:16px 0;border-top:1px solid {RULE}">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td width="46" valign="top" style="width:46px;padding-right:14px">{logo_html(job)}</td>'
        f'<td valign="top">'
        f'<a class="accent" href="{escape(job.url)}" style="color:{ACCENT};font-size:15px;font-weight:600;'
        f'line-height:21px">{escape(job.title)}</a>'
        f'<div class="ink" style="color:{INK};font-size:14px;line-height:20px;margin-top:2px">{escape(job.company)}</div>'
        f'<div class="faint" style="color:{FAINT};font-size:13px;line-height:18px;margin-top:1px">{escape(where)}</div>'
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
    headline = plural(n, "new job") if n else "Nothing new today"
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
    if r.failures:
        body.append(
            f'<tr><td class="pad" style="padding:20px 32px 0"><div class="warn" style="background:#fdf5e6;'
            f'border:1px solid #f0dcb4;border-radius:10px;padding:12px 14px;color:#7a4b06;font-size:13px;'
            f'line-height:19px">{escape(failure_note(r.failures))}</div></td></tr>'
        )
    body += [section_html(s, g, r.settings) for s, g in groups if g]
    quiet = [s["title"] for s, g in groups if not g and s["title"] not in r.failures]
    if not n:
        note = "No new postings matched your searches since the last alert."
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
        f'Sent by your LinkedIn job alerts script at {datetime.now():%-I:%M %p}.<br>'
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
    if not r.jobs:
        lines.append("No new postings matched your searches.")
    if r.failures:
        lines.append(failure_note(r.failures))
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
    path.write_text(render_html(r))
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
    args = parser.parse_args()

    load_env(ROOT / ".env")

    if args.test_notify:
        sample = Job(id="0", title="Software Engineer (sample alert)", company="Example Co", location="Remote",
                     posted="1 hour ago", posted_date=None, salary="$120K/yr - $150K/yr",
                     url="https://www.linkedin.com/jobs/", search="Test alert", early_applicant=True)
        return 0 if notify(Report([sample], [{"title": "Test alert"}], {}, [])) else 1

    config = load_config(args.config)
    settings = config.get("settings", {})
    searches = config["search"]
    seen = load_seen()

    session = requests.Session()
    session.headers.update(HEADERS)
    new: dict[str, Job] = {}
    failures = []
    for i, search in enumerate(searches):
        if i:
            time.sleep(random.uniform(5, 10))
        log(f"Searching {search['title']!r} in {search.get('location') or 'any location'}")
        try:
            jobs = run_search(session, search, settings)
        except Exception as e:
            log(f"  search failed: {e}")
            failures.append(search["title"])
            continue
        fresh = [j for j in jobs if j.id not in seen and j.id not in new]
        log(f"  {len(fresh)} new since last run")
        new.update((j.id, j) for j in fresh)

    report = Report(list(new.values()), searches, settings, failures)
    if args.preview:
        write_preview(report)
        return 0
    if args.dry_run:
        print("\n" + render_text(report), flush=True)
        return 0

    if report.jobs or failures or settings.get("send_when_empty", False):
        if not notify(report):
            log("No notification went through; not marking jobs as seen so they're retried next run")
            return 1
    else:
        log("Nothing new; no notification sent")

    today = date.today().isoformat()
    seen.update((j.id, today) for j in report.jobs)
    save_seen(seen)
    return 1 if len(failures) == len(searches) else 0


if __name__ == "__main__":
    sys.exit(main())
