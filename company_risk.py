"""
公司风险检测模块 — 含 KPI/诈骗公司识别

支持两种模式:
  - rule: 基于规则的关键词/规模/行业检测（免费离线）
  - api:  调用企查查/天眼查 API 获取企业风险数据（需配置 token）

返回 RiskLevel 枚举: SAFE / LOW / MEDIUM / HIGH
"""

from __future__ import annotations

import re
import json
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional

import requests
from utils import get_logger

logger = get_logger("risk")


class RiskLevel(Enum):
    SAFE = "safe"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class RiskResult:
    level: RiskLevel = RiskLevel.SAFE
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    @property
    def is_safe(self) -> bool:
        return self.level in (RiskLevel.SAFE, RiskLevel.LOW)

    def to_dict(self) -> dict:
        return {
            "level": self.level.value,
            "score": self.score,
            "reasons": self.reasons,
            "safe": self.is_safe,
        }


# ==================== 风险关键词库 ====================

# 人力资源/外包/劳务公司 — 几乎100%是中介（勿用「网络科技」等常见公司后缀）
HR_OUTSOURCE_INDICATORS = [
    "人力资源", "劳务派遣", "外包服务", "人才服务",
    "劳务公司", "猎头",
]

# 公司名模式：XX科技/XX网络 — 需要结合其他信号判断
GENERIC_COMPANY_SUFFIX = [
    "科技有限公司", "网络科技有限公司", "信息技术有限公司",
    "电子商务有限公司", "贸易有限公司",
]

# 公司名含以下词 → 高风险
COMPANY_NAME_HIGH_RISK = [
    "劳务", "外包", "派遣", "中介", "猎头",
    "培训学校", "培训机构", "教育咨询", "辅导",
    "理财", "投资管理", "资产管理", "财富",
    "保险代理", "保险经纪",
    "融资租赁", "小额贷款", "担保",
    "文化传媒", "影视传媒", "直播",
]

# 岗位描述中的 KPI/诈骗 信号词
KPI_PHRASES = [
    "有较强的抗压能力", "抗压能力强", "能承受较大工作压力",
    "弹性工作制", "弹性工作",
    "适应高强度", "服从加班", "适应加班",
    "无底薪", "有责底薪", "责任底薪",
    "自带客户", "自带资源",
    "试用期不交社保", "试用期无社保",
    "入职后培训", "先培训", "岗前培训",
    "提供住宿", "包住宿",  # 可能是工厂/外地招聘
    "996", "007", "大小周",
]

# 刷 KPI 的典型特征
KPI_TITLE_KEYWORDS = [
    "急聘", "急招", "高薪急聘", "大量招聘", "诚聘",
    "月入过万", "轻松", "简单", "小白",
]

# 过于笼统的描述特征（可能是刷KPI的）
GENERIC_DESC_PATTERNS = [
    "负责日常", "完成领导", "完成上级",
    "协助部门", "配合团队", "参与项目",
]

# 高风险行业
RISKY_INDUSTRIES = [
    "培训", "保险", "理财", "P2P", "信贷",
    "教育", "房地产中介", "证券",
]

# BOSS 匿名公司名（无法爱企查）
_ANON_COMPANY_RE = re.compile(r"^某.{0,30}(公司|企业|集团|机构|基金|互联网|银行|证券)")
_SALARY_LINE_RE = re.compile(r'\d+K', re.IGNORECASE)

# 爱企查无效结果特征
_AIQICHA_INVALID_MARKERS = (
    "请登录", "登录百度", "百度账号", "验证码", "安全验证",
    "未找到", "暂无相关", "没有找到", "搜索结果为空",
    "未搜索到", "无相关企业", "请尝试其他关键词",
)


