"""
投递执行模块 — DrissionPage 版本
BOSS直聘: 打开岗位详情 → 点击「立即沟通」→ 系统自动发送招呼语
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from DrissionPage import Chromium
from job_search import JobPosting
from utils import get_logger

if TYPE_CHECKING:
    from DrissionPage._pages.chromium_tab import ChromiumTab

logger = get_logger("submit")


class SubmitResult:
    SUCCESS = "投递成功"
    SKIPPED = "skipped"
    DAILY_LIMIT = "daily_limit"
    ALREADY_APPLIED = "already_applied"
    LOGIN_REQUIRED = "login_required"
    DAILY_LIMIT = "daily_limit"
    FAILED = "failed"
    RISK_REJECTED = "risk_rejected"
    MATCH_LOW = "match_low"
    KPI_REJECTED = "kpi_rejected"


class JobSubmitter:

    def __init__(self, browser: Chromium, config: dict):
        self.browser = browser
        self.cfg = config
        submit_cfg = config.get("submit", {})
        self.greeting = submit_cfg.get(
            "greeting",
            "您好，我是2026届信息工程专业本科毕业生，比较匹配贵公司岗位的招聘要求，可以发个简历给您看看吗？"
        )
        self.daily_limit = submit_cfg.get("daily_limit", 150)

        # 每日计数器文件
        self._counter_file = Path(__file__).parent / "daily_count.json"
        self._today = date.today().isoformat()
        self._today_count = self._load_today_count()

    # ==================== 每日计数 ====================

    def _load_today_count(self) -> int:
        """从文件加载今日投递数，跨天自动归零"""
        try:
            if self._counter_file.exists():
                data = json.loads(self._counter_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("date") == self._today:
                    return int(data.get("count", 0))
        except Exception:
            pass
        return 0

    def _save_today_count(self):
        """保存今日投递数到文件"""
        try:
            self._counter_file.write_text(
                json.dumps({"date": self._today, "count": self._today_count},
                           ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"保存每日计数失败: {e}")

    # ==================== 投递 ====================

    def submit(self, job: JobPosting, match_score: float) -> str:
        """打开岗位 → 点立即沟通 → 系统自动发消息"""

        # 每次调用都刷新（防止跨天）
        new_today = date.today().isoformat()
        if new_today != self._today:
            self._today = new_today
            self._today_count = self._load_today_count()

        if self._today_count >= self.daily_limit:
            logger.warning(f"今日已投 {self._today_count}/{self.daily_limit}，已达上限")
            return SubmitResult.DAILY_LIMIT

        logger.info(
            f"  投递 [{self._today_count + 1}/{self.daily_limit}] "
            f"{job.title} @ {job.company}"
        )

        if not job.url:
            logger.warning("  ❌ 无岗位链接")
            return SubmitResult.FAILED

        submit_tab = None
        try:
            submit_tab = self.browser.new_tab(job.url)
            submit_tab.wait(4)
            self._scroll(submit_tab)

            cur_url = str(submit_tab.url).lower()
            if "login" in cur_url:
                logger.warning("  ⚠️ 跳转登录页")
                return SubmitResult.LOGIN_REQUIRED

            if self._already_contacted(submit_tab):
                logger.info("  ⚠️ 已沟通过")
                return SubmitResult.ALREADY_APPLIED

            if not self._click_contact(submit_tab):
                return SubmitResult.FAILED

            self._today_count += 1
            self._save_today_count()
            logger.info(
                f"  ✅ 沟通已发起 ({self._today_count}/{self.daily_limit})"
            )
            return SubmitResult.SUCCESS

        except Exception as e:
            logger.error(f"  ❌ 投递异常: {e}")
            return SubmitResult.FAILED

        finally:
            if submit_tab:
                try:
                    submit_tab.close()
                except Exception:
                    pass

    # ==================== 页面状态检测 ====================

    def _already_contacted(self, tab) -> bool:
        try:
            html = tab.html[:3000]
            if any(s in html for s in ["已沟通过", "继续沟通", "已投递", "已发送"]):
                return True
        except Exception:
            pass
        return False

    # ==================== 点击沟通按钮 ====================

    def _click_contact(self, tab) -> bool:
        """多策略查找并点击「立即沟通」"""
        for text in ("立即沟通", "聊一聊", "发消息", "投递简历"):
            try:
                btn = tab.ele(f"text:{text}")
                if btn:
                    btn.click()
                    logger.info(f"  👆 点击「{text}」")
                    time.sleep(1)
                    return True
            except Exception:
                continue

        for text in ("沟通", "投递"):
            try:
                btn = tab.ele(f"@@text():{text}")
                if btn:
                    btn.click()
                    time.sleep(1)
                    return True
            except Exception:
                continue

        logger.warning("  ❌ 未找到「立即沟通」按钮")
        return False

    # ==================== 辅助 ====================

    def _scroll(self, tab):
        for _ in range(2):
            try:
                tab.run_js("window.scrollBy(0, 300)")
            except Exception:
                pass
            time.sleep(0.3)

    def get_today_count(self) -> int:
        return self._today_count
