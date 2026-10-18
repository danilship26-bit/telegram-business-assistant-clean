"""Match Russian contact names across grammatical cases."""

import re
from functools import lru_cache


def normalize(value):
    return value.strip(" \t\r\n.,!?;:«»\"").casefold().replace("ё", "е").lstrip("@")


@lru_cache(maxsize=1)
def analyzer():
    import pymorphy3
    return pymorphy3.MorphAnalyzer()


@lru_cache(maxsize=2048)
def forms(name):
    """Only generate case forms for a known name; never guess a new contact."""
    words = name.strip().split()
    if not words or len(words) > 3:
        return frozenset((normalize(name),))
    output = {normalize(name)}
    for index, word in enumerate(words):
        if not re.fullmatch(r"[А-Яа-яЁё-]+", word):
            continue
        parses = analyzer().parse(word)
        for parsed in parses[:5]:
            if "NOUN" not in parsed.tag and "ADJF" not in parsed.tag:
                continue
            for case in ("gent", "datv", "accs", "ablt", "loct"):
                inflected = parsed.inflect({case})
                if inflected:
                    variant = words.copy()
                    variant[index] = inflected.word
                    output.add(normalize(" ".join(variant)))
    return frozenset(output)


def match_names(query, alias, display_name, username):
    """Return score for ordered matching: exact alias, exact name, inflected names."""
    query = normalize(query)
    if alias and query == normalize(alias):
        return 0
    if username and query == normalize(username):
        return 1
    if query == normalize(display_name):
        return 1
    if alias and query in forms(alias):
        return 2
    if query in forms(display_name):
        return 3
    first = display_name.split()[0] if display_name.split() else ""
    if first and query in forms(first):
        return 4
    return None
