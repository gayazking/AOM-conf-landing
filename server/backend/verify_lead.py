import app
cfg=app.load_env()
r=app.amo_request(cfg,"GET","/api/v4/leads/43644193?with=contacts",None)
print("GET lead status=%d" % r.status_code)
if r.status_code==200:
    d=r.json()
    print("  lead id=%s name=%s price=%s pipeline=%s status=%s" % (d.get("id"),d.get("name"),d.get("price"),d.get("pipeline_id"),d.get("status_id")))
    nr=app.amo_request(cfg,"GET","/api/v4/leads/43644193/notes",None)
    notes=nr.json().get("_embedded",{}).get("notes",[]) if nr.status_code==200 else []
    print("  notes=%d" % len(notes))
