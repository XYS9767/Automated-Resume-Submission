"""
岗位搜索模块 — DrissionPage 版本
"""

from __future__ import annotations

import re
import json
import time
import random
from urllib.parse import quote
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from DrissionPage import Chromium
from utils import clean_text, random_delay, extract_salary_range, get_logger

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
    """是否处于 BOSS 安全验证页。

    注意：普通职位页正文也可能出现「验证码」字样，不能单靠文案误判。
    strict=True 时只认 URL / 明确验证控件；默认也会识别明显滑块文案。
    """
    try:
        url = (tab.url or "").lower()
        # 明确风控 URL（最可靠）
        if any(k in url for k in (
            "security.html", "_security_check=",
            "/web/passport/zp/security", "/captcha", "antispam",
        )):
            return True

        # 已有岗位卡片 → 绝不是验证页
        if has_job_cards(tab):
            return False

        # 首页/城市站不要当成验证页
        if "web/geek" not in url and "security" not in url and "passport" not in url:
            return False

        body = tab.run_js(
            "return document.body? document.body.innerText.slice(0,2500) : ''"
        ) or ""

        # 明确滑块/人机验证界面（需同时像验证页，避免误伤）
        hard_markers = ("请完成安全验证", "滑动验证", "拖动滑块", "人机验证", "请完成验证后继续")
        if any(k in body for k in hard_markers):
            return True

        if strict:
            return False

        # 职位页长时间骨架且无卡片：可能是风控，也可能只是慢加载
        # 这里返回 False，由 wait_for_job_cards / 用户确认处理
        return False
    except Exception:
        return False


def wait_user_clear_security(tab, logger=None, prompt: str = "") -> bool:
    """暂停等待用户完成安全验证。返回是否可继续搜索/投递。"""
    log = logger or get_logger("jobs")
    for round_i in range(5):
        on_security = is_boss_security_page(tab, strict=True)
        print()
        log.warning("=" * 55)
        if on_security:
            log.warning("  ⚠️ 当前在 BOSS 安全验证页 (security.html)")
            log.warning("     请在浏览器完成滑块/验证码，等页面自动跳走")
            log.warning("     跳到职位列表后，再回终端按 Enter")
            log.warning("     （此时输入 y 无效，必须先离开验证页）")
        else:
            log.warning("  ⚠️ 需要你在浏览器准备好职位列表")
            log.warning("     1. 打开 BOSS「职位」页")
            log.warning("     2. 手动搜索目标关键词，等到左侧出现岗位")
            log.warning("     3. 回车继续；确认有列表可输入 y")
        if prompt:
            log.warning(f"     {prompt}")
        log.warning("=" * 55)
        try:
            ans = (input("  继续? [Enter/y] ").strip().lower())
        except EOFError:
            ans = ""
        try:
            tab.wait(1)
        except Exception:
            pass

        if is_boss_security_page(tab, strict=True):
            log.warning("  ⚠️ 仍在验证页，请先完成验证再按 Enter")
            continue
        if has_job_cards(tab) or wait_for_job_cards(tab, timeout=6):
            log.info("  ✅ 已检测到岗位列表")
            return True
        if ans in ("y", "yes", "强制", "1"):
            log.info("  ✅ 用户确认继续（将使用当前页面）")
            return True
        if round_i < 4:
            log.info("  未检测到岗位列表，请在浏览器操作后再试...")
    return has_job_cards(tab)


def has_boss_auth_cookies(browser) -> bool:
    """检查 BOSS 登录 Cookie（wt2 / zp_at）"""
    try:
        cookies = browser.cookies(all_domains=True)
    except TypeError:
        cookies = browser.cookies()
    except Exception:
        return False
    auth_names = {"wt2", "zp_at", "__zp_stoken__"}
    for c in cookies or []:
        domain = str(c.get("domain") or "")
        if "zhipin.com" not in domain:
            continue
        if c.get("name") in auth_names and c.get("value"):
            return True
    return False


def is_boss_logged_in_visually(tab, browser=None) -> bool:
    """通过页面 UI / Cookie 判断求职者是否已登录（不依赖 API）"""
    if browser and has_boss_auth_cookies(browser):
        return True
    try:
        hit = tab.run_js("""
            const body = document.body;
            if (!body) return false;
            const text = body.innerText || '';
            // 求职者登录后顶部有「消息」「简历」
            if (text.includes('消息') && text.includes('简历')) return true;
            const sels = [
                'a[ka="header-my"]', '.nav-user', '.user-nav',
                '.header-login .user-name', '.label-text .user-name',
            ];
            for (const s of sels) {
                if (document.querySelector(s)) return true;
            }
            return false;
        """)
        return bool(hit)
    except Exception:
        return False


