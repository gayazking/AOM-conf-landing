import json, os, segno
URL = "https://sadaosato.pro/pretix"
DEVICES = [
 ("Регистратор 1","j4jjuqxphs97kjpp"),("Регистратор 2","mp2vwkl32twq8h4w"),
 ("Регистратор 3","h74em7yavwrrl3m6"),("Регистратор 4","hhp6up8vvjvjyi1t"),
 ("Регистратор 5","u4j095g2qxf0odm4"),("Регистратор 6","2jtw1ow9768y5lz7"),
 ("Регистратор 7","46cfy8f791av3e2r"),("Регистратор 8","0b56l9ybh83huuzn"),
 ("Регистратор 9","24o3rr66hui2fp5o"),("Регистратор 10","fjrffsqoz7f2fusu"),
]
outdir="/opt/sato/registrars/qr"
cards=[]
for i,(name,tok) in enumerate(DEVICES,1):
    payload=json.dumps({"handshake_version":1,"url":URL,"token":tok})
    fn="reg-%d.png"%i
    segno.make_qr(payload,error="m").save(os.path.join(outdir,fn),scale=7,border=3)
    cards.append((name,fn,tok))

legend=[("Полный пакет · 4 дня (1800€)","ЗОЛОТОЙ браслет","#c9a572"),
        ("Практика · оффлайн (1000€)","СИНИЙ браслет","#2b6cb0"),
        ("Теория · оффлайн (800€)","ЗЕЛЁНЫЙ браслет","#1f9d55"),
        ("Теория · онлайн (500€)","БЕЗ браслета · трансляция","#7a7f8a")]
html=['<!doctype html><html lang=ru><head><meta charset=utf-8><title>Регистраторы · pretixSCAN</title>',
'<style>body{font-family:-apple-system,Segoe UI,Roboto,Arial;color:#11151c;margin:24px;background:#fff}',
'h1{color:#9a7b35}h2{margin:18px 0 6px}.grid{display:flex;flex-wrap:wrap;gap:14px}',
'.card{border:1px solid #ccc;border-radius:12px;padding:12px;width:230px;text-align:center;page-break-inside:avoid}',
'.card img{width:190px;height:190px}.tok{font:12px monospace;color:#666;word-break:break-all}',
'.leg{display:flex;align-items:center;gap:10px;margin:6px 0;font-size:15px}',
'.sw{width:26px;height:26px;border-radius:6px;display:inline-block}',
'.steps{max-width:760px;line-height:1.5}.r{color:#a01b1b}.g{color:#0f7a32}.y{color:#9a7d1a}',
'@media print{.card{width:30%}}</style></head><body>',
'<h1>Стойка регистрации · «Казань — Токио 2026»</h1>',
'<div class=steps><h2>Как подключить планшет регистратора</h2><ol>',
'<li>Установите приложение <b>pretixSCAN</b> (Google Play) на планшет/телефон.</li>',
'<li>Откройте приложение → «Connect» / «Подключить устройство».</li>',
'<li>Наведите камеру на персональный QR ниже (по одному QR на устройство).</li>',
'<li>Готово — приложение само скачает список участников и работает офлайн.</li></ol>',
'<h2>Как проверять на входе</h2><ul>',
'<li><span class=g>● Зелёный «OK»</span> — впустить, выдать браслет по тарифу (см. легенду).</li>',
'<li><span class=y>● Жёлтый «Already redeemed»</span> — участник уже входил.</li>',
'<li><span class=r>● Красный</span> — билет не оплачен / недействителен. Направить к старшему менеджеру.</li>',
'<li>Нет QR у гостя? В приложении — поиск по фамилии (онлайн-режим).</li></ul>',
'<h2>Браслеты по тарифам</h2>']
for nm,wb,col in legend:
    html.append('<div class=leg><span class=sw style="background:%s"></span><b>%s</b> → %s</div>'%(col,nm,wb))
html.append('</div><h2>QR подключения устройств (по одному на регистратора)</h2><div class=grid>')
for nm,fn,tok in cards:
    html.append('<div class=card><div><b>%s</b></div><img src="qr/%s"><div class=tok>%s</div></div>'%(nm,fn,tok))
html.append('</div><p style="color:#a01b1b;margin-top:18px"><b>Важно:</b> QR одноразовые — действуют до первого подключения устройства. После привязки токен становится недействительным. Не публиковать.</p>')
html.append('</body></html>')
open("/opt/sato/registrars/registrars-sheet.html","w").write("\n".join(html))
print("generated", len(cards), "device QRs + registrars-sheet.html")
