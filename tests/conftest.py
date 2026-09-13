import pytest

@pytest.fixture
def config(tmp_path):
    return {
        "search": {"keywords": ["RAG开发工程师"], "city": "杭州", "page_limit": 3, "capture_timeout": .02},
        "filter": {"min_match_score": 50, "ai_fit_check": False, "skip_kpi": False},
        "submit": {"daily_limit": 3, "interval": {"min": 0, "max": 0},
                   "state_file": str(tmp_path / "state.json"), "counter_file": str(tmp_path / "daily.json"),
                   "confirm_timeout": .03, "send_greeting": False},
        "risk_check": {"mode": "rule"},
        "resume": {}, "browser": {}, "login": {}, "runtime": {},
        "output": {"log_file": str(tmp_path / "log.csv"), "record_file": str(tmp_path / "records.json"),
                   "queue_file": str(tmp_path / "queue.json")}
    }
