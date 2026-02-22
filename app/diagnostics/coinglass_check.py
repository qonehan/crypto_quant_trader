"""
coinglass_check.py — Coinglass 수집 강제 점검 (PASS/FAIL, SKIP 금지)

사용법:
  poetry run python -m app.diagnostics.coinglass_check
  poetry run python -m app.diagnostics.coinglass_check --window 600
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from app.config import load_settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lag(ts) -> float | None:
    if ts is None:
        return None
    if hasattr(ts, "tzinfo") and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (_now() - ts).total_seconds()


def _lag_badge(lag: float | None, warn_sec: float = 60.0, fail_sec: float = 600.0) -> str:
    if lag is None:
        return "N/A"
    if lag <= warn_sec:
        return f"{lag:.1f}s ✅"
    elif lag <= fail_sec:
        return f"{lag:.1f}s ⚠️"
    else:
        return f"{lag:.1f}s ❌"


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


def check_call_status(conn, symbol: str, window_sec: int) -> tuple[bool, str]:
    """Check coinglass_call_status table for ok=true within window."""
    lines = ["[coinglass_call_status]"]

    rows, err = _safe_query(
        conn,
        """SELECT count(*) as ok_cnt FROM coinglass_call_status
           WHERE symbol=:sym AND ok=true
             AND ts >= now() AT TIME ZONE 'UTC' - interval '{w} seconds'""".replace(
            "{w}", str(window_sec)
        ),
        {"sym": symbol},
    )
    if err or rows is None:
        lines.append(f"  ERROR: {err}")
        lines.append("  → FAIL ❌")
        return False, "\n".join(lines)

    ok_cnt = rows[0][0]
    lines.append(f"  ok=true count (last {window_sec}s) = {ok_cnt}")

    rows2, _ = _safe_query(
        conn,
        """SELECT ts, ok, http_status, error_msg, latency_ms
           FROM coinglass_call_status
           WHERE symbol=:sym ORDER BY ts DESC LIMIT 5""",
        {"sym": symbol},
    )
    if rows2:
        lines.append("  last 5 rows:")
        for r in rows2:
            lines.append(_fmt_row(r))

    ok = ok_cnt >= 1
    lines.append(f"  → {'PASS ✅' if ok else 'FAIL ❌'}")
    return ok, "\n".join(lines)


def check_liq_map(conn, symbol: str, window_sec: int) -> tuple[bool, str]:
    """Check coinglass_liquidation_map has at least 1 row within window."""
    lines = ["[coinglass_liquidation_map]"]

    rows, err = _safe_query(
        conn,
        """SELECT count(*) as cnt FROM coinglass_liquidation_map
           WHERE symbol=:sym
             AND ts >= now() AT TIME ZONE 'UTC' - interval '{w} seconds'""".replace(
            "{w}", str(window_sec)
        ),
        {"sym": symbol},
    )
    if err or rows is None:
        lines.append(f"  ERROR: {err}")
        lines.append("  → FAIL ❌")
        return False, "\n".join(lines)

    cnt = rows[0][0]
    lines.append(f"  row count (last {window_sec}s) = {cnt}")

    rows2, _ = _safe_query(
        conn,
        """SELECT ts, symbol, exchange, timeframe
           FROM coinglass_liquidation_map
           WHERE symbol=:sym ORDER BY ts DESC LIMIT 3""",
        {"sym": symbol},
    )
    if rows2:
        lines.append("  last 3 rows:")
        for r in rows2:
            lines.append(_fmt_row(r))

    ok = cnt >= 1
    lines.append(f"  → {'PASS ✅' if ok else 'FAIL ❌'}")
    return ok, "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Coinglass 수집 강제 점검 (PASS/FAIL)")
    parser.add_argument("--window", type=int, default=600, help="점검 윈도우(초, 기본 600)")
    args = parser.parse_args()
    window_sec = args.window

    s = load_settings()
    symbol = s.ALT_SYMBOL_COINGLASS

    # COINGLASS_ENABLED=false이면 FAIL
    if not s.COINGLASS_ENABLED:
        print("=" * 60)
        print("  Coinglass 수집 강제 점검")
        print("  COINGLASS_ENABLED = false → FAIL ❌")
        print("  (SKIP PASS 금지: COINGLASS_ENABLED=true 설정 필요)")
        print("=" * 60)
        return 1

    if not s.COINGLASS_API_KEY:
        print("=" * 60)
        print("  Coinglass 수집 강제 점검")
        print("  COINGLASS_API_KEY 미설정 → FAIL ❌")
        print("  (SKIP PASS 금지: API 키 설정 필요)")
        print("=" * 60)
        return 1

    engine = create_engine(s.DB_URL)
    sep = "=" * 60

    print(sep)
    print("  Coinglass 수집 강제 점검 (PASS/FAIL)")
    print(f"  symbol       = {symbol}")
    print(f"  window       = {window_sec}s")
    print(f"  COINGLASS_ENABLED  = {s.COINGLASS_ENABLED}")
    print(f"  COINGLASS_KEY_SET  = {bool(s.COINGLASS_API_KEY)}")
    print(sep)

    results: dict[str, bool] = {}

    with engine.connect() as conn:
        ok, out = check_call_status(conn, symbol, window_sec)
        results["call_status"] = ok
        print(out)
        print()

        ok, out = check_liq_map(conn, symbol, window_sec)
        results["liq_map"] = ok
        print(out)
        print()

    overall_ok = all(results.values())

    print(sep)
    print("  개별 결과:")
    for k, v in results.items():
        print(f"    {k:30s}: {'PASS ✅' if v else 'FAIL ❌'}")
    print()
    print(f"  OVERALL: {'PASS ✅' if overall_ok else 'FAIL ❌'}")
    print(sep)

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
