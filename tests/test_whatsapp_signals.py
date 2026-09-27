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

from datetime import date, datetime, timedelta, timezone  # noqa: E402

from src.core import metrics, whatsapp  # noqa: E402
from src.engine.real_executor import (  # noqa: E402
    _logical_key_from_trade, cash_flow_residual, cumulative_split_factor, engine_symbol_root,
    intent_key, reconcile_kind, split_adjust_position, symbol_lag_days,
    build_shadow_cash, extract_shadow_buys, shadow_signal_key, shadow_buyable,
    SHADOW_DAY_CASH, effective_entry_params, estimate_position_size, entry_requirements,
    entry_verdict,
)


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

def _order(**kw):
    base = {"side": "BUY", "qty": 3, "price": 1290.5, "symbol": "RELIANCE",
            "reason": "entry_scan_11:00_drop_-3%", "status": "open", "error": None}
    base.update(kw)
    return base


def test_format_order_placed_is_clean():
    txt = whatsapp.format_order_event(_order(), now_ist_str="12:05 IST")
    assert "BUY" in txt and "RELIANCE" in txt and "placed" in txt.lower()
    assert "reject" not in txt.lower()


def test_format_order_rejected_flags_manual_action():
    txt = whatsapp.format_order_event(
        _order(symbol="PARACABLES", qty=45, status="error",
               error="Angel placeOrder rejected [AB4036]: cautionary"),
        now_ist_str="12:00 IST")
    assert "AB4036" in txt
    assert "REJECTED" in txt
    assert "manually" in txt.lower()


def test_format_order_sell_uses_sell_label():
    txt = whatsapp.format_order_event(
        _order(side="SELL", reason="target_+32%_tier2", status="open"),
        now_ist_str="09:15 IST")
    assert "SELL" in txt and "Live bot" in txt


def test_format_quarantine_skip_nudges_manual_buy():
    # A benched (never-placed) BUY: the bot won't fire it, so the message must clearly say
    # 'skipped' + 'buy it manually', carry the qty/symbol, and surface the bench code.
    skip = {"symbol": "PARACABLES", "side": "BUY", "qty": 45, "price": 118.4,
            "reason": "entry_scan_14:00_drop_-6%"}
    txt = whatsapp.format_quarantine_skip(skip, reason_code="AB4036", now_ist_str="14:02 IST")
    assert "PARACABLES" in txt and "45" in txt
    assert "AB4036" in txt
    assert "manually" in txt.lower()
    assert "reject" not in txt.lower()      # nothing was attempted → not a rejection


def test_format_quarantine_skip_without_code():
    txt = whatsapp.format_quarantine_skip(
        {"symbol": "XYZ", "side": "BUY", "qty": 1, "price": 10.0},
        reason_code=None, now_ist_str="10:00 IST")
    assert "XYZ" in txt and "manually" in txt.lower()


# ---------- corporate-action guard: held-position staleness ----------

def _dt(y, mo, d, h=15, mi=25):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def test_symbol_lag_days_fresh_is_zero():
    now = _dt(2026, 8, 20)
    assert symbol_lag_days(now, now) == 0                       # trades in step with market
    assert symbol_lag_days(now, _dt(2026, 8, 19)) == 1          # one day behind


def test_symbol_lag_days_flags_a_halt():
    # Symbol last priced Aug 12; universe fresh to Aug 20 → 8-day lag (suspension/merger).
    assert symbol_lag_days(_dt(2026, 8, 20), _dt(2026, 8, 12)) == 8


def test_symbol_lag_days_never_priced_is_large():
    assert symbol_lag_days(_dt(2026, 8, 20), None) >= 1000


def test_symbol_lag_days_outage_and_edge_cases_dont_flag():
    # Whole universe stale (platform outage) → universe_latest is old too → lag stays ~0.
    assert symbol_lag_days(_dt(2026, 8, 12), _dt(2026, 8, 12)) == 0
    assert symbol_lag_days(None, _dt(2026, 8, 12)) == 0         # no candles anywhere → never flag
    # A symbol somehow ahead of the universe max clamps to 0 (never negative).
    assert symbol_lag_days(_dt(2026, 8, 12), _dt(2026, 8, 20)) == 0


