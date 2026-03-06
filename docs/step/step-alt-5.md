# Step ALT-5 — 24h 운영 + 점검 PASS + Export 결과보고서

> **작성일**: 2026-02-22  
> **브랜치**: `copilot/review-alt-3-changes`  
> **환경**: GitHub Actions CI (PostgreSQL DB 미제공)  
> **상태**: ⏳ 코드 준비 완료 — **Codespaces/DevContainer에서 실행 필요**

---

## 1. 최종 판정

| 항목 | 결과 | 비고 |
|------|------|------|
| 코드 컴파일 | ✅ PASS (51/51 파일) | 전체 Python 파일 에러 없음 |
| 필수 스크립트/모듈 존재 | ✅ PASS (9/9 파일) | bot, dashboard, diagnostics 4종, export, activate, pipeline checks |
| 의존성 설치 | ✅ PASS | `poetry install` 성공 |
| 봇 실행 | ⏳ DB 필요 | `DB_URL` 호스트(`db`) 미해석 — Codespaces/DevContainer 필요 |
| 원클릭 점검 (10분 후) | ⏳ DB 필요 | diagnostics 모듈은 로드 성공, DB 쿼리에서 중단 |
| 원클릭 점검 (24h 후) | ⏳ DB 필요 | |
| Export (1h) | ⏳ DB 필요 | `export_dataset.py` 로드 성공, DB 쿼리에서 중단 |
| Export (24h) | ⏳ DB 필요 | |
| **결론** | **코드 PASS / 런타임 ⏳** | Codespaces에서 봇 구동 후 재실행 필요 |

---

## 2. 환경변수 (값 노출 금지)

| 변수 | 상태 | 비고 |
|------|------|------|
| `COINGLASS_ENABLED=true` | ✅ 설정됨 | `.env`에 활성 라인 존재 |
| `COINGLASS_API_KEY` set | ❌ placeholder | `your_key_here` (len=13) — 실제 키 교체 필요 |
| `DB_URL` | ✅ 설정됨 | `postgresql+psycopg://postgres:postgres@db:5432/quant` |

**필수 조치**: `.env`에서 `COINGLASS_API_KEY`를 실제 키로 교체해야 `coinglass_check` PASS 가능

---

## 3. 점검 결과 요약 (window=600)

### 코드 검증 (CI 환경에서 실행 가능)

| 항목 | 결과 |
|------|------|
| Python 컴파일 (51 파일) | ✅ 0 failures |
| `is_real_key` 참조 잔존 | ✅ 0건 |
| `feature_snapshots` 참조 잔존 | ✅ 0건 |
| `app.features` 참조 잔존 | ✅ 0건 |
| SQL injection 패턴 (변경 파일) | ✅ 0건 |

### 런타임 점검 (Codespaces 필요)

| 모듈 | 결과 | 비고 |
|------|------|------|
| `altdata_check` | ⏳ DB 필요 | Binance WS 수집 데이터 필요 |
| `feature_check` | ⏳ DB 필요 | predictions 테이블 데이터 필요 |
| `feature_leak_check` | ⏳ DB 필요 | predictions 테이블 데이터 필요 |
| `coinglass_check` | ⏳ DB + 실제 키 필요 | 설정은 정상 로드됨 (`COINGLASS_ENABLED=True`, `COINGLASS_KEY_SET=True`) |

**`coinglass_check` 실행 로그** (설정 로드 부분):
```
Coinglass 수집 강제 점검 (PASS/FAIL)
symbol       = BTC
window       = 600s
COINGLASS_ENABLED  = True
COINGLASS_KEY_SET  = True
```
→ 설정은 정상, DB 연결만 해결되면 실행 가능

---

## 4. Export 로그 요약

| 항목 | 결과 |
|------|------|
| `btc_1h.parquet` | ⏳ DB 필요 |
| `btc_24h.parquet` | ⏳ DB 필요 |
| label_ts 위반 drop | ⏳ |
| label 분포 (1h) | ⏳ |
| label 분포 (24h) | ⏳ |

---

## 5. 이슈/조치

### 이슈
1. **DB 미제공**: GitHub Actions CI 환경에는 PostgreSQL이 없음 → 봇/점검/Export 모두 DB 연결 단계에서 중단
2. **Coinglass API 키**: `.env.example`의 `your_key_here` placeholder가 그대로 활성화됨 → 실제 키 교체 필요

### 조치
1. **Codespaces/DevContainer에서 실행**: `docker-compose`가 DB를 자동 제공하므로, 해당 환경에서 봇 구동 + 점검 실행
2. `.env`에서 `COINGLASS_API_KEY=your_key_here` → 실제 키로 교체
3. 봇을 최소 10분 구동 후 `bash scripts/run_pipeline_checks.sh --window 600` 실행

### 재발 방지
- `.env.bak*` 파일이 git에 올라가지 않도록 `.gitignore`에 패턴 추가 완료

---

## 6. Codespaces에서 실행할 커맨드 (복사-실행용)

```bash
# 1) 환경변수 설정
cd /workspaces/crypto_quant_trader
python scripts/activate_env_keys.py
# .env에서 COINGLASS_API_KEY를 실제 키로 교체
grep -nE '^(COINGLASS_ENABLED|COINGLASS_API_KEY)=' .env

# 2) 봇 실행 (터미널 1, 최소 10분)
poetry run python -m app.bot

# 3) 대시보드 실행 (터미널 2)
poetry run streamlit run app/dashboard.py \
  --server.address 0.0.0.0 --server.port 8501 --server.headless true

# 4) 10분 후: 원클릭 점검
bash scripts/run_pipeline_checks.sh --window 600
echo "EXIT_CODE=$?"

# 5) Export
mkdir -p ./data/datasets
poetry run python scripts/export_dataset.py \
  --output ./data/datasets/btc_1h.parquet --horizon 120
ls -lh ./data/datasets/btc_1h.parquet

# 6) 24시간 후: Export 24h
poetry run python scripts/export_dataset.py \
  --output ./data/datasets/btc_24h.parquet --horizon 120
```

---

## 7. 다음 단계 제안

1. **Codespaces에서 봇 24h 구동** + 점검 PASS 확인 + Export 생성
2. **r_t/horizon 튜닝**: 24h 데이터 기반 라벨 분포 확인 → NONE만이면 r_t 조정
3. **베이스라인 모델 학습/백테스트**: 수수료/슬리피지 PnL, buy&hold 대비, drawdown/hit rate
