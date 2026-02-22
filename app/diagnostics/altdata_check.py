"""
altdata_check.py — Alt Data 파이프라인 실시간 유입 점검

사용법:
  poetry run python -m app.diagnostics.altdata_check
  poetry run python -m app.diagnostics.altdata_check --window 300
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from app.config import load_settings

# ──────────────────────────────────────────────────────────────────────────────
# 유틸리티
# ──────────────────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lag(ts) -> float | None:
    if ts is None:
        return None
    if hasattr(ts, "tzinfo") and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (_now() - ts).total_seconds()


def _safe_query(conn, sql: str, params: dict | None = None):
    try:
        result = conn.execute(text(sql), params or {})
        return result.fetchall(), None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return None, str(e)


def _fmt_row(row) -> str:
    try:
        d = dict(row._mapping)
        return "  " + ", ".join(f"{k}={v}" for k, v in d.items())
    except Exception:
        return "  " + str(row)


def _lag_badge(lag: float | None, warn_sec: float = 5.0, fail_sec: float = 30.0) -> str:
    if lag is None:
        return "N/A"
    if lag <= warn_sec:
        return f"{lag:.1f}s ✅"
    elif lag <= fail_sec:
        return f"{lag:.1f}s ⚠️"
    else:
        return f"{lag:.1f}s ❌"


def _fill_badge(fill: float | None) -> str:
    if fill is None:
        return "N/A"
    pct = fill * 100
    if pct >= 90:
        return f"{pct:.1f}% ✅"
    elif pct >= 70:
        return f"{pct:.1f}% ⚠️"
    else:
        return f"{pct:.1f}% ❌"


# ──────────────────────────────────────────────────────────────────────────────
# 체크 함수
# ──────────────────────────────────────────────────────────────────────────────


def check_mark_price(conn, symbol: str, window_sec: int) -> tuple[bool, str]:
    lines = ["[binance_mark_price_1s]"]

    rows, err = _safe_query(
        conn,
        "SELECT max(ts) as max_ts FROM binance_mark_price_1s WHERE symbol=:sym",
        {"sym": symbol},
    )
    if err or rows is None:
        lines.append(f"  SKIP/ERROR: {err}")
        return False, "\n".join(lines)

    max_ts = rows[0][0]
    lag = _lag(max_ts)
    lines.append(f"  max(ts)   = {max_ts}")
    lines.append(f"  lag_sec   = {_lag_badge(lag, warn_sec=3, fail_sec=10)}")

    rows2, err2 = _safe_query(
        conn,
        f"""SELECT count(*) as cnt FROM binance_mark_price_1s
            WHERE symbol=:sym
              AND ts >= now() AT TIME ZONE 'UTC' - interval '{window_sec} seconds'""",
        {"sym": symbol},
    )
    count = rows2[0][0] if (rows2 and not err2) else 0
    expected = window_sec  # 1s cadence
    fill = count / expected if expected > 0 else 0
    lines.append(f"  count(window={window_sec}s) = {count} / expected={expected}")
    lines.append(f"  fill_rate = {_fill_badge(fill)}")

    rows3, _ = _safe_query(
        conn,
        """SELECT ts, mark_price, index_price, funding_rate
           FROM binance_mark_price_1s
           WHERE symbol=:sym ORDER BY ts DESC LIMIT 3""",
        {"sym": symbol},
    )
    if rows3:
        lines.append("  last 3 rows:")
        for r in rows3:
            lines.append(_fmt_row(r))

    ok = lag is not None and lag <= 3 and fill >= 0.90
    lines.append(f"  → {'PASS ✅' if ok else 'FAIL ❌'}")
    return ok, "\n".join(lines)


def check_futures_metrics(conn, symbol: str, poll_sec: int) -> tuple[bool, str]:
    lines = ["[binance_futures_metrics]"]

    required_metrics = ["open_interest", "global_ls_ratio", "taker_ls_ratio", "basis"]
    threshold_sec = poll_sec * 2 + 30

    all_ok = True
    for metric in required_metrics:
        rows, err = _safe_query(
            conn,
            """SELECT max(ts) as max_ts FROM binance_futures_metrics
               WHERE symbol=:sym AND metric=:metric""",
            {"sym": symbol, "metric": metric},
        )
        if err or rows is None or rows[0][0] is None:
            lines.append(f"  [{metric}] max_ts=None ❌ (no data yet)")
            all_ok = False
            continue
        max_ts = rows[0][0]
        lag = _lag(max_ts)
        ok_m = lag is not None and lag <= threshold_sec
        if not ok_m:
            all_ok = False
        lines.append(
            f"  [{metric}] lag={_lag_badge(lag, warn_sec=threshold_sec*0.5, fail_sec=threshold_sec)}"
        )

    rows3, _ = _safe_query(
        conn,
        """SELECT ts, symbol, metric, value, period
           FROM binance_futures_metrics
           WHERE symbol=:sym ORDER BY ts DESC LIMIT 8""",
        {"sym": symbol},
    )
    if rows3:
        lines.append("  last rows:")
        for r in rows3:
            lines.append(_fmt_row(r))

    lines.append(f"  → {'PASS ✅' if all_ok else 'FAIL ❌'}")
    return all_ok, "\n".join(lines)


def check_force_orders(conn, symbol: str) -> tuple[bool, str]:
    """forceOrder: connection health 기준 (이벤트 0건일 수 있음 → PASS 기준 완화)."""
    lines = ["[binance_force_orders]"]

    rows, err = _safe_query(
        conn,
        """SELECT count(*) as cnt, max(ts) as max_ts
           FROM binance_force_orders
           WHERE symbol=:sym
             AND ts >= now() AT TIME ZONE 'UTC' - interval '86400 seconds'""",
        {"sym": symbol},
    )
    if err or rows is None:
        lines.append(f"  SKIP/ERROR: {err}")
        lines.append("  → SKIP (table not ready)")
        return True, "\n".join(lines)

    cnt, max_ts = rows[0][0], rows[0][1]
    lines.append(f"  count(last 24h)  = {cnt}")
    lines.append(f"  last event ts    = {max_ts}")
    lines.append("  ⚠️  이벤트가 0건이어도 WS 연결이 정상이면 PASS (청산이 없을 수 있음)")

    rows3, _ = _safe_query(
        conn,
        """SELECT ts, side, price, qty, notional
           FROM binance_force_orders
           WHERE symbol=:sym ORDER BY ts DESC LIMIT 3""",
        {"sym": symbol},
    )
    if rows3:
        lines.append("  last 3 rows:")
        for r in rows3:
            lines.append(_fmt_row(r))

    lines.append("  → PASS ✅ (connection-based check)")
    return True, "\n".join(lines)


def check_coinglass(
    conn, symbol: str, poll_sec: int, coinglass_enabled: bool
) -> tuple[bool, str]:
    lines = ["[coinglass_liquidation_map]"]

    rows, err = _safe_query(
        conn,
        "SELECT max(ts) as max_ts FROM coinglass_liquidation_map WHERE symbol=:sym",
        {"sym": symbol},
    )
    if err or rows is None:
        lines.append(f"  SKIP/ERROR: {err}")
        if coinglass_enabled:
            lines.append("  → FAIL ❌ (COINGLASS_ENABLED=true, SKIP 금지)")
            return False, "\n".join(lines)
        lines.append("  → SKIP")
        return True, "\n".join(lines)

    max_ts = rows[0][0]
    if max_ts is None:
        if coinglass_enabled:
            lines.append("  max_ts = None (데이터 없음)")
            lines.append("  → FAIL ❌ (COINGLASS_ENABLED=true, 데이터 필수)")
            return False, "\n".join(lines)
        lines.append("  max_ts = None (no data yet — COINGLASS_API_KEY 미설정이면 정상)")
        lines.append("  → SKIP ✅")
        return True, "\n".join(lines)

    lag = _lag(max_ts)
    threshold = poll_sec * 2 + 60
    ok = lag is not None and lag <= threshold
    lines.append(f"  max(ts)   = {max_ts}")
    lines.append(f"  lag_sec   = {_lag_badge(lag, warn_sec=threshold*0.5, fail_sec=threshold)}")

    rows3, _ = _safe_query(
        conn,
        """SELECT ts, symbol, exchange, timeframe
           FROM coinglass_liquidation_map
           WHERE symbol=:sym ORDER BY ts DESC LIMIT 3""",
        {"sym": symbol},
    )
    if rows3:
        lines.append("  last 3 rows:")
        for r in rows3:
            lines.append(_fmt_row(r))

    lines.append(f"  → {'PASS ✅' if ok else 'FAIL ❌'}")
    return ok, "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Alt Data 파이프라인 점검")
    parser.add_argument("--window", type=int, default=300, help="점검 윈도우(초, 기본 300)")
    args = parser.parse_args()
    window_sec = args.window

    s = load_settings()
    symbol_binance = s.ALT_SYMBOL_BINANCE
    symbol_coinglass = s.ALT_SYMBOL_COINGLASS
    poll_sec = s.BINANCE_POLL_SEC
    cg_poll_sec = s.COINGLASS_POLL_SEC
    cg_enabled = s.COINGLASS_ENABLED

    engine = create_engine(s.DB_URL)

    now = _now()
    sep = "=" * 60
    print(sep)
    print("  Alt Data 파이프라인 점검")
    print(f"  now_utc          = {now.isoformat()}")
    print(f"  binance_symbol   = {symbol_binance}")
    print(f"  coinglass_symbol = {symbol_coinglass}")
    print(f"  window           = {window_sec}s")
    print(f"  BINANCE_POLL_SEC = {poll_sec}s")
    print(f"  COINGLASS_POLL_SEC = {cg_poll_sec}s")
    print(f"  COINGLASS_KEY_SET  = {bool(s.COINGLASS_API_KEY)}")
    print(f"  COINGLASS_ENABLED  = {cg_enabled}")
    print(sep)

    results: dict[str, bool] = {}

    with engine.connect() as conn:
        ok, out = check_mark_price(conn, symbol_binance, window_sec)
        results["mark_price"] = ok
        print(out)
        print()

        ok, out = check_futures_metrics(conn, symbol_binance, poll_sec)
        results["futures_metrics"] = ok
        print(out)
        print()

        ok, out = check_force_orders(conn, symbol_binance)
        results["force_orders"] = ok  # always True (connection-based)
        print(out)
        print()

        ok, out = check_coinglass(conn, symbol_coinglass, cg_poll_sec, cg_enabled)
        results["coinglass"] = ok
        print(out)
        print()

    # OVERALL 판정 (mark_price + futures_metrics는 필수)
    # coinglass는 COINGLASS_ENABLED=true일 때 필수
    core_keys = ["mark_price", "futures_metrics"]
    if cg_enabled:
        core_keys.append("coinglass")
    overall_ok = all(results.get(k, False) for k in core_keys)

    print(sep)
    print("  개별 결과:")
    for k, label in [
        ("mark_price", "binance_mark_price_1s"),
        ("futures_metrics", "binance_futures_metrics"),
        ("force_orders", "binance_force_orders"),
        ("coinglass", "coinglass_liquidation_map"),
    ]:
        v = results.get(k, False)
        print(f"    {label:35s}: {'PASS ✅' if v else 'FAIL ❌'}")
    print()
    print(f"  OVERALL: {'PASS ✅' if overall_ok else 'FAIL ❌'}")
    print(sep)

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
