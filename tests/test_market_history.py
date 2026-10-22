from __future__ import annotations

import base64
import json
from contextlib import contextmanager
from datetime import date
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from stock_triage.db import WriterQueue, connect, run_migrations
from stock_triage.market import run_market_sync
from stock_triage.market.idx_history import HISTORY_PATH, history_url, parse_history
from stock_triage.market.importer import run_market_import

from .test_market_data import _JISDOR_PAGE, _FakeIDX
from .test_workbook_extraction import _xlsx_bytes

_FROM_FIELD = "ctl00$ctl50$g_x$ctl00$TextBoxFrom"


def _report_year(url: str) -> int:
    query = parse_qs(urlsplit(url).query)["query"][0]
    return int(json.loads(base64.b64decode(query))["year"])


def _report(year: int, closes: dict[date, float]) -> dict:
    # Shaped like IDX's LINK_DPS_COMPOSITE_STOCK_PRICE: volume first, then the composite.
    points = [{"x": day.isoformat(), "y": close, "value": "", "color": None} for day, close in sorted(closes.items()) if year - 2 <= day.year <= year]
    return {
        "title": "Indonesia Stock Exchange",
        "subtitle": f"Composite Stock Price Index and Stock Trading Volume<br>{year - 2} - {year}",
        "series": [
            {"seriesName": "Trading Volume (LHS)", "seriesData": [{**point, "y": 4000.0} for point in points]},
            {"seriesName": "Composite Index (RHS)", "seriesData": points},
        ],
    }


class _HistoryIDX(_FakeIDX):
    def __init__(self, closes: dict[date, float], history: dict[date, float]) -> None:
        super().__init__(closes)
        self.history, self.reports = history, []

    def fetch_url(self, url):
        if urlsplit(url).path != HISTORY_PATH:
            return super().fetch_url(url)
        self.reports.append(_report_year(url))
        return _report(self.reports[-1], self.history)


