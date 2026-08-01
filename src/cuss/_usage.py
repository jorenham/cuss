import ast
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Set as AbstractSet
from dataclasses import dataclass, field
from enum import StrEnum

from cuss._pkg import Qualname


class Kind(StrEnum):
    CALL = "call"
    SUBCLASS = "subclass"
    DECORATOR = "decorator"
    ANNOTATION = "annotation"
    REFERENCE = "reference"
    IMPORT = "import"


@dataclass(frozen=True, slots=True)
class Ref:
    name: Qualname
    kind: Kind
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Blob:
    repo: str
    sha: str
    source: str


@dataclass(slots=True)
class Stat:
    refs: int = 0
    files: set[str] = field(default_factory=set)
    repos: set[str] = field(default_factory=set)
    kinds: Counter[str] = field(default_factory=Counter)
    keywords: Counter[str] = field(default_factory=Counter)

    @property
    def rank(self) -> tuple[int, int]:
        return len(self.repos), self.refs


def bindings(
    tree: ast.AST,
    root: str,
    api: Mapping[Qualname, frozenset[str]],
) -> dict[str, Qualname]:
    """Map local names to the qualified names under *root* they refer to.

    >>> bindings(ast.parse("import scipy.special as sp"), "scipy", {})
    {'sp': 'scipy.special'}
    >>> bindings(ast.parse("from scipy.special import gamma as g"), "scipy", {})
    {'g': 'scipy.special.gamma'}
    >>> bindings(ast.parse("import scipy.special"), "scipy", {})
    {'scipy': 'scipy'}
    >>> bindings(ast.parse("from scipy import stats"), "scipy", {})
    {'stats': 'scipy.stats'}
    >>> bindings(ast.parse("from scipy.io import *"), "scipy", {"scipy.io": {"mmread"}})
    {'mmread': 'scipy.io.mmread'}
    """
    found: dict[str, Qualname] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _under(alias.name, root):
                    found[alias.asname or _head(alias.name)] = (
                        alias.name if alias.asname else _head(alias.name)
                    )
        elif isinstance(node, ast.ImportFrom) and (module := _source(node, root)):
            for alias in node.names:
                if alias.name == "*":
                    found |= {s: f"{module}.{s}" for s in api.get(module, ())}
                else:
                    found[alias.asname or alias.name] = f"{module}.{alias.name}"
    return found


def imports(tree: ast.AST, root: str) -> list[Qualname]:
    """Qualified names brought into scope by import statements.

    >>> imports(ast.parse("import scipy.special\\nfrom scipy import stats"), "scipy")
    ['scipy.special', 'scipy.stats']
    """
    found: list[Qualname] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if _under(a.name, root)]
        elif isinstance(node, ast.ImportFrom) and (module := _source(node, root)):
            found += [f"{module}.{a.name}" for a in node.names if a.name != "*"]
    return found


def usages(
    source: str,
    root: str,
    api: Mapping[Qualname, frozenset[str]],
) -> Iterator[Ref]:
    """Every reference to *root*'s API in *source*, with how it was used.

    >>> api = {"scipy.special": frozenset({"gamma"})}
    >>> src = "from scipy.special import gamma as g\\ng(1, out=None)\\n"
    >>> [(r.name, r.kind.value, r.keywords) for r in usages(src, "scipy", api)]
    [('scipy.special.gamma', 'call', ('out',))]
    >>> [r.kind.value for r in usages("import scipy.special\\n", "scipy", api)]
    ['import']
    """
    tree = ast.parse(source)
    shadowed = _shadowed(tree)
    binds = {k: v for k, v in bindings(tree, root, api).items() if k not in shadowed}
    known = frozenset(api) | {f"{m}.{s}" for m, names in api.items() for s in names}
    context = _classify(tree)
    seen: set[Qualname] = set()

    for node in _references(tree, context):
        dotted = _dotted(node)
        head, _, tail = (dotted or "").partition(".")
        if dotted is None or (target := binds.get(head)) is None:
            continue
        if (name := _trim(f"{target}.{tail}" if tail else target, known)) is None:
            continue
        seen.add(name)
        kind = context.kinds.get(id(node), Kind.REFERENCE)
        yield Ref(name, kind, context.keywords.get(id(node), ()))

    for name in imports(tree, root):
        if not any(s == name or s.startswith(f"{name}.") for s in seen):
            yield Ref(name, Kind.IMPORT)


