"""页面投递：精确岗位身份、确认结果、持久化防重发。"""
import json
import time
from datetime import date, datetime
from urllib.parse import urlsplit, parse_qs, urljoin
from job_search import extract_encrypt_job_id, job_url_key, is_boss_security_page, JobSearcher, enable_durable_responses
from utils import get_logger, resolve_path, safe_save_json

logger = get_logger('submit')


class SubmitResult:
    SUCCESS = '投递成功'  # 兼容旧记录，实际表示已确认发起沟通。
    SKIPPED = 'skipped'
    DAILY_LIMIT = 'daily_limit'
    RUN_LIMIT = 'run_limit'
    ALREADY_APPLIED = 'already_applied'
    LOGIN_REQUIRED = 'login_required'
    SECURITY_REQUIRED = 'security_required'
    UNCONFIRMED = 'unconfirmed'
    FAILED = 'failed'
    RISK_REJECTED = 'risk_rejected'
    MATCH_LOW = 'match_low'
    KPI_REJECTED = 'kpi_rejected'


class JobSubmitter:
    def __init__(self, browser, config, work_tab=None, get_detail_tab=None):
        self.browser, self.cfg, self.work_tab = browser, config, work_tab
        self._get_detail_tab_fn = get_detail_tab
        self._detail_tab = None
        cfg = config.get('submit', {})
        self.greeting = cfg.get('greeting', '').strip()
        self.send_greeting = cfg.get('send_greeting', True)
        self.daily_limit = int(cfg.get('daily_limit', 150))
        self.max_attempts = int(cfg.get('max_attempts_per_run', 0))
        self.attempt_count = 0
        self.timeout = float(cfg.get('confirm_timeout', 12))
        self.last_reason, self.last_details, self._clicked = '', {}, False
        self._state_path = resolve_path(cfg.get('state_file', 'submission_state.json'))
        self._counter_file = resolve_path(cfg.get('counter_file', 'daily_count.json'))
        self._state = {'jobs': {}, 'date': date.today().isoformat(), 'count': 0}
        if self._state_path.exists():
            loaded = json.loads(self._state_path.read_text(encoding='utf-8'))
            if not isinstance(loaded.get('jobs'), dict):
                raise ValueError('投递状态文件损坏，请先恢复备份: ' + str(self._state_path))
            self._state.update(loaded)
        self._refresh_day()
        if self._counter_file.exists():
            legacy = json.loads(self._counter_file.read_text(encoding='utf-8'))
            if legacy.get('date') == self._state['date']:
                self._state['count'] = max(self._state['count'], int(legacy.get('count', 0)))

    def _refresh_day(self):
        if self._state['date'] != date.today().isoformat():
            self._state.update(date=date.today().isoformat(), count=0)

    def get_today_count(self):
        self._refresh_day()
        return int(self._state['count'])

    def _mark(self, job, status, reason=''):
        self._state['jobs'][job_url_key(job.url)] = {
            'status': status, 'reason': reason, 'title': job.title, 'company': job.company,
            'updated_at': datetime.now().isoformat(timespec='seconds')}
        safe_save_json(self._state, str(self._state_path))

    def _get_detail_tab(self):
        if self._get_detail_tab_fn:
            return self._get_detail_tab_fn()
        if self._detail_tab is not None:
            try:
                _ = self._detail_tab.url
                return self._detail_tab
            except Exception:
                pass
        self._detail_tab = self.browser.new_tab('about:blank')
        return self._detail_tab

    @staticmethod
    def _tab_has_job(tab, encrypt_id, title=''):
        if not encrypt_id:
            return False
        if extract_encrypt_job_id(tab.url) == encrypt_id:
            return True
        if urlsplit(tab.url).path == '/web/geek/chat':
            # The chat router removes query parameters after selecting a recipient.
            # Read only the active conversation's exact job identity, not the sidebar.
            try:
                return bool(tab.run_js("""
                    const conversation = document.querySelector('.chat-conversation .top-info-content')?.__vue__?.['conversation$'];
                    return !!conversation && String(conversation.encryptJobId || '') === arguments[0];
                """, str(encrypt_id)))
            except Exception:
                return False
        return False

    def _open_job_detail(self, tab, job):
        if self._tab_has_job(tab, job.encrypt_job_id):
            return True
        tab.get(job.url)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if is_boss_security_page(tab, strict=True):
                return False
            if self._tab_has_job(tab, job.encrypt_job_id):
                return True
            if any(x in urlsplit(tab.url).path for x in ('/login', '/passport')):
                return False
            time.sleep(.3)
        return False

    def _page_state(self, tab):
        if is_boss_security_page(tab, strict=True):
            return SubmitResult.SECURITY_REQUIRED
        if any(x in urlsplit(tab.url).path for x in ('/login', '/passport')):
            return SubmitResult.LOGIN_REQUIRED
        info = tab.run_js("""
            const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
            const alerts = [...document.querySelectorAll(
                '[role="alert"], [role="dialog"], .toast, .toast-wrap, .dialog-wrap, .dialog-container, .el-message'
            )].filter(visible).map(e => e.innerText || '').join(' ');
            const controls = [...document.querySelectorAll('.btn-startchat, .btn-talk, a, button')]
                .filter(visible).map(e => (e.innerText || '').trim()).filter(t => t.length < 20);
            return {alerts, controls};
        """) or {}
        alerts = info.get('alerts', '')
        if any(t in alerts for t in ('今日沟通已达上限', '今日沟通次数已达', '今日沟通人数已达', '次数用完', '明日再来', '今日开聊已达')):
            return SubmitResult.DAILY_LIMIT
        if any(t in alerts for t in ('请先登录', '登录后沟通', '扫码登录')):
            return SubmitResult.LOGIN_REQUIRED
        if any(t in alerts for t in ('操作频繁', '操作过于频繁', '环境存在异常', '请完成安全验证')):
            return SubmitResult.SECURITY_REQUIRED
        if any(t in info.get('controls', []) for t in ('继续沟通', '已沟通过', '已投递')):
            return SubmitResult.ALREADY_APPLIED
        return ''

    def _find_contact(self, tab):
        labels = {'立即沟通', '免费沟通', '聊一聊', '和BOSS沟通', '和Ta沟通', '投递简历'}
        # Real BOSS anchors contain whitespace/nested nodes: exact text locators
        # return nothing even when ChromiumElement.text reads "立即沟通".
        for selector in ('css:.btn-startchat, .btn-talk', 'css:button, a, [role="button"]'):
            for button in tab.eles(selector, timeout=0):
                label = ''.join((button.text or '').split())
                if (label in labels and button.states.is_displayed
                        and button.attr('disabled') is None
                        and button.attr('aria-disabled') != 'true'):
                    return button
        return None

    @staticmethod
    def _response_result(data):
        if not isinstance(data, dict):
            return ''
        code = str(data.get('code', ''))
        msg = str(data.get('message') or data.get('msg') or '')
        if any(t in msg for t in ('上限', '次数用完', '明日再来')):
            return SubmitResult.DAILY_LIMIT
        if code == '37' or any(t in msg for t in ('频繁', '安全验证', '环境存在异常')):
            return SubmitResult.SECURITY_REQUIRED
        if code in ('7', '101') or '请先登录' in msg:
            return SubmitResult.LOGIN_REQUIRED
        if any(t in msg for t in ('已沟通', '已经沟通', '已经添加', '已投递')):
            return SubmitResult.ALREADY_APPLIED
        return SubmitResult.SUCCESS if code == '0' else SubmitResult.FAILED if code else ''

    def _wait_confirmation(self, tab, job):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            packet = tab.listen.wait(timeout=.25, raise_err=False)
            if packet and not isinstance(packet, bool):
                for item in packet if isinstance(packet, list) else [packet]:
                    try:
                        if urlsplit(item.url).path != '/wapi/zpgeek/friend/add.json':
                            continue
                        params = JobSearcher._packet_params(item)
                        request_id = str(params.get('jobId') or params.get('encryptJobId') or '')
                        security_id = str(params.get('securityId') or '')
                        if request_id:
                            if request_id != job.encrypt_job_id:
                                continue
                        elif not (job._security_id and security_id == job._security_id):
                            continue
                        body = item.response.body
                        if isinstance(body, bytes):
                            body = body.decode('utf-8')
                        data = json.loads(body) if isinstance(body, str) else body
                        result = self._response_result(data)
                        if result:
                            self.last_reason = str(data.get('message') or data.get('msg') or '网站已返回沟通结果')[:200]
                            return result
                    except (ValueError, AttributeError, TypeError):
                        continue
            if getattr(getattr(tab, 'states', None), 'is_loading', False):
                continue  # Navigation must not block network confirmation behind page-load waits.
            state = self._page_state(tab)
            if state == SubmitResult.ALREADY_APPLIED and self._tab_has_job(tab, job.encrypt_job_id):
                self.last_reason = '当前岗位按钮已变为继续沟通'
                return SubmitResult.SUCCESS
            if state in (SubmitResult.DAILY_LIMIT, SubmitResult.LOGIN_REQUIRED, SubmitResult.SECURITY_REQUIRED):
                return state
            time.sleep(.15)
        self.last_reason = '已点击但未收到可核实结果，暂存待核实，避免重发'
        return SubmitResult.UNCONFIRMED

    @staticmethod
    def _greeting_status(tab, text):
        return tab.run_js("""
            const item = [...document.querySelectorAll('.chat-conversation .message-item.item-myself')]
                .find(e => e.innerText.includes(arguments[0]));
            if (!item) return '';
            const state = item.querySelector('.message-status');
            return state && (/送达|已读/.test(state.innerText) ||
                /status-delivery|status-read/.test(state.className)) ? 'delivered' : 'visible';
        """, text) or ''

    def _send_greeting(self, tab, job):
        if not self._tab_has_job(tab, job.encrypt_job_id):
            return '未发送：聊天窗口岗位身份无法确认'
        if urlsplit(tab.url).path != '/web/geek/chat':
            for button in tab.eles('css:.btn-startchat, .btn-talk', timeout=0):
                link = button.attr('redirect-url') or ''
                destination = urljoin(tab.url, link)
                if (urlsplit(destination).hostname == 'www.zhipin.com'
                        and urlsplit(destination).path == '/web/geek/chat'
                        and extract_encrypt_job_id(destination) == job.encrypt_job_id):
                    tab.get(destination)
                    break
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if getattr(getattr(tab, 'states', None), 'is_loading', False):
                time.sleep(.2)
                continue
            if self._tab_has_job(tab, job.encrypt_job_id):
                editor = tab.ele('css:#chat-input[contenteditable="true"], .chat-input textarea, '
                                 'textarea.input-area, .chat-input [contenteditable="true"], '
                                 '.chat-editor[contenteditable="true"]', timeout=0)
                if editor and editor.states.is_displayed:
                    break
            time.sleep(.2)
        else:
            return '未发送：未确认当前岗位聊天输入框'
        draft = (editor.attr('value') if editor.tag == 'textarea' else editor.text) or ''
        if draft.strip() and draft.strip() != self.greeting:
            return '未发送：输入框已有草稿，保留原内容'
        if self._greeting_status(tab, self.greeting):
            return '已存在同样的招呼语，未重复发送'
        if draft.strip() != self.greeting:
            editor.input(self.greeting, clear=True)
        if not self._tab_has_job(tab, job.encrypt_job_id):
            return '未发送：输入过程中会话已切换'
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            button = tab.ele('css:button.btn-send, .chat-input button[type="submit"]', timeout=0)
            if (button and button.states.is_displayed
                    and 'disabled' not in str(button.attr('class') or '').split()
                    and button.attr('disabled') is None):
                break
            time.sleep(.1)
        else:
            return '未发送：发送按钮尚不可用'
        if not self._tab_has_job(tab, job.encrypt_job_id):
            return '未发送：会话已切换'
        tab.actions.move_to(button).click()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if not self._tab_has_job(tab, job.encrypt_job_id):
                break
            if self._greeting_status(tab, self.greeting) == 'delivered':
                return '招呼语已送达（当前岗位会话）'
            time.sleep(.2)
        return '已点击发送（当前岗位会话），平台送达回执待确认'

    def submit(self, job, match_score=0):
        self.last_reason, self.last_details, self._clicked = '', {}, False
        if self.max_attempts and self.attempt_count >= self.max_attempts:
            self.last_reason = '已达到本次沟通点击上限，包含结果待核实的尝试'
            return SubmitResult.RUN_LIMIT
        if self.get_today_count() >= self.daily_limit:
            return SubmitResult.DAILY_LIMIT
        job.encrypt_job_id = job.encrypt_job_id or extract_encrypt_job_id(job.url)
        if not job.encrypt_job_id or urlsplit(job.url).hostname not in ('www.zhipin.com', 'zhipin.com'):
            self.last_reason = '缺少有效的 BOSS 岗位链接'
            return SubmitResult.FAILED
        previous = self._state['jobs'].get(job_url_key(job.url), {}).get('status')
        if previous in (SubmitResult.SUCCESS, SubmitResult.ALREADY_APPLIED):
            return SubmitResult.ALREADY_APPLIED
        if previous in ('pending', SubmitResult.UNCONFIRMED):
            self.last_reason = '上次点击结果待核实，已跳过重复发送；请核对沟通记录'
            return SubmitResult.UNCONFIRMED
        tab, listening = None, False
        try:
            tab = self._get_detail_tab()
            if not self._open_job_detail(tab, job):
                self.last_reason = '无法确认目标岗位详情'
                return self._page_state(tab) or SubmitResult.FAILED
            deadline = time.monotonic() + self.timeout
            button = None
            while time.monotonic() < deadline:
                state = self._page_state(tab)
                if state:
                    if state == SubmitResult.ALREADY_APPLIED:
                        self._mark(job, state)
                    return state
                button = self._find_contact(tab)
                if button:
                    break
                time.sleep(.3)
            if not button:
                self.last_reason = '目标岗位未出现可用沟通按钮'
                return SubmitResult.FAILED
            tab.listen.start('/wapi/zpgeek/friend/add.json')
            enable_durable_responses(tab.listen)
            listening = True
            self._mark(job, 'pending', '即将点击沟通')
            actions = getattr(tab, 'actions', None)
            if actions is not None:
                # The live site prepares its chat action on mouse enter.
                actions.move_to(button).wait(.3)
            self._clicked = True
            self.attempt_count += 1
            clicked = actions.click() if actions is not None else button.click()
            if clicked is False:
                self._clicked = False
                self.last_reason = '浏览器未执行点击，保留岗位待重试'
                self._mark(job, SubmitResult.FAILED, self.last_reason)
                return SubmitResult.FAILED
            result = self._wait_confirmation(tab, job)
            if result == SubmitResult.SUCCESS:
                self._state['count'] += 1
                self._mark(job, result, self.last_reason)
                try:
                    safe_save_json({'date': self._state['date'], 'count': self._state['count']}, str(self._counter_file))
                except OSError:
                    logger.warning('兼容计数文件未写入，确认结果和计数已保存在 submission_state.json')
                self.last_details.update(contact='已确认发起沟通', attachment='未自动发送附件')
                if self.send_greeting and self.greeting:
                    try:
                        self.last_details['greeting'] = self._send_greeting(tab, job)
                    except Exception:
                        self.last_details['greeting'] = '沟通成功，自定义招呼发送失败'
                logger.info('已确认沟通成功 %s/%s', self.get_today_count(), self.daily_limit)
            else:
                self._mark(job, result, self.last_reason)
            return result
        except Exception as exc:
            result = SubmitResult.UNCONFIRMED if self._clicked else SubmitResult.FAILED
            self.last_reason = f'{type(exc).__name__}: {exc}'[:300]
            if self._clicked:
                self._mark(job, result, self.last_reason)
            logger.warning('投递异常: %s', self.last_reason)
            return result
        finally:
            if tab and listening:
                try:
                    tab.listen.stop()
                except Exception:
                    pass
