# AGENTS.md — Enea Licznik Integration

## Konwencje ogólne

- Wszystkie stałe konfiguracyjne i behawioralne trzymaj w **`const.py`** — nie definiuj stałych modułowych w innych plikach, jeśli mają szerszy kontekst.
- Zachowuj w `const.py` następującą kolejność sekcji:
  1. **Integration identity** — `DOMAIN`, `PLATFORMS`, `DEFAULT_NAME`
  2. **API URLs** — `BASE_URL`, `URL_*`
  3. **Config entry keys** — `CONF_*`
  4. **Defaults** — `DEFAULT_*`
  5. **Statistics API** — `MEASUREMENT_ID_*`, `MeasurementType`, `Resolution`, `BACKFILL_*`, `RANGE_FETCH_CHUNK_DAYS`
- Każda nowa funkcja, metoda i klasa musi mieć **docstring**.
- **Nie twórz metod będących wyłącznie wrapperami** — jeśli metoda X robi tylko `return await self.Y()`, spłaszcz X i Y w jedną metodę. Wyjątek: gdy HA wymusza nazwę metody jako punkt wejścia (np. `async_step_reauth` z `entry_data`), użyj rozróżnienia po zawartości parametru zamiast tworzyć osobną metodę `_confirm`.

## Konwencje nazewnictwa

- Zawsze używaj **"Portal Odbiorcy Enea"** (z wielkiej litery) — nigdy "portal Enea" ani "portal". Dotyczy tekstów UI, tłumaczeń, komentarzy i dokumentacji.
- W angielskich tekstach: **"Portal Odbiorcy Enea"** (nazwa własna, bez tłumaczenia), np. "from the Portal Odbiorcy Enea".

## Przegląd projektu

Niniejszy projekt to custom component dla Home Assistant integrujący liczniki zdalnego odczytu (AMI) Enea Operator przez nieoficjalne REST API Portalu Odbiorcy Enea.

## Struktura projektu

```
custom_components/enea/
├── __init__.py      — setup/unload entry, EneaRuntimeData, EneaConfigEntry, _matching_coordinators, serwisy refresh/backfill
├── connector.py     — klient HTTP (EneaApiClient, _request helper), wyjątki, get_active_meter(), format_address()
├── coordinator.py   — EneaUpdateCoordinator: dane sensorów + pobieranie/wstrzykiwanie statystyk, _async_inject_days, async_backfill; klient API jako self.client
├── config_flow.py   — EneaConfigFlow: krok "user", "select_meter", "configure", reconfigure, reauth; EneaOptionsFlow; _validate_options, _async_validate_and_update_credentials
├── sensor.py        — EneaSensor, EneaEnergySensor, EneaBillSensor, SENSOR_DESCRIPTIONS, _address_attrs, _meter_model_attrs, _get_reading_date
├── date.py          — EneaBillDateEntity (Platform.DATE): edytowalne daty odczytu z RestoreEntity
├── billing.py       — PricesConfig, BillEstimate, find_prices_config, async_estimate_bill; szacowanie rachunku z long-term statistics
├── statistics.py    — async_insert_historical_statistics, _collect_series, _inject_energy_series, _inject_power_series, write_cumulative_series + _shift_later_totals (wspólny zapis serii skumulowanej dla energii i kosztów)
├── costs.py         — async_insert_cost_statistics, async_get_cost_latest_date, async_cost_days_missing, _inject_cost_series, get_cost_statistic_name, find_tariff_group
├── diagnostics.py   — async_get_config_entry_diagnostics (z wymuszonym odświeżeniem)
├── services.yaml    — definicja akcji "refresh" i "backfill"
├── const.py         — DOMAIN, URLs, klucze konfiguracji, stałe statystyk, stałe kosztów (ENEA_PRICES_DOMAIN, UNIT_COST, COST_ZONE_DISPLAY, VAT_RATE, BILL_KEY_*)
├── manifest.json    — metadane integracji (wymagane przez HA/HACS/hassfest)
└── translations/
    ├── en.json      — angielski (kopia strings.json)
    └── pl.json      — polski
```

**Kluczowa zasada podziału źródeł danych:**
- `/consumptionDashboard/ppe/{id}` → **wyłącznie sensory** (aktualne odczyty, info o liczniku)
- `/consumption/{id}/...` → **wyłącznie statystyki** (dane historyczne godzinowe, resolution=2)

