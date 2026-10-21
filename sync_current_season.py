"""NBA 2026/27: conservative ESPN completed-game importer for NBA Props Lab.

Replaces the previous SportsDataverse 2027 parquet dependency (not yet available).
Reads ESPN public scoreboard + game summary JSON. ESPN's endpoints are not a
contracted data API: schema, rate limits and accessibility may change.

No odds and no inferred injuries: DNP only from an explicit boxscore entry.
Regular-season final games strictly before today in New York are accepted.
Preserves 2025/26 history; makes no changes if nothing verifiable is available.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
START = pd.Timestamp("2026-10-20")
END = pd.Timestamp("2027-04-12")
BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
PLAYER_COLUMNS = ["game_id", "date", "player", "team", "opponent", "minutes", "pts", "ast", "reb", "status", "home"]
SCHEDULE_COLUMNS = ["game_id", "date", "home_team", "away_team"]
ALIASES = {"GS": "GSW", "NY": "NYK", "NO": "NOP", "SA": "SAS", "WSH": "WAS", "PHO": "PHX", "UTH": "UTA", "BRK": "BKN"}
KNOWN = {
    "ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW", "HOU", "IND",
    "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK", "OKC", "ORL", "PHI",
    "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
}


def team_code(team):
    value = str((team or {}).get("abbreviation") or "").upper().strip()
    canonical = ALIASES.get(value, value)
    if canonical not in KNOWN:
        raise ValueError(f"Unknown ESPN NBA team abbreviation {value!r}")
    return canonical


def minutes(s):
    if s is None or str(s).strip() in {"", "--", "DNP", "-", "nan"}:
        return 0.0
    value = str(s).strip()
    if ":" in value:
        match = re.fullmatch(r"(\d+):(\d{1,2})", value)
        if not match:
            raise ValueError(f"Invalid NBA minutes {value!r}")
        return int(match.group(1)) + int(match.group(2)) / 60
    return float(value)


def json_get(session, url, params):
    try:
        resp = session.get(url, params=params, timeout=25, headers={"Accept": "application/json", "User-Agent": "NBAPropsLab/1.0"})
        resp.raise_for_status()
        result = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"ESPN unavailable or invalid JSON: {url}: {exc}") from exc
    if not isinstance(result, dict):
        raise ValueError("Unexpected ESPN JSON response type")
    return result


def scoreboard_events(board, date):
    if "events" not in board or not isinstance(board["events"], list):
        raise ValueError(f"Missing ESPN scoreboard events list for {date}")
    result = []
    for event in board["events"]:
        if str(event.get("season", {}).get("type", "")) != "2":
            continue  # preseason / playoffs
        stat = event.get("status", {}).get("type", {})
        if not (stat.get("completed") is True and stat.get("name") == "STATUS_FINAL"):
            continue
        event_id = str(event.get("id", ""))
        if not re.fullmatch(r"\d{7,12}", event_id):
            raise ValueError("Invalid final ESPN event ID")
        comps = event.get("competitions", [])
        if len(comps) != 1:
            raise ValueError("Unrecognized ESPN competition data")
        entries = comps[0].get("competitors", [])
        if len(entries) != 2:
            raise ValueError("Final game does not contain exactly two competitors")
        identified = {str(x.get("homeAway", "")): team_code(x.get("team")) for x in entries}
        if set(identified) != {"home", "away"} or identified["home"] == identified["away"]:
            raise ValueError("Cannot identify both opponents")
        # Use ESPN event start time in NY; night matches might finish after midnight UTC.
        start = pd.to_datetime(event.get("date"), utc=True, errors="coerce")
        if pd.isna(start):
            raise ValueError("No event start timestamp for final NBA game")
        if start.tz_convert("America/New_York").strftime("%Y-%m-%d") != date:
            raise ValueError("ESPN score date does not agree with New York game date")
        result.append((event_id, identified["home"], identified["away"]))
    return result


def parse_boxscore(summary, game_id, day, home, away):
    players = summary.get("boxscore", {}).get("players")
    if not isinstance(players, list) or len(players) != 2:
        raise ValueError(f"Missing two boxscore teams for ESPN game {game_id}")
    rows = []
    used_teams = set()
    for section in players:
        team = team_code(section.get("team"))
        if team not in (home, away) or team in used_teams:
            raise ValueError("Boxscore team inconsistent with final scoreboard")
        used_teams.add(team)
        sections = section.get("statistics")
        if not isinstance(sections, list) or not sections:
            raise ValueError("Missing player statistic groups")
        team_played = 0
        names_seen = set()
        for group in sections:
            labels = [str(x).upper().strip() for x in group.get("names", [])]
            if not all(col in labels for col in ("MIN", "PTS", "AST", "REB")):
                # Some API versions use labels rather than names.
                labels = [str(x).upper().strip() for x in group.get("labels", [])]
            if not all(col in labels for col in ("MIN", "PTS", "AST", "REB")):
                raise ValueError("ESPN player-stat headers changed; stopping safely")
            for entry in group.get("athletes", []):
                if not isinstance(entry, dict):
                    continue
                person = entry.get("athlete") or {}
                name = (person.get("displayName") or person.get("fullName") or "").strip()
                if not name or name in names_seen:
                    continue
                stats = entry.get("stats") or []
                if len(stats) != len(labels):
                    if entry.get("didNotPlay") is True:
                        stats = None
                    else:
                        raise ValueError(f"Incomplete player stat row for {name}")
                if stats is None:
                    mins, pts, ast, reb = 0.0, 0, 0, 0
                    status = "DNP"
                else:
                    min_str = str(stats[labels.index("MIN")]).strip()
                    if min_str in {"DNP", "--", ""} and entry.get("didNotPlay") is not True:
                        # Incomplete/empty is not evidence of a DNP.
                        continue
                    mins = minutes(min_str)
                    status = "PLAYED" if mins > 0 else "DNP"
                    if status == "DNP" and entry.get("didNotPlay") is not True:
                        # A zero-minute row with boxscore stats may be explicit DNP.
                        if min_str not in {"0", "0:00", "DNP"}:
                            continue
                    if status == "PLAYED":
                        raw_vals = [str(stats[labels.index(k)]).strip() for k in ("PTS", "AST", "REB")]
                        if any(not re.fullmatch(r"\d+", x) for x in raw_vals):
                            raise ValueError(f"Malformed played stats for {name}: {raw_vals}")
                        pts, ast, reb = map(int, raw_vals)
                    else:
                        pts = ast = reb = 0
                rows.append({"game_id": game_id, "date": day, "player": name, "team": team,
                             "opponent": away if team == home else home, "minutes": mins,
                             "pts": pts, "ast": ast, "reb": reb, "status": status,
                             "home": team == home})
                names_seen.add(name)
                team_played += status == "PLAYED"
        if team_played < 7:
            raise ValueError(f"Incomplete final NBA boxscore: only {team_played} active players for {team}")
    if used_teams != {home, away}:
        raise ValueError("Incomplete NBA boxscore; both teams required")
    return rows


def fetch_espn(session, start_day, end_day):
    """Read [start_day,end_day) completed games; fail loudly on broken schema/network."""
    all_rows = []
    all_games = []
    for day in pd.date_range(start_day, end_day - pd.Timedelta(days=1), freq="D"):
        date_str = day.strftime("%Y-%m-%d")
        board = json_get(session, BASE + "/scoreboard", {"dates": day.strftime("%Y%m%d"), "limit": 100})
        for eid, home, away in scoreboard_events(board, date_str):
            summary = json_get(session, BASE + "/summary", {"event": eid})
            # Prefix ESPN IDs to keep existing historical NBA IDs distinct.
            gid = "espn_" + eid
            rows = parse_boxscore(summary, gid, date_str, home, away)
            all_rows.extend(rows)
            all_games.append({"game_id": gid, "date": date_str, "home_team": home, "away_team": away})
            time.sleep(0.20)  # Be polite to ESPN. Not guaranteed to work in GitHub Actions.
    return (pd.DataFrame(all_rows, columns=PLAYER_COLUMNS),
            pd.DataFrame(all_games, columns=SCHEDULE_COLUMNS))


def merge_new(old, new, keycols, sortcols):
    old = old.copy()
    new = new.copy()
    for frame in (old, new):
        frame["game_id"] = frame["game_id"].astype(str).str.strip()
        frame["date"] = pd.to_datetime(frame["date"], format="mixed", errors="raise").dt.strftime("%Y-%m-%d")
    return (pd.concat([old, new], ignore_index=True)
              .drop_duplicates(keycols, keep="last")
              .sort_values(sortcols)
              .reset_index(drop=True))


def update(directory=ROOT, *, session=None, today_ny=None):
    root = Path(directory)
    ppath = root / "data" / "real_player_games.csv"
    spath = root / "data" / "real_schedule.csv"
    if not ppath.exists() or not spath.exists():
        raise FileNotFoundError("Missing historical data/real_player_games.csv and data/real_schedule.csv")
    old_p = pd.read_csv(ppath, dtype={"game_id": str})
    old_s = pd.read_csv(spath, dtype={"game_id": str})
    if set(PLAYER_COLUMNS) - set(old_p) or set(SCHEDULE_COLUMNS) - set(old_s):
        raise ValueError("Unexpected existing CSV schema. No changes written")
    old_p = old_p[PLAYER_COLUMNS].copy()
    old_s = old_s[SCHEDULE_COLUMNS].copy()
    old_p["date"] = pd.to_datetime(old_p.date, format="mixed", errors="raise").dt.strftime("%Y-%m-%d")
    old_s["date"] = pd.to_datetime(old_s.date, format="mixed", errors="raise").dt.strftime("%Y-%m-%d")
    old_dates = pd.to_datetime(old_p["date"])
    past = old_p[old_dates < START].copy()
    past_count = len(past)
    if past_count < 100:
        raise ValueError("Historical 2025/26 data missing or suspiciously small. No changes")

    ny_today = pd.Timestamp(today_ny or datetime.now(ZoneInfo("America/New_York")).date()).normalize()
    upper = min(ny_today, END + pd.Timedelta(days=1))
    if upper <= START:
        print("WAITING_FOR_COMPLETED_GAMES: Before first NBA season game is completed")
        return "WAITING"
    existing_season = old_p[old_dates >= START].copy()
    if existing_season.empty:
        # Initial import capped at 21 days to avoid overwhelming ESPN on a late installation.
        lower = max(START, upper - pd.Timedelta(days=21))
    else:
        latest = pd.Timestamp(existing_season["date"].max())
        lower = max(START, latest - pd.Timedelta(days=3), upper - pd.Timedelta(days=21))
    if lower > START and existing_season.empty:
        print("WARNING: first import covers only last 21 days; older 2026/27 games require backfill")
    sess = session or requests.Session()
    new_p, new_s = fetch_espn(sess, lower, upper)
    if new_p.empty:
        print("WAITING_FOR_ESPN_FINAL_BOXSCORES: No verified completed regular-season games in queried window")
        return "WAITING"

    # Check for already existing same-date/opponent games from other providers.
    existing_games = old_s.copy()
    existing_games["date"] = pd.to_datetime(existing_games.date, format="mixed").dt.strftime("%Y-%m-%d")
    # If any same matchup exists with a different game ID, preserve ESPN output and
    # remove that game's old rows instead of duplicating the event.
    current_keys = set(zip(new_s.date, new_s.home_team, new_s.away_team))
    obsolete_ids = set(existing_games[existing_games.apply(lambda x: (x.date, x.home_team, x.away_team) in current_keys, axis=1)].game_id) - set(new_s.game_id)
    retained_p = old_p[~old_p.game_id.isin(obsolete_ids)].copy()
    retained_s = old_s[~old_s.game_id.isin(obsolete_ids)].copy()
    merged_p = merge_new(retained_p, new_p, ["game_id", "player", "team"], ["date", "game_id", "team", "player"])
    merged_s = merge_new(retained_s, new_s, ["game_id"], ["date", "game_id"])
    check_past = merged_p[pd.to_datetime(merged_p.date) < START]
    if len(check_past) != past_count:
        raise RuntimeError("Safety check failed: would change historic 2025/26 dataset")
    if merged_s.duplicated(["date", "home_team", "away_team"]).any():
        raise RuntimeError("Duplicate schedule event detected after merge")
    current = merged_p[pd.to_datetime(merged_p.date) >= START]
    if current.empty:
        raise RuntimeError("No current NBA players after merge")
    active = (current[current.status == "PLAYED"].sort_values(["date", "game_id"])
              .drop_duplicates(["player", "team"], keep="last")[["player", "team", "date", "status"]]
              .rename(columns={"date": "last_boxscore_date", "status": "boxscore_status"}))
    report = {"season": "2026/27", "source": "ESPN scoreboard + summary (public unofficial JSON)",
              "schema_checks_passed": True, "old_rows": past_count, "current_rows": len(current),
              "new_game_count": len(new_s), "current_games_total": len(merged_s) - len(existing_games[existing_games.date < "2026-10-20"]),
              "latest_game_date": str(current.date.max()),
              "current_roster_source": "observed players in completed boxscores only (not full rosters)",
              "odds_integrated": False}
    outputs = {
        ppath: merged_p.to_csv(index=False),
        spath: merged_s.to_csv(index=False),
        root / "data" / "current_roster_observed.csv": active.to_csv(index=False),
        root / "data" / "current_season_status.json": json.dumps(report, ensure_ascii=False, indent=2) + "\n",
    }
    # Stage validated outputs before replacing any file. Workflow commits only if changed.
    for path, contents in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.with_name(path.name + ".tmp").write_text(contents, encoding="utf8")
    for path in outputs:
        path.with_name(path.name + ".tmp").replace(path)
    print("UPDATED_2026_27_ESPN: " + json.dumps(report, ensure_ascii=False))
    return "UPDATED"


if __name__ == "__main__":
    argparse.ArgumentParser(description="Update NBA Props history from ESPN completed NBA games").parse_args()
    print("SYNC_RESULT:", update())
