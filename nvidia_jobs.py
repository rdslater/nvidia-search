#!/usr/bin/env python3
"""
NVIDIA job tracker
==================
Pulls job postings from NVIDIA's Workday careers site (via the same JSON API the
website itself uses), stores them in a local SQLite database, and tells you which
ones are NEW since the last run.

Usage
-----
    pip install requests

    python nvidia_jobs.py                      # scrape, store, export CSVs, show new jobs
    python nvidia_jobs.py --search "firmware"  # only scrape postings matching a keyword
    python nvidia_jobs.py --list-new           # show jobs you haven't triaged yet
    python nvidia_jobs.py --find "cuda AND compiler"   # full-text search the database
    python nvidia_jobs.py --mark-applied JR1234567 JR7654321
    python nvidia_jobs.py --mark-ignored JR1111111
    python nvidia_jobs.py --export             # just rewrite the CSVs from the DB

Files (created next to this script, or in --data-dir):
    nvidia_jobs.db            SQLite database (the source of truth)
    nvidia_jobs.csv           tracking sheet: every job ever seen (open in Excel)
    new_jobs_YYYY-MM-DD.csv   only the jobs first seen on that run date

Run it daily with Task Scheduler (Windows) or cron (WSL) -- see README notes at
the bottom of this file.
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path

import requests

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
HOST = "https://nvidia.wd5.myworkdayjobs.com"
TENANT = "nvidia"
SITE = "NVIDIAExternalCareerSite"  # the path segment in .../en-US/<SITE>
PAGE_SIZE = 20                     # Workday caps this at 20
REQUEST_DELAY = 0.4                # seconds between requests (be polite)
MAX_RETRIES = 4
CAP = 2000                         # Workday won't page past ~2000 results per query

# Applied on every scrape unless you pass --filter or --all-countries.
# Values are matched against the site's filter names (see --list-facets).
DEFAULT_FILTERS = ["United States of America"]

# Relevance keywords: phrase -> weight. Nothing is hidden; this only ranks jobs
# (best match first in the CSVs and in --list-new). A hit in the job TITLE counts
# TITLE_BONUS times more than a hit in the description. Edit freely, then run
# `python nvidia_jobs.py --rescore` to re-rank everything already in the database.
KEYWORDS = {
    "computer vision": 5,
    "perception": 3,
    "image processing": 3,
    "object detection": 3,
    "3d reconstruction": 3,
    "vision language model": 3,
    "vlm": 3,
    "slam": 3,
    "segmentation": 2,
    "pose estimation": 2,
    "depth estimation": 2,
    "video analytics": 2,
    "deep learning": 2,
    "opencv": 2,
    "robotics": 1,
    "autonomous": 1,
    "multimodal": 1,
    "pytorch": 1,
    "tensorrt": 1,
    "cuda": 1,
}
TITLE_BONUS = 3

LIST_URL = f"{HOST}/wday/cxs/{TENANT}/{SITE}/jobs"
DETAIL_URL = f"{HOST}/wday/cxs/{TENANT}/{SITE}"          # + externalPath
PUBLIC_URL = f"{HOST}/en-US/{SITE}"                       # + externalPath

DEFAULT_DATA_DIR = Path(__file__).resolve().parent

CSV_COLUMNS = [
    "job_id", "title", "score", "matched", "location", "posted_date", "status",
    "first_seen", "url", "description",
]


def score_job(title: str | None, description: str | None) -> tuple[int, str]:
    """Return (score, 'kw1, kw2, ...') for a job using KEYWORDS."""
    title_l, desc_l = (title or "").lower(), (description or "").lower()
    score, hits = 0, []
    for kw, w in KEYWORDS.items():
        pat = r"(?<!\w)" + re.escape(kw.lower()) + r"(?!\w)"
        in_title = re.search(pat, title_l) is not None
        in_desc = re.search(pat, desc_l) is not None
        if in_title or in_desc:
            score += w * ((TITLE_BONUS if in_title else 0) + (1 if in_desc else 0))
            hits.append(kw + ("*" if in_title else ""))   # * = matched in title
    return score, ", ".join(hits)


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept": "application/json",
        "Accept-Language": "en-US",
    })
    return s


def request_json(session: requests.Session, method: str, url: str, **kw) -> dict:
    """HTTP request with retry/backoff; returns parsed JSON."""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.request(method, url, timeout=30, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last_err = e
            wait = 2 ** attempt
            print(f"  ! {e} (attempt {attempt}/{MAX_RETRIES}), retrying in {wait}s",
                  file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"Giving up on {url}: {last_err}")


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #
def post_page(session, applied: dict, search_text: str, offset: int) -> dict:
    body = {"appliedFacets": applied, "limit": PAGE_SIZE, "offset": offset,
            "searchText": search_text}
    return request_json(session, "POST", LIST_URL, json=body)


def flatten_facets(facets) -> list[dict]:
    """Workday facet tree -> [{param, label, values:[{descriptor, id, count}]}]."""
    groups: dict[str, dict] = {}

    def walk(node, param=None, label=None):
        if "values" in node:
            p = node.get("facetParameter") or param
            l = node.get("descriptor") or label
            for v in node["values"]:
                walk(v, p, l)
        elif "id" in node:
            p = node.get("facetParameter") or param
            g = groups.setdefault(p, {"param": p, "label": label, "values": []})
            g["values"].append({"descriptor": node.get("descriptor", ""),
                                "id": node["id"], "count": node.get("count", 0)})

    for f in facets or []:
        walk(f)
    return list(groups.values())


def list_facets(session) -> None:
    first = post_page(session, {}, "", 0)
    print(f"Total postings on site (unfiltered): {first.get('total')}\n")
    for g in flatten_facets(first.get("facets")):
        print(f"== {g['param']}   ({g['label']})")
        for v in sorted(g["values"], key=lambda v: -v["count"]):
            print(f"     {v['descriptor']}  [{v['count']}]")
    print('\nUse with:  --filter "<param>=<value>"   e.g.  --filter "locationHierarchy1=United States of America"')


def resolve_filters(session, specs: list[str]) -> dict:
    """Turn ['United States', 'jobFamilyGroup=Engineering'] into Workday appliedFacets."""
    if not specs:
        return {}
    groups = flatten_facets(post_page(session, {}, "", 0).get("facets"))
    applied: dict[str, list[str]] = {}
    for spec in specs:
        param_wanted, text = (spec.split("=", 1) if "=" in spec else (None, spec))
        text_l = text.strip().lower()
        pool = [g for g in groups
                if not param_wanted or (g["param"] or "").lower() == param_wanted.strip().lower()]
        exact = [(g, v) for g in pool for v in g["values"]
                 if v["descriptor"].strip().lower() == text_l]
        hits = exact or [(g, v) for g in pool for v in g["values"]
                         if text_l in v["descriptor"].lower()]
        if not hits:
            sys.exit(f"Filter {spec!r} matched nothing. Run with --list-facets to see options.")
        params = {g["param"] for g, _ in hits}
        if len(params) > 1:
            lines = "\n".join(f"   --filter \"{g['param']}={v['descriptor']}\"" for g, v in hits[:15])
            sys.exit(f"Filter {spec!r} is ambiguous; be specific with param=value:\n{lines}")
        for g, v in hits:
            applied.setdefault(g["param"], [])
            if v["id"] not in applied[g["param"]]:
                applied[g["param"]].append(v["id"])
            print(f"  filter: {g['param']} = {v['descriptor']} ({v['count']} jobs)")
    return applied


def pick_split_group(groups: list[dict], applied: dict) -> dict | None:
    """Choose an unapplied facet that splits the results into the smallest chunks."""
    cands = []
    for g in groups:
        vals = [v for v in g["values"] if v["count"]]
        if g["param"] in applied or len(vals) < 2:
            continue
        cands.append((max(v["count"] for v in vals), g["param"], g, vals))
    if not cands:
        return None
    _, _, g, vals = min(cands, key=lambda c: c[:2])
    return {"param": g["param"], "values": vals}


def fetch_listing(session, search_text: str = "", applied: dict | None = None,
                  state: dict | None = None, _depth: int = 0) -> list[dict]:
    """Return every posting summary. Workday refuses to page past ~2000 results for one
    query, so a big result set is split by another facet and the pieces are merged."""
    applied = applied or {}
    state = state if state is not None else {"complete": True}
    first = post_page(session, applied, search_text, 0)
    total = first.get("total", 0) or 0

    if total >= CAP and _depth < 3:
        grp = pick_split_group(flatten_facets(first.get("facets")), applied)
        if grp:
            print(f"  {total} results hits Workday's {CAP} cap; splitting by {grp['param']} "
                  f"into {len(grp['values'])} parts")
            out: list[dict] = []
            for v in grp["values"]:
                out.extend(fetch_listing(session, search_text,
                                         {**applied, grp["param"]: [v["id"]]}, state, _depth + 1))
            return out

    postings = list(first.get("jobPostings", []))
    offset = len(postings)
    while postings and offset < total:
        time.sleep(REQUEST_DELAY)
        page = post_page(session, applied, search_text, offset).get("jobPostings", [])
        if not page:
            break
        postings.extend(page)
        offset += len(page)
        print(f"  listing: {len(postings)}/{total}", end="\r")
    if len(postings) < total:
        state["complete"] = False
        print(f"\n  WARNING: got {len(postings)} of {total} postings for this query "
              f"(Workday cap). Add a --filter to narrow it.", file=sys.stderr)
    return postings


def job_id_from(posting: dict) -> str | None:
    """Requisition id like JR1999999. Lives in bulletFields, or end of externalPath."""
    for b in posting.get("bulletFields") or []:
        if re.fullmatch(r"[A-Z]{1,3}\d{4,}", str(b).strip()):
            return str(b).strip()
    m = re.search(r"_([A-Z]{1,3}\d{4,})(?:-\d+)?$", posting.get("externalPath", ""))
    return m.group(1) if m else None


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "br", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "tr"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html or "")
    text = "".join(p.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def fetch_detail(session: requests.Session, external_path: str) -> dict:
    data = request_json(session, "GET", DETAIL_URL + external_path)
    info = data.get("jobPostingInfo", {})
    return {
        "title": info.get("title"),
        "location": info.get("location"),
        "posted_date": info.get("startDate"),            # YYYY-MM-DD
        "description": html_to_text(info.get("jobDescription", "")),
        "url": info.get("externalUrl") or (PUBLIC_URL + external_path),
        "job_id": info.get("jobReqId"),
    }


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    title         TEXT,
    location      TEXT,
    posted_date   TEXT,
    description   TEXT,
    url           TEXT,
    external_path TEXT,
    score         INTEGER NOT NULL DEFAULT 0,    -- keyword relevance (see KEYWORDS)
    matched       TEXT,                          -- which keywords hit (* = in title)
    status        TEXT NOT NULL DEFAULT 'new',   -- new | applied | ignored
    active        INTEGER NOT NULL DEFAULT 1,    -- 0 once it disappears from the site
    first_seen    TEXT,
    last_seen     TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
    job_id UNINDEXED, title, location, description
);
"""


