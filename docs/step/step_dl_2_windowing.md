# Step DL-2: PyTorch 딥러닝 데이터 변환 (Windowing & DataLoader)

## 1. 작업 목표

Step 1에서 생성한 `data/datasets/btc_1m_dl.parquet`를 PyTorch가 학습할 수 있는
3차원 텐서 `(Batch, SeqLen=60, Features=52)` 구조로 변환하는 DataLoader 파이프라인을 구축한다.
- Data Leakage 없는 시간순 Train/Val/Test 분할
- RobustScaler를 Train에만 fit (Val/Test는 transform만)
- 슬라이딩 윈도우 Dataset + DataLoader 팩토리

---

## 2. 수정/생성 파일

| 파일 경로 | 역할 |
|---|---|
| `app/predictor/dl_dataset.py` | CryptoTimeSeriesDataset, build_dataloaders(), transform_realtime() |
| `scripts/dl/step_dl_2_dataset.py` | 파이프라인 검증 및 결과 출력 스크립트 |
| `artifacts/dl_prod/scaler.joblib` | Train 전용 RobustScaler (신규 생성) |
| `artifacts/dl_prod/feature_cols.json` | 52개 피처 컬럼 목록 (신규 생성) |

---

## 3. 실행 결과

### 3-1. 시간순 분할 결과

| 분할 | 행 수 | 비율 | 기간 |
|---|---|---|---|
| Train | 180,138 | 70.0% | 2025-09-13 ~ 2026-01-17 |
| Val | 38,601 | 15.0% | 2026-01-17 ~ 2026-02-13 |
| Test | 38,601 | 15.0% | 2026-02-13 ~ 2026-03-12 |
| **합계** | **257,340** | 100% | — |

### 3-2. Tensor Shape (윈도우 기준)

```
Train 윈도우 수: 180,078
Val   윈도우 수:  38,541
Test  윈도우 수:  38,541
```

**첫 번째 배치 (batch_size=512):**
```
x_batch shape : (512, 60, 52)  ← (Batch, SeqLen, Features)
y_batch shape : (512,)          ← (Batch,)
x_batch dtype : torch.float32
y_batch dtype : torch.float32
```

### 3-3. 클래스 분포

| 분할 | LONG(1) 비율 | FLAT(0) 비율 |
|---|---|---|
| Train | 33.5% | 66.5% |
| Val | 34.7% | 65.3% |
| Test | 39.0% | 61.0% |

> 클래스 불균형 존재 (LONG ~34%). Step DL-3 학습 시 BCEWithLogitsLoss의 `pos_weight` 파라미터로 보정 예정.

### 3-4. Data Leakage 검증 결과

```
Train 피처 중앙값 평균: 0.0000  ✅ (RobustScaler fit 기준점 = 0)
Val   피처 중앙값 평균: -0.1308  (약간의 분포 이동 — 정상)
Test  피처 중앙값 평균: -0.3169  (최근 시장 환경 차이 — 정상)
Train IQR 평균 — Q25: -0.4455, Q75: 0.5545
```

**검증 결론:**
- Train 중앙값 정확히 0 → RobustScaler가 Train 분포 기준으로 정확히 적용됨
- Val/Test가 0이 아님 → Val/Test에는 transform만 적용된 것이 확인됨 ✅
- 미래 정보가 Train fit에 개입하지 않음 (Data Leakage 없음) ✅

---

## 4. 주요 설계 결정

### CryptoTimeSeriesDataset
```python
class CryptoTimeSeriesDataset(Dataset):
    def __getitem__(self, idx):
        x_window = self.X[idx : idx + seq_len]     # (60, 52)
        y_label = self.y[idx + seq_len]             # scalar
        return x_window, y_label
```
- 타겟은 윈도우의 **다음 캔들** 기준 (idx+seq_len) — 미래 참조 없음
- Train DataLoader: `shuffle=True` (윈도우 단위 셔플, 내부 순서 보존)

### 스케일링 전략
- **RobustScaler** 선택 이유: 암호화폐 특유의 급등/급락(Fat Tail) 극단값에 강인
- `fit()` → Train만 / `transform()` → Val, Test (Data Leakage 완전 차단)
- `target`, `future_ret` 컬럼은 스케일링 대상에서 제외

### 피처 수 확정: **52개**
- OHLCV, 수익률, 이동평균, 변동성, 기술지표, 시간인코딩, 매크로
- 제외: `target` (타겟), `future_ret` (미래 정보)

---

## 5. 트러블슈팅

특이한 에러 없이 1회 실행에 성공.
NaN 처리: `ffill().bfill()` 체인으로 매크로 일봉 ffill 공백(~3272행) 해소.

---

## 6. 다음 단계 (Next Step)

**Step DL-3: LSTM/1D-CNN 모델 설계 및 학습**

- `app/predictor/dl_model.py` — PyTorch LSTM 모델 클래스 (`LSTMClassifier`)
- 입력: `(Batch, 60, 52)` → 출력: `(Batch, 1)` sigmoid logit
- 손실함수: `BCEWithLogitsLoss(pos_weight=2.0)` (클래스 불균형 보정)
- 조기 종료(Early Stopping) + LR Scheduler 포함
- 가중치 저장: `artifacts/dl_prod/lstm_model.pt`
- 검증 지표: Val Accuracy, Val F1-score, Val AUC-ROC
