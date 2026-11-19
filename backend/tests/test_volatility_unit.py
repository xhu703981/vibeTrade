from backend.market.volatility import FALLBACK_STOP_PCT, atr_pct, stop_words, suggest_stop


def _candles(rng_pct, n=20, px=1.0):
    out = []
    for i in range(n):
        h, l = px * (1 + rng_pct / 200), px * (1 - rng_pct / 200)
        out.append([i * 900, px, h, l, px, 1.0])
    return out


def test_atr_pct_of_flat_range():
    assert abs(atr_pct(_candles(4.0)) - 4.0) < 1e-6


def test_stop_is_atr_times_multiple_clamped():
    assert suggest_stop(_candles(4.0))["stop_pct"] == 7.0        # 4 x 1.75 = 7
    assert suggest_stop(_candles(1.0))["stop_pct"] == 5.0        # floor 5%
    assert suggest_stop(_candles(30.0))["stop_pct"] == 25.0      # cap 25%


def test_fallback_without_candles_and_1h_backup():
    assert suggest_stop(None)["stop_pct"] == FALLBACK_STOP_PCT
    s = suggest_stop([], _candles(6.0))
    assert s["tf"] == "1h" and s["stop_pct"] == 9.0               # 6 x 1.5


def test_spoken_line_has_no_names():
    s = suggest_stop(_candles(4.0))
    line = stop_words(s, "coin 1")
    assert line == "Coin 1 usually swings about 4 percent, so I'd put the stop at about 7 percent."
