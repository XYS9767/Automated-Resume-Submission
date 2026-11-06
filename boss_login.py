"""Use an isolated Edge profile; reuse login without killing unrelated browsers."""
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler
from DrissionPage import Chromium, ChromiumOptions
from job_search import is_boss_security_page, is_boss_logged_in_visually
from utils import resolve_path, get_logger, safe_save_json

logger = get_logger('login')


class BossLogin:
    def __init__(self, config):
        from access_safety import AccessGuard
        self.guard = AccessGuard(config)
        self.cfg = config
        self.browser, self.work_tab = None, None
        self._edge_process = None
        self.data_dir = resolve_path(config.get('browser', {}).get('data_dir', 'browser_data')).resolve()
        self.cookie_file = resolve_path(config.get('login', {}).get('cookie_file', 'cookies.json'))

    @staticmethod
    def _find_edge_exe():
        for base in (os.environ.get('PROGRAMFILES(X86)', ''), os.environ.get('PROGRAMFILES', ''),
                     os.environ.get('LOCALAPPDATA', '')):
            path = Path(base) / 'Microsoft/Edge/Application/msedge.exe'
            if path.is_file():
                return str(path)
        raise RuntimeError('找不到 Microsoft Edge，请安装后重试。')

    @staticmethod
    def _is_port_open(port):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=.5):
                return True
        except OSError:
            return False

    @staticmethod
    def _debug_endpoint(port):
        try:
            # Loopback control traffic must never go through an environment proxy.
            opener = build_opener(ProxyHandler({}))
            with opener.open(f'http://127.0.0.1:{int(port)}/json/version', timeout=1) as response:
                data = json.load(response)
            if not isinstance(data, dict):
                return None
            path = urlsplit(data.get('webSocketDebuggerUrl', '')).path
            return path if path.startswith('/devtools/browser/') else None
        except (OSError, ValueError, TypeError):
            return None

    def _read_debug_port(self):
        # A listening TCP port alone can belong to an unrelated browser/process.
        # Match the saved browser websocket identity before reusing a connection.
        candidates = []
        try:
            saved = json.loads((self.data_dir / 'browser_connection.json').read_text(encoding='utf-8'))
            candidates.append((int(saved['port']), saved['websocket_path']))
        except (OSError, ValueError, TypeError, KeyError):
            pass
        try:
            lines = (self.data_dir / 'DevToolsActivePort').read_text().splitlines()
            candidates.append((int(lines[0]), lines[1]))
        except (OSError, ValueError, IndexError):
            pass
        for port, expected in candidates:
            if 1 <= port <= 65535 and self._is_port_open(port) and self._debug_endpoint(port) == expected:
                return port
        return None

    @staticmethod
    def _available_port():
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            return listener.getsockname()[1]

    def _launch_args(self, executable, port):
        browser_cfg = self.cfg.get('browser', {})
        args = [executable, f'--remote-debugging-port={port}', '--remote-debugging-address=127.0.0.1',
                '--user-data-dir=' + str(self.data_dir), '--no-first-run', '--no-default-browser-check',
                '--window-size=' + ','.join(map(str, browser_cfg.get('window_size', [1280, 800])))]
        if browser_cfg.get('headless', False):
            args.append('--headless=new')
        mode = browser_cfg.get('proxy_mode', 'system')
        if mode not in ('system', 'direct'):
            raise ValueError('browser.proxy_mode 只支持 system 或 direct')
        if mode == 'direct':
            args.append('--no-proxy-server')
        return args + ['about:blank']

    def _ensure_browser(self):
        browser_cfg = self.cfg.get('browser', {})
        executable = browser_cfg.get('executable') or self._find_edge_exe()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        port = self._read_debug_port()
        if not port:
            requested_port = self._available_port()
            args = self._launch_args(executable, requested_port)
            self._edge_process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                identity = self._debug_endpoint(requested_port)
                if identity:
                    port = requested_port
                    safe_save_json({'port': port, 'websocket_path': identity},
                                   str(self.data_dir / 'browser_connection.json'))
                    break
                if self._edge_process.poll() is not None:
                    break
                time.sleep(.3)
            if not port:
                raise RuntimeError('Edge 调试端口未就绪。请关闭本工具的专用浏览器后重试，或配置新的 browser.data_dir。')
        options = ChromiumOptions()
        options.set_browser_path(executable)
        options.set_address(f'127.0.0.1:{port}')
        options.existing_only(True)
        options.set_timeouts(base=10, page_load=25, script=15)
        self.browser = Chromium(options)
        tabs = self.browser.get_tabs()
        self.work_tab = next((t for t in tabs if urlsplit(t.url).hostname in ('www.zhipin.com', 'zhipin.com')), None)
        if self.work_tab is None:
            self.guard.before('login_page')
            self.work_tab = self.browser.new_tab('https://www.zhipin.com')
        if self.cfg.get('login', {}).get('method') == 'cookie' and self.cookie_file.exists():
            cookies = json.loads(self.cookie_file.read_text(encoding='utf-8'))
            cookies = [c for c in cookies if str(c.get('domain', '')).lstrip('.') in ('zhipin.com', 'www.zhipin.com')]
            self.work_tab.set.cookies(cookies)
            self.guard.before('cookie_refresh', self.work_tab)
            self.work_tab.refresh()

    def _wait_for_user_login(self):
        self.guard.inspect(self.work_tab)
        if is_boss_logged_in_visually(self.work_tab) and not is_boss_security_page(self.work_tab, strict=True):
            logger.info('已恢复登录，自动继续。')
            return
        logger.info('请在专用 Edge 窗口扫码登录；完成后自动继续，无需按回车。')
        timeout = self.cfg.get('login', {}).get('wait_seconds', 180)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.guard.inspect(self.work_tab)
            if not is_boss_security_page(self.work_tab, strict=True) and is_boss_logged_in_visually(self.work_tab):
                logger.info('已检测到登录。')
                return
            time.sleep(1)
        raise TimeoutError(f'等待登录超时（{timeout}秒）；请登录后重新运行，登录状态会保留。')

    def login(self):
        self.guard.check()
        self._ensure_browser()
        self._wait_for_user_login()
        try:
            cookies = self.browser.cookies(all_info=True)
            cookies = [c for c in cookies if 'zhipin.com' in c.get('domain', '')]
            safe_save_json(cookies, str(self.cookie_file))
        except Exception as exc:
            logger.warning('Cookie 导出失败，浏览器用户目录仍保存登录状态: %s', type(exc).__name__)
        return self.browser

    def close(self):
        if self.browser:
            self.browser.quit()