## Architektura statystyk

Statystyki historyczne są wstrzykiwane jako **external statistics** (poza systemem encji HA) przez `async_add_external_statistics`. Dzięki temu Energy Dashboard może wyświetlać dane z prawidłowymi timestampami (godzinowa granularność — HA wymaga pełnych godzin dla external statistics) niezależnie od częstotliwości pollingu.

- Coordinator co każde odświeżenie sprawdza aktualność statystyk przez `get_last_statistics` — odpytuje wszystkie aktywne serie (energy_consumed/returned, power_consumed/returned) **równolegle** (`asyncio.gather`) i bierze najnowszą datę.
- Jeśli nie ma danych do wczoraj — pobiera brakujące dni i wstrzykuje.
- Backfill przy pierwszym uruchomieniu: zawsze pobiera maksymalną dostępną historię. Odbywa się jako **background task** (`hass.async_create_task`) — nie blokuje pierwszego odświeżenia koordynatora, sensory stają się dostępne natychmiast. Task jest cancellowany przy unload entry (`entry.async_on_unload`).
- "Ile się da" = gdy `assemblyDate` jest znane — fetch od daty montażu do wczoraj jednym zakresem; gdy nieznane — cofaj się chunkami 180-dniowymi, zatrzymaj gdy początek chunka zawiera 7 kolejnych dni bez danych.
- Pobieranie danych odbywa się przez **range endpoint** (`/consumption/{id}/{startDate}/{endDate}/{mtype}/{resolution}`), który zwraca dane za wiele dni naraz. Zakres jest dzielony na chunki `RANGE_FETCH_CHUNK_DAYS = 180` dni przetwarzane sekwencyjnie; w każdym chunku 2–4 żądania HTTP są wysyłane **równolegle** (`asyncio.gather`) — po jednym na typ pomiaru. Wydajność: ~2s na 6 miesięcy, ~5.5s na rok.
- Odpowiedź range endpoint to płaska lista slotów godzinowych. **Liczba slotów na dobę NIE jest stała** — w dniu zmiany czasu doba ma 23 lub 25 godzin (API nie dopełnia do 24). `_split_range_response` grupuje sloty po **rzeczywistej dacie** wyliczonej z `integrationEnd` (a nie po sztywnych blokach 24) → odporne na DST. Wynik to per-day dicty `{"values": [...], "zones": [...]}` identyczne ze strukturą single-day, więc `has_data`, `_collect_series` i koszty nie wymagają zmian. Dla dużych zakresów dane bywają w `valuesToTable` (a `values` może zawierać krótki, częściowy wycinek) — kod bierze **dłuższe** z pól `values`/`valuesToTable`. Dokładny czas slotu liczy `slot_start_dt(entry)` z `integrationEnd` (nie z `timeId`) — patrz niżej.
- Manualny backfill dowolnego zakresu dat: akcja `enea.backfill` (patrz Akcje).
- `has_data` zwraca `False` gdy odpowiedź API zawiera wyłącznie wartości `null` (`if item.get("value") is not None`). Zera są traktowane jako dane (zerowe zużycie) — dni z zerowym zużyciem są importowane. Filtrowanie danych starego licznika odbywa się przez `_strip_pre_assembly_slots` na poziomie godzin, nie przez `has_data`.
- **Dni bez danych (`has_data() == False`)** — Portal Odbiorcy Enea czasem w ogóle nie publikuje danych za dany dzień (potwierdzone przypadki trwałych dziur, nie tylko opóźnień). `_fetch_range` w `coordinator.py` obsługuje to przez parametry `zero_fill_stale`/`grace_days`: dzień bez danych młodszy niż `MISSING_DAY_GRACE_DAYS` (`const.py`, domyślnie 3 dni) jest pomijany jak dotychczas (dane zwykle pojawiają się po ok. 11:00 następnego dnia — patrz niżej); dzień starszy jest traktowany jako trwale brakujący i wstrzykiwany z wyzerowanymi wartościami (`_zero_fill_missing_day` — nadpisuje `null` na `0.0` w istniejących slotach, nie tworzy sztucznych slotów) zamiast być pomijany. Dzięki temu luka nie zeruje skumulowanej sumy (`running_sum`) kolejnych dni w `_inject_energy_series`/`_inject_cost_series` (zob. sekcja Architektura kosztów). Ponowne uruchomienie `enea.backfill` dla tego samego zakresu zawsze odpytuje API na nowo i nadpisuje wyzerowany dzień prawdziwymi danymi, jeśli się później pojawią. Skanowanie wsteczne w `_fetch_days_backward` (gdy `assemblyDate` jest nieznane) celowo używa `zero_fill_stale=False` — polega na prawdziwym braku danych, żeby wykryć granicę początku historii licznika.

