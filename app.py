"""NBA Props Lab v1 – panel Streamlit (telefon / desktop).
Uruchom: streamlit run app.py
"""
from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import os
import pandas as pd
import streamlit as st
from engine import (normalize_logs, normalize_schedule, metric_forecast,
                    teammate_splits, back_to_back, p_over, bdl_to_logs, METRICS)
from bdl import BDLClient, games_to_schedule

st.set_page_config(page_title="NBA Props Lab", page_icon="🏀", layout="wide", initial_sidebar_state="expanded")
st.markdown("""<style>
.block-container {padding-top:1.5rem;max-width:1280px}
.hero{padding:1.25rem 1.4rem;border:1px solid #263449;background:linear-gradient(120deg,#15253a,#0b1525);border-radius:16px;margin-bottom:1rem}
.hero h1{font-size:2rem;color:#f8fafc;margin:0;letter-spacing:-1px}
.hero p{color:#b4c3d3;margin:.45rem 0 0}
.smallnote{color:#90a4bc;font-size:.84rem}
</style>""", unsafe_allow_html=True)
st.markdown('<div class="hero"><h1>🏀 NBA Props Lab</h1><p>PTS · AST · REB · PRA — matchup, B2B i potwierdzone absencje</p></div>', unsafe_allow_html=True)

DATA_DIR = Path(__file__).parent / "data"


def get_api_key():
    key = os.getenv("BALLDONTLIE_API_KEY", "")
    try:
        key = key or st.secrets.get("BALLDONTLIE_API_KEY", "")
    except Exception:
        pass
    return key


@st.cache_data(ttl=900, show_spinner=False)
def cached_search(key, q):
    return BDLClient(key).search_players(q)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_player_stats(key, pid, season):
    return BDLClient(key).player_stats(pid, season)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_team_map(key):
    items = BDLClient(key).all_pages("/teams", max_pages=2)
    return {int(x["id"]): x["abbreviation"] for x in items}


@st.cache_data(ttl=600, show_spinner=False)
def cached_injuries(key, pids):
    return BDLClient(key).injuries(pids)


@st.cache_data(ttl=1200, show_spinner=False)
def cached_games(key, date_str):
    return BDLClient(key).games_for_date(date_str)


def read_csv(f, name):
    try:
        return pd.read_csv(f, dtype={"game_id": str})
    except Exception as e:
        st.error(f"Nie można odczytać {name}: {e}")
        st.stop()


def player_picker(key, label, search_key):
    q = st.text_input(label, key=search_key, placeholder="np. Nikola Jokic")
    if len(q.strip()) < 3:
        return None
    try:
        results = cached_search(key, q.strip())
    except Exception as e:
        st.error(str(e))
        return None
    if not results:
        st.warning("Nie znaleziono zawodnika")
        return None
    def n(x):
        return f"{x.get('first_name', '')} {x.get('last_name', '')} — {((x.get('team') or {}).get('abbreviation', ''))} [ID {x['id']}]"
    return st.selectbox(label + " — wybierz", results, format_func=n, key=search_key + "_choice")


with st.sidebar:
    st.header("Źródło danych")
    mode = st.radio("Tryb pracy", ["DEMO (fikcyjne dane)", "Import CSV", "BALLDONTLIE API"], index=0)
    st.caption("DEMO nie pokazuje rzeczywistych wyników NBA. API wymaga własnego klucza i płatnego planu na statystyki/kontuzje.")
    selected_day = st.date_input("Data analizowanego meczu", value=date.today())
    season = st.selectbox("Sezon danych", [2025, 2026, 2024], format_func=lambda v: f"{v}–{str(v + 1)[-2:]}")
    st.divider()
    st.caption("Prawdopodobieństwa są eksperymentalną aproksymacją, niezweryfikowanym modelem bukmacherskim.")

logs = None
schedule = None
injuries = pd.DataFrame(columns=["player", "team", "status", "reason", "updated_at"])
player = None
teammate = None
team = None
opponent = None
api_key = get_api_key()

