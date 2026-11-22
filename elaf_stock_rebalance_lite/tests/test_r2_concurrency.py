"""Remediation R2 (S5-R3): two real database connections analyse the same run at once.

Unlike the other tests, this one needs committed data (a second connection cannot see the
test transaction): it commits a minimal scenario, runs two threads with their own
cursors, then removes what it committed and restores the usage counter exactly.
"""
import threading
import time
import uuid
from unittest.mock import patch

import psycopg2
from psycopg2 import errorcodes

from odoo import SUPERUSER_ID, api, sql_db
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

USAGE_KEY = 'completed_runs'


@tagged('post_install', '-at_install')
class TestRebalanceR2Concurrency(TransactionCase):

    def _env(self, cr):
        return api.Environment(cr, SUPERUSER_ID, {})

    def _setup_committed(self, db, tag):
        with db.cursor() as cr:
            env = self._env(cr)
            cr.execute('SELECT used, bound_company_id, last_used_at FROM elaf_rebalance_usage WHERE key = %s',
                       (USAGE_KEY,))
            usage_before = cr.fetchone()
            company = env.ref('base.main_company')
            wh_main = env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)
            wh_new = env['stock.warehouse'].create({'name': 'R2 Concurrency %s' % tag, 'code': tag[:5],
                                                    'company_id': company.id})
            product = env['product.product'].create(self._product_vals('R2 Concurrency %s' % tag))
            env['stock.quant']._update_available_quantity(product, wh_main.lot_stock_id, 10)
            run = env['elaf.rebalance.run'].with_company(company).create({
                'company_id': company.id,
                'warehouse_ids': [(6, 0, [wh_main.id, wh_new.id])],
                'product_ids': [(6, 0, product.ids)],
            })
            cr.commit()
            return {'usage_before': usage_before, 'wh_new': wh_new.id, 'product': product.id, 'run': run.id}

    @staticmethod
    def _call_with_retry(env, func, retried):
        """What odoo.service.model.retrying() does for an RPC call: on a
        serialization failure roll back, reset the caches and call again."""
        for _attempt in range(5):
            try:
                result = func()
                env.cr.flush()
                return result
            except psycopg2.OperationalError as error:
                if error.pgcode not in (errorcodes.SERIALIZATION_FAILURE, errorcodes.LOCK_NOT_AVAILABLE,
                                        errorcodes.DEADLOCK_DETECTED):
                    raise
                retried.append(error.pgcode)
                env.cr.rollback()
                env.invalidate_all()
                time.sleep(0.2)
        raise AssertionError('still failing after 5 attempts')

    def _product_vals(self, name):
        return {'name': name, 'detailed_type': 'product'}

    def _cleanup(self, db, data):
        with db.cursor() as cr:
            cr.execute("DELETE FROM elaf_rebalance_usage_log WHERE res_model = 'elaf.rebalance.run' AND res_id = %s",
                       (data['run'],))
            before = data['usage_before']
            if before is None:
                cr.execute('DELETE FROM elaf_rebalance_usage WHERE key = %s', (USAGE_KEY,))
            else:
                cr.execute('UPDATE elaf_rebalance_usage SET used = %s, bound_company_id = %s, last_used_at = %s '
                           'WHERE key = %s', (before[0], before[1], before[2], USAGE_KEY))
            env = self._env(cr)
            env['elaf.rebalance.run'].browse(data['run']).unlink()
            cr.execute('DELETE FROM stock_quant WHERE product_id = %s', (data['product'],))
            product = env['product.product'].browse(data['product'])
            template = product.product_tmpl_id
            product.unlink()
            template.unlink()
            env['stock.warehouse'].browse(data['wh_new']).write({'active': False})
            cr.commit()

    def test_01_parallel_analyse_consumes_once(self):
        db = sql_db.db_connect(self.env.cr.dbname)
        tag = 'R2C%s' % uuid.uuid4().hex[:2].upper()
        data = self._setup_committed(db, tag)
        try:
            Run = type(self.env['elaf.rebalance.run'])
            original = Run._compute_stats
            in_progress = threading.Event()

            def slow_compute_stats(run_self, *args, **kwargs):
                in_progress.set()
                time.sleep(1.5)  # hold the run lock while the other request arrives
                return original(run_self, *args, **kwargs)

            barrier = threading.Barrier(2)
            results, errors, retries = {}, {}, {0: [], 1: []}
            # Environments are built here: the registry lock is held by the test runner,
            # so a worker thread cannot look the registry up itself.
            cursors = [db.cursor() for _i in range(2)]
            envs = [self._env(cr) for cr in cursors]

            def worker(idx):
                cr = cursors[idx]
                try:
                    run = envs[idx]['elaf.rebalance.run'].browse(data['run'])
                    barrier.wait(timeout=30)
                    if idx == 1:
                        in_progress.wait(timeout=30)  # the first request is inside the analysis
                    results[idx] = self._call_with_retry(envs[idx], run.action_analyze, retries[idx])
                    cr.commit()
                except Exception as error:  # noqa: BLE001 - reported below
                    errors[idx] = error
                    cr.rollback()

            try:
                # the waiting request gets a serialization failure on the run lock, which
                # Odoo logs as "bad query" before its service layer retries it
                with patch.object(Run, '_compute_stats', slow_compute_stats), mute_logger('odoo.sql_db'):
                    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=120)
                self.assertFalse([t for t in threads if t.is_alive()], 'a request is still blocked')
            finally:
                for cr in cursors:
                    cr.close()
            self.assertFalse(errors, 'both requests complete without error: %s' % errors)
            self.assertEqual(len(results), 2)
            with db.cursor() as cr:
                cr.execute("""SELECT event, used_after FROM elaf_rebalance_usage_log
                               WHERE res_model = 'elaf.rebalance.run' AND res_id = %s ORDER BY id""",
                           (data['run'],))
                events = cr.fetchall()
                cr.execute('SELECT used FROM elaf_rebalance_usage WHERE key = %s', (USAGE_KEY,))
                used = cr.fetchone()[0]
                cr.execute('SELECT state FROM elaf_rebalance_run WHERE id = %s', (data['run'],))
                state = cr.fetchone()[0]
            before = data['usage_before'][0] if data['usage_before'] else 0
            self.assertEqual([e[0] for e in events if e[0] == 'consumed'], ['consumed'],
                             'exactly one consumption for two requests: %s' % events)
            self.assertEqual(retries[0], [], 'the first request runs straight through')
            self.assertEqual(retries[1], ['40001'], 'the second waited on the run lock, then was retried')
            self.assertEqual(used, before + 1)
            self.assertEqual(state, 'analyzed')
        finally:
            self._cleanup(db, data)
