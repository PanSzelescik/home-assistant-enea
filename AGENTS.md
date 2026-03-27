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
├── config_flow.py   — EneaConfigFlow: krok "user", "select_meter", reconfigure; EneaOptionsFlow; _validate_options, _async_validate_and_update_credentials
├── sensor.py        — EneaSensor, EneaEnergySensor, EneaCostSensor, SENSOR_DESCRIPTIONS, _address_attrs, _meter_model_attrs, _get_reading_date
├── statistics.py    — async_insert_historical_statistics, _collect_series, _inject_energy_series, _inject_power_series
├── costs.py         — async_insert_cost_statistics, get_cost_stats, _inject_cost_series, _get_cost_entries, find_tariff_group
├── diagnostics.py   — async_get_config_entry_diagnostics (z wymuszonym odświeżeniem)
├── services.yaml    — definicja akcji "refresh" i "backfill"
├── const.py         — DOMAIN, URLs, klucze konfiguracji, stałe statystyk, stałe kosztów (ENEA_PRICES_DOMAIN, UNIT_COST, COST_ZONE_DISPLAY)
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
- Odpowiedź range endpoint to płaska lista slotów 1-24 (zawsze dokładnie 24 na dzień, niezależnie od DST). `_split_range_response` dzieli ją na per-day dicty z kluczami `{"values": [...], "zones": [...]}` identycznymi ze strukturą single-day endpoint — dzięki temu `has_data`, `_collect_series` i koszty nie wymagają zmian. Dla bardzo dużych zakresów API zwraca dane w `valuesToTable` zamiast `values` — kod sprawdza oba pola.
- Manualny backfill dowolnego zakresu dat: akcja `enea.backfill` (patrz Akcje).
- `has_data` zwraca `False` gdy odpowiedź API zawiera wyłącznie wartości `null` (`if item.get("value") is not None`). Zera są traktowane jako dane (zerowe zużycie) — dni z zerowym zużyciem są importowane. Filtrowanie danych starego licznika odbywa się przez `_strip_pre_assembly_slots` na poziomie godzin, nie przez `has_data`.

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

### Statystyki kosztów vs. statystyki energii

Statystyki kosztów używają **`async_import_statistics`** z `source="recorder"` (nie `source=DOMAIN` jak energia). Wymagania:
- Encja musi istnieć w rejestrze encji HA przed wstrzyknięciem statystyk
- `statistic_id` to `entity_id` encji (format: `sensor.enea_{meter_code}_koszt_{direction}_{zone}`), pobierany przez `entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id)`
- Dlatego `async_setup_costs()` jest wywoływane z `__init__.py` **po** `async_forward_entry_setups`, gdy encje są już zarejestrowane

### EneaCostSensor — encja "hydrauliczna"

`EneaCostSensor` w `sensor.py` ma:
- `state_class=TOTAL`, `device_class=MONETARY`, `native_unit_of_measurement="PLN"`
- Stan encji = skumulowana suma kosztów od początku danych (nie koszt za bieżący okres)
- Encja istnieje wyłącznie jako "hak" dla funkcji **"encja śledząca całkowite koszty"** w Energy Dashboard — HA oblicza koszt dla dowolnego okresu jako różnicę między wartościami sum
- `native_value` czytane z `coordinator.cost_sums[unique_id]`
- Tworzone tylko gdy `find_tariff_group` zwraca pasującą taryfę

### Timing wstrzykiwania kosztów

```
async_setup_entry()
  → async_forward_entry_setups()   # rejestruje EneaCostSensor w rejestrze encji
  → async_setup_costs()            # wstrzykuje statystyki kosztów (encje już istnieją)
```

`async_setup_costs()` używa `_pending_cost_days` — listy dni już pobranych przez standardowy backfill energii — żeby nie wykonywać dodatkowych żądań do API. Jeśli energia jest aktualna, `_async_inject_missing_costs(yesterday)` odpowiada za niezależne uzupełnienie brakujących kosztów.

### Obsługa świąt (G12w)

Koszty są obliczane przez `period.get_zone_at_hour(hour, day=day)` — `enea_prices` wykrywa polskie święta automatycznie na podstawie przekazanej daty (biblioteka `holidays`). Dla taryf bez harmonogramu tygodniowego (G11, G12) parametr `day` nie ma wpływu na wynik.

### Deduplikacja przy backfill

`_inject_cost_series` w `costs.py` filtruje wpisy, których datetime jest `<=` ostatniemu wstrzykniętemu datetime, zapobiegając podwójnemu liczeniu przy wielokrotnym backfill tego samego zakresu dat.

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

Zwraca listę punktów poboru energii przypisanych do konta. Pole `address` jest zawsze `null` — adres dostępny tylko przez endpoint dashboard. Odpowiedź cachowana przez 5 minut (patrz `METERS_CACHE_TTL` w `connector.py`).

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
- `values[]` — płaska lista: 24 sloty × liczba dni, `timeId` 1-24 powtarzający się per dzień; dla zakresów >~30 dni może być pusta (dane w `valuesToTable[]` o tej samej strukturze)
- `valuesToTable[]` — fallback dla dużych zakresów (identyczna struktura jak `values[]`)
- `items[].tarifZoneId` — ID strefy
- `items[].value` — wartość (kWh lub kW), może być `null` gdy brak odczytu
- `zones[]` — definicje stref wspólne dla całego zakresu: `{id, name}` (np. `{id: 1, name: "Dzień"}`)
- Zawsze dokładnie 24 sloty na dzień, niezależnie od DST

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

### Koszty (EneaCostSensor, opcjonalne)
Tworzone dynamicznie per strefa i kierunek, gdy `find_tariff_group` zwraca pasującą taryfę z `enea_prices`.
- `state_class=TOTAL`, `device_class=MONETARY`, jednostka PLN
- Stan = skumulowana suma kosztów od początku danych; służy wyłącznie jako "hak" dla Energy Dashboard (`encja śledząca całkowite koszty`)
- `unique_id` format: `enea-{meter_code}-koszt_{direction}_{zone}` (używany do odszukania encji w entity registry; `statistic_id` w `async_import_statistics` to **`entity_id`** tej encji)

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
| `sensor.enea_*_koszt_energii_pobrana_dzien` | obliczone z energii + cennik enea_prices | Energy Dashboard (encja śledząca koszty) |

W Energy Dashboard **nie** dodajemy sensorów energii (`sensor.enea_..._energia_...`) do wykresu historii — zamiast tego dodajemy statystyki zewnętrzne (`enea:...`). Sensory energii służą do bieżącego wyświetlania wartości na dashboardach Lovelace. Sensory kosztów (`sensor.enea_..._koszt_...`) są wskazywane w Energy Dashboard jako "encja śledząca całkowite koszty" dla danego źródła energii.

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
