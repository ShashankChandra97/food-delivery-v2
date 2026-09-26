"""Local transactional support service for the n8n food-delivery workflow."""
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from maps import Maps, MapUnavailable, AddressInvalid, miles

ROOT = Path(__file__).resolve().parent
TERMINAL = {'DELIVERED', 'CANCELLED', 'REJECTED'}
EVENTS = {'order_placed', 'customer_confirmed', 'restaurant_accepted', 'driver_accepted',
          'driver_picked_up', 'delivery_update', 'route_report', 'delivered', 'cancelled',
          'address_updated', 'human_review', 'region_toggle'}
STEPS = [
    ('Validate customer delivery address', 'RULE', 'Require a verified street and city.'),
    ('Check delivery service area', 'RULE', 'Use the configured boundary and emergency switches.'),
    ('Check current traffic conditions', 'MODEL', 'Use fresh provider travel estimates; missing delay requires review.'),
    ('Identify blocked or restricted roads', 'RULE', 'Eliminate closed, reported or vehicle-incompatible routes.'),
    ('Find the fastest feasible route', 'MODEL', 'Compare provider travel-time estimates.'),
    ('Determine whether an alternative route is needed', 'RULE', 'Avoid blocked routes and routes above the ETA limit.'),
    ('Check shared delivery', 'MODEL', 'Compare both stop sequences and every delivery deadline.'),
    ('Calculate delivery price', 'RULE', 'Apply configured base, distance, route time, toll and surge charges.'),
    ('Determine whether human review is required', 'HUMAN', 'A dispatcher resolves exceptions before dispatch.'),
    ('Re-evaluate route during delivery', 'MODEL', 'Recompute from the latest driver location.')]


def dumps(value):
    return json.dumps(value, separators=(',', ':'), sort_keys=True, allow_nan=False)


def stamp(value):
    from datetime import datetime
    if not isinstance(value, str):
        raise ValueError('Timestamp must be an ISO 8601 string with timezone')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp needs a timezone')
    return result.timestamp()


def iso(value=None):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).isoformat()


