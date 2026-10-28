"""Read the signed-in user's own online resume and declared search expectations."""
import re
import time
from dataclasses import dataclass
from resume_parser import ResumeParser, ResumeData
from job_search import is_boss_security_page, JobSearcher


@dataclass
class OnlineProfile:
    resume: ResumeData
    targets: list[dict]


def profile_from_data(data, use_expectations=True):
    name = str(data.get('name') or '').strip()
    sections = data.get('sections') or {}
    raw = '姓名：' + name + '\n' + '\n'.join(str(value) for value in sections.values())
    if not name or len(raw) < 200:
        raise ValueError('BOSS 在线简历尚未完整显示，停止投递')
    parser = ResumeParser('boss_online_resume.txt')
    resume = parser.parse_text(raw, 'BOSS在线简历')
    resume.name = name
    resume.work_experiences = parser._parse_entries(str(sections.get('work') or '').splitlines(), 'work')
    resume.education = parser._parse_entries(str(sections.get('education') or '').splitlines(), 'edu')
    years = re.search(r'(\d+)年经验', str(data.get('base') or ''))
    resume.declared_experience_years = int(years.group(1)) if years else None
    if not use_expectations:
        return OnlineProfile(resume, [])
    targets = []
    seen = set()
    for expectation in data.get('expectations') or []:
        keyword = str(expectation.get('position') or '').strip()
        salary = str(expectation.get('salary') or '').strip()
        for city in re.split(r'[，,、；;\s]+', str(expectation.get('cities') or '')):
            city = city.removesuffix('市')
            if not keyword or not city or (keyword, city) in seen:
                continue
            code = JobSearcher.CITY_CODES.get(city)
            if not code:
                raise ValueError('在线简历城市代码暂未配置: ' + city)
            targets.append({'keyword': keyword, 'city': city, 'city_code': code,
                            'salary': '' if salary == '面议' else salary})
            seen.add((keyword, city))
    if not targets:
        raise ValueError('在线简历没有可识别的期望职位/城市，停止投递')
    return OnlineProfile(resume, targets)


def read_online_profile(browser, timeout=20, use_expectations=True):
    tab = browser.new_tab('https://www.zhipin.com/web/geek/resume')
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if is_boss_security_page(tab, strict=True):
                raise RuntimeError('在线简历页面需要网站验证')
            data = tab.run_js("""
                const text = selector => document.querySelector(selector)?.innerText.trim() || '';
                return {name:text('.resume-baseInfo .name'), base:text('.resume-baseInfo .user-info'),
                    sections: {summary:text('.resume-userDesc'), work:text('.resume-workExpList'),
                               projects:text('.resume-projectExpList'), education:text('.resume-educationExpList'),
                               skills:text('.resume-professionalSkill')},
                    expectations:[...document.querySelectorAll('.expect-list-item')].map(e=>({
                        position:e.querySelector('.position-item')?.innerText.trim() || '',
                        salary:e.querySelector('.money-item')?.innerText.trim() || '',
                        cities:e.querySelector('.city-item')?.innerText.trim() || ''}))};
            """) or {}
            if (data.get('name') and data.get('sections', {}).get('work')
                    and (data.get('expectations') or not use_expectations)):
                return profile_from_data(data, use_expectations=use_expectations)
            time.sleep(.3)
        raise TimeoutError('未在限时内读取到已登录账号的在线简历')
    finally:
        tab.close()
