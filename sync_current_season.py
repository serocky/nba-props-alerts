"""NBA 2026/27 daily refresh (public SportsDataverse parquet releases).

Keeps season 2025/26 intact, merges 2026/27 REGULAR-SEASON finished
boxscores into data/real_player_games.csv and data/real_schedule.csv.
Does NOT infer injury from DNP, and does NOT fetch betting odds.

Before current boxscores are published, exits 0 without changing files.
Run once daily after the source's normal morning refresh.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import argparse
import json
import sys

import pandas as pd
import requests

from sync_nba_history import BASE, get_asset, parse_assets

ROOT = Path(__file__).resolve().parent
START = pd.Timestamp('2026-10-20')
END = pd.Timestamp('2027-04-12')
PLAYER_COLUMNS = ['game_id','date','player','team','opponent','minutes','pts','ast','reb','status','home']
SCHEDULE_COLUMNS = ['game_id','date','home_team','away_team']


def fetch_2027(session=None):
    """Return (players, schedule) or None when source is not ready.

    Schema errors and transient server errors must FAIL loudly rather than erase data.
    """
    session = session or requests.Session()
    b_url = f'{BASE}/nba_stats_player_boxscores/player_boxscores_2027.parquet'
    s_url = f'{BASE}/nba_stats_schedules/nba_schedule_2027.parquet'
    try:
        boxscores = get_asset(b_url, session=session)
        schedule = get_asset(s_url, session=session)
    except requests.HTTPError as ex:
        if getattr(ex.response, 'status_code', None) == 404:
            return None
        raise
    try:
        return parse_assets(boxscores, schedule)
    except ValueError as ex:
        if 'Brak regular-season boxscore' in str(ex):
            return None
        raise


def restrict_past_games(boxes, schedule, *, today_ny=None):
    """Never use today's unfinished/partial games or future boxscores."""
    today_ny = pd.Timestamp(today_ny or datetime.now(ZoneInfo('America/New_York')).date()).normalize()
    cutoff = min(today_ny, END)
    b = boxes.copy()
    s = schedule.copy()
    b['date'] = pd.to_datetime(b['date'], errors='raise')
    s['date'] = pd.to_datetime(s['date'], errors='raise')
    valid_s = s[(s.date >= START) & (s.date < cutoff)].copy()
    valid_ids = set(valid_s.game_id.astype(str))
    b = b[(b.date >= START) & (b.date < cutoff) & (b.game_id.astype(str).isin(valid_ids))].copy()
    if b.empty: return None
    # Store only actually evidenced games; don't accidentally treat scheduled games as final.
    valid_s = valid_s[valid_s.game_id.astype(str).isin(set(b.game_id.astype(str)))].copy()
    return b, valid_s


def merge_old_and_new(old, current, *, keycols, sortcols):
    old = old.copy()
    current = current.copy()
    old['game_id'] = old.game_id.astype(str).str.strip()
    current['game_id'] = current.game_id.astype(str).str.strip()
    for frame in (old, current):
        frame['date'] = pd.to_datetime(frame['date'], format='mixed', errors='raise').dt.strftime('%Y-%m-%d')
    merged = pd.concat([old, current], ignore_index=True)
    return (merged.drop_duplicates(keycols, keep='last')
            .sort_values(sortcols).reset_index(drop=True))


def update(base_dir=ROOT, *, session=None, today_ny=None):
    base_dir = Path(base_dir)
    data = base_dir / 'data'
    players_csv = data / 'real_player_games.csv'
    schedule_csv = data / 'real_schedule.csv'
    if not players_csv.exists() or not schedule_csv.exists():
        raise FileNotFoundError('Najpierw pobierz historię 2025/26: brakuje data/real_*.csv')
    old_players = pd.read_csv(players_csv, dtype={'game_id':str})
    old_schedule = pd.read_csv(schedule_csv, dtype={'game_id':str})
    old_players['date'] = pd.to_datetime(old_players['date'], format='mixed', errors='raise').dt.strftime('%Y-%m-%d')
    old_schedule['date'] = pd.to_datetime(old_schedule['date'], format='mixed', errors='raise').dt.strftime('%Y-%m-%d')
    if set(PLAYER_COLUMNS) - set(old_players.columns) or set(SCHEDULE_COLUMNS) - set(old_schedule.columns):
        raise ValueError('Niepoprawny format istniejącej bazy. Przerwano, bez nadpisywania.')
    result = fetch_2027(session)
    if result is None:
        print('WAITING_FOR_2026_27: Brak opublikowanych boxscore; baza 2025/26 bez zmian.')
        return 'WAITING'
    filtered = restrict_past_games(*result, today_ny=today_ny)
    if filtered is None:
        print('WAITING_FOR_COMPLETED_GAMES: Brak potwierdzonych zakończonych meczów 2026/27.')
        return 'WAITING'
    new_players, new_schedule = filtered
    # Keep only known shared columns. Preserve 2025/26 records in primary files.
    merged_players = merge_old_and_new(old_players[PLAYER_COLUMNS], new_players[PLAYER_COLUMNS],
                                       keycols=['game_id','player','team'], sortcols=['date','game_id','team','player'])
    merged_schedule = merge_old_and_new(old_schedule[SCHEDULE_COLUMNS], new_schedule[SCHEDULE_COLUMNS],
                                        keycols=['game_id'], sortcols=['date','game_id'])
    # Do not silently lose any past-season rows/games.
    past = old_players[pd.to_datetime(old_players.date) < START]
    check = merged_players[pd.to_datetime(merged_players.date) < START]
    if len(check) != len(past):
        raise ValueError('BŁĄD: przetworzenie usunęło dane 2025/26. Przerwano.')
    current = merged_players[pd.to_datetime(merged_players.date) >= START]
    # Confirmed-current-season listed players, NOT official roster of all teams.
    active = (current.sort_values(['date','game_id'])
              .drop_duplicates('player', keep='last')[['player','team','date','status']]
              .rename(columns={'date':'last_boxscore_date','status':'boxscore_status'}))
    status = {
        'season': '2026/27', 'old_rows':len(past),
        'current_rows':len(current), 'current_games':len(new_schedule),
        'latest_game_date':str(current.date.max()),
        'current_roster_source':'observed boxscore entries only; not verified complete NBA roster',
        'odds_integrated':False,
    }
    # Stage everything before replacing anything. Same non-atomic batch as existing workflow,
    # with validated inputs and a retry-safe commit; no destructive source fallback.
    data.mkdir(parents=True, exist_ok=True)
    objects = {
        players_csv: merged_players.to_csv(index=False),
        schedule_csv: merged_schedule.to_csv(index=False),
        data/'current_roster_observed.csv': active.to_csv(index=False),
        data/'current_season_status.json': json.dumps(status,ensure_ascii=False,indent=2)+'\n',
    }
    for path, content in objects.items():
        temp = path.with_name(path.name+'.tmp')
        temp.write_text(content, encoding='utf-8')
    for path in objects:
        path.with_name(path.name+'.tmp').replace(path)
    print('UPDATED_2026_27:',json.dumps(status,ensure_ascii=False))
    return 'UPDATED'


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--smoke',action='store_true',help='Validate program import without network')
    args=parser.parse_args()
    if args.smoke:
        print('SMOKE_OK: season update code imported successfully')
    else:
        print('SYNC_RESULT:',update())
