import hashlib
import json
import math
from collections import defaultdict
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_round

from .edition import USAGE_KEY_RUNS

# A second analysis of the same run within this window is treated as a
# duplicate click: it returns without recomputing or consuming usage.
DUPLICATE_WINDOW_SECONDS = 5


def _fmt(value, digits=2):
    """Human number: at most `digits` decimals, no trailing zeros."""
    text = ('%.' + str(digits) + 'f') % (value or 0.0)
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text or '0'


class ElafRebalanceRun(models.Model):
    _name = 'elaf.rebalance.run'
    _description = 'Stock Rebalancing Run'
    _inherit = ['mail.thread']
    _order = 'id desc'
    _check_company_auto = True

    name = fields.Char(default=lambda self: self.env._('New'), readonly=True, copy=False,
                       required=True, tracking=True)
    company_id = fields.Many2one('res.company', required=True, index=True,
                                 default=lambda self: self.env.company, tracking=True)
    currency_id = fields.Many2one(related='company_id.currency_id', string='Currency')
    user_id = fields.Many2one('res.users', string='Planner', default=lambda self: self.env.user,
                              tracking=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('analyzed', 'Analyzed'),
        ('cancel', 'Cancelled'),
    ], default='draft', required=True, readonly=True, tracking=True, copy=False, string='Status')
    date_analysis = fields.Datetime('Analysed On', readonly=True, copy=False)
    warehouse_ids = fields.Many2many(
        'stock.warehouse', string='Warehouses', check_company=True,
        domain="[('company_id', '=', company_id)]",
        help='Warehouses to compare (at least 2). Lite: at most 2.')
    product_categ_ids = fields.Many2many('product.category', string='Product Categories',
                                         help='Limit the analysis to these categories (and their children).')
    product_ids = fields.Many2many('product.product', string='Products',
                                   domain="[('is_storable', '=', True)]",
                                   help='Limit the analysis to these products. Empty = every storable '
                                        'product with stock or demand in the selected warehouses.')
    lookback_days = fields.Integer('Demand Look-back (days)', default=90, required=True,
                                   help='Average daily demand = done customer deliveries over this '
                                        'many days / days.')
    target_coverage_days = fields.Float('Target Coverage (days)', default=30.0, required=True,
                                        help='A warehouse whose coverage is below this is in shortage. '
                                             'Receivers are filled up to this coverage.')
    donor_min_coverage_days = fields.Float(
        'Donor Minimum Coverage (days)', default=45.0, required=True,
        help='A donor never gives away stock that would take it below this coverage. '
             'A warehouse above donor minimum + target coverage is in excess.')
    whole_units = fields.Boolean('Whole Quantities', default=True,
                                 help='Round needs up and spare quantities down to whole numbers.')
    analysis_note = fields.Text('Analysis Notes', readonly=True, copy=False)
    analysis_fingerprint = fields.Char(
        'Analysis Fingerprint', readonly=True, copy=False,
        help='Digest of the parameters and stock data of the last analysis. Re-analysing an '
             'analysed run whose parameters and data are unchanged is free (S5-R3).')

    stat_ids = fields.One2many('elaf.rebalance.stat', 'run_id', string='Coverage', readonly=True)
    line_ids = fields.One2many('elaf.rebalance.line', 'run_id', string='Recommendation Lines', readonly=True)

    warehouse_count = fields.Integer(compute='_compute_counts', store=True, string='Warehouses Analysed')
    product_count = fields.Integer(compute='_compute_counts', store=True, string='Products Analysed')
    shortage_count = fields.Integer(compute='_compute_counts', store=True, string='Shortages')
    excess_count = fields.Integer(compute='_compute_counts', store=True, string='Excesses')
    line_count = fields.Integer(compute='_compute_counts', store=True, string='Recommendations')
    recommended_qty = fields.Float(compute='_compute_counts', store=True, string='Recommended Units',
                                   digits='Product Unit of Measure')
    avoided_procurement_value = fields.Monetary(
        compute='_compute_counts', store=True, string='Avoided Procurement (estimate)',
        help='ESTIMATE: sum over the recommendations of quantity x product cost.')
    hidden_line_count = fields.Integer('Additional Recommendations Found', readonly=True, copy=False,
                                       help='Recommendations found beyond the Lite limit of 10 per run.')
    hidden_line_qty = fields.Float('Additional Units Found', readonly=True, copy=False,
                                   digits='Product Unit of Measure')
    hidden_avoided_value = fields.Monetary('Additional Avoided Procurement (estimate)', readonly=True,
                                           copy=False)

    _sql_constraints = [
        ('lookback_positive', 'CHECK(lookback_days > 0)', 'The look-back must be at least one day.'),
        ('target_positive', 'CHECK(target_coverage_days > 0)', 'The target coverage must be positive.'),
    ]

    # ----------------------------------------------------------------- ORM
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == self.env._('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('elaf.rebalance.run') or '/'
        return super().create(vals_list)

    @api.depends('stat_ids.status', 'line_ids.quantity', 'line_ids.avoided_procurement_value')
    def _compute_counts(self):
        for run in self:
            stats = run.stat_ids
            run.warehouse_count = len(stats.warehouse_id)
            run.product_count = len(stats.product_id)
            run.shortage_count = len(stats.filtered(lambda s: s.status == 'shortage'))
            run.excess_count = len(stats.filtered(lambda s: s.status == 'excess'))
            run.line_count = len(run.line_ids)
            run.recommended_qty = sum(run.line_ids.mapped('quantity'))
            run.avoided_procurement_value = sum(run.line_ids.mapped('avoided_procurement_value'))

    @api.constrains('target_coverage_days', 'donor_min_coverage_days')
    def _check_coverage_params(self):
        for run in self:
            if run.donor_min_coverage_days < run.target_coverage_days:
                raise ValidationError(self.env._(
                    'The donor minimum coverage (%(donor)s days) must be at least the target '
                    'coverage (%(target)s days); otherwise stock would move back and forth.',
                    donor=_fmt(run.donor_min_coverage_days), target=_fmt(run.target_coverage_days)))

    @api.constrains('warehouse_ids')
    def _check_warehouse_limit(self):
        limits = self.env['elaf.rebalance.edition']._elaf_rebalance_limits()
        for run in self:
            if limits['max_warehouses'] and len(run.warehouse_ids) > limits['max_warehouses']:
                raise ValidationError(self.env._(
                    'The Lite edition compares up to %(max)s warehouses per run. Comparing more '
                    'warehouses in one run is a Pro feature.', max=limits['max_warehouses']))
            if run.warehouse_ids.filtered(lambda w: w.company_id != run.company_id):
                raise ValidationError(self.env._(
                    'All warehouses must belong to the company of the run (%(company)s).',
                    company=run.company_id.name))

    def export_data(self, fields_to_export):
        """Export is a Pro capability (Lite edition limit, disclosed in the manifest)."""
        if not self.env['elaf.rebalance.edition']._elaf_rebalance_is_pro():
            raise UserError(self.env._('Exporting rebalancing data is a Pro feature. The Lite edition '
                                       'shows all results on screen.'))
        return super().export_data(fields_to_export)

    # ------------------------------------------------------------- actions
    def action_cancel(self):
        self.write({'state': 'cancel'})

    def action_reset_draft(self):
        self.write({'state': 'draft'})

    def action_analyze(self):
        """Run the analysis. Any user of the Stock Rebalancing group may do it."""
        notifications = []
        for run in self:
            notifications.append(run.with_company(run.company_id)._analyze_one())
        if len(self) == 1 and notifications[0]:
            return notifications[0]
        return True

    def _analyze_one(self):
        self.ensure_one()
        if self.state == 'cancel':
            raise UserError(self.env._('A cancelled run cannot be analysed. Reset it to draft first.'))
        # Lock the run first: a double click or a concurrent request waits here,
        # then sees the fresh result and does not analyse (nor consume) twice.
        self.env.cr.execute('SELECT id FROM elaf_rebalance_run WHERE id = %s FOR UPDATE', (self.id,))
        self.invalidate_recordset(['state', 'date_analysis'])
        if self.state == 'analyzed' and self.date_analysis and \
                (fields.Datetime.now() - self.date_analysis).total_seconds() < DUPLICATE_WINDOW_SECONDS:
            return False
        # Only someone who may modify this run (owner, or a manager) analyses it:
        # results are then written with sudo() below.
        self.check_access('write')
        Usage = self.env['elaf.rebalance.usage']
        Usage._check_company(self.company_id)
        # Re-analysing an analysed run can be free (see below): its allowance is
        # checked once we know whether parameters or data changed.
        if self.state != 'analyzed':
            Usage._check_available(USAGE_KEY_RUNS)
        self._check_before_analysis()

        loc_map = self._get_scope_locations()
        products, quant_data, demand_data = self._get_scope_data(loc_map)
        self._check_product_limit(products)
        stats = self._compute_stats(products, quant_data, demand_data)
        candidates = self._compute_recommendations(products, stats)
        candidates = self._postprocess_candidates(candidates, stats)
        skipped = [c for c in candidates if c.get('_skipped')]
        candidates = [c for c in candidates if not c.get('_skipped')]
        kept, hidden = self._apply_recommendation_limit(candidates)
        self._apply_after_values(stats, kept)

        # S5-R3: an analysed run re-analysed with the same parameters and the same
        # stock data gives the same result: it is refreshed but not counted.
        fingerprint = self._analysis_fingerprint(products, stats, kept, skipped, hidden)
        unchanged = bool(self.state == 'analyzed' and self.analysis_fingerprint
                         and self.analysis_fingerprint == fingerprint)
        if self.state == 'analyzed' and not unchanged:
            Usage._check_available(USAGE_KEY_RUNS)

        self._clear_results()
        # sudo: coverage rows and recommendations are engine output, read-only for
        # every user (ACL read only + create/write/unlink guards); the caller's
        # right to modify this run was checked above.
        self.env['elaf.rebalance.stat'].sudo().create(
            [self._prepare_stat_create_vals(row) for row in stats.values()])
        for seq, vals in enumerate(kept, start=1):
            vals['sequence'] = seq
        self.env['elaf.rebalance.line'].sudo().create(
            [self._prepare_line_create_vals(v) for v in kept + self._prepare_skipped_lines(skipped)])
        self.write({
            'state': 'analyzed',
            'date_analysis': fields.Datetime.now(),
            'hidden_line_count': hidden['count'],
            'hidden_line_qty': hidden['qty'],
            'hidden_avoided_value': hidden['value'],
            'analysis_note': '\n'.join(self._analysis_notes(products, stats, kept, hidden)) or False,
            'analysis_fingerprint': fingerprint,
        })
        if unchanged:
            Usage._log_unchanged(USAGE_KEY_RUNS, self)
            return self._unchanged_notification()
        # Consume one completed run in this same transaction: if anything above
        # failed we never get here, and if this fails everything rolls back.
        used = Usage._consume(USAGE_KEY_RUNS, self)
        return self._milestone_notification(used)

    # ------------------------------------------------------- fingerprint
    def _fingerprint_params(self):
        """Parameters that change the result of an analysis (extended by Pro)."""
        self.ensure_one()
        return {
            'company': self.company_id.id,
            'warehouses': sorted(self.warehouse_ids.ids),
            'products': sorted(self.product_ids.ids),
            'categories': sorted(self.product_categ_ids.ids),
            'lookback': self.lookback_days,
            'target': self.target_coverage_days,
            'donor_min': self.donor_min_coverage_days,
            'whole_units': self.whole_units,
        }

    @staticmethod
    def _fingerprint_value(value):
        if isinstance(value, float):
            return round(value, 6)
        if isinstance(value, models.BaseModel):
            return value.ids
        if isinstance(value, (list, tuple)):
            return [ElafRebalanceRun._fingerprint_value(v) for v in value]
        if isinstance(value, dict):
            return {str(k): ElafRebalanceRun._fingerprint_value(v) for k, v in sorted(value.items())}
        if hasattr(value, 'isoformat'):
            return value.isoformat()
        return value

    def _fingerprint_line(self, vals):
        """Numeric content of one recommendation (language-independent)."""
        return {k: self._fingerprint_value(v) for k, v in sorted(vals.items())
                if k not in ('explanation', 'lot_note', 'skip_reason', '_skipped', '_uom', 'sequence')}

    def _analysis_fingerprint(self, products, stats, kept, skipped, hidden):
        """sha256 of the parameters and the data the analysis used.

        Stock data enter through the coverage rows (on hand, reserved, demand,
        open transfers) and the recommendations (quantities, lots, costs).
        Translated texts and dates of the analysis are left out, so the digest
        does not depend on the user's language or on the time of the click.
        """
        self.ensure_one()
        stat_keys = ('on_hand_qty', 'reserved_qty', 'available_qty', 'demand_qty',
                     'pending_in_qty', 'pending_out_qty', 'status', 'need_qty', 'spare_qty')
        payload = {
            'params': self._fingerprint_value(self._fingerprint_params()),
            'products': sorted(products.ids),
            'stats': sorted([[key[0], key[1]] + [self._fingerprint_value(row.get(k)) for k in stat_keys]
                             for key, row in stats.items()]),
            'lines': [self._fingerprint_line(v) for v in kept],
            'skipped': [self._fingerprint_line(v) for v in skipped],
            'hidden': self._fingerprint_value(hidden),
        }
        data = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(data.encode()).hexdigest()

    def _unchanged_notification(self):
        status = self.env['elaf.rebalance.usage']._get_status(USAGE_KEY_RUNS)
        if status['capped']:
            message = self.env._('Parameters and stock data are unchanged since the last analysis: the '
                                 'results were refreshed and no Lite analysis was used (%(used)s of '
                                 '%(limit)s used).', used=status['used'], limit=status['limit'])
        else:
            message = self.env._('Parameters and stock data are unchanged since the last analysis: the '
                                 'results were refreshed.')
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': self.env._('Analysis unchanged'),
                'message': message,
                'type': 'info',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def _check_before_analysis(self):
        self.ensure_one()
        if len(self.warehouse_ids) < 2:
            raise UserError(self.env._('Select at least two warehouses to compare.'))
        self._check_warehouse_limit()
        missing = self.warehouse_ids.filtered(lambda w: not w.lot_stock_id)
        if missing:
            raise UserError(self.env._('Warehouse %(wh)s has no stock location.', wh=missing[0].name))

    def _check_product_limit(self, products):
        limits = self.env['elaf.rebalance.edition']._elaf_rebalance_limits()
        if limits['max_products'] and len(products) > limits['max_products']:
            raise UserError(self.env._(
                'This run covers %(count)s products; the Lite edition analyses up to %(max)s '
                'products per run. Narrow the product or category filter, or use the Pro edition '
                'for unlimited products.', count=len(products), max=limits['max_products']))
        if not products:
            raise UserError(self.env._(
                'No storable product has stock or customer demand in the selected warehouses '
                'for this filter and look-back period.'))

    def _clear_results(self):
        # sudo: engine-only results (see _analyze_one, which checked write access on the run)
        self.check_access('write')
        self.line_ids.sudo().unlink()
        self.stat_ids.sudo().unlink()

    def _milestone_notification(self, used):
        status = self.env['elaf.rebalance.usage']._get_status(USAGE_KEY_RUNS)
        if not status['capped']:
            return False
        message = False
        if used == 1:
            message = self.env._(
                'First analysis completed. The Lite edition includes %(limit)s analyses; '
                '%(remaining)s remaining.', limit=status['limit'], remaining=status['remaining'])
        elif status['percent'] >= 80:
            message = self.env._(
                '%(used)s of %(limit)s Lite analyses used. Past runs always stay available; the Pro '
                'edition removes the limit.', used=status['used'], limit=status['limit'])
        if not message:
            return False
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': self.env._('Analysis completed'),
                'message': message,
                'type': 'info',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    # ----------------------------------------------------- data gathering
    def _get_scope_locations(self):
        """Return {warehouse_id: internal stock.location recordset} analysed per warehouse.

        Every internal location below the warehouse stock location is aggregated.
        """
        self.ensure_one()
        Location = self.env['stock.location']
        return {
            wh.id: Location.search([('id', 'child_of', wh.lot_stock_id.id), ('usage', '=', 'internal')])
            for wh in self.warehouse_ids
        }

    def _get_product_domain(self):
        self.ensure_one()
        domain = [('is_storable', '=', True), ('company_id', 'in', [False, self.company_id.id])]
        if self.product_ids:
            domain.append(('id', 'in', self.product_ids.ids))
        if self.product_categ_ids:
            domain.append(('categ_id', 'child_of', self.product_categ_ids.ids))
        return domain

    def _get_scope_data(self, loc_map):
        """Return (products, quant_data, demand_data).

        quant_data: {(product_id, warehouse_id): (on_hand, reserved)}
        demand_data: {(product_id, warehouse_id): net customer demand over the look-back}
        """
        self.ensure_one()
        candidates = self.env['product.product'].search(self._get_product_domain())
        loc_to_wh = {loc.id: wh_id for wh_id, locs in loc_map.items() for loc in locs}
        quant_data = defaultdict(lambda: [0.0, 0.0])
        if candidates and loc_to_wh:
            groups = self.env['stock.quant']._read_group(
                [('product_id', 'in', candidates.ids), ('location_id', 'in', list(loc_to_wh))],
                ['product_id', 'location_id'], ['quantity:sum', 'reserved_quantity:sum'])
            for product, location, qty, reserved in groups:
                key = (product.id, loc_to_wh[location.id])
                quant_data[key][0] += qty
                quant_data[key][1] += reserved
        demand_data = self._get_demand_data(candidates)
        if self.product_ids:
            products = candidates
        else:
            relevant = {k[0] for k, v in quant_data.items() if v[0] or v[1]}
            relevant |= {k[0] for k, v in demand_data.items() if v}
            products = candidates.filtered(lambda p: p.id in relevant)
        return products, quant_data, demand_data

    def _get_demand_date_from(self):
        return fields.Datetime.now() - timedelta(days=self.lookback_days)

    def _get_demand_data(self, products):
        """Net customer demand per (product, warehouse) over the look-back period.

        Demand = done moves from a location of the warehouse to a customer
        location, minus done customer returns into the warehouse, never below 0.
        Quantities are in the product's unit (stock.move.product_qty).
        """
        self.ensure_one()
        result = defaultdict(float)
        if not products:
            return result
        Move = self.env['stock.move']
        wh_ids = self.warehouse_ids.ids
        base = [
            ('state', '=', 'done'),
            ('product_id', 'in', products.ids),
            ('date', '>=', self._get_demand_date_from()),
            ('company_id', '=', self.company_id.id),
        ]
        out_groups = Move._read_group(
            base + [('location_dest_id.usage', '=', 'customer'),
                    ('location_id.warehouse_id', 'in', wh_ids)],
            ['product_id', 'location_id'], ['product_qty:sum'])
        for product, location, qty in out_groups:
            result[(product.id, location.warehouse_id.id)] += qty
        ret_groups = Move._read_group(
            base + [('location_id.usage', '=', 'customer'),
                    ('location_dest_id.warehouse_id', 'in', wh_ids)],
            ['product_id', 'location_dest_id'], ['product_qty:sum'])
        for product, location, qty in ret_groups:
            result[(product.id, location.warehouse_id.id)] -= qty
        for key in list(result):
            result[key] = max(0.0, result[key])
        return result

    # ----------------------------------------------------------- rounding
    def _uom_round(self, qty, rounding_method):
        """Round to the 'Product Unit of Measure' decimal precision (as uom.round on 19.0)."""
        digits = self.env['decimal.precision'].precision_get('Product Unit of Measure')
        return float_round(qty, precision_digits=digits, rounding_method=rounding_method)

    def _round_up(self, qty, product):
        if qty <= 0:
            return 0.0
        qty = round(qty, 6)
        if self.whole_units:
            qty = math.ceil(qty)
        return self._uom_round(qty, 'UP')

    def _round_down(self, qty, product):
        if qty <= 0:
            return 0.0
        qty = round(qty, 6)
        if self.whole_units:
            qty = math.floor(qty)
        return self._uom_round(qty, 'DOWN')

    # -------------------------------------------------------------- stats
    def _compute_stats(self, products, quant_data, demand_data):
        """Return {(product_id, warehouse_id): row dict} for every product x warehouse."""
        self.ensure_one()
        stats = {}
        lookback = float(self.lookback_days)
        for product in products:
            for wh in self.warehouse_ids:
                key = (product.id, wh.id)
                on_hand, reserved = quant_data.get(key, (0.0, 0.0))
                demand = demand_data.get(key, 0.0)
                stats[key] = self._compute_stat_row(product, wh, on_hand, reserved, demand, lookback)
        return stats

    def _compute_stat_row(self, product, wh, on_hand, reserved, demand, lookback):
        row = {
            'product': product,
            'warehouse': wh,
            'on_hand_qty': on_hand,
            'reserved_qty': reserved,
            'available_qty': max(0.0, on_hand - reserved),
            'demand_qty': demand,
            'daily_demand': demand / lookback if lookback else 0.0,
            'incoming_qty': 0.0,
            'outgoing_qty': 0.0,
        }
        return self._finish_stat_row(row)

    def _finish_stat_row(self, row):
        """Coverage, status, need and spare from row['available_qty'] and row['daily_demand']."""
        product = row['product']
        available = row['available_qty']
        daily = row['daily_demand']
        target = self.target_coverage_days
        donor_min = self.donor_min_coverage_days
        if daily > 0:
            coverage = available / daily
            infinite = False
            if coverage < target:
                status = 'shortage'
            elif coverage > donor_min + target:
                status = 'excess'
            else:
                status = 'balanced'
        else:
            coverage = 0.0
            infinite = True
            status = 'excess' if available > 0 else 'inactive'
        target_qty = daily * target
        protected = daily * donor_min
        row.update({
            'coverage_days': coverage,
            'coverage_infinite': infinite,
            'status': status,
            'target_qty': target_qty,
            'protected_qty': protected,
            'need_qty': self._round_up(target_qty - available, product) if status == 'shortage' else 0.0,
            'spare_qty': self._round_down(available - protected, product) if available > protected else 0.0,
        })
        return row

    def _prepare_stat_create_vals(self, row):
        return {
            'run_id': self.id,
            'product_id': row['product'].id,
            'warehouse_id': row['warehouse'].id,
            'on_hand_qty': row['on_hand_qty'],
            'reserved_qty': row['reserved_qty'],
            'available_qty': row['available_qty'],
            'demand_qty': row['demand_qty'],
            'daily_demand': row['daily_demand'],
            'coverage_days': row['coverage_days'],
            'coverage_infinite': row['coverage_infinite'],
            'status': row['status'],
            'target_qty': row['target_qty'],
            'protected_qty': row['protected_qty'],
            'need_qty': row['need_qty'],
            'spare_qty': row['spare_qty'],
            'incoming_qty': row['incoming_qty'],
            'outgoing_qty': row['outgoing_qty'],
            'available_after_qty': row.get('available_after_qty', row['available_qty']),
            'coverage_after_days': row.get('coverage_after_days', row['coverage_days']),
        }

    # ---------------------------------------------------- recommendations
    def _get_receivers(self, rows):
        receivers = [r for r in rows if r['status'] == 'shortage' and r['need_qty'] > 0]
        return sorted(receivers, key=lambda r: (r['coverage_days'], r['warehouse'].name, r['warehouse'].id))

    def _order_donors(self, product, receiver, donors):
        """Most comfortable donors first: no demand, then highest coverage."""
        return sorted(donors, key=lambda d: (
            0 if d['coverage_infinite'] else 1, -d['coverage_days'], -d['spare_qty'], d['warehouse'].id))

    def _compute_line_qty(self, product, need, spare, receiver, donor, ctx):
        """Quantity proposed for one donor -> receiver pair.

        Returns a dict: qty (never above spare), note (why it is 0, for
        skipped candidates) and extra (additional line values). Lite rounds
        min(need, spare) down to the unit precision; Pro adds lots, pack size
        and minimum quantity.
        """
        return {'qty': self._round_down(min(need, spare), product), 'note': '', 'extra': {}}

    def _allocate(self, product, receivers, donors, remaining_spare, ctx, reason='shortage', fill_key='need_qty'):
        """Greedy allocation: most urgent receiver first, most comfortable donor first."""
        candidates = []
        for receiver in receivers:
            need = receiver[fill_key]
            for donor in self._order_donors(product, receiver, donors):
                if need <= 0:
                    break
                if donor['warehouse'] == receiver['warehouse']:
                    continue
                spare = remaining_spare[donor['warehouse'].id]
                if spare <= 0:
                    continue
                res = self._compute_line_qty(product, need, spare, receiver, donor, ctx)
                qty = min(res['qty'], spare)
                if qty <= 0:
                    if res['note']:
                        candidates.append({
                            '_skipped': res['note'], 'product_id': product.id,
                            'donor_warehouse_id': donor['warehouse'].id,
                            'receiver_warehouse_id': receiver['warehouse'].id,
                            'reason': reason, 'receiver_need_qty': need, 'donor_spare_qty': spare,
                        })
                    continue
                vals = self._prepare_line_vals(product, donor, receiver, qty, need, spare, reason=reason)
                vals.update(res['extra'])
                if res['note']:
                    vals['explanation'] += ' ' + res['note']
                candidates.append(vals)
                need -= qty
                remaining_spare[donor['warehouse'].id] = spare - qty
        return candidates

    def _compute_recommendations(self, products, stats, ctx=None):
        """Shortage pass. `ctx` carries allocation state (remaining spare per
        product and donor) so that extensions can run further passes."""
        self.ensure_one()
        candidates = []
        ctx = {} if ctx is None else ctx
        ctx.setdefault('remaining_spare', {})
        for product in products:
            rows = [stats[(product.id, wh.id)] for wh in self.warehouse_ids]
            donors = [r for r in rows if r['spare_qty'] > 0 and r['status'] != 'shortage']
            remaining_spare = {d['warehouse'].id: d['spare_qty'] for d in donors}
            ctx['remaining_spare'][product.id] = remaining_spare
            candidates += self._allocate(product, self._get_receivers(rows), donors, remaining_spare, ctx)
        return candidates

    def _prepare_skipped_lines(self, skipped):
        """Hook: Lite does not store skipped candidates."""
        return []

    def _postprocess_candidates(self, candidates, stats):
        """Hook for constraints (Pro)."""
        return candidates

    def _get_unit_cost(self, product):
        return product.with_company(self.company_id).standard_price

    def _prepare_line_vals(self, product, donor, receiver, qty, need, spare, reason='shortage'):
        uom = product.uom_id.name
        cost = self._get_unit_cost(product)
        r_daily = receiver['daily_demand']
        d_daily = donor['daily_demand']
        r_after = (receiver['available_qty'] + qty) / r_daily if r_daily else 0.0
        explanation = self._build_explanation(product, donor, receiver, qty, need, spare)
        return {
            'product_id': product.id,
            'donor_warehouse_id': donor['warehouse'].id,
            'receiver_warehouse_id': receiver['warehouse'].id,
            'quantity': qty,
            'reason': reason,
            'explanation': explanation,
            'receiver_available_qty': receiver['available_qty'],
            'receiver_daily_demand': r_daily,
            'receiver_coverage_days': receiver['coverage_days'],
            'receiver_need_qty': need,
            'receiver_coverage_after_days': r_after,
            'donor_available_qty': donor['available_qty'],
            'donor_reserved_qty': donor['reserved_qty'],
            'donor_daily_demand': d_daily,
            'donor_coverage_days': donor['coverage_days'],
            'donor_coverage_infinite': donor['coverage_infinite'],
            'donor_protected_qty': donor['protected_qty'],
            'donor_spare_qty': spare,
            'donor_coverage_after_days': 0.0,  # filled in _apply_after_values
            'unit_cost': cost,
            'avoided_procurement_value': qty * cost,
            '_uom': uom,
        }

    def _build_explanation(self, product, donor, receiver, qty, need, spare):
        _ = self.env._
        uom = product.uom_id.name
        rwh, dwh = receiver['warehouse'].name, donor['warehouse'].name
        parts = []
        full_need = receiver['need_qty']
        if abs(full_need - need) > 1e-6:
            parts.append(_(
                '%(rwh)s still needs %(need)s %(uom)s (of %(full)s) to reach %(target)s days of cover '
                '(available %(avail)s, demand %(daily)s/day, coverage %(cov)s days).',
                rwh=rwh, need=_fmt(need), full=_fmt(full_need), uom=uom,
                target=_fmt(self.target_coverage_days), avail=_fmt(receiver['available_qty']),
                daily=_fmt(receiver['daily_demand'], 2), cov=_fmt(receiver['coverage_days'], 1)))
        else:
            parts.append(_(
                '%(rwh)s needs %(need)s %(uom)s to reach %(target)s days of cover '
                '(available %(avail)s, demand %(daily)s/day, coverage %(cov)s days).',
                rwh=rwh, need=_fmt(need), uom=uom, target=_fmt(self.target_coverage_days),
                avail=_fmt(receiver['available_qty']), daily=_fmt(receiver['daily_demand'], 2),
                cov=_fmt(receiver['coverage_days'], 1)))
        if donor['coverage_infinite']:
            parts.append(_(
                '%(dwh)s has no customer demand in the last %(days)s days, so it can spare %(spare)s '
                '%(uom)s (available %(avail)s after excluding %(res)s reserved).',
                dwh=dwh, days=self.lookback_days, spare=_fmt(spare), uom=uom,
                avail=_fmt(donor['available_qty']), res=_fmt(donor['reserved_qty'])))
        else:
            parts.append(_(
                '%(dwh)s can spare %(spare)s %(uom)s while keeping %(min)s days of cover '
                '(available %(avail)s after excluding %(res)s reserved, demand %(daily)s/day, '
                'protected %(prot)s).',
                dwh=dwh, spare=_fmt(spare), uom=uom, min=_fmt(self.donor_min_coverage_days),
                avail=_fmt(donor['available_qty']), res=_fmt(donor['reserved_qty']),
                daily=_fmt(donor['daily_demand'], 2), prot=_fmt(donor['protected_qty'])))
        raw = min(need, spare)
        if abs(raw - qty) > 1e-6:
            parts.append(_('Recommended = min(%(need)s, %(spare)s) = %(raw)s, rounded to %(qty)s %(uom)s.',
                           need=_fmt(need), spare=_fmt(spare), raw=_fmt(raw), qty=_fmt(qty), uom=uom))
        else:
            parts.append(_('Recommended = min(%(need)s, %(spare)s) = %(qty)s %(uom)s.',
                           need=_fmt(need), spare=_fmt(spare), qty=_fmt(qty), uom=uom))
        return ' '.join(parts)

    def _impact_key(self, vals):
        return (-vals.get('avoided_procurement_value', 0.0), -vals['quantity'])

    def _apply_recommendation_limit(self, candidates):
        """Lite shows the top N recommendations by impact; Pro shows all."""
        limits = self.env['elaf.rebalance.edition']._elaf_rebalance_limits()
        ordered = sorted(candidates, key=self._impact_key)
        max_lines = limits['max_recommendations']
        hidden = {'count': 0, 'qty': 0.0, 'value': 0.0}
        if max_lines and len(ordered) > max_lines:
            rest = ordered[max_lines:]
            ordered = ordered[:max_lines]
            hidden = {
                'count': len(rest),
                'qty': sum(v['quantity'] for v in rest),
                'value': sum(v.get('avoided_procurement_value', 0.0) for v in rest),
            }
        return ordered, hidden

    def _apply_after_values(self, stats, lines):
        """Fill incoming/outgoing and after-transfer coverage from the kept lines."""
        for vals in lines:
            stats[(vals['product_id'], vals['receiver_warehouse_id'])]['incoming_qty'] += vals['quantity']
            stats[(vals['product_id'], vals['donor_warehouse_id'])]['outgoing_qty'] += vals['quantity']
        for row in stats.values():
            after = row['available_qty'] + row['incoming_qty'] - row['outgoing_qty']
            row['available_after_qty'] = after
            row['coverage_after_days'] = after / row['daily_demand'] if row['daily_demand'] else 0.0
        for vals in lines:
            donor = stats[(vals['product_id'], vals['donor_warehouse_id'])]
            vals['donor_coverage_after_days'] = donor['coverage_after_days']

    def _prepare_line_create_vals(self, vals):
        clean = {k: v for k, v in vals.items() if not k.startswith('_')}
        clean['run_id'] = self.id
        return clean

    def _analysis_notes(self, products, stats, kept, hidden):
        _ = self.env._
        notes = [_('%(p)s products x %(w)s warehouses analysed; demand look-back since %(d)s.',
                   p=len(products), w=len(self.warehouse_ids),
                   d=fields.Date.to_string(self._get_demand_date_from().date()))]
        if not kept:
            shortages = sum(1 for r in stats.values() if r['status'] == 'shortage')
            if shortages:
                notes.append(_('%(n)s shortages found, but no warehouse can spare stock without '
                               'falling below its minimum coverage.', n=shortages))
            else:
                notes.append(_('No shortage found: no transfer is needed.'))
        if hidden['count']:
            notes.append(_('%(n)s additional recommendations (%(q)s units) were found beyond the '
                           'Lite limit of 10 shown per run.', n=hidden['count'], q=_fmt(hidden['qty'])))
        return notes

    # -------------------------------------------------------- navigation
    def action_view_stats(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'elaf_stock_rebalance_lite.action_elaf_rebalance_stat')
        action['domain'] = [('run_id', '=', self.id)]
        action['context'] = {'search_default_group_product': 0}
        return action

    def action_view_lines(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'elaf_stock_rebalance_lite.action_elaf_rebalance_line')
        action['domain'] = [('run_id', '=', self.id)]
        action['context'] = {}
        return action

    def action_view_shortages(self):
        action = self.action_view_stats()
        action['domain'] = [('run_id', '=', self.id), ('status', '=', 'shortage')]
        return action

    def action_view_excesses(self):
        action = self.action_view_stats()
        action['domain'] = [('run_id', '=', self.id), ('status', '=', 'excess')]
        return action
