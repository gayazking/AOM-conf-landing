import app, json, base64, time
TOKEN=open('/tmp/amo_token').read().strip()
pl=TOKEN.split('.')[1]; pl+='='*(-len(pl)%4)
d=json.loads(base64.urlsafe_b64decode(pl))
exp=int(d.get('exp', int(time.time())+36000))
app.write_tokens({"access_token":TOKEN,"refresh_token":"","expires_at_epoch":exp})
print("TOKEN_SET exp=%d (in %.1fh) scopes=%s" % (exp,(exp-time.time())/3600,d.get('scopes')))
cfg=app.load_env()
r=app.amo_request(cfg,"GET","/api/v4/leads/pipelines",None)
print("pipelines_status=%d" % r.status_code)
if r.status_code==200:
    pls=r.json()["_embedded"]["pipelines"]
    main=next((p for p in pls if p.get("is_main")),pls[0])
    sts=main["_embedded"]["statuses"]
    inc=next((s for s in sts if s.get("type")==1),None)
    chosen=inc or min((s for s in sts if s["id"] not in (142,143)),key=lambda s:s.get("sort",0))
    print("ALLPIPE="+json.dumps([{"id":p["id"],"name":p["name"],"main":p.get("is_main")} for p in pls],ensure_ascii=False))
    print("RESULT PIPELINE_ID=%d STATUS_ID=%d" % (main["id"],chosen["id"]))
    print("CHOSEN pipeline=%s stage=%s" % (main["name"],chosen["name"]))
else:
    print("ERR "+r.text[:300])
