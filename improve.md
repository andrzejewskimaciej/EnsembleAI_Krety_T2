
## Jak działa engine.py (silnik bazowy)

`engine.py` to prosty silnik kontekstowy oparty na dopasowaniu identyfikatorów przez wyrażenia regularne.

**Cykl działania:**
1. Dla każdego datapointa wyodrębnia ostatnie 20 linii prefixu i pierwsze 20 linii sufiksu ("okno FIM").
2. Ze słownictwa okna FIM usuwa stopwords Pythona (`import`, `def`, `self` itp.).
3. Dla każdego pliku `.py` w repozytorium sprawdza dwa warunki:
   - **Tier 1 (definicje):** czy jakikolwiek identyfikator z okna FIM pokrywa się z nazwą funkcji/klasy *zdefiniowaną* w danym pliku (wykrytą regexem `^def|class`). Pliki z pokryciem trafiają do wyższego priorytetu.
   - **Tier 2 (słowa):** czy liczba wspólnych słów przekracza próg > 3. Pliki spełniające warunek trafiają do niższego priorytetu.
4. Oba tiery są posortowane malejąco po wyniku i pakowane do kontekstu zachłannie, aż do limitu 24 000 znaków. Każdy plik jest przycinany do 4 000 znaków (zawsze od początku pliku).
5. Wynikowy kontekst jest odwracany (najwyższy priorytet na dole, by uniknąć przycinania przez modele od lewej).

**Ograniczenia:** Ignoruje pole `modified`, nie obsługuje importów względnych, nie różnicuje "identyfikator zdefiniowany" od "identyfikator wywoływany", przycinanie pliku zawsze bierze głowicę (pierwsze znaki).

---

## Jak działa engine_improve.py (silnik ulepszony)

`engine_improve.py` rozszerza silnik bazowy o sześć warstw tierowania i kilka technik poprawiających precyzję doboru kontekstu.

**Budowanie cache repozytorium:**
- Definicje funkcji i klas wyodrębniane przez `ast.parse()` (regex jako fallback przy `SyntaxError`).
- Tokeny BM25 generowane przez `_tokenize()`: split na słowa + ekspansja camelCase/snake_case (`MyClass` → `my`, `class`; `get_value` → `get`, `value`).
- Globalny słownik częstości dokumentów (`df`) i średnia długość dokumentu (`avg_dl`) obliczane raz dla całego repozytorium.

**Budowanie zapytania (query) z okna FIM:**
- Szersze okno: 40 linii (zamiast 30).
- `extract_local_words` — surowe słownictwo.
- `extract_called_names` — nazwy wywoływane jako funkcje/konstruktory: `Foo(` lub `obj.bar(` — te nazwy dostają 20× mnożnik przy scoringu.
- `extract_imported_modules` — moduły z instrukcji `import`/`from ... import`.
- `resolve_relative_imports` — `from . import X` zamieniany na konkretną ścieżkę pliku.

**Sześć tierów priorytetowych (od najwyższego):**
| Tier | Zawartość | Dlaczego |
|------|-----------|----------|
| `tier_rel` | Pliki wskazane przez importy względne | Deterministyczne wskazanie na plik |
| `tier_mod` | Pole `modified` z datapointa | Współedycja = silna korelacja |
| `tier_samedir` | Pliki z tego samego katalogu (+ `__init__.py` rodziców) | Sąsiedztwo przestrzenne |
| `tier_1_defs` | Pliki definiujące wywołane/obecne symbole | Semantyczne dopasowanie |
| `tier_2_imports` | Pliki pasujące do nazwy importowanego modułu | Import = zależność |
| `tier_3_bm25` | Reszta plików wg pełnego BM25 | Szerokie pokrycie |

**Inteligentne wycinanie fragmentu (sliding window):** zamiast brać pierwsze 4 000 znaków, okno przesuwa się po liniach i wybiera fragment o najwyższej gęstości dopasowanych identyfikatorów.

**Mapa projektu:** jeśli po wypełnieniu wszystkich tierów zostało miejsce w budżecie, dołączana jest struktura katalogów projektu (`__project_map__`).

---

## Wyniki (zbiór practice, 47 punktów ukończenia)

| Metryka | Baseline (engine.py) | Ulepszony  (engine_improve.py) | Delta |
|---|---|---|---|
| Identifier Hit Rate (IHR) | 0.7287 | 0.7894 | +0.0607 (+8.3%) |
| Import Coverage (IC) | 0.9048 | 0.9783 | +0.0735 (+8.1%) |
| Średnia długość kontekstu (znaki) | 21 712 | 23 927 | +10.2% |
| Średnia liczba wstrzykniętych plików | 6.51 | 10.66 | +64% |

- **Lepszy**: 26/47 punktów (55.3%)
- **Taki sam**: 8/47 (17.0%)
- **Gorszy**: 13/47 (27.7%)

## Co się zmieniło

### 1. Pole `modified` — pliki współzmienione 
Każdy punkt danych zawiera listę `modified` — pliki edytowane w tej samej sesji/commicie.
Trafiają teraz do najwyższego tieru (powyżej same-dir), bo współedycja = silna korelacja semantyczna.
Poprzedni silnik całkowicie ignorował to pole.


