import uuid
from unittest.mock import patch

import psycopg2

from odoo import sql_db
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import new_test_user, tagged

from odoo.addons.elaf_stock_rebalance_lite.models import edition
from .common import RebalanceCommon


@tagged('post_install', '-at_install')
class TestRebalanceUsage(RebalanceCommon):
    """Freemium limits of the Lite edition (FREEMIUM_SPEC A.7 / B)."""

    def setUp(self):
        super().setUp()
        self.Usage = self.env['elaf.rebalance.usage']
        if self.env['elaf.rebalance.edition']._elaf_rebalance_is_pro():
            self.skipTest('Lite limits do not apply when the Pro edition is installed')

    def _analyze_new(self, **vals):
        run = self._make_run(**vals)
        run.action_analyze()
        return run

    def _used(self):
        return self.Usage._get_status()['used']

    def test_01_counting(self):
        self.assertEqual(self._used(), 0)
        run = self._analyze_new()
        status = self.Usage._get_status()
        self.assertEqual(status['used'], 1)
        self.assertEqual(status['limit'], 3)
        self.assertEqual(status['remaining'], 2)
        self.assertEqual(status['bound_company_id'], self.company.id)
        log = self.env['elaf.rebalance.usage.log'].search([('res_id', '=', run.id)])
        self.assertEqual(log.event, 'consumed')
        self.assertEqual(log.used_after, 1)

    def test_02_limit_reached_denied_and_history_readable(self):
        runs = [self._analyze_new() for _i in range(3)]
        self.assertEqual(self._used(), 3)
        self.assertTrue(self.Usage._get_status()['exhausted'])
        fourth = self._make_run()
        with self.assertRaises(UserError) as err:
            fourth.action_analyze()
        self.assertIn('3/3', str(err.exception))
        self.assertEqual(fourth.state, 'draft')
        self.assertEqual(self._used(), 3)
        # history stays readable for users, and in-flight drafts can still be edited/cancelled
        for run in runs:
            run_u = run.with_user(self.user)
            self.assertEqual(run_u.state, 'analyzed')
            self.assertTrue(run_u.line_ids.mapped('explanation'))
        fourth.write({'lookback_days': 60})
        fourth.action_cancel()
        self.assertEqual(fourth.state, 'cancel')
        data = self.env['elaf.rebalance.run'].with_user(self.user).get_dashboard_data({})
        self.assertTrue(data['usage']['exhausted'])
        self.assertEqual(data['run']['id'], runs[-1].id)

    def test_03_failed_operation_does_not_consume(self):
        run = self._make_run()
        Run = type(self.env['elaf.rebalance.run'])
        with patch.object(Run, '_milestone_notification', side_effect=RuntimeError('boom after counter')):
            with self.assertRaises(RuntimeError):
                run.action_analyze()
        self.assertEqual(self._used(), 0)
        self.assertEqual(run.state, 'draft')
        self.assertFalse(run.line_ids)

    def test_04_duplicate_click_does_not_double_consume(self):
        run = self._make_run()
        run.action_analyze()
        lines = run.line_ids
        run.action_analyze()  # immediate second click
        self.assertEqual(self._used(), 1)
        self.assertEqual(run.line_ids, lines)

    def test_05_lock_path(self):
        """The consume path takes a row lock: a second connection cannot grab it."""
        key = 'test_lock_%s' % uuid.uuid4().hex[:8]
        db = sql_db.db_connect(self.env.cr.dbname)
        cr_a, cr_b = db.cursor(), db.cursor()
        try:
            cr_a.execute("""INSERT INTO elaf_rebalance_usage (key, used, "limit") VALUES (%s, 0, 3)
                            RETURNING id""", (key,))
            row_id = cr_a.fetchone()[0]
            cr_a.commit()
            cr_a.execute('SELECT id FROM elaf_rebalance_usage WHERE id = %s FOR UPDATE', (row_id,))
            raw_b = cr_b._cnx.cursor()  # raw psycopg2 cursor: no Odoo 'bad query' log
            with self.assertRaises(psycopg2.errors.LockNotAvailable):
                raw_b.execute('SELECT id FROM elaf_rebalance_usage WHERE id = %s FOR UPDATE NOWAIT', (row_id,))
            raw_b.close()
            cr_b.rollback()
            cr_a.execute('UPDATE elaf_rebalance_usage SET used = used + 1 WHERE id = %s AND used < "limit" '
                         'RETURNING used', (row_id,))
            self.assertEqual(cr_a.fetchone()[0], 1)
            cr_a.rollback()
        finally:
            cr_a.execute('DELETE FROM elaf_rebalance_usage WHERE key = %s', (key,))
            cr_a.commit()
            cr_a.close()
            cr_b.close()

    def test_06_nobody_can_write_usage(self):
        self._analyze_new()
        usage = self.Usage.search([('key', '=', 'completed_runs')])
        for user in (self.user, self.manager):
            with self.assertRaises(AccessError):
                usage.with_user(user).write({'used': 0})
            with self.assertRaises(AccessError):
                usage.with_user(user).unlink()
            with self.assertRaises(AccessError):
                self.Usage.with_user(user).create({'key': 'x'})
            self.assertEqual(usage.with_user(user).used, 1)  # reading is allowed
        # even the superuser environment cannot change it through the ORM
        with self.assertRaises(AccessError):
            usage.sudo().write({'used': 0})
        log = self.env['elaf.rebalance.usage.log'].search([], limit=1)
        with self.assertRaises(AccessError):
            log.sudo().write({'event': 'denied'})
        with self.assertRaises(AccessError):
            log.sudo().unlink()

    def test_07_second_company_refused(self):
        self._analyze_new()
        company_b = self.env['res.company'].create({'name': 'RB Second Co'})
        Warehouse = self.env['stock.warehouse'].with_company(company_b)
        wh1 = Warehouse.create({'name': 'B1', 'code': 'BB1', 'company_id': company_b.id})
        wh2 = Warehouse.create({'name': 'B2', 'code': 'BB2', 'company_id': company_b.id})
        run_b = self.env['elaf.rebalance.run'].with_company(company_b).create({
            'company_id': company_b.id, 'warehouse_ids': [(6, 0, [wh1.id, wh2.id])]})
        with self.assertRaises(UserError) as err:
            run_b.action_analyze()
        self.assertIn('one company', str(err.exception))
        self.assertEqual(self._used(), 1)

    def test_08_functional_limits(self):
        with self.assertRaises(ValidationError):
            self._make_run(warehouse_ids=[(6, 0, (self.wh_central | self.wh_north | self.wh_east).ids)])
        with patch.object(edition, 'LITE_MAX_PRODUCTS', 3):
            run = self._make_run()
            with self.assertRaises(UserError) as err:
                run.action_analyze()
            self.assertIn('up to 3', str(err.exception))
        self.assertEqual(self._used(), 0)

    def test_09_top_10_recommendations(self):
        products = self.env['product.product']
        for i in range(1, 13):
            p = self.env['product.product'].create({'name': 'RB Q%s' % i, 'detailed_type': 'product',
                                                    'standard_price': float(i)})
            self._deliver(p, self.wh_north, 90)
            self._set_stock(p, self.wh_north, 0)
            self._set_stock(p, self.wh_central, 100)
            products |= p
        run = self._analyze_new(product_ids=[(6, 0, products.ids)])
        # each product: need 30 at North, Central has no demand -> 12 x 30 recommended
        self.assertEqual(run.line_count, 10)
        self.assertEqual(run.hidden_line_count, 2)
        self.assertAlmostEqual(run.hidden_line_qty, 60)
        self.assertAlmostEqual(run.hidden_avoided_value, 30 * 1 + 30 * 2)
        self.assertEqual(set(run.line_ids.product_id.mapped('name')),
                         {'RB Q%s' % i for i in range(3, 13)})
        self.assertEqual(run.line_ids.sorted('sequence')[0].product_id.name, 'RB Q12')
        self.assertIn('2 additional recommendations', run.analysis_note)

    def test_10_caps_switch(self):
        with patch.object(edition, 'ELAF_USAGE_CAPS_ENABLED', False):
            for _i in range(4):
                self._analyze_new()
            self.assertEqual(self._used(), 4)
            # the functional limits stay
            with self.assertRaises(ValidationError):
                self._make_run(warehouse_ids=[(6, 0, (self.wh_central | self.wh_north | self.wh_east).ids)])

    def test_12_comparison_value_from_real_data(self):
        products = self.env['product.product']
        for i in range(1, 13):
            p = self.env['product.product'].create({'name': 'RB C%s' % i, 'detailed_type': 'product',
                                                    'standard_price': float(i)})
            self._deliver(p, self.wh_north, 90)
            self._set_stock(p, self.wh_north, 0)
            self._set_stock(p, self.wh_central, 100)
            products |= p
        run = self._analyze_new(product_ids=[(6, 0, products.ids)])
        data = self.env['elaf.rebalance.run'].with_user(self.user).get_comparison_data()
        value = data['value']
        self.assertEqual(value['runs'], 1)
        self.assertEqual(value['hidden_count'], 2)
        # 12 lines x 30 units x cost i: 30 x (1 + ... + 12) = 2340
        self.assertAlmostEqual(value['avoided_value'], 2340)
        self.assertAlmostEqual(value['avoided_hidden'], 90)
        self.assertAlmostEqual(run.avoided_procurement_value + run.hidden_avoided_value, 2340)
        self.assertIn(self.wh_east.id, (self.env['stock.warehouse'].search([]) - run.warehouse_ids).ids)
        self.assertGreaterEqual(value['warehouses_not_analysed'], 1)
        self.assertTrue(data['pro_url'].endswith('/17.0/elaf_stock_rebalance'))
        self.assertEqual(data['support_email'], 'info@elaf-development.com')
        self.assertTrue(any('2 warehouses' in t for t in data['limits_text']))

    def test_13_export_is_pro(self):
        run = self._analyze_new()
        with self.assertRaises(UserError):
            run.line_ids.export_data(['product_id', 'quantity'])
        with self.assertRaises(UserError):
            run.stat_ids.export_data(['product_id', 'coverage_days'])

    def test_11_first_run_milestone(self):
        run = self._make_run()
        action = run.action_analyze()
        self.assertEqual(action['tag'], 'display_notification')
        self.assertIn('First analysis', action['params']['message'])
