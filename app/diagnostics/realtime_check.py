"""
realtime_check.py — 실시간 데이터 파이프라인 정상 유입 점검 스크립트

사용법:
  poetry run python -m app.diagnostics.realtime_check
  poetry run python -m app.diagnostics.realtime_check --window 600
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from app.config import load_settings

# ────────────────────────────────────────────────────────────────
# 유틸리티
# ────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lag(ts) -> float | None:
    """최신 타임스탬프와 현재 UTC 시각의 차이(초)를 반환. None이면 None."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (_now() - ts).total_seconds()


def _safe_query(conn, sql: str, params: dict | None = None):
    """쿼리 실행 후 결과 반환. 실패하면 rollback 후 None과 에러 메시지 반환."""
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
    """SQLAlchemy Row를 보기 좋은 dict 문자열로 변환."""
    try:
        d = dict(row._mapping)
        return "  " + ", ".join(f"{k}={v}" for k, v in d.items())
    except Exception:
        return "  " + str(row)


def _fill_badge(fill_rate: float | None) -> str:
    if fill_rate is None:
        return "N/A"
    pct = fill_rate * 100
    if pct >= 90:
        return f"{pct:.1f}% ✅"
    elif pct >= 70:
        return f"{pct:.1f}% ⚠️"
    else:
        return f"{pct:.1f}% ❌"


def _lag_badge(lag: float | None) -> str:
    if lag is None:
        return "N/A"
    if lag <= 3:
        return f"{lag:.1f}s ✅"
    elif lag <= 10:
        return f"{lag:.1f}s ⚠️"
    else:
        return f"{lag:.1f}s ❌"


# ────────────────────────────────────────────────────────────────
# 각 테이블 점검 함수
# ────────────────────────────────────────────────────────────────

def check_market_1s(conn, sym: str, window_sec: int, now: datetime) -> tuple[bool, str]:
    lines = ["[market_1s]"]

    rows, err = _safe_query(conn, "SELECT max(ts) as max_ts FROM market_1s WHERE symbol=:sym", {"sym": sym})
    if err or rows is None:
        lines.append(f"  SKIP: {err}")
        return False, "\n".join(lines)

    max_ts = rows[0][0]
    lag = _lag(max_ts)
    lines.append(f"  max(ts)   = {max_ts}")
    lines.append(f"  lag_sec   = {_lag_badge(lag)}")

    rows2, err2 = _safe_query(conn, """
        SELECT count(*) as cnt FROM market_1s
        WHERE symbol=:sym AND ts >= now() AT TIME ZONE 'UTC' - interval '{w} seconds'
    """.replace("{w}", str(window_sec)), {"sym": sym})
    count = rows2[0][0] if (rows2 and not err2) else 0
    expected = window_sec
    fill = count / expected if expected > 0 else 0
    lines.append(f"  count(window={window_sec}s) = {count} / expected={expected}")
    lines.append(f"  fill_rate = {_fill_badge(fill)}")

    rows3, _ = _safe_query(conn, """
        SELECT ts, bid_close_1s, ask_close_1s, spread_bps, imb_notional_top5
        FROM market_1s WHERE symbol=:sym ORDER BY ts DESC LIMIT 3
    """, {"sym": sym})
    if rows3:
        lines.append("  last 3 rows:")
        for r in rows3:
            lines.append(_fmt_row(r))

    ok = lag is not None and lag <= 3 and fill >= 0.90
    lines.append(f"  → {'PASS ✅' if ok else 'FAIL ❌'}")
    return ok, "\n".join(lines)


def check_barrier_state(conn, sym: str, window_sec: int, interval_sec: int) -> tuple[bool, str]:
    lines = ["[barrier_state]"]

    rows, err = _safe_query(conn, "SELECT max(ts) as max_ts FROM barrier_state WHERE symbol=:sym", {"sym": sym})
    if err or rows is None:
        lines.append(f"  SKIP: {err}")
        return False, "\n".join(lines)

    max_ts = rows[0][0]
    lag = _lag(max_ts)
    lines.append(f"  max(ts)   = {max_ts}")
    lines.append(f"  lag_sec   = {_lag_badge(lag)}")

    rows2, err2 = _safe_query(conn, """
        SELECT count(*) as cnt FROM barrier_state
        WHERE symbol=:sym AND ts >= now() AT TIME ZONE 'UTC' - interval '{w} seconds'
    """.replace("{w}", str(window_sec)), {"sym": sym})
    count = rows2[0][0] if (rows2 and not err2) else 0
    expected = window_sec // interval_sec if interval_sec > 0 else 0
    fill = count / expected if expected > 0 else 0
    lines.append(f"  count(window={window_sec}s) = {count} / expected≈{expected}")
    lines.append(f"  fill_rate = {_fill_badge(fill)}")

    rows3, _ = _safe_query(conn, """
        SELECT ts, r_t, r_min_eff, cost_roundtrip_est, status, k_vol_eff, none_ewma
        FROM barrier_state WHERE symbol=:sym ORDER BY ts DESC LIMIT 3
    """, {"sym": sym})
    if rows3:
        lines.append("  last 3 rows:")
        for r in rows3:
            lines.append(_fmt_row(r))
    elif not rows3:
        # 컬럼명이 다를 수 있으므로 fallback
        rows3b, _ = _safe_query(conn, "SELECT * FROM barrier_state WHERE symbol=:sym ORDER BY ts DESC LIMIT 3", {"sym": sym})
        if rows3b:
            lines.append("  last 3 rows (full):")
            for r in rows3b:
                lines.append(_fmt_row(r))

    ok = lag is not None and lag <= 10 and fill >= 0.90
    lines.append(f"  → {'PASS ✅' if ok else 'FAIL ❌'}")
    return ok, "\n".join(lines)


