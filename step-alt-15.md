# Step ALT-15 — Always-on 수집 환경 확정 + 연속성 PASS + 표준 평가(자동) 결과보고서

작성일: 2026-02-24
브랜치: copilot/review-alt-3-changes
환경: GitHub Codespaces (idle 중단 문제 있음) / PostgreSQL / KRW-BTC

---

## 1) 실행 환경 확정

- **선택**: 옵션 3 (Codespaces 임시 — idle 완화)
- **근거**: 현재 로컬/VM 배포 이전 전에 Codespaces에서 가능한 범위까지 진행
- **운영 방식**:
  - `nohup bash scripts/run_bot_forever.sh` (지수 백오프 재기동)
  - keepalive 루프: `nohup bash -c 'while true; do date >> logs/keepalive.log; sleep 60; done' &`
  - `devcontainer.json`에 `postStartCommand` 추가 → Codespace 재기동 시 봇 자동 시작

### 신규 생성 스크립트
| 파일 | 역할 |
|------|------|
| `scripts/run_bot_forever.sh` | 봇 크래시 시 지수 백오프(3→60s) 자동 재시작 |
| `scripts/snapshot_health.sh` | continuity + core checks 스냅샷 기록 (`logs/health_snapshots.log`) |
| `scripts/run_full_eval_alt15.sh` | 연속성 HARD gate → export → econ/baseline 원클릭 |

### `devcontainer.json` postStartCommand 추가
```json
"postStartCommand": "bash -lc 'mkdir -p logs && nohup bash scripts/run_bot_forever.sh >> logs/bot_supervisor.log 2>&1 &'"
```
→ Codespace가 재기동(resume)될 때 봇이 자동으로 시작됨. **갭 자체를 막지는 못하지만 재시작 시간을 단축**.

---

## 2) Continuity Check (48h)

```
poetry run python -m app.diagnostics.continuity_check --hours 48 --pred-gap-sec 60 --mkt-gap-sec 10
```

| 테이블 | n (48h) | max_gap_sec | avg_gap_sec | 기준 | 판정 |
|--------|---------|-------------|-------------|------|------|
| predictions | 1,799 | **155,555s (43.2h)** | 91.46s | 60s | **FAIL ❌** |
| market_1s | 9,001 | **155,550s (43.2h)** | 18.28s | 10s | **FAIL ❌** |

**OVERALL: FAIL ❌**

### 연속 구간 분석
```
총 segments = 2
seg# 1: rows=958  dur=1.33h  [2026-02-22 11:48:50 ~ 2026-02-22 13:08:35] ← largest
seg# 2: rows=842  dur=1.17h  [2026-02-24 08:21:10 ~ 2026-02-24 09:31:15]
```
- **12h 연속 달성: NO** (최대 1.33h)
- **24h 연속 달성: NO**

**Codespaces idle 정책이 구조적 장벽. postStartCommand로 재기동 시 지연을 줄이지만, 갭 자체는 막을 수 없음.**

---

## 3) Core Pipeline Checks

```
bash scripts/run_pipeline_checks.sh --window 600
```

| 체크 | 상태 | 비고 |
|------|------|------|
| altdata_check | PASS ✅ | |
| feature_check | PASS ✅ | |
| feature_leak_check | PASS ✅ | |
| coinglass_check | **FAIL ❌** | COINGLASS_ENABLED=false (Known Fail) |

**PIPELINE OVERALL: FAIL ❌** (coinglass Known Fail)

---

## 4) Full Eval 실행 로그 (`run_full_eval_alt15.sh`)

> continuity HARD GATE가 FAIL하므로 스크립트 전체 자동 실행 불가 → 단계별 수동 실행.

### Export 결과 (segment 필터 ON, max-feature-gap-sec=60)

| 파일 | 원본 rows | segment rows | dropped_by_gap | 최종 rows |
|------|-----------|--------------|----------------|-----------|
| btc_alt15_h900_maker | 958 | 958 | 842 | 779 |
| btc_alt15_h900_taker | 958 | 958 | 842 | 779 |
| btc_alt15_h1800_maker | 958 | 958 | 842 | 599 |
| btc_alt15_h1800_taker | 958 | 958 | 842 | 599 |

- 선택된 segment: `2026-02-22 11:48:50 ~ 13:08:35` (1.33h, 958 predictions)
- label_lag_sec: min=horizon, max=horizon (오염 zero ✅)

---

