from odoo import _, api, models
from odoo.exceptions import AccessError

from .edition import USAGE_KEY_RUNS

SUPPORT_EMAIL = 'info@elaf-development.com'
HEAT_MAX_PRODUCTS = 60


class ElafRebalanceRunDashboard(models.Model):
    _inherit = 'elaf.rebalance.run'

    # ------------------------------------------------------------ helpers
    @api.model
    def _dashboard_check_access(self):
        if not self.env.user.has_group('elaf_stock_rebalance_lite.group_rebalance_user'):
            raise AccessError(_('You need the Stock Rebalancing user access to view this page.'))

    @api.model
    def _act(self, name, model, domain, context=None, views=None):
        return {
            'type': 'ir.actions.act_window',
            'name': name,
            'res_model': model,
            'domain': domain,
            'views': views or [[False, 'tree'], [False, 'form']],
            'context': context or {},
            'target': 'current',
        }

    @api.model
    def _currency_info(self, currency):
        return {'symbol': currency.symbol, 'position': currency.position,
                'digits': currency.decimal_places}

    # ---------------------------------------------------------- dashboard
    @api.model
    def get_dashboard_data(self, filters=None):
        """Plain JSON for the OWL dashboard. Every figure comes from stored run data."""
        self._dashboard_check_access()
        filters = filters or {}
        Edition = self.env['elaf.rebalance.edition']
        limits = Edition._elaf_rebalance_limits()
        usage = self.env['elaf.rebalance.usage']._get_status(USAGE_KEY_RUNS)
        runs = self.search([('state', '=', 'analyzed')], limit=30)
        run = self.browse()
        if filters.get('run_id'):
            run = self.search([('id', '=', int(filters['run_id'])), ('state', '=', 'analyzed')], limit=1)
        if not run:
            run = runs[:1]
        data = {
            'is_pro': limits['is_pro'],
            'limits': limits,
            'usage': usage,
            'support_email': SUPPORT_EMAIL,
            'is_manager': self.env.user.has_group('elaf_stock_rebalance_lite.group_rebalance_manager'),
            'runs': [{'id': r.id, 'name': r.name,
                      'date': r.date_analysis and r.date_analysis.isoformat() or False,
                      'warehouses': ', '.join(r.warehouse_ids.mapped('name'))} for r in runs],
            'draft_count': self.search_count([('state', '=', 'draft')]),
            'run': False,
            'kpis': [],
            'heat': False,
            'recommendations': [],
            'banner': self._dashboard_banner(usage, limits),
            'locked_features': [] if limits['is_pro'] else self._locked_features(),
        }
        if run:
            data.update(run._dashboard_run_data())
        return data

    @api.model
    def _dashboard_banner(self, usage, limits):
        if limits['is_pro'] or not usage['capped']:
            return False
        if usage['exhausted']:
            return {'level': 'bad', 'key': 'exhausted',
                    'text': _('Lite analysis allowance used (%(used)s/%(limit)s). Your runs and '
                              'recommendations stay available. The Pro edition removes the limit.',
                              used=usage['used'], limit=usage['limit'])}
        if usage['percent'] >= 80:
            return {'level': 'warn', 'key': 'high',
                    'text': _('%(used)s of %(limit)s Lite analyses used; %(rem)s remaining.',
                              used=usage['used'], limit=usage['limit'], rem=usage['remaining'])}
        if usage['used'] == 1:
            return {'level': 'info', 'key': 'first',
                    'text': _('Your first analysis is ready. Open a recommendation to see how its '
                              'quantity was computed.')}
        return False

    @api.model
    def _locked_features(self):
        return [
            _('Create draft transfers from recommendations'),
            _('Lot and expiry awareness (FEFO)'),
            _('Before / after coverage simulation'),
            _('Constraints: minimum quantity, pack size, routes, exclusions'),
            _('Excess redistribution and scheduled analysis'),
            _('More than 2 warehouses and 50 products per run, multi-company'),
        ]

    def _dashboard_run_data(self):
        self.ensure_one()
        stats = self.stat_ids
        lines = self.line_ids
        shortage_products = stats.filtered(lambda s: s.status == 'shortage').product_id
        excess_products = stats.filtered(lambda s: s.status == 'excess').product_id
        run_domain = [('run_id', '=', self.id)]
        currency = self.currency_id
        kpis = [
            {'key': 'warehouses', 'label': _('Warehouses analysed'), 'value': len(self.warehouse_ids),
             'hint': ', '.join(self.warehouse_ids.mapped('name')), 'tone': '',
             'action': self._act(_('Coverage by warehouse'), 'elaf.rebalance.stat', run_domain,
                                 {'group_by': 'warehouse_id'})},
            {'key': 'shortage', 'label': _('Products in shortage'), 'value': len(shortage_products),
             'hint': _('%(n)s warehouse-product pairs below %(t)s days',
                       n=self.shortage_count, t=round(self.target_coverage_days, 1)),
             'tone': 'bad' if shortage_products else 'ok',
             'action': self._act(_('Shortages'), 'elaf.rebalance.stat',
                                 run_domain + [('status', '=', 'shortage')])},
            {'key': 'excess', 'label': _('Products in excess'), 'value': len(excess_products),
             'hint': _('%(n)s pairs above %(t)s days',
                       n=self.excess_count,
                       t=round(self.donor_min_coverage_days + self.target_coverage_days, 1)),
             'tone': 'warn' if excess_products else '',
             'action': self._act(_('Excess stock'), 'elaf.rebalance.stat',
                                 run_domain + [('status', '=', 'excess')])},
            {'key': 'units', 'label': _('Recommended units'), 'value': round(self.recommended_qty, 2),
             'hint': _('%(n)s recommendations', n=self.line_count), 'tone': '',
             'action': self._act(_('Recommendations'), 'elaf.rebalance.line', run_domain)},
            {'key': 'avoided', 'label': _('Avoided procurement (estimate)'),
             'value': self.avoided_procurement_value, 'monetary': True, 'estimate': True,
             'hint': _('Estimate: recommended quantity x product cost'), 'tone': 'ok',
             'action': self._act(_('Recommendations'), 'elaf.rebalance.line', run_domain)},
        ]
        heat = self._dashboard_heat()
        recos = [{
            'id': line.id,
            'product': line.product_id.display_name,
            'donor': line.donor_warehouse_id.name,
            'receiver': line.receiver_warehouse_id.name,
            'qty': line.quantity,
            'uom': line.uom_id.name,
            'value': line.avoided_procurement_value,
            'explanation': line.explanation,
            'state': line.state,
            'state_label': dict(line._fields['state']._description_selection(self.env)).get(line.state),
            'reason': line.reason,
        } for line in lines.sorted('sequence')[:10]]
        return {
            'run': {
                'id': self.id, 'name': self.name,
                'date': self.date_analysis and self.date_analysis.isoformat() or False,
                'target': self.target_coverage_days, 'donor_min': self.donor_min_coverage_days,
                'lookback': self.lookback_days, 'line_count': self.line_count,
                'hidden_count': self.hidden_line_count, 'hidden_qty': self.hidden_line_qty,
                'hidden_value': self.hidden_avoided_value, 'note': self.analysis_note or '',
                'currency': self._currency_info(currency),
            },
            'kpis': kpis,
            'heat': heat,
            'recommendations': recos,
        }

    def _dashboard_heat(self):
        self.ensure_one()
        stats = self.stat_ids
        warehouses = self.warehouse_ids.sorted('name')
        by_key = {(s.product_id.id, s.warehouse_id.id): s for s in stats}
        products = stats.product_id

        def urgency(product):
            covs = [by_key[(product.id, w.id)] for w in warehouses if (product.id, w.id) in by_key]
            shortage = any(s.status == 'shortage' for s in covs)
            finite = [s.coverage_days for s in covs if not s.coverage_infinite]
            return (0 if shortage else 1, min(finite) if finite else 1e9, product.display_name)

        products = sorted(products, key=urgency)
        total = len(products)
        rows = []
        for product in products[:HEAT_MAX_PRODUCTS]:
            cells = []
            for wh in warehouses:
                s = by_key.get((product.id, wh.id))
                if not s:
                    cells.append(False)
                    continue
                cells.append({
                    'id': s.id, 'status': s.status, 'coverage': s.coverage_display,
                    'infinite': s.coverage_infinite, 'available': s.available_qty,
                    'reserved': s.reserved_qty, 'daily': round(s.daily_demand, 2),
                    'after': None if s.coverage_infinite else round(s.coverage_after_days, 1),
                    'changed': bool(s.incoming_qty or s.outgoing_qty),
                })
            rows.append({'product_id': product.id, 'product': product.display_name, 'cells': cells})
        return {
            'warehouses': [{'id': w.id, 'name': w.name} for w in warehouses],
            'rows': rows,
            'total': total,
            'truncated': total > HEAT_MAX_PRODUCTS,
        }

    # --------------------------------------------------------- comparison
    @api.model
    def get_comparison_data(self):
        """Lite vs Pro page, with the value summary computed from real data."""
        self._dashboard_check_access()
        limits = self.env['elaf.rebalance.edition']._elaf_rebalance_limits()
        usage = self.env['elaf.rebalance.usage']._get_status(USAGE_KEY_RUNS)
        runs = self.search([('state', '=', 'analyzed')])
        company = self.env.company
        currency = company.currency_id
        visible_value = sum(runs.mapped('avoided_procurement_value'))
        hidden_value = sum(runs.mapped('hidden_avoided_value'))
        analysed_wh = runs.warehouse_ids
        all_wh = self.env['stock.warehouse'].search([('company_id', '=', company.id)])
        analysed_products = runs.stat_ids.product_id
        stocked = self.env['stock.quant']._read_group(
            [('location_id.usage', '=', 'internal'), ('company_id', '=', company.id),
             ('quantity', '>', 0), ('product_id.type', '=', 'product')],
            ['product_id'], ['quantity:sum'])
        stocked_products = {p.id for p, _qty in stocked}
        y, n = '✓', '—'
        features = [
            (_('Coverage per product and warehouse (on hand, reserved, demand)'), y, y),
            (_('Explained recommendations with donor protection'), y, y),
            (_('Reserved stock never proposed'), y, y),
            (_('Avoided procurement estimate'), y, y),
            (_('Warehouses per run'), str(limits['max_warehouses'] or 2), _('Unlimited')),
            (_('Products per run'), str(limits['max_products'] or 50), _('Unlimited')),
            (_('Completed analyses'), _('%(n)s in total', n=usage['limit']), _('Unlimited')),
            (_('Recommendations shown per run'), _('Top 10'), _('All')),
            (_('Companies'), _('One'), _('Multi-company')),
            (_('Create draft internal transfers (manager approval)'), n, y),
            (_('Lot and expiry awareness, FEFO'), n, y),
            (_('Minimum quantity, pack size, route priorities, exclusions'), n, y),
            (_('Before / after coverage simulation'), n, y),
            (_('Avoided procurement net of transfer costs'), n, y),
            (_('Excess redistribution mode'), n, y),
            (_('Scheduled analysis with manager activities'), n, y),
            (_('Location-level analysis'), n, y),
            (_('Export of coverage and recommendations'), n, y),
        ]
        return {
            'is_pro': limits['is_pro'],
            'usage': usage,
            'pro_url': limits['pro_url'],
            'support_email': SUPPORT_EMAIL,
            'currency': self._currency_info(currency),
            'features': [{'label': f[0], 'lite': f[1], 'pro': f[2]} for f in features],
            'limits_text': [
                _('One company.'),
                _('Up to 2 warehouses per analysis run.'),
                _('Up to 50 products per analysis run.'),
                _('Up to 3 completed analysis runs in total; past runs stay viewable.'),
                _('Up to 10 recommendations shown per run; the number of additional ones is shown.'),
                _('Recommendations are read-only: transfer creation is a Pro feature.'),
                _('No export, schedule or bulk operations.'),
            ],
            'value': {
                'runs': len(runs),
                'avoided_value': visible_value + hidden_value,
                'avoided_visible': visible_value,
                'avoided_hidden': hidden_value,
                'hidden_count': sum(runs.mapped('hidden_line_count')),
                'hidden_qty': sum(runs.mapped('hidden_line_qty')),
                'warehouses_total': len(all_wh),
                'warehouses_not_analysed': len(all_wh - analysed_wh),
                'products_stocked': len(stocked_products),
                'products_not_analysed': len(stocked_products - set(analysed_products.ids)),
            },
        }
