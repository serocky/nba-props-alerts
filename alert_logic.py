"""Ocena sygnału i bezpieczeństwo alertów. Brak EV bez wystarczających danych."""
from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any
import pandas as pd

from engine import metric_forecast, p_over

PROPS = {"points": "pts", "assists": "ast", "rebounds": "reb", "points_rebounds_assists": "pra"}
PROP_LABEL = {"points": "PTS", "assists": "AST", "rebounds": "REB", "points_rebounds_assists": "PRA"}


def parse_ts(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        value = datetime.fromisoformat(str(text).replace('Z', '+00:00'))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def decimal_odds(american: Any) -> float | None:
    """BALD's odds over/under are American (-110/+120), not European."""
    try:
        a = float(american)
    except (TypeError, ValueError):
        return None
    if not isfinite(a) or (a > -100 and a < 100):
        return None
    return 1 + a / 100 if a > 0 else 1 + 100 / abs(a)


def current_odds(prop: dict, now: datetime, max_age_seconds: int) -> bool:
    ts = parse_ts(prop.get('updated_at'))
    if not ts:
        return False
    age = (now - ts).total_seconds()
    return -60 <= age <= max_age_seconds


def qualifies_for_signal(forecast: dict, metric: str) -> bool:
    """Only historical confirmed with/without and sufficient history.

    Does not imply calibration or causality of the observed difference.
    """
    if forecast.get('error') or forecast.get('sample_low'):
        return False
    splits = forecast.get('splits') or {}
    return (splits.get('with', {}).get('n', 0) >= 5
            and splits.get('without', {}).get('n', 0) >= 3
            and forecast.get('n', 0) >= 12)


def evaluate_prop(logs: pd.DataFrame, *, player: str, missing: str,
                  opponent: str, prop: dict, b2b: bool | None,
                  now: datetime, tax: float = 0.12,
                  min_ev: float = 0.06, max_odds_age_seconds: int = 180) -> dict | None:
    """No 'value' signal when prices stale, injury splits absent or line pushes."""
    stat = PROPS.get(prop.get('prop_type'))
    if not stat or prop.get('market', {}).get('type') != 'over_under':
        return None
    if not current_odds(prop, now, max_odds_age_seconds):
        return None
    try:
        line = float(prop.get('line_value'))
    except (ValueError, TypeError):
        return None
    # Push handling not implemented, so integer lines are intentionally excluded.
    if not isfinite(line) or abs((line % 1) - 0.5) > 1e-6 or line < 0:
        return None
    f = metric_forecast(logs, player, stat, opponent, b2b, missing)
    if not qualifies_for_signal(f, stat):
        return None
    p = p_over(f['mean'], f['sd'], line)
    candidates = []
    for side, prob, aodds in (
        ('OVER', p, prop['market'].get('over_odds')),
        ('UNDER', 1-p, prop['market'].get('under_odds')),
    ):
        odds = decimal_odds(aodds)
        if odds is None:
            continue
        ev = prob * (odds * (1-tax)) - 1
        if ev >= min_ev:
            candidates.append({
                'player': player, 'missing': missing, 'market': PROP_LABEL[prop['prop_type']],
                'line': line, 'side': side, 'book': prop.get('vendor', 'nieznany'),
                'price': round(odds, 3), 'p_model': round(prob, 4), 'ev': round(ev, 4),
                'mean': round(f['mean'], 2), 'baseline': round(f['base_mu'], 2),
                'with_n': f['splits']['with']['n'], 'without_n': f['splits']['without']['n'],
                'status': 'WSTĘPNY SYGNAŁ — model niezwalidowany',
                'updated_at': prop.get('updated_at'),
            })
    return max(candidates, key=lambda x: x['ev']) if candidates else None


def formatted_alert(event: dict, cand: dict) -> str:
    return (f"🔔 NBA — WSTĘPNY SYGNAŁ (model niezwalidowany)\n"
            f"🏀 {event['home']} vs {event['away']}\n"
            f"🚫 OUT: {event['missing']}\n"
            f"👤 {cand['player']} — {cand['market']} {cand['side']} {cand['line']:.1f}\n"
            f"🏷 {cand['book']} | kurs {cand['price']:.2f}\n"
            f"📊 Prognoza {cand['mean']:.1f} (bazowa {cand['baseline']:.1f})\n"
            f"📉 Szansa modelu {cand['p_model']:.1%} | orientacyjne EV po podatku {cand['ev']:+.1%}\n"
            f"🗂 Próba: z {cand['with_n']} / bez {cand['without_n']} meczów\n"
            f"🕘 Kurs z: {cand['updated_at']} (UTC)\n"
            f"⚠️ Sprawdź kurs u bukmachera. Splity nie dowodzą przyczynowości; "
            f"to test modelu, nie potwierdzona przewaga.")


def injury_status(status: str | None) -> str:
    s = str(status or '').strip().lower()
    if s in ('out', 'inactive', 'ruled out'):
        return 'OUT'
    if s in ('questionable', 'doubtful', 'probable'):
        return s.upper()
    return 'UNKNOWN'
