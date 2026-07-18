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
import sys
import time
from pathlib import Path
from typing import Optional

from DrissionPage import Chromium, ChromiumOptions
from job_search import verify_boss_session, is_boss_security_page, is_boss_logged_in_visually, probe_boss_session
from utils import resolve_path, get_logger

logger = get_logger("login")


class BossLogin:
    """通过 DrissionPage 连接原生浏览器"""

    def __init__(self, config: dict):
        self.cfg = config
        login_cfg = config["login"]
        browser_cfg = config["browser"]

        self.method = login_cfg.get("method", "qr")
        self.cookie_file = resolve_path(login_cfg.get("cookie_file", "cookies.json"))
        self.data_dir = str(resolve_path(browser_cfg.get("data_dir", "./browser_data")).resolve())
        self.headless = browser_cfg.get("headless", False)
        self.window_size = browser_cfg.get("window_size", [1280, 800])
        self.kill_edge_on_start = browser_cfg.get("kill_edge_on_start", True)

        self.browser: Optional[Chromium] = None
        self._edge_process: Optional[subprocess.Popen] = None
        self._debug_port: Optional[int] = None
        self.work_tab = None

    def login(self):
        """主流程"""
        self._ensure_browser()
        self._wait_for_user_login()
        self._verify_login()
        self._save_cookies()
        tab = self.work_tab or self.browser.latest_tab
        if verify_boss_session(tab) and not is_boss_security_page(tab):
            self._open_job_search_page()
        elif is_boss_logged_in_visually(tab, self.browser):
            logger.info("  保持当前页面，避免触发安全验证跳转")
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

    # ==================== 启动 / 连接浏览器 ====================

    @staticmethod
    def _find_edge_exe() -> str:
        edge_paths = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        for p in edge_paths:
            if os.path.exists(p):
                return p
        raise RuntimeError("找不到 Edge 浏览器，请确认已安装 Microsoft Edge")

    @staticmethod
    def _is_port_open(port: int, host: str = "127.0.0.1") -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            return s.connect_ex((host, port)) == 0

    def _prepare_edge_environment(self):
        """启动前清理占用 browser_data 的 Edge 进程"""
        if not self.kill_edge_on_start:
            return

        logger.info("清理 Edge 进程，释放 browser_data 配置目录...")
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/IM", "msedge.exe", "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.run(
                ["pkill", "-f", "msedge"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        time.sleep(2)

        data = Path(self.data_dir)
        data.mkdir(parents=True, exist_ok=True)
        port_file = data / "DevToolsActivePort"
        if port_file.exists():
            try:
                port_file.unlink()
            except OSError:
                pass
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
            lock = data / name
            if lock.exists():
                try:
                    lock.unlink()
                except OSError:
                    pass

    def _read_debug_port(self) -> Optional[int]:
        port_file = Path(self.data_dir) / "DevToolsActivePort"
        if not port_file.exists():
            return None
        try:
            first = port_file.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
            return int(first)
        except (ValueError, IndexError, OSError):
            return None

    def _launch_edge_with_auto_port(self) -> int:
        """手动启动 Edge，使用 port=0 让浏览器自动分配调试端口"""
        cmd = [
            self._edge_exe,
            "--remote-debugging-port=0",
            f"--user-data-dir={self.data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-allow-origins=*",
            "--disable-restore-session-state",
            "--disable-session-crashed-bubble",
            # 降低自动化特征，减少 BOSS code=37「环境异常」
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--lang=zh-CN",
            "about:blank",
        ]
        if self.headless:
            cmd.insert(1, "--headless=new")

        self._edge_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        deadline = time.time() + 30
        while time.time() < deadline:
            port = self._read_debug_port()
            if port and self._is_port_open(port):
                return port
            time.sleep(0.5)

        raise RuntimeError(
            "Edge 已启动，但未能获取 CDP 调试端口。\n"
            "请确认 Microsoft Edge 可正常打开，然后重试。"
        )

    def _connect_browser(self, port: int):
        co = ChromiumOptions()
        co.set_browser_path(self._edge_exe)
        co.set_address(f"127.0.0.1:{port}")
        co.existing_only(True)
        self.browser = Chromium(co)
        self._debug_port = port
        self._apply_stealth()

    def _apply_stealth(self):
        """隐藏 webdriver 等自动化痕迹"""
        js = """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        window.chrome = window.chrome || { runtime: {} };
        Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        """
        try:
            for t in self.browser.get_tabs():
                try:
                    t.run_cdp(
                        "Page.addScriptToEvaluateOnNewDocument",
                        source=js,
                    )
                    t.run_js(js)
                except Exception:
                    pass
        except Exception:
            pass

    def _is_blank_url(self, url: str) -> bool:
        u = (url or "").lower()
        return u in ("about:blank", "") or u.startswith(("edge://newtab", "chrome://newtab"))

    def _normalize_tabs(self, target_url: str = "https://www.zhipin.com"):
        """关闭多余标签，只保留一个 BOSS 工作页"""
        tabs = list(self.browser.get_tabs())
        keep = None
        blanks = []
        extras = []

        for t in tabs:
            url = (t.url or "").lower()
            if "zhipin.com" in url:
                if keep is None:
                    keep = t
                else:
                    extras.append(t)
            elif self._is_blank_url(url):
                blanks.append(t)
            else:
                extras.append(t)

        for t in extras:
            try:
                t.close()
            except Exception:
                pass

        if keep is None:
            keep = blanks[0] if blanks else self.browser.new_tab(target_url)
        else:
            for t in blanks:
                if t is not keep:
                    try:
                        t.close()
                    except Exception:
                        pass

        keep.get(target_url)
        keep.wait(2)
        self.work_tab = keep
        logger.info(f"工作标签页: {keep.url}")
        return keep

    def _open_job_search_page(self):
        if not self.work_tab:
            return
        if is_boss_security_page(self.work_tab):
            return
        self.work_tab.get("https://www.zhipin.com/web/geek/jobs")
        self.work_tab.wait(2)

    def _ensure_browser(self):
        """启动 Edge 并通过 DevToolsActivePort 连接 CDP"""
        logger.info("正在启动 Edge 浏览器...")
        self._edge_exe = self._find_edge_exe()
        self._prepare_edge_environment()

        port = self._launch_edge_with_auto_port()
        logger.info(f"Edge 调试端口: {port}")
        self._connect_browser(port)
        self._normalize_tabs("https://www.zhipin.com")
        logger.info(f"✅ 浏览器已就绪 (CDP 127.0.0.1:{port})")

    # ==================== 等待登录 ====================

    def _wait_for_user_login(self):
        print()
        logger.info("=" * 55)
        logger.info("  📱 请在浏览器中扫码登录 BOSS直聘")
        logger.info("     如出现安全验证页，先完成滑块/验证码")
        logger.info("     页面稳定后再回到终端按 Enter")
        logger.info("=" * 55)
        try:
            input()
        except EOFError:
            pass

    # ==================== 登录验证 ====================

    def _verify_login(self):
        tab = self.work_tab or self.browser.latest_tab
        tab.wait(2)

        # ① API 完全通过
        if verify_boss_session(tab):
            logger.info("✅ 已登录 BOSS直聘")
            return

        # ② 页面/Cookie 已登录（右上角有用户名）→ 直接继续
        if is_boss_logged_in_visually(tab, self.browser):
            api_status = probe_boss_session(tab)
            logger.info("✅ 页面已登录（检测到用户名/消息入口）")
            if api_status == "security":
                logger.warning("⚠️ API 安全验证未通过，将使用页面点击方式搜索/投递")
                logger.info("  建议：手动点击顶部「职位」并搜索一次，有助于解除 API 限制")
            elif api_status == "login":
                logger.warning("⚠️ Cookie 可能过期，如遇问题请重新扫码登录")
            return

        # ③ 确实未登录 → 最多等待 3 次
        for _ in range(3):
            if is_boss_security_page(tab):
                logger.warning("⚠️ 浏览器在安全验证页")
                logger.info("  → 请完成滑块/验证码，验证成功后按 Enter")
            else:
                logger.warning("⚠️ 未检测到登录状态")
                logger.info("  → 请在浏览器扫码登录，完成后按 Enter")

            try:
                input()
            except EOFError:
                pass
            tab = self.work_tab or self.browser.latest_tab
            tab.wait(2)

            if verify_boss_session(tab):
                logger.info("✅ 已登录 BOSS直聘")
                return
            if is_boss_logged_in_visually(tab, self.browser):
                logger.info("✅ 页面已登录")
                return

        logger.warning("⚠️ 仍未检测到登录，继续运行可能失败")

    def _is_logged_in(self, tab) -> bool:
        return is_boss_logged_in_visually(tab, self.browser)

    def _save_cookies(self):
        try:
            cookies = self.browser.cookies()
            with open(self.cookie_file, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            logger.info("Cookie 已保存")
        except Exception as e:
            logger.warning(f"Cookie 保存失败: {e}")
