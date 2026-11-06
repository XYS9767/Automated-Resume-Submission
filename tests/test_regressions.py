import json
from datetime import date
from types import SimpleNamespace as NS
from unittest.mock import Mock
import pytest

from job_search import JobSearcher, JobPosting, job_url_key, extract_encrypt_job_id
from submitter import JobSubmitter, SubmitResult
from relevance import keyword_matches, contains_term
from resume_parser import ResumeData, ResumeParser, Experience
from job_matcher import JobMatcher
from main import AutoBossApp
from company_risk import CompanyRiskChecker
from recorder import ApplyRecorder
from run_queue import RunQueue
from utils import safe_save_json, validate_config
from access_safety import AccessStopped




def job(eid="a", **kw):
    return JobPosting(url=f"https://www.zhipin.com/job_detail/{eid}.html?lid=one",
                      encrypt_job_id=eid, title=kw.pop("title", "RAG开发工程师"),
                      company=kw.pop("company", "示例科技"), **kw)


@pytest.mark.parametrize("url", [
    "https://www.zhipin.com/job_detail/abc.html?lid=1",
    "https://www.zhipin.com/job_detail/abc.html?lid=2",
    "https://www.zhipin.com/web/geek/job_detail?jobId=abc&lid=3",
])
def test_tracking_and_spa_identity(url):
    assert job_url_key(url) == "job:abc"
    assert job_url_key(url) != job_url_key("https://www.zhipin.com/web/geek/job_detail?jobId=other")


@pytest.mark.parametrize("title,kw,desc,expected", [
    ("Java开发实习生", "aigc实习", "", False),
    ("JavaScript工程师", "Java开发", "", False),
    ("企业智能体研发实习生", "Agent开发实习生", "", True),
    ("大模型应用工程师", "LLM工程实习生", "", True),
    ("后端工程师", "RAG开发工程师", "负责RAG检索增强、向量数据库开发", True),
    ("普通销售实习生", "RAG开发工程师", "负责客户开发", False),
    ("C++工程师", "C++开发", "", True),
    ("计算机视觉", "C开发", "", False),
    ("软件工程师", "Python开发工程师", "系统采用 Python/FastAPI 开发", True),
])
def test_direction_relevance(title, kw, desc, expected):
    assert keyword_matches(title, kw, desc) is expected


def test_parser_latin_boundaries_and_new_ai_skills(tmp_path):
    p = tmp_path / "resume.txt"
    p.write_text("本人熟悉 JavaScript、RAG、LangGraph、Python 和 Docker。", encoding="utf-8")
    data = ResumeParser(str(p)).parse()
    assert {"RAG", "LangGraph", "Python", "JavaScript"} <= set(data.skills)
    assert "Java" not in data.skills and "C" not in data.skills and "Go" not in data.skills


def test_candidate_specific_score():
    r = ResumeData(skills=["Python", "RAG"], raw_text="Python RAG 大模型研发，本科", education=[Experience(title_or_major="本科")])
    m = JobMatcher(r)
    assert m._calc_title_bonus("嵌入式STM32固件工程师") == 0
    assert m.match(job(skills_required=["Python", "RAG"])).total_score <= 100


@pytest.mark.parametrize("exp,limit,expected", [
    ("3-5年", "1-3年", False), ("5-10年", "1-3年", True),
    ("经验不限", "应届生", False), ("1年以内", "应届生", True),
    ("10年以上", "", False)
])
def test_minimum_experience(exp, limit, expected):
    assert AutoBossApp._experience_too_high(job(experience=exp), limit) is expected


def test_location_uses_workplace_not_company_headquarters():
    app = AutoBossApp.__new__(AutoBossApp)
    assert not app._job_wrong_city(job(company="北京示例科技", location="杭州 西湖区"), "杭州")
    assert app._job_wrong_city(job(location="北京 海淀区"), "杭州")
    assert not app._education_too_high("研发工程师", "本科")