### Dolna granica fetchowania — assemblyDate

Coordinator przechowuje `_assembly_datetime: datetime | None` — pełny timestamp montażu aktywnego licznika w lokalnej strefie czasowej (wpis w `meters[]` bez `disassemblyDate`). Pole `assemblyDate` z API jest w ms od epoki. Data jest dostępna jako `self._assembly_datetime.date()`.

**Dolna granica na poziomie dni:**
- `_fetch_days_forward` i `_fetch_range`: `start_date = max(start_date, self._assembly_datetime.date())` — nie fetchuje dni sprzed montażu
- `_fetch_days_backward` (tryb "ile się da", assembly date znane): deleguje do `_fetch_days_forward(assembly_date, yesterday)` — nie potrzeba cofania

**Filtr godzinowy dla dnia montażu — `_strip_pre_assembly_slots`:**

Dzień montażu jest fetchowany, ale zawiera dane zarówno starego (godziny przed montażem), jak i nowego licznika (godziny po montażu). Metoda `_strip_pre_assembly_slots(day, day_data)` usuwa timeId `<= assembly_datetime.hour` z odpowiedzi API dla dnia montażu:

```
timeId N = godzina (N-1):00–N:00
montaż o 12:13 → cutoff = 12 → wyrzuca timeId 1–12 (0:00–12:00), zostawia 13+ (12:00 wzwyż)
```

Metoda jest aplikowana w `_fetch_days_forward` i `_fetch_days_backward` bezpośrednio przed dołączeniem dnia do listy wyników. Dotyczy wszystkich ścieżek: inicjalny backfill, uzupełnianie luk, koszty (`_async_inject_missing_costs` korzysta z `_fetch_days_forward`).

### Nazwy statystyk

Format: `enea:{meter_code}_{slugified_name}`, np. `enea:590310600000000001_energia_pobrana`.

| Nazwa | statistic_id (przykład) | Jednostka |
|-------|-------------------------|-----------|
| Energia pobrana | `enea:..._energia_pobrana` | kWh |
| Energia pobrana – Dzień | `enea:..._energia_pobrana_dzien` | kWh |
| Energia pobrana – Noc | `enea:..._energia_pobrana_noc` | kWh |
| Energia oddana | `enea:..._energia_oddana` | kWh |
| Moc pobrana | `enea:..._moc_pobrana` | kW |
| Moc pobrana – Dzień | `enea:..._moc_pobrana_dzien` | kW |
| (analogicznie oddana) | | |

Nazwy stref (`Dzień`, `Noc`, …) są **dynamiczne** — pobierane z pola `zones[].name` w odpowiedzi API. Kod nie zakłada żadnej konkretnej taryfy (działa z G11, G12, G13 i innymi).

## Architektura kosztów

Koszty energii są funkcją opcjonalną — integracja współpracuje z zewnętrzną integracją `enea_prices`, jeśli jest zainstalowana. Brak `enea_prices` nie powoduje żadnych błędów ani ograniczeń funkcjonalności.

### Integracja z enea_prices (duck typing)

`find_tariff_group(hass, tariff_name)` w `costs.py` wyszukuje obiekt `TariffGroup` z domeny `enea_prices` przez duck typing — bez importu modułu. Wzorzec:

```python
for entry in hass.config_entries.async_entries(ENEA_PRICES_DOMAIN):
    if entry.data.get("tariff") != tariff_name:
        continue
    tariff = getattr(getattr(entry, "runtime_data", None), "tariff", None)
    if tariff is not None:
        return tariff
```

Dzięki temu `enea_prices` nie jest twardą zależnością i integracja nie wymaga wpisu w `manifest.json`.

### Statystyki kosztów = statystyki zewnętrzne (jak energia)

