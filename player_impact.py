"""Conservative, game-level NBA PTS/AST/REB impact analysis.
No simulated values, no fake live odds. With/without = player actually PLAYED or explicit DNP row.
Not a causal on/off minutes model; never extrapolate OUT from missing rows.
"""
from __future__ import annotations
from pathlib import Path
import pandas as pd
from engine import normalize_logs, normalize_schedule, teammate_splits, metric_forecast, back_to_back

ROOT = Path(__file__).resolve().parent
TEAMS = {
    'Atlanta Hawks':'ATL','Boston Celtics':'BOS','Brooklyn Nets':'BKN','Charlotte Hornets':'CHA',
    'Chicago Bulls':'CHI','Cleveland Cavaliers':'CLE','Dallas Mavericks':'DAL','Denver Nuggets':'DEN',
    'Detroit Pistons':'DET','Golden State Warriors':'GSW','Houston Rockets':'HOU','Indiana Pacers':'IND',
    'LA Clippers':'LAC','Los Angeles Clippers':'LAC','Los Angeles Lakers':'LAL','Memphis Grizzlies':'MEM',
    'Miami Heat':'MIA','Milwaukee Bucks':'MIL','Minnesota Timberwolves':'MIN','New Orleans Pelicans':'NOP',
    'New York Knicks':'NYK','Oklahoma City Thunder':'OKC','Orlando Magic':'ORL','Philadelphia 76ers':'PHI',
    'Phoenix Suns':'PHX','Portland Trail Blazers':'POR','Sacramento Kings':'SAC','San Antonio Spurs':'SAS',
    'Toronto Raptors':'TOR','Utah Jazz':'UTA','Washington Wizards':'WAS',
}


def historical_data(allow_demo:bool=False, root:Path=ROOT):
    bp=root/'data'/'real_player_games.csv'
    sp=root/'data'/'real_schedule.csv'
    if not bp.exists():
        if not allow_demo: return None,None,'BRAK_DANYCH'
        bp=root/'data'/'demo_player_games.csv'; sp=root/'data'/'demo_schedule.csv'
    if not sp.exists(): return None,None,'BRAK_TERMINARZA'
    return normalize_logs(pd.read_csv(bp)),normalize_schedule(pd.read_csv(sp)), 'DEMO' if 'demo_' in bp.name else 'REAL'


def _previous_season_coverage(logs:pd.DataFrame, player:str, day:str):
    """No future leakage; limit to rolling 180d and restrict to each player's same-team matches."""
    target=pd.Timestamp(day)
    return logs[(logs['date']<target)&(logs['date']>=target-pd.Timedelta(days=390))].copy()


