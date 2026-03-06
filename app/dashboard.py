import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import json

import altair as alt
import streamlit as st
import pandas as pd
from datetime import datetime, timezone
from sqlalchemy import text

from app.config import load_settings
from app.db.session import get_engine
from app.evaluator.evaluator import compute_calibration

DB_RESOLVE_HINT = (
    "DB host 'db'를 찾지 못했습니다. "
    "Codespaces에서 Dev Containers: Rebuild and Reopen in Container를 실행해 "
    "docker-compose devcontainer로 들어가 있는지 확인하세요. "
    "또한 db 컨테이너가 정상 실행 중인지 확인하세요."
)

# ── 색상 박스 헬퍼 ─────────────────────────────────────────────────────────────

def _action_badge(action: str) -> str:
    """action_hat 값을 색상 배지 HTML로 변환."""
    palette = {
        "ENTER_LONG": ("🟢", "#1a7f37", "매수 신호 (LONG)"),
        "STAY_FLAT":  ("⚪", "#555555", "관망 중 (WAIT)"),
        "EXIT_LONG":  ("🔴", "#b91c1c", "매도 신호 (EXIT)"),
    }
    icon, color, label = palette.get(action, ("❓", "#888", action))
    return (
        f'<div style="background:{color};border-radius:12px;padding:14px 24px;'
        f'display:inline-block;color:#fff;font-size:1.5rem;font-weight:700;">'
        f'{icon}&nbsp;&nbsp;{label}</div>'
    )


def _trend_arrow(now_mid: float, prev_mid: float) -> str:
    if prev_mid <= 0:
        return "➡️"
    return "📈" if now_mid >= prev_mid else "📉"


# ══════════════════════════════════════════════════════════════════════════════
# Tab 1 — 직관적인 요약 (비전문가용)
# ══════════════════════════════════════════════════════════════════════════════

