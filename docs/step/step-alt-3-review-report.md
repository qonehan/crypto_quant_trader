# Step ALT-3 리뷰 작업 보고서

> **작업일시**: 2026-02-22  
> **브랜치**: `copilot/force-actual-data-collection` → `copilot/review-alt-3-changes` (main 병합 대상)  
> **목적**: ALT-3 변경사항(Coinglass 실수집 강제 / Export 라벨 누수 방지 / 원클릭 점검) 리뷰  
> **상태**: ✅ 리뷰 완료 — **PR #2 OPEN** (maintainer 머지 대기중)

---

## 1. 작업 개요

`copilot/force-actual-data-collection` 브랜치의 ALT-3 변경사항을 리뷰하고, main 병합을 위한 PR 브랜치(`copilot/review-alt-3-changes`)에 반영하였다.

### 변경 규모

| 항목 | 수치 |
|------|------|
| 변경 파일 수 | **26개** |
| 추가 코드 | **+993줄** |
| 삭제 코드 | **-2,340줄** |
| 신규 파일 | 4개 (`scripts/export_dataset.py`, `scripts/run_pipeline_checks.sh`, `step-alt-3-improvements.md`, `step-alt-3-review-report.md`) |
| 삭제 파일 | 8개 (`app/features/*`, `app/diagnostics/prune_altdata.py`, 구 step 문서 4개) |

---

## 2. ALT-3 핵심 변경사항 검증 결과

### A. Coinglass 실수집 강제 (SKIP PASS 금지)

| 항목 | 검증 결과 | 파일 | 설명 |
|------|-----------|------|------|
| `COINGLASS_ENABLED` 설정 | ✅ 확인 | `app/config.py:134` | `COINGLASS_ENABLED: bool = False` 추가됨 |
| `is_real_key()` 함수 제거 | ✅ 확인 | `app/config.py` | placeholder 검증 로직 완전 제거, 코드베이스 전체에 참조 없음 |
| `coinglass_call_status` 테이블 | ✅ 확인 | `app/db/migrate.py:402-418` | `symbol` 컬럼 포함 CREATE TABLE, 인덱스 생성 |
| API 호출 상태 기록 | ✅ 확인 | `app/altdata/coinglass_rest.py` | `insert_coinglass_call_status()` 4곳에서 호출 (성공/실패/에러 모두 기록) |
| `coinglass_check.py` FAIL 정책 | ✅ 확인 | `app/diagnostics/coinglass_check.py` | `COINGLASS_ENABLED=false` → FAIL, 키 미설정 → FAIL |
| `altdata_check.py` FAIL 정책 | ✅ 확인 | `app/diagnostics/altdata_check.py` | `COINGLASS_ENABLED=true` + 데이터 없음 → FAIL (SKIP 아님) |
| `.env.example` 업데이트 | ✅ 확인 | `.env.example` | `COINGLASS_ENABLED` 항목 추가됨 |

**Coinglass 점검 흐름 (coinglass_check.py)**:
```
COINGLASS_ENABLED=false → FAIL ❌ (SKIP 금지)
COINGLASS_API_KEY 미설정 → FAIL ❌ (SKIP 금지)
coinglass_call_status에 ok=true 없음 → FAIL ❌
coinglass_liquidation_map에 데이터 없음 → FAIL ❌
모두 통과 → PASS ✅
```

### B. Export 라벨 생성 누수/왜곡 방지

| 항목 | 검증 결과 | 파일 | 설명 |
|------|-----------|------|------|
| `scripts/export_dataset.py` 신규 | ✅ 확인 | `scripts/export_dataset.py` (187줄) | 구 `app/features/export_dataset.py` 대체 |
| `merge_asof(direction="forward")` | ✅ 확인 | 84행 | `nearest` 매칭 금지, `forward` 만 사용 |
| `label_ts` 컬럼 저장 | ✅ 확인 | 81행 | 매칭된 미래 시각을 `label_ts`로 기록 |
| `label_ts >= t0 + horizon_sec` 보장 | ✅ 확인 | 99-105행 | 위반 row drop + 집계 출력 |
| 최종 검증 게이트 | ✅ 확인 | 163-166행 | Export 완료 전 한 번 더 위반 검사 → FATAL 시 exit 1 |

