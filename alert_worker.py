"""Polling of live NBA injuries + NBA player props and Telegram alerts.

Run this as a SEPARATE always-on process (not a Streamlit background thread).
Real alerts require paid BDL GOAT + optional Telegram credentials.
No real bets, orders or wagers are placed by this program.
"""
from __future__ import annotations
import logging
import os
import time
import json
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from alert_logic import injury_status, parse_ts, evaluate_prop, formatted_alert, PROPS, PROP_LABEL, current_odds, decimal_odds
from alert_store import (connect, iso_now, save_statuses, pending_events,
                         close_event, save_alert, mark_alert, telemetry)
from bdl import BDLClient
from engine import bdl_to_logs, normalize_logs, back_to_back, normalize_schedule

ROOT = Path(__file__).parent
LOG = logging.getLogger('nba_alerts')
DB = os.getenv('NBA_ALERT_DB', str(ROOT / 'runtime' / 'alerts.sqlite3'))
POLL = max(30, int(os.getenv('POLL_SECONDS', '60')))
VENUES = {v.strip().lower() for v in os.getenv('VENDORS', 'draftkings,fanduel,betrivers').split(',') if v.strip()}
TAX = float(os.getenv('STAKE_TAX', '0.12'))
MIN_EV = float(os.getenv('MIN_EV', '0.06'))
MAX_AGE = int(os.getenv('MAX_ODDS_AGE_SECONDS', '180'))
PROFILES_MAX = max(1, int(os.getenv('MAX_PLAYERS_PER_EVENT', '20')))


def telegram_send(message: str) -> tuple[bool, str | None]:
    token, chat_id = os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        LOG.info('DRY-RUN Telegram (brak tokenu/chat id):\n%s', message)
        return False, 'Brak TELEGRAM_BOT_TOKEN lub TELEGRAM_CHAT_ID'
    try:
        resp = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                             json={'chat_id': chat_id, 'text': message}, timeout=15)
        resp.raise_for_status()
        if not resp.json().get('ok'):
            raise RuntimeError('Telegram API: ok=false')
        return True, None
    except (requests.RequestException, ValueError, RuntimeError) as e:
        error = str(e).replace(token, '[REDACTED]')
        LOG.warning('Telegram send failed: %s', error)
        return False, error[:250]


def new_alert(conn, key: str, event_key: str, payload: dict, message: str) -> bool:
    # One unique insert makes repeated scheduler ticks idempotent.
    payload = dict(payload, _message=message)
    if not save_alert(conn, key, event_key, payload):
        return False
    sent, error = telegram_send(message)
    mark_alert(conn, key, error)
    return sent