def open_db(path: Path) -> tuple[sqlite3.Connection, bool]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # migrate databases created before scoring existed
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "score" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN score INTEGER NOT NULL DEFAULT 0")
    if "matched" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN matched TEXT")
        conn.commit()
        _rescore(conn)
    has_fts = True
    try:
        conn.executescript(FTS_SCHEMA)
    except sqlite3.OperationalError:
        has_fts = False  # SQLite built without FTS5; fall back to LIKE search
    return conn, has_fts


def upsert_new_job(conn, has_fts, job: dict, today: str):
    """Insert a job if its id is unseen. Returns True if inserted, False if it existed."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO jobs (job_id, title, location, posted_date, description,
                             url, external_path, score, matched, status, active,
                             first_seen, last_seen)
           VALUES (:job_id, :title, :location, :posted_date, :description, :url,
                   :external_path, :score, :matched, 'new', 1, :today, :today)""",
        {**job, "today": today},
    )
    if cur.rowcount == 0:
        return False
    if has_fts:
        conn.execute(
            "INSERT INTO jobs_fts (job_id, title, location, description) VALUES (?,?,?,?)",
            (job["job_id"], job["title"], job["location"], job["description"]),
        )
    return True


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_scrape(args, conn, has_fts, data_dir: Path):
    today = date.today().isoformat()
    session = make_session()

    print(f"Fetching job list from {HOST} ...")
    specs = args.filter if args.filter else ([] if args.all_countries else DEFAULT_FILTERS)
    applied = resolve_filters(session, specs)
    state = {"complete": True}
    postings = fetch_listing(session, args.search or "", applied, state)
    args.listing_complete = state["complete"]
    if not postings:
        print("No postings returned. Check SITE in the config section.", file=sys.stderr)
        return 1

    seen_ids: set[str] = set()
    to_fetch: list[tuple[str, dict]] = []
    known = {r["job_id"] for r in conn.execute("SELECT job_id FROM jobs")}

    for p in postings:
        jid = job_id_from(p)
        if not jid:
            print(f"  ? could not determine id for {p.get('title')!r}", file=sys.stderr)
            continue
        if jid in seen_ids:
            continue  # same requisition listed twice (paging overlap / multi-location)
        seen_ids.add(jid)
        if jid not in known:
            to_fetch.append((jid, p))

    print(f"{len(postings)} postings on site, {len(to_fetch)} new to me.")

    new_ids: list[str] = []
    local = threading.local()

    def worker(item):
        # one HTTP session per thread
        if not hasattr(local, "s"):
            local.s = make_session()
        jid, p = item
        try:
            return jid, p, fetch_detail(local.s, p["externalPath"]), None
        except Exception as e:  # keep going; it will be retried next run
            return jid, p, None, e

    pool = ThreadPoolExecutor(max_workers=max(1, args.workers))
    try:
        results = pool.map(worker, to_fetch)  # yields in order, runs in parallel
        _process = enumerate(results, 1)
        _scrape_loop(_process, to_fetch, conn, has_fts, today, new_ids)
    except KeyboardInterrupt:
        print("\nInterrupted -- saving progress. Re-run to continue.", file=sys.stderr)
        pool.shutdown(wait=False, cancel_futures=True)
        conn.commit()
        return 130
    pool.shutdown()
    return _finish_scrape(args, conn, data_dir, today, seen_ids, new_ids)