if mode.startswith("DEMO"):
    logs = normalize_logs(pd.read_csv(DATA_DIR / "demo_player_games.csv", dtype={"game_id": str}))
    schedule = normalize_schedule(pd.read_csv(DATA_DIR / "demo_schedule.csv", dtype={"game_id": str}))
    injuries = pd.read_csv(DATA_DIR / "demo_injuries.csv")
    st.warning("TRYB DEMONSTRACYJNY: zawodnicy, wyniki, kontuzje oraz daty są fikcyjne. Niczego nie traktuj jako typ NBA.")
    opts = sorted(logs.player.unique())
    a, b, c = st.columns([1, 1, 1])
    with a:
        player = st.selectbox("Zawodnik", opts, index=0)
    with b:
        mate_opts = ["Brak"] + [x for x in opts if x != player]
        teammate = st.selectbox("Nieobecny kolega — scenariusz", mate_opts, index=mate_opts.index("Jordan Demo") if "Jordan Demo" in mate_opts else 0)
        teammate = None if teammate == "Brak" else teammate
    with c:
        opponent = st.selectbox("Rywal", sorted(logs.opponent.unique()))
    team = "LAB"
    # W demo wybieramy datę pozwalającą na pokazanie B2B, chyba że zmieniono ją ręcznie.
    if selected_day == date.today():
        demo_last = schedule.date.max().date()
        selected_day = demo_last
        st.caption(f"Demonstracyjny mecz ustawiony na {demo_last} dla zobrazowania kalendarza.")

elif mode == "Import CSV":
    a, b, c = st.columns(3)
    with a:
        raw_logs = st.file_uploader("1. Logi zawodników", type=["csv"], help="Kolumny wymagane opisano w README")
    with b:
        raw_sched = st.file_uploader("2. Terminarz", type=["csv"])
    with c:
        raw_inj = st.file_uploader("3. Statusy kontuzji (opcjonalnie)", type=["csv"])
    if raw_logs is None:
        st.info("Wgraj plik logów CSV. W paczce znajduje się przykład struktury kolumn.")
        st.stop()
    try:
        logs = normalize_logs(read_csv(raw_logs, "logi"))
        schedule = normalize_schedule(read_csv(raw_sched, "terminarz")) if raw_sched else pd.DataFrame(columns=["game_id", "date", "home_team", "away_team"])
        if raw_inj:
            injuries = read_csv(raw_inj, "kontuzje")
    except ValueError as e:
        st.error(str(e))
        st.stop()
    opts = sorted(logs.loc[logs.status == "PLAYED", "player"].unique())
    if not opts:
        st.error("Brak wierszy PLAYED")
        st.stop()
    a, b, c = st.columns(3)
    with a:
        player = st.selectbox("Zawodnik", opts)
    with b:
        team = str(logs[(logs.player == player) & (logs.status == "PLAYED")].sort_values("date").iloc[-1]["team"])
        mates = sorted(logs.loc[(logs.team == team) & (logs.player != player), "player"].unique())
        mateopt = st.selectbox("Kolega / absencja", ["Brak"] + mates)
        teammate = mateopt if mateopt != "Brak" else None
    with c:
        other = sorted(set(logs.opponent.unique()) | set(schedule.home_team.unique()) | set(schedule.away_team.unique()) - {team})
        other = [x for x in other if x != team and x]
        opponent = st.selectbox("Rywal", other) if other else st.text_input("Rywal (skrót)", value="NYK")

