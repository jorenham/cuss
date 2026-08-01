import argparse
import asyncio
import datetime as dt
import json
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from cuss._github import Filter, collect, connect, queries, since
from cuss._pkg import Api, Qualname, read, resolve
from cuss._usage import Blob, Stat, tally

_KEYWORDS = 5
_LABELS = {
    "call": "call",
    "subclass": "subcls",
    "decorator": "decor",
    "annotation": "annot",
    "reference": "ref",
    "import": "import",
}


@dataclass(frozen=True, slots=True)
class Options:
    target: str
    max_files: int
    top: int
    min_stars: int
    since: str | None
    extra: str
    forks: bool
    exclude_archived: bool
    unused: bool
    keywords: bool
    as_json: bool
    refresh: bool
    verbose: bool


def parse(argv: Sequence[str] | None) -> Options:
    parser = argparse.ArgumentParser(
        prog="cuss",
        description="usage statistics for a Python package's public API",
    )

    def add(*names: str, **options: Any) -> None:
        _ = parser.add_argument(*names, **options)

    add("target", help="dotted module, symbol, distribution, or directory")
    add("-n", "--max-files", type=int, default=500, help="corpus size (default: 500)")
    add("-t", "--top", type=int, default=40, help="rows to show (default: 40)")
    add("--min-stars", type=int, default=0, help="skip repositories below this")
    add("--since", help="only repositories pushed since DATE or age (e.g. 2y)")
    add("-q", "--extra", default="", help="extra code-search qualifiers")
    add("--forks", action="store_true", help="include forks")
    add("--exclude-archived", action="store_true", help="skip archived repositories")
    add("--unused", action="store_true", help="list public symbols with no usage")
    add("--keywords", action="store_true", help="show keyword arguments passed")
    add("--json", dest="as_json", action="store_true", help="emit JSON")
    add("--refresh", action="store_true", help="ignore cached results")
    add("-v", "--verbose", action="store_true", help="report progress on stderr")

    args = parser.parse_args(argv)
    return Options(
        target=args.target,
        max_files=args.max_files,
        top=args.top,
        min_stars=args.min_stars,
        since=args.since,
        extra=args.extra,
        forks=args.forks,
        exclude_archived=args.exclude_archived,
        unused=args.unused,
        keywords=args.keywords,
        as_json=args.as_json,
        refresh=args.refresh,
        verbose=args.verbose,
    )


def main(argv: Sequence[str] | None = None) -> int:
    options = parse(argv)
    try:
        base, scope = resolve(options.target)
        api = read(base, scope.partition(".")[0])
        blobs = asyncio.run(_gather(options, api, scope))
    except (LookupError, RuntimeError, ValueError, OSError, httpx.HTTPError) as error:
        print(f"cuss: {error}", file=sys.stderr)
        return 2

    stats = tally(blobs, api.root, api.modules)
    public = api.within(scope)
    report = Report(
        scope=scope,
        files=len(blobs),
        repos=len({blob.repo for blob in blobs}),
        ranked=sorted(
            ((name, stats[name]) for name in public & stats.keys()),
            key=lambda pair: pair[1].rank,
            reverse=True,
        ),
        unused=sorted(public - stats.keys()),
    )

    if options.as_json:
        print(json.dumps(_payload(report), indent=2))
    else:
        for line in _render(options, report):
            print(line)
    return 0


async def _gather(options: Options, api: Api, scope: Qualname) -> list[Blob]:
    module = scope if scope in api.modules else scope.rpartition(".")[0] or scope
    now = dt.datetime.now(dt.UTC)
    keep = Filter(
        since=since(options.since, now) if options.since else None,
        min_stars=options.min_stars,
        forks=options.forks,
        archived=not options.exclude_archived,
    )
    asked = [query + options.extra for query in queries(module)]
    async with connect(refresh=options.refresh, verbose=options.verbose) as github:
        return await collect(github, asked, options.max_files, keep)


@dataclass(frozen=True, slots=True)
class Report:
    scope: Qualname
    files: int
    repos: int
    ranked: Sequence[tuple[Qualname, Stat]]
    unused: Sequence[Qualname]


def _render(options: Options, report: Report) -> Iterator[str]:
    filters = f", pushed >= {options.since}" if options.since else ""
    yield f"{report.scope} — {report.files} files, {report.repos} repos{filters}"
    yield ""

    shown = report.ranked[: options.top]
    kinds = [k for k in _LABELS if any(k in stat.kinds for _, stat in shown)]
    header = ("symbol", "refs", "files", "repos", *(_LABELS[k] for k in kinds))
    align = "<>>>" + ">" * len(kinds)
    if options.keywords:
        header, align = (*header, "keywords"), align + "<"
    rows = [header, *(_row(pair, report.scope, kinds, options) for pair in shown)]
    yield from _table(rows, align) if shown else ("no usage found",)

    yield ""
    listing = "" if options.unused else " (--unused)"
    yield f"{len(report.unused)} public symbols unused{listing}"
    if options.unused:
        yield from (f"  {_relative(name, report.scope)}" for name in report.unused)


def _row(
    pair: tuple[Qualname, Stat],
    scope: Qualname,
    kinds: Sequence[str],
    options: Options,
) -> tuple[str, ...]:
    name, stat = pair
    row = (
        _relative(name, scope),
        str(stat.refs),
        str(len(stat.files)),
        str(len(stat.repos)),
        *(str(stat.kinds[k]) if k in stat.kinds else "" for k in kinds),
    )
    return (
        (*row, _counts(stat.keywords.most_common(_KEYWORDS)))
        if options.keywords
        else row
    )


def _relative(name: Qualname, scope: Qualname) -> str:
    """Drop the scope the header already states, keeping the scope itself whole.

    >>> _relative("scipy.special.gamma", "scipy.special"), _relative("a.b", "a.b")
    ('gamma', 'a.b')
    """
    return name if name == scope else name.removeprefix(f"{scope}.")


def _counts(items: Sequence[tuple[str, int]]) -> str:
    return ", ".join(f"{name} {count}" for name, count in items)


def _table(rows: Sequence[Sequence[str]], align: str) -> Iterator[str]:
    widths = [max(len(row[column]) for row in rows) for column in range(len(align))]
    for row in rows:
        cells = zip(row, align, widths, strict=True)
        yield "  ".join(
            c.rjust(w) if a == ">" else c.ljust(w) for c, a, w in cells
        ).rstrip()


def _payload(report: Report) -> dict[str, object]:
    return {
        "scope": report.scope,
        "files": report.files,
        "repos": report.repos,
        "symbols": {
            name: {
                "refs": stat.refs,
                "files": len(stat.files),
                "repos": len(stat.repos),
                "kinds": dict(stat.kinds.most_common()),
                "keywords": dict(stat.keywords.most_common()),
            }
            for name, stat in report.ranked
        },
        "unused": list(report.unused),
    }