def tally(
    blobs: Iterable[Blob],
    root: str,
    api: Mapping[Qualname, frozenset[str]],
) -> dict[Qualname, Stat]:
    stats: dict[Qualname, Stat] = {}
    for blob in blobs:
        try:
            refs = [*usages(blob.source, root, api)]
        except SyntaxError, ValueError, RecursionError:
            continue
        for ref in refs:
            stat = stats.setdefault(ref.name, Stat())
            stat.refs += 1
            stat.files.add(blob.sha)
            stat.repos.add(blob.repo)
            stat.kinds[ref.kind.value] += 1
            stat.keywords.update(ref.keywords)
    return stats


@dataclass(slots=True)
class _Context:
    kinds: dict[int, Kind] = field(default_factory=dict)
    keywords: dict[int, tuple[str, ...]] = field(default_factory=dict)
    inner: set[int] = field(default_factory=set)


def _classify(tree: ast.AST) -> _Context:
    context = _Context()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            context.inner.add(id(node.value))
        elif isinstance(node, ast.Call):
            context.kinds[id(node.func)] = Kind.CALL
            context.keywords[id(node.func)] = tuple(
                k.arg for k in node.keywords if k.arg
            )
        if isinstance(node, ast.ClassDef):
            _mark(context.kinds, node.bases, Kind.SUBCLASS)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            _mark(context.kinds, node.decorator_list, Kind.DECORATOR)
        for annotation in _annotations(node):
            for part in ast.walk(annotation):
                _ = context.kinds.setdefault(id(part), Kind.ANNOTATION)
    return context


def _references(tree: ast.AST, context: _Context) -> Iterator[ast.Name | ast.Attribute]:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name | ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and id(node) not in context.inner
        ):
            yield node


def _annotations(node: ast.AST) -> Iterator[ast.expr]:
    match node:
        case (
            ast.AnnAssign(annotation=annotation)
            | ast.arg(
                annotation=ast.expr() as annotation,
            )
        ):
            yield annotation
        case (
            ast.FunctionDef(returns=ast.expr() as returns)
            | ast.AsyncFunctionDef(
                returns=ast.expr() as returns,
            )
        ):
            yield returns
        case _:
            return


def _shadowed(tree: ast.AST) -> set[str]:
    """Names the file binds itself, which therefore cannot mean the imported one.

    >>> sorted(_shadowed(ast.parse("def f(g, *, h=1):\\n    i = 2\\n")))
    ['f', 'g', 'h', 'i']
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        match node:
            case ast.Name(id=name, ctx=ast.Store()) | ast.arg(arg=name):
                names.add(name)
            case ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name):
                names.add(name)
            case ast.ClassDef(name=name):
                names.add(name)
            case ast.ExceptHandler(name=str(name)) | ast.MatchAs(name=str(name)):
                names.add(name)
            case ast.MatchStar(name=str(name)):
                names.add(name)
            case _:
                continue
    return names


def _mark(kinds: dict[int, Kind], nodes: Iterable[ast.expr], kind: Kind) -> None:
    kinds.update(dict.fromkeys(map(id, nodes), kind))


def _dotted(node: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _trim(qualname: Qualname, known: AbstractSet[Qualname]) -> Qualname | None:
    parts = qualname.split(".")
    for stop in range(len(parts), 0, -1):
        if (name := ".".join(parts[:stop])) in known:
            return name
    return None


def _source(node: ast.ImportFrom, root: str) -> str | None:
    if node.level or not node.module or not _under(node.module, root):
        return None
    return node.module


def _under(name: str, root: str) -> bool:
    return name == root or name.startswith(f"{root}.")


def _head(name: str) -> str:
    return name.partition(".")[0]
