import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from boss_resume import OnlineProfile, profile_from_data
from job_search import JobPosting, is_boss_security_page
from main import AutoBossApp
from resume_parser import ResumeData
from submitter import JobSubmitter, SubmitResult
from utils import extract_salary_range, validate_config


class SecurityDetectionTests(unittest.TestCase):
    def tab(self, url, controls=False, body='运维工程师 岗位详情'):
        return SimpleNamespace(url=url, run_js=Mock(return_value={
            'body': body, 'notices': '', 'content': False, 'challenge': controls}))

    def test_completed_verification_query_does_not_block(self):
        tab = self.tab('https://www.zhipin.com/web/geek/jobs?_security_check=token')
        self.assertFalse(is_boss_security_page(tab, strict=True))

    def test_security_redirect_in_query_is_not_current_route(self):
        tab = self.tab('https://www.zhipin.com/web/geek/jobs?from=/captcha/security.html')
        self.assertFalse(is_boss_security_page(tab, strict=True))

    def test_actual_verification_route_still_blocks(self):
        for path in ('/web/passport/zp/security', '/captcha', '/web/common/security.html'):
            with self.subTest(path=path):
                self.assertTrue(is_boss_security_page(self.tab('https://www.zhipin.com' + path)))

    def test_visible_verification_widget_still_blocks(self):
        self.assertTrue(is_boss_security_page(self.tab(
            'https://www.zhipin.com/web/geek/jobs?_security_check=token', controls=True), strict=True))

    def test_active_challenge_text_still_blocks(self):
        self.assertTrue(is_boss_security_page(self.tab(
            'https://www.zhipin.com/web/geek/jobs', body='请完成安全验证'), strict=True))

    def test_ordinary_job_mentioning_verification_code_is_allowed(self):
        self.assertFalse(is_boss_security_page(self.tab(
            'https://www.zhipin.com/web/geek/jobs', body='负责验证码系统的运维'), strict=True))


class SearchPreferenceTests(unittest.TestCase):
    def config(self):
        cfg = {'resume': {'source': 'boss_online'},
               'search': {'source': 'config', 'city': '深圳',
                          'keywords': ['运维工程师', 'DevOps'], 'salary': '10-20K'}}
        validate_config(cfg)
        return cfg

    def run_resume_only(self, cfg):
        cfg = copy.deepcopy(cfg)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        cfg['safety']['state_file'] = str(Path(directory.name) / 'access.json')
        cfg['output']['summary_file'] = str(Path(directory.name) / 'summary.json')
        resume = ResumeData(name='测试用户', raw_text='Linux Docker 运维',
                            declared_experience_years=5)
        profile = OnlineProfile(resume, [{'keyword': '运维工程师', 'city': '武汉',
                                          'city_code': '101200100', 'salary': '8-15K'}])
        with patch('main.load_config', return_value=cfg), patch('main.BossLogin'), \
                patch('boss_resume.read_online_profile', return_value=profile) as read, \
                patch.object(AutoBossApp, '_stop_requested', return_value=False), \
                patch.object(AutoBossApp, '_validate_resumes'):
            app = AutoBossApp('unused.yaml')
            self.assertEqual(app.run(resume_only=True), 0)
            return app, read

    def test_online_resume_keeps_configured_shenzhen_targets(self):
        cfg = self.config()
        original = copy.deepcopy(cfg)
        app, read = self.run_resume_only(cfg)
        self.assertEqual(app.config['search']['city'], '深圳')
        self.assertEqual(app.config['search']['keywords'], ['运维工程师', 'DevOps'])
        self.assertEqual(app.config['search']['salary'], '10-20K')
        self.assertNotIn('targets', app.config['search'])
        self.assertFalse(read.call_args.kwargs['use_expectations'])
        self.assertEqual(cfg, original, '运行期间不能污染缓存中的原始配置')

    def test_explicit_online_expectations_still_work(self):
        cfg = self.config()
        cfg['search']['source'] = 'boss_online'
        app, read = self.run_resume_only(cfg)
        self.assertEqual(app.config['search']['city'], '武汉')
        self.assertEqual(app.config['search']['salary'], '8-15K')
        self.assertTrue(read.call_args.kwargs['use_expectations'])

    def test_old_config_keeps_online_behavior(self):
        cfg = self.config()
        del cfg['search']['source']
        app, _ = self.run_resume_only(cfg)
        self.assertEqual(app.config['search']['city'], '武汉')

    def test_config_search_does_not_require_online_expectations(self):
        data = {'name': '测试用户', 'base': '5年经验',
                'sections': {'work': '工作经历\nLinux Docker 运维维护。' * 30},
                'expectations': [{'position': '运维工程师', 'cities': '未收录城市'}]}
        profile = profile_from_data(data, use_expectations=False)
        self.assertEqual(profile.resume.declared_experience_years, 5)
        self.assertEqual(profile.targets, [])
        with self.assertRaisesRegex(ValueError, '城市代码'):
            profile_from_data(data)


