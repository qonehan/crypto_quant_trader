# STEP 6.1 결과 보고서 — 비용 기반 r_t 하한(r_min_eff) + Multi-flag Decision Reasons + Dashboard

## 1. 추가/수정 파일 목록

| 파일 | 변경 내용 |
|---|---|
| `app/config.py` | `R_MIN_COST_MULT=1.10`, `COST_SPREAD_LOOKBACK_SEC=60` 추가 |
| `app/db/migrate.py` | barrier_state 3개 컬럼(spread_bps_med, cost_roundtrip_est, r_min_eff) + paper_decisions 1개 컬럼(reason_flags) 마이그레이션 |
| `app/db/models.py` | BarrierState에 spread_bps_med/cost_roundtrip_est/r_min_eff, PaperDecision에 reason_flags 추가 |
| `app/barrier/controller.py` | `compute_spread_median()`, `compute_cost_roundtrip()` 신규, `compute_r_t()`에 r_min_eff 적용, 로그 포맷에 r_min_eff/cost 포함 |
| `app/db/writer.py` | barrier_state UPSERT SQL에 3개 컬럼 추가, paper_decisions INSERT에 reason_flags 추가 |
| `app/trading/policy.py` | `decide_action()` 리턴 변경: `(action, reason)` → `(action, primary_reason, reason_flags, diag)`. 모든 실패 조건을 수집 후 priority order로 primary 선택 |
| `app/trading/runner.py` | 새 4-tuple policy 시그니처 처리, reason_flags JSON 직렬화하여 DB 저장 |
| `app/dashboard.py` | [A] r_min_eff/cost_roundtrip 메트릭 + r_t vs r_min_eff vs cost 시계열 차트, [E] reason_flags 분포 (flag-level count) 추가 |

## 2. 핵심 변경 로직

### r_min_eff (비용 기반 r_t 하한)
```
spread_bps_med = median(spread_bps) over COST_SPREAD_LOOKBACK_SEC
cost_roundtrip_est = EV_COST_MULT * (2*FEE_RATE + 2*SLIPPAGE_BPS/1e4 + spread_bps_med/1e4)
r_min_eff = max(R_MIN, R_MIN_COST_MULT * cost_roundtrip_est)
r_t = clamp(k_vol_eff * sigma_h, r_min_eff, R_MAX)
```

### Multi-flag Decision Reasons
기존: early-return으로 첫 번째 실패 조건만 반환 → 항상 EV_RATE_LOW만 표시
변경: 모든 실패 조건을 수집 후 priority order로 primary 선택
- Priority: DATA_LAG > SPREAD_WIDE > NO_PRED > COST_GT_RT > PNONE_HIGH > PDIR_WEAK > EV_RATE_LOW

## 3. Bot 로그 (~40초)

```
05:11:50 Barrier: r_t=0.001540 r_min_eff=0.001540 cost=0.001400 status=WARMUP n=0 k_eff=0.7550
05:11:55 Barrier: r_t=0.001595 r_min_eff=0.001595 cost=0.001450 status=WARMUP n=0 k_eff=0.7550
05:11:55 Paper: pos=FLAT action=STAY_FLAT reason=PNONE_HIGH
05:12:00 Barrier: r_t=0.001584 r_min_eff=0.001584 cost=0.001440 status=WARMUP n=1 k_eff=0.7550
05:12:00 Paper: pos=FLAT action=STAY_FLAT reason=PNONE_HIGH
05:12:10 Paper: pos=FLAT action=STAY_FLAT reason=COST_GT_RT
05:12:15 Paper: pos=FLAT action=STAY_FLAT reason=PNONE_HIGH
05:12:25 Paper: pos=FLAT action=STAY_FLAT reason=PNONE_HIGH
```

- r_t가 기존 0.001에서 **0.001540~0.001595**로 상승 (r_min_eff 적용됨)
- reason이 PNONE_HIGH, COST_GT_RT 등 **다양하게 표시** (기존: 100% EV_RATE_LOW)

## 4. DB 확인 쿼리

### barrier_state (last 5) — 새 컬럼 확인
```
ts                  r_t       r_min_eff  cost_roundtrip  spread_bps_med  status  n    k_eff
05:24:20 UTC        0.001606  0.001606   0.001460        0.598           OK      120  0.500
05:24:15 UTC        0.001694  0.001694   0.001540        1.396           OK      119  0.500
05:24:10 UTC        0.001694  0.001694   0.001540        1.396           OK      119  0.500
```
- r_t ≥ r_min_eff ≥ R_MIN_COST_MULT * cost_roundtrip — 제약 정상 작동

### paper_decisions (last 10, 요약)
```
ts                  action      reason       reason_flags                                 ev_rate     p_none  spread_bps  r_t
05:24:21 UTC        STAY_FLAT   PNONE_HIGH   ["PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]     -7.2e-06    0.975   0.299       0.001606
05:24:16 UTC        STAY_FLAT   PNONE_HIGH   ["PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]     -7.2e-06    0.983   0.299       0.001694
05:23:51 UTC        STAY_FLAT   COST_GT_RT   ["COST_GT_RT","PNONE_HIGH","PDIR_WEAK","EV_RATE_LOW"]   -6.3e-06  0.989  2.094  0.001759
```

### Primary Reason 분포 (전체 269건)
```
EV_RATE_LOW: 119건 (44%) — 기존 Step 6.0 시점의 행 (reason_flags=NULL)
COST_GT_RT:   90건 (33%)
PNONE_HIGH:   60건 (22%)
```

### reason_flags 샘플 (Step 6.1 이후)
```json
["PNONE_HIGH", "PDIR_WEAK", "EV_RATE_LOW"]          — 대부분
["COST_GT_RT", "PNONE_HIGH", "PDIR_WEAK", "EV_RATE_LOW"]  — spread 넓을 때
```

## 5. Step 6.0 대비 개선 분석

| 항목 | Step 6.0 | Step 6.1 |
|---|---|---|
| r_t | 0.001 (고정 R_MIN) | 0.0015~0.0017 (cost-based floor) |
| r_t vs cost 관계 | r_t < cost (구조적 음수 EV) | r_t ≥ 1.1 * cost (음수 EV 완화) |
| primary reason | 100% EV_RATE_LOW | PNONE_HIGH 22%, COST_GT_RT 33%, EV_RATE_LOW 44% |
| reason_flags | N/A (단일 reason만) | JSON 배열로 모든 실패 조건 기록 |
| Dashboard | reason bar chart만 | r_t/r_min_eff/cost 시계열 + flag-level 분포 추가 |

## 6. DoD 체크리스트

- [x] barrier_state에 spread_bps_med, cost_roundtrip_est, r_min_eff 저장됨
- [x] r_t ≥ r_min_eff = max(R_MIN, R_MIN_COST_MULT * cost_roundtrip_est) 제약 적용
- [x] paper_decisions에 reason_flags (JSON array) 저장됨
- [x] policy가 모든 실패 조건을 수집 후 priority order로 primary 선택
- [x] Dashboard: r_t vs r_min_eff vs cost 시계열 차트 + flag-level 분포 표시
- [x] bot 크래시 없이 12분+ 지속 실행 (269 decisions)
- [x] primary reason 분포가 다양해짐 (PNONE_HIGH, COST_GT_RT, EV_RATE_LOW)