Statystyki kosztów używają **`async_add_external_statistics`** z `source=DOMAIN` i `statistic_id` w formacie `enea:{meter_code}_{slugify(name)}` (np. `enea:..._koszt_energii_pobrana_dzien`) — **dokładnie jak statystyki energii**. Nazwa budowana jest przez `get_cost_statistic_name(direction, zone_str)` = `f"Koszt energii {direction} – {zone_display}"`, więc `statistic_id` powstaje przez wspólne `get_statistic_id(meter_code, name)` ze `statistics.py`.

**Dlaczego nie `async_import_statistics` (source="recorder") jak wcześniej:** podpięcie statystyk pod `statistic_id` będący `entity_id` encji z `state_class` powodowało, że **rekorder HA sam kompilował** długoterminowe statystyki dla tej encji (zwykły `INSERT`), kolidując z naszymi wstrzyknięciami → `UNIQUE constraint failed: statistics.metadata_id, statistics.start_ts`. A że rekorder zapisuje wszystkie encje w jednej transakcji, jeden taki konflikt wywalał `session.flush()` i blokował zapis statystyk **wszystkim** encjom (nie tylko Enea). Statystyki zewnętrzne nie są powiązane z encją, więc rekorder ich nigdy nie kompiluje — problem znika u źródła.

W Energy Dashboard koszt wybierasz przez **„Użyj encji śledzącej całkowite koszty"** — lista pokazuje statystyki w walucie HA (PLN), w tym zewnętrzne `enea:..._koszt_...` (z `name` jako etykietą, np. „Koszt energii pobrana – Dzień").

### Timing wstrzykiwania kosztów

Statystyki zewnętrzne **nie wymagają** istnienia encji w rejestrze, więc wstrzykiwanie kosztów może iść tą samą ścieżką co energia (`_async_inject_days`) podczas pierwszego odświeżenia lub backfillu w tle — niezależnie od kolejności setupu. Nie ma już mechanizmu `_pending_cost_days`/`set_pending`.

`_async_inject_missing_costs` (wołane z `_async_fetch_and_inject_stats`) uzupełnia brakujące koszty, gdy statystyki energii są już aktualne. `async_get_cost_latest_date` odpytuje recorder po stat IDs wyliczonych ze stref **wszystkich** okresów taryfy — nie wymaga rejestrów encji ani okresu obowiązującego dziś (tabela taryf kończy się na sztywnej dacie, a po niej nie ma żadnego bieżącego okresu).

Zakres dni do pobrania wyznacza `async_cost_days_missing` w `costs.py` (coordinator tylko pobiera i wstrzykuje). Górną granicą jest **ostatni dzień pokryty przez tabelę taryf**, a nie „wczoraj": dni po końcu tabeli nie dostaną ceny, więc ich pobranie nie przesunęłoby najnowszej statystyki kosztów i ten sam — rosnący z dnia na dzień — zakres wracałby przy każdym odświeżeniu.

### Obsługa świąt (G12w)

Koszty są obliczane przez `period.get_zone_at_hour(hour, day=day)` — `enea_prices` wykrywa polskie święta automatycznie na podstawie przekazanej daty (biblioteka `holidays`). Dla taryf bez harmonogramu tygodniowego (G11, G12) parametr `day` nie ma wpływu na wynik.

### Deduplikacja przy backfill

`_inject_cost_series` w `costs.py` i `_inject_energy_series` w `statistics.py` budują tylko `StatisticMetaData`, a sam zapis serii skumulowanej wykonuje wspólne `write_cumulative_series` w `statistics.py`. Obie serie różnią się wyłącznie tym, co godzina raportuje jako własny `state`: energia — odczyt kWh za tę godzinę, koszt — sumę narastającą (parametr `state_is_running_total`).

`write_cumulative_series` zaczepia sumę o statystykę tuż przed `series[0]`, a nie o najnowszy wpis serii — inaczej ponowne wstrzyknięcie pokrytego zakresu doliczyłoby go do samego siebie. Zapytanie o ten punkt zaczepienia jest celowo nieograniczone od dołu: seria strefowa zawiera tylko godziny swojej strefy, a dzień nieopublikowany przez portal powiększa lukę jeszcze bardziej, więc każde stałe okno prędzej czy później trafiłoby w pustkę i po cichu wyzerowało sumę.

