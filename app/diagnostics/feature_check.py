"""
feature_check.py — feature_snapshots 파이프라인 품질 진단

사용법:
  poetry run python -m app.diagnostics.feature_check
  poetry run python -m app.diagnostics.feature_check --window 300
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
    return max(0.0, (_now() - ts).total_seconds())


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


def _lag_badge(lag: float | None, warn_sec: float = 5.0, fail_sec: float = 30.0) -> str:
    if lag is None:
        return "N/A"
    if lag <= warn_sec:
        return f"{lag:.1f}s ✅"
    elif lag <= fail_sec:
        return f"{lag:.1f}s ⚠️"
    else:
        return f"{lag:.1f}s ❌"


def _fill_badge(fill: float | None, warn: float = 0.90, fail: float = 0.70) -> str:
    if fill is None:
        return "N/A"
    pct = fill * 100
    if fill >= warn:
        return f"{pct:.1f}% ✅"
    elif fill >= fail:
        return f"{pct:.1f}% ⚠️"
    else:
        return f"{pct:.1f}% ❌"


def _null_badge(null_rate: float | None, warn: float = 0.05, fail: float = 0.20) -> str:
    if null_rate is None:
        return "N/A"
    pct = null_rate * 100
    if null_rate <= warn:
        return f"{pct:.1f}% ✅"
    elif null_rate <= fail:
        return f"{pct:.1f}% ⚠️"
    else:
        return f"{pct:.1f}% ❌"


# ──────────────────────────────────────────────────────────────────────────────
# 체크 함수
# ──────────────────────────────────────────────────────────────────────────────


def check_lag(conn, symbol: str) -> tuple[bool, str]:
    lines = ["[feature_snapshots — lag]"]
    rows, err = _safe_query(
        conn,
        "SELECT max(ts) AS max_ts FROM feature_snapshots WHERE symbol = :sym",
        {"sym": symbol},
    )
    if err or rows is None:
        lines.append(f"  SKIP/ERROR: {err}")
        return False, "\n".join(lines)

    max_ts = rows[0][0]
    lag = _lag(max_ts)
    lines.append(f"  max(ts)  = {max_ts}")
    lines.append(f"  lag_sec  = {_lag_badge(lag, warn_sec=10, fail_sec=30)}")
    ok = lag is not None and lag <= 10
    lines.append(f"  → {'PASS ✅' if ok else 'FAIL ❌'}")
    return ok, "\n".join(lines)


def check_fill_rate(conn, symbol: str, window_sec: int, interval_sec: int) -> tuple[bool, str]:
    lines = [f"[feature_snapshots — fill_rate (window={window_sec}s)]"]
    rows, err = _safe_query(
        conn,
        f"""
        SELECT count(*) AS cnt
        FROM feature_snapshots
        WHERE symbol = :sym
          AND ts >= now() AT TIME ZONE 'UTC' - interval '{window_sec} seconds'
        """,
        {"sym": symbol},
    )
    if err or rows is None:
        lines.append(f"  SKIP/ERROR: {err}")
        return False, "\n".join(lines)

    count = rows[0][0]
    expected = window_sec / interval_sec
    fill = count / expected if expected > 0 else 0.0
    lines.append(f"  count = {count}  expected ≈ {expected:.0f}  fill_rate = {_fill_badge(fill)}")
    ok = fill >= 0.90
    lines.append(f"  → {'PASS ✅' if ok else 'FAIL ❌'}")
    return ok, "\n".join(lines)


def check_null_rates(conn, symbol: str, window_sec: int) -> tuple[bool, str]:
    lines = [f"[feature_snapshots — null_rates (window={window_sec}s)]"]

    rows, err = _safe_query(
        conn,
        f"""
        SELECT
            count(*) AS total,
            count(*) FILTER (WHERE mid_krw IS NULL) AS null_mid_krw,
            count(*) FILTER (WHERE spread_bps IS NULL) AS null_spread_bps,
            count(*) FILTER (WHERE p_none IS NULL) AS null_p_none,
            count(*) FILTER (WHERE ev_rate IS NULL) AS null_ev_rate,
            count(*) FILTER (WHERE bin_funding_rate IS NULL) AS null_bin_funding,
            count(*) FILTER (WHERE oi_value IS NULL) AS null_oi_value,
            count(*) FILTER (WHERE liq_5m_notional IS NULL) AS null_liq_notional
        FROM feature_snapshots
        WHERE symbol = :sym
          AND ts >= now() AT TIME ZONE 'UTC' - interval '{window_sec} seconds'
        """,
        {"sym": symbol},
    )
    if err or rows is None:
        lines.append(f"  SKIP/ERROR: {err}")
        return False, "\n".join(lines)

    r = rows[0]
    total = r[0] or 1  # avoid div-by-zero

    # (컬럼명, null_count, warn_threshold, fail_threshold)
    checks = [
        ("mid_krw",          r[1], 0.05, 0.20),
        ("spread_bps",       r[2], 0.05, 0.20),
        ("p_none",           r[3], 0.05, 0.20),
        ("ev_rate",          r[4], 0.05, 0.20),
        ("bin_funding_rate", r[5], 0.50, 0.80),  # alt data 수집 타이밍에 따라 완화
        ("oi_value",         r[6], 0.50, 0.80),
        ("liq_5m_notional",  r[7], 0.05, 0.20),  # 0은 정상, null은 비정상
    ]

    all_ok = True
    for col, null_cnt, warn, fail in checks:
        nr = null_cnt / total
        badge = _null_badge(nr, warn=warn, fail=fail)
        lines.append(f"  {col:25s}: null_rate={badge}  ({null_cnt}/{total})")
        if nr > fail:
            all_ok = False

    lines.append(f"  → {'PASS ✅' if all_ok else 'FAIL ❌'}")
    return all_ok, "\n".join(lines)


def check_liq_not_null(conn, symbol: str, window_sec: int) -> tuple[bool, str]:
    """liq_5m_notional은 0이어도 정상, null만 비정상."""
    lines = ["[feature_snapshots — liq_5m_notional null check]"]
    rows, err = _safe_query(
        conn,
        f"""
        SELECT count(*) FILTER (WHERE liq_5m_notional IS NULL) AS nulls,
               count(*) AS total
        FROM feature_snapshots
        WHERE symbol = :sym
          AND ts >= now() AT TIME ZONE 'UTC' - interval '{window_sec} seconds'
        """,
        {"sym": symbol},
    )
    if err or rows is None:
        lines.append(f"  SKIP/ERROR: {err}")
        return False, "\n".join(lines)

    nulls, total = rows[0][0], rows[0][1]
    total = total or 1
    nr = nulls / total
    lines.append(f"  liq_5m_notional null_rate = {nr*100:.1f}% ({nulls}/{total})")
    lines.append("  ※ 값이 0이면 정상 (청산 이벤트 없음), null은 파이프라인 오류")
    ok = nr <= 0.20
    lines.append(f"  → {'PASS ✅' if ok else 'FAIL ❌'}")
    return ok, "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="feature_snapshots 파이프라인 점검")
    parser.add_argument("--window", type=int, default=300, help="점검 윈도우(초, 기본 300)")
    args = parser.parse_args()
    window_sec = args.window

    s = load_settings()
    symbol = s.SYMBOL
    interval_sec = s.DECISION_INTERVAL_SEC

    engine = create_engine(s.DB_URL)

    now = _now()
    sep = "=" * 60
    print(sep)
    print("  feature_snapshots 파이프라인 점검")
    print(f"  now_utc            = {now.isoformat()}")
    print(f"  symbol             = {symbol}")
    print(f"  window             = {window_sec}s")
    print(f"  interval_sec       = {interval_sec}s")
    print(f"  expected_rows      ≈ {window_sec // interval_sec}")
    print(sep)

    results: dict[str, bool] = {}

    with engine.connect() as conn:
        ok, out = check_lag(conn, symbol)
        results["lag"] = ok
        print(out)
        print()

        ok, out = check_fill_rate(conn, symbol, window_sec, interval_sec)
        results["fill_rate"] = ok
        print(out)
        print()

        ok, out = check_null_rates(conn, symbol, window_sec)
        results["null_rates"] = ok
        print(out)
        print()

        ok, out = check_liq_not_null(conn, symbol, window_sec)
        results["liq_not_null"] = ok
        print(out)
        print()

        # 최근 5행 샘플
        rows, _ = _safe_query(
            conn,
            """
            SELECT ts, p_none, ev_rate, bin_funding_rate, oi_value, liq_5m_notional
            FROM feature_snapshots
            WHERE symbol = :sym
            ORDER BY ts DESC LIMIT 5
            """,
            {"sym": symbol},
        )
        if rows:
            print("[feature_snapshots — 최근 5행 샘플]")
            for r in rows:
                print(f"  {r}")
            print()

    overall_ok = all(results.values())

    print(sep)
    print("  개별 결과:")
    for k, v in results.items():
        print(f"    {k:20s}: {'PASS ✅' if v else 'FAIL ❌'}")
    print()
    print(f"  OVERALL: {'PASS ✅' if overall_ok else 'FAIL ❌'}")
    print(sep)

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
