"""Manual NBA player prop comparison for four Polish books.

No scraping, no inferred live prices and no betting recommendation.
Input is supplied by the user at workflow_dispatch. No money is staked.
"""
from __future__ import annotations

import csv
import math
import os
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
GAMES = ROOT / 'data' / 'real_player_games.csv'
BOOKS = [('Superbet', 'SUPERBET'), ('STS', 'STS'), ('Fortuna', 'FORTUNA'), ('Betclic', 'BETCLIC')]
MARKET_TO_FIELD = {'PTS': 'pts', 'AST': 'ast', 'REB': 'reb', 'PRA': 'pra'}
TAX = 0.12
WARSAW = ZoneInfo('Europe/Warsaw')
MAX_AGE_MINUTES = 3


def normalize(value: str) -> str:
    value = unicodedata.normalize('NFKD', str(value).casefold())
    return ''.join(c for c in value if not unicodedata.combining(c)).strip()


def number(raw: str) -> float:
    value = float(str(raw).strip().replace(',', '.'))
    if not math.isfinite(value):
        raise ValueError('Wartość musi być skończoną liczbą')
    return value


def parse_quote(raw: str, name: str) -> dict | None:
    """Accept `6.5@1.85` or blank. Never guess missing line/odds."""
    raw = str(raw or '').strip()
    if not raw or raw in ('-', '—'):
        return None
    parts = raw.split('@')
    if len(parts) != 2:
        raise ValueError(f'{name}: wpisz LINIA@KURS, np. 6.5@1.85')
    line, odds = (number(part) for part in parts)
    if not (0 <= line <= 150 and 1.01 <= odds <= 100):
        raise ValueError(f'{name}: nieprawidłowa linia albo kurs')
    return {'book': name, 'line': line, 'odds': odds, 'threshold': 1/((1-TAX)*odds)}


def quote_age(label: str, now: datetime) -> float | None:
    """Manual HH:MM time in Europe/Warsaw. Allows local-midnight rollover."""
    label = label.strip()
    if not label:
        return None
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', label):
        raise ValueError('Czas odczytu kursów: wpisz HH:MM czasu polskiego, np. 19:42')
    local = now.astimezone(WARSAW)
    h, m = map(int, label.split(':'))
    observed = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if observed > local + timedelta(seconds=59):
        observed -= timedelta(days=1)
    return (local - observed).total_seconds()/60


def get_history(csv_path: Path, player: str, game_date: date, market: str,
                side: str, line: float, limit: int = 20) -> dict:
    """Historical descriptives, NEVER a calibrated probability.

    Deduplicates game_id; excludes DNP and games on/after the analysis date.
    Reads the appended NBA dataset without needing a paid API.
    """
    if not csv_path.exists():
        return {'n': 0, 'wins': 0, 'losses': 0, 'pushes': 0, 'average': None, 'last_game': None}
    rows = {}
    with csv_path.open(encoding='utf-8-sig', newline='') as file:
        reader = csv.DictReader(file)
        required = {'game_id', 'date', 'player', 'minutes', 'pts', 'ast', 'reb', 'status'}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError('Baza meczów ma nieprawidłowe kolumny')
        for item in reader:
            if normalize(item['player']) != normalize(player):
                continue
            try:
                d = date.fromisoformat(item['date'][:10])
                if d >= game_date or normalize(item['status']) != 'played':
                    continue
                minutes = number(item['minutes'])
                if minutes <= 0:
                    continue
                pts, ast, reb = (number(item[k]) for k in ('pts', 'ast', 'reb'))
                value = {'PTS': pts, 'AST': ast, 'REB': reb, 'PRA': pts+ast+reb}[market]
                rows[str(item['game_id'])] = (d, value)
            except (ValueError, KeyError, TypeError):
                continue
    series = sorted(rows.values(), key=lambda x:x[0], reverse=True)[:limit]
    wins = sum((v>line if side == 'OVER' else v<line) for _,v in series)
    pushes = sum(v == line for _,v in series)
    n = len(series)
    return {'n': n, 'wins': wins, 'losses': n-wins-pushes, 'pushes': pushes,
            'average': sum(v for _,v in series)/n if n else None,
            'last_game': series[0][0].isoformat() if n else None}


