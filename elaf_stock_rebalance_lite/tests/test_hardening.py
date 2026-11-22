"""Fixes of the security review (RB-07, RB-08, RB-12, RB-I1) and Lite UX checks."""
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import RebalanceCommon

LITE_MENUS = (
    'elaf_stock_rebalance_lite.menu_elaf_rebalance_usage',
    'elaf_stock_rebalance_lite.menu_elaf_rebalance_usage_log',
    'elaf_stock_rebalance_lite.menu_elaf_rebalance_comparison',
)


@tagged('post_install', '-at_install')
class TestRebalanceHardening(RebalanceCommon):

    def _is_pro(self):
        return self.env['elaf.rebalance.edition']._elaf_rebalance_is_pro()

    def test_01_results_are_engine_only_for_everybody(self):
        """Owner, manager and Settings administrator cannot create, edit or delete
        recommendations or coverage rows; only the analysis writes them."""
        run = self._make_run(user=self.user)
        run.with_user(self.user).action_analyze()
        admin = self.env.ref('base.user_admin')
        line, stat = run.line_ids[:1], run.stat_ids[:1]
        self.assertTrue(line and stat)
        for user in (self.user, self.manager, admin):
            with self.assertRaises(AccessError):
                line.with_user(user).write({'explanation': 'edited'})
            with self.assertRaises(AccessError):
                stat.with_user(user).write({'available_qty': 0})
            with self.assertRaises(AccessError):
                line.with_user(user).unlink()
            with self.assertRaises(AccessError):
                stat.with_user(user).unlink()
            with self.assertRaises(AccessError):
                self.env['elaf.rebalance.line'].with_user(user).create({
                    'run_id': run.id, 'product_id': self.p4.id, 'quantity': 1,
                    'donor_warehouse_id': self.wh_central.id, 'receiver_warehouse_id': self.wh_north.id})
            with self.assertRaises(AccessError):
                self.env['elaf.rebalance.stat'].with_user(user).create({
                    'run_id': run.id, 'product_id': self.p4.id, 'warehouse_id': self.wh_central.id})
        # results stay readable and a re-run by the owner still replaces them
        self.assertTrue(line.with_user(self.user).explanation)
        old_lines = run.line_ids
        run.write({'date_analysis': False})
        run.with_user(self.user).action_analyze()
        self.assertFalse(old_lines.exists())
        self.assertTrue(run.line_ids)

    def test_02_other_user_cannot_reanalyse_foreign_run(self):
        """Re-analysis clears results with sudo(): only who may modify the run can trigger it."""
        run = self._make_run(user=self.manager)
        run.with_user(self.manager).action_analyze()
        lines = run.line_ids
        run.write({'date_analysis': False})
        with self.assertRaises(AccessError):
            run.with_user(self.user).action_analyze()
        self.assertTrue(lines.exists())

    def test_03_run_list_export_is_pro(self):
        """RB-I1: the run list is locked like the coverage and recommendation lists."""
        run = self._make_run()
        run.action_analyze()
        if self._is_pro():
            self.assertTrue(run.export_data(['name'])['datas'])
            return
        with self.assertRaises(UserError):
            run.with_user(self.user).export_data(['name', 'line_count'])
        arch = self.env['elaf.rebalance.run'].get_view(
            self.env.ref('elaf_stock_rebalance_lite.view_elaf_rebalance_run_list').id, 'list')['arch']
        self.assertIn('export_xlsx="0"', arch)

    def test_04_lite_menus_follow_edition(self):
        """'Lite Usage', 'Usage Log' and 'Lite vs Pro' are shown in Lite and hidden with Pro,
        computed at menu load (no record is changed, so uninstalling Pro shows them again)."""
        menu_ids = {self.env.ref(x).id for x in LITE_MENUS}
        Menu = self.env['ir.ui.menu'].with_user(self.manager)
        blacklisted = set(Menu._load_menus_blacklist())
        loaded = set(Menu.load_menus(debug=False))
        if self._is_pro():
            self.assertLessEqual(menu_ids, blacklisted)
            self.assertFalse(menu_ids & loaded)
        else:
            self.assertFalse(menu_ids & blacklisted)
            self.assertLessEqual(menu_ids, loaded)
        for xmlid in LITE_MENUS:
            self.assertTrue(self.env.ref(xmlid).active)

    def test_05_denial_log_test_guard(self):
        """RB-12: the test-mode guard of _log_denied is effective on this series."""
        self.assertTrue(self.env['elaf.rebalance.usage']._in_test_mode())
