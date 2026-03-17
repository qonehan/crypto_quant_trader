 구현 완료 요약

  생성된 파일

  ┌───────────────────────────────────┬───────────────────────────────────┐
  │               파일                │               역할                │
  ├───────────────────────────────────┼───────────────────────────────────┤
  │ app/rl/crypto_env.py              │ Gymnasium CryptoTradingEnv 클래스 │
  ├───────────────────────────────────┼───────────────────────────────────┤
  │ scripts/rl/step_rl_1_train_ppo.py │ 전체 PPO 학습 파이프라인          │
  ├───────────────────────────────────┼───────────────────────────────────┤
  │ artifacts/rl_training_report.md   │ 자동 생성 결과 보고서             │
  └───────────────────────────────────┴───────────────────────────────────┘

  ---
  핵심 설계 포인트

  [환경] CryptoTradingEnv
  - 오라클 추론을 외부에서 사전 계산 → df에 xgb_prob, ridge_pred 열로 주입, 학습 루프에서 추론 없음
  - 관측: 10 × 69 + 2 = 692 dim (69 = 67 스케일링 피처 + xgb_prob + ridge_pred)
  - NumPy 배열 캐싱으로 환경 스텝 속도 최적화
  - 보상 수식:
  r_t = pos × log(P_{t+1}/P_t) × 10000  (bps 단위)
       - |Δpos| × 0.0005 × 10000          (수수료)
       - 0.001 × Δpos²                    (휩소 이차 패널티)

  [학습] step_rl_1_train_ppo.py
  - 데이터 누수 방지: scaler/XGB/Ridge 모두 Train 80% 구간에만 fit
  - VecNormalize norm_reward=False (bps 단위 보상은 이미 해석 가능)
  - 평가 시 VecNormalize auto-reset에 의한 trajectory 유실 방지: raw env 직접 구동 + obs 수동 정규화
  - ValRewardCallback으로 학습 중 검증 성능 주기적 TensorBoard 기록

  [실행 명령]
  # 스모크 테스트 (20K 데이터, 50K 스텝, ~1분)
  poetry run python scripts/rl/step_rl_1_train_ppo.py --smoke

  # 정식 학습 (전체 1M 행, 5M 스텝, ~수시간)
  poetry run python scripts/rl/step_rl_1_train_ppo.py

  # 학습 스텝 조정
  poetry run python scripts/rl/step_rl_1_train_ppo.py --timesteps 10000000

  # TensorBoard 모니터링
  tensorboard --logdir artifacts/rl_prod/tensorboard