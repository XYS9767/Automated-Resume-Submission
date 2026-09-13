"""Offline startup diagnostics; no account/network interaction."""
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path
from utils import load_config, resolve_path, find_all_resume_files


def diagnose(config_path='config.yaml'):
    errors = 0
    try:
        config = load_config(config_path)
        print('[OK] YAML 配置和数值范围有效')
    except Exception as exc:
        print('[ERROR]', exc)
        return 1
    for package in ('DrissionPage', 'PyYAML', 'requests', 'python-docx', 'PyMuPDF', 'filelock'):
        try:
            print('[OK]', package, version(package))
        except PackageNotFoundError:
            print('[ERROR] 未安装:', package)
            errors += 1
    try:
        from boss_login import BossLogin
        print('[OK] 浏览器:', config['browser'].get('executable') or BossLogin._find_edge_exe())
    except Exception as exc:
        print('[ERROR]', exc)
        errors += 1
    cfg = config['resume']
    if cfg.get('source') == 'boss_online':
        print('[OK] 简历来源：当前登录 BOSS 账号的在线简历，运行时自动读取，无需本地 PDF')
    else:
        directory = resolve_path(cfg.get('resume_dir') or 'resumes')
        paths = find_all_resume_files(directory) if directory.is_dir() else []
        paths += [resolve_path(p) for p in cfg.get('file_paths', [])]
        if cfg.get('file_path'):
            paths.append(resolve_path(cfg['file_path']))
        if any(p.is_file() for p in paths):
            print('[OK] 已找到简历文件')
        else:
            print('[待配置] 把自己的 PDF/DOCX/TXT 简历放入 resumes 文件夹')
    from company_risk import CompanyRiskChecker
    checker = CompanyRiskChecker(config)
    print('[OK] 匹配模式:', 'AI + 规则回退' if checker.ai_available else '本地规则（配置有效 Key 后启用 AI）')
    print('关键词:', '、'.join(config['search']['keywords']) or '空，需配置/AI推荐')
    print('城市:', config['search'].get('city'), '搜索页数:', config['search']['page_limit'])
    print('此检查不会登录、发起沟通或消耗投递次数。')
    return 1 if errors else 0
