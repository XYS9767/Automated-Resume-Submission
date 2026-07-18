"""
岗位匹配度分析模块

将 简历数据 ↔ 岗位数据 进行多维度匹配
输出 0-100 的综合匹配分数，以及各维度得分明细

匹配维度:
  1. 技能匹配 (权重 40%) — 简历技能 vs 岗位技能
  2. 经验匹配 (权重 20%) — 工作年限
  3. 学历匹配 (权重 15%) — 学历要求
  4. 薪资匹配 (权重 10%) — 匹配度
  5. 关键词匹配 (权重 15%) — JD 中的热词与简历的契合度
"""

from __future__ import annotations

import re
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

from resume_parser import ResumeData
from job_search import JobPosting, JobSearcher
from utils import get_logger

logger = get_logger("matcher")


@dataclass
class MatchResult:
    """匹配结果"""
    total_score: float = 0.0          # 总分 0-100
    skill_score: float = 0.0          # 技能匹配分 (满分40)
    experience_score: float = 0.0     # 经验匹配分 (满分20)
    education_score: float = 0.0      # 学历匹配分 (满分15)
    salary_score: float = 0.0         # 薪资匹配分 (满分10)
    keyword_score: float = 0.0        # 关键词匹配分 (满分15)
    match_details: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_score": round(self.total_score, 1),
            "skill_score": round(self.skill_score, 1),
            "experience_score": round(self.experience_score, 1),
            "education_score": round(self.education_score, 1),
            "salary_score": round(self.salary_score, 1),
            "keyword_score": round(self.keyword_score, 1),
            "details": self.match_details,
            "missing_skills": self.missing_skills,
        }


