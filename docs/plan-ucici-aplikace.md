# Implementační plán: desktopová učící aplikace nad PDF

Osobní desktopová aplikace v Pythonu, která z ~200stránkového PDF s učivem jednorázově
vygeneruje banku otázek přes Claude API a pak slouží jako offline kvízový trenažér
s náhledem zdroje přímo v PDF.

---

## 1. Architektonická rozhodnutí (a proč)

| Rozhodnutí | Volba | Důvod |
|---|---|---|
| Jazyk | Python 3.11+ | rychlá iterace, dobrý PDF i LLM ekosystém |
| GUI | PySide6 (Qt 6) | nativní okno, `QSplitter`, `QtPdf` widget zdarma |
| Náhled PDF | `QPdfView` + `QPdfDocument` | scroll, zoom, výběr textu a zvýraznění out-of-the-box |
| Extrakce textu | PyMuPDF (`pymupdf`) | rychlá, drží čísla stránek a souřadnice slov |
| Úložiště | SQLite (`sqlite3` ze stdlib) | single-user desktop, jeden soubor, nula instalace |
| Migrace schématu | Alembic | SQLite má slabé `ALTER TABLE`, schéma se bude měnit |
| LLM | Claude API, běžná (ne batch) volání | živý progress bar > 50% sleva u jednorázového běhu |
| Strukturovaný výstup | tool schema, ne „vrať JSON" | výrazně spolehlivější parsování |
| Embeddingy | `sentence-transformers`, lokálně na CPU | jen na deduplikaci banky, žádná vektorová DB |

**Klíčový princip:** generování je jednorázová dávková fáze, běh aplikace je pak
offline a bez API klíče. LLM za běhu zůstává jen jako volitelné hodnocení
otevřených odpovědí.

**Druhý klíčový princip:** stav se drží **per chunk** v DB. Generování je tedy
kdykoli přerušitelné, obnovitelné a přegenerovatelné po částech.

---

## 2. Struktura projektu

```
studyapp/
├── main.py                  # entrypoint, spuštění QApplication
├── config.py                # cesty, konstanty, načtení API klíče z keyring
├── db/
│   ├── models.py            # SQLAlchemy modely (nebo čisté SQL, viz níže)
│   ├── repo.py              # dotazy: pending chunks, otázky k procvičení, ...
│   └── migrations/          # Alembic
├── ingest/
│   ├── extract.py           # PDF -> text po stránkách + slova se souřadnicemi
│   └── chunker.py           # text -> chunky s page_start/page_end
├── generate/
│   ├── client.py            # obal nad anthropic SDK, retry/backoff
│   ├── prompts.py           # systémový prompt + tool schema
│   ├── validate.py          # ověření source_quote proti textu chunku
│   ├── rects.py             # předpočet souřadnic zvýraznění
│   └── worker.py            # QObject worker se signály progress/chunk_done
├── embed/
│   ├── encode.py            # výpočet embeddingů otázek
│   └── dedupe.py            # cosine similarity, deaktivace duplicit
├── ui/
│   ├── main_window.py       # QMainWindow + QTabWidget (Kvíz / Generování)
│   ├── generate_view.py     # výběr PDF, progress bar, tabulka chunků
│   ├── quiz_view.py         # levá polovina splitu
│   └── pdf_view.py          # pravá polovina splitu
└── tests/
```

Poznámka k `db/`: pro tenhle rozsah stačí čisté SQL v `repo.py`.
SQLAlchemy dává smysl, pokud chceš z Alembicu autogenerovat migrace. Rozhodni se
na začátku a nemíchej to.

---

## 3. Datový model