def check_predictions(conn, sym: str, window_sec: int, interval_sec: int) -> tuple[bool, str]:
    lines = ["[predictions]"]

    rows, err = _safe_query(conn, "SELECT max(t0) as max_t0 FROM predictions WHERE symbol=:sym", {"sym": sym})
    if err or rows is None:
        lines.append(f"  SKIP: {err}")
        return False, "\n".join(lines)

    max_t0 = rows[0][0]
    lag = _lag(max_t0)
    lines.append(f"  max(t0)   = {max_t0}")
    lines.append(f"  lag_sec   = {_lag_badge(lag)}")

    rows2, err2 = _safe_query(conn, """
        SELECT count(*) as cnt FROM predictions
        WHERE symbol=:sym AND t0 >= now() AT TIME ZONE 'UTC' - interval '{w} seconds'
    """.replace("{w}", str(window_sec)), {"sym": sym})
    count = rows2[0][0] if (rows2 and not err2) else 0
    expected = window_sec // interval_sec if interval_sec > 0 else 0
    fill = count / expected if expected > 0 else 0
    lines.append(f"  count(window={window_sec}s) = {count} / expected≈{expected}")
    lines.append(f"  fill_rate = {_fill_badge(fill)}")

    rows3, _ = _safe_query(conn, """
        SELECT t0, p_up, p_down, p_none, ev, ev_rate, action_hat, model_version
        FROM predictions WHERE symbol=:sym ORDER BY t0 DESC LIMIT 3
    """, {"sym": sym})
    if rows3:
        lines.append("  last 3 rows:")
        for r in rows3:
            lines.append(_fmt_row(r))

    ok = lag is not None and lag <= 10 and fill >= 0.90
    lines.append(f"  → {'PASS ✅' if ok else 'FAIL ❌'}")
    return ok, "\n".join(lines)


def check_evaluation_results(conn, sym: str, window_sec: int, h_sec: int) -> tuple[bool, str]:
    lines = ["[evaluation_results (exec_v1)]"]
    lines.append(f"  ⚠️  evaluator는 horizon({h_sec}s) 경과 후 정산 → 초반엔 행이 적을 수 있음")

    rows, err = _safe_query(conn, """
        SELECT count(*) as cnt FROM evaluation_results
        WHERE symbol=:sym
          AND t0 >= now() AT TIME ZONE 'UTC' - interval '600 seconds'
    """, {"sym": sym})
    if err or rows is None:
        lines.append(f"  SKIP: {err}")
        return True, "\n".join(lines)

    count = rows[0][0]
    lines.append(f"  count(last 10min) = {count}")

    rows3, _ = _safe_query(conn, """
        SELECT t0, actual_direction, touch_sec, brier, logloss, label_version
        FROM evaluation_results WHERE symbol=:sym ORDER BY t0 DESC LIMIT 3
    """, {"sym": sym})
    if rows3:
        lines.append("  last 3 rows:")
        for r in rows3:
            lines.append(_fmt_row(r))

    if count >= 1:
        lines.append("  → PASS ✅")
        return True, "\n".join(lines)
    else:
        lines.append("  → PENDING ⏳ (시간이 더 필요하거나 bot을 먼저 실행해야 함)")
        return True, "\n".join(lines)  # optional — OVERALL에 영향 없음


