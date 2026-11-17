# pykdpw — dane obligacji z KDPW RZE

Niezależna paczka Python do pobierania publicznego eksportu Rejestru Zobowiązań Emitentów KDPW.
Każde uruchomienie `fetch` próbuje pobrać jeden ZIP z XML-em, zachowuje oryginał
i importuje obligacje oraz ich świadczenia do lokalnej bazy SQLite.
Program przeznaczony jest do uruchamiania raz dziennie. Nie instaluje harmonogramu.

## Instalacja i uruchomienie

Python **3.12 lub nowszy**, Windows lub Linux. W aktywnym środowisku Pythona
zainstaluj paczkę i pobierz dane:

```shell
python -m pip install pykdpw
pykdpw fetch
pykdpw nominal
pykdpw status
```

Pliki wydań i kolejne wersje są dostępne w [GitHub Releases](https://github.com/solek13/kdpw/releases).
Polecenie `python -m pykdpw` wykonuje te same operacje co `pykdpw`.
Do pobierania danych nie potrzeba konta KDPW ani klucza API.

Domyślny katalog danych to `./data`, względem bieżącego katalogu procesu.
Przy regularnym uruchamianiu wskazuj **ten sam bezwzględny katalog**:

```shell
pykdpw fetch --data-dir /pelna/sciezka/do/danych
```

Na Windows użyj swojej ścieżki, np. `C:\dane\rze`. Zawsze wskazuj ten sam katalog
również w poleceniach `status`, `nominal` i `import`.

| Polecenie | Działanie |
| --- | --- |
| `fetch` | Jedna próba pobrania eksportu i import do SQLite |
| `import PLIK.zip` | Zapis i import lokalnego archiwum, bez dostępu do internetu |
| `status` | Ostatnia próba, ostatni udany import, daty raportu, liczebności i blokady |
| `nominal [--run-id ID]` | Lokalna suma podanego nominału aktywnych obligacji według walut i raport braków |
| `resume` | Usunięcie wstrzymania po HTTP 401/403 po ręcznym sprawdzeniu przyczyny |

Każde polecenie przyjmuje `--data-dir`. Wynik jest JSON-em na standardowym wyjściu,
a komunikaty postępu i błędy trafiają na standardowe wyjście błędów.
Kody zakończenia: `0` sukces lub wykorzystana już dzienna próba, `1` błąd,
`2` aktywne wstrzymanie/czas oczekiwania, `130` przerwanie przez użytkownika.

## Użycie z Pythona

```python
from pathlib import Path
from pykdpw import Collector, nominal_report

data_dir = Path("data")
collector = Collector(data_dir)
result = collector.fetch()  # To samo ograniczenie pobierania co w CLI.
print(result["status"])
print(collector.status())

# Wyłącznie lokalne obliczenie, gdy w bazie jest już udany import.
report = nominal_report(data_dir)
print(report["by_currency"])
```

`Collector.import_local(Path("raport.zip"))` importuje posiadany plik bez internetu.
`nominal_report(data_dir, run_id=1)` odczytuje wybraną obserwację historyczną.
`CollectorError` jest dostępny w `pykdpw`; błędy sieci i danych wejściowych mogą
również pochodzić ze standardowych modułów Pythona.

## Źródło i zakres

- [Publiczny RZE](https://data.kdpw.pl/oth/rze/info/).
- [Oficjalny eksport ZIP/XML](https://data.kdpw.pl/oth/rze/info/api/file/getxml).
- [Opis usługi i bezpłatnego dostępu bez konta](https://www.kdpw.pl/uploads/user_files/dokumenty/rejestr-zobowiazan-emitentow.pdf).
- [Opis dziennej aktualizacji danych](https://www.kdpw.pl/pl/zobowiazania-emitentow.html).
- [Dokumentacja komunikatu reda.dbt.001.02](https://www.kdpw.pl/uploads/reda-dbt-001-02.html).

Archiwum zachowuje **cały eksport RZE**. Baza zawiera emitentów mających obligacje
oraz wszystkie ich instrumenty `SctyTp=BOND` obecne w eksporcie, również nieaktywne.
Listy zastawne (`MRBD`) i certyfikaty inwestycyjne (`CRIN`) pozostają w archiwum;
liczebności raportu obejmują je osobno od liczebności obligacji.

Zachowywane są dane emitenta, identyfikatory ISIN lub identyfikatory wewnętrzne,
CFI/FISN, parametry emisji, terminy, kwoty i waluty, kalendaria, realizacja
świadczeń oraz szczegóły serii/subskrypcji/ofert, jeżeli występują w źródle.
Pełny zakres pól każdego rekordu pozostaje również w `payload_json`.
Szczegóły serii są w `instrument_versions.payload_json`, pod kluczem `Srs`.

„Wszystkie” oznacza wszystkie obligacje **opublikowane przez KDPW w tym eksporcie**.
Nie ma gwarancji, że emitenci zgłosili każdą emisję. Kolektor nie odtwarza dawnych
stanów rejestru; historia obserwacji zaczyna się od pierwszego uruchomienia.

## Ograniczenie ruchu

- Maksymalnie jedna próba sieciowa na dzień kalendarzowy `Europe/Warsaw`, także po błędzie.
- Jedno żądanie oficjalnego eksportu; program nie odwiedza stron poszczególnych emitentów.
- Limit jest zapisany w SQLite **przed żądaniem**. Restart procesu go nie usuwa.
- Blokada systemowa zapobiega równoległym zapisom w jednym katalogu danych.
- Brak automatycznych ponowień i podążania za przekierowaniami HTTP.
- HTTP 429 wstrzymuje dostęp do terminu `Retry-After`, co najmniej do następnego dnia polskiego.
- HTTP 401/403 wstrzymuje następne pobrania do ręcznego `resume`.
- `resume` zachowuje dzienny limit i czas oczekiwania po 429.
- Jawny `User-Agent`, zwykła weryfikacja certyfikatu TLS, 60 s oczekiwania na operację
  sieciową i 300 s budżetu pobierania sprawdzanego między blokami danych.

To konserwatywna polityka tego programu, **nie limit ogłoszony przez KDPW**.
Nie gwarantuje braku blokady. Używaj jednego katalogu danych i jednej instalacji
pobierającej; inne katalogi/maszyny nie współdzielą limitu. Nie usuwaj bazy,
aby wymuszać kolejne próby. Program nie stosuje rotacji adresów ani obchodzenia zabezpieczeń.

## Nominał aktywnych obligacji

```powershell
.\.venv\Scripts\python.exe -m pykdpw nominal
.\.venv\Scripts\python.exe -m pykdpw nominal --run-id 1
```

Raport działa wyłącznie na lokalnej bazie. Domyślnie wybiera ostatnią udaną
obserwację; `--run-id` wybiera konkretny zapis historyczny. Sumuje `NmnlVal`
dla obligacji `BOND` ze statusem `ACTV`, osobno dla każdej waluty, używając
`Decimal`. Jest to **suma podanego nominału aktywnych wpisów w RZE**.
`NmnlVal` to wartość nominalna długu całej emisji — nie mnożymy jej ponownie
przez `Qty`, nie dodajemy odsetek ani kwot z kalendariów świadczeń.
Nie wykluczamy emisji wyłącznie dlatego, że minęła data zapadalności.

Wynik zawiera datę obserwacji i datę danych KDPW, kwoty `by_currency`, liczbę
uwzględnionych rekordów i `quality`: braki nominału lub waluty, niepoprawne kwoty,
nieznane statusy oraz powtarzające się identyfikatory instrumentu przy tych samych
opublikowanych danych identyfikacyjnych emitenta. Nie ma automatycznego usuwania
takich duplikatów ani zastępowania braków zerem. Status `partial` oznacza, że
sumy są obliczone dla dostępnych poprawnych wartości i wymagają uwzględnienia
raportu jakości; samo wyliczenie raportu nadal kończy się kodem `0`.

`NACT` jest wykluczony z sumy także wtedy, gdy źródło nadal zawiera dodatni nominał;
liczba takich wpisów jest pokazywana oddzielnie. To nie jest wycena rynkowa ani
pełna statystyka wszystkich obligacji w Polsce. Jedna suma w PLN wymaga
osobnego wyboru źródła i daty kursów walutowych. Polecenie nie pobiera kursów.

## Archiwum i historia

```text
data/
  rze.sqlite3
  .collector.lock
  archives/
    2026-11-17/
      00000001/
        source.zip
        manifest.json
```

`source.zip` to oryginalne bajty pobranego lub importowanego pliku.
`manifest.json` opisuje próbę, jej źródło, czas, hash ZIP-a i XML-a, datę raportu,
liczebności oraz wynik. Nieudana próba też ma zapis w bazie i manifest;
przy nieudanym pobraniu kompletny ZIP może nie istnieć.
Baza jest źródłem prawdy o zakończeniu importu, również gdy proces zakończy się
między zatwierdzeniem transakcji a aktualizacją manifestu.

Rozróżniamy:

- `observation_date` — dzień uruchomienia w Polsce;
- `started_at` — czas rozpoczęcia próby w UTC;
- `report_date` — data stanu bazy z `RptDtTm` w XML, bez zgadywania z nazwy ZIP-a;
- `created_at_source` — oryginalna data utworzenia komunikatu przez KDPW.

Brak źródłowej daty pozostaje `NULL`. Stary raport pobrany w weekend daje nową
obserwację tego samego raportu. Zmiana raportu z tą samą datą tworzy nową wersję;
poprzednia pozostaje dostępna. Nie ma udawanego zapisu za dni, kiedy program nie działał.

XML jest czytany strumieniowo bez rozpakowywania go na dysk. Limity wejścia wynoszą
128 MiB ZIP i 2 GiB XML. Akceptowany jest dokładnie jeden plik XML w archiwum,
z przestrzenią nazw `urn:kdpw:xsd:reda.dbt.001.02`. DTD i encje są zabronione.
Importer kontroluje strukturę i pola konieczne do poprawnego przypisania danych;
nie wykonuje pełnej walidacji XSD ani nie potwierdza merytorycznej poprawności danych emitenta.

Przy sprawdzeniu eksportu z 2026-11-16 ZIP miał około 21 MB, a XML około 478 MB.
Same dzienne ZIP-y przy takim rozmiarze zajmą około 7,6 GB rocznie.
Baza zajmuje dodatkowe miejsce zależne od liczby zmian. Nie ma automatycznego
usuwania historii ani kopii zapasowych — ich wykonanie pozostaje po stronie operatora.

## SQLite

| Tabela/widok | Zawartość |
| --- | --- |
| `runs` | Wszystkie próby sieciowe i importy lokalne, statusy, archiwa i przypisanie raportu |
| `reports` | Niepowtarzające się XML-e według SHA-256, daty i statystyki |
| `issuer_versions` | Niezmienne wersje danych emitentów |
| `instrument_versions` | Niezmienne wersje obligacji, hash obejmuje też świadczenia |
| `payments` | Wszystkie wystąpienia świadczeń danej wersji instrumentu |
| `report_issuers`, `report_instruments` | Skład każdego raportu z pozycjami w źródłowym XML |
| `daily_bonds` | Obligacje każdej zakończonej powodzeniem obserwacji |
| `control` | Trwały limit prób i wstrzymania |

Identyczna wersja instrumentu współdzieli parametry i świadczenia między raportami.
Zmiana któregokolwiek pola instrumentu lub jego kalendarza tworzy pełną nową wersję
tego instrumentu. Przetwarzanie raportu jest transakcyjne: błąd cofa cały import,
a archiwum i zapis błędu pozostają. Powtórzony XML nie duplikuje raportu.

Kwoty, oprocentowanie i identyfikatory są zapisane jako tekst, aby zachować
dokładność i zera wiodące. Do obliczeń pieniężnych używaj `decimal.Decimal`,
nie `float` ani SQLite `REAL`. Brak pola (`NULL`) różni się od `0.00` i pustego pola.
Źródłowe daty zastępcze, np. `0001-01-01`, pozostają dosłowne i nie oznaczają
rzeczywistej daty zapadalności. `OnTmInd`/`timely_update` oznacza **terminowość
aktualizacji informacji**, nie terminowość płatności.

Agregaty emitenta są wartościami źródłowymi dla jego grupy instrumentów;
nie są ponownie obliczane jako suma wyłącznie obligacji.
Sam `SctyId` nie jest kluczem unikalnym w całej historii. Raport zachowuje
każde wystąpienie instrumentu i świadczenia, także identyczne duplikaty.
Pozycje w XML (`ordinal`) służą do odtworzenia raportu; nie są stałą tożsamością
między dniami. Przy porównaniach używaj identyfikatorów emitenta i instrumentu
oraz typu rejestracji, uwzględniając niejednoznaczności w danych źródłowych.

Przykładowe zapytania:

```sql
-- Udane obserwacje: data pobrania i niezależna data danych KDPW.
SELECT r.id, r.observation_date, p.report_date, r.source
FROM runs r JOIN reports p ON p.id = r.report_id
WHERE r.status = 'complete'
ORDER BY r.id DESC;

-- Obligacje ostatniego udanego uruchomienia.
SELECT issuer_name, issuer_lei, security_id, status,
       nominal_amount, nominal_currency, maturity_date
FROM daily_bonds
WHERE run_id = (SELECT MAX(id) FROM runs WHERE status = 'complete');

-- Wszystkie świadczenia jednej obligacji z określonej obserwacji.
SELECT b.issuer_name, b.security_id, p.*
FROM daily_bonds b
JOIN payments p ON p.instrument_version_id = b.instrument_version_id
WHERE b.run_id = 1 AND b.security_id = 'WYBRANY_IDENTYFIKATOR'
ORDER BY b.issuer_ordinal, b.instrument_ordinal, p.ordinal;
```

W jednym dniu mogą istnieć importy lokalne oprócz pobrania sieciowego.
Do jednoznacznego wyboru obserwacji używaj `run_id`; samo filtrowanie po dniu
może zwrócić kilka obserwacji.

## Błędy i ponowny import

```powershell
.\.venv\Scripts\python.exe -m pykdpw status
.\.venv\Scripts\python.exe -m pykdpw import .\data\archives\2026-11-17\00000001\source.zip
```

Lokalny import działa bez internetu, również przy aktywnym wstrzymaniu pobierania.
Tworzy nową obserwację z datą bieżącego uruchomienia i zachowuje datę raportu z XML.
Po awarii system operacyjny zwalnia blokadę; następne polecenie zapisujące dane
oznacza niedokończoną próbę jako `interrupted`. Nie usuwa to limitu pobierania.

## Testy

Instalacja deweloperska z lokalnej kopii repozytorium:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[test]'
.\.venv\Scripts\python.exe -m pytest -q
```

Na Linuksie odpowiednikiem interpretera jest `.venv/bin/python`.
Testy korzystają z syntetycznych XML-i i lokalnego serwera HTTP. Nie łączą się z KDPW.
CI sprawdza Windows i Linux na Pythonie 3.12, 3.13 i 3.14.
Archiwa, SQLite, logi i środowisko wirtualne są wykluczone z Git.

## Licencja i publikacja

Kod i dokumentacja projektu są dostępne na licencji [MIT](https://github.com/solek13/kdpw/blob/main/LICENSE).
Dane KDPW nie są dołączone do paczki; licencja projektu nie nadaje praw do danych KDPW.

Instrukcja budowania wydań i konfiguracji publikacji na PyPI znajduje się w
[docs/publishing.md](https://github.com/solek13/kdpw/blob/main/docs/publishing.md).