def numeric(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def point(p):
    return isinstance(p, dict) and numeric(p.get('lat')) and numeric(p.get('lng')) and -90 <= p['lat'] <= 90 and -180 <= p['lng'] <= 180


class Rejected(Exception):
    def __init__(self, message, code=422, status='REJECTED'):
        self.message, self.code, self.status = message, code, status


class Store:
    def __init__(self, db_path, config):
        self.path, self.config = Path(db_path), config
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.maps = Maps(config)
        with self.connect() as db:
            db.executescript((ROOT / 'schema.sql').read_text())
            for region in config['regions']:
                db.execute('INSERT OR IGNORE INTO region_switches VALUES(?,?,?,?)',
                           (region['id'], int(region['enabled']), 'Initial configuration', time.time()))
            db.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)', ('started_at', str(time.time())))

    def connect(self):
        db = sqlite3.connect(self.path, timeout=60)
        db.row_factory = sqlite3.Row
        return db

    def load(self, db, order_id):
        row = db.execute('SELECT data FROM orders WHERE order_id=?', (order_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, db, order):
        db.execute('INSERT INTO orders VALUES(?,?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET state=excluded.state,region=excluded.region,updated_at=excluded.updated_at,data=excluded.data',
                   (order['order_id'], order['state'], order.get('region', 'unknown'), order['created_at'], order['updated_at'], dumps(order)))

    def notify(self, db, order_id, kind, recipient, payload, key):
        db.execute('INSERT OR IGNORE INTO notifications(dedupe_key,created_at,order_id,recipient,kind,payload) VALUES(?,?,?,?,?,?)',
                   (key, time.time(), order_id, recipient, kind, dumps(payload)))

    def alert(self, db, key, kind, region, details, active=True):
        now = time.time()
        if active:
            existing = db.execute('SELECT resolved_at FROM alerts WHERE alert_key=?', (key,)).fetchone()
            db.execute('INSERT INTO alerts VALUES(?,?,?,?,?,NULL,?) ON CONFLICT(alert_key) DO UPDATE SET updated_at=excluded.updated_at,resolved_at=NULL,details=excluded.details',
                       (key, kind, region, now, now, dumps(details)))
            if existing is None or existing['resolved_at'] is not None:
                self.notify(db, None, kind, 'dispatch', details, f'alert:{key}:{now}')
        else:
            db.execute('UPDATE alerts SET resolved_at=?,updated_at=? WHERE alert_key=? AND resolved_at IS NULL', (now, now, key))

    def record(self, db, body, response, calls, start, duplicate=False):
        now = time.time()
        event_type = str(body.get('event_type', 'order_placed'))[:80]
        failure = int(response['http_status'] >= 400 or response.get('state') in ('QUARANTINED', 'REJECTED') or bool(response.get('technical_failure')))
        escalation = int(response.get('needs_human_review', False))
        safe_id = lambda value: value[:120] if isinstance(value, str) else None
        db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (response['run_id'], safe_id(body.get('event_id')), safe_id(body.get('order_id')), event_type,
                    response.get('region', 'unknown'), now, response.get('state', response.get('status', 'ERROR')),
                    response['http_status'], failure, escalation, int(duplicate), round((time.monotonic()-start)*1000, 2),
                    safe_id(body.get('_execution_id')), dumps(response)))
        for c in calls:
            db.execute('INSERT INTO api_calls(run_id,provider,operation,attempt,success,duration_ms,error,observed_at) VALUES(?,?,?,?,?,?,?,?)',
                       (response['run_id'], c['provider'], c['operation'], c['attempt'], int(c['success']), c['duration_ms'], c['error'], c['observed_at']))
        # Only redacted audit fields leave the machine when a Loki sink is configured.
        payload = {k: response.get(k) for k in ('run_id', 'order_id', 'event_type', 'state', 'http_status', 'region', 'needs_human_review', 'reasons')}
        payload.update({'failure': failure, 'duplicate': duplicate, 'policy_version': self.config['policy_version']})
        db.execute('INSERT INTO loki_outbox(run_id,created_at,payload) VALUES(?,?,?)', (response['run_id'], now, dumps(payload)))

    def trace(self):
        return [{'step': i+1, 'name': name, 'type': kind, 'reason': reason, 'result': 'not_applicable'}
                for i, (name, kind, reason) in enumerate(STEPS)]

    def evaluate(self, db, order, event, calls, trace):
        cfg = self.config
        reasons = []
        customer = order['customer']
        trace[0]['result'] = 'verified_address'
        region = next((r for r in cfg['regions'] if r['south'] <= customer['lat'] <= r['north'] and r['west'] <= customer['lng'] <= r['east']), None)
        order['region'] = region['id'] if region else 'outside'
        hard = []
        if not region:
            hard.append('outside_service_boundary')
        elif not db.execute('SELECT enabled FROM region_switches WHERE region=?', (region['id'],)).fetchone()[0]:
            hard.append('service_region_disabled')
        elif min(miles(customer, {'lat': region['south'], 'lng': customer['lng']}),
                 miles(customer, {'lat': region['north'], 'lng': customer['lng']}),
                 miles(customer, {'lat': customer['lat'], 'lng': region['west']}),
                 miles(customer, {'lat': customer['lat'], 'lng': region['east']})) <= cfg['boundary_review_miles']:
            reasons.append('borderline_service_boundary')
        trace[1]['result'] = hard[0] if hard else ('borderline' if reasons else 'inside_enabled_region')
        if hard:
            order.update({'review_reasons': hard, 'hard_blocks': hard, 'route': None, 'quotes': []})
            trace[8]['result'] = 'required'
            return
        origin = order.get('driver_location') if order.get('picked_up_at') else order['restaurant']
        args = {'origin': origin, 'destination': customer, 'vehicle': order['vehicle']}
        try:
            result = self.maps.call('routes', args, event.get('scenario', 'normal'), calls)
        except MapUnavailable as exc:
            order.update({'review_reasons': [str(exc)], 'hard_blocks': ['routing_unavailable'], 'route': None, 'quotes': [], 'technical_failure': True})
            trace[2]['result'], trace[8]['result'] = 'primary_and_backup_failed', 'required'
            return
        order['technical_failure'] = False
        trace[2].update({'result': result['provider'], 'observed_at': iso(result['observed_at']),
                         'freshness_basis': 'provider_response_received_at', 'confidence': None})
        routes = result['routes']
        blocked_ids = order.get('excluded_routes', [])
        feasible = [r for r in routes if not r['blocked'] and not r['restricted'] and r['id'] not in blocked_ids]
        trace[3].update({'result': 'filtered', 'excluded_count': len(routes)-len(feasible)})
        prep = 0 if order.get('picked_up_at') else order['restaurant']['preparation_minutes']
        for route in feasible:
            delay = route.get('traffic_delay_minutes')
            # Reject missing/negative/non-numeric provider delay, never coerce it to zero.
            if not numeric(delay) or delay < 0:
                route['minutes'] += cfg['historical_median_delay_minutes']
                route['traffic_delay_minutes'] = cfg['historical_median_delay_minutes']
                reasons.append('historical_traffic_fallback_requires_review')
            route['eta_minutes'] = math.ceil(prep + route['minutes'])
        preferred = next((r for r in routes if r.get('preferred')), None)
        acceptable = [r for r in feasible if r['eta_minutes'] <= cfg['max_eta_minutes']]
        route = min(acceptable or feasible, key=lambda r: r['eta_minutes']) if feasible else None
        trace[4].update({'result': 'selected' if route else 'no_feasible_route', 'route_id': route['id'] if route else None, 'confidence': None})
        alternative_needed = not preferred or preferred not in feasible or preferred.get('eta_minutes', math.inf) > cfg['max_eta_minutes']
        trace[5]['result'] = 'alternative_selected' if alternative_needed and route else ('no_alternative' if alternative_needed else 'not_needed')
        if not route:
            hard.append('no_safe_route')
        else:
            if route['eta_minutes'] > cfg['max_eta_minutes']:
                reasons.append('eta_exceeds_threshold')
            if route['toll'] is None:
                hard.append('toll_estimate_unavailable')
            if order.get('promised_at') and time.time()+route['eta_minutes']*60 > order['promised_at']:
                reasons.append('promised_window_at_risk')
            trace[7].update({'result': 'calculated' if route['toll'] is not None else 'unknown_tolls', 'formula': '(base + miles*per_mile + route_minutes*per_minute + toll)*surge'})
        order['route'] = route
        order['review_reasons'] = sorted(set(reasons+hard))
        order['hard_blocks'] = hard
        order['quotes'] = []
        if route and route['toll'] is not None:
            cost = round((cfg['base_fee']+route['distance_miles']*cfg['per_mile_fee']+route['minutes']*cfg['per_route_minute_fee']+route['toll'])*cfg['surge_multiplier'], 2)
            order['quotes'] = [{'option': 'standard', 'fee': cost, 'currency': 'USD', 'eta_minutes': route['eta_minutes'],
                                'promised_window_minutes': min(self.config['max_eta_minutes'], route['eta_minutes']+self.config['delivery_window_buffer_minutes'])}]
            # Sharing is offered only with another known, confirmed order and a checked sequence.
            candidate = self.shared_candidate(db, order, calls, event)
            if candidate:
                order['quotes'].append({'option': 'shared', 'fee': round(cost*(1-cfg['shared_discount']), 2),
                                        'currency': 'USD', 'promised_window_minutes': min(self.config['max_eta_minutes'], candidate['eta_minutes']+self.config['delivery_window_buffer_minutes']), **candidate})
                trace[6]['result'] = 'both_sequences_and_deadlines_checked'
            else:
                trace[6]['result'] = 'no_eligible_peer_within_capacity_and_windows'
        order['quote_id'] = str(uuid.uuid4())
        order['quote_expires_at'] = time.time()+cfg['traffic_freshness_seconds']
        trace[8]['result'] = 'required' if order['review_reasons'] else 'not_required'
        trace[9]['result'] = 'recomputed_from_driver_location' if order.get('picked_up_at') else 'awaiting_pickup'

    def shared_candidate(self, db, order, calls, event):
        if order.get('picked_up_at') or order.get('driver_id') or order.get('review_reasons'):
            return None
        peers = db.execute("SELECT data FROM orders WHERE state='AWAITING_DRIVER' AND order_id<>? ORDER BY created_at LIMIT 10", (order['order_id'],)).fetchall()
        for row in peers:
            peer = json.loads(row[0])
            if peer['restaurant']['id'] != order['restaurant']['id'] or not peer.get('allow_shared') or peer.get('driver_id') or peer['vehicle'] != order['vehicle']:
                continue
            candidates = []
            for first, second in [(order, peer), (peer, order)]:
                try:
                    first_leg = self.maps.call('routes', {'origin': order['restaurant'], 'destination': first['customer'], 'vehicle': order['vehicle']}, event.get('scenario', 'normal'), calls)
                    second_leg = self.maps.call('routes', {'origin': first['customer'], 'destination': second['customer'], 'vehicle': order['vehicle']}, event.get('scenario', 'normal'), calls)
                    forbidden = set(order.get('excluded_routes', [])+peer.get('excluded_routes', []))
                    safe = lambda result: [r for r in result['routes'] if not r['blocked'] and not r['restricted'] and r['id'] not in forbidden and numeric(r.get('traffic_delay_minutes')) and r['traffic_delay_minutes'] >= 0 and r['toll'] is not None]
                    a, b = safe(first_leg), safe(second_leg)
                    if not a or not b:
                        continue
                    a, b = min(a, key=lambda r: r['minutes']), min(b, key=lambda r: r['minutes'])
                    first_eta = math.ceil(order['restaurant']['preparation_minutes']+a['minutes'])
                    second_eta = math.ceil(first_eta+2+b['minutes'])
                    etas = {first['order_id']: first_eta, second['order_id']: second_eta}
                    if all(etas[o['order_id']] <= self.config['max_eta_minutes'] and
                           etas[o['order_id']] <= o['route']['eta_minutes']+self.config['shared_max_detour_minutes'] and
                           time.time()+etas[o['order_id']]*60 <= o.get('promised_at', time.time()+self.config['max_eta_minutes']*60)
                           for o in (order, peer)):
                        candidates.append({'peer_order_id': peer['order_id'], 'stop_order': [first['order_id'], second['order_id']],
                                           'eta_minutes': etas[order['order_id']], 'etas': etas, 'capacity_used': 2})
                except MapUnavailable:
                    return None
            if candidates:
                return min(candidates, key=lambda c: max(c['etas'].values()))
        return None

    def refresh_shared(self, db, order, body, calls, trace):
        remaining = [self.load(db, key) for key in order['shared_group']]
        remaining = [o for o in remaining if o and o['state'] != 'DELIVERED']
        if any(not o.get('picked_up_at') for o in remaining):
            order['review_reasons'].append('shared_pickup_incomplete')
            return
        # Preserve the confirmed stop order; recompute each remaining leg and deadline.
        origin = order['driver_location']
        total = 0
        legs = []
        exclusions = set(key for o in remaining for key in o.get('excluded_routes', []))
        try:
            for stop in remaining:
                result = self.maps.call('routes', {'origin': origin, 'destination': stop['customer'], 'vehicle': order['vehicle']}, body.get('scenario', 'normal'), calls)
                safe = [r for r in result['routes'] if not r['blocked'] and not r['restricted'] and r['id'] not in exclusions and numeric(r.get('traffic_delay_minutes')) and r['traffic_delay_minutes'] >= 0]
                if not safe:
                    raise MapUnavailable('shared_route_has_no_safe_leg')
                leg = min(safe, key=lambda r: r['minutes'])
                total += leg['minutes']
                leg['eta_minutes'] = math.ceil(total)
                legs.append({'order_id': stop['order_id'], **leg})
                target = order if stop['order_id'] == order['order_id'] else stop
                target['route'] = leg
                if time.time()+leg['eta_minutes']*60 > target.get('promised_at', math.inf):
                    target['review_reasons'] = sorted(set(target.get('review_reasons', [])+['promised_window_at_risk']))
                    target['state'] = 'HUMAN_REVIEW'
                    self.notify(db, target['order_id'], 'significant_delivery_delay', 'customer', {'eta_minutes': leg['eta_minutes']}, 'shared-delay:'+body['event_id']+':'+target['order_id'])
                    self.notify(db, target['order_id'], 'promised_window_at_risk', 'dispatch', {'eta_minutes': leg['eta_minutes']}, 'shared-dispatch:'+body['event_id']+':'+target['order_id'])
                if target is not order:
                    target['updated_at'] = time.time()
                    self.save(db, target)
                total += 2
                origin = stop['customer']
            order['shared_route_legs'] = legs
            trace[9]['result'] = 'all_remaining_shared_stops_recomputed'
        except MapUnavailable:
            order['review_reasons'].append('shared_route_replan_unavailable')
            order['hard_blocks'].append('shared_route_replan_unavailable')

    def process(self, incoming, admin=False):
        start, now = time.monotonic(), time.time()
        body = copy.deepcopy(incoming) if isinstance(incoming, dict) else {}
        for source, target in [('Customer_address', 'customer_address'), ('Restaurant_id', 'restaurant_id'), ('Order_timestamp', 'order_timestamp')]:
            if source in body and target not in body:
                body[target] = body.pop(source)
        body.setdefault('event_type', 'order_placed')
        event_id, order_id = body.get('event_id'), body.get('order_id')
        request_hash = hashlib.sha256(dumps({k: v for k, v in body.items() if not k.startswith('_')}).encode()).hexdigest()
        response = {'run_id': str(uuid.uuid4()), 'order_id': order_id, 'event_id': event_id,
                    'event_type': body['event_type'], 'http_status': 200, 'mode': self.config['mode'],
                    'policy_version': self.config['policy_version'], 'logging': {'sql': True, 'loki': 'queued' if os.environ.get('LOKI_PUSH_URL') else 'not_configured'},
                    'notifications': 'local_inbox', 'decision_trace': self.trace()}
        calls = []
        duplicate = False
        with self.connect() as db:
            # Serialize check-and-write: retries and two drivers cannot pass the same guard.
            db.execute('BEGIN IMMEDIATE')
            try:
                if not isinstance(incoming, dict):
                    raise Rejected('Request body must be a JSON object', 400)
                for field in ('event_id', 'order_id'):
                    value = body.get(field)
                    if not isinstance(value, str) or not value.strip() or len(value) > 120:
                        raise Rejected(f'{field} must be a nonempty string up to 120 characters', 400)
                if not isinstance(body['event_type'], str) or body['event_type'] not in EVENTS:
                    raise Rejected('Unsupported event_type', 400)
                if body['event_type'] in ('human_review', 'region_toggle') and not admin:
                    raise Rejected('Use the local admin controls for human decisions and emergency changes', 403)
                previous = db.execute('SELECT request_hash,response FROM events WHERE event_id=?', (event_id,)).fetchone()
                if previous:
                    if previous['request_hash'] != request_hash:
                        raise Rejected('event_id already used with a different payload', 409)
                    saved = json.loads(previous['response'])
                    saved.update({'original_run_id': saved['run_id'], 'run_id': response['run_id'], 'duplicate': True})
                    response, duplicate = saved, True
                else:
                    time_field = 'order_timestamp' if body['event_type'] == 'order_placed' else 'event_timestamp'
                    try:
                        request_time = stamp(body.get(time_field))
                    except (ValueError, TypeError):
                        raise Rejected(f'{time_field} must be ISO 8601 with a timezone', 400)
                    if round(abs(now-request_time), 6) > self.config['timestamp_window_seconds']:
                        raise Rejected(f'{time_field} must be within five minutes of server UTC', 422)
                    if self.config['mode'] != 'demo' and 'scenario' in body:
                        raise Rejected('Demo scenarios are disabled in live mode', 400)
                    self.apply_event(db, body, response, calls, now)
                    db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',
                               (event_id, order_id, body['event_type'], request_hash, now, dumps(response)))
            except Rejected as exc:
                response.update({'http_status': exc.code, 'status': exc.status, 'reasons': [exc.message], 'needs_human_review': False})
            self.record(db, body, response, calls, start, duplicate or response.get('duplicate', False))
        return response

    def apply_event(self, db, body, response, calls, now):
        event, order_id = body['event_type'], body['order_id']
        order = self.load(db, order_id)
        trace = response['decision_trace']
        actions = []
        def action(name, reversible, undo, owner='Delivery API'):
            actions.append({'action': name, 'result': 'completed_in_local_system', 'owner': owner, 'reversible': reversible, 'undo': undo})
        if event == 'region_toggle':
            region = body.get('region')
            if region not in [r['id'] for r in self.config['regions']] or not isinstance(body.get('enabled'), bool) or not body.get('reason'):
                raise Rejected('Known region, boolean enabled and reason are required', 400)
            db.execute('UPDATE region_switches SET enabled=?,reason=?,updated_at=? WHERE region=?', (int(body['enabled']), body['reason'], now, region))
            if not body['enabled']:
                for row in db.execute('SELECT data FROM orders WHERE region=?', (region,)).fetchall():
                    affected = json.loads(row[0])
                    if affected['state'] not in TERMINAL:
                        affected.update({'state_before_review': affected['state'], 'state': 'HUMAN_REVIEW', 'updated_at': now,
                                         'review_reasons': ['service_region_disabled'], 'hard_blocks': ['service_region_disabled']})
                        self.save(db, affected)
                        self.notify(db, affected['order_id'], 'emergency_region_disabled', 'dispatch', {'region': region}, f"region:{body['event_id']}:{affected['order_id']}")
            response.update({'status': 'REGION_UPDATED', 'region': region, 'enabled': body['enabled']})
            return
        if event == 'order_placed':
            if order:
                if (body.get('customer_address') != order.get('customer_address') or body.get('restaurant_id') != order.get('restaurant', {}).get('id')
                    or body.get('vehicle', 'car') != order.get('vehicle') or (body.get('allow_shared') is True) != order.get('allow_shared')):
                    raise Rejected('order_id already exists with different order details', 409)
                response.update({'state': order['state'], 'duplicate': True, 'order': order, 'region': order.get('region', 'unknown')})
                return
            restaurant = next((r for r in self.config['restaurants'] if r['id'] == body.get('restaurant_id') and r['active']), None)
            if not restaurant:
                self.notify(db, order_id, 'inactive_or_unknown_restaurant', 'customer_support', {}, 'vendor:'+body['event_id'])
                raise Rejected('restaurant_id must match an active vendor in config.json')
            if body.get('vehicle', 'car') not in ('car', 'bicycle', 'scooter'):
                raise Rejected('vehicle must be car, bicycle, or scooter', 400)
            order = {'order_id': order_id, 'restaurant': restaurant, 'customer_address': body.get('customer_address'),
                     'created_at': now, 'updated_at': now, 'state': 'AWAITING_CUSTOMER', 'region': 'unknown',
                     'vehicle': body.get('vehicle', 'car'), 'allow_shared': body.get('allow_shared') is True,
                     'review_reasons': [], 'hard_blocks': [], 'excluded_routes': [], 'driver_id': None}
            try:
                if not isinstance(order['customer_address'], str) or not order['customer_address'].strip():
                    raise AddressInvalid()
                order['customer'] = self.maps.call('geocode', {'address': order['customer_address']}, body.get('scenario', 'normal'), calls)
                if not point(order['customer']):
                    raise AddressInvalid()
                self.evaluate(db, order, body, calls, trace)
            except AddressInvalid:
                order.update({'state': 'QUARANTINED', 'review_reasons': ['address_not_geocodable_reenter_street_and_city'], 'hard_blocks': ['unverified_address']})
                trace[0]['result'] = 'failed'
                self.notify(db, order_id, 'reenter_address', 'customer', {}, 'address:'+body['event_id'])
                response['http_status'] = 422
            except MapUnavailable:
                order.update({'state': 'HUMAN_REVIEW', 'review_reasons': ['geocoding_providers_unavailable'], 'hard_blocks': ['unverified_address'], 'technical_failure': True})
            if order['review_reasons'] and order['state'] != 'QUARANTINED':
                order['state'] = 'HUMAN_REVIEW'
            action('create_delivery_order', True, 'cancelled event before restaurant acceptance or dispatch')
            if order.get('customer'):
                action('confirm_delivery_location', True, 'address_updated event before dispatch')
            if order.get('quotes'):
                action('display_available_delivery_options', True, 'select another option before confirmation')
                action('apply_dynamic_delivery_fee', True, 'recalculate before customer confirmation')
        else:
            if not order:
                raise Rejected('Unknown order_id: send order_placed first', 404)
            if order['state'] in TERMINAL:
                raise Rejected('Order is already terminal; no further changes allowed', 409)
            if event in ('driver_picked_up', 'delivery_update', 'route_report', 'delivered'):
                if not body.get('driver_id') or body['driver_id'] != order.get('driver_id'):
                    raise Rejected('Event must belong to the assigned driver', 409)
            if event == 'address_updated':
                if order.get('driver_id'):
                    raise Rejected('Address changes require dispatch review after assignment', 409)
                try:
                    if not isinstance(body.get('customer_address'), str):
                        raise AddressInvalid()
                    customer = self.maps.call('geocode', {'address': body['customer_address']}, body.get('scenario', 'normal'), calls)
                except AddressInvalid:
                    raise Rejected('Address could not be verified; re-enter street and city')
                except MapUnavailable:
                    raise Rejected('Address verification unavailable; previous address retained', 503)
                order.update({'customer_address': body['customer_address'], 'customer': customer, 'customer_confirmed': False,
                              'selected_option': None, 'confirmed_fee': None, 'state': 'AWAITING_CUSTOMER'})
                self.evaluate(db, order, body, calls, trace)
                if order['review_reasons']:
                    order['state'] = 'HUMAN_REVIEW'
                action('confirm_delivery_location', True, 'update again before dispatch')
            elif event == 'customer_confirmed':
                if order['state'] != 'AWAITING_CUSTOMER':
                    raise Rejected('Order must clear review before customer confirmation', 409)
                if body.get('quote_id') != order.get('quote_id'):
                    raise Rejected('quote_id must match the latest quote', 409)
                if now > order['quote_expires_at']:
                    self.evaluate(db, order, body, calls, trace)
                    order['state'] = 'HUMAN_REVIEW' if order['review_reasons'] else 'AWAITING_CUSTOMER'
                    response['http_status'] = 409
                    response['reasons'] = ['quote_expired_review_refreshed_quote']
                else:
                    quote = next((q for q in order['quotes'] if q['option'] == body.get('option', 'standard')), None)
                    if not quote:
                        raise Rejected('Selected option is unavailable', 409)
                    if quote['option'] == 'shared':
                        peer = self.load(db, quote['peer_order_id'])
                        if not peer or peer['state'] != 'AWAITING_DRIVER' or peer.get('driver_id'):
                            raise Rejected('Shared peer is no longer available; request a refreshed quote', 409)
                    order.update({'customer_confirmed': True, 'selected_option': quote, 'confirmed_fee': quote['fee'],
                                  'promised_at': now+quote['promised_window_minutes']*60, 'state': 'AWAITING_DRIVER' if order.get('restaurant_accepted') else 'AWAITING_RESTAURANT'})
                    action('confirm_customer_selected_delivery_option', True, 'cancel subject to restaurant preparation and dispatch gate')
            elif event == 'restaurant_accepted':
                if body.get('restaurant_id') != order['restaurant']['id']:
                    raise Rejected('restaurant_id must match this order', 409)
                if order['state'] not in ('AWAITING_CUSTOMER', 'AWAITING_RESTAURANT', 'AWAITING_DRIVER'):
                    raise Rejected('Restaurant cannot accept in this state', 409)
                if not order.get('restaurant_accepted'):
                    order.update({'restaurant_accepted': True, 'restaurant_accepted_at': now})
                    order['state'] = 'AWAITING_DRIVER' if order.get('customer_confirmed') else 'AWAITING_CUSTOMER'
                    action('create_route_job', True, 'dispatcher cancellation before assignment')
                else:
                    response.update({'state': order['state'], 'order': order, 'region': order.get('region', 'unknown'), 'duplicate': True})
                    return
            elif event == 'driver_accepted':
                if order['state'] != 'AWAITING_DRIVER' or order.get('driver_id'):
                    raise Rejected('Order already locked or not ready for driver acceptance', 409)
                driver_id = body.get('driver_id')
                if not isinstance(driver_id, str) or not driver_id.strip():
                    raise Rejected('driver_id is required', 400)
                occupied = [json.loads(r[0]) for r in db.execute("SELECT data FROM orders WHERE state NOT IN ('DELIVERED','CANCELLED','REJECTED')").fetchall()]
                if any(o.get('driver_id') == driver_id for o in occupied):
                    raise Rejected('Driver already has an active assignment', 409)
                selected = order['selected_option']
                peer = self.load(db, selected['peer_order_id']) if selected['option'] == 'shared' else None
                if selected['option'] == 'shared' and (not peer or peer['state'] != 'AWAITING_DRIVER' or peer.get('driver_id')):
                    raise Rejected('Shared peer no longer available', 409)
                if now > order['quote_expires_at']:
                    raise Rejected('Route data expired; dispatcher must refresh before assignment', 409)
                order.update({'driver_id': driver_id, 'assigned_at': now, 'state': 'ASSIGNED'})
                action('assign_order_to_driver', True, 'dispatcher reassignment before pickup', 'Dispatch')
                if peer:
                    peer.update({'driver_id': driver_id, 'assigned_at': now, 'state': 'ASSIGNED', 'updated_at': now,
                                 'shared_group': selected['stop_order']})
                    order['shared_group'] = selected['stop_order']
                    self.save(db, peer)
                    action('add_order_to_shared_multi_stop_route', True, 'dispatcher replans both orders before pickup', 'Dispatch')
                self.notify(db, order_id, 'driver_route', driver_id,
                            {'route': order['route'], 'stop_order': order.get('shared_group', [order_id])}, 'driver:'+body['event_id'])
                action('send_route_and_stop_sequence_to_driver', False, 'send a correcting route update; original notification remains', 'Dispatch')
            elif event == 'driver_picked_up':
                if order['state'] != 'ASSIGNED':
                    raise Rejected('Order must be assigned before pickup', 409)
                order.update({'state': 'IN_TRANSIT', 'picked_up_at': now, 'driver_location': {k: order['restaurant'][k] for k in ('lat', 'lng')}, 'location_at': now})
            elif event in ('delivery_update', 'route_report'):
                if not order.get('picked_up_at'):
                    raise Rejected('Order must be picked up before delivery updates', 409)
                location = body.get('driver_location')
                if not point(location):
                    raise Rejected('Valid driver_location.lat and driver_location.lng are required', 400)
                order.update({'driver_location': location, 'location_at': order.get('location_at', now) if body.get('_scheduled') else now})
                old_eta = order.get('route', {}).get('eta_minutes') if order.get('route') else None
                if event == 'route_report':
                    if body.get('reason') not in ('blocked', 'vehicle_restricted', 'route_deviation') or not body.get('route_id'):
                        raise Rejected('route_id and reason blocked/vehicle_restricted/route_deviation are required', 400)
                    order['excluded_routes'] = sorted(set(order['excluded_routes']+[body['route_id']]))
                    self.notify(db, order_id, 'incorrect_route_recommendation', 'dispatch', {'route_id': body['route_id'], 'reason': body['reason']}, 'wrong:'+body['event_id'])
                self.evaluate(db, order, body, calls, trace)
                if order.get('shared_group'):
                    self.refresh_shared(db, order, body, calls, trace)
                order['state'] = 'HUMAN_REVIEW' if order['review_reasons'] else 'IN_TRANSIT'
                action('recalculate_and_update_driver_route', True, 'subsequent delivery_update recalculates again', 'Dispatch')
                action('update_delivery_eta', True, 'subsequent update replaces ETA')
                eta = order['route']['eta_minutes'] if order.get('route') else None
                if eta is not None and (now+eta*60 > order.get('promised_at', math.inf) or (old_eta is not None and eta-old_eta >= self.config['significant_delay_minutes'])):
                    self.notify(db, order_id, 'significant_delivery_delay', 'customer', {'eta_minutes': eta, 'promised_at': order.get('promised_at')}, 'delay:'+body['event_id'])
                    self.notify(db, order_id, 'promised_window_at_risk', 'dispatch', {'eta_minutes': eta}, 'delay-dispatch:'+body['event_id'])
                    action('notify_customer_of_significant_delivery_delay', False, 'send a correction; retain original notification', 'Customer support')
                if order['route']:
                    self.notify(db, order_id, 'updated_driver_route', order['driver_id'], {'route': order['route'], 'shared_route_legs': order.get('shared_route_legs')}, 'reroute:'+body['event_id'])
            elif event == 'human_review':
                if not isinstance(body.get('reviewer'), str) or not body['reviewer'].strip() or not body.get('reason'):
                    raise Rejected('reviewer and reason are required', 400)
                decision = body.get('decision')
                if decision == 'reject':
                    if order.get('picked_up_at'):
                        raise Rejected('In-transit rejection requires a real dispatch recovery process', 409)
                    order['state'] = 'REJECTED'
                elif decision in ('approve', 'refresh'):
                    if not order.get('customer'):
                        raise Rejected('Correct the unverified address before review approval', 409)
                    self.evaluate(db, order, body, calls, trace)
                    if order.get('shared_group') and order.get('picked_up_at'):
                        self.refresh_shared(db, order, body, calls, trace)
                    if order['hard_blocks']:
                        order['state'] = 'HUMAN_REVIEW'
                        response['http_status'] = 409
                    elif decision == 'approve':
                        order['review_reasons'] = []
                        order['state'] = ('IN_TRANSIT' if order.get('picked_up_at') else 'ASSIGNED' if order.get('driver_id') else
                                          'AWAITING_DRIVER' if order.get('customer_confirmed') and order.get('restaurant_accepted') else
                                          'AWAITING_RESTAURANT' if order.get('customer_confirmed') else 'AWAITING_CUSTOMER')
                    else:
                        order['state'] = 'HUMAN_REVIEW' if order['review_reasons'] else ('IN_TRANSIT' if order.get('picked_up_at') else 'ASSIGNED' if order.get('driver_id') else 'AWAITING_DRIVER' if order.get('customer_confirmed') and order.get('restaurant_accepted') else 'AWAITING_RESTAURANT' if order.get('customer_confirmed') else 'AWAITING_CUSTOMER')
                else:
                    raise Rejected('decision must be approve, reject or refresh', 400)
                trace[8].update({'result': decision, 'reviewer': body['reviewer'], 'review_reason': body['reason']})
            elif event == 'delivered':
                if not order.get('picked_up_at') or not isinstance(body.get('delivery_proof'), str) or not body['delivery_proof'].strip():
                    raise Rejected('Pickup and delivery_proof reference are required', 409)
                for preceding in order.get('shared_group', []):
                    if preceding == order_id:
                        break
                    if self.load(db, preceding)['state'] != 'DELIVERED':
                        raise Rejected('Complete earlier shared stop first', 409)
                order.update({'state': 'DELIVERED', 'delivered_at': now, 'delivery_proof': body['delivery_proof'],
                              'actual_minutes': round((now-order['created_at'])/60, 2), 'late': now > order.get('promised_at', math.inf)})
                action('mark_order_as_delivered', False, 'append an audited correction through operations', 'Driver')
                if order['late']:
                    self.notify(db, order_id, 'delivery_completed_late', 'dispatch', {'actual_minutes': order['actual_minutes']}, 'late:'+body['event_id'])
            elif event == 'cancelled':
                if order.get('restaurant_accepted') or order.get('driver_id'):
                    raise Rejected('Preparation or assignment started; cancellation needs the business cancellation policy', 409)
                order['state'] = 'CANCELLED'
        order['updated_at'] = now
        self.save(db, order)
        needs_review = order['state'] == 'HUMAN_REVIEW'
        if needs_review:
            self.notify(db, order_id, 'human_review_required', 'dispatch', {'reasons': order['review_reasons'], 'route': order.get('route')}, 'review:'+body['event_id'])
        response.update({'state': order['state'], 'region': order.get('region', 'unknown'), 'order': order,
                         'needs_human_review': needs_review, 'technical_failure': order.get('technical_failure', False),
                         'reasons': response.get('reasons', order.get('review_reasons', [])), 'actions': actions})
        if needs_review and response['http_status'] == 200:
            response['http_status'] = 202

    def maintenance(self):
        now = time.time()
        with self.connect() as db:
            in_transit = [json.loads(r[0]) for r in db.execute("SELECT data FROM orders WHERE state='IN_TRANSIT' ORDER BY updated_at LIMIT 10")]
        for order in in_transit:
            if now-order.get('location_at', 0) <= self.config['driver_location_timeout_minutes']*60:
                self.process({'event_type': 'delivery_update', 'event_id': f"scheduled:{order['order_id']}:{int(now//60)}",
                              'order_id': order['order_id'], 'event_timestamp': iso(int(now//60)*60),
                              'driver_id': order['driver_id'], 'driver_location': order['driver_location'], '_scheduled': True})
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute("SELECT data FROM orders WHERE state NOT IN ('DELIVERED','CANCELLED','REJECTED')").fetchall():
                order = json.loads(row[0])
                reason = None
                if order['state'] == 'AWAITING_RESTAURANT' and now-order['updated_at'] > self.config['restaurant_timeout_minutes']*60:
                    reason = 'restaurant_confirmation_missing'
                elif order['state'] == 'AWAITING_DRIVER' and now-order['restaurant_accepted_at'] > self.config['driver_timeout_minutes']*60:
                    reason = 'driver_acceptance_timeout'
                elif order['state'] == 'ASSIGNED' and now-order['assigned_at'] > self.config['driver_timeout_minutes']*60:
                    reason = 'driver_pickup_timeout'
                elif order['state'] == 'IN_TRANSIT' and now-order.get('location_at', 0) > self.config['driver_location_timeout_minutes']*60:
                    reason = 'driver_location_stale'
                if reason:
                    order.update({'state_before_review': order['state'], 'state': 'HUMAN_REVIEW', 'review_reasons': [reason], 'updated_at': now})
                    self.save(db, order)
                    self.notify(db, order['order_id'], reason, 'dispatch', {}, f"timeout:{order['order_id']}:{reason}")
                    audit = {'run_id': str(uuid.uuid4()), 'http_status': 202, 'state': 'HUMAN_REVIEW', 'needs_human_review': True,
                             'region': order.get('region', 'unknown'), 'reasons': [reason], 'order_id': order['order_id']}
                    self.record(db, {'event_type': 'maintenance', 'order_id': order['order_id']}, audit, [], time.monotonic())
            two_hours = db.execute("SELECT count(*) AS n,sum(failure) AS f,min(created_at) AS first FROM runs WHERE created_at>? AND duplicate=0 AND event_type NOT IN ('maintenance','workflow_error','region_toggle')", (now-7200,)).fetchone()
            rate = (two_hours['f'] or 0)/two_hours['n'] if two_hours['n'] else 0
            # Require every populated 15-minute bucket over the full two-hour window above 25%.
            buckets = db.execute("SELECT CAST((?-created_at)/900 AS INTEGER) AS bucket,count(*) AS n,avg(failure) AS rate FROM runs WHERE created_at>? AND duplicate=0 AND event_type NOT IN ('maintenance','workflow_error','region_toggle') GROUP BY bucket", (now, now-7200)).fetchall()
            sustained = len(buckets) == 8 and all(b['rate'] > .25 for b in buckets)
            self.alert(db, 'failure-2h', 'failure_rate_above_25_percent_for_two_hours', 'all', {'rate': rate, 'runs': two_hours['n'], 'evaluated_buckets': len(buckets)}, sustained)
            days = db.execute("SELECT * FROM daily_metrics WHERE day>=date('now','-3 days') AND day<date('now') ORDER BY day").fetchall()
            escalation = len(days) == 3 and all(d['escalation_rate'] > .25 for d in days)
            self.alert(db, 'escalation-3d', 'escalation_above_25_percent_three_days', 'all', {'days': [dict(d) for d in days]}, escalation)
            for region in self.config['regions']:
                r = db.execute('SELECT count(*) n,avg(failure) rate FROM runs WHERE region=? AND created_at>? AND duplicate=0', (region['id'], now-900)).fetchone()
                active = r['n'] >= self.config['region_alert_minimum_runs'] and (r['rate'] or 0) > .25
                self.alert(db, 'region:'+region['id'], 'regional_failure_spike', region['id'], dict(r), active)
            recent = db.execute("SELECT max(created_at) FROM events WHERE event_type='order_placed'").fetchone()[0]
            started = float(db.execute("SELECT value FROM metadata WHERE key='started_at'").fetchone()[0])
            quiet = now-(recent or started) > self.config['missing_order_alert_minutes']*60
            self.alert(db, 'zero-orders', 'no_order_triggers_received', 'all', {'last_order_at': recent}, quiet)
        return {'status': 'maintenance_complete', 'at': iso(now)}

    def backup(self):
        destination = self.path.parent.parent/'backups'/('delivery-'+str(time.time_ns())+'.sqlite3')
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, sqlite3.connect(destination) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup failed integrity check')
        return {'status': 'backup_verified', 'path': str(destination), 'off_machine_backup': False}

    def workflow_error(self, body):
        response = {'run_id': str(uuid.uuid4()), 'state': 'WORKFLOW_ERROR', 'http_status': 503,
                    'needs_human_review': True, 'reasons': ['n8n_workflow_execution_failed'],
                    'execution_id': str(body.get('execution_id', 'unknown'))[:120]}
        with self.connect() as db:
            self.record(db, {'event_type': 'workflow_error', '_execution_id': response['execution_id']}, response, [], time.monotonic())
            self.notify(db, None, 'n8n_workflow_failed', 'dispatch', response, 'n8n-error:'+response['execution_id'])
        return response

    def flush_loki(self):
        url = os.environ.get('LOKI_PUSH_URL')
        if not url:
            return {'status': 'not_configured'}
        if not url.startswith(('http://localhost:', 'http://127.0.0.1:', 'https://')):
            return {'status': 'invalid_loki_url'}
        with self.connect() as db:
            rows = db.execute('SELECT * FROM loki_outbox WHERE sent_at IS NULL AND next_attempt_at<=? ORDER BY created_at LIMIT 50', (time.time(),)).fetchall()
        for row in rows:
            headers = {'Content-Type': 'application/json'}
            if os.environ.get('LOKI_AUTHORIZATION'):
                headers['Authorization'] = os.environ['LOKI_AUTHORIZATION']
            payload = {'streams': [{'stream': {'app': 'food-delivery', 'mode': self.config['mode']},
                                    'values': [[str(int(row['created_at']*1e9)), row['payload']]]}]}
            error = None
            try:
                req = urllib.request.Request(url, data=dumps(payload).encode(), headers=headers)
                with urllib.request.urlopen(req, timeout=3):
                    pass
            except Exception as exc:
                error = type(exc).__name__
            with self.connect() as db:
                db.execute('UPDATE loki_outbox SET attempts=attempts+1,next_attempt_at=?,sent_at=?,last_error=? WHERE run_id=?',
                           (time.time()+min(3600, 2**min(12, row['attempts']+1)), None if error else time.time(), error, row['run_id']))
        return {'status': 'processed', 'count': len(rows)}

    def snapshot(self):
        with self.connect() as db:
            orders = [json.loads(r[0]) for r in db.execute('SELECT data FROM orders ORDER BY updated_at DESC LIMIT 200')]
            runs = [dict(r) for r in db.execute('SELECT run_id,order_id,event_type,status,http_status,created_at,duration_ms,duplicate FROM runs ORDER BY created_at DESC LIMIT 200')]
            metrics = dict(db.execute("SELECT count(*) AS runs,COALESCE(sum(failure),0) AS failures,COALESCE(sum(escalation),0) AS escalations FROM runs WHERE duplicate=0 AND event_type NOT IN ('maintenance','workflow_error','region_toggle')").fetchone())
            metrics['completed_orders'] = db.execute("SELECT count(*) FROM orders WHERE state='DELIVERED'").fetchone()[0]
            metrics['failure_rate'] = metrics['failures']/metrics['runs'] if metrics['runs'] else None
            metrics['escalation_rate'] = metrics['escalations']/metrics['runs'] if metrics['runs'] else None
            return {'mode': self.config['mode'], 'orders': orders, 'runs': runs, 'metrics': metrics,
                    'notifications': [dict(r) for r in db.execute('SELECT * FROM notifications ORDER BY id DESC LIMIT 200')],
                    'alerts': [dict(r) for r in db.execute('SELECT * FROM alerts WHERE resolved_at IS NULL')],
                    'regions': [dict(r) for r in db.execute('SELECT * FROM region_switches')],
                    'loki': {'configured': bool(os.environ.get('LOKI_PUSH_URL')), 'queued': db.execute('SELECT count(*) FROM loki_outbox WHERE sent_at IS NULL').fetchone()[0]},
                    'daily_metrics': [dict(r) for r in db.execute('SELECT * FROM daily_metrics ORDER BY day DESC LIMIT 30')]}


class Handler(BaseHTTPRequestHandler):
    store = None
    rate_lock = threading.Lock()
    requests = []

    def log_message(self, *_):
        pass

    def reply(self, status, value, content_type='application/json'):
        raw = dumps(value).encode() if content_type == 'application/json' else value
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.headers.get('Host') not in {f'localhost:{self.server.server_port}', f'127.0.0.1:{self.server.server_port}'}:
            return self.reply(403, {'error': 'Local host required'})
        path = self.path.split('?')[0]
        if path == '/':
            return self.reply(200, (ROOT/'dashboard.html').read_bytes(), 'text/html; charset=utf-8')
        if path == '/lucide.min.js':
            return self.reply(200, (ROOT/'lucide.min.js').read_bytes(), 'text/javascript; charset=utf-8')
        if path == '/health':
            return self.reply(200, {'status': 'ok', 'mode': self.store.config['mode']})
        if path == '/api/status':
            url = self.store.config['n8n_base_url'].rstrip('/') + '/healthz'
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    n8n_ready = response.status == 200
            except (urllib.error.URLError, TimeoutError):
                n8n_ready = False
            return self.reply(200, {'service': 'ready', 'n8n': 'ready' if n8n_ready else 'unavailable',
                                    'mode': self.store.config['mode']})
        if path == '/api/snapshot':
            return self.reply(200, self.store.snapshot())
        if path == '/api/example':
            return self.reply(200, {'event_id': str(uuid.uuid4()), 'order_id': 'DEMO-'+uuid.uuid4().hex[:8], 'event_type': 'order_placed',
                                    'customer_address': '1200 E Campbell Rd, Richardson, TX', 'restaurant_id': 'REST-001',
                                    'order_timestamp': iso(), 'vehicle': 'car', 'allow_shared': True, 'scenario': 'normal'})
        if path.startswith('/api/runs/'):
            with self.store.connect() as db:
                row = db.execute('SELECT details FROM runs WHERE run_id=?', (path.removeprefix('/api/runs/'),)).fetchone()
                return self.reply(200, json.loads(row[0])) if row else self.reply(404, {'error': 'Run not found'})
        return self.reply(404, {'error': 'Not found'})

    def do_POST(self):
        if self.headers.get('Host') not in {f'localhost:{self.server.server_port}', f'127.0.0.1:{self.server.server_port}'}:
            return self.reply(403, {'error': 'Local host required'})
        origin = self.headers.get('Origin')
        allowed = {f'http://localhost:{self.server.server_port}', f'http://127.0.0.1:{self.server.server_port}'}
        if origin and origin not in allowed:
            return self.reply(403, {'error': 'Cross-origin requests are not allowed'})
        if not self.headers.get('Content-Type', '').startswith('application/json'):
            return self.reply(415, {'error': 'Use Content-Type: application/json'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 65536:
                return self.reply(413, {'error': 'JSON body must be between 1 and 65536 bytes'})
            body = json.loads(self.rfile.read(length), parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite number')))
        except (ValueError, json.JSONDecodeError):
            return self.reply(400, {'error': 'Invalid JSON'})
        if not isinstance(body, dict):
            return self.reply(400, {'error': 'Body must be a JSON object'})
        try:
            if self.path in ('/events', '/api/admin'):
                body = {k: v for k, v in body.items() if not k.startswith('_') or k == '_execution_id'}
                with self.rate_lock:
                    self.requests[:] = [t for t in self.requests if t > time.time()-60]
                    limited = len(self.requests) >= self.store.config['max_requests_per_minute']
                    if not limited:
                        self.requests.append(time.time())
                if limited:
                    with self.store.connect() as db:
                        self.store.alert(db, 'request-spike', 'request_rate_limit_exceeded', 'all', {'limit_per_minute': self.store.config['max_requests_per_minute']})
                    return self.reply(429, {'error': 'Rate limit exceeded; retry in 60 seconds'})
                if self.path == '/api/admin' and body.get('event_type') not in ('human_review', 'region_toggle'):
                    return self.reply(400, {'error': 'Admin endpoint only accepts human_review and region_toggle'})
                result = self.store.process(body, admin=self.path == '/api/admin')
                return self.reply(result['http_status'], result)
            if self.path == '/maintenance':
                return self.reply(200, self.store.maintenance())
            if self.path == '/backup':
                return self.reply(200, self.store.backup())
            if self.path == '/workflow-errors':
                return self.reply(200, self.store.workflow_error(body))
            if self.path == '/loki/flush':
                return self.reply(200, self.store.flush_loki())
            if self.path == '/api/call':
                mode = body.get('target', 'n8n-production')
                paths = {'n8n-production': '/webhook/', 'n8n-test': '/webhook-test/'}
                if mode not in paths:
                    return self.reply(400, {'error': 'Choose n8n-production or n8n-test'})
                url = self.store.config['n8n_base_url'].rstrip('/')+paths[mode]+self.store.config['webhook_path']
                request = urllib.request.Request(url, data=dumps(body.get('payload', {})).encode(), headers={'Content-Type': 'application/json'})
                try:
                    with urllib.request.urlopen(request, timeout=120) as upstream:
                        text = upstream.read().decode()
                        return self.reply(upstream.status, {'target': url, 'response': json.loads(text) if text else None})
                except urllib.error.HTTPError as exc:
                    raw = exc.read().decode()
                    try:
                        error = json.loads(raw)
                    except ValueError:
                        error = {'message': raw[:500]}
                    return self.reply(exc.code, {'target': url, 'response': error})
                except (urllib.error.URLError, TimeoutError):
                    return self.reply(503, {'error': 'n8n is unreachable or timed out; check it is running and retry the SAME event_id', 'target': url})
            return self.reply(404, {'error': 'Not found'})
        except Exception as exc:
            # Do not acknowledge an event when the SQL transaction failed.
            print('Request failed:', type(exc).__name__, flush=True)
            return self.reply(503, {'error': 'Processing or SQL persistence failed; retry with the SAME event_id', 'type': type(exc).__name__})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int)
    parser.add_argument('--db', default=str(ROOT/'data'/'delivery.sqlite3'))
    args = parser.parse_args()
    config = json.loads((ROOT/'config.json').read_text())
    if config['mode'] not in ('demo', 'live'):
        raise SystemExit('config.mode must be demo or live')
    Handler.store = Store(args.db, config)
    server = ThreadingHTTPServer(('127.0.0.1', args.port or config['port']), Handler)
    print(f"Food Delivery Control: http://localhost:{server.server_port} ({config['mode']})", flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
