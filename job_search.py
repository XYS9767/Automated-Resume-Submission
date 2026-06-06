"""
岗位搜索模块 — DrissionPage 版本
"""

from __future__ import annotations

import re
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
    if not line or len(line) < 2 or len(line) > 50: return False
    if _GARBAGE_RE.search(line): return False
    return True


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
        "天津": "101030100",
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

    # ==================== 卡片查找 ====================

    def _find_card_elements(self, tab) -> list:
        """查找岗位卡片容器：从含 /job_detail/ 的 a 标签向上找"""
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
                logger.warning(f"  API code={code} p{page}")
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
            job.company_size = j.get("brandScaleName", "")
            job.company_industry = j.get("brandIndustry", "")
            job.description = j.get("itemDescription", "") or ""
            eid = (j.get("encryptJobId") or j.get("encryptBossId")
                    or j.get("securityId") or j.get("jobId") or "")
            job.encrypt_job_id = eid
            job._security_id = eid
            job.url = f"https://www.zhipin.com/job_detail/{eid}.html" if eid else f"zhipin://{job.title}|{job.company}"
            tags = j.get("jobLabels") or j.get("skills") or []
            job.skills_required = tags[:8] if tags else []
            if job.title:
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
                    job.url = href_s if href_s.startswith("http") else f"https://www.zhipin.com{href_s}"
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
                if _is_title(line) and not _is_salary(line):
                    job.title = line
                    break
            if not job.title and lines:
                job.title = lines[0]

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
        results: list[JobPosting] = []
        seen_urls: set[str] = set()
        for pg in range(page_limit):
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

    def fetch_job_description(self, tab, job: JobPosting) -> str:
        """打开职位详情页，从 DOM 抓取完整职位描述

        直接导航到详情页提取描述文本 — 最可靠的方式
        返回描述文本；失败返回空字符串
        """
        if job.description and len(job.description) >= 30:
            return job.description  # 已有足够描述，跳过

        job_url = job.url
        if not job_url:
            return ""

        try:
            # 打开详情页
            tab.get(job_url)
            # 等待描述区域加载（多种选择器兜底）
            desc_selectors = [
                "div.job-sec-text", "div.job-detail", ".job-detail-section",
                "div[class*='job-detail'] div[class*='text']",
                ".detail-section", "div.detail-content",
            ]
            desc = ""
            for sel in desc_selectors:
                try:
                    el = tab.ele(sel, timeout=3)
                    if el:
                        desc = (el.text or "").strip()
                        if len(desc) >= 50:
                            break
                        desc = ""
                except Exception:
                    continue

            # 兜底：取 body 中疑似描述的长文本块
            if not desc or len(desc) < 30:
                try:
                    # 整页文本，尝试定位 JD 区域
                    full_text = tab.ele("tag:body", timeout=2)
                    if full_text:
                        lines = (full_text.text or "").split("\n")
                        # 找到"职位描述""岗位职责"等关键词后面的内容
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
                return desc
            return ""
        except Exception as e:
            logger.debug(f"  描述获取失败 {job.title[:20]}: {e}")
            return ""

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
