"""
prune_altdata.py — Alt Data / Feature Snapshots retention 정리

사용법:
  poetry run python -m app.diagnostics.prune_altdata
  poetry run python -m app.diagnostics.prune_altdata --days 7
  poetry run python -m app.diagnostics.prune_altdata --days 3 --dry-run
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import create_engine, text

from app.config import load_settings

# 기본 retention: 7일
_DEFAULT_DAYS = 7

# 대상 테이블 + 타임스탬프 컬럼
_PRUNE_TARGETS = [
    ("binance_mark_price_1s", "ts"),   # 가장 큰 테이블 (1s cadence)
    ("binance_force_orders", "ts"),    # 이벤트 기반
    ("binance_futures_metrics", "ts"), # 60s cadence, 상대적으로 작음
    ("feature_snapshots", "ts"),       # 5s cadence
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Alt Data retention 정리")
    parser.add_argument("--days", type=int, default=_DEFAULT_DAYS, help=f"보관 기간(일, 기본 {_DEFAULT_DAYS})")
    parser.add_argument("--dry-run", action="store_true", help="삭제 없이 대상 행 수만 출력")
    args = parser.parse_args()

    s = load_settings()
    engine = create_engine(s.DB_URL)

    sep = "=" * 60
    print(sep)
    print(f"  prune_altdata — retention={args.days}일  dry_run={args.dry_run}")
    print(sep)

    total_deleted = 0
    with engine.begin() as conn:
        for table, ts_col in _PRUNE_TARGETS:
            # 삭제 대상 행 수 확인
            cnt_row = conn.execute(
                text(f"""
                    SELECT count(*) AS cnt
                    FROM {table}
                    WHERE {ts_col} < now() AT TIME ZONE 'UTC' - interval '{args.days} days'
                """)
            ).fetchone()
            cnt = cnt_row.cnt if cnt_row else 0
            print(f"  [{table}] 삭제 대상: {cnt}행 (older than {args.days}d)")

            if cnt == 0:
                continue

            if args.dry_run:
                print(f"    dry-run: SKIP (실제 삭제 안 함)")
            else:
                conn.execute(
                    text(f"""
                        DELETE FROM {table}
                        WHERE {ts_col} < now() AT TIME ZONE 'UTC' - interval '{args.days} days'
                    """)
                )
                print(f"    → {cnt}행 삭제 완료")
                total_deleted += cnt

    print(sep)
    if args.dry_run:
        print("  dry-run 완료 (삭제 없음)")
    else:
        print(f"  총 {total_deleted}행 삭제 완료")
    print(sep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
