import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sba_loanscout.config import load_rules  # noqa: E402
from sba_loanscout.extract import build_page  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def rules():
    return load_rules()


@pytest.fixture
def page_factory(rules):
    """Build a Page from a fixture file, classifying its page type from the URL."""

    def _make(fixture_name: str, url: str = "https://example.com/"):
        html = (FIXTURES / fixture_name).read_text(encoding="utf-8")
        return build_page(url, html, page_type=rules.page_type(url))

    return _make
