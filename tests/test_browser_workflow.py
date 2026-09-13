"""Real Edge against localhost only: no BOSS login, API key or real recipient."""
import json
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit, parse_qs, urlencode
from types import SimpleNamespace as NS
from pathlib import Path
import pytest
from DrissionPage import Chromium, ChromiumOptions

from boss_login import BossLogin
from job_search import JobSearcher, JobPosting
from submitter import JobSubmitter, SubmitResult
from main import AutoBossApp
from test_regressions import job


LIST_HTML = """<!doctype html><meta charset="utf-8">
<a class="nav-user">测试用户</a><a>消息</a><a>简历</a>
<input class="search-input"><div class="job-list-box"></div><a rel="next" href="#">下一页</a>
<script>
const query = new URLSearchParams(location.search).get('query');
document.querySelector('input').value = query;
let page = 1;
async function load() {
  // Stale response arrives before the requested query; it must never become a candidate.
  if (page === 1) await fetch('/wapi/zpgeek/search/joblist.json?query=Java&city=101210100&page=1');
  const data = await (await fetch('/wapi/zpgeek/search/joblist.json?' +
     new URLSearchParams({query, city:'101210100', page}))).json();
  for (const j of data.zpData.jobList) {
    const el = document.createElement('div'); el.className = 'job-card-wrapper';
    const a = document.createElement('a'); a.href = 'https://www.zhipin.com/job_detail/' + j.encryptJobId + '.html';
    a.className = 'job-name'; a.innerText = j.jobName;
    const company = document.createElement('span'); company.className = 'company-name'; company.innerText = j.brandName;
    el.append(a, company); document.querySelector('.job-list-box').append(el);
  }
  if (!data.zpData.hasMore) document.querySelector('[rel=next]').className = 'disabled';
}
document.querySelector('[rel=next]').onclick = e => {e.preventDefault(); page++; load();};
load();
</script>"""

DETAIL_HTML = """<!doctype html><meta charset="utf-8">
<div class="job-detail"><h1>RAG开发工程师</h1><div class="job-sec-text">负责 RAG、Python 和智能体系统开发。此处为本地测试岗位。</div>
<button class="btn-startchat">立即沟通</button><div role="alert"></div></div>
<script>
const id = location.pathname.split('/').pop().split('.')[0];
const button = document.querySelector('button');
if (id === 'already') button.innerText = '继续沟通';
let entered = false;
button.onmouseenter = () => { entered = true; };
button.onclick = async () => {
  if (id === 'hover' && !entered) return;
  if (id === 'noack') return;
  const result = await (await fetch('/wapi/zpgeek/friend/add.json', {
    method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'},
    body: new URLSearchParams({jobId:id === 'wrong' ? 'other' : id})
  })).json();
  if (result.code === 0 && id !== 'wrong') button.innerText = '继续沟通';
  else document.querySelector('[role=alert]').innerText = result.message || '';
};
</script>"""