```sql
CREATE TABLE documents (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL,
    filename     TEXT NOT NULL,
    sha256       TEXT NOT NULL UNIQUE,
    page_count   INTEGER NOT NULL,
    created_at   TEXT NOT NULL          -- ISO 8601, SQLite nemá datetime typ
);

CREATE TABLE chunks (
    id           INTEGER PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES documents(id),
    ordinal      INTEGER NOT NULL,
    page_start   INTEGER NOT NULL,      -- 0-based, ať sedí na PyMuPDF i QtPdf
    page_end     INTEGER NOT NULL,
    text         TEXT NOT NULL,
    sha256       TEXT NOT NULL,         -- detekce změny -> přegenerovat
    status       TEXT NOT NULL,         -- pending | running | done | skipped | failed
    error        TEXT,
    updated_at   TEXT NOT NULL
);

CREATE TABLE questions (
    id            INTEGER PRIMARY KEY,
    chunk_id      INTEGER NOT NULL REFERENCES chunks(id),
    type          TEXT NOT NULL,        -- mcq | open
    question      TEXT NOT NULL,
    options_json  TEXT,                 -- JSON pole variant, jen u mcq
    correct_index INTEGER,              -- index do options_json, jen u mcq
    correct_text  TEXT,                 -- vzorová odpověď, jen u open
    explanation   TEXT,
    source_quote  TEXT NOT NULL,
    quote_score   REAL,                 -- shoda citace s textem chunku, 0–1
    page          INTEGER,              -- DOPOČÍTANÉ z nálezu citace, ne od modelu
    rects_json    TEXT,                 -- předpočítané souřadnice zvýraznění
    embedding     BLOB,                 -- np.float32.tobytes()
    is_active     INTEGER NOT NULL DEFAULT 1,  -- 0 = duplicita nebo vyřazeno
    starred       INTEGER NOT NULL DEFAULT 0,
    edited_at     TEXT,                 -- ruční úprava; regenerace se jí nedotkne
    created_at    TEXT NOT NULL
);

CREATE TABLE sessions (
    id           INTEGER PRIMARY KEY,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    types_json   TEXT NOT NULL,         -- ["mcq"] | ["open"] | ["mcq","open"]
    source       TEXT NOT NULL,         -- all | mistakes | chapter
    filter_json  TEXT                   -- rozsah kapitol, limit počtu otázek
);

CREATE TABLE attempts (
    id           INTEGER PRIMARY KEY,
    session_id   INTEGER REFERENCES sessions(id),
    question_id  INTEGER NOT NULL REFERENCES questions(id),
    answered_at  TEXT NOT NULL,
    user_answer  TEXT,                  -- zvolená varianta u mcq, NULL u open
    is_correct   INTEGER NOT NULL,      -- 0/1; u open self-grading (Knew / Didn't)
    self_graded  INTEGER NOT NULL DEFAULT 0,
    revealed_ms  INTEGER                -- jak dlouho trvalo, než odhalil zdroj
);

-- až ve fázi 9
CREATE TABLE srs (
    question_id  INTEGER PRIMARY KEY REFERENCES questions(id),
    ease         REAL NOT NULL DEFAULT 2.5,
    interval_days INTEGER NOT NULL DEFAULT 0,
    repetitions  INTEGER NOT NULL DEFAULT 0,
    due_at       TEXT NOT NULL
);

CREATE INDEX idx_chunks_status ON chunks(status);
CREATE INDEX idx_questions_chunk ON questions(chunk_id);
CREATE INDEX idx_questions_active ON questions(is_active, type);
CREATE INDEX idx_attempts_question ON attempts(question_id, id);
```

**Proč `correct_index` a ne `correct` jako text.** Model občas správnou odpověď
přeformuluje tak, že přesně neodpovídá žádné z `options`, a porovnávání stringů
by ti pak tiše označovalo správné odpovědi za špatné. Index je jednoznačný
a přežije i zamíchání variant.

**Proč `page` smí být NULL.** Dopočítává se z toho, kde se citace v PDF našla.
Když se nenajde, je otázka stejně podezřelá a míří do „needs review": lepší
prázdno než smyšlené číslo.

Duplicity a špatné otázky **nemazat**, jen `is_active = 0`. Ušetří to vysvětlování,
proč jich je najednou míň.

---

## 4. Fáze implementace

Každá fáze je samostatně otestovatelná. Nepokračuj dál, dokud neplatí „hotovo, když".

### Fáze 0: Skeleton
```bash
python -m venv .venv && source .venv/bin/activate
pip install PySide6 pymupdf anthropic alembic keyring numpy
pip install sentence-transformers   # až ve fázi 6, je to velká závislost
```
Prázdné `QMainWindow` se dvěma taby.

**Jazyková politika. Rozhodni hned na začátku, jsou to tři nezávislé věci:**

| Co | Jazyk | Poznámka |
|---|---|---|
| UI aplikace (popisky, tlačítka, hlášky) | **angličtina** | všechny stringy obal do `self.tr("...")` |
| Text učiva v PDF | jazyk zdroje | neměnit, nepřekládat |
| `source_quote` v otázkách | **vždy jazyk zdroje, doslovně** | jinak přestane fungovat validace i zvýrazňování |
| Znění otázek a vysvětlení | konfigurovatelné | `QUESTION_LANGUAGE` v `config.py` |

`self.tr()` stojí navíc pár znaků a umožní ti kdykoli přidat český překlad přes
Qt Linguist (`pyside6-lupdate` / `.ts` soubory) bez přepisování kódu. I když teď
chceš jen angličtinu, nedávej stringy natvrdo.

**Hotovo, když:** okno se otevře a zavře, popisky jsou anglické.

---

### Fáze 1: Extrakce a chunking (bez GUI, jako CLI skript)

`extract.py`:
- `fitz.open(path)`, pro každou stránku `page.get_text("text")` a `page.get_text("words")`
- vrátit seznam stránek: `{page_no, text, words}`

`chunker.py`:
- slučovat stránky do chunků ~1500–2500 slov, respektovat hranice kapitol
  (heuristika: řádek psaný velkými písmeny, číslování `1.2.3`, výrazně větší font
  font si vytáhneš z `page.get_text("dict")`)
- držet `page_start` / `page_end`
- normalizovat whitespace, ale **neměnit slova**, jinak ti přestane sedět
  validace citací

**Hotovo, když:** skript vypíše počet chunků, průměrnou délku a u náhodných 5
sedí rozsah stránek na to, co je v PDF.

---

### Fáze 2: Databáze
Schéma výše, Alembic init, `repo.py` s funkcemi `insert_document`,
`insert_chunks`, `get_pending_chunks`, `set_chunk_status`, `insert_questions`.

**Hotovo, když:** import PDF naplní `documents` + `chunks` se statusem `pending`.

