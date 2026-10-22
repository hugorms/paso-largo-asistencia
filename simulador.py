"""Simulador de terminal facial Hikvision (ISAPI) para probar el sistema sin el equipo real.

Uso:  python simulador.py            -> escucha en http://127.0.0.1:8090  (usuario admin / clave 12345)
Genera marcaciones aleatorias de los usuarios registrados cada pocos segundos.
"""
import hashlib
import json
import random
import re
import secrets
import threading
import time
from datetime import datetime, timedelta

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

USER, PASSWORD, REALM = "admin", "12345", "DS-K1T341AMF"
_z = datetime.now().astimezone().strftime("%z")
TZ = f"{_z[:3]}:{_z[3:]}"
app = FastAPI()
state = {"users": {}, "faces": {}, "events": [], "serial": 0, "door": "close"}
lock = threading.Lock()
NONCES = set()


def _md5(s):
    return hashlib.md5(s.encode()).hexdigest()


def _challenge():
    nonce = secrets.token_hex(16)
    NONCES.add(nonce)
    return Response(status_code=401, headers={
        "WWW-Authenticate": f'Digest realm="{REALM}", qop="auth", nonce="{nonce}", algorithm=MD5'})


@app.middleware("http")
async def digest(request: Request, call_next):
    h = request.headers.get("authorization", "")
    if not h.startswith("Digest "):
        return _challenge()
    p = dict(re.findall(r'(\w+)="?([^",]+)"?', h[7:]))
    if p.get("username") != USER or p.get("nonce") not in NONCES:
        return _challenge()
    ha1 = _md5(f"{USER}:{REALM}:{PASSWORD}")
    ha2 = _md5(f"{request.method}:{p.get('uri')}")
    expected = _md5(f"{ha1}:{p['nonce']}:{p.get('nc')}:{p.get('cnonce')}:{p.get('qop')}:{ha2}")
    if expected != p.get("response"):
        return _challenge()
    return await call_next(request)


def now_iso(dt=None):
    return (dt or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S") + TZ


def add_event(minor, emp=None, when=None):
    with lock:
        state["serial"] += 1
        u = state["users"].get(emp) if emp else None
        state["events"].append({
            "major": 5, "minor": minor, "time": now_iso(when), "serialNo": state["serial"],
            "employeeNoString": emp or "", "name": u["name"] if u else "",
            "currentVerifyMode": "face" if emp else "invalid", "cardReaderNo": 1, "doorNo": 1,
            "pictureURL": f"http://127.0.0.1:8090/picture/{state['serial']}.jpg" if emp else None,
        })
        state["events"].sort(key=lambda e: e["time"])


@app.get("/ISAPI/System/deviceInfo")
def device_info():
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><DeviceInfo version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
           "<deviceName>Simulador</deviceName><deviceID>1</deviceID><model>DS-K1T341AMF (simulado)</model>"
           "<serialNumber>SIM0001</serialNumber><macAddress>00:11:22:33:44:55</macAddress>"
           "<firmwareVersion>V3.2.30</firmwareVersion><deviceType>ACS</deviceType></DeviceInfo>")
    return Response(xml, media_type="application/xml")


@app.get("/ISAPI/System/time")
def get_time():
    return Response(f"<Time><timeMode>manual</timeMode><localTime>{now_iso()}</localTime>"
                    "<timeZone>CST+5:00:00</timeZone></Time>", media_type="application/xml")


@app.put("/ISAPI/System/time")
@app.put("/ISAPI/System/reboot")
def ok_xml():
    return Response("<ResponseStatus><statusCode>1</statusCode><statusString>OK</statusString></ResponseStatus>",
                    media_type="application/xml")


@app.get("/ISAPI/AccessControl/UserInfo/Count")
def count():
    return {"UserInfoCount": {"userNumber": len(state["users"])}}


@app.post("/ISAPI/AccessControl/UserInfo/Search")
async def search(request: Request):
    c = (await request.json())["UserInfoSearchCond"]
    users = list(state["users"].values())
    if c.get("EmployeeNoList"):
        wanted = {x["employeeNo"] for x in c["EmployeeNoList"]}
        users = [u for u in users if u["employeeNo"] in wanted]
    pos, n = c["searchResultPosition"], c["maxResults"]
    page = users[pos:pos + n]
    if not users:
        return {"UserInfoSearch": {"searchID": c["searchID"], "responseStatusStrg": "NO MATCH", "numOfMatches": 0, "totalMatches": 0}}
    return {"UserInfoSearch": {"searchID": c["searchID"], "responseStatusStrg": "MORE" if pos + n < len(users) else "OK",
                               "numOfMatches": len(page), "totalMatches": len(users), "UserInfo": page}}


