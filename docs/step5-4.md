# STEP 5.4 결과 보고서 — Barrier EWMA 피드백 + dt 다운샘플 σ 추정

## 목표
- barrier(r_t)의 운영 목표를 "none_rate 목표"로 명시하고, 정산 결과(지연)를 이용해 k_vol_eff를 EWMA 자동 조절
- σ 추정을 dt(=VOL_DT_SEC) 다운샘플 기반으로 전환하여 1초 잡음 완화
- barrier_params 테이블에 상태 저장 (재시작 후 유지)
- barrier_state에 k_vol_eff/none_ewma/target_none/alpha/eta/vol_dt_sec 매 tick 기록

---

## 추가/수정 파일 목록

| 파일 | 변경 유형 | 설명 |
|------|-----------|------|
| `app/config.py` | **수정** | TARGET_NONE/EWMA_ALPHA/EWMA_ETA/K_VOL_MIN/K_VOL_MAX/VOL_DT_SEC 추가 |
| `app/barrier/controller.py` | **전면 재작성** | dt 다운샘플 σ, k_vol_eff 사용, barrier_params 로드, v1 컬럼 기록 |
| `app/evaluator/evaluator.py` | **수정** | EWMA 피드백 루프 (_update_ewma_feedback), barrier_params 갱신 |
| `app/db/writer.py` | **수정** | upsert_barrier_state에 6개 v1 컬럼 추가, barrier_params CRUD 함수 2개 추가 |

---

## 핵심 변경 사항

### Settings (config.py)
```
TARGET_NONE=0.55  EWMA_ALPHA=0.98  EWMA_ETA=0.15
K_VOL_MIN=0.50    K_VOL_MAX=2.00   VOL_DT_SEC=5
```

### σ 추정 dt 다운샘플 (controller.py)
- `mid_close_1s` (없으면 `mid`) 조회 → `mids[::dt]`로 다운샘플
- `sigma_dt = std(log_returns, ddof=1)`
- `sigma_1s = sigma_dt / sqrt(dt)`
- `sigma_h = sigma_1s * sqrt(H_SEC)`
- Warmup: `sample_n < max(30, (VOL_WINDOW_SEC/dt)*0.3)` → R_MIN 고정

### r_t 계산 (controller.py)
```
r_t = clamp(R_MIN, k_vol_eff * sigma_h, R_MAX)
```
- `k_vol_eff`는 barrier_params에서 로드 (evaluator가 EWMA로 갱신)

### EWMA 피드백 (evaluator.py)
- 정산된 각 결과에 대해 순차 적용:
  ```
  none_flag = 1 if actual_direction=="NONE" else 0
  none_ewma = alpha * none_ewma + (1-alpha) * none_flag
  k_vol_eff = k_vol_eff * exp(-eta * (none_ewma - target_none))
  k_vol_eff = clamp(K_VOL_MIN, k_vol_eff, K_VOL_MAX)
  ```
- none_ewma > target → NONE 과다 → k_vol_eff 감소 → r_t 줄어듦 → hit 증가
- none_ewma < target → hit 과다 → k_vol_eff 증가 → r_t 늘어남 → NONE 증가

### barrier_params CRUD (writer.py)
- `get_or_create_barrier_params()`: SELECT, 없으면 INSERT defaults
- `update_barrier_params()`: k_vol_eff, none_ewma, last_eval_t0, updated_at=now()

---

## Evaluator 피드백 로그 (BarrierFeedback 발췌)

```
12:09:20 [INFO] BarrierFeedback: n_new=26 none_ewma=0.6703 k_vol_eff=0.7629 (target=0.55 alpha=0.98 eta=0.15)
12:09:25 [INFO] BarrierFeedback: n_new=1 none_ewma=0.6569 k_vol_eff=0.7508 (target=0.55 alpha=0.98 eta=0.15)
12:09:30 [INFO] BarrierFeedback: n_new=1 none_ewma=0.6638 k_vol_eff=0.7381 (target=0.55 alpha=0.98 eta=0.15)
12:09:40 [INFO] BarrierFeedback: n_new=1 none_ewma=0.6771 k_vol_eff=0.7111 (target=0.55 alpha=0.98 eta=0.15)
12:10:00 [INFO] BarrierFeedback: n_new=1 none_ewma=0.7022 k_vol_eff=0.6527 (target=0.55 alpha=0.98 eta=0.15)
12:10:20 [INFO] BarrierFeedback: n_new=1 none_ewma=0.7253 k_vol_eff=0.5906 (target=0.55 alpha=0.98 eta=0.15)
12:10:40 [INFO] BarrierFeedback: n_new=1 none_ewma=0.7466 k_vol_eff=0.5273 (target=0.55 alpha=0.98 eta=0.15)
12:10:50 [INFO] BarrierFeedback: n_new=1 none_ewma=0.7566 k_vol_eff=0.5000 (target=0.55 alpha=0.98 eta=0.15)
12:10:55 [INFO] BarrierFeedback: n_new=1 none_ewma=0.7615 k_vol_eff=0.5000 (target=0.55 alpha=0.98 eta=0.15)
```

