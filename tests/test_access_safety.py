import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from access_safety import AccessGuard, AccessStopped


class Clock:
    now = 1804500000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def guard(config, clock):
    return AccessGuard(config, clock.time, clock.sleep)


def page(body='', url='https://www.zhipin.com/web/geek/jobs'):
    return NS(url=url, run_js=Mock(return_value=body), get=Mock())


def test_shared_interval_and_daily_budget_survive_restart(config):
    config['safety'].update(min_interval_seconds=15, max_actions_per_day=3)
    clock = Clock()
    first = guard(config, clock)
    first.before('search')
    start = clock.now
    first.before('search_page')
    assert clock.now == start + 15
    restarted = guard(config, clock)
    restarted.before('detail')
    assert clock.now == start + 30
    with pytest.raises(AccessStopped, match='上限'):
        guard(config, clock).before('contact')
    assert json.loads(first.path.read_text(encoding='utf-8'))['count'] == 3


def test_run_budget_counts_failed_actions(config):
    config['safety']['max_actions_per_run'] = 1
    g = guard(config, Clock())
    g.before('detail_that_failed')
    with pytest.raises(AccessStopped, match='上限'):
        g.before('retry')


def test_new_day_resets_budget_but_not_restriction(config):
    config['safety']['max_actions_per_day'] = 1
    clock = Clock()
    g = guard(config, clock)
    g.before('search')
    clock.now += 86400
    g.before('detail')
    with pytest.raises(AccessStopped):
        g.block('账号受限')
    clock.now += 86400
    with pytest.raises(AccessStopped, match='账号受限'):
        guard(config, clock).before('restart')


def test_pause_and_corrupt_state_fail_closed(config):
    config['safety']['paused'] = True
    g = guard(config, Clock())
    with pytest.raises(AccessStopped, match='暂停'):
        g.before('login')
    config['safety']['paused'] = False
    g.path.write_text('{broken', encoding='utf-8')
    with pytest.raises(AccessStopped, match='损坏'):
        g.before('login')


def test_stop_interrupts_interval_without_another_action(config):
    config['safety']['min_interval_seconds'] = 15
    clock = Clock()
    g = guard(config, clock)
    g.before('search')
    def stopped(seconds):
        config['safety']['paused'] = True
        clock.sleep(seconds)
    g.sleep = stopped
    with pytest.raises(AccessStopped):
        g.before('detail')
    assert json.loads(g.path.read_text(encoding='utf-8'))['count'] == 1


@pytest.mark.parametrize('body', [
    '访问受限 您的账户存在异常行为，已暂时被限制访问！将于 2026-11-07 10:38 恢复正常',
])
def test_restriction_is_durable_and_never_refreshes(config, body):
    g = guard(config, Clock())
    tab = page(body)
    with pytest.raises(AccessStopped):
        g.before('search_page', tab)
    tab.get.assert_not_called()
    with pytest.raises(AccessStopped):
        guard(config, Clock()).before('restart')


def test_restriction_during_wait_prevents_action(config):
    config['safety']['min_interval_seconds'] = 15
    g = guard(config, Clock())
    g.before('search')
    tab = page()
    tab.run_js.side_effect = ['', '访问受限']
    with pytest.raises(AccessStopped):
        g.before('detail', tab)
    assert json.loads(g.path.read_text(encoding='utf-8'))['count'] == 1


@pytest.mark.parametrize('warning', ['请完成安全验证', '操作过于频繁', '拖动滑块'])
def test_verification_stops_current_run_without_locking_next_manual_start(config, warning):
    g = guard(config, Clock())
    g.before('search')
    tab = page({'body': warning, 'notices': warning, 'content': False, 'challenge': True})
    with pytest.raises(AccessStopped):
        g.inspect(tab)
    with pytest.raises(AccessStopped):
        g.before('retry')
    assert not json.loads(g.path.read_text(encoding='utf-8')).get('blocked_reason')
    fresh = guard(config, Clock())
    fresh.check()
    fresh.inspect(page({'body': '正常岗位列表', 'content': True, 'notices': '', 'challenge': False}))
    tab.get.assert_not_called()


@pytest.mark.parametrize('text', ['负责滑动验证组件开发', '帮助中心：操作频繁怎么办',
                               '遇到访问受限请联系管理员', '请完成安全验证后使用测试系统'])
def test_normal_job_text_does_not_trigger_security(config, text):
    from job_search import is_boss_security_page
    tab = page({'body': text, 'content': True, 'notices': '', 'challenge': False})
    guard(config, Clock()).inspect(tab)
    assert not is_boss_security_page(tab, strict=True)


def test_visible_challenge_on_normal_page_still_stops(config):
    tab = page({'body': '正常列表', 'content': True, 'notices': '', 'challenge': True})
    g = guard(config, Clock())
    with pytest.raises(AccessStopped):
        g.inspect(tab)
    assert g.session_stop
    assert not g.path.exists()


def test_explicit_account_restriction_dialog_still_persists(config):
    tab = page({'body': '岗位列表', 'content': True, 'challenge': False,
                'notices': '您的账户存在异常行为，已暂时被限制访问！将于 2026-11-07 10:38 恢复正常'})
    g = guard(config, Clock())
    with pytest.raises(AccessStopped):
        g.inspect(tab)
    state = json.loads(g.path.read_text(encoding='utf-8'))
    assert '2026-11-07 10:38' in state['blocked_reason']
    with pytest.raises(AccessStopped):
        guard(config, Clock()).check()


