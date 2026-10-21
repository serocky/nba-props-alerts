"""Safely refresh 2026/27 NBA team rosters using ESPN's public web JSON.

No key, no betting odds. ESPN's endpoint is unofficial/undocumented and may
change or be rate-limited. Fail closed: NEVER write a partial league snapshot.

Output (only on successful 30-team download):
  data/current_rosters_2026_27.csv
  data/roster_comparison_2026_27.csv
  data/roster_metadata_2026_27.json
"""
from __future__ import annotations
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import time
import unicodedata

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
SEASON = 2027
# ESPN identifiers are NOT NBA team identifiers. Fixed list protects us from
# an incomplete /teams index response.
ESPN_TEAMS = {
    'ATL': 1, 'BOS': 2, 'BKN': 17, 'CHA': 30, 'CHI': 4,
    'CLE': 5, 'DAL': 6, 'DEN': 7, 'DET': 8, 'GSW': 9,
    'HOU': 10, 'IND': 11, 'LAC': 12, 'LAL': 13, 'MEM': 29,
    'MIA': 14, 'MIL': 15, 'MIN': 16, 'NOP': 3, 'NYK': 18,
    'OKC': 25, 'ORL': 19, 'PHI': 20, 'PHX': 21, 'POR': 22,
    'SAC': 23, 'SAS': 24, 'TOR': 28, 'UTA': 26, 'WAS': 27,
}
# The league box-score dataset uses canonical NBA abbreviations, while ESPN
# sometimes uses shorter abbreviations in roster responses. Keep the canonical
# output and explicitly allow only the known aliases for that team. ESPN team
# ID must still match the expected team ID independently.
ESPN_ABBR_ALIASES = {
    'GSW': {'GS'},
    'NOP': {'NO'},
    'NYK': {'NY'},
    'SAS': {'SA'},
    'UTA': {'UTAH'},
    'WAS': {'WSH'},
}

ENDPOINT = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/roster'
ROSTER_FIELDS = ('team', 'player', 'espn_player_id', 'position', 'roster_status', 'season_end', 'source_url')
COMPARE_FIELDS = ('player', 'team_2026_27', 'last_team_2025_26', 'classification', 'history_games_2025_26')


def norm_name(value: str) -> str:
    # This is only a conservative matching aid, NOT a player identifier.
    value = unicodedata.normalize('NFKD', str(value))
    value = ''.join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold().replace('’', "'")
    value = re.sub(r"['.´`]+", '', value)
    return re.sub(r'\s+', ' ', value).strip()


def roster_items(payload, expected_team: str, team_id: int, url: str):
    if not isinstance(payload, dict):
        raise ValueError('Odpowiedź nie jest obiektem JSON')
    team_obj = payload.get('team') or {}
    if not isinstance(team_obj, dict):
        raise ValueError('Brak obiektu team')
    if str(team_obj.get('id', '')).strip() != str(team_id):
        raise ValueError(f'Nieprawidłowy ESPN team ID dla {expected_team}')
    abbr = str(team_obj.get('abbreviation') or '').upper()
    if abbr != expected_team and abbr not in ESPN_ABBR_ALIASES.get(expected_team, set()):
        raise ValueError(f'Nieprawidłowy skrót drużyny: {expected_team} != {abbr}')
    season = payload.get('season')
    if isinstance(season, dict) and season.get('year') not in (None, SEASON):
        raise ValueError(f'API zwróciło inny sezon dla {expected_team}: {season}')
    raw = payload.get('athletes')
    if not isinstance(raw, list):
        raise ValueError(f'Brak listy athletes dla {expected_team}')
    items = []
    for group in raw:
        if not isinstance(group, dict):
            continue
        if isinstance(group.get('items'), list):
            items.extend(x for x in group['items'] if isinstance(x, dict))
        elif 'id' in group and ('displayName' in group or 'fullName' in group):
            items.append(group)
    results = []
    seen_ids = set()
    for athlete in items:
        player_id = str(athlete.get('id') or '').strip()
        name = str(athlete.get('displayName') or athlete.get('fullName') or '').strip()
        if not player_id or not name or not norm_name(name):
            raise ValueError(f'Niepełny zawodnik w {expected_team}')
        if player_id in seen_ids:
            continue
        seen_ids.add(player_id)
        pos = athlete.get('position') or {}
        status = athlete.get('status') or {}
        results.append({
            'team': expected_team, 'player': name, 'espn_player_id': player_id,
            'position': pos.get('abbreviation', '') if isinstance(pos, dict) else str(pos),
            'roster_status': status.get('type', status.get('name', '')) if isinstance(status, dict) else str(status),
            'season_end': str(SEASON), 'source_url': url,
        })
    if not (10 <= len(results) <= 26):
        raise ValueError(f'Nieprawdopodobna liczba zawodników w {expected_team}: {len(results)}')
    return results


def get_session():
    s = requests.Session()
    policy = Retry(total=2, backoff_factor=0.6, status_forcelist=[429, 500, 502, 503, 504],
                   allowed_methods=['GET'])
    s.mount('https://', HTTPAdapter(max_retries=policy))
    s.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; NBAPropsResearch/1.0; personal analytics)',
                      'Accept': 'application/json'})
    return s