def _scrape_loop(results, to_fetch, conn, has_fts, today, new_ids):
    for i, (jid, p, d, err) in results:
        if err is not None:
            print(f"  ! detail failed for {jid}: {err}", file=sys.stderr)
            continue
        job = {
            "job_id": jid,
            "title": d["title"] or p.get("title"),
            "location": d["location"] or p.get("locationsText"),
            "posted_date": d["posted_date"],
            "description": d["description"],
            "url": d["url"],
            "external_path": p["externalPath"],
        }
        job["score"], job["matched"] = score_job(job["title"], job["description"])
        if not upsert_new_job(conn, has_fts, job, today):
            continue  # already stored; skip
        new_ids.append(jid)
        print(f"  [{i}/{len(to_fetch)}] NEW {jid}  {job['title']}")
        if i % 25 == 0:
            conn.commit()
    conn.commit()


def _finish_scrape(args, conn, data_dir, today, seen_ids, new_ids):
    # Refresh last_seen / active flags (only when scraping the whole site, not a keyword search)
    if seen_ids:
        conn.executemany(
            "UPDATE jobs SET last_seen=?, active=1 WHERE job_id=?",
            [(today, j) for j in seen_ids],
        )
        if not args.search and args.listing_complete:
            marks = ",".join("?" * len(seen_ids))
            conn.execute(f"UPDATE jobs SET active=0 WHERE job_id NOT IN ({marks})",
                         list(seen_ids))
    conn.commit()

    export_csvs(conn, data_dir, new_today=today)
    print(f"\nDone. {len(new_ids)} new job(s) today. "
          f"Total in database: {conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]}")
    return 0


