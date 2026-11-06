"""Persistent budgets for browser actions, not a promise of platform approval."""
import json
import math
import time
import re
from datetime import datetime
from urllib.parse import urlsplit

from filelock import FileLock
from utils import resolve_path, safe_save_json


class AccessStopped(RuntimeError):
    """Must reach the run boundary; never retry this as a browser failure."""


def page_access_status(page):
    """Classify visible current UI, excluding normal job/chat content from warnings."""
    url = page.url
    path = urlsplit(url if isinstance(url, str) else '').path.lower()
    evidence = page.run_js(r"""
        const visible = e => {
            if (!e || !e.getClientRects().length) return false;
            for (let n=e; n; n=n.parentElement) {
                const s=getComputedStyle(n);
                if (s.display==='none' || s.visibility==='hidden' || s.visibility==='collapse'
                    || Number(s.opacity)===0 || n.getAttribute('aria-hidden')==='true') return false;
            }
            return true;
        };
        const contentSelector = '.job-card-wrapper, .job-card-box, .job-sec-text, '
            + '.job-detail, .chat-conversation';
        const content = [...document.querySelectorAll(contentSelector)].some(visible);
        const notices = [...document.querySelectorAll('[role="alert"], [role="dialog"], '
            + '.toast, .toast-wrap, .dialog-wrap, .dialog-container, .el-message')]
            .filter(e => visible(e) && !e.closest(contentSelector))
            .map(e => e.innerText || '').join('\n');
        const challenge = [...document.querySelectorAll('iframe[src*="captcha"], '
            + 'iframe[src*="security"], .geetest_panel, .nc-container')].some(visible);
        return {body: document.body ? document.body.innerText.slice(0,4000) : '',
                notices, content, challenge};
    """)
    # String fallback keeps simple offline page doubles usable.
    if isinstance(evidence, str):
        evidence = {'body': evidence}
    if not isinstance(evidence, dict):
        raise AccessStopped('无法核实当前页面状态，停止本次运行。')
    body = str(evidence.get('body') or '').strip()
    notices = str(evidence.get('notices') or '').strip()
    # A short standalone error page or a visible site dialog is actionable.
    warning = notices or (body if not evidence.get('content') and len(body) < 1000 else '')
    if any(x in warning for x in ('您的账户存在异常行为', '您的账号存在异常行为', '暂时被限制访问')) or warning == '访问受限':
        recovery = re.search(r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}', warning)
        return 'restricted', ('网站明确限制账号访问' +
            ('；页面恢复时间 ' + recovery.group() if recovery else '') + '。需人工核实。')
    if any(x in path for x in ('security.html', '/captcha', 'antispam', '/web/passport/zp/security')):
        return 'verification', '当前页面要求安全验证；停止本次运行，请手动处理后重新启动。'
    if evidence.get('challenge') is True or any(x in warning for x in (
            '请完成安全验证', '操作过于频繁', '操作频繁', '环境存在异常',
            '拖动滑块', '滑动验证', '人机验证', '请完成验证后继续')):
        return 'verification', '当前可见提示要求验证或访问异常；停止本次运行，请手动核实后重新启动。'
    return '', ''


class AccessGuard:
    def __init__(self, config, clock=None, sleep=None):
        self.options = config.get('safety', {})
        self.path = resolve_path(self.options.get('state_file', 'access_state.json'))
        self.stop_path = resolve_path('stop_requested.flag')
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self.run_count = 0
        self.session_stop = ''
        self.interval = float(self.options.get('min_interval_seconds', 15))
        self.daily_limit = int(self.options.get('max_actions_per_day', 120))
        self.run_limit = int(self.options.get('max_actions_per_run', 60))
        if not math.isfinite(self.interval) or self.interval < 0 or min(self.daily_limit, self.run_limit) < 1:
            raise ValueError('safety 的访问间隔必须非负，访问上限必须为正数')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(self.path) + '.lock', timeout=0)

    def _load(self):
        if not self.path.exists():
            return {}
        try:
            state = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(state, dict):
                raise ValueError('not an object')
            if not isinstance(state.get('count', 0), int) or state.get('count', 0) < 0:
                raise ValueError('invalid count')
            if not math.isfinite(float(state.get('last_action_at', 0))):
                raise ValueError('invalid timestamp')
            return state
        except (OSError, ValueError, TypeError) as exc:
            raise AccessStopped('访问状态文件损坏，停止访问；请保留文件核实，不能清零后继续。') from exc

    def _check(self, state):
        if self.session_stop:
            raise AccessStopped(self.session_stop)
        if self.options.get('paused', False):
            raise AccessStopped('safety.paused 已开启：程序保持暂停，不连接 BOSS。')
        if self.stop_path.exists():
            raise AccessStopped('收到停止请求。')
        if state.get('blocked_reason'):
            raise AccessStopped('已持久暂停访问：' + str(state['blocked_reason']))

    def check(self):
        with self.lock:
            state = self._load()
            self._check(state)
            day = datetime.fromtimestamp(self.clock()).date().isoformat()
            if ((state.get('date') == day and state.get('count', 0) >= self.daily_limit)
                    or self.run_count >= self.run_limit):
                raise AccessStopped('已达到浏览访问上限，不连接浏览器。')

    def before(self, action, page=None):
        if page is not None:
            self.inspect(page)
        while True:
            with self.lock:
                state = self._load()
                self._check(state)
                now = self.clock()
                day = datetime.fromtimestamp(now).date().isoformat()
                count = state.get('count', 0) if state.get('date') == day else 0
                if count >= self.daily_limit or self.run_count >= self.run_limit:
                    raise AccessStopped('已达到浏览访问上限；保存进度并停止，不自动重试。')
                wait = max(0, float(state.get('last_action_at', 0)) + self.interval - now)
                if not wait:
                    state.update(date=day, count=count + 1, last_action_at=now, last_action=action)
                    # Reserve before the operation: failures also consume the budget.
                    safe_save_json(state, str(self.path))
                    self.run_count += 1
                    return
            self.sleep(min(wait, .5))
            if page is not None:
                self.inspect(page)

    def block(self, reason):
        with self.lock:
            state = self._load()
            state.update(blocked_reason=reason, blocked_at=self.clock())
            safe_save_json(state, str(self.path))
        raise AccessStopped(reason)

    def stop(self, reason):
        """Stop this run without permanently locking future manual starts."""
        self.session_stop = reason
        raise AccessStopped(reason)

    def inspect(self, page):
        """Only read the current DOM. Never refresh or issue a network probe."""
        try:
            kind, reason = page_access_status(page)
        except AccessStopped:
            raise
        except Exception as exc:
            raise AccessStopped('无法检查当前页面状态，停止访问。') from exc
        if kind == 'restricted':
            self.block(reason)
        if kind == 'verification':
            self.stop(reason)

    def detail_failed(self):
        with self.lock:
            state = self._load()
            failures = int(state.get('detail_failures', 0)) + 1
            state['detail_failures'] = failures
            if failures >= int(self.options.get('max_detail_failures', 2)):
                state.update(blocked_reason='连续无法确认岗位详情，已暂停访问，不能按列表资料继续投递。',
                             blocked_at=self.clock())
            safe_save_json(state, str(self.path))
            if state.get('blocked_reason'):
                raise AccessStopped(state['blocked_reason'])

    def detail_ok(self):
        with self.lock:
            state = self._load()
            if state.get('detail_failures'):
                state['detail_failures'] = 0
                safe_save_json(state, str(self.path))
