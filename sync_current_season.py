"""Update NBA 2026/27 PTS/AST/REB history, preserving 2025/26.

Sources, in order:
  1. SportsDataverse public parquet (when published)
  2. Public NBA CDN schedule + FINAL boxscores (fallback)

No injury diagnosis is inferred from DNP. No betting odds are fetched.
Requires pandas, requests and pyarrow (for SportsDataverse parquet).
This is an unofficial use of publicly accessible endpoints, without a data SLA.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from sync_nba_history import BASE, get_asset, parse_assets, minutes_value

ROOT = Path(__file__).resolve().parent
START = pd.Timestamp('2026-10-20')
END = pd.Timestamp('2027-04-12')
PLAYER_COLUMNS = ['game_id','date','player','team','opponent','minutes','pts','ast','reb','status','home']
SCHEDULE_COLUMNS = ['game_id','date','home_team','away_team']
SCHEDULE_URL = 'https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json'
SCHEDULE_URL_FALLBACK = 'https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json'
BOX_URL = 'https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{game_id}.json'
HEADERS = {'User-Agent':'Mozilla/5.0 (compatible; NBAHistoryResearch/1.0)', 'Accept':'application/json'}
MAX_GAMES_PER_RUN = 35


def request_json(session, url):
    response = session.get(url, timeout=35, headers=HEADERS)
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError('NBA JSON: nieprawidłowa odpowiedź: '+url)
    return value


def fetch_sportsdataverse(session):
    box_url = f'{BASE}/nba_stats_player_boxscores/player_boxscores_2027.parquet'
    schedule_url = f'{BASE}/nba_stats_schedules/nba_schedule_2027.parquet'
    try:
        boxes = get_asset(box_url, session=session)
        games = get_asset(schedule_url, session=session)
    except requests.HTTPError as err:
        if err.response is not None and err.response.status_code == 404:
            print('SPORTSDATAVERSE_PENDING: Brak jednego z plików 2027; probuje NBA CDN.')
            return None
        raise
    try:
        return parse_assets(boxes, games)
    except ValueError as err:
        if 'Brak regular-season boxscore' in str(err):
            print('SPORTSDATAVERSE_PENDING: Brak regular-season; probuje NBA CDN.')
            return None
        raise


def parse_date(game, day):
    code = str(game.get('gameCode') or '')
    if re.match(r'^20\d{6}/', code):
        return pd.Timestamp(datetime.strptime(code[:8], '%Y%m%d')).normalize()
    for v in (game.get('gameDateEst'), game.get('gameDateTimeEst'), day.get('gameDate')):
        if v:
            ts = pd.to_datetime(v, errors='coerce')
            if not pd.isna(ts):
                return pd.Timestamp(ts).tz_localize(None).normalize() if ts.tzinfo else pd.Timestamp(ts).normalize()
    raise ValueError('Nieznana data NBA: '+repr(game.get('gameId')))


def schedule_games(data, now_ny):
    league = data.get('leagueSchedule', {})
    blocks = league.get('gameDates', [])
    if not isinstance(blocks, list) or not blocks:
        raise ValueError('NBA schedule: brak leagueSchedule.gameDates')
    today = pd.Timestamp(now_ny).normalize()
    out=[]
    for day in blocks:
        for g in (day.get('games') or []):
            gid = str(g.get('gameId','')).strip()
            if not re.fullmatch(r'00226\d{5}', gid):
                continue   # regular season 2026/27 only
            date = parse_date(g, day)
            if date < START or date > END or date > today:
                continue
            # Yesterday's games should already be final. For today's only status 3.
            if date == today and int(g.get('gameStatus') or 0) != 3:
                continue
            home = str((g.get('homeTeam') or {}).get('teamTricode') or '').upper()
            away = str((g.get('awayTeam') or {}).get('teamTricode') or '').upper()
            if not re.fullmatch(r'[A-Z]{2,3}', home) or not re.fullmatch(r'[A-Z]{2,3}', away) or home == away:
                raise ValueError('NBA schedule: nieprawidlowe druzyny dla '+gid)
            out.append({'game_id':gid, 'date':date.strftime('%Y-%m-%d'), 'home_team':home,'away_team':away})
    return sorted(out, key=lambda g:(g['date'],g['game_id']))


def parse_final_boxscore(payload, expected):
    g=payload.get('game') or {}
    gid = expected['game_id']
    if str(g.get('gameId')) != gid:
        raise ValueError('NBA boxscore: gameId nie zgadza sie '+gid)
    if int(g.get('gameStatus') or 0) != 3:
        return None  # Never store unfinished data.
    home = g.get('homeTeam') or {}
    away = g.get('awayTeam') or {}
    if str(home.get('teamTricode','')).upper()!=expected['home_team'] or str(away.get('teamTricode','')).upper()!=expected['away_team']:
        raise ValueError('NBA boxscore: sprzeczne druzyny '+gid)
    rows=[]
    for team,enemy,is_home in ((home,away,True),(away,home,False)):
        roster = team.get('players')
        if not isinstance(roster,list) or len(roster)<5:
            raise ValueError('NBA boxscore: brak pelnej listy zawodnikow '+gid)
        played_count=0
        for p in roster:
            first=str(p.get('firstName') or '').strip()
            family=str(p.get('familyName') or '').strip()
            name=(first+' '+family).strip() or str(p.get('name') or '').strip()
            if not name:
                raise ValueError('NBA boxscore: zawodnik bez nazwiska '+gid)
            stat=p.get('statistics') or {}
            raw_minutes=stat.get('minutes') or stat.get('minutesCalculated') or '0'
            minutes=minutes_value(raw_minutes)
            if not (0<=minutes<=80):
                raise ValueError('NBA boxscore: blad minut '+gid+' '+name)
            participated = minutes>0
            if participated:
                played_count+=1
            vals=[]
            for field in ('points','assists','reboundsTotal'):
                val = stat.get(field)
                if participated and (val is None or val == ''):
                    raise ValueError('NBA boxscore: brak statystyki '+field+' '+gid+' '+name)
                vals.append(int(val or 0))
            if any(v < 0 or v > 130 for v in vals):
                raise ValueError('NBA boxscore: nierealne statystyki '+gid+' '+name)
            rows.append({'game_id':gid,'date':expected['date'], 'player':name,
                         'team':str(team['teamTricode']).upper(), 'opponent':str(enemy['teamTricode']).upper(),
                         'minutes':round(minutes,4),'pts':vals[0],'ast':vals[1],'reb':vals[2],
                         'status':'PLAYED' if participated else 'DNP','home':is_home})
        if played_count<5:
            raise ValueError('NBA boxscore: za malo zawodnikow z minutami '+gid)
    return rows


def fetch_nba_cdn(session, existing_ids, now_ny, *, max_games=MAX_GAMES_PER_RUN):
    try:
        schedule=request_json(session,SCHEDULE_URL)
    except requests.HTTPError as err:
        if err.response is None or err.response.status_code != 404:
            raise
        print('NBA_CDN: terminarz V2_1 niedostepny, probuje V2.')
        schedule=request_json(session,SCHEDULE_URL_FALLBACK)
    candidates=schedule_games(schedule,now_ny)
    missing=[x for x in candidates if x['game_id'] not in existing_ids]
    if not missing:
        print('NBA_CDN: brak nowych finalnych spotkan do zapisania.')
        return None
    print(f'NBA_CDN: w kolejce {len(missing)} meczow, limit na uruchomienie {max_games}.')
    p_rows=[]; s_rows=[]; skipped=[]
    for item in missing[:max_games]:
        gid=item['game_id']
        try:
            doc=request_json(session,BOX_URL.format(game_id=gid))
        except requests.HTTPError as err:
            if err.response is not None and err.response.status_code == 404:
                skipped.append(gid)
                continue
            raise
        parsed=parse_final_boxscore(doc,item)
        if parsed is None:
            skipped.append(gid)
            continue
        p_rows.extend(parsed); s_rows.append(item)
    print(f'NBA_CDN_FETCH: added_games={len(s_rows)} skipped={len(skipped)}; example_skipped={skipped[:5]}')
    if not p_rows:
        if skipped:
            raise RuntimeError('NBA_CDN: terminarz ma mecze, ale brak dostepnych finalnych boxscore. Bez aktualizacji, sprawdz pozniej.')
        return None
    return pd.DataFrame(p_rows,columns=PLAYER_COLUMNS), pd.DataFrame(s_rows,columns=SCHEDULE_COLUMNS)


def merge_frames(old,current,keycols,sortcols):
    old=old.copy(); current=current.copy()
    for frame in (old,current):
        frame['game_id']=frame['game_id'].astype(str).str.strip()
        frame['date']=pd.to_datetime(frame['date'],format='mixed',errors='raise').dt.strftime('%Y-%m-%d')
    result=pd.concat([old,current],ignore_index=True)
    return result.drop_duplicates(keycols,keep='last').sort_values(sortcols).reset_index(drop=True)


def update(base_dir=ROOT,*, session=None, now_ny=None):
    base_dir=Path(base_dir)
    data=base_dir/'data'
    pf=data/'real_player_games.csv'; sf=data/'real_schedule.csv'
    if not pf.exists() or not sf.exists():
        raise FileNotFoundError('Brakuje data/real_player_games.csv lub data/real_schedule.csv')
    oldp=pd.read_csv(pf,dtype={'game_id':str})
    olds=pd.read_csv(sf,dtype={'game_id':str})
    if set(PLAYER_COLUMNS)-set(oldp.columns) or set(SCHEDULE_COLUMNS)-set(olds.columns):
        raise ValueError('Istniejaca baza ma niekompletne kolumny. Nie nadpisuje niczego.')
    oldp=oldp[PLAYER_COLUMNS].copy();olds=olds[SCHEDULE_COLUMNS].copy()
    oldp['date']=pd.to_datetime(oldp['date'],format='mixed',errors='raise').dt.strftime('%Y-%m-%d')
    olds['date']=pd.to_datetime(olds['date'],format='mixed',errors='raise').dt.strftime('%Y-%m-%d')
    session=session or requests.Session()
    now_ny=now_ny or datetime.now(ZoneInfo('America/New_York')).date()
    print('SEASON_SOURCE: probuje SportsDataverse, nastepnie NBA CDN (publiczne JSON).')
    result=fetch_sportsdataverse(session)
    source='SPORTSDATAVERSE'
    if result is None:
        source='NBA_CDN'
        result=fetch_nba_cdn(session,set(olds.game_id.astype(str)),now_ny)
    if result is None:
        print('WAITING_FOR_COMPLETED_GAMES: brak nowych finalnych meczow. Baza zachowana.')
        print('SYNC_RESULT: WAITING')
        return 'WAITING'
    p,s=result
    p=p[PLAYER_COLUMNS].copy();s=s[SCHEDULE_COLUMNS].copy()
    p['date']=pd.to_datetime(p['date'],format='mixed',errors='raise').dt.strftime('%Y-%m-%d')
    s['date']=pd.to_datetime(s['date'],format='mixed',errors='raise').dt.strftime('%Y-%m-%d')
    cutoff=pd.Timestamp(now_ny).normalize()
    s=s[(pd.to_datetime(s['date'])>=START)&(pd.to_datetime(s['date'])<=cutoff)&(pd.to_datetime(s['date'])<=END)].copy()
    ids=set(s.game_id.astype(str))
    p=p[(pd.to_datetime(p['date'])>=START)&p.game_id.astype(str).isin(ids)].copy()
    if p.empty:
        print('WAITING_FOR_COMPLETED_GAMES: bez zweryfikowanych boxscore; baza zachowana.')
        print('SYNC_RESULT: WAITING')
        return 'WAITING'
    mp=merge_frames(oldp,p,['game_id','player','team'],['date','game_id','team','player'])
    ms=merge_frames(olds,s,['game_id'],['date','game_id'])
    pastp=oldp[pd.to_datetime(oldp.date)<START]
    pasts=olds[pd.to_datetime(olds.date)<START]
    if len(mp[pd.to_datetime(mp.date)<START])!=len(pastp) or len(ms[pd.to_datetime(ms.date)<START])!=len(pasts):
        raise ValueError('Historia 2025/26 moglaby byc zmieniona; przerwano bez zapisu.')
    current=mp[pd.to_datetime(mp.date)>=START].copy()
    observed=(current.sort_values(['date','game_id']).drop_duplicates(['player','team'],keep='last')
              [['player','team','date','status']].rename(columns={'date':'last_boxscore_date','status':'boxscore_status'}))
    info={'season':'2026/27','source':source,'current_rows':int(len(current)),
          'current_games':int(len(ms[pd.to_datetime(ms.date)>=START])),
          'latest_game_date':str(current.date.max()),
          'past_2025_26_rows':int(len(pastp)),
          'current_roster_source':'only observed boxscore entries, NOT full verified NBA rosters',
          'odds_integrated':False}
    outputs={pf:mp.to_csv(index=False),sf:ms.to_csv(index=False),
             data/'current_roster_observed.csv':observed.to_csv(index=False),
             data/'current_season_status.json':json.dumps(info,indent=2,ensure_ascii=False)+'\n'}
    for path,contents in outputs.items():
        tmp=path.with_name(path.name+'.tmp')
        tmp.write_text(contents,encoding='utf-8')
    for path in outputs:
        path.with_name(path.name+'.tmp').replace(path)
    print('UPDATED_2026_27: '+json.dumps(info,ensure_ascii=False))
    print('SYNC_RESULT: UPDATED')
    return 'UPDATED'


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--smoke',action='store_true')
    args=parser.parse_args()
    if args.smoke:
        print('SMOKE_OK: NBA CDN fallback importer ready')
    else:
        update()
