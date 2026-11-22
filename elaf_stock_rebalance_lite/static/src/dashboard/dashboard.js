/** @odoo-module **/
import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { formatFloat } from "@web/core/utils/numbers";

const MODEL = "elaf.rebalance.run";

export function rbFormatNumber(value, digits = 2) {
    if (value === null || value === undefined || value === false) {
        return "";
    }
    const rounded = Math.round(value * 10 ** digits) / 10 ** digits;
    const isInt = Number.isInteger(rounded);
    return formatFloat(rounded, { digits: [16, isInt ? 0 : digits] });
}

export function rbFormatMoney(value, currency) {
    const cur = currency || { symbol: "", position: "after", digits: 2 };
    const amount = formatFloat(value || 0, { digits: [16, cur.digits ?? 2] });
    return cur.position === "before" ? `${cur.symbol} ${amount}` : `${amount} ${cur.symbol}`;
}

function storageGet(key) {
    try {
        return window.localStorage.getItem(key);
    } catch {
        return null;
    }
}

function storageSet(key, value) {
    try {
        window.localStorage.setItem(key, value);
    } catch {
        // storage unavailable (private mode): the banner simply shows again
    }
}

export class RebalanceDashboard extends Component {
    static template = "elaf_stock_rebalance_lite.Dashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({
            loading: true,
            error: false,
            data: null,
            runId: false,
            dismissed: {},
        });
        onWillStart(() => this.load());
    }

    get data() {
        return this.state.data;
    }

    get currency() {
        return this.data?.run?.currency;
    }

    get bannerVisible() {
        const banner = this.data?.banner;
        if (!banner) {
            return false;
        }
        if (banner.key === "exhausted") {
            return true;
        }
        const key = `elaf_rb_banner_${banner.key}`;
        return !this.state.dismissed[banner.key] && !storageGet(key);
    }

    async load() {
        this.state.loading = true;
        this.state.error = false;
        try {
            const filters = this.state.runId ? { run_id: this.state.runId } : {};
            this.state.data = await this.orm.call(MODEL, "get_dashboard_data", [filters]);
            this.state.runId = this.state.data.run ? this.state.data.run.id : false;
        } catch (error) {
            this.state.error = error?.data?.message || error?.message || _t("Unknown error");
        } finally {
            this.state.loading = false;
        }
    }

    fmt(value, digits = 2) {
        return rbFormatNumber(value, digits);
    }

    money(value) {
        return rbFormatMoney(value, this.currency);
    }

    kpiValue(kpi) {
        return kpi.monetary ? this.money(kpi.value) : this.fmt(kpi.value);
    }

    usageBarClass(usage) {
        if (usage.percent >= 100) {
            return "bad";
        }
        return usage.percent >= 80 ? "warn" : "";
    }

    dismissBanner() {
        const banner = this.data.banner;
        this.state.dismissed[banner.key] = true;
        storageSet(`elaf_rb_banner_${banner.key}`, "1");
    }

    onRunChange(ev) {
        this.state.runId = parseInt(ev.target.value) || false;
        this.load();
    }

    openAction(action) {
        if (action) {
            this.action.doAction(action);
        }
    }

    openRecord(model, id, name) {
        this.action.doAction({
            type: "ir.actions.act_window",
            name,
            res_model: model,
            res_id: id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    openStat(cell) {
        if (cell) {
            this.openRecord("elaf.rebalance.stat", cell.id, _t("Coverage"));
        }
    }

    openLine(reco) {
        this.openRecord("elaf.rebalance.line", reco.id, _t("Recommendation"));
    }

    openRun() {
        if (this.data.run) {
            this.openRecord(MODEL, this.data.run.id, this.data.run.name);
        }
    }

    openRuns() {
        this.action.doAction("elaf_stock_rebalance_lite.action_elaf_rebalance_run");
    }

    newRun() {
        if (this.data.usage.exhausted) {
            this.openComparison();
            return;
        }
        this.action.doAction({
            type: "ir.actions.act_window",
            name: _t("New Analysis"),
            res_model: MODEL,
            views: [[false, "form"]],
            target: "current",
        });
    }

    openComparison() {
        this.action.doAction("elaf_stock_rebalance_lite.action_elaf_rebalance_comparison");
    }

    statusLabel(status) {
        return {
            shortage: _t("Shortage"),
            balanced: _t("Balanced"),
            excess: _t("Excess"),
            inactive: _t("No stock, no demand"),
        }[status];
    }

    cellTitle(cell) {
        if (!cell) {
            return "";
        }
        return _t("Available %(available)s (reserved %(reserved)s), demand %(daily)s/day", {
            available: this.fmt(cell.available),
            reserved: this.fmt(cell.reserved),
            daily: this.fmt(cell.daily),
        });
    }
}

registry.category("actions").add("elaf_rebalance_dashboard", RebalanceDashboard);
