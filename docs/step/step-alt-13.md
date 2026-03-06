# Step ALT-13 — Export 무결성 하드닝 + 24~48h 데이터 확장 + 경제적 게이트 재검증(베이스라인 비교) 결과보고서

작성일: 2026-02-24
브랜치: copilot/review-alt-3-changes
환경: GitHub Codespaces / PostgreSQL / KRW-BTC

---

## 1) Export 무결성 하드닝 결과

### 구현된 가드 (scripts/export_dataset.py)
- `--max-label-lag-mult` (float, default=2.0) 추가
  - `max_label_lag_sec = int(horizon_sec × max_label_lag_mult)`
- `--fee-bps-roundtrip` (int, default=10) 추가
  - taker=10bps, maker=4bps 전환 가능
- **price 쿼리 상한 제한**: `t_max + max_label_lag_sec`로 범위 제한 → DB에 미래 데이터가 있어도 로드 안 됨
- **label_lag_sec 컬럼** 저장 (검증 편의)
- 자동 drop 2종:
  - `dropped_early` (lag < horizon_sec): 너무 이른 매칭
  - `dropped_late` (lag > max_label_lag_sec): 갭/재기동 오염 방지
- **하드 FAIL (exit 1)**: 위반 row가 남으면 데이터셋 무효 선언

### Sanity 검증 결과 (h1800, max-label-lag-mult=2.0)
```
rows=1081  min_lag=1800.0s  max_lag=1800.0s
viol_low=0    (lag < 1800s)
viol_high=0   (lag > 3600s)
```
**PASS** ✅ — 모든 위반 zero 확인

### Drop 통계 (h1800, 전체 1800 features)
| 항목 | 수 | 원인 |
|------|-----|------|
| dropped_no_label | 360 | 미래 가격 없음 (데이터 종료 지점) |
| dropped_early | 0 | 없음 |
| dropped_late | 359 | **갭 오염 방지** (43.2h 봇 중단 구간) |
| **최종 rows** | **1081** | |

---

## 2) 데이터 확보 현황

### 봇 운영 상태
- 봇 정상 동작 확인 (09:31:15 UTC 기준 최신 예측 정상 출력)
- Paper trading: pos=FLAT, COST_GT_RT 이유로 계속 STAY_FLAT

### DB 현황 (최근 48시간)
| 테이블 | rows(48h) | max_gap_sec |
|--------|-----------|-------------|
| market_1s | 9,001 | **155,550 (43.2시간!)** |
| predictions | 1,799 | **155,555 (43.2시간!)** |

### 데이터 구조 분석
- feature ts 범위: `2026-02-22 11:48:50` ~ `2026-02-24 09:31:15` (45.7시간)
- **실제 연속 구간은 2개 단편**: 43.2시간 갭으로 인해 데이터가 불연속
  - 구간 A: 2026-02-22 11:48~14:48 (약 3시간)
  - 구간 B: 2026-02-24 07:xx~09:31 (약 2시간)
- **24h 연속 데이터 미달**: 현재 기술적으로 "45.7시간 스팬"이지만 실질 연속 데이터는 ~5시간분

### 결론
- lag 가드가 43.2시간 갭 오염(359행)을 정확히 차단함 ✅
- **진정한 24~48h 연속 데이터 확보는 아직 미달** → 봇을 더 안정적으로 운영해 연속 데이터 축적 필요

---

## 3) 경제성(비용 커버 가능성) 표

### 원본 출력
```
                       file    n  absret_q50  absret_q80  absret_q90  cost_q50  cost_q80  P(|ret|>cost)  P(|ret|>2cost)  P(ret>cost)  P(ret<-cost)  max_label_lag_sec
btc_24h_h1800_maker.parquet 1081    0.001357    0.002474    0.003608   0.00051  0.000810       0.869565        0.576318     0.082331      0.787234             1800.0
btc_24h_h1800_taker.parquet 1081    0.001357    0.002474    0.003608   0.00111  0.001410       0.550416        0.234043     0.034228      0.516189             1800.0
btc_24h_h3600_maker.parquet  361    0.002782    0.003389    0.003722   0.00074  0.000926       1.000000        0.925208     0.000000      1.000000             3600.0
btc_24h_h3600_taker.parquet  361    0.002782    0.003389    0.003722   0.00134  0.001526       0.939058        0.576177     0.000000      0.939058             3600.0
 btc_24h_h900_maker.parquet 1441    0.000805    0.001843    0.002506   0.00049  0.000780       0.651631        0.376128     0.158917      0.492713              900.0
 btc_24h_h900_taker.parquet 1441    0.000805    0.001843    0.002506   0.00109  0.001380       0.383761        0.119362     0.026371      0.357391              900.0
```

