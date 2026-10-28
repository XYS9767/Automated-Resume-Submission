"""
BOSS直聘 自动简历投递 — 精简版
搜一个关键词 → 全量滚动 → 全量投递 → 下一个关键词
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import random
from copy import deepcopy
from pathlib import Path
from datetime import datetime
from typing import Optional

from utils import (
    load_config, setup_logger, get_logger, find_resume_file, find_all_resume_files,
    resolve_path, boss_active_rank, parse_max_boss_inactive,
)

from resume_parser import ResumeParser, ResumeData
from boss_login import BossLogin
from job_search import (
    JobSearcher, JobPosting, _sanitize_job, title_matches_keyword,
    is_boss_security_page, wait_user_clear_security,
    has_job_cards, wait_for_job_cards, probe_boss_session,
    ensure_boss_api_ready, job_url_key,
)
from company_risk import CompanyRiskChecker, RiskLevel, RiskResult
from job_matcher import JobMatcher
from submitter import JobSubmitter, SubmitResult
from recorder import ApplyRecorder


class AutoBossApp:

    def __init__(self, config_path: str = "config.yaml"):
        self.config = deepcopy(load_config(config_path))
        self.logger = get_logger("app")
        self.browser = None
        self.work_tab = None
        self.resumes: list[ResumeData] = []
        self._primary_resume: Optional[ResumeData] = None
        self.logger.info(f"启动 {datetime.now():%Y-%m-%d %H:%M:%S}")
        self._searcher = None
        self._boss_url_nav_blocked = False

    def run(self, search_only=False, resume_only=False, resume_path=None, interactive=False):
        from collections import Counter
        from run_queue import RunQueue
        from relevance import keyword_matches
        stats = Counter()
        recorder = None
        try:
            if self._stop_requested():
                return 0
            cfg = self.config
            online = cfg.get('resume', {}).get('source') == 'boss_online' and not resume_path
            if online:
                from boss_resume import read_online_profile
                login = BossLogin(cfg)
                self.browser = login.login()
                self.work_tab = login.work_tab
                use_expectations = cfg['search'].get('source', 'boss_online') == 'boss_online'
                profile = read_online_profile(self.browser, use_expectations=use_expectations)
                self.resumes = [profile.resume]
                if use_expectations:
                    cfg['search']['targets'] = profile.targets
                    cfg['search']['keywords'] = list(dict.fromkeys(t['keyword'] for t in profile.targets))
                    first = profile.targets[0]
                    cfg['search'].update(city=first['city'], city_code=first['city_code'], salary=first['salary'])
                years = profile.resume.declared_experience_years
                if years is not None:
                    cfg['filter']['max_experience'] = str(years) + '年'
                self.logger.info('使用当前 BOSS 在线简历：%s；求职条件来源=%s',
                                 profile.resume.name, '在线期望' if use_expectations else '配置文件')
            else:
                paths = self._resolve_resume_paths(resume_path)
                if not paths:
                    raise FileNotFoundError("请把自己的 PDF/DOCX/TXT 简历放入 resumes 文件夹，再运行。")
                self.resumes = [ResumeParser(str(path)).parse() for path in paths]
            self._primary_resume = self.resumes[0]
            self._all_resume_text = self._build_combined_resume_text()
            self._validate_resumes()
            if resume_only:
                return 0
            cfg = self.config
            filter_cfg = cfg.get("filter", {})
            risk_checker = CompanyRiskChecker(cfg)
            keywords = list(dict.fromkeys(k.strip() for k in cfg["search"]["keywords"] if k.strip()))
            if interactive:
                keywords = self._load_or_review_keywords(cfg, filter_cfg, keywords)
                max_exp, max_edu = self._configure_filters_interactive(filter_cfg)
                filter_cfg.update(max_experience=max_exp, max_education=max_edu)
            elif not keywords:
                if cfg["search"].get("use_saved_keywords"):
                    keywords = self._load_saved_keywords() or []
                if not keywords:
                    keywords = risk_checker.suggest_search_keywords(self._all_resume_text)
            if not keywords:
                raise ValueError("search.keywords 为空且没有可用 AI 推荐，请在 config.yaml 填写目标岗位。")
            self.logger.info("自动模式 | 城市=%s | 关键词=%s", cfg["search"].get("city"), "、".join(keywords))
            self.logger.info("匹配门槛=%s；经验上限=%s；BOSS活跃=%s",
                             filter_cfg.get("min_match_score", 55), filter_cfg.get("max_experience") or "不限",
                             filter_cfg.get("max_boss_inactive") or "不限")
            if self.browser is None:
                login = BossLogin(cfg)
                self.browser = login.login()
                self.work_tab = login.work_tab
            searcher = self._searcher = JobSearcher(self.browser, cfg)
            risk_checker.set_browser(self.browser)
            if cfg.get("risk_check", {}).get("company_lookup", False) and interactive:
                self._login_aiqicha(risk_checker)
            matchers = [JobMatcher(resume) for resume in self.resumes]
            recorder = ApplyRecorder(cfg)
            queue = RunQueue(cfg, self.resumes)
            submitter = JobSubmitter(self.browser, cfg, self.work_tab, searcher.ensure_detail_tab)
            old_success = {job_url_key(r["url"]) for r in recorder._records
                           if r.get("url") and r.get("result") in ("投递成功", "已投递过", "沟通成功")}
            handled = set()
            failures = 0
            recovery_rounds = 0
            targets = cfg['search'].get('targets') or [
                {'keyword': keyword, 'city': searcher.city, 'city_code': searcher.city_code,
                 'salary': cfg['search'].get('salary', '')} for keyword in keywords]
            for target in targets:
                if self._stop_requested():
                    return 0
                keyword = target['keyword']
                searcher.city = target['city']
                searcher.city_code = str(target.get('city_code') or JobSearcher.CITY_CODES[searcher.city])
                cfg['search']['salary'] = target.get('salary', '')
                scope = searcher.city + '｜' + keyword if cfg['search'].get('targets') else keyword
                self.logger.info('开始处理：%s / %s', searcher.city, keyword)
                prior = queue.get(scope)
                jobs = prior
                # Resume unfinished jobs first; new searches are deduplicated against them.
                for attempt in range(2):
                    try:
                        fresh = searcher.search_with_recovery(self.work_tab, keyword, self._trigger_search)
                        queue.add(scope, fresh)
                        jobs = queue.get(scope)
                        if searcher.capture_status != "ok":
                            self.logger.warning("自动搜索暂停：%s；未处理岗位已保存。", searcher.capture_reason or searcher.capture_status)
                            stats["search_error"] += 1
                            return 2
                        break
                    except Exception as exc:
                        self.logger.warning("搜索失败 (%s)，尝试恢复标签页", type(exc).__name__)
                        if attempt == 0:
                            self.work_tab = self._recover_work_tab()
                            if self.work_tab:
                                submitter.work_tab = self.work_tab
                                continue
                        stats["search_error"] += 1
                if not jobs:
                    self.logger.info('「%s」在当前条件下没有找到岗位。', keyword)
                    stats['empty_search'] += 1
                self._save_capture_log(keyword, jobs)
                stats["captured"] += len(jobs)
                for job in jobs:
                    if self._stop_requested():
                        return 0
                    _sanitize_job(job)
                    key = job_url_key(job.url)
                    if key in old_success or key in handled:
                        stats["duplicate"] += 1
                        queue.done(scope, job)
                        continue
                    try:
                        decision = self._evaluate_job(job, keyword, searcher, risk_checker, matchers)
                        result, reason, match, risk, score, resume_name = decision
                        if result:
                            recorder.record(job, match, risk, result, reason=reason, score=score, resume_name=resume_name)
                            stats[result] += 1
                            # A mismatch on this keyword can still fit a later keyword.
                            queue.done(scope, job)
                            continue
                        if search_only:
                            recorder.record(job, match, risk, "预览通过", reason="满足配置条件", score=score, resume_name=resume_name)
                            stats["preview"] += 1
                            handled.add(key)
                            # Preview does not remove eligible jobs from the submission queue.
                            continue
                        if self._stop_requested():
                            return 0
                        result = submitter.submit(job, score)
                        if result in (SubmitResult.LOGIN_REQUIRED, SubmitResult.SECURITY_REQUIRED):
                            recorder.record(job, match, risk, result, reason=submitter.last_reason, score=score, resume_name=resume_name)
                            page = searcher.ensure_detail_tab()
                            if wait_user_clear_security(page, self.logger,
                                    timeout=cfg.get("runtime", {}).get("security_wait_seconds", 180),
                                    ready=lambda page: submitter._tab_has_job(page, job.encrypt_job_id)):
                                result = submitter.submit(job, score)
                            else:
                                return 2
                        recorder.record(job, match, risk, result, reason=submitter.last_reason,
                                        score=score, resume_name=resume_name, details=submitter.last_details)
                        stats[result] += 1
                        if result in (SubmitResult.DAILY_LIMIT, SubmitResult.RUN_LIMIT):
                            self.logger.warning("已到每日或本次沟通上限，剩余岗位已保存。")
                            return 0
                        if result in (SubmitResult.LOGIN_REQUIRED, SubmitResult.SECURITY_REQUIRED):
                            return 2
                        if result in (SubmitResult.SUCCESS, SubmitResult.ALREADY_APPLIED):
                            old_success.add(key)
                            handled.add(key)
                            queue.done(scope, job)
                            failures = 0
                            if submitter.get_today_count() >= submitter.daily_limit:
                                self.logger.info('已达到本次配置的每日上限，剩余岗位保存完成。')
                                return 0
                        elif result == SubmitResult.UNCONFIRMED:
                            handled.add(key)
                            queue.done(scope, job)  # Kept in durable submission ledger for review.
                            failures += 1
                        else:
                            failures += 1
                        if failures >= cfg.get("runtime", {}).get("max_consecutive_failures", 5):
                            if recovery_rounds >= cfg.get("runtime", {}).get("recovery_rounds", 2):
                                self.logger.warning("连续失败，已保存剩余队列。请查看 apply_record.json 的具体原因。")
                                return 2
                            delay = cfg.get("runtime", {}).get("recovery_cooldown", 30)
                            self.logger.warning("连续失败，等待 %s 秒后重建详情标签继续。", delay)
                            time.sleep(delay)
                            searcher.close_detail_tab()
                            failures = 0
                            recovery_rounds += 1
                        if result != SubmitResult.ALREADY_APPLIED:
                            interval = cfg.get("submit", {}).get("interval", {"min": 8, "max": 20})
                            time.sleep(random.uniform(interval["min"], interval["max"]))
                    except Exception as exc:
                        self.logger.exception("单岗处理失败，保留待重试: %s", job.title)
                        recorder.record(job, submit_result=SubmitResult.FAILED,
                                        reason=f"{type(exc).__name__}: {exc}"[:300])
                        stats["job_error"] += 1
                        # Keep queue entry, continue other jobs rather than abort entire run.
            return 0
        except KeyboardInterrupt:
            self.logger.warning("已停止；已完成记录和未完成队列保留，下次运行自动续接。")
            return 130
        finally:
            if recorder:
                recorder.save_json()
            if stats:
                self.logger.info("本次统计: %s", dict(stats))
                from utils import safe_save_json
                safe_save_json({"stats": dict(stats), "time": datetime.now().isoformat()},
                               self.config.get("output", {}).get("summary_file", "last_run_summary.json"))
            if self.browser:
                try:
                    self._searcher.close_detail_tab() if self._searcher else None
                    if not self.config.get("browser", {}).get("keep_open", True):
                        self.browser.quit()
                except Exception:
                    pass

    def _stop_requested(self):
        if resolve_path('stop_requested.flag').exists():
            self.logger.info('收到停止请求；进度已保存。双击启动.bat 可继续。')
            return True
        return False

    def _evaluate_job(self, job, keyword, searcher, risk_checker, matchers):
        from relevance import keyword_matches, contains_term
        f = self.config.get("filter", {})
        def reject(reason, result="条件不符"):
            return result, reason, None, None, None, ""
        if any(contains_term(job.title, k) for k in f.get("exclude_titles", [])):
            return reject("标题命中排除词")
        if any(contains_term(job.company, k) for k in f.get("exclude_companies", [])):
            return reject("公司命中排除词")
        if self._job_wrong_city(job, searcher.city):
            return reject("工作地点与配置城市不符: " + job.location)
        if f.get("max_education") and self._education_too_high(job.education, f["max_education"]):
            return reject("学历要求超过配置上限: " + job.education)
        if f.get("max_experience") and self._experience_too_high(job, f["max_experience"]):
            return reject("经验要求超过配置上限: " + job.experience)
        if f.get("job_type") == "internship" and not any(w in job.title + job.experience for w in ("实习", "在校")):
            return reject("仅选择实习岗位")
        if f.get('job_type') == 'fulltime' and any(w in job.title + job.experience for w in ('实习', '在校', '兼职')):
            return reject('在线简历只选择全职岗位')
        need_relevance = not keyword_matches(job.title, keyword, job.description, job.skills_required)
        need_desc = len(job.description or "") < 30
        need_boss = bool(f.get("require_boss_online") or f.get("max_boss_inactive"))
        if need_relevance or need_desc or (need_boss and self._needs_boss_detail(job, f.get("require_boss_online"))):
            searcher.fetch_job_detail(self.work_tab, job, need_description=True, need_boss_status=need_boss)
        if not keyword_matches(job.title, keyword, job.description, job.skills_required):
            return reject("标题/JD/技能均不符合搜索方向: " + keyword, "方向不符")
        if f.get("require_description", False) and len(job.description or "") < 30:
            return reject("缺少可核实岗位描述", "资料不足")
        salary_reason = self._salary_rejection_reason(job, self.config.get('search', {}).get('salary', ''))
        if salary_reason:
            return reject(salary_reason)
        if f.get("require_boss_online") and job.boss_online is not True:
            return reject("BOSS未确认在线", "BOSS不在线")
        max_rank = parse_max_boss_inactive(f.get("max_boss_inactive", ""))
        if max_rank is not None and not self._boss_active_ok(job, max_rank):
            if job.boss_active_time or f.get("unknown_boss_activity", "allow") == "skip":
                return reject("BOSS活跃度不符: " + (job.boss_active_time or "未知"), "BOSS不活跃")
        risk = risk_checker.check(job.company, job.to_dict())
        blocked = self._parse_risk_block_levels(self.config.get("risk_check", {}).get("block_levels", ["high"]))
        if risk.level in blocked:
            return SubmitResult.RISK_REJECTED, "; ".join(risk.reasons), None, risk, None, ""
        if f.get("skip_kpi", True):
            kpi = risk_checker.check_kpi(job.to_dict(), job.company)
            if kpi.score >= f.get("skip_kpi_score", 70):
                return SubmitResult.KPI_REJECTED, "; ".join(kpi.reasons), None, risk, None, ""
        # Match primary title requirements per resume before comparing scores.
        eligible_matchers = [m for m in matchers if not f.get('require_title_skills', True)
                             or not m.missing_title_skills(job)]
        if not eligible_matchers:
            missing = min((m.missing_title_skills(job) for m in matchers), key=len)
            return reject('简历缺少职位标题明确要求的技能: ' + '、'.join(missing), '方向不符')
        # Pick one complete resume instead of combining unrelated skills across files.
        candidates = [(m.match(job), m.resume) for m in eligible_matchers]
        match, resume = max(candidates, key=lambda item: item[0].total_score)
        score = match.total_score
        reason = "规则匹配"
        if f.get("ai_fit_check") and risk_checker.ai_available:
            ai = risk_checker.match_job(resume.raw_text, job.to_dict())
            if ai is not None:
                score = ai["score"]
                reason = "; ".join(ai.get("reasons", []))
                if not ai["fit"] or score < f.get("ai_fit_min_score", 40):
                    return SubmitResult.MATCH_LOW, reason, match, risk, score, resume.parsed_from
                return None, reason, match, risk, score, resume.parsed_from
        if score < f.get("min_match_score", 55):
            return SubmitResult.MATCH_LOW, f"规则匹配 {score:.1f} < {f.get('min_match_score', 55)}", match, risk, score, resume.parsed_from
        return None, reason, match, risk, score, resume.parsed_from

    @staticmethod
    def _salary_rejection_reason(job, salary_filter):
        """有月薪要求时，起薪必须满足底线，不能仅凭最高薪资与目标重叠。"""
        from utils import extract_salary_range
        lo, hi = extract_salary_range(salary_filter)
        if not hi:
            return ''
        salary = job.salary or ''
        if any(unit in salary for unit in ('/天', '元/日', '/时', '/小时', '/年', '日薪', '时薪', '年薪')):
            return '岗位非月薪，无法确认月薪底线: ' + salary
        job_lo, job_hi = extract_salary_range(salary)
        if not job_lo or not job_hi or job_lo > job_hi:
            return '岗位薪资不明，无法确认月薪底线: ' + (salary or '未标注')
        if job_lo < lo:
            return '最低月薪低于配置下限: ' + salary
        if job_lo > hi:
            return '最低月薪超过配置上限: ' + salary
        return ''

    @staticmethod
    def _parse_risk_block_levels(levels) -> set:
        mapping = {
            "high": RiskLevel.HIGH,
            "medium": RiskLevel.MEDIUM,
            "low": RiskLevel.LOW,
            "safe": RiskLevel.SAFE,
        }
        if not levels:
            return {RiskLevel.HIGH}
        parsed = {mapping[str(lv).lower()] for lv in levels if str(lv).lower() in mapping}
        return parsed or {RiskLevel.HIGH}

    @classmethod
    def _needs_boss_detail(cls, job: JobPosting, require_boss_online: bool) -> bool:
        """是否仍需打开详情页补全 BOSS 状态"""
        if job.boss_online is True:
            return False
        if require_boss_online:
            return True
        return not bool(job.boss_active_time)

    @classmethod
    def _boss_active_ok(cls, job: JobPosting, max_rank: int) -> bool:
        """方案B：BOSS 活跃等级是否在允许范围内"""
        if job.boss_online is True:
            return True
        active = (job.boss_active_time or "").strip()
        if active in ("在线", "刚刚活跃", "刚刚在线"):
            return True
        rank = boss_active_rank(active)
        if rank == 99:
            return False
        return rank <= max_rank

    _EDU_RANKS = {"学历不限":0,"不限":0,"高中":1,"中专":2,"大专":3,
                  "本科":4,"学士":4,"硕士":5,"研究生":5,
                  "博士":6,"博士后":6}

    @classmethod
    def _education_too_high(cls, job_edu: str, max_edu: str) -> bool:
        """岗位要求的学历是否超过限制"""
        if not job_edu: return False
        jr = max((cls._EDU_RANKS.get(k, 0) for k in cls._EDU_RANKS if k in job_edu), default=0)
        mr = cls._EDU_RANKS.get(max_edu, 4)
        return jr > mr

    _JAVA_FILTER = {"java", "javase", "javaee", "spring", "springboot",
                    "springcloud", "mybatis", "hibernate", "jvm", "jdk",
                    "tomcat", "servlet", "jsp", "maven", "gradle", "struts"}

    @classmethod
    def _is_java_job(cls, job) -> bool:
        """判断岗位是否 Java 相关（标题/技能/描述任一命中）"""
        text = (job.title + " " + " ".join(job.skills_required or [])
                + " " + (job.description or "")).lower()
        text = text.replace("javascript", "")  # JS 不是 Java
        return any(kw in text for kw in cls._JAVA_FILTER)

    def _build_search_url(self, keyword, searcher):
        return searcher.build_search_url(keyword)

    def _force_boss_city(self, tab, searcher):
        """把浏览器 lastCity / 当前站写成配置城市，避免掉回青岛等默认城"""
        code = searcher.city_code
        city = searcher.city
        try:
            tab.set.cookies({
                "name": "lastCity",
                "value": str(code),
                "domain": ".zhipin.com",
                "path": "/",
            })
        except Exception:
            pass
        try:
            tab.run_js(
                f"document.cookie='lastCity={code}; domain=.zhipin.com; path=/; max-age=31536000';"
            )
        except Exception:
            pass
        try:
            # 先落到配置城市站，减少 /qingdao/ 首页干扰
            tab.get(f"https://www.zhipin.com/web/geek/job?city={code}")
            tab.wait(2)
            self.logger.info(f"  🌏 已切换到配置城市：{city} ({code})")
        except Exception as e:
            self.logger.warning(f"  ⚠️ 切换城市失败: {e}")

    def _job_wrong_city(self, job, city):
        # Company HQ/name must not override the actual workplace.
        location = (job.location or '').strip()
        if not city or not location or city in location or str(city).isdigit():
            return False
        return any(other in location for other in JobSearcher.CITY_CODES if other != city)

    def _trigger_search(self, tab, keyword, searcher):
        if is_boss_security_page(tab, strict=True):
            searcher.capture_status = 'security'
            searcher.capture_reason = '网站仍在校验，等待恢复后自动搜索'
            return False
        tab.get(self._build_search_url(keyword, searcher))
        wait_for_job_cards(tab, timeout=4)
        # Let the listener capture the server's reason even after navigation.
        if is_boss_security_page(tab, strict=True):
            return True
        from urllib.parse import urlsplit
        if any(x in urlsplit(tab.url).path for x in ('/passport', '/login')):
            searcher.capture_status = 'login'
            searcher.capture_reason = '搜索跳转到了登录页面'
            return False
        if not searcher._search_context_matches(tab, keyword):
            searcher.capture_status = 'redirected'
            searcher.capture_reason = '搜索被重定向，尚未进入目标关键词与城市的结果页'
            return False
        # A slow list is handled by the listener/DOM wait; do not submit a second
        # UI search just because cards take more than four seconds to render.
        return True

    def _ui_search(self, tab, keyword):
        from DrissionPage.common import Keys
        if not tab or is_boss_security_page(tab, strict=True):
            return False
        selectors = ('input[placeholder*="职位"], input[placeholder*="搜索"], '
                     'input[placeholder*="公司"], input[type="search"], .search-input input, input.search-input')
        inputs = tab.eles('css:' + selectors)
        entry = next((e for e in inputs if e.states.is_displayed), None)
        if not entry:
            return False
        entry.input(keyword, clear=True)
        buttons = tab.eles('css:button.btn-search, .btn-search, .search-btn, button[ka*="search"]')
        button = next((e for e in buttons if e.states.is_displayed), None)
        if button:
            button.click()
        else:
            entry.input(Keys.ENTER)
        return wait_for_job_cards(tab, timeout=6)

    def _recover_work_tab(self):
        """标签页断连时尝试恢复 BOSS 工作页"""
        self.logger.warning("  ⚠️ 浏览器标签页断连，尝试恢复...")
        try:
            for t in self.browser.get_tabs():
                u = (t.url or "").lower()
                if "zhipin.com" in u:
                    self.work_tab = t
                    return t
            self.work_tab = self.browser.new_tab("https://www.zhipin.com")
            self.work_tab.wait(3)
            return self.work_tab
        except Exception as e:
            self.logger.error(f"  ❌ 无法恢复工作标签页: {e}")
            return None

    def _scroll(self, tab):
        for _ in range(3):
            try: tab.run_js("window.scrollBy(0, 500)")
            except: pass
            time.sleep(0.3)

    def _scroll_bottom(self, tab):
        try: tab.run_js("window.scrollTo(0, document.body.scrollHeight)")
        except: pass
        time.sleep(2)

    def _login_aiqicha(self, risk_checker=None):
        """打开爱企查让用户登录，保留标签页供后续公司查询"""
        print()
        self.logger.info("=" * 55)
        self.logger.info("  🏢 请在浏览器中登录爱企查 aiqicha.baidu.com")
        self.logger.info("     登录后公司信息查询会更准确")
        self.logger.info("     ⚠️ 如果不需要，直接按 Enter 跳过")
        self.logger.info("=" * 55)
        aiqicha_tab = None
        try:
            aiqicha_tab = self.browser.new_tab("https://aiqicha.baidu.com")
            aiqicha_tab.wait(3)
            try:
                url = aiqicha_tab.url.lower()
                body = (aiqicha_tab.run_js("return document.body? document.body.innerText : ''") or "").lower()
                if "个人中心" in body or "user" in url:
                    self.logger.info("  ✅ 爱企查已登录")
            except Exception:
                pass
            input("  按 Enter 继续...")
        except EOFError:
            pass
        finally:
            if risk_checker and aiqicha_tab:
                risk_checker.set_aiqicha_tab(aiqicha_tab)
                self.logger.info("  ✅ 爱企查标签页已保留供公司查询")
            elif aiqicha_tab:
                try:
                    aiqicha_tab.close()
                except Exception:
                    pass
            if self.work_tab:
                try:
                    self.work_tab.get("https://www.zhipin.com/web/geek/job")
                    self.work_tab.wait(1)
                except Exception:
                    pass

    def _print_stats(self, stats: dict):
        self.logger.info(f"\n{'='*40}")
        self.logger.info(
            f"✅投递{stats['success']} ⛔风险{stats['risk']} "
            f"🚫KPI{stats['kpi']} 🎯AI不适合{stats['fit']} "
            f"⏭️匹配{stats['match']} 📋已投{stats['dup']} "
            f"👤离线{stats.get('boss_offline', 0)} 💤不活跃{stats.get('boss_inactive', 0)} "
            f"⚠️已沟通{stats['already']} 🔇过滤{stats['excl']} ❌失败{stats['fail']}"
        )

    # ==================== 多简历 & 关键词筛选 ====================

    def _validate_resumes(self):
        """启动时校验简历是否有效，避免误把日志文件当简历"""
        if not self.resumes:
            raise FileNotFoundError("未找到简历文件！")
        r = self._primary_resume
        text_len = len((r.raw_text or "").strip())
        if text_len < 80:
            raise ValueError(
                f"简历「{r.parsed_from}」内容过短（{text_len} 字符）。\n"
                f"  请将 PDF/DOCX 简历放入 resumes/ 文件夹，文件名含「简历」或 resume。\n"
                f"  当前 resumes/ 为空时会误读项目里的 .txt 日志文件。"
            )
        if len(r.skills) == 0:
            self.logger.warning(
                f"  ⚠️ 简历「{r.parsed_from}」未识别到技能关键词，"
                f"将依赖全文 {text_len} 字符做 AI 匹配"
            )

    def _build_combined_resume_text(self) -> str:
        """合并所有简历文本，标注来源，供 AI 匹配时使用"""
        if not self.resumes:
            return ""
        if len(self.resumes) <= 1:
            r = self.resumes[0]
            skills_str = "、".join(r.skills[:40]) if r.skills else ""
            head = f"技能：{skills_str}\n\n" if skills_str else ""
            return head + (r.raw_text or "")
        parts = []
        for i, r in enumerate(self.resumes):
            # 提取技能摘要，避免全文过长
            skills_str = "、".join(r.skills[:30]) if r.skills else "（未识别到技能）"
            parts.append(
                f"【简历{i+1}：{r.parsed_from}】\n"
                f"技能：{skills_str}\n"
                f"全文：{r.raw_text[:2000]}"
            )
        return "\n\n".join(parts)

    def _resolve_resume_paths(self, cli_resume: Optional[str] = None) -> list:
        """解析简历文件路径列表

        优先级: CLI --resume > 配置 resume_dir 扫描 > 配置 file_paths >
                配置 file_path > 自动扫描
        """
        # 1) CLI 参数（单文件，兼容旧行为）
        if cli_resume:
            p = resolve_path(cli_resume)
            if p.exists():
                return [p]
            raise FileNotFoundError(f"CLI 指定的简历不存在: {cli_resume}")

        resume_cfg = self.config.get("resume", {})

        # 2) 配置 resume_dir — 扫描整个文件夹
        resume_dir = resume_cfg.get("resume_dir", "")
        if resume_dir:
            dir_path = resolve_path(resume_dir)
            if dir_path.is_dir():
                files = find_all_resume_files(dir_path)
                if files:
                    self.logger.info(f"从 {resume_dir}/ 扫描到 {len(files)} 份简历")
                    return files
                self.logger.warning(f"简历文件夹为空: {resume_dir}")
            else:
                self.logger.warning(f"简历文件夹不存在: {resume_dir}，创建空目录")
                dir_path.mkdir(parents=True, exist_ok=True)

        # 3) 配置 file_paths 列表
        file_paths = resume_cfg.get("file_paths", [])
        if file_paths:
            paths = []
            for fp in file_paths:
                p = resolve_path(fp)
                if p.exists():
                    paths.append(p)
                else:
                    self.logger.warning(f"简历不存在，跳过: {fp}")
            if paths:
                return paths

        # 4) 配置 file_path 单个（兼容旧配置）
        single = resume_cfg.get("file_path", "")
        if single:
            p = resolve_path(single)
            if p.exists():
                return [p]
            self.logger.warning(f"简历不存在: {single}")

        # 5) 不再从项目根目录随意抓取 .txt
        return []

    def _review_keywords(
        self, recommended: list[str], fallback: list[str]
    ) -> list[str]:
        """交互式关键词筛选 + 排序

        显示 AI 推荐关键词 → 用户可排序/删除/修改/添加/确认
        列表顺序 = 投递优先级，排前面的先搜先投
        """
        keywords = list(recommended) if recommended else list(fallback)
        if not keywords:
            self.logger.error("无可用关键词！")
            return []

        print("\n" + "=" * 55)
        print("  📋 岗位搜索关键词（可编辑 & 排序）")
        print("  列表顺序 = 投递优先级，排前面的先搜先投")
        print("=" * 55)
        if recommended:
            print(f"  来自 {len(self.resumes)} 份简历的 AI 推荐：")

        self._print_keywords(keywords)

        print("\n  操作:")
        print("    删除:  输入编号（如 2 或 2,5 或 1-3）")
        print("    排序:  move 3 1   （把第3项移到第1位）")
        print("    修改:  edit 2 新名字")
        print("    添加:  + 关键词")
        print("    重置:  reset   确认: 直接回车")

        while True:
            try:
                cmd = input("\n  > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not cmd:
                break  # 确认

            if cmd.lower() == "reset":
                keywords = list(recommended) if recommended else list(fallback)
                self._print_keywords(keywords)
                continue

            # ---- 排序: move <from> <to> ----
            if cmd.lower().startswith("move ") or cmd.lower().startswith("mv "):
                parts = cmd.split()
                if len(parts) == 3:
                    try:
                        fr = int(parts[1]) - 1
                        to = int(parts[2]) - 1
                        if 0 <= fr < len(keywords) and 0 <= to < len(keywords):
                            kw = keywords.pop(fr)
                            keywords.insert(to, kw)
                            print(f"  🔄 已将「{kw}」从 #{fr+1} 移到 #{to+1}")
                            self._print_keywords(keywords)
                        else:
                            print(f"  ❌ 编号超出范围 (1-{len(keywords)})")
                    except ValueError:
                        print("  ❌ 格式: move 从编号 到编号")
                else:
                    print("  ❌ 格式: move 从编号 到编号（如 move 3 1）")
                continue

            # ---- 修改: edit <idx> <new_name> ----
            if cmd.lower().startswith("edit "):
                parts = cmd.split(maxsplit=2)
                if len(parts) >= 3:
                    try:
                        idx = int(parts[1]) - 1
                        if 0 <= idx < len(keywords):
                            old = keywords[idx]
                            keywords[idx] = parts[2]
                            print(f"  ✏️  #{idx+1} 「{old}」→「{parts[2]}」")
                            self._print_keywords(keywords)
                        else:
                            print(f"  ❌ 编号 {parts[1]} 超出范围")
                    except ValueError:
                        print("  ❌ 格式: edit 编号 新名称")
                else:
                    print("  ❌ 格式: edit 编号 新名称")
                continue

            # ---- 添加 ----
            if cmd.startswith("+"):
                new_kw = cmd[1:].strip()
                if new_kw:
                    if new_kw not in keywords:
                        keywords.append(new_kw)
                        print(f"  ➕ 已添加: 「{new_kw}」")
                        self._print_keywords(keywords)
                    else:
                        print(f"  ⚠️ 「{new_kw}」已在列表中")
                continue

            # ---- 删除 ----
            try:
                indices = []
                for part in cmd.replace("，", ",").split(","):
                    part = part.strip()
                    if "-" in part:
                        a, b = part.split("-", 1)
                        indices.extend(range(int(a.strip()) - 1, int(b.strip())))
                    else:
                        indices.append(int(part) - 1)
                removed = []
                for i in sorted(set(indices), reverse=True):
                    if 0 <= i < len(keywords):
                        removed.append(keywords.pop(i))
                if removed:
                    print(f"  ➖ 已删除: {', '.join(f'「{r}」' for r in reversed(removed))}")
                    self._print_keywords(keywords)
                else:
                    print("  ❌ 编号超出范围")
            except ValueError:
                print("  ❌ 无效输入，请重新输入")

        if not keywords:
            self.logger.warning("关键词列表为空，使用配置文件默认值")
            keywords = list(fallback)

        print(f"\n  ✅ 最终关键词 ({len(keywords)} 个): {', '.join(keywords)}")
        print("=" * 55 + "\n")
        return keywords

    _EXP_RANKS = {"不限":0,"经验不限":0,"应届生":1,"在校生":1,"1年以内":2,
                  "1-3年":3,"3-5年":4,"5-10年":5,"10年以上":6}

    @classmethod
    def _experience_too_high(cls, job, max_exp=''):
        if not max_exp or max_exp in ('不限', '经验不限'):
            return False
        caps = {'应届生': 0, '在校生': 0, '1年以内': 1, '1-3年': 3, '3-5年': 5, '5-10年': 10, '10年以上': 99}
        max_exp = str(max_exp).strip()
        cap = caps.get(max_exp)
        if cap is None:
            exact = re.fullmatch(r'(\d+)\s*年', max_exp)
            if not exact:
                raise ValueError('无法识别经验上限: ' + max_exp)
            cap = int(exact.group(1))
        exp = (job.experience or '').strip()
        if any(k in exp for k in ('不限', '应届', '在校', '实习')):
            return False
        numbers = re.findall(r'\d+', exp)
        if numbers:
            # Compare the minimum required years, not an enum rank or the upper end of a band.
            return int(numbers[0]) > cap
        if job.description:
            years = re.findall(r'(?:至少|具备|拥有|要求)?\s*(\d+)\s*年(?:以上|及以上).*?(?:经验|经历)', job.description[:1000])
            if years and max(map(int, years)) > cap:
                return True
        return False

    @staticmethod
    def _print_keywords(keywords: list[str]):
        """打印编号关键词列表"""
        for i, kw in enumerate(keywords, 1):
            print(f"    [{i}] {kw}")

    # ==================== 手动筛选设置 ====================

    def _configure_filters_interactive(self, filter_cfg: dict) -> tuple:
        """手动选择工作经验/学历筛选条件"""
        exp_options = [
            ("1", "应届生/在校生", "应届生"),
            ("2", "1年以内", "1年以内"),
            ("3", "1-3年", "1-3年"),
            ("4", "3-5年", "3-5年"),
            ("5", "5-10年", "5-10年"),
            ("6", "10年以上", "10年以上"),
            ("0", "不限（不过滤）", ""),
        ]
        edu_options = [
            ("1", "大专及以下", "大专"),
            ("2", "本科", "本科"),
            ("3", "硕士/研究生", "硕士"),
            ("4", "博士", "博士"),
            ("0", "不限（不过滤）", ""),
        ]
        default_exp = filter_cfg.get("max_experience", "")
        default_edu = filter_cfg.get("max_education", "")

        print("\n" + "=" * 50)
        print("  ⚙️  筛选条件设置（直接回车=使用默认值）")
        print("=" * 50)

        # 工作经验
        print("\n  📅 最高可接受的工作经验：")
        exp_default_num = "0"
        for num, label, val in exp_options:
            mark = " ← 默认" if val == default_exp else ""
            if val == default_exp:
                exp_default_num = num
            print(f"    [{num}] {label}{mark}")
        try:
            ans = input(f"\n  选择 [{exp_default_num}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        max_exp = default_exp
        for num, _, val in exp_options:
            if ans == num:
                max_exp = val
                break

        # 学历
        print("\n  🎓 最高可接受的学历要求：")
        edu_default_num = "0"
        for num, label, val in edu_options:
            mark = " ← 默认" if val == default_edu else ""
            if val == default_edu:
                edu_default_num = num
            print(f"    [{num}] {label}{mark}")
        try:
            ans = input(f"\n  选择 [{edu_default_num}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        max_edu = default_edu
        for num, _, val in edu_options:
            if ans == num:
                max_edu = val
                break

        print(f"\n  ✅ 经验≤{max_exp or '不限'} | 学历≤{max_edu or '不限'}")
        print("=" * 50)
        return max_exp, max_edu

    # ==================== 关键词持久化 ====================

    def _keywords_file(self) -> Path:
        return resolve_path("keywords.json")

    def _save_capture_log(self, keyword: str, jobs: list[JobPosting]):
        """将捕获的岗位信息以简洁方式保存到 capture_log.txt"""
        lines = [
            f"搜索关键词: {keyword}",
            f"捕获数量: {len(jobs)}",
            f"更新时间: {datetime.now().strftime('%H:%M:%S')}",
            "-" * 70,
        ]
        for i, j in enumerate(jobs, 1):
            title = j.title[:35] if j.title else ""
            salary = j.salary[:12] if j.salary else "面议"
            company = j.company[:25] if j.company else "未知"
            location = j.location[:8] if j.location else ""
            if j.boss_online is True:
                boss = "在线"
            elif j.boss_online is False:
                boss = "离线"
            elif j.boss_active_time:
                boss = j.boss_active_time[:8]
            else:
                boss = "?"
            lines.append(
                f"[{i:3d}] {title:35s} | {salary:12s} | {company:20s} | {location} | BOSS:{boss}"
            )
        cap_file = resolve_path("capture_log.txt")
        cap_file.write_text("\n".join(lines), encoding="utf-8")

    def _load_or_review_keywords(
        self, cfg: dict, filter_cfg: dict, config_keywords: list[str]
    ) -> list[str]:
        """加载已保存的关键词，或重新推荐 + 审核

        流程：
          有保存 → 询问直接使用 / 修改 / 重新AI推荐
          无保存 → AI推荐 → 人工审核 → 保存
        """
        saved = self._load_saved_keywords()

        if saved:
            keywords = self._handle_saved_keywords(saved, cfg, filter_cfg, config_keywords)
        else:
            keywords = self._run_ai_and_review(cfg, filter_cfg, config_keywords)

        # 确认后保存
        if keywords:
            self._save_keywords(keywords)
        return keywords

    def _load_saved_keywords(self) -> list[str] | None:
        kf = self._keywords_file()
        if not kf.exists():
            return None
        try:
            data = json.loads(kf.read_text(encoding="utf-8"))
            kw = data.get("keywords", [])
            if isinstance(kw, list) and kw:
                return kw
        except Exception:
            pass
        return None

    def _handle_saved_keywords(
        self, saved: list[str], cfg: dict, filter_cfg: dict, config_keywords: list[str]
    ) -> list[str]:
        """询问用户对已保存关键词的处理方式"""
        print("\n" + "=" * 55)
        print("  📦 检测到上次保存的搜索关键词：")
        print("=" * 55)
        self._print_keywords(saved)
        print(f"\n  上次保存时间: {self._keywords_saved_time()}")

        while True:
            try:
                ans = input("\n  直接使用? [y]是/[m]修改/[n]重新AI推荐: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return saved

            if ans in ("y", "yes", ""):
                print("  ✅ 使用已保存的关键词\n")
                return saved
            elif ans in ("m", "modify"):
                print("  📝 进入编辑模式...")
                return self._review_keywords(saved, config_keywords)
            elif ans in ("n", "no", "new"):
                print("  🤖 重新 AI 推荐...")
                return self._run_ai_and_review(cfg, filter_cfg, config_keywords)
            else:
                print("  ❌ 请输入 y / m / n")


    def _run_ai_and_review(
        self, cfg: dict, filter_cfg: dict, config_keywords: list[str]
    ) -> list[str]:
        """AI 推荐关键词 → 人工审核"""
        ai_keywords: list[str] = []
        if filter_cfg.get("ai_fit_check") and self.resumes:
            from company_risk import CompanyRiskChecker
            tmp_checker = CompanyRiskChecker(cfg)
            all_kw: list[str] = []
            for i, r in enumerate(self.resumes):
                self.logger.info(f"  AI 分析简历{i+1}「{r.parsed_from}」推荐关键词...")
                kw = tmp_checker.suggest_search_keywords(r.raw_text[:3000])
                if kw:
                    self.logger.info(f"    推荐: {', '.join(kw)}")
                    all_kw.extend(kw)
                else:
                    self.logger.info(f"    推荐失败，跳过")
            seen_kw = set()
            for k in all_kw:
                if k not in seen_kw:
                    seen_kw.add(k)
                    ai_keywords.append(k)
            if not ai_keywords:
                self.logger.info("  AI关键词推荐失败，使用配置文件关键词")

        return self._review_keywords(
            ai_keywords or config_keywords,
            config_keywords,
        )

    def _save_keywords(self, keywords: list[str]):
        """保存关键词到 JSON"""
        data = {
            "keywords": keywords,
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "resume_count": len(self.resumes),
        }
        kf = self._keywords_file()
        kf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.logger.info(f"关键词已保存到 {kf.name}")

    def _keywords_saved_time(self) -> str:
        kf = self._keywords_file()
        try:
            data = json.loads(kf.read_text(encoding="utf-8"))
            return data.get("saved_at", "未知")
        except Exception:
            return "未知"


def _setup_console_encoding():
    """Windows 控制台默认 GBK，避免 print 表情符号时报错"""
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass


def main():
    _setup_console_encoding()
    p = argparse.ArgumentParser(description="BOSS直聘自动投递")
    p.add_argument("--config", "-c", default="config.yaml")
    p.add_argument("--resume", "-r", default=None, help="简历文件路径")
    p.add_argument("--search-only", "-s", action="store_true")
    p.add_argument("--resume-only", "-ro", action="store_true")
    p.add_argument("--interactive", action="store_true", help="启用关键词/筛选交互菜单")
    p.add_argument("--doctor", action="store_true", help="检查配置和依赖，不登录不投递")
    args = p.parse_args()

    for name in ["app","resume","login","jobs","risk","matcher","submit","recorder"]:
        setup_logger(name, "INFO")

    from filelock import FileLock, Timeout
    from diagnostics import diagnose
    if args.doctor:
        return diagnose(args.config)
    try:
        app = AutoBossApp(args.config)
        with FileLock(str(resolve_path('.auto_boss.lock')), timeout=0):
            return app.run(search_only=args.search_only, resume_only=args.resume_only,
                           resume_path=args.resume, interactive=args.interactive)
    except Timeout:
        print('已有程序在运行，不能同时启动两个投递任务。')
        return 2
    except Exception as exc:
        get_logger('app').exception('运行失败: %s', exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
