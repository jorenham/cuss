import pytest

from cuss._usage import Blob, Kind, tally, usages

API = {
    "scipy": frozenset(),
    "scipy.special": frozenset({"gamma", "erf"}),
    "scipy.stats": frozenset({"norm"}),
}


def kinds(source: str) -> dict[str, str]:
    return {ref.name: ref.kind.value for ref in usages(source, "scipy", API)}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from scipy.special import gamma\ngamma(1)", "scipy.special.gamma"),
        ("from scipy.special import gamma as g\ng(1)", "scipy.special.gamma"),
        ("import scipy.special\nscipy.special.gamma(1)", "scipy.special.gamma"),
        ("import scipy.special as sp\nsp.gamma(1)", "scipy.special.gamma"),
        ("from scipy import special\nspecial.gamma(1)", "scipy.special.gamma"),
        ("from scipy.special import *\ngamma(1)", "scipy.special.gamma"),
    ],
)
def test_binding_forms_resolve(source: str, expected: str) -> None:
    assert kinds(source) == {expected: Kind.CALL.value}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from scipy.stats import norm\nnorm(1)", Kind.CALL),
        ("from scipy.stats import norm\nclass A(norm): ...", Kind.SUBCLASS),
        ("from scipy.stats import norm\n@norm\ndef f(): ...", Kind.DECORATOR),
        ("from scipy.stats import norm\ndef f(x: norm): ...", Kind.ANNOTATION),
        ("from scipy.stats import norm\ndef f() -> norm: ...", Kind.ANNOTATION),
        ("from scipy.stats import norm\nx: list[norm] = []", Kind.ANNOTATION),
        ("from scipy.stats import norm\nf(norm)", Kind.REFERENCE),
    ],
)
def test_usage_kinds(source: str, expected: Kind) -> None:
    assert kinds(source) == {"scipy.stats.norm": expected.value}


def test_a_bare_import_is_not_a_use() -> None:
    assert kinds("from scipy.stats import norm") == {}
    assert kinds("import scipy.special") == {}


def test_attribute_chains_trim_to_known_symbols() -> None:
    source = "from scipy.special import gamma\ngamma(1).shape\nscipy_other.gamma"
    assert kinds(source) == {"scipy.special.gamma": Kind.CALL.value}


def test_unrelated_imports_are_ignored() -> None:
    assert kinds("from numpy import gamma\ngamma(1)") == {}


def test_stores_are_not_counted() -> None:
    assert kinds("from scipy.special import gamma\ngamma = 1") == {}


def test_keywords_are_collected_per_call() -> None:
    source = "from scipy.special import gamma\ngamma(1, out=None, dtype=float)"
    (ref,) = usages(source, "scipy", API)
    assert ref.keywords == ("out", "dtype")


def test_tally_counts_files_and_repos_once_per_reference() -> None:
    source = "from scipy.special import gamma\ngamma(1)\ngamma(2)"
    blobs = [Blob("a/one", "sha1", source), Blob("b/two", "sha2", source)]
    stats = tally(blobs, "scipy", API)
    stat = stats["scipy.special.gamma"]
    assert (stat.refs, len(stat.files), len(stat.repos)) == (4, 2, 2)
    assert stat.rank == (2, 4)


def test_tally_skips_files_that_do_not_parse() -> None:
    good = "import scipy.stats\nscipy.stats.norm(1)"
    blobs = [Blob("a", "sha1", "def ("), Blob("b", "sha2", good)]
    assert set(tally(blobs, "scipy", API)) == {"scipy.stats.norm"}


def test_locally_defined_names_shadow_star_imports() -> None:
    source = "from scipy.special import *\ndef gamma(): ...\ngamma()"
    assert kinds(source) == {}


def test_locally_defined_names_shadow_explicit_imports() -> None:
    source = "from scipy.special import gamma\ndef gamma(): ...\ngamma(1)"
    assert kinds(source) == {}


@pytest.mark.parametrize(
    "source",
    [
        "from scipy.stats import norm\ndef f(norm):\n    return norm(1)\n",
        "from scipy.stats import norm\ntry:\n    f()\nexcept E as norm:\n    norm(1)\n",
        "from scipy.stats import norm\nfor norm in xs:\n    norm(1)\n",
    ],
)
def test_locally_bound_names_are_not_attributed(source: str) -> None:
    assert kinds(source) == {}


def test_a_parameter_elsewhere_does_not_hide_real_usage() -> None:
    source = "from scipy.stats import norm\ndef f(x):\n    return norm(x)\n"
    assert kinds(source) == {"scipy.stats.norm": Kind.CALL.value}


def test_corpus_warnings_do_not_leak() -> None:
    source = "from scipy.stats import norm\nnorm('\\d+')\n"
    assert kinds(source) == {"scipy.stats.norm": Kind.CALL.value}