def analyze_event(event:dict, logs:pd.DataFrame, schedule:pd.DataFrame, top:int=4):
    team=TEAMS.get(event.get('team',''),str(event.get('team','')).upper())
    day=str(event['game_date']); opponent=''
    matchup=str(event.get('matchup','')).upper()
    if '@' in matchup:
        away,home=matchup.split('@',1)
        opponent = home if team==away else away if team==home else ''
    hist=_previous_season_coverage(logs,team,day)
    hist=hist[hist['team']==team]
    absent=event['player']
    if hist.empty: return {'status':'NO_DATA','message':'Brak historycznych występów tej drużyny','candidates':[]}
    # Candidate teammates must have a reasonable number of game appearances.
    played=hist[(hist.status=='PLAYED')&(hist.minutes>=8)]
    candidates=[]
    for player,n in played.groupby('player').size().sort_values(ascending=False).items():
        # 3+ played games in each of two groups require at least 6 appearances.
        if player.casefold()==absent.casefold() or n<6: continue
        split=teammate_splits(hist,player,absent)
        wy=split['with']; wo=split['without']
        if wy['n']<3 or wo['n']<3: continue
        row={'player':player,'team':team,'vs':opponent,'b2b':None,
             'with_games':int(wy['n']),'without_games':int(wo['n']),'unknown_games':int(split['unknown']),
             'minutes_with':round(wy['minutes'],1),'minutes_without':round(wo['minutes'],1),
             'quality':('WIEKSZA_PROBA' if wy['n']>=12 and wo['n']>=8
                        else 'SREDNIA_PROBA' if wy['n']>=8 and wo['n']>=8
                        else 'SPEKULACYJNA_3_3')}
        b2b=back_to_back(schedule,team,day)
        row['b2b']=b2b
        for metric in ('pts','ast','reb'):
            f=metric_forecast(hist,player,metric,opponent,selected_b2b=b2b,absent_mate=absent)
            if 'error' in f: continue
            row[metric]={'with':round(wy[metric],2),'without':round(wo[metric],2),
                         'delta':round(wo[metric]-wy[metric],2),'forecast':round(f['mean'],2),
                         'base':round(f['base_mu'],2), 'opponent_n':f['opponent_n'],
                         'note':f['mate_note'],
                         'last5':round(f['mean_last5'],2), 'last10':round(f['mean_last10'],2),
                         'season_mean':round(f['mean_season'],2), 'sample_n':f['n']}
        if any(k in row for k in ('pts','ast','reb')):
            row['rank']=max(row[k]['delta'] for k in ('pts','ast','reb') if k in row)
            candidates.append(row)
    candidates.sort(key=lambda x:(x['rank'],x['without_games']),reverse=True)
    return {'status':'OK' if candidates else 'LOW_SAMPLE','team':team,'absent':absent,
            'candidates':candidates[:top], 'all_candidates':len(candidates),
            'message':'Porównanie całych meczów, bez przyczynowego on/off. Brak wiersza zawodnika ≠ kontuzja.'}


def telegram_impact(event:dict, logs:pd.DataFrame, schedule:pd.DataFrame)->str:
    """Historical context only. We do NOT verify current rosters or value bets here."""
    res=analyze_event(event,logs,schedule,top=3)
    if not res['candidates']:
        return ('\n\n📊 HISTORIA KOLEGÓW: Brak wystarczających meczów z/bez '
                '(wstępny filtr 3/3) lub brak danych dla tej drużyny. '
                'Nie wyciągaj wniosków o zakładach.')
    max_date=logs['date'].max().date().isoformat()
    parts=['\n\n📊 ZMIANA PRODUKCJI — HISTORYCZNE PORÓWNANIE (NIE TYP):',
           f'📅 Ostatni mecz w bazie: {max_date}. Skład obecnego sezonu NIEZWERYFIKOWANY.',
           'ℹ️ "Z/bez" oznacza całe mecze; DNP nie dowodzi kontuzji.']
    for x in res['candidates']:
        quality=('⚠️ SPEKULACYJNE 3/3 (bardzo mała próba)'
                 if x['quality']=='SPEKULACYJNA_3_3'
                 else '📘 ŚREDNIA PRÓBA (nadal niezwalidowana)'
                 if x['quality']=='SREDNIA_PROBA'
                 else '📗 WIĘKSZA PRÓBA (nadal niezwalidowana)')
        parts.append(f"\n👤 {x['player']} · z/bez: {x['with_games']}/{x['without_games']} meczów · {quality}")
        parts.append(f"Minuty: {x['minutes_with']:.1f} → {x['minutes_without']:.1f}")
        for metric,label in [('pts','PTS'),('ast','AST'),('reb','REB')]:
            m=x.get(metric)
            if m:
                line=f"{label}: {m['with']:.1f} → {m['without']:.1f} | model bazowy {m['forecast']:.1f}"
                gap=abs(m['forecast']-m['without'])
                if gap > max(2.0 if metric=='pts' else 0.9, 0.25 * max(m['without'],1.0)):
                    line += ' ⚠️ rozbieżność prognozy'
                parts.append(line)
    parts.append('⚠️ Minimum 3/3 służy WYKRYWANIU KANDYDATÓW, a nie ocenie prawdopodobieństwa. Model bazowy nie oznacza prognozy po absencji.')
    parts.append('⛔ Bez potwierdzenia składu, aktualnego matchup, kursu i backtestu nie jest to sygnał do gry.')
    return '\n'.join(parts)
