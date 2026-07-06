"""
AS 網格交易系統 - Web UI 主應用
================================
Dashboard 首頁 - 專業交易儀表板風格
"""

import streamlit as st

# 頁面配置 (必須在最前面)
st.set_page_config(
    page_title="Louis Grid",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 導入主題和狀態管理
from theme import apply_custom_theme, render_status_badge, render_metric_card, render_header_with_logo
from components.sidebar import render_sidebar
from state import (
    init_session_state,
    get_config,
)

# 套用自訂主題
apply_custom_theme()

# 初始化
init_session_state()


def render_header():
    """渲染頁面標題"""
    col1, col2 = st.columns([3, 1])

    with col1:
        render_header_with_logo("Louis Grid", "MAX 增強版 網格交易系統")

    with col2:
        st.markdown(
            render_status_badge("running", "🛰️ 引擎於 GCE 運行"),
            unsafe_allow_html=True
        )


def render_main_metrics():
    """渲染主要指標"""
    config = get_config()

    # 配置摘要（生產引擎狀態由 GCE 端管理，web 不做即時統計）
    enabled = [s for s in config.symbols.values() if s.enabled]

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.markdown(f"""
        <div class="card">
            <div class="card-header">已配置交易對</div>
            <div class="card-value">{len(config.symbols)}</div>
        </div>
        """, unsafe_allow_html=True)

    with col2:
        st.markdown(f"""
        <div class="card">
            <div class="card-header">已啟用</div>
            <div class="card-value">{len(enabled)}</div>
        </div>
        """, unsafe_allow_html=True)

    with col3:
        api_status = "✓" if config.api_key else "✗"
        st.markdown(f"""
        <div class="card">
            <div class="card-header">API 狀態</div>
            <div class="card-value">{api_status}</div>
        </div>
        """, unsafe_allow_html=True)

    with col4:
        mode = "增強" if config.max_enhancement.all_enhancements_enabled else "純淨"
        st.markdown(f"""
        <div class="card">
            <div class="card-header">交易模式</div>
            <div class="card-value">{mode}</div>
        </div>
        """, unsafe_allow_html=True)


def render_control_panel():
    """渲染引擎狀態說明（bot 生命週期由 GCE systemd 管理，web 不啟停）"""
    st.markdown("### 引擎狀態")

    from web.services import history_reader
    df = history_reader.load_decisions(max_lines=1)
    last = history_reader.last_activity(df)

    col1, col2 = st.columns([3, 1])
    with col1:
        if last is not None:
            st.info(f"🛰️ 生產引擎於 GCE 運行（本機最後決策記錄: "
                    f"{last.strftime('%Y-%m-%d %H:%M:%S')}）。"
                    f"實盤告警走 Telegram，web 僅為歷史檢視。")
        else:
            st.info("🛰️ 生產引擎於 GCE 運行。本機無決策記錄檔（logs/decisions.jsonl）。"
                    "實盤告警走 Telegram，web 僅為歷史檢視。")
    with col2:
        if st.button("📊 查看歷史", width='stretch'):
            st.switch_page("pages/1_📈_交易監控.py")


def render_positions_preview():
    """渲染持倉預覽"""
    st.markdown("### 持倉概覽")

    config = get_config()

    if not config.symbols:
        st.info("尚未配置交易對，請先新增交易對")
        if st.button("➕ 新增交易對"):
            st.switch_page("pages/2_⚙️_交易對管理.py")
        return

    # 配置的交易對（即時持倉走生產引擎，本頁只顯示配置摘要）
    for symbol, cfg in config.symbols.items():
        status_icon = "🟢" if cfg.enabled else "⚪"

        with st.container():
            col1, col2, col3, col4 = st.columns([2, 1, 1, 1])

            with col1:
                st.markdown(f"{status_icon} **{symbol}**")

            with col2:
                st.caption(f"止盈: {cfg.take_profit_spacing*100:.2f}%")

            with col3:
                st.caption(f"補倉: {cfg.grid_spacing*100:.2f}%")

            with col4:
                st.caption(f"數量: {cfg.initial_quantity}")

        st.divider()




def main():
    """主函數"""
    render_header()
    st.divider()

    render_main_metrics()
    st.divider()

    render_control_panel()
    st.divider()

    # 持倉概覽 (全寬)
    render_positions_preview()

    # 側邊欄
    render_sidebar()


# 執行頁面
main()
