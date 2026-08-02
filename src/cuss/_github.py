import asyncio
import datetime as dt
import hashlib
import json
import os
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
import time
from collections.abc import AsyncGenerator, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from itertools import starmap
from math import ceil
from pathlib import Path
from typing import Any

import httpx

from cuss._usage import Blob

_API = "https://api.github.com"
_RAW = "https://raw.githubusercontent.com"
_CAPPED = 422
_THROTTLED = (403, 429)
_ATTEMPTS = 3
_RATE = 10
_PERIOD = 60.0
_MAX_PAUSE = 300.0
_PER_PAGE = 100
_PAGES = 10
_BATCH = 100
_CONCURRENCY = 16
_MAX_BYTES = 512 * 1024
_SEARCH_TTL = 7 * 24 * 3600.0
_REPO_TTL = 24 * 3600.0
_FIELDS = "nameWithOwner pushedAt createdAt stargazerCount isArchived isFork"
_AGES = {"d": 1, "m": 30, "y": 365}
_BANDS = (
    "",
    " size:<1000",
    " size:1000..4000",
    " size:4000..16000",
    " size:16000..65536",
    " size:>65536",
)


@dataclass(frozen=True, slots=True)
class Hit:
    repo: str
    ref: str
    path: str
    sha: str


@dataclass(frozen=True, slots=True)
class Repo:
    name: str
    pushed: dt.datetime
    created: dt.datetime
    stars: int
    archived: bool
    fork: bool


@dataclass(frozen=True, slots=True)
class Filter:
    since: dt.datetime | None = None
    min_stars: int = 0
    forks: bool = False
    archived: bool = True

    def __call__(self, repo: Repo) -> bool:
        return (
            (self.since is None or repo.pushed >= self.since)
            and repo.stars >= self.min_stars
            and (self.forks or not repo.fork)
            and (self.archived or not repo.archived)
        )


