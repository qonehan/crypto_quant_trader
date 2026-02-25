"""
continuity_check.py — predictions / market_1s 연속성(갭) 진단

사용법:
  poetry run python -m app.diagnostics.continuity_check
  poetry run python -m app.diagnostics.continuity_check --hours 48 --pred-gap-sec 60 --mkt-gap-sec 10

PASS 조건:
  - predictions.max_gap_sec  <= pred-gap-sec  (기본 60s)
  - market_1s.max_gap_sec    <= mkt-gap-sec   (기본 10s)
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from app.config import load_settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


def check_table_continuity(
    engine,
    table: str,
    ts_col: str,
    hours: int,
    gap_threshold_sec: float,
    symbol: str | None = None,
    symbol_col: str | None = None,
) -> tuple[bool, dict]:
    """LAG() 기반으로 max_gap_sec과 전체 row 수를 반환."""
    # hours와 table/col은 제어된 정수/식별자 값이므로 f-string으로 직접 삽입 (안전)
    params: dict = {}
    symbol_filter = ""
    if symbol and symbol_col:
        symbol_filter = f"AND {symbol_col} = :sym"
        params["sym"] = symbol

    sql = f"""
        WITH x AS (
            SELECT
                {ts_col},
                LAG({ts_col}) OVER (ORDER BY {ts_col}) AS prev_ts
            FROM {table}
            WHERE {ts_col} > now() - make_interval(hours => {int(hours)})
              {symbol_filter}
        )
        SELECT
            COUNT(*)                                                        AS n,
            MAX(EXTRACT(EPOCH FROM ({ts_col} - prev_ts)))                   AS max_gap_sec,
            AVG(EXTRACT(EPOCH FROM ({ts_col} - prev_ts)))                   AS avg_gap_sec,
            MIN({ts_col})                                                   AS ts_min,
            MAX({ts_col})                                                   AS ts_max
        FROM x
        WHERE prev_ts IS NOT NULL;
    """
    with engine.connect() as conn:
        row = conn.execute(text(sql), params).fetchone()

    n = int(row[0]) if row[0] is not None else 0
    max_gap = float(row[1]) if row[1] is not None else float("nan")
    avg_gap = float(row[2]) if row[2] is not None else float("nan")
    ts_min = row[3]
    ts_max = row[4]

    ok = (n > 0) and (max_gap <= gap_threshold_sec)

    return ok, {
        "table": table,
        "n": n,
        "max_gap_sec": max_gap,
        "avg_gap_sec": avg_gap,
        "ts_min": str(ts_min) if ts_min else "N/A",
        "ts_max": str(ts_max) if ts_max else "N/A",
        "threshold_sec": gap_threshold_sec,
        "pass": ok,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="데이터 연속성(갭) 진단")
    parser.add_argument("--hours", type=int, default=48, help="조회 시간 범위 (기본 48h)")
    parser.add_argument(
        "--pred-gap-sec",
        type=float,
        default=60.0,
        help="predictions max gap 허용치(초, 기본 60s). 초과시 FAIL.",
    )
    parser.add_argument(
        "--mkt-gap-sec",
        type=float,
        default=10.0,
        help="market_1s max gap 허용치(초, 기본 10s). 초과시 FAIL.",
    )
    args = parser.parse_args()

    s = load_settings()
    engine = create_engine(s.DB_URL, pool_pre_ping=True)
    symbol = s.SYMBOL

    sep = "=" * 60
    now = _now()
    print(sep)
    print("  Continuity Check (데이터 연속성 진단)")
    print(f"  now_utc           = {now.isoformat()}")
    print(f"  hours             = {args.hours}h")
    print(f"  pred_gap_thr      = {args.pred_gap_sec}s")
    print(f"  mkt_gap_thr       = {args.mkt_gap_sec}s")
    print(sep)

    results = {}

    # predictions 체크
    print("\n[1] predictions 연속성 체크...")
    ok_pred, info_pred = check_table_continuity(
        engine,
        table="predictions",
        ts_col="t0",
        hours=args.hours,
        gap_threshold_sec=args.pred_gap_sec,
        symbol=symbol,
        symbol_col="symbol",
    )
    results["predictions"] = ok_pred
    print(f"  n              = {info_pred['n']:,}")
    print(f"  ts_range       = {info_pred['ts_min']} ~ {info_pred['ts_max']}")
    print(f"  max_gap_sec    = {info_pred['max_gap_sec']:.1f}s  (threshold={args.pred_gap_sec}s)")
    print(f"  avg_gap_sec    = {info_pred['avg_gap_sec']:.2f}s")
    if info_pred["n"] == 0:
        print("  ⚠️  rows=0 → 데이터 없음")
    elif info_pred["max_gap_sec"] > args.pred_gap_sec:
        print(f"  ❌ max_gap_sec({info_pred['max_gap_sec']:.1f}s) > threshold({args.pred_gap_sec}s)")
    print(f"  → {'PASS ✅' if ok_pred else 'FAIL ❌'}")

    # market_1s 체크
    print("\n[2] market_1s 연속성 체크...")
    ok_mkt, info_mkt = check_table_continuity(
        engine,
        table="market_1s",
        ts_col="ts",
        hours=args.hours,
        gap_threshold_sec=args.mkt_gap_sec,
        symbol=symbol,
        symbol_col="symbol",
    )
    results["market_1s"] = ok_mkt
    print(f"  n              = {info_mkt['n']:,}")
    print(f"  ts_range       = {info_mkt['ts_min']} ~ {info_mkt['ts_max']}")
    print(f"  max_gap_sec    = {info_mkt['max_gap_sec']:.1f}s  (threshold={args.mkt_gap_sec}s)")
    print(f"  avg_gap_sec    = {info_mkt['avg_gap_sec']:.2f}s")
    if info_mkt["n"] == 0:
        print("  ⚠️  rows=0 → 데이터 없음")
    elif info_mkt["max_gap_sec"] > args.mkt_gap_sec:
        print(f"  ❌ max_gap_sec({info_mkt['max_gap_sec']:.1f}s) > threshold({args.mkt_gap_sec}s)")
    print(f"  → {'PASS ✅' if ok_mkt else 'FAIL ❌'}")

    # 연속 구간 분석
    print("\n[3] 연속 구간 분석 (segments)...")
    _print_segment_analysis(engine, symbol, args.hours, args.pred_gap_sec)

    # 요약
    overall_ok = ok_pred and ok_mkt
    print(f"\n{sep}")
    print("  결과 요약:")
    print(f"    predictions : {'PASS ✅' if ok_pred else 'FAIL ❌'}  "
          f"(max_gap={info_pred['max_gap_sec']:.1f}s, n={info_pred['n']:,})")
    print(f"    market_1s   : {'PASS ✅' if ok_mkt else 'FAIL ❌'}  "
          f"(max_gap={info_mkt['max_gap_sec']:.1f}s, n={info_mkt['n']:,})")
    print()
    print(f"  OVERALL: {'PASS ✅' if overall_ok else 'FAIL ❌'}")
    print(sep)

    return 0 if overall_ok else 1


def _print_segment_analysis(engine, symbol: str, hours: int, gap_threshold_sec: float) -> None:
    """predictions의 연속 구간별 rows/시간 분석."""
    sql = f"""
        WITH ordered AS (
            SELECT t0,
                   LAG(t0) OVER (ORDER BY t0) AS prev_t0
            FROM predictions
            WHERE symbol = :sym
              AND t0 > now() - make_interval(hours => {int(hours)})
            ORDER BY t0
        ),
        flagged AS (
            SELECT t0,
                   CASE
                     WHEN prev_t0 IS NULL THEN 1
                     WHEN EXTRACT(EPOCH FROM (t0 - prev_t0)) > :gap THEN 1
                     ELSE 0
                   END AS is_new_seg
            FROM ordered
        ),
        numbered AS (
            SELECT t0,
                   SUM(is_new_seg) OVER (ORDER BY t0 ROWS UNBOUNDED PRECEDING) AS seg_id
            FROM flagged
        )
        SELECT
            seg_id,
            COUNT(*)         AS n_rows,
            MIN(t0)          AS seg_start,
            MAX(t0)          AS seg_end,
            EXTRACT(EPOCH FROM (MAX(t0) - MIN(t0))) AS duration_sec
        FROM numbered
        GROUP BY seg_id
        ORDER BY n_rows DESC;
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(sql),
            {"sym": symbol, "gap": gap_threshold_sec},
        ).fetchall()

    if not rows:
        print("  데이터 없음")
        return

    print(f"  총 segments = {len(rows)}")
    for i, r in enumerate(rows[:5]):  # 상위 5개만 출력
        seg_id, n_rows, seg_start, seg_end, dur = r
        dur_h = float(dur) / 3600 if dur else 0
        print(f"  seg#{int(seg_id):2d}: rows={int(n_rows):4d}  "
              f"dur={dur_h:.2f}h  "
              f"[{str(seg_start)[:19]} ~ {str(seg_end)[:19]}]"
              + (" ← largest" if i == 0 else ""))
    if len(rows) > 5:
        print(f"  ... (총 {len(rows)}개 구간)")


if __name__ == "__main__":
    sys.exit(main())
