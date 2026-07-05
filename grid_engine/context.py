"""ExchangeContext：exchange/precisions/funding_manager 共享可變容器。

兩階段初始化協定：bot __init__ 建立空 ctx 注入各組件；run()→_init_exchange 才寫入
真值。組件一律呼叫當下讀 self.ctx.<attr>，絕不在自己 __init__ 存成員快照——
否則捕獲 None → 下單全炸 / funding 同步靜默失效（spec C1）。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ExchangeContext:
    exchange: Optional[Any] = None
    precisions: Dict[str, dict] = field(default_factory=dict)
    funding_manager: Optional[Any] = None
