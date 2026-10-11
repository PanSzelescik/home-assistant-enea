# Współtworzenie Enea Licznik

Dzięki, że chcesz pomóc! Ten plik opisuje, jak przygotować zmianę, żeby dało się ją szybko
przejrzeć i wydać.

*In English: issues and pull requests in English are welcome. Run the tests with
`uv run --locked --group dev pytest -q`, never commit real customer data (PPE numbers,
meter ids, addresses — the `data/` directory is git-ignored for that reason), write commit
messages in English and add a test that fails without your fix.*

## Zanim zaczniesz

- **Pytania** o instalację i konfigurację zadawaj w [Dyskusjach](https://github.com/PanSzelescik/home-assistant-enea/discussions).
- **Błędy, pomysły i nowe modele liczników** zgłaszaj przez szablony w zakładce Issues —
  formularz nowego modelu licznika otwiera się też z przycisku w Naprawach Home Assistant.
- **Większą zmianę** (nowa funkcja, zmiana zachowania statystyk lub kosztów) najpierw opisz
  w zgłoszeniu — łatwiej ustalić podejście, zanim powstanie kod.

Architektura, format API Portalu Odbiorcy Enea i powody wielu decyzji są opisane
w [`AGENTS.md`](AGENTS.md). Zajrzyj tam przed zmianą w statystykach, kosztach lub rachunku.

## Środowisko

Potrzebujesz [uv](https://docs.astral.sh/uv/); wersję Pythona (3.14) uv pobierze sam.

```sh
uv sync --locked --group dev
uv run --locked --group dev pytest -q
```

Pokrycie kodu (linie i rozgałęzienia, raport HTML w `htmlcov/index.html`):

```sh
uv run --locked --group dev coverage run -m pytest -q
uv run --locked --group dev coverage report
uv run --locked --group dev coverage html
```

Testy nie łączą się z Portalem Odbiorcy Enea i nie wymagają konta. Część z nich uruchamia
prawdziwy Home Assistant (`tests/ha_instance.py`) z atrapą portalu, recordera i zegara.

### Testy kontraktu z Enea Ceny

Enea Licznik i [Enea Ceny](https://github.com/PanSzelescik/home-assistant-enea-prices)
czytają się nawzajem po nazwach (atrybuty, klucze wpisów, moduły), bez importów.
`tests/test_enea_prices_contract.py` ładuje prawdziwy kod Enea Ceny z checkoutu:

- domyślnie z katalogu `home-assistant-enea-prices` obok tego repo,
- albo ze ścieżki w zmiennej `ENEA_PRICES_PATH`.

Bez checkoutu te testy są pomijane. CI zawsze je uruchamia na gałęzi `master` Enea Ceny,
a lustrzane testy w Enea Ceny — na `master` tego repo. Zmiana czegoś, co czyta druga
integracja, wymaga więc pull requestów w obu repo; opisz w nich, w jakiej kolejności je
scalić.

## Dane odbiorcy

Repo jest publiczne, a logi i raporty diagnostyczne trafiają do publicznych zgłoszeń.

- **Nigdy nie commituj prawdziwych danych** — numeru PPE, ID licznika z Portalu Odbiorcy Enea
  (`meter_id`), adresu, numerów seryjnych i numerów umów. Dotyczy kodu, testów, dokumentacji,
  opisów commitów i pull requestów. W przykładach używaj zmyślonych wartości, np. `meter_id`
  `12345` i PPE `590310600000001234`.
- Zapisane odpowiedzi API trzymaj w `data/` (jest w `.gitignore`). Fixture'y testowe
  z prawdziwego konta twórz skryptem, który podmienia identyfikatory, skaluje zużycie i odmawia
  zapisu, gdy coś zostałoby w wyniku:

  ```sh
  uv run python scripts/anonymize_fixtures.py
  ```

- W logach numer PPE i `statistic_id` przepuszczaj przez `mask_ppe()`, `meter_id` pomijaj,
  a tekst wyjątków aiohttp, które cytują URL, przez `hide_meter_id()`. Nowe pola
  identyfikujące odbiorcę dopisz do `TO_REDACT` w `diagnostics.py`.

## Kod

Najważniejsze zasady (pełna lista w [`AGENTS.md`](AGENTS.md#konwencje-ogólne)):

- Stałe w `const.py`, w ustalonej kolejności sekcji.
- Docstring dla każdej nowej funkcji, metody i klasy.
- „Teraz” i „dziś” wyłącznie z zegara Home Assistant: `dt_util.now()` — nigdy `date.today()`
  ani `datetime.now()`. Pilnuje tego `tests/test_clock.py`.
- Stan encji koordynatora przypisuj do pól `_attr_*`, nie licz go w `@cached_property`.
- Bez metod, które tylko wołają inną metodę.
- Zawsze „Portal Odbiorcy Enea” — także w tekstach angielskich.
- Teksty interfejsu zmieniaj w obu tłumaczeniach: `translations/pl.json` i `translations/en.json`.

## Testy

- Poprawka błędu przychodzi z testem, który bez niej nie przechodzi.
- Nowe zachowanie testuj tam, gdzie widzi je użytkownik: pełny setup w działającym Home
  Assistant (`test_setup_entry.py`, `test_refresh_cycle.py` i pokrewne) albo zanonimizowane
  odpowiedzi portalu (`test_real_data.py`). Czyste funkcje (`parsing.py`, `costs.py`)
  testuj bezpośrednio.
- Testy muszą przechodzić w każdej strefie czasowej procesu — Home Assistant bez `TZ` działa
  w UTC, a liczy dni po polsku.

## Commity i pull requesty

- Commity po angielsku, w trybie rozkazującym, opisujące zachowanie, np.
  `Count only loaded entries when unloading`. Jeden commit — jedna logiczna zmiana.
- Pull request kieruj do `master`. W opisie: co się zmienia i dlaczego (*Summary*) oraz jak
  zostało sprawdzone (*Test plan*).
- Przed scaleniem muszą przejść workflow **Tests**, **Validate HACS** i **Validate with hassfest**.
- Nie podbijaj wersji w `manifest.json` ani `pyproject.toml` — wydania przygotowuje opiekun.