def export_csvs(conn, data_dir: Path, new_today: str | None = None):
    cols = ", ".join(CSV_COLUMNS)

    def clean(col, val):
        """Make a value safe for spreadsheets: no line breaks / control chars, <32k chars."""
        if val is None:
            return ""
        if not isinstance(val, str):
            return val
        val = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", val.replace("\r\n", "\n").replace("\r", "\n"))
        val = re.sub(r"\n+", " | ", val.strip())   # keep paragraph breaks visible as " | "
        return val[:30000]

    def write(path: Path, rows):
        data = [[clean(c, r[c]) for c in CSV_COLUMNS] for r in rows]
        # utf-8-sig so Excel opens it with the right encoding; QUOTE_ALL = unambiguous cells
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, quoting=csv.QUOTE_ALL)
            w.writerow(CSV_COLUMNS)
            w.writerows(data)
        write_xlsx(path.with_suffix(".xlsx"), data)

    def write_xlsx(path: Path, data):
        try:
            from openpyxl import Workbook
        except ImportError:
            if not getattr(export_csvs, "_warned", False):
                print("  (tip: `pip install openpyxl` to also get .xlsx files, "
                      "which open in Excel without any import quirks)")
                export_csvs._warned = True
            return
        wb = Workbook()
        ws = wb.active
        ws.title = "jobs"
        ws.append(CSV_COLUMNS)
        for row in data:
            ws.append(row)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        widths = {"job_id": 12, "title": 50, "score": 7, "matched": 40, "location": 24,
                  "posted_date": 12, "status": 9, "first_seen": 12, "url": 30, "description": 80}
        for i, c in enumerate(CSV_COLUMNS, 1):
            ws.column_dimensions[ws.cell(1, i).column_letter].width = widths.get(c, 15)
        wb.save(path)

    all_rows = conn.execute(
        f"SELECT {cols} FROM jobs ORDER BY first_seen DESC, score DESC, "
        f"posted_date DESC").fetchall()
    write(data_dir / "nvidia_jobs.csv", all_rows)
    print(f"Wrote {data_dir / 'nvidia_jobs.csv'} ({len(all_rows)} rows)")

    if new_today:
        new_rows = conn.execute(
            f"SELECT {cols} FROM jobs WHERE first_seen=? ORDER BY score DESC, posted_date DESC",
            (new_today,)).fetchall()
        if new_rows:
            p = data_dir / f"new_jobs_{new_today}.csv"
            write(p, new_rows)
            print(f"Wrote {p} ({len(new_rows)} rows)")


