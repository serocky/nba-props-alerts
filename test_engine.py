# NBA Props Lab v2 — OUT Alerts (MVP)

**Status:** działający kod mechanizmu monitorującego + testy symulacyjne; nie jest to jeszcze uruchomiona usługa 24/7. Nie podłączono rzeczywistych kluczy, więc **nie potwierdzono integracji end-to-end z live NBA API i Telegramem**. Sygnały EV są prognozami niezwalidowanego modelu, a nie rekomendacjami inwestycyjnymi.

## Co robi v2

1. Co `POLL_SECONDS` (domyślnie 60 s) sprawdza poprzez BALLDONTLIE aktualny terminarz nadchodzących meczów NBA (do 24 h) i statusy kontuzji.
2. Pierwszy przebieg jest **rozgrzewką**: zapisuje istniejące OUT/QUESTIONABLE i NIE wysyła powiadomień o starych kontuzjach.
3. Gdy zauważy **nowy status OUT** (np. `QUESTIONABLE -> OUT` albo wcześniej niewidoczny zawodnik staje się OUT), zapisuje zdarzenie w SQLite i wysyła wstępne powiadomienie Telegram.
4. Próbuje odczytać bieżące linie PTS / AST / REB / PRA dla tego spotkania ze źródła BALLDONTLIE GOAT. Ignoruje rynki `milestone` i linie całkowite (push nieobsługiwany); uwzględnia wyłącznie dwustronne `over_under`.
5. Natychmiast po wykryciu OUT próbuje wysłać **watchlistę do 6 bieżących linii z dostawcy** (bez rekomendacji). Dopiero przy dostępnej, niezależnie zweryfikowanej historii absencji dopasowuje graczy według ID, pobiera archiwum ich występów, sprawdza B2B i analizuje zmianę statystyk.
6. **Automatyczny sygnał analityczny z linią/kursem** przechodzi przez twarde warunki: 12 występów zawodnika, min. 5 gier z nieobecnym kolegą i 3 potwierdzone gry bez niego, aktualny timestamp kursu, linia .5, modelowe EV po podatku >= 6%.
7. Wysyła maks. trzy sygnały na zdarzenie; zapamiętuje wysłane, aby unikać duplikatów.
8. Panel `pages/1_Alerty_live.py` pokazuje wyniki, błędy i ostatni cykl pracy workera.

**Ważne ograniczenia:** Dane BALLDONTLIE o kontuzji nie są zawsze przypisane do konkretnego meczu, dlatego `OUT` z feedu = **status wymagający potwierdzenia w raporcie meczowym**. Status OUT starszej kontuzji nie powinien automatycznie wywołać powiadomienia ponownie w następnym spotkaniu. Bez potwierdzonych historycznych absencji **zostanie wysłany alert kontuzji i watchlista świeżych linii (jeśli są dostępne), ale nie zostanie wygenerowany sygnał statystycznej przewagi**. Prawdopodobieństwa i EV nie są historycznie skalibrowane; brak podstaw do deklarowania przewagi nad rynkiem.

## Start bez API — działające demo

Python 3.10+:

```bash
python -m pip install -r requirements.txt
python demo_alert.py
python -m pytest -q
streamlit run app.py
```

`demo_alert.py` symuluje zmianę statusu fikcyjnego gracza `Jordan Demo` z `QUESTIONABLE` na `OUT`, uruchamia model względem fikcyjnego `Alex Sample` i wypisuje potencjalny alert z zmyśloną linią i kursem. Żadne liczby z tego testu nie są danymi NBA ani kursami rzeczywistego bukmachera. Pliki `data/demo_*.csv` są fikcyjne.

## Uruchomienie API + Telegram (lokalnie)

