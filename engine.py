"""Przejrzysty, wstępny model statystyk zawodników NBA.

Wyniki nie są kalibrowanymi prawdopodobieństwami i nie stanowią
potwierdzonej przewagi nad rynkiem bukmacherskim.
"""
from __future__ import annotations
from datetime import date, timedelta
from math import erf, sqrt
import pandas as pd
import numpy as np

METRICS = {"Punkty": "pts", "Asysty": "ast", "Zbiórki": "reb", "PRA": "pra"}
REQUIRED = {"game_id", "date", "player", "team", "opponent", "minutes", "pts", "ast", "reb", "status"}
VALID_STATUS = {"PLAYED", "OUT", "DNP", "QUESTIONABLE", "DOUBTFUL", "PROBABLE", "UNKNOWN"}


def normalize_logs(df: pd.DataFrame) -> pd.DataFrame:
    missing = REQUIRED - set(df.columns)
    if missing:
        raise ValueError("Brakuje kolumn: " + ", ".join(sorted(missing)))
    x = df.copy()
    for col in ("game_id", "player", "team", "opponent", "status"):
        x[col] = x[col].astype(str).str.strip()
    x["team"] = x["team"].str.upper()
    x["opponent"] = x["opponent"].str.upper()
    x["status"] = x["status"].str.upper()
    if any(~x["status"].isin(VALID_STATUS)):
        raise ValueError("Niedozwolony status; użyj PLAYED, OUT, DNP, QUESTIONABLE, DOUBTFUL, PROBABLE lub UNKNOWN")
    x["date"] = pd.to_datetime(x["date"], errors="coerce").dt.normalize()
    if x["date"].isna().any():
        raise ValueError("Niepoprawna data: użyj YYYY-MM-DD")
    for col in ("minutes", "pts", "ast", "reb"):
        x[col] = pd.to_numeric(x[col], errors="coerce")
    played = x["status"] == "PLAYED"
    if (x.loc[played, ["minutes", "pts", "ast", "reb"]].isna().any().any()
        or (x.loc[played, ["minutes", "pts", "ast", "reb"]] < 0).any().any()):
        raise ValueError("Wiersze PLAYED wymagają nieujemnych danych minutes, pts, ast, reb")
    if (x.loc[played, "minutes"] <= 0).any():
        raise ValueError("Wiersze PLAYED wymagają minutes > 0")
    if x.duplicated(["game_id", "player"]).any():
        raise ValueError("Duplikat pary game_id + player")
    if "home" not in x.columns:
        x["home"] = ""
    x["pra"] = x[["pts", "ast", "reb"]].sum(axis=1, min_count=3)
    return x.sort_values(["date", "game_id"]).reset_index(drop=True)


def normalize_schedule(df: pd.DataFrame) -> pd.DataFrame:
    needed = {"game_id", "date", "home_team", "away_team"}
    if not needed.issubset(df.columns):
        raise ValueError("Terminarz wymaga: game_id,date,home_team,away_team")
    x = df.copy()
    x["date"] = pd.to_datetime(x["date"], errors="coerce").dt.normalize()
    if x["date"].isna().any():
        raise ValueError("Niepoprawne daty w terminarzu")
    for col in ("home_team", "away_team"):
        x[col] = x[col].astype(str).str.strip().str.upper()
    x["game_id"] = x["game_id"].astype(str).str.strip()
    return x.drop_duplicates("game_id").sort_values("date")


def back_to_back(schedule: pd.DataFrame, team: str, gamedate: date | str) -> bool | None:
    if schedule.empty:
        return None
    target = pd.Timestamp(gamedate).normalize()
    # Opieramy się na terminarzu drużyny; brak pełnego terminarza nie oznacza braku B2B.
    previous = target - pd.Timedelta(days=1)
    return bool(schedule[(schedule["date"] == previous) & ((schedule["home_team"] == team) | (schedule["away_team"] == team))].shape[0])


def teammate_splits(logs: pd.DataFrame, player: str, mate: str) -> dict:
    """Porównuje MECZE, nie minuty on/off. Nie zgaduje statusu przy braku wiersza."""
    main = logs[(logs.player == player) & (logs.status == "PLAYED")].copy()
    teammate = logs[logs.player == mate][["game_id", "team", "status"]].rename(columns={"status": "mate_status"})
    j = main.merge(teammate, on=["game_id", "team"], how="left", validate="one_to_one")
    yes = j[j.mate_status == "PLAYED"]
    no = j[j.mate_status.isin(["OUT", "DNP"])]
    unknown = j[~j.mate_status.isin(["PLAYED", "OUT", "DNP"])]
    def summarize(g):
        if g.empty:
            return {"n": 0}
        z = {"n": len(g), "minutes": float(g.minutes.mean())}
        for m in ("pts", "ast", "reb", "pra"):
            z[m] = float(g[m].mean())
            z[m + "_per36"] = float((g[m].sum() / g.minutes.sum()) * 36)
        return z
    return {"with": summarize(yes), "without": summarize(no), "unknown": len(unknown)}


