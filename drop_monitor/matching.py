"""Case-insensitive, accent-insensitive, punctuation-tolerant keyword matching."""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_SYNONYMS = {
    # frequent shop-side spellings that users rarely type
    "pokémon": "pokemon",
    "pokemòn": "pokemon",
    "italiano": "ita",
    "italiana": "ita",
    "it": "ita",
}
_STOPWORDS = {"di", "da", "del", "della", "con", "e", "the", "of", "a", "il", "la", "lo", "le", "i"}


def normalize(text: str) -> str:
    """Lowercase, strip accents, drop ordinal signs/punctuation, collapse spaces."""
    text = text.replace("º", "").replace("°", "").replace("ª", "")  # before NFKD: 'º' decomposes to 'o'
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return " ".join(_SPLIT_RE.split(text)).strip()


def tokens(text: str) -> list[str]:
    out = []
    for tok in normalize(text).split():
        tok = _SYNONYMS.get(tok, tok)
        if tok and tok not in _STOPWORDS:
            out.append(tok)
    return out


@dataclass
class Watch:
    """One entry of the `products` list in config.yaml."""

    keywords: str  # e.g. "Pokemon 30 Anniversario Mini Tin Case ITA"
    exclude: list[str] = field(default_factory=list)  # titles containing any of these never match
    url: str | None = None  # optional exact product URL (strongest signal)
    name: str | None = None  # display name; defaults to keywords
    fuzzy: float = 0.0  # 0 = disabled; else min difflib ratio for a fuzzy fallback (e.g. 0.85)

    @property
    def label(self) -> str:
        return self.name or self.keywords


def matches(watch: Watch, title: str, url: str | None = None) -> bool:
    """True when `title` (or `url`) is the product described by `watch`.

    Strategy, in order:
      1. exact URL match (if the watch has a URL);
      2. every keyword token must appear in the title tokens (order-free);
      3. optional fuzzy ratio over the normalized strings.
    Exclusions always win.
    """
    norm_title = normalize(title)
    for ex in watch.exclude:
        if ex and normalize(ex) in norm_title:
            return False

    if watch.url and url:
        if _canon_url(watch.url) == _canon_url(url):
            return True

    want = tokens(watch.keywords)
    have = set(tokens(title))
    if want and all(tok in have for tok in want):
        return True

    if watch.fuzzy > 0:
        ratio = difflib.SequenceMatcher(None, " ".join(want), " ".join(tokens(title))).ratio()
        if ratio >= watch.fuzzy:
            return True
    return False


def _canon_url(url: str) -> str:
    url = url.strip().lower()
    url = re.sub(r"^https?://(www\.)?", "", url)
    return url.rstrip("/")


def find_watch(watches: list[Watch], title: str, url: str | None = None) -> Watch | None:
    """Return the first watch matching the product, or None."""
    for w in watches:
        if matches(w, title, url):
            return w
    return None
