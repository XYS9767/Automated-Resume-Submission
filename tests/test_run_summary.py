import json
import logging
from copy import deepcopy
from datetime import date, timedelta

import pytest

from main import AutoBossApp
from submitter import JobSubmitter, SubmitResult


def app_for(config):
    app = AutoBossApp.__new__(AutoBossApp)
    app.config = config
    app.logger = logging.getLogger('summary-test')
    app.browser = None
    return app


def write_count(path, count, day=None):
    from pathlib import Path
    Path(path).write_text(json.dumps({'date': day or date.today().isoformat(), 'count': count}), encoding='utf-8')


def test_summary_uses_this_run_counts_and_separate_daily_total(config, caplog):
    write_count(config['submit']['counter_file'], 47)
    stats = {SubmitResult.SUCCESS: 5, SubmitResult.FAILED: 1, 'job_error': 2,
             SubmitResult.UNCONFIRMED: 3, '条件不符': 100, 'match_low': 20,
             SubmitResult.ALREADY_APPLIED: 8}
    with caplog.at_level(logging.INFO):
        summary = app_for(config)._print_communication_summary(stats)
    assert summary == {'success': 5, 'failed': 3, 'unconfirmed': 3, 'today_success': 47}
    assert '本次沟通成功：5 个' in caplog.text
    assert '本次沟通失败：3 个' in caplog.text
    assert '本次结果待核实：3 个' in caplog.text
    assert f'当天累计沟通成功（{date.today()}）：47 个' in caplog.text


def test_interrupted_click_is_unconfirmed_but_old_pending_is_not(config):
    sender = JobSubmitter(None, config)
    sender._state['jobs']['old'] = {'status': 'pending'}
    initial = deepcopy(sender._state['jobs'])
    sender._state['jobs']['new'] = {'status': 'pending'}
    summary = app_for(config)._print_communication_summary({}, sender, initial)
    assert summary['unconfirmed'] == 1
    assert summary['success'] == 0


def test_confirmed_ledger_result_survives_interruption_before_stats_update(config):
    sender = JobSubmitter(None, config)
    sender._state['jobs']['new'] = {'status': SubmitResult.SUCCESS}
    sender._state['count'] = 1
    summary = app_for(config)._print_communication_summary({}, sender, {})
    assert summary['success'] == summary['today_success'] == 1


def test_unconfirmed_ledger_and_stats_are_not_counted_twice(config):
    sender = JobSubmitter(None, config)
    sender._state['jobs']['new'] = {'status': SubmitResult.UNCONFIRMED}
    summary = app_for(config)._print_communication_summary({SubmitResult.UNCONFIRMED: 1}, sender, {})
    assert summary['unconfirmed'] == 1


def test_previous_day_counter_does_not_appear_as_today(config):
    write_count(config['submit']['counter_file'], 103, str(date.today() - timedelta(days=1)))
    summary = app_for(config)._print_communication_summary({})
    assert summary['today_success'] == 0


def test_preview_is_labeled_and_does_not_write_counter(config, caplog):
    from pathlib import Path
    path = Path(config['submit']['counter_file'])
    write_count(path, 47)
    before = path.read_bytes()
    with caplog.at_level(logging.INFO):
        result = app_for(config)._print_communication_summary({'preview': 8}, search_only=True)
    assert '预览结束（未发起沟通）' in caplog.text
    assert result['success'] == result['failed'] == result['unconfirmed'] == 0
    assert path.read_bytes() == before
    assert not Path(config['submit']['state_file']).exists()


def test_unreadable_counter_is_not_reported_as_zero(config, caplog):
    from pathlib import Path
    Path(config['submit']['counter_file']).write_text('{bad json', encoding='utf-8')
    with caplog.at_level(logging.INFO):
        summary = app_for(config)._print_communication_summary({})
    assert summary['today_success'] is None
    assert '无法读取' in caplog.text


@pytest.mark.parametrize('reason', ['stop', 'interrupt', 'error'])
def test_run_exit_always_prints_summary_without_login(config, monkeypatch, caplog, reason):
    app = app_for(config)
    write_count(config['submit']['counter_file'], 47)
    def stop_check():
        if reason == 'interrupt':
            raise KeyboardInterrupt()
        if reason == 'error':
            raise RuntimeError('early error')
        return True
    app._stop_requested = stop_check
    monkeypatch.setattr('main.BossLogin', lambda *args: pytest.fail('不应访问浏览器'))
    with caplog.at_level(logging.INFO):
        if reason == 'error':
            with pytest.raises(RuntimeError, match='early error'):
                app.run()
        else:
            assert app.run() == (130 if reason == 'interrupt' else 0)
    assert '本次沟通成功：0 个' in caplog.text
    assert '当天累计沟通成功' in caplog.text
    assert '47 个' in caplog.text
