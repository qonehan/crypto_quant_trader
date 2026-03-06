# STEP 5.3 결과 보고서 — Evaluator exec_v1 라벨 (ask entry + bid touch + DOWN 우선)

## 목표
기존 mid 기반 정산을 폐기하고, 실행가 정합 라벨(exec_v1)로 정산한다.
- entry = ask_close_1s(t0) * (1+slippage)
- 터치 = bid_high_1s / bid_low_1s * (1-slippage) 기준
- 양방 터치 → DOWN 우선(보수적)
- brier/logloss 확률평가 저장

---

## 추가/수정 파일 목록

| 파일 | 변경 유형 | 설명 |
|------|-----------|------|
| `app/evaluator/evaluator.py` | **전면 재작성** | exec_v1 라벨 로직, brier/logloss, DOWN 우선, 집계 필터 |
| `app/db/writer.py` | **수정** | `upsert_evaluation_result` SQL에 exec_v1 컬럼 8개 추가 |

---

## 핵심 변경 사항

### exec_v1 정산 로직 (evaluator.py)

1. **Entry row 조회**: `ts <= t0 ORDER BY ts DESC LIMIT 1` (bar-end 기준 최신)
   - 5초 초과 스탈 시 스킵

2. **Entry price**: `ask_close_1s * (1 + SLIPPAGE_BPS/10000)`

3. **Barrier**: `u_exec = entry * (1+r_t)`, `d_exec = entry * (1-r_t)`

4. **Touch 판정** (ts > t0, ts <= t0+H):
   - `executable_bid_high = bid_high_1s * (1 - slip_rate)`
   - `executable_bid_low = bid_low_1s * (1 - slip_rate)`
   - 양방 터치 → `ambig_touch=True`, `actual_direction=DOWN` (보수적)
   - `touch_time_sec = max(0, (row.ts - t0).total_seconds() - 0.5)`

5. **NONE 시**: `r_h = (exit_bid*(1-slip) - entry) / entry`

6. **Brier**: `(p_up-y_up)² + (p_down-y_down)² + (p_none-y_none)²`
   **Logloss**: `-log(p_actual + eps)`

7. **집계 필터**: `WHERE label_version='exec_v1'`

### writer.py
- INSERT/ON CONFLICT UPDATE에 8개 신규 컬럼 추가:
  `label_version, entry_price, u_exec, d_exec, ambig_touch, r_h, brier, logloss`

---

## 봇 로그 (정산 라인 발췌)

```
11:48:38 [INFO] __main__: DB migrations applied
11:48:38 [INFO] app.evaluator.evaluator: Evaluator: waiting 125s for first horizon to expire...
11:48:39 [INFO] app.marketdata.upbit_ws: WS connected to wss://api.upbit.com/websocket/v1
...
11:50:44 [INFO] app.evaluator.evaluator: Eval(exec_v1): settled=50 total=50 acc=0.580 hit=0.420 ambig=0 brier=0.5019 logloss=3.5008
11:50:49 [INFO] app.evaluator.evaluator: Eval(exec_v1): settled=28 total=78 acc=0.590 hit=0.410 ambig=0 brier=0.6299 logloss=5.3461
11:50:54 [INFO] app.evaluator.evaluator: Eval(exec_v1): settled=1 total=79 acc=0.595 hit=0.405 ambig=0 brier=0.6221 logloss=5.2796
11:51:04 [INFO] app.evaluator.evaluator: Eval(exec_v1): settled=1 total=81 acc=0.605 hit=0.395 ambig=0 brier=0.6102 logloss=5.1582
11:51:29 [INFO] app.evaluator.evaluator: Eval(exec_v1): settled=1 total=86 acc=0.628 hit=0.372 ambig=0 brier=0.5777 logloss=4.8685
11:52:04 [INFO] app.evaluator.evaluator: Eval(exec_v1): settled=1 total=93 acc=0.656 hit=0.344 ambig=0 brier=0.5634 logloss=4.5534
11:52:39 [INFO] app.evaluator.evaluator: Eval(exec_v1): settled=1 total=100 acc=0.680 hit=0.320 ambig=0 brier=0.5741 logloss=4.3232
11:53:04 [INFO] app.evaluator.evaluator: Eval(exec_v1): settled=1 total=100 acc=0.690 hit=0.310 ambig=0 brier=0.5772 logloss=4.0756
```