### 2. Poziom 0: Pliki z tego samego katalogu
Pliki z tego samego folderu co plik docelowy mają wysoki priorytet.
`__init__.py` z bieżącego katalogu dostaje dodatkowy bonus (+500) — definiuje publiczne API pakietu.

**Dlaczego to pomaga:** Pliki testowe sąsiadują z innymi testami. Moduły biblioteki sąsiadują ze swoimi pomocnikami.

### 3. Rozwiązywanie importów względnych (dokładne dopasowanie pliku)
`from . import foo` oraz `from .module import Bar` są teraz zamieniane na konkretne ścieżki plików
(`transport/foo.py`, `transport/module.py`) i trafiają do tieru najwyższego priorytetu.
Poprzedni silnik używał tylko przybliżonego dopasowania po nazwie stem modułu.

**Dlaczego to pomaga:** Import względny to deterministyczne wskazanie na konkretny plik — zero fałszywych trafień, wysoka precyzja.

### 4. Ekstrakcja miejsc wywołań (call-site extraction)
Oprócz identyfikatorów obecnych w oknie FIM, wyodrębniamy teraz nazwy **wywoływane jako funkcje/konstruktory**: `SomeClass(`, `some_func(`.
Pliki, które *definiują* wywoływany symbol, dostają 20× mnożnik przy scoringu.

**Dlaczego to pomaga:** Wywoływany symbol to dokładnie to, czego model potrzebuje do uzupełnienia — wyższe znaczenie niż przypadkowe współwystępowanie identyfikatora.

### 5. Inteligentne wycinanie fragmentu pliku (sliding window)
Poprzedni silnik zawsze brał pierwsze MAX_CHARS znaków pliku.
Teraz okno przesuwne szuka gęstości dopasowań identyfikatorów i wyciąga najistotniejszy fragment.

**Dlaczego to pomaga:** Duże pliki (>4 000 znaków) często mają kluczową klasę lub funkcję w środku, nie na początku.

### 6. Lepsza tokenizacja BM25 (podział camelCase / snake_case)
`MyClass` → `my`, `class`; `get_value` → `get`, `value`.
Tokenizacja rozszerzona o pod-tokeny, co poprawia dopasowania między różnymi konwencjami nazewnictwa.

### 7. Rodzicielskie pliki `__init__.py`
Pliki `__init__.py` z katalogów nadrzędnych pliku docelowego trafiają teraz do tieru same-dir (z niższym bonusem +200).

### 8. Ekstrakcja definicji przez AST
Użyto `ast.parse()` do wyodrębniania nazw funkcji i klas zamiast czystego regex.
Przy `SyntaxError` fallback do regex.

### 9. BM25 dla tieru 3 (zasięg)
Zastąpiono proste zliczanie wspólnych słów algorytmem BM25 — obniża wagę popularnych identyfikatorów (`data`, `value`, `result`) i nagradza rzadkie, specyficzne.

### 10. Obrona przed przycinaniem od lewej
Fragmenty odwrócone przed złączeniem → najwyższy priorytet na dole (prawa strona).
Okna kontekstowe modeli przycinają od lewej, więc krytyczny kontekst jest zawsze zachowany.

### 11. Szersze okno FIM (40 linii zamiast 30)
Więcej linii prefixu/sufiksu do budowania słownictwa dla dopasowania.

## Gdzie ulepszony silnik nadal przegrywa (27.7% przypadków)

- Pliki bez sąsiadów w tym samym katalogu (izolowane skrypty): brak korzyści z tieru same-dir
- Przypadki, gdzie `modified` zawiera nieistotne pliki i wypiera ważniejszy kontekst same-dir
- Mapa projektu (~1 tys. znaków) może wyprzeć jeden dodatkowy plik przy napiętym budżecie
- Smart truncation może w rzadkich przypadkach wybrać gorszy fragment niż prosta głowica pliku

### Dlaczego wykrywanie importów jest poziomem 2, a nie 0
Pierwsza wersja umieszczała importy jako poziom 0. Dla `t/integration/test_redis.py` importy wskazują `import kombu` → silnik wybierał moduły wewnętrzne biblioteki kombu. Ale test potrzebuje `t/integration/common.py` (ten sam katalog). Pliki importów zajęły budżet, pliki same-dir zostały wyparte.
Wynik: IHR SPADŁ z 0.73 do 0.52.

### Dlaczego nie pobrano lokalnego LLM do ewaluacji
Zbiór practice nie ma ground-truth ukończeń. Pobieranie Qwen2.5-Coder-0.5B wymagałoby:
- ~1 GB pobrania + czas działania
- GPU/CPU do wnioskowania na 47 punktach
- I tak dawałoby opinię tylko jednego modelu (ewaluacja używa 3: Mellum, Codestral, Qwen2.5-Coder)

Metryki proxy IHR/IC działają w <10 s i bezpośrednio mierzą to, co nagradza ChrF.