---

### Fáze 3: Generování otázek (pilot na 3–5 chuncích)

Tool schema:

```python
QUESTION_TOOL = {
    "name": "save_questions",
    "description": "Ulož vygenerované otázky k danému úseku učiva.",
    "input_schema": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["mcq", "open"]},
                        "question": {"type": "string"},
                        "options": {
                            "type": "array", "items": {"type": "string"},
                            "description": "4 varianty, pouze u type=mcq"
                        },
                        "correct": {"type": "string"},
                        "explanation": {"type": "string"},
                        "source_quote": {
                            "type": "string",
                            "description": "Doslovný souvislý úsek dodaného textu, "
                                           "ze kterého odpověď plyne. Neupravovat."
                        },
                    },
                    "required": ["type", "question", "correct", "source_quote"],
                },
            }
        },
        "required": ["questions"],
    },
}
```

Systémový prompt (kostra):
- jsi zkušený vysokoškolský pedagog, tvoříš otázky ke zkoušce
- ptej se **jen** na to, co je v dodaném textu; nic nedoplňuj z obecných znalostí
- `source_quote` musí být doslovný souvislý úsek dodaného textu, kopíruj znak po znaku
- u `mcq` vytvoř 4 varianty, distraktory musí být věrohodné a ze stejné oblasti
  (ne zjevně nesmyslné)
- **počet otázek přizpůsob obsahu, 2 až 8**: u obsahu, rejstříku nebo titulní
  stránky klidně nula
- **`source_quote` vždy doslovně v jazyce zdrojového textu**: nikdy nepřekládej,
  na tomhle poli stojí validace i zvýrazňování v PDF
- znění otázky, variant a vysvětlení v jazyce podle `QUESTION_LANGUAGE`

Volání s `tool_choice={"type": "tool", "name": "save_questions"}`.

**Validace v `validate.py`:**
1. `source_quote` v normalizovaném textu chunku → OK
2. jinak `difflib.SequenceMatcher(None, quote, best_window).ratio() > 0.9` → OK
3. jinak zahodit otázku a zalogovat
4. u `mcq`: `correct` **musí přesně odpovídat jedné z `options`**: model ho občas
   přeformuluje. Neshoda → zahodit. Do DB ukládej index, ne string.
5. **`page` si dopočítej sám**, nechtěj ho po modelu. Model čísla stránek v textu
   nevidí, takže by je vymyslel. Chunk zná svůj rozsah `page_start`–`page_end`;
   projdi ty stránky, najdi na které citace leží, a to číslo ulož. Když se
   nenajde na žádné → `page = NULL` a otázka jde do „needs review".
6. ulož `quote_score` (poměr z bodu 2), je to nejlepší jednoduchý indikátor
   toho, že model v daném úseku halucinoval.

**Před uložením zamíchej `options`.** LLM dává správnou odpověď systematicky
na podobnou pozici (typicky poslední nebo nejdelší variantu) a ty si na to
podvědomě zvykneš. `random.shuffle` a přepočítat index.

**Odhad rozsahu.** 512 chunků × zhruba 2,5k vstupních a 1,5k výstupních tokenů
je řádově 1,3M vstupních a 0,8M výstupních tokenů za celý průchod, jednotky
dolarů. Počítadlo utracených tokenů si dej do UI hned, ne až později; při ladění
promptu pustíš průchod víckrát, než čekáš.

**Napiš si k `validate.py` test hned.** Ulož si 3–4 reálné odpovědi modelu jako
fixtures (včetně jedné vadné) a testuj proti nim. Validace je jediné místo, které
rozhoduje, co se dostane do banky, a zároveň to budeš při ladění promptu
nejčastěji měnit.

**Hotovo, když:** na 5 chuncích projde validací >85 % otázek a ručně přečtené
otázky dávají smysl. **Teprve teď dolaď prompt a pusť zbytek**: přepisovat prompt
po 600 otázkách je zbytečná ztráta.

---

### Fáze 4: Worker a view pro generování

**Vlákna počítají, zapisuje jedno místo.** Tohle je nejdůležitější rozhodnutí
celé fáze. Spojení `sqlite3` nejde sdílet mezi vlákny a při 3–5 souběžných
zapisovatelích dostaneš `database is locked`. Worker proto dělá jen HTTP volání
a validaci, výsledek pošle signálem a **zápis proběhne v jednom vlákně**:

```python
class ChunkJob(QRunnable):
    def __init__(self, chunk, sink):
        super().__init__()
        self.chunk, self.sink = chunk, sink

    def run(self):                       # běží v QThreadPool
        try:
            qs = generate_for_chunk(self.chunk)     # API + validace, žádná DB
            self.sink.done.emit(self.chunk.id, qs, None)
        except Exception as e:
            self.sink.done.emit(self.chunk.id, [], str(e))


class GeneratorSink(QObject):
    done     = Signal(int, list, object)   # chunk_id, otázky, chyba
    progress = Signal(int, int)

    @Slot(int, list, object)
    def on_done(self, chunk_id, qs, err):  # běží v GUI vlákně = jediný writer
        if err:
            repo.set_chunk_status(chunk_id, "failed", err)
        elif not qs:
            repo.set_chunk_status(chunk_id, "skipped")
        else:
            repo.insert_questions(chunk_id, qs)
            repo.set_chunk_status(chunk_id, "done")
        self._n += 1
        self.progress.emit(self._n, self._total)
```