@pytest.fixture(scope="module")
def site():
    calls = []
    search_attempts = {}
    def row(eid, title="RAG开发工程师"):
        return {"encryptJobId": eid, "jobName": title, "brandName": "示例科技",
                "salaryDesc": "10-15K", "cityName": "杭州", "jobExperience": "经验不限",
                "jobDegree": "本科", "skills": ["Python", "RAG"], "lid": "fixture",
                "itemDescription": ("负责 RAG 检索增强生成和 Python 大模型应用系统开发。熟悉向量数据库与检索流程。" * 4
                                    if title != "Java实习生" else "负责Java后台业务开发，熟悉Spring。" * 8)}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def send(self, data, mime):
            data = data.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            parts = urlsplit(self.path)
            if parts.path == '/web/passport/zp/security.html':
                self.send('<html><body>正在加载中<script>setTimeout(() => location.href="/", 300)</script></body></html>', 'text/html')
            elif parts.path == '/':
                self.send('<html><a class="nav-user">测试用户</a><body>已登录首页</body></html>', 'text/html')
            elif parts.path.endswith("/search/joblist.json"):
                p = parse_qs(parts.query); page = int(p.get("page", ["1"])[0])
                if p.get("query") == ["Java"]:
                    rows = [row("stale", "Java开发工程师")]
                else:
                    rows = [row("j1"), row("j2", "Java实习生")] if page == 1 else [row("j1"), row("j3")]
                self.send(json.dumps({"code": 0, "zpData": {"jobList": rows, "hasMore": page == 1}}), "application/json")
            elif "/job_detail/" in parts.path:
                self.send(DETAIL_HTML, "text/html")
            elif "/web/geek/job" in parts.path:
                keyword = parse_qs(parts.query).get('query', [''])[0]
                if keyword in ('RAG自动恢复', 'RAG持续校验'):
                    self.send(RECOVERY_HTML, 'text/html')
                else:
                    self.send(LIST_HTML, "text/html")
            else:
                self.send("<html><body>Local test only</body></html>", "text/html")
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            if urlsplit(self.path).path.endswith('/search/joblist.json'):
                keyword = parse_qs(body).get('query', [''])[0]
                search_attempts[keyword] = search_attempts.get(keyword, 0) + 1
                blocked = search_attempts[keyword] == 1 or keyword == 'RAG持续校验'
                data = {'code': 37, 'message': '您的环境存在异常.'} if blocked else {
                    'code': 0, 'zpData': {'jobList': [row('recovered')], 'hasMore': False}}
                self.send(json.dumps(data), 'application/json')
                return
            eid = parse_qs(body).get("jobId", [""])[0]
            calls.append(eid)
            data = {"code": 0}
            if eid == "security":
                data = {"code": 37, "message": "环境存在异常"}
            if eid == "limit":
                data = {"code": 99, "message": "今日沟通已达上限"}
            self.send(json.dumps(data), "application/json")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", calls
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


class RoutedTab:
    def __init__(self, tab, origin):
        self.tab, self.origin = tab, origin
    def __getattr__(self, name):
        return getattr(self.tab, name)
    def get(self, url, *args, **kwargs):
        parts = urlsplit(url)
        if parts.hostname in ("www.zhipin.com", "zhipin.com"):
            url = self.origin + parts.path + ("?" + parts.query if parts.query else "")
        assert url == "about:blank" or url.startswith(self.origin), "No external network navigation in tests"
        return self.tab.get(url, *args, **kwargs)


class RoutedBrowser:
    def __init__(self, browser, origin):
        self.browser, self.origin = browser, origin
    def new_tab(self, url="about:blank"):
        tab = RoutedTab(self.browser.new_tab("about:blank"), self.origin)
        if url != "about:blank":
            tab.get(url)
        return tab
    def get_tabs(self):
        return [RoutedTab(t, self.origin) for t in self.browser.get_tabs()]
    def quit(self):
        self.browser.quit()


@pytest.fixture(scope="module")
def browser(tmp_path_factory, site):
    options = ChromiumOptions()
    options.set_browser_path(BossLogin._find_edge_exe())
    options.set_user_data_path(str(tmp_path_factory.mktemp("edge-profile")))
    options.auto_port()
    options.headless()
    options.set_argument("--disable-background-networking")
    options.set_timeouts(base=3, page_load=10, script=5)
    raw = Chromium(options)
    yield RoutedBrowser(raw, site[0])
    raw.quit()


def test_real_edge_search_collects_second_page_and_excludes_stale_query(browser, config):
    config["search"]["capture_timeout"] = 2
    tab = browser.new_tab()
    app = AutoBossApp.__new__(AutoBossApp)
    searcher = JobSearcher(browser, config)
    result = searcher.collect_search_results(tab, "RAG开发工程师", app._trigger_search)
    assert [j.encrypt_job_id for j in result] == ["j1", "j2", "j3"]
    parsed = searcher._parse_page(tab)
    assert parsed and all(j.company == "示例科技" for j in parsed)
    tab.close()


