import ast
import sys
from collections.abc import Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

type Qualname = str

_SUFFIXES = (".pyi", ".py")


def symbols(source: str) -> frozenset[str]:
    """Public names a module exports, from ``__all__`` when present.

    >>> sorted(symbols("__all__ = ['a', 'b']\\ndef c(): ...\\n"))
    ['a', 'b']
    >>> sorted(symbols("def c(): ...\\nclass D: ...\\n_e = 1\\n"))
    ['D', 'c']
    >>> sorted(symbols("from x import y as y, z\\n"))
    ['y']
    """
    tree = ast.parse(source)
    if (declared := _declared(tree)) is not None:
        return frozenset(declared)
    return frozenset(name for name in _defined(tree) if not name.startswith("_"))


def _declared(tree: ast.Module) -> list[str] | None:
    found: list[str] | None = None
    for node in tree.body:
        match node:
            case ast.Assign(targets=[ast.Name(id="__all__")], value=value):
                pass
            case ast.AugAssign(target=ast.Name(id="__all__"), value=value):
                pass
            case ast.AnnAssign(
                target=ast.Name(id="__all__"), value=ast.expr() as value
            ):
                pass
            case _:
                continue
        with suppress(ValueError):
            found = [*(found or []), *ast.literal_eval(value)]
    return found


def _defined(tree: ast.Module) -> Iterator[str]:
    for node in tree.body:
        match node:
            case ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name):
                yield name
            case ast.ClassDef(name=name):
                yield name
            case ast.Assign(targets=targets):
                yield from (t.id for t in targets if isinstance(t, ast.Name))
            case ast.AnnAssign(target=ast.Name(id=name)):
                yield name
            case ast.ImportFrom(names=aliases):
                yield from (a.name for a in aliases if a.asname == a.name)
            case _:
                continue


def is_public(qualname: Qualname) -> bool:
    """Whether every component of *qualname* is public.

    >>> is_public("scipy.special.gamma"), is_public("scipy.special._ufuncs")
    (True, False)
    """
    return not any(part.startswith("_") for part in qualname.split("."))


@dataclass(frozen=True, slots=True)
class Api:
    root: str
    modules: Mapping[Qualname, frozenset[str]]

    @property
    def public(self) -> frozenset[Qualname]:
        return frozenset(
            f"{module}.{symbol}"
            for module, names in self.modules.items()
            if is_public(module)
            for symbol in names
            if not symbol.startswith("_")
        )

    def within(self, scope: Qualname) -> frozenset[Qualname]:
        return frozenset(
            name
            for name in self.public
            if name == scope or name.startswith(f"{scope}.")
        )


def resolve(target: str) -> tuple[Path, Qualname]:
    """Locate the package directory for *target* and the dotted scope it asks about."""
    if target.startswith(".") or "/" in target or "\\" in target:
        base = Path(target).resolve()
        if not base.is_dir():
            msg = f"{target!r} is not a directory"
            raise LookupError(msg)
        return base, _unstub(base.name)

    head, _, rest = target.partition(".")
    root = _unstub(head)
    return _locate(root), f"{root}.{rest}" if rest else root


def _unstub(name: str) -> str:
    return name.removesuffix("-stubs").removesuffix("_stubs").replace("-", "_")


def _locate(root: str) -> Path:
    for entry in sys.path:
        base = Path(entry or ".")
        for name in (f"{root}-stubs", root):
            if (path := base / name).is_dir():
                return path
    msg = f"no package or stubs found for {root!r} on sys.path"
    raise LookupError(msg)


def read(base: Path, root: str) -> Api:
    """Index every module under *base*, preferring stubs over sources."""
    paths: dict[Qualname, Path] = {}
    for suffix in _SUFFIXES:
        for path in sorted(base.rglob(f"*{suffix}")):
            parts = path.relative_to(base).with_suffix("").parts
            parts = parts[:-1] if parts[-1] == "__init__" else parts
            _ = paths.setdefault(".".join((root, *parts)), path)

    modules: dict[Qualname, frozenset[str]] = {}
    for module, path in paths.items():
        with suppress(SyntaxError, UnicodeDecodeError):
            modules[module] = symbols(path.read_text(encoding="utf-8"))
    return Api(root, modules)