def test_format_suspension_alert_is_a_manual_nudge():
    txt = whatsapp.format_suspension_alert("INOXGREEN", days_lag=6, qty=5, now_ist_str="15:25 IST")
    assert "INOXGREEN" in txt and "5" in txt and "6" in txt
    assert "manually" in txt.lower()
    assert "HOLD" in txt


# ---------- regression: benched-skip nudge must NOT spam every tick ----------

def _skip_trade(price, time, qty):
    # Same logical action (date·symbol·side·reason), only the live price/time/qty wiggle.
    return {"date": "2026-09-01", "time": time, "symbol": "INOXGREEN", "side": "BUY",
            "qty": qty, "price": price, "reason": "entry_scan_10:00_drop_-3%"}


def test_benched_skip_dedup_key_is_stable_across_ticks():
    # The INOXGREEN-every-minute spam: emit_quarantine_signals must dedup on the price/time-
    # free LOGICAL key, so the same benched signal at 10:41/10:42/10:43 collapses to one nudge.
    a = _logical_key_from_trade(_skip_trade(161.67, "10:41", 11))
    b = _logical_key_from_trade(_skip_trade(160.56, "10:42", 11))
    c = _logical_key_from_trade(_skip_trade(161.25, "10:43", 12))
    assert a == b == c                                   # one logical signal → one dedup key
    assert "161" not in a and "10:41" not in a           # price/time are NOT in the key
    # The full intent_key DOES carry price/time — deduping on IT is what spammed.
    assert len({intent_key(_skip_trade(161.67, "10:41", 11)),
                intent_key(_skip_trade(160.56, "10:42", 11))}) == 2


def test_benched_skip_distinct_reasons_still_nudge_separately():
    scan = _logical_key_from_trade(_skip_trade(161.67, "10:41", 11))
    pyr = _logical_key_from_trade({**_skip_trade(155.0, "11:30", 11),
                                   "reason": "pyramid_close_-8%_lvl1"})
    assert scan != pyr                                   # a genuinely different action still pings


# ---------- split / bonus back-adjustment factor ----------

def test_split_factor_no_actions_is_one():
    assert cumulative_split_factor(date(2024, 1, 1), []) == 1.0


def test_split_factor_divides_only_pre_ex_date_bars():
    acts = [(date(2024, 10, 28), 5.0)]                   # 5:1 split, ex-date Oct 28
    assert cumulative_split_factor(date(2024, 10, 27), acts) == 5.0   # before → ÷5 (price), ×5 (vol)
    assert cumulative_split_factor(date(2024, 10, 28), acts) == 1.0   # on ex-date → real price
    assert cumulative_split_factor(date(2024, 11, 1), acts) == 1.0    # after → untouched


def test_split_factor_compounds_multiple_actions():
    acts = [(date(2023, 6, 1), 2.0), (date(2024, 10, 28), 5.0)]       # a 2:1 then a 5:1
    assert cumulative_split_factor(date(2023, 1, 1), acts) == 10.0    # before both → ×2 ×5
    assert cumulative_split_factor(date(2024, 1, 1), acts) == 5.0     # between → only the later 5:1
    assert cumulative_split_factor(date(2025, 1, 1), acts) == 1.0     # after both → 1


def test_split_factor_ignores_bad_ratios():
    acts = [(date(2024, 10, 28), 0.0), (date(2024, 10, 28), None), (date(2024, 10, 28), -3.0)]
    assert cumulative_split_factor(date(2020, 1, 1), acts) == 1.0     # 0 / None / negative skipped


# ---------- adopted-position basis: back-adjust a pre-split snapshot ----------

