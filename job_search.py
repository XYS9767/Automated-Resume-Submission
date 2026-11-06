"""
岗位搜索模块 — DrissionPage 版本
"""

from __future__ import annotations

import re
import json
import time
import random
from urllib.parse import quote, urlencode, urlsplit, parse_qs
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from DrissionPage import Chromium
from access_safety import AccessGuard, AccessStopped
from utils import clean_text, random_delay, extract_salary_range, get_logger
from relevance import keyword_matches, contains_term, normalize, is_skill_label

if TYPE_CHECKING:
    from DrissionPage._pages.chromium_tab import ChromiumTab

logger = get_logger("jobs")

_JOB_URL_RE = re.compile(r'/job_detail/|jobId=|/web/geek/job')
_SALARY_RE = re.compile(r'\d+K', re.IGNORECASE)
_GARBAGE_RE = re.compile(
    r'热线|举报|ICP|营业执照|经营许可|公网安备|网安\d+号',
    re.IGNORECASE,
)
_SCHEDULE_RE = re.compile(r'^\d+天/周$|^\d+个月$|^\d+小时$')
_PHONE_RE = re.compile(r'^[\d\s\-()（）]+$')
_BOSS_RE = re.compile(r'(先生|女士|在线|离线|活跃|新职位|回复率)')
_BOSS_STATUS_LINES = frozenset({
    "在线", "离线", "刚刚活跃", "刚刚在线", "今日活跃",
    "3日内活跃", "本周活跃", "本月活跃", "近半年活跃",
})


def enable_durable_responses(listener):
    # DrissionPage 4.1.1.4 silently returns '' when getResponseBody loses a
    # renderer during navigation. Keep captured bodies outside the renderer.
    driver = getattr(listener, '_driver', None)
    if driver is not None:
        try:
            result = driver.run('Network.configureDurableMessages', maxTotalBufferSize=20000000)
            if isinstance(result, dict) and 'error' in result:
                driver.run('Network.enable', maxTotalBufferSize=20000000,
                           maxResourceBufferSize=5000000, enableDurableMessages=True)
        except Exception:
            logger.debug('当前浏览器不支持持久响应缓存，保留普通监听', exc_info=True)


def has_job_cards(tab) -> bool:
    """页面是否已出现岗位卡片/链接"""
    try:
        return bool(tab.run_js("""
            return !!(document.querySelector(
                '.job-card-wrapper, .job-card-box, li.job-card-box, '
                + 'a[href*="job_detail"], .job-list-box li, .rec-job-list li'
            ));
        """))
    except Exception:
        return False


