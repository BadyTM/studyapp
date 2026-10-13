"""The visual style, translated from the "Skriptorium v kartach" mock-up.

Colours live here as names rather than in the stylesheet string, because the
painted widgets -- the dot strip, the blurred page -- need the same values and a
hex code repeated in two places drifts apart within a week.
"""

import os
from functools import lru_cache

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPen, QPixmap

# --- palette ----------------------------------------------------------------

CANVAS = "#e7e5df"          # window ground: a darker shade of the same paper
CARD = "#fbfaf7"            # question and preview cards
CARD_BORDER = "#dcd9d2"
PAGE = "#ffffff"            # the PDF page inside the preview card
PAGE_BORDER = "#e6e3dc"
OPTION_BORDER = "#e2dfd8"
SELECTION = "#e4e1d9"       # a selected row: the canvas tone, not an accent
FILTER_GROUND = "#f2f0ea"   # the filter strip: a half-step off the card
HOVER = "#f1efe9"           # the row under the cursor, a step lighter again

INK = "#17181a"
INK_SECONDARY = "#78766f"
INK_TERTIARY = "#a8a59d"
INK_META = "#8a877f"

CORRECT_BORDER = "#4a7a55"
CORRECT_FILL = "#eef3ec"
CORRECT_TEXT = "#2f5c3b"
WRONG_BORDER = "#a86b6b"
WRONG_FILL = "#f7eeee"
WRONG_TEXT = "#7b4141"

HIGHLIGHT = "#ffeaa0"       # the marker over the quote in the PDF

# Muted so the progress strip cannot outshout the question above it.
DOT_CORRECT = "#8fae94"
DOT_WRONG = "#c79b9b"
DOT_CURRENT = INK
DOT_FUTURE = "#d3d0c8"

# --- type -------------------------------------------------------------------

# Iowan Old Style ships with macOS only. The fallbacks are what Windows and Linux
# actually have; without them Qt silently substitutes a sans and the whole design
# loses the one thing it is built on.
SERIF_STACK = ["Iowan Old Style", "Palatino Linotype", "Georgia", "Constantia", "DejaVu Serif"]

QUESTION_SIZE = 21
PAGE_TEXT_SIZE = 13
CONTROL_SIZE = 14
META_SIZE = 12

# The question is set to roughly this many characters per line. Enforced by
# capping the label width, never by inserting line breaks -- hand-wrapped text
# comes apart the moment the window is resized.
QUESTION_LINE_LENGTH = 33


def serif_family() -> str:
    """The first serif face on the stack that this machine actually has.

    The last resort is a name rather than a generic: paired with the Serif style
    hint below, Qt substitutes whatever serif the system does have, which is
    always better than falling through to the default sans.
    """
    available = set(QFontDatabase.families())
    for family in SERIF_STACK:
        if family in available:
            return family
    return SERIF_STACK[-2]


def question_font() -> QFont:
    font = QFont(serif_family(), QUESTION_SIZE)
    font.setStyleHint(QFont.Serif)          # governs what Qt substitutes
    font.setStyleStrategy(QFont.PreferAntialias)
    return font


def question_width(font: QFont | None = None) -> int:
    """Pixel width of `QUESTION_LINE_LENGTH` average characters in the question font."""
    from PySide6.QtGui import QFontMetrics

    metrics = QFontMetrics(font or question_font())
    return metrics.averageCharWidth() * QUESTION_LINE_LENGTH


def color(name: str) -> QColor:
    return QColor(name)


# --- drawn assets -----------------------------------------------------------