**라벨 생성 흐름**:
```
[1] predictions 테이블에서 feature 로드
[2] market_1s 테이블에서 가격 로드
[3] merge_asof(direction='forward')로 t0+horizon 이후 첫 가격 매칭
[4] label_ts < t0 + horizon_sec 위반 row → drop + 카운트 출력
[5] label_ts가 없는 row → drop
[6] 최종 검증: 위반 row 0건 확인
[7] parquet/csv 저장
```

### C. 운영 점검 원클릭

| 항목 | 검증 결과 | 파일 | 설명 |
|------|-----------|------|------|
| `run_pipeline_checks.sh` | ✅ 확인 | `scripts/run_pipeline_checks.sh` (79줄) | 4개 점검 순서 실행, FAIL 시 exit 1 |
| `--window` 인자 지원 | ✅ 확인 | 12-20행 | `--window 600` 형태로 사용 가능 |
| `feature_check.py` | ✅ 확인 | `app/diagnostics/feature_check.py` | predictions + market_1s 품질 점검 |
| `feature_leak_check.py` | ✅ 확인 | `app/diagnostics/feature_leak_check.py` | 타임스탬프 순서 + horizon + 정렬 점검 |

**원클릭 점검 실행 순서**:
```
[1/4] altdata_check     — Alt Data 파이프라인 유입 점검
[2/4] feature_check     — Feature 품질 점검 (null rate, 값 범위)
[3/4] feature_leak_check — Feature 미래 누수 점검 (ts 순서, horizon)
[4/4] coinglass_check    — Coinglass 수집 강제 점검 (PASS/FAIL)
──────────────────────────────────────────────
PIPELINE OVERALL: PASS ✅ / FAIL ❌
```

### D. SQL Injection 수정 (추가 보안 작업)

`copilot/force-actual-data-collection` 브랜치의 원래 코드 일부에 SQL 문자열 보간(`interval '{w} seconds'`)이 있었으며, 리뷰 과정에서 **파라미터화된 쿼리(`make_interval(secs => :wsec)`)로 모두 교체**하였다.

| 파일 | 수정 개소 |
|------|-----------|
| `app/diagnostics/coinglass_check.py` | 2곳 |
| `app/diagnostics/feature_check.py` | 3곳 |
| `app/diagnostics/feature_leak_check.py` | 5곳 |
| `app/diagnostics/altdata_check.py` | 1곳 (원래 copilot 브랜치에서 누락된 곳) |
| **합계** | **11곳** |

**수정 전** (취약):
```python
f"... interval '{window_sec} seconds'"
```

**수정 후** (안전):
```python
"... make_interval(secs => :wsec)", {"sym": symbol, "wsec": window_sec}
```

### E. 코드베이스 정리

| 항목 | 상태 | 설명 |
|------|------|------|
| `app/features/` 모듈 삭제 | ✅ | `scripts/export_dataset.py`로 대체 |
| `feature_snapshots` 테이블 제거 | ✅ | migrate.py에서 CREATE TABLE 제거, 코드 참조 0건 |
| `is_real_key()` 함수 제거 | ✅ | 전체 코드베이스에서 참조 0건 |
| `prune_altdata.py` 삭제 | ✅ | 불필요한 진단 도구 제거 |
| Dashboard G4 패널 제거 | ✅ | `feature_snapshots` 기반 패널 제거 (dashboard.py -234줄) |
| `activate_env_keys.py` 간소화 | ✅ | placeholder 검증 로직 제거 (-63줄) |
| `predictor/runner.py` 간소화 | ✅ | `feature_snapshots` 저장 로직 제거 (-147줄) |
| 구 step 문서 정리 | ✅ | `step-alt-1-improvements.md`, `step-alt-2-improvements.md`, `step11-*.md` 삭제 |

---

## 3. 검증 체크리스트

