from odoo.exceptions import AccessError, ValidationError
from odoo.tests import new_test_user, tagged

from .common import RebalanceCommon


@tagged('post_install', '-at_install')
class TestRebalanceSecurity(RebalanceCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_b = cls.env['res.company'].create({'name': 'RB Other Company'})
        cls.wh_other = cls.env['stock.warehouse'].with_company(cls.company_b).create({
            'name': 'Other Co WH', 'code': 'OCW', 'company_id': cls.company_b.id})
        cls.user_b = new_test_user(cls.env, login='rb_user_company_b', name='RB User B',
                                   company_id=cls.company_b.id, company_ids=[(6, 0, cls.company_b.ids)],
                                   groups='elaf_stock_rebalance_lite.group_rebalance_user')

    def test_01_other_company_cannot_see_run(self):
        run = self._make_run()
        run.action_analyze()
        Run_b = self.env['elaf.rebalance.run'].with_user(self.user_b)
        self.assertFalse(Run_b.search([('id', '=', run.id)]))
        self.assertFalse(self.env['elaf.rebalance.line'].with_user(self.user_b).search(
            [('run_id', '=', run.id)]))
        self.assertFalse(self.env['elaf.rebalance.stat'].with_user(self.user_b).search(
            [('run_id', '=', run.id)]))
        with self.assertRaises(AccessError):
            run.with_user(self.user_b).read(['name'])

    def test_02_warehouses_of_run_company_only(self):
        with self.assertRaises(ValidationError):
            self._make_run(warehouse_ids=[(6, 0, [self.wh_central.id, self.wh_other.id])])

    def test_03_user_cannot_delete_or_edit_others_runs(self):
        run = self._make_run(user=self.manager)
        with self.assertRaises(AccessError):
            run.with_user(self.user).unlink()
        with self.assertRaises(AccessError):
            run.with_user(self.user).write({'lookback_days': 30})
        own = self._make_run(user=self.user)
        with self.assertRaises(AccessError):
            own.with_user(self.user).unlink()  # deleting runs is a manager right
        own.with_user(self.user).write({'lookback_days': 30})
        run.with_user(self.manager).unlink()

    def test_04_user_without_group_has_no_access(self):
        outsider = new_test_user(self.env, login='rb_outsider', groups='stock.group_stock_user')
        with self.assertRaises(AccessError):
            self.env['elaf.rebalance.run'].with_user(outsider).search([])