---

## 최근 20행 출력 전문

```
t0                        | label   | dir  | ambig | entry_price   | u_exec        | d_exec        | touch_sec | r_h         | p_up   | p_down | p_none | brier  | logloss
2026-02-17 11:51:00 UTC   | exec_v1 | NONE | False | 100507097.4   | 100607604.5   | 100406590.3   | None      | -7.17e-05   | 0.2126 | 0.3873 | 0.4001 | 0.5550 | 0.9159
2026-02-17 11:50:55 UTC   | exec_v1 | NONE | False | 100507097.4   | 100607604.5   | 100406590.3   | None      | -5.18e-05   | 0.2012 | 0.3485 | 0.4503 | 0.4641 | 0.7978
2026-02-17 11:50:50 UTC   | exec_v1 | NONE | False | 100507097.4   | 100607604.5   | 100406590.3   | None      | -7.17e-05   | 0.1935 | 0.3249 | 0.4817 | 0.4116 | 0.7305
2026-02-17 11:50:45 UTC   | exec_v1 | NONE | False | 100507097.4   | 100607604.5   | 100406590.3   | None      | -7.17e-05   | 0.2143 | 0.3935 | 0.3922 | 0.5701 | 0.9359
2026-02-17 11:50:40 UTC   | exec_v1 | NONE | False | 100507097.4   | 100607604.5   | 100406590.3   | None      | -2.71e-04   | 0.2125 | 0.3870 | 0.4006 | 0.5542 | 0.9149
2026-02-17 11:50:35 UTC   | exec_v1 | NONE | False | 100490094.0   | 100590584.1   | 100389603.9   | None      | -1.01e-04   | 0.2348 | 0.4799 | 0.2852 | 0.7964 | 1.2545
2026-02-17 11:50:30 UTC   | exec_v1 | NONE | False | 100510098.0   | 100610608.1   | 100409587.9   | None      | -3.00e-04   | 0.1943 | 0.3275 | 0.4782 | 0.4173 | 0.7377
2026-02-17 11:50:25 UTC   | exec_v1 | NONE | False | 100510098.0   | 100610608.1   | 100409587.9   | None      | -3.00e-04   | 0.1911 | 0.3178 | 0.4912 | 0.3964 | 0.7110
2026-02-17 11:50:20 UTC   | exec_v1 | NONE | False | 100510098.0   | 100610608.1   | 100409587.9   | None      | -3.00e-04   | 0.2625 | 0.6613 | 0.0762 | 1.3595 | 2.5741
2026-02-17 11:50:15 UTC   | exec_v1 | NONE | False | 100493094.6   | 100593587.7   | 100392601.5   | None      | -4.30e-04   | 0.2601 | 0.6390 | 0.1009 | 1.2843 | 2.2935
2026-02-17 11:50:10 UTC   | exec_v1 | NONE | False | 100510098.0   | 100610608.1   | 100409587.9   | None      | -5.99e-04   | 0.2303 | 0.4586 | 0.3111 | 0.7380 | 1.1678
2026-02-17 11:50:05 UTC   | exec_v1 | NONE | False | 100510098.0   | 100610608.1   | 100409587.9   | None      | -5.99e-04   | 0.0518 | 0.0578 | 0.8905 | 0.0180 | 0.1160
2026-02-17 11:50:00 UTC   | exec_v1 | NONE | False | 100510098.0   | 100610608.1   | 100409587.9   | None      | -3.00e-04   | 0.4237 | 0.2221 | 0.3542 | 0.6459 | 1.0378
2026-02-17 11:49:55 UTC   | exec_v1 | NONE | False | 100510098.0   | 100610608.1   | 100409587.9   | None      | -8.16e-05   | 0.4090 | 0.2184 | 0.3726 | 0.6086 | 0.9872
2026-02-17 11:49:50 UTC   | exec_v1 | NONE | False | 100489093.8   | 100589582.9   | 100388604.7   | None      | -9.15e-05   | 0.2076 | 0.1458 | 0.6466 | 0.1893 | 0.4361
2026-02-17 11:49:45 UTC   | exec_v1 | NONE | False | 100489093.8   | 100589582.9   | 100388604.7   | None      | -9.15e-05   | 0.4935 | 0.2376 | 0.2690 | 0.8343 | 1.3131
2026-02-17 11:49:40 UTC   | exec_v1 | NONE | False | 100464088.8   | 100564552.9   | 100363624.7   | None      | 1.57e-04    | 0.0930 | 0.0784 | 0.8286 | 0.0442 | 0.1881
2026-02-17 11:49:35 UTC   | exec_v1 | NONE | False | 100462088.4   | 100562550.5   | 100361626.3   | None      | 1.77e-04    | 0.0606 | 0.0540 | 0.8854 | 0.0197 | 0.1217
2026-02-17 11:49:30 UTC   | exec_v1 | NONE | False | 100464088.8   | 100564552.9   | 100363624.7   | None      | 1.57e-04    | 0.3076 | 0.1875 | 0.5049 | 0.3750 | 0.6835
2026-02-17 11:49:25 UTC   | exec_v1 | NONE | False | 100464088.8   | 100564552.9   | 100363624.7   | None      | 1.57e-04    | 0.1338 | 0.1838 | 0.6824 | 0.1526 | 0.3822
```

