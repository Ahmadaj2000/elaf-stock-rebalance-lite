"""Independent adversarial security / QA review of Stock Rebalancing Lite (ported to 17.0:
read_group instead of formatted_read_group, Registry from odoo.modules.registry).

Tests that demonstrate a vulnerability FAIL until the product is fixed;
control tests pass and document a verified protection.
"""
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import new_test_user, tagged

from odoo.addons.elaf_stock_rebalance_lite.models.edition import USAGE_KEY_RUNS
from .common import RebalanceCommon

_CTX = 'elaf_rb_usage_internal'


@tagged('post_install', '-at_install')
class TestZZSecurityReviewRebalanceLite(RebalanceCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = cls.env.ref('base.user_admin')
        cls.company_b = cls.env['res.company'].create({'name': 'RB SecRev Company B'})
        cls.wh_b1 = cls.env['stock.warehouse'].with_company(cls.company_b).create({
            'name': 'SecRev B1', 'code': 'SRB1', 'company_id': cls.company_b.id})
        cls.wh_b2 = cls.env['stock.warehouse'].with_company(cls.company_b).create({
            'name': 'SecRev B2', 'code': 'SRB2', 'company_id': cls.company_b.id})
        cls.user_b = new_test_user(
            cls.env, login='rb_secrev_user_b', name='RB SecRev User B',
            company_id=cls.company_b.id, company_ids=[(6, 0, cls.company_b.ids)],
            groups='elaf_stock_rebalance_lite.group_rebalance_user')
        cls.user2 = new_test_user(cls.env, login='rb_secrev_user2', name='RB SecRev Planner 2',
                                  groups='elaf_stock_rebalance_lite.group_rebalance_user')

    def _is_pro(self):
        return self.env['elaf.rebalance.edition']._elaf_rebalance_is_pro()

    def _skip_if_pro(self):
        if self._is_pro():
            self.skipTest('Lite-only check: the Pro edition is installed')

    def _used(self):
        return self.env['elaf.rebalance.usage']._get_status()['used']

    def _new_product(self, name, cost):
        return self.env['product.product'].create({
            'name': name, 'detailed_type': 'product', 'categ_id': self.categ.id,
            'standard_price': cost, 'uom_id': self.uom_unit.id})

    # ------------------------------------------------------------ usage
    def test_rb01_usage_rows_tamper_proof_with_internal_context(self):
        """SECURITY-REVIEW: RB-01 usage counter/log cannot be created, written or deleted by
        user, manager or Settings admin, even when the RPC caller adds the internal context flag."""
        usage = self.env['elaf.rebalance.usage']._get_or_create(USAGE_KEY_RUNS)
        for user in (self.user, self.manager, self.admin):
            row = usage.with_user(user).with_context(**{_CTX: True})
            self.assertEqual(row.read(['used'])[0]['used'], usage.used)  # read is allowed
            with self.assertRaises(AccessError):
                row.write({'used': 0})
            with self.assertRaises(AccessError):
                row.unlink()
            with self.assertRaises(AccessError):
                self.env['elaf.rebalance.usage'].with_user(user).with_context(**{_CTX: True}).create(
                    {'key': 'secrev_fake', 'used': 0, 'limit': 999})
            with self.assertRaises(AccessError):
                self.env['elaf.rebalance.usage.log'].with_user(user).with_context(**{_CTX: True}).create(
                    {'key': USAGE_KEY_RUNS, 'event': 'consumed'})

    def test_rb02_copy_rerun_and_reset_all_consume(self):
        """SECURITY-REVIEW: RB-02 copy(), cancel/reset/re-analyse each consume one run; the
        4th analysis is refused and a refused analysis does not consume."""
        self._skip_if_pro()
        run = self._make_run(user=self.user).with_user(self.user)
        run.action_analyze()
        self.assertEqual(self._used(), 1)
        dup = run.copy()
        dup.action_analyze()
        self.assertEqual(self._used(), 2)
        run.action_cancel()
        run.action_reset_draft()
        run.action_analyze()
        self.assertEqual(self._used(), 3)
        fourth = dup.copy()
        with self.assertRaises(UserError):
            fourth.action_analyze()
        self.assertEqual(fourth.state, 'draft')
        self.assertEqual(self._used(), 3)
        # an immediate second click on an analysed run neither recomputes nor consumes
        run.action_analyze()
        self.assertEqual(self._used(), 3)

    def test_rb03_more_warehouses_after_check_refused(self):
        """SECURITY-REVIEW: RB-03 adding a 3rd warehouse to an existing / analysed run is refused
        (constraint), also for a Settings administrator."""
        self._skip_if_pro()
        run = self._make_run()
        run.action_analyze()
        three = [(6, 0, (self.wh_central | self.wh_north | self.wh_east).ids)]
        for user in (self.user, self.admin):
            with self.assertRaises(ValidationError):
                self._make_run(user=user, warehouse_ids=three)
        with self.assertRaises(ValidationError):
            run.with_user(self.admin).write({'warehouse_ids': three})

    def test_rb04_second_company_refused_in_lite(self):
        """SECURITY-REVIEW: RB-04 Lite binds to the first company that analysed; a run of a
        second company is refused without consuming."""
        self._skip_if_pro()
        self._make_run().action_analyze()
        mc_manager = new_test_user(
            self.env, login='rb_secrev_mc', name='RB SecRev MC',
            company_id=self.company_b.id, company_ids=[(6, 0, (self.company.id, self.company_b.id))],
            groups='elaf_stock_rebalance_lite.group_rebalance_manager')
        run_b = self.env['elaf.rebalance.run'].with_user(mc_manager).with_company(self.company_b).create({
            'company_id': self.company_b.id,
            'warehouse_ids': [(6, 0, (self.wh_b1 | self.wh_b2).ids)],
        })
        with self.assertRaises(UserError):
            run_b.action_analyze()
        self.assertEqual(self._used(), 1)

    def test_rb05_hidden_recommendations_not_stored(self):
        """SECURITY-REVIEW: RB-05 recommendations beyond the top 10 are not stored, so they cannot
        be read through search_read / read_group, even as superuser."""
        self._skip_if_pro()
        products = self.env['product.product']
        for i in range(12):
            p = self._new_product('RB SecRev H%s' % i, 1.0 + i)
            self._deliver(p, self.wh_north, 90)
            self._set_stock(p, self.wh_north, 0)
            self._set_stock(p, self.wh_central, 100)
            products |= p
        run = self._make_run(user=self.user, product_ids=[(6, 0, products.ids)])
        run.with_user(self.user).action_analyze()
        Line = self.env['elaf.rebalance.line']
        self.assertEqual(Line.sudo().search_count([('run_id', '=', run.id)]), 10)
        self.assertEqual(len(Line.with_user(self.user).search_read([('run_id', '=', run.id)], ['id'])), 10)
        groups = Line.with_user(self.user).read_group([('run_id', '=', run.id)], ['__count'], [])
        self.assertEqual(groups[0]['__count'], 10)
        self.assertEqual(run.hidden_line_count, 2)
        # the two cheapest (impact 1x30, 2x30) are the hidden ones
        self.assertEqual(run.hidden_line_qty, 60)
        self.assertAlmostEqual(run.hidden_avoided_value, 30 * 1.0 + 30 * 2.0)

    def test_rb06_no_transfer_creation_in_lite(self):
        """SECURITY-REVIEW: RB-06 Lite has no transfer path: no transfer method, line states other
        than 'proposed' are rejected, analysis creates no picking, export is refused."""
        self._skip_if_pro()
        Picking = self.env['stock.picking'].sudo()
        before = Picking.search_count([])
        run = self._make_run(user=self.user)
        run.with_user(self.user).with_context(elaf_rb_usage_internal=True, create_transfers=True).action_analyze()
        self.assertEqual(Picking.search_count([]), before)
        for name in ('action_create_transfers', 'action_approve_all'):
            self.assertFalse(hasattr(run, name), name)
        line = run.line_ids[:1].with_user(self.manager)
        self.assertTrue(line)
        self.assertFalse(hasattr(line, 'action_approve'))
        self.assertNotIn('picking_id', line._fields)
        with self.assertRaises(ValueError):
            line.write({'state': 'approved'})
        with self.assertRaises(UserError):
            run.line_ids.with_user(self.user).export_data(['quantity'])

    def test_rb07_user_cannot_tamper_other_users_recommendations(self):
        """SECURITY-REVIEW: RB-07 recommendations and coverage rows are read-only results: a plain
        user must not rewrite or delete the lines/coverage rows of another planner's run."""
        run = self._make_run(user=self.manager)
        run.with_user(self.manager).action_analyze()
        line = run.line_ids[:1]
        stat = run.stat_ids[:1]
        self.assertTrue(line and stat)
        problems = []
        try:
            line.with_user(self.user2).write({
                'explanation': 'Tampered by another planner', 'avoided_procurement_value': 1e9})
            problems.append('line written')
        except AccessError:
            pass
        try:
            stat.with_user(self.user2).write({'status': 'balanced', 'available_qty': 0})
            problems.append('coverage row written')
        except AccessError:
            pass
        try:
            run.line_ids.with_user(self.user2).unlink()
            problems.append('lines deleted')
        except AccessError:
            pass
        self.assertFalse(problems, 'Non-owner plain user could alter a run he cannot edit: %s'
                         % ', '.join(problems))

    def test_rb08_user_cannot_fabricate_recommendations(self):
        """SECURITY-REVIEW: RB-08 a plain user must not create recommendation lines by RPC (bypass
        of the engine and of the Lite top-10 limit, fake 'avoided procurement' figures)."""
        run = self._make_run(user=self.manager)
        run.with_user(self.manager).action_analyze()
        before = run.line_count
        try:
            self.env['elaf.rebalance.line'].with_user(self.user2).create({
                'run_id': run.id, 'product_id': self.p4.id,
                'donor_warehouse_id': self.wh_central.id, 'receiver_warehouse_id': self.wh_north.id,
                'quantity': 9999, 'avoided_procurement_value': 1e6,
                'explanation': 'Fabricated recommendation',
            })
        except AccessError:
            return
        run.invalidate_recordset()
        self.fail('Plain user fabricated a recommendation on a manager run (line_count %s -> %s)'
                  % (before, run.line_count))

    def test_rb09_cross_company_dashboard_and_reads(self):
        """SECURITY-REVIEW: RB-09 a user of another company gets no run, line, coverage or
        valuation data through the dashboard methods, search_read or read_group."""
        run = self._make_run()
        run.action_analyze()
        Run_b = self.env['elaf.rebalance.run'].with_user(self.user_b)
        data = Run_b.get_dashboard_data({'run_id': run.id})
        self.assertFalse(data['run'])
        self.assertFalse(data['runs'])
        self.assertFalse(data['recommendations'])
        comp = Run_b.get_comparison_data()
        self.assertEqual(comp['value']['runs'], 0)
        self.assertEqual(comp['value']['avoided_value'], 0)
        Line_b = self.env['elaf.rebalance.line'].with_user(self.user_b)
        self.assertFalse(Line_b.search_read([('run_id', '=', run.id)], ['quantity']))
        self.assertFalse(Line_b.read_group([], ['avoided_procurement_value:sum'], ['run_id']))
        self.assertFalse(self.env['elaf.rebalance.stat'].with_user(self.user_b).read_group(
            [], ['on_hand_qty:sum'], ['product_id']))
        outsider = new_test_user(self.env, login='rb_secrev_outsider', groups='stock.group_stock_user')
        with self.assertRaises(AccessError):
            self.env['elaf.rebalance.run'].with_user(outsider).get_dashboard_data()

    # ------------------------------------------------ business spot checks
    def test_rb10_spot_check_reserved_excluded_quantity_and_value(self):
        """SECURITY-REVIEW: RB-10 hand-checked: reserved stock excluded, qty = min(need, spare),
        avoided procurement = qty x cost.

        Central: on hand 400, reserved 100 -> available 300; demand 180/90 = 2/day;
                 protected 2 x 45 = 90; spare 300 - 90 = 210; coverage 150 (excess > 75).
        North:   on hand 10; demand 450/90 = 5/day; coverage 2 (shortage);
                 target 5 x 30 = 150; need 150 - 10 = 140.
        Qty = min(140, 210) = 140. Avoided = 140 x 2.5 = 350.
        North after = 150 / 5 = 30 days; Central after = (300 - 140) / 2 = 80 days.
        """
        q = self._new_product('RB SecRev Q1', 2.5)
        self._deliver(q, self.wh_central, 180)
        self._deliver(q, self.wh_north, 450)
        self._set_stock(q, self.wh_central, 400)
        self._set_stock(q, self.wh_north, 10)
        self._reserve(q, self.wh_central, 100)
        run = self._make_run(product_ids=[(6, 0, q.ids)])
        run.action_analyze()
        central = self._stat(run, q, self.wh_central)
        self.assertAlmostEqual(central.reserved_qty, 100)
        self.assertAlmostEqual(central.available_qty, 300)
        self.assertAlmostEqual(central.spare_qty, 210)
        line = self._lines(run, q)
        self.assertEqual(len(line), 1)
        self.assertEqual(line.donor_warehouse_id, self.wh_central)
        self.assertAlmostEqual(line.receiver_need_qty, 140)
        self.assertAlmostEqual(line.quantity, 140)
        self.assertAlmostEqual(line.avoided_procurement_value, 350)
        self.assertAlmostEqual(line.receiver_coverage_after_days, 30)
        self.assertAlmostEqual(line.donor_coverage_after_days, 80)
        self.assertLessEqual(line.quantity, central.available_qty - central.protected_qty)

    def test_rb11_spot_check_donor_protection_binding(self):
        """SECURITY-REVIEW: RB-11 hand-checked donor protection when spare < need.

        Central: on hand 100, demand 90/90 = 1/day, protected 45, spare 55.
        North:   on hand 0, demand 270/90 = 3/day, need 3 x 30 = 90.
        Qty = min(90, 55) = 55; Central keeps 45 = exactly 45 days; avoided 55 x 4 = 220.
        """
        q = self._new_product('RB SecRev Q2', 4.0)
        self._deliver(q, self.wh_central, 90)
        self._deliver(q, self.wh_north, 270)
        self._set_stock(q, self.wh_central, 100)
        self._set_stock(q, self.wh_north, 0)
        run = self._make_run(product_ids=[(6, 0, q.ids)])
        run.action_analyze()
        line = self._lines(run, q)
        self.assertEqual(len(line), 1)
        self.assertAlmostEqual(line.receiver_need_qty, 90)
        self.assertAlmostEqual(line.donor_spare_qty, 55)
        self.assertAlmostEqual(line.quantity, 55)
        self.assertAlmostEqual(line.avoided_procurement_value, 220)
        self.assertGreaterEqual(line.donor_coverage_after_days, run.donor_min_coverage_days - 1e-9)

    def test_rb12_denial_log_does_not_escape_test_transaction(self):
        """SECURITY-REVIEW: RB-12 the 'testing' guard of _log_denied (usage.py) relies on
        threading.current_thread().testing, which Odoo 19 no longer sets (ported to 17.0): during tests a denial
        opens a second cursor and commits a log row outside the test transaction."""
        from unittest.mock import patch
        from odoo.modules.registry import Registry
        with patch.object(Registry, 'cursor', side_effect=RuntimeError('second cursor')) as cursor:
            self.env['elaf.rebalance.usage']._log_denied(USAGE_KEY_RUNS)
        self.assertFalse(cursor.called, '_log_denied opened an independent cursor while testing')
