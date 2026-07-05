"""RestGateway：全部同步 ccxt REST 卸載到單 worker thread（#2 語意原樣）。

單 worker 不可改：同步 ccxt 實例非 thread-safe，多 worker 會並發打同一 Session。
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial


class RestGateway:
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ccxt-rest")

    async def call(self, fn, *args, **kwargs):
        """在專用單 worker thread 執行同步 REST 呼叫，不阻塞 event loop"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, partial(fn, *args, **kwargs))

    def shutdown(self):
        """排隊中的 REST 直接取消；in-flight 的自然結束"""
        self._executor.shutdown(wait=False, cancel_futures=True)
