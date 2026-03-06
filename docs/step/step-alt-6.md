# Step ALT-6 — Codespaces 런타임 게이트 통과 + Coinglass 실수집 증거 + Export 1h 생성 결과보고서

> **작성일**: 2026-02-22  
> **브랜치**: `copilot/review-alt-3-changes`  
> **환경**: GitHub Actions CI (DB 미제공) → Codespaces/DevContainer에서 실행 필요  
> **상태**: ✅ 코드 준비 완료 — 런타임 게이트는 Codespaces에서 실행 필요

---

## 1. 최종 판정

| 항목 | 결과 | 비고 |
|------|------|------|
| 코드 컴파일 (51 파일) | ✅ PASS | 전체 Python 파일 에러 없음 |
| 필수 스크립트/모듈 (9개) | ✅ PASS | bot, dashboard, diagnostics 4종, export, activate, pipeline checks |
| DB_CONNECT_OK | ⏳ Codespaces 필요 | CI 환경에 `db` 호스트 없음 (`getent hosts db` → 실패) |
| bot 10분 구동 | ⏳ Codespaces 필요 | DB 연결 필요 |
| run_pipeline_checks(600s) | ⏳ Codespaces 필요 | DB 연결 필요 |
| coinglass_call_status ok=true | ⏳ Codespaces 필요 | DB + 실제 API 키 필요 |
| Export 1h (btc_1h.parquet) | ⏳ Codespaces 필요 | DB 연결 필요 |
| **결론** | **코드 PASS / 런타임 ⏳** | Codespaces에서 실행 시 PASS 예상 |

---

## 2. 코드 검증 결과 (CI에서 완료)

| 항목 | 결과 |
|------|------|
| Python 컴파일 검사 | ✅ 51/51 파일 PASS |
| `is_real_key` 참조 잔존 | ✅ 0건 |
| `feature_snapshots` 참조 잔존 | ✅ 0건 |
| SQL injection 패턴 (변경 파일) | ✅ 0건 (make_interval 사용) |
| `.gitignore` 설정 | ✅ `.env`, `.env.bak*`, `data/datasets/*`, `logs/` |

---

## 3. 환경 확인 결과

| 항목 | CI 환경 | Codespaces 예상 |
|------|---------|-----------------|
| `getent hosts db` | ❌ 해석 불가 | ✅ docker-compose가 `db` 제공 |
| `docker ps` | 빈 컨테이너 목록 | ✅ postgres 등 실행 중 |
| `.env` 존재 | ❌ (`.gitignore`됨) | ✅ `.env.example`에서 생성 |
| `COINGLASS_API_KEY` | placeholder | ⚠️ 실제 키 교체 필요 |

---

## 4. Codespaces에서 실행할 전체 절차 (복사-실행용)

### Step 1: DB 가용성 확인
```bash
cd /workspaces/crypto_quant_trader
getent hosts db || echo "DB host not resolved"
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

# DB 연결 스모크 테스트
python3 -c "
from app.config import load_settings
from sqlalchemy import create_engine, text
s = load_settings()
e = create_engine(s.DB_URL, pool_pre_ping=True)
with e.connect() as c:
    c.execute(text('SELECT 1'))
print('DB_CONNECT_OK')
"
```

### Step 2: 환경변수 설정
```bash
python scripts/activate_env_keys.py

# .env에서 COINGLASS_API_KEY를 실제 키로 교체 (에디터 사용)
# nano .env 또는 code .env

# 확인 (값 노출 금지, 존재/길이만)
grep -nE '^(COINGLASS_ENABLED|COINGLASS_API_KEY)=' .env

python3 -c "
from dotenv import dotenv_values
env = dotenv_values('.env')
k = (env.get('COINGLASS_API_KEY') or '').strip()
print('COINGLASS_ENABLED=', (env.get('COINGLASS_ENABLED') or '').strip())
print('COINGLASS_API_KEY_LEN=', len(k))
print('COINGLASS_API_KEY_PLACEHOLDER=', k.lower() in ('your_key_here',''))
"
```