else:
    if not api_key:
        st.error("Nie skonfigurowano BALLDONTLIE_API_KEY. Ustaw go w sekretach Streamlit albo jako zmienną środowiskową. Nie wklejaj go do kodu HTML.")
        st.stop()
    st.info("API: pobieranie aktualnych kontuzji wymaga co najmniej planu ALL-STAR. Statusów z bieżącego raportu nie wolno utożsamiać z historycznymi absencjami.")
    a, b = st.columns(2)
    with a:
        selected_p = player_picker(api_key, "Szukaj zawodnika", "pquery")
    with b:
        selected_m = player_picker(api_key, "Szukaj kolegi (opcjonalnie)", "mquery")
    if not selected_p:
        st.info("Wpisz nazwisko zawodnika w wyszukiwarce")
        st.stop()
    player = f"{selected_p.get('first_name', '')} {selected_p.get('last_name', '')}".strip()
    if selected_m and selected_m["id"] != selected_p["id"] and (selected_m.get("team") or {}).get("id") != (selected_p.get("team") or {}).get("id"):
        st.warning("Wybrany drugi zawodnik jest aktualnie w innej drużynie; nie zostanie użyty jako kolega w analizie absencji.")
        selected_m = None
    teammate = (f"{selected_m.get('first_name', '')} {selected_m.get('last_name', '')}".strip() if selected_m and selected_m['id'] != selected_p['id'] else None)
    try:
        with st.spinner("Pobieram historyczne mecze i statusy kontuzji…"):
            tm = cached_team_map(api_key)
            raw = cached_player_stats(api_key, selected_p["id"], season)
            if teammate:
                raw += cached_player_stats(api_key, selected_m["id"], season)
            logs = bdl_to_logs(raw, tm)
            today_games = cached_games(api_key, selected_day.isoformat())
            prev_games = cached_games(api_key, (selected_day - timedelta(days=1)).isoformat())
            schedule = games_to_schedule(today_games + prev_games)
            injury_records = cached_injuries(api_key, tuple(sorted({selected_p['id']} | ({selected_m['id']} if teammate else set()))))
    except Exception as e:
        st.error(f"Problem API: {e}")
        st.stop()
    if logs.empty:
        st.warning("API nie zwróciło występów w tym sezonie. Wybierz np. sezon 2025–26.")
        st.stop()
    if selected_p.get("team"):
        team = selected_p["team"].get("abbreviation")
    else:
        team = logs.loc[logs.player == player, "team"].iloc[-1]
    scheduled = schedule[(schedule.date == pd.Timestamp(selected_day)) & ((schedule.home_team == team) | (schedule.away_team == team))]
    if not scheduled.empty:
        opprow = scheduled.iloc[0]
        opponent = opprow["away_team"] if opprow["home_team"] == team else opprow["home_team"]
        st.success(f"Mecz z terminarza: {team} – {opponent} | {selected_day}")
    else:
        st.warning("Nie znaleziono meczu tej drużyny na wskazaną datę. Poniżej można zrobić jedynie analizę hipotetycznego matchupu.")
        choices = [t for t in tm.values() if t != team]
        opponent = st.selectbox("Hipotetyczny rywal", sorted(choices))
    injuries = pd.DataFrame([{
        "player": f"{(d.get('player') or {}).get('first_name','')} {(d.get('player') or {}).get('last_name','')}".strip(),
        "team": team, "status": d.get("status", "?"), "reason": d.get("description", ""),
        "updated_at": "Pobrano " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    } for d in injury_records])

if logs is None or player is None:
    st.stop()

if schedule is not None and not schedule.empty:
    day_matches = schedule[(schedule.date == pd.Timestamp(selected_day)) & ((schedule.home_team == team) | (schedule.away_team == team))]
    b2b = back_to_back(schedule, team, selected_day) if not day_matches.empty else None
else:
    b2b = None
played = logs[(logs.player == player) & (logs.status == "PLAYED")].copy()

st.subheader("Kontekst meczu")
c1, c2 = st.columns(2)
c1.metric("Zawodnik", player)
c2.metric("Matchup", f"{team} vs {opponent}")
c3, c4 = st.columns(2)
c3.metric("B2B drużyny", "TAK" if b2b is True else "NIE" if b2b is False else "NIEUSTALONE")
c4.metric("Występy w bazie", str(len(played)))
if b2b is None:
    st.caption("B2B nieustalone: brak potwierdzonego meczu na tę datę lub terminarza.")
elif mode == "Import CSV":
    st.caption("B2B obliczono na podstawie dostarczonego terminarza: upewnij się, że obejmuje również poprzedni dzień.")

