"""Remediation R2 (S5-R3): retries and duplicates never consume extra Lite allowances.

Decision: re-analysing an analysed run whose parameters and stock data are unchanged
is free (results are refreshed, the usage log records 'unchanged'); changed parameters
or data, a copy, or a run reset to draft count as a new analysis.
"""
from datetime import timedelta
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import RebalanceCommon


@tagged('post_install', '-at_install')
class TestRebalanceR2Usage(RebalanceCommon):

    def _used(self):
        return self.env['elaf.rebalance.usage']._get_status()['used']

    def _is_pro(self):
        return self.env['elaf.rebalance.edition']._elaf_rebalance_is_pro()

    @staticmethod
    def _age(run):
        """Move the last analysis out of the double-click window."""
        run.date_analysis = run.date_analysis - timedelta(minutes=10)

    def _log(self, run):
        return self.env['elaf.rebalance.usage.log'].search([('res_id', '=', run.id)], order='id')

    def test_01_unchanged_reanalysis_is_free(self):
        run = self._make_run()
        run.action_analyze()
        used = self._used()
        old_lines = run.line_ids
        fingerprint = run.analysis_fingerprint
        self.assertTrue(fingerprint)
        self._age(run)
        action = run.action_analyze()
        self.assertEqual(self._used(), used, 'same parameters and data: not counted')
        self.assertEqual(action['params']['title'], 'Analysis unchanged')
        self.assertEqual(run.analysis_fingerprint, fingerprint)
        self.assertFalse(old_lines.exists(), 'results are refreshed')
        self.assertEqual(len(run.line_ids), len(old_lines))
        self.assertEqual(self._log(run).mapped('event'), ['consumed', 'unchanged'])

    def test_02_changed_data_or_parameters_count(self):
        run = self._make_run()
        run.action_analyze()
        used = self._used()
        self._age(run)
        self._set_stock(self.p1, self.wh_central, 450)  # data changed
        run.action_analyze()
        self.assertEqual(self._used(), used + 1)
        self._age(run)
        run.write({'target_coverage_days': 31})  # parameter changed
        run.action_analyze()
        self.assertEqual(self._used(), used + 2)
        self.assertEqual(self._log(run).mapped('event'), ['consumed'] * 3)

    def test_07_new_reservation_is_a_data_change(self):
        run = self._make_run()
        run.action_analyze()
        used = self._used()
        self._age(run)
        self._reserve(self.p2, self.wh_central, 5)
        run.action_analyze()
        self.assertEqual(self._used(), used + 1)
        self.assertAlmostEqual(self._stat(run, self.p2, self.wh_central).reserved_qty, 5)

    def test_03_fingerprint_independent_of_language_and_user(self):
        run = self._make_run(user=self.user)
        run.with_user(self.user).action_analyze()
        used = self._used()
        self._age(run)
        self.env['res.lang']._activate_lang('ar_001')
        self.user.lang = 'ar_001'
        run.with_user(self.user).with_context(lang='ar_001').action_analyze()
        self.assertEqual(self._used(), used)

    def test_04_failure_mid_analysis_never_consumes(self):
        run = self._make_run()
        Run = type(self.env['elaf.rebalance.run'])
        used = self._used()
        with patch.object(Run, '_compute_recommendations', side_effect=RuntimeError('boom mid-run')):
            with self.assertRaises(RuntimeError):
                run.action_analyze()
        self.assertEqual(self._used(), used)
        self.assertEqual(run.state, 'draft')
        self.assertFalse(run.line_ids or run.stat_ids)
        # an analysed run whose re-analysis fails keeps its results and its counter
        run.action_analyze()
        used = self._used()
        lines = run.line_ids
        self._age(run)
        self._set_stock(self.p1, self.wh_central, 450)
        with patch.object(Run, '_apply_after_values', side_effect=RuntimeError('boom late')):
            with self.assertRaises(RuntimeError):
                run.action_analyze()
        self.assertEqual(self._used(), used)
        self.assertTrue(lines.exists())
        self.assertEqual(run.state, 'analyzed')

    def test_05_exhausted_lite_unchanged_rerun_allowed_changed_refused(self):
        if self._is_pro():
            self.skipTest('Lite allowance only')
        runs = [self._make_run() for _i in range(3)]
        for run in runs:
            run.action_analyze()
        self.assertEqual(self._used(), 3)
        first = runs[0]
        self._age(first)
        first.action_analyze()  # unchanged: allowed although the allowance is used up
        self.assertEqual(self._used(), 3)
        self._age(first)
        first.write({'lookback_days': 60})
        lines = first.line_ids
        with self.assertRaises(UserError) as err:
            first.action_analyze()
        self.assertIn('3/3', str(err.exception))
        self.assertTrue(lines.exists(), 'a refused re-analysis keeps the previous results')
        self.assertEqual(self._used(), 3)

    def test_06_double_click_and_reset(self):
        run = self._make_run()
        run.action_analyze()
        used = self._used()
        lines = run.line_ids
        run.action_analyze()  # second click inside the window: nothing recomputed
        self.assertEqual(self._used(), used)
        self.assertEqual(run.line_ids, lines)
        # reset to draft is a new analysis (RB-02), even with unchanged data
        run.action_cancel()
        run.action_reset_draft()
        run.action_analyze()
        self.assertEqual(self._used(), used + 1)