### Step 3: 봇 실행 (tmux, 최소 10분)
```bash
mkdir -p logs
tmux new -s bot -d "poetry run python -m app.bot 2>&1 | tee -a logs/bot.log"
tmux ls

# 로그 확인
tail -n 80 -f logs/bot.log
```

### Step 4: 대시보드 실행 (선택)
```bash
tmux new -s dash -d "poetry run streamlit run app/dashboard.py --server.address 0.0.0.0 --server.port 8501 --server.headless true 2>&1 | tee -a logs/dashboard.log"
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:8501/healthz
```

### Step 5: 10분 후 — 원클릭 점검
```bash
bash scripts/run_pipeline_checks.sh --window 600
echo "EXIT_CODE=$?"
```

FAIL이면 개별 점검:
```bash
poetry run python -m app.diagnostics.altdata_check --window 600
poetry run python -m app.diagnostics.feature_check --window 600
poetry run python -m app.diagnostics.feature_leak_check --window 600
poetry run python -m app.diagnostics.coinglass_check --window 600
```

### Step 6: Coinglass 수집 증거 확인
```bash
python3 -c "
from app.config import load_settings
from sqlalchemy import create_engine, text
s = load_settings()
e = create_engine(s.DB_URL, pool_pre_ping=True)
q = '''
SELECT ts, ok, http_status, COALESCE(error_msg,'') AS error_msg
FROM coinglass_call_status
ORDER BY ts DESC LIMIT 20
'''
with e.connect() as c:
    rows = c.execute(text(q)).fetchall()
print('coinglass_call_status last20:')
for r in rows:
    print(tuple(r))
"
```

### Step 7: Export 1h 생성
```bash
mkdir -p ./data/datasets
poetry run python scripts/export_dataset.py \
  --output ./data/datasets/btc_1h.parquet \
  --horizon 120
ls -lh ./data/datasets/btc_1h.parquet
test -s ./data/datasets/btc_1h.parquet && echo "PARQUET_OK" || echo "PARQUET_EMPTY_OR_MISSING"
```

---

## 5. FAIL 해석 가이드

| 모듈 | FAIL 원인 | 조치 |
|------|----------|------|
| DB 연결 | `db` 호스트 미해석 | `docker compose up -d db` 실행 |
| `altdata_check` | Binance WS 수집 lag/fill_rate | 네트워크/재연결 확인 |
| `feature_check` | predictions/market_1s null-rate | predictor 로직 확인 |
| `feature_leak_check` | 시간 정렬/미래 참조 | 즉시 수정 대상 |
| `coinglass_check` | 키/활성화/수집기록 부재 | `.env` 실제 키 + `coinglass_call_status` 확인 |
| Export 0바이트 | 데이터 부족 | 봇 구동 시간 연장 (최소 10분) |

---

## 6. 이슈/조치

### 이슈
1. **CI 환경 제약**: GitHub Actions에는 PostgreSQL DB가 없어 런타임 테스트 불가
2. **Coinglass API 키**: `.env.example`에 placeholder만 존재 — 실제 키 필요

### 조치
1. Codespaces/DevContainer에서 위 Step 1-7 순서대로 실행
2. `.env`에서 `COINGLASS_API_KEY`를 실제 키로 교체
3. 봇 10분 구동 후 점검 실행

### 재발 방지
- `logs/` 디렉토리 `.gitignore`에 추가 완료
- `.env.bak*` 패턴 `.gitignore`에 추가 완료

---

## 7. 다음 단계 제안

| 단계 | 내용 | 전제 |
|------|------|------|
| ALT-7 | 라벨 분포 확인 + r_t 튜닝 | 24h Export 성공 |
| ML-1 | 베이스라인 모델 학습 | 유효 라벨 분포 확보 |
| ML-2 | 백테스트/검증 루프 | 학습 완료 모델 |
