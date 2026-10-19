# NBA Props Lab v3 — Free Injury Watch (0–50 zł/mies.)

**Status 13.10.2026: PROTOTYP.** Przeszedł testy jednostkowe i symulacyjne; **nie przeprowadzono realnego pobrania aktualnego raportu PDF NBA, ani wysłania prawdziwego Telegrama**, ponieważ klucze i połączenia z serwerami nie są skonfigurowane. Ten etap NIE jest gotowym, zwalidowanym systemem sygnałów bukmacherskich.

## Co naprawdę działa, a co nie

| Funkcja | Status |
| --- | --- |
| Darmowa kontrola oficjalnego raportu NBA (PDF) | kod gotowy, integracja live do potwierdzenia na serwerze |
| Przechwytywanie `QUESTIONABLE/PROBABLE/DOUBTFUL → OUT` lub nowego OUT | przetestowane na raportach symulacyjnych |
| Brak powiadomień przy pierwszym pobraniu (baseline), deduplikacja | przetestowane |
| Kolejkowanie powiadomień na wypadek błędu Telegrama | implementacja, test logiczny |
| Wysłanie powiadomienia Telegram | integracja przygotowana; wymaga własnego tokenu |
| Wstępna lista potencjalnie dotkniętych graczy | wyłącznie na podstawie własnego pliku `data/impact_rules.csv` |
| Linie Superbet / STS / Fortuna / Betclic | CSV wpisany/zaktualizowany ręcznie, nie automatyczny feed |
| Odrzucanie kursów starszych niż 180 sekund | przetestowane |
| Porównywanie cen DLA TAKIEJ SAMEJ linii i strony | przetestowane |
| PTS/AST/REB/PRA, B2B i splity ze wcześniejszego v2 | zachowane w `app.py`; wymagają danych historycznych |
| Automatyczna, wiarygodna linia + EV + rekomendacja | **NIEDOSTĘPNE za 0 zł bez zweryfikowanego feedu kursów i modelu** |

**Nie należy mylić alertu „OUT” z sygnałem wartościowego zakładu.** Faktyczna publikacja NBA, nieprawidłowa interpretacja PDF, opóźnienie GitHub Actions i zawieszenie rynku mogą wystąpić niezależnie od siebie. Formalny raport NBA zwykle nie jest pierwszą publiczną informacją o kontuzji.

## Architektura kosztowa

- **0 zł/mies.**: GitHub Actions (standard runner w *publicznym* repozytorium) uruchamia `free_alert_worker.py` co 10 min w godzinach 16:00–07:59 UTC; przy zmianie statusu Telegram wysyła wiadomość. Snapshot z publicznymi statusami jest commitowany do repozytorium tylko przy zmianie. **Cron jest best-effort; zdarzają się opóźnienia lub pominięcia.** Nie jest to monitoring „w sekundę”.
- **0 zł/mies.**: opcjonalny panel Streamlit Community Cloud (zasypia po braku ruchu; **NIE** zastępuje workera), z zakładką „Darmowe alerty NBA”.
- **Orientacyjnie 20–50 zł/mies.**: mały VPS 24/7 daje stabilniejszy monitoring co ~5 min; wymaga konfiguracji bezpieczeństwa. Nie obejmuje płatnych feedów kursów. Bez VPS można uruchomić worker na własnym, włączonym komputerze.
- BDL Free **nie udostępnia endpointów NBA injuries ani player props**. Nie kupuj planu GOAT wyłącznie na podstawie makiety projektu: nie potwierdzono wszystkich polskich bukmacherów.

### Co pobieramy

Źródło: `https://official.nba.com/nba-injury-report-2026-27-season/`. Skrypt szuka najnowszego oficjalnego PDF, a jeśli strona nie ma dostępnych linków, próbuje typowe adresy plików NBA. Raporty są przypisane do konkretnego meczu, drużyny i zawodnika. Kiedy nie znajdzie pliku, dokument jest stary lub parser nie rozpoznaje wierszy, **raportuje błąd i nie generuje żadnego alertu**.

