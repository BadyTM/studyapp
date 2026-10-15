# StudyApp

Osobní desktopová učící aplikace v Pythonu. Z jednoho PDF s učivem se
jednorázově vygeneruje banka otázek přes Claude API; aplikace pak jede
offline jako kvízový trenažér s náhledem zdroje v PDF.

Podrobný plán je v `docs/plan-ucici-aplikace.md` — **přečti si vždy jen sekci k fázi, na které
zrovna děláme**, ne celý soubor. Wireframy a designové tokeny v `docs/design-varianty.html`.

## Stack

Python 3.11+ · PySide6 (Qt 6) · QtPdf pro náhled · PyMuPDF pro extrakci
a předrender · SQLite přes `sqlite3` ze stdlib · Alembic na migrace ·
`anthropic` SDK jen v generovacím kroku.

UI je **anglicky**, obsah otázek v jazyce zdrojového PDF. Všechny UI stringy
obaluj do `self.tr()`.

## Neporušitelná pravidla

Tohle jsou rozhodnutí, na kterých stojí zbytek. Když se ti zdá, že je potřeba
některé porušit, řekni to a zeptej se — neobcházej je potichu.

1. **`source_quote` je doslovný úsek zdrojového textu.** Nikdy se nepřekládá,
   needituje ani neupravuje. Stojí na něm validace i zvýrazňování v PDF.
2. **`page` se nikdy nebere od modelu** — dopočítává se z toho, kde se citace
   v rozsahu chunku najde. Model čísla stránek nevidí a vymyslel by je.
3. **Z workerů se nezapisuje do SQLite.** Vlákna jen počítají a posílají výsledek
   signálem; zapisuje jedno vlákno. Jinak `database is locked`.
4. **Otázky se nemažou, jen `is_active = 0`.** Na `questions` visí `attempts`.
5. **Zdroj v náhledu se odkrývá až po odpovědi** — včetně čísla stránky a názvu
   kapitoly. Rozmazání skryje text, metadata odpověď prozradí sama.
6. **`Reveal` je jediná cesta dál.** Žádné přeskakování otázek, tečky v hlavičce
   nejsou klikací.
7. **PyTorch nikdy do aplikace.** `sentence-transformers` se používá výhradně
   v odděleném CLI kroku pro deduplikaci.
8. Stav generování se drží **per chunk** v DB, aby šlo kdykoli přerušit,
   navázat a přegenerovat jen část.
9. Ruční úpravy otázek chrání `edited_at` — regenerace chunku se jich nedotkne.
10. Časové značky v UTC, ISO 8601 string.

## Databáze

Schéma je v `docs/plan-ucici-aplikace.md`, sekce 3; závazná podoba je ale
vždy poslední Alembic migrace v `studyapp/db/migrations/versions/`. Zatím je
tam jedna (`0001`) a drží celé schéma — dokud appka běží jen tady, nemá smysl
řetězit revize; **jakmile v DB budou data, která nechceš ztratit, další změny
už přidávej jako novou migraci**, ne editací `0001`. Na každém spojení nastav:

```python
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA busy_timeout=5000")
conn.execute("PRAGMA foreign_keys=ON")   # v SQLite defaultně VYPNUTÉ
```

Změny schématu vždy přes Alembic. Migrace piš ručně — bez SQLAlchemy modelů
`--autogenerate` nefunguje.

## Příkazy

Spuštění bez terminálu: **dvojklik na `StudyApp.pyw`** v kořeni projektu.
Launcher si sám přepne na interpret z `.venv` (dvojklik jinak pustí systémový
pythonw.exe, kde PySide6 není) a případnou chybu ukáže v okně, ne do neexistující
konzole.

Ikona knihy na `StudyApp.pyw`: Windows u datových souborů žádnou ikonu per-file
nemá — bere ji z **typu** souboru. Je proto nastavená jednorázově v registru,
jen v HKCU (tenhle účet, nic systémového):

    HKCU\Software\Classes\Python.NoConFile\DefaultIcon
      = "<projekt>ssets\studyapp.ico",0

Dvě věci, se kterými je potřeba počítat:

- Ikonu dostanou **všechny** `.pyw` soubory tohoto účtu, ne jen tenhle.
- Registr míří na `assets\studyapp.ico` **absolutní cestou**. Když se projekt
  přesune nebo se ikona smaže, `.pyw` soubory zůstanou bez ikony — pak buď
  cestu v registru oprav, nebo ji vrať na původní hodnotu
  `"%LOCALAPPDATA%\Programs\Python\Launcher\py.exe",1`.

`assets/studyapp.ico` proto nemazat — visí na něm i ikona okna a taskbaru
(`config.icon_path()`).

