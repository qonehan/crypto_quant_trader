# Step ALT-12 — 24~48h 데이터 확장 + 비용/수익 가능성 진단 + 경제적 게이트(Edge) 기반 검증 결과보고서

작성일: 2026-02-24
브랜치: copilot/review-alt-3-changes
환경: GitHub Codespaces (Linux 6.8, Python 3.11, poetry)

---

## 0) 변경사항 요약

| 파일 | 변경 내용 |
|------|----------|
| `scripts/train_and_trade_econ_gate.py` | 신규 생성 — Ridge/HGBR + 경제적 게이트(pred_edge > gamma*cost) + valid gamma 선택 + non-overlap 트레이드 시뮬 |
| `scripts/export_dataset.py` | (ALT-11에서 반영) `cost_roundtrip_est` 컬럼 추가 확인 |

---

## 1) 데이터 확보 현황

### 봇 구동 상태
- 봇은 `nohup` 백그라운드로 기동 중 (`logs/bot.log`)
- 현재 DB 데이터: **2026-02-22 11:48 ~ 13:08 (약 80분)** — 24h 미달
- predictions: 958 rows, market_1s: 4792 rows

### Export 파일 (80분 데이터)

| 파일 | 전체 rows | 정제 후 rows | 기간 |
|------|-----------|-------------|------|
| btc_80m_h600_clean.parquet | 958 → 839 | 839 | 11:48~12:47 |
| btc_80m_h900_clean.parquet | 958 → 779 | 779 | 11:48~12:42 |
| btc_80m_h1800_clean.parquet | 958 → 599 | 599 | 11:48~12:29 |
| btc_80m_h3600_clean.parquet | 958 → 239 | 239 | 11:48~12:17 |

> ⚠️ **라벨 오염 이슈 발견 및 수정**: 전체 958개 중 119~719개가 t0+horizon 이후 수십 시간 뒤의 가격을 참조 (2일 뒤 봇 재기동으로 생긴 시장가 데이터가 merge_asof에 잡힘). `actual_lag ≤ 2×horizon` 필터로 정제.

### 런타임 점검 결과 (`--window 600`)

| 점검 | 결과 |
|------|------|
| altdata_check | **PASS ✅** |
| feature_check | **PASS ✅** |
| feature_leak_check | **PASS ✅** |
| coinglass_check | **FAIL ❌** (COINGLASS_ENABLED=false, 키 없음 — 정책상 정상) |

---

## 2) 비용 vs 수익 가능성(경제성) 요약 ⭐

### 2-1) 정제 전(오염 포함) — 참고만

| horizon | P(|ret|>cost) | P(|ret|>2cost) | 비고 |
|---------|--------------|---------------|------|
| h600 | 0.330 | 0.145 | |
| h900 | 0.453 | 0.200 | |
| h1800 | 0.676 | 0.411 | |
| h3600 | 0.983 | 0.875 | 수십 시간 뒤 가격 오염 — **무효** |

### 2-2) 정제 후(clean labels) — 유효 수치

| horizon | n(clean) | |ret|_q50 | |ret|_q80 | |ret|_q90 | cost_q50 | P(|ret|>cost) | P(|ret|>2cost) | P(ret>cost) | P(ret<-cost) |
|---------|---------|-----------|-----------|-----------|----------|--------------|---------------|------------|------------|
| h600 | 839 | 0.000445 | 0.001281 | 0.001576 | 0.001080 | **0.235** | 0.024 | 0.005 | 0.230 |
| h900 | 779 | 0.000676 | 0.001486 | 0.001744 | 0.001080 | **0.327** | 0.017 | 0.000 | 0.327 |
| h1800 | 599 | 0.001234 | 0.001797 | 0.002035 | 0.001150 | **0.482** | 0.058 | 0.000 | 0.482 |
| h3600 | 239 | 0.002642 | 0.003155 | 0.003261 | 0.001420 | **0.933** | 0.498 | 0.000 | 0.933 |

**시장 특성:** 이 80분 구간은 **일방향 하락장**. P(ret>cost)≈0 전 horizon — 롱 포지션은 구조적으로 불리. 숏 포지션(P(ret<-cost))은 h1800에서 48%, h3600에서 93%.