def test_split_adjust_position_pre_split_rebases_and_conserves_notional():
    # 10 shares @ ₹1000 adopted before a 5:1 split → 50 @ ₹200 (candles are ₹200-space).
    acts = [(date(2024, 10, 28), 5.0)]
    qty, avg = split_adjust_position(date(2024, 10, 1), 10, 1000.0, acts)
    assert qty == 50 and avg == 200.0
    assert qty * avg == 10 * 1000.0        # notional conserved → engine cash debit unchanged


def test_split_adjust_position_on_or_after_ex_date_is_untouched():
    acts = [(date(2024, 10, 28), 5.0)]
    # Snapshot already in post-split space (broker reports the split shares) → leave it be.
    assert split_adjust_position(date(2024, 10, 28), 50, 200.0, acts) == (50, 200.0)
    assert split_adjust_position(date(2024, 11, 5), 50, 200.0, acts) == (50, 200.0)


def test_split_adjust_position_no_actions_is_identity():
    assert split_adjust_position(date(2024, 1, 1), 7, 314.5, []) == (7, 314.5)


def test_split_adjust_position_compounds_and_rounds_qty():
    # A 2:1 then a 5:1, snapshot before both → qty ×10, price ÷10. Odd lot rounds to nearest.
    acts = [(date(2023, 6, 1), 2.0), (date(2024, 10, 28), 5.0)]
    qty, avg = split_adjust_position(date(2023, 1, 1), 3, 990.0, acts)
    assert qty == 30 and avg == 99.0


# ---------- cash-flow reconciliation: unrecorded deposit / withdrawal ----------

def test_cash_flow_residual_no_trades_is_the_raw_delta():
    assert cash_flow_residual(18_000, 28_000, []) == 10_000.0    # +10k appeared, nothing traded
    assert cash_flow_residual(28_000, 18_000, []) == -10_000.0   # -10k left


def test_cash_flow_residual_nets_out_trades():
    # A sell added 5000, a buy took 2000 → free cash should have risen 3000 on its own.
    trades = [("SELL", 5_000.0), ("BUY", 2_000.0)]
    assert cash_flow_residual(10_000, 13_000, trades) == 0.0     # fully explained by trading
    # Same trades, but cash rose 13000 → 10000 unexplained on top (a deposit).
    assert cash_flow_residual(10_000, 23_000, trades) == 10_000.0


def test_cash_flow_residual_ignores_unknown_sides_and_casts_strings():
    trades = [("sell", "1000"), ("hold", 999), ("BUY", "400")]   # +1000 −400, 'hold' ignored
    assert cash_flow_residual(0, 600, trades) == 0.0


def test_reconcile_kind_thresholds():
    assert reconcile_kind(10_000, 1_000) == "deposit"
    assert reconcile_kind(-10_000, 1_000) == "withdrawal"
    assert reconcile_kind(500, 1_000) is None                    # brokerage/slippage noise
    assert reconcile_kind(-500, 1_000) is None
    assert reconcile_kind(1_000, 1_000) == "deposit"             # boundary is inclusive


def test_format_cash_reconcile_nudges_confirmation():
    dep = whatsapp.format_cash_reconcile(amount=10_000, kind="deposit", now_ist_str="10:00 IST")
    assert "10,000" in dep and "confirm" in dep.lower() and "profit" in dep.lower()
    wd = whatsapp.format_cash_reconcile(amount=-4_000, kind="withdrawal", now_ist_str="10:00 IST")
    assert "4,000" in wd and "confirm" in wd.lower()
    # a deep link is embedded only when a dashboard URL is supplied
    linked = whatsapp.format_cash_reconcile(amount=5_000, kind="deposit", now_ist_str="10:00 IST",
                                            confirm_url="https://x.test/bot")
    assert "https://x.test/bot" in linked


def test_reconcile_alert_threshold_default():
    from src.core.config import settings
    assert settings.reconcile_alert_threshold == 1_000.0


# ---------- gateway is OFF by default (no accidental network sends) ----------

