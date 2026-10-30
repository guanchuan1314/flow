"""MT5-style name masks, as MT5 uses them for groups and symbols.

"real\\*, !real\\vip" matches every group starting with real\\ except real\\vip. Masks are separated by commas
or spaces, `*` stands for any text (`?` for one character), a leading `!` leaves names out and `[` is an ordinary
character. Like MT5's group names, matching ignores case.
"""

import re
from collections.abc import Iterable
from fnmatch import fnmatchcase

from app.config import get_settings


def parse(masks: str | Iterable[str] | None) -> list[str]:
    """The separate masks of "a*, b* !c" (a list passes through, minus blanks)."""
    if masks is None:
        return []
    if not isinstance(masks, str):
        masks = ",".join(masks)
    return [m for m in re.split(r"[,\s]+", masks) if m]


def _fits(name: str, mask: str) -> bool:
    return fnmatchcase(name.lower(), mask.lower().replace("[", "[[]"))


def matches(masks: str | Iterable[str] | None, name: str) -> bool:
    """True if some mask takes the name in and no "!" mask leaves it out."""
    found = False
    for mask in parse(masks):
        if mask.startswith("!"):
            if _fits(name, mask[1:]):
                return False
        elif _fits(name, mask):
            found = True
    return found


def to_like(mask: str) -> str:
    """One mask (without its "!") as a lower-case SQL LIKE pattern; use it with escape="!" on a lower()ed column."""
    out = mask.lower().replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return out.replace("*", "%").replace("?", "_")


def excluded_groups() -> list[str]:
    """The MT5 groups the CRM never imports or touches (setting MT5_SYNC_EXCLUDE_GROUPS)."""
    return parse(get_settings().mt5_sync_exclude_groups)
