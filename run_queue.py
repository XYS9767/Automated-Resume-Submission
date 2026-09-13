"""Atomic resumable search queue; confirmed contact history lives separately."""
import hashlib
import json
from dataclasses import asdict, fields
from job_search import JobPosting, job_url_key
from utils import resolve_path, safe_save_json


class RunQueue:
    def __init__(self, config, resumes):
        self.path = resolve_path(config.get('output', {}).get('queue_file', 'pending_jobs.json'))
        scope = {k: config.get(k, {}) for k in ('search', 'filter', 'risk_check')}
        scope['resumes'] = [r.raw_text for r in resumes]
        fingerprint = hashlib.sha256(json.dumps(scope, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.data = {'fingerprint': fingerprint, 'keywords': {}}
        if self.path.exists():
            previous = json.loads(self.path.read_text(encoding='utf-8'))
            if previous.get('fingerprint') == fingerprint:
                self.data = previous

    def get(self, keyword):
        names = {f.name for f in fields(JobPosting)}
        return [JobPosting(**{k: v for k, v in row.items() if k in names})
                for row in self.data['keywords'].get(keyword, {}).values()]

    def add(self, keyword, jobs):
        queue = self.data['keywords'].setdefault(keyword, {})
        for job in jobs:
            queue[job_url_key(job.url)] = asdict(job)
        self.save()

    def done(self, keyword, job):
        self.data['keywords'].get(keyword, {}).pop(job_url_key(job.url), None)
        self.save()

    def save(self):
        safe_save_json(self.data, str(self.path))