Kdyby ti zápis v GUI vlákně přišel jako brzda (nebude, je to pár set INSERTů),
alternativa je `queue.Queue` a jedno dedikované writer vlákno. Nikdy ale ne
zápis z každého workeru.

Na spojení nastav hned při otevření:
```python
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA busy_timeout=5000")
conn.execute("PRAGMA foreign_keys=ON")   -- SQLite je má defaultně VYPNUTÉ
```

Zbytek fáze:
- **nikdy nesahat na widgety z workeru**: jen signály
- paralelizace: 3–5 chunků souběžně přes `QThreadPool` + `QRunnable`
  (500 chunků sériově ≈ 20 min, paralelně ≈ 5 min)
- retry s exponenciálním backoffem **a jitterem** na HTTP 429, 500 a 529;
  strop 5 pokusů, pak `failed`. Bez jitteru ti všech pět vláken retryuje naráz
- storno se kontroluje mezi chunky, rozjeté volání se nepřerušuje
- **chunk bez otázek není chyba**: 0 otázek u obsahu nebo titulní strany
  ulož jako `skipped`, ne `failed`, ať ti to nekazí statistiku
- API klíč přes `keyring.get_password("studyapp", "anthropic")`, **ne** v QSettings

GUI: `QFileDialog.getOpenFileName(filter="PDF (*.pdf)")` + drag & drop
(`dragEnterEvent` / `dropEvent`), `QProgressBar`, `QTableView` nad `chunks`
se sloupci ordinal / stránky / status / počet otázek, tlačítka Start / Storno /
Přegenerovat vybrané.

**Hotovo, když:** appka během generování nezamrzá a po restartu naváže tam, kde skončila.

---

### Fáze 5: Předpočet souřadnic zvýraznění

Při ukládání otázky rovnou spočítat rects a uložit do `rects_json`:

```python
def locate(doc, chunk, quote):
    for pno in range(chunk.page_start, chunk.page_end + 1):
        page = doc[pno]
        rects = page.search_for(quote[:80]) \
             or page.search_for(" ".join(quote.split()[:6])) \
             or match_over_words(page.get_text("words"), quote)
        if rects:
            return pno, rects
    return None, []
```

Jeden průchod tedy vyřeší obojí: **číslo stránky i souřadnice**. Proto se `page`
nepřebírá od modelu, vypadne z tohohle hledání jako vedlejší produkt.

`search_for` selhává na citacích lámaných přes řádek nebo s dělením slov, proto
fallback přes `words`. Za běhu se pak jen čte z DB, žádné hledání.

**Bonus:** otázka, u které se nepodařilo najít rects, je podezřelá i obsahově.
Označ ji a projdi ručně.

**Hotovo, když:** >90 % otázek má neprázdné `rects_json`.

---

### Fáze 6: Deduplikace

200 stran vygeneruje řádově 600–1000 otázek a spousta z nich se překrývá.

- model: `BAAI/bge-m3` nebo `intfloat/multilingual-e5-base` (oba umí česky, běží na CPU)
- embedduj text otázky, ulož jako BLOB
- brute-force cosine přes numpy matici: u tisíce otázek jednotky milisekund,
  žádná vektorová DB není potřeba
- similarity > 0.9 → ponech tu s delší `source_quote`, druhé nastav `is_active = 0`

**Drž to mimo aplikaci.** `sentence-transformers` táhne PyTorch, což je ~2 GB.
Deduplikace je jednorázový krok nad hotovou bankou, takže z ní udělej samostatný
CLI skript (`python -m studyapp.embed.dedupe`) ve vlastním virtualenvu. Do appky
ani do PyInstaller buildu torch nikdy nedávej, jinak ti z 80MB exe bude 2GB.

**Hotovo, když:** ručně zkontrolovaných 10 zneaktivněných dvojic jsou skutečně duplicity.

---

### Fáze 7: Kvízové view (layout B + trvalý náhled z A)

**Startovní dialog testu** (před zahájením session):
- typ otázek: Multiple choice / Open / Both
- zdroj: All questions / Mistakes only / vybrané kapitoly
- počet otázek v session (např. 20), pořadí náhodné

`QSplitter(Qt.Horizontal)`, poměr ~46/54, uložený do `QSettings`:
- **vlevo** `quiz_view`: jedna vycentrovaná karta, minimum ovládacích prvků.
  Znění otázky patkovým písmem, ~34 znaků na řádek. Žádné menu ani taby uvnitř session.
- **vpravo** `pdf_view`: **viditelný pořád**, ale do odpovědi rozmazaný.

**Multiple choice:** varianty jako klikací řádky (1–4) → *Submit* → obarvit
správnou a špatnou, zapsat `attempts`, zaostřit náhled, odemknout *Next*.

**Open (self-graded):** jen otázka, žádné vstupní pole. *Reveal answer* → zaostření
náhledu, skok na stránku, zvýraznění `source_quote`, zobrazení `correct_text`
a `explanation`. **Teprve teď** se objeví *I knew it* / *I didn't know* → zápis
do `attempts` s `self_graded = 1`.

