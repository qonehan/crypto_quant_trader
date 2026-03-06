# Step ALT-3 Improvements Report

## 변경 요약

### A. Coinglass 실수집 강제

| 항목 | 상태 | 설명 |
|------|------|------|
| `COINGLASS_ENABLED` 설정 추가 | ✅ | `app/config.py`에 `COINGLASS_ENABLED: bool = False` 추가 |
| `coinglass_call_status` 테이블 | ✅ | `app/db/migrate.py`에 CREATE TABLE 추가 |
| API 호출 상태 기록 | ✅ | `coinglass_rest.py`에서 매 호출 결과를 `coinglass_call_status`에 기록 |
| `coinglass_check.py` 신규 | ✅ | 전용 점검 모듈: `COINGLASS_ENABLED=false` → FAIL, 키 미설정 → FAIL |
| `altdata_check.py` 수정 | ✅ | `COINGLASS_ENABLED=true`일 때 Coinglass SKIP → FAIL 변경 |
| `.env.example` 업데이트 | ✅ | `COINGLASS_ENABLED` 항목 추가 |

### B. Export 라벨 생성 누수/왜곡 방지

| 항목 | 상태 | 설명 |
|------|------|------|
| `export_dataset.py` 신규 | ✅ | `merge_asof(direction="forward")` 사용 |
| `label_ts` 컬럼 저장 | ✅ | 매칭된 미래 시각 기록 |
| `label_ts >= t0 + horizon_sec` 보장 | ✅ | 위반 row drop + 집계 출력 |

### C. 운영 점검 원클릭

| 항목 | 상태 | 설명 |
|------|------|------|
| `scripts/run_pipeline_checks.sh` | ✅ | 4개 점검 순서대로 실행, FAIL 시 exit 1 |
| `feature_check.py` 신규 | ✅ | Feature 품질 점검 |
| `feature_leak_check.py` 신규 | ✅ | Feature 미래 누수 점검 |

## 점검 실행 방법

```bash
# 전체 파이프라인 점검 (원클릭)
bash scripts/run_pipeline_checks.sh --window 600

# 개별 점검
poetry run python -m app.diagnostics.altdata_check --window 600
poetry run python -m app.diagnostics.feature_check --window 600
poetry run python -m app.diagnostics.feature_leak_check --window 600
poetry run python -m app.diagnostics.coinglass_check --window 600
```

## 데이터셋 Export

```bash
poetry run python scripts/export_dataset.py --output dataset.parquet --horizon 120
```

## 주요 원칙

1. **SKIP PASS 금지**: `COINGLASS_ENABLED=true`일 때 데이터 미수집은 FAIL
2. **Feature 미래값 조인 금지**: `ts` 기준 미래값(`>ts`) 조인 절대 금지
3. **라벨 horizon 준수**: `label_ts >= t0 + horizon_sec` (nearest 매칭 금지, forward만 허용)