@app.post("/ISAPI/AccessControl/UserInfo/Record")
@app.put("/ISAPI/AccessControl/UserInfo/Modify")
async def record(request: Request):
    u = (await request.json())["UserInfo"]
    state["users"][u["employeeNo"]] = u
    return {"statusCode": 1, "statusString": "OK", "subStatusCode": "ok"}


@app.put("/ISAPI/AccessControl/UserInfo/Delete")
async def delete(request: Request):
    for x in (await request.json())["UserInfoDelCond"]["EmployeeNoList"]:
        state["users"].pop(x["employeeNo"], None)
    return {"statusCode": 1, "statusString": "OK"}


@app.put("/ISAPI/Intelligent/FDLib/FDSetUp")
@app.post("/ISAPI/Intelligent/FDLib/FaceDataRecord")
async def face(request: Request):
    f = await request.form()
    meta = json.loads(f["FaceDataRecord"])
    img = await f["img"].read()
    if len(img) > 200 * 1024:
        return JSONResponse({"statusCode": 6, "subStatusCode": "imageTooLarge"}, status_code=400)
    state["faces"][meta["FPID"]] = img
    return {"statusCode": 1, "statusString": "OK"}


@app.put("/ISAPI/Intelligent/FDLib/FDSearch/Delete")
async def face_delete(request: Request):
    for x in (await request.json())["FPID"]:
        state["faces"].pop(x["value"], None)
    return {"statusCode": 1, "statusString": "OK"}


@app.post("/ISAPI/AccessControl/AcsEvent")
async def acs_event(request: Request):
    c = (await request.json())["AcsEventCond"]
    evs = [e for e in state["events"] if c["startTime"][:19] <= e["time"][:19] <= c["endTime"][:19]]
    pos, n = c["searchResultPosition"], c["maxResults"]
    page = evs[pos:pos + n]
    status = "NO MATCH" if not evs else ("MORE" if pos + n < len(evs) else "OK")
    return {"AcsEvent": {"searchID": c["searchID"], "responseStatusStrg": status, "numOfMatches": len(page),
                         "totalMatches": len(evs), "InfoList": page}}


@app.put("/ISAPI/AccessControl/RemoteControl/door/{door}")
async def door(door: int, request: Request):
    body = (await request.body()).decode()
    state["door"] = re.search(r"<cmd>(\w+)</cmd>", body).group(1)
    add_event(1024)
    print(f"[simulador] Puerta {door}: {state['door']}")
    return ok_xml()


@app.get("/ISAPI/Event/notification/alertStream")
async def alert_stream():
    import asyncio

    async def gen():
        seen = state["serial"]
        beat = 0
        while True:
            await asyncio.sleep(0.5)
            beat += 1
            if state["serial"] > seen:
                seen = state["serial"]
                ev = state["events"][-1]
                body = json.dumps({"eventType": "AccessControllerEvent", "dateTime": ev["time"],
                                   "AccessControllerEvent": {"majorEventType": 5, "subEventType": ev["minor"],
                                                             "employeeNoString": ev["employeeNoString"]}})
            elif beat % 20 == 0:
                body = json.dumps({"eventType": "heartBeat", "eventState": "active"})
            else:
                continue
            yield (f"--MIME_boundary\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
                   f"{body}\r\n").encode()

    from fastapi.responses import StreamingResponse
    return StreamingResponse(gen(), media_type="multipart/mixed; boundary=MIME_boundary")


@app.get("/picture/{serial}.jpg")
def picture(serial: int):
    ev = next((e for e in state["events"] if e["serialNo"] == serial), None)
    img = state["faces"].get(ev["employeeNoString"]) if ev else None
    return Response(img, media_type="image/jpeg") if img else Response(status_code=404)


def generator():
    """Crea historial de 10 días y luego marcaciones aleatorias en vivo."""
    seeded = False
    while True:
        time.sleep(5)
        users = list(state["users"])
        if not users:
            continue
        if not seeded:
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            for d in range(10, 0, -1):
                day = today - timedelta(days=d)
                if day.isoweekday() > 5:
                    continue
                for emp in users:
                    if random.random() < 0.1:
                        continue  # falta
                    add_event(75, emp, day + timedelta(hours=7, minutes=random.randint(40, 85)))
                    add_event(75, emp, day + timedelta(hours=17, minutes=random.randint(0, 50)))
            seeded = True
            print("[simulador] historial de 10 días generado")
        if random.random() < 0.3:
            emp = random.choice(users)
            minor = 75 if random.random() < 0.9 else 76
            add_event(minor, emp if minor == 75 else None)
            print(f"[simulador] evento {minor} empleado {emp}")


if __name__ == "__main__":
    threading.Thread(target=generator, daemon=True).start()
    print(f"Simulador ISAPI en http://127.0.0.1:8090  (usuario {USER} / clave {PASSWORD})")
    uvicorn.run(app, host="127.0.0.1", port=8090, log_level="warning")
