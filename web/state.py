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


def get_config() -> GlobalConfig:
    init_session_state()
    return st.session_state.config


def save_config(symbol_extras: dict | None = None):
    """merge-preserve 儲存（防止 engine schema 缺欄位流失，見 config_store）。"""
    config_store.save_config(st.session_state.config, symbol_extras=symbol_extras)
    st.session_state.config_version = st.session_state.get("config_version", 0) + 1


def reload_config():
    st.session_state.config = config_store.load_config()


def check_config_updated() -> bool:
    """檢查配置是否已被其他頁面更新，不同步則自動重載。"""
    init_session_state()
    try:
        file_config = config_store.load_config()
        current_symbols = set(st.session_state.config.symbols.keys())
        file_symbols = set(file_config.symbols.keys())
        if current_symbols != file_symbols:
            st.session_state.config = file_config
            return True
        for symbol in current_symbols:
            current = st.session_state.config.symbols[symbol]
            file_cfg = file_config.symbols[symbol]
            if (current.take_profit_spacing != file_cfg.take_profit_spacing or
                    current.grid_spacing != file_cfg.grid_spacing or
                    current.initial_quantity != file_cfg.initial_quantity or
                    current.leverage != file_cfg.leverage or
                    current.limit_multiplier != file_cfg.limit_multiplier or
                    current.threshold_multiplier != file_cfg.threshold_multiplier or
                    current.enabled != file_cfg.enabled):
                st.session_state.config = file_config
                return True
        return False
    except Exception as e:
        print(f"[State] 檢查配置失敗: {e}")
        return False
