import app, re
cfg=app.load_env()
code=open('/tmp/amo_code').read().strip()
t=app.exchange_code(cfg,code)
print("TOKENS_OK access_len=%d refresh_len=%d" % (len(t['access_token']),len(t.get('refresh_token',''))))
r=app.amo_request(cfg,"GET","/api/v4/leads/pipelines",None)
print("pipelines_status=%d" % r.status_code)
data=r.json()
pls=data["_embedded"]["pipelines"]
main=next((p for p in pls if p.get("is_main")),pls[0])
sts=main["_embedded"]["statuses"]
inc=next((s for s in sts if s.get("type")==1),None)
chosen=inc or min((s for s in sts if s["id"] not in (142,143)), key=lambda s:s.get("sort",0))
print("PIPELINE_NAME=%s STATUS_NAME=%s" % (main["name"], chosen["name"]))
print("RESULT PIPELINE_ID=%d STATUS_ID=%d" % (main["id"], chosen["id"]))
