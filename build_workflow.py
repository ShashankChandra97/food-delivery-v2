"""Generate the importable n8n workflow and Postman collection."""
import json
from pathlib import Path
import uuid

ROOT = Path(__file__).resolve().parent
nodes, connections = [], {}


def node(name, kind, version, position, parameters, **extra):
    nodes.append({'id': str(uuid.uuid4()), 'name': name, 'type': 'n8n-nodes-base.'+kind,
                  'typeVersion': version, 'position': position, 'parameters': parameters, **extra})


def connect(source, target, output=0):
    main = connections.setdefault(source, {'main': []})['main']
    while len(main) <= output:
        main.append([])
    main[output].append({'node': target, 'type': 'main', 'index': 0})


node('Food Delivery POST', 'webhook', 2, [-680, 0], {'httpMethod': 'POST', 'path': 'food-delivery-serviceability-v2',
     'responseMode': 'responseNode', 'options': {}}, webhookId='food-delivery-v2')
node('Prepare Event Envelope', 'code', 2, [-440, 0], {'jsCode': "const body = $json.body;\nif (!body || typeof body !== 'object' || Array.isArray(body)) return [{json:{payload:body ?? null}}];\nconst payload = {...body, event_type:body.event_type ?? 'order_placed', _execution_id:$execution.id};\nfor (const [source,target] of [['Customer_address','customer_address'],['Restaurant_id','restaurant_id'],['Order_timestamp','order_timestamp']]) { if (source in payload && !(target in payload)) {payload[target]=payload[source]; delete payload[source];} }\nreturn [{json:{payload}}];"})
node('SQL Transaction - Validate Route Lock Log', 'httpRequest', 4.2, [-180, 0], {
    'method': 'POST', 'url': 'http://127.0.0.1:8787/events', 'sendBody': True, 'specifyBody': 'json',
    'jsonBody': '={{ $json.payload }}', 'options': {'timeout': 90000, 'response': {'response': {'fullResponse': True, 'neverError': True, 'responseFormat': 'json'}}}},
    retryOnFail=True, maxTries=4, waitBetweenTries=1000, onError='continueRegularOutput',
    notesInFlow=True, notes='Local SQLite transaction: event dedupe, active vendor lookup, geocoding, service boundary, traffic, safe alternatives, shared route, pricing, customer and restaurant gates, driver lock, SQL audit and notification inbox. Four map attempts then human review. Business 4xx responses are not retried.')
node('Normalize HTTP Response', 'code', 2, [100, 0], {'jsCode': "const r = $json;\nif (r.statusCode && r.body) return [{json:{code:r.statusCode, body:r.body}}];\nreturn [{json:{code:503,body:{status:'SERVICE_UNAVAILABLE', logging:{sql:false,n8n_execution:true}, message:'Local SQL service unreachable after retries. Start START.command and retry the SAME event_id.'}}}];"})
node('Return Status and Decision', 'respondToWebhook', 1.4, [340, 0], {'respondWith': 'json', 'responseBody': '={{ $json.body }}', 'options': {'responseCode': '={{ $json.code }}'}})
node('Check Timeouts Every Minute', 'scheduleTrigger', 1.2, [-680, 320], {'rule': {'interval': [{'field': 'minutes', 'minutesInterval': 1}]}})
node('Escalate Missing Events and Evaluate Alerts', 'httpRequest', 4.2, [-400, 320], {
    'method': 'POST', 'url': 'http://127.0.0.1:8787/maintenance', 'sendBody': True, 'specifyBody': 'json', 'jsonBody': '{}', 'options': {'timeout': 90000}},
    retryOnFail=True, maxTries=4, waitBetweenTries=1000)
node('Flush Durable Loki Outbox', 'httpRequest', 4.2, [-100, 320], {
    'method': 'POST', 'url': 'http://127.0.0.1:8787/loki/flush', 'sendBody': True, 'specifyBody': 'json', 'jsonBody': '{}', 'options': {'timeout': 180000}},
    notesInFlow=True, notes='No-op until LOKI_PUSH_URL is configured. Pending SQL outbox rows survive restarts; failures retry with backoff. Local SQL logging does not depend on Loki availability.')
