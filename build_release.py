"""Build a portable Windows release from explicit public payload files only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import yaml

ROOT = Path(__file__).resolve().parent
APP = 'BOSS-Auto-Apply'
PAYLOAD = ('config.yaml', 'README.txt', '启动.bat', '预览岗位.bat', '检查环境.bat', '停止自动投递.bat')
METADATA = ('DrissionPage', 'PyYAML', 'requests', 'python-docx', 'PyMuPDF', 'pdfplumber', 'jieba', 'filelock')


def validate_public_config(config):
    from utils import validate_config
    validate_config(config)
    def inspect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if any(word in str(key).lower() for word in ('token', 'password', 'secret', 'api_key')) and item:
                    raise ValueError('Public configuration contains a credential field: ' + str(key))
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)
    inspect(config)
    if config['resume'].get('file_path') or config['resume'].get('file_paths'):
        raise ValueError('Public configuration must not reference a personal resume')
    expected = ((config['browser'], 'data_dir', './browser_data'),
                (config['login'], 'cookie_file', './cookies.json'),
                (config['output'], 'record_file', './apply_record.json'),
                (config['output'], 'log_file', './submit_log.csv'))
    for section, key, value in expected:
        if section.get(key) != value:
            raise ValueError('Public configuration must use a fresh relative path: ' + key)


def check_archive(path, package_name):
    expected = {package_name + '/' + name for name in (*PAYLOAD, APP + '.exe', 'LICENSE', 'build-info.json')}
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(expected) or set(names) != expected:
            raise ValueError('Release archive contains missing or unexpected files')
        validate_public_config(yaml.safe_load(archive.read(package_name + '/config.yaml')))
        if archive.testzip() is not None:
            raise ValueError('Release archive CRC check failed')


def verify_archive(path, package_name):
    check_archive(path, package_name)
    with tempfile.TemporaryDirectory(prefix='BOSS-发布验证-') as temporary:
        with zipfile.ZipFile(path) as archive:
            archive.extractall(temporary)
        package = Path(temporary) / package_name
        env = dict(os.environ, PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
        env['PATH'] = os.environ.get('SystemRoot', r'C:\Windows') + r'\System32'
        for key in ('PYTHONHOME', 'PYTHONPATH', 'DEEPSEEK_API_KEY'):
            env.pop(key, None)
        for flag in ('--help', '--doctor'):
            result = subprocess.run([str(package / (APP + '.exe')), flag], cwd=temporary,
                                    env=env, capture_output=True, encoding='utf-8', errors='replace', timeout=90)
            print(result.stdout, end='')
            if result.returncode:
                raise RuntimeError('Packaged executable failed ' + flag + ': ' + result.stderr)
        if (package / 'browser_data').exists() or (package / 'submission_state.json').exists():
            raise RuntimeError('Offline verification unexpectedly started a browser or submission')


def build(version):
    if sys.platform != 'win32' or sys.maxsize <= 2**32:
        raise RuntimeError('Build requires 64-bit Windows Python')
    if not re.fullmatch(r'[0-9][A-Za-z0-9._-]*', version):
        raise ValueError('Invalid release version')
    template = ROOT / 'packaging'
    validate_public_config(yaml.safe_load((template / 'config.yaml').read_text(encoding='utf-8')))
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    package_name = f'{APP}-{version}-windows-x64'
    package = output / package_name
    if package.exists():
        raise FileExistsError('Use a new version or move the previous package first: ' + str(package))
    work = ROOT / 'build' / version
    work.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--console',
               '--name', APP, '--distpath', str(work / 'binary'), '--workpath', str(work / 'work'),
               '--specpath', str(work), '--collect-all', 'DrissionPage', '--collect-all', 'jieba']
    for module in ('docx', 'fitz', 'pdfplumber', 'filelock'):
        command += ['--hidden-import', module]
    for distribution in METADATA:
        command += ['--copy-metadata', distribution]
    subprocess.run(command + [str(ROOT / 'main.py')], cwd=ROOT, check=True)
    package.mkdir()
    shutil.copy2(work / 'binary' / (APP + '.exe'), package)
    for name in PAYLOAD:
        shutil.copy2(template / name, package / name)
    shutil.copy2(ROOT / 'LICENSE', package)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip())
    sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(ROOT.glob('*.py'))}
    (package / 'build-info.json').write_text(json.dumps(
        {'version': version, 'commit': commit, 'working_tree_dirty': dirty, 'source_sha256': sources},
        ensure_ascii=False, indent=2), encoding='utf-8')
    archive_path = output / (package_name + '.zip')
    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in (*PAYLOAD, APP + '.exe', 'LICENSE', 'build-info.json'):
            archive.write(package / name, package_name + '/' + name)
    verify_archive(archive_path, package_name)
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    (output / 'SHA256SUMS.txt').write_text(digest + '  ' + archive_path.name + '\n', encoding='ascii')
    print('VERIFIED RELEASE:', archive_path)
    print('SHA256:', digest)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', default='2026.10.28')
    build(parser.parse_args().version)