def metric_forecast(logs: pd.DataFrame, player: str, metric: str, opponent: str,
                    selected_b2b: bool | None, absent_mate: str | None = None,
                    minute_override: float | None = None) -> dict:
    """MVP: season + recent, jawnie ograniczona korekta absent teammate.

    Matchup i B2B pokazujemy diagnostycznie. Nie nakładamy arbitralnych
    korekt bez historycznego, wystarczającego materiału i walidacji.
    """
    if metric not in {"pts", "ast", "reb", "pra"}:
        raise ValueError("Nieznana statystyka")
    played = logs[(logs.player == player) & (logs.status == "PLAYED") & (logs.minutes >= 5)].sort_values("date")
    if played.empty:
        return {"error": "Brak występów zawodnika"}
    rec = played.tail(10)
    # Minuty z ostatnich gier z lekkim dociążeniem sezonu.
    typical_minutes = float(0.7 * rec.minutes.median() + 0.3 * played.minutes.median())
    projected_minutes = float(minute_override) if minute_override is not None else typical_minutes
    if projected_minutes < 1 or projected_minutes > 53:
        return {"error": "Prognozowane minuty poza zakresem 1–53"}
    # Predykcja tempa produkcji; recent + długookresowy priorytet.
    rate = 0.58 * (rec[metric].sum() / rec.minutes.sum()) + 0.42 * (played[metric].sum() / played.minutes.sum())
    base_rate = float(rate)
    splits = None
    mate_note = "Brak wybranego zawodnika"
    if absent_mate:
        splits = teammate_splits(logs, player, absent_mate)
        yes, no = splits["with"], splits["without"]
        if yes["n"] >= 5 and no["n"] >= 3:
            r0 = yes[metric + "_per36"] / 36
            r1 = no[metric + "_per36"] / 36
            # Shrinkage i limit efektu; nie jest to estymator przyczynowy.
            w = min(0.45, no["n"] / (no["n"] + 10))
            delta = max(-0.30, min(0.30, (r1 / max(r0, 0.01)) - 1.0))
            rate = base_rate * (1 + w * delta)
            mate_note = f"Wstępna korekta o {w * delta:+.1%} (splity {yes['n']}/{no['n']} meczów, nie przyczynowość)"
        else:
            mate_note = "Za mało potwierdzonych meczów z/bez kolegi (min. 5 i 3); bez korekty prognozy"
    mu = float(rate * projected_minutes)
    matchup = played[played.opponent == opponent.upper()]
    # Stan B2B dotyczy drużyny i nie musi oznaczać, że zawodnik sam grał dzień wcześniej.
    # Nie utożsamiać korelacji z wpływem B2B.
    # Wstępny odchył empiryczny po skalowaniu do prognozy minut.
    transformed = (played[metric] / played.minutes * projected_minutes).tail(30)
    floor_sd = {"pts": 4.0, "ast": 1.5, "reb": 1.8, "pra": 5.5}[metric]
    sd = float(max(floor_sd, transformed.std(ddof=1) if len(transformed) > 1 else floor_sd))
    return {
        "n": len(played), "recent_n": len(rec), "mean": mu, "sd": sd,
        "minutes": projected_minutes, "mean_last5": float(played.tail(5)[metric].mean()),
        "mean_last10": float(rec[metric].mean()), "mean_season": float(played[metric].mean()),
        "opponent_n": len(matchup), "opponent_avg": float(matchup[metric].mean()) if len(matchup) else None,
        "b2b": selected_b2b, "splits": splits, "mate_note": mate_note,
        "sample_low": len(played) < 12, "recent": played.tail(10)[["date", "opponent", "minutes", metric]].copy(),
        "base_mu": base_rate * projected_minutes,
    }


def p_over(mean: float, sd: float, line: float) -> float:
    """Normalna aproksymacja: P(wynik > linia). Dla .5 próg = ceil(linia)."""
    if sd <= 0:
        raise ValueError("Odchylenie musi być dodatnie")
    # Ciągła poprawka: 20.5 => wygrana od 21, dolna granica 20.5.
    # Wyniki z linią całkowitą mają push; tu jedynie uproszczona aproksymacja.
    return 0.5 * (1 - erf((line - mean) / (sd * sqrt(2))))


def bdl_to_logs(records: list[dict], team_map: dict | None = None) -> pd.DataFrame:
    rows = []
    team_map = team_map or {}
    for item in records:
        p, t, g = item.get("player") or {}, item.get("team") or {}, item.get("game") or {}
        mn = item.get("min")
        if not mn or mn in ("0", "0:00"):
            continue
        if isinstance(mn, str) and ":" in mn:
            a, b = mn.split(":", 1)
            try:
                mins = float(a) + float(b) / 60
            except ValueError:
                continue
        else:
            try:
                mins = float(mn)
            except (TypeError, ValueError):
                continue
        teamid = t.get("id")
        rows.append({
            "game_id": str(g.get("id", "")), "date": str(g.get("date", ""))[:10],
            "player": " ".join(str(p.get(k, "")).strip() for k in ("first_name", "last_name")).strip(),
            "team": str(t.get("abbreviation", t.get("id", ""))).upper(),
            "opponent": str(team_map.get(g.get("visitor_team_id") if teamid == g.get("home_team_id") else g.get("home_team_id"), "")),
            "minutes": mins, "pts": item.get("pts"), "ast": item.get("ast"), "reb": item.get("reb"),
            "status": "PLAYED", "home": bool(teamid == g.get("home_team_id")),
        })
    if not rows:
        return pd.DataFrame(columns=list(REQUIRED) + ["home", "pra"])
    return normalize_logs(pd.DataFrame(rows))
