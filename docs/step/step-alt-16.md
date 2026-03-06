# Step ALT-16 — 항상-켜진 환경 이전 준비 + 봇 재기동 + 평가 재실행 결과보고서

작성일: 2026-02-24
브랜치: copilot/review-alt-3-changes
환경: GitHub Codespaces (idle 중단 문제 있음) / PostgreSQL / KRW-BTC

---

## 1) ALT-16 목표 및 실행 요약

| 항목 | 목표 | 달성 여부 |
|------|------|-----------|
| 항상-켜진 환경 이전 스크립트 | `setup_always_on.sh` 작성 | ✅ 완료 |
| `--skip-continuity` 옵션 | `run_full_eval_alt15.sh` 수정 | ✅ 완료 |
| 봇 재기동 | `run_bot_forever.sh` 재실행 | ✅ 완료 |
| continuity PASS | max_gap ≤ 60s (24h 유지) | ❌ FAIL (43.2h gap 지속) |
| 표준 평가 재실행 | h900/h1800 × maker/taker | ✅ 실행 완료 (데이터 동일) |
| step-alt-16.md | 보고서 작성 | ✅ 완료 |

---

## 2) 신규 작업 내용

### 2-1) `scripts/setup_always_on.sh` (신규 생성)

로컬 PC / VM에서 항상-켜진 수집 환경을 세팅하는 5단계 가이드 스크립트:

1. `poetry install --with dev`
2. PostgreSQL Docker 컨테이너 생성/시작 (`--restart unless-stopped`, `quant_pgdata` 볼륨)
3. DB 연결 확인 (`app.config.load_settings()`)
4. `nohup bash scripts/run_bot_forever.sh` 백그라운드 supervisor 시작
5. 상태 요약 출력 (PID, 로그 경로, 다음 명령 안내)

```bash
# 사용법 (로컬/VM):
git clone <repo> ~/crypto_quant_trader && cd ~/crypto_quant_trader
cp .env.example .env   # .env 편집 후
bash scripts/setup_always_on.sh
```

### 2-2) `run_full_eval_alt15.sh` 에 `--skip-continuity` 옵션 추가

```bash
# continuity FAIL 시에도 강제 실행 (임시 평가용):
bash scripts/run_full_eval_alt15.sh 600 48 --skip-continuity
```

continuity FAIL이어도 export/econ/baseline 단계까지 진행 가능.

### 2-3) 봇 재기동

```
bot 상태: dead (Codespaces idle suspend 이후)
재기동: nohup bash scripts/run_bot_forever.sh >> logs/bot_supervisor.log 2>&1 &
새 segment 시작: seg#3 시작 (2026-02-24 10:59:55~, 93 rows)
```

---

## 3) Continuity Check (48h, 재실행)

```
poetry run python -m app.diagnostics.continuity_check --hours 48 --pred-gap-sec 60 --mkt-gap-sec 10
```

| 테이블 | n (48h) | max_gap_sec | avg_gap_sec | 기준 | 판정 |
|--------|---------|-------------|-------------|------|------|
| predictions | 1,892 | **155,555s (43.2h)** | 90.02s | 60s | **FAIL ❌** |
| market_1s | 9,467 | **155,550s (43.2h)** | 17.99s | 10s | **FAIL ❌** |

**OVERALL: FAIL ❌**

### 연속 구간 분석 (ALT-16 시점)

```
총 segments = 3
seg# 1: rows= 958  dur=1.33h  [2026-02-22 11:48:50 ~ 2026-02-22 13:08:35] ← largest
seg# 2: rows= 842  dur=1.17h  [2026-02-24 08:21:10 ~ 2026-02-24 09:31:15]
seg# 3: rows=  93  dur=0.13h  [2026-02-24 10:59:55 ~ 2026-02-24 11:07:35] ← 봇 재기동 후
```

- seg#3은 ALT-16에서 봇을 재기동한 이후의 새 segment (growing)
- 최대 연속 구간은 여전히 1.33h — **12h 연속 달성: NO**

---

## 4) Export 결과 (ALT-16 datasets)

```bash
# 사용된 명령:
for H in 900 1800; do
  for FEE_LABEL in maker taker; do
    [[ "$FEE_LABEL" == "maker" ]] && FEE=4 || FEE=10
    poetry run python scripts/export_dataset.py \
      --output data/datasets/btc_alt16_h${H}_${FEE_LABEL}.parquet \
      --horizon ${H} --max-label-lag-mult 2.0 \
      --fee-bps-roundtrip ${FEE} --max-feature-gap-sec 60
  done
done
```