## 5) Econ Summary & Baseline (핵심)

### Econ Summary
```
                         file   n  absret_q50  absret_q80  cost_q50  P(|ret|>cost)  P(|ret|>2cost)  P(ret>cost)  P(ret<-cost)  mean_ret  std_ret
 btc_alt15_h900_maker.parquet 779    0.000676    0.001486   0.00048         0.6765          0.3055        0.172        0.5045 -0.000624 0.000907
 btc_alt15_h900_taker.parquet 779    0.000676    0.001486   0.00108         0.3273          0.0167        0.000        0.3273 -0.000624 0.000907
btc_alt15_h1800_maker.parquet 599    0.001234    0.001797   0.00055         0.9249          0.5259        0.000        0.9249 -0.001320 0.000499
btc_alt15_h1800_taker.parquet 599    0.001234    0.001797   0.00115         0.4825          0.0584        0.000        0.4825 -0.001320 0.000499
```

### Baseline (test 15%, non-overlap)
```
  btc_alt15_h900_maker  H=900s  test_rows=117  nonoverlap_n=1  ⚠️
    avg_label_ret=+0.000466  std=0.000000
    FLAT         net= 0.000000
    ALWAYS_SHORT net=-0.000886
    ALWAYS_LONG  net=+0.000046

  btc_alt15_h1800_maker H=1800s  test_rows=90  nonoverlap_n=1  ⚠️
    avg_label_ret=-0.000831  std=0.000000
    FLAT         net= 0.000000
    ALWAYS_SHORT net=+0.000421
    ALWAYS_LONG  net=-0.001241
```

### 해석
- **모든 케이스에서 nonoverlap_n=1** → 통계적 평가 불가
- h900 test 구간: 시장 약간 상승 → ALWAYS_SHORT 손실, ALWAYS_LONG 소폭 수익
- h1800 test 구간: 시장 하락 → ALWAYS_SHORT 소폭 수익
- **시장 방향이 매번 달라지므로** "연속 데이터 없이는 어떤 전략도 신뢰성 있게 평가 불가"

---

## 6) 결론

### "통계적으로 평가 가능 상태"인가? **NO**
- nonoverlap_n=1은 동전 던지기와 구분 불가
- test std=0.000 → 단일 시나리오만 테스트됨
- **코드 인프라(export/게이트/스크립트)는 완비됐으나, 데이터가 없음**

### "다음 단계(ALT-16)로 넘어갈 준비가 됐는가?" **조건부 YES**
- 코드 레벨: 준비 완료 (continuity_check, export segment, run_full_eval_alt15.sh)
- 데이터 레벨: **Codespaces 환경 한계를 인정하고 ALT-16에서 환경 이전 결정 필요**

### Codespaces 한계 공식 선언
| 문제 | 근본 원인 | 해결책 |
|------|-----------|--------|
| max_gap=43.2h | Codespace idle → suspend | **항상 켜진 환경 이전** |
| nonoverlap_n=1 | 연속 데이터 1.33h뿐 | 12h+ 연속 확보 후 재실행 |
| 모델 평가 무의미 | 위의 결과 | 연속 데이터 확보 후 재실행 |

---

## 7) ALT-16에서 할 일 3가지

1. **항상 켜진 환경으로 봇 이전 (ALT-16 최우선)**
   - Railway.app / Render.com / Fly.io 중 택1
   - `scripts/run_bot_forever.sh`를 그대로 사용, DB는 Codespaces에서 Railway PostgreSQL로 이전
   - 목표: `continuity_check --pred-gap-sec 60` PASS (max_gap ≤ 60s, 24h 유지)
   - 달성하면 `run_full_eval_alt15.sh`를 그대로 실행해서 ALT-15가 원래 목표했던 평가 완성

2. **24h 연속 달성 즉시: 표준 평가 자동화 실행**
   - `bash scripts/run_full_eval_alt15.sh 600 24`
   - h900 maker 기준 `nonoverlap_n ≥ 10` 달성 여부 확인
   - ALWAYS_SHORT / ALWAYS_LONG / FLAT 대비 모델 우위 최초 판정

3. **Coinglass Track A 전환 (ALT-16 병행)**
   - 환경이 안정화된 이후 Coinglass API 키 발급 + `.env` 설정
   - `coinglass_check` PASS → PIPELINE OVERALL 4/4 PASS
   - 이로써 운영 게이트 완성 + ALT-17에서 paper trading → live trading 전환 준비