@lru_cache(maxsize=None)
def chevron(direction: str = "down", size: int = 9,
            colour: str = INK_SECONDARY) -> str:
    """A chevron for a combo box or a spin box, as a path a stylesheet can use.

    Styling the box at all makes Qt stop drawing its own arrow, and a stylesheet
    can only put one back as an image -- there is no way to describe a chevron in
    QSS, and a border turned on its corner comes out as an L. So it is drawn
    here, written next to the database, and referenced by path.

    Drawn rather than shipped so it follows the palette, and cached so a
    repainting combo box is not writing a file each time.
    """
    from .. import config

    path = config.data_dir() / "ui" / f"chevron-{direction}-{size}-{colour[1:]}.png"
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(colour))
        pen.setWidthF(1.4)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)

        inset, middle = 1.6, size / 2.0
        far = size - inset
        if direction == "down":
            points = [QPointF(inset, middle - 1.4), QPointF(middle, far - 1.4),
                      QPointF(far, middle - 1.4)]
        else:
            points = [QPointF(inset, middle + 1.4), QPointF(middle, inset + 1.4),
                      QPointF(far, middle + 1.4)]
        painter.drawPolyline(points)
        painter.end()
        # Written beside itself and renamed in, for the same reason the blurred
        # pages are: the file existing is the whole cache check, so a half-written
        # one would be a torn arrow on every combo box from then on.
        temporary = path.with_suffix(".png.part")
        pixmap.save(str(temporary), "PNG")
        os.replace(temporary, path)

    # Qt stylesheets want forward slashes, on every platform.
    return path.as_posix()


# --- stylesheet -------------------------------------------------------------

