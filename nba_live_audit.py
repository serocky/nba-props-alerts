"""NBA Props Lab: read-only audit of REAL game logs, roster data and injury snapshot.

No network requests, no synthetic observations, no Telegram messages, no state updates.
Run after each deployment to establish if real impact analysis can be attached.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


def validate(base: Path, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    errors: list[str] = []
    warnings: list[str] = []
    print('NBA_REAL_DATA_AUDIT_V1')
    print('UTC:', now.strftime('%Y-%m-%dT%H:%M:%SZ'))
    from player_impact import historical_data
    from roster_guard import load_verified_rosters, roster_gated_impact
    from official_injuries import NBA_TEAMS

    logs, schedule, source = historical_data(allow_demo=False, root=base)
    if source != 'REAL' or logs is None or schedule is None or logs.empty:
        errors.append('BRAK_PRAWDZIWEJ_HISTORII: wymagane data/real_player_games.csv i real_schedule.csv')
    else:
        played = logs[logs['status'] == 'PLAYED']
        if played.empty:
            errors.append('BRAK_WYSTEPOW_PLAYED')
        last = logs['date'].max()
        print('HISTORY_SOURCE: REAL')
        print('HISTORY_ROWS:', len(logs))
        print('HISTORY_GAMES:', int(logs['game_id'].nunique()))
        print('HISTORY_PLAYERS:', int(logs['player'].nunique()))
        print('HISTORY_LAST_GAME:', last.date().isoformat())
        old = int(((logs['date'] >= '2025-10-01') & (logs['date'] < '2026-07-01')).sum())
        new = int((logs['date'] >= '2026-10-01').sum())
        print('HISTORY_2025_26_ROWS:', old)
        print('HISTORY_2026_27_ROWS:', new)
        if new == 0:
            warnings.append('BRAK_DANYCH_2026_27: analizy mogą bazować tylko na sezonie 2025/26')
        if last > now.replace(tzinfo=None) + timedelta(days=1):
            errors.append('HISTORIA_Z_PRZYSZLOSCI: zweryfikuj daty CSV')

    try:
        rosters = load_verified_rosters(data_dir=base / 'data', now=now)
        print('ROSTERS_VERIFIED:', len(rosters))
        print('ROSTER_PLAYERS:', sum(map(len, rosters.values())))
    except Exception as exc:
        rosters = None
        errors.append('ROSTERS_INVALID: ' + str(exc)[:240])

    state_path = base / 'runtime' / 'official_snapshot.json'
    statuses = []
    if not state_path.is_file():
        warnings.append('BRAK_SNAPSHOTU_KONTUZJI: monitor musi wykonać przynajmniej jeden pełny odczyt')
    else:
        try:
            state = json.loads(state_path.read_text(encoding='utf-8'))
            statuses = list((state.get('status_rows') or {}).values())
            if not state.get('initialized'):
                warnings.append('NIEZAINICJALIZOWANY_SNAPSHOT_KONTUZJI')
            meta = state.get('metadata') or {}
            print('INJURY_REPORT_AT:', meta.get('report_at', 'UNKNOWN'))
            print('INJURY_STATUS_ROWS:', len(statuses))
            print('INJURY_PENDING:', len(state.get('pending', [])))
            if not statuses:
                warnings.append('SNAPSHOT_BEZ_STATUSOW')
        except (ValueError, TypeError) as exc:
            errors.append('SNAPSHOT_INVALID: ' + type(exc).__name__)

    # A preview comes only from a real official-report snapshot; do not claim a new OUT.
    if rosters is not None and logs is not None and schedule is not None and not logs.empty:
        from sync_nba_rosters import norm_name
        pick = None
        for item in statuses:
            team = NBA_TEAMS.get(str(item.get('team', '')).casefold())
            if item.get('status') != 'OUT' or team not in rosters:
                continue
            if norm_name(item.get('player', '')) not in rosters[team]:
                continue
            pick = item
            break
        if pick:
            print('REAL_REPORT_PREVIEW_ONLY:', pick['game_date'], pick['team'], pick['player'])
            print('NOTE: preview is old/current recorded OUT, NOT a new alert')
            sample = roster_gated_impact(pick, logs, schedule, data_dir=base / 'data', now=now)
            print('IMPACT_PREVIEW:', sample[:2500].replace('\n', ' | '))
        else:
            warnings.append('BRAK_OUT_W_ZWERYFIKOWANYM_SKLADZIE: nie można wykonać podglądu')
    for note in warnings:
        print('WARNING:', note)
    for note in errors:
        print('ERROR:', note)
    print('AUDIT_RESULT:', 'PASS' if not errors else 'FAIL')
    return not errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=str(Path(__file__).resolve().parent))
    args = parser.parse_args()
    ok = validate(Path(args.root))
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