with st.expander("Raport absencji i wiarygodność danych", expanded=True):
    if not injuries.empty and {"player", "status"}.issubset(injuries.columns):
        sel = injuries[injuries.player.isin([player, teammate] if teammate else [player])]
        if not sel.empty:
            st.dataframe(sel, use_container_width=True, hide_index=True)
        else:
            st.info("Brak statusu w dostarczonym raporcie ≠ potwierdzenie, że zawodnik jest zdrowy i dostępny.")
    else:
        st.info("Brak danych o aktualnych absencjach.")
    if mode == "BALLDONTLIE API":
        st.caption("API zwraca obecne raporty kontuzji. Historyczny status OUT/DNP nie jest dostępny w pobranych box score'ach, więc nie liczymy zmyślonych splitów.")

st.divider()
st.subheader("Prognoza zawodnika")
cc, dd, ee = st.columns(3)
with cc:
    use_absence = st.checkbox("Załóż nieobecność wybranego kolegi", value=bool(teammate)) if teammate else False
with dd:
    override = st.number_input("Minuty: 0 = estymacja automatyczna", min_value=0.0, max_value=53.0, step=0.5, value=0.0)
with ee:
    tax = st.checkbox("Uwzględnij polski podatek 12% od stawki", value=True)

analyses = {}
market_rows = []
for label, field in METRICS.items():
    analyses[field] = metric_forecast(logs, player, field, opponent, b2b,
                                      absent_mate=teammate if use_absence else None,
                                      minute_override=override if override > 0 else None)

metric_items = list(METRICS.items())
for i in (0, 2):
    metric_cols = st.columns(2)
    for col, (label, field) in zip(metric_cols, metric_items[i:i + 2]):
        a = analyses[field]
        if "error" in a:
            col.warning(a["error"])
        else:
            col.metric(label + " (est.)", f"{a['mean']:.1f}", help=f"Ostatnie 10: {a['mean_last10']:.1f}. Cała baza: {a['mean_season']:.1f}")

st.caption("Estymacja = produkcja na minutę (ostatnie 10 + sezon) × przewidywane minuty. Matchup i B2B są wyświetlane diagnostycznie; nie wprowadzamy niezweryfikowanych korekt. Wynik jest scenariuszem, nie obietnicą.")

with st.expander("Wpływ absencji — grał vs nie grał", expanded=True):
    if not teammate:
        st.info("Wybierz kolegę z drużyny, aby porównać wyniki.")
    else:
        splits = teammate_splits(logs, player, teammate)
        yes, no = splits["with"], splits["without"]
        st.write(f"**{player}** w meczach z **{teammate}** i w meczach z potwierdzonym OUT/DNP")
        rows = []
        for name, data in [("Kolega grał", yes), ("Kolega OUT/DNP", no)]:
            rows.append({"Scenariusz": name, "Meczów": data["n"],
                         "Minuty": round(data.get("minutes", float('nan')), 1),
                         "PTS": round(data.get("pts", float('nan')), 1),
                         "AST": round(data.get("ast", float('nan')), 1),
                         "REB": round(data.get("reb", float('nan')), 1),
                         "PRA": round(data.get("pra", float('nan')), 1)})
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.caption(f"Mecze o nieznanym statusie kolegi: {splits['unknown']}. Nie zaliczamy ich automatycznie do absencji.")
        if use_absence:
            st.info(analyses["pts"]["mate_note"])
        st.caption("To porównanie całych meczów (with/without), nie prawdziwy on/off w minutach. Różnica nie musi być skutkiem samej absencji.")

