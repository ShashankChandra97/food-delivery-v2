"""One-command POST with a fresh timestamp; optionally retry an exact saved event."""
import argparse
import json
from pathlib import Path
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

parser = argparse.ArgumentParser()
parser.add_argument('--test', action='store_true')
parser.add_argument('--retry', action='store_true')
args = parser.parse_args()
saved = Path(__file__).resolve().parent/'last-request.json'
if args.retry:
    body = json.loads(saved.read_text())
else:
    body = {'event_id':str(uuid.uuid4()),'order_id':'DEMO-'+uuid.uuid4().hex[:8], 'event_type':'order_placed',
            'customer_address':'1200 E Campbell Rd, Richardson, TX','restaurant_id':'REST-001',
            'order_timestamp':datetime.now(timezone.utc).isoformat(),'scenario':'normal'}
    saved.write_text(json.dumps(body,indent=2)+'\n')
url = 'http://localhost:5678/'+('webhook-test' if args.test else 'webhook')+'/food-delivery-serviceability-v2'
request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={'Content-Type':'application/json'})
try:
    with urllib.request.urlopen(request,timeout=120) as response:
        print('HTTP',response.status)
        print(json.dumps(json.load(response),indent=2))
except urllib.error.HTTPError as exc:
    print('HTTP',exc.code, exc.read().decode())
    if exc.code==404:
        print('Test: click Execute workflow, then call once. Production: import and publish the v2 workflow.')
    raise SystemExit(1)
except urllib.error.URLError:
    print('n8n is unreachable. Start n8n and START.command, then retry with --retry.')
    raise SystemExit(1)
