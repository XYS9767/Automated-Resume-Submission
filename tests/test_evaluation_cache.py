from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import Mock

from evaluation_cache import EvaluationCache
from job_search import JobPosting
from main import AutoBossApp


def job():
    return JobPosting(url='https://www.zhipin.com/job_detail/example.html',
                      title='天猫运营', company='示例', salary='6-9K', salary_min=6000, salary_max=9000,
                      location='深圳', description='负责天猫店铺的日常运营、商品上架、活动报名、主图优化和经营数据分析。')


def test_details_reused_across_keywords_and_restart(config):
    cache = EvaluationCache(config)
    first = job()
    cache.save_detail(first)
    another = job()
    another.description = ''
    another.url += '?lid=another_keyword'
    assert EvaluationCache(config).restore_detail(another)
    assert another.description == first.description


def test_detail_expiry_and_changed_listing(config):
    clock = NS(now=100)
    cache = EvaluationCache(config, clock=lambda: clock.now)
    original = job()
    cache.save_detail(original)
    changed = job()
    changed.salary = '5-6K'
    assert not cache.restore_detail(changed)
    clock.now += 3601
    assert not cache.restore_detail(original)


def test_rejections_persist_but_are_scoped_and_invalidated(config):
    resumes = [NS(raw_text='我的电商简历')]
    cache = EvaluationCache(config, resumes)
    j = job()
    cache.save_rejection(j, '京东运营', '深圳', '6-9K', '方向不符', '不匹配')
    assert EvaluationCache(config, resumes).rejection(j, '京东运营', '深圳', '6-9K')
    assert not cache.rejection(j, '天猫运营', '深圳', '6-9K')
    assert not cache.rejection(j, '京东运营', '广州', '6-9K')
    assert not cache.rejection(j, '京东运营', '深圳', '8-12K')
    changed = deepcopy(config)
    changed['filter']['min_match_score'] = 80
    assert not EvaluationCache(changed, resumes).rejection(j, '京东运营', '深圳', '6-9K')
    assert not EvaluationCache(config, [NS(raw_text='新简历')]).rejection(j, '京东运营', '深圳', '6-9K')


def test_positive_or_uncertain_decisions_are_never_cached(config):
    cache = EvaluationCache(config)
    for result in ('预览通过', '投递成功', '需重新登录', '详情未核实', '沟通结果待核实'):
        cache.save_rejection(job(), '天猫运营', '深圳', '6-9K', result, '')
        assert not cache.rejection(job(), '天猫运营', '深圳', '6-9K')


def test_underpaid_job_rejected_before_detail_access(config):
    app = AutoBossApp.__new__(AutoBossApp)
    app.config = config
    config['search']['salary'] = '6-9K'
    j = job()
    j.salary, j.salary_min = '5-10K', 5000
    j.description = ''
    searcher = NS(city='深圳', fetch_job_detail=Mock())
    result = app._evaluate_job(j, '天猫运营', searcher, None, [])
    assert result[0] == '条件不符'
    assert '低于' in result[1]
    searcher.fetch_job_detail.assert_not_called()
