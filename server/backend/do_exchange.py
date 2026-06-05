import app
cfg=app.load_env()
code=open('/tmp/amo_code').read().strip()
t=app.exchange_code(cfg,code)
print("OK access_len=%d refresh_len=%d" % (len(t['access_token']), len(t.get('refresh_token',''))))
