# Enea Licznik — Home Assistant Integration

Nieoficjalna integracja Home Assistant dla liczników zdalnego odczytu (AMI) Enea Operator.
Pozwala na monitorowanie danych licznika bezpośrednio z Portalu Odbiorcy Enea.

## Wymagania

- Home Assistant 2024.1.0 lub nowszy
- Zweryfikowane konto na [portalodbiorcy.operator.enea.pl](https://portalodbiorcy.operator.enea.pl/)
- Licznik zdalnego odczytu (AMI) od Enea Operator

## Instalacja

### Przez HACS (zalecane)

1. Otwórz HACS w Home Assistant
2. Przejdź do **Integracje**
3. Kliknij menu (⋮) → **Własne repozytoria**
4. Dodaj URL: `https://github.com/PanSzelescik/home-assistant-enea` z kategorią **Integration**
5. Znajdź **Enea Licznik** na liście i kliknij **Pobierz**
6. Uruchom ponownie Home Assistant

### Ręczna instalacja

1. Pobierz zawartość folderu `custom_components/enea/` z tego repozytorium
2. Skopiuj go do `<config_dir>/custom_components/enea/`
3. Uruchom ponownie Home Assistant

## Konfiguracja

[![Otwórz Home Assistant i zacznij konfigurację integracji.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=enea)

1. Kliknij przycisk powyżej lub przejdź do **Ustawienia → Urządzenia i usługi → Dodaj integrację** i wyszukaj **Enea Licznik**
2. Wprowadź adres e-mail i hasło z Portalu Odbiorcy Enea
3. Wybierz licznik (PPE) z listy — każdy pokazuje kod PPE, taryfę i adres
4. Wybierz ile dni historii pobrać (7 / 30 / 60 / 90 dni lub **Maksymalnie — ile się da**)
5. Gotowe!

## Statystyki historyczne godzinowe

Po dodaniu integracji automatycznie pobierane są historyczne dane energii i mocy z granularnością **godzinową**. Dane są wstrzykiwane jako **statystyki zewnętrzne** Home Assistant, co pozwala wyświetlić pełną historię w panelu Energia z prawidłowymi timestampami.

> **Kiedy dostępne:** Dane za poprzedni dzień pojawiają się zwykle po godzinie 11:00. Integracja automatycznie sprawdza dostępność przy każdym odświeżeniu i pobiera brakujące dni.

### Dostępne statystyki

| Statystyk | Typ | Opis |
|-----------|-----|------|
| `enea:{kod}_energia_pobrana` | kWh | Energia pobrana — suma wszystkich stref |
| `enea:{kod}_energia_pobrana_dzien` | kWh | Energia pobrana — strefa dzienna (G12/G13) |
| `enea:{kod}_energia_pobrana_noc` | kWh | Energia pobrana — strefa nocna (G12/G13) |
| `enea:{kod}_energia_oddana` | kWh | Energia oddana (fotowoltaika) — suma |
| `enea:{kod}_moc_pobrana` | kW | Chwilowa moc pobrana — suma |
| `enea:{kod}_moc_pobrana_dzien` | kW | Chwilowa moc pobrana — strefa dzienna |
| *(analogicznie dla oddanej)* | | |

Liczba statystyk strefowych zależy od taryfy (G11 = brak stref, G12 = 2 strefy, G13 = 3 strefy). Nazwy stref są pobierane dynamicznie z API.

### Konfiguracja Energy Dashboard

Aby zobaczyć dane historyczne w panelu Energia:

1. **Ustawienia → Energia → Sieć elektryczna**
2. Kliknij **Dodaj zużycie** i wyszukaj `enea:{kod}_energia_pobrana`
3. Jeśli masz fotowoltaikę, dodaj **Energia zwrócona**: `enea:{kod}_energia_oddana`

> **Uwaga:** Nie dodawaj sensorów `sensor.enea_...` — używaj statystyk zewnętrznych `enea:...`, które mają prawidłowe znaczniki czasu.

## Encje

### Sensory energii

Widoczne w panelu **Energia** Home Assistant. Liczba sensorów strefowych zależy od taryfy (G11 = brak stref, G12 = 2 strefy, G13 = 3 strefy).

> **Uwaga:** Integracja była testowana wyłącznie na taryfie **G12** (strefy Dzień i Noc). Działanie na innych taryfach jest możliwe, ale nie zostało zweryfikowane.

| Encja                   | Opis | Przykład |
|-------------------------|------|---------|
| Energia pobrana         | Całkowite zużycie energii (suma stref) | 0,6580 kWh |
| Energia pobrana – Dzień | Zużycie w strefie dziennej | 0,6580 kWh |
| Energia pobrana – Noc   | Zużycie w strefie nocnej | 0,0000 kWh |
| Energia oddana          | Całkowita energia oddana do sieci (suma stref) | 0,1080 kWh |
| Energia oddana – Dzień  | Energia oddana w strefie dziennej | 0,1080 kWh |
| Energia oddana – Noc    | Energia oddana w strefie nocnej | 0,0000 kWh |

### Sensory diagnostyczne

| Encja | Opis | Przykład                    |
|-------|------|-----------------------------|
| Grupa taryfowa | Nazwa grupy taryfowej | G12                         |
| Moc umowna | Moc umowna | 14 kW                       |
| Status | Status licznika | Aktywny_Pod napięciem...    |
| Adres | Adres punktu poboru energii | Pastelowa 8, 60-198, Poznań |
| Ostatni odczyt | Data i godzina ostatniego odczytu z licznika | 2 marca 2026, 14:32         |
| Model licznika | Model aktualnie zamontowanego licznika | OTUS3                       |

## Częstotliwość odświeżania

Integracja odpytuje Portal Odbiorcy Enea domyślnie **co 8 godzin 30 minut**. Interwał można zmienić przez options flow:

**Ustawienia → Urządzenia i usługi → Enea → Konfiguruj** → wpisz żądany czas (minimum 30 minut).

Timer jest resetowany po restarcie Home Assistant, przeładowaniu integracji lub zmianie opcji.

Jeśli masz kilka liczników na tym samym koncie, integracja loguje się tylko raz i współdzieli sesję między licznikami.

### Ręczne odświeżanie

Możesz wymusić natychmiastowe pobranie danych przez akcję **`enea.refresh`**:

**Narzędzia deweloperskie → Akcje → `enea.refresh`** → wybierz urządzenie → Wywołaj

Jeśli nie wybierzesz urządzenia, zostaną odświeżone wszystkie skonfigurowane liczniki.

### Diagnostyki

W przypadku problemów pobierz raport diagnostyczny (hasło jest automatycznie ukrywane):

**Ustawienia → Urządzenia i usługi → Enea → menu (⋮) → Pobierz diagnostyki**

## Znane ograniczenia

- API Enea jest nieoficjalne i może ulec zmianie bez ostrzeżenia
- Odczyty energii są wartościami skumulowanymi — przy wymianie licznika nowe urządzenie zaczyna od 0, co może chwilowo zaburzyć statystyki w panelu Energia

## Rozwiązywanie problemów

- Upewnij się, że logujesz się tymi samymi danymi co na [portalodbiorcy.operator.enea.pl](https://portalodbiorcy.operator.enea.pl/)
- Sprawdź logi Home Assistant (`Ustawienia → System → Logi`) w poszukiwaniu błędów z domeny `enea`

## Podziękowania

Inspirowane integracją [Tauron AMIplus](https://github.com/PiotrMachowski/Home-Assistant-custom-components-Tauron-AMIplus) autorstwa [@PiotrMachowski](https://github.com/PiotrMachowski).

Napisane przy pomocy [Claude Code](https://claude.ai/claude-code).

## Prawa autorskie

Logo Enea pochodzi z oficjalnej teczki prasowej Enea S.A.: [media.enea.pl/teczka-prasowa/logotypy](https://media.enea.pl/teczka-prasowa/logotypy). Wszelkie prawa do logotypu należą do Enea S.A.