def test_whatsapp_not_configured_in_test_env():
    assert whatsapp.configured() is False
    assert whatsapp.DEFAULT_GROUP_JID == "120363411936940548@g.us"


# ---------- fixed opening capital + deposit auto-detect defaults ----------

def test_real_opening_capital_defaults_to_18k():
    from src.core.config import settings
    assert settings.real_opening_capital == 18000.0


def test_deposit_autodetect_off_by_default():
    from src.core.config import settings
    # The net-value deposit detector is the source of the phantom deposits; it must be
    # OFF unless explicitly re-enabled, so the cost basis stays the fixed opening.
    assert settings.deposit_autodetect is False


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


# ---------- shadow buy-points: what the strategy wants while cash-gated ----------

def test_build_shadow_cash_maps_every_day_to_unreachable_cash():
    days = ["2026-09-20", "2026-09-21", "2026-09-20"]     # duplicate collapses
    cash = build_shadow_cash(days)
    assert cash == {"2026-09-20": SHADOW_DAY_CASH, "2026-09-21": SHADOW_DAY_CASH}
    assert all(v >= 1e11 for v in cash.values())          # big enough to defeat any cash gate
    assert build_shadow_cash([]) == {}                     # empty → engine no-op


def test_shadow_signal_key_is_price_free_and_stable():
    k = shadow_signal_key("2026-09-24", "SUZLON", "entry_scan_14:00_drop_-3%")
    assert k == "2026-09-24|SUZLON|entry_scan_14:00_drop_-3%"
    # same date·symbol·reason → same key regardless of price/qty (dedup across ticks)
    assert shadow_signal_key("2026-09-24", "SUZLON", "entry_scan_14:00_drop_-3%") == k


def test_extract_shadow_buys_keeps_recent_buys_only():
    trades = [
        {"date": "2026-09-24", "time": "14:00", "symbol": "AAA", "side": "BUY",
         "price": 100.0, "reason": "entry_scan_14:00_drop_-3%"},
        {"date": "2026-09-24", "time": "15:00", "symbol": "AAA", "side": "SELL",
         "price": 110.0, "reason": "target_1"},            # a sell is not a buy-point
        {"date": "2026-01-01", "time": "10:00", "symbol": "BBB", "side": "BUY",
         "price": 50.0, "reason": "entry"},                # too old (outside lookback)
        {"date": "2099-01-01", "time": "10:00", "symbol": "CCC", "side": "BUY",
         "price": 5.0, "reason": "entry"},                 # future date — never
    ]
    out = extract_shadow_buys(trades, "2026-09-25", lookback_days=45)
    assert len(out) == 1
    assert out[0]["symbol"] == "AAA" and out[0]["price"] == 100.0 and out[0]["time"] == "14:00"


def test_shadow_buyable_compares_current_price_to_wanted():
    assert shadow_buyable(100.0, 95.0) is True     # cheaper now → catchable at your price or better
    assert shadow_buyable(100.0, 100.0) is True    # exactly at the price → still catchable
    assert shadow_buyable(100.0, 120.0) is False   # ran up
    assert shadow_buyable(100.0, None) is False     # no candle data → fail closed


def test_shadow_config_defaults():
    from src.core.config import settings
    assert settings.shadow_buy_points is True
    assert settings.shadow_lookback_days == 45


def test_suspend_stale_days_default_is_five():
    from src.core.config import settings
    # bumped 3→5 to cut edge-of-threshold noise from transient multi-day per-symbol data gaps
    assert settings.suspend_stale_days == 5


# ---------- "why isn't it buying?" ----------