### 해석
| Horizon | Fee | P(|ret|>cost) | 판정 | 비고 |
|---------|-----|--------------|------|------|
| h900 | taker 10bps | 38.4% | ❌ 어려움 | 절대 수익 자체가 작음 |
| h900 | maker 4bps | 65.2% | △ 가능성 | 방향성 문제 남음 |
| h1800 | taker 10bps | 55.0% | △ 보통 | 마진 빠듯 |
| h1800 | maker 4bps | 86.9% | ✅ 양호 | |
| h3600 | taker 10bps | 93.9% | ✅ 높음 | 단 P(ret>cost)=0% (전부 하락) |
| h3600 | maker 4bps | **100.0%** | ✅ 최고 | 단 방향성 편향 심각 |

**경고**: h3600에서 `P(ret>cost)=0.0%`, `P(ret<-cost)=100%`
→ 전 기간이 하락장. "비용 커버"는 가능하지만 **항상 SHORT만 유리** → 모델이 아닌 시장 방향성

**결론**:
- 주력 horizon = **h1800** (rows 충분, P(|ret|>cost) 균형)
- 비용 시나리오: **maker 4bps**가 구조적으로 유리 (86.9% vs 55.0%)
- 단, 현재 데이터가 하락장 편향 → 방향 판단이 무의미

---

## 4) econ_gate 모델 결과(모델별/fee별)

### h1800 taker (ridge, hgbr)
```
  ridge: gamma=1.5  trades=0  total_net=0.000000  RMSE=0.00429  IC=0.232  sign_acc=0.012
  hgbr:  gamma=1.5  trades=0  total_net=0.000000  RMSE=0.00423  IC=0.106  sign_acc=0.172
```
→ 모두 게이트 통과 실패 (0 trades = FLAT과 동일)

### h1800 maker (ridge, hgbr)
```
  ridge: gamma=2.5  trades=0   total_net= 0.000000  (FLAT)
  hgbr:  gamma=1.0  trades=1   total_net=-0.004018  (LONG 1건 → LOSS)
```

### h1800 maker --drop-pnone
```
  ridge: gamma=2.5  trades=0   total_net= 0.000000  (FLAT)
  hgbr:  gamma=1.0  trades=1   total_net=-0.004018  (변화 없음)
```
→ `p_none` 제거 효과 없음 (hgbr 결과 동일)

### h900 maker (ridge, hgbr)
```
  ridge: gamma=2.0  trades=2  total_net=-0.004255  (LONG 2건 → LOSS)
  hgbr:  gamma=1.0  trades=2  total_net=-0.006371  (LONG 2건 → LOSS)
```
→ 두 모델 모두 상승 예측 → 하락장에서 전부 손실

### h3600 maker (ridge, hgbr)
```
  ridge: gamma=3.0  trades=1  total_net=-0.004252  (LONG 1건 → LOSS)
  hgbr:  gamma=2.5  trades=1  total_net=+0.003263  (SHORT 1건 → WIN)  sign_acc=1.0
```
→ hgbr h3600 maker만 유일하게 양수 (단 n=1 trade, 통계 의미 없음)

---

## 5) 베이스라인 비교(필수)

### h1800 maker (test 15% = 163행, nonoverlap_n=1)
```
  avg_label_ret = -0.003203  std=0.000000  (단일 방향, 하락)
  FLAT:          total_net =  0.000000
  ALWAYS_SHORT:  total_net = +0.002771  (gross=+0.003203, cost=-0.000432)
  ALWAYS_LONG:   total_net = -0.003635
  모델(ridge):   total_net =  0.000000  (0 trades)
  모델(hgbr):    total_net = -0.004018  (LONG 1건 → 손실)
```