node('Nightly SQL Backup', 'scheduleTrigger', 1.2, [-680, 590], {'rule': {'interval': [{'field': 'cronExpression', 'expression': '0 2 * * *'}]}})
node('Create and Verify Recovery Snapshot', 'httpRequest', 4.2, [-400, 590], {
    'method': 'POST', 'url': 'http://127.0.0.1:8787/backup', 'sendBody': True, 'specifyBody': 'json', 'jsonBody': '{}', 'options': {'timeout': 90000}},
    retryOnFail=True, maxTries=4, waitBetweenTries=1000)
node('Implementation and Demo Scope', 'stickyNote', 1, [-690, -400], {
    'content': '## Food Delivery Spec Pack v2\nLocal demo: start START.command first. Import and publish this workflow.\nPOST /webhook/food-delivery-serviceability-v2\nControl page: http://localhost:8787\n\nSQLite support service owns atomic state, driver locks, routing decisions and persistent logs. n8n owns intake, retries, HTTP responses and scheduled supervision. Open each response decision_trace for all ten workbook steps.\n\nMap results are DEMO fixtures. Notifications are written to a LOCAL INBOX, not delivered to real customers. Live providers and Loki require setup. See SPEC_AUDIT.md for all eight sheets and explicit assumptions.',
    'width': 1000, 'height': 320})
connect('Food Delivery POST', 'Prepare Event Envelope')
event_branches = [
    ('order_placed', '1 Validate Address - Route - Quote - Log'),
    ('address_updated', '2 Correct Address and Refresh Quote'),
    ('customer_confirmed', '3 Confirm Option and Freeze Price'),
    ('restaurant_accepted', '4 Restaurant Confirms Route Job'),
    ('driver_accepted', '5 Lock Driver and Shared Capacity'),
    ('driver_picked_up', '6 Confirm Pickup and Start Delivery'),
    ('delivery_update', '7 Recheck Traffic Route ETA and Delay'),
    ('route_report', '8 Exclude Wrong Route and Recalculate'),
    ('delivered', '9 Verify Handoff and Mark Delivered'),
    ('cancelled', '10 Check Cancellation Policy')]
template = next(n for n in nodes if n['name'] == 'SQL Transaction - Validate Route Lock Log')
nodes.remove(template)
rules = []
for index, (event, name) in enumerate(event_branches):
    rules.append({'renameOutput': True, 'outputKey': event,
                  'conditions': {'options': {'caseSensitive': True, 'leftValue': '', 'typeValidation': 'strict', 'version': 2},
                                 'conditions': [{'id': str(uuid.uuid4()), 'leftValue': '={{ $json.payload?.event_type }}',
                                                 'rightValue': event, 'operator': {'type': 'string', 'operation': 'equals'}}],
                                 'combinator': 'and'}})
    branch = json.loads(json.dumps(template))
    branch.update({'id': str(uuid.uuid4()), 'name': name, 'position': [100, -880+index*200]})
    nodes.append(branch)
    connect('Route Delivery Event', name, index)
    connect(name, 'Normalize HTTP Response')
node('Route Delivery Event', 'switch', 3.2, [-180, 0], {'rules': {'values': rules}, 'options': {'fallbackOutput': 'extra'}})
fallback = json.loads(json.dumps(template))
fallback.update({'id': str(uuid.uuid4()), 'name': 'Reject Unsupported Event and Save Audit', 'position': [100, 1120]})
nodes.append(fallback)
connect('Route Delivery Event', fallback['name'], len(event_branches))
connect(fallback['name'], 'Normalize HTTP Response')
connect('Prepare Event Envelope', 'Route Delivery Event')
for n in nodes:
    if n['name'] == 'Normalize HTTP Response': n['position'] = [460, 0]
    if n['name'] == 'Return Status and Decision': n['position'] = [700, 0]
    if n['name'] == 'Implementation and Demo Scope': n['position'] = [-720,-1460]
    if n['name'] in ('Check Timeouts Every Minute', 'Escalate Missing Events and Evaluate Alerts', 'Flush Durable Loki Outbox'):
        n['position'][1] = 1450
    if n['name'] in ('Nightly SQL Backup', 'Create and Verify Recovery Snapshot'):
        n['position'][1] = 1730
