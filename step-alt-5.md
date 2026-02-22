# Step ALT-5: 24시간 운영 + 데이터셋 품질 확보 + 학습/백테스트 준비

> **작업일시**: 2026-02-22  
> **환경**: Codespaces / DevContainer  
> **브랜치**: `copilot/review-alt-3-changes`  
> **상태**: 📋 실행 계획 수립 완료 — 운영 환경에서 실행 필요

---

## 1. 요약

### 목적
1. **24시간 연속 데이터 수집** (Upbit WS + Binance Futures + Coinglass)
2. **점검 스크립트 PASS** (FAIL이면 즉시 원인 분해/수정)
3. **학습 가능한 데이터셋(Parquet) 생성** + 라벨 분포 확인
4. 결과를 본 문서에 기록

### PASS 기준 (DoD)
- [ ] 봇 24h 연속 구동 (또는 최소 장시간 구동 + 중단 원인 분석)
- [ ] `bash scripts/run_pipeline_checks.sh --window 600` → `EXIT_CODE=0`
- [ ] `./data/datasets/btc_24h.parquet` 생성 성공 (유효 크기, 0바이트 아님)
- [ ] 본 문서(`step-alt-5.md`) 결과 기록 완료

---

## 2. 사전 준비: 환경변수 필수 설정

`.env`에 아래 값이 **실제 값으로 설정되어야 한다**. 미설정 시 Step ALT-5는 FAIL.

```bash
cd /workspaces/crypto_quant_trader

python scripts/activate_env_keys.py

# 필수 키 확인 (값은 출력하지 말고 존재 여부만)
grep -nE '^(COINGLASS_ENABLED|COINGLASS_API_KEY)=' .env
```

**필수 확인 항목**:
- `COINGLASS_ENABLED=true` — 반드시 `true`
- `COINGLASS_API_KEY=<REAL_KEY>` — placeholder(`your_key_here`) 금지

---

## 3. 실행 절차

### 3-1. 봇 실행 (터미널 1) — 24시간 유지 목표

```bash
cd /workspaces/crypto_quant_trader
poetry run python -m app.bot
```

**운영 중 확인 포인트**:
- Binance WS 수집이 꾸준히 들어오는지
- Coinglass 수집이 성공 기록을 남기는지 (`coinglass_call_status` 테이블)
- 예외가 나도 전체 프로세스가 죽지 않는지

### 3-2. 대시보드 실행 (터미널 2)

```bash
cd /workspaces/crypto_quant_trader
poetry run streamlit run app/dashboard.py \
  --server.address 0.0.0.0 \
  --server.port 8501 \
  --server.headless true
```

헬스 체크:
```bash
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:8501/healthz
```
- 기대: `HTTP 200`
- 401이면: Codespaces "Ports" 탭에서 8501 포트 Visibility를 Public으로 변경

### 3-3. 10분 후: 원클릭 점검 (첫 번째 게이트)

```bash
cd /workspaces/crypto_quant_trader
bash scripts/run_pipeline_checks.sh --window 600
echo "EXIT_CODE=$?"
```

**PASS 기준**: `EXIT_CODE=0`

**FAIL이면 즉시 원인 분해**:
```bash
poetry run python -m app.diagnostics.altdata_check --window 600
poetry run python -m app.diagnostics.feature_check --window 600
poetry run python -m app.diagnostics.feature_leak_check --window 600
poetry run python -m app.diagnostics.coinglass_check --window 600
```

FAIL 해석 가이드:
| 모듈 | FAIL 원인 | 조치 |
|------|----------|------|
| `altdata_check` | Binance WS/REST 수집 lag/fill_rate | 네트워크/재연결/DB insert 확인 |
| `feature_check` | predictions/market_1s null-rate 또는 범위 | predictor 로직/DB 확인 |
| `feature_leak_check` | 시간 정렬/조인에서 미래 데이터 참조 | 즉시 수정 대상 |
| `coinglass_check` | 키/활성화/수집기록 부재 | `.env` 설정 + `coinglass_call_status` 확인 |

### 3-4. 1시간 후: Export 1차 생성