### h900 maker (test 15% = 217행, nonoverlap_n=2)
```
  avg_label_ret = -0.002154  (하락 편향)
  FLAT:          total_net =  0.000000
  ALWAYS_SHORT:  total_net = +0.003412
  모델(ridge):   total_net = -0.004255  (LONG 2건 → 손실)
  모델(hgbr):    total_net = -0.006371  (LONG 2건 → 손실)
```

### h3600 maker (test 15% = 55행, nonoverlap_n=1)
```
  avg_label_ret = -0.003422  (전량 하락)
  FLAT:          total_net =  0.000000
  ALWAYS_SHORT:  total_net = +0.002592
  모델(ridge):   total_net = -0.004252  (LONG 1건 → 손실)
  모델(hgbr):    total_net = +0.003263  (SHORT 1건 → win, but n=1)
```

### 판정: **NO — 모델이 베이스라인 대비 유의미한 개선 없음**

근거:
1. **ALWAYS_SHORT이 모든 horizon/fee 조합에서 모델보다 우수**
2. 모델(ridge)은 대부분 FLAT(0 trades), 최소한 손실을 피하지만 기회도 없음
3. 모델(hgbr)은 상승(LONG) 예측을 하지만 하락장에서 전부 손실
4. hgbr h3600 maker의 SHORT 1건 승리는 n=1 → 통계적 의미 없음
5. **핵심 문제: 현재 데이터가 하락장 편향** → 모델 방향성 검증 자체가 불가능
6. test set의 std=0 (h1800): 테스트 구간 전체가 동일 수익률 → 과적합/데이터 불충분

---

## 6) 운영 점검 상태

파이프라인 체크 (scripts/run_pipeline_checks.sh --window 600):

| 체크 | 상태 | 비고 |
|------|------|------|
| altdata_check | PASS ✅ | |
| feature_check | PASS ✅ | |
| feature_leak_check | PASS ✅ | |
| coinglass_check | **FAIL ❌** | COINGLASS_ENABLED=false (키 미설정) |

**PIPELINE OVERALL: FAIL ❌** (Known FAIL — 트랙 B 운영 중)

---

## 7) 결론 및 다음 Step에서 할 일 3가지

### 종합 판정
ALT-13의 핵심 목표인 **라벨 무결성 하드닝은 완료**:
- lag 가드 기본 동작, dropped_late=359 (갭 오염 차단)
- label_lag_sec 컬럼 추가, 하드 FAIL 검증 구현

그러나 **경제성/모델 재검증 결론은 부정적**:
- 현 데이터(~5시간 연속 × 2구간 = 단편적 하락장)로는 모델 우위 입증 불가
- 모든 horizon에서 ALWAYS_SHORT > 모델 (시장 방향성이 결과를 설명)
- maker 4bps가 taker 10bps보다 구조적으로 유리하나, 데이터 편향이 더 큰 문제

### 다음 Step(ALT-14)에서 할 일 3가지

1. **연속 데이터 축적 우선**: 봇을 48h 이상 중단 없이 운영 → 진정한 24h+ 연속 데이터 확보.
   현재 43.2h 갭이 있는 상태에서는 어떤 모델도 의미 있는 평가 불가.
   Codespaces 절전/슬립 방지 설정 또는 외부 배포(Railway, Render 등) 고려.

2. **데이터 다양성 확보 후 재검증**: 상승/하락/횡보가 섞인 구간에서만
   `ALWAYS_SHORT` vs `ALWAYS_LONG` vs `모델` 비교가 의미가 있음.
   현재는 단방향 하락으로 baseline과의 비교 자체가 의미 없음.

3. **maker 전략 구현 검토**: P(|ret|>cost)가 maker 4bps에서 크게 개선(55%→87%).
   taker 기반의 현재 COST_GT_RT 로직을 maker 예상 비용으로 재조정하거나,
   Binance limit order 전략으로 전환하는 것이 비용 구조 개선의 핵심.