Uwaga: HTML, wzór nazw PDF i szablon raportu mogą się zmienić. Ten mechanizm musi zostać sprawdzony na prawdziwym PDF aktualnego sezonu. Serwis NBA może też ograniczyć automatyczne pobieranie; należy przestrzegać warunków źródła.

## Sposób A — lokalnie (najszybszy test)

Wymagany Python 3.11+ (testy uruchomione na Pythonie 3.13). Z katalogu wypakowanego projektu:

```bash
python -m pip install -r requirements.txt pytest
python -m pytest -q
python free_alert_worker.py --once
streamlit run app.py
```

**Pierwszy udany przebieg to rozgrzewka**, więc nie powinien wysłać starych OUT. Aby stale monitorować lokalnie:

```bash
python free_alert_worker.py --interval 300
```

W innym oknie terminala uruchom `streamlit run app.py` i wybierz stronę „Darmowe alerty NBA”. Na laptopie po jego uśpieniu monitoring przestaje działać.

## Sposób B — GitHub Actions 0 zł + Telegram

1. Załóż repozytorium GitHub **publiczne** (nie umieszczaj w nim prywatnych danych ani tokenów). Wgraj zawartość **katalogu** ZIP, łącznie z `.github/workflows/free-injuries.yml`, a nie sam ZIP. Publiczne Actions na standardowym runnerze są bezpłatne. Repozytoria prywatne mają limity minut i mogą przekroczyć darmowy próg.
2. Otwórz Telegram -> `@BotFather` -> `/newbot` -> skopiuj token bota. Napisz do swojego bota `/start`.
3. Aby poznać chat ID, wywołaj lokalnie (nie publikuj linku z tokenem!):

   ```bash
   python -c "import requests,os,json;print(requests.get('https://api.telegram.org/bot'+os.environ['TELEGRAM_BOT_TOKEN']+'/getUpdates').json())"
   ```

   Ustaw wcześniej zmienną `TELEGRAM_BOT_TOKEN` na swoim komputerze. `chat.id` znajdziesz w odpowiedzi. Możesz też użyć zaufanego bota do wyświetlenia własnego chat ID.
4. W GitHub: `Settings -> Secrets and variables -> Actions -> New repository secret` dodaj **TELEGRAM_BOT_TOKEN** oraz **TELEGRAM_CHAT_ID**. Nie zapisuj ich w pliku `.env` w repozytorium.
5. W `Actions -> NBA official injury alerts (free) -> Run workflow` wykonaj uruchomienie kontrolne. Sprawdź logi. Przed regularnym sezonem 2026/27 raport może nie istnieć lub być niekompletny; to nie jest błąd statystyczny.
6. Po pierwszym skutecznym sprawdzeniu skrypt zapisze `runtime/official_snapshot.json` w repozytorium (bez danych prywatnych). Następne przebiegi zgłoszą **zmiany**, a nie wszystkie stare statusy `OUT`.
7. Harmonogram w `.github/workflows/free-injuries.yml` sprawdza raport co 10 min między 16:00 a 07:59 **UTC** (w Polsce zwykle 18:00–09:59 latem, 17:00–08:59 zimą). Może zostać opóźniony przez GitHub.

**Test powiadomienia w rzeczywistej usłudze:** po wpisaniu sekretów możesz użyć polecenia `python -c "from free_alert_worker import send_telegram;print(send_telegram('NBA Props Lab - test połączenia'))"` lokalnie. Testujący nie powinien publikować sekretów ani pełnych adresów Telegram API.

### Udostępnienie panelu telefonowi