| # | 검증 항목 | 결과 |
|---|----------|------|
| 1 | Python 구문 검사 (14개 파일 전체 py_compile) | ✅ PASS |
| 2 | `is_real_key` 참조 잔존 | ✅ 0건 (완전 제거) |
| 3 | `feature_snapshots` 참조 잔존 | ✅ 0건 (완전 제거) |
| 4 | `app.features` 참조 잔존 | ✅ 0건 (완전 제거) |
| 5 | SQL injection 패턴 (`interval '{}'`) 잔존 | ✅ 0건 (변경 파일 내) |
| 6 | Ruff lint — 신규 이슈 | ✅ 0건 (기존 이슈만 존재) |
| 7 | CodeQL 보안 스캔 | ✅ 0 alerts |

---

## 4. 변경 파일 전체 목록

### 수정된 파일 (M)

| 파일 | 변경 내용 |
|------|----------|
| `.env.example` | `COINGLASS_ENABLED` 항목 추가 |
| `app/altdata/coinglass_rest.py` | 키 검증 간소화, per-call status 기록 추가 |
| `app/altdata/runner.py` | `is_real_key` import 제거 |
| `app/altdata/writer.py` | `insert_coinglass_call_status` 시그니처 변경 (symbol 추가, poll_count 제거) |
| `app/bot.py` | `is_real_key` import 제거 |
| `app/config.py` | `is_real_key()` 제거, `COINGLASS_ENABLED` 추가, `BINANCE_METRICS_FRESH_SEC` 제거 |
| `app/dashboard.py` | Coinglass 섹션 간소화, G4 feature_snapshots 패널 제거 |
| `app/db/migrate.py` | `coinglass_call_status` 스키마 변경 (symbol 추가), `feature_snapshots` 제거 |
| `app/diagnostics/altdata_check.py` | `COINGLASS_ENABLED` FAIL 정책 + SQL injection 수정 |
| `app/diagnostics/coinglass_check.py` | 강제 점검 모듈 전면 재작성 + SQL injection 수정 |
| `app/diagnostics/feature_check.py` | predictions + market_1s 품질 점검으로 재작성 + SQL injection 수정 |
| `app/diagnostics/feature_leak_check.py` | predictions 기반 누수 점검으로 재작성 + SQL injection 수정 |
| `app/predictor/runner.py` | feature_snapshots 저장 로직 제거 |
| `scripts/activate_env_keys.py` | placeholder 검증 로직 제거, 간소화 |

### 신규 파일 (A)

| 파일 | 설명 |
|------|------|
| `scripts/export_dataset.py` | 라벨 누수 방지 Export (merge_asof forward + label_ts 검증) |
| `scripts/run_pipeline_checks.sh` | 운영 점검 원클릭 스크립트 (4개 점검, exit 1 on FAIL) |
| `step-alt-3-improvements.md` | ALT-3 변경 요약 문서 |
| `step-alt-3-review-report.md` | ALT-3 리뷰 & 병합 작업 보고서 (본 문서) |

### 삭제 파일 (D)

| 파일 | 사유 |
|------|------|
| `app/diagnostics/prune_altdata.py` | 불필요한 진단 도구 |
| `app/features/__init__.py` | `app/features` 모듈 삭제 |
| `app/features/export_dataset.py` | `scripts/export_dataset.py`로 대체 |
| `app/features/writer.py` | `app/features` 모듈 삭제 |
| `step-alt-1-improvements.md` | 구 문서 정리 |
| `step-alt-2-improvements.md` | 구 문서 정리 |
| `step11-fix-ip-whitelist.md` | 구 문서 정리 |
| `step11-verify.md` | 구 문서 정리 |

---

## 5. 사용법

### 원클릭 파이프라인 점검
```bash
bash scripts/run_pipeline_checks.sh --window 600
```

### 개별 점검
```bash
poetry run python -m app.diagnostics.altdata_check --window 600
poetry run python -m app.diagnostics.feature_check --window 600
poetry run python -m app.diagnostics.feature_leak_check --window 600
poetry run python -m app.diagnostics.coinglass_check --window 600
```

### 데이터셋 Export
```bash
poetry run python scripts/export_dataset.py --output dataset.parquet --horizon 120
```

---

## 6. 주요 원칙 (ALT-3 정책)