```bash
cd /workspaces/crypto_quant_trader
mkdir -p ./data/datasets

poetry run python scripts/export_dataset.py \
  --output ./data/datasets/btc_1h.parquet \
  --horizon 120

ls -lh ./data/datasets/btc_1h.parquet
```

**PASS 기준**:
- parquet 파일이 0바이트가 아닌 유효한 파일
- `label_ts >= t0 + horizon_sec` 위반 drop이 0건에 가까울 것

### 3-5. 24시간 후: Export 24h 생성 (핵심 산출물)

```bash
cd /workspaces/crypto_quant_trader

bash scripts/run_pipeline_checks.sh --window 600
echo "EXIT_CODE=$?"

poetry run python scripts/export_dataset.py \
  --output ./data/datasets/btc_24h.parquet \
  --horizon 120

ls -lh ./data/datasets/btc_24h.parquet
```

**라벨 분포가 NONE만 나오는 경우**:
- 코드 실패가 아니라 데이터/임계치(r_t)/변동성 구간 문제
- r_t 정책(고정 vs 동적) 비교 실험으로 다음 Step에서 해결

---

## 4. 점검 결과

> ⏳ 아래는 운영 환경에서 실행 후 기록한다.

### 원클릭 점검 (`run_pipeline_checks.sh --window 600`)

| 시점 | EXIT_CODE | 결과 |
|------|-----------|------|
| 10분 후 | ⏳ | ⏳ |
| 1시간 후 | ⏳ | ⏳ |
| 24시간 후 | ⏳ | ⏳ |

### 개별 점검

| 모듈 | 10분 후 | 24시간 후 | 비고 |
|------|---------|----------|------|
| altdata_check | ⏳ | ⏳ | |
| feature_check | ⏳ | ⏳ | |
| feature_leak_check | ⏳ | ⏳ | |
| coinglass_check | ⏳ | ⏳ | |

---

## 5. Export 결과

> ⏳ 아래는 운영 환경에서 Export 실행 후 기록한다.

### 1시간 Export (`btc_1h.parquet`)

| 항목 | 결과 |
|------|------|
| 파일 크기 | ⏳ |
| 총 row 수 | ⏳ |
| label_ts 위반 drop 수 | ⏳ |
| 라벨 분포 (UP/DOWN/NONE) | ⏳ |

### 24시간 Export (`btc_24h.parquet`)

| 항목 | 결과 |
|------|------|
| 파일 크기 | ⏳ |
| 총 row 수 | ⏳ |
| label_ts 위반 drop 수 | ⏳ |
| 라벨 분포 (UP/DOWN/NONE) | ⏳ |
| 해당 기간 변동성 (대략) | ⏳ |

---

## 6. 운영 관측

> ⏳ 아래는 24h 구동 후 기록한다.

### 연속 구동
- 시작 시각: ⏳
- 종료/중단 시각: ⏳
- 연속 구동 시간: ⏳
- 중단 원인 (있을 경우): ⏳

### 주요 에러/경고 로그 (상위 5건)

```
⏳ 운영 환경에서 실행 후 기록
```

---

## 7. 리스크/주의사항

- **환경변수 필수**: `COINGLASS_ENABLED=true` + 실제 키 미설정 → 점검 FAIL (ALT-3 정책)
- **데이터 편향**: 봇 중단 시 특정 시간대만 존재 → Export 데이터셋 편향
- **NONE 라벨**: 라벨 전부 NONE이면 r_t가 너무 크거나 변동성 낮은 구간 → 다음 Step에서 r_t 튜닝
- **Codespaces 유휴**: Codespaces가 30분 유휴 시 자동 중단될 수 있음 → 터미널 활동 유지 필요

---

## 8. 다음 액션

### Step ALT-5 완료 후 (데이터 확보 전제)
1. **r_t 튜닝 실험**: 24h 데이터 기반 라벨 분포 집계 → r_t 고정값 vs 동적값(cost+변동성) 비교
2. **피처 표준화**: `predictions` + altdata + coinglass → 학습용 뷰/테이블로 표준화
3. **백테스트/검증 루프**: 수수료/슬리피지 포함 PnL, buy&hold 대비, drawdown/hit rate/turnover