| 파일 | 원본 rows | segment 선택 | dropped_by_gap | 최종 rows |
|------|-----------|-------------|----------------|-----------|
| btc_alt16_h900_maker | 1,892 | seg#1 (958) | 842 | 779 |
| btc_alt16_h900_taker | 1,892 | seg#1 (958) | 842 | 779 |
| btc_alt16_h1800_maker | 1,892 | seg#1 (958) | 842 | 599 |
| btc_alt16_h1800_taker | 1,892 | seg#1 (958) | 842 | 599 |

- 선택된 segment: `2026-02-22 11:48:50 ~ 13:08:35` (1.33h, 958 predictions)
- label_lag_sec: min=horizon, max=horizon (오염 zero ✅)
- **seg#2(842), seg#3(93)는 label window 내 가격 데이터 부족으로 dropped_late 처리**

---

## 5) Econ Summary (ALT-16 datasets)

```
======================================================================
ECON SUMMARY (ALT-16 datasets)
======================================================================
                         file   n  absret_q50  absret_q80  cost_q50  P(|ret|>cost)  P(|ret|>2cost)  P(ret>cost)  P(ret<-cost)  mean_ret   std_ret  max_lag_sec
 btc_alt16_h900_maker.parquet 779    0.000676    0.001486   0.00048         0.6765          0.3055        0.172        0.5045 -0.000624  0.000907        900.0
 btc_alt16_h900_taker.parquet 779    0.000676    0.001486   0.00108         0.3273          0.0167        0.000        0.3273 -0.000624  0.000907        900.0
btc_alt16_h1800_maker.parquet 599    0.001234    0.001797   0.00055         0.9249          0.5259        0.000        0.9249 -0.001320  0.000499       1800.0
btc_alt16_h1800_taker.parquet 599    0.001234    0.001797   0.00115         0.4825          0.0584        0.000        0.4825 -0.001320  0.000499       1800.0
```

### 해석
- **h1800_maker**: P(|ret|>cost)=92.5% — 절대 수익률이 비용(0.55bps)을 압도적으로 초과 → 이론적 채굴 기회 있음
- **모든 케이스에서 P(ret>cost)=0%**: 이 1.33h 구간은 **지속적 하락 추세**였음
- h1800_maker에서 mean_ret=-0.00132 (하락), std_ret=0.000499 (변동 작음) → 단방향 강하락 구간

---

## 6) Baseline 비교 (test 15%, non-overlap)

```
  btc_alt16_h900_maker  H=900s   test_rows=117  nonoverlap_n=1  ⚠️
    avg_label_ret=+0.000466  std=0.000000
    FLAT         net= 0.000000
    ALWAYS_SHORT net=-0.000886
    ALWAYS_LONG  net=+0.000046

  btc_alt16_h900_taker  H=900s   test_rows=117  nonoverlap_n=1  ⚠️
    FLAT         net= 0.000000
    ALWAYS_SHORT net=-0.001486
    ALWAYS_LONG  net=-0.000554

  btc_alt16_h1800_maker H=1800s  test_rows=90   nonoverlap_n=1  ⚠️
    avg_label_ret=-0.000831  std=0.000000
    FLAT         net= 0.000000
    ALWAYS_SHORT net=+0.000421
    ALWAYS_LONG  net=-0.001241

  btc_alt16_h1800_taker H=1800s  test_rows=90   nonoverlap_n=1  ⚠️
    FLAT         net= 0.000000
    ALWAYS_SHORT net=-0.000179
    ALWAYS_LONG  net=-0.001841
```

**모든 케이스 nonoverlap_n=1** → 단일 거래만 시뮬레이션됨, 통계적 평가 불가.

---

## 7) 모델 평가 (train_and_trade_econ_gate.py)

### h900_maker (Ridge + HGBR)

| 모델 | gamma | trades | total_net | IC | sign_acc |
|------|-------|--------|-----------|-----|----------|
| ridge | 1.5 | **0** | 0.000000 | 0.681 | 0.744 |
| hgbr | 2.0 | **0** | 0.000000 | 0.472 | 0.897 |

- **trades=0**: 예측 신뢰도가 threshold(gamma)를 넘지 못해 거래 없음
- test RMSE: ridge=0.000376 (naive=0.000434 대비 13% 감소)
- sign_acc=0.897 (hgbr) → 방향 예측 자체는 양호하나, 비용 초과 확신도 부족

### h1800_maker (Ridge + HGBR)

| 모델 | gamma | trades | total_net | IC | sign_acc |
|------|-------|--------|-----------|-----|----------|
| ridge | 1.0 | **1** | **+0.000421** | 0.714 | 1.000 |
| hgbr | 2.5 | **1** | **+0.000396** | 0.132 | 1.000 |