1. **SKIP PASS 금지**: `COINGLASS_ENABLED=true`일 때 데이터 미수집은 무조건 FAIL
2. **Feature 미래값 조인 금지**: `ts` 기준 미래값(`>ts`) 조인 절대 금지
3. **라벨 horizon 준수**: `label_ts >= t0 + horizon_sec` — `nearest` 매칭 금지, `forward`만 허용
4. **SQL injection 방지**: `interval '{}'` 문자열 보간 금지, `make_interval(secs => :param)` 사용

---

## 7. 보안 요약

- **SQL injection**: 진단 모듈 전체(11곳)에서 문자열 보간 → 파라미터화 쿼리로 수정 완료
- **CodeQL 스캔**: 0 alerts (보안 취약점 없음)
- **API 키 노출 방지**: `is_real_key()` 제거 후 단순 boolean 체크로 전환, 키 값 로그 출력 없음

---

## 8. PR 상태 & 다음 액션

### 8-1. PR 현황

| PR | 브랜치 | 상태 | 처리 |
|----|--------|------|------|
| #1 | `copilot/force-actual-data-collection` | **Open → maintainer가 Close** | 중간 산출물 — 최종본은 PR #2 |
| #2 | `copilot/review-alt-3-changes` | **Open** | ✅ maintainer가 머지 |

### 8-2. main 반영

main 병합은 **maintainer(권한 보유자)**가 PR #2를 머지하여 수행한다.

### 8-3. 브랜치 삭제 (머지 완료 후, 권한 있을 때만)

PR #2가 머지 완료된 후 maintainer가 브랜치를 삭제한다:
- `copilot/force-actual-data-collection` — 원격 삭제
- `copilot/review-alt-3-changes` — 원격 삭제 (GitHub 머지 시 자동 삭제 옵션 사용 가능)

---

## 9. 재현/검증 (머지 전후 모두 실행 가능)

### 원클릭 파이프라인 점검
```bash
bash scripts/run_pipeline_checks.sh --window 600
echo "EXIT_CODE=$?"
```
- **기대 결과**: EXIT_CODE=0 (4개 점검 모두 PASS)

### 개별 점검
```bash
poetry run python -m app.diagnostics.altdata_check --window 600
poetry run python -m app.diagnostics.feature_check --window 600
poetry run python -m app.diagnostics.feature_leak_check --window 600
poetry run python -m app.diagnostics.coinglass_check --window 600
```
- **기대 결과**: 각 점검 모듈이 PASS 출력, exit 0 반환

### Export 검증
```bash
poetry run python scripts/export_dataset.py --output dataset.parquet --horizon 120
ls -lh dataset.parquet
```
- **기대 결과**: parquet 파일 생성 (0바이트가 아닌 유효한 파일)

---

## 10. 리스크/주의사항

- **운영 영향**: `COINGLASS_ENABLED=true` + 실제 API 키가 `.env`에 설정되어야 `coinglass_check`가 PASS
- **롤백**: main의 `backup/pre-alt3-merge-*` 태그(생성 시)로 되돌리기 가능
- **키/권한**: Coinglass API 키가 미설정이면 점검 FAIL — 이는 ALT-3 정책상 의도된 동작

---

## 11. 최종 체크리스트

- [x] `copilot/review-alt-3-changes`가 ALT-3 최종본 (copilot/force-actual-data-collection + SQL injection 수정 + 보고서)
- [x] Python 구문 검사 14개 파일 전체 PASS
- [x] 제거 대상 참조(`is_real_key`, `feature_snapshots`, `app.features`) 잔존 0건
- [x] SQL injection 취약 패턴 잔존 0건 (변경 파일 내)
- [x] CodeQL 보안 스캔 0 alerts
- [x] PR #1 Close 필요 표시 (maintainer가 Close — 권한 제한으로 자동 Close 불가)
- [x] PR #2 Open 유지 (maintainer 머지 대기)
- [x] 보고서에 재현/검증 커맨드 포함
- [ ] (maintainer) PR #2 → main 머지
- [ ] (maintainer, 머지 후) copilot 브랜치 삭제
- [ ] (머지 후) `bash scripts/run_pipeline_checks.sh --window 600` → EXIT_CODE=0
- [ ] (머지 후) `poetry run python scripts/export_dataset.py --output dataset.parquet --horizon 120` → 파일 생성 성공
