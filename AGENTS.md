# CLAUDE.md — Enea Licznik Integration

## Konwencje nazewnictwa

- Zawsze używaj **"Portal Odbiorcy Enea"** (z wielkiej litery) — nigdy "portal Enea" ani "portal". Dotyczy tekstów UI, tłumaczeń, komentarzy i dokumentacji.
- W angielskich tekstach: **"Portal Odbiorcy Enea"** (nazwa własna, bez tłumaczenia), np. "from the Portal Odbiorcy Enea".

## Przegląd projektu

Niniejszy projekt to custom component dla Home Assistant integrujący liczniki zdalnego odczytu (AMI) Enea Operator przez nieoficjalne REST API Portalu Odbiorcy Enea.

## Struktura projektu

```
custom_components/enea/
├── __init__.py      — setup/unload entry, EneaRuntimeData, EneaConfigEntry, serwisy refresh/backfill
├── connector.py     — klient HTTP (EneaApiClient, _request helper), wyjątki, format_address()
├── coordinator.py   — EneaUpdateCoordinator: dane sensorów + pobieranie/wstrzykiwanie statystyk, async_backfill
├── config_flow.py   — EneaConfigFlow: krok "user", "select_meter", reconfigure; EneaOptionsFlow
├── sensor.py        — EneaSensor, EneaEnergySensor, SENSOR_DESCRIPTIONS, _get_reading_date
├── statistics.py    — async_insert_historical_statistics, _collect_series, _inject_energy/power_series
├── diagnostics.py   — async_get_config_entry_diagnostics (z wymuszonym odświeżeniem)
├── services.yaml    — definicja akcji "refresh" i "backfill"
├── const.py         — DOMAIN, URLs, klucze konfiguracji, stałe statystyk
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

- Coordinator co każde odświeżenie sprawdza aktualność statystyk przez `get_last_statistics` — sprawdza wszystkie aktywne serie (energy_consumed/returned, power_consumed/returned) i bierze najnowszą datę.
- Jeśli nie ma danych do wczoraj — pobiera brakujące dni i wstrzykuje.
- Backfill przy pierwszym uruchomieniu: konfigurowalny przez użytkownika (7/30/60/90 dni lub "ile się da").
- "Ile się da" = cofaj się dzień po dniu do tyłu od wczoraj bez limitu, zatrzymaj po 7 kolejnych dniach bez danych z API.
- Pobieranie per dzień jest równoległe (`asyncio.gather`) — 2 lub 4 żądania jednocześnie zależnie od opcji fetch_consumption/fetch_generation.
- Manualny backfill dowolnego zakresu dat: akcja `enea.backfill` (patrz Akcje).

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

Gdzie `{id}` to pole `id` z odpowiedzi `/user/ppes` (np. `73689`). Główny endpoint odpytywany przez coordinator zgodnie z konfigurowalnym interwałem (domyślnie 8h 30min, zmiana przez options flow).

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

### Endpoint statystyk historycznych

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

Kluczowe pola odpowiedzi:
- `values[]` — 24 sloty z `timeId` (1–24) i `items[]` per strefa taryfowa (resolution=2, godzinowe)
- `items[].tarifZoneId` — ID strefy
- `items[].value` — wartość (kWh lub kW), może być `null` gdy brak odczytu
- `zones[]` — definicje stref: `{id, name}` (np. `{id: 1, name: "Dzień"}`)

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

W Energy Dashboard **nie** dodajemy sensorów (`sensor.enea_...`) do wykresu historii — zamiast tego dodajemy statystyki zewnętrzne (`enea:...`). Sensory służą do bieżącego wyświetlania wartości na dashboardach Lovelace.

## Opcje integracji (options flow)

Dostępne przez **Ustawienia → Urządzenia i usługi → Enea → Konfiguruj**:

| Opcja | Domyślnie | Opis |
|-------|-----------|------|
| `update_interval` | 8h 30min | Interwał odpytywania dashboardu; minimum 30 min |
| `fetch_consumption` | `True` | Pobieranie statystyk i sensorów energii pobranej |
| `fetch_generation` | `True` | Pobieranie statystyk i sensorów energii oddanej |

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

## CI/CD

- `hacs.yaml` — walidacja przez `hacs/action@main` (push, PR, codziennie)
- `hassfest.yaml` — walidacja `manifest.json` przez `home-assistant/actions/hassfest@master`

## Testowanie lokalne

1. Skopiuj `custom_components/enea/` do `<ha_config>/custom_components/enea/`
2. Uruchom ponownie HA
3. Dodaj integrację przez UI: **Ustawienia → Urządzenia i usługi → Dodaj integrację → Enea Licznik**
4. Sprawdź logi: `Ustawienia → System → Logi`, filtruj po `enea`
