"""
feature_leak_check.py — feature_snapshots 미래값 사용(데이터 누수) 점검

검사 원칙:
  bin_mark_ts, oi_ts, liq_last_ts 는 반드시 <= ts(snapshot timestamp) 이어야 한다.
  위반 row가 1건이라도 있으면 FAIL.

사용법:
  poetry run python -m app.diagnostics.feature_leak_check
  poetry run python -m app.diagnostics.feature_leak_check --window 600
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


# ──────────────────────────────────────────────────────────────────────────────
# 체크 함수
# ──────────────────────────────────────────────────────────────────────────────


def check_source_ts_not_future(conn, symbol: str, window_sec: int) -> tuple[bool, str]:
    """bin_mark_ts, oi_ts가 각 snapshot ts를 초과하지 않는지 검사."""
    lines = [f"[feature_leak_check — source_ts <= snapshot_ts (window={window_sec}s)]"]

    # 1. bin_mark_ts 누수
    rows, err = _safe_query(
        conn,
        f"""
        SELECT count(*) AS violations
        FROM feature_snapshots
        WHERE symbol = :sym
          AND ts >= now() AT TIME ZONE 'UTC' - interval '{window_sec} seconds'
          AND bin_mark_ts IS NOT NULL
          AND bin_mark_ts > ts
        """,
        {"sym": symbol},
    )
    if err or rows is None:
        lines.append(f"  bin_mark_ts check ERROR: {err}")
        return False, "\n".join(lines)
    bin_violations = rows[0][0]
    lines.append(f"  bin_mark_ts > ts violations: {bin_violations}")

    # 2. oi_ts 누수
    rows2, err2 = _safe_query(
        conn,
        f"""
        SELECT count(*) AS violations
        FROM feature_snapshots
        WHERE symbol = :sym
          AND ts >= now() AT TIME ZONE 'UTC' - interval '{window_sec} seconds'
          AND oi_ts IS NOT NULL
          AND oi_ts > ts
        """,
        {"sym": symbol},
    )
    if err2 or rows2 is None:
        lines.append(f"  oi_ts check ERROR: {err2}")
        return False, "\n".join(lines)
    oi_violations = rows2[0][0]
    lines.append(f"  oi_ts > ts violations: {oi_violations}")

    # 3. liq_last_ts 누수
    rows3, err3 = _safe_query(
        conn,
        f"""
        SELECT count(*) AS violations
        FROM feature_snapshots
        WHERE symbol = :sym
          AND ts >= now() AT TIME ZONE 'UTC' - interval '{window_sec} seconds'
          AND liq_last_ts IS NOT NULL
          AND liq_last_ts > ts
        """,
        {"sym": symbol},
    )
    if err3 or rows3 is None:
        lines.append(f"  liq_last_ts check ERROR: {err3}")
        return False, "\n".join(lines)
    liq_violations = rows3[0][0]
    lines.append(f"  liq_last_ts > ts violations: {liq_violations}")

    total_violations = bin_violations + oi_violations + liq_violations
    ok = total_violations == 0

    if not ok:
        lines.append(f"  총 위반 rows: {total_violations} — 샘플 5건:")
        sample_rows, _ = _safe_query(
            conn,
            f"""
            SELECT ts, bin_mark_ts, oi_ts, liq_last_ts
            FROM feature_snapshots
            WHERE symbol = :sym
              AND ts >= now() AT TIME ZONE 'UTC' - interval '{window_sec} seconds'
              AND (
                (bin_mark_ts IS NOT NULL AND bin_mark_ts > ts) OR
                (oi_ts IS NOT NULL AND oi_ts > ts) OR
                (liq_last_ts IS NOT NULL AND liq_last_ts > ts)
              )
            ORDER BY ts DESC LIMIT 5
            """,
            {"sym": symbol},
        )
        for r in (sample_rows or []):
            lines.append(f"    {r}")
    else:
        lines.append("  ✅ 미래값 사용 위반 없음")

    lines.append(f"  → {'PASS ✅' if ok else 'FAIL ❌'}")
    return ok, "\n".join(lines)


def check_source_ts_coverage(conn, symbol: str, window_sec: int) -> tuple[bool, str]:
    """source_ts 컬럼이 최근 rows에 존재하는지(NULL이 너무 많지 않은지) 확인."""
    lines = [f"[feature_leak_check — source_ts coverage (window={window_sec}s)]"]

    rows, err = _safe_query(
        conn,
        f"""
        SELECT
            count(*) AS total,
            count(*) FILTER (WHERE bin_mark_ts IS NOT NULL) AS has_mark_ts,
            count(*) FILTER (WHERE oi_ts IS NOT NULL)       AS has_oi_ts
        FROM feature_snapshots
        WHERE symbol = :sym
          AND ts >= now() AT TIME ZONE 'UTC' - interval '{window_sec} seconds'
        """,
        {"sym": symbol},
    )
    if err or rows is None:
        lines.append(f"  ERROR: {err}")
        return False, "\n".join(lines)

    total, has_mark, has_oi = rows[0]
    total = total or 1
    lines.append(f"  total rows (window): {total}")
    lines.append(f"  bin_mark_ts coverage: {has_mark}/{total} ({has_mark/total*100:.1f}%)")
    lines.append(f"  oi_ts coverage:       {has_oi}/{total} ({has_oi/total*100:.1f}%)")
    lines.append("  ※ coverage < 100% 는 source_ts 컬럼 추가 전 수집분 (이전 data — 정상)")

    ok = True  # coverage는 정보성(PASS만 반환, 낮아도 구버전 data이므로 FAIL하지 않음)
    lines.append(f"  → {'PASS ✅' if ok else 'FAIL ❌'} (coverage 체크는 정보성)")
    return ok, "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="feature_snapshots 데이터 누수 점검")
    parser.add_argument("--window", type=int, default=600, help="점검 윈도우(초, 기본 600)")
    args = parser.parse_args()
    window_sec = args.window

    s = load_settings()
    symbol = s.SYMBOL
    engine = create_engine(s.DB_URL)

    now = _now()
    sep = "=" * 60
    print(sep)
    print("  feature_leak_check — 미래값(데이터 누수) 점검")
    print(f"  now_utc  = {now.isoformat()}")
    print(f"  symbol   = {symbol}")
    print(f"  window   = {window_sec}s")
    print(sep)

    results: dict[str, bool] = {}

    with engine.connect() as conn:
        ok, out = check_source_ts_not_future(conn, symbol, window_sec)
        results["no_future_ts"] = ok
        print(out)
        print()

        ok, out = check_source_ts_coverage(conn, symbol, window_sec)
        results["coverage"] = ok
        print(out)
        print()

    # OVERALL: no_future_ts가 핵심 (coverage는 정보성이므로 포함)
    overall_ok = all(results.values())

    print(sep)
    print("  개별 결과:")
    for k, v in results.items():
        print(f"    {k:25s}: {'PASS ✅' if v else 'FAIL ❌'}")
    print()
    print(f"  OVERALL: {'PASS ✅' if overall_ok else 'FAIL ❌'}")
    print(sep)

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
