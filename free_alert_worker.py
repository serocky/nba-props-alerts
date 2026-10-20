"""FREE official-NBA injury status monitor with Telegram delivery.

Runs once (--once, for GitHub Actions) or loops (VPS). No bookmaker claims,
no paid API, and no inferred value bets from missing prices.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from official_injuries import fetch_official_report

ROOT = Path(__file__).resolve().parent
STATE = Path(os.getenv("FREE_STATE_PATH", str(ROOT / "runtime" / "official_snapshot.json")))
TELEGRAM_URL = "https://api.telegram.org/bot{}/sendMessage"
WATCHLIST = Path(os.getenv("IMPACT_RULES_CSV", str(ROOT / "data" / "impact_rules.csv")))
ODDS = Path(os.getenv("POLISH_ODDS_CSV", str(ROOT / "data" / "polish_odds.csv")))
ALLOWED_BOOKS = {"Superbet", "STS", "Fortuna", "Betclic"}


def candidates_for_event(event: dict, now: datetime | None = None) -> list[dict]:
    """OPTIONAL manual watchlist and manually timestamped odds.

    These are NOT modeled recommendations. Stale odds or missing rules -> [].
    """
    if not WATCHLIST.exists() or not ODDS.exists():
        return []
    now = now or datetime.now(timezone.utc)
    with WATCHLIST.open(encoding="utf-8-sig", newline="") as f:
        rules = list(csv.DictReader(f))
    with ODDS.open(encoding="utf-8-sig", newline="") as f:
        quotes = list(csv.DictReader(f))
    affected = {(r.get("affected_player", "").strip().casefold(), r.get("market", "").strip().upper()):
                r for r in rules
                if r.get("absent_player", "").strip().casefold() == event["player"].casefold()
                and r.get("team", "").strip().casefold() == event["team"].casefold()}
    out = []
    for quote in quotes:
        player = quote.get("player", "").strip()
        market = quote.get("market", "").strip().upper()
        if (player.casefold(), market) not in affected:
            continue
        if quote.get("game", "").strip().upper() != event["matchup"].upper():
            continue
        if quote.get("game_date", "").strip() != event["game_date"]:
            continue
        if quote.get("bookmaker", "").strip() not in ALLOWED_BOOKS:
            continue
        try:
            ts = datetime.fromisoformat(quote["checked_at_utc"].replace("Z", "+00:00")).astimezone(timezone.utc)
            delta = (now - ts).total_seconds()
            odds = float(quote["odds"])
            line = float(quote["line"])
            if not (0 <= delta <= 180 and odds > 1 and line >= 0 and market in ("PTS", "AST", "REB", "PRA")):
                continue
        except (ValueError, KeyError, TypeError):
            continue
        out.append({"player": player, "market": market, "side": quote.get("side", "").upper(),
                    "line": line, "bookmaker": quote["bookmaker"], "odds": odds,
                    "age_s": int(delta), "why": affected[(player.casefold(), market)].get("reason", "")})
    return sorted(out, key=lambda d: (d["player"], d["market"], d["line"], -d["odds"]))[:8]



def status_key(row: dict) -> str:
    return "|".join(str(row[k]).casefold() for k in ("game_date", "matchup", "team", "player"))


def person_key(row: dict) -> str:
    return "|".join(str(row[k]).casefold() for k in ("team", "player"))


def process_report(state: dict, current: list[dict], meta: dict) -> tuple[dict, list[dict]]:
    if not current:
        raise ValueError("Odmowa nadpisania stanu pustym raportem")
    prev = state.get("status_rows") or {}
    prev_person = state.get("by_person") or {}
    baseline = bool(state.get("initialized", False))
    new_by_key = {status_key(row): row for row in current}
    # Discard malformed legacy team keys produced by old PDF page headers.
    # Otherwise an old invalid identity would be kept forever.
    new_person = {key: value for key, value in prev_person.items()
                  if not key.startswith("injury report:|")}
    old_game_player = {
        (str(r.get("game_date", "")), str(r.get("matchup", "")).upper(), str(r.get("player", "")).casefold()): r
        for r in prev.values()
    }
    new_events = []
    for key, row in new_by_key.items():
        old = prev.get(key, {}).get("status", "")
        person_before = prev_person.get(person_key(row), "")
        # A corrected team mapping is NOT a new injury: an already-OUT player
        # in the same game must not produce a spurious Telegram alert.
        earlier_same_player = old_game_player.get((row["game_date"], row["matchup"].upper(), row["player"].casefold()))
        was_out_in_game = earlier_same_player is not None and earlier_same_player.get("status") == "OUT"
        changed_to_out = row["status"] == "OUT" and old != "OUT" and not was_out_in_game
        if baseline and changed_to_out:
            # Out for a NEW future matchup but already Out in previous game
            # must not generate recycled news.
            if not old and person_before == "OUT":
                pass
            else:
                new_events.append({"event_id": hashlib.sha256(key.encode()).hexdigest()[:18] + ":OUT",
                                   "previous": old or person_before or "NOT LISTED",
                                   **row})
        new_person[person_key(row)] = row["status"]
    latest_history = (state.get("events") or [])[-200:]
    already = {event["event_id"] for event in latest_history}
    events = [x for x in new_events if x["event_id"] not in already]
    latest_history += events
    # Pending queue protects against Telegram outage; retries next run.
    queue = {e["event_id"]: e for e in state.get("pending") or []}
    for event in events:
        queue[event["event_id"]] = event
    newstate = {"initialized": True, "status_rows": new_by_key,
                "by_person": new_person, "events": latest_history[-200:],
                "pending": list(queue.values()), "metadata": meta}
    return newstate, events


def format_telegram(event: dict, candidates: list[dict] | None = None) -> str:
    msg = (f"🚨 NBA — ZMIANA NA OUT (raport oficjalny)\n"
            f"🏀 {event['matchup']} | {event['game_date']}\n"
            f"👤 {event['player']} — {event['team']}\n"
            f"🔁 {event.get('previous', 'UNKNOWN')} → OUT\n"
            f"📝 {event.get('reason') or 'Brak szczegółów'}\n\n"
            f"🔎 Sprawdź wpływ na minuty, usage, PTS/AST/REB jego kolegów.\n"
            f"⚠️ To alert o absencji, nie gotowy typ.\n"
            f"📄 {event['source_url']}")
    # Add historical game-level teammate impact ONLY when real, documented game data exist.
    # Import lazily: no pandas required for the official PDF-only monitoring path.
    try:
        from player_impact import historical_data, telegram_impact
        real_logs, real_schedule, provenance = historical_data(allow_demo=False)
        if provenance == 'REAL' and real_logs is not None:
            msg += telegram_impact(event, real_logs, real_schedule)
    except Exception as exc:
        print('ANALIZA HISTORYCZNA NIEDOSTĘPNA:', type(exc).__name__)
    if candidates:
        msg += "\n\n📋 OBSERWOWANE LINIE (wprowadzone ręcznie, NIE live feed):"
        for q in candidates:
            msg += (f"\n• {q['player']} {q['market']} {q['side']} {q['line']:g} | "
                    f"{q['bookmaker']} {q['odds']:.2f} | wiek {q['age_s']} s")
        msg += "\nKurs sprawdź w aplikacji przed decyzją."
    return msg


def send_telegram(msg: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("TELEGRAM NOT CONFIGURED (dry-run):", msg)
        return False
    r = requests.post(TELEGRAM_URL.format(token), json={"chat_id": chat, "text": msg,
                   "disable_web_page_preview": True}, timeout=18)
    r.raise_for_status()
    if not r.json().get("ok"):
        raise RuntimeError("Błąd Telegram API")
    return True


def write_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def tick(session=None, explicit_url: str | None = None, send_fn=send_telegram) -> dict:
    state = json.loads(STATE.read_text("utf-8")) if STATE.exists() else {}
    now = datetime.now(timezone.utc)
    current, meta = fetch_official_report(session=session, explicit_url=explicit_url, now=now)
    if meta.get("state") == "awaiting_submissions":
        # A pre-submission PDF is valid, but there are no player statuses to compare.
        # Don't erase the last good snapshot or treat this as a parser failure.
        print("NBA REPORT: NOT YET SUBMITTED — waiting for team injury statuses")
        return {"fresh": 0, "pending": len(state.get("pending", [])), "attempted": 0,
                "rows": 0, "report_at": meta.get("report_at"),
                "source": meta.get("source_url"), "state": "awaiting_submissions"}
    state, fresh = process_report(state, current, meta)
    # On first run, never back-notify old OUT statuses.
    attempted = 0
    pending = []
    for event in state["pending"]:
        attempted += 1
        try:
            delivered = send_fn(format_telegram(event, candidates_for_event(event, now=now)))
        except Exception as exc:
            print("BŁĄD wysyłki alertu:", type(exc).__name__)
            delivered = False
        if not delivered:
            pending.append(event)
    state["pending"] = pending
    # Reduce GitHub commits: keep last successful fetch time in logs, but only
    # write versioned snapshot when official content, report URL or queue changes.
    old_content = dict(json.loads(STATE.read_text("utf-8"))) if STATE.exists() else {}
    old_content.get("metadata", {}).pop("fetched_at", None)
    comparable = json.loads(json.dumps(state))
    comparable.get("metadata", {}).pop("fetched_at", None)
    if old_content != comparable:
        write_atomic(STATE, state)
    return {"fresh": len(fresh), "pending": len(pending), "attempted": attempted,
            "rows": len(current), "report_at": meta.get("report_at"), "source": meta.get("source_url")}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="One run for scheduler/GitHub Actions")
    p.add_argument("--interval", type=int, default=300)
    args = p.parse_args()
    while True:
        try:
            result = tick(explicit_url=os.getenv("NBA_REPORT_PDF_URL") or None)
            print(json.dumps(result, ensure_ascii=False))
        except Exception as exc:
            print("BŁĄD MONITORINGU:", type(exc).__name__, str(exc)[:350])
            if args.once:
                raise
        if args.once:
            break
        time.sleep(max(120, args.interval))


if __name__ == "__main__":
    main()
