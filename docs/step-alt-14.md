# Step ALT-14 — 연속성 게이트 + 구간 분리 Export + 재평가(베이스라인 포함) 결과보고서

작성일: 2026-02-24
브랜치: copilot/review-alt-3-changes
환경: GitHub Codespaces / PostgreSQL / KRW-BTC

---

## 1) Continuity Check 결과 (48h)

### 신규 스크립트: `app/diagnostics/continuity_check.py`
```
poetry run python -m app.diagnostics.continuity_check --hours 48 --pred-gap-sec 60 --mkt-gap-sec 10
EXIT_CODE=1
```

| 테이블 | n (48h) | max_gap_sec | avg_gap_sec | 기준 | 판정 |
|--------|---------|-------------|-------------|------|------|
| predictions | 1,799 | **155,555s (43.2h)** | 91.46s | 60s | **FAIL ❌** |
| market_1s | 9,001 | **155,550s (43.2h)** | 18.28s | 10s | **FAIL ❌** |

**OVERALL: FAIL ❌** (예상된 결과 — Codespaces 43.2h 중단 갭 존재)

### 연속 구간 분석 결과
```
총 segments = 2
seg# 1: rows= 958  dur=1.33h  [2026-02-22 11:48:50 ~ 2026-02-22 13:08:35] ← largest
seg# 2: rows= 842  dur=1.17h  [2026-02-24 08:21:10 ~ 2026-02-24 09:31:15]
```
- 최대 연속 구간: **1.33시간** (958 predictions)
- 두 번째 구간: **1.17시간** (842 predictions)
- **진정한 24h 연속 데이터 미달** — Codespaces idle 정책으로 반복 중단

---

## 2) Export "연속 구간" 기능 추가 내용

### 구현: `scripts/export_dataset.py` + `select_largest_segment()` 함수
- `--max-feature-gap-sec` 파라미터 추가 (default 0 = 비활성)
- feature ts 정렬 후 gap > threshold 지점에서 segment_id 증가
- **가장 큰 segment만 자동 선택** (rows 기준)
- 출력 로그에 segments/selected_range/dropped_by_gap 포함

### btc_seg_h1800_maker export 로그 요약
```
segments=2
selected_segment_id=0  (rows=958, dur=1.33h)
selected_range=[2026-02-22 11:48:50 ~ 2026-02-22 13:08:35]
dropped_by_gap=842  (2구간의 모든 rows)
dropped_no_label=359  (horizon 너머 가격 없음)
dropped_late=0, dropped_early=0
최종 rows=599
```

---

## 3) 경제성 요약 (세그먼트 기반)

| 파일 | rows | P(\|ret\|>cost) | P(\|ret\|>2cost) | P(ret>cost) | P(ret<-cost) | 비고 |
|------|------|-----------------|------------------|-------------|--------------|------|
| btc_seg_h1800_maker | 599 | **92.5%** | 52.6% | **0.0%** | **92.5%** | 전기간 하락 |
| btc_seg_h900_maker | 779 | 67.7% | 30.6% | 17.2% | 50.5% | 다소 혼합 |

**해석:**
- h1800: `P(ret>cost)=0.0%`, `P(ret<-cost)=92.5%` → **전기간 하락장**, 비용 커버 가능성 높지만 방향 편향 심각
- h900: `P(ret>cost)=17.2%`, `P(ret<-cost)=50.5%` → 약간 더 다양하지만 여전히 하락 편향
- maker 4bps는 비용 커버 가능성이 높음 (**taker 10bps 대비 구조적 우위**)

---

## 4) 모델/게이트 결과

### h1800 maker (ridge, hgbr) — 세그먼트 기반
```
  features=11  train=419, valid=90, test=90

  ridge: gamma=1.0  trades=1(SHORT)  total_net=+0.000421  win_rate=1.00  IC=0.714  sign_acc=1.00
  hgbr:  gamma=2.5  trades=1(SHORT)  total_net=+0.000396  win_rate=1.00  IC=0.132  sign_acc=1.00
```

### h1800 maker --drop-pnone
```
  ridge: gamma=1.0  trades=1(SHORT)  total_net=+0.000421  (변화 없음)
  hgbr:  gamma=2.5  trades=1(SHORT)  total_net=+0.000396  (변화 없음)
```
→ `p_none` 제거 효과 없음 (완전 동일한 결과)

### h900 maker (ridge, hgbr)
```
  ridge: gamma=1.5  trades=0  total_net=0.000000  IC=0.681  sign_acc=0.744
  hgbr:  gamma=2.0  trades=0  total_net=0.000000  IC=0.472  sign_acc=0.897
```
→ 게이트 통과 없음 (모두 FLAT)

**주의**: h1800 test 90행은 모두 동일 label_return(std=0) → **통계적으로 유효하지 않음**

---

## 5) 베이스라인 비교(필수)

