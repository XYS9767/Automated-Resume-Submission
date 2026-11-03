from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from job_matcher import JobMatcher, graduation_cohorts
from job_search import JobPosting
from main import AutoBossApp
from relevance import extract_ecommerce_skills, keyword_matches
from resume_parser import Experience, ResumeData, ResumeParser


@pytest.fixture
def resume():
    return ResumeData(
        raw_text='电商运营 京东 淘宝 天猫 商品上下架 标题优化 主图AB测试 '
                 '竞品调研 详情优化 生意参谋 京东商智 京东快车 数据复盘',
        education=[Experience(title_or_major='大专')],
        declared_experience_years=2,
    )


def test_ecommerce_aliases_and_missing_tools():
    skills = extract_ecommerce_skills('网店运营 商品上下架 主图AB测试 竞品调研 生意参谋')
    assert {'电商运营', '商品上架', '主图优化', '竞品分析', '生意参谋'} <= skills
    assert '直通车' not in skills
    assert keyword_matches('网店运营专员', '电商运营')
    assert not keyword_matches('电话销售', '电商运营', '负责客户电话沟通')


def test_separate_employers_and_nonstandard_month(tmp_path):
    path = tmp_path / 'resume.txt'
    path.write_text(
        '教育背景Education\n2023.09~2025.06\n示例职业学院\n电子商务\n大专\n'
        '实习实践Experiences\n2026.04~至今\n示例科技有限公司\n国内电商运营\n'
        '负责京东自营、淘宝店铺运维和商品主图AB测试。\n'
        '2025.07~2026~03\n示例宠物用品有限公司\n国内电商运营\n'
        '通过生意参谋分析数据。\n'
        '2025.01~2025.06\n示例电子商务有限公司\n电商运营助理\n商品上架与标题优化',
        encoding='utf-8')
    parsed = ResumeParser(str(path)).parse()
    assert len(parsed.education) == 1
    assert [e.company_or_school for e in parsed.work_experiences] == [
        '示例科技有限公司', '示例宠物用品有限公司', '示例电子商务有限公司']
    assert parsed.work_experiences[1].end_date == '2026~03'
    assert parsed.work_experiences[2].title_or_major == '电商运营助理'
    assert {'京东', '淘宝', '生意参谋', '主图优化'} <= set(parsed.skills)


def test_overlapping_work_months_are_counted_once():
    data = ResumeData(work_experiences=[
        Experience(start_date='2024.01', end_date='2024.12'),
        Experience(start_date='2024.07', end_date='2025.06'),
    ])
    assert JobMatcher(data)._my_exp_years == 1.5


def test_relevant_ecommerce_beats_unfamiliar_platform(resume):
    matcher = JobMatcher(resume, expected_salary='6-9K')
    common = dict(salary='6-9K', salary_min=6000, salary_max=9000,
                  education='大专', experience='1-3年')
    familiar = matcher.match(JobPosting(
        title='京东运营', description='负责京东商品上架、标题优化、竞品分析，使用京东商智和京东快车。', **common))
    unfamiliar = matcher.match(JobPosting(
        title='抖店运营', description='负责抖店、拼多多店铺运营，熟练万相台、引力魔方及直通车。', **common))
    assert familiar.total_score >= 75
    assert familiar.salary_score == 10
    assert familiar.total_score > unfamiliar.total_score
    assert '直通车' in unfamiliar.missing_skills


@pytest.mark.parametrize('salary,allowed', [
    ('5-8K', False), ('6-9K', True), ('8-12K', True),
    ('10-15K', False), ('面议', False), ('300元/天', False),
])
def test_salary_floor_remains_a_hard_filter(salary, allowed):
    reason = AutoBossApp._salary_rejection_reason(SimpleNamespace(salary=salary), '6-9K')
    assert (not reason) is allowed


@pytest.fixture
def graduate_matcher(resume):
    resume.education[0].end_date = '2025.06'
    return JobMatcher(resume, expected_salary='6-9K')


@pytest.mark.parametrize('text,years', [
    ('【26届】国内电商管培生', {2026}),
    ('面向2025届毕业生', {2025}),
    ('25/26届均可', {2025, 2026}),
    ('2024-2026届', {2024, 2025, 2026}),
    ('面向2026年毕业生', {2026}),
    ('2026年公司拓展国内电商业务', set()),
])
def test_explicit_graduation_cohorts(text, years):
    assert graduation_cohorts(text) == years


@pytest.mark.parametrize('title,description,rejected', [
    ('【26届】国内电商管培生', '负责京东店铺运营', True),
    ('【26届】国内电商管培生', '负责淘宝运营，另有25届员工培养计划。', True),
    ('国内电商运营', '仅限2026届毕业生，负责淘宝店铺', True),
    ('【25届】淘宝运营', '负责淘宝店铺运营', False),
    ('【25/26届】淘宝运营', '负责淘宝店铺运营', False),
    ('亚马逊跨境电商运营专员', '负责电商运营及数据分析', True),
    ('电商运营', '负责Amazon平台商品上架，具有淘宝运营经验', True),
    ('TikTok Shop运营', '熟悉淘宝商品上架', True),
    ('海外独立站运营', '使用Shopify开展海外销售', True),
    ('淘宝运营', '负责淘宝运营，无需跨境经验', False),
    ('电商运营', '无需跨境经验，负责Amazon平台和淘宝运营', True),
    ('天猫京东运营', '负责天猫、京东商品上架和数据分析', False),
    ('国内电商运营', '负责淘系店铺商品上架和活动报名', False),
    ('抖店运营', '负责抖店、拼多多商品和推广', True),
    ('电商运营', '负责电商销售和客户管理', True),
    ('多平台电商运营', '负责天猫、拼多多、京东、抖音的商品运营', False),
])
def test_domestic_direction_and_graduation(graduate_matcher, title, description, rejected):
    reason = graduate_matcher.eligibility_rejection_reason(
        JobPosting(title=title, description=description), domestic_ecommerce_only=True)
    assert bool(reason) is rejected


def test_cohort_unknown_requires_review(resume):
    reason = JobMatcher(resume).eligibility_rejection_reason(JobPosting(title='26届京东运营'))
    assert '无法确认' in reason


def test_domestic_filter_is_scoped(graduate_matcher):
    assert not graduate_matcher.eligibility_rejection_reason(
        JobPosting(title='Amazon运营'), domestic_ecommerce_only=False)


@pytest.mark.parametrize('title', ['【26届】国内电商管培生', '亚马逊跨境电商运营专员'])
def test_eligibility_cannot_be_overridden_by_score_or_ai(graduate_matcher, title):
    app = AutoBossApp.__new__(AutoBossApp)
    app.config = {'filter': {'domestic_ecommerce_only': True}}
    app.work_tab = None
    job = JobPosting(title=title, description='负责淘宝和京东店铺运营、商品上架和数据分析。' * 3)
    searcher = SimpleNamespace(city='深圳', fetch_job_detail=Mock())
    checker = Mock()
    graduate_matcher.match = Mock(side_effect=AssertionError('不应对资格不符岗位评分'))
    decision = app._evaluate_job(job, '电商运营', searcher, checker, [graduate_matcher])
    assert decision[0] == '资格不符'
    checker.check.assert_not_called()
    checker.match_job.assert_not_called()