1. Zarejestruj konto na https://www.balldontlie.io/ i uzyskaj plan **GOAT NBA** (dokumentacja 13.10.2026: **39,99 USD/mies.**). ALL-STAR za 9,99 USD/mies. ma statystyki i kontuzje, ale **bez player props**, więc nie może zasilać pełnego alertu automatycznie.
2. Na Telegramie otwórz `@BotFather`, użyj `/newbot`, zachowaj token prywatnie. Otwórz czat z botem, wyślij `/start`. Odczytaj `chat_id` z odpowiedzi API `https://api.telegram.org/bot<TOKEN>/getUpdates` (nigdy nie udostępniaj tokenu ani pełnego adresu komuś innemu).
3. Ustaw w środowisku trzy sekrety: `BALLDONTLIE_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
4. Uruchom: `python alert_worker.py` (ten proces musi działać nieprzerwanie), a w innym terminalu `streamlit run app.py`.
5. Opcjonalnie wybierz z listy `/pages` podstronę alertową i sprawdzaj zarejestrowane sygnały.

Na start można skopiować `.env.example` do `.env`, ale plik `.env` musi pozostać **prywatny**. Lokalny worker uruchamiany zwykłym Pythonem **nie wczytuje .env automatycznie**: ustaw zmienne systemowe lub użyj Docker Compose.

### Hosting 24/7

Dla jednej maszyny z Docker Compose:

```bash
cp .env.example .env
# Wypełnij .env prawdziwymi sekretami, nie commituj go do repo
mkdir -p runtime
docker compose up -d --build
```

Na tej samej maszynie:
- `dashboard` wyświetla panel na porcie 8501,
- `alerts` odpytuje dostawcę danych co 60 s,
- wspólny wolumen SQLite `./runtime` przechowuje historię i eliminuje duplikaty.

**Uwaga dot. bezpieczeństwa:** publiczne wdrożenie wymaga reverse proxy HTTPS, hasła/autoryzacji do panelu, firewall i kopii SQLite. Nie wystawiaj surowego portu 8501 publicznie. Streamlit Community Cloud **nie jest usługą dla workera 24/7**; otwieranie panelu na telefonie nie zastępuje niezależnego serwera.

## Dane historyczne with/without

Dodatkowo do API można dostarczyć `data/verified_absences.csv` (brak domyślnie). Format jak w danych demonstracyjnych:

```csv
game_id,date,player,team,opponent,minutes,pts,ast,reb,status,home
18440001,2025-11-01,Imie Nazwisko,BOS,NYK,,,,,OUT,True
```

Wpisy muszą pochodzić z weryfikowalnego historycznego raportu nieobecności **dla dokładnego game_id**. Bez tego model nie wie, czy zawodnik nie grał z powodu kontuzji, odpoczynku czy niedostępności danych. Pojedyncze sprzedane i nieaktualne kursy nie upoważniają do wysyłania sygnału, jeśli `updated_at` jest starszy niż `MAX_ODDS_AGE_SECONDS`.

**Automatyczne pozyskanie całej historii potwierdzonych OUT jest jeszcze do wykonania**. Same statystyki `stats` obejmują tylko zarejestrowane występy, a więc nie ma w nich wiarygodnej listy nieobecności. Obecna integracja jest fundamentem obsługi wydarzeń, a nie kompletnym wytrenowanym modelem betowym.

## Źródła kursów

BALLDONTLIE API `GET /v2/odds/player_props` obsługuje wybranych głównie amerykańskich bukmacherów, **nie Superbet**. Dla Superbetu nie potwierdziliśmy legalnego, stabilnego feedu API; linie z DraftKings/FanDuel nie są automatycznie kursami, które możesz zawrzeć w Polsce. Dla rzetelnych alertów dotyczących Superbetu trzeba oddzielnie uzyskać dostęp do autoryzowanego feedu (lub wdrożyć ręczną weryfikację linii).

## Uwagi metodologiczne

- `engine.py` to prototyp; nie jest to model wytrenowany ani skalibrowany na danych out-of-sample.
- `B2B` identyfikuje terminarz, ale nie jest arbitralnie przeliczany na -X% PTS. Matchup również jest diagnostyczny w v1/v2.
- Przy `2+` nieobecnościach jednocześnie efekt nie jest sumą dwóch splitów. System ocenia każdego osobno, a to ograniczenie trzeba poprawić modelem lineup-level.
- Model dopuszcza EV >= 6% po domyślnym 12% podatku, lecz wynik może być fałszywy przy błędnej kalibracji.
- Nie obsługuje wysyłania zleceń zakładów. Nie publikuje żadnych fikcyjnych live kursów.
- Nie gwarantuje alertu w sekundzie ogłoszenia OUT: obserwacja trwa do interwału pollingu **plus opóźnienie źródła**. Rynek może zawiesić lub wycofać linię, zanim alert dotrze.

## Przyszłe etapy

1. Rzetelna baza historyczna `roster/game_id/OUT` + dane WOWY on/off (np. pbpstats), potwierdzenie źródła kontuzji dla dokładnego spotkania.
2. Zweryfikowany feed kursów realnie dostępnych w Polsce (Superbet — jeśli uzyskamy dostęp) i audyt timestampów/zmian po newsach.
3. Modele minut i rates w zależności od lineup, `usage`, `potential assists`, matchup/pace/B2B.
4. Walk-forward backtesting z wynikiem po podatku, odsetkiem fałszywych alarmów, średnią zwłoką alertu, CLV i logami alert-to-price.