`write_cumulative_series` zaczepia sumę o statystykę tuż przed `series[0]`, a nie o najnowszy wpis serii — inaczej ponowne wstrzyknięcie pokrytego zakresu doliczyłoby go do samego siebie. Zapytanie o ten punkt zaczepienia jest celowo nieograniczone od dołu: seria strefowa zawiera tylko godziny swojej strefy, a dzień nieopublikowany przez portal powiększa lukę jeszcze bardziej, więc każde stałe okno prędzej czy później trafiłoby w pustkę i po cichu wyzerowało sumę.

`async_add_external_statistics` aktualizuje wpis o tym samym czasie rozpoczęcia, ale **wyłącznie dla przekazanych wpisów**. Gdy ponowny import zwróci inne wartości niż zapisane (dzień wstrzyknięty zerami, który portal opublikował później; korekta danych po stronie Enei; zakres starszy niż cała zapisana historia), wpisy po zakresie nadal niosłyby sumę naliczoną od starych wartości — seria spadałaby na styku, a HA czyta spadek sumy jako wymianę licznika. Dlatego `_shift_later_totals` dodaje różnicę między nową a poprzednią sumą końcową zakresu do wszystkich późniejszych wpisów. Godzinowe wartości własne pozostają nietknięte, więc cały ogon przesuwa się o tę samą wartość. Przy niezmienionych danych różnica wynosi 0 i nic nie jest doczytywane ani zapisywane.

### Szacowanie rachunku (billing.py)

`find_prices_config(hass, tariff_name)` zwraca `PricesConfig` (duck-typed z `enea_prices.runtime_data`: `tariff`, `phases`, `annual_kwh`, `billing_months`; `akcyza` odczytana przez `sys.modules["custom_components.enea_prices.const"].AKCYZA`, fallback `0.0`).

`async_estimate_bill(hass, meter_code, cfg, start, end)` → `BillEstimate` (metoda jak faktura Enea, ale kWh precyzyjne):
- kWh per strefa = precyzyjna różnica sum skumulowanych statystyk zewnętrznych `enea:..._energia_pobrana_{strefa}` na granicach `(start, end]` (bez zaokrąglania do całości).
- **Sprzedaż energii** netto per strefa = `round(kWh × (zone.energy + cfg.akcyza), 2)`.
- **Usługa dystrybucji** — cztery składniki zaokrąglane osobno per strefa: `round(kWh × zone.variable_network, 2)`, `round(kWh × zone.quality, 2)`, `round(kWh × zone.oze, 2)`, `round(kWh × zone.cogeneration, 2)`.
- Opłaty stałe netto = `round(network_fixed × months, 2)` + `round(capacity × months, 2)` + `round(subscription × months, 2)`.
- `total_netto = round(energy_netto + distribution_netto, 2)`, `total = round(total_netto × 1.23, 2)` — VAT doliczany raz na końcu.
- Metoda `tariff.get_period_for_date(end)` wybiera właściwy cennik.

`coordinator.async_recompute_bills()` oblicza dwa okresy:
- **poprzedni**: `(bill_prev_reading, bill_last_reading]`
- **bieżący**: `(bill_last_reading, yesterday]`

Wywoływana: po zmianie daty przez użytkownika (z `EneaBillDateEntity.async_set_value`) i po każdym odświeżeniu danych, gdy co najmniej jedna data jest ustawiona.

### Automatyczne przeładowanie

`enea_prices.__init__` po swoim setup wywołuje `_async_reload_matching_enea_entries`, która przeładowuje wpisy Enea z pasującą taryfą. Dzięki temu użytkownik nie musi ręcznie przeładowywać integracji po zainstalowaniu `enea_prices`.

## Dokumentacja API Enea

Baza URL: `https://portalodbiorcy.operator.enea.pl/portalOdbiorcy/api`

### Logowanie

```
POST /auth/login
Content-Type: application/json

{"username": "email@example.com", "password": "haslo"}
```

**Odpowiedź sukcesu (200):**
- Header `Set-Cookie: PER_JSESSIONID=<wartość>` — to ciasteczko musi być wysyłane we wszystkich kolejnych żądaniach
- aiohttp zarządza nim automatycznie przez CookieJar sesji

**Błąd autoryzacji (401):** nieprawidłowe dane logowania

### Lista liczników (PPE)

```
GET /user/ppes
Cookie: PER_JSESSIONID=<wartość>
```

