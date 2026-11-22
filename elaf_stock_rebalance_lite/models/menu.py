from odoo import models

# Menus that only make sense in the Lite edition (usage counter, comparison page).
LITE_ONLY_MENUS = (
    'elaf_stock_rebalance_lite.menu_elaf_rebalance_usage',
    'elaf_stock_rebalance_lite.menu_elaf_rebalance_usage_log',
    'elaf_stock_rebalance_lite.menu_elaf_rebalance_comparison',
)


class IrUiMenu(models.Model):
    _inherit = 'ir.ui.menu'

    def _load_menus_blacklist(self):
        """Hide the Lite-only menus while the Pro edition is installed.

        Computed from the edition hook at menu load time, so no record is
        modified: uninstalling Pro (registry reload) shows them again.
        """
        res = super()._load_menus_blacklist()
        if self.env['elaf.rebalance.edition']._elaf_rebalance_is_pro():
            for xmlid in LITE_ONLY_MENUS:
                menu = self.env.ref(xmlid, raise_if_not_found=False)
                if menu:
                    res.append(menu.id)
        return res