def packet(query="RAG开发工程师", city="101210100", rows=None, page=1, more=False, code=0):
    from urllib.parse import urlencode
    return NS(url="https://www.zhipin.com/wapi/zpgeek/search/joblist.json?" + urlencode(
        {"query": query, "city": city, "page": page}), request=NS(postData=None),
        response=NS(body={"code": code, "zpData": {"jobList": rows or [], "hasMore": more}}))


def test_listener_rejects_stale_queries_wrong_cities_and_recommendations(config):
    s = JobSearcher(None, config)
    packets = [
        packet(query="Java", rows=[{"jobName": "Java", "encryptJobId": "bad"}]),
        packet(city="101010100", rows=[{"jobName": "RAG", "encryptJobId": "wrongcity"}]),
        packet(query="", rows=[{"jobName": "RAG", "encryptJobId": "recommend"}]),
        packet(rows=[{"jobName": "RAG", "encryptJobId": "good", "bossOnline": "false", "lid": "track"}]),
    ]
    listener = NS(wait=lambda **kw: packets.pop(0) if packets else False)
    jobs = s.wait_capture(NS(listen=listener), "RAG开发工程师", timeout=.02)
    assert [j.encrypt_job_id for j in jobs] == ["good"]
    assert jobs[0].boss_online is False
    assert "lid=track" in jobs[0].url


def test_second_page_is_collected_and_listener_starts_before_navigation(config, monkeypatch):
    s = JobSearcher(None, config)
    events = []
    tab = NS(url="about:blank", run_js=Mock(return_value=""), listen=NS(start=lambda *a: events.append("listen"), stop=lambda: events.append("stop")))
    batches = iter([[job("a")], [job("a"), job("b")]])
    def wait(*args, **kw):
        batch = next(batches)
        s.capture_has_more = len(batch) == 1
        return batch
    monkeypatch.setattr("job_search.is_boss_security_page", lambda *a, **k: False)
    monkeypatch.setattr(s, "wait_capture", wait)
    monkeypatch.setattr(s, "advance_page", lambda tab: events.append("next") or True)
    out = s.collect_search_results(tab, "RAG", lambda *a: events.append("navigate") or True)
    assert [j.encrypt_job_id for j in out] == ["a", "b"]
    assert events == ["listen", "navigate", "next", "stop"]


@pytest.mark.parametrize("data,expected", [
    ({"code": 0}, SubmitResult.SUCCESS),
    ({"code": 37}, SubmitResult.SECURITY_REQUIRED),
    ({"code": 7}, SubmitResult.LOGIN_REQUIRED),
    ({"code": 1, "message": "今日沟通次数已达上限"}, SubmitResult.DAILY_LIMIT),
    ({"code": 1, "message": "已经沟通过"}, SubmitResult.ALREADY_APPLIED),
    ({"code": 999, "message": "failed"}, SubmitResult.FAILED)
])
def test_contact_response_outcomes(data, expected):
    assert JobSubmitter._response_result(data) == expected


def test_same_title_wrong_id_never_reuses_detail():
    tab = NS(url="https://www.zhipin.com/job_detail/other.html")
    assert not JobSubmitter._tab_has_job(tab, "wanted", "RAG开发工程师")


def mocked_submitter(config, monkeypatch, result):
    sub = JobSubmitter(None, config)
    listener = NS(start=Mock(), stop=Mock())
    tab = NS(url=job().url, run_js=Mock(return_value=""), listen=listener)
    button = NS(click=Mock())
    monkeypatch.setattr(sub, "_get_detail_tab", lambda: tab)
    monkeypatch.setattr(sub, "_open_job_detail", lambda *a: True)
    monkeypatch.setattr(sub, "_page_state", lambda *a: "")
    monkeypatch.setattr(sub, "_find_contact", lambda *a: button)
    monkeypatch.setattr(sub, "_wait_confirmation", lambda *a: result)
    return sub, button


