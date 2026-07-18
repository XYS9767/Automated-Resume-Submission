"""
工具函数模块 — 日志、随机延时、配置加载、文件路径等
"""

import os
import re
import sys
import json
import time
import random
import logging
from pathlib import Path
from typing import Optional

import yaml


# ---------- 路径 ----------

def project_root() -> Path:
    """返回项目根目录（exe模式=exe所在目录, 否则=源码目录）"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent.resolve()
    return Path(__file__).parent.resolve()


def _pyinstaller_data_dir() -> Path:
    """PyInstaller 打包后资源文件的临时解压目录"""
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS).resolve()
    return project_root()


def resolve_path(path_str: str) -> Path:
    """将相对路径解析为绝对路径

    读取文件: 优先 exe 目录, 其次 PyInstaller 资源目录
    写入文件: 始终写入 exe 目录（不污染临时目录）
    """
    p = Path(path_str)
    if p.is_absolute():
        return p

    root = project_root()
    candidate = root / p
    # 如果是读取已有文件，且 exe 目录没有，尝试 PyInstaller 资源目录
    if candidate.exists():
        return candidate
    data_dir = _pyinstaller_data_dir()
    alt = data_dir / p
    if alt.exists():
        return alt
    # 不存在时返回 exe 目录（用于写入新文件）
    return candidate


# ---------- 配置 ----------

_config_cache: dict[str, dict] = {}


def load_config(path: str = "config.yaml") -> dict:
    """加载 YAML 配置文件（按路径缓存）"""
    global _config_cache
    if path in _config_cache:
        return _config_cache[path]
    cfg_path = resolve_path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {cfg_path}")
    with open(cfg_path, "r", encoding="utf-8") as f:
        _config_cache[path] = yaml.safe_load(f)
    return _config_cache[path]


def reload_config(path: str = "config.yaml"):
    """清除缓存，强制重新加载配置"""
    global _config_cache
    _config_cache.pop(path, None)
    return load_config(path)


# ---------- 日志 ----------

_log_initialized: set[str] = set()


def setup_logger(name: str = "auto-boss", level: str = "INFO") -> logging.Logger:
    """初始化日志器（支持多个命名 logger 独立配置）"""
    global _log_initialized
    logger = logging.getLogger(name)
    if name in _log_initialized:
        return logger

    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
    }
    logger.setLevel(level_map.get(level.upper(), logging.INFO))

    if not logger.handlers:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(level_map.get(level.upper(), logging.INFO))
        fmt = logging.Formatter(
            "[%(asctime)s] %(levelname)-7s | %(message)s",
            datefmt="%H:%M:%S",
        )
        ch.setFormatter(fmt)
        logger.addHandler(ch)

    _log_initialized.add(name)
    return logger


def get_logger(name: str = "auto-boss") -> logging.Logger:
    return logging.getLogger(name)


# ---------- 随机延时 ----------

def random_delay(min_sec: float = 1.5, max_sec: float = 4.0):
    """随机等待，模拟人类操作间隔"""
    delay = random.uniform(min_sec, max_sec)
    time.sleep(delay)


def human_delay():
    """短随机延时，适合页面操作间"""
    time.sleep(random.uniform(0.3, 1.2))


# ---------- 文本清理 ----------

def clean_text(text: str) -> str:
    """清理文本：去除多余空白、特殊字符"""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_numbers(text: str) -> list:
    """提取文本中所有数字（含小数）"""
    return [float(x) for x in re.findall(r"\d+\.?\d*", text)]


def extract_salary_range(text: str) -> tuple:
    """
    从薪资描述中提取范围，如 "15K-30K" → (15000, 30000)
    返回 (min_salary, max_salary)，无法解析返回 (0, 0)
    """
    if not text:
        return (0, 0)
    # 匹配类似 "15K-30K" / "15k-30k" / "15K-30K·14薪"
    pattern = r"(\d+\.?\d*)\s*[Kk]\s*[-~–—]\s*(\d+\.?\d*)\s*[Kk]"
    m = re.search(pattern, text)
    if m:
        return (float(m.group(1)) * 1000, float(m.group(2)) * 1000)
    # 尝试 "15-30K" 格式
    pattern2 = r"(\d+\.?\d*)\s*[-~–—]\s*(\d+\.?\d*)\s*[Kk]"
    m2 = re.search(pattern2, text)
    if m2:
        return (float(m2.group(1)) * 1000, float(m2.group(2)) * 1000)
    return (0, 0)


# ---------- BOSS 活跃状态 ----------

# 数值越小越活跃；99=未知
BOSS_ACTIVE_RANK: dict[str, int] = {
    "在线": 0,
    "刚刚活跃": 0,
    "刚刚在线": 0,
    "今日活跃": 1,
    "3日内活跃": 2,
    "本周活跃": 3,
    "2周内活跃": 3,
    "本月活跃": 4,
    "2月内活跃": 4,
    "近半年活跃": 5,
    "半年前活跃": 6,
}


def boss_active_rank(text: str) -> int:
    """将 BOSS 活跃描述转为等级（越小越活跃）"""
    if not text:
        return 99
    text = text.strip()
    if text in BOSS_ACTIVE_RANK:
        return BOSS_ACTIVE_RANK[text]
    if "刚刚" in text or text == "在线":
        return 0
    if "今日" in text:
        return 1
    if "3日" in text:
        return 2
    if "本周" in text or "2周" in text:
        return 3
    if "本月" in text or "2月" in text:
        return 4
    if "近半年" in text:
        return 5
    if "半年" in text:
        return 6
    return 99


def parse_max_boss_inactive(cfg_value: str) -> Optional[int]:
    """解析 max_boss_inactive 配置，返回允许的最差等级；None 表示不限制"""
    if not cfg_value or str(cfg_value).strip() in ("", "不限", "无", "none", "None"):
        return None
    return boss_active_rank(str(cfg_value).strip())


# ---------- 文件辅助 ----------

def ensure_dir(path: str):
    """确保目录存在"""
    p = resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)


def safe_save_json(data, path: str):
    """安全写入 JSON 文件"""
    p = resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# 非简历文件（自动扫描时排除）
_RESUME_EXCLUDE_NAMES = frozenset({
    "requirements.txt", "capture_log.txt", "deepseek_trace.log",
    "readme.txt", "license.txt",
})


def _is_resume_filename(name: str) -> bool:
    lower = name.lower()
    if lower in _RESUME_EXCLUDE_NAMES:
        return False
    return any(k in lower for k in ("简历", "resume", "cv"))


def find_resume_file(base_dir: Optional[Path] = None) -> Optional[Path]:
    """自动扫描目录找到简历文件（.pdf/.docx/.txt）"""
    files = find_all_resume_files(base_dir)
    return files[0] if files else None


def find_all_resume_files(base_dir: Optional[Path] = None) -> list[Path]:
    """扫描目录下的简历文件；根目录扫描时仅保留文件名含 简历/resume/cv 的文件"""
    root = base_dir or project_root()
    is_resume_dir = base_dir is not None and base_dir.name.lower() in ("resumes", "resume")
    candidates = []
    for f in root.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() not in (".pdf", ".docx", ".txt"):
            continue
        if f.name.lower() in _RESUME_EXCLUDE_NAMES:
            continue
        if not is_resume_dir and not _is_resume_filename(f.name):
            continue
        priority = 0 if _is_resume_filename(f.name) else 1
        candidates.append((priority, -f.stat().st_mtime, f))
    if not candidates:
        return []
    candidates.sort()
    return [c[2] for c in candidates]