@pytest.mark.parametrize("eid,expected,count", [
    ("success", SubmitResult.SUCCESS, 1),
    ("hover", SubmitResult.SUCCESS, 1),
    ("noack", SubmitResult.UNCONFIRMED, 0),
    ("wrong", SubmitResult.UNCONFIRMED, 0),
    ("security", SubmitResult.SECURITY_REQUIRED, 0),
    ("limit", SubmitResult.DAILY_LIMIT, 0),
    ("already", SubmitResult.ALREADY_APPLIED, 0),
])
def test_real_edge_click_requires_matching_confirmation(browser, config, site, eid, expected, count):
    config["submit"]["confirm_timeout"] = 1
    submitter = JobSubmitter(browser, config)
    before = len(site[1])
    assert submitter.submit(job(eid)) == expected
    assert submitter.get_today_count() == count
    if eid in ("already", "noack"):
        assert len(site[1]) == before
    submitter._detail_tab.close()


def test_real_edge_full_workflow_preview_then_submit_and_restart(browser, config, site, tmp_path, monkeypatch):
    import yaml
    import main
    resume = tmp_path / "resume.txt"
    resume.write_text("姓名：测试者\n教育经历\n测试大学 计算机 本科\n技能\nPython RAG 大模型 LangGraph\n项目经历\n" +
                      "使用 Python 和 RAG 实现检索增强大模型问答，负责向量数据库与系统部署。\n" * 8, encoding="utf-8")
    config["resume"] = {"file_path": str(resume)}
    config["search"]["capture_timeout"] = 2
    config["filter"].update(min_match_score=40)
    config["output"]["summary_file"] = str(tmp_path / "summary.json")
    config["submit"]["confirm_timeout"] = 1
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    work = browser.new_tab()
    class Login:
        def __init__(self, cfg):
            self.work_tab = work
        def login(self):
            return browser
    monkeypatch.setattr(main, "BossLogin", Login)
    monkeypatch.setattr(AutoBossApp, "_save_capture_log", lambda *args: None)
    before = len(site[1])
    app = AutoBossApp(str(cfg))
    assert app.run(search_only=True) == 0
    assert len(site[1]) == before
    preview = json.loads(Path(config["output"]["record_file"]).read_text(encoding="utf-8"))
    # j2 carries misleading RAG tags, but its actual JD says Java; see strict JD relevance test.
    assert any(r["result"] == "预览通过" for r in preview["records"])
    original_submit = JobSubmitter.submit
    def interrupt_second(self, job, score=0):
        if job.encrypt_job_id == 'j3':
            raise KeyboardInterrupt()
        return original_submit(self, job, score)
    monkeypatch.setattr(JobSubmitter, 'submit', interrupt_second)
    assert AutoBossApp(str(cfg)).run() == 130
    queue = json.loads(Path(config['output']['queue_file']).read_text(encoding='utf-8'))
    assert any('job:j3' in rows for rows in queue['keywords'].values())
    monkeypatch.setattr(JobSubmitter, 'submit', original_submit)
    assert AutoBossApp(str(cfg)).run() == 0
    posted = site[1][before:]
    assert "j1" in posted and "j3" in posted
    assert "stale" not in posted and "j2" not in posted
    before_restart = len(site[1])
    assert AutoBossApp(str(cfg)).run() == 0
    assert len(site[1]) == before_restart
    work.close()


def test_zero_size_security_iframe_is_not_a_visible_challenge(browser, site):
    from job_search import is_boss_security_page
    tab = browser.new_tab(site[0])
    tab.run_js("""
        document.body.innerHTML = '<div class="job-card-wrapper">岗位</div>' +
          '<iframe src="/security.html" style="width:0;height:0;border:0"></iframe>';
    """)
    assert not is_boss_security_page(tab, strict=True)
    tab.run_js("const frame=document.querySelector('iframe'); frame.style.width='300px'; frame.style.height='200px';")
    assert is_boss_security_page(tab, strict=True)
    tab.close()