def render_tab1(engine, settings, now_utc: datetime) -> None:
    st.markdown("### 지금 AI 봇은 무엇을 하고 있나요?")

    # ── 1. 현재 시장 가격 ─────────────────────────────────────────────────────
    try:
        with engine.connect() as conn:
            mkt_df = pd.read_sql_query(
                text("SELECT ts, mid FROM market_1s ORDER BY ts DESC LIMIT 2"),
                conn,
            )
    except Exception:
        mkt_df = pd.DataFrame()

    col_price, col_lag, col_model = st.columns(3)

    if not mkt_df.empty:
        now_mid  = float(mkt_df["mid"].iloc[0]) if pd.notna(mkt_df["mid"].iloc[0]) else 0.0
        prev_mid = float(mkt_df["mid"].iloc[1]) if len(mkt_df) > 1 and pd.notna(mkt_df["mid"].iloc[1]) else 0.0
        last_ts  = pd.to_datetime(mkt_df["ts"].iloc[0], utc=True)
        lag_sec  = (now_utc - last_ts).total_seconds()
        trend    = _trend_arrow(now_mid, prev_mid)
        col_price.metric(
            f"{trend} 현재 BTC 가격",
            f"₩{now_mid:,.0f}",
            help="업비트 호가 중간가(매수·매도 호가 평균)입니다.",
        )
        col_lag.metric(
            "데이터 지연",
            f"{lag_sec:.1f}초",
            help="마지막으로 시장 데이터를 받은 시각으로부터 경과된 시간입니다. 5초 이하면 정상입니다.",
        )
    else:
        col_price.info("시장 데이터 없음 — 봇을 먼저 실행해 주세요.")

    # ── 2. 현재 AI 모델 정보 ──────────────────────────────────────────────────
    try:
        with engine.connect() as conn:
            pred_latest = pd.read_sql_query(
                text(
                    "SELECT t0, p_up, p_down, p_none, ev, ev_rate, "
                    "action_hat, model_version, mom_z, spread_bps "
                    "FROM predictions WHERE symbol = :sym ORDER BY t0 DESC LIMIT 1"
                ),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception:
        pred_latest = pd.DataFrame()

    if not pred_latest.empty:
        pr = pred_latest.iloc[0]
        mv = str(pr.get("model_version") or "N/A")
        col_model.metric(
            "AI 모델",
            mv,
            help="현재 판단에 사용 중인 AI 모델 이름입니다. ridge_h3600_v1 = 1시간 호흡 Ridge 회귀 모델 (sign_acc 82%).",
        )
    else:
        col_model.info("예측 데이터 없음")

    st.divider()

    # ── 3. AI의 현재 판단 ─────────────────────────────────────────────────────
    st.markdown("#### AI의 현재 판단")

    if not pred_latest.empty:
        pr = pred_latest.iloc[0]
        action = str(pr.get("action_hat") or "STAY_FLAT")
        st.markdown(_action_badge(action), unsafe_allow_html=True)
        st.caption(
            "AI는 매 5초마다 시장을 분석하여 매수·관망·매도 중 하나를 결정합니다. "
            "판단 기준은 기댓값(EV)과 수수료를 비교한 결과입니다."
        )

        st.markdown("")
        c1, c2, c3, c4 = st.columns(4)
        p_up   = float(pr["p_up"])   if pd.notna(pr.get("p_up"))   else 0.0
        p_down = float(pr["p_down"]) if pd.notna(pr.get("p_down")) else 0.0
        p_none = float(pr["p_none"]) if pd.notna(pr.get("p_none")) else 1.0
        ev     = float(pr["ev"])     if pd.notna(pr.get("ev"))     else 0.0

        c1.metric(
            "상승 확률",
            f"{p_up:.1%}",
            help="AI가 생각하는 2분 뒤 가격이 오를 확률입니다.",
        )
        c2.metric(
            "하락 확률",
            f"{p_down:.1%}",
            help="AI가 생각하는 2분 뒤 가격이 내릴 확률입니다.",
        )
        c3.metric(
            "관망 확률",
            f"{p_none:.1%}",
            help="배리어(목표 수익 구간)에 도달하지 못하고 그냥 끝날 확률입니다. 높을수록 AI가 신호를 보내지 않습니다.",
        )
        c4.metric(
            "기댓값 (EV)",
            f"{ev:.6f}",
            help="이번 거래에 진입했을 때 예상되는 평균 수익률입니다. 수수료를 빼고도 이득일 때만 거래합니다.",
        )
    else:
        st.info("아직 AI 예측 결과가 없습니다. 봇을 실행하면 자동으로 표시됩니다.")

    st.divider()

    # ── 4. 모의투자 성과 요약 ─────────────────────────────────────────────────
    st.markdown("#### 모의투자 성과 요약")

    # 포지션 현황
    try:
        with engine.connect() as conn:
            pp_df = pd.read_sql_query(
                text(
                    "SELECT status, cash_krw, qty, entry_price, initial_krw, "
                    "equity_high, halted, halt_reason "
                    "FROM paper_positions WHERE symbol = :sym"
                ),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception:
        pp_df = pd.DataFrame()

    # 거래 통계
    try:
        with engine.connect() as conn:
            exit_stats = pd.read_sql_query(
                text("""
                    SELECT count(*) as trades,
                           avg(case when pnl_krw > 0 then 1.0 else 0.0 end) as win_rate,
                           sum(pnl_krw) as total_pnl_krw,
                           avg(pnl_rate) as avg_pnl_rate
                    FROM (
                        SELECT * FROM paper_trades
                        WHERE symbol = :sym AND action = 'EXIT_LONG'
                        ORDER BY t DESC LIMIT 200
                    ) sub
                """),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception:
        exit_stats = pd.DataFrame()

    if not pp_df.empty:
        pp = pp_df.iloc[0]
        initial_krw = float(pp.get("initial_krw") or 1_000_000)
        cash_krw    = float(pp.get("cash_krw") or 0)
        qty         = float(pp.get("qty") or 0)
        cur_mid     = now_mid if not mkt_df.empty else 0.0
        equity_est  = cash_krw + qty * cur_mid
        pnl_total   = equity_est - initial_krw
        pnl_pct     = pnl_total / initial_krw if initial_krw > 0 else 0.0

        pa, pb, pc, pd_ = st.columns(4)
        pa.metric(
            "현재 자산 (추정)",
            f"₩{equity_est:,.0f}",
            delta=f"{'+'if pnl_total>=0 else ''}{pnl_total:,.0f}원 ({pnl_pct:+.2%})",
            help="현금 + 보유 BTC를 현재 시세로 환산한 추정 총 자산입니다.",
        )
        pb.metric(
            "보유 포지션",
            pp["status"],
            help="FLAT = 현금만 보유(관망 중), LONG = BTC 매수 중.",
        )
        if not exit_stats.empty and exit_stats.iloc[0]["trades"] > 0:
            es = exit_stats.iloc[0]
            pc.metric(
                "승률",
                f"{es['win_rate']:.1%}",
                help="AI가 방향을 정확히 맞혀 수익을 낸 거래의 비율입니다.",
            )
            pd_.metric(
                "총 거래 횟수",
                f"{int(es['trades'])}회",
                help="AI가 매수 후 매도까지 완료한 거래 횟수입니다.",
            )
        else:
            pc.info("거래 기록 없음")

        if pp.get("halted"):
            st.warning(f"⚠️ 거래 일시 정지 중: {pp.get('halt_reason', '사유 불명')}")
    else:
        st.info("모의투자 포지션 데이터가 없습니다.")

    st.divider()

    # ── 5. 최근 거래 내역 (간략) ──────────────────────────────────────────────
    st.markdown("#### 최근 거래 내역")
    try:
        with engine.connect() as conn:
            pt_simple = pd.read_sql_query(
                text(
                    "SELECT t, action, price, pnl_krw, pnl_rate, hold_sec "
                    "FROM paper_trades WHERE symbol = :sym ORDER BY t DESC LIMIT 10"
                ),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception:
        pt_simple = pd.DataFrame()

    if not pt_simple.empty:
        def _fmt_row(row):
            action = row.get("action", "")
            pnl    = row.get("pnl_krw")
            if pnl is None or pd.isna(pnl):
                pnl_str = "-"
            else:
                pnl_str = f"{'+'if pnl>=0 else ''}{pnl:,.0f}원"
            hold = row.get("hold_sec")
            hold_str = f"{hold:.0f}초" if hold and pd.notna(hold) else "-"
            return pd.Series({
                "시각": str(row["t"])[:19],
                "액션": action,
                "가격": f"₩{row['price']:,.0f}" if pd.notna(row.get("price")) else "-",
                "손익": pnl_str,
                "보유 시간": hold_str,
            })

        display_df = pt_simple.apply(_fmt_row, axis=1)
        st.dataframe(display_df, use_container_width=True, height=280)
    else:
        st.info("아직 거래가 없습니다. AI가 매수 신호를 감지하면 자동으로 거래가 시작됩니다.")

    # ── 6. 실시간 Net PnL 및 수익 곡선 ───────────────────────────────────────
    st.markdown("#### 실시간 순수익(Net PnL) 및 자산 변화 (최근 6시간)")
    st.caption(
        "Net PnL = 실현손익 - 업비트 시장가 왕복 수수료(0.1%) - 스프레드 비용. "
        "fee_krw 컬럼에 이미 수수료가 차감되어 있으며 pnl_krw가 순수익입니다."
    )
    # Net PnL 요약 (EXIT_LONG 기준)
    try:
        with engine.connect() as conn:
            net_pnl_df = pd.read_sql_query(
                text("""
                    SELECT sum(pnl_krw) as total_net_pnl,
                           sum(fee_krw) as total_fee,
                           count(*) as n_exits
                    FROM paper_trades
                    WHERE symbol = :sym AND action = 'EXIT_LONG'
                """),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception:
        net_pnl_df = pd.DataFrame()

    if not net_pnl_df.empty and net_pnl_df.iloc[0]["n_exits"] > 0:
        np_ = net_pnl_df.iloc[0]
        na, nb, nc = st.columns(3)
        total_net = float(np_["total_net_pnl"] or 0)
        total_fee = float(np_["total_fee"] or 0)
        na.metric(
            "총 순수익 (Net PnL)",
            f"{'+'if total_net>=0 else ''}{total_net:,.0f}원",
            help="수수료·슬리피지 차감 후 순수익 합계",
        )
        nb.metric(
            "총 납부 수수료",
            f"{total_fee:,.0f}원",
            help="왕복 체결 수수료 합계 (매수+매도 각 0.05%)",
        )
        nc.metric("청산 횟수", f"{int(np_['n_exits'])}회")

    try:
        with engine.connect() as conn:
            eq_df = pd.read_sql_query(
                text("""
                    SELECT ts, equity_est, drawdown_pct
                    FROM paper_decisions
                    WHERE symbol = :sym AND equity_est IS NOT NULL
                      AND ts >= now() - interval '6 hours'
                    ORDER BY ts ASC
                """),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception:
        eq_df = pd.DataFrame()

    if not eq_df.empty:
        eq_chart = eq_df.set_index("ts")
        st.line_chart(eq_chart["equity_est"], use_container_width=True)
        st.caption("자산 곡선: 올라갈수록 수익, 내려갈수록 손실입니다.")
    else:
        st.info("자산 변화 데이터가 아직 없습니다.")


# ══════════════════════════════════════════════════════════════════════════════
# Tab 2 — 세부 계산 데이터 (전문가용)
# ══════════════════════════════════════════════════════════════════════════════

def render_tab2(engine, settings, now_utc: datetime) -> None:

    # ── 가격 흐름 차트 + 진입 마커 ───────────────────────────────────────────
    st.header("가격 흐름 — 최근 5분 (ridge_h3600_v1 진입 타점)")
    try:
        with engine.connect() as conn:
            df300 = pd.read_sql_query(
                text("SELECT ts, mid FROM market_1s ORDER BY ts DESC LIMIT 300"),
                conn,
            )
    except Exception:
        df300 = pd.DataFrame()

    # 최근 5분 paper_trades에서 ENTER_LONG / ENTER_SHORT 이벤트 조회
    try:
        with engine.connect() as conn:
            entry_df = pd.read_sql_query(
                text("""
                    SELECT t AS ts, action, price
                    FROM paper_trades
                    WHERE symbol = :sym
                      AND action IN ('ENTER_LONG', 'ENTER_SHORT')
                      AND t >= now() AT TIME ZONE 'UTC' - interval '5 minutes'
                    ORDER BY t ASC
                """),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception:
        entry_df = pd.DataFrame()

    if not df300.empty:
        price_df = df300.sort_values("ts").copy()
        price_df["ts"] = pd.to_datetime(price_df["ts"], utc=True)

        base_chart = (
            alt.Chart(price_df)
            .mark_line(color="steelblue", strokeWidth=1.5)
            .encode(
                x=alt.X("ts:T", title="시각", axis=alt.Axis(format="%H:%M:%S")),
                y=alt.Y("mid:Q", title="중간가 (KRW)", scale=alt.Scale(zero=False)),
            )
        )

        layers = [base_chart]

        if not entry_df.empty:
            entry_df["ts"] = pd.to_datetime(entry_df["ts"], utc=True)
            entry_df["price"] = entry_df["price"].astype(float)

            long_df = entry_df[entry_df["action"] == "ENTER_LONG"]
            short_df = entry_df[entry_df["action"] == "ENTER_SHORT"]

            if not long_df.empty:
                layers.append(
                    alt.Chart(long_df)
                    .mark_point(shape="triangle-up", size=120, color="#1a7f37", filled=True)
                    .encode(
                        x="ts:T",
                        y=alt.Y("price:Q"),
                        tooltip=[
                            alt.Tooltip("ts:T", title="진입 시각", format="%H:%M:%S"),
                            alt.Tooltip("action:N", title="액션"),
                            alt.Tooltip("price:Q", title="진입가", format=",.0f"),
                        ],
                    )
                )
            if not short_df.empty:
                layers.append(
                    alt.Chart(short_df)
                    .mark_point(shape="triangle-down", size=120, color="#b91c1c", filled=True)
                    .encode(
                        x="ts:T",
                        y=alt.Y("price:Q"),
                        tooltip=[
                            alt.Tooltip("ts:T", title="진입 시각", format="%H:%M:%S"),
                            alt.Tooltip("action:N", title="액션"),
                            alt.Tooltip("price:Q", title="진입가", format=",.0f"),
                        ],
                    )
                )

        combined = alt.layer(*layers).properties(height=300)
        st.altair_chart(combined, use_container_width=True)
        if entry_df.empty:
            st.caption("초록 ▲ = ENTER_LONG 타점  |  빨강 ▼ = ENTER_SHORT 타점  (최근 5분간 진입 없음)")
        else:
            st.caption("초록 ▲ = ENTER_LONG 타점  |  빨강 ▼ = ENTER_SHORT 타점")
    else:
        st.info("시장 데이터 없음")

    # ── [A] 배리어 피드백 ─────────────────────────────────────────────────────
    st.header("[A] 배리어 피드백 (Barrier Feedback)")
    st.caption(
        "배리어(r_t)는 AI가 '가격이 얼마나 움직여야 거래할 만한가'를 결정하는 동적 기준입니다. "
        "변동성이 클수록 자동으로 높아져, 노이즈에 의한 오진입을 막습니다."
    )

    try:
        with engine.connect() as conn:
            bp_df = pd.read_sql_query(
                text(
                    "SELECT symbol, k_vol_eff, none_ewma, target_none, "
                    "ewma_alpha, ewma_eta, updated_at "
                    "FROM barrier_params WHERE symbol = :sym"
                ),
                conn,
                params={"sym": settings.SYMBOL},
            )
            bs_latest = pd.read_sql_query(
                text(
                    "SELECT ts, symbol, r_t, sigma_1s, sigma_h, status, sample_n, "
                    "h_sec, vol_window_sec, r_min, k_vol, k_vol_eff, none_ewma, "
                    "r_min_eff, cost_roundtrip_est, spread_bps_med "
                    "FROM barrier_state WHERE symbol = :sym ORDER BY ts DESC LIMIT 1"
                ),
                conn,
                params={"sym": settings.SYMBOL},
            )
            bs_chart = pd.read_sql_query(
                text(
                    "SELECT ts, r_t, sigma_h, k_vol_eff, none_ewma, "
                    "r_min_eff, cost_roundtrip_est, spread_bps_med "
                    "FROM barrier_state WHERE symbol = :sym "
                    "ORDER BY ts DESC LIMIT 720"
                ),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"barrier 데이터 없음: {e}")
        bp_df = bs_latest = bs_chart = pd.DataFrame()

    if not bp_df.empty:
        bp = bp_df.iloc[0]
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("k_vol_eff", f"{bp['k_vol_eff']:.4f}",
                  help="배리어 크기를 결정하는 변동성 배율. AI가 자동으로 조절합니다.")
        c2.metric("none_ewma", f"{bp['none_ewma']:.4f}",
                  help="최근 '관망' 비율의 지수평균. target_none에 가까울수록 배리어가 안정적입니다.")
        c3.metric("target_none", f"{bp['target_none']:.2f}",
                  help="AI가 목표로 하는 관망 비율입니다. (기본 0.55 = 55%)")
        c4.metric("ewma_alpha", f"{bp['ewma_alpha']:.2f}",
                  help="과거 기억 강도. 1에 가까울수록 과거 데이터를 오래 기억합니다.")
        c5.metric("ewma_eta", f"{bp['ewma_eta']:.2f}",
                  help="배리어 조정 속도. 클수록 빠르게 반응합니다.")
        st.caption(f"Updated at: {bp['updated_at']}")

    if not bs_latest.empty:
        row = bs_latest.iloc[0]
        bc1, bc2, bc3, bc4, bc5, bc6 = st.columns(6)
        bc1.metric("r_t (배리어)", f"{row['r_t']:.6f}",
                   help="현재 거래 진입을 위한 최소 기대 수익률 기준입니다.")
        bc2.metric("sigma_h", f"{row['sigma_h']:.8f}" if pd.notna(row["sigma_h"]) else "N/A",
                   help="horizon 시간(H_SEC) 동안의 예상 가격 표준편차입니다.")
        bc3.metric("Status", row["status"],
                   help="OK = 정상 운영 / WARMUP = 데이터 수집 중 / ERROR = 오류")
        bc4.metric("sample_n", int(row["sample_n"]) if pd.notna(row["sample_n"]) else 0,
                   help="변동성 계산에 사용된 샘플 수입니다.")
        bc5.metric("r_min_eff", f"{row['r_min_eff']:.6f}" if pd.notna(row.get("r_min_eff")) else "N/A",
                   help="수수료를 고려한 최소 배리어 하한선입니다.")
        bc6.metric("cost_roundtrip", f"{row['cost_roundtrip_est']:.6f}" if pd.notna(row.get("cost_roundtrip_est")) else "N/A",
                   help="왕복 수수료(진입+청산) 추정값입니다.")

    if not bs_chart.empty:
        bsc = bs_chart.sort_values("ts").set_index("ts")
        st.subheader("r_t vs r_min_eff vs cost_roundtrip — 시계열")
        cost_cols = ["r_t", "r_min_eff", "cost_roundtrip_est"]
        cost_data = bsc[cost_cols].dropna(how="all")
        if not cost_data.empty:
            st.line_chart(cost_data)

        st.subheader("배리어 세부 지표 선택")
        chart_sel = st.selectbox(
            "표시할 지표",
            ["r_t", "k_vol_eff", "none_ewma", "sigma_h", "spread_bps_med"],
        )
        col_data = bsc[chart_sel].dropna() if chart_sel in bsc.columns else pd.Series(dtype=float)
        if not col_data.empty:
            st.line_chart(col_data)

    # ── [B] AI 확률 지표 ──────────────────────────────────────────────────────
    st.header("[B] AI 확률 지표 (Probabilistic Metrics)")

    eval_n = settings.EVAL_WINDOW_N
    try:
        with engine.connect() as conn:
            eval_agg = pd.read_sql_query(
                text("""
                    SELECT count(*) as n,
                           avg(brier) as mean_brier,
                           avg(logloss) as mean_logloss,
                           avg(case when actual_direction='NONE' then 1 else 0 end) as none_rate,
                           avg(case when actual_direction='UP' then 1 else 0 end) as up_rate,
                           avg(case when actual_direction='DOWN' then 1 else 0 end) as down_rate,
                           avg(case when direction_hat = actual_direction then 1 else 0 end) as accuracy,
                           avg(case when touch_time_sec is not null then 1 else 0 end) as hit_rate
                    FROM (
                        SELECT * FROM evaluation_results
                        WHERE symbol = :sym AND label_version='exec_v1'
                          AND brier IS NOT NULL AND logloss IS NOT NULL
                        ORDER BY t0 DESC LIMIT :n
                    ) sub
                """),
                conn,
                params={"sym": settings.SYMBOL, "n": eval_n},
            )
    except Exception as e:
        st.warning(f"evaluation_results 없음: {e}")
        eval_agg = pd.DataFrame()

    if not eval_agg.empty and eval_agg.iloc[0]["n"] > 0:
        ea = eval_agg.iloc[0]
        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("N", int(ea["n"]),
                  help="평가에 사용된 예측 샘플 수입니다.")
        m2.metric("Accuracy", f"{ea['accuracy']:.3f}",
                  help="AI가 방향(UP/DOWN/NONE)을 정확히 맞춘 비율입니다.")
        m3.metric("Hit Rate", f"{ea['hit_rate']:.3f}",
                  help="배리어(목표 가격)에 실제로 도달한 비율입니다.")
        m4.metric("None Rate", f"{ea['none_rate']:.3f}",
                  help="실제로 배리어에 도달하지 못하고 관망으로 끝난 비율입니다.")
        m5.metric("Mean Brier", f"{ea['mean_brier']:.4f}",
                  help="확률 예측 오차(낮을수록 정확). 0이 완벽, 1이 최악입니다.")
        m6.metric("Mean LogLoss", f"{ea['mean_logloss']:.4f}",
                  help="로그 손실. 확률 보정 품질을 나타냅니다. 낮을수록 좋습니다.")
        st.caption(
            f"실제 분포: UP={ea['up_rate']:.3f}  DOWN={ea['down_rate']:.3f}  NONE={ea['none_rate']:.3f}"
        )
    else:
        st.info("평가 결과 없음 (exec_v1)")

    # ── [C] 캘리브레이션 테이블 ───────────────────────────────────────────────
    st.header("[C] 캘리브레이션 테이블 (Calibration)")
    st.caption("예측 확률 구간별로 실제 발생 비율과 얼마나 일치하는지 확인합니다. ECE가 낮을수록 잘 보정된 모델입니다.")

    try:
        with engine.connect() as conn:
            calib_rows = conn.execute(
                text("""
                    SELECT p_up, p_down, p_none, actual_direction
                    FROM evaluation_results
                    WHERE symbol = :sym AND label_version='exec_v1'
                      AND brier IS NOT NULL AND logloss IS NOT NULL
                    ORDER BY t0 DESC LIMIT :n
                """),
                {"sym": settings.SYMBOL, "n": eval_n},
            ).fetchall()
    except Exception as e:
        st.warning(f"calibration 데이터 없음: {e}")
        calib_rows = []

    if calib_rows:
        for cls in ("UP", "DOWN", "NONE"):
            calib = compute_calibration(calib_rows, cls)
            calib_df = pd.DataFrame(calib)
            total_count = calib_df["count"].sum()
            ece = (
                (calib_df["abs_gap"] * calib_df["count"]).sum() / total_count
                if total_count > 0 else 0.0
            )
            st.subheader(f"Calibration: {cls}  (ECE = {ece:.4f})")
            non_empty = calib_df[calib_df["count"] > 0]
            if not non_empty.empty:
                st.dataframe(non_empty, use_container_width=True)
            else:
                st.info(f"{cls} 샘플 없음")
    else:
        st.info("캘리브레이션 데이터 없음")

    # ── [D] EV/비용 진단 ──────────────────────────────────────────────────────
    st.header("[D] EV / 비용 진단 패널")

    pred_n = settings.DASH_PRED_WINDOW_N
    try:
        with engine.connect() as conn:
            pred_diag = pd.read_sql_query(
                text("""
                    SELECT ev, ev_rate, p_none, spread_bps, action_hat
                    FROM predictions
                    WHERE symbol = :sym AND ev IS NOT NULL
                    ORDER BY t0 DESC LIMIT :n
                """),
                conn,
                params={"sym": settings.SYMBOL, "n": pred_n},
            )
    except Exception as e:
        st.warning(f"predictions 없음: {e}")
        pred_diag = pd.DataFrame()

    if not pred_diag.empty:
        d1, d2, d3, d4 = st.columns(4)
        d1.metric("EV mean", f"{pred_diag['ev'].mean():.8f}",
                  help="최근 N회 예측의 평균 기댓값입니다. 양수이고 비용보다 클수록 거래 기회가 많습니다.")
        d1.metric("EV median", f"{pred_diag['ev'].median():.8f}")
        d2.metric(
            "EV_rate mean",
            f"{pred_diag['ev_rate'].mean():.2e}" if pred_diag["ev_rate"].notna().any() else "N/A",
            help="단위 시간당 기댓값(EV / 예상 보유 시간)입니다.",
        )
        d2.metric("EV_rate median",
                  f"{pred_diag['ev_rate'].median():.2e}" if pred_diag["ev_rate"].notna().any() else "N/A")
        d3.metric("p_none mean", f"{pred_diag['p_none'].mean():.4f}",
                  help="평균 관망 확률. 높을수록 AI가 신호를 잘 내지 않습니다.")
        d3.metric("p_none median", f"{pred_diag['p_none'].median():.4f}")

        spd = pred_diag["spread_bps"].dropna()
        d4.metric(
            "스프레드 (bps) mean",
            f"{spd.mean():.2f}" if not spd.empty else "N/A",
            help="스프레드: 살 때와 팔 때의 가격 차이로, 우리가 내야 하는 숨겨진 수수료입니다.",
        )
        d4.metric("스프레드 (bps) median",
                  f"{spd.median():.2f}" if not spd.empty else "N/A")

        if "action_hat" in pred_diag.columns:
            st.subheader("action_hat 분포")
            st.bar_chart(pred_diag["action_hat"].value_counts())

        st.subheader("수수료 분해 (추정)")
        fee_round    = 2 * settings.FEE_RATE
        slip_round   = 2 * (settings.SLIPPAGE_BPS / 10000.0)
        spread_median = spd.median() / 10000.0 if not spd.empty else 0.0
        cost_est     = settings.EV_COST_MULT * (fee_round + slip_round + spread_median)
        cc1, cc2, cc3, cc4 = st.columns(4)
        cc1.metric("수수료 (왕복)", f"{fee_round:.6f}")
        cc2.metric("슬리피지 (왕복)", f"{slip_round:.6f}")
        cc3.metric("스프레드 (중앙값)", f"{spread_median:.6f}")
        cc4.metric("왕복 총비용 추정", f"{cost_est:.6f}")
    else:
        st.info("EV/비용 진단 데이터 없음")

    # ── 최근 예측 원본 테이블 ─────────────────────────────────────────────────
    st.header("예측 원본 데이터 — 최근 20건")
    st.caption(
        "AI가 매 5초마다 계산한 원시 피처와 확률값 테이블입니다. "
        "mom_z: 현재 가격 상승/하락 기세 | spread_bps: 스프레드 | "
        "imb_notional_top5: 호가 불균형(사려는/팔려는 물량 차이)"
    )

    try:
        with engine.connect() as conn:
            pred_recent = pd.read_sql_query(
                text(
                    "SELECT t0, r_t, p_up, p_down, p_none, z_barrier, "
                    "ev, ev_rate, mom_z, spread_bps, imb_notional_top5, "
                    "action_hat, model_version, status "
                    "FROM predictions WHERE symbol = :sym ORDER BY t0 DESC LIMIT 20"
                ),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"predictions 테이블 없음: {e}")
        pred_recent = pd.DataFrame()

    if not pred_recent.empty:
        pr = pred_recent.iloc[0]
        pc1, pc2, pc3, pc4 = st.columns(4)
        pc1.metric("action_hat", pr.get("action_hat", "N/A"))
        pc2.metric(
            "EV",
            f"{pr['ev']:.8f}",
            help="이번 거래에 진입했을 때 예상되는 평균 수익률입니다. 수수료를 빼고도 이득일 때만 거래합니다.",
        )
        pc3.metric(
            "mom_z",
            f"{pr['mom_z']:.4f}" if pd.notna(pr.get("mom_z")) else "N/A",
            help="현재 시장의 가격 상승/하락 기세가 얼마나 강한지 나타내는 지표입니다. 양수=상승세, 음수=하락세.",
        )
        pc4.metric(
            "spread_bps",
            f"{pr['spread_bps']:.2f}" if pd.notna(pr.get("spread_bps")) else "N/A",
            help="살 때와 팔 때의 가격 차이(bps 단위)로, 우리가 내야 하는 숨겨진 수수료입니다.",
        )
        st.dataframe(pred_recent, use_container_width=True, height=400)

    # ── 평가 결과 테이블 ──────────────────────────────────────────────────────
    st.header("평가 결과 — 최근 20건")
    try:
        with engine.connect() as conn:
            eval_recent = pd.read_sql_query(
                text(
                    "SELECT t0, r_t, direction_hat, actual_direction, actual_r_t, "
                    "touch_time_sec, brier, logloss, status "
                    "FROM evaluation_results WHERE symbol = :sym ORDER BY t0 DESC LIMIT 20"
                ),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"evaluation_results 없음: {e}")
        eval_recent = pd.DataFrame()

    if not eval_recent.empty:
        st.dataframe(eval_recent, use_container_width=True, height=400)
    else:
        st.info("평가 결과 아직 없음")

    # ── [E] 모의투자 상세 ─────────────────────────────────────────────────────
    st.header("[E] 모의투자 상세 (Paper Trading)")

    try:
        with engine.connect() as conn:
            pp_df = pd.read_sql_query(
                text(
                    "SELECT symbol, status, cash_krw, qty, entry_time, entry_price, "
                    "u_exec, d_exec, h_sec, entry_r_t, entry_ev_rate, entry_p_none, "
                    "initial_krw, equity_high, day_start_date, day_start_equity, "
                    "halted, halt_reason, halted_at, updated_at "
                    "FROM paper_positions WHERE symbol = :sym"
                ),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"paper_positions 없음: {e}")
        pp_df = pd.DataFrame()

    if not pp_df.empty:
        pp = pp_df.iloc[0]
        p1, p2, p3, p4, p5 = st.columns(5)
        p1.metric("Status", pp["status"])
        p1.metric("Cash (KRW)", f"{pp['cash_krw']:,.0f}")
        p2.metric("Qty", f"{pp['qty']:.8f}")
        p2.metric("Entry Price", f"{pp['entry_price']:,.0f}" if pd.notna(pp["entry_price"]) else "N/A")
        p3.metric("equity_high", f"{pp['equity_high']:,.0f}" if pd.notna(pp.get("equity_high")) else "N/A")
        p3.metric("initial_krw", f"{pp['initial_krw']:,.0f}" if pd.notna(pp.get("initial_krw")) else "N/A")
        p4.metric("Halted", str(pp.get("halted") or "false"))
        p4.metric("Halt Reason", pp.get("halt_reason") or "N/A")
        p5.metric("entry_r_t", f"{pp['entry_r_t']:.6f}" if pd.notna(pp["entry_r_t"]) else "N/A")
        p5.metric("Profile", getattr(settings, "PAPER_POLICY_PROFILE", "strict"))
        st.caption(f"Updated at: {pp['updated_at']}")

    # 낙폭 차트
    st.subheader("낙폭(Drawdown) — 최근 6시간")
    try:
        with engine.connect() as conn:
            eq_df = pd.read_sql_query(
                text("""
                    SELECT ts, equity_est, drawdown_pct
                    FROM paper_decisions
                    WHERE symbol = :sym AND equity_est IS NOT NULL
                      AND ts >= now() - interval '6 hours'
                    ORDER BY ts ASC
                """),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"equity 데이터 없음: {e}")
        eq_df = pd.DataFrame()

    if not eq_df.empty:
        eq_chart = eq_df.set_index("ts")
        st.line_chart(eq_chart["drawdown_pct"] * 100)
        st.caption("낙폭(%): 고점 대비 얼마나 빠졌는지 나타냅니다. 작을수록 안정적입니다.")

    # 거래 통계
    st.subheader("거래 통계 (EXIT_LONG, 최근 200건)")
    try:
        with engine.connect() as conn:
            exit_stats = pd.read_sql_query(
                text("""
                    SELECT count(*) as trades,
                           avg(case when pnl_krw > 0 then 1.0 else 0.0 end) as win_rate,
                           avg(pnl_krw) as avg_pnl_krw,
                           avg(pnl_rate) as avg_pnl_rate,
                           avg(hold_sec) as avg_hold_sec,
                           sum(fee_krw) as total_fee_krw
                    FROM (
                        SELECT * FROM paper_trades
                        WHERE symbol = :sym AND action = 'EXIT_LONG'
                        ORDER BY t DESC LIMIT 200
                    ) sub
                """),
                conn,
                params={"sym": settings.SYMBOL},
            )
            exit_reasons = pd.read_sql_query(
                text("""
                    SELECT reason, count(*) as cnt
                    FROM (
                        SELECT reason FROM paper_trades
                        WHERE symbol = :sym AND action = 'EXIT_LONG'
                        ORDER BY t DESC LIMIT 200
                    ) sub
                    GROUP BY reason ORDER BY cnt DESC
                """),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"거래 통계 없음: {e}")
        exit_stats = pd.DataFrame()
        exit_reasons = pd.DataFrame()

    if not exit_stats.empty and exit_stats.iloc[0]["trades"] > 0:
        es = exit_stats.iloc[0]
        s1, s2, s3, s4, s5 = st.columns(5)
        s1.metric("거래 횟수", int(es["trades"]))
        s2.metric("승률", f"{es['win_rate']:.2%}")
        s3.metric("평균 손익 (KRW)", f"{es['avg_pnl_krw']:,.0f}")
        s4.metric("평균 보유(초)", f"{es['avg_hold_sec']:.0f}" if pd.notna(es["avg_hold_sec"]) else "N/A")
        s5.metric("총 수수료 (KRW)", f"{es['total_fee_krw']:,.0f}")
        if not exit_reasons.empty:
            st.caption("청산 사유 분포:")
            st.dataframe(exit_reasons, use_container_width=True)

    # 거래 로그
    st.subheader("거래 로그 — 최근 30건")
    try:
        with engine.connect() as conn:
            pt_df = pd.read_sql_query(
                text(
                    "SELECT t, action, reason, price, qty, fee_krw, cash_after, "
                    "pnl_krw, pnl_rate, hold_sec, model_version "
                    "FROM paper_trades WHERE symbol = :sym ORDER BY t DESC LIMIT 30"
                ),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"paper_trades 없음: {e}")
        pt_df = pd.DataFrame()

    if not pt_df.empty:
        st.dataframe(pt_df, use_container_width=True, height=300)
    else:
        st.info("아직 거래 없음 (비용 > r_t 일 때 정상)")

    # 의사결정 로그
    st.subheader("의사결정 로그 — 최근 60건")
    try:
        with engine.connect() as conn:
            pd_df = pd.read_sql_query(
                text(
                    "SELECT ts, pos_status, action, reason, reason_flags, ev_rate, p_none, "
                    "spread_bps, lag_sec, cost_roundtrip_est, r_t, "
                    "equity_est, drawdown_pct, policy_profile "
                    "FROM paper_decisions WHERE symbol = :sym ORDER BY ts DESC LIMIT 60"
                ),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"paper_decisions 없음: {e}")
        pd_df = pd.DataFrame()

    if not pd_df.empty:
        st.dataframe(pd_df, use_container_width=True, height=400)

    # 관망 사유 분포
    st.subheader("주요 관망 사유 분포 (최근 500건)")
    try:
        with engine.connect() as conn:
            reason_dist = pd.read_sql_query(
                text("""
                    SELECT reason, count(*) as cnt
                    FROM (
                        SELECT reason FROM paper_decisions
                        WHERE symbol = :sym
                        ORDER BY ts DESC LIMIT 500
                    ) sub
                    GROUP BY reason ORDER BY cnt DESC LIMIT 8
                """),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"reason 분포 없음: {e}")
        reason_dist = pd.DataFrame()

    if not reason_dist.empty:
        st.bar_chart(reason_dist.set_index("reason")["cnt"])
        st.dataframe(reason_dist, use_container_width=True)

    # reason_flags 분포
    st.subheader("세부 플래그 분포 (최근 500건)")
    try:
        with engine.connect() as conn:
            flags_raw = conn.execute(
                text("""
                    SELECT reason_flags FROM paper_decisions
                    WHERE symbol = :sym AND reason_flags IS NOT NULL
                    ORDER BY ts DESC LIMIT 500
                """),
                {"sym": settings.SYMBOL},
            ).fetchall()
    except Exception as e:
        st.warning(f"reason_flags 없음: {e}")
        flags_raw = []

    if flags_raw:
        flag_counts: dict[str, int] = {}
        for row in flags_raw:
            try:
                for f in json.loads(row.reason_flags):
                    flag_counts[f] = flag_counts.get(f, 0) + 1
            except (json.JSONDecodeError, TypeError):
                pass
        if flag_counts:
            fc_df = pd.DataFrame(
                sorted(flag_counts.items(), key=lambda x: -x[1]),
                columns=["flag", "count"],
            )
            st.bar_chart(fc_df.set_index("flag")["count"])
            st.dataframe(fc_df, use_container_width=True)

    # ── [F] Upbit 거래소 ──────────────────────────────────────────────────────
    st.header("[F] Upbit 거래소 연동")

    live_guard = (
        settings.LIVE_TRADING_ENABLED
        and settings.UPBIT_TRADE_MODE == "live"
        and settings.LIVE_GUARD_PHRASE == "I_CONFIRM_LIVE_TRADING"
        and settings.PAPER_POLICY_PROFILE != "test"
    )
    has_key = bool(settings.UPBIT_ACCESS_KEY and settings.UPBIT_SECRET_KEY)

    g1, g2, g3, g4, g5 = st.columns(5)
    g1.metric("LIVE_TRADING_ENABLED", str(settings.LIVE_TRADING_ENABLED))
    g2.metric("UPBIT_TRADE_MODE", settings.UPBIT_TRADE_MODE)
    g3.metric("ORDER_TEST_ENABLED", str(settings.UPBIT_ORDER_TEST_ENABLED))
    g4.metric("SHADOW_ENABLED", str(settings.UPBIT_SHADOW_ENABLED))
    g5.metric("API Keys", "set" if has_key else "not set")

    live_label = "LIVE ACTIVE" if live_guard else "SAFE (실거래 비활성)"
    st.info(f"Live Guard: {live_label}  |  POLICY_PROFILE={settings.PAPER_POLICY_PROFILE}")

    # 계좌 잔액
    st.subheader("계좌 잔액 (최신 스냅샷)")
    try:
        with engine.connect() as conn:
            acct_df = pd.read_sql_query(
                text("""
                    SELECT DISTINCT ON (currency)
                        ts, currency, balance, locked, avg_buy_price, unit_currency
                    FROM upbit_account_snapshots WHERE symbol = :sym
                    ORDER BY currency, ts DESC
                """),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"upbit_account_snapshots 없음: {e}")
        acct_df = pd.DataFrame()

    if not acct_df.empty:
        st.dataframe(acct_df, use_container_width=True)
        st.caption(f"스냅샷 기준: {acct_df['ts'].max()}")
    else:
        st.info("계좌 스냅샷 없음")

    # 주문 시도 로그
    st.subheader("주문 시도 로그 (최근 50건)")
    try:
        with engine.connect() as conn:
            oa_df = pd.read_sql_query(
                text("""
                    SELECT ts, action, mode, status, uuid, identifier,
                           side, ord_type, price, volume,
                           http_status, latency_ms, remaining_req,
                           retry_count, final_state, error_msg, paper_trade_id
                    FROM upbit_order_attempts WHERE symbol = :sym
                    ORDER BY ts DESC LIMIT 50
                """),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"upbit_order_attempts 없음: {e}")
        oa_df = pd.DataFrame()

    if not oa_df.empty:
        total     = len(oa_df)
        shadow_n  = int((oa_df["mode"] == "shadow").sum())
        test_n    = int((oa_df["mode"] == "test").sum())
        live_n    = int((oa_df["mode"] == "live").sum())
        error_n   = int((oa_df["status"] == "error").sum())
        f1, f2, f3, f4, f5 = st.columns(5)
        f1.metric("Total", total)
        f2.metric("Shadow", shadow_n)
        f3.metric("Test", test_n)
        f4.metric("Live", live_n)
        f5.metric("Errors", error_n)
        st.dataframe(oa_df, use_container_width=True, height=350)
    else:
        st.info("주문 시도 기록 없음")

    # ── [G] Alt Data ──────────────────────────────────────────────────────────
    st.header("[G] Alt Data (Binance / Coinglass)")

    alt_sym  = settings.ALT_SYMBOL_BINANCE
    cg_sym   = settings.ALT_SYMBOL_COINGLASS
    poll_sec = settings.BINANCE_POLL_SEC

    st.subheader("G1 — Binance 마크가격 WS 상태")
    try:
        with engine.connect() as conn:
            mp_row = conn.execute(
                text("""
                    SELECT max(ts) as last_ts,
                           count(*) FILTER (
                               WHERE ts >= now() AT TIME ZONE 'UTC' - interval '300 seconds'
                           ) as cnt_5m
                    FROM binance_mark_price_1s WHERE symbol = :sym
                """),
                {"sym": alt_sym},
            ).fetchone()
    except Exception as e:
        st.warning(f"binance_mark_price_1s 없음: {e}")
        mp_row = None

    if mp_row is not None:
        last_ts_mp = mp_row.last_ts
        cnt_5m = mp_row.cnt_5m or 0
        fill_5m = cnt_5m / 300 if cnt_5m else 0
        lag_mp = None
        if last_ts_mp is not None:
            if last_ts_mp.tzinfo is None:
                last_ts_mp = last_ts_mp.replace(tzinfo=timezone.utc)
            lag_mp = (now_utc - last_ts_mp).total_seconds()
        g1c1, g1c2, g1c3 = st.columns(3)
        g1c1.metric("Last Insert", str(last_ts_mp)[:19] if last_ts_mp else "N/A")
        g1c2.metric("Lag (sec)", f"{lag_mp:.1f}" if lag_mp is not None else "N/A")
        g1c3.metric("Fill Rate 5min", f"{fill_5m*100:.1f}% ({cnt_5m}/300)")

    st.subheader("G2 — Binance Futures 지표 (최근 6h)")
    try:
        with engine.connect() as conn:
            bfm_df = pd.read_sql_query(
                text("""
                    SELECT ts, metric, value, value2, period
                    FROM binance_futures_metrics WHERE symbol=:sym
                      AND ts >= now() AT TIME ZONE 'UTC' - interval '21600 seconds'
                    ORDER BY ts DESC LIMIT 200
                """),
                conn,
                params={"sym": alt_sym},
            )
    except Exception as e:
        st.warning(f"binance_futures_metrics 없음: {e}")
        bfm_df = pd.DataFrame()

    if not bfm_df.empty:
        for m in ("open_interest", "global_ls_ratio", "taker_ls_ratio", "basis"):
            sub = bfm_df[bfm_df["metric"] == m].sort_values("ts")
            if sub.empty:
                continue
            latest = sub.iloc[-1]
            lag_m = (now_utc - pd.to_datetime(latest["ts"], utc=True)).total_seconds()
            st.metric(m, f"{latest['value']:.6g}" if pd.notna(latest["value"]) else "N/A",
                      delta=f"lag={lag_m:.0f}s")
        st.dataframe(bfm_df, use_container_width=True, height=250)
    else:
        st.info(f"Binance 지표 없음 (poll={poll_sec}s, 대기 중)")

    st.subheader("G3 — Coinglass 청산 맵")
    cg_key_set = bool(settings.COINGLASS_API_KEY)
    st.caption(f"COINGLASS_API_KEY: {'설정됨' if cg_key_set else '미설정 (수집 SKIP)'}")
    try:
        with engine.connect() as conn:
            cg_df = pd.read_sql_query(
                text("""
                    SELECT ts, symbol, exchange, timeframe, summary_json
                    FROM coinglass_liquidation_map WHERE symbol=:sym
                    ORDER BY ts DESC LIMIT 5
                """),
                conn,
                params={"sym": cg_sym},
            )
    except Exception as e:
        st.warning(f"coinglass_liquidation_map 없음: {e}")
        cg_df = pd.DataFrame()

    if not cg_df.empty:
        cg_last = cg_df.iloc[0]
        cg_ts  = pd.to_datetime(cg_last["ts"], utc=True)
        cg_lag = (now_utc - cg_ts).total_seconds()
        st.metric("Last Poll", str(cg_ts)[:19], delta=f"lag={cg_lag:.0f}s")
        st.dataframe(cg_df, use_container_width=True, height=200)
    else:
        st.info("Coinglass 데이터 없음" if cg_key_set else "COINGLASS_API_KEY 미설정")


# ══════════════════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    st.set_page_config(
        page_title="BTC AI Trading Bot",
        page_icon="📊",
        layout="wide",
    )
    st.title("BTC AI Trading Bot Dashboard")

    settings = load_settings()
    now_utc  = datetime.now(timezone.utc)

    # ── 활성 모델 정보 (ACTIVE_MODEL 환경변수 기반 자동 렌더링) ──────────────
    try:
        from app.predictor.ml_model import ModelFactory
        _spec = ModelFactory.get_spec(settings.ACTIVE_MODEL)
        _model_badge = (
            f"**{_spec.display_name}**  |  "
            f"model_id: `{_spec.model_id}`  |  "
            f"H={_spec.h_sec}s  |  γ={_spec.gamma}  |  version: `{_spec.version}`"
        )
    except Exception:
        _model_badge = f"ACTIVE_MODEL: `{settings.ACTIVE_MODEL}`"

    st.info(f"활성 모델 — {_model_badge}")
    st.caption(
        f"종목: {settings.SYMBOL}"
        f"  |  기준 시각: {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC"
        f"  |  .env ACTIVE_MODEL 변경 후 재시작하면 모델이 즉시 교체됩니다."
    )

    # DB 연결 확인
    try:
        engine = get_engine(settings)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        st.success("DB 연결 정상")
    except Exception as e:
        err = str(e)
        if "failed to resolve host" in err or "could not translate host name" in err:
            st.error(f"DB 연결 실패: {e}")
            st.warning(DB_RESOLVE_HINT)
        else:
            st.error(f"DB 연결 실패: {e}")
        return

    tab1, tab2 = st.tabs(["📊 직관적인 요약 (메인)", "🔬 세부 계산 데이터 (전문가용)"])

    with tab1:
        render_tab1(engine, settings, now_utc)

    with tab2:
        render_tab2(engine, settings, now_utc)

    # 5초마다 자동 갱신
    import time as _time
    _time.sleep(5)
    st.rerun()


if __name__ == "__main__":
    main()