st.subheader("Porównanie z liniami bukmachera")
st.caption("Wpisz własną linię i kurs. Brak automatycznego pobierania kursów w v1.0. Bez walidacji modelu przewaga EV jest wyłącznie orientacyjna.")
for label, field in METRICS.items():
    a = analyses[field]
    if "error" in a:
        continue
    left, mid, right = st.columns([1.4, 1, 1])
    with left:
        line = st.number_input(f"{label}: linia O/U (0 = pomiń)", min_value=0.0, max_value=150.5, value=0.0, step=0.5, key="line_" + field)
    with mid:
        odds_over = st.number_input("Kurs OVER", min_value=1.01, max_value=25.0, value=1.85, step=0.05, key="odds_" + field)
    with right:
        odds_under = st.number_input("Kurs UNDER", min_value=1.01, max_value=25.0, value=1.85, step=0.05, key="odds_under_" + field)
    if line <= 0:
        continue
    if float(line).is_integer():
        st.caption(f"Uwaga: linia {line:g} może zakończyć się zwrotem stawki (push). MVP nie modeluje zwrotu — wybierz linię połówkową dla analizy EV.")
        continue
    over = p_over(a["mean"], a["sd"], line)
    for side, prob, odds in [("OVER", over, odds_over), ("UNDER", 1 - over, odds_under)]:
        ev = (prob * odds * (0.88 if tax else 1) - 1) * 100
        market_rows.append({"Rynek": label, "Strona": side, "Linia": line,
                            "Kurs": odds, "P(model)": f"{prob:.1%}", "EV*": f"{ev:+.1f}%", "_ev": ev})
if market_rows:
    if any(x.get("sample_low") for x in analyses.values()):
        st.warning("Próbka poniżej 12 rozegranych meczów — prognozy i EV mają bardzo wysoką niepewność.")
    market_rows.sort(key=lambda r: r["_ev"], reverse=True)
    st.dataframe(pd.DataFrame(market_rows).drop(columns=["_ev"]), hide_index=True, use_container_width=True)
    st.caption("* EV to wynik uproszczonego niekalibrowanego modelu z aproksymacją normalną. Nie jest potwierdzoną przewagą nad bukmacherem.")

st.divider()
t1, t2 = st.tabs(["📊 Ostatnie występy i matchup", "🗂️ Dane i metodologia"])
with t1:
    m = st.selectbox("Statystyka wykresu", list(METRICS), index=0)
    field = METRICS[m]
    last = played.sort_values("date").tail(15).copy()
    if not last.empty:
        last["Mecz"] = last.date.dt.strftime("%d.%m") + " vs " + last.opponent
        st.bar_chart(last.set_index("Mecz")[[field]])
        st.dataframe(last[["date", "opponent", "minutes", "pts", "ast", "reb", "pra"]].sort_values("date", ascending=False),
                     use_container_width=True, hide_index=True)
    a = analyses[field]
    st.write(f"Przeciwko **{opponent}**: **{a['opponent_n']} meczów** w bazie; średnia: **{a['opponent_avg']:.1f}**" if a.get("opponent_avg") is not None else "Brak historii meczów z wybranym rywalem.")
    st.caption("Próbka H2H często jest mała, dlatego wyświetlamy ją informacyjnie bez mechanicznej korekty prognozy.")
with t2:
    st.write("**Wymagane kolumny CSV logów:** `game_id,date,player,team,opponent,minutes,pts,ast,reb,status`.")
    st.write("`status`: PLAYED, OUT, DNP, QUESTIONABLE, DOUBTFUL, PROBABLE, UNKNOWN. Wpisuj OUT/DNP wyłącznie z potwierdzonego źródła. PLAYED musi mieć dodatnie minuty.")
    st.write("**Terminarz:** `game_id,date,home_team,away_team`. **Kontuzje:** `player,team,status,reason,updated_at`.")
    st.write("**Ważne ograniczenia:** zawodnik może wystąpić w kilku drużynach w sezonie; handel/transfery mogą zaburzyć splity. Aktualny raport absencji nie daje historii absencji. Play-by-play jest wymagany do prawdziwego on/off court.")
    st.write("**Walidacja:** przed wykorzystaniem do zakładów porównaj prognozy z wynikami przyszłych meczów, zamrażając dane i statusy dostępne przed rozpoczęciem spotkania.")
    st.write("**Źródła i dokumentacja:** https://docs.balldontlie.io/ · https://api.pbpstats.com/docs · https://www.nba.com/stats")
    st.download_button("Pobierz obecnie używane logi CSV", logs.drop(columns=["pra"], errors="ignore").to_csv(index=False).encode("utf-8-sig"), "nba_player_logs.csv", mime="text/csv")
    if not schedule.empty:
        st.download_button("Pobierz terminarz CSV", schedule.to_csv(index=False).encode("utf-8-sig"), "nba_schedule.csv", mime="text/csv")
