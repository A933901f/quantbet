"""Youth / reserve team-name markers (pure, dependency-free).

Used by the v4.5-sharp league hygiene (:class:`quantbet.syndicate.SharpParams`, which carries its own
frozen token tuples) and by the team-name matcher (:func:`quantbet.poller.team_similarity`), so a youth
or reserve side ("Arsenal U21", "Jong Ajax", "Barcelona B", "Bayern Munchen II") is never fuzzy-matched
to the senior club's ratings, sharp prices or events.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Sequence

# Same values as the SharpParams defaults (tests/test_sharp_v45.py asserts they stay equal).
YOUTH_TOKENS: tuple[str, ...] = ("u21", "u23", "u19", "u20", "u18", "u17", "reserves", "academy", "jong")
YOUTH_SUFFIX_TOKENS: tuple[str, ...] = (" b", " ii", " iii")
YOUTH_NAME_EXCEPTIONS: tuple[str, ...] = ("juan pablo ii",)  # senior Peruvian Liga 1 club

_NAME_SEP = re.compile(r"[_\-./]+")
_UNDER_NN = re.compile(r"(?<![a-z0-9])under ?(\d{2})(?![a-z0-9])")


def normalise_name(name: str | None) -> str:
    """Lower-case, accents stripped, ``_ - . /`` -> spaces, ``under 21`` / ``under-21`` -> ``u21``."""
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    s = " ".join(_NAME_SEP.sub(" ", s).split())
    return _UNDER_NN.sub(lambda m: "u" + m.group(1), s)


def youth_reserve_hit(name: str | None, tokens: Sequence[str] = YOUTH_TOKENS,
                      suffix_tokens: Sequence[str] = YOUTH_SUFFIX_TOKENS,
                      exceptions: Sequence[str] = YOUTH_NAME_EXCEPTIONS) -> str | None:
    """The youth / reserve marker found in a team name, or ``None``.

    The name is normalised by :func:`normalise_name` (``arsenal_u21`` -> ``arsenal u21``, ``Arsenal U-21``
    -> ``arsenal u 21``, ``Arsenal Under 21`` -> ``arsenal u21``). ``tokens`` (``u21``, ``reserves``,
    ``jong``, ...) match as whole words, an internal space allowed after ``u`` (``u 21``): ``Jong Ajax`` /
    ``Jong PSV`` hit, ``Jonge Kracht`` / ``Jongbloed`` do not. ``suffix_tokens`` (``" b"``, ``" ii"``,
    ``" iii"``) must be a separate word preceded by a space: ``Barcelona B`` / ``Bayern Munchen II`` hit,
    ``Club Brugge`` / ``Bayern`` / ``B 93`` do not. Names listed in ``exceptions`` (whole normalised name,
    e.g. the senior Peruvian club ``juan pablo ii``) never hit.
    """
    if not name:
        return None
    n = normalise_name(name)
    if n in exceptions:
        return None
    for t in tokens:
        tt = t.strip().lower()
        pat = (r"u ?" + re.escape(tt[1:])) if (len(tt) > 1 and tt[0] == "u" and tt[1:].isdigit()) else re.escape(tt)
        if re.search(r"(?<![a-z0-9])" + pat + r"(?![a-z0-9])", n):
            return tt
    for t in suffix_tokens:
        tt = t.strip().lower()
        if re.search(r"(?<=\s)" + re.escape(tt) + r"(?=\s|$)", n):
            return " " + tt
    return None


def youth_compatible(a: str | None, b: str | None) -> bool:
    """``False`` when exactly one name is a youth / reserve side, or both are but with different markers
    (``Arsenal U21`` vs ``Arsenal``; ``Arsenal U21`` vs ``Arsenal U18``) - such names must never be matched."""
    return youth_reserve_hit(a) == youth_reserve_hit(b)


__all__ = ["YOUTH_TOKENS", "YOUTH_SUFFIX_TOKENS", "YOUTH_NAME_EXCEPTIONS", "normalise_name", "youth_reserve_hit",
           "youth_compatible"]
