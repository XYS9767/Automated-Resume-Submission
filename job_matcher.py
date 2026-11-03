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
from utils import get_logger, extract_salary_range
from relevance import contains_term, canonical_skill, is_skill_label, extract_ecommerce_skills

logger = get_logger("matcher")

DOMESTIC_PLATFORMS = ('京东', '淘宝', '天猫', '苏宁', '拼多多', '抖店', '抖音',
                      '快手', '小红书', '美团', '饿了么', '唯品会')
CROSS_BORDER_TERMS = ('跨境', '海外', '外贸', '亚马逊', 'Amazon', 'Shopee',
                      'Lazada', 'eBay', 'Shopify', 'AliExpress', '速卖通',
                      'Temu', 'Shein', 'TikTok', '独立站', 'Wish')


def graduation_cohorts(text):
    """Read explicit graduate cohorts, not arbitrary years in a job description."""
    years = set()
    for match in re.finditer(r'(?<!\d)((?:(?:20)?\d{2}[、/／至到~—–\-\s]*)+)届', text):
        values = [int(v) + (2000 if len(v) == 2 else 0)
                  for v in re.findall(r'(?:20)?\d{2}', match.group(1))]
        if len(values) == 2 and re.search(r'[至到~—–\-]', match.group(1)):
            years.update(range(min(values), max(values) + 1))
        else:
            years.update(values)
    years.update(int(y) for y in re.findall(r'(20\d{2})年(?:的)?(?:应届)?毕业生?', text))
    return years


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

    def __init__(self, resume: ResumeData, weights: Optional[dict] = None, expected_salary: str = ''):
        self.resume = resume
        self.weights = weights or self.WEIGHTS.copy()
        self.expected_salary = expected_salary

        # 预计算：避免每次 match() 重复计算
        self._resume_skills_lower = set(canonical_skill(s) for s in resume.skills)
        self._resume_words = set(
            re.findall(r"[a-zA-Z\u4e00-\u9fff]+", resume.raw_text.lower())
        )
        self._tech_kw_lower = {s.lower() for s in JobSearcher.TECH_KEYWORDS}
        self._my_exp_years = (resume.declared_experience_years if resume.declared_experience_years is not None
                              else self._calculate_total_experience_years())
        self._my_edu_level = self._get_highest_education_level()

        self._stopwords = {
            "的", "了", "在", "是", "我", "有", "和", "就",
            "不", "人", "都", "一", "一个", "上", "也",
            "很", "到", "说", "要", "去", "你", "会",
            "着", "没有", "看", "好", "自己", "这",
        }

        logger.info(f"匹配器初始化: 简历技能 {len(resume.skills)} 项, "
                     f"工作经历 {len(resume.work_experiences)} 段")

    def missing_title_skills(self, job):
        """An explicit primary language/platform in the title is a real requirement."""
        primary = ('Java', 'Python', 'C', 'C++', 'C#', 'Go', 'PHP', 'Rust',
                   'Swift', 'Kotlin', 'Dart', 'Flutter', 'Scala')
        required = [s for s in primary if contains_term(job.title, s)]
        present = [s for s in required if canonical_skill(s) in self._resume_skills_lower
                   or contains_term(self.resume.raw_text, s)]
        # Titles such as Java/Python explicitly offer alternatives.
        if present and re.search(r'/|或|任选', job.title):
            return []
        return [s for s in required if s not in present]

    def eligibility_rejection_reason(self, job, domestic_ecommerce_only=False):
        """Hard eligibility checks run before scoring or AI can approve a job."""
        text = job.title + '\n' + (job.description or '')
        # A title's explicit cohort is the advertised role restriction. Do not
        # broaden it with unrelated years mentioned elsewhere in the JD.
        cohorts = graduation_cohorts(job.title) or graduation_cohorts(job.description or '')
        if cohorts:
            graduation_years = [self._parse_year(edu.end_date) for edu in self.resume.education]
            graduation_years = [year for year in graduation_years if year]
            if not graduation_years:
                return '岗位限定毕业届别，但简历毕业年份无法确认'
            year = max(graduation_years)
            if year not in cohorts:
                return f'毕业届别不符: 简历{year}届，岗位限定' + '/'.join(map(str, sorted(cohorts))) + '届'
        if not domestic_ecommerce_only:
            return ''
        # Read title/JD, not company background or recommendation skill labels.
        # Negated/optional cross-border experience is not a required direction.
        required_description = re.sub(
            r'(?:无需|不要求|不需要|不限|不涉及|非)[^，,。；;\n]{0,8}(?:跨境|海外|外贸)(?:电商)?(?:运营)?(?:经验)?'
            r'|(?:跨境|海外|外贸)(?:电商)?(?:运营)?经验(?:者)?(?:优先|加分|不要求|不限)',
            '', job.description or '')
        direction = job.title + '\n' + required_description
        foreign = [term for term in CROSS_BORDER_TERMS if contains_term(direction, term)]
        if foreign:
            return '仅匹配国内电商，岗位涉及跨境/海外方向: ' + '、'.join(foreign)
        platforms = {canonical_skill(p) for p in DOMESTIC_PLATFORMS if contains_term(text, p)}
        # Also recognize aliases such as 淘系 and 京东自营.
        platforms.update(extract_ecommerce_skills(text) & set(DOMESTIC_PLATFORMS))
        familiar = extract_ecommerce_skills(self.resume.raw_text) & set(DOMESTIC_PLATFORMS)
        familiar.update(canonical_skill(p) for p in DOMESTIC_PLATFORMS
                        if contains_term(self.resume.raw_text, p))
        if not platforms:
            return '岗位未明确国内运营平台，无法确认与简历经验相符'
        if not platforms & familiar:
            return '岗位平台与简历经验不符: ' + '、'.join(sorted(platforms))
        return ''

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

        # 6. 标题与当前简历技能一致时加分
        title_bonus = self._calc_title_bonus(job.title)
        if title_bonus > 0:
            result.match_details.append(f"标题匹配: +{title_bonus:.0f}分")

        # 综合总分 = 各维度加权求和
        result.total_score = min(100.0, (
            result.skill_score
            + result.experience_score
            + result.education_score
            + result.salary_score
            + result.keyword_score
            + title_bonus
        ))

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
        job_skills = {canonical_skill(s) for s in job.skills_required if is_skill_label(s)}
        # Detail requirements must augment sparse card labels, even if labels exist.
        job_skills.update(canonical_skill(s) for s in self._tech_kw_lower
                          if contains_term(job.title + ' ' + job.description, s))
        job_skills.update(extract_ecommerce_skills(job.title + ' ' + job.description))

        if not job_skills:
            result.skill_score = self.weights["skill"] * 100 * 0.6
            result.match_details.append("岗位无明确技能要求，取默认分")
            return

        resume_skills = resume_skills | extract_ecommerce_skills(self.resume.raw_text) | {s for s in job_skills if contains_term(self.resume.raw_text, s)}
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
        if not job.education or '不限' in job.education:
            result.education_score = self.weights['education'] * 100
            return
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
        expected_min, expected_max = extract_salary_range(self.expected_salary)
        if expected_max:
            job_min, job_max = extract_salary_range(job.salary)
            if not job_min:
                job_min, job_max = job.salary_min, job.salary_max
            result.salary_score = self.weights['salary'] * 100 * (
                1.0 if expected_min <= job_min <= expected_max else 0.0)
            result.match_details.append('薪资按配置期望评估: ' + self.expected_salary)
            return
        if job.salary_min <= 0 and job.salary_max <= 0:
            result.salary_score = self.weights["salary"] * 100 * 0.5
            return

        if '/天' in job.salary or '元/日' in job.salary:
            result.salary_score = self.weights['salary'] * 100 * .7
            result.match_details.append('日薪岗位单独计分，不与月薪直接比较')
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
        ecommerce = extract_ecommerce_skills(job.title + ' ' + job.description)
        if '电商运营' in extract_ecommerce_skills(self.resume.raw_text) and ecommerce:
            covered = ecommerce & extract_ecommerce_skills(self.resume.raw_text)
            result.keyword_score = self.weights['keyword'] * 100 * len(covered) / len(ecommerce)
            result.match_details.append(f'电商能力覆盖: {len(covered)}/{len(ecommerce)}')
            return
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
        intervals = []
        now = datetime.now()
        current_month = now.year * 12 + now.month - 1
        def month_index(value, end=False):
            numbers = re.findall(r'\d+', str(value))
            if not numbers:
                return current_month if re.search(r'至今|现在|Present|Now', str(value), re.I) else None
            month = int(numbers[1]) if len(numbers) > 1 else (12 if end else 1)
            return int(numbers[0]) * 12 + month - 1
        for exp in self.resume.work_experiences:
            start = month_index(exp.start_date)
            end = month_index(exp.end_date, end=True)
            if start is not None and end is not None and start <= end:
                intervals.append((start, min(end, current_month)))
        merged = []
        for start, end in sorted(intervals):
            if end < start:
                continue
            if merged and start <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return round(sum(end - start + 1 for start, end in merged) / 12, 2)

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

    def _calc_title_bonus(self, title):
        # No universal embedded/hardware bonus: use this candidate's actual skills.
        matched = [s for s in self._resume_skills_lower if contains_term(title, s)]
        return min(10.0, len(matched) * 5.0)
