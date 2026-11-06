"""MT5-style name masks, as MT5 uses them for groups and symbols.

"real\\*, !real\\vip" matches every group starting with real\\ except real\\vip. Masks are separated by commas
or spaces, `*` stands for any text (`?` for one character), a leading `!` leaves names out and `[` is an ordinary
character. Like MT5's group names, matching ignores case.
"""

import re
from collections.abc import Iterable
from fnmatch import fnmatchcase

from sqlalchemy import String, cast, false, func

from app.config import get_settings


def wildcard(pattern: str) -> str | None:
    """A LIKE pattern for `*` (any text) and `?` (one character) searches, e.g. *EUR* contains EUR, EUR* starts
    with it, *EUR ends with it; None when the text has no wildcard (then it's an exact match)."""
    if "*" not in pattern and "?" not in pattern:
        return None
    escaped = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped.replace("*", "%").replace("?", "_")


def filter_login_symbol(query, model, login: str, symbol: str):
    """Filter a query on a model's login and symbol. Both accept * and ? wildcards."""
    login, symbol = login.strip(), symbol.strip().upper()
    if login:
        like = wildcard(login)
        if like is not None:
            query = query.where(cast(model.login, String).like(like, escape="\\"))
        elif login.isdigit():
            query = query.where(model.login == int(login))
        else:
            query = query.where(false())  # not a login and no wildcard: nothing matches
    if symbol:
        like = wildcard(symbol)
        query = query.where(func.upper(model.symbol).like(like, escape="\\") if like is not None
                            else model.symbol == symbol)
    return query


def filter_comment(query, model, comment: str):
    """Filter a query on a model's comment, ignoring case. Accepts * and ? wildcards."""
    comment = comment.strip().upper()
    if not comment:
        return query
    like = wildcard(comment)
    column = func.upper(model.comment)
    return query.where(column.like(like, escape="\\") if like is not None else column == comment)


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
