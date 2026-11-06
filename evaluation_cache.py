"""Local, expiring job evidence and negative decisions; never cache permission to send."""
import hashlib
import json
import time

from utils import resolve_path, safe_save_json


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class EvaluationCache:
    VERSION = 1

    def __init__(self, config, resumes=(), clock=None):
        self.options = config.get('cache', {})
        self.path = resolve_path(self.options.get('file', 'evaluation_cache.json'))
        self.clock = clock or time.time
        # Hash credentials too, but never persist them or resume contents.
        self.context = digest([self.VERSION, config.get('filter', {}), config.get('risk_check', {}),
                               [r.raw_text for r in resumes]])
        self.data = {'details': {}, 'rejections': {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding='utf-8'))
                if all(isinstance(loaded.get(k), dict) for k in self.data):
                    self.data = loaded
            except (OSError, ValueError, AttributeError):
                pass  # Cache loss never changes the independently persisted access budget.

    @staticmethod
    def identity(job):
        from job_search import job_url_key
        return job_url_key(job.url)

    @staticmethod
    def listing(job):
        return digest([job.title, job.company, job.salary, job.location, job.experience,
                       job.education, job.tags, job.skills_required])

    def _get(self, kind, key, fingerprint):
        row = self.data[kind].get(key)
        if not isinstance(row, dict) or row.get('fingerprint') != fingerprint:
            return None
        if not isinstance(row.get('expires'), (int, float)) or row['expires'] <= self.clock():
            return None
        return row.get('value')

    def _put(self, kind, key, fingerprint, value, ttl):
        now = self.clock()
        for entries in self.data.values():
            for old in list(entries):
                row = entries[old]
                if not isinstance(row, dict) or not isinstance(row.get('expires'), (int, float)) or row['expires'] <= now:
                    del entries[old]
        self.data[kind][key] = dict(fingerprint=fingerprint, value=value, expires=now + ttl)
        safe_save_json(self.data, str(self.path))

    def restore_detail(self, job):
        value = self._get('details', self.identity(job), self.listing(job))
        if not isinstance(value, dict) or len(value.get('description', '')) < 30:
            return False
        for field in ('description', 'boss_online', 'boss_active_time'):
            setattr(job, field, value.get(field))
        return True

    def save_detail(self, job):
        if len(job.description or '') < 30 or getattr(job, 'detail_unverified', False):
            return
        value = {field: getattr(job, field) for field in ('description', 'boss_online', 'boss_active_time')}
        self._put('details', self.identity(job), self.listing(job), value,
                  float(self.options.get('detail_ttl_seconds', 3600)))

    def rejection_key(self, job, keyword, city, salary):
        # A direction mismatch for one keyword must not reject another direction.
        return digest([self.context, self.identity(job), keyword, city, salary])

    def rejection(self, job, keyword, city, salary):
        return self._get('rejections', self.rejection_key(job, keyword, city, salary), self.listing(job))

    def save_rejection(self, job, keyword, city, salary, result, reason):
        if result not in ('条件不符', '资格不符', '方向不符', 'BOSS不活跃', 'BOSS不在线'):
            return
        ttl = 900 if result.startswith('BOSS') else float(self.options.get('rejection_ttl_seconds', 86400))
        self._put('rejections', self.rejection_key(job, keyword, city, salary), self.listing(job),
                  {'result': result, 'reason': reason}, ttl)
