from odoo import api, models

# Switch for the cumulative usage cap (3 completed runs). If the Odoo Apps
# store treats capped free apps as trials, set this to False: the functional
# limits (2 warehouses, 50 products, top 10 recommendations, no transfers)
# stay in place, only the cumulative run counter stops blocking.
ELAF_USAGE_CAPS_ENABLED = True

# Lite edition limits (Python constants, not editable configuration).
LITE_MAX_WAREHOUSES = 2
LITE_MAX_PRODUCTS = 50
LITE_MAX_RUNS = 3
LITE_MAX_RECOMMENDATIONS = 10
USAGE_KEY_RUNS = 'completed_runs'

PRO_URL = 'https://apps.odoo.com/apps/modules/19.0/elaf_stock_rebalance'


class ElafRebalanceEdition(models.AbstractModel):
    """Edition hook. Lite returns False; the Pro module overrides it."""

    _name = 'elaf.rebalance.edition'
    _description = 'Stock Rebalancing Edition'

    @api.model
    def _elaf_rebalance_is_pro(self):
        return False

    @api.model
    def _elaf_rebalance_limits(self):
        is_pro = self._elaf_rebalance_is_pro()
        return {
            'is_pro': is_pro,
            'caps_enabled': ELAF_USAGE_CAPS_ENABLED and not is_pro,
            'max_warehouses': 0 if is_pro else LITE_MAX_WAREHOUSES,
            'max_products': 0 if is_pro else LITE_MAX_PRODUCTS,
            'max_runs': 0 if is_pro else LITE_MAX_RUNS,
            'max_recommendations': 0 if is_pro else LITE_MAX_RECOMMENDATIONS,
            'pro_url': PRO_URL,
        }