def test_click_without_ack_is_not_counted_and_restart_wont_resend(config, monkeypatch):
    sub, button = mocked_submitter(config, monkeypatch, SubmitResult.UNCONFIRMED)
    assert sub.submit(job()) == SubmitResult.UNCONFIRMED
    assert sub.get_today_count() == 0
    resumed = JobSubmitter(None, config)
    assert resumed.submit(job()) == SubmitResult.UNCONFIRMED
    assert button.click.call_count == 1


def test_confirmed_contact_count_survives_restart_and_tracking_changes(config, monkeypatch):
    sub, button = mocked_submitter(config, monkeypatch, SubmitResult.SUCCESS)
    assert sub.submit(job()) == SubmitResult.SUCCESS
    resumed = JobSubmitter(None, config)
    j = job(); j.url = j.url.replace("one", "two")
    assert resumed.get_today_count() == 1
    assert resumed.submit(j) == SubmitResult.ALREADY_APPLIED
    assert button.click.call_count == 1


def test_limit_and_midnight(config):
    sub = JobSubmitter(None, config)
    sub._state["count"] = 3
    assert sub.submit(job()) == SubmitResult.DAILY_LIMIT
    sub._state["date"] = "2000-01-01"
    assert sub.get_today_count() == 0


def test_crash_pending_is_not_silently_retried(config):
    sub = JobSubmitter(None, config)
    sub._mark(job(), "pending")
    assert JobSubmitter(None, config).submit(job()) == SubmitResult.UNCONFIRMED


