# STEP 5.5 결과 보고서 — BaselineModel v1 패치

## 1. 추가/수정 파일 목록

| 파일 | 변경 내용 |
|---|---|
| `app/config.py` | v1 Settings 추가 (P_HIT_CZ, SCORE_A/B/C, ENTER_* 임계값) — 이전 스텝에서 완료 |
| `app/models/baseline_v1.py` | **신규** — `BaselineModelV1(BaseModel)` 구현, model_version="baseline_v1_exec" |
| `app/models/interface.py` | PredictionOutput에 v1 필드 추가 (z_barrier, p_hit_base, ev_rate, r_none_pred, t_up/t_down_cond_pred, mom_z, spread_bps, imb_notional_top5, action_hat) |
| `app/predictor/runner.py` | v1 필드 DB 적재 + Pred(v1) 로그 포맷 |
| `app/db/writer.py` | upsert_prediction SQL에 v1 컬럼 10개 추가 |
| `app/db/migrate.py` | predictions 테이블 v1 컬럼 마이그레이션 (이전 스텝에서 완료) |
| `app/bot.py` | `BaselineModel` → `BaselineModelV1` 교체 |
| `.env.example` | v1 파라미터 주석 추가 |

## 2. Bot 로그 (barrier OK 구간, ~30초)

```
03:22:45 Pred(v1): t0=03:22:45 r_t=0.001000 z=1.725 p_none=0.5248 p_up=0.1778 p_down=0.2975 ev=-0.00183122 ev_rate=-0.00001550 action=STAY_FLAT
03:22:50 Pred(v1): t0=03:22:50 r_t=0.001000 z=1.720 p_none=0.5227 p_up=0.1340 p_down=0.3433 ev=-0.00173445 ev_rate=-0.00001494 action=STAY_FLAT
03:22:55 Pred(v1): t0=03:22:55 r_t=0.001000 z=1.739 p_none=0.5303 p_up=0.1746 p_down=0.2951 ev=-0.00157330 ev_rate=-0.00001332 action=STAY_FLAT
03:23:00 Pred(v1): t0=03:23:00 r_t=0.001000 z=1.757 p_none=0.5378 p_up=0.2273 p_down=0.2349 ev=-0.00160398 ev_rate=-0.00001338 action=STAY_FLAT
03:23:05 Pred(v1): t0=03:23:05 r_t=0.001000 z=1.775 p_none=0.5451 p_up=0.2250 p_down=0.2298 ev=-0.00142575 ev_rate=-0.00001189 action=STAY_FLAT
03:23:10 Pred(v1): t0=03:23:10 r_t=0.001000 z=1.781 p_none=0.5476 p_up=0.2603 p_down=0.1920 ev=-0.00154958 ev_rate=-0.00001302 action=STAY_FLAT
03:23:15 Pred(v1): t0=03:23:15 r_t=0.001000 z=1.791 p_none=0.5515 p_up=0.1833 p_down=0.2653 ev=-0.00170048 ev_rate=-0.00001431 action=STAY_FLAT
03:23:20 Pred(v1): t0=03:23:20 r_t=0.001000 z=1.808 p_none=0.5585 p_up=0.2483 p_down=0.1933 ev=-0.00156460 ev_rate=-0.00001312 action=STAY_FLAT
03:23:25 Pred(v1): t0=03:23:25 r_t=0.001000 z=1.826 p_none=0.5653 p_up=0.2184 p_down=0.2163 ev=-0.00161864 ev_rate=-0.00001349 action=STAY_FLAT
03:23:30 Pred(v1): t0=03:23:30 r_t=0.001000 z=1.843 p_none=0.5721 p_up=0.2361 p_down=0.1918 ev=-0.00145724 ev_rate=-0.00001220 action=STAY_FLAT
03:25:15 Pred(v1): t0=03:25:15 r_t=0.001000 z=2.163 p_none=0.6896 p_up=0.2092 p_down=0.1012 ev=-0.00127445 ev_rate=-0.00001078 action=STAY_FLAT
03:25:50 Pred(v1): t0=03:25:50 r_t=0.001000 z=2.261 p_none=0.7213 p_up=0.1514 p_down=0.1273 ev=-0.00122715 ev_rate=-0.00001025 action=STAY_FLAT
03:27:05 Pred(v1): t0=03:27:05 r_t=0.001000 z=2.453 p_none=0.7779 p_up=0.1721 p_down=0.0500 ev=-0.00121445 ev_rate=-0.00001034 action=STAY_FLAT
03:27:40 Pred(v1): t0=03:27:40 r_t=0.001000 z=2.539 p_none=0.8004 p_up=0.1318 p_down=0.0678 ev=-0.00121982 ev_rate=-0.00001026 action=STAY_FLAT
```

## 3. predictions 최근 20행 (DB 쿼리)

