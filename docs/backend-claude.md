# Připojení na Claude — dvě cesty, jedno rozhraní

Zjištěno z `the-narrator`, kde už obojí běží. Platí pro fázi 3 a 4.

## Vzor z the-narrator

Dva moduly se stejným rozhraním, okno mezi nimi nepozná rozdíl:

| Modul | Platí se | Jak |
|---|---|---|
| `engine_api.py` | API kredit | `anthropic.Anthropic()`, klíč z `ANTHROPIC_API_KEY` |
| `engine_claude_code.py` | předplatné | podproces `claude -p`, komunikace v JSON lines |

Výběr: `BACKEND` v `("auto", "claude-code")` → když `find_claude()` najde
spustitelný soubor, jede předplatné; jinak fallback na API klíč.

```python
def find_claude():
    for name in ("claude.cmd", "claude.exe", "claude"):
        if found := shutil.which(name):
            return found
    npm_default = os.path.expandvars(r"%APPDATA%\npm\claude.cmd")
    return npm_default if os.path.exists(npm_default) else None
```

Podproces se pouští v **prázdné stabilní složce** (`%LOCALAPPDATA%\studyapp\claude`),
ne v projektu — jinak si Claude Code načte kontext repozitáře a začne se chovat
jako programátorský asistent. A `--disallowedTools` na všechno, co by se ptalo
na oprávnění.

## Kde se StudyApp od the-narrator liší

**1 · Tool schema na cestě přes předplatné neexistuje.** CLI neumí zaregistrovat
vlastní nástroj s `tool_choice`. The-narrator to obešel textovou značkou
`[HOD: 1d20+3]`, kterou program vychytá ze streamu. Pro `save_questions` to
znamená požádat o JSON blok v odpovědi a parsovat ho tolerantně.

Není to tak křehké, jak to zní: `validate.py` stejně každou otázku prověřuje
proti textu chunku a vadné zahazuje, takže špatně naparsovaná odpověď skončí
jako `skipped` nebo `failed` chunk, ne jako smetí v bance. **Na API cestě ale
zůstane skutečné tool schema** — plán ho vyžaduje a je spolehlivější.

Obě cesty tedy vracejí stejný `list[dict]` a `validate.py` je za nimi společné.

**2 · Text chunku musí jít přes stdin, ne přes argv.** Windows seká příkazovou
řádku na 8191 znacích; chunk má 1500–2500 slov, tedy klidně 15 000 znaků.
The-narrator kvůli tomu drží `MAX_COMMAND_LENGTH = 7500` a systémový prompt
schválně krátí. U nás: systémový prompt přes `--system-prompt` (vejde se),
text chunku vždy rourou.

**3 · Žádná konverzace.** Každý chunk je samostatné jednorázové volání, takže
odpadá dlouhoběžící proces i `build_recap()`. Jeden `claude -p` na chunk,
`--output-format json`, přečíst pole `result`.

**4 · Paralelismus je dražší.** Na API cestě je 4 souběžných volání triviální.
Na cestě přes předplatné je to 4 procesy Claude Code najednou — funguje, ale
startuje pomaleji a naráží na jiné limity. Zvaž `MAX_PARALLEL_CHUNKS` nižší,
když jede předplatné.

## Co si vzít i beze změny

- **Počítadlo tokenů od začátku.** The-narrator má `Costs` pro API (odhad v dolarech
  z cen za milion tokenů) a `SubscriptionCosts` pro předplatné (tahy a tokeny,
  dolary tam nedávají smysl). Plán fáze 3 to chce taky — při ladění promptu
  pustíš průchod víckrát, než čekáš.
- **`describe_error()`** — jedno místo, které z výjimky udělá jednu větu pro uživatele.
  `anthropic.APIStatusError` nese stavový kód, který stojí za zobrazení.
- **`anthropic` jako volitelný import.** `try: import anthropic / except ImportError`
  a `AVAILABLE = anthropic is not None`. Kdo generuje přes předplatné, balíček
  nepotřebuje — a hotová appka ho nepotřebuje vůbec.