**결론:**
- **주력 horizon: h1800** — P(|ret|>cost)=48%, 데이터량 599행, 트레이드 실험 가능
- **후보 horizon: h3600** — coverage 좋지만 clean 239행 너무 적음, 통계 미흡
- h600/h900: P(|ret|>2cost)<2% → 비용 구조상 거의 불가

---

## 3) p_none 분포 (80분 데이터 기준)

| 파일 | p_none_q50 | p_none_q90 | p_none_min | p_none_max |
|------|-----------|-----------|-----------|-----------|
| h600_clean | 0.9900 | 0.9900 | **0.7531** | 1.0000 |
| h900_clean | 0.9900 | 0.9900 | **0.7531** | 1.0000 |
| h1800_clean | 0.9781 | 0.9900 | **0.7531** | 1.0000 |
| h3600_clean | 0.9900 | 0.9900 | **0.9262** | 1.0000 |

라이브 봇 로그에서도 확인: `p_none=0.9900` (거의 항상).

**결론: `p_none` 게이트는 ALT-12에서 폐기.** 업스트림 분류 모델이 구조적으로 p_none≈0.99 출력 → 유효 게이트 역할 불가. 대신 회귀 pred_edge 기반 게이트만 사용.

---

## 4) 경제적 게이트(Edge) 기반 모델 결과

### 4-1) 모델 예측 품질 (h1800_clean, test_n=90)

| 모델 | RMSE | RMSE_naive0 | 개선률 | IC | sign_acc |
|------|------|------------|--------|-----|---------|
| Ridge (alpha=50) | **0.000128** | 0.000988 | **+87%** | **+0.714** | **1.000** |
| HGBR | 0.000282 | 0.000988 | **+71%** | +0.132 | 1.000 |

> sign_acc=1.00은 이 구간이 단방향 하락이었기 때문에 (trivial). Ridge IC=0.714가 실질 예측력 지표.

### 4-2) h900_clean 모델 비교 (test_n=117)

| 모델 | RMSE | RMSE_naive0 | 개선률 | IC | sign_acc |
|------|------|------------|--------|-----|---------|
| Ridge (alpha=50) | 0.000376 | 0.000434 | **+13%** | **+0.681** | 0.744 |
| HGBR | **0.000289** | 0.000434 | **+33%** | +0.472 | 0.897 |

> h900에서 HGBR이 RMSE 기준 best. Ridge IC는 h1800 쪽이 더 높음.

### 4-3) 경제적 게이트(pred_edge) 미니백테스트 (h1800, Ridge, gamma=1.0, fixed)

> gamma는 valid에서 최적화 시 gamma=1.5로 선택됐으나, test의 max(|pred|/cost)=1.467로 threshold 미달 → test trades=0. gamma=1.0으로 고정 재실행:

| 설정 | trades | side | gross | net | cost | 방향 적중? |
|------|--------|------|-------|-----|------|-----------|
| Ridge gamma=1.0 | **1** | SHORT | +0.000831 | **-0.000199** | 0.001030 | ✅ |
| HGBR gamma=1.0 | **1** | SHORT | +0.000826 | **-0.000204** | 0.001030 | ✅ |
| Ridge gamma=0.9 | 1 | SHORT | +0.000831 | -0.000204 | — | ✅ |
| Ridge gamma=0.5 | 1 | SHORT | +0.000831 | -0.000179 | — | ✅ |

- **gamma grid**: [0.5, 0.8, 1.0, 1.5, 2.0]
- **valid에서 gamma=1.5 선택** (valid 기간 pnl 최대화 기준)
- **test에서 gamma=1.0 기준 1 trade** — 방향은 맞음(SHORT), 하지만 gross(0.083%) < cost(0.103%) → net 소폭 손실

**Non-overlap 제약**: 1800초 horizon × test 구간 445초 → 최대 1 트레이드 가능. 따라서 trades=1이 이론적 상한.

- artifacts: `./artifacts/ml2/btc_80m_h1800_clean_{ridge,hgbr}/`

---

## 5) 해석/결론

### (1) 비용 구조가 수익을 압도하는가?

**YES (현재 단기 horizon)** — h600/h900에서:
- P(|ret|>cost) = 24~33% (과반 미달)
- avg gross PnL/trade ≈ 0.03~0.05% vs avg cost ≈ 0.11% → cost가 2~3배