---

## Direction 분포 및 Sanity Check

| 항목 | 값 |
|------|-----|
| 총 평가 건수 | 105 |
| UP | 0 |
| DOWN | 32 |
| NONE | 73 |
| ambig_touch 발생 | 0건 (양방 터치 없음 — r_t=0.1%로 작아 양방 동시 터치가 1초 내 발생하기 어려움) |

| Sanity | 통과 |
|--------|------|
| label_version='exec_v1' | 105/105 (100%) |
| entry_price NOT NULL | 105/105 (100%) |
| u_exec NOT NULL | 105/105 (100%) |
| d_exec NOT NULL | 105/105 (100%) |
| brier NOT NULL | 105/105 (100%) |
| logloss NOT NULL | 105/105 (100%) |
| NONE + r_h NOT NULL | 73/73 (100%) |
| predictions SETTLED | 105건 (PENDING 25건은 아직 horizon 미도래) |

---

## DOWN 정산 예시 (touch 확인)

```
t0=11:47:40 | dir=DOWN | ambig=False | entry=100,492,094 | touch_sec=68.5 | r_h=None | brier=0.431 | logloss=0.765
t0=11:47:35 | dir=DOWN | ambig=False | entry=100,492,094 | touch_sec=73.5 | r_h=None | brier=0.557 | logloss=0.920
t0=11:47:30 | dir=DOWN | ambig=False | entry=100,492,094 | touch_sec=78.5 | r_h=None | brier=0.550 | logloss=0.911
t0=11:47:25 | dir=DOWN | ambig=False | entry=100,492,094 | touch_sec=83.5 | r_h=None | brier=0.658 | logloss=1.042
t0=11:47:20 | dir=DOWN | ambig=False | entry=100,487,093 | touch_sec=88.5 | r_h=None | brier=0.146 | logloss=0.317
```

- DOWN 건의 touch_time_sec: 68.5~88.5초 (horizon 중간에 bid_low가 d_exec 이하로 터치)
- DOWN 건의 r_h: None (정책대로 UP/DOWN 시 r_h 미저장)

---

## DoD 체크리스트

- [x] evaluation_results 최신 행에 `label_version="exec_v1"` 생성됨
- [x] entry_price/u_exec/d_exec가 NULL이 아님
- [x] 터치 판정이 bid_high/bid_low 기반으로 동작 (DOWN 32건 확인)
- [x] 양방 터치 시 DOWN 우선 처리 로직 구현됨 (이번 세션에서는 0건 — r_t 크기상 정상)
- [x] NONE에서 r_h 기록됨 (73건 모두 NOT NULL)
- [x] brier/logloss 저장됨 (105건 모두 NOT NULL)
- [x] predictions.status가 SETTLED로 전환됨 (105건)