**Rozmazaný náhled: jak na to.** Nepoužívej `QGraphicsBlurEffect` přímo na
`QPdfView`; efekt nad scrollovaným view v Qt bliká a překresluje se draho. Místo
toho `QStackedWidget` se dvěma vrstvami a přepnutím při odhalení:

```python
def blurred_pixmap(doc, page_no, zoom=1.5, radius=6):
    png = doc[page_no].get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")
    img = Image.open(io.BytesIO(png)).filter(ImageFilter.GaussianBlur(radius))
    buf = io.BytesIO(); img.save(buf, "PNG")
    pm = QPixmap(); pm.loadFromData(buf.getvalue()); return pm
```

Vrstva 0 je `QLabel` s tímhle pixmapem, vrstva 1 je skutečný `QPdfView`.

**Lepší varianta: předrenderuj rozmazané stránky už při importu** do `cache/blur/`
jako PNG. 200 stránek při zoomu 1.5 zabere pár MB a získáš tím víc než rychlost:
za běhu pak aplikace **vůbec nepotřebuje PyMuPDF**. Jinak bys v jednom procesu
držel dva PDF engine (PyMuPDF na rozmazání, QtPdf na zobrazení), což je zbytečná
paměť i o jednu velkou závislost v balíčku víc.

**Pozor na skok při zaostření.** Rozmazaný pixmap má fixní zoom, `QPdfView` má
svůj vlastní. Kdyby obě vrstvy ukazovaly totéž místo, přepnutí by viditelně
poskočilo. Řeš to tím, že rozmazaná vrstva ukazuje **jinou stránku než tu cílovou**
(předchozí pozici) a zaostření je vždy spojené se skokem, skok ten rozdíl
schová a navíc je to přesně to chování, které nesmí prozradit odpověď.

**Co v rozmazaném stavu nesmí být vidět:** číslo stránky, název kapitoly ani
skok na cílovou stránku. Rozmazání skryje text, ale „p. 137, 8.3.1 Clusterovaný
index" v liště ti odpověď prozradí samo. Nech náhled na pozici, kde byl,
a metadata odkryj až spolu s ostřením.

**Odhalení:**
```python
nav = self.pdf_view.pageNavigator()
nav.jump(question.page, QPointF(0, 0))
self.search_model.setSearchString(question.source_quote[:80])
self.stack.setCurrentIndex(1)
```
`QPdfSearchModel` napojený na `QPdfView` výskyty podbarví sám. Když nenajde nic,
použij uložené `rects_json` jako fallback.

**Klávesnice** (u stovek otázek je myš brzda): `1`–`4` volba, `Enter` submit,
`Space` reveal, `Y`/`N` self-grade, `→` další, `S` hvězdička. Zobraz je jako
tiché nápovědy pod tlačítky, ne jako samostatnou legendu.

Zkratky registruj jako `QShortcut` na okně, ne přes `keyPressEvent` widgetu:
jakmile má fokus tlačítko varianty, `Space` by ho zmáčkl místo odhalení zdroje.
Tlačítkům variant dej `setFocusPolicy(Qt.NoFocus)`, ať se ti fokus po obrazovce
nepotuluje.

**Hotovo, když:** náhled je pořád na obrazovce, do odpovědi z něj nic nepřečteš,
a po odhalení skočí na správnou stránku se zvýrazněnou větou.

---

### Fáze 7b: Vizuální styl (varianta „Skriptorium v kartách")

| Role | Hodnota |
|---|---|
| Plátno okna | `#e7e5df` |
| Karta (otázka, náhled) | `#fbfaf7`, rámeček `#dcd9d2`, radius 14 px |
| Stránka PDF uvnitř karty | `#ffffff`, rámeček `#e6e3dc`, radius 10 px |
| Varianta odpovědi | `#ffffff`, rámeček `#e2dfd8`, radius 10 px |
| Inkoust / sekundární / terciární | `#17181a` / `#78766f` / `#a8a59d` |
| Správně | rámeček `#4a7a55`, výplň `#eef3ec` |
| Špatně | rámeček `#a86b6b`, výplň `#f7eeee`, text `#7b4141` |
| Zvýraznění v PDF | `#ffeaa0` |
| Písmo otázky a stránky | Iowan Old Style / Georgia, 21 px / 13 px |
| Písmo ovládání a metadat | systémový sans / mono, 14 px / 12 px |

Kostra QSS:

```css
QWidget#canvas   { background: #e7e5df; }
QFrame#card      { background: #fbfaf7; border: 1px solid #dcd9d2; border-radius: 14px; }
QLabel#question  { font-family: "Iowan Old Style", Georgia, serif;
                   font-size: 21px; color: #17181a; }
QPushButton#option {
    background: #ffffff; border: 1px solid #e2dfd8; border-radius: 10px;
    padding: 12px 15px; text-align: left; font-size: 15px; color: #17181a;
}
QPushButton#option:hover                 { border-color: #c9c5bc; }
QPushButton#option[state="selected"]     { border: 2px solid #17181a; padding: 11px 14px; }
QPushButton#option[state="correct"]      { border-color: #4a7a55; background: #eef3ec; }
QPushButton#option[state="wrong"]        { border-color: #a86b6b; background: #f7eeee;
                                           color: #7b4141; }
```