connect('Normalize HTTP Response', 'Return Status and Decision')
connect('Check Timeouts Every Minute', 'Escalate Missing Events and Evaluate Alerts')
connect('Escalate Missing Events and Evaluate Alerts', 'Flush Durable Loki Outbox')
connect('Nightly SQL Backup', 'Create and Verify Recovery Snapshot')
workflow = {'id': 'foodDeliverySpecV2', 'versionId': str(uuid.uuid4()), 'name': 'Food Delivery v2 - Spec Pack and SQL Logging', 'nodes': nodes, 'connections': connections,
            'active': False, 'settings': {'executionOrder': 'v1', 'timezone': 'UTC', 'saveDataSuccessExecution': 'all',
            'saveDataErrorExecution': 'all', 'saveManualExecutions': True, 'saveExecutionProgress': True,
            'errorWorkflow': 'foodDeliveryV2Errors'}, 'pinData': {}}
(ROOT/'food-delivery-v2.n8n.json').write_text(json.dumps(workflow, indent=2)+'\n')
# A separate manual workflow verifies the actual HTTP node in an isolated n8n runtime.
manual = json.loads(json.dumps(workflow))
manual_names = {'Prepare Event Envelope', 'Route Delivery Event', 'Normalize HTTP Response', fallback['name'], *[name for _,name in event_branches]}
manual['nodes'] = [n for n in manual['nodes'] if n['name'] in manual_names]
manual['nodes'].insert(0, {'id': str(uuid.uuid4()), 'name': 'Manual Test', 'type': 'n8n-nodes-base.manualTrigger', 'typeVersion': 1, 'position': [-900, 0], 'parameters': {}})
manual['nodes'].insert(1, {'id': str(uuid.uuid4()), 'name': 'Sample Order', 'type': 'n8n-nodes-base.code', 'typeVersion': 2, 'position': [-680, 0], 'parameters': {'jsCode': "return [{json:{body:{event_type:'order_placed',event_id:'n8n-test-'+Date.now(),order_id:'N8N-'+Date.now(),customer_address:'1200 E Campbell Rd, Richardson, TX',restaurant_id:'REST-001',order_timestamp:new Date().toISOString(),scenario:'normal'}}}];"}})
manual['connections'] = {k:v for k,v in connections.items() if k in manual_names and k != 'Normalize HTTP Response'}
manual['connections']['Manual Test'] = {'main': [[{'node':'Sample Order','type':'main','index':0}]]}
manual['connections']['Sample Order'] = {'main': [[{'node':'Prepare Event Envelope','type':'main','index':0}]]}
manual['name'] = 'Food Delivery v2 - Isolated Integration Test'
manual['id'] = 'foodDeliveryV2Test'
manual['settings'].pop('errorWorkflow', None)
(ROOT/'integration-test.n8n.json').write_text(json.dumps(manual, indent=2)+'\n')
error_workflow = {'id': 'foodDeliveryV2Errors', 'versionId': str(uuid.uuid4()),
    'name': 'Food Delivery v2 - Workflow Error Log', 'active': False,
    'nodes': [
        {'id': str(uuid.uuid4()), 'name': 'Workflow Failed', 'type': 'n8n-nodes-base.errorTrigger', 'typeVersion': 1, 'position': [0,0], 'parameters': {}},
        {'id': str(uuid.uuid4()), 'name': 'Persist Failure and Notify Local Dispatch', 'type': 'n8n-nodes-base.httpRequest', 'typeVersion': 4.2, 'position': [300,0],
         'parameters': {'method': 'POST', 'url': 'http://127.0.0.1:8787/workflow-errors', 'sendBody': True, 'specifyBody': 'json',
                        'jsonBody': '={{ {execution_id: $json.execution?.id || $json.trigger?.error?.timestamp || "unknown"} }}', 'options': {'timeout': 10000}},
         'retryOnFail': True, 'maxTries': 4, 'waitBetweenTries': 1000}],
    'connections': {'Workflow Failed': {'main': [[{'node': 'Persist Failure and Notify Local Dispatch', 'type': 'main', 'index': 0}]]}},
    'settings': {'executionOrder':'v1','saveDataErrorExecution':'all','saveDataSuccessExecution':'all'}}
