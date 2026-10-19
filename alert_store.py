"""Durable SQLite state: prevents duplicate alerts and first-start backfill."""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def connect(path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=20)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS player_state (
      player_id INTEGER PRIMARY KEY, status TEXT NOT NULL, changed_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS injury_state (
      game_id TEXT NOT NULL, player_id INTEGER NOT NULL, status TEXT NOT NULL,
      changed_at TEXT NOT NULL, PRIMARY KEY(game_id, player_id));
    CREATE TABLE IF NOT EXISTS injury_events (
      event_key TEXT PRIMARY KEY, game_id TEXT NOT NULL, player_id INTEGER NOT NULL,
      created_at TEXT NOT NULL, payload TEXT NOT NULL, dispatched INTEGER NOT NULL DEFAULT 0,
      processed_at TEXT);
    CREATE TABLE IF NOT EXISTS alerts (
      signal_key TEXT PRIMARY KEY, event_key TEXT NOT NULL, created_at TEXT NOT NULL,
      payload TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0, telegram_error TEXT);
    CREATE TABLE IF NOT EXISTS telemetry (
      key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    conn.commit()
    return conn


def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def first_run(conn) -> bool:
    return conn.execute("SELECT value FROM meta WHERE key='initialized'").fetchone() is None


def save_statuses(conn, snapshots: list[dict]):
    """Atomic snapshot: existing statuses can turn OUT and become events.
    On initial poll, seed statuses and DO NOT send retroactive signals.
    Each event key includes timestamp of real transition to distinguish re-IN / OUT.
    """
    bootstrap = first_run(conn)
    events = []
    now = iso_now()
    with conn:
        # One global transition per player: do not advertise a season-long OUT as a NEW OUT every game.
        seen_players = set()
        for item in snapshots:
            gid, pid = str(item['game_id']), int(item['player_id'])
            if pid in seen_players:
                global_changed = False
                prior_global = item['status']
            else:
                seen_players.add(pid)
                global_row = conn.execute('SELECT status FROM player_state WHERE player_id=?', (pid,)).fetchone()
                prior_global = global_row[0] if global_row else 'UNKNOWN'
                global_changed = prior_global != item['status']
                if global_changed:
                    conn.execute('INSERT INTO player_state(player_id,status,changed_at) VALUES (?,?,?) '
                                 'ON CONFLICT(player_id) DO UPDATE SET status=excluded.status,changed_at=excluded.changed_at',
                                 (pid, item['status'], now))
            status = item['status']
            row = conn.execute('SELECT status FROM injury_state WHERE game_id=? AND player_id=?', (gid, pid)).fetchone()
            previous = row[0] if row else 'UNKNOWN'
            if previous != status:
                conn.execute('INSERT INTO injury_state (game_id,player_id,status,changed_at) VALUES (?,?,?,?) '
                             'ON CONFLICT(game_id,player_id) DO UPDATE SET status=excluded.status,changed_at=excluded.changed_at',
                             (gid, pid, status, now))
                if not bootstrap and status == 'OUT' and global_changed and prior_global != 'OUT':
                    key = f'{gid}:{pid}:{now}'
                    payload = dict(item, previous=prior_global, event_key=key)
                    conn.execute('INSERT OR IGNORE INTO injury_events (event_key,game_id,player_id,created_at,payload) VALUES (?,?,?,?,?)',
                                 (key, gid, pid, now, json.dumps(payload, ensure_ascii=False)))
                    events.append(payload)
        if bootstrap:
            conn.execute("INSERT OR IGNORE INTO meta (key,value) VALUES ('initialized',?)", (now,))
    return events, bootstrap


def pending_events(conn, max_minutes=20):
    from datetime import timedelta
    min_time = (datetime.now(timezone.utc) - timedelta(minutes=max_minutes)).isoformat(timespec='seconds')
    return [json.loads(r[0]) for r in conn.execute(
        'SELECT payload FROM injury_events WHERE dispatched=0 AND created_at >= ? ORDER BY created_at', (min_time,))]


def close_event(conn, key):
    with conn:
        conn.execute('UPDATE injury_events SET dispatched=1, processed_at=? WHERE event_key=?', (iso_now(), key))


def save_alert(conn, key: str, event_key: str, payload: dict) -> bool:
    with conn:
        r = conn.execute('INSERT OR IGNORE INTO alerts (signal_key,event_key,created_at,payload) VALUES (?,?,?,?)',
                         (key, event_key, iso_now(), json.dumps(payload, ensure_ascii=False)))
    return r.rowcount > 0


def mark_alert(conn, key: str, error: str | None):
    with conn:
        conn.execute('UPDATE alerts SET sent=?,telegram_error=? WHERE signal_key=?', (0 if error else 1, error, key))


def telemetry(conn, key: str, value: str):
    with conn:
        conn.execute('INSERT INTO telemetry(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key,value))