**Tři Qt pasti u tohohle stylu:**

1. **Dynamické property se samy nepřekreslí.** Po `btn.setProperty("state", "correct")`
   musíš vynutit přepočet:
   ```python
   btn.style().unpolish(btn); btn.style().polish(btn)
   ```
   Bez toho zůstane tlačítko vizuálně nezměněné a budeš to hledat hodinu.
2. **Zvětšení rámečku na 2 px posune obsah.** Proto má stav `selected` o 1 px menší
   padding, jinak text při výběru poskočí.
3. **Iowan Old Style existuje jen na macOS.** Dej fallback (`Georgia`, `DejaVu Serif`),
   nebo si patkové písmo zabal přes `QFontDatabase.addApplicationFont()` a měj stejný
   vzhled všude.

Sázej otázku na ~33 znaků na řádek přes `QLabel.setMaximumWidth()`, ne přes ruční
zalamování, jinak se ti to rozbije při změně velikosti okna.

**Hlavička session: proužek teček.** Jedna tečka za každou otázku v session:
zodpovězené `#8fae94` / `#c79b9b`, aktuální `#17181a`, budoucí `#d3d0c8`.
Vpravo textem `7 / 20 · mistakes only`.

- Nekresli 20 samostatných widgetů. Jeden `QWidget` s `paintEvent` a `drawRoundedRect`
  ve smyčce je jednodušší, rychlejší a snadno se překresluje jedním `update()`.
- **Tečky nesmí být klikací.** Skok na jinou otázku obchází pravidlo, že *Reveal*
  je jediná cesta dál. Nanejvýš tooltip s číslem otázky.
- Aktuální otázka je neutrální inkoustová, dokud neodpovíš. Nikdy nepředbarvuj.
- Nad ~30 otázek se proužek při šířce karty nevejde. Nezmenšuj tečky pod 8 px,
  místo toho přepni hlavičku na vlasovou linku s procentem.

---

### Fáze 8: Fronta chyb a statistiky

Špatné odpovědi **neukládej do zvláštní tabulky**: jsou odvoditelné z `attempts`
a duplicitní stav by se rozešel. Stačí dotaz nad posledním pokusem u každé otázky:

```sql
SELECT q.* FROM questions q
JOIN attempts a ON a.question_id = q.id
WHERE q.is_active = 1
  AND a.id = (SELECT MAX(id) FROM attempts WHERE question_id = q.id)
  AND a.is_correct = 0
ORDER BY RANDOM();
```

