# Craft the Context - Code Completion Context Strategy

Hackathon solution for the "Craft the Context" task - building a context collection pipeline that enriches code completion points with relevant repository-wide information to maximize ChrF score across three LLM backends.

## Problem

Given a set of completion points in a codebase, compose the most informative context possible for each point. The context is fed to three models (Mellum, Codestral, Qwen2.5-Coder) which generate completions evaluated against ground truth using **ChrF** (character n-gram F-score).

The challenge is entirely in **context engineering** - the models are fixed, so the only lever is what information you provide them.

## Scoring

- **Metric:** ChrF - measures character-level n-gram similarity between generated and reference completions
- **Final score:** Average ChrF across all three models and all completion points
- **Evaluation pipeline:** format validation → context injection → model completion → ChrF vs. ground truth

[Repo with baseline kit](https://github.com/JetBrains-Research/EnsembleAI2026-starter-kit)

[New public dataset](https://drive.google.com/drive/folders/1U5WGXTqbED9vnkk2lDWJA4ETaxrut2we)




## Rozwiązanie

Plik z implementacją znajduje się w `core/engine.py`.


**Architektura**

Autocomplete działa w trzech odrębnych fazach:

### Faza 1: Szybki odczyt repozytorium w czasie O(N) i parsowanie za pomocą wyrażeń regularnych (Regex)

* [cite_start]**Zachowanie surowego tekstu:** Pliki są odczytywane w ich surowym formacie tekstowym, aby zachować dokładne wcięcia, co jest kluczowe, ponieważ końcowa metryka ewaluacyjna (ChrF) jest wysoce wrażliwa na składnię i odstępy na poziomie znaków[cite: 41, 42].
* **Ekstrakcja definicji:** Skrypt skanuje każdy plik `.py` przy użyciu wyrażeń regularnych, aby wyodrębnić definicje klas i funkcji (`def`, `async def`, `class`).
* **Słownictwo zapasowe (Fallback Vocabulary):** Szerszy zbiór wszystkich słów dłuższych niż 3 znaki jest również buforowany do wtórnego dopasowywania leksykalnego.

### Faza 2: Ukierunkowana ekstrakcja "Luki FIM" (Fill-in-the-Middle)

* **Wycinanie kontekstowe:** Algorytm izoluje bezpośredni kontekst wokół kursora programisty, wyodrębniając ostatnich 20 linii prefiksu i pierwszych 20 linii sufiksu.
* **Destylacja słownictwa:** Skrypt usuwa standardowe słowa ignorowane (stopwords) w Pythonie (`import`, `def`, `return`, `pass` itp.), aby wygenerować bardzo specyficzne "słownictwo docelowe" identyfikatorów, których model potrzebuje do pomyślnego wypełnienia luki w kodzie.

### Faza 3: Składanie i obrona przed przycinaniem od lewej (Left-Trim Defense)

* **Dwupoziomowe rankowanie:** * **Poziom 1 (Złoty):** Pliki, które definiują klasę lub funkcję używaną w pobliżu luki FIM.
    * **Poziom 2 (Srebrny):** Pliki, które wykazują ogólne pokrycie leksykalne z kontekstem luki FIM.
* [cite_start]**Budżetowanie:** Aby zmieścić się w ścisłym oknie kontekstowym 8K tokenów modelu Mellum od JetBrains[cite: 37], skrypt wymusza ścisły limit 24 000 znaków (`MAX_CONTEXT_CHARS`). Ogromne pliki są mocno skracane (`MAX_CHARS_PER_FILE = 4000`), aby zapobiec zdominowaniu budżetu kontekstowego przez jeden pojedynczy plik.
* [cite_start]**Odwrócenie po lewym przycięciu (Left-Trim Reversal):** Ewaluatorzy przycinają przesłany przez użytkownika kontekst od lewej strony, aby dopasować go do limitów okna kontekstowego odpowiednich modeli[cite: 37]. Aby się przed tym uchronić, skrypt odwraca końcowy proces składania fragmentów. Zapewnia to, że najbardziej krytyczny kontekst (dopasowania definicji z Poziomu 1) zostaje umieszczony na samym dole (po prawej stronie) bloku kontekstowego, gwarantując, że będzie to ostatnia rzecz podlegająca przycięciu.
* [cite_start]**Formatowanie:** Fragmenty kontekstu są łączone za pomocą wymaganego tokena `<|file_sep|>`[cite: 38].