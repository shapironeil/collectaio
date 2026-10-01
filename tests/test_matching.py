import pytest

from drop_monitor.matching import Watch, find_watch, matches, normalize, tokens

TIN = "Pokemon 30 Anniversario - Mini Tin Case Sealed - ITA"
TIN_RANDOM = "Pokémon 30° Anniversario - Mini Tin Casuale (ITA)"
BUNDLE = "Pokemon 30 Anniversario - Bundle 6 Buste - ITA"


def test_normalize_strips_accents_ordinals_and_punctuation():
    assert normalize("Pokémon 30º Anniversario – Mini Tin (ITA)!") == "pokemon 30 anniversario mini tin ita"


def test_tokens_apply_synonyms_and_stopwords():
    assert tokens("Collezione con Statuina di Mew") == ["collezione", "statuina", "mew"]
    assert tokens("Pokémon Italiano") == ["pokemon", "ita"]


@pytest.mark.parametrize(
    "keywords,title,expected",
    [
        ("Pokemon 30 Anniversario Mini Tin Case ITA", TIN, True),
        ("pokemon 30 anniversario mini tin case ita", TIN, True),
        ("POKÉMON 30º ANNIVERSARIO MINI TIN CASE", TIN, True),
        ("Mini Tin Case", TIN, True),
        ("Pokemon 30 Anniversario Mini Tin Case ITA", TIN_RANDOM, False),  # "case" != "casuale"
        ("Pokemon 30 Anniversario Bundle 6 Buste ITA", BUNDLE, True),
        ("Pokemon 30 Anniversario Bundle 6 Buste ITA", TIN, False),
        ("Bundle 6 Buste JAP", BUNDLE, False),
        ("Mini Tin Case ITA", "Pokemon 30 Anniversario - Mini Tin Case Sealed - ITA", True),
    ],
)
def test_keyword_matching(keywords, title, expected):
    assert matches(Watch(keywords=keywords), title) is expected


def test_exclude_wins():
    w = Watch(keywords="Mini Tin", exclude=["casuale"])
    assert matches(w, TIN) is True
    assert matches(w, TIN_RANDOM) is False


def test_url_match_beats_title():
    w = Watch(keywords="something else entirely", url="https://www.gemcardinfinitycollection.it/it/pokemon-30-anniversario-mini-tin-case-sealed-ita")
    assert matches(w, "Weird renamed title", "https://gemcardinfinitycollection.it/it/pokemon-30-anniversario-mini-tin-case-sealed-ita/") is True
    assert matches(w, "Weird renamed title", "https://www.gemcardinfinitycollection.it/it/other") is False


def test_fuzzy_fallback():
    w = Watch(keywords="Pokemon 30 Anniversario Mini Tin Case Sealed ITA", fuzzy=0.85)
    assert matches(w, "Pokemon 30 Anniversario Mini Tin Case Seald ITA") is True  # typo on the shop side
    assert matches(Watch(keywords="Pokemon 30 Anniversario Mini Tin Case Sealed ITA"), "Pokemon 30 Anniversario Mini Tin Case Seald ITA") is False


def test_find_watch_returns_first_match():
    watches = [Watch(keywords="Bundle 6 Buste"), Watch(keywords="Mini Tin Case")]
    assert find_watch(watches, TIN).keywords == "Mini Tin Case"
    assert find_watch(watches, "Mazzo Lotte Umbreon ex") is None