def test_invalid_api_key_makes_no_requests(config, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    config["risk_check"] = {"mode": "api", "api": {"provider": "deepseek", "token": "YOUR_DEEPSEEK_API_KEY"}}
    checker = CompanyRiskChecker(config)
    checker._session.post = Mock(side_effect=AssertionError("must not call"))
    assert not checker.ai_available
    assert checker.match_job("x" * 100, {}) is None
    checker.check("示例科技", job().to_dict())
    checker._session.post.assert_not_called()


def test_auth_error_disables_ai_for_remaining_jobs(config, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    config["risk_check"] = {"mode": "api", "api": {"provider": "deepseek", "token": "test-token"}}
    checker = CompanyRiskChecker(config)
    import requests
    response = Mock(status_code=401)
    response.raise_for_status.side_effect = requests.HTTPError("401")
    checker._session.post = Mock(return_value=response)
    assert checker.match_job("resume " * 50, job().to_dict()) is None
    assert not checker.ai_available
    assert checker.match_job("resume " * 50, job().to_dict()) is None
    assert checker._session.post.call_count == 1


def test_queue_and_history_restart(config):
    resumes = [ResumeData(raw_text="example")]
    q = RunQueue(config, resumes)
    q.add("RAG", [job("a"), job("b")])
    q.done("RAG", job("a"))
    assert [j.encrypt_job_id for j in RunQueue(config, resumes).get("RAG")] == ["b"]
    r = ApplyRecorder(config)
    r.record(job("a"), submit_result=SubmitResult.SUCCESS, reason="confirmed", score=87, resume_name="ai.pdf")
    loaded = ApplyRecorder(config)
    assert loaded.is_already_success(job("a").url.replace("one", "two"))
    assert loaded._records[0]["match_score"] == 87


def test_atomic_write_preserves_previous_file_on_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    safe_save_json({"count": 3}, str(path))
    monkeypatch.setattr("os.replace", Mock(side_effect=PermissionError("locked")))
    with pytest.raises(PermissionError):
        safe_save_json({"count": 0}, str(path))
    assert json.loads(path.read_text()) == {"count": 3}


def test_bad_config_reports_problem_before_browser(config):
    config["submit"]["interval"] = {"min": 20, "max": 1}
    with pytest.raises(ValueError, match="间隔"):
        validate_config(config)


def test_pipeline_rejects_direction_and_company_but_keeps_relevant_internship(config):
    app = AutoBossApp.__new__(AutoBossApp)
    app.config, app.work_tab = config, None
    resume = ResumeData(raw_text="Python RAG 大模型 " * 20, skills=["Python", "RAG"], parsed_from="ai.pdf",
                        education=[Experience(title_or_major="本科")])
    searcher = JobSearcher(None, config)
    searcher.fetch_job_detail = Mock(return_value="")
    checker = CompanyRiskChecker(config)
    matchers = [JobMatcher(resume)]
    good = job(description="RAG Python 大模型开发，负责检索增强生成研发工作。" * 4, skills_required=["RAG", "Python"], salary="200-300元/天")
    assert app._evaluate_job(good, "RAG开发工程师", searcher, checker, matchers)[0] is None
    wrong = job(title="Java实习生", skills_required=["Java"], description="负责Java后端业务" * 10)
    assert app._evaluate_job(wrong, "RAG开发工程师", searcher, checker, matchers)[0] == "方向不符"
    config["filter"]["exclude_companies"] = ["外包"]
    good.company = "示例外包服务"
    assert app._evaluate_job(good, "RAG", searcher, checker, matchers)[1] == "公司命中排除词"


def test_no_greeting_to_a_different_job(config):
    sub = JobSubmitter(None, config)
    assert '身份无法确认' in sub._send_greeting(NS(url=job('other').url), job('expected'))


def test_corrupt_history_does_not_silently_reset(config):
    from pathlib import Path
    Path(config['output']['record_file']).write_text('{broken', encoding='utf-8')
    with pytest.raises(ValueError, match='历史记录损坏'):
        ApplyRecorder(config)


def test_ai_false_string_is_not_truthy(config, monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    config['risk_check'] = {'mode': 'api', 'api': {'provider': 'deepseek', 'token': 'test-only'}}
    checker = CompanyRiskChecker(config)
    response = Mock()
    response.json.return_value = {'choices': [{'message': {'content': '{"score":80,"fit":"false","reasons":[]}'}}]}
    checker._post_ai = Mock(return_value=response)
    checker._log_deepseek = Mock()
    assert checker.match_job('example resume ' * 20, job().to_dict())['fit'] is False


def test_detailed_jd_overrides_misleading_tags():
    assert not keyword_matches('Java实习生', 'RAG开发工程师', '负责 Java 后端开发，熟悉 Spring 框架与 MySQL 业务系统。' * 3, ['RAG', 'Python'])

@pytest.mark.parametrize("period,start,end", [
    ("2021-2025", "2021", "2025"), ("2025.6-2026.9", "2025.6", "2026.9"),
    ("2020/01-至今", "2020/01", "至今"), ("2023-09 — 2024-12", "2023-09", "2024-12"),
    ("2021年9月-2025年6月", "2021年9月", "2025年6月"),
])
def test_real_pdf_date_formats(period, start, end):
    match = ResumeParser.DATE_RANGE_PATTERN.search(period)
    assert match and match.groups() == (start, end)


def test_pdf_organization_date_and_role_on_separate_lines(tmp_path):
    path = tmp_path / "resume.txt"
    path.write_text(
        "工作经历\n示例科技有限公司\n2025.6-2026.9\n岗位：大模型应用开发工程师\n"
        "负责 RAG 系统和向量数据库开发。\n"
        "教育经历\n示例理工学院\n2021-2025\n计算机科学与技术 | 本科 | 全日制统招\n",
        encoding="utf-8")
    result = ResumeParser(str(path)).parse()
    assert len(result.work_experiences) == 1
    assert len(result.education) == 1
    assert result.work_experiences[0].company_or_school == "示例科技有限公司"
    assert result.work_experiences[0].title_or_major == "大模型应用开发工程师"
    assert result.education[0].company_or_school == "示例理工学院"
    assert JobMatcher(result)._my_edu_level == 3
    assert JobMatcher(result)._my_exp_years == pytest.approx(16 / 12, abs=0.01)


def test_cookie_export_uses_installed_browser_signature(config, monkeypatch):
    from boss_login import BossLogin
    login = BossLogin(config)
    # Signature matches installed DrissionPage Chromium.cookies(all_info=False).
    exported = []
    class Browser:
        def cookies(self, all_info=False):
            assert all_info
            return [{"domain": ".zhipin.com", "name": "test", "value": "fixture-only"}]
    login.browser = Browser()
    monkeypatch.setattr(login, "_ensure_browser", lambda: None)
    monkeypatch.setattr(login, "_wait_for_user_login", lambda: None)
    monkeypatch.setattr("boss_login.safe_save_json", lambda data, path: exported.extend(data))
    assert login.login() is login.browser
    assert len(exported) == 1


def test_signed_in_homepage_does_not_mean_search_is_restored(monkeypatch):
    from job_search import wait_user_clear_security
    monkeypatch.setattr("job_search.is_boss_security_page", lambda *a, **k: False)
    monkeypatch.setattr("job_search.is_boss_logged_in_visually", lambda *a, **k: True)
    monkeypatch.setattr("job_search.time.sleep", lambda *a: None)
    assert not wait_user_clear_security(NS(), timeout=.01, ready=lambda tab: False)


@pytest.mark.parametrize("body,expected", [
    ({"code": 37}, "security"), ("", "unavailable"), ("<html>error</html>", "invalid_response"),
])
def test_search_post_form_and_unavailable_body_are_not_zero_jobs(config, body, expected):
    p = packet()
    p.url = "https://www.zhipin.com/wapi/zpgeek/search/joblist.json?_=123"
    p.request.postData = "query=RAG&city=101210100&page=1&pageSize=15"
    p.response.body = body
    packets = [p]
    tab = NS(listen=NS(wait=lambda **kw: packets.pop(0) if packets else False))
    s = JobSearcher(None, config)
    if expected == 'security':
        with pytest.raises(AccessStopped):
            s.wait_capture(tab, "RAG", timeout=.01)
        assert s.guard.session_stop
        assert not s.guard.path.exists()
        return
    assert s.wait_capture(tab, "RAG", timeout=.01) == []
    assert s.capture_status == expected
    assert s.capture_reason


def test_durable_network_capture_falls_back_for_older_browser(config):
    driver = NS(run=Mock(side_effect=[{"error": "method unsupported"}, {}]))
    tab = NS(listen=NS(start=Mock(), _driver=driver))
    JobSearcher(None, config).start_capture(tab)
    assert driver.run.call_args_list[0].args == ("Network.configureDurableMessages",)
    assert driver.run.call_args_list[1].kwargs["enableDurableMessages"] is True


def test_security_redirect_still_consumes_the_actual_search_error(config, monkeypatch):
    s = JobSearcher(None, config)
    p = packet(query="RAG", code=37)
    packets = [p]
    tab = NS(url="about:blank", run_js=Mock(return_value=""), listen=NS(start=Mock(), stop=Mock(), wait=lambda **kw: packets.pop(0) if packets else False))
    monkeypatch.setattr("job_search.is_boss_security_page", lambda *a, **k: True)
    def trigger(*args):
        s.capture_status = 'redirected'
        s.capture_reason = 'home'
        return False
    with pytest.raises(AccessStopped):
        s.collect_search_results(tab, "RAG", trigger)
    assert s.guard.session_stop
    assert not json.loads(s.guard.path.read_text(encoding='utf-8')).get('blocked_reason')


def test_search_security_stops_without_auto_recovery(config, monkeypatch):
    s = JobSearcher(None, config)
    states = iter([('security', [job('a')]), ('ok', [job('a'), job('b')])])
    def collect(tab, keyword, trigger):
        s.capture_status, batch = next(states)
        return batch
    collect = Mock(side_effect=collect)
    monkeypatch.setattr(s, 'collect_search_results', collect)
    wait = Mock(return_value=True)
    monkeypatch.setattr('job_search.wait_user_clear_security', wait)
    with pytest.raises(AccessStopped):
        s.search_with_recovery(NS(), 'RAG', Mock())
    assert collect.call_count == 1
    wait.assert_not_called()


def test_persistent_security_has_no_search_retry(config, monkeypatch):
    s = JobSearcher(None, config)
    def collect(*args):
        s.capture_status, s.capture_reason = 'security', 'code=37'
        return []
    collect = Mock(side_effect=collect)
    monkeypatch.setattr(s, 'collect_search_results', collect)
    wait = Mock(return_value=True)
    monkeypatch.setattr('job_search.wait_user_clear_security', wait)
    with pytest.raises(AccessStopped):
        s.search_with_recovery(NS(), 'RAG', Mock())
    assert collect.call_count == 1
    wait.assert_not_called()
    assert s.capture_status == 'security'


def test_interactive_verification_is_not_navigated_away(config, monkeypatch):
    s = JobSearcher(None, config)
    tab = NS(url="about:blank", run_js=Mock(return_value=""), get=Mock(), listen=NS(start=Mock(), stop=Mock(), wait=lambda **kw: False))
    monkeypatch.setattr('main.is_boss_security_page', lambda *a, **kw: True)
    monkeypatch.setattr(s, 'wait_capture', Mock(return_value=[]))
    monkeypatch.setattr('job_search.wait_user_clear_security', Mock(return_value=False))
    app = AutoBossApp.__new__(AutoBossApp)
    with pytest.raises(AccessStopped):
        s.search_with_recovery(tab, 'RAG', app._trigger_search)
    tab.get.assert_not_called()


@pytest.mark.parametrize('mode,direct', [('system', False), ('direct', True)])
def test_browser_launch_uses_explicit_loopback_port_and_scoped_proxy(config, tmp_path, mode, direct):
    from boss_login import BossLogin
    config['browser'].update(data_dir=str(tmp_path/'profile'), proxy_mode=mode)
    login = BossLogin(config)
    args = login._launch_args('edge.exe', 9228)
    assert '--remote-debugging-port=9228' in args
    assert '--remote-debugging-port=0' not in args
    assert '--remote-debugging-address=127.0.0.1' in args
    assert ('--no-proxy-server' in args) is direct


@pytest.mark.parametrize('saved_path,actual_path,expected', [
    ('/devtools/browser/owned', '/devtools/browser/owned', 9228),
    ('/devtools/browser/owned', '/devtools/browser/unrelated', None),
    ('/devtools/browser/owned', None, None),
])
def test_browser_reconnect_requires_saved_identity(config, tmp_path, monkeypatch, saved_path, actual_path, expected):
    from boss_login import BossLogin
    config['browser']['data_dir'] = str(tmp_path)
    (tmp_path/'browser_connection.json').write_text(json.dumps({'port':9228,'websocket_path':saved_path}),encoding='utf-8')
    login = BossLogin(config)
    monkeypatch.setattr(login, '_is_port_open', lambda port: True)
    monkeypatch.setattr(login, '_debug_endpoint', lambda port: actual_path)
    assert login._read_debug_port() == expected


def test_legacy_browser_port_file_is_still_supported(config, tmp_path, monkeypatch):
    from boss_login import BossLogin
    config['browser']['data_dir'] = str(tmp_path)
    (tmp_path/'DevToolsActivePort').write_text('9228\n/devtools/browser/legacy\n',encoding='utf-8')
    login = BossLogin(config)
    monkeypatch.setattr(login, '_is_port_open', lambda port: True)
    monkeypatch.setattr(login, '_debug_endpoint', lambda port: '/devtools/browser/legacy')
    assert login._read_debug_port() == 9228


def test_invalid_browser_proxy_mode_fails_before_launch(config):
    config['browser']['proxy_mode'] = 'typo'
    with pytest.raises(ValueError, match='proxy_mode'):
        validate_config(config)


@pytest.mark.parametrize('label', ['本科','硕士及以上','1-3年','经验不限','5天/周','3个月','在校/应届'])
def test_card_qualifications_are_not_skills(label):
    from relevance import is_skill_label
    assert not is_skill_label(label)
    assert is_skill_label('Python')


def test_jd_skills_are_used_even_when_card_has_degree_and_experience():
    r = ResumeData(skills=['Python','RAG'],raw_text='Python RAG 本科')
    matcher = JobMatcher(r)
    result = matcher.match(job(skills_required=['本科','1-3年'],description='要求 Java、Spring 和 MySQL 开发能力。'))
    assert {'java','spring','mysql'} <= set(result.missing_skills)
    assert '本科' not in result.missing_skills and '1-3年' not in result.missing_skills
    assert result.skill_score < 40


@pytest.mark.parametrize('title,missing', [
    ('Java AI Agent开发工程师',['Java']), ('Web&Flutter开发工程师',['Flutter']),
    ('Python开发工程师',[]), ('Java/Python开发工程师',[]), ('JavaScript开发工程师',[]),
])
def test_primary_title_stack_is_checked_against_each_resume(title, missing):
    matcher = JobMatcher(ResumeData(skills=['Python','RAG','JavaScript'],raw_text='Python RAG JavaScript'))
    assert matcher.missing_title_skills(job(title=title)) == missing



def test_online_profile_uses_declared_experience_and_all_exact_city_pairs():
    from boss_resume import profile_from_data
    data = {'name':'测试者','base':'3年经验本科', 'sections': {
        'summary':'熟悉 Python、Linux、Docker、Jenkins 和 Prometheus 自动化运维。'*12,
        'work':'工作经历\n示例科技有限公司\n运维工程师\n2023.06-至今\n负责部署和运维',
        'education':'教育经历\n示例理工学院\n2019-2023\n本科',
        'projects':'项目经历\n2024.01-至今\n测试项目'},
        'expectations':[{'position':'运维开发工程师','salary':'面议','cities':'广州，深圳，广州'},
                        {'position':'运维工程师','salary':'10-20K','cities':'深圳，上海'}]}
    profile = profile_from_data(data)
    assert profile.resume.name == '测试者'
    assert profile.resume.parsed_from == 'BOSS在线简历'
    assert JobMatcher(profile.resume)._my_exp_years == 3
    assert len(profile.resume.work_experiences) == 1  # Projects are not extra employers.
    assert [(t['keyword'],t['city']) for t in profile.targets] == [
        ('运维开发工程师','广州'),('运维开发工程师','深圳'),('运维工程师','深圳'),('运维工程师','上海')]
    assert profile.targets[0]['salary'] == ''


def test_empty_online_profile_never_falls_back_to_an_unrelated_local_resume():
    from boss_resume import profile_from_data
    with pytest.raises(ValueError,match='在线简历'):
        profile_from_data({'name':'','sections':{},'expectations':[]})


def test_city_scoped_queues_do_not_mix_locations(config):
    q = RunQueue(config,[ResumeData(raw_text='online profile')])
    q.add('广州｜运维工程师',[job('gz',location='广州')])
    q.add('深圳｜运维工程师',[job('sz',location='深圳')])
    q.done('广州｜运维工程师',job('gz'))
    assert [j.encrypt_job_id for j in q.get('深圳｜运维工程师')] == ['sz']


def test_fulltime_search_does_not_submit_internship(config):
    config['filter']['job_type'] = 'fulltime'
    app = AutoBossApp.__new__(AutoBossApp);app.config=config
    rejected = app._evaluate_job(job(title='运维开发实习生'), '运维开发工程师', JobSearcher(None,config), None, [])
    assert rejected[1] == '在线简历只选择全职岗位'
