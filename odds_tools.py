"""Manual four-book odds comparison. No live pricing claimed or generated."""
from __future__ import annotations
import math
from datetime import datetime, timezone
import pandas as pd

BOOKS = {"Superbet", "STS", "Fortuna", "Betclic"}
COLUMNS = ["game_date", "game", "player", "market", "bookmaker", "side", "line", "odds", "checked_at_utc"]


def validate_odds(frame: pd.DataFrame, now: datetime | None = None, tax: float = 0.12) -> pd.DataFrame:
    if not 0 <= tax < 1:
        raise ValueError("Podatek poza zakresem")
    if not set(COLUMNS).issubset(frame.columns):
        raise ValueError("Brak kolumn: " + ", ".join(sorted(set(COLUMNS) - set(frame.columns))))
    f = frame.copy()
    for name in ("game_date", "game", "player", "market", "bookmaker", "side"):
        f[name] = f[name].astype(str).str.strip()
    f["bookmaker"] = f.bookmaker.str.replace("eFortuna", "Fortuna", regex=False)
    if not f.bookmaker.isin(BOOKS).all():
        raise ValueError("Wspierani bukmacherzy: Superbet, STS, Fortuna, Betclic")
    f["market"] = f.market.str.upper()
    f["side"] = f.side.str.upper()
    if not f.market.isin(["PTS", "AST", "REB", "PRA"]).all() or not f.side.isin(["OVER", "UNDER"]).all():
        raise ValueError("Niedozwolony rynek lub strona")
    f["line"] = pd.to_numeric(f.line, errors="raise")
    f["odds"] = pd.to_numeric(f.odds, errors="raise")
    if not ((f.line >= 0) & (f.odds > 1)).all():
        raise ValueError("Linie muszą być nieujemne, kursy > 1")
    f["checked_at_utc"] = pd.to_datetime(f.checked_at_utc, utc=True, errors="raise")
    now = now or datetime.now(timezone.utc)
    f["age_s"] = (pd.Timestamp(now) - f.checked_at_utc).dt.total_seconds()
    f["fresh"] = f.age_s.between(0, 180)
    f["break_even_after_tax"] = 1 / ((1 - tax) * f.odds)
    f["possible_after_tax"] = f.break_even_after_tax < 1
    return f.sort_values(["game", "player", "market", "side", "line", "odds"],
                         ascending=[True, True, True, True, True, False])


def best_by_matching_line(f: pd.DataFrame) -> pd.DataFrame:
    """Do not compare 5.5 assists vs 6.5 as if same market!"""
    if f.empty:
        return f
    return f.sort_values("odds", ascending=False).drop_duplicates(
        ["game_date", "game", "player", "market", "side", "line"]).sort_values(
        ["game_date", "game", "player", "market", "side", "line"])
