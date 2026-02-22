"""
feature_check.py — Feature 품질 점검

사용법:
  poetry run python -m app.diagnostics.feature_check
  poetry run python -m app.diagnostics.feature_check --window 600
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


def check_predictions_quality(conn, symbol: str, window_sec: int) -> tuple[bool, str]:
    """Check prediction feature quality: null rates, value ranges."""
    lines = ["[predictions — feature quality]"]

    rows, err = _safe_query(
        conn,
        """SELECT
             count(*) as total,
             count(p_up) as p_up_cnt,
             count(p_down) as p_down_cnt,
             count(p_none) as p_none_cnt,
             count(ev) as ev_cnt,
             count(ev_rate) as ev_rate_cnt,
             count(r_t) as r_t_cnt,
             count(mom_z) as mom_z_cnt,
             count(spread_bps) as spread_bps_cnt
           FROM predictions
           WHERE symbol=:sym
             AND t0 >= now() AT TIME ZONE 'UTC' - make_interval(secs => :wsec)""",
        {"sym": symbol, "wsec": window_sec},
    )
    if err or rows is None:
        lines.append(f"  ERROR: {err}")
        lines.append("  → FAIL ❌")
        return False, "\n".join(lines)

    row = rows[0]
    total = row[0]
    if total == 0:
        lines.append("  No predictions in window")
        lines.append("  → FAIL ❌")
        return False, "\n".join(lines)

    fields = ["p_up", "p_down", "p_none", "ev", "ev_rate", "r_t", "mom_z", "spread_bps"]
    all_ok = True
    for i, field in enumerate(fields):
        cnt = row[i + 1]
        fill = cnt / total
        ok = fill >= 0.95
        badge = f"{fill*100:.1f}%" + (" ✅" if ok else " ❌")
        lines.append(f"  {field:20s}: {cnt}/{total} = {badge}")
        if not ok:
            all_ok = False

    # Range checks for probabilities
    rows2, _ = _safe_query(
        conn,
        """SELECT
             min(p_up) as min_pup, max(p_up) as max_pup,
             min(p_down) as min_pdn, max(p_down) as max_pdn,
             min(p_none) as min_pnone, max(p_none) as max_pnone
           FROM predictions
           WHERE symbol=:sym
             AND t0 >= now() AT TIME ZONE 'UTC' - make_interval(secs => :wsec)""",
        {"sym": symbol, "wsec": window_sec},
    )
    if rows2:
        r = rows2[0]
        lines.append(f"  p_up range:   [{r[0]:.4f}, {r[1]:.4f}]")
        lines.append(f"  p_down range: [{r[2]:.4f}, {r[3]:.4f}]")
        lines.append(f"  p_none range: [{r[4]:.4f}, {r[5]:.4f}]")
        # Probabilities should be in [0, 1]
        if any(v is not None and (v < 0 or v > 1) for v in r):
            lines.append("  ⚠️  Probability out of [0,1] range")
            all_ok = False

    lines.append(f"  → {'PASS ✅' if all_ok else 'FAIL ❌'}")
    return all_ok, "\n".join(lines)


def check_market_features(conn, symbol: str, window_sec: int) -> tuple[bool, str]:
    """Check market_1s feature quality."""
    lines = ["[market_1s — feature quality]"]

    rows, err = _safe_query(
        conn,
        """SELECT
             count(*) as total,
             count(mid_close_1s) as mid_cnt,
             count(spread_bps) as spread_cnt,
             count(imb_notional_top5) as imb_cnt,
             count(bid_close_1s) as bid_cnt,
             count(ask_close_1s) as ask_cnt
           FROM market_1s
           WHERE symbol=:sym
             AND ts >= now() AT TIME ZONE 'UTC' - make_interval(secs => :wsec)""",
        {"sym": symbol, "wsec": window_sec},
    )
    if err or rows is None:
        lines.append(f"  ERROR: {err}")
        lines.append("  → FAIL ❌")
        return False, "\n".join(lines)

    row = rows[0]
    total = row[0]
    if total == 0:
        lines.append("  No market_1s data in window")
        lines.append("  → FAIL ❌")
        return False, "\n".join(lines)

    fields = ["mid_close_1s", "spread_bps", "imb_notional_top5", "bid_close_1s", "ask_close_1s"]
    all_ok = True
    for i, field in enumerate(fields):
        cnt = row[i + 1]
        fill = cnt / total
        ok = fill >= 0.90
        badge = f"{fill*100:.1f}%" + (" ✅" if ok else " ❌")
        lines.append(f"  {field:20s}: {cnt}/{total} = {badge}")
        if not ok:
            all_ok = False

    lines.append(f"  → {'PASS ✅' if all_ok else 'FAIL ❌'}")
    return all_ok, "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Feature 품질 점검")
    parser.add_argument("--window", type=int, default=300, help="점검 윈도우(초, 기본 300)")
    args = parser.parse_args()
    window_sec = args.window

    s = load_settings()
    symbol = s.SYMBOL

    engine = create_engine(s.DB_URL)
    sep = "=" * 60

    print(sep)
    print("  Feature 품질 점검")
    print(f"  symbol  = {symbol}")
    print(f"  window  = {window_sec}s")
    print(sep)

    results: dict[str, bool] = {}

    with engine.connect() as conn:
        ok, out = check_predictions_quality(conn, symbol, window_sec)
        results["predictions_quality"] = ok
        print(out)
        print()

        ok, out = check_market_features(conn, symbol, window_sec)
        results["market_features"] = ok
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