- **trades=1**: nonoverlap_n=1과 동일 (단일 거래)
- sign_acc=1.000 (완벽한 방향 예측) — 이 구간이 단방향 하락이라 SHORT만 수익
- ALWAYS_SHORT net=+0.000421 == ridge net=+0.000421 → **모델이 ALWAYS_SHORT와 동일한 거래**

### 결론
- h900: 모델이 거래를 포기 (conservative gamma), FLAT과 동일
- h1800: 모델=ALWAYS_SHORT (단일 거래, 단방향 시장에서 우연 일치)
- **통계적 의미: 없음** (nonoverlap_n=1, std=0)

---

## 8) 종합 결론

### "ALT-16 목표 달성 여부"

| 목표 | 상태 |
|------|------|
| 항상-켜진 환경 이전 코드 준비 | ✅ `setup_always_on.sh` 완비 |
| continuity PASS (max_gap ≤ 60s) | ❌ 43.2h gap 지속 |
| 12h+ 연속 데이터 확보 | ❌ 최대 1.33h |
| nonoverlap_n ≥ 10 | ❌ 모든 케이스 n=1 |
| 모델 vs 베이스라인 신뢰성 있는 비교 | ❌ 데이터 부족 |

### 근본 원인 (변경 없음)

**GitHub Codespaces idle 정책이 구조적 장벽이다.**

- postStartCommand, keepalive, run_bot_forever.sh 모두 구현 완료
- 단, Codespaces가 suspend되면 어떤 프로세스도 살아남지 못함
- 재기동 후 지연(수 분) + 수집 재개 → 새 segment 생성 반복
- 이 구조적 문제는 **항상 켜진 환경으로 이전하기 전까지 해결 불가**

### Codespaces 한계 공식 선언 (ALT-16 업데이트)

| 문제 | 근본 원인 | 해결책 |
|------|-----------|--------|
| max_gap=43.2h | Codespace idle → suspend | **항상 켜진 환경 이전** |
| segments=3, max=1.33h | 재기동마다 새 segment | 로컬/VM/Railway 등 이전 |
| nonoverlap_n=1 | 1.33h 연속뿐 | 12h+ 연속 확보 후 재실행 |
| 모델 평가 무의미 | 위 결과 | 연속 데이터 확보 후 재실행 |

---

## 9) ALT-17에서 할 일

### 최우선: 항상-켜진 환경 확보

```bash
# 로컬/VM에서 실행:
bash scripts/setup_always_on.sh
# → 봇 가동 후 24h 기다린 후:
poetry run python -m app.diagnostics.continuity_check --hours 24 --pred-gap-sec 60 --mkt-gap-sec 10
# → PASS 확인 후:
bash scripts/run_full_eval_alt15.sh 600 24
```

### 목표 기준

| 기준 | 목표값 |
|------|--------|
| continuity max_gap | ≤ 60s (24h 유지) |
| 최대 연속 segment | ≥ 12h |
| nonoverlap_n (h900_maker, test 15%) | ≥ 10 |
| 모델 비교 | ridge/hgbr vs FLAT/SHORT/LONG 유의미 비교 |

### 병행 작업

1. **Coinglass API 키 발급** → `.env` 설정 → `coinglass_check` PASS
   - 4/4 pipeline checks PASS → PIPELINE OVERALL PASS
2. **환경 이전 옵션** (우선순위 순):
   - 옵션 A: 로컬 PC/VM + `setup_always_on.sh`
   - 옵션 B: Railway.app Free tier (PostgreSQL + 봇 동시 배포)
   - 옵션 C: Fly.io (작은 VM, 항상 켜짐, $0-5/월)

### 코드 레벨 준비 상태

```
✅ export_dataset.py         — 라벨 무결성, segment 필터, fee 파라미터화
✅ continuity_check.py       — HARD gate 진단
✅ run_full_eval_alt15.sh    — 원클릭 평가 파이프라인 (--skip-continuity 지원)
✅ run_bot_forever.sh        — 지수 백오프 supervisor
✅ snapshot_health.sh        — 주기적 건강 스냅샷
✅ setup_always_on.sh        — 로컬/VM 이전 가이드
✅ devcontainer.json         — postStartCommand (Codespaces 재기동 시 자동 시작)
⬜ Coinglass 연동            — API 키 발급 후 즉시 활성화 가능
⬜ 24h 연속 데이터           — 환경 이전 후 자동으로 해결
```

**코드 인프라는 ALT-16 시점에서 완비됨. 남은 것은 환경(데이터)뿐.**
