"""Step 9 리컨실리에이션 스크립트 — 주문/계좌/포지션 정합성 점검.

키가 없으면 SKIP (종료코드 0).
키가 있으면:
  1) Upbit list_open_orders → 현재 미체결 주문 목록
  2) DB에서 status=submitted, final_state IS NULL인 attempt 목록
  3) 두 목록을 대조하여 DB에만 있거나 exchange에만 있는 항목 리포트
  (자동 취소/수정 절대 없음 — 조회 및 리포트만)

주의: 이 스크립트는 DB를 변경하지 않음. 결과를 stdout에만 출력.

사용법:
  poetry run python -m app.exchange.reconcile
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from sqlalchemy import text

from app.config import load_settings
from app.db.session import get_engine
from app.exchange.upbit_rest import UpbitApiError, UpbitRestClient, parse_remaining_req


def main() -> int:
    s = load_settings()

    if not s.UPBIT_ACCESS_KEY or not s.UPBIT_SECRET_KEY:
        print("ℹ️  UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 미설정 — 리컨실리에이션 SKIP")
        print("   (shadow 모드에서는 실제 주문이 없으므로 리컨실리에이션 불필요)")
        return 0

    engine = get_engine(s)
    client = UpbitRestClient(
        access_key=s.UPBIT_ACCESS_KEY,
        secret_key=s.UPBIT_SECRET_KEY,
        base_url=s.UPBIT_API_BASE,
        timeout=s.UPBIT_REST_TIMEOUT_SEC,
        max_retry=s.UPBIT_REST_MAX_RETRY,
    )

    print(f"Reconcile: {s.SYMBOL}  [{datetime.now(timezone.utc).isoformat()}]")
    print("=" * 60)

    # ── 1) Exchange: 미체결 주문 ────────────────────────────────
    print(f"[1] GET /v1/orders/open?market={s.SYMBOL}")
    open_orders: list[dict] = []
    try:
        open_orders = client.list_open_orders(s.SYMBOL)
        meta = client._last_call_meta
        parsed = parse_remaining_req(meta.get("remaining_req"))
        print(f"  미체결 주문 {len(open_orders)}건  "
              f"remaining-req: sec={parsed.get('sec')} min={parsed.get('min')}")
        for o in open_orders:
            print(
                f"  uuid={o.get('uuid')}  side={o.get('side')}  "
                f"state={o.get('state')}  remaining_volume={o.get('remaining_volume')}"
            )
    except UpbitApiError as e:
        print(f"  ERROR: {e}  (http={e.http_status})")
        return 1
    except Exception as e:
        print(f"  ERROR: {e}")
        return 1

    # ── 2) DB: submitted 상태 + final_state IS NULL ──────────────
    print(f"[2] DB upbit_order_attempts (submitted, no final_state)")
    db_submitted: list[dict] = []
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT id, ts, action, identifier, uuid, mode, status, final_state
                    FROM upbit_order_attempts
                    WHERE symbol = :sym
                      AND status = 'submitted'
                      AND final_state IS NULL
                    ORDER BY ts DESC
                    LIMIT 100
                """),
                {"sym": s.SYMBOL},
            ).fetchall()
            db_submitted = [dict(r._mapping) for r in rows]
    except Exception as e:
        print(f"  DB error: {e}")
        return 1

    print(f"  DB submitted 건수: {len(db_submitted)}")
    for row in db_submitted:
        print(
            f"  id={row['id']}  uuid={row['uuid']}  identifier={row['identifier']}  "
            f"ts={str(row['ts'])[:19]}"
        )

    # ── 3) 대조 ─────────────────────────────────────────────────
    print("[3] 대조 결과")
    exchange_uuids = {o.get("uuid") for o in open_orders if o.get("uuid")}
    db_uuids = {r["uuid"] for r in db_submitted if r.get("uuid")}

    only_in_db = db_uuids - exchange_uuids
    only_in_exchange = exchange_uuids - db_uuids
    matched = db_uuids & exchange_uuids

    print(f"  MATCHED (DB + exchange 모두 있음): {len(matched)}건")
    for u in matched:
        print(f"    uuid={u}")

    if only_in_db:
        print(f"  ⚠️  DB에만 있음 (exchange에 없음, 이미 체결/취소됐을 가능성): {len(only_in_db)}건")
        for u in only_in_db:
            print(f"    uuid={u}  → DB final_state 미갱신 가능성 — get_order로 확인 필요")

    if only_in_exchange:
        print(f"  ⚠️  Exchange에만 있음 (DB 미기록): {len(only_in_exchange)}건")
        for u in only_in_exchange:
            print(f"    uuid={u}")

    if not only_in_db and not only_in_exchange and not matched:
        print("  ✅ 미체결 주문 없음, DB submitted 없음 — 정합성 OK")
    elif not only_in_db and not only_in_exchange:
        print("  ✅ 불일치 없음")

    # ── 4) /v1/orders/chance (가능 여부 확인) ─────────────────────
    print(f"[4] GET /v1/orders/chance?market={s.SYMBOL}")
    try:
        chance = client.get_orders_chance(s.SYMBOL)
        meta = client._last_call_meta
        parsed = parse_remaining_req(meta.get("remaining_req"))
        bid_acc = chance.get("bid_account", {})
        ask_acc = chance.get("ask_account", {})
        print(
            f"  bid_fee={chance.get('bid_fee')}  ask_fee={chance.get('ask_fee')}"
        )
        print(
            f"  bid_available={bid_acc.get('balance')}  ask_available={ask_acc.get('balance')}"
        )
        print(f"  remaining-req: sec={parsed.get('sec')} min={parsed.get('min')}")
    except Exception as e:
        print(f"  SKIP (orders/chance): {e}")

    print("=" * 60)
    print("Reconcile 완료. DB 변경 없음.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
