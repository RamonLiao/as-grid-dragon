"""狀態管理模組
============
管理 Streamlit session state 的配置生命週期。
bot 生命週期已移除——生產引擎（grid_engine）在 GCE 以獨立行程運行，
web 只做監控（讀落地檔）、回測、設定。
"""
import sys
from pathlib import Path

import streamlit as st

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from grid_engine.config import GlobalConfig  # noqa: E402
from web.services import config_store  # noqa: E402


def init_session_state():
    """初始化 session state"""
    if "initialized" not in st.session_state:
        st.session_state.initialized = True
        st.session_state.config = config_store.load_config()
        st.session_state.config_version = 0
        st.session_state.config_mtime = config_store.get_mtime()


def get_config() -> GlobalConfig:
    init_session_state()
    return st.session_state.config


def save_config(symbol_extras: dict | None = None):
    """merge-preserve 儲存（防止 engine schema 缺欄位流失，見 config_store）。"""
    config_store.save_config(st.session_state.config, symbol_extras=symbol_extras)
    st.session_state.config_version = st.session_state.get("config_version", 0) + 1
    st.session_state.config_mtime = config_store.get_mtime()


def reload_config():
    st.session_state.config = config_store.load_config()
    st.session_state.config_mtime = config_store.get_mtime()


def check_config_updated() -> bool:
    """檢查配置是否已被其他頁面更新，不同步則自動重載。

    以檔案 mtime 判斷，而非逐欄位比對 session 內的 config 物件——
    後者會被頁內 widget（例如回測頁槓桿輸入框對超出 UI 上限的既有值
    做 clamp 後寫回 sym_config）誤判成「其他頁面更新了配置」，導致
    每次互動都被這裡強制 st.rerun() 打斷，使本次 widget 提交的新值
    在該次 rerun 中遺失（頁3 整頁互動失效的根因）。
    """
    init_session_state()
    try:
        current_mtime = config_store.get_mtime()
        if current_mtime != st.session_state.config_mtime:
            st.session_state.config = config_store.load_config()
            st.session_state.config_mtime = current_mtime
            return True
        return False
    except Exception as e:
        print(f"[State] 檢查配置失敗: {e}")
        return False