Na [Streamlit Community Cloud](https://share.streamlit.io/) wybierz repozytorium i `app.py` jako plik główny. W menu Streamlit otwórz stronę `2_Darmowe_alerty_NBA.py`. Dane statusów są pobierane z pliku stanu zapisanym w GitHub; aplikacja aktualizuje się zgodnie ze stanem repozytorium, ale nie jest monitorem 24/7. **Publiczny panel wystawia dane zapisane w repozytorium wszystkim odwiedzającym** — nie udostępniaj prywatnych logów ani tokenów.

## Sposób C — komputer lub VPS (bez GitHub Actions)

Wypełnij prywatny `.env` na bazie `.env.example`. Wariant Docker Compose w tym katalogu **uruchamia domyślnie darmowy `free_alert_worker.py`** zamiast płatnego BDL workera:

```bash
cp .env.example .env
# edytuj .env, NIE udostępniaj pliku
mkdir -p runtime
docker compose up -d --build
```

Dashboard pozostaje na `127.0.0.1:8501` i powinien być udostępniany zdalnie tylko przez prawidłowo zabezpieczony tunel/HTTPS z autoryzacją. Nie wystawiaj surowego portu publicznie. Na VPS monitoring ma ustawiony interwał 300 s (bez gwarancji publikacji źródła w tym czasie).

## Ręcznie uzupełniane linie czterech bukmacherów

`data/polish_odds_template.csv` to szablon. Jeśli chcesz używać ręcznie aktualizowanych kursów, utwórz `data/polish_odds.csv` o identycznym nagłówku:

```csv
game_date,game,player,market,bookmaker,side,line,odds,checked_at_utc
2026-10-20,BOS@NYK,EXAMPLE PLAYER,AST,Superbet,OVER,5.5,1.90,2026-10-20T18:40:00Z
```

**Ten wiersz jest fikcyjny i służy wyłącznie jako przykład formatu.** Czas musi być rzeczywistym momentem ręcznego sprawdzenia oferty, zapisanym w UTC; starsze niż 180 s oferty są oznaczane jako nieaktualne i nigdy nie trafią do alertu.

Aby pod wiadomością OUT pojawiła się nazwa potencjalnego beneficjenta absencji i jego RĘCZNIE wprowadzona linia, w `data/impact_rules.csv` wstaw regułę:

```csv
absent_player,team,affected_player,market,reason
EXAMPLE ABSENT,Boston Celtics,EXAMPLE PLAYER,AST,Ręcznie zweryfikowany wpływ rotacji
```

Nazwy zawodników muszą zgadzać się z oficjalnym raportem (np. `Jayson Tatum`); reguła wskazuje jedynie **kogo obserwować**, bez automatycznej prognozy statystycznej i bez potwierdzania przewagi. Aby polskie linie trafiały do alertu, potrzebny jest rzeczywiście aktualizowany plik `polish_odds.csv` w miejscu uruchamiania workera; GitHub repozytorium nie aktualizuje go samo.

Panel `2_Darmowe_alerty_NBA.py` porównuje **identyczne** linie i strony zakładu, nie traktuje wyższego kursu na wyższy próg jako lepszej oferty. Kolumna `min_probability_needed` podaje tylko matematyczny próg opłacalności przy 12% podatku od stawki (bez ofert promocyjnych): `1 / (kurs × 0.88)`. Nie jest to prawdopodobieństwo wyliczone przez model.

## Co dalej, gdy prototyp przejdzie test rzeczywistych raportów?

1. Zebrać i zwalidować historyczne box score + absencje; sprawdzić minutes/36, usage/possession, role i B2B; nie wyciągać wniosków o związku przyczynowym z prostych splitów 3 meczów.
2. Wytrenować model prognozy PTS, AST, REB w sposób walk-forward na niewidzianych meczach; zwalidować kalibrację over/under i oszacowanie błędu.
3. Uzyskać uprawniony, stabilny i **w budżecie** feed bieżących rynków player props u czterech polskich bukmacherów; bez tego nigdy nie obiecywać automatycznych alertów z realnym kursem.
4. Mierzyć opóźnienie od oficjalnej publikacji PDF do wiadomości Telegram oraz aktualność kursu w momencie jej otrzymania.

### Źródła

- https://official.nba.com/nba-injury-report-2026-27-season/
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- https://docs.github.com/en/billing/concepts/product-billing/github-actions
- https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app
- https://docs.balldontlie.io/

W projekcie zachowano kod v2 (płatny feed) tylko dla późniejszego rozwoju; nie jest uruchamiany domyślnie. Instrukcja v2: `docs/README_V2.md`.
