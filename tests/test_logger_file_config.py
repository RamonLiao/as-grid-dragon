"""釘死引擎 file logger 設定。

為什麼重要：觀察期要靠 log/as_terminal_max.log 回答「-2019/斷路器何時發生、
發生幾次」。曾經 format 只有 %(message)s（datefmt 是死參數），事件落地但
無法定位時間；且單檔曾長到 202MB 無 rotation。這裡釘住三件事：
1. 每行帶 asctime 時間戳
2. handler 是 RotatingFileHandler（有上限、有備份）

3. logging 設定不做 import 副作用，由引擎入口顯式呼叫 setup_file_logging()
   —— RotatingFileHandler 的 rollover 會 rename 活檔，web/streamlit 進程也
   import grid_engine.utils，多 writer 輪替會互相抽走引擎的 fd

不檢查 root logger runtime 狀態：pytest 的 logging plugin 會先裝 handler，
讓 utils 的 basicConfig 變 no-op，測到的會是 pytest 自己的 handler。
改釘 utils 的模組常數與 handler 工廠（basicConfig 直接吃它們，同源耦合）。
"""
import logging
import logging.handlers
from pathlib import Path

from grid_engine import utils


def test_log_lines_carry_timestamp():
    assert "%(asctime)s" in utils.LOG_FORMAT, (
        f"LOG_FORMAT={utils.LOG_FORMAT!r} 缺 %(asctime)s：事件落地但無法定位時間"
    )


def test_file_handler_rotates():
    h = utils.build_log_handler()
    try:
        assert isinstance(h, logging.handlers.RotatingFileHandler), (
            "非 RotatingFileHandler：單檔會無上限長大（曾到 202MB）"
        )
        assert h.maxBytes > 0 and h.backupCount > 0
        assert h.baseFilename.endswith("as_terminal_max.log")
        assert h.stream is None, "delay=True 失效：import/建構即開活檔"
    finally:
        h.close()


def test_import_has_no_logging_side_effect():
    """import grid_engine.utils 不得在 root 裝 handler（多進程單一 writer）"""
    import subprocess
    import sys

    code = (
        "import logging, grid_engine.utils; "
        "assert logging.getLogger().handlers == [], logging.getLogger().handlers"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        cwd=str(Path(__file__).parent.parent),
    )
    assert r.returncode == 0, f"import 即污染 root logger: {r.stderr}"


def test_setup_overrides_preexisting_root_handlers():
    """root 已被先裝 handler（如第三方庫 import 副作用）時，setup 仍必須生效。

    basicConfig 預設遇到已配置的 root 會無聲 no-op —— 「事件不落磁碟」
    這個失敗模式會在無人察覺下重演，必須 force。
    """
    import subprocess
    import sys

    code = (
        "import logging, logging.handlers; "
        "logging.getLogger().addHandler(logging.NullHandler()); "
        "from grid_engine.utils import setup_file_logging; "
        "setup_file_logging(); "
        "hs = logging.getLogger().handlers; "
        "assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in hs), hs"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        cwd=str(Path(__file__).parent.parent),
    )
    assert r.returncode == 0, f"root 有既存 handler 時 setup 無聲失效: {r.stderr}"


def test_engine_entrypoint_wires_setup():
    """引擎入口必須顯式呼叫 setup_file_logging()，否則事件不落磁碟"""
    src = (Path(__file__).parent.parent / "as_terminal_max.py").read_text(
        encoding="utf-8"
    )
    assert "setup_file_logging()" in src