class CompanyRiskChecker:

    def __init__(self, config: dict):
        risk_cfg = config.get("risk_check", {})
        self.mode = risk_cfg.get("mode", "rule")
        self.risk_keywords = risk_cfg.get("risk_keywords", [])
        self.api_cfg = risk_cfg.get("api", {})

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })

        # 跟踪：同公司有多少岗位
        self._company_job_count: dict[str, int] = {}
        self._browser = None  # DrissionPage browser，set_browser() 注入
        self._aiqicha_tab = None  # 爱企查专用 tab，复用避免频繁建/关
        self._aiqicha_errors = 0
        self._aiqicha_last_at = 0.0

        logger.info(f"风险检测模式: {self.mode}")

    def set_browser(self, browser):
        """注入浏览器实例，用于爱企查等平台的公司信息查询"""
        self._browser = browser

    def set_aiqicha_tab(self, tab):
        """复用已登录的爱企查标签页"""
        self._aiqicha_tab = tab

    @staticmethod
    def _normalize_company_name(name: str) -> str:
        if not name:
            return ""
        for line in name.replace("\r", "\n").split("\n"):
            line = line.strip()
            if not line or len(line) < 2:
                continue
            if _SALARY_LINE_RE.search(line):
                continue
            if "·" in line and not any(k in line for k in ("公司", "有限", "集团", "股份", "企业")):
                continue
            if line in ("本科", "大专", "硕士", "博士", "学历不限", "经验不限"):
                continue
            return line[:50]
        return name.split("\n")[0].strip()[:50]

    @classmethod
    def _is_anonymous_company(cls, name: str) -> bool:
        name = cls._normalize_company_name(name)
        if not name:
            return False
        if "某" in name:
            return True
        return bool(_ANON_COMPANY_RE.match(name))

    @classmethod
    def _is_valid_aiqicha_text(cls, text: str) -> bool:
        if not text or len(text) < 80:
            return False
        hits = sum(1 for m in _AIQICHA_INVALID_MARKERS if m in text)
        return hits == 0

    def check(self, company_name: str, job: Optional[dict] = None) -> RiskResult:
        name = self._normalize_company_name(company_name)
        if self._is_anonymous_company(name):
            logger.info(f"  🏢 匿名公司「{name}」，跳过爱企查")
            return self._check_via_rules(name, job)
        if self.mode == "api" and self.api_cfg.get("token"):
            return self._check_via_api(name, job)
        else:
            return self._check_via_rules(name, job)

    def check_kpi(self, job: dict, company_name: str = "") -> RiskResult:
        """专门检测岗位是否为 KPI 刷量岗位 (0=正常, 100=明显KPI)"""
        result = RiskResult(level=RiskLevel.SAFE)
        score = 0
        reasons = []

        title = job.get("title", "")
        desc = job.get("description", "")
        salary_min = job.get("salary_min", 0)
        salary_max = job.get("salary_max", 0)

        # KPI 1: 标题含 KPI 关键词
        for kw in KPI_TITLE_KEYWORDS:
            if kw in title:
                score += 15
                reasons.append(f"标题含 KPI 关键词: 「{kw}」")
                break

        # KPI 2: 薪资范围过大 (>5倍)
        if salary_min > 0 and salary_max > salary_min * 5:
            score += 20
            reasons.append(f"薪资范围过大 ({salary_min:.0f}-{salary_max:.0f})，疑似 KPI 岗")
        elif salary_min > 0 and salary_max > salary_min * 3:
            score += 8
            reasons.append(f"薪资范围偏大 ({salary_min:.0f}-{salary_max:.0f})")

        # KPI 3: 岗位描述含高压/坑人描述
        for phrase in KPI_PHRASES:
            if phrase in desc:
                score += 20
                reasons.append(f"描述含风险表述: 「{phrase}」")
                break

        # KPI 4: 描述过于笼统（可能是模板生成的）
        if desc:
            generic_count = sum(1 for p in GENERIC_DESC_PATTERNS if p in desc)
            if generic_count >= 3:
                score += 15
                reasons.append("岗位描述过于笼统，疑似复制模板")
            elif generic_count >= 2:
                score += 8
                reasons.append("岗位描述偏笼统")

            # KPI 5: 描述太短 (<80字)
            if len(desc) < 80:
                score += 10
                reasons.append(f"岗位描述过短({len(desc)}字)，可能为虚岗")
            elif len(desc) < 150:
                score += 5

        # KPI 6: 同公司挂多个岗位（累计跟踪）
        if company_name:
            self._company_job_count[company_name] = self._company_job_count.get(company_name, 0) + 1
            count = self._company_job_count[company_name]
            if count >= 10:
                score += 15
                reasons.append(f"该公司已挂 {count} 个岗位，疑似刷量")

        # KPI 7: "弹性工作" + "抗压" 组合
        if "弹性" in desc and ("抗压" in desc or "加班" in desc):
            score += 10
            reasons.append("「弹性工作」+「抗压/加班」组合，实为变相996")

        # KPI 8: 没有明确的技能要求
        skills = job.get("skills_required", [])
        if isinstance(skills, list) and len(skills) <= 1:
            score += 5

        score = max(0, min(100, score))
        result.score = score

        if score >= 60:
            result.level = RiskLevel.HIGH
        elif score >= 35:
            result.level = RiskLevel.MEDIUM
        elif score >= 15:
            result.level = RiskLevel.LOW
        else:
            result.level = RiskLevel.SAFE

        result.reasons = reasons
        return result

    # ==================== 规则模式 ====================

    def _check_via_rules(self, name: str, job: Optional[dict] = None) -> RiskResult:
        result = RiskResult()
        name_lower = name.lower()
        score = 0
        reasons = []

        # ———— 公司名维度 ————

        # 1. 人力资源/外包关键词
        for indicator in HR_OUTSOURCE_INDICATORS:
            if indicator in name:
                score += 35
                reasons.append(f"公司含「{indicator}」，可能为外包/中介")
                break

        # 2. 高风险公司名关键词
        for kw in COMPANY_NAME_HIGH_RISK:
            if kw in name:
                score += 30
                reasons.append(f"公司名含高风险词: 「{kw}」")
                break

        # 3. 配置中的自定义风险关键词
        for kw in self.risk_keywords:
            if kw in name_lower:
                score += 25
                reasons.append(f"匹配自定义风险关键词: {kw}")

        # 4. 过于通用的公司名后缀（XX科技有限公司）+ 无具体信息
        is_generic_name = any(name.endswith(suf) for suf in GENERIC_COMPANY_SUFFIX)
        if is_generic_name and job:
            has_industry = bool(job.get("company_industry", ""))
            has_size = bool(job.get("company_size", ""))
            if not has_industry and not has_size:
                score += 20
                reasons.append("公司名称过于通用且无行业/规模信息")
            elif not has_industry:
                score += 10
                reasons.append("公司名称通用且无行业信息")

        # ———— 岗位维度 ————

        if job:
            size = job.get("company_size", "")
            industry = job.get("company_industry", "")
            desc = job.get("description", "")
            salary_min = job.get("salary_min", 0)
            salary_max = job.get("salary_max", 0)

            # 5. 高风险行业
            for ri in RISKY_INDUSTRIES:
                if ri in industry:
                    score += 25
                    reasons.append(f"公司行业存在风险: {ri}")
                    break

            # 6. 小微 + 无信息 = 可疑
            if ("0-20" in size or "少于" in size) and not industry:
                score += 15
                reasons.append("小微企业且无行业信息")

            # 7. 大公司 = 加分（降低风险分）
            if "10000" in size or "1000" in size:
                score = max(0, score - 10)

            # 8. 岗位描述含明显坑
            for phrase in KPI_PHRASES:
                if phrase in desc:
                    score += 20
                    reasons.append(f"岗位描述含风险表述: 「{phrase}」")
                    break

            # 9. 薪资异常
            if salary_max > 0 and salary_max > salary_min * 5:
                score += 20
                reasons.append(f"薪资范围异常过大 ({salary_min:.0f}-{salary_max:.0f})")
            elif salary_max > 0 and salary_max > salary_min * 3:
                score += 10
                reasons.append(f"薪资范围偏大 ({salary_min:.0f}-{salary_max:.0f})")
            if salary_min > 80000:
                score += 8
                reasons.append("高薪资请自行核实")

        score = max(0, min(100, score))
        result.score = score

        if score >= 60:
            result.level = RiskLevel.HIGH
        elif score >= 35:
            result.level = RiskLevel.MEDIUM
        elif score >= 15:
            result.level = RiskLevel.LOW
        else:
            result.level = RiskLevel.SAFE

        result.reasons = reasons
        result.details = {"company": name, "mode": "rule"}

        if reasons:
            logger.info(f"  🔍 风险 [{result.level.value}] {name}: {'; '.join(reasons)}")
        else:
            logger.debug(f"  🔍 风险 [{result.level.value}] {name}: 无明显风险")
        return result

    # ==================== API 模式 ====================

    def _check_via_api(self, name: str, job: Optional[dict] = None) -> RiskResult:
        provider = self.api_cfg.get("provider", "")
        token = self.api_cfg.get("token", "")

        if not token:
            logger.warning("API token 未配置，回退到规则模式")
            return self._check_via_rules(name, job)

        try:
            if provider == "tianyancha":
                url = f"https://open.tianyancha.com/open/company/{name}/risk"
                headers = {"Authorization": token}
                resp = self._session.get(url, headers=headers, timeout=10)
                return self._parse_api_response(resp.json())
            elif provider == "qichacha":
                url = f"https://api.qichacha.com/CompanyRisk/GetRiskInfo?key={token}&company={name}"
                resp = self._session.get(url, timeout=10)
                return self._parse_api_response(resp.json())
            elif provider == "deepseek":
                return self._check_via_deepseek(name, job)
            else:
                logger.warning(f"不支持的 API 提供商: {provider}")
                return self._check_via_rules(name, job)
        except Exception as e:
            logger.error(f"API 风险检测失败: {e}")
            return self._check_via_rules(name, job)

    def _parse_api_response(self, data: dict) -> RiskResult:
        result = RiskResult()
        risk_score = data.get("riskScore", 0) or data.get("score", 0)
        if isinstance(risk_score, (int, float)):
            result.score = min(100, max(0, int(risk_score)))
            if result.score >= 70:
                result.level = RiskLevel.HIGH
            elif result.score >= 40:
                result.level = RiskLevel.MEDIUM
            elif result.score >= 15:
                result.level = RiskLevel.LOW
            else:
                result.level = RiskLevel.SAFE

        reasons = data.get("riskItems", []) or data.get("reasons", [])
        result.reasons = reasons if isinstance(reasons, list) else [str(reasons)]
        result.details = data
        return result

    # ==================== DeepSeek 模式 ====================

    def _scrape_company_info(self, name: str, _retry: bool = True) -> str:
        """用浏览器打开爱企查搜索公司（复用已登录 tab）"""
        if not self._browser:
            return ""
        if self._aiqicha_errors >= 5:
            return ""
        name = self._normalize_company_name(name)
        if not name or len(name) < 2:
            return ""

        import time as _time
        gap = _time.time() - self._aiqicha_last_at
        if gap < 2.0:
            _time.sleep(2.0 - gap)

        try:
            from urllib.parse import quote

            if self._aiqicha_tab is None:
                self._aiqicha_tab = self._browser.new_tab("https://aiqicha.baidu.com")
                self._aiqicha_tab.wait(3)

            tab = self._aiqicha_tab
            search_url = f"https://aiqicha.baidu.com/s?q={quote(name)}"
            tab.get(search_url)
            tab.wait(3)
            self._aiqicha_last_at = _time.time()

            body_text = ""
            try:
                body_text = tab.run_js("return document.body? document.body.innerText : ''") or ""
            except Exception:
                try:
                    el = tab.ele("tag:body")
                    body_text = el.text if el else ""
                except Exception:
                    pass

            result = (body_text or "").strip()[:2500]
            if not self._is_valid_aiqicha_text(result):
                logger.warning(
                    f"  🏢 爱企查「{name}」无有效结果"
                    f"（可能未登录或公司名有误）"
                )
                return ""

            self._aiqicha_errors = max(0, self._aiqicha_errors - 1)
            logger.info(
                f"  🏢 爱企查「{name}」: {len(result)} 字符"
                f" | {result[:80].replace(chr(10), ' ')}"
            )
            return result
        except Exception as e:
            self._aiqicha_errors += 1
            err = str(e).lower()
            logger.warning(f"  爱企查搜索失败「{name}」: {e}")
            if _retry and any(k in err for k in ("连接", "disconnect", "closed", "abort")):
                self._aiqicha_tab = None
                _time.sleep(3)
                return self._scrape_company_info(name, _retry=False)
            return ""

    def _check_via_deepseek(self, name: str, job: Optional[dict] = None) -> RiskResult:
        """先浏览器查爱企查 → 结果喂给 DeepSeek 分析 → 无结果时规则兜底"""
        token = self.api_cfg.get("token", "")
        if not token:
            return self._check_via_rules(name, job)

        aiqicha_data = self._scrape_company_info(name)
        has_direct_data = bool(aiqicha_data and len(aiqicha_data) > 100)

        if not has_direct_data:
            logger.info(f"  🔍 爱企查无数据，规则模式评估: \"{name}\"")
            return self._check_via_rules(name, job)

        prompt = (
            f'以下是"{name}"在爱企查上的搜索结果（浏览器实际抓取），请据此评估该公司对求职者的风险。\n\n'
            f'=== 爱企查搜索结果 ===\n{aiqicha_data[:2000]}\n=== 数据结束 ===\n\n'
            '分析要点：\n'
            '- 工商状态：是否存续/在业？有无经营异常/失信/注销？\n'
            '- 公司性质：是否人力资源/劳务派遣/外包/中介？\n'
            '- 注册资本、参保人数、成立时间等基本信息\n'
            '- 有无劳动纠纷、欠薪、裁员等负面记录\n\n'
            '返回 JSON（不要加其他文字）：\n'
            '{"score":0-100,"reasons":["基于爱企查数据的分析"],"summary":"15字内总结"}\n'
            '评分：0-14=safe  15-34=low  35-59=medium  60-100=high'
        )
        logger.info(f"  🔍 分析爱企查数据: \"{name}\"")

        self._log_deepseek("REQ-风险", f"公司: {name} | 爱企查数据: {len(aiqicha_data)}字符")

        try:
            resp = self._session.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "deepseek-chat",
                    "messages": [
                        {"role": "system", "content": "你是招聘风险评估专家。只输出JSON。"},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0,
                    "max_tokens": 500,
                },
                timeout=30,
            )
            resp.raise_for_status()
            body = resp.json()
            content = body["choices"][0]["message"]["content"]
            self._log_deepseek("RSP-风险", content[:500])
            parsed = json.loads(self._clean_json(content))
            return self._build_risk_result(parsed, name)
        except Exception as e:
            logger.error(f"DeepSeek 失败: {e}")
            return self._check_via_rules(name, job)

    # ---- DeepSeek 请求/响应日志 ----
    _deepseek_log_path = None

    @classmethod
    def _log_deepseek(cls, tag: str, content: str):
        """将 DeepSeek 请求/响应写入日志文件"""
        try:
            if cls._deepseek_log_path is None:
                from pathlib import Path as _P
                cls._deepseek_log_path = str(_P(__file__).parent / "deepseek_trace.log")
            from datetime import datetime
            ts = datetime.now().strftime("%H:%M:%S")
            with open(cls._deepseek_log_path, "a", encoding="utf-8") as f:
                f.write(f"\n[{ts}] {tag}\n{content}\n{'='*60}\n")
        except Exception:
            pass

    @staticmethod
    def _clean_json(text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = lines[1:] if lines[0].startswith("```") else lines
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)
        return text

    # 无公开信息的信号词 — 搜不到信息的公司直接跳过
    _NO_INFO_PATTERNS = [
        "未搜索到", "未找到该公司的", "无相关信息",
        "可能为不存在的公司", "名称有误",
    ]
    # 正面/已查到信息的信号 — 绝对优先，只要出现就不触发无信息判定
    _FOUND_PATTERNS = [
        "风险低", "风险极低", "安全", "safe",
        "正常经营", "科技公司", "信息技术", "网络科技",
        "知名", "上市", "注册资金", "参保人数",
        "成立", "存续", "在业",
        "有限公", "股份", "集团",
    ]

    def _build_risk_result(self, parsed: dict, name: str) -> RiskResult:
        result = RiskResult()
        result.score = min(100, max(0, int(parsed.get("score", 0))))
        result.reasons = parsed.get("reasons", [])
        if isinstance(result.reasons, str):
            result.reasons = [result.reasons]
        summary = parsed.get("summary", "")

        # 检测"无公开信息"的公司 → 标为高风险跳过
        # 但正面信号绝对优先：只要模型自己说"风险低/安全/正常经营"，就信任它
        all_text = " ".join(result.reasons) + " " + summary
        has_positive = any(p in all_text for p in self._FOUND_PATTERNS)
        if has_positive:
            # 模型已查到公司真实信息且判断低风险 → 信任模型
            result.level = self._score_to_level(result.score)
        elif any(p in all_text for p in self._NO_INFO_PATTERNS):
            # 完全搜不到信息 → 高风险跳过
            result.score = 80
            result.level = RiskLevel.HIGH
            result.reasons = ["⚠️ 无公开信息，风险未知，跳过"]
        else:
            result.level = self._score_to_level(result.score)

        result.details = {"company": name, "mode": "deepseek+search",
                          "summary": summary, "raw": parsed}
        logger.info(
            f"  🤖 DeepSeek [{result.level.value}] {name}: "
            f"score={result.score} | {summary}"
        )
        return result

    @staticmethod
    def _score_to_level(score: int) -> RiskLevel:
        if score >= 60: return RiskLevel.HIGH
        if score >= 35: return RiskLevel.MEDIUM
        if score >= 15: return RiskLevel.LOW
        return RiskLevel.SAFE

    # ==================== AI 岗位匹配（DeepSeek） ====================

    def match_job(self, resume_text: str, job: dict) -> Optional[dict]:
        """用 DeepSeek 实时分析岗位是否匹配简历。

        返回 {
            "score": 0-100,       # 综合匹配分
            "fit": True/False,    # 是否推荐投递
            "reasons": [...],     # 匹配/不匹配的具体理由
            "missing": [...],     # 候选人缺少的技能/经验
        }
        失败返回 None → 回退规则匹配
        """
        token = self.api_cfg.get("token", "")
        if not token:
            return None

        resume_snippet = resume_text[:5000] if resume_text else ""
        if len(resume_snippet.strip()) < 80:
            logger.warning("  AI匹配跳过: 简历文本过短")
            return None
        title = job.get("title", "")
        desc = (job.get("description") or "")[:2000]
        skills = job.get("skills_required", [])
        location = job.get("location", "")
        education = job.get("education", "")
        experience = job.get("experience", "")
        salary = job.get("salary", "")

        prompt = (
            "你是求职匹配专家。根据候选人简历（可能包含多份不同方向的简历），判断以下岗位是否值得投递。\n\n"
            "⚠️ 重要：\n"
            "- 候选人可能有多份简历覆盖不同技术方向（如一份嵌入式、一份AI/Python），"
            "请综合所有简历的技能来判断，只要任一简历的方向与岗位匹配即可。\n"
            "- ⚠️ 岗位描述(JD)才是技能要求的核心来源！不要只看标题，"
            "必须从描述中提取实际的技术栈、工具链、领域知识要求，再与简历比对。\n"
            "- 标题可能是 HR 随手写的，描述里的「任职要求/岗位职责」才是真实的。\n\n"
            "=== 岗位信息 ===\n"
            f"标题：{title}\n"
            f"薪资：{salary} | 地点：{location}\n"
            f"经验要求：{experience} | 学历要求：{education}\n"
            f"标签技能：{', '.join(skills) if skills else '无'}\n"
            f"--- 岗位描述(JD) ---\n"
            f"{desc if desc else '（无描述，标题为唯一参考）'}\n"
            f"--- 描述结束 ---\n\n"
            f"=== 候选人简历 ===\n{resume_snippet}\n\n"
            "分析步骤（严格按顺序）：\n"
            "1. 从「岗位描述」中提取实际技术要求：需要哪些编程语言？框架？工具？领域知识？\n"
            "2. 将提取的要求与所有简历逐一比对：简历中有没有这些技能？直接相关还是可迁移？\n"
            "3. 判断经验/学历是否满足\n"
            "4. 判断岗位真实性：JD 是否笼统空泛？是否像刷KPI/培训广告？\n\n"
            "返回 JSON（不要加其他文字）：\n"
            '{"score":0-100,"fit":true/false,'
            '"reasons":["理由：哪些技能匹配/不匹配，依据JD中的哪条要求"],'
            '"missing":["候选人缺少的关键技能"]}\n\n'
            "score>=75=技术要求大部分匹配，值得投递 fit=true\n"
            "score 50-74=部分匹配可以投 fit=true\n"
            "score 35-49=核心技能不匹配，不建议投 fit=false\n"
            "score<35=方向完全不符 fit=false"
        )

        self._log_deepseek("REQ-匹配", f"岗位: {title} @ {job.get('company','?')}\n简历: {resume_snippet[:300]}")

        last_err = None
        for attempt in range(3):
            try:
                resp = self._session.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "deepseek-chat",
                        "messages": [
                            {"role": "system", "content": "你是求职匹配专家，候选人可能有多份不同方向的简历，请综合判断。只输出JSON。"},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.1,
                        "max_tokens": 500,
                    },
                    timeout=45,
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                self._log_deepseek("RSP-匹配", content[:600])
                parsed = json.loads(self._clean_json(content))

                score = min(100, max(0, int(parsed.get("score", 50))))
                fit = parsed.get("fit", score >= 50)
                reasons = parsed.get("reasons", [])
                if isinstance(reasons, str):
                    reasons = [reasons]
                missing = parsed.get("missing", [])
                if isinstance(missing, str):
                    missing = [missing]

                logger.info(
                    f"  🎯 AI匹配: fit={fit} score={score} | "
                    f"{'; '.join(reasons[:2]) if reasons else ''}"
                )
                return {"score": score, "fit": fit, "reasons": reasons, "missing": missing}
            except Exception as e:
                last_err = e
                if attempt < 2:
                    import time as _time
                    logger.warning(f"  AI匹配重试({attempt + 1}/3): {e}")
                    _time.sleep(2 + attempt * 2)
                    continue
        logger.error(f"AI匹配失败: {last_err}")
        return None

    # ==================== 简历分析 → 搜索关键词（DeepSeek） ====================

    def suggest_search_keywords(self, resume_text: str) -> list[str]:
        """用 DeepSeek 分析简历，推荐 BOSS直聘 搜索关键词。

        返回关键词列表，如 ["AI应用开发", "Python开发", "机器学习"]
        失败返回空列表 → 使用配置文件中的关键词
        """
        token = self.api_cfg.get("token", "")
        if not token:
            return []

        resume_snippet = resume_text[:2500] if resume_text else ""

        prompt = (
            "你是求职岗位分析专家。根据候选人简历，推荐在BOSS直聘上应该搜索哪些岗位关键词。\n\n"
            f"=== 候选人简历 ===\n{resume_snippet}\n\n"
            "要求：\n"
            "1. 提取简历中的核心技术栈和方向，生成3-6个精准的搜索关键词\n"
            "2. 关键词应该是BOSS直聘上的常见岗位名称（如：Python开发工程师、AI应用开发、数据分析师）\n"
            "3. 优先推荐与候选人技能直接匹配的岗位方向\n"
            "4. 关键词不要太宽泛（如只写AI），也要有具体岗位名\n"
            "5. 按匹配度从高到低排序\n\n"
            "返回 JSON（不要加其他文字）：\n"
            '{"keywords": ["关键词1", "关键词2", "关键词3", ...]}'
        )

        try:
            resp = self._session.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "deepseek-chat",
                    "messages": [
                        {"role": "system", "content": "你是求职分析专家，只输出JSON。"},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 300,
                },
                timeout=20,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            parsed = json.loads(self._clean_json(content))
            keywords = parsed.get("keywords", [])
            if isinstance(keywords, list) and keywords:
                logger.info(f"  🧠 AI推荐搜索关键词: {', '.join(keywords)}")
                return keywords
        except Exception as e:
            logger.error(f"AI关键词推荐失败: {e}")
        return []
