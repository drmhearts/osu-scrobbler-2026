"""Keep the original credit while separating explicit featured performers."""

from dataclasses import dataclass
import re
import unicodedata


FEATURED = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+", re.I)


@dataclass(frozen=True)
class ArtistCredit:
    raw: str
    primary: str
    featured: tuple[str, ...] = ()


def split_credit(value: str) -> ArtistCredit:
    raw = str(value or "")
    parts = FEATURED.split(unicodedata.normalize("NFKC", raw).strip())
    return ArtistCredit(raw, parts[0].strip(), tuple(part.strip() for part in parts[1:] if part.strip()))


def primary(value):
    return split_credit(value).primary


def cache_text(value):
    # Cache identity keeps Unicode; transliteration is fuzzy evidence only.
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def collaboration_match(expected, actual):
    credit = split_credit(expected)
    if not credit.featured or not re.search(r"\s+(?:&|and|x)\s+", actual, re.I):
        return False
    actual_names = re.split(r"\s+(?:&|and|x)\s+", actual, flags=re.I)
    wanted = [credit.primary, *credit.featured]
    return sorted(cache_text(name) for name in wanted) == sorted(cache_text(name) for name in actual_names)