→ none_ewma > 0.55(target)이므로 k_vol_eff가 1.0에서 0.50(K_VOL_MIN)까지 감소하여 clamp됨.
이후 DOWN이 계속 유입되면 none_ewma가 내려가서 k_vol_eff가 다시 올라감.

---

## barrier_params 쿼리 출력

```
('KRW-BTC', 0.5843, 0.4158, 0.55, 0.98, 0.15,
 datetime(2026-02-17 12:12:10 UTC), datetime(2026-02-17 12:14:11 UTC))
```

- k_vol_eff = 0.5843 (초기 1.0에서 감소 후 DOWN 유입으로 다시 회복 중)
- none_ewma = 0.4158 (target 0.55 이하 — DOWN이 많이 들어오면서 하락)
- updated_at 갱신 확인

---

## barrier_state 최근 10행 출력

```
ts                        | status | r_t    | sigma_1s     | sigma_h      | sample_n | k_vol_eff | none_ewma | target | dt
2026-02-17 12:14:10 UTC   | OK     | 0.0010 | 0.00006264   | 0.00068620   | 82       | 0.5727    | 0.4243    | 0.55   | 5
2026-02-17 12:14:05 UTC   | OK     | 0.0010 | 0.00006245   | 0.00068408   | 81       | 0.5620    | 0.4330    | 0.55   | 5
2026-02-17 12:14:00 UTC   | OK     | 0.0010 | 0.00006284   | 0.00068837   | 80       | 0.5522    | 0.4418    | 0.55   | 5
2026-02-17 12:13:55 UTC   | OK     | 0.0010 | 0.00006324   | 0.00069274   | 79       | 0.5433    | 0.4508    | 0.55   | 5
2026-02-17 12:13:50 UTC   | OK     | 0.0010 | 0.00006364   | 0.00069716   | 78       | 0.5353    | 0.4600    | 0.55   | 5
2026-02-17 12:13:45 UTC   | OK     | 0.0010 | 0.00006406   | 0.00070172   | 77       | 0.5281    | 0.4694    | 0.55   | 5
2026-02-17 12:13:40 UTC   | OK     | 0.0010 | 0.00006380   | 0.00069891   | 76       | 0.5218    | 0.4790    | 0.55   | 5
2026-02-17 12:13:35 UTC   | OK     | 0.0010 | 0.00006072   | 0.00066517   | 75       | 0.5163    | 0.4888    | 0.55   | 5
2026-02-17 12:13:30 UTC   | OK     | 0.0010 | 0.00006113   | 0.00066962   | 74       | 0.5116    | 0.4988    | 0.55   | 5
2026-02-17 12:13:25 UTC   | OK     | 0.0010 | 0.00006154   | 0.00067416   | 73       | 0.5076    | 0.5089    | 0.55   | 5
```

- vol_dt_sec = 5 (설정값과 일치)
- sigma_1s/sigma_h: NaN 없이 정상값
- k_vol_eff: 시간에 따라 변화 관측 (0.5076 → 0.5727)
- none_ewma: 시간에 따라 0.4243 → 0.5089 변화 관측

---

## k_vol_eff 변화 코멘트

k_vol_eff는 초기값 1.0에서 시작하여, NONE 비율이 높은 초기 정산 결과(none_ewma ~0.76)에 의해 0.50(K_VOL_MIN)까지 급감.
이후 DOWN 정산이 계속 유입되면서 none_ewma가 0.55 아래로 내려가자, k_vol_eff가 다시 상승 중 (0.50 → 0.58).
**피드백 루프가 정상 동작하고 있음을 확인.**

---

## Sanity Check

| 항목 | 값 |
|------|-----|
| barrier_state total (7min) | 78 rows |
| status=OK | 47 rows (WARMUP 이후) |
| sigma_1s NOT NULL | 78/78 (100%) |
| vol_dt_sec NOT NULL | 78/78 (100%) |
| k_vol_eff NOT NULL | 78/78 (100%) |
| none_ewma NOT NULL | 78/78 (100%) |
| barrier_params updated_at 갱신 | 확인 |
| BarrierFeedback 로그 | 다수 관측 (n_new>0) |

---

## DoD 체크리스트

- [x] barrier_params row가 존재하며 updated_at이 갱신됨
- [x] barrier_state에 k_vol_eff/none_ewma/target_none/vol_dt_sec가 NULL 없이 기록됨
- [x] sigma_1s/sigma_h가 WARMUP 이후 OK 상태에서 NaN 없이 생성됨
- [x] EWMA 피드백 로그가 다수 관측됨 (n_new>0)
- [x] k_vol_eff가 초기값(1.0) 대비 변화함 (→0.50 → 0.58 회복 중)
- [x] dt 다운샘플 적용 (vol_dt_sec=5)
