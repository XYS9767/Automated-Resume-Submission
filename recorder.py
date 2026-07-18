"""
结果记录模块

将每次投递的结果记录到:
  1. CSV 日志 — 便于用 Excel 打开查看
  2. JSON 详细记录 — 包含完整的匹配维度信息
"""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from job_search import JobPosting
from job_matcher import MatchResult
from company_risk import RiskResult
from submitter import SubmitResult
from utils import resolve_path, safe_save_json, ensure_dir, get_logger

logger = get_logger("recorder")


class ApplyRecorder:
    """
    投递记录器
    线程安全（单线程场景）
    """

    def __init__(self, config: dict):
        output_cfg = config.get("output", {})
        self.csv_path = resolve_path(output_cfg.get("log_file", "./submit_log.csv"))
        self.json_path = resolve_path(output_cfg.get("record_file", "./apply_record.json"))
        self._records: list[dict] = []
        self._load_existing()

    def record(
        self,
        job: JobPosting,
        match: Optional[MatchResult] = None,
        risk: Optional[RiskResult] = None,
        submit_result: Optional[str] = None,
    ):
        """
        记录一次投递结果

        Args:
            job: 岗位信息
            match: 匹配结果（可选）
            risk: 风险检测结果（可选）
            submit_result: 投递结果（SubmitResult 的静态属性值）
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record = {
            "timestamp": timestamp,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "title": job.title,
            "company": job.company,
            "salary": job.salary,
            "location": job.location,
            "experience": job.experience,
            "education": job.education,
            "url": job.url,
            "company_size": job.company_size,
            "company_industry": job.company_industry,
            "tags": "; ".join(job.tags),
        }

        # 添加匹配信息
        if match:
            record.update({
                "match_score": round(match.total_score, 1),
                "skill_score": round(match.skill_score, 1),
                "experience_score": round(match.experience_score, 1),
                "education_score": round(match.education_score, 1),
                "salary_score": round(match.salary_score, 1),
                "keyword_score": round(match.keyword_score, 1),
                "missing_skills": "; ".join(match.missing_skills[:5]),
                "match_details": " | ".join(match.match_details),
            })
        else:
            record.update({
                "match_score": "",
                "skill_score": "",
                "experience_score": "",
                "education_score": "",
                "salary_score": "",
                "keyword_score": "",
                "missing_skills": "",
                "match_details": "",
            })

        # 添加风险信息
        if risk:
            record["risk_level"] = risk.level.value
            record["risk_score"] = risk.score
            record["risk_reasons"] = "; ".join(risk.reasons)
        else:
            record["risk_level"] = ""
            record["risk_score"] = ""
            record["risk_reasons"] = ""

        # 添加投递结果
        result_map = {
            SubmitResult.SUCCESS: "投递成功",
            SubmitResult.SKIPPED: "已跳过",
            SubmitResult.ALREADY_APPLIED: "已投递过",
            SubmitResult.LOGIN_REQUIRED: "需重新登录",
            SubmitResult.DAILY_LIMIT: "已达每日上限",
            SubmitResult.FAILED: "投递失败",
            SubmitResult.RISK_REJECTED: "公司风险过高,跳过",
            SubmitResult.MATCH_LOW: "匹配度过低,跳过",
            SubmitResult.KPI_REJECTED: "KPI刷量岗位,跳过",
        }
        record["result"] = result_map.get(submit_result, submit_result or "未知")

        # 追加到内存列表
        self._records.append(record)

        # 写入 CSV
        self._append_csv(record)

        logger.info(
            f"记录: {record['result']} | "
            f"{record['company']} - {record['title']} | "
            f"匹配 {record.get('match_score', 'N/A')}"
        )

    def summary(self) -> dict:
        """
        生成汇总统计
        """
        total = len(self._records)
        if total == 0:
            return {"total": 0, "message": "暂无投递记录"}

        success = sum(
            1 for r in self._records
            if r.get("result") == "投递成功"
        )
        failed = sum(
            1 for r in self._records
            if r.get("result") == "投递失败"
        )
        skipped = sum(
            1 for r in self._records
            if r.get("result") in ("匹配度过低,跳过", "公司风险过高,跳过", "已投递过")
        )

        # 平均匹配分
        scores = [
            r.get("match_score", 0) or 0
            for r in self._records
            if r.get("match_score")
        ]
        avg_score = sum(scores) / len(scores) if scores else 0

        return {
            "total": total,
            "success": success,
            "failed": failed,
            "skipped": skipped,
            "avg_match_score": round(avg_score, 1),
            "success_rate": f"{success / total * 100:.1f}%" if total > 0 else "0%",
        }

    def save_json(self):
        """保存完整记录到 JSON 文件"""
        safe_save_json(
            {
                "records": self._records,
                "summary": self.summary(),
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            str(self.json_path),
        )
        logger.info(f"详细记录已保存: {self.json_path}")

    def is_already_processed(self, job_url: str) -> bool:
        """检查某个岗位 URL 是否已经处理过（成功投递/已跳过）"""
        if not job_url:
            return False
        for r in self._records:
            if r.get("url") == job_url:
                return True
        return False

    def is_already_success(self, job_url: str) -> bool:
        """检查某个岗位 URL 是否已成功投递过"""
        if not job_url:
            return False
        for r in self._records:
            if r.get("url") == job_url and r.get("result") == "投递成功":
                return True
        return False

    def get_processed_urls(self) -> set[str]:
        """获取所有已处理过的岗位 URL"""
        return {r.get("url", "") for r in self._records if r.get("url")}

    # ---------- 内部方法 ----------

    def _load_existing(self):
        """加载已有的 JSON 记录"""
        if self.json_path.exists():
            try:
                with open(self.json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._records = data.get("records", [])
                logger.info(f"加载已有记录 {len(self._records)} 条")
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"加载历史记录失败: {e}")

    def _append_csv(self, record: dict):
        """追加一行到 CSV 文件"""
        ensure_dir(str(self.csv_path))

        # CSV 列定义
        fieldnames = [
            "timestamp", "date", "result",
            "company", "title", "salary", "location",
            "experience", "education",
            "match_score", "skill_score", "experience_score",
            "education_score", "salary_score", "keyword_score",
            "risk_level", "risk_score",
            "missing_skills", "company_size", "company_industry",
            "url",
        ]

        file_exists = self.csv_path.exists()
        try:
            with open(self.csv_path, "a", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                if not file_exists:
                    writer.writeheader()
                # 只写入 CSV 列定义的字段
                row = {k: record.get(k, "") for k in fieldnames}
                writer.writerow(row)
        except Exception as e:
            logger.error(f"CSV 写入失败: {e}")