def _s404_like():
    """Duck-typed stand-in shaped like S404_s392_side_only's entry fields (no pandas in tests)."""
    from types import SimpleNamespace as NS
    mode = lambda **kw: NS(**{f: None for f in ("fall_threshold", "volume_spike_min", "macd_filter",
                                                 "sma_above_prev", "allocation_pct",
                                                 "max_new_buys_per_day")} | kw)
    return NS(
        fall_threshold=-0.030, volume_spike_min=1.1, macd_filter=None, sma_above_prev=None,
        allocation_pct=0.16, max_new_buys_per_day=None,
        mode_params_bull=mode(fall_threshold=-0.025, allocation_pct=0.18, macd_filter="__off__"),
        mode_params_bear=mode(fall_threshold=-0.030, allocation_pct=0.14, volume_spike_min=1.2,
                              macd_filter="positive", sma_above_prev=20),
        mode_params_sideways=mode(fall_threshold=-0.025, allocation_pct=0.16, macd_filter="__off__",
                                  sma_above_prev=-1),
    )


def test_effective_entry_params_swaps_by_regime_and_falls_back():
    s = _s404_like()
    bear = effective_entry_params(s, "bear")
    assert bear["fall_threshold"] == -0.030 and bear["volume_spike_min"] == 1.2
    assert bear["macd_filter"] == "positive" and bear["sma_above_prev"] == 20
    assert bear["allocation_pct"] == 0.14
    bull = effective_entry_params(s, "bull")
    assert bull["fall_threshold"] == -0.025 and bull["volume_spike_min"] == 1.1  # None → base
    assert bull["macd_filter"] is None                                           # "__off__" sentinel
    assert effective_entry_params(s, "sideways")["sma_above_prev"] is None       # -1 sentinel
    base = effective_entry_params(s, None)                                       # single-mode
    assert base["fall_threshold"] == -0.030 and base["allocation_pct"] == 0.16
    assert effective_entry_params(s, "weird")["fall_threshold"] == -0.030


def test_estimate_position_size_by_mode():
    assert estimate_position_size("pct_equity", 0.16, None, equity=20_000, cash=500) == 3_200
    assert estimate_position_size("pct_cash", 0.5, None, equity=20_000, cash=1_000) == 500
    assert estimate_position_size("fixed", None, 10_000, equity=0, cash=0) == 10_000
    assert estimate_position_size("pct_equity", None, None, equity=1, cash=1) is None
    assert estimate_position_size("pct_equity", 0.16, None, equity=-5, cash=0) == 0.0


def test_entry_requirements_wording_for_bear():
    reqs = entry_requirements(effective_entry_params(_s404_like(), "bear"))
    assert reqs == ["≥3.0% drop", "≥1.2× usual volume", "MACD histogram > 0",
                    "above its 20-day SMA yesterday"]
    assert entry_requirements({}) == []


def _diag(**kw):
    d = {"date": "2026-09-25", "regime": "bear", "params": effective_entry_params(_s404_like(), "bear"),
         "scan_times": ["11:00", "14:00"], "free_cash": 5_000.0, "position_size": 3_000.0,
         "min_entry_cash": None, "wanted_today": [], "bought_today": [], "quarantined": [],
         "nearest": [{"symbol": "ABC", "change": -0.021}], "today_bars": 900}
    d.update(kw)
    return d


def _v(diag, *, on=True, now="15:00", weekday=True, today="2026-09-25"):
    return entry_verdict(diag, bot_enabled=on, today_str=today, now_hhmm=now, is_weekday=weekday)


def test_entry_verdict_precedence_top():
    assert _v(None)[0] == "nodata"
    assert _v(_diag(), on=False)[0] == "off"
    both = _diag(bought_today=["XYZ"], wanted_today=[{"symbol": "XYZ", "price": 10.0}])
    assert _v(both) == ("bought", "Bought today: XYZ.")


def test_entry_verdict_wanted_but_not_bought_reasons():
    w = [{"symbol": "AAA", "price": 100.0}]
    assert _v(_diag(wanted_today=w, quarantined=["AAA"]))[0] == "quarantined"
    code, head = _v(_diag(wanted_today=w, free_cash=900.0, min_entry_cash=1_000.0))
    assert code == "cash" and "entry floor" in head
    code, head = _v(_diag(wanted_today=w, free_cash=1_500.0, position_size=3_000.0))
    assert code == "cash" and "below one position" in head
    code, head = _v(_diag(wanted_today=[{"symbol": "MRF", "price": 130_000.0}]))
    assert code == "size" and "MRF" in head
    assert _v(_diag(wanted_today=w))[0] == "pending"
    # wanted rows from a stale (yesterday) payload never count as today's
    assert _v(_diag(date="2026-09-24", wanted_today=w), now="12:00")[0] == "stale"


