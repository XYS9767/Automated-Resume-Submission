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
        self.config = load_config(config_path)
        self.logger = get_logger("app")
        self.browser = None
        self.work_tab = None
        self.resumes: list[ResumeData] = []
        self._primary_resume: Optional[ResumeData] = None
        self.logger.info(f"启动 {datetime.now():%Y-%m-%d %H:%M:%S}")
        self._searcher = None
        self._boss_url_nav_blocked = False

    def run(self, search_only: bool = False, resume_only: bool = False,
            resume_path: Optional[str] = None):
        try:
            # 1. 简历解析 — 支持多简历
            self.logger.info("解析简历...")
            paths = self._resolve_resume_paths(resume_path)
            if not paths:
                raise FileNotFoundError(
                    "未找到简历文件！\n"
                    "  请将 PDF/DOCX 简历放入 resumes/ 文件夹后再运行。\n"
                    "  示例：resumes/运维工程师简历.pdf"
                )
            for p in paths:
                self.logger.info(f"  解析: {p.name}")
                parser = ResumeParser(str(p))
                self.resumes.append(parser.parse())
            self._primary_resume = self.resumes[0]
            # 合并所有简历文本，AI 匹配时使用全部技能
            self._all_resume_text = self._build_combined_resume_text()
            self._validate_resumes()
            self.logger.info(
                f"共解析 {len(self.resumes)} 份简历"
                f"，文本 {len(self._all_resume_text)} 字符"
                f"，技能 {len(self._primary_resume.skills)} 项"
            )
            if resume_only:
                return

            # 2. 确定搜索关键词：上次保存 > AI 推荐 > 配置文件
            cfg = self.config
            filter_cfg = cfg.get("filter", {})
            config_keywords = cfg["search"]["keywords"]
            keywords = self._load_or_review_keywords(cfg, filter_cfg, config_keywords)

            # 3. 手动选择工作经验 / 学历筛选
            max_exp, max_edu = self._configure_filters_interactive(filter_cfg)

            # 4. 登录
            self.logger.info("登录 BOSS直聘...")
            login = BossLogin(self.config)
            self.browser = login.login()
            self.work_tab = login.work_tab

            # 5. 初始化模块
            searcher = JobSearcher(self.browser, self.config)
            self._searcher = searcher
            # 搜索经验与交互筛选对齐，避免「搜 3-5 年却只收 ≤1-3 年」全被跳过
            if max_exp:
                searcher.experience = max_exp
                searcher.exp_code = searcher.EXPERIENCE_CODES.get(max_exp, "")
            self.logger.info(
                f"搜索城市={searcher.city}({searcher.city_code}) | "
                f"经验条件={searcher.experience or '不限'}"
                f"{'(' + searcher.exp_code + ')' if searcher.exp_code else ''}"
            )
            risk_checker = CompanyRiskChecker(self.config)
            risk_checker.set_browser(self.browser)
            self._login_aiqicha(risk_checker)

            # 爱企查完成后：必须先解除 code=37，否则无法开聊/打开详情
            tab0 = self.work_tab or self.browser.latest_tab
            if not ensure_boss_api_ready(tab0, self.logger, timeout=300):
                self.logger.error(
                    "BOSS API 未解锁，自动投递无法进行。"
                    "请换网络/隔几小时后再试，或在普通 Edge（非本程序）里确认能正常沟通岗位。"
                )
                return
            self._boss_url_nav_blocked = False
            # 强制把账号 lastCity 写成配置城市，避免搜索框回落到青岛等默认城
            self._force_boss_city(tab0, searcher)

            matcher = JobMatcher(self._primary_resume)
            submitter = JobSubmitter(
                self.browser, self.config,
                work_tab=self.work_tab,
                get_detail_tab=searcher.ensure_detail_tab,
            )
            recorder = ApplyRecorder(self.config)

            self.logger.info(f"今日已投 {submitter.get_today_count()}/{submitter.daily_limit}")
            if submitter.get_today_count() >= submitter.daily_limit:
                self.logger.warning(f"今日已达上限 {submitter.daily_limit}，退出")
                return

            # 加载历史成功URL（跨运行去重）
            old_success = set()
            for r in recorder._records:
                if r.get("result") == "投递成功" and r.get("url"):
                    old_success.add(r["url"])
            self.logger.info(f"历史已投递 {len(old_success)} 个")

            min_score = filter_cfg.get("min_match_score", 40)
            skip_kpi = filter_cfg.get("skip_kpi", True)
            kpi_threshold = filter_cfg.get("skip_kpi_score", 60)
            exclude_titles = [k.lower() for k in filter_cfg.get("exclude_titles", [])]

            risk_cache: dict[str, RiskResult] = {}
            risk_cfg = cfg.get("risk_check", {})
            block_levels = self._parse_risk_block_levels(risk_cfg.get("block_levels", ["high"]))

            ai_fit_check = filter_cfg.get("ai_fit_check", False)
            ai_fit_min = filter_cfg.get("ai_fit_min_score", 40)
            require_boss_online = filter_cfg.get("require_boss_online", False)
            max_boss_rank = parse_max_boss_inactive(filter_cfg.get("max_boss_inactive", ""))

            stats = {"success": 0, "fail": 0, "risk": 0, "kpi": 0, "fit": 0,
                     "match": 0, "dup": 0, "excl": 0, "already": 0,
                     "boss_offline": 0, "boss_inactive": 0}

            # ---- 主循环：每个关键词依次全量处理 ----
            for keyword in keywords:
                self.logger.info(f"\n{'='*40}")
                self.logger.info(f"🔍 {keyword}")

                # 每个关键词开始时清除上次捕获记录
                cap_file = resolve_path("capture_log.txt")
                cap_file.write_text("", encoding="utf-8")

                # 监听 + 触发搜索（固定使用 BOSS 工作标签页，避免 latest_tab 指到爱企查）
                tab = self.work_tab or self.browser.latest_tab
                if not self._trigger_search(tab, keyword, searcher):
                    self.logger.warning(f"  ⚠️ 搜索触发失败，跳过关键词「{keyword}」")
                    continue

                all_jobs: list[JobPosting] = []
                seen = set()

                # 仅明确验证 URL 才停；有卡片则继续
                if is_boss_security_page(tab, strict=True) and not has_job_cards(tab):
                    if not wait_user_clear_security(tab, self.logger):
                        self.logger.warning(f"  ⚠️ 跳过关键词「{keyword}」")
                        continue
                    tab = self.work_tab or self.browser.latest_tab

                # 优先 CDP API（最可靠）
                self.logger.info("  尝试 CDP API 搜索...")
                if not is_boss_security_page(tab, strict=True):
                    api_jobs = searcher.fetch_jobs_cdp(tab, keyword)
                    for j in api_jobs:
                        if j.url and j.url not in seen:
                            seen.add(j.url)
                            all_jobs.append(j)
                    if all_jobs:
                        self._save_capture_log(keyword, all_jobs)
                        self.logger.info(f"  📡 CDP API 共 {len(all_jobs)} 个岗位")

                # 补充：listen 捕获
                if not all_jobs and not is_boss_security_page(tab, strict=True):
                    searcher.start_capture(tab)
                    captured = searcher.wait_capture(tab, keyword)
                    if captured:
                        for j in captured:
                            if j.url and j.url not in seen:
                                seen.add(j.url)
                                all_jobs.append(j)
                        self._save_capture_log(keyword, all_jobs)

                # 翻页：滚动 → 监听 → 捕获
                if not all_jobs and not is_boss_security_page(tab, strict=True):
                    for p in range(2, 11):
                        if is_boss_security_page(tab, strict=True):
                            break
                        searcher.start_capture(tab)
                        searcher._scroll_to_bottom(tab)
                        time.sleep(2)
                        more = searcher.wait_capture(tab, keyword)
                        if not more:
                            break
                        added = 0
                        for j in more:
                            if j.url and j.url not in seen:
                                seen.add(j.url)
                                all_jobs.append(j)
                                added += 1
                        self.logger.info(f"  📡 p{p}: +{added} (累计{len(all_jobs)})")
                        self._save_capture_log(keyword, all_jobs)
                        if not added or len(more) < 15:
                            break
                        time.sleep(random.uniform(0.5, 1.0))

                # 最后兜底 → DOM
                if not all_jobs:
                    if is_boss_security_page(tab, strict=True) and not has_job_cards(tab):
                        self.logger.warning("  ⚠️ 安全验证中，无法 DOM 解析")
                    else:
                        if not has_job_cards(tab):
                            wait_for_job_cards(tab, timeout=8)
                        self.logger.info("  API/listen 无结果，尝试 DOM 解析...")
                        dom_jobs = searcher.collect_visible_jobs(tab, page_limit=5)
                        for j in dom_jobs:
                            if j.url and j.url not in seen:
                                if not title_matches_keyword(j.title, keyword):
                                    continue
                                seen.add(j.url)
                                all_jobs.append(j)

                self.logger.info(f"  共 {len(all_jobs)} 个岗位")
                # 输出前 20 个岗位名称，确认搜索相关性
                if all_jobs:
                    self.logger.info(f"  📋 搜索「{keyword}」TOP20:")
                    for idx, j in enumerate(all_jobs[:20], 1):
                        self.logger.info(
                            f"  [{idx:2d}] {j.title[:35]:35s} | {j.salary:12s} | {j.company[:20]}")
                    if len(all_jobs) > 20:
                        self.logger.info(f"  ... 还有 {len(all_jobs) - 20} 个")

                self.logger.info(f"  开始投递...")

                # 逐个投递
                for i, job in enumerate(all_jobs, 1):
                    _sanitize_job(job)
                    title_lower = job.title.lower()

                    # 跳过历史已投递
                    if job.url in old_success:
                        stats["dup"] += 1
                        self.logger.info(f"  ⏭️ 已投过: {job.title[:30]}")
                        continue

                    # 标题过滤（排除关键词）
                    if any(k in title_lower for k in exclude_titles):
                        stats["excl"] += 1
                        continue

                    # 城市过滤（配置杭州时跳过明确标青岛等其它城市的岗位）
                    if self._job_wrong_city(job, searcher.city):
                        stats["excl"] += 1
                        loc = (getattr(job, "location", "") or "")[:20]
                        self.logger.info(f"  🌏 城市不符({loc or '非'+searcher.city}): {job.title[:30]}")
                        continue

                    # 学历过滤
                    if max_edu:
                        edu = (job.education or "").strip()
                        if not edu:
                            edu = job.title or ""
                        if self._education_too_high(edu, max_edu):
                            stats["excl"] += 1
                            self.logger.info(f"  🎓 学历不符({edu[:20]}): {job.title[:30]}")
                            continue

                    # 经验过滤
                    if self._experience_too_high(job, max_exp):
                        stats["excl"] += 1
                        self.logger.info(f"  ⏳ 经验不符({job.experience[:15]}): {job.title[:30]}")
                        continue

                    # 技能过滤：Java 相关一律跳过
                    if self._is_java_job(job):
                        stats["excl"] += 1
                        self.logger.info(f"  ☕ Java跳过: {job.title[:30]}")
                        continue

                    # 方案A：列表 API 已明确 BOSS 离线
                    if require_boss_online and job.boss_online is False:
                        stats["boss_offline"] += 1
                        self.logger.info(f"  👤 BOSS离线: {job.title[:30]}")
                        continue

                    need_desc = ai_fit_check and (not job.description or len(job.description) < 30)
                    need_boss = require_boss_online or max_boss_rank is not None
                    if need_desc or (need_boss and self._needs_boss_detail(job, require_boss_online)):
                        searcher.fetch_job_detail(
                            tab, job,
                            need_description=need_desc,
                            need_boss_status=need_boss,
                        )

                    if require_boss_online and job.boss_online is not True:
                        stats["boss_offline"] += 1
                        label = job.boss_active_time or "离线/未知"
                        self.logger.info(f"  👤 BOSS未在线({label}): {job.title[:30]}")
                        continue

                    if max_boss_rank is not None and not self._boss_active_ok(job, max_boss_rank):
                        # 详情被风控打不开时活跃度未知：放行，避免全部误杀
                        if not job.boss_active_time and job.boss_online is not True:
                            self.logger.info(
                                f"  👤 BOSS活跃度未知(详情未打开)，放行: {job.title[:30]}"
                            )
                        else:
                            stats["boss_inactive"] += 1
                            label = job.boss_active_time or "未知"
                            self.logger.info(f"  👤 BOSS不活跃({label}): {job.title[:30]}")
                            continue

                    # 公司风险（缓存同一公司避免重复调 API）
                    risk = None
                    if job.company != "未知公司":
                        risk = risk_cache.get(job.company) or risk_checker.check(job.company, job.to_dict())
                        risk_cache[job.company] = risk
                    if risk and risk.level in block_levels:
                        recorder.record(job, None, risk, SubmitResult.RISK_REJECTED)
                        stats["risk"] += 1
                        self.logger.info(
                            f"  ⛔ 风险跳过 [{risk.level.value}/{risk.score}]: "
                            f"{job.company[:20]} | {'; '.join(risk.reasons[:1])}"
                        )
                        continue

                    # KPI
                    if skip_kpi:
                        kpi = risk_checker.check_kpi(job.to_dict(), job.company)
                        if kpi.score >= kpi_threshold:
                            recorder.record(job, None, None, SubmitResult.KPI_REJECTED)
                            stats["kpi"] += 1
                            continue

                    # 岗位匹配：AI 模式实时分析简历 → 规则模式关键词打分
                    score = 0
                    match = None
                    if ai_fit_check and self.resumes:
                        ai = risk_checker.match_job(self._all_resume_text, job.to_dict())
                        if ai is not None:
                            score = ai["score"]
                            reasons_text = " ".join(ai.get("reasons", []))
                            no_context = (
                                score == 0
                                and (
                                    "描述为空" in reasons_text
                                    or "无具体描述" in reasons_text
                                    or "无任何岗位描述" in reasons_text
                                )
                            )
                            if no_context and not (job.description or "").strip():
                                match = matcher.match(job)
                                score = match.total_score
                                self.logger.info(
                                    f"  📋 无JD，规则匹配 score={score:.0f}: {job.title[:30]}"
                                )
                                if not search_only and score < min_score:
                                    recorder.record(job, match, None, SubmitResult.MATCH_LOW)
                                    stats["match"] += 1
                                    continue
                            elif not ai["fit"] or score < ai_fit_min:
                                stats["fit"] += 1
                                self.logger.info(
                                    f"  🎯 AI不匹配(score={score}): "
                                    f"{'; '.join(ai.get('reasons', [])[:2])[:60]}"
                                )
                                continue
                        else:
                            # AI 失败回退到规则匹配
                            match = matcher.match(job)
                            score = match.total_score
                            if not search_only and score < min_score:
                                recorder.record(job, match, None, SubmitResult.MATCH_LOW)
                                stats["match"] += 1
                                continue
                    else:
                        # 规则匹配
                        match = matcher.match(job)
                        score = match.total_score
                        if not search_only and score < min_score:
                            recorder.record(job, match, None, SubmitResult.MATCH_LOW)
                            stats["match"] += 1
                            continue

                    if search_only:
                        risk_label = risk.level.value if risk else "skip"
                        self.logger.info(
                            f"  [{i}] {job.title[:30]} | "
                            f"{job.salary} | 匹配{score:.0f} | 风险{risk_label}"
                        )
                        continue

                    # 投递
                    self.logger.info(
                        f"  🚀 [{i}/{len(all_jobs)}] {job.title[:35]} 匹配{score:.0f}"
                    )
                    result = submitter.submit(job, score)
                    recorder.record(job, match, None, result)
                    recorder.save_json()

                    if result == SubmitResult.SUCCESS:
                        old_success.add(job.url)
                        stats["success"] += 1
                        d = random.uniform(*[cfg["submit"]["interval"][k] for k in ("min", "max")])
                        self.logger.info(f"  ✅ 等待 {d:.0f}s")
                        time.sleep(d)
                    elif result == SubmitResult.DAILY_LIMIT:
                        self.logger.warning("⚠️ 已达每日投递上限，退出")
                        recorder.save_json()
                        self._print_stats(stats)
                        return
                    elif result == SubmitResult.ALREADY_APPLIED:
                        old_success.add(job.url)
                        stats["already"] += 1
                    else:
                        stats["fail"] += 1

            # 汇总
            self._print_stats(stats)
            recorder.save_json()

        except KeyboardInterrupt:
            self.logger.warning("用户中断")
        finally:
            if self.browser:
                try:
                    if getattr(self, "_searcher", None):
                        self._searcher.close_detail_tab()
                except Exception:
                    pass
                try: self.browser.quit()
                except: pass

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
                  "本科":4,"学士":4,"硕士":5,"研究生":5,"研":5,"硕":5,
                  "博士":6,"博":6,"博士后":6}

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

    def _build_search_url(self, keyword: str, searcher) -> str:
        from urllib.parse import quote as _quote
        url = (
            f"https://www.zhipin.com/web/geek/jobs?"
            f"query={_quote(keyword)}&city={searcher.city_code}"
        )
        if searcher.exp_code:
            url += f"&experience={searcher.exp_code}"
        return url

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
            tab.get(f"https://www.zhipin.com/web/geek/jobs?city={code}")
            tab.wait(2)
            self.logger.info(f"  🌏 已切换到配置城市：{city} ({code})")
        except Exception as e:
            self.logger.warning(f"  ⚠️ 切换城市失败: {e}")

    def _job_wrong_city(self, job, city: str) -> bool:
        """岗位城市明显不是配置城市时跳过（防 DOM/混排串城）"""
        if not city:
            return False
        text = f"{getattr(job, 'location', '') or ''} {job.title or ''} {job.company or ''}"
        if city in text:
            return False
        # 仅当出现其他已知城市名、且不含配置城市时判定串城
        for other in JobSearcher.CITY_CODES:
            if other == city:
                continue
            if other in text:
                return True
        return False

    def _trigger_search(self, tab, keyword: str, searcher) -> bool:
        """始终用配置城市的搜索 URL，禁止无城市参数的搜索框回落。"""
        url = self._build_search_url(keyword, searcher)
        self.logger.info(
            f"  🌏 {searcher.city}({searcher.city_code}) | 关键词「{keyword}」"
        )

        if is_boss_security_page(tab, strict=True) and not has_job_cards(tab):
            if not ensure_boss_api_ready(tab, self.logger, timeout=120):
                return False
            tab = self.work_tab or self.browser.latest_tab

        # 无论 probe 是否瞬时失败，都强制带 city= 的 URL（避免掉回青岛）
        try:
            tab.get(url)
            tab.wait(4)
        except Exception as e:
            self.logger.warning(f"  ⚠️ 搜索导航失败: {e}")
            return False

        cur = (tab.url or "")
        if searcher.city_code not in cur:
            self.logger.warning(
                f"  ⚠️ 页面城市不是配置的 {searcher.city}，强制重开搜索 URL"
            )
            try:
                self._force_boss_city(tab, searcher)
                tab.get(url)
                tab.wait(4)
            except Exception as e:
                self.logger.warning(f"  ⚠️ 重开搜索失败: {e}")

        if is_boss_security_page(tab, strict=True):
            self.logger.warning("  ⚠️ 搜索触发验证，等待解锁...")
            if not ensure_boss_api_ready(tab, self.logger, timeout=120):
                return False
            try:
                tab.get(url)
                tab.wait(4)
            except Exception:
                pass

        wait_for_job_cards(tab, timeout=10)
        if has_job_cards(tab):
            self._scroll(tab)

        final = (tab.url or "")[:140]
        if searcher.city_code not in final:
            self.logger.warning(f"  ⚠️ 最终 URL 城市仍异常 | {final}")
        else:
            self.logger.info(f"  ✅ 搜索「{keyword}」导航完成 | {final}")
        return True

    def _ui_search(self, tab, keyword: str) -> bool:
        """在首页/职位页用搜索框输入关键词（兼容 React 受控输入）"""
        import json as _json
        if not tab or is_boss_security_page(tab, strict=True):
            return False
        try:
            ok = tab.run_js(f"""
                (() => {{
                    const kw = {_json.dumps(keyword)};
                    const inputs = Array.from(document.querySelectorAll(
                        'input[placeholder*="职位"], input[placeholder*="搜索"],'
                        + 'input[placeholder*="公司"], input[type="search"],'
                        + '.search-input input, input.ipt, input[class*="search"]'
                    ));
                    let input = inputs.find(i => i.offsetParent !== null) || inputs[0];
                    if (!input) return false;
                    input.focus();
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    setter.call(input, kw);
                    input.dispatchEvent(new Event('input', {{bubbles:true}}));
                    input.dispatchEvent(new Event('change', {{bubbles:true}}));
                    const btn = document.querySelector(
                        'button.btn-search, .btn-search, button[ka*="search"],'
                        + '.search-btn, button[type="submit"], .search-form button'
                    );
                    if (btn) {{ btn.click(); return true; }}
                    input.dispatchEvent(new KeyboardEvent('keydown', {{
                        key:'Enter', code:'Enter', keyCode:13, which:13, bubbles:true
                    }}));
                    input.dispatchEvent(new KeyboardEvent('keyup', {{
                        key:'Enter', code:'Enter', keyCode:13, which:13, bubbles:true
                    }}));
                    return true;
                }})()
            """)
            if ok:
                tab.wait(5)
                return True
        except Exception as e:
            self.logger.debug(f"  UI搜索失败: {e}")
        return False

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
            self.logger.warning(f"CLI 指定的简历不存在: {cli_resume}")

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
    def _experience_too_high(cls, job, max_exp: str = "") -> bool:
        """判断岗位经验要求是否超过 max_exp 限制（留空=不过滤）"""
        if not max_exp:
            return False

        max_rank = cls._EXP_RANKS.get(max_exp, 3)

        # 1) 从API经验字段判断
        exp = (job.experience or "").strip()
        for k, v in cls._EXP_RANKS.items():
            if k in exp:
                if v > max_rank:
                    return True
                return False  # 明确匹配在范围内

        # 2) 从标题/描述提取年限数字
        text = ((job.title or "") + " " + (job.description or "")[:500])
        for yrs in range(max_rank + 1, 7):
            pat = re.compile(rf'{yrs}\s*年[以之]?[上内]|至少\s*{yrs}\s*年|{yrs}\s*年以上|{yrs}\s*年\s*以上')
            if pat.search(text):
                return True

        # 3) 高级职位关键词
        if max_rank <= 2:  # 1-3年及以下
            high_kw = ["senior", "资深", "高级", "主管", "经理", "组长", "负责人", "总监"]
            title_low = (job.title or "").lower()
            for kw in high_kw:
                if kw.lower() in title_low:
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
    args = p.parse_args()

    for name in ["app","resume","login","jobs","risk","matcher","submit","recorder"]:
        setup_logger(name, "INFO")

    app = AutoBossApp(args.config)
    app.run(search_only=args.search_only, resume_only=args.resume_only,
            resume_path=args.resume)


if __name__ == "__main__":
    main()
