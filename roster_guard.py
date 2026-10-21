"""Conservative NBA 2026/27 roster gate for injury alert impact context.

Only ESPN rosters synced by sync_nba_rosters.py after validation of 30 teams
may define potential teammates. The incoming official NBA injury event remains
an independent alert even when roster verification fails.

This module offers historical context, never live betting recommendations.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path

from sync_nba_rosters import ESPN_TEAMS, norm_name
from player_impact import TEAMS, analyze_event

ROOT = Path(__file__).resolve().parent
MAX_ROSTER_AGE_HOURS = 48
MIN_WITH_GAMES = 12
MIN_WITHOUT_GAMES = 8


def _hold(reason: str) -> str:
    return ("\n\n🛡️ ANALIZA KOLEGÓW WSTRZYMANA: " + reason +
            "\nℹ️ Alert OUT pozostaje ważną informacją, ale bez potwierdzonej analizy PTS/AST/REB.")


def load_verified_rosters(data_dir: Path = ROOT / 'data', now: datetime | None = None) -> dict[str, dict[str, str]]:
    """Raise ValueError if current rosters are stale, incomplete or altered."""
    now = now or datetime.now(timezone.utc)
    meta_path = data_dir / 'roster_metadata_2026_27.json'
    csv_path = data_dir / 'current_rosters_2026_27.csv'
    if not meta_path.is_file() or not csv_path.is_file():
        raise ValueError('Brak zweryfikowanej bazy składów 2026/27')
    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    if (meta.get('status') != 'VERIFIED_30_TEAMS_FROM_ESPN'
            or str(meta.get('season_end')) != '2027'
            or meta.get('teams') != 30):
        raise ValueError('Brak potwierdzenia pełnych 30 składów sezonu 2026/27')
    updated = datetime.fromisoformat(str(meta['fetched_at_utc']).replace('Z', '+00:00'))
    if updated.tzinfo is None:
        raise ValueError('Brak strefy czasowej aktualizacji składów')
    delta = now - updated.astimezone(timezone.utc)
    if not timedelta(0) <= delta <= timedelta(hours=MAX_ROSTER_AGE_HOURS):
        raise ValueError('Składy są nieaktualne lub mają przyszłą datę aktualizacji')
    with csv_path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if not {'team', 'player', 'espn_player_id', 'season_end'}.issubset(set(reader.fieldnames or [])):
            raise ValueError('Niepoprawne kolumny w pliku składów')
        rows = list(reader)
    if len(rows) != meta.get('players'):
        raise ValueError('Liczba zawodników nie zgadza się z metadanymi')
    counts = {team: 0 for team in ESPN_TEAMS}
    ids = set()
    result: dict[str, dict[str, str]] = {team: {} for team in ESPN_TEAMS}
    for row in rows:
        team = row['team'].strip().upper()
        pid = row['espn_player_id'].strip()
        name = row['player'].strip()
        if team not in counts or not pid or not name or row.get('season_end', '').strip() != '2027':
            raise ValueError('Nieprawidłowa drużyna, sezon lub nazwisko w składach')
        if pid in ids or norm_name(name) in result[team]:
            raise ValueError('Zduplikowany identyfikator lub nazwisko zawodnika')
        ids.add(pid)
        counts[team] += 1
        result[team][norm_name(name)] = name
    if len(result) != 30 or any(not 10 <= n <= 26 for n in counts.values()):
        raise ValueError('Brak kompletnego składu jednej lub kilku drużyn')
    stable = '\n'.join(f"{r['team']}|{r['espn_player_id']}|{r['player']}"
                       for r in sorted(rows, key=lambda x: (x['team'], norm_name(x['player']))))
    digest = hashlib.sha256(stable.encode()).hexdigest()
    if digest != meta.get('roster_sha256'):
        raise ValueError('Nieprawidłowa suma kontrolna składów')
    return result


def roster_gated_impact(event: dict, logs, schedule, data_dir: Path = ROOT / 'data',
                        now: datetime | None = None) -> str:
    """Compose vetted, non-causal history only for current verified teammates."""
    try:
        rosters = load_verified_rosters(data_dir=data_dir, now=now)
    except (ValueError, KeyError, OSError, TypeError, json.JSONDecodeError) as exc:
        return _hold(str(exc))
    team = TEAMS.get(event.get('team', ''), str(event.get('team', '')).upper())
    if team not in rosters:
        return _hold('Drużyna z raportu nie występuje w aktualnych składach')
    absent = norm_name(str(event.get('player', '')))
    if absent not in rosters[team]:
        return _hold('Nie znaleziono nieobecnego zawodnika w aktualnym składzie ESPN; wymaga weryfikacji')
    if logs is None or schedule is None:
        return _hold('Brak historycznych meczów')
    try:
        # Restrict league history to pre-event games only, even if the source dataset is ahead.
        hist = logs[logs['date'] < event['game_date']]
        res = analyze_event(event, hist, schedule, top=100)
    except (KeyError, ValueError, TypeError) as exc:
        return _hold('Nie można policzyć historycznych porównań: ' + type(exc).__name__)
    selected = []
    for candidate in res.get('candidates', []):
        # Explicitly prohibit a former teammate, even if historical team matches.
        if norm_name(candidate['player']) not in rosters[team]:
            continue
        if (candidate['with_games'] < MIN_WITH_GAMES
                or candidate['without_games'] < MIN_WITHOUT_GAMES):
            continue
        # Only describe credible-sized increases; do not imply causal effect.
        increments = {
            metric: (candidate.get(metric, {}).get('delta') or 0)
            for metric in ('pts', 'ast', 'reb')
        }
        if not (increments['pts'] >= 1.5 or increments['ast'] >= 0.5 or increments['reb'] >= 0.8):
            continue
        selected.append(candidate)
        if len(selected) == 3:
            break
    if not selected:
        return _hold('Nie ma kolegów z aktualnego składu spełniających próg 12/8 meczów i dodatniej różnicy')
    pre_event = hist['date'].max()
    latest = pre_event.date().isoformat() if pre_event is not None else 'nieznana'
    lines = ["\n\n📊 HISTORIA WYŁĄCZNIE Z POTWIERDZONYM SKŁADEM 2026/27 (NIE TYP):",
             f"🏀 Drużyna: {team} | ostatni mecz w bazie: {latest}",
             'Wymagane: ≥12 spotkań z kolegą i ≥8 bez kolegi.',
             'DNP oznacza brak występu, nie potwierdzoną kontuzję.']
    for c in selected:
        lines.append(f"\n👤 {c['player']} | z/bez: {c['with_games']}/{c['without_games']} meczów")
        lines.append(f"Minuty z/bez: {c['minutes_with']:.1f} → {c['minutes_without']:.1f}")
        for stat, label in (('pts', 'PTS'), ('ast', 'AST'), ('reb', 'REB')):
            x = c.get(stat)
            if x is None:
                continue
            lines.append(f"{label}: {x['with']:.1f} → {x['without']:.1f} | szacunek {x['forecast']:.1f}")
    lines.extend(["⚠️ To porównanie historyczne, NIE efekt przyczynowy absencji.",
                  "⛔ Bez aktualnego kursu i walidacji modelu nie jest to sygnał do gry."])
    return '\n'.join(lines)
