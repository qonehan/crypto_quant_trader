"""
coinglass_check.py — Coinglass 수집 상태 점검

COINGLASS_ENABLED=False(기본) 이면 SKIP PASS.
COINGLASS_ENABLED=True 이면 키 + 데이터 + lag 모두 체크.

사용법:
  poetry run python -m app.diagnostics.coinglass_check
  poetry run python -m app.diagnostics.coinglass_check --window 600
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from app.config import is_real_key, load_settings


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


# ──────────────────────────────────────────────────────────────────────────────
# 체크 함수
# ──────────────────────────────────────────────────────────────────────────────


def check_coinglass_collection(
    conn, symbol: str, window_sec: int, cg_enabled: bool, key_is_real: bool
) -> tuple[bool, str]:
    lines = ["[coinglass_collection]"]

    if not cg_enabled:
        lines.append("  COINGLASS_ENABLED=False → SKIP ✅")
        return True, "\n".join(lines)

    if not key_is_real:
        lines.append("  COINGLASS_ENABLED=True 이나 API 키 비정상 — 설정 오류 ❌")
        lines.append("  → FAIL ❌ (.env에서 COINGLASS_API_KEY를 실제 키로 교체 필요)")
        return False, "\n".join(lines)

    # 데이터 있는지 확인
    rows, err = _safe_query(
        conn,
        "SELECT max(ts) AS max_ts, count(*) AS cnt FROM coinglass_liquidation_map WHERE symbol=:sym",
        {"sym": symbol},
    )
    if err or rows is None:
        lines.append(f"  ERROR: {err}")
        return False, "\n".join(lines)

    max_ts, total_cnt = rows[0]
    lag = _lag(max_ts)
    lines.append(f"  max(ts)   = {max_ts}")
    lines.append(f"  total cnt = {total_cnt}")

    # window 내 count
    rows2, _ = _safe_query(
        conn,
        f"""
        SELECT count(*) AS cnt
        FROM coinglass_liquidation_map
        WHERE symbol = :sym
          AND ts >= now() AT TIME ZONE 'UTC' - interval '{window_sec} seconds'
        """,
        {"sym": symbol},
    )
    window_cnt = rows2[0][0] if rows2 else 0
    lines.append(f"  count(window={window_sec}s) = {window_cnt}")

    if lag is None or lag > window_sec:
        lines.append(f"  lag={lag}s > window={window_sec}s → FAIL ❌")
        return False, "\n".join(lines)
    if window_cnt < 1:
        lines.append(f"  window 내 데이터 없음 → FAIL ❌")
        return False, "\n".join(lines)

    lines.append(f"  lag={lag:.0f}s ✅  window_count={window_cnt} ≥ 1 ✅")
    lines.append("  → PASS ✅")
    return True, "\n".join(lines)


def check_call_status(conn, window_sec: int) -> tuple[bool, str]:
    """coinglass_call_status 테이블에서 최근 성공/실패 이력."""
    lines = ["[coinglass_call_status]"]

    rows, err = _safe_query(
        conn,
        """
        SELECT ok, ts, http_status, error_msg, latency_ms
        FROM coinglass_call_status
        ORDER BY ts DESC LIMIT 1
        """,
    )
    if err:
        lines.append(f"  coinglass_call_status not available: {err}")
        lines.append("  → SKIP (테이블 없음)")
        return True, "\n".join(lines)
    if not rows:
        lines.append("  no call records yet")
        lines.append("  → SKIP")
        return True, "\n".join(lines)

    last = rows[0]
    lines.append(f"  last call: ok={last[0]} ts={last[1]} http={last[2]} latency={last[4]}ms")
    if last[3]:
        lines.append(f"  last error: {last[3][:200]}")

    # 24h success count
    rows2, _ = _safe_query(
        conn,
        "SELECT count(*) FROM coinglass_call_status WHERE ok=true AND ts >= now() - interval '24 hours'",
    )
    ok_24h = rows2[0][0] if rows2 else 0
    rows3, _ = _safe_query(
        conn,
        "SELECT count(*) FROM coinglass_call_status WHERE ts >= now() - interval '24 hours'",
    )
    total_24h = rows3[0][0] if rows3 else 0
    lines.append(f"  24h: {ok_24h}/{total_24h} success")
    lines.append("  → INFO (call_status 정보성)")
    return True, "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Coinglass 수집 상태 점검")
    parser.add_argument("--window", type=int, default=600, help="점검 윈도우(초, 기본 600)")
    args = parser.parse_args()
    window_sec = args.window

    s = load_settings()
    symbol = s.ALT_SYMBOL_COINGLASS
    cg_enabled = getattr(s, "COINGLASS_ENABLED", False)
    key_is_real = is_real_key(s.COINGLASS_API_KEY)

    engine = create_engine(s.DB_URL)

    now = _now()
    sep = "=" * 60
    print(sep)
    print("  Coinglass 수집 상태 점검")
    print(f"  now_utc            = {now.isoformat()}")
    print(f"  coinglass_symbol   = {symbol}")
    print(f"  window             = {window_sec}s")
    print(f"  COINGLASS_ENABLED  = {cg_enabled}")
    print(f"  COINGLASS_KEY_REAL = {key_is_real}")
    print(sep)

    results: dict[str, bool] = {}

    with engine.connect() as conn:
        ok, out = check_coinglass_collection(
            conn, symbol, window_sec, cg_enabled, key_is_real
        )
        results["collection"] = ok
        print(out)
        print()

        ok, out = check_call_status(conn, window_sec)
        results["call_status"] = ok
        print(out)
        print()

    overall_ok = all(results.values())

    print(sep)
    print("  개별 결과:")
    for k, v in results.items():
        print(f"    {k:20s}: {'PASS ✅' if v else 'FAIL ❌'}")
    print()
    print(f"  OVERALL: {'PASS ✅' if overall_ok else 'FAIL ❌'}")
    if not cg_enabled:
        print("  ※ COINGLASS_ENABLED=False → SKIP PASS (수집 비활성 상태)")
    print(sep)

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
