# Step ALT-7 — Codespaces 런타임 PASS + Coinglass 실수집 증거 + Export/라벨분포 결과보고서

> **작성일**: 2026-02-22  
> **브랜치**: `copilot/review-alt-3-changes`  
> **환경**: GitHub Actions CI (DB 미제공) → Codespaces/DevContainer에서 실행 필요  
> **상태**: ✅ 코드 준비 완료 — 런타임 게이트는 Codespaces에서 실행 필요

---

## 1. 최종 판정

| 항목 | 결과 | 비고 |
|------|------|------|
| 코드 컴파일 (51 파일) | ✅ PASS | 전체 Python 파일 에러 없음 |
| 필수 파일 (10개) | ✅ PASS | bot, dashboard, diagnostics 4종, export, activate, pipeline checks, datasets dir |
| DB_CONNECT_OK | ⏳ Codespaces 필요 | CI에 `db` 호스트 없음 |
| run_pipeline_checks(600s) | ⏳ Codespaces 필요 | DB 연결 필요 |
| coinglass_call_status ok=true | ⏳ Codespaces 필요 | DB + 실제 API 키 필요 |
| Export 1h (btc_1h.parquet) | ⏳ Codespaces 필요 | DB 연결 필요 |
| Export 24h (btc_24h.parquet) | ⏳ Codespaces 필요 | 24h 봇 구동 후 |
| 라벨 분포 확인 | ⏳ Export 후 | r_t 튜닝 판단 근거 |
| **결론** | **코드 PASS / 런타임 ⏳** | |

---

## 2. 코드 검증 (CI에서 완료)

| 항목 | 결과 |
|------|------|
| Python 컴파일 | ✅ 51/51 PASS |
| `is_real_key` 참조 잔존 | ✅ 0건 |
| `feature_snapshots` 참조 잔존 | ✅ 0건 |
| SQL injection (변경 파일) | ✅ 0건 (make_interval 사용) |
| `.gitignore` | ✅ `.env`, `.env.bak*`, `data/datasets/*`, `logs/` |

---

## 3. Codespaces 실행 절차 (복사-실행용)

### Step 1: DB 확인
```bash
cd /workspaces/crypto_quant_trader
getent hosts db || true
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
# 안 떠있으면: docker compose up -d db

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

### Step 2: 환경변수
```bash
python scripts/activate_env_keys.py
# .env에서 COINGLASS_API_KEY를 실제 키로 교체

python3 -c "
from dotenv import dotenv_values
env = dotenv_values('.env')
k = (env.get('COINGLASS_API_KEY') or '').strip()
print('COINGLASS_ENABLED=', (env.get('COINGLASS_ENABLED') or '').strip())
print('COINGLASS_API_KEY_LEN=', len(k))
print('COINGLASS_API_KEY_PLACEHOLDER=', k.lower() in ('your_key_here',''))
"
```
PASS 기준: `ENABLED=true`, `LEN>=12`, `PLACEHOLDER=False`

### Step 3: 봇 실행 (tmux, 최소 10분)
```bash
mkdir -p logs
tmux new -s bot -d "poetry run python -m app.bot 2>&1 | tee -a logs/bot.log"
tmux ls
tail -n 80 -f logs/bot.log
```

### Step 4: 대시보드 (선택)
```bash
tmux new -s dash -d "poetry run streamlit run app/dashboard.py --server.address 0.0.0.0 --server.port 8501 --server.headless true 2>&1 | tee -a logs/dashboard.log"
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:8501/healthz
```

### Step 5: 10분 후 — 원클릭 점검
```bash
bash scripts/run_pipeline_checks.sh --window 600
echo "EXIT_CODE=$?"
```
FAIL 시:
```bash
poetry run python -m app.diagnostics.altdata_check --window 600
poetry run python -m app.diagnostics.feature_check --window 600
poetry run python -m app.diagnostics.feature_leak_check --window 600
poetry run python -m app.diagnostics.coinglass_check --window 600
```

### Step 6: Coinglass 수집 증거
```bash
python3 -c "
from app.config import load_settings
from sqlalchemy import create_engine, text
s = load_settings()
e = create_engine(s.DB_URL, pool_pre_ping=True)
q = '''
SELECT ts, ok, http_status, COALESCE(error_msg,'') AS error_msg
FROM coinglass_call_status ORDER BY ts DESC LIMIT 30
'''
with e.connect() as c:
    rows = c.execute(text(q)).fetchall()
