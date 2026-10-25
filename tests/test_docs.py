"""The READMEs and docs/architecture.md stay in line with the code.

Both configuration tables list every setting in the order of ``config.py`` with its real
default, every relative link points at a file in the repository, and every test the docs send
the reader to exists.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from paidclub.config import Settings

ROOT = Path(__file__).resolve().parent.parent
READMES = ("README.md", "README.ru.md")
DOCS = (*READMES, "docs/architecture.md")

TABLE_ROW = re.compile(r"^\| `(PAIDCLUB_[A-Z0-9_]+)` \| ([^|]+) \|", re.MULTILINE)
LINK = re.compile(r"\]\(([^)\s]+)\)")
TEST_NAME = re.compile(r"`(?:tests/[\w/]+\.py::)?(test_\w+)`")
#: Defaults the tables describe in words: JSON plans, and a secret made up at every start.
DESCRIBED_IN_WORDS = {"plans", "fake_webhook_secret"}


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def documented_default(field: str) -> str | None:
    """The default as the tables write it, or ``None`` where they put it in words."""
    if field in DESCRIBED_IN_WORDS:
        return None
    value = Settings.model_fields[field].get_default(call_default_factory=True)
    if value is None or value == []:
        return None
    if isinstance(value, bool):
        return f"`{int(value)}`"
    if isinstance(value, list):
        return "`" + ",".join(v if isinstance(v, str) else f"{v:g}" for v in value) + "`"
    if isinstance(value, int | float):
        return f"`{value:g}`"
    return f"`{value}`"


@pytest.mark.parametrize("name", READMES)
def test_the_configuration_table_lists_every_setting_in_the_order_of_config_py(name: str) -> None:
    documented = [variable for variable, _ in TABLE_ROW.findall(read(name))]
    assert documented == [f"PAIDCLUB_{field.upper()}" for field in Settings.model_fields]


@pytest.mark.parametrize("name", READMES)
def test_the_configuration_table_shows_the_real_defaults(name: str) -> None:
    cells = dict(TABLE_ROW.findall(read(name)))
    for field in Settings.model_fields:
        expected = documented_default(field)
        if expected is not None:
            assert cells[f"PAIDCLUB_{field.upper()}"].strip() == expected, field


@pytest.mark.parametrize("name", DOCS)
def test_relative_links_point_at_files_in_the_repository(name: str) -> None:
    path = ROOT / name
    for target in LINK.findall(read(name)):
        if target.startswith(("http://", "https://", "#")):
            continue
        assert (path.parent / target.split("#")[0]).exists(), f"{name}: {target}"


def test_the_tests_the_docs_point_to_exist() -> None:
    defined = set(
        re.findall(
            r"^\s*(?:async )?def (test_\w+)\(",
            "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "tests").glob("*.py")),
            re.MULTILINE,
        )
    )
    cited = {test for name in DOCS for test in TEST_NAME.findall(read(name))}
    assert cited, "the docs cite no tests at all"
    assert cited <= defined, sorted(cited - defined)
