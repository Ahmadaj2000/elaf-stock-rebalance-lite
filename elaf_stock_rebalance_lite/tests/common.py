from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, new_test_user


class RebalanceCommon(TransactionCase):
    """Hand-checkable scenario (look-back 90 days, target 30, donor minimum 45).

    P1 (cost 10): Central on hand 500, delivered 270 -> 3/day, cover 166.7 (excess)
                  North on hand 20, delivered 540 -> 6/day, cover 3.3 (shortage)
                  need = 6*30 - 20 = 160; Central spare = 500 - 3*45 = 365 -> 160
                  (a 999 delivery 120 days ago at North is outside the look-back)
    P2 (cost 4):  Central on hand 200, delivered 360 -> 4/day, cover 50 (balanced)
                  North on hand 0, delivered 200, returned 20 -> net 180 -> 2/day
                  need = 60; Central spare = 200 - 4*45 = 20 -> 20 (donor protection)
    P3 (cost 7):  Central on hand 300 of which 250 reserved, delivered 90 -> 1/day
                  available 50, protected 45, spare 5
                  North on hand 10, delivered 90 -> 1/day, need 30 - 10 = 20 -> 5
    P4 (cost 1):  Central 100 on hand, no demand anywhere -> excess / inactive
    P5 (cost 2):  Central on hand 100, delivered 100 -> 1.111/day, protected 50, spare 50
                  North on hand 7, delivered 50 -> 0.5556/day, target 16.67
                  need 9.67 -> whole units 10 (0.01 precision otherwise: 9.67)
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.customer_loc = cls.env.ref('stock.stock_location_customers')
        Warehouse = cls.env['stock.warehouse']
        cls.wh_central = Warehouse.create({'name': 'Test Central DC', 'code': 'TCD'})
        cls.wh_north = Warehouse.create({'name': 'Test North Store', 'code': 'TNS'})
        cls.wh_east = Warehouse.create({'name': 'Test East Store', 'code': 'TES'})
        cls.categ = cls.env['product.category'].create({'name': 'Rebalance Test'})
        cls.uom_unit = cls.env.ref('uom.product_uom_unit')

        def product(name, cost, **extra):
            vals = {'name': name, 'type': 'consu', 'is_storable': True, 'categ_id': cls.categ.id,
                    'standard_price': cost, 'uom_id': cls.uom_unit.id}
            vals.update(extra)
            return cls.env['product.product'].create(vals)

        cls.p1 = product('RB Test P1', 10.0)
        cls.p2 = product('RB Test P2', 4.0)
        cls.p3 = product('RB Test P3', 7.0)
        cls.p4 = product('RB Test P4', 1.0)
        cls.p5 = product('RB Test P5', 2.0)

        cls._deliver(cls.p1, cls.wh_central, 270)
        cls._deliver(cls.p1, cls.wh_north, 540)
        cls._deliver(cls.p1, cls.wh_north, 999, days_ago=120)
        cls._deliver(cls.p2, cls.wh_central, 360)
        cls._deliver(cls.p2, cls.wh_north, 200)
        cls._return(cls.p2, cls.wh_north, 20)
        cls._deliver(cls.p3, cls.wh_central, 90)
        cls._deliver(cls.p3, cls.wh_north, 90)
        cls._deliver(cls.p5, cls.wh_central, 100)
        cls._deliver(cls.p5, cls.wh_north, 50)

        cls._set_stock(cls.p1, cls.wh_central, 500)
        cls._set_stock(cls.p1, cls.wh_north, 20)
        cls._set_stock(cls.p2, cls.wh_central, 200)
        cls._set_stock(cls.p2, cls.wh_north, 0)
        cls._set_stock(cls.p3, cls.wh_central, 300)
        cls._set_stock(cls.p3, cls.wh_north, 10)
        cls._set_stock(cls.p4, cls.wh_central, 100)
        cls._set_stock(cls.p5, cls.wh_central, 100)
        cls._set_stock(cls.p5, cls.wh_north, 7)
        cls.reservation = cls._reserve(cls.p3, cls.wh_central, 250)

        cls.user = new_test_user(cls.env, login='rb_planner_test', name='RB Planner',
                                 groups='elaf_stock_rebalance_lite.group_rebalance_user')
        cls.manager = new_test_user(cls.env, login='rb_manager_test', name='RB Manager',
                                    groups='elaf_stock_rebalance_lite.group_rebalance_manager')

    # ---------------------------------------------------------- helpers
    @classmethod
    def _done_move(cls, product, src, dest, qty, days_ago):
        move = cls.env['stock.move'].create({
            'name': product.display_name,  # required on 17.0/18.0
            'product_id': product.id,
            'product_uom_qty': qty,
            'product_uom': product.uom_id.id,
            'location_id': src.id,
            'location_dest_id': dest.id,
            'company_id': cls.company.id,
        })
        move._action_confirm()
        move.quantity = qty
        move.picked = True
        move._action_done()
        move.date = fields.Datetime.now() - timedelta(days=days_ago)
        return move

    @classmethod
    def _deliver(cls, product, wh, qty, days_ago=10):
        return cls._done_move(product, wh.lot_stock_id, cls.customer_loc, qty, days_ago)

    @classmethod
    def _return(cls, product, wh, qty, days_ago=5):
        return cls._done_move(product, cls.customer_loc, wh.lot_stock_id, qty, days_ago)

    @classmethod
    def _set_stock(cls, product, wh, qty, location=None, lot=None):
        location = location or wh.lot_stock_id
        Quant = cls.env['stock.quant']
        current = sum(Quant.search([('product_id', '=', product.id), ('location_id', '=', location.id),
                                    ('lot_id', '=', lot.id if lot else False)]).mapped('quantity'))
        Quant._update_available_quantity(product, location, qty - current, lot_id=lot)

    @classmethod
    def _reserve(cls, product, wh, qty):
        move = cls.env['stock.move'].create({
            'name': product.display_name,  # required on 17.0/18.0
            'product_id': product.id,
            'product_uom_qty': qty,
            'product_uom': product.uom_id.id,
            'location_id': wh.lot_stock_id.id,
            'location_dest_id': cls.customer_loc.id,
            'company_id': cls.company.id,
        })
        move._action_confirm()
        move._action_assign()
        return move

    def _make_run(self, user=None, **vals):
        env = self.env(user=user) if user else self.env
        base = {
            'warehouse_ids': [(6, 0, [self.wh_central.id, self.wh_north.id])],
            'product_ids': [(6, 0, (self.p1 | self.p2 | self.p3 | self.p4 | self.p5).ids)],
        }
        base.update(vals)
        return env['elaf.rebalance.run'].create(base)

    @staticmethod
    def _stat(run, product, wh):
        return run.stat_ids.filtered(lambda s: s.product_id == product and s.warehouse_id == wh)

    @staticmethod
    def _lines(run, product):
        return run.line_ids.filtered(lambda line: line.product_id == product)