def probe_boss_session(tab) -> str:
    """探测 BOSS 会话：ok / security / login / error"""
    js = (
        "fetch('https://www.zhipin.com/wapi/zpgeek/search/joblist.json?"
        "scene=1&query=test&city=101210100&page=1&pageSize=1',"
        "{headers:{'x-requested-with':'XMLHttpRequest'}})"
        ".then(r=>r.text()).catch(e=>'ERR:'+e.message)"
    )
    try:
        resp = tab.run_cdp(
            "Runtime.evaluate",
            expression=js,
            awaitPromise=True,
            returnByValue=True,
        )
        raw = (resp.get("result") or {}).get("value") or ""
        if not isinstance(raw, str) or not raw.startswith("{"):
            return "error"
        data = json.loads(raw)
        code = data.get("code", -1)
        if code == 0:
            return "ok"
        if code == 37:
            return "security"
        if code in (7, 101):
            return "login"
        return "error"
    except Exception:
        return "error"


def ensure_boss_api_ready(tab, logger=None, timeout: int = 180) -> bool:
    """确保 BOSS API 可用。code=37 时引导用户完成验证并轮询。"""
    log = logger or get_logger("jobs")
    st = probe_boss_session(tab)
    if st == "ok":
        log.info("✅ BOSS API 可用")
        return True

    log.warning("=" * 55)
    log.warning("  ⚠️ BOSS 返回「环境存在异常」(code=37)，无法自动开聊/打开详情")
    log.warning("     这是账号/浏览器风控，不是岗位选择器坏了")
    log.warning("     请按下面做（必须做完 API 通过才能投递）：")
    log.warning("     1. 看 Edge 是否有滑块/验证码 → 完成")
    log.warning("     2. 或手动点顶部「职位」，完成验证直到能正常搜岗位")
    log.warning("     3. 验证成功后回到终端按 Enter，程序会自动检测")
    log.warning("=" * 55)

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            # 非阻塞：若用户已在操作，每 3 秒探测一次
            st = probe_boss_session(tab)
            if st == "ok":
                log.info("✅ BOSS API 已恢复，可以自动投递")
                return True
            # 去掉 URL 上残留的 _security_check 再试一次加载
            url = (tab.url or "")
            if "_security_check=" in url.lower():
                try:
                    clean = url.split("?")[0]
                    if clean:
                        tab.get(clean)
                        tab.wait(2)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            ans = input("  API 未通过，完成验证后按 Enter 重试，输入 q 放弃: ").strip().lower()
        except EOFError:
            ans = ""
        if ans == "q":
            return False
        st = probe_boss_session(tab)
        if st == "ok":
            log.info("✅ BOSS API 已恢复，可以自动投递")
            return True
        log.warning(f"  仍未通过 (api={st})，请继续在浏览器完成验证...")

    log.error("❌ 等待超时：API 仍不可用，自动投递无法进行")
    return False


def verify_boss_session(tab) -> bool:
    """用搜索 API 探测 BOSS 是否已登录且通过安全验证"""
    status = probe_boss_session(tab)
    if status == "ok":
        return True
    if status == "security":
        logger.warning("  ⚠️ BOSS 需要安全验证（请在浏览器完成滑块/验证码）")
    elif status == "login":
        logger.warning("  ⚠️ BOSS 未登录或会话过期")
    return False


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


def title_matches_keyword(title: str, keyword: str) -> bool:
    """判断岗位标题是否与搜索关键词相关（DOM 兜底时用）"""
    if not title or not keyword:
        return True
    t = title.lower()
    k = keyword.lower()
    if k in t or t in k:
        return True
    parts = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9]{2,}", k)
    return any(p.lower() in t for p in parts)


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


def job_url_key(url: str) -> str:
    """去重用的岗位 URL 主键（去掉 query）"""
    return _clean_job_url(url, keep_query=False)


def extract_encrypt_job_id(url: str) -> str:
    """从 BOSS 岗位链接提取 encryptJobId"""
    if not url:
        return ""
    m = re.search(r"/job_detail/([^./?#]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]jobId=([^&]+)", url)
    return m.group(1) if m else ""


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
        }


