"""交易歷史檢視頁
================
資料源：grid_engine 落地檔（logs/decisions.jsonl + logs/bandit_state.json）。
非即時監控——實盤告警走 Telegram（grid_engine notifier），
本頁為事後歷史檢視。
"""
import streamlit as st
import pandas as pd

st.set_page_config(
    page_title="交易歷史 - AS 網格",
    page_icon="📈",
    layout="wide",
)

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from theme import apply_custom_theme
from components.sidebar import render_sidebar
apply_custom_theme()

from state import init_session_state, get_config, check_config_updated
from web.services import history_reader

init_session_state()


def render_header(df: pd.DataFrame):
    col1, col2 = st.columns([3, 2])
    with col1:
        st.title("📈 交易歷史檢視")
    with col2:
        last = history_reader.last_activity(df)
        if last is not None:
            st.metric("最後決策時間", last.strftime("%m-%d %H:%M:%S"))
    st.caption("⚠️ 本頁為引擎落地檔的歷史檢視，非即時監控；實盤告警走 Telegram。")


def render_position_timeline(df: pd.DataFrame):
    st.subheader("📊 持倉軌跡")
    if df.empty:
        st.info("無決策記錄（logs/decisions.jsonl 不存在或為空）。"
                "引擎在本機跑過後才會有資料。")
        return
    symbols = sorted(df["symbol"].dropna().unique())
    symbol = st.selectbox("交易對", options=symbols, key="hist_symbol")
    sdf = df[df["symbol"] == symbol].set_index("ts")
    if sdf.empty:
        st.info("此交易對無記錄")
        return
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**價格**")
        st.line_chart(sdf["price"])
    with c2:
        st.markdown("**多/空持倉**")
        st.line_chart(sdf[["long_position", "short_position"]])
    # 裝死模式事件
    dead = sdf[(sdf["long_dead_mode"] == True) | (sdf["short_dead_mode"] == True)]  # noqa: E712
    if not dead.empty:
        st.warning(f"⚠️ 期間出現裝死模式 {len(dead)} 筆決策記錄"
                   f"（最近: {dead.index.max().strftime('%m-%d %H:%M')}）")


def render_latest_snapshot(df: pd.DataFrame):
    st.subheader("🔍 各交易對最新狀態")
    if df.empty:
        st.info("無決策記錄")
        return
    latest = df.sort_values("ts").groupby("symbol").tail(1)
    rows = [{
        "交易對": r["symbol"],
        "時間": r["ts"].strftime("%m-%d %H:%M:%S"),
        "價格": f"{r['price']:.6f}" if pd.notna(r["price"]) else "-",
        "多單": f"{r['long_position']:.2f}" if pd.notna(r["long_position"]) else "-",
        "空單": f"{r['short_position']:.2f}" if pd.notna(r["short_position"]) else "-",
    } for _, r in latest.iterrows()]
    st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)


def render_bandit_state():
    st.subheader("🎰 Bandit 狀態")
    state = history_reader.load_bandit_state()
    if not state:
        st.info("無 bandit 狀態檔（logs/bandit_state.json）")
        return
    st.json(state, expanded=False)


def render_symbol_config():
    st.subheader("⚙️ 交易對配置")
    config = get_config()
    if not config.symbols:
        st.info("未配置交易對")
        return
    symbol = st.selectbox("選擇交易對", options=list(config.symbols.keys()),
                          key="cfg_symbol")
    if not symbol:
        return
    cfg = config.symbols[symbol]
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**策略參數**")
        st.write(f"- 止盈間距: {cfg.take_profit_spacing*100:.2f}%")
        st.write(f"- 補倉間距: {cfg.grid_spacing*100:.2f}%")
        st.write(f"- 每單數量: {cfg.initial_quantity}")
        st.write(f"- 槓桿（回測假設，非交易所實際）: {cfg.assumed_leverage}x")
    with col2:
        st.markdown("**倉位控制**")
        st.write(f"- 止盈加倍門檻: {cfg.position_limit:.1f}（另需為淨曝險側）")
        st.write(f"- 裝死模式觸發: {cfg.position_threshold:.1f}")
        st.write(f"- 加倍倍數: {cfg.limit_multiplier}x")
        st.write(f"- 裝死倍數: {cfg.threshold_multiplier}x")


def main():
    render_sidebar()
    if check_config_updated():
        st.info("✅ 檢測到配置已更新，正在刷新...")
        st.rerun()

    df = history_reader.load_decisions()
    render_header(df)
    st.divider()

    left, right = st.columns([2, 1])
    with left:
        render_position_timeline(df)
    with right:
        render_latest_snapshot(df)
        st.divider()
        render_bandit_state()
        st.divider()
        render_symbol_config()

    if st.button("🔄 重新載入"):
        st.rerun()


main()
