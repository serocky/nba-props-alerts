"""Download past-season NBA player boxscores and schedule from SportsDataverse GitHub releases.
Free public assets; use in compliance with data provider conditions. Does NOT fetch odds.
Run manually, outside the time-critical injury monitor.
"""
from __future__ import annotations
import argparse
import io
from pathlib import Path
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
BASE = 'https://github.com/sportsdataverse/sportsdataverse-data/releases/download'


def minutes_value(s):
    if s is None or pd.isna(s): return 0.0
    if isinstance(s, (int, float)): return float(s)
    val = str(s).strip()
    if not val: return 0.0
    if val.startswith('PT'):
        import re
        match = re.fullmatch(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?', val)
        return (int(match.group(1) or 0)*60 + int(match.group(2) or 0) + float(match.group(3) or 0)/60) if match else 0.0
    if ':' in val:
        a, b = val.split(':', 1)
        return float(a) + float(b)/60
    return float(val)


def parse_assets(boxscores: pd.DataFrame, schedule: pd.DataFrame):
    """Public SportsDataverse nba_stats_player_boxscores + nba_stats_schedules schema.

    Preserve DNP only where that player has an explicit boxscore row. Never infer OUT from missing rows.
    """
    b = boxscores.copy()
    s = schedule.copy()
    b.columns = b.columns.str.lower()
    s.columns = s.columns.str.lower()
    required_b = {'game_id','team_tricode','first_name','family_name','minutes','points','assists','rebounds_total'}
    required_s = {'game_id','game_date','home_team_abbreviation','away_team_abbreviation'}
    if (required_b-set(b.columns)) or (required_s-set(s.columns)):
        raise ValueError('Zmieniony format SportsDataverse: brak kolumn: ' + repr(sorted((required_b-set(b.columns)) | (required_s-set(s.columns)))))
    s['game_id'] = s['game_id'].astype(str).str.strip()
    b['game_id'] = b['game_id'].astype(str).str.strip()
    s = s[s.game_id.str.match(r'^002\d{7}$')].copy() # NBA regular season only
    b = b[b.game_id.str.match(r'^002\d{7}$')].copy()
    s['date'] = pd.to_datetime(s['game_date'], errors='coerce').dt.strftime('%Y-%m-%d')
    s = s[s.date.notna()].drop_duplicates('game_id')
    s['home_team'] = s['home_team_abbreviation'].astype(str).str.upper()
    s['away_team'] = s['away_team_abbreviation'].astype(str).str.upper()
    b = b.merge(s[['game_id','date','home_team','away_team']],on='game_id',how='inner',validate='many_to_one')
    b['team'] = b['team_tricode'].astype(str).str.upper()
    b = b[(b.team==b.home_team)|(b.team==b.away_team)].copy()
    b['opponent'] = b.apply(lambda row: row.away_team if row.team==row.home_team else row.home_team,axis=1)
    b['home'] = b.team == b.home_team
    b['player'] = (b.first_name.fillna('').astype(str).str.strip() + ' ' + b.family_name.fillna('').astype(str).str.strip()).str.strip()
    b['minutes'] = b['minutes'].map(minutes_value)
    b['status'] = b.minutes.map(lambda m: 'PLAYED' if m>0 else 'DNP')
    b = b[b.player.ne('')].copy()
    for key, src in [('pts','points'),('ast','assists'),('reb','rebounds_total')]:
        b[key] = pd.to_numeric(b[src],errors='coerce')
    played=b.status=='PLAYED'
    b=b[~(played & (b[['pts','ast','reb']].isna().any(axis=1)))].copy()
    # If a player did not play, the DNP record is not proof of injury.
    b.loc[b.status=='DNP',['pts','ast','reb']]=0
    b=b.drop_duplicates(['game_id','player','team'],keep='last')
    b=b[['game_id','date','player','team','opponent','minutes','pts','ast','reb','status','home']].sort_values(['date','game_id','team','player'])
    sc=s[['game_id','date','home_team','away_team']].sort_values('date')
    if b.empty: raise ValueError('Brak regular-season boxscore w obu pobranych plikach')
    return b, sc


def get_asset(url: str, session=None) -> pd.DataFrame:
    session = session or requests.Session()
    r = session.get(url,timeout=90,headers={'User-Agent':'NBAPropsResearch/0.4 (historical CSV)'})
    r.raise_for_status()
    if not r.content[:4] == b'PAR1':
        raise RuntimeError('Źródło nie zwróciło poprawnego pliku Parquet: '+url)
    return pd.read_parquet(io.BytesIO(r.content))


def sync(season_end: int=2026, directory: Path | None=None, session=None):
    if season_end < 2020 or season_end > 2030: raise ValueError('year kończący sezon 2020–2030')
    directory=directory or ROOT/'data'
    b_url = f'{BASE}/nba_stats_player_boxscores/player_boxscores_{season_end}.parquet'
    s_url = f'{BASE}/nba_stats_schedules/nba_schedule_{season_end}.parquet'
    print('Pobieranie historycznych danych NBA (bez kursów):',b_url,s_url)
    b,s=parse_assets(get_asset(b_url,session),get_asset(s_url,session))
    directory.mkdir(parents=True, exist_ok=True)
    b.to_csv(directory/'real_player_games.csv',index=False)
    s.to_csv(directory/'real_schedule.csv',index=False)
    print(f'Zapisane: {len(b)} występów / {len(s)} meczów. Źródło: SportsDataverse, sezon kończący się {season_end}.')
    return b,s


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--season-end',type=int,default=2026)
    args=parser.parse_args()
    sync(args.season_end)
