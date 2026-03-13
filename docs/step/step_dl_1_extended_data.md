# Step DL-1 (개정): 2년치 데이터 수집 및 Colab 학습 준비 보고서

## 1. 작업 개요

| 항목 | 내용 |
|---|---|
| 목적 | TCN 모델 일반화 성능 극대화를 위한 학습 데이터 확장 |
| 기존 | 6개월치 (180일) → `btc_1m_dl.parquet` |
| 변경 | **2년치 (730일)** → `btc_1m_dl_2y.parquet` |
| 추가 | Colab GPU 학습 노트북 (`colab_tcn_train.ipynb`) |

---

## 2. 데이터 수집 결과

> 아래 수치는 수집 완료 후 기록할 것 (스크립트 실행 시 터미널에 출력됨)

| 항목 | 값 |
|---|---|
| **시작일** | *수집 후 기록* |
| **종료일** | *수집 후 기록* |
| **전체 행 수** | *수집 후 기록 (예상: ~1,050,000행)* |
| **유효 행 수** (target 존재) | *수집 후 기록 (전체 −60행)* |
| **파일 크기** | *수집 후 기록 (예상: 50~80 MB)* |
| LONG(1) 비율 | *수집 후 기록* |
| 피처 수 | 52개 (기술지표 44 + 매크로 8) |

### 예상 수치 (사전 계산)

```
목표 일수     : 730일 (2년)
총 분봉 수    : 730 × 1440 = 1,051,200분
API 호출 수   : 1,051,200 ÷ 200 ≈ 5,256회
예상 소요 시간: 5,256 × 0.13초 ≈ 11.4분 (에러 없을 때)
예상 파일 크기: ~60 MB (Parquet 압축)
```

---

## 3. 개정된 수집 스크립트 (`step_dl_1_collect_data.py`) 변경사항

### 3-1. CLI 인수 지원

```bash
# 2년 수집 (기본값)
poetry run python scripts/dl/step_dl_1_collect_data.py

# 6개월 수집 (기존 동작)
poetry run python scripts/dl/step_dl_1_collect_data.py --days 180

# 체크포인트 재개
poetry run python scripts/dl/step_dl_1_collect_data.py --resume
```

### 3-2. 체크포인트/재개 메커니즘

| 항목 | 내용 |
|---|---|
| 저장 위치 | `data/.checkpoints/upbit_1m_730d_ckpt.parquet` |
| 저장 주기 | 매 **300 청크** (300 × 200분 = 1,000시간) |
| 재개 방법 | `--resume` 플래그 — 체크포인트 로드 후 가장 오래된 시점부터 이어서 수집 |
| 완료 시 | 체크포인트 파일 자동 삭제 |

```
인터럽트 발생 시:
  KeyboardInterrupt → 즉시 체크포인트 저장 → 종료

재시작 시:
  --resume → 체크포인트 로드(N행) → 나머지 기간 수집 → 병합 → 저장
```

### 3-3. 출력 파일 경로 자동 결정

```python
days <= 200  →  data/datasets/btc_1m_dl.parquet    (6개월 이하)
days > 200   →  data/datasets/btc_1m_dl_2y.parquet (2년)
```

### 3-4. rate limit 설정

```python
SLEEP_SEC          = 0.13   # 업비트 ~8req/s (기존 0.12 → 안전 마진 추가)
SLEEP_ON_ERROR_SEC = 5      # 에러 시 대기 (기존 3 → 5초로 강화)
CHECKPOINT_EVERY   = 300    # 300청크마다 중간 저장
```

---

## 4. Colab 학습 노트북 (`colab_tcn_train.ipynb`)

### 4-1. 파일 위치

```
scripts/dl/colab_tcn_train.ipynb
```

### 4-2. 노트북 구성

| # | 셀 제목 | 내용 |
|---|---|---|
| 1 | 환경 설정 | pip 설치, GPU 확인, Drive 마운트 |
| 2 | 경로·하이퍼파라미터 | Drive 경로, 모든 하이퍼파라미터 한 곳에 정의 |
| 3 | 임포트 | torch, sklearn, pandas 등 |
| 4 | 데이터 로드 및 전처리 | parquet 로드, 분할, RobustScaler fit, DataLoader |
| 5 | TCN 모델 정의 | 전체 코드 inline (외부 의존성 없음) |
| 6 | 학습 | FocalLoss, AdamW, Precision 우선 Early Stopping |
| 7 | 평가·Threshold 탐색 | Test 평가, 0.45~0.75 sweep |
| 8 | Drive 저장·다운로드 | 아티팩트 Drive 자동 저장, 로컬 다운로드 |

### 4-3. GPU 가속 효과 (예상)

| 환경 | 에포크당 소요 시간 (Train 70만행 기준) |
|---|---|
| CPU (로컬) | ~30~60분/에포크 |
| **Colab T4 GPU** | **~2~5분/에포크** |
| Colab A100 GPU | ~30초~1분/에포크 |

> TCN은 LSTM과 달리 **완전 병렬 학습**이 가능하므로 GPU 효과가 극대화됨.

### 4-4. 노트북 실행 절차

```
1. Google Colab → 파일 열기 → colab_tcn_train.ipynb 업로드
2. 런타임 → 런타임 유형 변경 → T4 GPU (또는 A100)
3. btc_1m_dl_2y.parquet → Drive/MyDrive/btc_quant/ 에 업로드
4. 셀 2의 DRIVE_ROOT 경로 확인
5. 전체 실행 (런타임 → 모두 실행)
6. 학습 완료 후 마지막 셀에서 아티팩트 다운로드
7. 다운로드 파일 → 서버의 artifacts/dl_prod/ 에 배치
```

### 4-5. Drive 저장 아티팩트

```
/content/drive/MyDrive/btc_quant/artifacts/
  ├── tcn_model.pt           ← 학습된 가중치
  ├── tcn_model_meta.json    ← 모델 구조 메타데이터
  ├── scaler.joblib          ← RobustScaler (Train fit)
  ├── feature_cols.json      ← 52개 피처 순서
  └── tcn_train_result.json  ← 성능 지표 요약
```

---

## 5. 데이터 확장 효과 (2년 vs 6개월)

| 항목 | 6개월 (DL-3) | 2년 (DL-5) |
|---|---|---|
| 학습 행 수 | ~127,000행 | **~521,000행** |
| Train 윈도우 수 | ~42,000개 | **~173,000개** |
| 시장 사이클 커버 | 상승/하락 일부 | **강세·약세·횡보 모두** |
| 예상 일반화 성능 | 기준 | 향상 (과적합 위험 ↓) |

> 2년치는 2022년 약세장(FTX 붕괴), 2023년 회복, 2024~2025년 강세장을 모두 포함하여
> 다양한 시장 국면에서의 일반화 성능을 기대할 수 있다.

---

## 6. 준비 완료 선언

- [x] `step_dl_1_collect_data.py` — 2년치 수집, 체크포인트/재개 지원으로 개정
- [x] `colab_tcn_train.ipynb` — 외부 의존성 없는 독립 실행 노트북 생성
- [ ] `btc_1m_dl_2y.parquet` 수집 실행 (`poetry run python scripts/dl/step_dl_1_collect_data.py`)
- [ ] Drive 업로드 및 Colab 학습 실행
- [ ] 학습 아티팩트 서버 배치 (`artifacts/dl_prod/`)

**Colab 학습 준비 완료.** 데이터 수집 후 즉시 노트북 실행 가능한 상태.