def test_entry_verdict_calendar_and_freshness():
    assert _v(_diag(), weekday=False)[0] == "closed"
    old = _diag(date="2026-09-24")
    assert _v(old, now="09:30")[0] == "waiting"      # before 11:00+5 → just not updated yet
    assert _v(old, now="11:05")[0] == "stale"        # past the first scan → trader is behind
    assert _v(_diag(today_bars=0))[0] == "holiday"
    assert _v(_diag(), now="10:59")[0] == "waiting"


def test_entry_verdict_no_setup_explains_requirements():
    code, head = _v(_diag(), now="12:00")
    assert code == "no_setup"
    assert "so far" in head and "(bear regime)" in head    # 14:00 scan still to come
    assert "MACD histogram > 0" in head and "Closest: ABC -2.1%" in head
    _, late = _v(_diag(), now="15:20")
    assert "so far" not in late
    _, bare = _v(_diag(regime=None, nearest=[]), now="15:20")
    assert "regime" not in bare and "Closest" not in bare


# ---------- daily digest ----------

def _digest(**kw):
    s = {"date_label": "Fri 25 Sep 2026", "market_data": True, "synced": True,
         "last_sync": "25 Sep 15:29 IST", "cash": 1_200.0, "holdings_value": 20_000.0,
         "pnl": metrics.split_pnl(21_200.0, 20_000.0, 700.0), "day_pnl": 312.0,
         "day_since": "Thu 24 Sep", "fills": [], "bot_enabled": True,
         "verdict": "No setups today (bear regime) — entries need ≥3.0% drop.", "wanted": []}
    s.update(kw)
    return s


def test_daily_digest_holiday_beats_sync_and_nosync_warns():
    # the trader still syncs the broker on a holiday, so no-candles must be checked first
    assert "holiday" in whatsapp.format_daily_digest(_digest(market_data=False))
    t = whatsapp.format_daily_digest(_digest(synced=False))
    assert "didn't sync" in t and "25 Sep 15:29 IST" in t


def test_daily_digest_full_body():
    t = whatsapp.format_daily_digest(_digest(
        fills=[{"side": "SELL", "symbol": "AAA", "qty": 3, "price": 105.5, "source": "bot"},
               {"side": "BUY", "symbol": "BBB", "qty": 2, "price": 50.0, "source": "manual"}],
        wanted=["CCC"]))
    assert "Net worth ₹21,200" in t
    assert "Since Thu 24 Sep +₹312" in t and "Total P&L +₹1,200 (+6.0%) on ₹20,000 invested" in t
    assert "Realized +₹500" in t and "Unrealized +₹700" in t
    assert "SELL 3 × AAA @ ₹105.50" in t and "BUY 2 × BBB @ ₹50.00 (manual)" in t
    assert "Why no buys:" in t                      # a manual buy isn't the bot buying
    assert "Strategy wanted today: CCC" in t
    first = whatsapp.format_daily_digest(_digest(day_pnl=None))
    assert "Since" not in first and "No trades today." in first
    assert "-₹50" in whatsapp.format_daily_digest(_digest(day_pnl=-50.0))


def test_daily_digest_why_line_only_when_on_and_no_bot_buy():
    off = whatsapp.format_daily_digest(_digest(bot_enabled=False))
    assert "Bot is OFF" in off and "Why no buys" not in off
    bought = whatsapp.format_daily_digest(_digest(
        fills=[{"side": "BUY", "symbol": "AAA", "qty": 1, "price": 10.0, "source": "bot"}]))
    assert "Why no buys" not in bought
