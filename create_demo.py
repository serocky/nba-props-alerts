"""Deterministyczne fikcyjne dane do sprawdzania działania aplikacji."""
from datetime import date, timedelta
from pathlib import Path
import random
import pandas as pd

random.seed(318)
out = Path(__file__).resolve().parent / "data"
out.mkdir(exist_ok=True)
players = [("Alex Sample", 27.8, 6.1, 5.8, 35), ("Jordan Demo", 19.1, 8.5, 3.6, 33),
           ("Taylor Fiction", 13.0, 4.1, 6.8, 29), ("Sam Example", 10.0, 2.7, 4.5, 25)]
opps = ["NYK", "BOS", "MIL", "CHI", "CLE", "MIA", "ORL", "PHI"]
rows, games = [], []
start = date(2025, 11, 2)
for i in range(45):
    # Wszystkie daty i nazwiska są fikcyjne; ligowe kody drużyn tylko w demonstracji UI.
    day = start + timedelta(days=i * 2 + (i // 6))
    opponent = opps[i % len(opps)]
    home = i % 2 == 0
    game_id = f"DEMO-{i + 1:03d}"
    games.append({"game_id": game_id, "date": day.isoformat(), "home_team": "LAB" if home else opponent,
                  "away_team": opponent if home else "LAB"})
    mate_out = i % 5 in (1, 2)
    for j, (name, points, ast, reb, minutes) in enumerate(players):
        if j == 1 and mate_out:
            status = "OUT"
            rows.append({"game_id": game_id, "date": day, "player": name, "team": "LAB", "opponent": opponent,
                         "minutes": "", "pts": "", "ast": "", "reb": "", "status": status, "home": home})
            continue
        boost = (3.8 if j == 0 else 0) if mate_out else 0
        mins = max(9, round(random.gauss(minutes + (1.3 if j == 0 and mate_out else 0), 2.4), 1))
        rows.append({"game_id": game_id, "date": day, "player": name, "team": "LAB", "opponent": opponent,
                     "minutes": mins, "pts": max(0, round(random.gauss(points + boost, 5))),
                     "ast": max(0, round(random.gauss(ast + (1 if j == 0 and mate_out else 0), 2.1))),
                     "reb": max(0, round(random.gauss(reb, 2.3))), "status": "PLAYED", "home": home})
# Dzień następny po ostatnim spotkaniu = B2B, wyłącznie DEMO
last = date.fromisoformat(str(games[-1]["date"]))
games.append({"game_id": "DEMO-NEXT", "date": (last + timedelta(days=1)).isoformat(),
              "home_team": "NYK", "away_team": "LAB"})
pd.DataFrame(rows).to_csv(out / "demo_player_games.csv", index=False)
pd.DataFrame(games).to_csv(out / "demo_schedule.csv", index=False)
pd.DataFrame([{"player": "Jordan Demo", "team": "LAB", "status": "OUT",
               "reason": "WYŁĄCZNIE DANE DEMONSTRACYJNE", "updated_at": "DEMO"}]).to_csv(out / "demo_injuries.csv", index=False)
print("Generated", out)
