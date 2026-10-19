"""Best-effort read of the PUBLIC official NBA injury report PDFs.

Not an NBA-endorsed API. HTML structure/publication schedules may change.
Returns empty results with an error rather than fabricating statuses.
"""
from __future__ import annotations
import io
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import fitz  # pymupdf
import requests
from bs4 import BeautifulSoup

INDEX = "https://official.nba.com/nba-injury-report-2026-27-season/"
CDN = "https://ak-static.cms.nba.com/referee/injury/"
ET = ZoneInfo("America/New_York")
STATUSES = {"OUT", "QUESTIONABLE", "DOUBTFUL", "PROBABLE", "AVAILABLE"}
PDF_RE = re.compile(r"Injury-Report_(\d{4}-\d{2}-\d{2})_(\d{2})_(\d{2})(AM|PM)\.pdf", re.I)
MATCHUP = re.compile(r"\b[A-Z]{2,3}@[A-Z]{2,3}\b")
DATE_RE = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")


def report_datetime(url: str) -> datetime | None:
    m = PDF_RE.search(url)
    if not m:
        return None
    try:
        return datetime.strptime(" ".join(m.groups()), "%Y-%m-%d %I %M %p").replace(tzinfo=ET)
    except ValueError:
        return None


def _candidate_urls(now: datetime) -> list[str]:
    """Small best-effort probe (recent quarter-hourly and older hourly files)."""
    now = now.astimezone(ET)
    first = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    moments = [first - timedelta(minutes=15*i) for i in range(17)]
    moments += [first - timedelta(hours=i) for i in range(5, 25)]
    unique = sorted(set(moments), reverse=True)
    return [CDN + f"Injury-Report_{v.strftime('%Y-%m-%d_%I_%M%p')}.pdf" for v in unique]


def latest_report_url(session: requests.Session | None = None,
                      now: datetime | None = None,
                      explicit_url: str | None = None) -> tuple[str, datetime | None]:
    """Find official latest PDF. Explicit URL is useful for debugging and fixtures."""
    if explicit_url:
        if not explicit_url.startswith("https://"):
            raise ValueError("NBA_REPORT_PDF_URL musi zaczynać się od https://")
        return explicit_url, report_datetime(explicit_url)
    session = session or requests.Session()
    now = now or datetime.now(timezone.utc)
    candidates: list[str] = []
    try:
        response = session.get(INDEX, timeout=12, headers={"User-Agent": "NBAPropsResearch/0.3"})
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for node in soup.select("a[href],iframe[src]"):
            ref = node.get("href") or node.get("src") or ""
            full = urljoin(INDEX, ref)
            if PDF_RE.search(full):
                candidates.append(full)
        candidates += [urljoin(INDEX, m.group(0)) for m in PDF_RE.finditer(response.text)]
    except requests.RequestException:
        pass  # Fall back to CDN filenames; fail explicitly if all fail.
    # HTML may include older snapshots; test newest first rather than assuming the first is current.
    candidates = sorted(set(candidates), key=lambda url: report_datetime(url) or datetime.min.replace(tzinfo=ET), reverse=True)
    for url in candidates[:6]:
        dt = report_datetime(url)
        if dt and (now.astimezone(ET) - dt).total_seconds() > 18 * 3600:
            continue
        try:
            r = session.get(url, timeout=15, headers={"Range": "bytes=0-24"})
            if r.ok and (r.content[:4] == b"%PDF" or "pdf" in r.headers.get("Content-Type", "").lower()):
                return url, report_datetime(url)
        except requests.RequestException:
            continue
    # CDN probing is only a fallback; no fake report link on failure.
    for url in _candidate_urls(now):
        try:
            r = session.head(url, timeout=5, allow_redirects=True)
            if r.ok and "pdf" in r.headers.get("Content-Type", "").lower():
                return url, report_datetime(url)
        except requests.RequestException:
            continue
    raise RuntimeError("Nie znaleziono aktualnego publicznego PDF z oficjalnym raportem NBA")


def surname_to_full(name: str) -> str:
    # NBA PDF uses e.g. 'Gilbert, Keshon'; keep surname suffixes intact.
    if "," in name:
        last, first = name.split(",", 1)
        return f"{first.strip()} {last.strip()}".strip()
    return name.strip()


