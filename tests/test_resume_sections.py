import pytest

from resume_parser import ResumeParser


@pytest.mark.parametrize('period', ['2026.3-5', '2026.03–05', '2026/3~5', '2026年3月到5月'])
def test_short_month_range_is_a_separate_employer(period):
    text = f'''工作经历
2026.05-至今 示例甲科技有限公司 运维开发工程师
维护云平台和监控服务。
{period} 示例乙信息技术有限公司 系统运维工程师
维护容器集群，编写项目报告。
2025.6-2025.12 示例丙科技有限公司 运维开发工程师
维护数据库与日志平台。
2023.6-2025.4 示例丁科技有限公司 运维工程师
部署项目，响应故障。
项目经历
2024.1-2024.3 示例项目有限公司 项目工程师
项目成果：搭建发布审批平台。
自我评价
具备良好的团队协作能力。
教育经历
2020-2024 示例工程学院 计算机科学与技术
'''
    result = ResumeParser('fixture.txt').parse_text(text)
    assert len(result.work_experiences) == 4
    assert [x.company_or_school for x in result.work_experiences] == [
        '示例甲科技有限公司', '示例乙信息技术有限公司', '示例丙科技有限公司', '示例丁科技有限公司']
    second = result.work_experiences[1]
    assert (second.start_date, second.end_date, second.title_or_major) == ('2026.3', '2026.5', '系统运维工程师')
    assert '示例乙' not in result.work_experiences[0].description
    assert '编写项目报告' in second.description
    assert '项目成果' not in result.work_experiences[-1].description
    assert '团队协作' not in result.work_experiences[-1].description
    assert len(result.education) == 1


@pytest.mark.parametrize('text', ['2026-03-05', '2026.3-50', '2026.12-2', '2026.3-2026.5', '2020-2024'])
def test_abbreviation_does_not_reinterpret_ambiguous_or_complete_dates(text):
    assert ResumeParser._expand_short_month_range(text) == text


def test_project_only_resume_does_not_invent_employment():
    r = ResumeParser('fixture.txt').parse_text('项目经历\n2024.1-2024.3 示例科技有限公司\n开发演示系统。')
    assert r.work_experiences == []


def test_skill_section_ends_employment_description():
    r = ResumeParser('fixture.txt').parse_text(
        '工作经历\n2023.6-2025.4 示例科技有限公司 运维工程师\n维护业务系统。\n专业技能\n熟悉 Python Docker。')
    assert len(r.work_experiences) == 1
    assert r.work_experiences[0].description == '维护业务系统。'
    assert 'Python' in r.skills