def print_rows(rows):
    if not rows:
        print("(none)")
        return
    for r in rows:
        print(f"{r['job_id']:<12} {r['posted_date'] or '??':<10} {r['status']:<8} "
              f"[{r['score']:>3}] {(r['title'] or '')[:55]:<55}  {r['location'] or ''}")
        print(f"{'':<12} {r['url']}")
        if r["matched"]:
            print(f"{'':<12} matched: {r['matched']}")


def cmd_list_new(conn):
    print_rows(conn.execute(
        "SELECT * FROM jobs WHERE status='new' AND active=1 "
        "ORDER BY score DESC, first_seen DESC, posted_date DESC").fetchall())


def _rescore(conn):
    rows = conn.execute("SELECT job_id, title, description FROM jobs").fetchall()
    conn.executemany(
        "UPDATE jobs SET score=?, matched=? WHERE job_id=?",
        [(*score_job(r["title"], r["description"]), r["job_id"]) for r in rows])
    conn.commit()
    return len(rows)


def cmd_find(conn, has_fts, query: str):
    if has_fts:
        rows = conn.execute(
            """SELECT j.* FROM jobs_fts f JOIN jobs j ON j.job_id=f.job_id
               WHERE jobs_fts MATCH ? ORDER BY rank LIMIT 100""", (query,)).fetchall()
    else:
        like = f"%{query}%"
        rows = conn.execute(
            """SELECT * FROM jobs WHERE title LIKE ? OR description LIKE ?
               OR location LIKE ? ORDER BY posted_date DESC LIMIT 100""",
            (like, like, like)).fetchall()
    print_rows(rows)