```
t0                       r_t     z_barrier  p_hit_base  p_up    p_down  p_none  t_up     t_down   mom_z    spread_bps  imb_notional  ev          ev_rate       action_hat  model_version
2026-02-18 03:27:40 UTC  0.001   2.539      0.200       0.1318  0.0678  0.8004  112.02   120.00   0.2148   0.200       0.363         -0.00122    -1.026e-05    STAY_FLAT   baseline_v1_exec
2026-02-18 03:27:35 UTC  0.001   2.527      0.203       0.0859  0.1168  0.7973  120.00   116.32   0.1977   0.300      -0.573         -0.00135    -1.128e-05    STAY_FLAT   baseline_v1_exec
2026-02-18 03:27:30 UTC  0.001   2.515      0.206       0.0654  0.1403  0.7943  120.00   110.84   0.0389   0.200      -0.802         -0.00139    -1.172e-05    STAY_FLAT   baseline_v1_exec
2026-02-18 03:27:25 UTC  0.001   2.503      0.209       0.0870  0.1219  0.7911  120.00   115.95   0.1048   0.300      -0.465         -0.00135    -1.133e-05    STAY_FLAT   baseline_v1_exec
2026-02-18 03:27:20 UTC  0.001   2.490      0.212       0.0945  0.1176  0.7878  120.00   117.38   0.3316   0.100      -0.706         -0.00130    -1.085e-05    STAY_FLAT   baseline_v1_exec
2026-02-18 03:27:15 UTC  0.001   2.478      0.215       0.1228  0.0926  0.7846  116.61   120.00   0.0932   0.400       0.182         -0.00132    -1.100e-05    STAY_FLAT   baseline_v1_exec
2026-02-18 03:27:10 UTC  0.001   2.466      0.219       0.1250  0.0936  0.7813  116.53   120.00   0.1369   0.500       0.134         -0.00135    -1.128e-05    STAY_FLAT   baseline_v1_exec
2026-02-18 03:27:05 UTC  0.001   2.453      0.222       0.1721  0.0500  0.7779  105.16   120.00   0.1816   0.300       0.994         -0.00121    -1.034e-05    STAY_FLAT   baseline_v1_exec
2026-02-18 03:27:00 UTC  0.001   2.441      0.226       0.1272  0.0983  0.7745  116.91   120.00   0.0940   0.200       0.137         -0.00138    -1.157e-05    STAY_FLAT   baseline_v1_exec
2026-02-18 03:26:55 UTC  0.001   2.428      0.229       0.1336  0.0954  0.7711  115.96   120.00   0.1282   0.100       0.155         -0.00136    -1.135e-05    STAY_FLAT   baseline_v1_exec
2026-02-18 03:26:50 UTC  0.001   2.416      0.233       0.1200  0.1125  0.7675  119.22   120.00  -0.0103   0.100       0.090         -0.00142    -1.182e-05    STAY_FLAT   baseline_v1_exec
2026-02-18 03:26:45 UTC  0.001   2.403      0.236       0.0843  0.1519  0.7639  120.00   112.93   0.0153   0.100      -0.602         -0.00145    -1.223e-05    STAY_FLAT   baseline_v1_exec
2026-02-18 03:26:40 UTC  0.001   2.390      0.240       0.0879  0.1519  0.7602  120.00   113.43   0.0609   0.100      -0.629         -0.00138    -1.162e-05    STAY_FLAT   baseline_v1_exec
2026-02-18 03:26:35 UTC  0.001   2.377      0.244       0.1023  0.1412  0.7566  120.00   116.13   0.1759   0.100      -0.576         -0.00136    -1.137e-05    STAY_FLAT   baseline_v1_exec
2026-02-18 03:26:30 UTC  0.001   2.364      0.247       0.1026  0.1447  0.7528  120.00   115.88   0.1614   0.100      -0.576         -0.00134    -1.121e-05    STAY_FLAT   baseline_v1_exec
2026-02-18 03:26:25 UTC  0.001   2.351      0.251       0.0895  0.1616  0.7490  120.00   112.90   0.0264   0.300      -0.601         -0.00142    -1.194e-05    STAY_FLAT   baseline_v1_exec
2026-02-18 03:26:20 UTC  0.001   2.338      0.255       0.0837  0.1713  0.7450  120.00   111.41   0.0645   0.400      -0.773         -0.00143    -1.207e-05    STAY_FLAT   baseline_v1_exec
2026-02-18 03:26:15 UTC  0.001   2.325      0.259       0.0895  0.1694  0.7411  120.00   112.34   0.1023   0.400      -0.751         -0.00141    -1.187e-05    STAY_FLAT   baseline_v1_exec
2026-02-18 03:26:10 UTC  0.001   2.312      0.263       0.0767  0.1862  0.7371  120.00   109.36  -0.0666   0.400      -0.747         -0.00144    -1.219e-05    STAY_FLAT   baseline_v1_exec
2026-02-18 03:26:05 UTC  0.001   2.301      0.266       0.0748  0.1914  0.7339  120.00   108.72  -0.0894   0.300      -0.776         -0.00143    -1.213e-05    STAY_FLAT   baseline_v1_exec
```

## 4. p_none 범위 (barrier OK 구간)

| 지표 | 값 |
|---|---|
| min | 0.52 |
| median | 0.73 |
| max | 0.80 |

(전체 포함: WARMUP 구간에서는 p_none=0.99)

## 5. ENTER_LONG 0건 — 원인 분석

ENTER_LONG이 0건인 이유: **p_none이 대부분 0.70 이상(ENTER_PNONE_MAX=0.70 초과)** + 보수적 임계값 조합.
- z_barrier가 1.7~2.5 범위로 높아서 `p_hit_base = exp(-0.25*z²)` 가 0.20~0.47로 작아 p_none이 0.53~0.80으로 높게 형성됨
- 또한 ev_rate가 모두 음수(-1.0e-05 ~ -1.5e-05)이므로 ENTER_EV_RATE_TH=0.0 조건도 미충족
- 이는 현재 r_t=0.001(1bps) 대비 비용(fee+spread+slip ≈ 1.4bps)이 커서 EV 자체가 음수인 정상 상태

## DoD 체크리스트

- [x] predictions에 v1 신규 컬럼 채워짐 (z_barrier, p_hit_base, ev_rate, r_none_pred 등)
- [x] p_up+p_down+p_none ≈ 1.0 (sum_min=1.0, sum_max≈1.0)
- [x] ev_rate 계산/저장 완료
- [x] action_hat 저장 (STAY_FLAT 107건)
- [x] model_version = "baseline_v1_exec" (107건)
- [x] bot 크래시 없이 5분+ 지속 실행 완료
