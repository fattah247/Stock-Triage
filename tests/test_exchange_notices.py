from __future__ import annotations

from contextlib import contextmanager
from datetime import date

from stock_triage.db import connect
from stock_triage.market import run_market_signals, run_market_sync
from stock_triage.market.exchange_notices import SUSPENSION_PATH, UMA_PATH, parse_suspensions, parse_uma

from .test_market_data import _FakeIDX, _jisdor_client, _seed_series, _weekdays

# Shaped like IDX's GetSuspension and GetUMA responses (docs/phase2_market_recon.md).
_SUSPENSIONS = {
    "ResultCount": 3,
    "Results": [
        {"Data_Download": "/StaticData/a.pdf", "Date": "2026-10-16T00:00:00", "Info_Type": "SPT", "Judul": "Penghentian Sementara Perdagangan Saham PT A Tbk. (AAAA)", "Kode": "AAAA"},
        {"Data_Download": "/StaticData/b.pdf", "Date": "2026-10-16T08:39:47", "Info_Type": "UPT", "Judul": "Pembukaan Penghentian Sementara Perdagangan Efek PT B Tbk. (BBBB)", "Kode": "BBBB"},
        {"Data_Download": "/StaticData/c.pdf", "Date": "2026-10-15T00:00:00", "Info_Type": "XYZ", "Judul": "Other", "Kode": "CCCC"},
    ],
}
_UMA = {
    "ResultCount": 1,
    "Results": [
        {"AnnouncementNo": "Peng-UMA-00001/BEI.WAS/10-2026", "Attachment": "/Portals/0/u.pdf", "CompanyID": "DDDD", "CompanyName": "D Tbk.", "Judul": "UMA atas Saham PT D Tbk. (DDDD)", "Status": "A", "UMADate": "2026-10-16T00:00:00", "UMAID": "20261016000001"}
    ],
}


def test_notices_parse_into_suspensions_lifts_and_uma():
    suspensions = parse_suspensions(_SUSPENSIONS)
    uma = parse_uma(_UMA)

    assert [(n["kind"], n["ticker"], n["notice_date"]) for n in suspensions] == [
        ("suspend", "AAAA", "2026-10-16"),
        ("unsuspend", "BBBB", "2026-10-16"),
    ]
    assert suspensions[0]["url"] == "https://www.idx.co.id/StaticData/a.pdf"
    assert (uma[0]["kind"], uma[0]["ticker"], uma[0]["announcement_no"]) == ("uma", "DDDD", "Peng-UMA-00001/BEI.WAS/10-2026")


def test_market_sync_stores_notices_and_daily_counts(tmp_config, monkeypatch):
    monkeypatch.setattr("stock_triage.market.jisdor._PAUSE_S", 0)
    closes = {date(2026, 10, 15): 5863.78, date(2026, 10, 16): 5185.1}
    fake = _FakeIDX(closes, notices={SUSPENSION_PATH: _SUSPENSIONS, UMA_PATH: _UMA})

    @contextmanager
    def factory():
        yield fake

    result = run_market_sync(
        tmp_config, start=date(2026, 10, 15), end=date(2026, 10, 16), idx_fetcher_factory=factory, jisdor_client=_jisdor_client([])
    )
    again = run_market_sync(
        tmp_config, start=date(2026, 10, 15), end=date(2026, 10, 17), idx_fetcher_factory=factory, jisdor_client=_jisdor_client([])
    )

    assert result.notices_new == 3 and again.notices_new == 0
    conn = connect(tmp_config.paths.db_path, read_only=True)
    try:
        counts = {
            (row["series_id"], row["trade_date"]): row["value"]
            for row in conn.execute("SELECT series_id, trade_date, value FROM market_series_daily WHERE series_id LIKE 'notices:%'")
        }
    finally:
        conn.close()
    # Zeros are explicit on covered trading days, so the usual level is honest.
    assert counts == {
        ("notices:suspend", "2026-10-16"): 1.0,
        ("notices:uma", "2026-10-16"): 1.0,
    }


def test_a_wave_of_suspensions_raises_the_notice_signal(tmp_config):
    days = _weekdays(date(2026, 1, 5), 200)
    suspend = {day: (1.0 if i % 2 else 0.0) for i, day in enumerate(days)}
    for day in days[-5:]:
        suspend[day] = 6.0  # 30 in five sessions against about 2.5 usually
    _seed_series(
        tmp_config,
        {"index:COMPOSITE": {day: 7000.0 for day in days}, "notices:suspend": suspend, "notices:uma": {day: 0.0 for day in days}},
    )

    latest = run_market_signals(tmp_config).latest

    reading = next(r for r in latest.readings if r.signal_name == "exchange_notices")
    assert reading.level == "crisis" and reading.value >= 5
    assert reading.text.startswith("IDX suspended or flagged 30 stocks in 5 sessions")
