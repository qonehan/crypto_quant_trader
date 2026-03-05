# Step 6.2 결과 보고서 — Paper 성과/리스크 매니저 + Equity Curve

## 1. 추가/수정 파일 목록

| 파일 | 변경 내용 |
|---|---|
| `app/config.py` | PAPER_MAX_DRAWDOWN_PCT, PAPER_DAILY_LOSS_LIMIT_PCT, PAPER_HALT_COOLDOWN_MIN, PAPER_EQUITY_LOG_ENABLED, PAPER_POLICY_PROFILE, TEST_* 설정 추가 |
| `app/db/migrate.py` | `_MIG_PAPER_POSITIONS` (initial_krw, equity_high, day_start_date, day_start_equity, halted, halt_reason, halted_at), `_MIG_PAPER_DECISIONS` (cash_krw, qty, equity_est, drawdown_pct, policy_profile) 멱등 ALTER 추가 |
| `app/db/models.py` | `PaperPosition`, `PaperDecision` 모델에 신규 컬럼 반영 |
| `app/db/writer.py` | `get_or_create_paper_position()` — initial_krw/equity_high/day_start_* 초기화; `_UPDATE_PAPER_POS` SQL 신규 컬럼 포함; `_INSERT_PAPER_DECISION` SQL equity 컬럼 포함 |
| `app/trading/policy.py` | HALTED 체크, `_get_thresholds()` strict/test 분기, test 모드 RATE_LIMIT/COOLDOWN 구현 |
| `app/trading/runner.py` | equity_est 계산, equity_high 업데이트, day 변경 감지, drawdown 계산, HALT 판정, 리스크 필드 매 tick 저장, decision에 equity 정보 기록 |
| `app/dashboard.py` | `[E] Paper Trading` 섹션에 Equity Curve (6h), Drawdown chart, Trade Stats (win_rate/avg_pnl/hold_sec), Exit Reason 분포, Policy Profile 표시 추가 |

---

## 2. 검증 쿼리 출력 전문

### positions:
```
('KRW-BTC', 'FLAT', 1000000.0, 0.0,
 initial_krw=1000000.0, equity_high=1000000.0,
 day_start_date=2026-02-18, day_start_equity=1000000.0,
 halted=False, halt_reason=None, halted_at=None,
 updated_at=2026-02-18 07:32:11+00:00)
```

### decisions (last 20):
```
(2026-02-18 07:32:11+00, 'STAY_FLAT', 'COST_GT_RT', '["COST_GT_RT","PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]', 1000000.0, 0.0, 1000000.0, 0.0, 'strict')
(2026-02-18 07:32:05+00, 'STAY_FLAT', 'COST_GT_RT', '["COST_GT_RT","PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]', 1000000.0, 0.0, 1000000.0, 0.0, 'strict')
(2026-02-18 07:32:00+00, 'STAY_FLAT', 'COST_GT_RT', '["COST_GT_RT","PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]', 1000000.0, 0.0, 1000000.0, 0.0, 'strict')
(2026-02-18 07:31:55+00, 'STAY_FLAT', 'PNONE_HIGH', '["PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]', 1000000.0, 0.0, 1000000.0, 0.0, 'strict')
(2026-02-18 07:31:50+00, 'STAY_FLAT', 'PNONE_HIGH', '["PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]', 1000000.0, 0.0, 1000000.0, 0.0, 'strict')
(2026-02-18 07:31:45+00, 'STAY_FLAT', 'PNONE_HIGH', '["PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]', 1000000.0, 0.0, 1000000.0, 0.0, 'strict')
(2026-02-18 07:31:40+00, 'STAY_FLAT', 'PNONE_HIGH', '["PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]', 1000000.0, 0.0, 1000000.0, 0.0, 'strict')
(2026-02-18 07:31:35+00, 'STAY_FLAT', 'PNONE_HIGH', '["PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]', 1000000.0, 0.0, 1000000.0, 0.0, 'strict')
(2026-02-18 07:31:30+00, 'STAY_FLAT', 'PNONE_HIGH', '["PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]', 1000000.0, 0.0, 1000000.0, 0.0, 'strict')
(2026-02-18 07:31:25+00, 'STAY_FLAT', 'PNONE_HIGH', '["PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]', 1000000.0, 0.0, 1000000.0, 0.0, 'strict')
(2026-02-18 07:31:20+00, 'STAY_FLAT', 'PNONE_HIGH', '["PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]', 1000000.0, 0.0, 1000000.0, 0.0, 'strict')
(2026-02-18 07:31:15+00, 'STAY_FLAT', 'PNONE_HIGH', '["PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]', 1000000.0, 0.0, 1000000.0, 0.0, 'strict')
(2026-02-18 07:31:10+00, 'STAY_FLAT', 'PNONE_HIGH', '["PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]', 1000000.0, 0.0, 1000000.0, 0.0, 'strict')
(2026-02-18 07:31:05+00, 'STAY_FLAT', 'PNONE_HIGH', '["PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]', 1000000.0, 0.0, 1000000.0, 0.0, 'strict')
(2026-02-18 07:31:00+00, 'STAY_FLAT', 'PNONE_HIGH', '["PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]', 1000000.0, 0.0, 1000000.0, 0.0, 'strict')
(2026-02-18 07:30:55+00, 'STAY_FLAT', 'PNONE_HIGH', '["PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]', 1000000.0, 0.0, 1000000.0, 0.0, 'strict')
(2026-02-18 07:30:50+00, 'STAY_FLAT', 'COST_GT_RT', '["COST_GT_RT","PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]', 1000000.0, 0.0, 1000000.0, 0.0, 'strict')
(2026-02-18 07:30:45+00, 'STAY_FLAT', 'COST_GT_RT', '["COST_GT_RT","PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]', 1000000.0, 0.0, 1000000.0, 0.0, 'strict')
(2026-02-18 07:30:40+00, 'STAY_FLAT', 'COST_GT_RT', '["COST_GT_RT","PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]', 1000000.0, 0.0, 1000000.0, 0.0, 'strict')
(2026-02-18 07:30:35+00, 'STAY_FLAT', 'COST_GT_RT', '["COST_GT_RT","PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]', 1000000.0, 0.0, 1000000.0, 0.0, 'strict')
```