def parse_injury_pdf(pdf_bytes: bytes, url: str = "") -> list[dict]:
    """Read fixed-column layout from official NBA reports using word geometry.

    Date, game, and team are printed once per group, so carry values forward
    across rows and pages. Only rows with an explicit recognized status count.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    rows = []
    game_day = matchup = team = ""
    for page in doc:
        width = page.rect.width
        wordlist = page.get_text("words")
        # Word grouping by top baseline, usually within 1-2 pts.
        lines: list[dict] = []
        for x0, y0, x1, y1, token, *_ in sorted(wordlist, key=lambda z: (z[1], z[0])):
            if not lines or abs(y0 - lines[-1]["y"]) > 2.4:
                lines.append({"y": y0, "words": []})
            lines[-1]["words"].append((x0, (y0 + y1) / 2, token))
        for line in lines:
            words = line["words"]
            cell = lambda start, end: " ".join(v for x, _, v in sorted(words) if width * start <= x < width * end).strip()
            # The fixed column starts are 2.5%, 14%, 23%, 31%, 50%, 69%, 79%.
            date_cell = cell(0.00, 0.14)
            match_cell = cell(0.23, 0.31)
            team_cell = cell(0.31, 0.50)
            player_cell = cell(0.50, 0.69)
            status_cell = cell(0.69, 0.79).upper()
            reason_cell = cell(0.79, 1.01)
            found_date = DATE_RE.search(date_cell)
            if found_date:
                try:
                    game_day = datetime.strptime(found_date.group(0), "%m/%d/%Y").date().isoformat()
                except ValueError:
                    pass
            found_match = MATCHUP.search(match_cell)
            if found_match:
                matchup = found_match.group(0)
            if team_cell and team_cell.lower() != "team" and not team_cell.lower().startswith("game"):
                team = team_cell
            # Some PDFs add spaces in Current Status; only recognized single statuses.
            if status_cell not in STATUSES or not player_cell or player_cell.lower() == "player name":
                continue
            if not (game_day and matchup and team):
                continue  # no valid game context — fail closed
            rows.append({"game_date": game_day, "matchup": matchup, "team": team,
                         "player": surname_to_full(player_cell), "status": status_cell,
                         "reason": reason_cell, "source_url": url})
    # Suppress corrupt and duplicate lines.
    output = {}
    for row in rows:
        key = (row["game_date"], row["matchup"], row["team"], row["player"])
        output[key] = row
    return list(output.values())



def _only_unsubmitted_report(pdf_bytes: bytes) -> bool:
    """Recognize the legitimate pre-report state, not a broken parser.

    Reports before clubs submit their information contain matchups and
    'NOT YET SUBMITTED' but *no* player statuses. Never mask a document
    containing actual statuses that our parser failed to recognize.
    """
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        content = "\n".join(page.get_text("text") for page in doc)
    pending = len(re.findall(r"\bNOT\s+YET\s+SUBMITTED\b", content, re.I))
    matchups = len(MATCHUP.findall(content))
    actual_statuses = re.search(r"\b(?:OUT|QUESTIONABLE|DOUBTFUL|PROBABLE|AVAILABLE)\b", content, re.I)
    return pending > 0 and matchups > 0 and actual_statuses is None


def fetch_official_report(session: requests.Session | None = None,
                          explicit_url: str | None = None,
                          now: datetime | None = None) -> tuple[list[dict], dict]:
    session = session or requests.Session()
    now = now or datetime.now(timezone.utc)
    url, report_at = latest_report_url(session=session, now=now, explicit_url=explicit_url)
    if report_at and (now.astimezone(ET) - report_at).total_seconds() > 18 * 3600:
        raise RuntimeError(f"Raport jest nieaktualny (>18h): {url}")
    reply = session.get(url, timeout=25)
    reply.raise_for_status()
    if not reply.content.startswith(b"%PDF"):
        raise RuntimeError("Zamiast PDF otrzymano inny format")
    statuses = parse_injury_pdf(reply.content, url=url)
    meta = {"source_url": url, "report_at": report_at.isoformat() if report_at else "unknown",
            "fetched_at": now.isoformat(), "count": len(statuses)}
    if not statuses:
        if _only_unsubmitted_report(reply.content):
            meta["state"] = "awaiting_submissions"
            return [], meta  # Valid PDF, but clubs have yet to report. Do NOT alter saved snapshot.
        raise RuntimeError("Nie rozpoznano żadnych zawodników w PDF; parser wymaga weryfikacji")
    meta["state"] = "ready"
    return statuses, meta
