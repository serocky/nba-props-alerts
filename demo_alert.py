"""Entirely synthetic scenario; requires no data subscription or secret."""
from __future__ import annotations
from datetime import datetime, timezone
import pandas as pd
from pathlib import Path
from alert_logic import evaluate_prop, formatted_alert, parse_ts, decimal_odds
from alert_store import connect, save_statuses, iso_now

root = Path(__file__).parent
logs = pd.read_csv(root / 'data' / 'demo_player_games.csv')
from engine import normalize_logs
logs = normalize_logs(logs)
conn = connect(':memory:')
base = dict(game_id='DEMO-GAME', player_id=999, missing='Jordan Demo', team='LAB', team_id=123,
            home='LAB', away='NYK', opponent='NYK', tip='2026-11-01T22:00:00Z',
            season=2026, detail='FIKCYJNY TEST: żadna rzeczywista kontuzja')
s, bootstrap = save_statuses(conn, [{**base, 'status': 'QUESTIONABLE'}])
assert bootstrap and len(s) == 0
s, bootstrap = save_statuses(conn, [{**base, 'status': 'OUT'}])
assert not bootstrap and len(s) == 1
example = s[0]
now = datetime.now(timezone.utc)
# Wide invented odds used to prove flow only; NOT a real sportsbook market.
signals = []
for market_name, point in [('points', 27.5), ('assists', 5.5), ('rebounds', 5.5)]:
    prop = {'prop_type': market_name, 'line_value': str(point), 'vendor': 'DEMO',
            'market': {'type': 'over_under', 'over_odds': 135, 'under_odds': -110},
            'updated_at': now.isoformat()}
    score = evaluate_prop(logs, player='Alex Sample', missing='Jordan Demo', opponent='NYK',
                          prop=prop, b2b=False, now=now, tax=0.12, min_ev=0)
    if score:
        signals.append(score)
print('DEMO — potwierdzono zmianę QUESTIONABLE -> OUT.')
if signals:
    print(formatted_alert(example, max(signals, key=lambda c: c['ev'])))
else:
    print('DEMO — brak sygnału przy przykładowych liniach; to prawidłowy wynik filtrów bezpieczeństwa.')
