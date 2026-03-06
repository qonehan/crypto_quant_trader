# STEP 6.0 결과 보고서 — Paper Trading + Policy + Decision Log

## 1. 추가/수정 파일 목록

| 파일 | 변경 내용 |
|---|---|
| `app/db/models.py` | `PaperPosition`, `PaperTrade`, `PaperDecision` 테이블 추가 |
| `app/config.py` | Paper trading 설정 7개 추가 (PAPER_TRADING_ENABLED, PAPER_INITIAL_KRW, MAX_POSITION_FRAC, MIN_ORDER_KRW, EXIT_EV_RATE_TH, DATA_LAG_SEC_MAX, COST_RMIN_MULT) |
| `app/db/writer.py` | `get_or_create_paper_position`, `update_paper_position`, `insert_paper_trade`, `insert_paper_decision` 함수 추가 |
| `app/trading/__init__.py` | **신규** 패키지 |
| `app/trading/policy.py` | **신규** — `decide_action()` 구현: FLAT→진입 필터(DATA_LAG, SPREAD_WIDE, EV_RATE_LOW, PNONE_HIGH, PDIR_WEAK, COST_GT_RT), LONG→청산(TP/SL/TIME/EV_BAD) |
| `app/trading/paper.py` | **신규** — `execute_enter_long()`, `execute_exit_long()` 페이퍼 브로커 |
| `app/trading/runner.py` | **신규** — `PaperTradingRunner` async 루프, decision log + trade 기록 |
| `app/bot.py` | `PaperTradingRunner` 태스크 추가 (PAPER_TRADING_ENABLED=true일 때) |
| `app/dashboard.py` | [E] Paper Trading 섹션: positions/trades/decisions 테이블 + reason 분포 |
| `.env.example` | Paper trading 설정 주석 추가 |

## 2. Bot 로그 (~30초, Paper 관련 포함)

```
04:35:23 Paper trading enabled
04:35:25 Pred(v1): t0=04:35:25 r_t=0.001000 z=N/A p_none=1.0000 ev=-0.00140000 ev_rate=N/A action=STAY_FLAT
04:35:29 Paper: pos=FLAT action=STAY_FLAT reason=EV_RATE_LOW cash=1000000 qty=0.00000000 equity_est=1000000
04:35:30 Pred(v1): t0=04:35:30 r_t=0.001000 z=N/A p_none=0.9900 ev=-0.00142017 ev_rate=-0.00001184 action=STAY_FLAT
04:35:34 Paper: pos=FLAT action=STAY_FLAT reason=EV_RATE_LOW cash=1000000 qty=0.00000000 equity_est=1000000
04:35:35 Pred(v1): t0=04:35:35 r_t=0.001000 z=N/A p_none=0.9900 ev=-0.00142017 ev_rate=-0.00001184 action=STAY_FLAT
04:35:39 Paper: pos=FLAT action=STAY_FLAT reason=EV_RATE_LOW cash=1000000 qty=0.00000000 equity_est=1000000
04:43:45 Paper: pos=FLAT action=STAY_FLAT reason=EV_RATE_LOW cash=1000000 qty=0.00000000 equity_est=1000000
04:44:50 Paper: pos=FLAT action=STAY_FLAT reason=EV_RATE_LOW cash=1000000 qty=0.00000000 equity_est=1000000
04:45:20 Paper: pos=FLAT action=STAY_FLAT reason=EV_RATE_LOW cash=1000000 qty=0.00000000 equity_est=1000000
```

## 3. 테이블 존재 확인

```
tables: [('paper_decisions',), ('paper_positions',), ('paper_trades',)]
```

## 4. DB 확인 쿼리 출력

### paper_positions
```
('KRW-BTC', 'FLAT', 1000000.0, 0.0, None, None, None, None, 2026-02-18 04:35:29 UTC)
```
- 초기 cash_krw=1,000,000 유지, FLAT 상태

### paper_trades (last 10)
```
(없음 — 거래 0건)
```

### paper_decisions (last 20, 요약)
```
ts                  pos_status  action      reason        ev_rate      p_none   spread_bps  lag_sec  cost_est   r_t
04:45:20 UTC        FLAT        STAY_FLAT   EV_RATE_LOW   -9.48e-06    0.906    0.100       0.17     0.00141    0.001
04:45:15 UTC        FLAT        STAY_FLAT   EV_RATE_LOW   -9.49e-06    0.906    0.100       1.46     0.00141    0.001
04:44:50 UTC        FLAT        STAY_FLAT   EV_RATE_LOW   -1.37e-05    0.904    2.397       0.28     0.00164    0.001
04:44:30 UTC        FLAT        STAY_FLAT   EV_RATE_LOW   -1.40e-05    0.893    2.497       0.21     0.00165    0.001
...
```
- 총 decisions: **119행** (10분, 5초 간격)

### reason 분포
```
EV_RATE_LOW: 119건 (100%)
```

## 5. 거래 0건 분석 — Decision Reason 상위 3개

| Reason | Count | 의미 |
|---|---|---|
| **EV_RATE_LOW** | 119 | ev_rate < 0 (ENTER_EV_RATE_TH=0.0 미충족). ev_rate가 -9e-06 ~ -1.4e-05로 일관되게 음수 |

**왜 EV_RATE_LOW만 나오는가:** Policy에서 첫 번째 통과 필터가 DATA_LAG/SPREAD_WIDE이고, 이후 ev_rate 검사에서 항상 음수이므로 다음 필터(PNONE_HIGH, PDIR_WEAK, COST_GT_RT)까지 도달하지 않는다. 근본 원인은 Step 5.5에서 확인한 대로 **왕복 비용(~0.0014) > 배리어 r_t(0.001)**이므로 EV가 구조적으로 음수이고, 따라서 ev_rate도 음수.

## DoD 체크리스트

- [x] paper_positions 1행 생성 (FLAT, cash_krw=1,000,000 유지)
- [x] paper_decisions 매 tick마다 쌓임 (119행 / 10분)
- [x] 거래 0건이지만 reason="EV_RATE_LOW"로 설명 가능
- [x] bot 크래시 없이 10분+ 지속 실행
- [x] 대시보드 [E] Paper Trading 섹션 구현 (positions/trades/decisions + reason 분포)
