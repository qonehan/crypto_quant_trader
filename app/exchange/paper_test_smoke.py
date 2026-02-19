"""Step 11: paper_test_smoke — 자동 TEST 연동 상태 진단 스크립트.

봇이 이미 실행 중인 상태에서 "paper_trades → test_ok 연동 여부"를 확인.
이 스크립트 자체는 봇을 실행하지 않음.

실행:
  poetry run python -m app.exchange.paper_test_smoke [--window 600]

종료코드:
  0 = PASS (paper_trades >= 1 AND test_ok >= 1 in window) OR keys missing (SKIP)
  1 = FAIL (paper_trades 있으나 test_ok 없음, 또는 blocked/error만 있음)
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from app.config import load_settings
from app.db.session import get_engine

_SEP = "=" * 60


def main(window_sec: int = 600) -> int:
    s = load_settings()

    if not s.UPBIT_ACCESS_KEY or not s.UPBIT_SECRET_KEY:
        print(f"ℹ️  SKIP (keys missing) — shadow 모드에서는 test_ok가 쌓이지 않음")
        print(f"   Set UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY in .env and UPBIT_TRADE_MODE=test")
        return 0

    print(_SEP)
    print(f"paper_test_smoke: window={window_sec}s  symbol={s.SYMBOL}")
    print(_SEP)

    engine = get_engine(s)

    # ── 1) paper_trades count in window ────────────────────────────────────
    with engine.connect() as conn:
        paper_cnt = conn.execute(
            text("""
                SELECT count(*) FROM paper_trades
                WHERE symbol = :sym
                  AND t >= now() - (:win || ' seconds')::interval
            """),
            {"sym": s.SYMBOL, "win": str(window_sec)},
        ).scalar() or 0

    print(f"[1] paper_trades (last {window_sec}s): {paper_cnt}")

    # ── 2) upbit_order_attempts test_ok count in window ────────────────────
    with engine.connect() as conn:
        test_ok_cnt = conn.execute(
            text("""
                SELECT count(*) FROM upbit_order_attempts
                WHERE symbol = :sym
                  AND mode = 'test'
                  AND status = 'test_ok'
                  AND ts >= now() - (:win || ' seconds')::interval
            """),
            {"sym": s.SYMBOL, "win": str(window_sec)},
        ).scalar() or 0

    print(f"[2] upbit_order_attempts mode=test status=test_ok (last {window_sec}s): {test_ok_cnt}")

    # ── 3) PASS check ──────────────────────────────────────────────────────
    if paper_cnt >= 1 and test_ok_cnt >= 1:
        print()
        print(f"✅ PASS — paper_trades={paper_cnt}  test_ok={test_ok_cnt}")
        print(_SEP)
        return 0

    # ── 4) FAIL: show blocked/throttled/error rows ────────────────────────
    print()
    print(f"❌ FAIL — paper={paper_cnt}  test_ok={test_ok_cnt}")
    print()

    with engine.connect() as conn:
        fail_rows = conn.execute(
            text("""
                SELECT ts, action, status, error_msg, blocked_reasons
                FROM upbit_order_attempts
                WHERE symbol = :sym
                  AND status IN ('blocked', 'throttled', 'error')
                  AND ts >= now() - (:win || ' seconds')::interval
                ORDER BY ts DESC
                LIMIT 20
            """),
            {"sym": s.SYMBOL, "win": str(window_sec)},
        ).fetchall()

    if fail_rows:
        print(f"[3] blocked/throttled/error rows (last {window_sec}s, max 20):")
        for r in fail_rows:
            print(
                f"  ts={str(r.ts)[:19]}  action={r.action}  status={r.status}"
                f"  error_msg={r.error_msg or '-'}"
            )
            if r.blocked_reasons:
                print(f"    blocked_reasons={r.blocked_reasons}")
    else:
        print(f"[3] No blocked/throttled/error rows in window — bot may not be running or no paper trades yet")

    # ── 5) blocked_reasons top 5 ──────────────────────────────────────────
    print()
    try:
        with engine.connect() as conn:
            top_reasons = conn.execute(
                text("""
                    SELECT reason, count(*) AS cnt
                    FROM (
                        SELECT jsonb_array_elements_text(blocked_reasons) AS reason
                        FROM upbit_order_attempts
                        WHERE symbol = :sym
                          AND blocked_reasons IS NOT NULL
                          AND ts >= now() - (:win || ' seconds')::interval
                    ) sub
                    GROUP BY reason
                    ORDER BY cnt DESC
                    LIMIT 5
                """),
                {"sym": s.SYMBOL, "win": str(window_sec)},
            ).fetchall()

        if top_reasons:
            print(f"[4] blocked_reasons top 5:")
            for r in top_reasons:
                print(f"  {r.reason}: {r.cnt}건")
        else:
            print(f"[4] blocked_reasons: (none in window)")
    except Exception as e:
        print(f"[4] blocked_reasons query error: {e}")

    print()
    print("Hint: 아래 설정을 확인하세요:")
    print("  UPBIT_TRADE_MODE=test")
    print("  UPBIT_ORDER_TEST_ENABLED=true")
    print("  UPBIT_TEST_ON_PAPER_TRADES=true")
    print(f"  PAPER_POLICY_PROFILE={s.UPBIT_TEST_REQUIRE_PAPER_PROFILE}  (현재={s.PAPER_POLICY_PROFILE})")
    print(_SEP)
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="paper_test_smoke")
    parser.add_argument("--window", type=int, default=600, help="Look-back window in seconds")
    args = parser.parse_args()
    sys.exit(main(args.window))