class JobSearcher:

    TECH_KEYWORDS = [
        "C", "C++", "Python", "RTOS", "FreeRTOS", "Linux", "ARM", "STM32",
        "单片机", "嵌入式", "驱动", "I2C", "SPI", "UART", "CAN", "TCP/IP",
        "DSP", "FPGA", "ZigBee", "BLE", "Keil", "IAR", "GCC", "Git",
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
        search_cfg = config["search"]
        self.keywords = search_cfg.get("keywords", ["嵌入式"])
        self.city = search_cfg.get("city", "杭州")
        self.city_code = self.CITY_CODES.get(self.city, self.city)
        self.experience = search_cfg.get("experience", "")
        self.exp_code = self.EXPERIENCE_CODES.get(self.experience, "")
        self.page_limit = search_cfg.get("page_limit", 5)
        self.jobs_per_page = search_cfg.get("jobs_per_page", 15)
        self.filter_cfg = config.get("filter", {})
        self._detail_tab = None

    def close_detail_tab(self):
        if self._detail_tab:
            try:
                self._detail_tab.close()
            except Exception:
                pass
            self._detail_tab = None

    def ensure_detail_tab(self):
        """获取/创建专用详情标签页（与搜索页隔离，避免首页跳转失败）"""
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

    def fetch_jobs_cdp(self, tab, keyword: str, max_pages: int | None = None) -> list[JobPosting]:
        """在已登录 tab 内用 CDP fetch 拉取搜索 API（比 listen/DOM 更可靠）"""
        self.ensure_boss_search_context(tab)
        self._seen_404 = False
        limit = max_pages or self.page_limit
        all_jobs: list[JobPosting] = []
        seen: set[str] = set()
        security_retried = False
        for page in range(1, limit + 1):
            batch = []
            try:
                batch = self._fetch_api(keyword, page, tab)
            except RuntimeError:
                if not security_retried:
                    security_retried = True
                    logger.warning("  ⚠️ BOSS 安全验证：请在浏览器完成滑块/验证码")
                    logger.info("  验证成功后按 Enter（程序不会自动跳转页面）")
                    try:
                        input()
                    except EOFError:
                        pass
                    try:
                        batch = self._fetch_api(keyword, page, tab)
                    except RuntimeError:
                        break
                else:
                    break
            else:
                security_retried = False
            if not batch:
                break
            for job in batch:
                if job.url and job.url not in seen:
                    seen.add(job.url)
                    _sanitize_job(job)
                    all_jobs.append(job)
            if len(batch) < 15:
                break
            time.sleep(random.uniform(0.3, 0.8))
        if all_jobs:
            logger.info(
                f"  📡 CDP API: {len(all_jobs)}个 | "
                f"首岗={all_jobs[0].title[:25]}"
            )
        return all_jobs

    # ==================== 卡片查找 ====================

    def _find_card_elements(self, tab) -> list:
        """查找岗位卡片容器：优先新版布局选择器，回退到链接向上查找"""
        card_selectors = (
            ".job-card-wrapper",
            ".job-card-box",
            "li.job-card-box",
            ".rec-job-list li",
            ".job-list-box .job-card-left",
            "div[class*='job-card-wrapper']",
            "div[class*='job-card-box']",
        )
        for sel in card_selectors:
            try:
                cards = tab.eles(sel)
                if cards:
                    logger.info(f"  定位到 {len(cards)} 个卡片 ({sel})")
                    return cards
            except Exception:
                continue

        card_set: set[int] = set()
        results: list = []

        all_links = tab.eles("tag:a")
        if not all_links:
            all_links = tab.eles("a")
        if not all_links:
            return results

        for a in all_links:
            try:
                href = a.link
            except Exception:
                continue
            if not href or not _JOB_URL_RE.search(str(href)):
                continue
            if "/job_detail/" not in str(href):
                continue

            for level in (2, 3, 4, 5):
                try:
                    card = a.parent(level)
                    if card is None:
                        continue
                    cid = id(card)
                    if cid in card_set:
                        continue
                    txt = card.text or ""
                    if len(txt) < 15 or len(txt) > 2000:
                        continue
                    if _GARBAGE_RE.search(txt[:200]):
                        continue
                    card_set.add(cid)
                    results.append(card)
                    break
                except Exception:
                    continue

        if results:
            logger.info(f"  定位到 {len(results)} 个独立卡片")
        else:
            logger.warning("  页面未找到岗位卡片")
        return results

    # ==================== API 获取（CDP fetch） ====================

    _seen_404 = False

    def _fetch_api(self, keyword: str, page: int, tab) -> list[JobPosting]:
        """CDP Runtime.evaluate + awaitPromise：在已登录搜索 tab 内执行 fetch()

        关键：fetch() 在页面原生 JS 上下文中运行，100% 继承页面 Cookie/鉴权。
        不创建新 tab，不会弹出 JSON 页面。
        """
        import json as _json
        from urllib.parse import urlencode, quote as _q

        params = {"scene": "1", "query": keyword, "city": self.city_code,
                  "page": page, "pageSize": 30}
        if self.exp_code:
            params["experience"] = self.exp_code
        qs = urlencode(params, quote_via=_q)
        api_url = f"https://www.zhipin.com/wapi/zpgeek/search/joblist.json?{qs}"

        js_code = (
            f"fetch('{api_url}', {{headers: {{'x-requested-with': 'XMLHttpRequest'}}}})"
            f".then(r => {{ if (!r.ok) return 'ERR:' + r.status; return r.text(); }})"
            f".catch(e => 'ERR:' + e.message)"
        )

        try:
            resp = tab.run_cdp("Runtime.evaluate",
                expression=js_code, awaitPromise=True, returnByValue=True)
        except Exception as e:
            logger.warning(f"  CDP fetch p{page}: {e}")
            return []

        result = resp.get("result", {})
        if result.get("subtype") == "error":
            logger.warning(f"  CDP fetch p{page} error: {result.get('description','?')[:80]}")
            return []

        raw = result.get("value", "") or ""
        if not isinstance(raw, str) or not raw.startswith("{"):
            if raw and page == 1:
                logger.warning(f"  CDP 搜索非JSON: {str(raw)[:100]}")
            return []

        try:
            data = _json.loads(raw)
        except Exception:
            return []

        code = data.get("code", -1)
        if code == 37:
            if not self._seen_404:
                self._seen_404 = True
            else:
                raise RuntimeError
        if code != 0:
            if code not in (37,):
                logger.warning(f"  搜索API code={code} msg={data.get('message','')[:60]}")
            return []

        jl = data.get("zpData", {}).get("jobList", [])
        return self._jobs_from_list(jl, keyword, page) if jl else []

    def scroll_and_capture(self, tab, keyword: str, max_pages: int = 10) -> list[JobPosting]:
        """滚动页面触发懒加载，每页用 listen 捕获 SPA 自己的 API 响应"""
        all_jobs: list[JobPosting] = []
        seen: set[str] = set()

        for pg in range(1, max_pages + 1):
            if pg > 1:
                # 滚动到底部，触发 SPA 懒加载下一页
                self._scroll_to_bottom(tab)
                time.sleep(2)

            # 启动监听，等待 SPA 翻页 API 响应
            try:
                tab.listen.start('joblist')
            except Exception:
                pass
            try:
                # 如果是首页，已经加载了，等一下就能捕获
                # 如果是翻页，scroll 触发了加载
                ok = tab.listen.wait(timeout=8)
            except Exception:
                try: tab.listen.stop()
                except: pass
                break
            try: tab.listen.stop()
            except: pass

            if not ok:
                if pg == 1:
                    continue
                break

            # 获取匹配的响应体
            body = None
            try:
                steps = tab.listen.steps()
                if steps:
                    last = steps[-1]
                    resp = last.response if hasattr(last, 'response') else last.get('response', {}) if isinstance(last, dict) else {}
                    if isinstance(resp, dict):
                        body = resp.get('body', resp)
                    elif hasattr(resp, 'body'):
                        body = resp.body
            except Exception:
                pass
            if body is None:
                if pg == 1:
                    continue
                break
            if isinstance(body, dict):
                data = body
            elif isinstance(body, (str, bytes)):
                import json as _json
                try:
                    data = _json.loads(body) if isinstance(body, str) else _json.loads(body.decode("utf-8", errors="replace"))
                except Exception:
                    break
            else:
                break

            if data.get("code", 0) != 0:
                break

            jl = data.get("zpData", {}).get("jobList", [])
            if not jl:
                break

            added = 0
            for j in self._jobs_from_list(jl, keyword, pg):
                if j.url and j.url not in seen:
                    seen.add(j.url)
                    all_jobs.append(j)
                    added += 1
            logger.info(f"  📡 p{pg}: +{added} (累计{len(all_jobs)})")
            if not added or len(jl) < 15:
                break
            time.sleep(random.uniform(0.5, 1.0))

        return all_jobs

    def _scroll_to_bottom(self, tab):
        """滚动到页面底部，触发懒加载"""
        try:
            tab.run_js("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        time.sleep(0.8)

    def inject_fetch_interceptor(self, tab):
        """通过 CDP 注入 fetch 拦截器（在每次页面加载时自动执行）"""
        js = """
        if (!window.__oc_injected) {
            window.__oc_injected = true;
            window.__oc_captured = null;
            var orig = window.fetch;
            window.fetch = function() {
                var url = arguments[0] || '';
                return orig.apply(this, arguments).then(function(r) {
                    if (typeof url === 'string' && url.indexOf('joblist') > -1) {
                        r.clone().text().then(function(t) {
                            window.__oc_captured = t;
                        }).catch(function(){});
                    }
                    return r;
                });
            };
        }
        """
        try:
            tab.run_cdp("Page.addScriptToEvaluateOnNewDocument", source=js)
        except Exception:
            pass

    def get_captured_response(self, tab, keyword: str) -> list[JobPosting]:
        """获取注入的 fetch 拦截器捕获到的 joblist 响应"""
        import json as _json

        try:
            raw = tab.run_js("return window.__oc_captured || ''")
        except Exception:
            return []

        if not raw or not isinstance(raw, str) or not raw.startswith("{"):
            return []

        try:
            data = _json.loads(raw)
        except Exception:
            return []

        if data.get("code", 0) != 0:
            return []

        jl = data.get("zpData", {}).get("jobList", [])
        if not jl:
            return []

        jobs = self._jobs_from_list(jl, keyword, 1)
        if jobs:
            logger.info(f"  📡 拦截API: {len(jobs)}个 | 首岗={jobs[0].title[:20]}")
        return jobs

    def start_capture(self, tab):
        """在导航前启动网络监听"""
        tab.listen.start('joblist')

    def wait_capture(self, tab, keyword: str) -> list[JobPosting]:
        """获取已捕获的搜索 API 响应"""
        import json as _json

        try:
            resp = tab.listen.wait(timeout=8)
        except Exception:
            return []
        finally:
            try: tab.listen.stop()
            except: pass

        # wait() 可能返回 DataPacket 或 bool
        body = None
        if resp and not isinstance(resp, bool):
            try:
                body = resp.response.body
            except Exception:
                pass
        elif resp:
            try:
                steps = tab.listen.steps()
                if steps and len(steps) > 0:
                    last = steps[-1]
                    if hasattr(last, 'response') and hasattr(last.response, 'body'):
                        body = last.response.body
            except Exception:
                pass

        if body is None:
            return []

        if isinstance(body, dict):
            data = body
        elif isinstance(body, bytes):
            try:
                data = _json.loads(body.decode("utf-8", errors="replace"))
            except Exception:
                return []
        elif isinstance(body, str):
            try:
                data = _json.loads(body)
            except Exception:
                return []
        else:
            return []

        if data.get("code", 0) != 0:
            return []

        jl = data.get("zpData", {}).get("jobList", [])
        if not jl:
            return []

        jobs = self._jobs_from_list(jl, keyword, 1)
        if jobs:
            logger.info(f"  📡 捕获API: {len(jobs)}个 | 首岗={jobs[0].title[:20]}")
        return jobs

    def _jobs_from_list(self, jl: list, keyword: str, page: int) -> list[JobPosting]:
        jobs = []
        for j in (jl or []):
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
                job.boss_online = bool(j.get("bossOnline"))
            job.company_size = j.get("brandScaleName", "")
            job.company_industry = j.get("brandIndustry", "")
            job.description = j.get("itemDescription", "") or ""
            eid = j.get("encryptJobId") or j.get("jobId") or ""
            job.encrypt_job_id = eid
            job._security_id = j.get("securityId") or j.get("encryptBossId") or ""
            job.url = _clean_job_url(
                f"https://www.zhipin.com/job_detail/{eid}.html" if eid else f"zhipin://{job.title}|{job.company}"
            )
            tags = j.get("jobLabels") or j.get("skills") or []
            job.skills_required = tags[:8] if tags else []
            if job.title:
                _sanitize_job(job)
                jobs.append(job)
        logger.info(f"  API p{page}: {len(jobs)}个 | 首岗={jobs[0].title[:25] if jobs else '?'}")
        return jobs

    def extract_from_js_state(self, tab) -> list[JobPosting]:
        """从页面 JS 全局状态提取 BOSS SPA 已渲染的岗位数据（最可靠）"""
        import json as _json

        js_code = """
        (function() {
            function findJobs(obj, depth) {
                if (!obj || typeof obj !== 'object' || depth > 5) return null;
                // 直接匹配: 数组中包含 jobName/encryptJobId 对象
                if (Array.isArray(obj) && obj.length > 0 &&
                    obj[0] && typeof obj[0] === 'object' &&
                    (obj[0].jobName || obj[0].encryptJobId)) {
                    return obj;
                }
                // 匹配常见的 jobList 字段名
                var knownKeys = ['jobList','joblist','resultList','list','dataList',
                    'recommendList','searchResultList','jobCardList','cards'];
                for (var i = 0; i < knownKeys.length; i++) {
                    if (obj[knownKeys[i]] && Array.isArray(obj[knownKeys[i]]) &&
                        obj[knownKeys[i]].length > 0) {
                        return obj[knownKeys[i]];
                    }
                }
                for (var k in obj) {
                    if (k === 'length' || k === 'constructor' || k === 'prototype') continue;
                    try {
                        var r = findJobs(obj[k], depth + 1);
                        if (r) return r;
                    } catch(e) {}
                }
                return null;
            }
            var result = findJobs(window, 0);
            return result ? JSON.stringify(result.slice(0, 100)) : '';
        })()
        """
        try:
            raw = tab.run_js(js_code)
            if not raw or not isinstance(raw, str) or not raw.startswith("["):
                return []
            data = _json.loads(raw)
            if not data:
                return []
            jobs = self._jobs_from_list(data, "_js_", 0)
            if jobs:
                logger.info(f"  JS状态提取: {len(jobs)} 个岗位 | 首岗={jobs[0].title[:25]}")
            return jobs
        except Exception as e:
            logger.debug(f"  JS状态提取失败: {e}")
            return []

    def _parse_page(self, tab) -> list[JobPosting]:
        cards = self._find_card_elements(tab)
        if not cards:
            return []

        jobs = []
        seen_urls: set[str] = set()
        for i, card in enumerate(cards):
            try:
                job = self._parse_card(card)
                if not job or not job.title:
                    continue
                # 过滤UI导航元素
                if job.title in ("职位", "职位搜索", "首页", "企业服务", "消息", "我的"):
                    continue
                if job.url and job.url in seen_urls:
                    continue
                if job.url:
                    seen_urls.add(job.url)
                if not job.company:
                    job.company = "未知公司"
                    # DEBUG: 输出第一张无公司名的卡片原始文本
                    if i == 0 and cards[0].text:
                        raw = cards[0].text[:400].replace("\n", "⏎")
                        logger.info(f"  🔍 首卡文本: {raw}")
                _sanitize_job(job)
                jobs.append(job)
            except Exception:
                continue
        return jobs

    def _parse_card(self, card) -> Optional[JobPosting]:
        job = JobPosting()

        try:
            text = card.text
        except Exception:
            return None
        if not text or len(text) < 10:
            return None
        if _GARBAGE_RE.search(text[:100]):
            return None

        # ==== 一次性收集所有 a 标签信息，避免反复遍历 ====
        all_as = card.eles("tag:a") or []
        company_candidates: list[str] = []
        for a in all_as:
            try:
                href = a.link
            except Exception:
                continue
            if not href:
                continue
            href_s = str(href)
            atext = (a.text or "").strip()
            if _JOB_URL_RE.search(href_s):
                if not job.url:
                    job.url = _clean_job_url(
                        href_s if href_s.startswith("http") else f"https://www.zhipin.com{href_s}"
                    )
                if not job.title and _is_title(atext):
                    job.title = atext
            if "/gongsi/" in href_s and atext and 2 <= len(atext) <= 50:
                company_candidates.append(atext)
            # 有些公司链接可能是 /company/ 格式
            if "/company/" in href_s and atext and 2 <= len(atext) <= 50:
                company_candidates.append(atext)

        # ==== 纯文本行解析 ====
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        # 标题兜底
        if not job.title:
            for line in lines:
                if line in _BOSS_STATUS_LINES:
                    continue
                if _is_title(line) and not _is_salary(line):
                    job.title = line
                    break
            if not job.title and lines:
                for line in lines:
                    if line not in _BOSS_STATUS_LINES and not _is_salary(line):
                        job.title = line
                        break

        # 薪资
        for line in lines:
            if _is_salary(line):
                job.salary = line
                job.salary_min, job.salary_max = extract_salary_range(line)
                break
        if not job.salary:
            job.salary = "面议"

        # 公司名：优先用 <a> 提取的，其次文本推断
        if company_candidates:
            job.company = company_candidates[0]
        if not job.company:
            # 文本行中推断公司：找薪资行后第一个像是公司的行
            passed_salary = False
            for line in lines:
                if _is_salary(line): passed_salary = True; continue
                if passed_salary and _is_company(line) and line != job.title:
                    job.company = line; break
            if not job.company:
                for line in lines:
                    if line == job.title: continue
                    if _is_company(line): job.company = line; break
        if job.company:
            job.company = _normalize_company(job.company)
        if job.title:
            job.title = _normalize_title(job.title)
        # 校验假公司名
        if job.company and job.title:
            _title_kw = ("工程师", "经理", "AI", "Java", "Python", "管培生", "校招", "应届", "开发", "测试", "产品")
            _comp_kw = ("公司", "有限", "科技", "网络", "集团", "技术", "股份", "企业", "华为", "阿里", "腾讯", "字节", "百度")
            if any(w in job.company for w in _title_kw) \
               and not any(w in job.company for w in _comp_kw):
                job.company = ""

        # 地点/经验/学历
        for line in lines:
            if "·" in line or "经验" in line or "应届" in line:
                for p in line.replace(" ", "").split("·"):
                    p = p.strip()
                    if any(c in p for c in self.CITY_CODES):
                        job.location = p
                    elif any(w in p for w in ("经验", "应届", "年", "在校")):
                        job.experience = p
                    elif any(d in p for d in ("本科", "大专", "硕士", "博士", "学历")):
                        job.education = p
            elif any(c in line for c in self.CITY_CODES) and not job.location:
                job.location = line

        # 技能
        job.skills_required = self._extract_skills(text + " " + job.title)
        if job.url and not job.encrypt_job_id:
            job.encrypt_job_id = extract_encrypt_job_id(job.url)
        return job

    # ==================== 滚动 ====================

    def _scroll_page(self, tab):
        for _ in range(4):
            try:
                tab.run_js("window.scrollBy(0, 600)")
            except Exception:
                pass
            time.sleep(0.4)

    # ==================== 搜索入口 ====================

    def search_all(self) -> list[JobPosting]:
        all_jobs: dict[str, JobPosting] = {}
        for keyword in self.keywords:
            jobs = self._search_one(keyword)
            for job in jobs:
                key = job.url or (job.title + job.company)
                if key not in all_jobs:
                    all_jobs[key] = job
        return list(all_jobs.values())

    def _search_one(self, keyword: str) -> list[JobPosting]:
        results = []
        tab = self.browser.latest_tab
        url = f"https://www.zhipin.com/web/geek/job?query={quote(keyword)}&city={self.city_code}"
        if self.exp_code:
            url += f"&experience={self.exp_code}"
        tab.get(url)
        tab.wait(4)
        self._scroll_page(tab)
        for _ in range(self.page_limit):
            tab.wait(2)
            self._scroll_page(tab)
            jobs = self._parse_page(tab)
            if not jobs:
                break
            results.extend(jobs)
        return results

    def collect_visible_jobs(self, tab, page_limit: int = 5) -> list[JobPosting]:
        """在已触发搜索的页面上滚动懒加载并解析 DOM 卡片（不导航，不调 API）"""
        if is_boss_security_page(tab, strict=True) and not has_job_cards(tab):
            logger.warning("  ⚠️ 安全验证中，跳过 DOM 滚动解析（避免页面闪烁）")
            return []
        results: list[JobPosting] = []
        seen_urls: set[str] = set()
        for pg in range(page_limit):
            if is_boss_security_page(tab, strict=True) and not has_job_cards(tab):
                logger.warning("  ⚠️ 解析中触发安全验证，停止滚动")
                break
            if pg > 0:
                tab.wait(1.5)
            self._scroll_page(tab)
            tab.wait(1.5)
            jobs = self._parse_page(tab)
            added = 0
            for j in jobs:
                if j.url and j.url not in seen_urls:
                    seen_urls.add(j.url)
                    results.append(j)
                    added += 1
            logger.info(f"  DOM p{pg+1}: +{added} (累计{len(results)})")
            if not added:
                break
        return results

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

    def _page_is_job_detail(self, page_tab, job: JobPosting) -> bool:
        """确认页面真的是岗位详情/侧栏，避免首页误判 BOSS 在线"""
        try:
            url = (page_tab.url or "").lower()
            if is_boss_security_page(page_tab):
                return False
            eid = (job.encrypt_job_id or "").lower()
            if eid and eid in url and ("job_detail" in url or "jobid=" in url):
                return True
            body = page_tab.run_js(
                "return document.body? document.body.innerText.slice(0,4000) : ''"
            ) or ""
            title = (job.title or "")[:8]
            has_btn = any(k in body for k in ("立即沟通", "免费沟通", "继续沟通", "职位描述", "任职要求"))
            if has_btn and title and title in body:
                return True
            if "job_detail" in url and has_btn:
                return True
        except Exception:
            pass
        return False

    def open_job_from_list(self, list_tab, job: JobPosting) -> bool:
        """在列表页点击岗位卡片，打开侧栏/详情（比直接 get URL 更抗风控）"""
        if not list_tab or is_boss_security_page(list_tab):
            return False
        job_id = job.encrypt_job_id or extract_encrypt_job_id(job.url)
        title = (job.title or "").strip()
        if not job_id and not title:
            return False
        try:
            import json as _json
            hit = list_tab.run_js(
                f"""
                (() => {{
                    const jobId = {_json.dumps(job_id or "")};
                    const title = {_json.dumps(title)};
                    const nodes = Array.from(document.querySelectorAll(
                        'a[href*="job_detail"], .job-card-wrapper, .job-card-box, li.job-card-box, [class*="job-card"]'
                    ));
                    for (const el of nodes) {{
                        const href = el.getAttribute('href')
                            || (el.querySelector && el.querySelector('a[href*="job_detail"]')
                                ? el.querySelector('a[href*="job_detail"]').getAttribute('href') : '')
                            || '';
                        const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                        const card = el.closest('.job-card-wrapper,.job-card-box,li,[class*="job-card"]') || el;
                        if ((jobId && href && href.includes(jobId))
                            || (title && text.includes(title.slice(0, Math.min(8, title.length))))) {{
                            card.scrollIntoView({{block:'center'}});
                            const link = card.querySelector('a[href*="job_detail"]') || card;
                            link.click();
                            return true;
                        }}
                    }}
                    return false;
                }})()
                """
            )
            if not hit:
                return False
            list_tab.wait(3)
            return self._page_is_job_detail(list_tab, job)
        except Exception as e:
            logger.debug(f"  列表打开岗位失败: {e}")
            return False

    def fetch_job_detail(
        self,
        tab,
        job: JobPosting,
        need_description: bool = True,
        need_boss_status: bool = True,
    ) -> str:
        """打开职位详情页，按需抓取描述与 BOSS 状态"""
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
            # 优先：列表页点卡片（不触发 URL 风控）
            if tab and self.open_job_from_list(tab, job):
                page_for_parse = tab
                logger.info(f"  📋 列表打开岗位: {job.title[:20]}")
            else:
                detail_tab = self.ensure_detail_tab()
                if job_url:
                    detail_tab.get(job_url)
                    detail_tab.wait(4)
                if self._page_is_job_detail(detail_tab, job):
                    page_for_parse = detail_tab
                else:
                    # URL 被踢回首页时，不要误读「在线」
                    logger.warning(
                        f"  ⚠️ 详情页未真正打开 | {(detail_tab.url or '')[:80]}"
                    )
                    return job.description if has_desc else ""

            if need_boss_status and page_for_parse:
                online, active_time = self._parse_boss_status(page_for_parse)
                if online is True:
                    job.boss_online = True
                if active_time:
                    job.boss_active_time = active_time

            page = page_for_parse
            if not need_description or has_desc:
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
                    el = page.ele(sel, timeout=2)
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
                job.description = desc
                logger.info(f"  📝 {job.title[:20]} ({len(desc)}字)")
            elif job.boss_active_time or job.boss_online is True:
                logger.info(
                    f"  👤 {job.title[:20]} BOSS="
                    f"{'在线' if job.boss_online else job.boss_active_time or '未知'}"
                )
            return desc if desc and len(desc) >= 30 else ""
        except Exception as e:
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
        return [s for s in self.TECH_KEYWORDS if s.lower() in lower]