### h1800 maker (test 15% = 90행, nonoverlap=1)
```
  avg_label_ret = -0.000831  std=0.000000  (단일 방향, 1번의 horizon만)
  FLAT:          total_net =  0.000000
  ALWAYS_SHORT:  total_net = +0.000421  (gross=+0.000831, cost=-0.000410)
  ALWAYS_LONG:   total_net = -0.001241
  모델(ridge):   total_net = +0.000421  (SHORT 1건 — ALWAYS_SHORT과 동일)
  모델(hgbr):    total_net = +0.000396  (SHORT 1건 — 약간 낮음)
```

### h900 maker (test 15% = 117행, nonoverlap=1)
```
  avg_label_ret = +0.000466  std=0.000000  (약간 상승)
  FLAT:          total_net =  0.000000
  ALWAYS_SHORT:  total_net = -0.000886  (LOSS — 상승장에서 SHORT)
  ALWAYS_LONG:   total_net = +0.000046
  모델(ridge):   total_net =  0.000000  (0 trades — FLAT)
  모델(hgbr):    total_net =  0.000000  (0 trades — FLAT)
```

### 판정: **NO — 통계적 평가 자체가 불가능**

근거:
1. **test set의 nonoverlap_n=1** (h1800/h900 모두): 단 1회 거래 기회 → 동전 던지기와 구분 불가
2. h1800: 모델이 SHORT을 맞췄지만 **ALWAYS_SHORT과 결과 동일** → 모델 기여 없음
3. h900: 시장이 약간 상승 → 모델이 FLAT 선택(0 trades) → 우연히 손실 피했지만 기회도 없음
4. **std=0.000**: 테스트 구간 전체가 단일 수익률 구간 → 진정한 variance 없음
5. **근본 원인**: 최대 연속 구간 1.33h로는 h1800 기준 2.67번의 horizon만 존재
   → 70/15/15 split 시 test에 배정되는 non-overlap 기회는 이론상 0.4회 → 1회로 반올림

---

## 6) 파이프라인 점검 상태

| 체크 | 상태 | 비고 |
|------|------|------|
| altdata_check | PASS ✅ | |
| feature_check | PASS ✅ | |
| feature_leak_check | PASS ✅ | |
| continuity_check | **FAIL ❌** | max_gap=155,555s (43.2h 갭) |
| coinglass_check | **FAIL ❌** | Known Fail — COINGLASS_ENABLED=false |

**PIPELINE OVERALL: FAIL ❌** (continuity + coinglass 2개 Known Fail)

---

## 7) ALT-14 DoD 달성 여부

| 조건 | 달성 | 비고 |
|------|------|------|
| 연속성 품질 게이트 실행 가능 | ✅ | continuity_check.py 구현 완료 |
| Export가 연속 구간 기반 선택 가능 | ✅ | --max-feature-gap-sec 옵션 구현 |
| 24h(최소 12h) 연속 데이터 확보 | ❌ | **최대 1.33h** (Codespaces 중단) |
| 베이스라인 비교 가능한 수준 | ❌ | **nonoverlap=1** — 통계 의미 없음 |

**결론**: 코드 인프라(게이트/export)는 완성됐지만, **Codespaces idle 중단**이 데이터 확보를 막음

---

## 8) 결론 및 ALT-15에서 할 일 3가지

### 종합 판단
ALT-14의 목표인 "연속성 게이트 구축"과 "구간 분리 Export"는 코드 레벨에서 완료됐다.
그러나 **Codespaces의 idle 종료 정책**으로 인해 43.2시간 갭이 생겼고,
최대 연속 구간이 1.33h에 불과해 어떤 평가도 통계적 의미가 없다.
**데이터 수집 환경 자체를 변경하지 않으면 ALT-15에서도 동일한 문제가 반복**된다.

### ALT-15에서 할 일 3가지

1. **Codespaces 연속 운영 방법 확정 (ALT-15 최우선)**
   - 옵션 A: `devcontainer.json`에 `postStartCommand`로 봇 자동 재기동 +
     GitHub Actions scheduled workflow로 Codespace 주기적 wake-up
   - 옵션 B: Railway / Render / Fly.io 에 봇 컨테이너 배포 (always-on)
   - 옵션 C: 로컬 머신(항상 켜진 PC/Mac)에서 Docker로 운영
   - **최소 목표**: `continuity_check --pred-gap-sec 60` PASS (max_gap_sec ≤ 60s)

2. **24h 연속 데이터 달성 후 표준 평가 파이프라인 실행**
   - `continuity_check` PASS → 자동으로 segment export → econ_gate 학습 → 베이스라인 비교
   - 이 파이프라인을 **bash 스크립트 1개**로 자동화 (`scripts/run_full_eval.sh`)
   - 목표: non-overlap test trades ≥ 10 (h900 기준 최소 10h 연속 필요)

3. **Coinglass Track A 전환 준비 (병행)**
   - ALT-15에서 봇 운영 환경이 안정화되면 Coinglass API 키 신청/적용
   - `COINGLASS_ENABLED=true` + `coinglass_check` PASS → PIPELINE OVERALL 4/4 달성
   - 운영 안정성 확보 없이 API 키를 적용해도 연속 실패가 반복되므로 환경 안정화 먼저