def test_consecutive_detail_failures_persist_and_success_resets(config):
    g = guard(config, Clock())
    g.detail_failed()
    g.detail_ok()
    g.detail_failed()
    with pytest.raises(AccessStopped, match='连续'):
        guard(config, Clock()).detail_failed()
    with pytest.raises(AccessStopped):
        guard(config, Clock()).before('restart')


def test_restricted_detail_does_not_navigate_or_fall_back(config):
    from job_search import JobSearcher, JobPosting
    s = JobSearcher(None, config)
    tab = page('访问受限')
    s.ensure_detail_tab = Mock(return_value=tab)
    j = JobPosting(url='https://www.zhipin.com/job_detail/a.html', encrypt_job_id='a')
    with pytest.raises(AccessStopped):
        s.fetch_job_detail(tab, j)
    tab.get.assert_not_called()


def test_restricted_submission_never_clicks(config):
    from submitter import JobSubmitter
    from job_search import JobPosting
    tab = page('访问受限', 'https://www.zhipin.com/job_detail/a.html')
    tab.actions = Mock()
    sender = JobSubmitter(None, config, get_detail_tab=lambda: tab)
    with pytest.raises(AccessStopped):
        sender.submit(JobPosting(url=tab.url, encrypt_job_id='a'))
    tab.get.assert_not_called()
    tab.actions.click.assert_not_called()
    assert sender.get_today_count() == 0


def test_partial_search_survives_budget_stop(config, monkeypatch):
    from job_search import JobSearcher, JobPosting
    config['safety']['max_actions_per_run'] = 1
    s = JobSearcher(None, config)
    tab = page()
    tab.listen = NS(start=Mock(), stop=Mock())
    s.capture_has_more = True
    j = JobPosting(url='https://www.zhipin.com/job_detail/a.html', encrypt_job_id='a')
    def captured(*args, **kwargs):
        s.capture_status, s.capture_has_more = 'ok', True
        return [j]
    monkeypatch.setattr(s, 'wait_capture', captured)
    with pytest.raises(AccessStopped, match='上限'):
        s.collect_search_results(tab, '电商运营', lambda *a: True)
    assert s.partial_jobs == [j]
    tab.listen.stop.assert_called_once()


def test_paused_run_never_constructs_login_or_api_client(config, monkeypatch):
    import logging
    from main import AutoBossApp
    config['safety']['paused'] = True
    app = AutoBossApp.__new__(AutoBossApp)
    app.config, app.logger, app.browser = config, logging.getLogger('offline-test'), None
    app._stop_requested = lambda: False
    login, api = Mock(), Mock()
    monkeypatch.setattr('main.BossLogin', login)
    monkeypatch.setattr('main.CompanyRiskChecker', api)
    assert app.run() == 2
    login.assert_not_called()
    api.assert_not_called()


def test_exhausted_budget_stops_before_login(config):
    config['safety']['max_actions_per_day'] = 1
    g = guard(config, Clock())
    g.before('search')
    with pytest.raises(AccessStopped, match='不连接浏览器'):
        guard(config, Clock()).check()


def test_empty_details_cannot_reset_failure_streak(config):
    from job_search import JobSearcher, JobPosting
    s = JobSearcher(None, config)
    tab = page('', 'https://www.zhipin.com/job_detail/a.html')
    tab.ele = Mock(return_value=None)
    s.ensure_detail_tab = Mock(return_value=tab)
    j = JobPosting(url=tab.url, encrypt_job_id='a')
    assert s.fetch_job_detail(tab, j) == ''
    assert j.detail_unverified
    with pytest.raises(AccessStopped, match='连续'):
        s.fetch_job_detail(tab, j)
    tab.get.assert_not_called()


def test_app_saves_partial_queue_and_exits_without_submission(config, monkeypatch):
    import logging
    from main import AutoBossApp
    from job_search import JobPosting
    from resume_parser import ResumeData
    from pathlib import Path
    app = AutoBossApp.__new__(AutoBossApp)
    app.config, app.logger = config, logging.getLogger('offline-run-test')
    app.browser, app.work_tab = NS(), page()
    app._stop_requested = lambda: False
    app._resolve_resume_paths = lambda *a: ['fake-resume']
    app._validate_resumes = lambda: None
    app._build_combined_resume_text = lambda: '离线简历'
    resume = ResumeData(raw_text='离线简历')
    monkeypatch.setattr('main.ResumeParser', lambda *a: NS(parse=lambda: resume))
    monkeypatch.setattr('main.CompanyRiskChecker', Mock())
    j = JobPosting(url='https://www.zhipin.com/job_detail/a.html', encrypt_job_id='a')
    searcher = NS(city='杭州', city_code='101210100', partial_jobs=[j],
                  search_with_recovery=Mock(side_effect=AccessStopped('访问预算已用完')),
                  ensure_detail_tab=Mock(), close_detail_tab=Mock())
    monkeypatch.setattr('main.JobSearcher', Mock(return_value=searcher))
    send = Mock()
    monkeypatch.setattr('submitter.JobSubmitter.submit', send)
    assert app.run() == 2
    saved = json.loads(Path(config['output']['queue_file']).read_text(encoding='utf-8'))
    assert 'job:a' in saved['keywords']['RAG开发工程师']
    send.assert_not_called()


@pytest.mark.parametrize('field,value', [('max_actions_per_day', 0), ('max_detail_failures', -1),
                                        ('min_interval_seconds', float('nan')), ('paused', 'false')])
def test_invalid_safety_configuration_rejected(config, field, value):
    from utils import validate_config
    config['safety'][field] = value
    with pytest.raises(ValueError):
        validate_config(config)
