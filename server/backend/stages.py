import app
cfg=app.load_env()
r=app.amo_request(cfg,"GET","/api/v4/leads/pipelines/10957730/statuses",None)
if r.status_code==200:
    for s in r.json()["_embedded"]["statuses"]:
        mark=" <== leads land here" if s["id"]==86154870 else ""
        print("  %d  %s%s" % (s["id"], s["name"], mark))