**h1800 이상부터 비용 커버 가능성**: P(|ret|>cost)=48%, P(|ret|>2cost)=5.8%
- 단, 1 trade gross=+0.083%, cost=0.103% → 아직 net 손실
- 더 강한 신호(|pred|/cost > 1.5)가 필요

### (2) 모델이 "비용 초과 기회"를 포착했는가?

**부분적으로 YES** (h1800):
- Ridge RMSE 87% 개선, IC=0.71 — 방향성 예측은 우수
- 그러나 예측 magnitude가 cost를 아직 충분히 초과하지 못함 (max |pred|/cost ≈ 1.47)
- 80분 단일 하락 구간이라 "항상 SHORT" 전략과 구별이 어려움 (trivial sign_acc=1.0)

### (3) 다음 단계에서 바꿀 것

1. **데이터**: 24~48h 이상 수집 필수 — 현재 80분/하락장 편향으로 모든 지표 신뢰도 낮음
2. **비용 구조**: 현재 taker fee ~10bps 왕복이 너무 큼 → maker 주문 전략(fee ~4bps) 검토
3. **Horizon**: h1800/h3600이 coverage 기준 우선. 단, 충분한 데이터가 있어야 통계적 의미

---

## 6) Coinglass 상태

- `coinglass_check`: **FAIL** (`COINGLASS_ENABLED=false`, API 키 없음)
- 정책상 FAIL이 정상 동작 — ML 검증은 Coinglass 없이 진행
- 조치 계획: Coinglass API 키 발급 후 `.env`에 `COINGLASS_ENABLED=true` + `COINGLASS_API_KEY=<key>` 설정 → 봇 재시작 → `coinglass_check` PASS

---

## 7) 다음 Step에서 할 일 3가지

1. **24~48h 이상 데이터 수집 후 전 과정 재실행**: 현재 80분 하락장 편향 데이터로는 모든 결과가 "항상 SHORT"과 구분 불가. 최소 여러 방향 전환이 포함된 데이터가 있어야 IC/sign_acc 신뢰도 확보 가능. 봇이 지속 구동 중이므로 24h 후 재Export → ALT-10~12 전체 재실행.

2. **maker 주문 전략으로 비용 구조 개선**: 현재 taker fee 왕복 ~10bps가 구조적 병목. maker fee(~4bps) 적용 시 cost ≈ 0.04~0.05%, 이는 h900 |ret|_q80=0.15%와 비교해 커버 가능 구간 대폭 확대. `fee_bps_roundtrip`을 4~6으로 낮춰 재시뮬레이션 필요.

3. **라이브 의사결정 통합 설계(feature-flag)**: h1800 Ridge 모델이 방향성 예측(IC=0.71)을 보이는 만큼, 라이브 봇에 "회귀 예측값 기반 경계(pred_edge > cost)" feature-flag 연결 설계를 준비. 단, 실제 진입 전 충분한 데이터로 out-of-sample 검증 선행 필수.

---

## 8) 아티팩트 목록

```
data/datasets/
├── btc_80m_h600.parquet       (958 rows, cost 포함)
├── btc_80m_h600_clean.parquet (839 rows, 정제)
├── btc_80m_h900.parquet       (958 rows)
├── btc_80m_h900_clean.parquet (779 rows, 정제)
├── btc_80m_h1800.parquet      (958 rows)
├── btc_80m_h1800_clean.parquet (599 rows, 정제) ← 주력
├── btc_80m_h3600.parquet      (958 rows)
└── btc_80m_h3600_clean.parquet (239 rows, 정제)

artifacts/ml2/
├── btc_80m_h900_clean_ridge/         metrics.json, test_trades.csv
├── btc_80m_h900_clean_hgbr/          metrics.json, test_trades.csv
├── btc_80m_h900_clean_ridge_nopnone/ metrics.json, test_trades.csv
├── btc_80m_h900_clean_hgbr_nopnone/  metrics.json, test_trades.csv
├── btc_80m_h1800_clean_ridge/        metrics.json, test_trades.csv  ← 주력
├── btc_80m_h1800_clean_hgbr/         metrics.json, test_trades.csv
├── btc_80m_h1800_clean_ridge_nopnone/ metrics.json, test_trades.csv
└── btc_80m_h1800_clean_hgbr_nopnone/ metrics.json, test_trades.csv

scripts/train_and_trade_econ_gate.py  (신규)
```