Zwraca listę punktów poboru energii przypisanych do konta. Pole `address` jest zawsze `null` — adres dostępny tylko przez endpoint dashboard. Odpowiedź cachowana przez 5 minut (patrz `METERS_CACHE_TTL` w `const.py`).

Przykład odpowiedzi: patrz `data/ppes.json`.

### Dashboard PPE (główne źródło danych)

```
GET /consumptionDashboard/ppe/{id}
Cookie: PER_JSESSIONID=<wartość>
```

Gdzie `{id}` to pole `id` z odpowiedzi `/user/ppes` (np. `73689`). Główny endpoint odpytywany przez coordinator zgodnie z konfigurowalnym interwałem (domyślnie 3h 30min, zmiana przez options flow).

Kluczowe pola odpowiedzi:
- `address` — pełny adres PPE `{street, houseNum, apartmentNum, postCode, city, district, parcelNum}`
- `agreementPower` — moc umowna (kW)
- `tariffGroupName` — nazwa grupy taryfowej (np. `"G12"`)
- `detailedStatus` — status licznika
- `meters[]` — historia fizycznych liczników `{serialNumber, typeName, assemblyDate, disassemblyDate}`
- `currentValues[]` — aktualne odczyty energii:
  - `measurementId=1` → energia czynna pobrana
  - `measurementId=2` → energia czynna oddana
  - `valueNoZones.value` — suma stref (kWh)
  - `valueZone1.value`, `valueZone2.value`, … — wartości per strefa
  - `ppeZones[]` — nazwy stref np. `["Dzień 1.8.1", "Noc 1.8.2"]`
  - `readingDate` — timestamp ostatniego odczytu (ms)
  - `unit.symbol="Wh"`, `unit.scaler=3` → wartości są w kWh

Przykład odpowiedzi: patrz `data/ppe73689.json`.

### Endpoint statystyk historycznych — single day (legacy)

```
GET /consumption/{ppeId}/1/{date}/{measurementType}/{resolution}
Cookie: PER_JSESSIONID=<wartość>
```

| Parametr | Opis |
|----------|------|
| `ppeId` | ID licznika (pole `id` z `/user/ppes`) |
| `date` | Data w formacie `YYYY-MM-DD` |
| `measurementType` | 1=energia pobrana, 5=energia oddana, 4=moc pobrana, 9=moc oddana |
| `resolution` | 1=15 min (96 wpisów), 2=60 min (24 wpisy) |

### Endpoint statystyk historycznych — zakres dat

```
GET /consumption/{ppeId}/{startDate}/{endDate}/{measurementType}/{resolution}
Cookie: PER_JSESSIONID=<wartość>
```

| Parametr | Opis |
|----------|------|
| `ppeId` | ID licznika (pole `id` z `/user/ppes`) |
| `startDate` | Data początkowa w formacie `YYYY-MM-DD` (włącznie) |
| `endDate` | Data końcowa w formacie `YYYY-MM-DD` (włącznie) |
| `measurementType` | 1=energia pobrana, 5=energia oddana, 4=moc pobrana, 9=moc oddana |
| `resolution` | 2=60 min (zalecane; 24 wpisy × liczba dni) |

Kluczowe pola odpowiedzi:
- `values[]` — płaska lista slotów godzinowych, `timeId` powtarzający się per dzień; dla dużych zakresów bywa pusta **albo zawiera krótki częściowy wycinek** (wtedy pełne dane są w `valuesToTable[]`)
- `valuesToTable[]` — pełne dane godzinowe dla dużych zakresów (identyczna struktura jak `values[]`); kod bierze **dłuższe** z pól `values`/`valuesToTable`
- `values[].integrationEnd` — znacznik końca godziny slotu (ms epoki); źródło dokładnego czasu slotu (`slot_start_dt`), odporne na DST — używane zamiast wyliczania z `timeId`
- `items[].tarifZoneId` — ID strefy
- `items[].value` — wartość (kWh lub kW), może być `null` gdy brak odczytu
- `zones[]` — definicje stref wspólne dla całego zakresu: `{id, name}` (np. `{id: 1, name: "Dzień"}`)
- Zwykle 24 sloty na dobę, ale **23 lub 25 w dniu zmiany czasu** (DST); podział na doby idzie po `integrationEnd`, nie po stałej liczbie slotów