(ROOT/'food-delivery-v2-errors.n8n.json').write_text(json.dumps(error_workflow,indent=2)+'\n')
(ROOT/'IMPORT-ALL.n8n.json').write_text(json.dumps([error_workflow,workflow],indent=2)+'\n')
collection = {'info': {'name': 'Food Delivery v2', 'schema': 'https://schema.getpostman.com/json/collection/v2.1.0/collection.json'},
              'variable': [{'key':'baseUrl','value':'http://localhost:5678/webhook/food-delivery-serviceability-v2'},
                           {'key':'orderId','value':'DEMO-POSTMAN-001'}, {'key':'quoteId','value':''}], 'item': []}
for name, data in [
    ('1 Place order', {'event_type':'order_placed','customer_address':'1200 E Campbell Rd, Richardson, TX','restaurant_id':'REST-001','order_timestamp':'{{requestTime}}','allow_shared':True,'scenario':'normal'}),
    ('2 Confirm option', {'event_type':'customer_confirmed','quote_id':'{{quoteId}}','option':'standard','event_timestamp':'{{requestTime}}'}),
    ('3 Restaurant accepts', {'event_type':'restaurant_accepted','restaurant_id':'REST-001','event_timestamp':'{{requestTime}}'}),
    ('4 Driver accepts', {'event_type':'driver_accepted','driver_id':'DRIVER-DEMO-001','event_timestamp':'{{requestTime}}'}),
    ('5 Driver picks up', {'event_type':'driver_picked_up','driver_id':'DRIVER-DEMO-001','event_timestamp':'{{requestTime}}'}),
    ('6 Update location', {'event_type':'delivery_update','driver_id':'DRIVER-DEMO-001','driver_location':{'lat':32.98,'lng':-96.73},'event_timestamp':'{{requestTime}}'}),
    ('7 Mark delivered', {'event_type':'delivered','driver_id':'DRIVER-DEMO-001','delivery_proof':'demo-handoff-reference','event_timestamp':'{{requestTime}}'})]:
    data.update({'event_id':'{{eventId}}','order_id':'{{orderId}}'})
    collection['item'].append({'name':name,'event':[
        {'listen':'prerequest','script':{'type':'text/javascript','exec':["pm.variables.set('requestTime', new Date().toISOString());", "pm.variables.set('eventId', pm.variables.replaceIn('{{$guid}}'));" ]}},
        {'listen':'test','script':{'type':'text/javascript','exec':["const r=pm.response.json(); if(r.order?.quote_id) pm.collectionVariables.set('quoteId',r.order.quote_id);", "pm.test('No server failure',()=>pm.expect(pm.response.code).to.be.below(500));"]}}],
        'request':{'method':'POST','header':[{'key':'Content-Type','value':'application/json'}], 'body':{'mode':'raw','raw':json.dumps(data,indent=2)},'url':'{{baseUrl}}'}})
(ROOT/'Food-Delivery-v2.postman_collection.json').write_text(json.dumps(collection,indent=2)+'\n')
print('Generated workflow, integration test, and Postman collection.')