def stylesheet() -> str:
    """The application stylesheet.

    Option buttons carry their result in a dynamic `state` property. Qt does not
    repaint on a property change by itself -- whoever sets it must follow with
    `style().unpolish(widget); style().polish(widget)` or nothing visibly happens.
    """
    serif = serif_family()
    chevron_down = chevron("down")
    chevron_up = chevron("up")
    return f"""
QWidget {{
    background: {CANVAS};
    color: {INK};
    font-size: {CONTROL_SIZE}px;
}}

QWidget#canvas {{ background: {CANVAS}; }}

QFrame#card, QFrame#previewCard {{
    background: {CARD};
    border: 1px solid {CARD_BORDER};
    border-radius: 14px;
}}

QLabel#question {{
    font-family: "{serif}";
    font-size: {QUESTION_SIZE}px;
    color: {INK};
    background: transparent;
}}

QLabel#explanation {{
    font-family: "{serif}";
    font-size: 15px;
    color: #3c3e42;
    background: transparent;
    border-top: 1px solid {OPTION_BORDER};
    padding-top: 18px;
}}

QLabel#meta {{
    font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
    font-size: {META_SIZE}px;
    color: {INK_META};
    background: transparent;
}}

QLabel#hint {{
    font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
    font-size: 11px;
    color: {INK_TERTIARY};
    background: transparent;
}}

QFrame#option {{
    background: {PAGE};
    border: 1px solid {OPTION_BORDER};
    border-radius: 10px;
}}
QFrame#option:hover                  {{ border-color: #c9c5bc; }}
/* One pixel of border is taken back out of the margin, so that selecting an
   option does not nudge its text sideways. */
QFrame#option[state="selected"]      {{ border: 2px solid {INK}; margin: -1px; }}
QFrame#option[state="correct"]       {{ border-color: {CORRECT_BORDER};
                                        background: {CORRECT_FILL}; }}
QFrame#option[state="wrong"]         {{ border-color: {WRONG_BORDER};
                                        background: {WRONG_FILL}; }}

QLabel#optionText {{
    background: transparent;
    border: 0;
    font-size: 15px;
    color: {INK};
}}
QLabel#optionText[state="wrong"]     {{ color: {WRONG_TEXT}; }}
QLabel#optionText[state="dimmed"]    {{ color: {INK_TERTIARY}; }}

QLabel#optionNumber {{
    background: transparent;
    border: 0;
    font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
    font-size: 12px;
    color: {INK_TERTIARY};
}}

QPushButton#primary {{
    background: transparent;
    border: 1px solid {INK};
    border-radius: 9px;
    padding: 9px 22px;
    font-size: {CONTROL_SIZE}px;
    color: {INK};
}}
QPushButton#primary:hover   {{ background: #f2f0eb; }}
QPushButton#primary:disabled {{ border-color: #cdcac2; color: {INK_TERTIARY}; }}

QPushButton#ghost {{
    background: transparent;
    border: 1px solid #cdcac2;
    border-radius: 9px;
    padding: 9px 22px;
    font-size: {CONTROL_SIZE}px;
    color: {INK_SECONDARY};
}}
QPushButton#ghost:hover {{ border-color: {INK_SECONDARY}; }}

QPushButton#gradeYes {{
    background: transparent; border: 1px solid {CORRECT_BORDER};
    border-radius: 9px; padding: 13px; color: {CORRECT_TEXT}; font-size: {CONTROL_SIZE}px;
}}
QPushButton#gradeNo {{
    background: transparent; border: 1px solid {WRONG_BORDER};
    border-radius: 9px; padding: 13px; color: {WRONG_TEXT}; font-size: {CONTROL_SIZE}px;
}}

QTabWidget::pane {{ border: 0; }}
QTabBar::tab {{
    background: transparent;
    color: {INK_SECONDARY};
    padding: 9px 18px;
    border: 0;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {INK}; border-bottom: 2px solid {INK}; }}

QTableView {{
    background: {CARD};
    border: 1px solid {CARD_BORDER};
    border-radius: 10px;
    gridline-color: {OPTION_BORDER};
    /* Without this Qt draws a focus rectangle around the current cell, on top
       of the row highlight. */
    outline: 0;
}}
/* The row highlight has to be stated per item. Setting only
   selection-background-color on the view leaves the native style painting the
   cells, which on Windows means the system accent colour -- and it lands as
   slivers at the cell edges rather than a filled row. */
QTableView::item {{
    padding: 5px 8px;
    border: 0;
    color: {INK};
}}
QTableView::item:selected {{
    background: {SELECTION};
    color: {INK};
}}
/* No ::item:hover rule: it would light the one cell under the cursor and
   leave the rest of its row plain. Row-wide hover is painted by
   tables.RowHoverDelegate, which needs the whole row and so cannot be a
   stylesheet rule at all. */

QHeaderView {{ background: {CARD}; }}
QHeaderView::section {{
    background: {CARD};
    color: {INK_META};
    border: 0;
    border-bottom: 1px solid {OPTION_BORDER};
    padding: 7px;
}}
/* --- the filter strip under the column headings ----------------------------
   It has to read as a second row of the header rather than as the first row of
   data, which is what it looked like with no ground of its own: the same paper
   as the header, and the same hairline under it that the header has.

   The controls are shrunk here and nowhere else. At the 7px padding they carry
   on the rest of the app the strip stood taller than three rows of the table it
   was meant to be a heading for. */
/* A ground of its own. On the card's own paper the strip was invisible: the
   hairline under it measured one pixel of #e2dfd8 against #fbfaf7, which is
   there but cannot be seen, and the filters read as the first row of questions.
   A half-step darker separates the two without introducing a new colour.

   The underline is drawn by FilterHeader.paintEvent, not stated here: a border
   rule on a plain widget is accepted and never painted. */
#filterHeader {{
    background: {FILTER_GROUND};
    border: 0;
}}
#filterHeader QComboBox, #filterHeader QLineEdit {{
    padding: 3px 8px;
    border-radius: 6px;
}}
#filterHeader QComboBox::drop-down {{ width: 18px; }}
#filterHeader QComboBox::down-arrow {{ margin-right: 5px; }}
/* Centred under a column whose heading is an icon, not a word. */
#filterHeader QCheckBox {{ spacing: 0; }}

QTableCornerButton::section {{
    background: {CARD};
    border: 0;
    border-bottom: 1px solid {OPTION_BORDER};
}}

/* --- radio buttons and checkboxes ------------------------------------------
   Unstyled, Qt gives these the native indicator, which on this paper ground is
   a pale circle whose checked state is nearly invisible -- there was no telling
   which option was chosen. */
QRadioButton, QCheckBox {{
    background: transparent;
    color: {INK};
    spacing: 8px;
    padding: 2px 0;
}}
QRadioButton::indicator, QCheckBox::indicator {{
    width: 15px;
    height: 15px;
    background: {PAGE};
    border: 1px solid #c9c5bc;
}}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator:hover, QCheckBox::indicator:hover {{
    border-color: {INK_SECONDARY};
}}
/* Filled with ink rather than ringed: at this size a ring reads as an empty
   circle from half a metre away, which is the distance this is read from. */
QRadioButton::indicator:checked {{
    background: {INK};
    border: 1px solid {INK};
}}
QCheckBox::indicator:checked {{
    background: {INK};
    border: 1px solid {INK};
}}
QRadioButton:disabled, QCheckBox:disabled {{ color: {INK_TERTIARY}; }}

/* --- text and pickers -----------------------------------------------------
   Left alone these keep the native blue-grey gradient, which sits next to the
   paper cards looking like a control panel bolted onto a book. */
QComboBox, QSpinBox, QLineEdit {{
    background: {PAGE};
    border: 1px solid {OPTION_BORDER};
    border-radius: 8px;
    padding: 7px 10px;
    color: {INK};
    selection-background-color: {SELECTION};
    selection-color: {INK};
}}
QComboBox:hover, QSpinBox:hover, QLineEdit:hover {{ border-color: #c9c5bc; }}
QComboBox:focus, QSpinBox:focus, QLineEdit:focus {{ border-color: {INK_SECONDARY}; }}
QComboBox:disabled, QSpinBox:disabled, QLineEdit:disabled {{
    background: #f4f2ee; color: {INK_TERTIARY};
}}

QComboBox::drop-down {{ border: 0; width: 24px; }}
QComboBox::down-arrow {{ image: url({chevron_down}); width: 9px; height: 9px;
                         margin-right: 7px; }}
QComboBox QAbstractItemView {{
    background: {CARD};
    border: 1px solid {CARD_BORDER};
    border-radius: 8px;
    padding: 4px;
    outline: 0;
    selection-background-color: {SELECTION};
    selection-color: {INK};
}}

QSpinBox::up-button, QSpinBox::down-button {{
    background: transparent;
    border: 0;
    width: 18px;
}}
QSpinBox::up-arrow {{ image: url({chevron_up}); width: 9px; height: 9px; }}
QSpinBox::down-arrow {{ image: url({chevron_down}); width: 9px; height: 9px; }}

/* --- scrollbars ------------------------------------------------------------
   A thin handle on an empty track, no stepper arrows. The default is a wide
   grey channel with buttons at each end, which beside a page of a book looks
   like scaffolding. */
QScrollBar:vertical {{
    background: transparent;
    width: 11px;
    margin: 2px 2px 2px 0;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 11px;
    margin: 0 2px 2px 2px;
}}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: #cbc7be;
    border-radius: 4px;
    border: 2px solid transparent;
}}
QScrollBar::handle:vertical {{ min-height: 28px; }}
QScrollBar::handle:horizontal {{ min-width: 28px; }}
QScrollBar::handle:hover {{ background: {INK_TERTIARY}; }}
QScrollBar::handle:pressed {{ background: {INK_SECONDARY}; }}
/* The stepper buttons and the track above and below the handle. Given no size
   they disappear; left alone they come back as arrows. */
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0; height: 0; border: 0; background: transparent;
}}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QProgressBar {{
    background: {DOT_FUTURE};
    border: 0;
    border-radius: 1px;
    height: 2px;
    text-align: center;
}}
QProgressBar::chunk {{ background: {INK_META}; }}

QSplitter::handle {{ background: transparent; }}
"""
