/** @odoo-module **/
import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { rbFormatMoney, rbFormatNumber } from "./dashboard";

export class RebalanceComparison extends Component {
    static template = "elaf_stock_rebalance_lite.Comparison";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({ loading: true, error: false, data: null });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        this.state.error = false;
        try {
            this.state.data = await this.orm.call("elaf.rebalance.run", "get_comparison_data", []);
        } catch (error) {
            this.state.error = error?.data?.message || error?.message || _t("Unknown error");
        } finally {
            this.state.loading = false;
        }
    }

    fmt(value) {
        return rbFormatNumber(value);
    }

    money(value) {
        return rbFormatMoney(value, this.state.data.currency);
    }

    discoverPro() {
        window.open(this.state.data.pro_url, "_blank", "noopener");
    }

    openDashboard() {
        this.action.doAction("elaf_stock_rebalance_lite.action_elaf_rebalance_dashboard");
    }
}

registry.category("actions").add("elaf_rebalance_comparison", RebalanceComparison);