def token() -> str:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        if value := os.environ.get(name):
            return value
    try:
        done = subprocess.run(
            ["gh", "auth", "token"],  # ruff: ignore[start-process-with-partial-path]
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except OSError, subprocess.SubprocessError:
        msg = "no GitHub token: set GITHUB_TOKEN or run `gh auth login`"
        raise RuntimeError(msg) from None
    return done.stdout.strip()


def cache_home() -> Path:
    match sys.platform:
        case "win32":
            base = os.environ.get("LOCALAPPDATA") or "~/AppData/Local"
        case "darwin":
            base = "~/Library/Caches"
        case _:
            base = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    return Path(base).expanduser() / "cuss"


@dataclass(frozen=True, slots=True)
class Cache:
    root: Path
    refresh: bool = False

    def get(self, kind: str, key: str, ttl: float = 0.0) -> str | None:
        path = self._path(kind, key)
        if self.refresh or not path.is_file():
            return None
        if ttl and time.time() - path.stat().st_mtime > ttl:
            return None
        return path.read_text(encoding="utf-8")

    def put(self, kind: str, key: str, text: str) -> None:
        path = self._path(kind, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text, encoding="utf-8")

    def _path(self, kind: str, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self.root / kind / digest[:2] / digest


@dataclass(slots=True)
class Limiter:
    rate: int
    period: float
    times: list[float] = field(default_factory=list)

    async def wait(self) -> float:
        now = time.monotonic()
        self.times = [t for t in self.times if now - t < self.period]
        delay = 0.0
        if len(self.times) >= self.rate:
            delay = self.period - (now - self.times[0])
            await asyncio.sleep(delay)
        self.times.append(time.monotonic())
        return delay


@dataclass(frozen=True, slots=True)
class GitHub:
    api: httpx.AsyncClient
    raw: httpx.AsyncClient
    cache: Cache
    limiter: Limiter
    verbose: bool = False

    async def page(self, query: str, number: int) -> tuple[list[Hit], bool]:
        """The hits on one page, and whether the query still has more to give."""
        items = await self._page(query, number)
        hits = [hit for item in items if (hit := _hit(item)) is not None]
        return hits, len(items) == _PER_PAGE

    async def repos(self, names: Sequence[str]) -> dict[str, Repo]:
        found: dict[str, Repo] = {}
        missing: list[str] = []
        for name in names:
            cached = self.cache.get("repo", name, _REPO_TTL)
            if cached is None:
                missing.append(name)
            elif (repo := _repo(json.loads(cached))) is not None:
                found[name] = repo

        for batch in _chunks(missing, _BATCH):
            self._log(f"metadata for {len(batch)} repositories")
            payload = await self._graphql(_query(batch))
            for node in payload.values():
                if (repo := _repo(node)) is not None:
                    found[repo.name] = repo
                    self.cache.put("repo", repo.name, json.dumps(node))
        return found

    async def blobs(self, hits: Sequence[Hit]) -> list[Blob]:
        self._log(f"fetching {len(hits)} files")
        gate = asyncio.Semaphore(_CONCURRENCY)
        fetched = await asyncio.gather(*(self._blob(hit, gate) for hit in hits))
        return [blob for blob in fetched if blob is not None]

    async def _page(self, query: str, page: int) -> list[dict[str, Any]]:
        key = f"{query}#{page}"
        if (cached := self.cache.get("search", key, _SEARCH_TTL)) is not None:
            return json.loads(cached)["items"]

        response = await self._attempt(query, page)
        if response.status_code == _CAPPED:
            return []
        _ = response.raise_for_status()
        self.cache.put("search", key, response.text)
        return response.json()["items"]

    async def _attempt(self, query: str, page: int) -> httpx.Response:
        """The limiter only knows this process; GitHub counts every one of them."""
        pause = 0.0
        for _ in range(_ATTEMPTS):
            await asyncio.sleep(pause)
            delay = await self.limiter.wait()
            waited = f" [+{delay:.0f}s]" if delay else ""
            self._log(f"search page {page}: {query}{waited}")
            response = await self.api.get(
                f"{_API}/search/code",
                params={"q": query, "per_page": _PER_PAGE, "page": page},
            )
            if response.status_code not in _THROTTLED:
                return response
            pause = _retry_after(response)
            self._log(f"throttled by GitHub, retrying in {pause:.0f}s")
        msg = "GitHub kept throttling code search; try again later"
        raise RuntimeError(msg)

    async def _graphql(self, query: str) -> dict[str, Any]:
        response = await self.api.post(f"{_API}/graphql", json={"query": query})
        _ = response.raise_for_status()
        return response.json().get("data") or {}

    async def _blob(self, hit: Hit, gate: asyncio.Semaphore) -> Blob | None:
        if (cached := self.cache.get("blob", hit.sha)) is not None:
            return Blob(hit.repo, hit.sha, cached)
        async with gate:
            try:
                response = await self.raw.get(f"{_RAW}/{hit.repo}/{hit.ref}/{hit.path}")
            except httpx.HTTPError:
                return None
        if not response.is_success or len(response.content) > _MAX_BYTES:
            return None
        self.cache.put("blob", hit.sha, response.text)
        return Blob(hit.repo, hit.sha, response.text)

    def _log(self, message: str) -> None:
        if self.verbose:
            _ = sys.stderr.write(f"cuss: {message}\n")


@asynccontextmanager
async def connect(
    *, refresh: bool = False, verbose: bool = False
) -> AsyncGenerator[GitHub]:
    headers = {
        "authorization": f"Bearer {token()}",
        "accept": "application/vnd.github+json",
        "user-agent": "cuss",
    }
    async with (
        httpx.AsyncClient(headers=headers, timeout=30.0) as api,
        httpx.AsyncClient(timeout=30.0, follow_redirects=True) as raw,
    ):
        cache = Cache(cache_home(), refresh)
        yield GitHub(api, raw, cache, Limiter(_RATE, _PERIOD), verbose)


async def collect(
    github: GitHub,
    queries: Sequence[str],
    limit: int,
    keep: Filter,
) -> list[Blob]:
    """Search, enrich, filter, then download only the files that survive.

    Each query is capped at an equal share. `import numpy` outruns
    `from numpy import` forty to one, so without a cap whichever is asked first
    fills the budget alone and the package gets described by one idiom.
    """
    share = ceil(limit / len(queries))
    seen: set[str] = set()
    taken = [0] * len(queries)
    hits: list[Hit] = []
    spent: set[str] = set()

    for index, query, page in _rounds(queries):
        if taken[index] >= share or query in spent:
            continue
        found, more = await github.page(query, page)
        if not more:
            spent.add(query)
        fresh = [h for h in found if h.sha not in seen]
        seen.update(h.sha for h in fresh)
        repos = await github.repos(sorted({h.repo for h in fresh}))
        good = [h for h in fresh if (r := repos.get(h.repo)) is not None and keep(r)]
        room = good[: share - taken[index]]
        taken[index] += len(room)
        hits += room
    return await github.blobs(hits[:limit])


def _rounds(queries: Sequence[str]) -> Iterator[tuple[int, str, int]]:
    """Which query, what to ask, which page — interleaved, never one query deep.

    >>> [*_rounds(("a", "b"))][:4]
    [(0, 'a', 1), (1, 'b', 1), (0, 'a', 2), (1, 'b', 2)]
    """
    for band in _BANDS:
        for page in range(1, _PAGES + 1):
            for index, query in enumerate(queries):
                yield index, query + band, page


def queries(module: str) -> list[str]:
    """Code-search queries that find files using *module*.

    >>> for query in queries("scipy.special"):
    ...     print(query)
    "from scipy.special import" language:python
    "import scipy.special" language:python
    """
    return [
        f'"from {module} import" language:python',
        f'"import {module}" language:python',
    ]


def _retry_after(response: httpx.Response) -> float:
    """How long GitHub asks us to back off, from either throttling header.

    >>> _retry_after(httpx.Response(403, headers={"retry-after": "12"}))
    12.0
    """
    after = response.headers.get("retry-after", "")
    if after.isdigit():
        return min(float(after), _MAX_PAUSE)
    reset = response.headers.get("x-ratelimit-reset", "")
    if reset.isdigit():
        return min(max(float(reset) - time.time(), 0.0), _MAX_PAUSE)
    return _PERIOD


def _hit(item: dict[str, Any]) -> Hit | None:
    repo = item["repository"]["full_name"]
    prefix = f"https://github.com/{repo}/blob/"
    url = item["html_url"]
    if not url.startswith(prefix):
        return None
    return Hit(
        repo, url.removeprefix(prefix).partition("/")[0], item["path"], item["sha"]
    )


def _repo(node: dict[str, Any] | None) -> Repo | None:
    if node is None:
        return None
    return Repo(
        node["nameWithOwner"],
        dt.datetime.fromisoformat(node["pushedAt"]),
        dt.datetime.fromisoformat(node["createdAt"]),
        node["stargazerCount"],
        node["isArchived"],
        node["isFork"],
    )


def _query(names: Sequence[str]) -> str:
    fields = "\n".join(starmap(_field, enumerate(names)))
    return f"{{\n{fields}\n}}"


def _field(index: int, full: str) -> str:
    owner, _, name = full.partition("/")
    args = f"owner: {json.dumps(owner)}, name: {json.dumps(name)}"
    return f"r{index}: repository({args}) {{ {_FIELDS} }}"


def _chunks[T](items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def since(value: str, now: dt.datetime) -> dt.datetime:
    """Parse an absolute date or a relative age like ``18m`` or ``2y``.

    >>> now = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
    >>> since("2y", now).date(), since("2024-01-01", now).date()
    (datetime.date(2024, 8, 1), datetime.date(2024, 1, 1))
    """
    if value[-1:] in _AGES and value[:-1].isdigit():
        return now - dt.timedelta(days=_AGES[value[-1]] * int(value[:-1]))
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        msg = f"invalid --since value {value!r}: use a date like 2024-01-01, or 18m"
        raise ValueError(msg) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
