import logging
from unittest.mock import Mock

import pytest
import requests

from company_risk import CompanyRiskChecker


@pytest.fixture
def checker(config, monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    config['risk_check'] = {'mode': 'api', 'api': {
        'provider': 'deepseek', 'token': 'test-only-secret', 'timeout': 20}}
    instance = CompanyRiskChecker(config)
    instance._log_deepseek = Mock()
    return instance


@pytest.mark.parametrize('error_type', [requests.ReadTimeout, requests.ConnectionError])
def test_request_error_is_preserved_during_cooldown(checker, caplog, error_type):
    checker._session.post = Mock(side_effect=error_type('original problem test-only-secret'))
    with caplog.at_level(logging.ERROR):
        assert checker.match_job('虚构电商简历 ' * 30, {'title': '京东运营'}) is None
    assert checker._session.post.call_count == 1
    assert error_type.__name__ in caplog.text
    assert 'original problem' in caplog.text
    assert 'test-only-secret' not in caplog.text
    assert 'AI 暂不可用' not in caplog.text
    assert not checker.ai_available
    assert checker.match_job('虚构电商简历 ' * 30, {}) is None
    assert checker._session.post.call_count == 1


def test_auth_failure_keeps_original_http_status(checker, caplog):
    response = Mock(status_code=401)
    response.raise_for_status.side_effect = requests.HTTPError('401 unauthorized')
    checker._session.post = Mock(return_value=response)
    with caplog.at_level(logging.ERROR):
        assert checker.match_job('虚构电商简历 ' * 30, {}) is None
    assert 'HTTPError' in caplog.text and '401 unauthorized' in caplog.text
    assert 'AI 暂不可用' not in caplog.text
    assert not checker.api_cfg['token']


def test_matching_recovers_after_cooldown(checker):
    response = Mock(status_code=200)
    response.json.return_value = {'choices': [{'message': {'content':
        '{"fit":true,"score":88,"reasons":["平台经验吻合"],"missing":[]}'}}]}
    checker._session.post = Mock(side_effect=[requests.ReadTimeout('temporary timeout'), response])
    assert checker.match_job('虚构电商简历 ' * 30, {}) is None
    checker._ai_disabled_until = 0
    result = checker.match_job('虚构电商简历 ' * 30, {})
    assert result['score'] == 88 and result['fit'] is True
    assert checker.ai_available
    assert checker._session.post.call_count == 2


def test_pipeline_labels_ai_and_fallback_sources(config):
    from main import AutoBossApp
    from job_search import JobSearcher, JobPosting
    from job_matcher import JobMatcher
    from resume_parser import ResumeData
    from company_risk import RiskResult, RiskLevel
    app = AutoBossApp.__new__(AutoBossApp)
    app.config, app.work_tab = config, None
    config['filter']['ai_fit_check'] = True
    risk = Mock(ai_available=True)
    risk.check.return_value = RiskResult(level=RiskLevel.SAFE)
    searcher = JobSearcher(None, config)
    candidate = ResumeData(raw_text='Python RAG 开发经验 ' * 20, skills=['Python', 'RAG'])
    job = JobPosting(title='RAG开发工程师', description='负责Python RAG 检索增强系统开发。' * 10)
    risk.match_job.return_value = {'fit': True, 'score': 88, 'reasons': ['经验匹配']}
    ai = app._evaluate_job(job, 'RAG开发工程师', searcher, risk, [JobMatcher(candidate)])
    assert ai[4] == 88 and ai[1].startswith('AI匹配')
    risk.match_job.return_value = None
    fallback = app._evaluate_job(job, 'RAG开发工程师', searcher, risk, [JobMatcher(candidate)])
    assert 'AI调用失败，已回退' in fallback[1]


def test_api_direct_mode_does_not_modify_global_proxy(config, monkeypatch):
    import os
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:7897')
    before = dict(os.environ)
    config['risk_check'] = {'mode': 'api', 'api': {'proxy_mode': 'direct'}}
    checker = CompanyRiskChecker(config)
    assert checker._session.trust_env is False
    assert dict(os.environ) == before
    assert config['browser'] == {}


def test_api_system_proxy_remains_default(config):
    assert CompanyRiskChecker(config)._session.trust_env is True


def test_direct_api_ignores_an_unreachable_environment_proxy(config, monkeypatch):
    from requests.adapters import BaseAdapter
    from requests import Response
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:7897')
    config['risk_check'] = {'mode': 'api', 'api': {
        'provider': 'deepseek', 'token': 'test-only-secret', 'proxy_mode': 'direct'}}
    checker = CompanyRiskChecker(config)
    seen = {}
    class Adapter(BaseAdapter):
        def send(self, request, **kwargs):
            seen['proxies'] = kwargs.get('proxies')
            response = Response()
            response.status_code = 200
            response._content = b'{}'
            return response
        def close(self):
            pass
    checker._session.mount('https://', Adapter())
    checker._post_ai('', json={'messages': []})
    assert not seen['proxies']


def test_invalid_api_proxy_mode_rejected_before_login(config):
    from utils import validate_config
    config['risk_check'] = {'api': {'proxy_mode': 'invalid'}}
    with pytest.raises(ValueError, match='proxy_mode'):
        validate_config(config)


def test_custom_proxy_overrides_bad_environment_proxy(config, monkeypatch):
    from requests.adapters import BaseAdapter
    from requests import Response
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:7897')
    config['risk_check'] = {'mode': 'api', 'api': {
        'token': 'test-only', 'proxy_mode': 'custom', 'proxy_url': 'http://127.0.0.1:7890'}}
    checker = CompanyRiskChecker(config)
    seen = {}
    class Adapter(BaseAdapter):
        def send(self, request, **kwargs):
            seen.update(kwargs['proxies'])
            response = Response()
            response.status_code = 200
            response._content = b'{}'
            return response
        def close(self):
            pass
    checker._session.mount('https://', Adapter())
    checker._post_ai('', json={'messages': []})
    assert checker._session.trust_env is False
    assert seen['https'] == 'http://127.0.0.1:7890'


@pytest.mark.parametrize('value', ['', '127.0.0.1:7890', 'file:///tmp/proxy'])
def test_custom_proxy_requires_http_url(config, value):
    from utils import validate_config
    config['risk_check'] = {'api': {'proxy_mode': 'custom', 'proxy_url': value}}
    with pytest.raises(ValueError, match='proxy_url'):
        validate_config(config)


def test_configured_model_and_thinking_are_sent(checker):
    checker.api_cfg.update(model='deepseek-flash', thinking='disabled')
    checker._session.post = Mock(return_value=Mock(status_code=200))
    checker._post_ai('', json={'messages': []})
    body = checker._session.post.call_args.kwargs['json']
    assert body['model'] == 'deepseek-flash'
    assert body['thinking'] == {'type': 'disabled'}
