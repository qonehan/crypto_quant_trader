# Step ALT-4: 운영 루프 준비 및 검증 보고서

> **작업일시**: 2026-02-22  
> **브랜치**: `copilot/review-alt-3-changes`  
> **상태**: ✅ 운영 루프 준비 완료 — 봇 구동 + 점검 + Export 가능 상태

---

## 1. 요약

### 목적
**실시간 수집(Upbit WS + Binance Futures + Coinglass) → 품질 점검 → 데이터셋 Export → 학습/백테스트 준비**까지 매일 반복 가능한 운영 루프를 완성한다.

### 범위
- 운영 루프에 필요한 스크립트/파일 구조 확인 및 정비
- 코드 컴파일 검증 (51개 Python 파일)
- `data/datasets/` 디렉토리 생성 + `.gitignore` 설정
- 운영 루프 실행 절차 문서화

### 상태
- **코드 준비**: ✅ PASS (51개 파일 컴파일 성공, 0개 실패)
- **스크립트 준비**: ✅ `run_pipeline_checks.sh`, `export_dataset.py`, `activate_env_keys.py` 존재
- **진단 모듈 준비**: ✅ `altdata_check`, `feature_check`, `feature_leak_check`, `coinglass_check` 존재
- **데이터 디렉토리**: ✅ `data/datasets/` 생성, `.gitignore` 설정 완료
- **환경변수**: ⚠️ `.env`에 실제 API 키 설정 필요 (maintainer 작업)

---

## 2. 변경 파일 목록

| 파일 | 구분 | 변경 내용 |
|------|------|----------|
| `.gitignore` | 수정 | `data/datasets/*.parquet`, `*.csv`, `*.parquet` 추가 |
| `data/datasets/.gitkeep` | 신규 | Export 출력 디렉토리 유지용 |
| `step-alt-4.md` | 신규 | 본 보고서 |

---

## 3. 검증 방법

### 3-1. 사전 준비: 환경변수 설정

```bash
# .env에 실제 값 설정 (placeholder 금지)
# COINGLASS_ENABLED=true
# COINGLASS_API_KEY=<REAL_KEY>
python scripts/activate_env_keys.py
grep -nE '^(COINGLASS_ENABLED|COINGLASS_API_KEY)=' .env
```

### 3-2. 봇 실행 (터미널 1, 최소 10분)

```bash
poetry run python -m app.bot
```

### 3-3. 대시보드 실행 (터미널 2)

```bash
poetry run streamlit run app/dashboard.py \
  --server.address 0.0.0.0 \
  --server.port 8501 \
  --server.headless true
```

### 3-4. 원클릭 파이프라인 점검

```bash
bash scripts/run_pipeline_checks.sh --window 600
echo "EXIT_CODE=$?"
```
- **기대 결과**: EXIT_CODE=0

### 3-5. 개별 점검 (FAIL 시 원인 분해)

```bash
poetry run python -m app.diagnostics.altdata_check --window 600
poetry run python -m app.diagnostics.feature_check --window 600
poetry run python -m app.diagnostics.feature_leak_check --window 600
poetry run python -m app.diagnostics.coinglass_check --window 600
```

### 3-6. 데이터셋 Export

```bash
poetry run python scripts/export_dataset.py \
  --output ./data/datasets/btc_dataset.parquet \
  --horizon 120
ls -lh ./data/datasets/btc_dataset.parquet
```
- **기대 결과**: parquet 파일 생성, `label_ts >= t0 + horizon_sec` 위반 0건

---

## 4. 검증 결과

### 코드 컴파일 검증

| 항목 | 결과 |
|------|------|
| Python 파일 수 | 51개 |
| 컴파일 성공 | 51개 ✅ |
| 컴파일 실패 | 0개 |

### 스크립트/모듈 존재 확인