Otázka tedy z fronty chyb vypadne automaticky, jakmile ji zodpovíš správně.
Pokud chceš přísnější kritérium („dvakrát správně po sobě"), uprav dotaz:
je to jeden `LIMIT 2` navíc a je to už skoro Leitner box, tedy přirozený
odrazový můstek k SM-2 ve fázi 9.

Přehled: celková úspěšnost, úspěšnost po kapitolách (join přes `chunks.page_start`),
počet otázek aktuálně ve frontě chyb, historie session.

Sloupec `questions.starred` je v schématu od začátku, občas budeš chtít označit
otázku k opakování, i když jsi ji trefil.

Dva důsledky, které stojí za vědomé přijetí:
- **Deaktivovaná otázka z fronty chyb tiše zmizí** (`is_active = 1` v dotazu).
  Je to správně, nechceš procvičovat duplicitu, ale nediv se, když počet
  ve frontě po deduplikaci klesne.
- **Session se neobnovuje.** Když appku zavřeš uprostřed, `sessions.ended_at`
  zůstane NULL a příště začínáš novou. Nic se neztratí, protože `attempts`
  se zapisuje po každé otázce, ne až na konci. Neřeš resume, není to potřeba.

---

### Fáze 8.5: Správa banky otázek (Settings → Question bank)

V Qt je tohle levné, ne komplikované: `QTableView` nad `QSqlTableModel` dá
editovatelnou mřížku prakticky zadarmo:

```python
model = QSqlTableModel(db=qt_db)
model.setTable("questions")
model.setEditStrategy(QSqlTableModel.OnManualSubmit)
model.setFilter("is_active = 1")
model.select()

view = QTableView()
view.setModel(model)
view.hideColumn(model.fieldIndex("embedding"))   # BLOB do mřížky nepatří
# ... tlačítko Save -> model.submitAll(), Revert -> model.revertAll()
```

`source_quote`, `page` a `rects_json` drž **read-only**: jsou to odvozená data
navázaná na text PDF, ne obsah k ladění. Stačí přepsat `flags()`:

```python
READONLY = {"source_quote", "quote_score", "page", "rects_json",
            "chunk_id", "embedding", "created_at"}

class QuestionModel(QSqlTableModel):
    def flags(self, index):
        f = super().flags(index)
        if self.record().fieldName(index.column()) in READONLY:
            f &= ~Qt.ItemIsEditable
        return f
```

Funkce: fulltextové hledání (`setFilter` s `LIKE`), filtr podle typu / kapitoly /
stavu, in-place editace znění, přepínání `is_active` a `starred`, hromadné akce
nad výběrem.

**Varianty needituj v mřížce.** `options_json` je JSON pole a `correct_index` do něj
ukazuje; ruční editace surového JSONu v buňce je cesta k rozbité otázce. Dej na to
malý dialog se čtyřmi poli a radiobuttonem pro správnou variantu, který si JSON
i index přepočítá sám.

**Každá uložená změna nastav `edited_at`.** Na tom stojí ochrana před přegenerováním
chunku (viz tabulka úskalí), bez toho ti regenerace ruční opravy tiše přepíše.

**Tři věci, které musíš ošetřit:**

1. **Vadná citace se neopravuje ručně.** `source_quote` je read-only, takže
   problém s rozejitím rects nenastane. Když je citace špatně, je špatně celá
   otázka: buď ji deaktivuj, nebo přegeneruj celý chunk (`status = pending`,
   fáze 4 ho dobere). Ruční přepis citace by ti jen zamaskoval, že model
   v tom úseku halucinoval.
2. **Nemazat, deaktivovat.** Na `questions` visí `attempts`; smazáním přijdeš
   o historii nebo si rozbiješ FK. `DELETE` nabídni jen jako „purge inactive"
   s potvrzením.
3. **Dvě spojení do jednoho SQLite souboru.** `QSqlTableModel` potřebuje QtSql
   (`QSQLITE` driver), což je jiné spojení než tvůj `sqlite3` v `repo.py`.
   Zapni WAL (`PRAGMA journal_mode=WAL`) a po `submitAll()` si v aplikaci
   znovu načti data. Pokud se ti dvě přístupové cesty do DB nelíbí, napiš místo
   toho vlastní `QAbstractTableModel` nad `repo.py`: je to asi o 60 řádků víc,
   ale zůstaneš u jednoho spojení.

Užitečný přednastavený filtr: **„needs review"** = `rects_json IS NULL
OR page IS NULL OR quote_score < 0.95`. Jsou podezřelé i obsahově a projít si
je ručně se vyplatí víc než procházet celou banku.

**Hotovo, když:** otázku najdeš, upravíš, uložíš a změna se projeví v kvízu.

---

### Fáze 9: Volitelné doplňky (až po tom, co appka reálně funguje)

- **Hodnocení otevřených odpovědí přes LLM**: díky self-gradingu ho **nepotřebuješ**.
  Dává smysl jen tehdy, kdyby sis chtěl odpovědi psát a nechat je oznámkovat.
  Pak: referenční pasáž + tvoje odpověď → skóre 0–100 a co chybělo (tool schema).
  Jediné použití API za běhu, jinak appka funguje kompletně offline.
- **Spaced repetition (SM-2)** nad tabulkou `srs`. U 200 stran je opakování
  slabých témat důležitější než samotný kvíz, ale nejdřív ať funguje základ.
- **Chat nad učivem**: tady se hodí ty embeddingy podruhé: dotaz → top-k chunků
  → odpověď s odkazem na stránku. Přidej BM25 (`rank_bm25`) vedle dense retrievalu;
  u odborné terminologie a zkratek trefuje často líp a kombinace obou je znatelně
  lepší než samotné embeddingy.

---

### Fáze 10: Balení
PyInstaller do jednoho spustitelného souboru. SQLite je jen soubor vedle,
takže distribuce i záloha jsou triviální.

- **`--exclude-module torch`** a všechno kolem `sentence-transformers`. Deduplikace
  je oddělený CLI krok (fáze 6), do runtime nepatří.
- Když jsi předrenderoval rozmazané stránky (fáze 7), vyhoď i `pymupdf`:
  runtime pak potřebuje jen PySide6 a stdlib.
- `anthropic` v runtime potřebuješ jen tehdy, když sis nechal LLM hodnocení
  otevřených odpovědí z fáze 9. Bez něj appka nesahá na síť vůbec.
- Zabalené patkové písmo (fáze 7b) přidej přes `--add-data`.

---

## 5. Úskalí, na která narazíš

| Problém | Řešení |
|---|---|
| PDF je sken bez textové vrstvy | zjisti hned na začátku (`page.get_text()` vrací prázdno) → potřebuješ OCR, jinak celý plán padá |
| Model si citaci „opraví" (mezery, diakritika) | fuzzy match s prahem 0.9, ne přesná shoda |
| Citace lámaná přes řádek nebo dělením slov | fallback přes `get_text("words")` a posuvné okno |
| Model přeloží `source_quote` do jazyka otázky | v promptu explicitně oddělit obě pole; validace to odhalí jako 100% failure rate na chunk |
| Přegenerování chunku smaže ruční úpravy | `questions.edited_at`: editované otázky při regeneraci nechat být, nové jen doplnit |
| Po přegenerování se vrátí duplicity | pusť deduplikaci znovu proti celé aktivní bance, ne jen v rámci chunku |
| Reimport PDF s jiným `sha256` | všechny `rects_json` jsou neplatné: přepočítej, nebo vynuluj a dopočítej líně |
| `QGraphicsBlurEffect` nad `QPdfView` | bliká a překresluje se draho: použij `QStackedWidget` s předrenderovaným rozmazaným pixmapem |
| Metadata prozradí odpověď dřív než text | číslo stránky a název kapitoly skryj spolu s rozmazáním, ne až s textem |
| Fixní počet otázek na chunk | dej modelu rozsah 2–8, ať neplní balastem prázdné stránky |
| Zamrzlé GUI | veškerá I/O a API práce ve workeru, komunikace jen signály |
| `database is locked` při paralelním generování | workery nesmí zapisovat: jen počítají a posílají signál, zapisuje jedno vlákno |
| Cizí klíče v SQLite nefungují | `PRAGMA foreign_keys=ON` na každém spojení, defaultně je to vypnuté |
| Reimport upraveného PDF | `documents.sha256` je UNIQUE → vznikne nový dokument a starý zůstane viset. Rozhodni se: buď starý označit jako nahrazený a nechat historii, nebo import odmítnout a nabídnout smazání |
| PyInstaller build má 2 GB | torch ze `sentence-transformers`; deduplikace patří do odděleného CLI, ne do appky |
| Retry bez jitteru | při 429 ti všech 5 vláken retryuje ve stejný okamžik: přidej náhodný rozptyl |
| Alembic bez SQLAlchemy modelů | `--autogenerate` nebude fungovat, migrace piš ručně. Je to pár řádků, ale počítej s tím |
| Časové značky v různých zónách | ukládej v UTC (`datetime.now(timezone.utc).isoformat()`), lokalizuj až při zobrazení |
| Datum v SQLite | ukládej ISO 8601 string nebo unix timestamp, SQLite nemá datetime typ |
| Změna schématu později | Alembic od začátku, `ALTER TABLE` v SQLite umí málo |
| Přegenerování po úpravě promptu | `chunks.sha256` + status → přegeneruj jen vybrané, ne celý dokument |

---

## 6. Doporučené pořadí na první víkend

1. Fáze 1 + 2: extrakce, chunking, DB (pár hodin)
2. Fáze 3 pilot na 5 chuncích: tady strávíš nejvíc času laděním promptu
3. Fáze 4: worker a spuštění celého generování na pozadí
4. Fáze 7: split view, protože to je ta část, kvůli které appku stavíš

Fáze 5 dělej rovnou s fází 3: dopočet stránky i souřadnic je jeden průchod.
Fáze 6, 8+ přidávej postupně, appka je použitelná i bez nich.

---

## 7. Co změnila revize

Nálezy seřazené podle toho, jak moc by bolely, kdyby se na ně přišlo až za běhu.

**1 · `page` od modelu bylo smyšlené číslo.** Model v textu chunku žádná čísla
stránek nevidí, musel by je vymyslet. Původní plán ho o `page` přesto žádal
a validace to pak jen kontrolovala. Teď se `page` nepožaduje vůbec a dopočítává
se z toho, kde se citace v rozsahu chunku najde. Ubyde jedno pole v tool schema
a hlavně tichá třída chyb, kde by skok v náhledu končil na špatné stránce.

**2 · Paralelní workery zapisovaly do SQLite.** Původní `GeneratorWorker` volal
`repo.insert_questions()` přímo ve vlákně. Při 3–5 souběžných to skončí na
`database is locked`, a to nedeterministicky, tedy nejhorší možný způsob.
Workery teď jen počítají a posílají výsledek signálem, zapisuje jedno vlákno.

**3 · Schéma se rozešlo s textem na čtyřech místech.** `starred` a `edited_at`
se v textu používaly, ale v `CREATE TABLE` nebyly. `correct` byl text, přestože
fáze 3 říká „ukládej index". Filtr „needs review" se odvolával na skóre shody
citace, které se nikam neukládalo. Doplněno: `correct_index`, `correct_text`,
`quote_score`, `starred`, `edited_at`, `page` nullable, status `skipped`.

**4 · PyInstaller build by měl 2 GB.** `sentence-transformers` táhne PyTorch.
Deduplikace je jednorázový krok nad hotovou bankou, takže patří do odděleného
CLI, ne do aplikace.

**5 · Dva PDF enginy v jednom procesu.** PyMuPDF na rozmazání, QtPdf na zobrazení.
Předrenderováním rozmazaných stránek při importu spadne runtime na PySide6
a stdlib.

**6 · `Space` by mačkal tlačítko, ne odhaloval zdroj.** Zkratky přes `QShortcut`
na okně a `Qt.NoFocus` na tlačítkách variant.

**7 · Drobnosti:** `PRAGMA foreign_keys=ON` (v SQLite je defaultně vypnutý),
jitter u retry, UTC u časových značek, editace variant dialogem místo surového
JSONu v buňce, Alembic bez modelů neumí autogenerate, chunk bez otázek je
`skipped` a ne `failed`, index na `attempts(question_id, id)` kvůli dotazu
na frontu chyb.

**Co revize naopak potvrdila:** rozdělení na jednorázové generování a offline běh,
`source_quote` jako read-only kotva, self-grading místo LLM hodnocení, odvozování
fronty chyb z `attempts` místo vlastní tabulky, SQLite místo Postgresu. Tyhle čtyři
věci drží zbytek pohromadě a neměnil bych na nich nic.