def format_report(settings: dict, now: datetime | None = None, csv_path: Path = GAMES) -> str:
    now = now or datetime.now(timezone.utc)
    player = (settings.get('PLAYER') or '').strip()
    matchup = (settings.get('GAME') or '').strip().upper()
    market = (settings.get('MARKET') or '').strip().upper()
    side = (settings.get('SIDE') or '').strip().upper()
    day = (settings.get('GAME_DATE') or '').strip()
    if not player or len(player) > 100 or not re.fullmatch(r'[A-Z]{2,3}@(?:[A-Z]{2,3})', matchup):
        raise ValueError('Wymagane nazwisko oraz mecz w formacie BOS@NYK')
    if market not in MARKET_TO_FIELD or side not in ('OVER','UNDER'):
        raise ValueError('Rynek: PTS/AST/REB/PRA; strona: OVER/UNDER')
    game_date = date.fromisoformat(day)
    if abs((game_date - now.astimezone(WARSAW).date()).days) > 7:
        raise ValueError('Data meczu musi być w zakresie ±7 dni od bieżącej daty')
    age = quote_age((settings.get('CHECKED_AT_PL') or '').strip(), now)
    groups = defaultdict(list)
    for book, field in BOOKS:
        quote = parse_quote(settings.get(field, ''), book)
        if quote:
            groups[quote['line']].append(quote)
    if not groups:
        raise ValueError('Podaj co najmniej jedną linię bukmachera')
    fresh = age is not None and 0 <= age <= MAX_AGE_MINUTES
    header = [
        '🏀 NBA — PORÓWNANIE WPISANYCH RĘCZNIE KURSÓW',
        f'👤 {player} | {market} {side}',
        f'📅 {game_date.isoformat()} | {matchup}',
        '⚠️ Kursy pochodzą od użytkownika, NIE z API bukmacherów.',
        (f'🕒 Deklarowany czas sprawdzenia: {settings["CHECKED_AT_PL"]} PL, ~{age:.1f} min temu.'
            if age is not None else '🕒 Nie podano czasu sprawdzenia kursów.'),
        ('✅ Czas odczytu w granicy 3 min (DEKLARACJA, nie niezależna weryfikacja).'
            if fresh else '⛔ Czas odczytu niepotwierdzony lub >3 min — SPRAWDŹ PONOWNIE.'),
        'ℹ️ Porównujemy tylko identyczne linie. Standardowo liczony jest podatek 12% od stawki.',
        'ℹ️ Próg dotyczy zakładu rozliczanego bez zwrotu (push); dla linii całkowitych zwrot zmienia rachunek.',
    ]
    for line in sorted(groups):
        qs = sorted(groups[line], key=lambda q:q['odds'], reverse=True)
        n = get_history(csv_path, player, game_date, market, side, line)
        header.append(f'\n📌 {market} {side} {line:g}')
        for q in qs:
            limit = f'{q["threshold"]*100:.1f}%' if q['threshold'] <= 1 else '>100%'
            header.append(f'• {q["book"]}: {q["odds"]:.2f} | próg po 12%: {limit}')
        if len(qs) > 1:
            header.append(f'Najwyższy kurs dla tej samej linii: {qs[0]["book"]} {qs[0]["odds"]:.2f}')
        if n['n']:
            avg = f'{n["average"]:.1f}'
            header.append(f'Historia (max 20): {n["wins"]} trafień / {n["losses"]} nietrafień / {n["pushes"]} zwrotów; średnia {avg}.')
            if n['n'] < 15:
                header.append(f'⚠️ Mała próba: {n["n"]} występów.')
            else:
                header.append(f'Ostatni uwzględniony mecz: {n["last_game"]}.')
        else:
            header.append('Historia: brak pasujących występów w lokalnej bazie.')
    header.extend(['\n⚠️ Historia trafień NIE jest skalibrowanym prawdopodobieństwem ani gwarancją zysku.',
                   '⛔ Brak automatycznego typowania. Kurs, dostępność gracza i linia wymagają ponownego sprawdzenia przed decyzją.'])
    return '\n'.join(header)


def main() -> None:
    settings = dict(os.environ)
    message = format_report(settings)
    print(message)
    if len(message) > 4096:
        message = message[:3980] + '\n… (wynik skrócony)'
    token = (os.getenv('TELEGRAM_BOT_TOKEN') or '').strip()
    chat = (os.getenv('TELEGRAM_CHAT_ID') or '').strip()
    if not token or not chat:
        print('TELEGRAM: brak ustawionych sekretów, wyświetlono raport w logu.')
        return
    response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
        json={'chat_id':chat,'text':message,'disable_web_page_preview':True}, timeout=18)
    # Never include Telegram API URL (it embeds the secret token) in logs.
    if response.status_code != 200:
        raise RuntimeError(f'Telegram HTTP {response.status_code}; sprawdz sekrety w GitHub Actions')
    if not response.json().get('ok'):
        raise RuntimeError('Telegram zwrócił błąd')
    print('TELEGRAM: raport wysłany.')

if __name__ == '__main__':
    main()
