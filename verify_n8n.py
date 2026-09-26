"""Run the real n8n nodes in an isolated database and report only test evidence."""
import json
import os
from pathlib import Path
import subprocess

root=Path(__file__).resolve().parent
env={**os.environ,'N8N_USER_FOLDER':'/private/tmp/food-delivery-n8n-v2-test','N8N_PORT':'5689',
     'N8N_RUNNERS_BROKER_PORT':'5690','N8N_DIAGNOSTICS_ENABLED':'false','N8N_VERSION_NOTIFICATIONS_ENABLED':'false'}
commands=[['/opt/homebrew/bin/n8n','import:workflow','--input='+str(root/'integration-test.n8n.json')],
          ['/opt/homebrew/bin/n8n','execute','--id=foodDeliveryV2Test','--rawOutput']]
for command in commands:
    result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=120)
    if result.returncode:
        raise SystemExit('n8n integration command failed: '+result.stderr[-1000:])
raw=result.stdout
start=raw.find('\n{\n')
if start<0:
    raise SystemExit('n8n did not return execution JSON')
execution=json.loads(raw[start+1:])
assert execution['status']=='success',execution['status']
runs=execution['data']['resultData']['runData']
last=runs['Normalize HTTP Response'][0]['data']['main'][0][0]['json']
assert last['code']==200,last
assert last['body']['state']=='AWAITING_CUSTOMER',last
assert len(last['body']['decision_trace'])==10
report={'result':'passed','workflow_status':execution['status'],'http_status':last['code'],
        'state':last['body']['state'],'steps':len(last['body']['decision_trace']),
        'executed_nodes':list(runs),'run_id':last['body']['run_id']}
(root/'n8n-verification.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
