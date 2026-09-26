import concurrent.futures
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
from service import Store, iso

ROOT = Path(__file__).resolve().parent


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = json.loads((ROOT/'config.json').read_text())
        self.store = Store(Path(self.tmp.name)/'delivery.sqlite3', self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def event(self, kind='order_placed', order_id='ORDER-1', **extra):
        body = {'event_id': str(uuid.uuid4()), 'event_type': kind, 'order_id': order_id}
        if kind == 'order_placed':
            body.update({'order_timestamp': iso(), 'customer_address': '1200 E Campbell Rd, Richardson, TX', 'restaurant_id': 'REST-001'})
        else:
            body['event_timestamp'] = iso()
        return {**body, **extra}

    def place(self, order_id='ORDER-1', **extra):
        return self.store.process(self.event(order_id=order_id, **extra))

    def ready(self, order_id='ORDER-1', **extra):
        placed = self.place(order_id, **extra)
        self.assertEqual(placed['state'], 'AWAITING_CUSTOMER')
        self.store.process(self.event('customer_confirmed', order_id, quote_id=placed['order']['quote_id'], option='standard'))
        return self.store.process(self.event('restaurant_accepted', order_id, restaurant_id='REST-001'))

    def transit(self):
        self.ready()
        self.store.process(self.event('driver_accepted', driver_id='DRIVER-1'))
        return self.store.process(self.event('driver_picked_up', driver_id='DRIVER-1'))

    def test_all_ten_steps_and_real_sql_logs(self):
        r = self.place()
        self.assertEqual(len(r['decision_trace']), 10)
        self.assertEqual(r['state'], 'AWAITING_CUSTOMER')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM orders').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM runs').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM api_calls').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM loki_outbox').fetchone()[0], 1)

    def test_exact_retry_has_one_order_two_audit_records(self):
        e = self.event()
        a, b = self.store.process(e), self.store.process(e)
        self.assertTrue(b['duplicate'])
        self.assertEqual(b['original_run_id'], a['run_id'])
        self.assertEqual(self.store.snapshot()['metrics']['runs'], 1)
        self.assertEqual(len(self.store.snapshot()['runs']), 2)

    def test_same_event_changed_payload_conflicts(self):
        e = self.event()
        self.store.process(e)
        self.assertEqual(self.store.process({**e,'customer_address':'different'})['http_status'],409)

    def test_same_order_new_event_deduplicates(self):
        self.place()
        r = self.place()
        self.assertTrue(r['duplicate'])
        self.assertEqual(self.store.snapshot()['metrics']['runs'],1)

    def test_concurrent_duplicate_creates_once(self):
        e = self.event()
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            results = list(pool.map(self.store.process,[e]*8))
        self.assertEqual(sum(not r.get('duplicate',False) for r in results),1)
        self.assertEqual(len(self.store.snapshot()['orders']),1)

    def test_competing_drivers_only_one_locks(self):
        self.ready()
        requests = [self.event('driver_accepted',driver_id='DRIVER-'+str(n)) for n in range(8)]
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            results = list(pool.map(self.store.process,requests))
        self.assertEqual(sum(r['http_status']==200 for r in results),1)
        self.assertEqual(sum(r['http_status']==409 for r in results),7)

    def test_no_premature_assignment(self):
        self.place()
        r = self.store.process(self.event('driver_accepted',driver_id='D'))
        self.assertEqual(r['http_status'],409)

    def test_inactive_vendor_rejects_and_notifies_support(self):
        r = self.place(restaurant_id='INACTIVE')
        self.assertEqual(r['http_status'],422)
        self.assertEqual(self.store.snapshot()['notifications'][0]['recipient'],'customer_support')

    def test_address_quarantine_and_recovery(self):
        r = self.place(customer_address='bad')
        self.assertEqual(r['state'],'QUARANTINED')
        fixed = self.store.process(self.event('address_updated',customer_address='1200 E Campbell Rd, Richardson, TX'))
        self.assertEqual(fixed['state'],'AWAITING_CUSTOMER')

    def test_stale_future_timezone_and_invalid_timestamps(self):
        for value in (iso(time.time()-301),iso(time.time()+301),'2026-09-21T12:00:00','not a date'):
            self.assertGreaterEqual(self.place(order_id=str(uuid.uuid4()),order_timestamp=value)['http_status'],400)

    def test_exact_300_second_boundary(self):
        now=time.time()
        with patch('service.time.time',return_value=now):
            self.assertEqual(self.place(order_timestamp=iso(now-300))['http_status'],200)

    def test_capitalized_workbook_fields(self):
        e=self.event()
        for k in ('customer_address','restaurant_id','order_timestamp'):
            e[k[0].upper()+k[1:]]=e.pop(k)
        self.assertEqual(self.store.process(e)['state'],'AWAITING_CUSTOMER')

    def test_missing_traffic_uses_median_and_reviews(self):
        r=self.place(scenario='invalid_traffic')
        self.assertEqual(r['state'],'HUMAN_REVIEW')
        self.assertEqual(r['order']['route']['traffic_delay_minutes'],8)

    def test_primary_failover_and_stale_failover(self):
        for scenario in ('primary_down','stale_primary'):
            r=self.place(order_id=scenario,scenario=scenario)
            self.assertEqual(r['state'],'AWAITING_CUSTOMER')
            self.assertEqual(r['decision_trace'][2]['result'],'demo-here')

    def test_both_fail_four_attempts_human_gate(self):
        r=self.place(scenario='both_apis_down')
        self.assertEqual(r['state'],'HUMAN_REVIEW')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM api_calls').fetchone()[0],4)

    def test_blocked_preferred_selects_safe_alternative(self):
        r=self.place(scenario='blocked_preferred')
        self.assertEqual(r['order']['route']['id'],'demo-route-2')
        self.assertEqual(r['decision_trace'][5]['result'],'alternative_selected')

    def test_vehicle_restricted_route_excluded(self):
        r=self.place(scenario='vehicle_restricted')
        self.assertEqual(r['order']['route']['id'],'demo-route-2')

    def test_no_safe_route_blocks_dispatch(self):
        r=self.place(scenario='all_blocked')
        self.assertEqual(r['state'],'HUMAN_REVIEW')
        self.assertIsNone(r['order']['route'])

    def test_boundary_review_and_outside_hard_block(self):
        self.assertEqual(self.place(customer_address='Demo Boundary, Richardson, TX')['state'],'HUMAN_REVIEW')
        outside=self.place(order_id='OUT',customer_address='Demo Outside, Dallas, TX')
        self.assertEqual(outside['order']['hard_blocks'],['outside_service_boundary'])
        r=self.store.process(self.event('human_review','OUT',reviewer='Tester',reason='test',decision='approve'),admin=True)
        self.assertEqual(r['http_status'],409)

    def test_full_lifecycle_and_fee_freeze(self):
        transit=self.transit()
        fee=transit['order']['confirmed_fee']
        update=self.store.process(self.event('delivery_update',driver_id='DRIVER-1',driver_location={'lat':32.98,'lng':-96.73}))
        self.assertEqual(update['order']['confirmed_fee'],fee)
        r=self.store.process(self.event('delivered',driver_id='DRIVER-1',delivery_proof='handoff-1'))
        self.assertEqual(r['state'],'DELIVERED')
        self.assertEqual(self.store.snapshot()['metrics']['completed_orders'],1)

    def test_foreign_driver_and_invalid_location_rejected(self):
        self.transit()
        self.assertEqual(self.store.process(self.event('delivered',driver_id='OTHER',delivery_proof='x'))['http_status'],409)
        self.assertEqual(self.store.process(self.event('delivery_update',driver_id='DRIVER-1',driver_location={'lat':None,'lng':0}))['http_status'],400)

    def test_route_report_logs_wrong_and_excludes_route(self):
        self.transit()
        r=self.store.process(self.event('route_report',driver_id='DRIVER-1',driver_location={'lat':32.98,'lng':-96.73},reason='blocked',route_id='demo-route-1'))
        self.assertEqual(r['order']['route']['id'],'demo-route-2')
        self.assertIn('incorrect_route_recommendation',[n['kind'] for n in self.store.snapshot()['notifications']])

    def test_emergency_toggle_pauses_existing_and_new_orders(self):
        self.place()
        e=self.event('region_toggle','REGION',region='richardson-demo',enabled=False,reason='Demo storm')
        self.assertEqual(self.store.process(e)['http_status'],403)
        self.store.process(e,admin=True)
        self.assertEqual(self.store.snapshot()['orders'][0]['state'],'HUMAN_REVIEW')
        self.assertEqual(self.place(order_id='NEW')['order']['hard_blocks'],['service_region_disabled'])

    def test_restaurant_and_driver_timeouts(self):
        p=self.place()
        self.store.process(self.event('customer_confirmed',quote_id=p['order']['quote_id']))
        with patch('service.time.time',return_value=time.time()+601):
            self.store.maintenance()
        self.assertIn('restaurant_confirmation_missing',self.store.snapshot()['orders'][0]['review_reasons'])

    def test_persistence_restart(self):
        self.ready()
        fresh=Store(self.store.path,self.config)
        self.assertEqual(fresh.snapshot()['orders'][0]['state'],'AWAITING_DRIVER')
        self.assertEqual(fresh.snapshot()['metrics']['runs'],3)

    def test_old_original_timestamp_does_not_break_later_events(self):
        self.ready()
        with self.store.connect() as db:
            order=self.store.load(db,'ORDER-1');order['created_at']-=3600;self.store.save(db,order)
        self.assertEqual(self.store.process(self.event('driver_accepted',driver_id='D'))['state'],'ASSIGNED')

    def test_quote_expiration_returns_new_quote(self):
        p=self.place()
        with self.store.connect() as db:
            o=self.store.load(db,'ORDER-1');o['quote_expires_at']=time.time()-1;self.store.save(db,o)
        r=self.store.process(self.event('customer_confirmed',quote_id=p['order']['quote_id']))
        self.assertEqual(r['http_status'],409)
        self.assertNotEqual(r['order']['quote_id'],p['order']['quote_id'])

    def test_shared_candidate_capacity_and_assignment(self):
        self.ready('PEER',allow_shared=True)
        new=self.place('SHARED',customer_address='1500 E Campbell Rd, Richardson, TX',allow_shared=True)
        shared=next(q for q in new['order']['quotes'] if q['option']=='shared')
        self.assertEqual(shared['capacity_used'],2)
        self.store.process(self.event('customer_confirmed','SHARED',quote_id=new['order']['quote_id'],option='shared'))
        self.store.process(self.event('restaurant_accepted','SHARED',restaurant_id='REST-001'))
        result=self.store.process(self.event('driver_accepted','SHARED',driver_id='SHARED-DRIVER'))
        self.assertEqual(result['http_status'],200)
        self.assertTrue(all(o['driver_id']=='SHARED-DRIVER' for o in self.store.snapshot()['orders']))

    def test_missing_loki_does_not_disable_sql(self):
        self.place()
        with patch.dict('os.environ',{},clear=True):
            self.assertEqual(self.store.flush_loki()['status'],'not_configured')
        self.assertEqual(self.store.snapshot()['loki']['queued'],1)

    def test_two_hour_failure_alert_needs_sustained_data(self):
        now=time.time()
        for i in range(8):
            self.place(order_id=str(i),restaurant_id='BAD')
        with self.store.connect() as db:
            ids=[r[0] for r in db.execute('SELECT run_id FROM runs')]
            for i,r in enumerate(ids):
                db.execute('UPDATE runs SET created_at=? WHERE run_id=?',(now-i*900-10,r))
        self.store.maintenance()
        self.assertIn('failure-2h',[a['alert_key'] for a in self.store.snapshot()['alerts']])

    def test_failure_alert_does_not_fire_for_brief_spike(self):
        self.place(restaurant_id='BAD')
        self.store.maintenance()
        self.assertNotIn('failure-2h',[a['alert_key'] for a in self.store.snapshot()['alerts']])

    def test_invalid_envelope_types_are_logged(self):
        for body in ([], self.event(event_id={}), self.event(order_id=[]), self.event(event_type=[])):
            self.assertEqual(self.store.process(body)['http_status'],400)
        self.assertEqual(len(self.store.snapshot()['runs']),4)

    def test_scheduled_recheck_does_not_refresh_driver_location(self):
        self.transit()
        with self.store.connect() as db:
            order=self.store.load(db,'ORDER-1')
            original=order['location_at']
        with patch('service.time.time',return_value=time.time()+61):
            self.store.maintenance()
        order=self.store.snapshot()['orders'][0]
        self.assertEqual(order['location_at'],original)
        self.assertEqual(order['state'],'IN_TRANSIT')

    def test_backup_restores_sql_state(self):
        self.ready()
        backup=self.store.backup()
        restored=Store(backup['path'],self.config)
        self.assertEqual(restored.snapshot()['orders'][0]['state'],'AWAITING_DRIVER')

    def test_workflow_failure_has_dispatch_notice(self):
        self.store.workflow_error({'execution_id':'test-execution-1'})
        self.assertEqual(self.store.snapshot()['notifications'][0]['kind'],'n8n_workflow_failed')

    def test_loki_payload_redacts_addresses_and_coordinates(self):
        self.place()
        with self.store.connect() as db:
            payload=db.execute('SELECT payload FROM loki_outbox').fetchone()[0]
        self.assertNotIn('Campbell',payload)
        self.assertNotIn('32.975',payload)

    def test_shared_remaining_stops_rechecked(self):
        self.ready('PEER',allow_shared=True)
        new=self.place('SHARED',customer_address='1500 E Campbell Rd, Richardson, TX',allow_shared=True)
        self.store.process(self.event('customer_confirmed','SHARED',quote_id=new['order']['quote_id'],option='shared'))
        self.store.process(self.event('restaurant_accepted','SHARED',restaurant_id='REST-001'))
        self.store.process(self.event('driver_accepted','SHARED',driver_id='GROUP-DRIVER'))
        for order_id in ('PEER','SHARED'):
            self.store.process(self.event('driver_picked_up',order_id,driver_id='GROUP-DRIVER'))
        r=self.store.process(self.event('delivery_update','SHARED',driver_id='GROUP-DRIVER',driver_location={'lat':32.98,'lng':-96.73}))
        self.assertEqual(r['decision_trace'][9]['result'],'all_remaining_shared_stops_recomputed')
        self.assertEqual(len(r['order']['shared_route_legs']),2)


if __name__=='__main__':
    unittest.main(verbosity=2)
