"""A rough guess at what language a document is written in.

Questions are written in the language of the source, so the generator has to know
what that is. The guess only has to separate a handful of plausible candidates,
not identify an arbitrary language, so counting function words beats pulling in a
dependency -- and when it is unsure it says so, and the import CLI takes the
answer from the command line instead.
"""

import re

# Function words are the giveaway: they are frequent, short, and do not travel
# between languages the way technical terms in a textbook do.
STOPWORDS = {
    "cs": {"a", "je", "se", "na", "že", "v", "do", "pro", "který", "která", "které",
           "jsou", "být", "podle", "tedy", "však", "nebo", "této", "tento", "při"},
    "sk": {"a", "je", "sa", "na", "že", "v", "do", "pre", "ktorý", "ktorá", "ktoré",
           "sú", "byť", "podľa", "teda", "však", "alebo", "tejto", "tento", "pri"},
    "en": {"the", "of", "and", "to", "in", "is", "that", "for", "as", "with",
           "are", "this", "which", "be", "on", "by", "an", "or", "from", "can"},
    "de": {"der", "die", "das", "und", "ist", "den", "von", "zu", "mit", "für",
           "sich", "auf", "dem", "nicht", "eine", "als", "auch", "werden", "sind"},
}

# Characters only one of the candidates uses, to break a tie between close pairs.
# Only the letters one of the pair has and the other does not. The sets used to
# list every accented letter each language uses, which meant both matched any
# Czech or Slovak text and the 1.5x went to both -- a tie-break that broke no
# tie. What separates them is a handful of letters: Czech has ě, ř, ů; Slovak
# has ľ, ĺ, ŕ, ô, ä.
MARKERS = {
    "cs": set("ěřů"),
    "sk": set("ľĺŕôä"),
    "de": set("öüß"),
}

MIN_WORDS = 200          # below this the counts are noise
MIN_MARGIN = 1.25        # how far ahead the winner has to be to be believed


def detect(text: str) -> str | None:
    """An ISO 639-1 code, or None when the text does not settle it.

    None is a real answer, not a failure: guessing wrong makes the generator write
    questions in a language the reader did not ask for, which is worse than asking.
    """
    words = re.findall(r"[^\W\d_]+", text.lower(), flags=re.UNICODE)
    if len(words) < MIN_WORDS:
        return None

    scores = {
        language: sum(1 for word in words if word in stopwords)
        for language, stopwords in STOPWORDS.items()
    }

    # Czech and Slovak share most of their function words; the alphabet does not.
    letters = set(text.lower())
    for language, markers in MARKERS.items():
        if markers & letters:
            scores[language] *= 1.5

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best, runner_up = ranked[0], ranked[1]
    if best[1] == 0:
        return None
    if runner_up[1] and best[1] < runner_up[1] * MIN_MARGIN:
        return None
    return best[0]


def name(code: str | None) -> str:
    """A readable name for the UI and for the generation prompt."""
    return {"cs": "Czech", "sk": "Slovak", "en": "English", "de": "German"}.get(
        code or "", "unknown"
    )