def retry_telegram(conn):
    """Retry delivery of saved notifications after temporary Telegram errors."""
    if not (os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_CHAT_ID')):
        return
    rows = conn.execute("SELECT signal_key, payload FROM alerts WHERE sent=0 ORDER BY created_at DESC LIMIT 20").fetchall()
    for key, raw in rows:
        try:
            msg = json.loads(raw).get('_message')
            if not msg:
                continue
            ok, err = telegram_send(msg)
            mark_alert(conn, key, err)
        except Exception as exc:
            LOG.warning('Retry Telegram %s failed: %s', key, type(exc).__name__)


def build_watchlist(event: dict, price_items: list[dict], roster: dict, out_ids: set[int], now: datetime):
    """Only raw bookmaker lines, deliberately NOT a 'value' recommendation."""
    seen = set()
    lines = []
    for item in price_items:
        pid = item.get('player_id')
        if pid is None or int(pid) not in roster or int(pid) in out_ids:
            continue
        if item.get('prop_type') not in ('points', 'assists', 'rebounds'):
            continue
        if item.get('market', {}).get('type') != 'over_under':
            continue
        if item.get('vendor', '').lower() not in VENUES or not current_odds(item, now, MAX_AGE):
            continue
        player = roster[int(pid)]
        name = f"{player.get('first_name','')} {player.get('last_name','')}".strip()
        if not name:
            continue
        k = (name, item['prop_type'])
        if k in seen:
            continue
        seen.add(k)
        odds_over = decimal_odds(item['market'].get('over_odds'))
        odds_under = decimal_odds(item['market'].get('under_odds'))
        if odds_over is None or odds_under is None:
            continue
        lines.append(f"• {name} | {PROP_LABEL[item['prop_type']]} {item['line_value']} | "
                     f"O {odds_over:.2f} / U {odds_under:.2f} ({item['vendor']})")
        if len(lines) >= 6:
            break
    if not lines:
        return None
    return (f"🔎 NBA — LINIE DO OBSERWACJI (bez rekomendacji)\n"
            f"🏀 {event['home']} – {event['away']} | OUT: {event['missing']}\n"
            + '\n'.join(lines)
            + '\n⚠️ Linie z feedu, nie z Superbetu. Brak potwierdzonego wpływu absencji; '
              'sprawdź dostępność i kurs u własnego bukmachera.')


class MarketService:
    def __init__(self, key: str):
        self.client = BDLClient(key)
        self.mem: dict[str, tuple[float, object]] = {}

    def cached(self, key, ttl: int, fetch):
        hit = self.mem.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        value = fetch()
        self.mem[key] = (time.time(), value)
        return value

    def api(self, endpoint: str, params: dict | None = None):
        return self.client.all_pages(endpoint, params, max_pages=20)

    def games(self, date_strings):
        # Uses NBA/US calendar day; same games may appear across dates, deduplicate.
        games = self.api('/games', {'dates[]': date_strings, 'season_type': 'regular'})
        return [g for g in games if g.get('status_state') == 'scheduled' and not g.get('postponed')]

    def injuries(self, team_ids):
        return self.api('/player_injuries', {'team_ids[]': list(team_ids)})

    def team_players(self, team_id):
        return self.cached(f'players:{team_id}', 3600, lambda: self.api('/players', {'team_ids[]': [team_id]}))

    def team_abbreviations(self):
        def fetch():
            return {t['id']: t['abbreviation'] for t in self.api('/teams')}
        return self.cached('teams', 86400, fetch)

    def props(self, game_id):
        # This is V2, different from injuries/stats V1 path.
        url = 'https://api.balldontlie.io/v2/odds/player_props'
        resp = self.client.session.get(url, params={'game_id': game_id}, timeout=30)
        if resp.status_code in (401, 403):
            raise RuntimeError('Player props API wymaga planu GOAT (401/403)')
        if resp.status_code == 429:
            raise RuntimeError('API limit 429: zwiększ POLL_SECONDS')
        resp.raise_for_status()
        return resp.json().get('data', [])

    def played_logs(self, player_id: int, season: int):
        def fetch():
            entries = []
            for sy in (season - 1, season):
                entries.extend(self.client.player_stats(player_id, sy))
            return bdl_to_logs(entries, self.team_abbreviations())
        return self.cached(f'stats:{player_id}:{season}', 3600, fetch)


def candidate_games(market: MarketService, now: datetime) -> list[dict]:
    # ET calendar is relevant to NBA dates. Only upcoming <24 hours.
    et = now.astimezone(ZoneInfo('America/New_York')).date()
    days = [str(et), str(et + timedelta(days=1))]
    selected = []
    for g in market.games(days):
        tip = parse_ts(g.get('datetime'))
        if tip is not None and 0 < (tip - now).total_seconds() <= 24 * 3600:
            selected.append(g)
    return selected


def injury_snapshots(games: list[dict], injury_records: list[dict]) -> list[dict]:
    # Injury feeds are not necessarily game-scoped: label as reported, not official game DNP.
    by_team = {}
    for i in injury_records:
        player = i.get('player') or {}
        team = player.get('team_id') or (player.get('team') or {}).get('id')
        if team is None or player.get('id') is None:
            continue
        by_team.setdefault(int(team), []).append(i)
    res = []
    for g in games:
        for team_key, opp_key in (('home_team', 'visitor_team'), ('visitor_team', 'home_team')):
            team, opp = g.get(team_key) or {}, g.get(opp_key) or {}
            for record in by_team.get(int(team.get('id', 0)), []):
                p = record['player']
                res.append({
                    'game_id': str(g['id']), 'player_id': int(p['id']),
                    'missing': (str(p.get('first_name', '')) + ' ' + str(p.get('last_name', ''))).strip(),
                    'team': team.get('abbreviation'), 'team_id': int(team['id']),
                    'opponent': opp.get('abbreviation'), 'home': g['home_team'].get('abbreviation'),
                    'away': g['visitor_team'].get('abbreviation'),
                    'tip': g.get('datetime'), 'season': int(g.get('season') or 2026),
                    'status': injury_status(record.get('status')),
                    'detail': record.get('description') or record.get('comment') or '',
                })
    return res


def scheduled_b2b(market: MarketService, team_id: int, current_game: dict) -> bool | None:
    target = parse_ts(current_game.get('tip'))
    if not target:
        return None
    nba_day = target.astimezone(ZoneInfo('America/New_York')).date()
    previous = nba_day - timedelta(days=1)
    try:
        # Exactly previous local date, not previous 24h UTC.
        old_games = market.api('/games', {'dates[]': [str(previous)], 'team_ids[]': [team_id]})
        return bool([g for g in old_games if g.get('status_state') in ('final', 'in_progress')])
    except Exception as e:
        LOG.warning('B2B unavailable: %s', e)
        return None


def history_for_event(market: MarketService, player_id: int, event: dict) -> pd.DataFrame:
    # Include ACTUAL PLAYED games by the unavailable teammate; without those
    # a with/without split would be mathematically undefined.
    logs = market.played_logs(player_id, int(event['season']))
    mate_logs = market.played_logs(int(event['player_id']), int(event['season']))
    if not mate_logs.empty:
        logs = normalize_logs(pd.concat([logs, mate_logs], ignore_index=True))
    verified_csv = Path(os.getenv('VERIFIED_ABSENCES_CSV', str(ROOT / 'data' / 'verified_absences.csv')))
    if not verified_csv.exists():
        return logs
    try:
        verified = normalize_logs(pd.read_csv(verified_csv, dtype={'game_id': str}))
        verified = verified[verified.status.isin(['OUT', 'DNP'])]
        # Ensure confirmed OUT records refer to same historical team and correct teammate.
        players_team = event['team']
        verified = verified[verified.team == players_team]
        if not verified.empty:
            merged = pd.concat([logs, verified], ignore_index=True)
            return normalize_logs(merged.drop_duplicates(['game_id', 'player'], keep='first'))
    except Exception as e:
        LOG.warning('Bad verified absences CSV: %s', e)
    return logs


def process_event(conn, market: MarketService, event: dict, now: datetime):
    ek = event['event_key']
    injury_note = (f"🚨 NBA OUT — STATUS Z FEEDU (sprawdź raport meczowy)\n"
                   f"🏀 {event['home']} – {event['away']}\n"
                   f"🚫 {event['missing']} (poprzednio: {event['previous']})\n"
                   f"🗒 {event.get('detail','')[:220]}\n"
                   "🔎 Analizuję linie PTS/AST/REB kolegów. "
                   "Feed może nie potwierdzać absencji w konkretnym meczu.")
    new_alert(conn, f'out:{ek}', ek, {'kind': 'injury', **event}, injury_note)

    odds = market.props(event['game_id'])
    selected_props = [p for p in odds if p.get('prop_type') in PROPS
                      and (p.get('vendor') or '').lower() in VENUES
                      and p.get('market', {}).get('type') == 'over_under']
    if not selected_props:
        LOG.info('No 2-way player props for game %s; retry for up to 20min.', event['game_id'])
        return
    pid_props = {}
    for p in selected_props:
        pid_props.setdefault(int(p['player_id']), []).append(p)
    roster = {int(p['id']): p for p in market.team_players(int(event['team_id']))}
    # Only players in affected team, exclude injured player and every currently OUT teammate.
    out_ids = {int(row[0]) for row in conn.execute(
        'SELECT player_id FROM injury_state WHERE game_id=? AND status="OUT"', (str(event['game_id']),))}
    watchlist = build_watchlist(event, selected_props, roster, out_ids, datetime.now(timezone.utc))
    if watchlist:
        new_alert(conn, f'watch:{ek}', ek, {'kind':'watchlist', **event}, watchlist)
    # If no independently verified historical OUT/DNP file exists,
    # alert with lines but do NOT fabricate a with/without recommendation.
    verified = Path(os.getenv('VERIFIED_ABSENCES_CSV', str(ROOT / 'data' / 'verified_absences.csv')))
    if not verified.exists():
        LOG.info('No verified historical absences: only raw line watchlist is available.')
        if watchlist:
            close_event(conn, ek)
        return
    candidates = []
    b2b = scheduled_b2b(market, int(event['team_id']), event)
    for pid in list(pid_props)[:100]:
        if pid not in roster or pid in out_ids:
            continue
        if len(candidates) >= PROFILES_MAX:
            break
        pinfo = roster[pid]
        pname = (str(pinfo.get('first_name', '')) + ' ' + str(pinfo.get('last_name', ''))).strip()
        if not pname:
            continue
        try:
            logs = history_for_event(market, pid, event)
            # Hard safety gate: if absent teammate has NO verified historical OUT/DNP,
            # no speculative full-value alert. This is common until snapshot archive is built.
            if logs.empty:
                continue
            candidates.append((pname, logs, pid))
        except Exception as e:
            LOG.warning('Player %s failed: %s', pname, e)
    if not candidates:
        LOG.info('No roster/log candidates for %s.', ek)
        return
    # Fetch fresh prices AFTER historical stats retrieval, to avoid using minutes-old snapshot.
    fresh_odds = market.props(event['game_id'])
    fresh_by_player = {}
    for p in fresh_odds:
        fresh_by_player.setdefault(p.get('player_id'), []).append(p)
    signals = []
    for pname, logs, pid in candidates:
        for p in fresh_by_player.get(pid, []):
            if (p.get('vendor') or '').lower() not in VENUES:
                continue
            s = evaluate_prop(logs, player=pname, missing=event['missing'],
                              opponent=event['opponent'], prop=p, b2b=b2b, now=datetime.now(timezone.utc),
                              tax=TAX, min_ev=MIN_EV, max_odds_age_seconds=MAX_AGE)
            if s is not None:
                signals.append(s)
    signals.sort(key=lambda r: r['ev'], reverse=True)
    sent_count = 0
    for best in signals[:3]:
        # Price snapshots may change quickly; do not send near-identical alerts for every minute.
        raw_key = '|'.join([ek, best['player'], best['market'], best['side'],
                            str(best['line']), best['book']])
        key = 'prop:' + hashlib.sha256(raw_key.encode('utf-8')).hexdigest()[:24]
        new_alert(conn, key, ek, {'kind': 'signal', **best}, formatted_alert(event, best))
        sent_count += 1
    LOG.info('Event %s => %d candidate signals (sent/recorded %d)', ek, len(signals), sent_count)
    # Mark done only when line has been assessed; if no signal, don't poll for 20min endlessly
    # after prices are available. However leave pending to check for later odds updates.
    if signals:
        close_event(conn, ek)


def tick(conn, market: MarketService):
    now = datetime.now(timezone.utc)
    games = candidate_games(market, now)
    team_ids = {int(x[k]['id']) for x in games for k in ('home_team', 'visitor_team')}
    if not games:
        telemetry(conn, 'status', f'{iso_now()}: brak spotkań w najbliższych 24 h')
        return
    injuries = market.injuries(team_ids)
    snapshots = injury_snapshots(games, injuries)
    new_events, seeded = save_statuses(conn, snapshots)
    LOG.info('%d games, %d injury statuses; new OUT=%d%s', len(games), len(snapshots),
             len(new_events), ' (bootstrapped)' if seeded else '')
    telemetry(conn, 'last_poll', iso_now())
    telemetry(conn, 'status', f'gry={len(games)}, statusy={len(snapshots)}, nowe_OUT={len(new_events)}')
    retry_telegram(conn)
    for event in pending_events(conn, max_minutes=20):
        tip = parse_ts(event.get('tip'))
        if not tip or tip <= datetime.now(timezone.utc):
            close_event(conn, event['event_key'])
            continue
        try:
            process_event(conn, market, event, now)
        except Exception as e:
            LOG.exception('Event %s not processed: %s', event.get('event_key'), e)
            telemetry(conn, 'last_error', f'{iso_now()}: {str(e)[:300]}')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    key = os.environ.get('BALLDONTLIE_API_KEY', '').strip()
    if not key:
        raise SystemExit('Brak BALLDONTLIE_API_KEY. Uruchom demo_alert.py bez płatnego API.')
    if not (0 <= TAX < 1) or not (0 < MIN_EV < 1):
        raise SystemExit('Niepoprawne parametry STAKE_TAX lub MIN_EV')
    conn = connect(DB)
    market = MarketService(key)
    LOG.info('Starting NBA OUT monitor, poll every %ds', POLL)
    while True:
        try:
            tick(conn, market)
        except KeyboardInterrupt:
            break
        except Exception as e:
            LOG.exception('Polling failed: %s', e)
            telemetry(conn, 'last_error', f'{iso_now()}: {str(e)[:300]}')
        time.sleep(POLL)


if __name__ == '__main__':
    main()
