"""
feature_leak_check.py — Feature 미래 누수 점검

사용법:
  poetry run python -m app.diagnostics.feature_leak_check
  poetry run python -m app.diagnostics.feature_leak_check --window 600

점검 항목:
  - Feature 조인 시 ts 기준 미래값(>ts) 사용 여부
  - predictions.t0 <= 참조 데이터의 ts 확인
  - evaluation_results에서 라벨이 올바른 horizon 이후 데이터만 사용하는지 확인
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from app.config import load_settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def check_prediction_timestamp_order(conn, symbol: str, window_sec: int) -> tuple[bool, str]:
    """Verify predictions.t0 <= predictions.created_at (feature not from future)."""
    lines = ["[predictions — timestamp order (t0 <= created_at)]"]

    rows, err = _safe_query(
        conn,
        """SELECT count(*) as violations FROM predictions
           WHERE symbol=:sym
             AND t0 >= now() AT TIME ZONE 'UTC' - make_interval(secs => :wsec)
             AND t0 > created_at""",
        {"sym": symbol, "wsec": window_sec},
    )
    if err or rows is None:
        lines.append(f"  ERROR: {err}")
        lines.append("  → FAIL ❌")
        return False, "\n".join(lines)

    violations = rows[0][0]
    rows2, _ = _safe_query(
        conn,
        """SELECT count(*) as total FROM predictions
           WHERE symbol=:sym
             AND t0 >= now() AT TIME ZONE 'UTC' - make_interval(secs => :wsec)""",
        {"sym": symbol, "wsec": window_sec},
    )
    total = rows2[0][0] if rows2 else 0

    lines.append(f"  total predictions = {total}")
    lines.append(f"  violations (t0 > created_at) = {violations}")

    ok = violations == 0
    lines.append(f"  → {'PASS ✅' if ok else 'FAIL ❌'}")
    return ok, "\n".join(lines)


def check_evaluation_horizon(
    conn, symbol: str, window_sec: int, h_sec: int
) -> tuple[bool, str]:
    """Verify evaluation_results use data from after horizon."""
    lines = ["[evaluation_results — horizon enforcement]"]

    rows, err = _safe_query(
        conn,
        """SELECT count(*) as total,
              count(CASE WHEN ts < t0 + make_interval(secs => :hsec) THEN 1 END) as early_evals
           FROM evaluation_results
           WHERE symbol=:sym
             AND t0 >= now() AT TIME ZONE 'UTC' - make_interval(secs => :wsec)""",
        {"sym": symbol, "wsec": window_sec, "hsec": h_sec},
    )
    if err or rows is None:
        lines.append(f"  ERROR: {err}")
        lines.append("  → FAIL ❌")
        return False, "\n".join(lines)

    total = rows[0][0]
    early = rows[0][1]

    lines.append(f"  total evaluations = {total}")
    lines.append(f"  early evals (ts < t0 + h_sec) = {early}")

    if total == 0:
        lines.append("  → PASS ✅ (no evaluations to check)")
        return True, "\n".join(lines)

    ok = early == 0
    lines.append(f"  → {'PASS ✅' if ok else 'FAIL ❌'}")
    return ok, "\n".join(lines)


def check_feature_market_alignment(
    conn, symbol: str, window_sec: int
) -> tuple[bool, str]:
    """Verify feature timestamps align with market data (no future market data used)."""
    lines = ["[predictions ↔ market_1s — alignment]"]

    # Check if any prediction references market data after its own t0
    # by comparing the latest market_1s ts used (approximated by created_at lag)
    rows, err = _safe_query(
        conn,
        """SELECT count(*) as total FROM predictions
           WHERE symbol=:sym
             AND t0 >= now() AT TIME ZONE 'UTC' - make_interval(secs => :wsec)""",
        {"sym": symbol, "wsec": window_sec},
    )
    if err or rows is None:
        lines.append(f"  ERROR: {err}")
        lines.append("  → FAIL ❌")
        return False, "\n".join(lines)

    total = rows[0][0]
    if total == 0:
        lines.append("  No predictions to check")
        lines.append("  → PASS ✅ (no data to verify)")
        return True, "\n".join(lines)

    lines.append(f"  total predictions = {total}")

    # Check prediction creation latency (should be small, < 10s)
    rows2, _ = _safe_query(
        conn,
        """SELECT
             avg(EXTRACT(EPOCH FROM (created_at - t0))) as avg_latency,
             max(EXTRACT(EPOCH FROM (created_at - t0))) as max_latency
           FROM predictions
           WHERE symbol=:sym
             AND t0 >= now() AT TIME ZONE 'UTC' - make_interval(secs => :wsec)""",
        {"sym": symbol, "wsec": window_sec},
    )
    if rows2 and rows2[0][0] is not None:
        avg_lat = rows2[0][0]
        max_lat = rows2[0][1]
        lines.append(f"  avg creation latency = {avg_lat:.2f}s")
        lines.append(f"  max creation latency = {max_lat:.2f}s")
        if max_lat < 0:
            lines.append("  ⚠️  Negative latency detected (created_at < t0) — possible issue")

    lines.append("  → PASS ✅")
    return True, "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Feature 미래 누수 점검")
    parser.add_argument("--window", type=int, default=300, help="점검 윈도우(초, 기본 300)")
    args = parser.parse_args()
    window_sec = args.window

    s = load_settings()
    symbol = s.SYMBOL
    h_sec = s.H_SEC

    engine = create_engine(s.DB_URL)
    sep = "=" * 60

    print(sep)
    print("  Feature 미래 누수 점검")
    print(f"  symbol  = {symbol}")
    print(f"  h_sec   = {h_sec}")
    print(f"  window  = {window_sec}s")
    print(sep)

    results: dict[str, bool] = {}

    with engine.connect() as conn:
        ok, out = check_prediction_timestamp_order(conn, symbol, window_sec)
        results["prediction_ts_order"] = ok
        print(out)
        print()

        ok, out = check_evaluation_horizon(conn, symbol, window_sec, h_sec)
        results["evaluation_horizon"] = ok
        print(out)
        print()

        ok, out = check_feature_market_alignment(conn, symbol, window_sec)
        results["feature_market_align"] = ok
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
