import datetime as dt
import time
from pathlib import Path

import httpx
import pytest

from cuss._cli import _LABELS
from cuss._github import (
    _BANDS,
    _PAGES,
    _PER_PAGE,
    Filter,
    Repo,
    _retry_after,
    _rounds,
    _share,
    _Source,
    since,
)
from cuss._pkg import read, resolve, symbols
from cuss._usage import Kind

SEVEN = 7.0
THIRTY = 30.0
MAX_PAUSE = 300.0

STUBS = {
    "__init__.pyi": "__all__ = ['special']\nfrom . import special\n",
    "special/__init__.pyi": "__all__ = ['gamma']\nfrom ._basic import gamma\n",
    "special/_basic.pyi": "__all__ = ['gamma', 'hidden']\n",
    "_lib/__init__.pyi": "__all__ = ['private']\n",
}


@pytest.fixture
def stubs(tmp_path: Path) -> Path:
    base = tmp_path / "toy-stubs"
    for name, source in STUBS.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(source, encoding="utf-8")
    return base


def test_read_indexes_public_and_private_modules(stubs: Path) -> None:
    api = read(stubs, "toy")
    assert api.modules["toy.special"] == frozenset({"gamma"})
    assert api.modules["toy.special._basic"] == frozenset({"gamma", "hidden"})
    assert api.public == frozenset({"toy.special", "toy.special.gamma"})


def test_within_scopes_to_a_prefix(stubs: Path) -> None:
    api = read(stubs, "toy")
    assert api.within("toy.special") == frozenset({"toy.special", "toy.special.gamma"})
    assert api.within("toy.special.gamma") == frozenset({"toy.special.gamma"})


def test_resolve_strips_the_stubs_suffix(stubs: Path) -> None:
    base, scope = resolve(str(stubs))
    assert (base, scope) == (stubs, "toy")


def test_resolve_rejects_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(LookupError):
        _ = resolve(str(tmp_path / "absent"))


def test_symbols_prefers_dunder_all_over_definitions() -> None:
    assert symbols("__all__ = ['a']\ndef b(): ...\n") == frozenset({"a"})


def test_symbols_accumulates_augmented_dunder_all() -> None:
    assert symbols("__all__ = ['a']\n__all__ += ['b']\n") == frozenset({"a", "b"})


def repo(**changes: object) -> Repo:
    stamp = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    fields = {
        "name": "a/b",
        "pushed": stamp,
        "created": stamp,
        "stars": 10,
        "archived": False,
        "fork": False,
    }
    return Repo(**(fields | changes))  # pyright: ignore[reportArgumentType]


def test_filter_defaults_exclude_forks_only() -> None:
    keep = Filter()
    assert keep(repo())
    assert not keep(repo(fork=True))
    assert keep(repo(archived=True))


def test_filter_applies_age_and_stars() -> None:
    keep = Filter(since=dt.datetime(2026, 6, 1, tzinfo=dt.UTC), min_stars=50)
    assert not keep(repo(stars=100))
    assert not keep(repo(pushed=dt.datetime(2026, 7, 1, tzinfo=dt.UTC)))
    assert keep(repo(stars=100, pushed=dt.datetime(2026, 7, 1, tzinfo=dt.UTC)))


def test_since_parses_relative_and_absolute() -> None:
    now = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
    assert since("2y", now) == dt.datetime(2024, 8, 1, tzinfo=dt.UTC)
    assert since("30d", now) == dt.datetime(2026, 7, 2, tzinfo=dt.UTC)
    assert since("2024-01-01", now) == dt.datetime(2024, 1, 1, tzinfo=dt.UTC)


def test_since_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="invalid --since"):
        _ = since("notadate", dt.datetime(2026, 8, 1, tzinfo=dt.UTC))


def throttled(**headers: str) -> httpx.Response:
    return httpx.Response(403, headers=headers)


def test_retry_after_prefers_the_explicit_header() -> None:
    assert _retry_after(throttled(**{"retry-after": "7"})) == pytest.approx(SEVEN)


def test_retry_after_falls_back_to_the_reset_timestamp() -> None:
    pause = _retry_after(throttled(**{"x-ratelimit-reset": str(int(time.time()) + 30)}))
    assert THIRTY - 10 <= pause <= THIRTY


def test_retry_after_is_capped() -> None:
    far = str(int(time.time()) + 10_000)
    assert _retry_after(throttled(**{"x-ratelimit-reset": far})) <= MAX_PAUSE


def test_climb_rises_from_a_subpackage_to_the_top_level(stubs: Path) -> None:
    assert resolve(str(stubs / "special")) == (stubs, "toy.special")


def test_climb_rises_from_a_module_file(stubs: Path) -> None:
    assert resolve(str(stubs / "special" / "_basic.pyi")) == (
        stubs,
        "toy.special._basic",
    )


def test_climb_treats_dunder_init_as_its_package(stubs: Path) -> None:
    assert resolve(str(stubs / "special" / "__init__.pyi")) == (stubs, "toy.special")


def test_climb_stops_at_the_top_level_package(stubs: Path) -> None:
    assert resolve(str(stubs)) == (stubs, "toy")


def test_climb_rejects_a_non_python_file(stubs: Path) -> None:
    other = stubs.parent / "notes.txt"
    _ = other.write_text("hi", encoding="utf-8")
    with pytest.raises(LookupError, match="not a Python file"):
        _ = resolve(str(other))


def test_every_kind_has_a_column_label() -> None:
    assert set(_LABELS) == set(Kind)


def test_the_catch_all_kind_is_last() -> None:
    assert list(_LABELS)[-1] is Kind.VALUE


@pytest.fixture
def lopsided() -> list[_Source]:
    return [_Source("rare", total=1_000), _Source("common", total=9_000)]


def test_share_never_drops_below_a_page(lopsided: list[_Source]) -> None:
    lopsided[1].total = 10**9
    assert _share(500, lopsided[0], lopsided) == _PER_PAGE


def test_a_source_that_runs_dry_hands_its_share_over(lopsided: list[_Source]) -> None:
    budget = 1_000
    rare, common = lopsided
    assert _share(budget, rare, lopsided) == budget // 10
    common.live = False
    assert _share(budget, rare, lopsided) == budget


def test_rounds_covers_every_band_and_page() -> None:
    turns = [*_rounds([_Source("a")])]
    assert len(turns) == len(_BANDS) * _PAGES
    assert turns[_PAGES][1].startswith("a size:")
