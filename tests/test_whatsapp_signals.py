"""Tests for the WhatsApp signal feed, the live-account P&L split, the days-running
counter, and the series-suffix normalization that fixes 'Unmanaged' manual buys.

All pure logic — no DB / no gateway I/O. The gateway send path is defensive and
config-gated (default OFF), so `whatsapp.configured()` is False under the test env and
`broadcast()`/`send_text()` are no-ops that never touch the network."""

from __future__ import annotations

import os

os.environ.setdefault("PG_PASSWORD", "test")
os.environ.setdefault("ANGEL_API_KEY", "test")
os.environ.setdefault("ANGEL_CLIENT_CODE", "test")
os.environ.setdefault("ANGEL_PASSWORD", "test")
os.environ.setdefault("ANGEL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
os.environ.setdefault("DASHBOARD_PASSWORD", "test")
os.environ.setdefault("SESSION_SECRET", "test-secret-do-not-use")

from datetime import datetime, timedelta, timezone  # noqa: E402

from src.core import metrics, whatsapp  # noqa: E402
from src.engine.real_executor import engine_symbol_root  # noqa: E402


# ---------- days-running counter ----------

def test_days_live_none_and_future_are_zero():
    assert metrics.days_live(None) == 0
    future = datetime.now(timezone.utc) + timedelta(days=5)
    assert metrics.days_live(future) == 0


def test_days_live_counts_whole_days():
    past = datetime.now(timezone.utc) - timedelta(days=10, hours=2)
    assert metrics.days_live(past) == 10


# ---------- realized / unrealized split ----------

def test_split_pnl_decomposes_total():
    s = metrics.split_pnl(net_worth=22_000, invested=20_000, unrealized=500)
    assert s["total_pnl"] == 2_000
    assert s["realized_pnl"] == 1_500          # total − unrealized
    assert s["unrealized_pnl"] == 500
    assert round(s["pct"], 4) == 10.0


def test_split_pnl_handles_loss_and_zero_invested():
    loss = metrics.split_pnl(net_worth=18_000, invested=20_000, unrealized=-1_200)
    assert loss["total_pnl"] == -2_000
    assert loss["realized_pnl"] == -800        # -2000 − (-1200)
    assert metrics.split_pnl(0, 0, 0)["pct"] is None


# ---------- signal formatting ----------

def _trade(**kw):
    base = {"side": "BUY", "qty": 3, "price": 1290.5, "symbol": "RELIANCE",
            "reason": "entry_scan_11:00_drop_-3%", "date": "2026-07-16", "time": "11:05"}
    base.update(kw)
    return base


def test_format_signal_placeable_is_clean():
    txt = whatsapp.format_signal(_trade(), portfolio_name="S404_live_sip_20k",
                                 placeable=True, note=None, now_ist_str="12:05 IST")
    assert "BUY" in txt and "RELIANCE" in txt and "S404_live_sip_20k" in txt
    assert "manual" not in txt.lower()


def test_format_signal_unplaceable_flags_manual_action():
    txt = whatsapp.format_signal(
        _trade(symbol="XYZ", qty=1, price=100.0), portfolio_name="pf",
        placeable=False, note="surveillance/cautionary block (e.g. AB4036)",
        now_ist_str="12:00 IST")
    assert "AB4036" in txt
    assert "manually" in txt.lower()


def test_format_signal_sell_uses_sell_label():
    txt = whatsapp.format_signal(_trade(side="SELL", reason="target_+32%_tier2"),
                                 portfolio_name="pf", placeable=True, note=None,
                                 now_ist_str="09:15 IST")
    assert "SELL" in txt and "target_+32%_tier2" in txt


# ---------- gateway is OFF by default (no accidental network sends) ----------

def test_whatsapp_not_configured_in_test_env():
    assert whatsapp.configured() is False
    assert whatsapp.DEFAULT_GROUP_JID == "120363411936940548@g.us"


# ---------- 'Unmanaged' fix: series-suffix normalization ----------

def test_engine_symbol_root_strips_known_series():
    assert engine_symbol_root("RELIANCE-EQ") == "RELIANCE"
    assert engine_symbol_root("XYZ-BE") == "XYZ"      # surveillance / T2T series
    assert engine_symbol_root("ABC-BZ") == "ABC"
    assert engine_symbol_root("FOO-ST") == "FOO"


def test_engine_symbol_root_leaves_plain_and_unknown_untouched():
    assert engine_symbol_root("PLAINSYM") == "PLAINSYM"
    assert engine_symbol_root("SOME-XYZ") == "SOME-XYZ"   # not a known series suffix
    assert engine_symbol_root("") == ""
    assert engine_symbol_root(None) == ""
