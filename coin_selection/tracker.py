"""
RotationTracker - 輪動歷史追蹤器
=================================

功能:
- 記錄輪動執行歷史
- 持久化儲存到 JSON 檔案
- 提供統計分析功能

使用方式:
    tracker = RotationTracker()

    # 記錄輪動
    tracker.record(rotation_log)

    # 獲取統計
    stats = tracker.get_stats()
    print(f"總輪動次數: {stats['total_rotations']}")
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any
from collections import defaultdict, Counter

from .models import RotationLog, RotationSignal, CoinScore

logger = logging.getLogger(__name__)

# 預設儲存路徑
DEFAULT_HISTORY_FILE = Path.home() / ".as_rotation_history.json"


class RotationTracker:
    """
    輪動歷史追蹤器

    負責記錄、儲存和分析輪動執行歷史
    """

    def __init__(self, history_file: Optional[Path] = None):
        """
        初始化追蹤器

        Args:
            history_file: 歷史記錄檔案路徑 (預設 ~/.as_rotation_history.json)
        """
        self.history_file = history_file or DEFAULT_HISTORY_FILE
        self.logs: List[RotationLog] = []

        # 載入歷史記錄
        self._load_history()

    # ========== 記錄管理 ==========

    def record(self, log: RotationLog):
        """
        記錄一次輪動

        Args:
            log: RotationLog 物件
        """
        self.logs.append(log)
        self._save_history()
        logger.info(
            f"輪動已記錄: {log.from_symbol} → {log.to_symbol}, "
            f"損益: {log.pnl_impact:+.4f}"
        )

    def record_from_signal(
        self,
        signal: RotationSignal,
        pnl_impact: float = 0.0
    ) -> RotationLog:
        """
        從 RotationSignal 建立並記錄輪動

        Args:
            signal: RotationSignal 物件
            pnl_impact: 輪動造成的損益影響

        Returns:
            建立的 RotationLog
        """
        log = RotationLog(
            timestamp=datetime.now(),
            from_symbol=signal.from_symbol,
            to_symbol=signal.to_symbol,
            trigger_reason=signal.reason,
            score_before=signal.from_score.final_score if signal.from_score else 0,
            score_after=signal.to_score.final_score if signal.to_score else 0,
            pnl_impact=pnl_impact
        )
        self.record(log)
        return log

    def clear_history(self, before_date: Optional[datetime] = None):
        """
        清除歷史記錄

        Args:
            before_date: 清除此日期之前的記錄，如果為 None 則清除全部
        """
        if before_date:
            self.logs = [
                log for log in self.logs
                if log.timestamp >= before_date
            ]
        else:
            self.logs = []

        self._save_history()
        logger.info("輪動歷史已清除")

    # ========== 歷史查詢 ==========

    def get_recent(self, days: int = 30) -> List[RotationLog]:
        """
        獲取最近的輪動記錄

        Args:
            days: 最近多少天

        Returns:
            RotationLog 列表
        """
        cutoff = datetime.now() - timedelta(days=days)
        return [
            log for log in self.logs
            if log.timestamp >= cutoff
        ]

    def get_by_symbol(self, symbol: str) -> List[RotationLog]:
        """
        獲取涉及特定幣種的輪動記錄

        Args:
            symbol: 交易對名稱

        Returns:
            RotationLog 列表
        """
        return [
            log for log in self.logs
            if log.from_symbol == symbol or log.to_symbol == symbol
        ]

    def get_all(self) -> List[RotationLog]:
        """
        獲取所有輪動記錄

        Returns:
            RotationLog 列表
        """
        return self.logs.copy()

    # ========== 統計分析 ==========

    def get_stats(self, days: Optional[int] = None) -> Dict[str, Any]:
        """
        獲取輪動統計

        Args:
            days: 統計最近多少天，None 表示全部

        Returns:
            統計字典
        """
        logs = self.get_recent(days) if days else self.logs

        if not logs:
            return {
                'total_rotations': 0,
                'avg_pnl_impact': 0.0,
                'total_pnl_impact': 0.0,
                'success_rate': 0.0,
                'most_frequent_from': None,
                'most_frequent_to': None,
                'avg_score_improvement': 0.0,
                'period_days': days or 0
            }

        # 基本統計
        total = len(logs)
        total_pnl = sum(log.pnl_impact for log in logs)
        avg_pnl = total_pnl / total

        # 成功率 (輪動後評分更高且盈利)
        successful = sum(
            1 for log in logs
            if log.score_after > log.score_before and log.pnl_impact >= 0
        )
        success_rate = successful / total * 100

        # 最頻繁輪出/輪入幣種
        from_counter = Counter(log.from_symbol for log in logs)
        to_counter = Counter(log.to_symbol for log in logs)

        most_from = from_counter.most_common(1)
        most_to = to_counter.most_common(1)

        # 平均評分改善
        avg_score_improvement = sum(
            log.score_after - log.score_before for log in logs
        ) / total

        return {
            'total_rotations': total,
            'avg_pnl_impact': round(avg_pnl, 4),
            'total_pnl_impact': round(total_pnl, 4),
            'success_rate': round(success_rate, 1),
            'most_frequent_from': most_from[0] if most_from else None,
            'most_frequent_to': most_to[0] if most_to else None,
            'avg_score_improvement': round(avg_score_improvement, 2),
            'period_days': days
        }

    def get_symbol_stats(self, symbol: str) -> Dict[str, Any]:
        """
        獲取特定幣種的輪動統計

        Args:
            symbol: 交易對名稱

        Returns:
            統計字典
        """
        logs = self.get_by_symbol(symbol)

        if not logs:
            return {
                'symbol': symbol,
                'rotations_from': 0,
                'rotations_to': 0,
                'avg_pnl_when_from': 0.0,
                'avg_pnl_when_to': 0.0
            }

        from_logs = [log for log in logs if log.from_symbol == symbol]
        to_logs = [log for log in logs if log.to_symbol == symbol]

        return {
            'symbol': symbol,
            'rotations_from': len(from_logs),
            'rotations_to': len(to_logs),
            'avg_pnl_when_from': (
                sum(log.pnl_impact for log in from_logs) / len(from_logs)
                if from_logs else 0.0
            ),
            'avg_pnl_when_to': (
                sum(log.pnl_impact for log in to_logs) / len(to_logs)
                if to_logs else 0.0
            )
        }

    def get_weekly_summary(self) -> List[Dict[str, Any]]:
        """
        獲取每週摘要

        Returns:
            週摘要列表 (最近 8 週)
        """
        if not self.logs:
            return []

        # 按週分組
        weekly: Dict[str, List[RotationLog]] = defaultdict(list)

        for log in self.logs:
            # 計算週開始日期 (週一)
            week_start = log.timestamp - timedelta(days=log.timestamp.weekday())
            week_key = week_start.strftime("%Y-%m-%d")
            weekly[week_key].append(log)

        # 生成摘要
        summaries = []
        for week_key in sorted(weekly.keys(), reverse=True)[:8]:
            logs = weekly[week_key]
            summaries.append({
                'week_start': week_key,
                'rotations': len(logs),
                'total_pnl': sum(log.pnl_impact for log in logs),
                'avg_score_improvement': sum(
                    log.score_after - log.score_before for log in logs
                ) / len(logs) if logs else 0
            })

        return summaries

    # ========== 持久化 ==========

    def _load_history(self):
        """從檔案載入歷史記錄"""
        if not self.history_file.exists():
            logger.debug(f"歷史檔案不存在: {self.history_file}")
            return

        try:
            with open(self.history_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self.logs = []
            for item in data.get('logs', []):
                log = RotationLog(
                    timestamp=datetime.fromisoformat(item['timestamp']),
                    from_symbol=item['from_symbol'],
                    to_symbol=item['to_symbol'],
                    trigger_reason=item['trigger_reason'],
                    score_before=item['score_before'],
                    score_after=item['score_after'],
                    pnl_impact=item.get('pnl_impact', 0.0)
                )
                self.logs.append(log)

            logger.info(f"載入 {len(self.logs)} 筆輪動歷史記錄")

        except Exception as e:
            logger.error(f"載入歷史記錄失敗: {e}")
            self.logs = []

    def _save_history(self):
        """儲存歷史記錄到檔案"""
        try:
            data = {
                'version': '1.0',
                'updated_at': datetime.now().isoformat(),
                'logs': [log.to_dict() for log in self.logs]
            }

            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            logger.debug(f"已儲存 {len(self.logs)} 筆輪動歷史")

        except Exception as e:
            logger.error(f"儲存歷史記錄失敗: {e}")

    def export_to_csv(self, output_path: Path) -> bool:
        """
        匯出歷史記錄為 CSV

        Args:
            output_path: 輸出檔案路徑

        Returns:
            是否成功
        """
        try:
            import csv

            with open(output_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)

                # 標題列
                writer.writerow([
                    '時間', '原幣種', '目標幣種', '觸發原因',
                    '原評分', '目標評分', '損益影響'
                ])

                # 資料列
                for log in self.logs:
                    writer.writerow([
                        log.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                        log.from_symbol,
                        log.to_symbol,
                        log.trigger_reason,
                        f"{log.score_before:.1f}",
                        f"{log.score_after:.1f}",
                        f"{log.pnl_impact:+.4f}"
                    ])

            logger.info(f"歷史記錄已匯出到: {output_path}")
            return True

        except Exception as e:
            logger.error(f"匯出 CSV 失敗: {e}")
            return False


# ========== 格式化輔助 ==========

class RotationHistoryDisplay:
    """輪動歷史顯示輔助類"""

    @staticmethod
    def format_log(log: RotationLog) -> str:
        """格式化單筆記錄"""
        pnl_str = f"{log.pnl_impact:+.4f}" if log.pnl_impact else "N/A"
        score_change = log.score_after - log.score_before
        score_str = f"{score_change:+.1f}" if score_change else "0"

        return (
            f"[{log.timestamp.strftime('%Y-%m-%d %H:%M')}] "
            f"{log.from_symbol} → {log.to_symbol} | "
            f"評分: {log.score_before:.0f}→{log.score_after:.0f} ({score_str}) | "
            f"損益: {pnl_str}"
        )

    @staticmethod
    def format_stats(stats: Dict[str, Any]) -> str:
        """格式化統計摘要"""
        lines = [
            "======= 輪動統計 =======",
            f"總輪動次數: {stats['total_rotations']}",
            f"平均損益影響: {stats['avg_pnl_impact']:+.4f}",
            f"總損益影響: {stats['total_pnl_impact']:+.4f}",
            f"成功率: {stats['success_rate']:.1f}%",
            f"平均評分改善: {stats['avg_score_improvement']:+.1f}",
        ]

        if stats['most_frequent_from']:
            symbol, count = stats['most_frequent_from']
            lines.append(f"最常輪出: {symbol} ({count}次)")

        if stats['most_frequent_to']:
            symbol, count = stats['most_frequent_to']
            lines.append(f"最常輪入: {symbol} ({count}次)")

        lines.append("========================")
        return "\n".join(lines)

    @staticmethod
    def format_history_table(logs: List[RotationLog], limit: int = 10) -> str:
        """格式化歷史表格"""
        if not logs:
            return "暫無輪動記錄"

        lines = [
            "┌─────────────────┬────────────┬────────────┬────────┬────────┐",
            "│      時間       │   原幣種   │  目標幣種  │ 評分變化│  損益  │",
            "├─────────────────┼────────────┼────────────┼────────┼────────┤"
        ]

        for log in logs[-limit:]:
            score_change = log.score_after - log.score_before
            line = (
                f"│ {log.timestamp.strftime('%m-%d %H:%M')} │"
                f" {log.from_symbol:10s} │"
                f" {log.to_symbol:10s} │"
                f" {score_change:+6.1f} │"
                f" {log.pnl_impact:+.3f} │"
            )
            lines.append(line)

        lines.append("└─────────────────┴────────────┴────────────┴────────┴────────┘")
        return "\n".join(lines)