Wydajność (zmierzona): 6 miesięcy ~2s, rok ~5.5s, 3 lata ~26s (5.5 MB).

Dane za poprzedni dzień są dostępne zwykle po godzinie 11:00 następnego dnia.

## Sensory

### Diagnostyczne (EntityCategory.DIAGNOSTIC)
| Klucz | Źródło danych |
|-------|--------------|
| `tariff` | `tariffGroupName` |
| `capacity` | `agreementPower` |
| `status` | `detailedStatus` |
| `address` | `address` (przez `format_address()`) |
| `reading_date` | `currentValues[0].readingDate` |
| `meter_model` | `meters[].typeName` aktywnego licznika |

### Energia (widoczne w dashboardach)
Tworzone dynamicznie w `async_setup_entry` na podstawie `currentValues[]`. Sensory dla wyłączonego kierunku (`fetch_consumption=False` lub `fetch_generation=False` w options) nie są tworzone.
- `consumption_total` / `generation_total` — sumy stref (statyczne)
- `consumption_zone{i}` / `generation_zone{i}` — per strefa (dynamiczne, nazwy z `ppeZones[]`)

### Szacowanie rachunku (EneaBillSensor + EneaBillDateEntity, opcjonalne)
Tworzone gdy `find_tariff_group` zwraca pasującą taryfę z `enea_prices`.
- Dwie encje `DateEntity` (Platform.DATE, `RestoreEntity`) — „Data poprzedniego odczytu" i „Data ostatniego odczytu". Po zmianie daty wołają `coordinator.async_recompute_bills()`.
- Dwa sensory `EneaBillSensor` (Platform.SENSOR) — „Szacowany rachunek – poprzedni okres" i „Szacowany rachunek – bieżący okres". `device_class=MONETARY`, PLN, **bez `state_class`**. `native_value` z `coordinator.bill_estimates[key].total`.
- `coordinator.bill_estimates` (dict `BILL_KEY_PREVIOUS/CURRENT → BillEstimate | None`) przeliczany przez `async_recompute_bills()` — wywołanie: po zmianie daty, po każdym odświeżeniu gdy daty są ustawione.
- `BillEstimate` z `billing.py`: `kwh_by_zone` (float), `energy_by_zone_netto`, `variable_network_by_zone_netto`, `quality_by_zone_netto`, `oze_by_zone_netto`, `cogeneration_by_zone_netto`, `energy_netto`, `distribution_netto`, `fixed_network_netto`, `fixed_capacity_netto`, `fixed_subscription_netto`, `total_netto`, `total` (jedyne brutto = stan sensora), `months`, `start`, `end`. Atrybuty sensora (w kolejności faktury): `start`, `end`, `months` → `kwh_{strefa}`, `energy_{strefa}_netto` per strefa → `energy_netto` → `fixed_network_netto`, `fixed_capacity_netto` → `variable_network_{strefa}_netto`, `quality_{strefa}_netto`, `oze_{strefa}_netto`, `cogeneration_{strefa}_netto` per strefa → `fixed_subscription_netto` → `distribution_netto` → `total_netto`.

## Obsługa sesji

- aiohttp `CookieJar` zarządza ciasteczkiem `PER_JSESSIONID` automatycznie
- Przy odpowiedzi 401/403 `_request()` w connectorze ponawia logowanie i powtarza żądanie (z `asyncio.Lock` — zapobiega wielokrotnym re-auth przy równoległych żądaniach)
- Przy restarcie HA sesja jest tracona — `get_meters()` automatycznie wywołuje `authenticate()`
- Przy permanentnym błędzie auth coordinator rzuca `ConfigEntryAuthFailed` → przepływ reauth w UI
- Zmiana danych logowania przez użytkownika: dostępna przez **reconfigure flow** (menu ⋮ integracji) — inaczej niż reauth, który jest wyzwalany automatycznie przez 401
- Wiele liczników na jednym koncie: współdzielony `EneaApiClient` w `hass.data[DOMAIN][username]`

## Statystyki a sensory — podział odpowiedzialności

