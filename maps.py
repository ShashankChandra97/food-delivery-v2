"""Map adapters. Demo fixtures are explicit; live mode never falls back to fixtures."""
import hashlib
import json
import math
import os
import time
import urllib.parse
import urllib.request


class MapUnavailable(Exception):
    pass


class AddressInvalid(Exception):
    pass


def miles(a, b):
    lat1, lat2 = map(math.radians, (a['lat'], b['lat']))
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(math.radians(b['lng']-a['lng'])/2)**2
    return 3958.8 * 2 * math.asin(min(1, math.sqrt(h)))


def http_json(url, timeout, body=None, headers=None):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json', **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


class Maps:
    def __init__(self, config):
        self.config = config

    def call(self, operation, args, scenario, calls):
        names = [self.config['primary_provider'], self.config['backup_provider']]
        # Four total attempts: primary twice, then backup twice, then a human gate.
        for attempt, provider in enumerate([names[0], names[0], names[1], names[1]], 1):
            start = time.monotonic()
            error = None
            try:
                if self.config['mode'] == 'demo':
                    if scenario == 'both_apis_down' or (scenario in ('primary_down', 'stale_primary') and attempt <= 2):
                        raise MapUnavailable('stale_data' if scenario == 'stale_primary' else 'provider_unavailable')
                    result = self.demo(operation, args, scenario)
                else:
                    result = getattr(self, provider)(operation, args)
                if time.time()-result['observed_at'] > self.config['traffic_freshness_seconds']:
                    raise MapUnavailable('stale_data')
                result['provider'] = ('demo-' if self.config['mode'] == 'demo' else '') + provider
                return result
            except AddressInvalid:
                error = 'address_not_verified'
                raise
            except Exception as exc:
                # URL-bearing exceptions can contain credentials. Persist only safe categories.
                error = type(exc).__name__ if not isinstance(exc, MapUnavailable) else str(exc)
            finally:
                calls.append({'provider': provider, 'operation': operation, 'attempt': attempt,
                              'success': error is None, 'error': error,
                              'duration_ms': round((time.monotonic()-start)*1000, 2), 'observed_at': time.time()})
        raise MapUnavailable('all_providers_failed_after_four_attempts')

    def demo(self, operation, args, scenario):
        if operation == 'geocode':
            address = self.config['demo_addresses'].get(args['address'])
            if not address:
                raise AddressInvalid('Address not in the explicit demo address book')
            return {**address, 'observed_at': time.time()}
        points = [args['origin'], *args.get('stops', []), args['destination']]
        distance = sum(miles(a, b) for a, b in zip(points, points[1:])) * 1.25
        traffic = 35 if scenario == 'heavy_traffic' else 5
        variants = []
        for index, factor in enumerate([1.0, 1.18, 1.35] if not args.get('stops') else [1.0]):
            variants.append({'id': f'demo-route-{index+1}', 'distance_miles': distance*factor,
                             'minutes': max(1, distance*factor*2.8)+traffic,
                             'traffic_delay_minutes': None if scenario == 'invalid_traffic' else traffic,
                             'toll': 1.5 if index == 0 else 0,
                             'blocked': scenario == 'all_blocked' or (scenario == 'blocked_preferred' and index == 0),
                             'restricted': scenario == 'vehicle_restricted' and index == 0,
                             'preferred': index == 0,
                             'stops': points[1:], 'observed_at': time.time()})
        return {'routes': variants, 'observed_at': time.time()}

    def google(self, operation, args):
        key = os.environ.get('GOOGLE_MAPS_API_KEY')
        if not key:
            raise MapUnavailable('google_key_not_configured')
        timeout = self.config['map_timeout_seconds']
        if operation == 'geocode':
            result = http_json('https://maps.googleapis.com/maps/api/geocode/json?' +
                               urllib.parse.urlencode({'address': args['address'], 'key': key}), timeout)
            if result.get('status') == 'ZERO_RESULTS':
                raise AddressInvalid()
            if result.get('status') != 'OK':
                raise MapUnavailable('google_geocode_error')
            item = result['results'][0]
            types = {t for c in item.get('address_components', []) for t in c['types']}
            if item.get('partial_match') or 'route' not in types or not types.intersection({'locality', 'postal_town', 'administrative_area_level_3'}):
                raise AddressInvalid()
            return {**item['geometry']['location'], 'observed_at': time.time()}
        def waypoint(p):
            return {'location': {'latLng': {'latitude': p['lat'], 'longitude': p['lng']}}}
        if args.get('vehicle', 'car') != 'car':
            raise MapUnavailable('google_vehicle_profile_not_supported')
        payload = {'origin': waypoint(args['origin']), 'destination': waypoint(args['destination']),
                   'intermediates': [waypoint(p) for p in args.get('stops', [])], 'travelMode': 'DRIVE',
                   'routingPreference': 'TRAFFIC_AWARE', 'computeAlternativeRoutes': not bool(args.get('stops')),
                   'extraComputations': ['TOLLS']}
        fields = 'routes.duration,routes.staticDuration,routes.distanceMeters,routes.polyline.encodedPolyline,routes.travelAdvisory.tollInfo,routes.routeLabels'
        result = http_json('https://routes.googleapis.com/directions/v2:computeRoutes', timeout, payload,
                           {'X-Goog-Api-Key': key, 'X-Goog-FieldMask': fields})
        routes = []
        for r in result.get('routes', []):
            duration = float(r['duration'].removesuffix('s'))/60
            static = float(r['staticDuration'].removesuffix('s'))/60
            toll_info = r.get('travelAdvisory', {}).get('tollInfo')
            prices = (toll_info or {}).get('estimatedPrice', [])
            toll = 0 if toll_info is None else None
            if prices and all(p.get('currencyCode') == 'USD' for p in prices):
                toll = sum(float(p.get('units', 0))+float(p.get('nanos', 0))/1e9 for p in prices)
            polyline = r.get('polyline', {}).get('encodedPolyline', json.dumps(r))
            routes.append({'id': hashlib.sha256(polyline.encode()).hexdigest()[:20],
                           'distance_miles': r['distanceMeters']/1609.344, 'minutes': duration,
                           'traffic_delay_minutes': max(0, duration-static), 'toll': toll,
                           'blocked': False, 'restricted': False,
                           'preferred': 'DEFAULT_ROUTE' in r.get('routeLabels', []),
                           'stops': [*args.get('stops', []), args['destination']], 'observed_at': time.time()})
        return {'routes': routes, 'observed_at': time.time()}

    def here(self, operation, args):
        key = os.environ.get('HERE_API_KEY')
        if not key:
            raise MapUnavailable('here_key_not_configured')
        timeout = self.config['map_timeout_seconds']
        if operation == 'geocode':
            result = http_json('https://geocode.search.hereapi.com/v1/geocode?' +
                               urllib.parse.urlencode({'q': args['address'], 'apiKey': key}), timeout)
            items = result.get('items', [])
            if not items or not items[0].get('address', {}).get('street') or not items[0].get('address', {}).get('city'):
                raise AddressInvalid()
            return {**items[0]['position'], 'observed_at': time.time()}
        position = lambda p: f"{p['lat']},{p['lng']}"
        vehicle = args.get('vehicle', 'car')
        if vehicle not in ('car', 'bicycle', 'scooter'):
            raise MapUnavailable('unsupported_vehicle_profile')
        query = {'apiKey': key, 'origin': position(args['origin']), 'destination': position(args['destination']),
                 'transportMode': vehicle, 'routingMode': 'fast', 'alternatives': 2,
                 'return': 'summary,polyline,tolls', 'currency': 'USD',
                 'via': [position(p) for p in args.get('stops', [])]}
        result = http_json('https://router.hereapi.com/v8/routes?' + urllib.parse.urlencode(query, doseq=True), timeout)
        routes = []
        for index, r in enumerate(result.get('routes', [])):
            sections = r['sections']
            duration = sum(s['summary']['duration'] for s in sections)/60
            base = sum(s['summary'].get('baseDuration', s['summary']['duration']) for s in sections)/60
            toll = 0
            for section in sections:
                for t in section.get('tolls', []):
                    prices = [f['price']['value'] for f in t.get('fares', []) if f.get('price', {}).get('currency') == 'USD']
                    if not prices:
                        toll = None
                        break
                    if toll is not None:
                        toll += min(prices)
            notices = [n for s in sections for n in s.get('notices', [])]
            routes.append({'id': hashlib.sha256(''.join(s.get('polyline', '') for s in sections).encode()).hexdigest()[:20],
                           'distance_miles': sum(s['summary']['length'] for s in sections)/1609.344,
                           'minutes': duration, 'traffic_delay_minutes': max(0, duration-base), 'toll': toll,
                           'blocked': False, 'restricted': any(n.get('severity') == 'critical' for n in notices),
                           'preferred': index == 0, 'stops': [*args.get('stops', []), args['destination']],
                           'observed_at': time.time()})
        return {'routes': routes, 'observed_at': time.time()}
