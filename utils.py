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


def find_resume_file(base_dir: Optional[Path] = None) -> Optional[Path]:
    """自动扫描目录找到简历文件（.pdf/.docx/.txt）"""
    root = base_dir or project_root()
    best = None
    name_kw = ["简历", "resume", "cv"]
    candidates = []
    for f in root.iterdir():
        if not f.is_file(): continue
        if f.suffix.lower() not in (".pdf", ".docx", ".txt"): continue
        if f.name == "requirements.txt": continue
        priority = 0 if any(k in f.name.lower() for k in name_kw) else 1
        candidates.append((priority, -f.stat().st_mtime, f))
    if not candidates: return None
    candidates.sort()
    return candidates[0][2]


def find_all_resume_files(base_dir: Optional[Path] = None) -> list[Path]:
    """自动扫描目录找到所有简历文件（.pdf/.docx/.txt），按修改时间倒序"""
    root = base_dir or project_root()
    name_kw = ["简历", "resume", "cv"]
    candidates = []
    for f in root.iterdir():
        if not f.is_file(): continue
        if f.suffix.lower() not in (".pdf", ".docx", ".txt"): continue
        if f.name == "requirements.txt": continue
        # 优先匹配简历关键词，其次按修改时间
        priority = 0 if any(k in f.name.lower() for k in name_kw) else 1
        candidates.append((priority, -f.stat().st_mtime, f))
    if not candidates: return []
    candidates.sort()
    return [c[2] for c in candidates]