| Encja | Źródło danych | Gdzie używana |
|-------|---------------|---------------|
| `sensor.enea_*_energia_pobrana` | `/ppe/{id}` dashboard | Energy Dashboard (encje) |
| `enea:..._energia_pobrana` | `/consumption/...` | Energy Dashboard (statystyki zewnętrzne) |
| `enea:..._koszt_energii_pobrana_dzien` | obliczone z energii + cennik enea_prices | Energy Dashboard (encja śledząca koszty) |
| `sensor.enea_*_szacowany_rachunek_*` | `coordinator.bill_estimates` | podgląd kwoty rachunku w Lovelace |

W Energy Dashboard **nie** dodajemy sensorów energii (`sensor.enea_..._energia_...`) do wykresu historii — zamiast tego dodajemy statystyki zewnętrzne (`enea:...`). Jako „encję śledzącą całkowite koszty" w Energy Dashboard wskazujemy **statystykę zewnętrzną** `enea:..._koszt_...` (widoczną na liście po jednostce PLN), nie encję.

## Opcje integracji (options flow)

Dostępne przez **Ustawienia → Urządzenia i usługi → Enea → Konfiguruj**:

| Opcja | Domyślnie | Opis |
|-------|-----------|------|
| `update_interval` | 3h 30min | Interwał odpytywania dashboardu; minimum 30 min |
| `fetch_consumption` | `True` | Pobieranie statystyk i sensorów energii pobranej |
| `fetch_generation` | `True` | Pobieranie statystyk i sensorów energii oddanej |
| `fetch_power_consumption` | `False` | Pobieranie statystyk mocy pobranej (kW) |
| `fetch_power_generation` | `False` | Pobieranie statystyk mocy oddanej (kW) |

Zmiana opcji powoduje natychmiastowy reload integracji (update listener w `__init__.py`).

## Akcje (services)

### `enea.refresh`
Wymusza natychmiastowe pobranie danych dashboardu i uzupełnienie brakujących statystyk (od ostatniej zapisanej daty do wczoraj).

### `enea.backfill`
Importuje statystyki historyczne dla dowolnego zakresu dat. Nie aktualizuje stanów sensorów.

| Parametr | Wymagany | Opis |
|----------|----------|------|
| `device_id` | nie | Konkretny licznik; puste = wszystkie |
| `start_date` | nie | Pierwsza data (YYYY-MM-DD); ma pierwszeństwo nad `days_back` |
| `end_date` | nie | Ostatnia data; domyślnie wczoraj gdy podano `start_date` |
| `days_back` | nie | Liczba dni wstecz od wczoraj (1–365) |
| *(brak parametrów)* | — | Domyślnie: ostatnie 30 dni |

## Jak dodać nowe endpointy API

1. **`const.py`** — dodaj URL
2. **`connector.py`** — dodaj metodę wywołującą `await self._request(url, "label")`
3. **`coordinator.py`** — rozszerz `_async_update_data()` o nowe wywołanie; gdy dane urosną, zamień typ generyczny `dict` na własny dataclass
4. **`sensor.py`** — dodaj nowe opisy sensorów w `SENSOR_DESCRIPTIONS` lub nową klasę sensorów

## Zarządzanie wersjami

Przy zmianie struktury danych w `ConfigEntry` (klucze w `entry.data`):
1. Zwiększ `VERSION` w `EneaConfigFlow`
2. Dodaj `async_migrate_entry()` w `__init__.py`

## Wydawanie nowej wersji

1. Podbij `version` w `custom_components/enea/manifest.json` oraz `version` w `pyproject.toml`
2. Zacommituj: `git commit -m "Release vX.Y.Z"`
3. Wypchnij: `git push`
4. Utwórz release przez GitHub CLI:
   ```
   gh release create vX.Y.Z --title "vX.Y.Z" --generate-notes
   ```
   Flaga `--generate-notes` automatycznie generuje changelog z commitów od poprzedniego tagu.

## CI/CD

- `hacs.yaml` — walidacja przez `hacs/action@main` (push, PR, codziennie)
- `hassfest.yaml` — walidacja `manifest.json` przez `home-assistant/actions/hassfest@master`

## Testowanie lokalne

1. Skopiuj `custom_components/enea/` do `<ha_config>/custom_components/enea/`
2. Uruchom ponownie HA
3. Dodaj integrację przez UI: **Ustawienia → Urządzenia i usługi → Dodaj integrację → Enea Licznik**
4. Sprawdź logi: `Ustawienia → System → Logi`, filtruj po `enea`
