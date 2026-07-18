"""
投递执行模块 — DrissionPage 版本
BOSS直聘: API 开聊（优先）→ 详情页点击 → 发送招呼语
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from DrissionPage import Chromium
from job_search import JobPosting, _clean_job_url, extract_encrypt_job_id, verify_boss_session, is_boss_security_page
from utils import get_logger

if TYPE_CHECKING:
    from DrissionPage._pages.chromium_tab import ChromiumTab

logger = get_logger("submit")

_CONTACT_TEXTS = (
    "立即沟通", "免费沟通", "继续沟通", "聊一聊",
    "和BOSS沟通", "和Ta沟通", "和 TA 沟通", "投递简历",
)


class SubmitResult:
    SUCCESS = "投递成功"
    SKIPPED = "skipped"
    DAILY_LIMIT = "daily_limit"
    ALREADY_APPLIED = "already_applied"
    LOGIN_REQUIRED = "login_required"
    FAILED = "failed"
    RISK_REJECTED = "risk_rejected"
    MATCH_LOW = "match_low"
    KPI_REJECTED = "kpi_rejected"


class JobSubmitter:

    def __init__(self, browser: Chromium, config: dict, work_tab=None, get_detail_tab=None):
        self.browser = browser
        self.work_tab = work_tab
        self._get_detail_tab_fn = get_detail_tab
        self.cfg = config
        submit_cfg = config.get("submit", {})
        self.greeting = submit_cfg.get(
            "greeting",
            "您好，我是2026届信息工程专业本科毕业生，比较匹配贵公司岗位的招聘要求，可以发个简历给您看看吗？"
        )
        self.send_greeting = submit_cfg.get("send_greeting", True)
        self.daily_limit = submit_cfg.get("daily_limit", 150)
        self._submit_tab: Optional[ChromiumTab] = None
        self._detail_tab: Optional[ChromiumTab] = None
        self._security_paused = False

        self._counter_file = Path(__file__).parent / "daily_count.json"
        self._today = date.today().isoformat()
        self._today_count = self._load_today_count()

    def _load_today_count(self) -> int:
        try:
            if self._counter_file.exists():
                data = json.loads(self._counter_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("date") == self._today:
                    return int(data.get("count", 0))
        except Exception:
            pass
        return 0

    def _save_today_count(self):
        try:
            self._counter_file.write_text(
                json.dumps({"date": self._today, "count": self._today_count},
                           ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"保存每日计数失败: {e}")

    def _alive_tab(self, tab) -> bool:
        if tab is None:
            return False
        try:
            _ = tab.url
            return True
        except Exception:
            return False

    def _get_boss_tab(self):
        """优先 work_tab（登录态最稳定），否则专用投递 tab"""
        if self._alive_tab(self.work_tab):
            return self.work_tab
        if self._alive_tab(self._submit_tab):
            return self._submit_tab
        self._submit_tab = self.browser.new_tab("https://www.zhipin.com/web/geek/job")
        self._submit_tab.wait(3)
        return self._submit_tab

    def _reset_boss_tab(self):
        self._submit_tab = None

    def _get_detail_tab(self):
        """详情页操作使用独立标签（与 fetch_job_detail 相同，避免 work_tab 在首页时跳转失败）"""
        if self._get_detail_tab_fn:
            return self._get_detail_tab_fn()
        if self._alive_tab(self._detail_tab):
            return self._detail_tab
        self._detail_tab = self.browser.new_tab("about:blank")
        return self._detail_tab

    def _pause_for_security(self, api_tab) -> bool:
        """BOSS 安全验证时暂停，让用户在浏览器完成验证"""
        if self._security_paused:
            return False
        self._security_paused = True
        print()
        logger.warning("=" * 55)
        logger.warning("  ⚠️ BOSS 需要安全验证！")
        logger.warning("     请在浏览器 BOSS 标签页完成滑块/验证码")
        logger.warning("     验证成功、页面跳转后再按 Enter")
        logger.warning("     （按 Enter 不会自动跳转，避免验证页来回切换）")
        logger.warning("=" * 55)
        try:
            input()
        except EOFError:
            pass
        if verify_boss_session(api_tab):
            logger.info("  ✅ 安全验证已通过")
            return True
        logger.warning("  ⚠️ API 仍未通过，将尝试详情页点击")
        return False

    def submit(self, job: JobPosting, match_score: float) -> str:
        new_today = date.today().isoformat()
        if new_today != self._today:
            self._today = new_today
            self._today_count = self._load_today_count()

        if self._today_count >= self.daily_limit:
            logger.warning(f"今日已投 {self._today_count}/{self.daily_limit}，已达上限")
            return SubmitResult.DAILY_LIMIT

        job_url = _clean_job_url(job.url)
        if not job.encrypt_job_id:
            job.encrypt_job_id = extract_encrypt_job_id(job_url)
        if not job_url and not job.encrypt_job_id:
            logger.warning("  ❌ 无岗位链接")
            return SubmitResult.FAILED

        logger.info(
            f"  投递 [{self._today_count + 1}/{self.daily_limit}] "
            f"{job.title} @ {job.company}"
        )

        tab = self._get_boss_tab()
        detail_tab = self._get_detail_tab()
        api_result = "fail"
        try:
            # ① 先在列表页点卡片沟通 —— 绝不能先 _ensure_boss_context，
            #    否则会把首页/推荐页的卡片导航走，导致找不到岗位
            if self._submit_via_list_card(tab, job):
                self._finish_success()
                return SubmitResult.SUCCESS

            # ② 复用抓 BOSS 状态时已打开的详情页
            if self._tab_has_job(detail_tab, job.encrypt_job_id, job.title):
                logger.info("  📄 复用已打开的详情页投递")
                result = self._submit_on_open_page(detail_tab)
                if result != SubmitResult.FAILED:
                    return result

            # ③ API 开聊（仅此处才确保 geek 上下文，且不破坏列表页时可跳过）
            api_result = self._submit_via_api(tab, job, force_context=False)
            if api_result == "success":
                self._finish_success()
                return SubmitResult.SUCCESS
            if api_result == "already":
                logger.info("  ⚠️ 已沟通过")
                return SubmitResult.ALREADY_APPLIED
            if api_result == "security":
                logger.warning("  ⚠️ API 触发安全验证，继续页面点击")

            # ④ 列表再试一次（API 可能切走过 tab）
            if self._submit_via_list_card(tab, job):
                self._finish_success()
                return SubmitResult.SUCCESS

            # ⑤ 最后才用 URL 打开详情（风控下常被踢回首页）
            if not job_url:
                job_url = f"https://www.zhipin.com/job_detail/{job.encrypt_job_id}.html"
            opened = self._open_job_detail(detail_tab, job_url, job.encrypt_job_id)
            if not opened and api_result == "security" and not self._security_paused:
                if self._pause_for_security(tab):
                    if self._submit_via_list_card(tab, job):
                        self._finish_success()
                        return SubmitResult.SUCCESS
                    api_result = self._submit_via_api(tab, job, force_context=True)
                    if api_result == "success":
                        self._finish_success()
                        return SubmitResult.SUCCESS
                    opened = self._open_job_detail(detail_tab, job_url, job.encrypt_job_id)
            if not opened:
                logger.warning("  ❌ 无法打开岗位详情页（URL 可能被安全验证拦截）")
                if api_result == "login":
                    return SubmitResult.LOGIN_REQUIRED
                return SubmitResult.FAILED

            result = self._submit_on_open_page(detail_tab)
            if result == SubmitResult.FAILED and api_result == "login":
                return SubmitResult.LOGIN_REQUIRED
            return result

        except Exception as e:
            logger.error(f"  ❌ 投递异常: {e}")
            self._reset_boss_tab()
            return SubmitResult.FAILED

    def _tab_has_job(self, tab, encrypt_id: str, title: str = "") -> bool:
        if not self._alive_tab(tab):
            return False
        if is_boss_security_page(tab):
            return False
        try:
            url = (tab.url or "").lower()
            if encrypt_id and encrypt_id.lower() in url:
                return True
            if "job_detail" in url or "jobid=" in url:
                return True
            body = tab.run_js("return document.body? document.body.innerText.slice(0,3000) : ''") or ""
            if any(k in body for k in ("立即沟通", "免费沟通", "职位描述", "任职要求")):
                if not title or title[:8] in body:
                    return True
        except Exception:
            pass
        return False

    def _submit_on_open_page(self, page_tab) -> str:
        """在已打开的岗位页上点击沟通"""
        cur_url = str(page_tab.url or "").lower()
        if "login" in cur_url or "passport" in cur_url:
            if "security" in cur_url:
                logger.warning("  ⚠️ BOSS 安全验证，请手动完成后再运行")
                return SubmitResult.FAILED
            return SubmitResult.LOGIN_REQUIRED
        if is_boss_security_page(page_tab):
            logger.warning("  ⚠️ BOSS 安全验证，请手动完成后再运行")
            return SubmitResult.FAILED

        if self._already_contacted(page_tab):
            return SubmitResult.ALREADY_APPLIED

        if not self._click_contact(page_tab):
            self._log_page_debug(page_tab)
            logger.warning("  ❌ 未找到沟通按钮")
            return SubmitResult.FAILED

        if self.send_greeting and self.greeting.strip():
            self._send_greeting(page_tab)

        self._finish_success()
        return SubmitResult.SUCCESS

    def _submit_via_list_card(self, tab, job: JobPosting) -> bool:
        """用 DrissionPage 点击列表卡片（保留完整 URL 参数），在新标签页点沟通"""
        if not self._alive_tab(tab) or is_boss_security_page(tab, strict=True):
            return False
        job_id = job.encrypt_job_id or extract_encrypt_job_id(job.url)
        title = (job.title or "").strip()
        if not job_id and not title:
            return False

        link_el = None
        try:
            if job_id:
                link_el = tab.ele(f'a[href*="{job_id}"]', timeout=2)
            if link_el is None and title:
                # 按标题文本找链接
                for a in (tab.eles("tag:a") or []):
                    try:
                        href = a.link or a.attr("href") or ""
                        text = (a.text or "").replace("\n", " ")
                        if "/job_detail/" not in href:
                            continue
                        if title[:8] in text:
                            link_el = a
                            break
                    except Exception:
                        continue
        except Exception as e:
            logger.debug(f"  查找卡片链接失败: {e}")

        if link_el is None:
            logger.info("  📋 列表页未找到对应卡片链接")
            return False

        before_ids = {id(t) for t in self.browser.get_tabs()}
        try:
            # 首页推荐位是 target=_blank，用真实点击打开
            link_el.click(by_js=False)
        except Exception:
            try:
                link_el.click(by_js=True)
            except Exception as e:
                logger.warning(f"  📋 点击卡片失败: {e}")
                return False

        time.sleep(3.5)
        # 找到新开的标签或当前页
        page = tab
        for t in self.browser.get_tabs():
            if id(t) not in before_ids:
                page = t
                break
        try:
            page.wait(2)
        except Exception:
            pass

        cur = (page.url or "").lower()
        logger.info(f"  📋 卡片打开 | {cur[:100]}")
        if is_boss_security_page(page, strict=True):
            logger.warning("  ⚠️ 打开岗位触发安全验证")
            return False
        # 被踢回首页则失败
        if "job_detail" not in cur and "jobid=" not in cur:
            body = ""
            try:
                body = page.run_js(
                    "return document.body? document.body.innerText.slice(0,500) : ''"
                ) or ""
            except Exception:
                pass
            if not any(k in body for k in ("立即沟通", "免费沟通", "继续沟通", "职位描述")):
                logger.warning("  ⚠️ 岗位页未打开（可能仍被风控拦截）")
                return False

        if self._already_contacted(page):
            logger.info("  ⚠️ 已沟通过")
            return True
        if not self._click_contact(page):
            logger.warning("  ❌ 岗位页未找到沟通按钮")
            return False
        if self.send_greeting and self.greeting.strip():
            self._send_greeting(page)
        return True

    def _finish_success(self):
        self._today_count += 1
        self._save_today_count()
        logger.info(f"  ✅ 沟通已发起 ({self._today_count}/{self.daily_limit})")

    def _ensure_boss_context(self, tab):
        """确保在 BOSS 求职者页，API 才能带上正确 Cookie（安全验证页不跳转）"""
        if is_boss_security_page(tab):
            return
        try:
            url = (tab.url or "").lower()
            if "web/geek" not in url:
                tab.get("https://www.zhipin.com/web/geek/jobs")
                tab.wait(3)
        except Exception:
            pass

    def _open_job_detail(self, tab, job_url: str, encrypt_id: str) -> bool:
        urls = []
        if encrypt_id:
            urls.append(f"https://www.zhipin.com/web/geek/job_detail?jobId={encrypt_id}")
        if job_url:
            urls.append(job_url)
        if encrypt_id:
            urls.append(f"https://www.zhipin.com/job_detail/{encrypt_id}.html")

        last_url = ""
        for url in urls:
            try:
                tab.get(url)
                tab.wait(5)
                self._scroll(tab)
                last_url = tab.url or ""
                if is_boss_security_page(tab):
                    logger.warning(f"  ⚠️ 详情页需安全验证: {last_url[:80]}")
                    continue
                if self._looks_like_job_page(tab, encrypt_id):
                    return True
            except Exception as e:
                logger.debug(f"  打开详情失败 {url[:60]}: {e}")
                continue
        if last_url:
            logger.warning(f"  详情页未识别 | URL={last_url[:100]}")
        return False

    def _looks_like_job_page(self, tab, encrypt_id: str) -> bool:
        try:
            url = (tab.url or "").lower()
            if "login" in url or "passport" in url:
                return False
            if "job_detail" in url or "jobid=" in url:
                return True
            if "zhipin.com" not in url:
                return False
            body = tab.run_js("return document.body? document.body.innerText : ''") or ""
            if any(k in body for k in ("职位描述", "岗位职责", "任职要求", "立即沟通", "免费沟通")):
                return True
            if encrypt_id and encrypt_id[:10] in body:
                return True
        except Exception:
            pass
        return False

    def _already_contacted(self, tab) -> bool:
        try:
            html = tab.html[:5000]
            if any(s in html for s in ("已沟通过", "已投递", "已发送", "今日沟通已达上限")):
                return True
        except Exception:
            pass
        return False

    def _click_contact(self, tab) -> bool:
        for _ in range(8):
            if self._try_click_contact_once(tab):
                return True
            time.sleep(1.2)
        return False

    def _try_click_contact_once(self, tab) -> bool:
        for text in _CONTACT_TEXTS:
            try:
                btn = tab.ele(f"text:{text}", timeout=1)
                if btn and self._is_visible(btn):
                    btn.click(by_js=True)
                    logger.info(f"  👆 点击「{text}」")
                    time.sleep(1.5)
                    return True
            except Exception:
                continue

        for sel in (
            ".btn-startchat", "a.btn-startchat", "button.btn-startchat",
            ".btn-talk", ".job-detail-op .btn", "css:a.op-btn",
        ):
            try:
                btn = tab.ele(sel, timeout=1)
                if btn and self._is_visible(btn):
                    btn.click(by_js=True)
                    logger.info("  👆 点击沟通按钮(css)")
                    time.sleep(1.5)
                    return True
            except Exception:
                continue

        try:
            hit = tab.run_js("""
                const texts = ['立即沟通','免费沟通','继续沟通','聊一聊','和BOSS沟通','和Ta沟通'];
                for (const el of document.querySelectorAll('a,button,span,div')) {
                    const t = (el.innerText || el.textContent || '').trim();
                    if (!t || t.length > 12) continue;
                    if (!texts.some(x => t === x || t.includes(x))) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 2 || r.height < 2) continue;
                    el.click();
                    return t;
                }
                const css = document.querySelector('.btn-startchat,a.btn-startchat,button.btn-startchat,.btn-talk');
                if (css) { css.click(); return 'css-js'; }
                return null;
            """)
            if hit:
                logger.info(f"  👆 JS点击「{hit}」")
                time.sleep(1.5)
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _is_visible(el) -> bool:
        try:
            return el.states.is_displayed
        except Exception:
            return True

    def _submit_via_api(self, tab, job: JobPosting, force_context: bool = True) -> str:
        """API 开聊，返回 success / already / security / login / fail"""
        job_id = job.encrypt_job_id or extract_encrypt_job_id(job.url)
        if not job_id:
            logger.warning("  ⚠️ 无 encryptJobId，跳过 API")
            return "fail"

        # force_context=False：当前页已有列表卡片时不要跳走
        if force_context:
            self._ensure_boss_context(tab)
        elif "zhipin.com" not in ((tab.url or "").lower()):
            self._ensure_boss_context(tab)
        sec_id = getattr(job, "_security_id", "") or ""
        js = """
        async (jobId, secId) => {
            const hdr = {'x-requested-with': 'XMLHttpRequest'};
            try {
                let sec = secId || '';
                let jid = jobId;
                let lid = '';
                if (!sec) {
                    const dr = await fetch(
                        'https://www.zhipin.com/wapi/zpgeek/job/detail.json?encryptJobId='
                        + encodeURIComponent(jobId),
                        {headers: hdr}
                    );
                    const dj = await dr.json();
                    if (!dj || dj.code !== 0 || !dj.zpData) {
                        return JSON.stringify({ok:false, step:'detail', code: dj && dj.code});
                    }
                    const z = dj.zpData;
                    sec = z.securityId || z.secureId || '';
                    jid = z.encryptJobId || jobId;
                    lid = z.lid || '';
                }
                if (!sec) {
                    return JSON.stringify({ok:false, step:'detail', msg:'no securityId'});
                }
                const body = new URLSearchParams({securityId: sec, jobId: jid, lid: lid});
                const ar = await fetch('https://www.zhipin.com/wapi/zpgeek/friend/add.json', {
                    method: 'POST',
                    headers: {...hdr, 'Content-Type': 'application/x-www-form-urlencoded'},
                    body: body.toString(),
                });
                return await ar.text();
            } catch (e) {
                return JSON.stringify({ok:false, step:'exception', msg: String(e)});
            }
        }
        """
        try:
            resp = tab.run_cdp(
                "Runtime.evaluate",
                expression=f"({js})({json.dumps(job_id)}, {json.dumps(sec_id)})",
                awaitPromise=True,
                returnByValue=True,
            )
            raw = (resp.get("result") or {}).get("value") or ""
        except Exception as e:
            logger.warning(f"  API开聊异常: {e}")
            return "fail"

        if not raw:
            return "fail"

        text = str(raw)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            if '"code":0' in text or '"code": 0' in text:
                logger.info("  👆 API 开聊成功")
                return "success"
            logger.warning(f"  API开聊响应: {text[:120]}")
            return "fail"

        # 内部包装格式 {ok:false, step:'detail', code:N}
        if data.get("ok") is False:
            code = int(data.get("code") or -1)
            step = data.get("step", "")
            if code == 37:
                return "security"
            if code in (7, 101):
                return "login"
            logger.warning(f"  API {step} 失败 code={code}")
            return "fail"

        code = int(data.get("code", -1))
        msg = str(data.get("message") or data.get("zpData") or "")
        if code == 0:
            logger.info("  👆 API 开聊成功")
            return "success"
        if code == 1 and ("已经" in msg or "沟通过" in msg):
            return "already"
        if code == 37:
            return "security"
        if code in (7, 101) or (code == 7 and "登录" in msg):
            return "login"
        logger.warning(f"  API开聊失败 code={code} msg={msg[:80]}")
        return "fail"

    def _log_page_debug(self, tab):
        try:
            info = tab.run_js("""
                return {
                    url: location.href,
                    title: document.title,
                    btns: [...document.querySelectorAll('a,button')].slice(0,30)
                        .map(el => (el.innerText||'').trim()).filter(t => t && t.length < 20).slice(0,10),
                };
            """)
            if info:
                logger.info(
                    f"  🔍 页面调试 url={info.get('url','')[:80]} "
                    f"buttons={info.get('btns', [])[:6]}"
                )
        except Exception:
            pass

    def _send_greeting(self, tab) -> bool:
        greeting = (self.greeting or "").strip()
        if not greeting:
            return True
        time.sleep(1.5)
        if self._try_send_greeting_on_tab(tab, greeting):
            return True
        try:
            latest = self.browser.latest_tab
            if latest and latest is not tab:
                return self._try_send_greeting_on_tab(latest, greeting)
        except Exception:
            pass
        logger.warning("  ⚠️ 未找到招呼语输入框，沟通已发起但未发自定义招呼")
        return False

    def _try_send_greeting_on_tab(self, tab, greeting: str) -> bool:
        for text in ("知道了", "我知道了"):
            try:
                btn = tab.ele(f"text:{text}", timeout=0.8)
                if btn:
                    btn.click()
                    time.sleep(0.5)
            except Exception:
                continue

        input_el = None
        for sel in (
            "css:.dialog-input input", "css:.dialog-container textarea",
            "css:.chat-input textarea", "css:textarea.input-area",
            "css:div[contenteditable='true']", "tag:textarea",
        ):
            try:
                el = tab.ele(sel, timeout=1)
                if el:
                    input_el = el
                    break
            except Exception:
                continue

        if not input_el:
            return False

        try:
            input_el.click()
            input_el.clear()
            input_el.input(greeting)
        except Exception:
            return False

        time.sleep(0.5)
        for text in ("发送", "立即发送", "确定"):
            try:
                btn = tab.ele(f"text:{text}", timeout=1.5)
                if btn:
                    btn.click()
                    logger.info(f"  💬 招呼语已发送 ({len(greeting)}字)")
                    time.sleep(1)
                    return True
            except Exception:
                continue
        return False

    def _scroll(self, tab):
        for _ in range(3):
            try:
                tab.run_js("window.scrollBy(0, 400)")
            except Exception:
                pass
            time.sleep(0.3)

    def get_today_count(self) -> int:
        return self._today_count
