import boto3
from datetime import datetime, timedelta, timezone
ddb = boto3.client("dynamodb", region_name="us-east-1")
NOW = datetime.now(timezone.utc)
def iso(d=0): return (NOW + timedelta(seconds=d)).isoformat()
def s(v): return {"S": str(v)}
def n(v): return {"N": str(v)}
def l(items): return {"L": items}

conflicts = [
    {"conflict_id":"ARBITER-UC01","severity":"HIGH","title":"Dropbox approved in policy but blocked by Zscaler","source_policy":"MIG-POL-001-CS01","source_technical":"ZIA-URLCAT-CLOUD-BLK-042","status":"OPEN","detected_at":iso(-7200),"finding":"Policy approves Dropbox; Zscaler blocks it.","impact":"~1800 employees blocked","domains":["SharePoint","Zscaler"]},
    {"conflict_id":"ARBITER-UC07","severity":"CRITICAL","title":"Prod ALB without WAF","source_policy":"MIG-POL-004-WAF01","source_technical":"alb-mig-prod-claims-api-001","status":"OPEN","detected_at":iso(-3600),"finding":"Production ALB internet-accessible without WAF.","impact":"PCI DSS Req 6.4 violation","domains":["AWS Config"]},
    {"conflict_id":"ARBITER-UC08","severity":"CRITICAL","title":"Dev-Prod VPC peering active 78 days","source_policy":"MIG-POL-004-SEG01","source_technical":"pcx-mig-prod-dev-001","status":"IN_PROGRESS","detected_at":iso(-7200),"finding":"Dev-prod peering active 78 days.","impact":"Segmentation failure","domains":["AWS Config"]},
]
for c in conflicts:
    ddb.put_item(TableName="dev-st21arbiter-poc-conflicts", Item={
        **{k: s(v) for k, v in c.items() if isinstance(v, str)},
        "domains": l([s(d) for d in c["domains"]]),
    })
print(f"✓ {len(conflicts)} conflicts")

crs = [
    {"cr_id":"CR-001","status":"PENDING_APPROVAL","conflict_id":"ARBITER-UC07","action_type":"SECURITY_FIX","target_resource":"alb-mig-prod-claims-api-001","target_environment":"PROD","severity":"CRITICAL","description":"Add WAF to claims API ALB","requested_by":"sec.analyst@example.com","justification":"PCI DSS Req 6.4","created_at":iso(-3600)},
]
for c in crs:
    ddb.put_item(TableName="dev-st21arbiter-poc-change-requests", Item={k:s(v) for k,v in c.items()})
print(f"✓ {len(crs)} change requests")

audit = [
    {"event_id":"1","timestamp":iso(0),"action_type":"SCAN_TRIGGERED","resource":"full-scan","user":"system","status":"COMPLETED","details":'{"conflicts_found":3}'},
    {"event_id":"2","timestamp":iso(-3600),"action_type":"CR_CREATED","resource":"alb-mig-prod-claims-api-001","user":"sec.analyst@example.com","status":"PENDING_APPROVAL","details":'{"cr_id":"CR-001"}'},
]
for e in audit:
    ddb.put_item(TableName="dev-st21arbiter-poc-audit-log", Item={k:s(v) for k,v in e.items()})
print(f"✓ {len(audit)} audit rows")