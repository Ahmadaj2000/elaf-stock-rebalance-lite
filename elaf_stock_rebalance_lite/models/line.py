from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError


def _check_engine_only(records, operation):
    """Analysis results are written by the engine only (RB-07/RB-08/RB-20/RB-21).

    The engine creates and clears lines and coverage rows with ``sudo()`` after
    checking that the caller may modify the run; every other create, write or
    unlink (RPC, UI, any user incl. managers and Settings administrators) is
    refused. The ACLs grant read only, this is the second line of defence.
    """
    if records.env.su:
        return
    raise AccessError(records.env._(
        '%(model)s rows are results of the analysis and cannot be %(operation)s manually. '
        'Re-run the analysis to refresh them.',
        model=records._description, operation=operation))


class ElafRebalanceLine(models.Model):
    """A transfer recommendation from a donor warehouse to a receiver warehouse."""

    _name = 'elaf.rebalance.line'
    _description = 'Stock Rebalancing Recommendation'

    @api.depends('product_id', 'donor_warehouse_id', 'receiver_warehouse_id')
    def _compute_display_name(self):
        for line in self:
            line.display_name = '%s: %s → %s' % (
                line.product_id.display_name or '',
                line.donor_warehouse_id.code or '',
                line.receiver_warehouse_id.code or '',
            )
    _order = 'run_id desc, sequence, id'
    _check_company_auto = True

    run_id = fields.Many2one('elaf.rebalance.run', required=True, ondelete='cascade',
                             index=True, readonly=True, string='Run')
    company_id = fields.Many2one(related='run_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='run_id.currency_id')
    sequence = fields.Integer(default=10, readonly=True)
    product_id = fields.Many2one('product.product', required=True, readonly=True, string='Product')
    uom_id = fields.Many2one(related='product_id.uom_id', string='Unit')
    donor_warehouse_id = fields.Many2one('stock.warehouse', required=True, readonly=True,
                                         string='From (Donor)')
    receiver_warehouse_id = fields.Many2one('stock.warehouse', required=True, readonly=True,
                                            string='To (Receiver)')
    quantity = fields.Float('Recommended Qty', digits='Product Unit', readonly=True)
    reason = fields.Selection([('shortage', 'Receiver shortage')], default='shortage',
                              required=True, readonly=True)
    state = fields.Selection([('proposed', 'Proposed')], default='proposed', required=True,
                             readonly=True, string='Status')
    explanation = fields.Text(readonly=True)

    receiver_available_qty = fields.Float('Receiver Available', digits='Product Unit', readonly=True)
    receiver_daily_demand = fields.Float('Receiver Daily Demand', readonly=True)
    receiver_coverage_days = fields.Float('Receiver Coverage', readonly=True)
    receiver_need_qty = fields.Float('Receiver Need', digits='Product Unit', readonly=True,
                                     help='Quantity still needed by the receiver when this line was '
                                          'computed (after earlier lines for the same receiver).')
    receiver_coverage_after_days = fields.Float('Receiver Coverage After', readonly=True)
    donor_available_qty = fields.Float('Donor Available', digits='Product Unit', readonly=True)
    donor_reserved_qty = fields.Float('Donor Reserved', digits='Product Unit', readonly=True)
    donor_daily_demand = fields.Float('Donor Daily Demand', readonly=True)
    donor_coverage_days = fields.Float('Donor Coverage', readonly=True)
    donor_coverage_infinite = fields.Boolean('Donor Has No Demand', readonly=True)
    donor_protected_qty = fields.Float('Donor Protected', digits='Product Unit', readonly=True)
    donor_spare_qty = fields.Float('Donor Can Spare', digits='Product Unit', readonly=True,
                                   help='Quantity the donor could still spare when this line was computed.')
    donor_coverage_after_days = fields.Float('Donor Coverage After', readonly=True)

    unit_cost = fields.Monetary('Unit Cost', readonly=True,
                                help="Product cost (standard price) in the company's currency at analysis time.")
    avoided_procurement_value = fields.Monetary(
        'Avoided Procurement (estimate)', readonly=True,
        help='ESTIMATE = recommended quantity x product cost: the purchase value the receiver '
             'would otherwise need to buy to reach its target coverage.')

    # ------------------------------------------------------------ guards
    @api.model_create_multi
    def create(self, vals_list):
        _check_engine_only(self, self.env._('created'))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            # Report invalid selection values (e.g. a state that does not exist in
            # this edition) as such before refusing the write itself.
            for name, value in vals.items():
                field = self._fields.get(name)
                if field is not None and field.type == 'selection':
                    field.convert_to_cache(value, self)
        _check_engine_only(self, self.env._('modified'))
        return super().write(vals)

    def unlink(self):
        _check_engine_only(self, self.env._('deleted'))
        return super().unlink()

    def export_data(self, fields_to_export):
        """Export is a Pro capability (Lite edition limit, disclosed in the manifest)."""
        if not self.env['elaf.rebalance.edition']._elaf_rebalance_is_pro():
            raise UserError(self.env._('Exporting rebalancing data is a Pro feature. The Lite edition '
                                       'shows all results on screen.'))
        return super().export_data(fields_to_export)
