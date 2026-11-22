from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from .common import RebalanceCommon


@tagged('post_install', '-at_install')
class TestRebalanceEngine(RebalanceCommon):

    def test_01_demand_and_coverage(self):
        run = self._make_run()
        run.action_analyze()
        self.assertEqual(run.state, 'analyzed')
        self.assertEqual(run.product_count, 5)
        self.assertEqual(run.warehouse_count, 2)

        s = self._stat(run, self.p1, self.wh_central)
        self.assertAlmostEqual(s.on_hand_qty, 500)
        self.assertAlmostEqual(s.demand_qty, 270)
        self.assertAlmostEqual(s.daily_demand, 3.0)
        self.assertAlmostEqual(s.coverage_days, 500 / 3.0, places=4)
        self.assertEqual(s.status, 'excess')
        self.assertAlmostEqual(s.spare_qty, 365)

        n = self._stat(run, self.p1, self.wh_north)
        # 540 within the look-back; the 999 delivered 120 days ago is ignored
        self.assertAlmostEqual(n.demand_qty, 540)
        self.assertAlmostEqual(n.daily_demand, 6.0)
        self.assertAlmostEqual(n.coverage_days, 20 / 6.0, places=4)
        self.assertEqual(n.status, 'shortage')
        self.assertAlmostEqual(n.need_qty, 160)

        # customer returns are netted: 200 - 20 = 180 -> 2/day
        n2 = self._stat(run, self.p2, self.wh_north)
        self.assertAlmostEqual(n2.demand_qty, 180)
        self.assertAlmostEqual(n2.daily_demand, 2.0)
        self.assertEqual(n2.coverage_days, 0.0)
        self.assertEqual(n2.status, 'shortage')
        c2 = self._stat(run, self.p2, self.wh_central)
        self.assertAlmostEqual(c2.coverage_days, 50.0)
        self.assertEqual(c2.status, 'balanced')

        c4 = self._stat(run, self.p4, self.wh_central)
        self.assertTrue(c4.coverage_infinite)
        self.assertEqual(c4.coverage_display, '∞')
        self.assertEqual(c4.status, 'excess')
        self.assertEqual(self._stat(run, self.p4, self.wh_north).status, 'inactive')

    def test_02_recommended_quantity(self):
        run = self._make_run()
        run.action_analyze()
        line = self._lines(run, self.p1)
        self.assertEqual(len(line), 1)
        self.assertEqual(line.donor_warehouse_id, self.wh_central)
        self.assertEqual(line.receiver_warehouse_id, self.wh_north)
        self.assertAlmostEqual(line.quantity, 160)
        self.assertAlmostEqual(line.receiver_need_qty, 160)
        self.assertAlmostEqual(line.donor_spare_qty, 365)
        self.assertAlmostEqual(line.receiver_coverage_after_days, 180 / 6.0)
        self.assertIn('160', line.explanation)
        self.assertIn('365', line.explanation)
        self.assertIn('min(160, 365) = 160', line.explanation)
        # avoided procurement estimate = 160 x 10
        self.assertAlmostEqual(line.avoided_procurement_value, 1600.0)
        self.assertAlmostEqual(run.avoided_procurement_value, 1600 + 20 * 4 + 5 * 7 + 10 * 2)

    def test_03_donor_protection(self):
        run = self._make_run()
        run.action_analyze()
        line = self._lines(run, self.p2)
        # North needs 60, Central can only spare 200 - 4*45 = 20
        self.assertAlmostEqual(line.quantity, 20)
        self.assertAlmostEqual(line.receiver_need_qty, 60)
        self.assertAlmostEqual(line.donor_protected_qty, 180)
        donor = self._stat(run, self.p2, self.wh_central)
        self.assertAlmostEqual(donor.available_after_qty, 180)
        self.assertAlmostEqual(donor.coverage_after_days, 45.0)
        for stat in run.stat_ids.filtered(lambda s: s.outgoing_qty and not s.coverage_infinite):
            self.assertGreaterEqual(stat.coverage_after_days + 1e-6, run.donor_min_coverage_days,
                                    'a donor must never fall below its minimum coverage')

    def test_04_reservations_never_moved(self):
        self.assertAlmostEqual(self.reservation.quantity, 250, msg='setup reserved 250')
        run = self._make_run()
        run.action_analyze()
        s = self._stat(run, self.p3, self.wh_central)
        self.assertAlmostEqual(s.on_hand_qty, 300)
        self.assertAlmostEqual(s.reserved_qty, 250)
        self.assertAlmostEqual(s.available_qty, 50)
        line = self._lines(run, self.p3)
        # need 20 but only 50 - 45 = 5 can be spared; 250 reserved stays untouched
        self.assertAlmostEqual(line.quantity, 5)
        self.assertAlmostEqual(line.donor_reserved_qty, 250)
        self.assertIn('250 reserved', line.explanation)

    def test_05_uom_rounding(self):
        run = self._make_run()
        run.action_analyze()
        self.assertAlmostEqual(self._lines(run, self.p5).quantity, 10)
        run2 = self._make_run(whole_units=False)
        run2.action_analyze()
        line = self._lines(run2, self.p5)
        self.assertAlmostEqual(line.quantity, 9.67, places=2)
        self.assertAlmostEqual(self._stat(run2, self.p5, self.wh_north).need_qty, 9.67, places=2)

    def test_06_no_demand_no_line_and_no_shortage_message(self):
        run = self._make_run(product_ids=[(6, 0, self.p4.ids)])
        run.action_analyze()
        self.assertFalse(run.line_ids)
        self.assertIn('No shortage', run.analysis_note)

    def test_07_validation(self):
        run = self._make_run(warehouse_ids=[(6, 0, self.wh_central.ids)])
        with self.assertRaises(UserError):
            run.action_analyze()
        with self.assertRaises(ValidationError):
            self._make_run(target_coverage_days=60, donor_min_coverage_days=30)
        categ = self.env['product.category'].create({'name': 'RB empty categ'})
        self.env['product.product'].create({'name': 'RB empty', 'detailed_type': 'product',
                                            'categ_id': categ.id})
        run = self._make_run(product_ids=False, product_categ_ids=[(6, 0, categ.ids)])
        # the only product of that category has no stock and no demand: nothing to analyse
        with self.assertRaises(UserError):
            run.action_analyze()
        self.assertEqual(run.state, 'draft')

    def test_08_rerun_replaces_results(self):
        run = self._make_run()
        run.action_analyze()
        first_lines = run.line_ids
        run.date_analysis = run.date_analysis.replace(year=run.date_analysis.year - 1)
        run.action_analyze()
        self.assertFalse(first_lines.exists())
        self.assertEqual(len(run.line_ids), 4)

    def test_09_sub_locations_aggregated(self):
        shelf = self.env['stock.location'].create({'name': 'Shelf X', 'location_id': self.wh_north.lot_stock_id.id})
        self._set_stock(self.p1, self.wh_north, 40, location=shelf)
        run = self._make_run()
        run.action_analyze()
        n = self._stat(run, self.p1, self.wh_north)
        self.assertAlmostEqual(n.on_hand_qty, 60)
        self.assertAlmostEqual(self._lines(run, self.p1).quantity, 120)

    def test_10_user_can_run(self):
        run = self._make_run(user=self.user)
        run.action_analyze()
        self.assertEqual(run.line_count, 4)
