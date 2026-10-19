"""Local Streamlit page for official report monitoring and manual Polish book lines."""
from pathlib import Path
import json
from datetime import datetime, timezone
import pandas as pd
import streamlit as st
from odds_tools import validate_odds, best_by_matching_line, COLUMNS

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "runtime" / "official_snapshot.json"
SHEET = ROOT / "data" / "polish_odds.csv"

st.set_page_config(page_title="NBA Props — darmowe alerty", page_icon="🏀", layout="wide")
st.title("🏀 NBA — darmowe alerty absencji")
st.caption("Oficjalne raporty NBA · linie czterech polskich bukmacherów wprowadzane ręcznie")

if STATE.exists():
    try:
        snapshot = json.loads(STATE.read_text("utf-8"))
    except (ValueError, OSError):
        snapshot = {}
else:
    snapshot = {}

info = snapshot.get("metadata") or {}
a, b, c = st.columns(3)
a.metric("Zawodnicy w raporcie", info.get("count", "—"))
b.metric("Nowe zdarzenia historycznie", len(snapshot.get("events") or []))
c.metric("Czekające powiadomienia", len(snapshot.get("pending") or []))

if not snapshot.get("initialized"):
    st.warning("Monitoring jeszcze nie pobrał pierwszego raportu. Uruchom python free_alert_worker.py --once.")
else:
    st.write("Źródło:", info.get("source_url", "—"))
    st.write("Czas raportu:", info.get("report_at", "—"), "· ostatnie pobranie:", info.get("fetched_at", "—"))
    history = pd.DataFrame(snapshot.get("events", []))
    if not history.empty:
        st.subheader("Wykryte zmiany → OUT")
        opts = ["game_date", "matchup", "team", "player", "previous", "status", "source_url"]
        st.dataframe(history[[x for x in opts if x in history.columns]].tail(50), use_container_width=True, hide_index=True)
    active = pd.DataFrame(list((snapshot.get("status_rows") or {}).values()))
    if not active.empty:
        st.subheader("Statusy w najnowszym raporcie")
        show = ["game_date", "matchup", "team", "player", "status"]
        st.dataframe(active[show], use_container_width=True, hide_index=True)

st.divider()
st.subheader("Porównanie linii Superbet / STS / Fortuna / Betclic")
st.info("W tej wersji kursy nie są pobierane automatycznie. Wprowadź świeży arkusz CSV; system nie wymyśla cen. Próg opłacalności zakłada standardowy podatek 12% od stawki bez promocji.")
file = st.file_uploader("Wczytaj CSV kursów", type="csv", key="polish_odds")
if file:
    frame = pd.read_csv(file)
elif SHEET.exists():
    frame = pd.read_csv(SHEET)
else:
    frame = pd.DataFrame(columns=COLUMNS)
if frame.empty:
    st.caption("Brak zweryfikowanych kursów. Szablon CSV znajduje się w data/polish_odds_template.csv")
else:
    try:
        clean = validate_odds(frame)
        what = st.radio("Wyświetl", ["Wszystkie kursy", "Najlepsza cena dla identycznej linii"], horizontal=True)
        if what.startswith("Najlepsza"):
            clean = best_by_matching_line(clean)
        clean["min_probability_needed"] = (clean.break_even_after_tax * 100).round(1)
        st.dataframe(clean[["game", "player", "market", "side", "line", "bookmaker", "odds", "min_probability_needed", "age_s", "fresh"]], hide_index=True, use_container_width=True)
        st.caption("min_probability_needed = minimalna prawdziwa szansa powodzenia po podatku. To NIE jest prognoza modelu ani rekomendacja zakładu.")
    except (ValueError, KeyError, TypeError) as exc:
        st.error(str(exc))
st.divider()
st.write("**B2B, prognoza PTS/AST/REB i splity with/without:** nadal w głównej części NBA Props Lab; wymagają danych historycznych. Darmowy monitoring nie tworzy przewidywań bez danych.")