RECOVERY_HTML = """<!doctype html><meta charset="utf-8"><a class="nav-user">测试用户</a>
<input class="search-input"><div class="job-list-box"></div>
<script>
const keyword = new URLSearchParams(location.search).get('query');
document.querySelector('input').value = keyword;
fetch('/wapi/zpgeek/search/joblist.json?_=123', {
  method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'},
  body:new URLSearchParams({query:keyword,city:'101210100',page:'1',pageSize:'15'})
}).then(r => r.json()).then(data => {
  if (data.code === 37) location.href='/web/passport/zp/security.html';
  else document.querySelector('.job-list-box').innerHTML =
    '<div class="job-card-wrapper"><a class="job-name" href="/job_detail/recovered.html">RAG开发工程师</a></div>';
});
</script>"""


@pytest.mark.parametrize('keyword,expected', [('RAG自动恢复', 'ok'), ('RAG持续校验', 'security')])
def test_real_edge_automatically_searches_again_after_verification_returns_home(browser, config, site, keyword, expected):
    config['search']['capture_timeout'] = 1
    config['runtime'] = {'security_wait_seconds': 8, 'search_retry_delay': .01}
    tab = browser.new_tab()
    s = JobSearcher(browser, config)
    app = AutoBossApp.__new__(AutoBossApp)
    before = len(site[1])
    try:
        result = s.search_with_recovery(tab, keyword, app._trigger_search)
        assert s.capture_status == expected, s.capture_reason
        assert [j.encrypt_job_id for j in result] == (['recovered'] if expected == 'ok' else [])
        assert len(site[1]) == before  # Search is automatic; no real/contact POST.
    finally:
        tab.close()



def test_real_edge_contact_anchor_with_whitespace_and_nested_nodes(browser, site, config):
    tab = browser.new_tab(site[0] + '/')
    try:
        tab.run_js('document.body.innerHTML = arguments[0]',
                   '<a class="btn-startchat" href="javascript:;">\n  <span>立即沟通</span>\n</a>')
        button = JobSubmitter(browser, config)._find_contact(tab)
        assert button and button.attr('class') == 'btn-startchat'
        button.run_js("this.setAttribute('aria-disabled','true')")
        assert JobSubmitter(browser, config)._find_contact(tab) is None
    finally:
        tab.close()



def test_real_edge_greeting_uses_active_chat_job_and_confirms_delivery(browser,site,config):
    tab = browser.new_tab(site[0] + '/web/geek/chat')
    config['submit']['greeting'] = '您好，这是本地测试消息。'
    try:
        tab.run_js("""
            document.body.innerHTML = '<div class="chat-conversation"><div class="top-info-content"></div>' +
                '<div class="chat-message"></div><div id="chat-input" contenteditable="true"></div>' +
                '<button class="btn-send disabled">发送</button></div>';
            document.querySelector('.top-info-content').__vue__ = {'conversation$':{encryptJobId:'greeting'}};
            const editor=document.querySelector('#chat-input'), button=document.querySelector('button');
            editor.oninput=()=>{button.className=editor.innerText.trim()?'btn-send':'btn-send disabled'};
            button.onclick=()=>{if(!editor.innerText.trim())return;
                const item=document.createElement('div');item.className='message-item item-myself';
                const status=document.createElement('span');status.className='message-status status-delivery';status.innerText='送达';
                item.append(status,document.createTextNode(editor.innerText));
                document.querySelector('.chat-message').append(item);editor.innerText='';};
        """)
        sub=JobSubmitter(browser,config)
        assert sub._tab_has_job(tab,'greeting')
        assert not sub._tab_has_job(tab,'other')
        assert '已送达' in sub._send_greeting(tab,job('greeting'))
        assert '未重复发送' in sub._send_greeting(tab,job('greeting'))
        assert len(tab.eles('css:.message-item',timeout=0)) == 1
        tab.run_js("document.querySelector('.top-info-content').__vue__['conversation$'].encryptJobId='other'")
        assert '身份无法确认' in sub._send_greeting(tab,job('greeting'))
    finally:
        tab.close()
