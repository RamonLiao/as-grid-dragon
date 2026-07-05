#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交易所符號轉換測試
==================
測試各交易所適配器的符號轉換功能
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exchanges.binance import BinanceAdapter
from exchanges.bitget import BitgetAdapter
from exchanges.bybit import BybitAdapter
from exchanges.gate import GateAdapter
from utils import normalize_symbol

def test_adapter_conversion(adapter_class, adapter_name):
    """測試單個適配器的符號轉換"""
    print(f"\n{'='*60}")
    print(f"測試 {adapter_name} 符號轉換")
    print(f"{'='*60}")
    
    adapter = adapter_class()
    
    test_cases = [
        "XRPUSDT",
        "XRP/USDT:USDT",
        "BTCUSDT",
        "BTC/USDT:USDT",
        "ETHUSDC",
        "ETH/USDC:USDC",
    ]
    
    print(f"\n1. CCXT 格式轉換 (Raw -> CCXT)")
    print("-" * 60)
    for raw in test_cases:
        try:
            ccxt = adapter.convert_symbol_to_ccxt(raw)
            print(f"   {raw:20} -> {ccxt}")
        except Exception as e:
            print(f"   {raw:20} -> ❌ {e}")
    
    print(f"\n2. WebSocket 格式轉換 (Raw -> WS)")
    print("-" * 60)
    for raw in test_cases:
        try:
            ws = adapter.convert_symbol_to_ws(raw)
            print(f"   {raw:20} -> {ws}")
        except Exception as e:
            print(f"   {raw:20} -> ❌ {e}")
    
    print(f"\n3. 雙向轉換測試 (CCXT -> WS -> CCXT)")
    print("-" * 60)
    ccxt_symbols = ["XRP/USDT:USDT", "BTC/USDT:USDT", "ETH/USDC:USDC"]
    for ccxt_sym in ccxt_symbols:
        try:
            ws_sym = adapter.convert_symbol_to_ws(ccxt_sym)
            ccxt_back = adapter.convert_symbol_to_ccxt(ws_sym)
            match = "✅" if ccxt_sym == ccxt_back else "❌"
            print(f"   {ccxt_sym:20} -> {ws_sym:15} -> {ccxt_back:20} {match}")
        except Exception as e:
            print(f"   {ccxt_sym:20} -> ❌ {e}")

def test_normalize_symbol():
    """測試 normalize_symbol 函數"""
    print(f"\n{'='*60}")
    print(f"測試 normalize_symbol 標準化")
    print(f"{'='*60}\n")
    
    test_cases = [
        "XRPUSDT",
        "XRP/USDT",
        "XRP/USDT:USDT",
        "xrp-usdt",
        "xrp_usdt",
        "BTCUSDC",
        "BTC/USDC:USDC",
    ]
    
    for symbol in test_cases:
        raw, ccxt, coin, quote = normalize_symbol(symbol)
        print(f"   {symbol:20} -> raw={raw:10} ccxt={ccxt:20} coin={coin:5} quote={quote}")

def test_matching_strategy():
    """模擬各交易所的符號匹配流程"""
    print(f"\n{'='*60}")
    print(f"模擬 Ticker 消息匹配流程")
    print(f"{'='*60}\n")
    
    # 模擬配置
    config_symbol = "XRPUSDT"
    ccxt_symbol = "XRP/USDT:USDT"
    
    exchanges = {
        "Binance": (BinanceAdapter(), "xrpusdt"),
        "Bitget": (BitgetAdapter(), "XRPUSDT"),
        "Bybit": (BybitAdapter(), "XRPUSDT"),
        "Gate.io": (GateAdapter(), "XRP_USDT"),
    }
    
    for exchange_name, (adapter, ws_message) in exchanges.items():
        print(f"\n{exchange_name} WebSocket 消息: {ws_message}")
        print("-" * 40)
        
        # 測試匹配策略
        matched = False
        
        # 1. CCXT 直接匹配
        if '/' in ws_message or ':' in ws_message:
            if ws_message == ccxt_symbol:
                print(f"   ✅ CCXT 直接匹配")
                matched = True
        
        # 2. 適配器轉換
        if not matched:
            try:
                ccxt_from_ws = adapter.convert_symbol_to_ccxt(ws_message)
                if ccxt_from_ws == ccxt_symbol:
                    print(f"   ✅ 適配器轉換匹配: {ws_message} -> {ccxt_from_ws}")
                    matched = True
            except Exception as e:
                print(f"   ❌ 適配器轉換失敗: {e}")
        
        # 3. WS 格式匹配
        if not matched:
            try:
                ws_from_config = adapter.convert_symbol_to_ws(config_symbol)
                if ws_message.upper() == ws_from_config.upper():
                    print(f"   ✅ WS 格式匹配: {ws_message} == {ws_from_config}")
                    matched = True
            except Exception as e:
                print(f"   ❌ WS 格式匹配失敗: {e}")
        
        # 4. normalize_symbol 匹配
        if not matched:
            normalized_ws = normalize_symbol(ws_message)[0]
            normalized_config = normalize_symbol(config_symbol)[0]
            if normalized_ws and normalized_ws == normalized_config:
                print(f"   ✅ 標準化匹配: {ws_message} ({normalized_ws}) == {config_symbol} ({normalized_config})")
                matched = True
        
        if not matched:
            print(f"   ❌ 無法匹配")

if __name__ == "__main__":
    print("\n" + "="*60)
    print("交易所符號轉換完整測試")
    print("="*60)
    
    # 測試各交易所適配器
    test_adapter_conversion(BinanceAdapter, "Binance")
    test_adapter_conversion(BitgetAdapter, "Bitget")
    test_adapter_conversion(BybitAdapter, "Bybit")
    test_adapter_conversion(GateAdapter, "Gate.io")
    
    # 測試標準化函數
    test_normalize_symbol()
    
    # 測試匹配策略
    test_matching_strategy()
    
    print(f"\n{'='*60}")
    print("測試完成")
    print(f"{'='*60}\n")
