"""Opcjonalne pobieranie danych BALLDONTLIE, tylko po stronie serwera.
NIE wpisuj klucza API do plików publicznych ani do plików CSV.
"""
import requests

BASE = "https://api.balldontlie.io/v1"


class BDLClient:
    def __init__(self, token: str):
        if not token:
            raise ValueError("Brak klucza BALLDONTLIE_API_KEY")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": token.strip()})

    def fetch(self, endpoint: str, params: dict | None = None):
        try:
            response = self.session.get(BASE + endpoint, params=params, timeout=25)
            if response.status_code == 429:
                raise RuntimeError("Limit zapytań API (429). Odczekaj chwilę.")
            if response.status_code in (401, 403):
                raise RuntimeError("Brak uprawnień (401/403). Statystyki i kontuzje wymagają planu ALL-STAR lub wyższego.")
            response.raise_for_status()
            return response.json().get("data", []), response.json().get("meta", {})
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Błąd komunikacji z API: {e}") from e

    def all_pages(self, endpoint, params=None, max_pages=30):
        params = (params or {}).copy()
        params["per_page"] = 100
        out = []
        for _ in range(max_pages):
            chunk, meta = self.fetch(endpoint, params)
            out.extend(chunk)
            nxt = meta.get("next_cursor")
            if nxt is None:
                return out
            params["cursor"] = nxt
        raise RuntimeError("Dane przekraczają limit stron; zawęź wyszukiwanie")

    def search_players(self, query):
        return self.all_pages("/players", {"search": query}, max_pages=5)

    def player_stats(self, player_id: int, season: int):
        return self.all_pages("/stats", {"player_ids[]": player_id, "seasons[]": season}, max_pages=8)

    def games_for_date(self, day):
        return self.all_pages("/games", {"dates[]": str(day)}, max_pages=4)

    def injuries(self, player_ids):
        ids = [str(x) for x in player_ids if x]
        if not ids:
            return []
        return self.all_pages("/player_injuries", {"player_ids[]": ids}, max_pages=4)


def games_to_schedule(items):
    import pandas as pd
    from engine import normalize_schedule
    rows = []
    for g in items:
        h = g.get("home_team") or {}
        a = g.get("visitor_team") or {}
        rows.append({"game_id": str(g.get("id", "")), "date": str(g.get("date", ""))[:10],
                     "home_team": h.get("abbreviation", ""), "away_team": a.get("abbreviation", "")})
    if not rows:
        return pd.DataFrame(columns=["game_id", "date", "home_team", "away_team"])
    return normalize_schedule(pd.DataFrame(rows))
