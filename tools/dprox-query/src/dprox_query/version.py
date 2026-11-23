"""This tool's version, and the dprox versions it works with.

The design asks for this by name: *`dprox-query` states which dprox versions
it works with and refuses others, so an old copy in an image fails with a
clear message.* The statement is here, in one place, and
``describe_support()`` is what both the error and ``dprox-query check`` print.
"""

from __future__ import annotations

__version__ = "0.3.0"

MIN_DPROX = (0, 3, 0)
"""The oldest dprox this tool can read. 0.3.0 is where one dprox began
answering every org, a result gained its ``org`` field and the response
metadata gained ``orgs_applied``. Against 0.2.x this tool would print results
with no org and silently imply there was only one."""

BELOW_DPROX = (0, 4, 0)
"""The first dprox this tool will not read. Below 1.0 a minor is a breaking
change (constitution §4), so the next minor is assumed incompatible until a
release of this tool says otherwise. Refusing a version that turns out to have
been fine costs one obvious error; accepting one that is not costs a wrong
answer nobody sees."""


class UnreadableVersion(ValueError):
    """A version string that cannot be compared. Never guessed at."""


def parse_version(text: str) -> tuple[int, int, int]:
    """Parse ``MAJOR.MINOR.PATCH``, ignoring any ``-rc1``-style suffix.

    Refuses anything else. A version this tool cannot read is not treated as
    old or as new: it is an error, because either guess is silent when wrong.
    """
    core = text.strip().lstrip("v").split("+", 1)[0].split("-", 1)[0]
    parts = core.split(".")
    if len(parts) != 3:
        raise UnreadableVersion(f"not a MAJOR.MINOR.PATCH version: {text!r}")
    try:
        major, minor, patch = (int(p) for p in parts)
    except ValueError as exc:
        raise UnreadableVersion(f"not a MAJOR.MINOR.PATCH version: {text!r}") from exc
    return major, minor, patch


def supports(dprox_version: str) -> bool:
    """True when this tool can read that dprox. Raises on an unreadable one."""
    return MIN_DPROX <= parse_version(dprox_version) < BELOW_DPROX


def describe_support() -> str:
    """The supported range, as a person reads it."""
    lo = ".".join(str(n) for n in MIN_DPROX)
    hi = ".".join(str(n) for n in BELOW_DPROX)
    return f"dprox >= {lo} and < {hi}"