class FilterTests(unittest.TestCase):
    def test_posted_minimum_must_reach_15k(self):
        for salary in ('10-20K', '12-18K', '14-25K', '14.9-20K', '10-12K·13薪'):
            with self.subTest(salary=salary):
                self.assertIn('低于配置下限', AutoBossApp._salary_rejection_reason(
                    JobPosting(salary=salary), '15-20K'))

    def test_acceptable_salary_floor_and_upper_target(self):
        for salary in ('15-20K', '15K-20K·13薪', '15-25K', '18-25K', '20-30K'):
            with self.subTest(salary=salary):
                self.assertEqual(AutoBossApp._salary_rejection_reason(
                    JobPosting(salary=salary), '15-20K'), '')
        self.assertIn('超过配置上限', AutoBossApp._salary_rejection_reason(
            JobPosting(salary='25-35K'), '15-20K'))

    def test_unknown_or_nonmonthly_pay_is_not_assumed_to_meet_floor(self):
        for salary in ('', '面议', '200-500元/天', '15-20K/年', '20-15K'):
            with self.subTest(salary=salary):
                self.assertTrue(AutoBossApp._salary_rejection_reason(JobPosting(salary=salary), '15-20K'))

    def test_unlimited_salary_keeps_unlimited_behavior(self):
        self.assertEqual(AutoBossApp._salary_rejection_reason(JobPosting(salary='面议'), ''), '')

    def test_numeric_experience_caps(self):
        for cap, requirement, rejected in (
                ('2年', '3-5年', True), ('5年', '5-10年', False),
                ('3年', '3-5年', False), ('0年', '1-3年', True),
                ('1-3年', '3-5年', False), ('3-5年', '5-10年', False),
                ('2年', '经验不限', False)):
            with self.subTest(cap=cap, requirement=requirement):
                self.assertEqual(AutoBossApp._experience_too_high(
                    JobPosting(experience=requirement), cap), rejected)

    def test_jd_experience_is_checked_against_actual_cap(self):
        job = JobPosting(description='要求5年以上运维经验')
        self.assertTrue(AutoBossApp._experience_too_high(job, '2年'))
        self.assertFalse(AutoBossApp._experience_too_high(job, '5年'))

    def test_salary_units_and_bounds(self):
        for text, expected in (('10-20K', (10000, 20000)),
                               ('15K-30K·14薪', (15000, 30000)),
                               ('3K以下', (0, 3000)),
                               ('50K以上', (50000, float('inf')))):
            with self.subTest(text=text):
                self.assertEqual(extract_salary_range(text), expected)
                validate_config({'search': {'salary': text}})

    def test_invalid_salary_fails_instead_of_disabling_filter(self):
        for salary in ('10-20', '20-10K', 'unknown'):
            with self.subTest(salary=salary):
                with self.assertRaisesRegex(ValueError, 'salary'):
                    validate_config({'search': {'salary': salary}})

    def test_invalid_source_and_attempt_limit(self):
        for cfg in ({'search': {'source': 'typo'}},
                    {'submit': {'max_attempts_per_run': -1}}):
            with self.assertRaises(ValueError):
                validate_config(cfg)


class SubmissionLimitTests(unittest.TestCase):
    def test_uncertain_click_consumes_limit_without_second_click(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = {'submit': {'daily_limit': 150, 'max_attempts_per_run': 1,
                              'send_greeting': False,
                              'state_file': str(Path(directory) / 'state.json'),
                              'counter_file': str(Path(directory) / 'count.json')}}
            cfg['safety'] = {'state_file': str(Path(directory) / 'access.json'), 'min_interval_seconds': 0}
            tab = Mock()
            tab.url = 'https://www.zhipin.com/job_detail/abc.html'
            tab.run_js.return_value = ''
            submitter = JobSubmitter(Mock(), cfg, get_detail_tab=lambda: tab)
            job = JobPosting(title='运维工程师', url='https://www.zhipin.com/job_detail/abc.html',
                             encrypt_job_id='abc')
            with patch.object(submitter, '_open_job_detail', return_value=True), \
                    patch.object(submitter, '_page_state', return_value=None), \
                    patch.object(submitter, '_find_contact', return_value=Mock()), \
                    patch.object(submitter, '_wait_confirmation', return_value=SubmitResult.UNCONFIRMED), \
                    patch('submitter.enable_durable_responses'):
                self.assertEqual(submitter.submit(job), SubmitResult.UNCONFIRMED)
                self.assertEqual(submitter.attempt_count, 1)
                self.assertEqual(submitter.get_today_count(), 0)
                second = JobPosting(url='https://www.zhipin.com/job_detail/def.html', encrypt_job_id='def')
                self.assertEqual(submitter.submit(second), SubmitResult.RUN_LIMIT)
                self.assertEqual(tab.actions.click.call_count, 1)
            again = JobSubmitter(Mock(), cfg)
            self.assertEqual(again.submit(job), SubmitResult.UNCONFIRMED)


if __name__ == '__main__':
    unittest.main()
