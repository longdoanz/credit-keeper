"""Rule matching and header mutation logic.

The functions here operate on plain header containers so they can be unit-tested
without depending on mitmproxy. The header argument to ``apply_ops`` may be:

* a :class:`CaseInsensitiveHeaders` (provided here for tests), or
* a mitmproxy ``Headers`` object (multidict-like, supports ``__setitem__``,
  ``__delitem__``, ``get_all`` and ``add``).
"""

from __future__ import annotations

import fnmatch
import re
from functools import lru_cache
from typing import Iterable, Iterator, Literal

from .config import HeaderOp, Rule

Direction = Literal["request", "response"]


# ---------------------------------------------------------------------------
# CaseInsensitiveHeaders helper
# ---------------------------------------------------------------------------


class CaseInsensitiveHeaders:
    """A simple multi-valued, case-insensitive header container for tests.

    Implements just enough of the mitmproxy ``Headers`` API for ``apply_ops``:
    ``__setitem__``, ``__delitem__``, ``__contains__``, ``get_all`` and ``add``.
    Stores entries as a list of ``(original_name, value)`` tuples to preserve
    insertion order while matching by lowercased name.
    """

    def __init__(self, items: Iterable[tuple[str, str]] | None = None) -> None:
        self._items: list[tuple[str, str]] = []
        if items:
            for k, v in items:
                self.add(k, v)

    # ------------------------------------------------------------------
    # mitmproxy-compatible surface
    # ------------------------------------------------------------------

    def add(self, name: str, value: str) -> None:
        self._items.append((name, value))

    def get_all(self, name: str) -> list[str]:
        lower = name.lower()
        return [v for k, v in self._items if k.lower() == lower]

    def __setitem__(self, name: str, value: str) -> None:
        # Replace all existing values for this header with a single value.
        lower = name.lower()
        new_items: list[tuple[str, str]] = []
        replaced = False
        for k, v in self._items:
            if k.lower() == lower:
                if not replaced:
                    new_items.append((name, value))
                    replaced = True
                # drop additional entries
            else:
                new_items.append((k, v))
        if not replaced:
            new_items.append((name, value))
        self._items = new_items

    def __delitem__(self, name: str) -> None:
        lower = name.lower()
        before = len(self._items)
        self._items = [(k, v) for k, v in self._items if k.lower() != lower]
        if len(self._items) == before:
            raise KeyError(name)

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        lower = name.lower()
        return any(k.lower() == lower for k, _ in self._items)

    def __getitem__(self, name: str) -> str:
        values = self.get_all(name)
        if not values:
            raise KeyError(name)
        return values[0]

    def __iter__(self) -> Iterator[str]:
        # Iterate over header names (case preserved as inserted).
        return iter(k for k, _ in self._items)

    def items(self) -> list[tuple[str, str]]:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"CaseInsensitiveHeaders({self._items!r})"


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@lru_cache(maxsize=256)
def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _host_glob_matches(pattern: str, host: str) -> bool:
    return fnmatch.fnmatchcase(host.lower(), pattern.lower())


def rule_matches(
    rule: Rule,
    *,
    host: str,
    url: str,
    method: str,
    direction: Direction,
) -> bool:
    """Return True if ``rule`` should fire for the given request/response."""

    # Direction filter
    if rule.apply_to != "both" and rule.apply_to != direction:
        return False

    # Method filter
    if rule.methods:
        if method.upper() not in {m.upper() for m in rule.methods}:
            return False

    # Host glob
    if rule.host is not None and not _host_glob_matches(rule.host, host):
        return False

    # Host regex
    if rule.host_regex is not None and not _compile(rule.host_regex).search(host):
        return False

    # URL regex
    if rule.url_regex is not None and not _compile(rule.url_regex).search(url):
        return False

    # The rule must actually have ops for this direction.
    if direction == "request" and not rule.request:
        return False
    if direction == "response" and not rule.response:
        return False

    return True


# ---------------------------------------------------------------------------
# Header mutation
# ---------------------------------------------------------------------------


def _get_all(headers, name: str) -> list[str]:
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        return list(get_all(name))
    # Plain dict fallback (case-sensitive keys, but try both).
    if hasattr(headers, "get"):
        v = headers.get(name)
        if v is None:
            return []
        return [v]
    return []


def _add(headers, name: str, value: str) -> None:
    add = getattr(headers, "add", None)
    if callable(add):
        add(name, value)
        return
    # Plain dict fallback: comma-join (RFC 7230 list syntax) if already present.
    existing = headers.get(name) if hasattr(headers, "get") else None
    if existing:
        headers[name] = f"{existing}, {value}"
    else:
        headers[name] = value


def _set(headers, name: str, value: str) -> None:
    headers[name] = value


def _delete(headers, name: str) -> None:
    try:
        del headers[name]
    except KeyError:
        return


def apply_ops(ops: list[HeaderOp], headers) -> None:
    """Apply a list of header operations to ``headers`` in order.

    Supports both :class:`CaseInsensitiveHeaders` and mitmproxy's ``Headers``.
    """

    for op in ops:
        if op.action == "set":
            assert op.value is not None  # validated by config loader
            _set(headers, op.name, op.value)
        elif op.action == "add":
            assert op.value is not None
            _add(headers, op.name, op.value)
        elif op.action == "remove":
            _delete(headers, op.name)
        elif op.action == "replace":
            assert op.pattern is not None and op.value is not None
            current = _get_all(headers, op.name)
            if not current:
                continue
            regex = _compile(op.pattern)
            new_values = [regex.sub(op.value, v) for v in current]
            # Replace all existing entries with the new list.
            _delete(headers, op.name)
            for v in new_values:
                _add(headers, op.name, v)
        else:  # pragma: no cover - guarded by config validation
            raise ValueError(f"unknown action: {op.action!r}")