**Qt se neinstaluje celé.** `requirements.txt` nežádá meta-balíček `PySide6`,
ale `PySide6-Essentials` + `PySide6-Pdf`. Appka importuje jen `QtCore`, `QtGui`,
`QtWidgets` (Essentials) a `QtPdf`, `QtPdfWidgets` (Pdf), a `PySide6-Pdf` závisí
pouze na Essentials — ne na Addons. Meta-balíček by přitáhl Addons a s ním
`pyside6_webengine`, celý Chromium o 125 MB, který se nikdy nenačte.

    PySide6 (vše)        295 MB ke stažení, 879 MB na disku
    Essentials + Pdf     131 MB ke stažení, 445 MB na disku

Ověřeno v čistém prostředí bez WebEnginu a Addons: 689 testů prochází, okno
nastartuje, PDF se najde a renderuje.

Dřív to tak nešlo a v tomhle souboru stál návod, jak z nainstalovaného PySide6
ručně vyřezat ~505 MB — `QtPdf` tehdy bydlel v `PySide6-Addons` spolu
s WebEnginem. **Od 6.12 je `PySide6_Pdf` samostatné kolo**, takže ruční ořez
není potřeba a nic ho nevrátí zpátky. Pokud ti v `.venv` zbyl ořezaný PySide6
z dřívějška, nech ho být, nebo prostředí postav znovu.

**Virtualenv patří mimo OneDrive.** I bez WebEnginu má 445 MB a v `.venv` uvnitř
projektu to OneDrive donekonečna synchronizuje. Launcher hledá interpret
postupně v `STUDYAPP_VENV`, pak v `.venv` u projektu, pak
v `%LOCALAPPDATA%\StudyApp\venv`. Přesun:

```powershell
python -m venv $env:LOCALAPPDATA\StudyApp\venv
& $env:LOCALAPPDATA\StudyApp\venv\Scripts\python.exe -m pip install -r requirements.txt
Remove-Item -Recurse -Force .venv
```

Windows, PowerShell:

```powershell
.venv\Scripts\Activate.ps1
python -m studyapp                       # spuštění aplikace
python -m studyapp.ingest <pdf>          # extrakce, chunking, uložení do DB
python -m studyapp.ingest <pdf> --dry-run # jen vypíše chunky, nic nezapíše
python -m studyapp.generate --limit 5 --dry-run  # pilot generování, nic nezapíše
pytest tests/                            # testy, hlavně validate.py
```

Poslat banku otázek někomu dalšímu: **Export… / Import…** na záložce
*Question bank*, formát je JSON (`db/transfer.py`). Co se exportuje, řídí
výběr dokumentu nad tabulkou — jedna kniha, nebo všechny.

Databáze se **nikdy nekopíruje souborem**. Dva důvody, první technický: v režimu
WAL leží poslední zápisy v `studyapp.db-wal` a samotný `.db` je nekompletní.
Druhý je horší — soubor s databází neumí sloučit. Příjemce by musel svou banku
buď zahodit, nebo tu tvoji nepřijmout. Dřív to tak bylo (`tools/export_bank.py`,
zip s celou appkou) a kvůli tomu to zmizelo.

Import **páruje podle obsahu, nikdy podle id**: dokument podle sha256 PDF, chunk
podle sha256 textu, otázka podle znění v rámci chunku. Nic nemaže, nic
nepřepisuje, takže naimportovat stejný soubor dvakrát je bez efektu.

Co **neputuje**: samotné PDF (stránka a obdélník nedávají smysl bez souboru, ze
kterého pocházejí — export nese jeho sha256 a import řekne, komu PDF chybí),
`attempts` a `sessions` (progres je osobní, banka ne) a embeddingy (cache, kterou
`embed/dedupe.py` spočítá znovu). Blur cache se taky nepřikládá — příjemci ji po
*Locate PDF* dorenderuje `generate_view.ensure_blur()` na signál
`BankView.document_relocated`.

Deduplikace jede v **odděleném virtualenvu** — `sentence-transformers` táhne
PyTorch (~2 GB), který se nikdy nesmí dostat do appky ani do buildu:

```powershell
python -m venv .venv-embed
.venv-embed\Scripts\python.exe -m pip install sentence-transformers
.venv-embed\Scripts\python.exe -m studyapp.embed.dedupe --dry-run
.venv-embed\Scripts\python.exe -m studyapp.embed.dedupe
```

`--skip-encode` počítá jen nad už uloženými vektory a model vůbec nenačítá,
takže projde i v hlavním venvu. Hlídá to test `TestNoTorchAnywhere`.

DB a cache leží v `%LOCALAPPDATA%\StudyApp`, ne v projektu — OneDrive synchronizující
SQLite ve WAL režimu je spolehlivá cesta k rozbitému souboru. Přepíše se
proměnnou `STUDYAPP_DB`.

## Konvence

- Žádná business logika v `ui/` — jen widgety a signály.
- Přístup do DB výhradně přes `db/repo.py`, ne přímé SQL po kódu.
- Když měníš `validate.py`, přidej fixture do `tests/fixtures/`.
