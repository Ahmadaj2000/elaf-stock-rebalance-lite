import logging
import threading

from odoo.modules import module as odoo_module

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from . import edition as edition_mod
from .edition import USAGE_KEY_RUNS

_logger = logging.getLogger(__name__)

_INTERNAL = 'elaf_rb_usage_internal'


class ElafRebalanceUsage(models.Model):
    """Monotonic usage counter of the Lite edition.

    Nobody (not even a Settings administrator) can create, write or unlink
    rows through the ORM: ACLs grant read only, and the overrides below
    refuse any change that does not come from ``_consume``. A database
    superuser with SQL access can still change the rows: this is a usage
    meter, not DRM.
    """

    _name = 'elaf.rebalance.usage'
    _description = 'Stock Rebalancing Usage Counter'
    _order = 'key'

    key = fields.Char(required=True, readonly=True)
    used = fields.Integer(readonly=True, default=0)
    limit = fields.Integer(readonly=True, default=0)
    bound_company_id = fields.Many2one('res.company', readonly=True,
                                       string='Bound Company')
    last_used_at = fields.Datetime(readonly=True, string='Last Used At')

    _sql_constraints = [
        ('key_uniq', 'unique(key)', 'One usage row per counter key.'),
    ]

    # ------------------------------------------------------------------ guards
    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get(_INTERNAL):
            raise AccessError(self.env._('Usage counters cannot be created manually.'))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.context.get(_INTERNAL):
            raise AccessError(self.env._('Usage counters cannot be modified.'))
        return super().write(vals)

    def unlink(self):
        if not self.env.context.get(_INTERNAL):
            raise AccessError(self.env._('Usage counters cannot be deleted.'))
        return super().unlink()

    # --------------------------------------------------------------- helpers
    @api.model
    def _limit_for(self, key):
        return {USAGE_KEY_RUNS: edition_mod.LITE_MAX_RUNS}.get(key, 0)

    @api.model
    def _get_or_create(self, key):
        # sudo: internal bookkeeping row; callers did their own checks.
        usage = self.sudo().search([('key', '=', key)], limit=1)
        if not usage:
            self.env.cr.execute(
                """INSERT INTO elaf_rebalance_usage (key, used, "limit", create_uid, write_uid,
                                                     create_date, write_date)
                   VALUES (%s, 0, %s, %s, %s, now() at time zone 'utc', now() at time zone 'utc')
                   ON CONFLICT (key) DO NOTHING""",
                (key, self._limit_for(key), self.env.uid, self.env.uid))
            usage = self.sudo().search([('key', '=', key)], limit=1)
        return usage

    @api.model
    def _is_capped(self):
        edition = self.env['elaf.rebalance.edition']
        return edition_mod.ELAF_USAGE_CAPS_ENABLED and not edition._elaf_rebalance_is_pro()

    @api.model
    def _get_status(self, key=USAGE_KEY_RUNS):
        usage = self._get_or_create(key)
        limit = usage.limit or self._limit_for(key)
        used = usage.used
        capped = self._is_capped()
        return {
            'key': key,
            'used': used,
            'limit': limit,
            'remaining': max(0, limit - used) if capped else False,
            'percent': min(100, round(100.0 * used / limit)) if (capped and limit) else 0,
            'exhausted': bool(capped and used >= limit),
            'capped': capped,
            'bound_company_id': usage.bound_company_id.id or False,
            'bound_company_name': usage.bound_company_id.name or False,
        }

    @api.model
    def _check_company(self, company):
        """Lite works with one company: the first one that consumed usage."""
        if self.env['elaf.rebalance.edition']._elaf_rebalance_is_pro():
            return
        usage = self._get_or_create(USAGE_KEY_RUNS)
        if usage.bound_company_id and usage.bound_company_id != company:
            raise UserError(self.env._(
                'The Lite edition works with one company: %(bound)s. '
                'The Pro edition supports multiple companies.',
                bound=usage.bound_company_id.name))

    @api.model
    def _check_available(self, key=USAGE_KEY_RUNS):
        """Early, non-locking check so users get the message before any work."""
        if not self._is_capped():
            return
        usage = self._get_or_create(key)
        if usage.used >= usage.limit:
            self._log_denied(key)
            raise UserError(self._exhausted_message(usage))

    @api.model
    def _exhausted_message(self, usage):
        return self.env._(
            'Lite analysis allowance used (%(used)s/%(limit)s). Your past runs and '
            'recommendations stay available. Upgrade to Pro for unlimited analyses.',
            used=usage.used, limit=usage.limit)

    @api.model
    def _consume(self, key, record):
        """Atomically consume one unit, in the caller's transaction.

        The row is locked (SELECT ... FOR UPDATE), then incremented only while
        below the limit. If the caller's transaction later fails, the increment
        rolls back with it, so failed operations never consume.
        """
        usage = self._get_or_create(key)
        cr = self.env.cr
        cr.execute('SELECT id FROM elaf_rebalance_usage WHERE id = %s FOR UPDATE', (usage.id,))
        capped = self._is_capped()
        if capped:
            cr.execute(
                """UPDATE elaf_rebalance_usage
                      SET used = used + 1, last_used_at = now() at time zone 'utc'
                    WHERE id = %s AND used < "limit"
                RETURNING used""", (usage.id,))
        else:
            cr.execute(
                """UPDATE elaf_rebalance_usage
                      SET used = used + 1, last_used_at = now() at time zone 'utc'
                    WHERE id = %s
                RETURNING used""", (usage.id,))
        row = cr.fetchone()
        usage.invalidate_recordset(['used', 'last_used_at'])
        if not row:
            self._log_denied(key, record)
            raise UserError(self._exhausted_message(usage))
        company = getattr(record, 'company_id', False)
        if company and not usage.bound_company_id and capped:
            usage.with_context(**{_INTERNAL: True}).write({'bound_company_id': company.id})
        self.env['elaf.rebalance.usage.log'].sudo().with_context(**{_INTERNAL: True}).create({
            'key': key, 'event': 'consumed', 'user_id': self.env.uid,
            'res_model': record._name, 'res_id': record.id, 'used_after': row[0],
        })
        return row[0]

    @api.model
    def _log_unchanged(self, key, record):
        """Audit a free re-analysis (parameters and data unchanged, S5-R3)."""
        usage = self._get_or_create(key)
        self.env['elaf.rebalance.usage.log'].sudo().with_context(**{_INTERNAL: True}).create({
            'key': key, 'event': 'unchanged', 'user_id': self.env.uid,
            'res_model': record._name, 'res_id': record.id, 'used_after': usage.used,
        })

    @api.model
    def _in_test_mode(self):
        """True while Odoo runs tests (any series 17-19).

        Odoo 19 no longer sets ``threading.current_thread().testing``; the test
        loader sets ``odoo.modules.module.current_test`` in every series, and
        HttpCase uses a registry test cursor.
        """
        return bool(getattr(odoo_module, 'current_test', False)
                    or getattr(threading.current_thread(), 'testing', False)
                    or self.env.registry.in_test_mode())

    @api.model
    def _log_denied(self, key, record=None):
        """Log a denial in an independent transaction (the caller rolls back).

        Skipped while running tests, where opening a second connection would
        escape the test transaction.
        """
        if self._in_test_mode():
            return
        try:
            with self.env.registry.cursor() as cr2:
                cr2.execute(
                    """INSERT INTO elaf_rebalance_usage_log
                         (key, event, user_id, res_model, res_id, at, create_uid, write_uid,
                          create_date, write_date)
                       VALUES (%s, 'denied', %s, %s, %s, now() at time zone 'utc', %s, %s,
                               now() at time zone 'utc', now() at time zone 'utc')""",
                    (key, self.env.uid, record._name if record else None,
                     record.id if record else None, self.env.uid, self.env.uid))
        except Exception:  # noqa: BLE001 - logging must never break the user flow
            _logger.warning('Could not log denied usage event', exc_info=True)


class ElafRebalanceUsageLog(models.Model):
    """Append-only usage log."""

    _name = 'elaf.rebalance.usage.log'
    _description = 'Stock Rebalancing Usage Log'
    _order = 'at desc, id desc'

    key = fields.Char(required=True, readonly=True)
    event = fields.Selection([('consumed', 'Consumed'), ('denied', 'Denied'),
                              ('unchanged', 'Re-analysis unchanged (not counted)')],
                             required=True, readonly=True)
    user_id = fields.Many2one('res.users', readonly=True, string='User')
    res_model = fields.Char(readonly=True, string='Model')
    res_id = fields.Integer(readonly=True, string='Record ID')
    used_after = fields.Integer(readonly=True, string='Used After')
    at = fields.Datetime(readonly=True, default=fields.Datetime.now, string='At')

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get(_INTERNAL):
            raise AccessError(self.env._('Usage log entries cannot be created manually.'))
        return super().create(vals_list)

    def write(self, vals):
        raise AccessError(self.env._('Usage log entries cannot be modified.'))

    def unlink(self):
        raise AccessError(self.env._('Usage log entries cannot be deleted.'))
