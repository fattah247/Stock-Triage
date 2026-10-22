from __future__ import annotations

from datetime import date, timedelta

import pytest

from stock_triage.__main__ import main
from stock_triage.db import WriterQueue, connect, run_migrations
from stock_triage.market import run_market_signals
from stock_triage.market.importer import MarketImportError, parse_closes_csv
from stock_triage.market.sync import _persist_day

_YAHOO = """﻿Date,Open,High,Low,Close,Adj Close,Volume
2008-01-02,2745.826050,2770.051025,2730.110107,2761.033936,2761.033936,0
2008-01-03,null,null,null,null,null,null
2008-01-04,2761.033936,2790.000000,2750.250000,2785.517090,2785.517090,0
"""
_STOOQ = """Date,Open,High,Low,Close,Volume
2008-01-02,2745.83,2770.05,2730.11,2761.03,1234567
2008-01-04,2761.03,2790,2750.25,2785.52,2345678
"""


def test_yahoo_and_stooq_layouts_give_the_same_closes():
    yahoo, stooq = parse_closes_csv(_YAHOO), parse_closes_csv(_STOOQ)

    assert yahoo.closes == pytest.approx({date(2008, 1, 2): 2761.033936, date(2008, 1, 4): 2785.51709})
    assert stooq.closes == {date(2008, 1, 2): 2761.03, date(2008, 1, 4): 2785.52}
    assert (yahoo.rows_read, yahoo.unparseable) == (3, 1)  # Yahoo writes null for a missing day
    assert (stooq.rows_read, stooq.unparseable) == (2, 0)


def test_indonesian_and_english_separators_and_iso_dates_only():
    indonesian = parse_closes_csv(
        "Date;Open;Close\n2008-01-02;2.745,83;2.761,03\n2008-01-04;2.761,03;2.785,5\n2008-01-07;2.785,5;1.234\n"
    )
    english = parse_closes_csv('Date,Close\n2008-01-02,"2,761.03"\n2008-01-04 00:00:00,"2,785.50"\n')
    dated = parse_closes_csv("Date,Close\n02/01/2008,2761.03\n2008-01-04,2785.5\n2008-01-04,2790\n2008-01-07,0\n")

    # ``1.234`` is settled by the rest of the file: a thousands point, not a decimal one.
    assert indonesian.closes == {date(2008, 1, 2): 2761.03, date(2008, 1, 4): 2785.5, date(2008, 1, 7): 1234.0}
    assert english.closes == {date(2008, 1, 2): 2761.03, date(2008, 1, 4): 2785.5}
    # Not ISO, and not positive, are unparseable; a repeated day keeps its first row.
    assert dated.closes == {date(2008, 1, 4): 2785.5}
    assert (dated.rows_read, dated.unparseable, dated.duplicate_dates) == (4, 2, 1)
    with pytest.raises(MarketImportError, match="no 'Price' column"):
        parse_closes_csv(_STOOQ, close_column="Price")


def _seed(tmp_config, rows: list[tuple[str, date, float, str]]) -> None:
    def _insert(conn):
        conn.executemany(
            "INSERT INTO market_series_daily (series_id, trade_date, value, source_name, retrieved_at) VALUES (?, ?, ?, ?, 'then')",
            [(series_id, day.isoformat(), value, source) for series_id, day, value, source in rows],
        )

    with WriterQueue(tmp_config) as wq:
        wq.submit(run_migrations)
        wq.submit(_insert)


def _composite_rows(tmp_config) -> dict[str, tuple[float, str]]:
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


def test_import_fills_missing_days_and_idx_always_wins(tmp_config, tmp_path, capsys):
    _seed(
        tmp_config,
        [
            ("index:COMPOSITE", date(2020, 1, 2), 6283.581, "idx_market"),
            ("index:COMPOSITE", date(2020, 1, 3), 6323.466, "idx_market"),
        ],
    )
    csv_path = tmp_path / "ihsg.csv"
    csv_path.write_text(
        "Date,Open,High,Low,Close,Adj Close,Volume\n"
        "12/26/2019,6300,6310,6290,6305,6305,0\n"
        "2019-12-27,null,null,null,null,null,null\n"
        "2019-12-30,6300,6310,6290,6299.539,6299.539,0\n"
        "2020-01-02,6300,6310,6290,6290.0,6290.0,0\n"
        "2020-01-03,6300,6330,6290,6330.0,6330.0,0\n"
        "2020-01-06,6320,6330,6250,6257.4,6257.4,0\n"
    )

    assert main(["market-import", "--csv", str(csv_path)]) == 0

    out = capsys.readouterr().out
    assert "rows=6 inserted=2 already_present=2 unparseable=2" in out
    assert "inserted days from 2019-12-30 to 2020-01-06" in out
    assert "checked against IDX on 2 shared days: 0 differ" in out
    rows = _composite_rows(tmp_config)
    assert rows["2020-01-02"] == (6283.581, "idx_market")  # IDX's close stands, not the file's 6290
    assert rows["2020-01-03"] == (6323.466, "idx_market")
    assert rows["2019-12-30"] == (6299.539, "import:ihsg.csv")
    assert rows["2020-01-06"] == (6257.4, "import:ihsg.csv")

    # Importing again changes nothing.
    assert main(["market-import", "--csv", str(csv_path)]) == 0
    assert "inserted=0 already_present=4 unparseable=2" in capsys.readouterr().out

    # And when market-sync later supplies an imported day, IDX's close replaces it.
    with WriterQueue(tmp_config) as wq:
        wq.submit(
            lambda conn: _persist_day(
                conn, source_name="idx_market", trade_date=date(2020, 1, 6), series={"index:COMPOSITE": 6257.402}, row_count=1
            )
        )
    assert _composite_rows(tmp_config)["2020-01-06"] == (6257.402, "idx_market")


