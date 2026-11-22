from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .line import _check_engine_only

STATUS_SELECTION = [
    ('shortage', 'Shortage'),
    ('balanced', 'Balanced'),
    ('excess', 'Excess'),
    ('inactive', 'No stock, no demand'),
]


class ElafRebalanceStat(models.Model):
    """Analysis snapshot per product and warehouse for one run."""

    _name = 'elaf.rebalance.stat'
    _description = 'Stock Rebalancing Coverage'
    _order = 'run_id desc, product_id, warehouse_id'
    _check_company_auto = True

    run_id = fields.Many2one('elaf.rebalance.run', required=True, ondelete='cascade',
                             index=True, readonly=True, string='Run')
    company_id = fields.Many2one(related='run_id.company_id', store=True, index=True)
    product_id = fields.Many2one('product.product', required=True, readonly=True,
                                 string='Product', index=True)
    uom_id = fields.Many2one(related='product_id.uom_id', string='Unit')
    warehouse_id = fields.Many2one('stock.warehouse', required=True, readonly=True,
                                   string='Warehouse', index=True)
    on_hand_qty = fields.Float('On Hand', digits='Product Unit of Measure', readonly=True)
    reserved_qty = fields.Float('Reserved', digits='Product Unit of Measure', readonly=True,
                                help='Quantity reserved for other operations. Never proposed for transfer.')
    available_qty = fields.Float('Available', digits='Product Unit of Measure', readonly=True,
                                 help='On hand minus reserved, never negative.')
    demand_qty = fields.Float('Demand (look-back)', digits='Product Unit of Measure', readonly=True,
                              help='Done deliveries to customers from this warehouse during the '
                                   'look-back period, minus customer returns into it.')
    daily_demand = fields.Float('Daily Demand', readonly=True,
                                help='Demand (look-back) / look-back days.')
    coverage_days = fields.Float('Coverage (days)', readonly=True,
                                 help='Available / daily demand. Infinite when there is no demand.')
    coverage_infinite = fields.Boolean('No Demand', readonly=True)
    coverage_display = fields.Char('Coverage', compute='_compute_coverage_display')
    status = fields.Selection(STATUS_SELECTION, readonly=True, index=True, string='Status')
    target_qty = fields.Float('Target Stock', digits='Product Unit of Measure', readonly=True,
                              help='Daily demand x target coverage days.')
    protected_qty = fields.Float('Protected Stock', digits='Product Unit of Measure', readonly=True,
                                 help='Daily demand x donor minimum coverage days. A donor never '
                                      'goes below this quantity.')
    need_qty = fields.Float('Need', digits='Product Unit of Measure', readonly=True,
                            help='Quantity missing to reach the target coverage.')
    spare_qty = fields.Float('Can Spare', digits='Product Unit of Measure', readonly=True,
                             help='Available quantity above the protected stock.')
    incoming_qty = fields.Float('Recommended In', digits='Product Unit of Measure', readonly=True)
    outgoing_qty = fields.Float('Recommended Out', digits='Product Unit of Measure', readonly=True)
    available_after_qty = fields.Float('Available After', digits='Product Unit of Measure', readonly=True)
    coverage_after_days = fields.Float('Coverage After (days)', readonly=True)

    @api.depends('coverage_days', 'coverage_infinite')
    def _compute_coverage_display(self):
        for rec in self:
            rec.coverage_display = '∞' if rec.coverage_infinite else '%.1f' % rec.coverage_days

    # ------------------------------------------------------------ guards
    @api.model_create_multi
    def create(self, vals_list):
        _check_engine_only(self, _('created'))
        return super().create(vals_list)

    def write(self, vals):
        _check_engine_only(self, _('modified'))
        return super().write(vals)

    def unlink(self):
        _check_engine_only(self, _('deleted'))
        return super().unlink()

    def export_data(self, fields_to_export):
        """Export is a Pro capability (Lite edition limit, disclosed in the manifest)."""
        if not self.env['elaf.rebalance.edition']._elaf_rebalance_is_pro():
            raise UserError(_('Exporting rebalancing data is a Pro feature. The Lite edition '
                                       'shows all results on screen.'))
        return super().export_data(fields_to_export)