def download_rosters(session=None, pause=0.1):
    session = session or get_session()
    all_rows = []
    for abbr, team_id in ESPN_TEAMS.items():
        url = ENDPOINT.format(team_id=team_id)
        response = session.get(url, params={'season': SEASON}, timeout=20)
        response.raise_for_status()
        group = roster_items(response.json(), abbr, team_id, f'{url}?season={SEASON}')
        all_rows.extend(group)
        print(f'ROSTER {abbr}: {len(group)}')
        if pause: time.sleep(pause)
    # 30 independently validated teams or nothing is committed.
    counts = {abbr: 0 for abbr in ESPN_TEAMS}
    for row in all_rows:
        counts[row['team']] += 1
    if len(counts) != 30 or any(v < 10 for v in counts.values()):
        raise RuntimeError('NIEPELNY_RAPORT_SKLADOW — niczego nie zapisano')
    ids = [x['espn_player_id'] for x in all_rows]
    if len(ids) != len(set(ids)):
        raise RuntimeError('Jeden identyfikator ESPN występuje w dwóch drużynach; zapis wstrzymany')
    return sorted(all_rows, key=lambda x: (x['team'], norm_name(x['player'])))


def last_season_history(path: Path):
    # Player identities in historical dataset are names only. Treat ambiguous
    # same-name mappings as uncertain rather than guessing.
    history = {}
    if not path.exists():
        return history
    with path.open(newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or not {'player', 'team', 'date', 'status'}.issubset(reader.fieldnames):
            raise ValueError('Historyczny CSV nie ma wymaganych kolumn')
        for row in reader:
            day = str(row.get('date') or '')[:10]
            if not ('2025-10-01' <= day <= '2026-07-31'):
                continue
            if (row.get('status') or '').upper() != 'PLAYED':
                continue
            key = norm_name(row.get('player', ''))
            if not key:
                continue
            group = history.setdefault(key, {'teams': {}, 'names': set()})
            group['names'].add(row['player'])
            info = group['teams'].setdefault(row['team'], {'last_date': '', 'games': set()})
            info['games'].add(str(row.get('game_id') or (day + '_' + row['team'])))
            info['last_date'] = max(info['last_date'], day)
    return history


def compare(rosters, history):
    name_frequency = {}
    for row in rosters:
        k = norm_name(row['player'])
        name_frequency[k] = name_frequency.get(k, 0) + 1
    compared = []
    for row in rosters:
        key = norm_name(row['player'])
        prior = history.get(key)
        if name_frequency[key] > 1:
            last, classification, games = '', 'AMBIGUOUS_NAME', 0
        elif prior is None:
            last, classification, games = '', 'NO_2025_26_HISTORY', 0
        else:
            teams = prior['teams']
            best = sorted(teams, key=lambda team: (teams[team]['last_date'], len(teams[team]['games'])), reverse=True)[0]
            last, games = best, sum(len(x['games']) for x in teams.values())
            classification = 'SAME_TEAM' if last == row['team'] else 'CHANGED_TEAM'
        compared.append({'player': row['player'], 'team_2026_27': row['team'],
                         'last_team_2025_26': last, 'classification': classification,
                         'history_games_2025_26': games})
    return compared


def csv_text(rows, fields):
    out = io.StringIO(newline='')
    w = csv.DictWriter(out, fieldnames=fields)
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


def sync(folder: Path = ROOT / 'data', session=None, pause=0.1, now=None):
    roster = download_rosters(session=session, pause=pause)  # May raise. No writes yet.
    historical = last_season_history(folder / 'real_player_games.csv')
    compared = compare(roster, historical)
    timestamp = (now or datetime.now(timezone.utc)).isoformat(timespec='seconds')
    stable_content = '\n'.join(f"{x['team']}|{x['espn_player_id']}|{x['player']}" for x in roster)
    metadata = {
        'status': 'VERIFIED_30_TEAMS_FROM_ESPN',
        'season_end': SEASON, 'fetched_at_utc': timestamp,
        'teams': len(ESPN_TEAMS), 'players': len(roster),
        'roster_sha256': hashlib.sha256(stable_content.encode()).hexdigest(),
        'source': 'ESPN public roster endpoint (unofficial; may change)',
        'warning': 'Roster ≠ confirmation of game availability; verify injuries separately.',
    }
    folder.mkdir(parents=True, exist_ok=True)
    output = {
        'current_rosters_2026_27.csv': csv_text(roster, ROSTER_FIELDS),
        'roster_comparison_2026_27.csv': csv_text(compared, COMPARE_FIELDS),
        'roster_metadata_2026_27.json': json.dumps(metadata, ensure_ascii=False, indent=2) + '\n',
    }
    # Validate all datasets before touching existing snapshot.
    for name, content in output.items():
        if not content.strip():
            raise ValueError(f'Pusty plik: {name}')
    for name, content in output.items():
        tmp = folder / (name + '.tmp')
        tmp.write_text(content, encoding='utf-8')
    for name in output:
        (folder / (name + '.tmp')).replace(folder / name)
    kinds = {}
    for row in compared:
        kinds[row['classification']] = kinds.get(row['classification'], 0) + 1
    print('ROSTERS_OK', json.dumps({'teams': 30, 'players': len(roster), 'classes': kinds}, ensure_ascii=False))
    print('WAZNE: To tylko bieżące przypisanie drużyn. Nie potwierdza absencji ani przewagi nad kursem.')
    return metadata


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='NBA 2026/27 roster validation')
    parser.add_argument('--data-dir', default=str(ROOT / 'data'))
    args = parser.parse_args()
    sync(folder=Path(args.data_dir))