def wait_for_job_cards(tab, timeout: float = 12.0) -> bool:
    """等待岗位列表渲染出来（加载中 ≠ 安全验证）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if has_job_cards(tab):
            return True
        if is_boss_security_page(tab, strict=True):
            return False
        try:
            tab.wait(0.8)
        except Exception:
            time.sleep(0.8)
    return has_job_cards(tab)


def is_boss_security_page(tab, strict: bool = False) -> bool:
    """Use the same visible-page evidence as the access guard."""
    from access_safety import page_access_status
    try:
        return bool(page_access_status(tab)[0])
    except Exception:
        return False


def wait_user_clear_security(tab, logger=None, prompt='', timeout=180, ready=None):
    log = logger or get_logger('jobs')
    log.info('等待网站登录/校验状态恢复，恢复后程序自动继续。' + prompt)
    notified = False
    deadline = time.monotonic() + timeout
    stable_since = None
    while time.monotonic() < deadline:
        try:
            usable = (not is_boss_security_page(tab, strict=True)
                      and is_boss_logged_in_visually(tab)
                      and (ready(tab) if ready is not None else True))
        except Exception:
            usable = False  # Browser navigation can invalidate its JS context briefly.
        if not usable and not notified and _needs_user_action(tab):
            log.warning('网站显示登录或验证码，请在浏览器完成这一项；搜索会由程序自动继续。')
            notified = True
        if usable:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= 2:
                return True
        else:
            stable_since = None
        time.sleep(.5)
    log.warning('网站登录/校验未在限时内恢复，已保留进度。')
    return False


def _needs_user_action(tab):
    """Loading/security redirects alone do not imply an interactive challenge."""
    try:
        path = urlsplit(tab.url).path
        if '/login' in path or ('/passport' in path and 'security' not in path):
            return True
        return bool(tab.run_js("""
            const visible = e => {
                const r = e.getBoundingClientRect();
                return r.width > 20 && r.height > 20 && getComputedStyle(e).visibility !== 'hidden';
            };
            return [...document.querySelectorAll('.geetest_panel, .nc-container, iframe[src*="captcha"]')].some(visible)
                || /请完成安全验证|拖动滑块|滑动验证|请完成验证后继续/.test(document.body?.innerText || '');
        """))
    except Exception:
        return False


def has_boss_auth_cookies(browser) -> bool:
    """检查 BOSS 登录 Cookie（wt2 / zp_at）"""
    try:
        cookies = browser.cookies(all_info=True)
    except TypeError:
        cookies = browser.cookies()
    except Exception:
        return False
    auth_names = {"wt2", "zp_at"}
    for c in cookies or []:
        domain = str(c.get("domain") or "")
        if "zhipin.com" not in domain:
            continue
        if c.get("name") in auth_names and c.get("value"):
            return True
    return False


def is_boss_logged_in_visually(tab, browser=None):
    """Require visible signed-in navigation; auth/security cookies alone are insufficient."""
    try:
        return bool(tab.run_js("""
            const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
            const users = [...document.querySelectorAll(
                'a[ka="header-my"], .nav-figure, .nav-user, .user-nav .avatar, .header-login .user-name'
            )];
            if (users.some(e => visible(e) && !/登录|注册/.test(e.innerText || ''))) return true;
            const nav = [...document.querySelectorAll('header a, #header a, .header a')].filter(visible);
            return nav.some(e => /消息/.test(e.innerText || '')) &&
                   nav.some(e => /简历/.test(e.innerText || '')) &&
                   !nav.some(e => (e.innerText || '').includes('登录'));
        """))
    except Exception:
        return False


def probe_boss_session(tab) -> str:
    """Inspect UI without issuing synthetic search requests."""
    if is_boss_security_page(tab, strict=True):
        return 'security'
    return 'ok' if is_boss_logged_in_visually(tab) else 'login'


def ensure_boss_api_ready(tab, logger=None, timeout=180):
    return probe_boss_session(tab) == 'ok' or wait_user_clear_security(tab, logger, timeout=timeout)


def verify_boss_session(tab):
    return probe_boss_session(tab) == 'ok'


def _is_salary(line: str) -> bool:
    return bool(_SALARY_RE.search(line)) or "元" in line

def _is_company(line: str) -> bool:
    if not line or len(line) < 2 or len(line) > 50:
        return False
    if _GARBAGE_RE.search(line): return False
    if _SCHEDULE_RE.match(line.strip()): return False
    if _PHONE_RE.match(line.strip()): return False
    if _is_salary(line): return False
    # HR名称模式：只有2-4字且包含"先生/女士/在线"
    if len(line) <= 4 and _BOSS_RE.search(line): return False
    return True

def _is_title(line: str) -> bool:
    if not line or len(line) < 2 or len(line) > 50:
        return False
    if line in _BOSS_STATUS_LINES:
        return False
    if _GARBAGE_RE.search(line):
        return False
    return True


def title_matches_keyword(title, keyword):
    return keyword_matches(title, keyword)


def _normalize_company(name: str) -> str:
    if not name:
        return ""
    for line in name.replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line or len(line) < 2:
            continue
        if _is_salary(line):
            continue
        if "·" in line and not any(k in line for k in ("公司", "有限", "集团", "股份", "企业")):
            continue
        if line in ("本科", "大专", "硕士", "博士", "学历不限", "经验不限"):
            continue
        return line[:50]
    return name.split("\n")[0].strip()[:50]


def _normalize_title(title: str) -> str:
    if not title:
        return ""
    for line in title.replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        if _is_salary(line):
            continue
        if line in ("本科", "大专", "硕士", "博士", "学历不限", "经验不限"):
            continue
        return line[:80]
    return title.split("\n")[0].strip()[:80]


def _sanitize_job(job: "JobPosting") -> None:
    job.company = _normalize_company(job.company)
    job.title = _normalize_title(job.title)
    job.url = _clean_job_url(job.url)


def _clean_job_url(url: str, keep_query: bool = True) -> str:
    """规范化岗位 URL。keep_query=True 保留 lid/sessionId（打开详情必需）。"""
    if not url:
        return ""
    url = url.strip()
    if not keep_query:
        url = url.split("?")[0].split("#")[0]
    else:
        url = url.split("#")[0]
    if url.startswith("/"):
        url = f"https://www.zhipin.com{url}"
    return url


def job_url_key(url):
    eid = extract_encrypt_job_id(url)
    return 'job:' + eid if eid else _clean_job_url(url, keep_query=False)


def extract_encrypt_job_id(url: str) -> str:
    """从 BOSS 岗位链接提取 encryptJobId"""
    if not url:
        return ""
    m = re.search(r"/job_detail/([^./?#]+)", url)
    if m:
        return m.group(1)
    params = parse_qs(urlsplit(url).query)
    return str((params.get('jobId') or params.get('encryptJobId') or [''])[0])


@dataclass
class JobPosting:
    title: str = ""
    company: str = ""
    salary: str = ""
    salary_min: float = 0.0
    salary_max: float = 0.0
    location: str = ""
    experience: str = ""
    education: str = ""
    tags: list[str] = field(default_factory=list)
    description: str = ""
    skills_required: list[str] = field(default_factory=list)
    url: str = ""
    company_size: str = ""
    company_industry: str = ""
    boss_name: str = ""
    boss_online: Optional[bool] = None
    boss_active_time: str = ""
    encrypt_job_id: str = ""
    _security_id: str = ""
    source_keyword: str = ""
    source: str = ""

    def to_dict(self) -> dict:
        return {
            "title": self.title, "company": self.company, "salary": self.salary,
            "salary_min": self.salary_min, "salary_max": self.salary_max,
            "location": self.location, "experience": self.experience,
            "education": self.education, "tags": self.tags,
            "description": self.description[:2000] if self.description else "",
            "skills_required": self.skills_required, "url": self.url,
            "company_size": self.company_size, "company_industry": self.company_industry,
            "boss_name": self.boss_name,
            "boss_online": self.boss_online,
            "boss_active_time": self.boss_active_time,
            "source_keyword": self.source_keyword, "source": self.source,
        }


class JobSearcher:

    TECH_KEYWORDS = [
        "C", "C++", "Python", "RTOS", "FreeRTOS", "Linux", "ARM", "STM32",
        "单片机", "嵌入式", "驱动", "I2C", "SPI", "UART", "CAN", "TCP/IP",
        "DSP", "FPGA", "ZigBee", "BLE", "Keil", "IAR", "GCC", "Git",
        "LLM", "大模型", "RAG", "Agent", "智能体", "AIGC", "LangChain", "LangGraph",
        "PyTorch", "TensorFlow", "FastAPI", "Django", "Java", "Spring", "Go", "React",
        "Vue", "JavaScript", "TypeScript", "MySQL", "Redis", "Docker", "Kubernetes",
        "Shell", "Bash", "Ansible", "Jenkins", "Prometheus", "Grafana", "Alertmanager",
        "Nginx", "Tomcat", "ELK", "Loki", "TiDB", "Ceph", "Terraform", "DevOps",
        "SRE", "K8s", "CentOS", "Ubuntu", "HAProxy", "Keepalived", "CI/CD",
        "运维", "运维开发", "自动化运维", "监控告警", "故障排查", "高可用",
    ]

    CITY_CODES = {
        "北京": "101010100", "上海": "101020100",
        "广州": "101280100", "深圳": "101280600",
        "杭州": "101210100", "成都": "101270100",
        "南京": "101190100", "武汉": "101200100",
        "西安": "101110100", "长沙": "101250100",
        "重庆": "101040100", "苏州": "101190400",
        "天津": "101030100", "青岛": "101120200",
        "济南": "101120100", "大连": "101070200",
        "宁波": "101210400", "厦门": "101230200",
    }

    EXPERIENCE_CODES = {
        "应届生": "102", "在校生": "108", "经验不限": "101",
        "1年以内": "103", "1-3年": "104",
        "3-5年": "105", "5-10年": "106", "10年以上": "107",
    }

    def __init__(self, browser: Chromium, config: dict):
        self.browser = browser
        self.cfg = config
        self.guard = AccessGuard(config)
        from evaluation_cache import EvaluationCache
        self.cache = EvaluationCache(config)
        search_cfg = config["search"]
        self.keywords = search_cfg.get("keywords", ["嵌入式"])
        self.city = search_cfg.get("city", "杭州")
        self.city_code = str(search_cfg.get('city_code') or self.CITY_CODES.get(self.city, self.city))
        if not self.city_code.isdigit():
            raise ValueError('未知城市，请配置 search.city_code（网站城市数字代码）: ' + str(self.city))
        self.experience = search_cfg.get("experience", "")
        self.exp_code = self.EXPERIENCE_CODES.get(self.experience, "")
        self.page_limit = search_cfg.get("page_limit", 5)
        self.jobs_per_page = search_cfg.get("jobs_per_page", 15)
        self.filter_cfg = config.get("filter", {})
        self._detail_tab = None
        self.capture_status = ''
        self.capture_reason = ''
        self.capture_has_more = None
        self.capture_page = 0
        self.partial_jobs = []

    def build_search_url(self, keyword):
        params = {'query': keyword, 'city': self.city_code}
        if self.exp_code:
            params['experience'] = self.exp_code
        return 'https://www.zhipin.com' + self.cfg.get('search', {}).get('path', '/web/geek/jobs') + '?' + urlencode(params)

    def close_detail_tab(self):
        if self._detail_tab:
            try:
                self._detail_tab.close()
            except Exception:
                pass
            self._detail_tab = None

    def ensure_detail_tab(self):
        """获取/创建专用详情标签页（与搜索页隔离，避免首页跳转失败）"""
        if self._detail_tab is not None:
            try:
                _ = self._detail_tab.url
            except Exception:
                self._detail_tab = None
        if self._detail_tab is None:
            self._detail_tab = self.browser.new_tab("about:blank")
        return self._detail_tab

    def ensure_boss_search_context(self, tab):
        """搜索/投递 API 前尽量处于 geek 页。风控/验证时绝不自动跳转。"""
        if is_boss_security_page(tab, strict=True):
            return
        try:
            url = (tab.url or "").lower()
            # 首页或已有卡片时不要强跳 /web/geek/jobs（会触发 security.html）
            if "web/geek" in url or has_job_cards(tab) or "zhipin.com" in url:
                return
        except Exception:
            pass

    def verify_session(self, tab) -> bool:
        return verify_boss_session(tab)

    def _scroll_to_bottom(self, tab):
        tab.run_js("""
            const nodes = [...document.querySelectorAll(
                '.job-list-box, .job-list-container, .search-job-result, .job-list, main'
            )];
            let scrolled = false;
            for (const node of nodes) {
                for (let p = node; p && p !== document.body; p = p.parentElement) {
                    if (p.scrollHeight > p.clientHeight + 5 && /auto|scroll/.test(getComputedStyle(p).overflowY)) {
                        p.scrollTop = p.scrollHeight; scrolled = true; break;
                    }
                }
            }
            if (!scrolled) window.scrollTo(0, document.documentElement.scrollHeight);
        """)

    def advance_page(self, tab):
        self.guard.before("search_page", tab)
        for selector in ('css:.options-pages a.next', 'css:.pagination .next',
                         'css:button[aria-label="下一页"]', 'css:a[rel="next"]'):
            button = tab.ele(selector, timeout=0)
            if button and button.states.is_displayed:
                classes = str(button.attr('class') or '')
                if 'disabled' in classes or button.attr('disabled') is not None or button.attr('aria-disabled') == 'true':
                    return False
                button.click()
                return True
        self._scroll_to_bottom(tab)
        return True

    def start_capture(self, tab):
        self.capture_status = ''
        self.capture_reason = ''
        self.capture_has_more = None
        self.capture_page = 0
        self.partial_jobs = []
        tab.listen.start(['/wapi/zpgeek/search/', '/wapi/zpgeek/pc/search/'])
        enable_durable_responses(tab.listen)

    @staticmethod
    def _packet_params(packet):
        params = {k: v[-1] for k, v in parse_qs(urlsplit(packet.url).query).items()}
        post = getattr(packet.request, 'postData', None)
        if isinstance(post, str) and post:
            try:
                post = json.loads(post)
            except ValueError:
                post = {k: v[-1] for k, v in parse_qs(post).items()}
        if isinstance(post, dict):
            params.update(post)
        return params

    def wait_capture(self, tab, keyword, timeout=None, stop=False):
        timeout = self.cfg.get('search', {}).get('capture_timeout', 8) if timeout is None else timeout
        self.capture_status = 'timeout'
        self.capture_reason = '未在限时内收到匹配关键词与城市的搜索响应'
        deadline = time.monotonic() + timeout
        jobs = {}
        try:
            while time.monotonic() < deadline:
                packet = tab.listen.wait(timeout=max(.05, min(deadline-time.monotonic(), .5 if jobs else 2)), raise_err=False)
                if not packet or isinstance(packet, bool):
                    if jobs:
                        break
                    continue
                for item in packet if isinstance(packet, list) else [packet]:
                    try:
                        params = self._packet_params(item)
                        if normalize(params.get('query')) != normalize(keyword):
                            continue
                        if str(params.get('city', '')) != str(self.city_code):
                            continue
                        body = item.response.body
                        if isinstance(body, bytes):
                            body = body.decode('utf-8')
                        if body in ('', None):
                            self.capture_status = 'unavailable'
                            self.capture_reason = '无法取得搜索响应正文；不能据此认定没有岗位'
                            continue
                        try:
                            data = json.loads(body) if isinstance(body, str) else body
                        except ValueError:
                            self.capture_status = 'invalid_response'
                            self.capture_reason = '搜索响应不是有效 JSON'
                            continue
                        if not isinstance(data, dict):
                            self.capture_status = 'invalid_response'
                            self.capture_reason = '搜索响应结构异常'
                            continue
                        code = str(data.get('code', ''))
                        message = str(data.get('message') or data.get('msg') or '')
                        if code == '37' or any(x in message for x in ('频繁', '限制访问', '异常行为', '安全验证')):
                            self.guard.stop('搜索接口提示访问异常或安全验证，停止本次运行；请手动核实后重新启动。')
                        if code != '0':
                            self.capture_status = {'37': 'security', '7': 'login', '101': 'login'}.get(code, 'error')
                            self.capture_reason = '搜索接口返回 code=' + code
                            if code == '37':
                                self.capture_reason += '（网站判定环境异常）'
                            logger.warning(self.capture_reason)
                            return list(jobs.values())
                        zp = data.get('zpData') or {}
                        rows = zp.get('jobList')
                        if not isinstance(rows, list):
                            continue
                        self.capture_status = 'ok'
                        self.capture_reason = ''
                        page = int(params.get('page') or 1)
                        if page >= self.capture_page:
                            self.capture_page = page
                            more = zp.get('hasMore')
                            self.capture_has_more = more if isinstance(more, bool) else None
                        for job in self._jobs_from_list(rows, keyword, page):
                            jobs[job_url_key(job.url)] = job
                    except (ValueError, TypeError, AttributeError):
                        logger.debug('忽略无法解析的搜索响应', exc_info=True)
                if self.capture_status == 'ok' and not jobs:
                    break
        finally:
            if stop:
                tab.listen.stop()
        return list(jobs.values())

    def collect_search_results(self, tab, keyword, trigger):
        results = {}
        self.start_capture(tab)
        empty_rounds = 0
        try:
            self.guard.before("search", tab)
            if not trigger(tab, keyword, self):
                status, reason = self.capture_status, self.capture_reason
                # A search response may arrive just before a redirect to home/login.
                self.wait_capture(tab, keyword, timeout=2)
                if self.capture_status not in ('security', 'login', 'error'):
                    self.capture_status, self.capture_reason = status, reason
                return []
            self.guard.inspect(tab)
            for page in range(self.page_limit):
                # Read the captured response before inspecting a redirected page.
                batch = self.wait_capture(tab, keyword)
                if self.capture_status in ('security', 'login', 'error'):
                    break
                if is_boss_security_page(tab, strict=True):
                    self.capture_status = 'security'
                    self.capture_reason = '搜索进入网站校验页面'
                    break
                if not batch and self.capture_status != 'ok' and self._search_context_matches(tab, keyword, check_input=True):
                    batch = self._parse_page(tab)
                    for job in batch:
                        job.source_keyword, job.source = keyword, 'dom'
                    if batch:
                        self.capture_status, self.capture_reason = 'ok', ''
                added = 0
                for job in batch:
                    key = job_url_key(job.url)
                    if not extract_encrypt_job_id(job.url) or key in results:
                        continue
                    results[key] = job
                    added += 1
                self.partial_jobs = list(results.values())
                logger.info('搜索「%s」第%d轮: +%d，累计%d', keyword, page+1, added, len(results))
                empty_rounds = 0 if added else empty_rounds + 1
                if self.capture_has_more is False or empty_rounds >= 2 or page+1 >= self.page_limit:
                    break
                if not self.advance_page(tab):
                    break
            return list(results.values())
        finally:
            tab.listen.stop()

    def search_with_recovery(self, tab, keyword, trigger):
        """Retry the configured search once after recovery, including return to home.

        The user never needs to type the keyword or navigate back to search.
        Actual login/captcha interactions remain in the website's own UI.
        """
        results = {}
        for attempt in range(2):
            batch = self.collect_search_results(tab, keyword, trigger)
            results.update((job_url_key(job.url), job) for job in batch)
            if self.capture_status == 'ok':
                break
            if attempt == 1 or self.capture_status == 'error':
                break
            logger.info('搜索尚未完成：%s', self.capture_reason or self.capture_status)
            if self.capture_status in ('security', 'login'):
                self.guard.stop('搜索遇到登录或安全验证，停止本次运行；请手动核实后重新启动。')
            else:
                time.sleep(self.cfg.get('runtime', {}).get('search_retry_delay', 5))
            logger.info('自动重新搜索「%s」，城市 %s（恢复重试 1/1）', keyword, self.city)
        return list(results.values())

    def _search_context_matches(self, tab, keyword, check_input=False):
        params = parse_qs(urlsplit(tab.url).query)
        valid = (normalize((params.get('query') or [''])[0]) == normalize(keyword)
                 and (params.get('city') or [''])[0] == str(self.city_code)
                 and urlsplit(tab.url).path.rstrip('/') in ('/web/geek/job', '/web/geek/jobs'))
        if valid and check_input:
            value = tab.run_js("""
                const input = [...document.querySelectorAll(
                    'input[placeholder*="搜索"], input[placeholder*="职位"], input.search-input, .search-input input'
                )].find(e => e.getClientRects().length);
                return input ? input.value : null;
            """)
            return value is not None and normalize(value) == normalize(keyword)
        return valid

    def _jobs_from_list(self, jl: list, keyword: str, page: int) -> list[JobPosting]:
        jobs = []
        for j in (jl or []):
            if not isinstance(j, dict):
                continue
            job = JobPosting()
            job.title = j.get("jobName", "")
            job.company = j.get("brandName", "")
            job.salary = j.get("salaryDesc", "")
            job.salary_min, job.salary_max = extract_salary_range(job.salary)
            job.location = f"{j.get('cityName','')} {j.get('areaDistrict','')}".strip()
            job.experience = j.get("jobExperience", "")
            job.education = j.get("jobDegree", "")
            job.boss_name = j.get("bossName", "")
            if "bossOnline" in j:
                job.boss_online = j.get("bossOnline") in (True, 1, "true", "1") if j.get("bossOnline") is not None else None
            job.company_size = j.get("brandScaleName", "")
            job.company_industry = j.get("brandIndustry", "")
            job.description = j.get("itemDescription", "") or ""
            eid = j.get("encryptJobId") or j.get("jobId") or ""
            job.encrypt_job_id = eid
            job._security_id = j.get("securityId") or ""
            job.source_keyword, job.source = keyword, "network"
            job.boss_active_time = str(j.get("bossActiveTime") or j.get("activeTimeDesc") or "")
            if not eid:
                continue
            query = {k: j[k] for k in ('lid', 'securityId') if j.get(k)}
            link = j.get('jobUrl') or f'https://www.zhipin.com/job_detail/{eid}.html'
            job.url = _clean_job_url(link)
            if query and '?' not in job.url:
                job.url += '?' + urlencode(query)
            tags = []
            for values in (j.get('jobLabels'), j.get('skills')):
                if isinstance(values, str):
                    tags.append(values)
                elif isinstance(values, list):
                    tags.extend(values)
            job.tags = [str(t) for t in tags if isinstance(t, (str, int))]
            job.skills_required = list(dict.fromkeys(
                [tag for tag in job.tags if is_skill_label(tag)]
                + self._extract_skills(job.title + ' ' + job.description)))
            if job.title:
                _sanitize_job(job)
                jobs.append(job)
        logger.info(f"  API p{page}: {len(jobs)}个 | 首岗={jobs[0].title[:25] if jobs else '?'}")
        return jobs

    def _parse_page(self, tab):
        """Read structured card fields in one browser call; never guess company from salary lines."""
        rows = tab.run_js("""
            const cards = [...document.querySelectorAll('.job-card-wrapper, .job-card-box')];
            const text = (card, selector) => (card.querySelector(selector)?.innerText || '').trim();
            return cards.filter(c => c.getClientRects().length).map(card => ({
                url: card.querySelector('a[href*="job_detail"]')?.href ||
                     (card.matches('a[href*="job_detail"]') ? card.href : ''),
                title: text(card, '.job-name, .job-title, .job-info .name'),
                company: text(card, '.company-name, .company-text .name, .company-info .name'),
                salary: text(card, '.salary, .job-salary'),
                location: text(card, '.job-area, .job-location, .job-area-wrapper'),
                tags: [...card.querySelectorAll('.tag-list li, .job-info .tag, .job-card-footer .tag')]
                    .map(e => e.innerText.trim()).filter(Boolean)
            }));
        """) or []
        jobs = {}
        for row in rows:
            if not isinstance(row, dict) or not row.get('title') or not extract_encrypt_job_id(row.get('url', '')):
                continue
            job = JobPosting(title=row['title'], company=row.get('company') or '未知公司',
                             url=_clean_job_url(row['url']), salary=row.get('salary', ''),
                             location=row.get('location', ''), tags=row.get('tags', []))
            job.encrypt_job_id = extract_encrypt_job_id(job.url)
            job.salary_min, job.salary_max = extract_salary_range(job.salary)
            job.experience = next((t for t in job.tags if re.search(r'年|应届|在校|经验', t)), '')
            job.education = next((t for t in job.tags if re.search(r'本科|大专|硕士|博士|高中|中专|学历', t)), '')
            job.skills_required = self._extract_skills(job.title + ' ' + ' '.join(job.tags))
            jobs[job_url_key(job.url)] = job
        return list(jobs.values())

    def _parse_boss_status(self, tab) -> tuple[Optional[bool], str]:
        """从当前详情页 DOM 读取 BOSS 在线/活跃状态"""
        online: Optional[bool] = None
        active_time = ""

        for sel in (".boss-online-tag", "css:.boss-online-tag"):
            try:
                el = tab.ele(sel, timeout=1)
                if el and "在线" in (el.text or ""):
                    online = True
                    active_time = "在线"
                    break
            except Exception:
                continue

        for sel in (".boss-active-time", "css:.boss-active-time"):
            try:
                el = tab.ele(sel, timeout=1)
                if el:
                    t = (el.text or "").strip()
                    if t:
                        active_time = t
                        break
            except Exception:
                continue

        return online, active_time

    def fetch_job_description(self, tab, job: JobPosting) -> str:
        """打开职位详情页，抓取职位描述与 BOSS 活跃状态"""
        return self.fetch_job_detail(tab, job, need_description=True, need_boss_status=True)

    def _page_is_job_detail(self, page_tab, job):
        if is_boss_security_page(page_tab, strict=True):
            return False
        expected = job.encrypt_job_id or extract_encrypt_job_id(job.url)
        return bool(expected and extract_encrypt_job_id(page_tab.url) == expected)

    def fetch_job_detail(
        self,
        tab,
        job: JobPosting,
        need_description: bool = True,
        need_boss_status: bool = True,
    ) -> str:
        """打开职位详情页，按需抓取描述与 BOSS 状态"""
        if self.cache.restore_detail(job):
            return job.description
        has_desc = bool(job.description and len(job.description) >= 30)
        has_boss = (
            job.boss_online is True
            or bool(job.boss_active_time)
        )
        if (not need_description or has_desc) and (not need_boss_status or has_boss):
            return job.description if has_desc else ""

        job_url = _clean_job_url(job.url)
        if not job_url and not job.encrypt_job_id:
            return ""

        detail_tab = None
        own_tab = False
        page_for_parse = None
        try:
            detail_tab = self.ensure_detail_tab()
            self.guard.inspect(detail_tab)
            if not self._page_is_job_detail(detail_tab, job):
                self.guard.before("detail", detail_tab)
                detail_tab.get(job_url)
                detail_tab.wait(1)
            self.guard.inspect(detail_tab)
            if not self._page_is_job_detail(detail_tab, job):
                self.guard.detail_failed()
                job.detail_unverified = True
                logger.warning('详情身份未确认，保留列表资料: %s', job.title)
                return job.description if has_desc else ''
            job.detail_unverified = False
            page_for_parse = detail_tab

            if need_boss_status and page_for_parse:
                online, active_time = self._parse_boss_status(page_for_parse)
                if online is True:
                    job.boss_online = True
                if active_time:
                    job.boss_active_time = active_time

            page = page_for_parse
            if not need_description or has_desc:
                self.guard.detail_ok()
                if job.boss_active_time or job.boss_online is True:
                    logger.info(
                        f"  👤 {job.title[:20]} BOSS="
                        f"{'在线' if job.boss_online else job.boss_active_time or '未知'}"
                    )
                return job.description

            desc_selectors = [
                "div.job-sec-text", "div.job-detail", ".job-detail-section",
                "div[class*='job-detail'] div[class*='text']",
                ".detail-section", "div.detail-content",
                ".job-detail-body", ".job-detail-info",
            ]
            desc = ""
            for sel in desc_selectors:
                try:
                    el = page.ele("css:" + sel, timeout=0)
                    if el:
                        desc = (el.text or "").strip()
                        if len(desc) >= 50:
                            break
                        desc = ""
                except Exception:
                    continue

            if not desc or len(desc) < 30:
                try:
                    desc = page.run_js("""
                        var nodes = document.querySelectorAll(
                            '.job-sec-text, .job-detail, [class*="job-detail"]'
                        );
                        var best = '';
                        nodes.forEach(function(n) {
                            var t = (n.innerText || '').trim();
                            if (t.length > best.length) best = t;
                        });
                        return best;
                    """) or ""
                    desc = desc.strip()
                except Exception:
                    pass

            if not desc or len(desc) < 30:
                try:
                    full_text = page.ele("tag:body", timeout=2)
                    if full_text:
                        lines = (full_text.text or "").split("\n")
                        capture = False
                        buf = []
                        for line in lines:
                            s = line.strip()
                            if any(w in s for w in ("职位描述", "岗位职责", "任职要求", "工作内容", "岗位要求")):
                                capture = True
                                continue
                            if capture and s:
                                if any(w in s for w in ("公司介绍", "工作地址", "工商信息", "职位发布者")):
                                    break
                                buf.append(s)
                        desc = "\n".join(buf) if buf else ""
                except Exception:
                    pass

            if desc and len(desc) >= 30:
                self.guard.detail_ok()
                job.description = desc
                self.cache.save_detail(job)
                logger.info(f"  📝 {job.title[:20]} ({len(desc)}字)")
            else:
                job.detail_unverified = True
                self.guard.detail_failed()
            return desc if desc and len(desc) >= 30 else ""
        except AccessStopped:
            raise
        except Exception as e:
            self.guard.detail_failed()
            job.detail_unverified = True
            logger.warning(f"  详情获取失败 {job.title[:20]}: {e}")
            return ""
        finally:
            if own_tab and detail_tab:
                try:
                    detail_tab.close()
                except Exception:
                    pass

    def batch_fetch_descriptions(self, tab, jobs: list[JobPosting], limit: int = 50):
        """批量获取职位描述：逐个打开详情页提取 DOM 内容"""
        need = sum(1 for j in jobs if not j.description or len(j.description) < 30)
        target = min(need, limit)
        fetched = 0
        for i, job in enumerate(jobs, 1):
            if not job.description or len(job.description) < 30:
                if self.fetch_job_description(tab, job):
                    fetched += 1
                    if fetched % 10 == 0:
                        logger.info(f"  描述获取进度: {fetched}/{target}")
                    if fetched >= limit:
                        break
                time.sleep(random.uniform(0.8, 1.5))  # 避免操作过快
        if fetched:
            logger.info(f"  批量获取描述完成: {fetched} 个")

    def _extract_skills(self, text: str) -> list[str]:
        if not text: return []
        lower = text.lower()
        return [s for s in self.TECH_KEYWORDS if contains_term(lower, s)]
