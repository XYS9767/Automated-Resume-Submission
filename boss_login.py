"""
BOSS直聘登录模块 — DrissionPage 版本
⚠️  仅支持 Microsoft Edge 浏览器，绝不使用 Google Chrome
DrissionPage 通过 CDP 直连 Edge，零 Selenium 痕迹
BOSS直聘 完全检测不到自动化特征
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from typing import Optional

from DrissionPage import Chromium, ChromiumOptions
from utils import resolve_path, get_logger

logger = get_logger("login")

DEBUG_PORT = 9222


class BossLogin:
    """通过 DrissionPage 连接原生浏览器"""

    def __init__(self, config: dict):
        self.cfg = config
        login_cfg = config["login"]
        browser_cfg = config["browser"]

        self.method = login_cfg.get("method", "qr")
        self.cookie_file = resolve_path(login_cfg.get("cookie_file", "cookies.json"))
        self.data_dir = str(resolve_path(browser_cfg.get("data_dir", "./browser_data")))
        self.headless = browser_cfg.get("headless", False)
        self.window_size = browser_cfg.get("window_size", [1280, 800])

        self.browser: Optional[Chromium] = None
        self._edge_process: Optional[subprocess.Popen] = None

    def login(self):
        """主流程"""
        self._launch_edge_with_debug_port()
        self._wait_for_user_login()
        self._connect_via_cdp()
        self._save_cookies()
        return self.browser

    def get_browser(self):
        if self.browser is None:
            raise RuntimeError("请先调用 login()")
        return self.browser

    def close(self):
        if self.browser:
            try:
                self.browser.quit()
            except Exception:
                pass
        if self._edge_process:
            try:
                self._edge_process.terminate()
            except Exception:
                pass

    # ==================== 启动原生浏览器 ====================

    def _launch_edge_with_debug_port(self):
        logger.info("正在启动原生 Edge 浏览器...")

        edge_paths = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        edge_exe = None
        for p in edge_paths:
            if os.path.exists(p):
                edge_exe = p
                break
        if edge_exe is None:
            raise RuntimeError("找不到 Edge 浏览器，请确认已安装 Microsoft Edge")
        self._edge_exe = edge_exe

        os.makedirs(self.data_dir, exist_ok=True)

        cmd = [
            edge_exe,
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={self.data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "https://www.zhipin.com",
        ]
        self._edge_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(3)
        logger.info("✅ 浏览器已启动")

    # ==================== 等待登录 ====================

    def _wait_for_user_login(self):
        print()
        logger.info("=" * 55)
        logger.info("  📱 请在浏览器中扫码登录 BOSS直聘")
        logger.info("     登录成功后 → 回到终端按 Enter")
        logger.info("=" * 55)
        try:
            input()
        except EOFError:
            pass

    # ==================== CDP 连接（仅 Edge） ====================

    def _connect_via_cdp(self):
        logger.info("正在通过 CDP 连接到 Edge 浏览器...")
        co = ChromiumOptions()
        # ⚠️ 强制指定 Edge 浏览器路径 — 无论什么情况都不允许启动 Chrome
        co.set_browser_path(self._edge_exe)
        co.set_local_port(DEBUG_PORT)

        self.browser = Chromium(co)
        logger.info("✅ 已通过 CDP 连接到 Edge 浏览器")
        logger.info(f"   browser 类型: {type(self.browser).__name__}")
        logger.info(f"   连接端口: {DEBUG_PORT}")

        # 验证登录状态
        tab = self.browser.latest_tab
        tab.wait(3)
        if self._is_logged_in(tab):
            logger.info("✅ 已登录 BOSS直聘")
        else:
            logger.warning("⚠️ 未检测到登录状态")
            logger.info("请确认登录后按 Enter...")
            try:
                input()
            except EOFError:
                pass

    def _is_logged_in(self, tab) -> bool:
        try:
            url = tab.url.lower()
            if "chat" in url or "geek" in url:
                return True
            # 检查页面内容
            html = tab.html
            if "登录" not in html and "消息" in html:
                return True
        except Exception:
            pass
        return False

    def _save_cookies(self):
        try:
            cookies = self.browser.cookies()
            with open(self.cookie_file, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            logger.info(f"Cookie 已保存")
        except Exception as e:
            logger.warning(f"Cookie 保存失败: {e}")