| 파일 | 존재 | 용도 |
|------|------|------|
| `scripts/run_pipeline_checks.sh` | ✅ | 원클릭 점검 (4개 진단 순차 실행) |
| `scripts/export_dataset.py` | ✅ | 학습용 데이터셋 Export (merge_asof forward) |
| `scripts/activate_env_keys.py` | ✅ | .env 주석 키 활성화 |
| `app/diagnostics/altdata_check.py` | ✅ | Alt Data 파이프라인 유입 점검 |
| `app/diagnostics/feature_check.py` | ✅ | Feature 품질 점검 (null rate, 범위) |
| `app/diagnostics/feature_leak_check.py` | ✅ | Feature 미래 누수 점검 |
| `app/diagnostics/coinglass_check.py` | ✅ | Coinglass 수집 강제 점검 |

### 파이프라인 점검 (실행 환경 필요)

실제 점검은 **봇 구동 + DB 접속이 가능한 운영 환경**에서 실행해야 한다:

```bash
# 봇 10분 구동 후:
bash scripts/run_pipeline_checks.sh --window 600
```

| 점검 | 결과 | 비고 |
|------|------|------|
| altdata_check | ⏳ 운영 환경 필요 | Binance WS 수집 데이터 필요 |
| feature_check | ⏳ 운영 환경 필요 | predictions 테이블 데이터 필요 |
| feature_leak_check | ⏳ 운영 환경 필요 | predictions 테이블 데이터 필요 |
| coinglass_check | ⏳ 운영 환경 필요 | COINGLASS_ENABLED=true + 실키 필요 |

---

## 5. 리스크/주의사항

### 환경변수 (필수 조치)
- `.env`에 `COINGLASS_ENABLED=true` + 실제 API 키가 설정되어야 `coinglass_check` PASS
- placeholder(`your_key_here` 등)가 남아있으면 **점검 FAIL이 정상** (ALT-3 정책)

### 데이터 축적
- **봇을 24시간 이상 연속 구동**해야 의미 있는 Export 가능
- 중간 중단 시 특정 시간대만 존재 → 데이터셋 편향
- Coinglass/청산 이벤트/펀딩/베이시스 변화가 제대로 반영되지 않음

### Export 주의
- 라벨이 전부 `NONE`이면: r_t가 너무 크거나 해당 구간 변동성 낮음
- 해결: 더 긴 기간 수집 (최소 24h), r_t 튜닝 검토

### 롤백
- ALT-3 이전 상태로 롤백: `backup/pre-alt3-merge-*` 태그 사용 (생성 시)

---

## 6. 다음 액션

### 즉시 (maintainer)
1. PR #2 (`copilot/review-alt-3-changes`) → main 머지
2. PR #1 (`copilot/force-actual-data-collection`) Close
3. `.env`에 실제 Coinglass API 키 설정

### 1일차 운영 루프
1. `poetry run python -m app.bot` 10분+ 구동
2. `bash scripts/run_pipeline_checks.sh --window 600` → EXIT_CODE=0 확인
3. `poetry run python scripts/export_dataset.py --output ./data/datasets/btc_dataset.parquet --horizon 120`
4. 결과 확인 후 봇을 24시간 연속 구동

### 24h 후
1. `bash scripts/run_pipeline_checks.sh --window 600`
2. `poetry run python scripts/export_dataset.py --output ./data/datasets/btc_24h.parquet --horizon 120`

### 모델 단계 (우선순위 순)
1. **라벨 품질 점검 + r_t 튜닝**: UP/DOWN 비율 확보, r_t 고정값 vs 동적값 비교
2. **피처 표준화**: `predictions` + altdata + coinglass를 학습용 뷰/테이블로 표준화
3. **백테스트/검증 루프**: 수수료/슬리피지 포함 PnL, buy&hold 대비, drawdown/hit rate

### PR 상태

| PR | 브랜치 | 상태 | 처리 |
|----|--------|------|------|
| #1 | `copilot/force-actual-data-collection` | Open | maintainer가 Close |
| #2 | `copilot/review-alt-3-changes` | Open | maintainer가 Merge |