def test_a_file_that_disagrees_with_idx_writes_nothing(tmp_config, tmp_path, capsys):
    days = [date(2020, 1, 2), date(2020, 1, 3), date(2020, 1, 6)]
    _seed(tmp_config, [("index:COMPOSITE", day, 6300.0, "idx_market") for day in days])
    # An LQ45 file saved under the wrong name: about 1,000, not 6,300.
    csv_path = tmp_path / "lq45.csv"
    csv_path.write_text(
        "Date,Close\n2019-12-30,1012.5\n" + "".join(f"{day.isoformat()},{1015 + n}\n" for n, day in enumerate(days))
    )

    assert main(["market-import", "--csv", str(csv_path)]) == 1

    assert "3 of the 3 days IDX also has differ" in capsys.readouterr().out
    assert set(_composite_rows(tmp_config)) == {day.isoformat() for day in days}
    assert main(["market-import", "--csv", str(tmp_path / "missing.csv")]) == 1
    assert "no file at" in capsys.readouterr().out


def _weekdays(start: date, end: date) -> list[date]:
    return [start + timedelta(days=n) for n in range((end - start).days + 1) if (start + timedelta(days=n)).weekday() < 5]


def test_market_signals_read_imported_closes_before_2020(tmp_config, tmp_path):
    history = _weekdays(date(2018, 11, 1), date(2019, 12, 31))
    # Calm, then a 2018-style sell-off of about 14% over ten sessions in May 2019.
    drop_from = history.index(date(2019, 5, 1))
    closes = []
    for n, day in enumerate(history):
        level = 6000.0 if n < drop_from else max(5200.0, 6000.0 - 80.0 * (n - drop_from + 1))
        closes.append(round(level * (1.004 if n % 2 else 0.996), 2))
    csv_path = tmp_path / "stooq_jci.csv"
    csv_path.write_text(
        "Date,Open,High,Low,Close,Volume\n"
        + "".join(f"{day.isoformat()},{close},{close},{close},{close},0\n" for day, close in zip(history, closes))
    )
    idx_days = _weekdays(date(2020, 1, 2), date(2020, 1, 8))
    _seed(
        tmp_config,
        [
            row
            for n, day in enumerate(idx_days)
            for row in (
                ("index:COMPOSITE", day, 5300.0 + n, "idx_market"),
                ("breadth:advancers", day, 200.0, "idx_market"),
                ("breadth:decliners", day, 300.0, "idx_market"),
                ("flow:foreign_net_value_est", day, -1e11, "idx_market"),
            )
        ],
    )
    assert main(["market-import", "--csv", str(csv_path)]) == 0

    result = run_market_signals(tmp_config)

    assert result.days == len(history) + len(idx_days)
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        levels = {
            (row["trade_date"], row["signal_name"]): row["level"]
            for row in conn.execute("SELECT trade_date, signal_name, level FROM market_signal_runs")
        }
    finally:
        conn.close()
    trough = date(2019, 5, 14).isoformat()  # ten sessions into the fall, at 5,200
    assert levels[(trough, "drawdown")] == "stressed"
    assert levels[(trough, "vol_ratio")] != "no_data"
    no_close_signals = ("foreign_sell_streak", "decline_share", "usdidr_change_20d", "exchange_notices")
    sector_signal = next(name for (day, name) in levels if day == trough and name.startswith("sector_"))
    for name in (*no_close_signals, sector_signal):
        assert levels[(trough, name)] == "no_data", name
    # IDX's own days carry breadth and foreign flow as before.
    last = idx_days[-1].isoformat()
    assert levels[(last, "decline_share")] != "no_data"
    assert levels[(last, "foreign_sell_streak")] != "no_data"