def check_paper_decisions(conn, sym: str, window_sec: int, interval_sec: int) -> tuple[bool, str]:
    lines = ["[paper_decisions]"]

    rows, err = _safe_query(conn, "SELECT max(ts) as max_ts FROM paper_decisions WHERE symbol=:sym", {"sym": sym})
    if err or rows is None:
        lines.append(f"  SKIP: {err}")
        return False, "\n".join(lines)

    max_ts = rows[0][0]
    lag = _lag(max_ts)
    lines.append(f"  max(ts)   = {max_ts}")
    lines.append(f"  lag_sec   = {_lag_badge(lag)}")

    rows2, err2 = _safe_query(conn, """
        SELECT count(*) as cnt FROM paper_decisions
        WHERE symbol=:sym AND ts >= now() AT TIME ZONE 'UTC' - interval '{w} seconds'
    """.replace("{w}", str(window_sec)), {"sym": sym})
    count = rows2[0][0] if (rows2 and not err2) else 0
    expected = window_sec // interval_sec if interval_sec > 0 else 0
    fill = count / expected if expected > 0 else 0
    lines.append(f"  count(window={window_sec}s) = {count} / expected≈{expected}")
    lines.append(f"  fill_rate = {_fill_badge(fill)}")

    rows3, _ = _safe_query(conn, """
        SELECT ts, pos_status, action, reason, equity_est, drawdown_pct, policy_profile
        FROM paper_decisions WHERE symbol=:sym ORDER BY ts DESC LIMIT 3
    """, {"sym": sym})
    if rows3:
        lines.append("  last 3 rows:")
        for r in rows3:
            lines.append(_fmt_row(r))

    ok = lag is not None and lag <= 10 and fill >= 0.90
    lines.append(f"  → {'PASS ✅' if ok else 'FAIL ❌'}")
    return ok, "\n".join(lines)


def check_paper_trades(conn, sym: str, window_sec: int) -> str:
    lines = ["[paper_trades]"]

    rows, err = _safe_query(conn, """
        SELECT count(*) as cnt FROM paper_trades
        WHERE symbol=:sym AND t >= now() AT TIME ZONE 'UTC' - interval '86400 seconds'
    """, {"sym": sym})
    if err or rows is None:
        lines.append(f"  SKIP: {err}")
        return "\n".join(lines)

    count = rows[0][0]
    lines.append(f"  count(last 24h) = {count}")

    rows3, _ = _safe_query(conn, """
        SELECT t, action, reason, price, qty, fee_krw, pnl_krw
        FROM paper_trades WHERE symbol=:sym ORDER BY t DESC LIMIT 3
    """, {"sym": sym})
    if rows3:
        lines.append("  last 3 rows:")
        for r in rows3:
            lines.append(_fmt_row(r))
    return "\n".join(lines)


def check_dashboard() -> tuple[bool, str]:
    lines = ["[dashboard (http://localhost:8501)]"]
    try:
        import urllib.request
        req = urllib.request.urlopen("http://localhost:8501/healthz", timeout=3)
        code = req.getcode()
        ok = (code == 200)
        lines.append(f"  /healthz → HTTP {code} {'✅' if ok else '❌'}")
        return ok, "\n".join(lines)
    except Exception as e:
        lines.append(f"  /healthz → ERROR: {e} ❌")
        return False, "\n".join(lines)


# ────────────────────────────────────────────────────────────────
# main
# ────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="실시간 데이터 파이프라인 점검")
    parser.add_argument("--window", type=int, default=300, help="점검 윈도우(초, 기본 300)")
    args = parser.parse_args()
    window_sec = args.window

    s = load_settings()
    sym = s.SYMBOL
    interval_sec = s.DECISION_INTERVAL_SEC
    h_sec = s.H_SEC

    engine = create_engine(s.DB_URL)

    now = _now()
    sep = "=" * 60
    print(sep)
    print("  실시간 데이터 파이프라인 점검")
    print(f"  now_utc  = {now.isoformat()}")
    print(f"  symbol   = {sym}")
    print(f"  window   = {window_sec}s  interval={interval_sec}s  h_sec={h_sec}s")
    print(sep)

    results: dict[str, bool] = {}

    with engine.connect() as conn:
        ok, out = check_market_1s(conn, sym, window_sec, now)
        results["market_1s"] = ok
        print(out)
        print()

        ok, out = check_barrier_state(conn, sym, window_sec, interval_sec)
        results["barrier_state"] = ok
        print(out)
        print()

        ok, out = check_predictions(conn, sym, window_sec, interval_sec)
        results["predictions"] = ok
        print(out)
        print()

        ok, out = check_evaluation_results(conn, sym, window_sec, h_sec)
        results["evaluation_results"] = ok  # optional
        print(out)
        print()

        ok, out = check_paper_decisions(conn, sym, window_sec, interval_sec)
        results["paper_decisions"] = ok
        print(out)
        print()

        out = check_paper_trades(conn, sym, window_sec)
        print(out)
        print()

    ok_dash, out_dash = check_dashboard()
    results["dashboard"] = ok_dash
    print(out_dash)
    print()

    # OVERALL 판정
    core_keys = ["market_1s", "barrier_state", "predictions", "paper_decisions"]
    overall_ok = all(results.get(k, False) for k in core_keys)

    print(sep)
    print("  개별 결과:")
    for k in core_keys:
        v = results.get(k, False)
        print(f"    {k:25s}: {'PASS ✅' if v else 'FAIL ❌'}")
    print(f"    {'evaluation_results':25s}: OPTIONAL (OVERALL 미포함)")
    print(f"    {'dashboard':25s}: {'PASS ✅' if ok_dash else 'FAIL ❌'}")
    print()
    print(f"  OVERALL: {'PASS ✅' if overall_ok else 'FAIL ❌'}")
    print(sep)

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
