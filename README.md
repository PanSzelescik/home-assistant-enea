# Enea Licznik — Home Assistant Integration

Nieoficjalna integracja Home Assistant dla liczników zdalnego odczytu (AMI) Enea Operator.
Pozwala na monitorowanie danych licznika bezpośrednio z Portalu Odbiorcy Enea.

## Wymagania

- Home Assistant 2026.3.3 lub nowszy
- Zweryfikowane konto na [portalodbiorcy.operator.enea.pl](https://portalodbiorcy.operator.enea.pl/)
- Licznik zdalnego odczytu (AMI) od Enea Operator

## Instalacja

### Przez HACS (zalecane)

[![Otwórz Home Assistant i dodaj repozytorium w HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=PanSzelescik&repository=home-assistant-enea)

1. Kliknij przycisk powyżej lub otwórz HACS → **Integracje** → menu (⋮) → **Własne repozytoria**
2. Dodaj URL: `https://github.com/PanSzelescik/home-assistant-enea` z kategorią **Integration**
3. Znajdź **Enea Licznik** na liście i kliknij **Pobierz**
4. Uruchom ponownie Home Assistant

### Ręczna instalacja

1. Pobierz zawartość folderu `custom_components/enea/` z tego repozytorium
2. Skopiuj go do `<config_dir>/custom_components/enea/`
3. Uruchom ponownie Home Assistant

## Konfiguracja

[![Otwórz Home Assistant i zacznij konfigurację integracji.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=enea)

1. Kliknij przycisk powyżej lub przejdź do **Ustawienia → Urządzenia i usługi → Dodaj integrację** i wyszukaj **Enea Licznik**
2. Wprowadź adres e-mail i hasło z Portalu Odbiorcy Enea
3. Wybierz licznik (PPE) z listy — każdy pokazuje kod PPE, taryfę i adres
4. Wybierz ile dni historii pobrać (7 / 30 / 60 / 90 dni lub **Maksymalnie — ile się da**; domyślnie: maksymalnie)
5. Ustaw interwał odświeżania (domyślnie 3h 30min, minimum 30 min)
6. Gotowe!

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
4. Opcjonalnie: jeśli masz zainstalowaną integrację `enea_prices`, możesz w konfiguracji źródła energii wskazać sensor kosztów jako **encję śledzącą całkowite koszty** — np. `sensor.enea_..._koszt_energii_pobrana_dzien`

> **Uwaga:** Nie dodawaj sensorów `sensor.enea_...` do wykresu zużycia — używaj statystyk zewnętrznych `enea:...`, które mają prawidłowe znaczniki czasu.

## Koszty energii (opcjonalne)

Integracja obsługuje automatyczne obliczanie kosztów energii we współpracy z oddzielną integracją [`enea_prices`](https://github.com/PanSzelescik/home-assistant-enea-prices). Funkcja ta jest całkowicie opcjonalna — jeśli `enea_prices` nie jest zainstalowane, żadna funkcjonalność Enea Licznik nie jest ograniczona.

### Wymagania

- Zainstalowana i skonfigurowana integracja [`enea_prices`](https://github.com/PanSzelescik/home-assistant-enea-prices) z taryfą odpowiadającą taryfie licznika (np. G12)

### Działanie

Gdy obie integracje są skonfigurowane i taryfy się zgadzają, Enea Licznik automatycznie:

1. Tworzy sensory kosztów per strefa i kierunek, np.:
   - Koszt energii pobrana – Dzień
   - Koszt energii pobrana – Noc
   - Koszt energii oddana – Dzień *(jeśli włączone pobieranie generacji)*
2. Wstrzykuje godzinowe statystyki kosztów w PLN, obliczone na podstawie danych energetycznych i cennika z `enea_prices`

### Sensory kosztów

| Encja | Opis |
|-------|------|
| Koszt energii pobrana – Dzień | Skumulowany całkowity koszt energii pobranej w strefie dziennej (PLN) |
| Koszt energii pobrana – Noc | Skumulowany całkowity koszt energii pobranej w strefie nocnej (PLN) |
| *(analogicznie dla energii oddanej)* | |

> **Ważne:** Stan sensora kosztów pokazuje **skumulowaną sumę od początku danych** — nie jest to koszt za bieżący dzień ani miesiąc. Taka architektura jest wymagana przez Home Assistant: Energy Dashboard oblicza koszty dla wybranego okresu jako różnicę między wartościami sum. Sam sensor nie jest szczególnie przydatny do bezpośredniego odczytu.

### Konfiguracja Energy Dashboard z kosztami

Aby śledzić koszty w panelu Energia:

1. **Ustawienia → Energia → Sieć elektryczna**
2. Kliknij ikonę edycji przy dodanym źródle zużycia energii
3. W polu **Encja śledząca całkowite koszty** wybierz odpowiedni sensor kosztów, np. `sensor.enea_..._koszt_energia_pobrana_dzien`

### Automatyczne przeładowanie

Jeśli `enea_prices` zostanie zainstalowane po Enea Licznik, integracja Enea automatycznie się przeładuje i utworzy sensory kosztów — nie jest wymagane ręczne przeładowanie.

## Encje

### Sensory energii

Widoczne w panelu **Energia** Home Assistant. Liczba sensorów strefowych zależy od taryfy (G11 = brak stref, G12 = 2 strefy, G13 = 3 strefy).

| Encja                   | Opis | Przykład |
|-------------------------|------|---------|
| Energia pobrana         | Całkowite zużycie energii (suma stref) | 0,6580 kWh |
| Energia pobrana – Dzień | Zużycie w strefie dziennej | 0,6580 kWh |
| Energia pobrana – Noc   | Zużycie w strefie nocnej | 0,0000 kWh |
| Energia oddana          | Całkowita energia oddana do sieci (suma stref) | 0,1080 kWh |
| Energia oddana – Dzień  | Energia oddana w strefie dziennej | 0,1080 kWh |
| Energia oddana – Noc    | Energia oddana w strefie nocnej | 0,0000 kWh |

### Sensory kosztów *(wymaga enea_prices)*

Tworzone per strefa i kierunek, gdy integracja `enea_prices` jest skonfigurowana z pasującą taryfą.

| Encja | Opis | Przykład |
|-------|------|---------|
| Koszt energii pobrana – Dzień | Skumulowany koszt energii pobranej w strefie dziennej | 1234,56 PLN |
| Koszt energii pobrana – Noc | Skumulowany koszt energii pobranej w strefie nocnej | 234,56 PLN |
| *(analogicznie dla energii oddanej)* | | |

### Sensory diagnostyczne

| Encja | Opis | Przykład                    |
|-------|------|-----------------------------|
| Grupa taryfowa | Nazwa grupy taryfowej | G12                         |
| Moc umowna | Moc umowna | 14 kW                       |
| Status | Status licznika | Aktywny_Pod napięciem...    |
| Adres | Adres punktu poboru energii | Pastelowa 8, 60-198, Poznań |
| Ostatni odczyt | Data i godzina ostatniego odczytu z licznika | 2 marca 2026, 14:32         |
| Model licznika | Model aktualnie zamontowanego licznika | OTUS3                       |

## Opcje

Dostępne przez **Ustawienia → Urządzenia i usługi → Enea → Konfiguruj**:

| Opcja | Domyślnie | Opis |
|-------|-----------|------|
| Interwał odświeżania | 3h 30min | Jak często odpytywać Portal Odbiorcy Enea (minimum 30 min) |
| Pobieraj statystyki energii pobranej | Tak | Wyłącz jeśli chcesz oszczędzić requesty do API |
| Pobieraj statystyki energii oddanej | Tak | Wyłącz jeśli nie masz fotowoltaiki ani innego źródła generacji |

Zmiana opcji powoduje natychmiastowe przeładowanie integracji. Wyłączenie danego kierunku ukrywa też odpowiednie sensory energii i kosztów.

Jeśli masz kilka liczników na tym samym koncie, integracja loguje się tylko raz i współdzieli sesję między licznikami.

### Ręczne odświeżanie

Możesz wymusić natychmiastowe pobranie danych przez akcję **`enea.refresh`**:

**Narzędzia deweloperskie → Akcje → `enea.refresh`** → wybierz urządzenie → Wywołaj

Jeśli nie wybierzesz urządzenia, zostaną odświeżone wszystkie skonfigurowane liczniki.

### Uzupełnianie historii (backfill)

Jeśli chcesz zaimportować statystyki dla konkretnego zakresu dat (np. dosięgnąć dalej niż początkowy backfill lub ponownie zaimportować problematyczny okres), użyj akcji **`enea.backfill`**:

**Narzędzia deweloperskie → Akcje → `enea.backfill`**

Dostępne parametry (wszystkie opcjonalne):

| Parametr | Opis |
|----------|------|
| Urządzenie | Konkretny licznik; puste = wszystkie |
| Data początkowa | Pierwsza data do zaimportowania (YYYY-MM-DD) |
| Data końcowa | Ostatnia data (domyślnie: wczoraj) |
| Liczba dni wstecz | Alternatywa dla dat — ile dni wstecz od wczoraj |
| *(brak parametrów)* | Domyślnie importuje ostatnie 30 dni |

### Zmiana danych logowania

Aby zmienić adres e-mail lub hasło bez usuwania integracji:

**Ustawienia → Urządzenia i usługi → Enea → menu (⋮) → Zmień konfigurację**

### Diagnostyki

W przypadku problemów pobierz raport diagnostyczny (hasło i adres są automatycznie ukrywane):

**Ustawienia → Urządzenia i usługi → Enea → menu (⋮) → Pobierz diagnostyki**

## Znane ograniczenia

- API Enea jest nieoficjalne i może ulec zmianie bez ostrzeżenia
- Odczyty energii są wartościami skumulowanymi — przy wymianie licznika nowe urządzenie zaczyna od 0; integracja automatycznie wykrywa datę i godzinę montażu aktualnego licznika i importuje tylko dane z godzin po montażu, więc historia zaczyna się od momentu wymiany
- Dni z zerowym zużyciem są uwzględniane w statystykach — nie wpływa to na poprawność sum, ponieważ zero nie zmienia wartości skumulowanej
- Integracja była testowana wyłącznie na taryfie **G12**; działanie na G11, G12w i G13 jest możliwe, ale niezweryfikowane

## Rozwiązywanie problemów

- Upewnij się, że logujesz się tymi samymi danymi co na [portalodbiorcy.operator.enea.pl](https://portalodbiorcy.operator.enea.pl/)
- Sprawdź logi Home Assistant (`Ustawienia → System → Logi`) w poszukiwaniu błędów z domeny `enea`

## Podziękowania

Inspirowane integracją [Tauron AMIplus](https://github.com/PiotrMachowski/Home-Assistant-custom-components-Tauron-AMIplus) autorstwa [@PiotrMachowski](https://github.com/PiotrMachowski).

Napisane przy pomocy [Claude Code](https://claude.ai/claude-code).

## Prawa autorskie

Logo Enea pochodzi z oficjalnej teczki prasowej Enea S.A.: [media.enea.pl/teczka-prasowa/logotypy](https://media.enea.pl/teczka-prasowa/logotypy). Wszelkie prawa do logotypu należą do Enea S.A.