def cmd_mark(conn, ids: list[str], status: str):
    for jid in ids:
        cur = conn.execute("UPDATE jobs SET status=? WHERE job_id=?", (status, jid))
        print(f"{jid}: {'marked ' + status if cur.rowcount else 'NOT FOUND'}")
    conn.commit()


def main():
    ap = argparse.ArgumentParser(description="Track new NVIDIA job postings.")
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                    help="where to keep the database and CSVs (default: next to script)")
    ap.add_argument("--search", help="only scrape postings matching this keyword")
    ap.add_argument("--filter", action="append", metavar="[PARAM=]VALUE",
                    help='Workday filter, repeatable, e.g. --filter "United States of America" '
                         'or --filter "jobFamilyGroup=Engineering". Same param = OR, '
                         'different params = AND. Overrides DEFAULT_FILTERS.')
    ap.add_argument("--all-countries", action="store_true",
                    help="ignore DEFAULT_FILTERS (no country filter)")
    ap.add_argument("--list-facets", action="store_true",
                    help="print the available filters/values (with job counts) and exit")
    ap.add_argument("--rescore", action="store_true",
                    help="recompute relevance scores after editing KEYWORDS, then rewrite CSVs")
    ap.add_argument("--workers", type=int, default=8,
                    help="parallel description downloads (default 8; lower if you see HTTP 429s)")
    ap.add_argument("--list-new", action="store_true",
                    help="list active jobs whose status is still 'new'")
    ap.add_argument("--find", metavar="QUERY",
                    help="full-text search the local database (FTS5 syntax: AND, OR, \"phrase\")")
    ap.add_argument("--mark-applied", nargs="+", metavar="JOB_ID")
    ap.add_argument("--mark-ignored", nargs="+", metavar="JOB_ID")
    ap.add_argument("--export", action="store_true", help="rewrite CSVs from the DB and exit")
    args = ap.parse_args()

    args.data_dir.mkdir(parents=True, exist_ok=True)
    conn, has_fts = open_db(args.data_dir / "nvidia_jobs.db")

    if args.list_facets:
        return list_facets(make_session())
    if args.rescore:
        print(f"Rescored {_rescore(conn)} jobs.")
        return export_csvs(conn, args.data_dir)
    if args.mark_applied:
        return cmd_mark(conn, args.mark_applied, "applied")
    if args.mark_ignored:
        return cmd_mark(conn, args.mark_ignored, "ignored")
    if args.find:
        return cmd_find(conn, has_fts, args.find)
    if args.list_new:
        return cmd_list_new(conn)
    if args.export:
        return export_csvs(conn, args.data_dir)
    return cmd_scrape(args, conn, has_fts, args.data_dir)


if __name__ == "__main__":
    sys.exit(main() or 0)

# --------------------------------------------------------------------------- #
# Scheduling notes
# --------------------------------------------------------------------------- #
# Windows Task Scheduler (daily 8:00am):
#   schtasks /create /tn "NVIDIA Jobs" /sc daily /st 08:00 ^
#     /tr "python C:\path\to\nvidia_jobs.py"
# WSL / Linux cron:
#   0 8 * * *  cd ~/nvidia-search && python3 nvidia_jobs.py >> run.log 2>&1
#
# If the script reports "No postings returned", open the careers site in a browser,
# look at the URL (…/en-US/<SITE>) and update SITE at the top of this file.