def _jisdor_by_year(requested: list[str]) -> httpx.Client:
    """A JISDOR export holding one rate, on the first day the search asked for."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=_JISDOR_PAGE)
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        if not form.get("__EVENTTARGET", "").endswith("ButtonExport"):
            return httpx.Response(200, text=_JISDOR_PAGE.replace("state-1", "state-2"))
        requested.append(form[_FROM_FIELD])
        day, month, year = form[_FROM_FIELD].split("/")
        export = _xlsx_bytes(
            [("Informasi Kurs Jisdor", None, [["NO", "Tanggal", "Kurs"], ["1", f"{int(month)}/{int(day)}/{year} 12:00:00 AM", "13.000,00"]])]
        )
        return httpx.Response(
            200, content=export, headers={"content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def _composite(tmp_config) -> dict[str, tuple[float, str]]:
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        return {
            row["trade_date"]: (row["value"], row["source_name"])
            for row in conn.execute(
                "SELECT trade_date, value, source_name FROM market_series_daily WHERE series_id = 'index:COMPOSITE'"
            )
        }
    finally:
        conn.close()


def test_history_url_asks_for_the_q4_report_and_parse_needs_the_composite():
    assert _report_year(history_url(2014)) == 2014
    query = parse_qs(urlsplit(history_url(2014)).query)
    assert query["urlName"] == ["LINK_DPS_COMPOSITE_STOCK_PRICE"]
    assert json.loads(base64.b64decode(query["query"][0])) == {"year": "2014", "month": "0", "quarter": 4, "type": "quarterly"}

    report = _report(2011, {date(2009, 1, 5): 1355.41, date(2011, 12, 30): 3821.99, date(2012, 1, 2): 3809.14})
    assert parse_history(report) == {date(2009, 1, 5): 1355.41, date(2011, 12, 30): 3821.99}
    # Reports before 2009 hold the series with no points.
    assert parse_history(_report(2008, {})) == {}
    with pytest.raises(ValueError, match="no Composite Index series"):
        parse_history({"series": [{"seriesName": "Trading Volume (LHS)", "seriesData": []}]})


def test_first_sync_fetches_the_history_before_2020_once(tmp_config, monkeypatch):
    monkeypatch.setattr("stock_triage.market.jisdor._PAUSE_S", 0)

    def _imported(conn):
        conn.execute(
            "INSERT INTO market_series_daily (series_id, trade_date, value, source_name, retrieved_at) "
            "VALUES ('index:COMPOSITE', '2009-01-05', 1300.0, 'import:ihsg.csv', 'then')"
        )

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        wq.submit(_imported)
    history = {
        date(2009, 1, 5): 1355.41,
        date(2013, 6, 13): 4515.0,
        date(2015, 8, 24): 4163.73,
        date(2018, 5, 2): 6012.24,
        date(2019, 12, 30): 6299.54,
        date(2020, 1, 2): 6283.58,
    }
    fake = _HistoryIDX({date(2020, 1, 2): 6283.581, date(2020, 1, 3): 6323.466}, history)

    @contextmanager
    def factory():
        yield fake

    requested: list[str] = []
    result = run_market_sync(
        tmp_config, start=date(2020, 1, 2), end=date(2020, 1, 3), idx_fetcher_factory=factory, jisdor_client=_jisdor_by_year(requested)
    )

    assert result.errors == []
    assert (result.history_days, result.history_first, result.history_last) == (5, date(2009, 1, 5), date(2019, 12, 30))
    # Three years per report, walking back until one comes back empty.
    assert fake.reports == [2020, 2017, 2014, 2011, 2008]
    composite = _composite(tmp_config)
    assert composite["2020-01-02"] == (6283.581, "idx_market")  # GetIndexSummary's close is kept
    assert composite["2009-01-05"] == (1355.41, "idx_statistics")  # and IDX's replaces an imported one
    assert composite["2015-08-24"] == (4163.73, "idx_statistics")
    # JISDOR from its first fixing, a year at a time, then the sync's own range.
    assert requested == ["20/05/2013"] + [f"01/01/{year}" for year in range(2014, 2020)] + ["02/01/2020"]

    fake.reports, requested[:] = [], []
    again = run_market_sync(
        tmp_config, start=date(2020, 1, 2), end=date(2020, 1, 3), idx_fetcher_factory=factory, jisdor_client=_jisdor_by_year(requested)
    )
    assert (again.history_days, fake.reports, requested) == (0, [], ["02/01/2020"])


def test_history_that_disagrees_with_idx_writes_nothing_and_is_retried(tmp_config, monkeypatch):
    monkeypatch.setattr("stock_triage.market.jisdor._PAUSE_S", 0)
    fake = _HistoryIDX({date(2020, 1, 2): 6283.581}, {date(2019, 12, 30): 6299.54, date(2020, 1, 2): 3141.0})

    @contextmanager
    def factory():
        yield fake

    def sync():
        return run_market_sync(
            tmp_config, start=date(2020, 1, 2), end=date(2020, 1, 2), idx_fetcher_factory=factory, jisdor_client=_jisdor_by_year([])
        )

    result = sync()

    assert result.history_days == 0
    assert result.errors == [
        "idx_statistics: 1 of the 1 days both sources hold differ by more than 2%; "
        "the statistics report was misread, so nothing was written"
    ]
    assert "2019-12-30" not in _composite(tmp_config)

    fake.history[date(2020, 1, 2)] = 6283.58
    assert sync().history_days == 1
    assert _composite(tmp_config)["2019-12-30"] == (6299.54, "idx_statistics")


def test_import_is_checked_against_and_never_overwrites_idx_statistics(tmp_config, tmp_path):
    def _history(conn):
        conn.execute(
            "INSERT INTO market_series_daily (series_id, trade_date, value, source_name, retrieved_at) "
            "VALUES ('index:COMPOSITE', '2009-01-05', 1355.41, 'idx_statistics', 'then')"
        )

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        wq.submit(_history)
    csv_path = tmp_path / "ihsg.csv"
    csv_path.write_text("Date,Close\n2008-12-30,1355.4\n2009-01-05,1356.0\n", encoding="utf-8")

    result = run_market_import(tmp_config, csv_path=csv_path)

    assert (result.inserted, result.skipped_present, result.idx_overlap, result.idx_disagreeing) == (1, 1, 1, 0)
    assert _composite(tmp_config)["2009-01-05"] == (1355.41, "idx_statistics")
