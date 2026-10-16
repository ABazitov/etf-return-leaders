"""Live check against efdsearch.senate.gov. Run explicitly: .venv/bin/python -m pytest -m e2e tests/e2e/test_senate_live.py"""
from datetime import date, timedelta

import pytest

from app.sources.senate import SenateClient, parse_ptr_html

pytestmark = pytest.mark.e2e


def test_recent_senate_reports_parse():
    with SenateClient() as client:
        listings = client.search(date.today() - timedelta(days=60))
        assert listings
        ptr = next(l for l in listings if l.kind == "ptr")
        trades = parse_ptr_html(client.page(ptr.url))
        assert trades and all(t.amount_text.startswith(("$", "Over")) for t in trades)