### trades (last 20):
```
(없음 — 거래 0건)
```

---

## 3. 대시보드 확인

headless 환경 — Streamlit 1.54.0 설치 확인, `dashboard.py` syntax OK.

대시보드 `[E] Paper Trading` 섹션 구성 확인:
- **Equity metrics**: equity_high / initial_krw / halted / halt_reason / policy_profile 메트릭
- **Equity Curve (Last 6h)**: `paper_decisions.equity_est` 라인 차트
- **Drawdown (%) Last 6h**: `paper_decisions.drawdown_pct * 100` 라인 차트
- **Trade Stats (EXIT_LONG, last 200)**: trades / win_rate / avg_pnl_krw / avg_hold_sec / total_fees
- **Exit Reason Distribution**: TP / SL / TIME / EV_BAD 분포 테이블
- **Paper Decisions (last 60)**: equity_est / drawdown_pct / policy_profile 컬럼 포함
- **Reason Flags Distribution**: flag별 누적 카운트 + bar chart

---

## 4. 현재 상태 및 reason_flags 분석

### Policy Profile
- **현재: `strict`** (PAPER_POLICY_PROFILE=strict)

### 거래 건수: **0건** (strict 모드 10분 운영)

### reason_flags Top 3 (최근 500건 기준):

| Flag | Count | 의미 |
|---|---|---|
| **PNONE_HIGH** | 282 | `p_none > 0.70` — 모델이 "방향없음" 확률을 높게 평가; WARMUP 후에도 p_none≈0.99 유지 |
| **PDIR_WEAK** | 282 | `p_up < p_down + 0.05` — 상승 방향 신호가 하락 대비 마진 미달 |
| **EV_RATE_LOW** | 282 | `ev_rate < 0.0` — EV rate가 임계값(0.0) 미달; 현재 ev_rate ≈ -1.4e-5 |
| COST_GT_RT | 151 | `r_t ≤ 1.10 * cost_roundtrip` — barrier r_t가 비용 대비 부족 (WARMUP 초기 집중) |

**해석**: PNONE_HIGH + PDIR_WEAK + EV_RATE_LOW가 동시 발생하는 것은 현재 시장이
"방향성 없는 횡보 구간(p_none≈0.99)"임을 뜻한다. z_barrier ≈ 5.0으로 배리어가
현재 변동성 대비 너무 높아 모델이 진입 신호를 거의 생성하지 않는다.
→ `test` 프로파일 전환 시 PNONE_MAX=0.99, PDIR_MARGIN=-1.0으로 이 조건들이 해제되어
진입이 발생할 수 있다.

---

## 5. DoD 체크리스트

- [x] `paper_decisions`에 equity_est / drawdown_pct / policy_profile 기록됨 (142건 NULL 없음)
- [x] `paper_positions`에 equity_high / day_start_* / halted 필드 존재 및 갱신됨
- [x] HALT 로직 코드 경로 존재 (MAX_DRAWDOWN, DAILY_LOSS_LIMIT 판정 + DB 저장)
- [x] Dashboard에서 Equity curve + DD + Trade stats + halted 상태 표시 구현
- [x] strict/test 프로파일 분기 완성 (test 모드에서 임계값 완화 + RATE_LIMIT/COOLDOWN)
- [x] 마이그레이션 멱등 적용 완료 (ALTER TABLE IF NOT EXISTS)

---

## 6. 실행 로그 요약

```
07:21:40 Paper: pos=FLAT action=STAY_FLAT reason=COST_GT_RT cash=1000000 qty=0 equity=1000000 dd=0.0000% halted=False profile=strict
...
07:31:55 Paper: pos=FLAT action=STAY_FLAT reason=PNONE_HIGH cash=1000000 qty=0 equity=1000000 dd=0.0000% halted=False profile=strict
07:31:53 EvalMetrics(exec_v1): N=500 acc=0.854 hit=0.146 none=0.854 brier=0.3029 logloss=0.8184
```

- 총 449건 paper_decisions 누적
- 최신 142건: equity_est/drawdown_pct/policy_profile 모두 NULL 없이 정상 기록
- halted=False, halt_reason=None — 드로우다운 없음 (equity 변동 없음)
- Streamlit health: `dashboard.py` syntax OK, Streamlit 1.54.0 정상 설치