class JobMatcher:
    """
    岗位匹配器
    支持自定义权重或使用默认权重
    """

    # 默认权重
    WEIGHTS = {
        "skill": 0.40,
        "experience": 0.20,
        "education": 0.15,
        "salary": 0.10,
        "keyword": 0.15,
    }

    # 学历层级映射
    EDUCATION_LEVELS = {
        "博士": 5,
        "硕士": 4,
        "本科": 3,
        "大专": 2,
        "中专": 1,
        "高中": 1,
        "学历不限": 3,  # 不限默认本科水平
        "不限": 3,
    }

    def __init__(self, resume: ResumeData, weights: Optional[dict] = None):
        self.resume = resume
        self.weights = weights or self.WEIGHTS.copy()

        # 预计算：避免每次 match() 重复计算
        self._resume_skills_lower = set(s.lower() for s in resume.skills)
        self._resume_words = set(
            re.findall(r"[a-zA-Z\u4e00-\u9fff]+", resume.raw_text.lower())
        )
        self._tech_kw_lower = {s.lower() for s in JobSearcher.TECH_KEYWORDS}
        self._my_exp_years = self._calculate_total_experience_years()
        self._my_edu_level = self._get_highest_education_level()

        self._stopwords = {
            "的", "了", "在", "是", "我", "有", "和", "就",
            "不", "人", "都", "一", "一个", "上", "也",
            "很", "到", "说", "要", "去", "你", "会",
            "着", "没有", "看", "好", "自己", "这",
        }

        logger.info(f"匹配器初始化: 简历技能 {len(resume.skills)} 项, "
                     f"工作经历 {len(resume.work_experiences)} 段")

    def match(self, job: JobPosting) -> MatchResult:
        """
        对单个岗位计算综合匹配分
        """
        result = MatchResult()

        # 1. 技能匹配
        self._calc_skill_match(job, result)

        # 2. 经验匹配
        self._calc_experience_match(job, result)

        # 3. 学历匹配
        self._calc_education_match(job, result)

        # 4. 薪资匹配
        self._calc_salary_match(job, result)

        # 5. 关键词匹配
        self._calc_keyword_match(job, result)

        # 6. 标题加分：标题含嵌入式关键词 → +15 分（解决应届生卡片信息不足的问题）
        title_bonus = self._calc_title_bonus(job.title)
        if title_bonus > 0:
            result.match_details.append(f"标题匹配: +{title_bonus:.0f}分")

        # 综合总分 = 各维度加权求和
        result.total_score = (
            result.skill_score
            + result.experience_score
            + result.education_score
            + result.salary_score
            + result.keyword_score
            + title_bonus
        )

        logger.debug(
            f"匹配 [{job.title} @ {job.company}] "
            f"总分={result.total_score:.1f} | "
            f"技能={result.skill_score:.1f}/40 | "
            f"经验={result.experience_score:.1f}/20 | "
            f"学历={result.education_score:.1f}/15 | "
            f"薪资={result.salary_score:.1f}/10 | "
            f"关键词={result.keyword_score:.1f}/15"
        )
        return result

    # ---------- 各维度计算方法 ----------

    def _calc_skill_match(self, job: JobPosting, result: MatchResult):
        resume_skills = self._resume_skills_lower
        job_skills = set(s.lower() for s in job.skills_required)

        if not job_skills:
            if job.description:
                desc_lower = job.description.lower()
                job_skills = {s for s in self._tech_kw_lower if s in desc_lower}

        if not job_skills:
            result.skill_score = self.weights["skill"] * 100 * 0.6
            result.match_details.append("岗位无明确技能要求，取默认分")
            return

        matched = resume_skills & job_skills
        missing = job_skills - resume_skills
        ratio = len(matched) / len(job_skills) if job_skills else 0.6

        result.skill_score = self.weights["skill"] * 100 * min(ratio, 1.0)
        result.missing_skills = sorted(missing)

        if matched:
            result.match_details.append(
                f"技能匹配: {len(matched)}/{len(job_skills)} "
                f"({', '.join(sorted(matched))})"
            )
        if missing:
            result.match_details.append(
                f"缺失技能: {', '.join(sorted(missing)[:5])}"
            )

    def _calc_experience_match(self, job: JobPosting, result: MatchResult):
        req_exp = self._parse_experience_years(job.experience)
        my_exp = self._my_exp_years

        if req_exp == 0:
            result.experience_score = self.weights["experience"] * 100
            result.match_details.append("经验要求: 不限/应届")
            return

        if my_exp >= req_exp:
            result.experience_score = self.weights["experience"] * 100
            result.match_details.append(f"经验满足: {my_exp}年 ≥ 要求{req_exp}年")
        elif my_exp <= 1 and req_exp <= 3:
            # 应届生投递1-3年岗位，给 70%
            result.experience_score = self.weights["experience"] * 100 * 0.7
            result.match_details.append(
                f"应届生投递，经验要求{req_exp}年（宽容评估）"
            )
        elif my_exp >= req_exp * 0.5:
            ratio = my_exp / req_exp
            result.experience_score = self.weights["experience"] * 100 * ratio
            result.match_details.append(
                f"经验部分匹配: 我有{my_exp}年, 要求{req_exp}年"
            )
        else:
            result.experience_score = self.weights["experience"] * 100 * 0.3
            result.match_details.append(
                f"经验差距较大: 我有{my_exp}年, 要求{req_exp}年"
            )

    def _calc_education_match(self, job: JobPosting, result: MatchResult):
        req_level = self.EDUCATION_LEVELS.get(job.education, 3)
        my_level = self._my_edu_level

        if req_level <= my_level:
            result.education_score = self.weights["education"] * 100
            result.match_details.append("学历满足要求")
        elif req_level - my_level == 1:
            result.education_score = self.weights["education"] * 100 * 0.6
            result.match_details.append(
                f"学历略低: 要求{job.education}"
            )
        else:
            result.education_score = self.weights["education"] * 100 * 0.2
            result.match_details.append(
                f"学历差距较大: 要求{job.education}"
            )

    def _calc_salary_match(self, job: JobPosting, result: MatchResult):
        if job.salary_min <= 0 and job.salary_max <= 0:
            result.salary_score = self.weights["salary"] * 100 * 0.5
            return

        my_exp = self._my_exp_years
        if my_exp <= 1:
            expected_min, expected_max = 8000, 15000
        elif my_exp <= 3:
            expected_min, expected_max = 15000, 25000
        elif my_exp <= 5:
            expected_min, expected_max = 20000, 35000
        elif my_exp <= 8:
            expected_min, expected_max = 30000, 50000
        else:
            expected_min, expected_max = 40000, 70000

        # 计算交集
        overlap_min = max(job.salary_min, expected_min)
        overlap_max = min(job.salary_max, expected_max)

        if overlap_min <= overlap_max:
            # 有交集：按岗位薪资范围覆盖度算
            job_range = job.salary_max - job.salary_min
            if job_range <= 0:
                ratio = 0.5
            else:
                overlap = overlap_max - overlap_min
                ratio = overlap / job_range
            result.salary_score = self.weights["salary"] * 100 * min(ratio + 0.3, 1.0)
        else:
            # 无交集，薪资期望不匹配
            result.salary_score = self.weights["salary"] * 100 * 0.1
            result.match_details.append(
                f"薪资不匹配: 范围{job.salary_min/1000:.0f}K-"
                f"{job.salary_max/1000:.0f}K, "
                f"期望{expected_min/1000:.0f}K-{expected_max/1000:.0f}K"
            )

    def _calc_keyword_match(self, job: JobPosting, result: MatchResult):
        if not job.description:
            result.keyword_score = self.weights["keyword"] * 100 * 0.5
            return

        desc_lower = job.description.lower()
        words = set(re.findall(r"[a-zA-Z\u4e00-\u9fff]+", desc_lower))
        keywords = words - self._stopwords

        if not keywords:
            result.keyword_score = self.weights["keyword"] * 100 * 0.5
            return

        # 用集合交集替代逐个查找
        matched = len(keywords & self._resume_words)
        ratio = matched / len(keywords)
        result.keyword_score = self.weights["keyword"] * 100 * min(ratio * 1.5, 1.0)

        if matched > 10:
            result.match_details.append(f"JD 关键词覆盖: {matched}/{len(keywords)}")

    # ---------- 辅助方法 ----------

    def _parse_experience_years(self, exp_text: str) -> int:
        """解析经验要求文本为年数"""
        if not exp_text:
            return 0
        exp_text = exp_text.strip()
        if "经验不限" in exp_text or "不限" in exp_text or "应届" in exp_text:
            return 0
        nums = re.findall(r"(\d+)", exp_text)
        if nums:
            return int(nums[0])
        return 0

    def _calculate_total_experience_years(self) -> int:
        """从简历工作经历计算总年数"""
        total_years = 0
        current_year = datetime.now().year
        for exp in self.resume.work_experiences:
            start = self._parse_year(exp.start_date)
            end = self._parse_year(exp.end_date)
            if start and end:
                total_years += max(0, end - start)
            elif start:
                # 至今的情况
                total_years += max(0, current_year - start)

        if total_years == 0:
            total_years = 0

        return total_years

    def _parse_year(self, date_str: str) -> Optional[int]:
        """从日期字符串解析年份"""
        if not date_str:
            return None
        m = re.search(r"(\d{4})", str(date_str))
        if m:
            return int(m.group(1))
        return None

    def _get_highest_education_level(self) -> int:
        """获取简历中最高的学历等级"""
        level = 2  # 默认大专
        for edu in self.resume.education:
            combined = (edu.title_or_major + " " + edu.description +
                        " " + edu.company_or_school)
            for keyword, lvl in self.EDUCATION_LEVELS.items():
                if keyword in combined:
                    if lvl > level:
                        level = lvl
        return level

    def _calc_title_bonus(self, title: str) -> float:
        """标题包含嵌入式关键词 → 加分（最高15分）"""
        if not title:
            return 0

        title_lower = title.lower()
        # 强相关关键词（+5分/个，上限15）
        strong = ["嵌入式", "单片机", "stm32", "arm", "rtos", "linux驱动",
                   "freertos", "fpga", "dsp", "物联网", "iot", "bms", "can",
                   "汽车电子", "工业控制", "电机控制", "固件"]
        # 弱相关关键词（+2分/个，上限6）
        weak = ["c/c++", "c++", "驱动", "底层", "硬件", "电子工程师",
                "系统软件", "通信", "自动化", "机器人"]

        bonus = 0.0
        for kw in strong:
            if kw in title_lower:
                bonus += 5.0
                if bonus >= 15.0:
                    return 15.0
        for kw in weak:
            if kw in title_lower:
                bonus += 2.0
                if bonus >= 15.0:
                    return 15.0
        return min(bonus, 15.0)