ok_count = sum(1 for r in rows if r[1])
print('coinglass_call_status last30:')
for r in rows[:10]:
    print((str(r[0]), bool(r[1]), r[2], r[3][:80]))
print('ok_true_count_in_last30=', ok_count)
"
```
PASS: `ok_true_count >= 1`, `http_status` 200대 1회 이상

### Step 7: Export 1h
```bash
mkdir -p ./data/datasets
poetry run python scripts/export_dataset.py \
  --output ./data/datasets/btc_1h.parquet --horizon 120
ls -lh ./data/datasets/btc_1h.parquet
test -s ./data/datasets/btc_1h.parquet && echo "PARQUET_1H_OK" || echo "PARQUET_1H_FAIL"
```

### Step 8: 라벨 분포 확인
```bash
python3 -c "
import pandas as pd
df = pd.read_parquet('./data/datasets/btc_1h.parquet')
cands = [c for c in df.columns if c.lower() in ('label','direction','y','target','class')]
print('label_candidates=', cands)
for c in cands:
    print(f'\n== {c} ==')
    print(df[c].value_counts(dropna=False).to_string())
print(f'\nrows={len(df)} cols={len(df.columns)}')
"
```

### Step 9: 24h 후 — Export 24h
```bash
bash scripts/run_pipeline_checks.sh --window 600
echo "EXIT_CODE=$?"
poetry run python scripts/export_dataset.py \
  --output ./data/datasets/btc_24h.parquet --horizon 120
ls -lh ./data/datasets/btc_24h.parquet
```

---

## 4. 라벨 분포 판단 기준

| 상태 | UP+DOWN 비율 | 판단 | 다음 |
|------|-------------|------|------|
| 정상 | ≥10% | 학습 가능 | 베이스라인 모델 (ML-1) |
| 불균형 | 1~10% | 제한적 학습 | r_t 튜닝 후 재시도 |
| 편향 | <1% (NONE만) | 학습 불가 | r_t/horizon 즉시 튜닝 |

**NONE 편향 원인**:
- `r_t`(임계치)가 너무 큼 → UP/DOWN 기준 미달
- `horizon`(120s)이 해당 변동성 대비 부적절
- 수집 구간의 변동성이 극히 낮음 (횡보장)

---

## 5. 이슈/조치

### 이슈
1. CI 환경에 PostgreSQL 없음 → 런타임 테스트 불가 (ALT-5, ALT-6과 동일)
2. Coinglass API 키 placeholder 상태 → 실제 키 교체 필요

### 조치
1. Codespaces/DevContainer에서 위 Step 1-9 순서대로 실행
2. `.env`에서 `COINGLASS_API_KEY`를 실제 키로 교체
3. 봇 10분+ 구동 후 점검 → Export → 라벨 분포 확인

---

## 6. 다음 단계 결론

| 조건 | 다음 단계 | 내용 |
|------|----------|------|
| 라벨 분포 정상 (UP+DOWN ≥10%) | **ML-1** | 베이스라인 모델 학습 (로지스틱/LightGBM) + 간단 백테스트 |
| 라벨 NONE 편향 | **ALT-8** | r_t/horizon 튜닝 실험 → 분포 개선 후 ML-1 |
| 점검 FAIL 지속 | **ALT-8** | 수집 안정화/에러 수정 후 재시도 |
