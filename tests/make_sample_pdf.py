"""Builds a textbook-shaped PDF to test extraction and chunking against.

A real textbook is not available to the test suite, and the properties that
matter here are structural: numbered headings in a larger face, running text
below them, and a table of contents at the front that should chunk differently
from everything after it.

    python tests/make_sample_pdf.py [out.pdf] [--pages 40]
"""

import argparse
from pathlib import Path

import pymupdf

BODY = (
    "Transakce je posloupnost operaci nad databazi, ktera se z pohledu okoli chova "
    "jako jedina nedelitelna jednotka prace. Souhrnne se pozadovane vlastnosti "
    "oznacuji zkratkou ACID a v nasledujicich odstavcich rozebereme kazdou z nich "
    "zvlast. Atomicita znamena, ze dilci operace transakce nelze pozorovat "
    "samostatne, a to ani v pripade, kdy system behem jejich provadeni selze. "
)

CHAPTERS = [
    "1 Uvod do databazovych systemu",
    "2 Relacni model",
    "3 Dotazovaci jazyk SQL",
    "4 Normalni formy",
    "5 Fyzicke ulozeni dat",
    "6 Transakce",
    "7 Zotaveni po havarii",
    "8 Indexy",
]


def build(path: Path, pages: int = 40) -> Path:
    document = pymupdf.open()
    every = max(2, pages // len(CHAPTERS))

    for number in range(pages):
        page = document.new_page()
        top = 72.0

        if number % every == 0:
            chapter = CHAPTERS[min(number // every, len(CHAPTERS) - 1)]
            page.insert_text((72, top), chapter, fontsize=18, fontname="hebo")
            top += 34

        for paragraph in range(4):
            text = BODY * 2
            box = pymupdf.Rect(72, top, 523, top + 150)
            page.insert_textbox(box, text, fontsize=11, fontname="helv")
            top += 160

    document.save(path)
    document.close()
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("out", nargs="?", type=Path,
                        default=Path(__file__).parent / "fixtures" / "sample.pdf")
    parser.add_argument("--pages", type=int, default=40)
    arguments = parser.parse_args()
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    print(build(arguments.out, arguments.pages))
