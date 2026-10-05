import os
import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

# הגדרת רישום יומנים (Logs) מפורט
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("CallCenter")

app = FastAPI()

# ----------------- משתני סביבה והגדרות מערכת -----------------
# ניתן לקבוע ב-Render דרך Environment Variables או לערוך כאן
YEMOT_API_TOKEN = os.getenv("YEMOT_API_TOKEN", "0770000000:1234")  # טוקן קבוע / username:password
YEMOT_BASE_URL = "https://www.call2all.co.il/ym/api"

# רשימת נציגים דינמית (נטענת ישירות מקובץ a.ini שבתוך השלוחה)
AGENTS_CONFIG: List[dict] = []

# רשימת חדרי הועידה הקיימים במערכת
# כל חדר כולל שלוחת מאזין ושלוחת מנהל שמפעילה את החדר
CONFERENCE_ROOMS = [
    {"room_id": 1, "listener_ext": "/10/1", "admin_ext": "/10/2", "busy": False, "call_id": None},
    {"room_id": 2, "listener_ext": "/11/1", "admin_ext": "/11/2", "busy": False, "call_id": None},
    {"room_id": 3, "listener_ext": "/12/1", "admin_ext": "/12/2", "busy": False, "call_id": None},
]

# ----------------- מבנה הנתונים בזיכרון -----------------
@dataclass
class WaitingCaller:
    call_id: str
    phone: str
    event: asyncio.Event
    assigned_room: Optional[dict] = None

waiting_queue: List[WaitingCaller] = []
active_calls: Dict[str, dict] = {}       # מעקב שיחות פעילות (ApiCallId -> פרטי השיחה)
busy_agents: set = set()                 # מספרי טלפון של נציגים שנמצאים בשיחה פעילה
tzintuk_task: Optional[asyncio.Task] = None
queue_lock = asyncio.Lock()


# ----------------- פונקציות טעינה וצינתוק -----------------

async def load_agents_from_ini(extension_path: str = "1"):
    """
    טעינת רשימת הנציגים מקובץ a.ini הנמצא בתוך שלוחת התור בימות המשיח.
    פורמט נתמך בכל שורה:
    טלפון=רשימת_צינתוק  (לדוגמה: 0501111111=101)
    או רק מספר רשימה/טלפון (לדוגמה: 101)
    """
    global AGENTS_CONFIG
    clean_path = extension_path.strip("/")
    url = f"{YEMOT_BASE_URL}/GetTextFile"
    params = {
        "token": YEMOT_API_TOKEN,
        "path": f"ivr2:{clean_path}/a.ini"
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            data = resp.json()

            if data.get("responseStatus") == "OK":
                content = data.get("message", "")
                loaded = []
                for line in content.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        phone, tz_list = line.split("=", 1)
                        loaded.append({
                            "phone": phone.strip(),
                            "tzintuk_list": tz_list.strip()
                        })
                    else:
                        loaded.append({
                            "phone": line,
                            "tzintuk_list": line
                        })
                AGENTS_CONFIG = loaded
                logger.info(f"[טעינת קובץ a.ini] נטענו {len(AGENTS_CONFIG)} נציגים משלוחה {clean_path}.")
            else:
                logger.warning(f"[שגיאת קובץ a.ini] לא ניתן לטעון: {data.get('message')}")
    except Exception as e:
        logger.error(f"[שגיאה בפנייה ל-GetTextFile] {e}")


async def send_tzintuk(list_id: str):
    """שליחת צינתוק לרשימת צינתוקים דרך ה-API של ימות המשיח"""
    url = f"{YEMOT_BASE_URL}/RunTzintuk"
    params = {
        "token": YEMOT_API_TOKEN,
        "phones": list_id
    }
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(url, params=params)
            logger.info(f"[צינתוק נשלח] רשימה: {list_id} | תגובה: {resp.text.strip()}")
    except Exception as e:
        logger.error(f"[שגיאת צינתוק] כשל בשליחת צינתוק לרשימה {list_id}: {e}")


async def tzintuk_loop_worker(extension_path: str):
    """
    תהליך רקע:
    כל עוד יש מאזינים ממתינים בתור:
    מצנתק לנציג 1 -> ממתין 10 שניות -> מצנתק לנציג הבא.
    בסיום הרשימה ממתין 30 שניות וחוזר לתחילתה.
    מדלג על נציגים שנמצאים בשיחה פעילה.
    """
    logger.info("[צינתוקים] תהליך הצינתוקים החל לפעול.")
    try:
        while True:
            # ריענון רשימת הנציגים מקובץ a.ini לפני כל סבב
            await load_agents_from_ini(extension_path)

            async with queue_lock:
                if not waiting_queue:
                    logger.info("[צינתוקים] התור התרוקן. עוצר תהליך צינתוקים.")
                    break

            for agent in AGENTS_CONFIG:
                async with queue_lock:
                    if not waiting_queue:
                        break

                # בדיקה האם הנציג כרגע בשיחה פעילה
                if agent["phone"] in busy_agents:
                    logger.info(f"[צינתוקים] מדלג על נציג {agent['phone']} - נמצא בשיחה כרגע.")
                    continue

                logger.info(f"[צינתוקים] מצנתק לנציג {agent['phone']} (רשימה {agent['tzintuk_list']})")
                await send_tzintuk(agent["tzintuk_list"])

                # המתנה של 10 שניות לנציג הבא
                await asyncio.sleep(10)

            async with queue_lock:
                if not waiting_queue:
                    break

            logger.info("[צינתוקים] הסתיים סבב נציגים. ממתין 30 שניות לסבב הבא...")
            await asyncio.sleep(30)

    except asyncio.CancelledError:
        logger.info("[צינתוקים] תהליך הצינתוקים נעצר.")
    finally:
        global tzintuk_task
        tzintuk_task = None


def get_caller_position(call_id: str) -> int:
    for idx, caller in enumerate(waiting_queue):
        if caller.call_id == call_id:
            return idx + 1
    return 0


def find_free_room() -> Optional[dict]:
    for room in CONFERENCE_ROOMS:
        if not room["busy"]:
            return room
    return None


# ----------------- ניתובים (Endpoints) -----------------

@app.api_route("/queue", methods=["GET", "POST"])
async def queue_handler(request: Request):
    """
    כניסת מאזין לתור + לולאת המתנה (Long-Polling של 60 שניות).
    משמיע למאזין את מיקומו בתור ומשהה תגובה עד מענה נציג או תום דקה.
    """
    params = dict(request.query_params)
    if request.method == "POST":
        form_data = await request.form()
        params.update(dict(form_data))

    call_id = params.get("ApiCallId", "unknown_id")
    phone = params.get("ApiPhone", "חסוי")
    extension = params.get("ApiExtension", "/1")

    global tzintuk_task
    current_caller: Optional[WaitingCaller] = None

    async with queue_lock:
        # בדיקה האם המאזין כבר קיים ברשימת התור
        for c in waiting_queue:
            if c.call_id == call_id:
                current_caller = c
                break

        if not current_caller:
            # מאזין חדש שהצטרף
            current_caller = WaitingCaller(call_id=call_id, phone=phone, event=asyncio.Event())
            waiting_queue.append(current_caller)
            pos = len(waiting_queue)
            logger.info(f"[מאזין חדש בתור] טלפון: {phone} | מזהה שיחה: {call_id} | מיקום: {pos}")

            # התחלת תהליך צינתוקים אם אינו פועל
            if tzintuk_task is None or tzintuk_task.done():
                tzintuk_task = asyncio.create_task(tzintuk_loop_worker(extension))
        else:
            pos = get_caller_position(call_id)
            logger.info(f"[מאזין ממתין] טלפון: {phone} | מזהה: {call_id} | מיקום נוכחי: {pos}")

    # אם כבר שובץ לו חדר ע"י נציג
    if current_caller.assigned_room:
        room = current_caller.assigned_room
        logger.info(f"[ניתוב מאזין] מאזין {phone} מועבר לחדר {room['room_id']} בשלוחה {room['listener_ext']}")
        return PlainTextResponse(f"go_to_folder={room['listener_ext']}")

    # השהיית התשובה עד 60 שניות (בזמן זה המערכת בימות המשיח משמיעה מוזיקה בהמתנה)
    try:
        await asyncio.wait_for(current_caller.event.wait(), timeout=60.0)

        # נציג ענה לפני פקיעת הדקה!
        room = current_caller.assigned_room
        logger.info(f"[מענה נציג!] מאזין {phone} יוצא מהמתנה ומועבר לחדר {room['room_id']} ({room['listener_ext']})")
        return PlainTextResponse(f"go_to_folder={room['listener_ext']}")

    except asyncio.TimeoutError:
        # חלפה דקה ללא מענה -> משמיע מיקום מעודכן ושולח שוב לאותה שלוחה לסבב הבא
        pos = get_caller_position(call_id)
        logger.info(f"[סיום דקה] מאזין {phone} עדיין ממתין. מיקום: {pos}. משמיע הודעה וממשיך המתנה.")
        response_text = f"id_list_message=t-מיקומך בתור הוא .n-{pos}&go_to_folder={extension}"
        return PlainTextResponse(response_text)


@app.api_route("/agent_pickup", methods=["GET", "POST"])
async def agent_pickup_handler(request: Request):
    """
    כניסת נציג לשלוחה 20:
    שולף את המאזין הבא בתור, מאתר חדר פנוי, מעביר את המאזין ואז מעביר את הנציג.
    """
    params = dict(request.query_params)
    if request.method == "POST":
        form_data = await request.form()
        params.update(dict(form_data))

    agent_phone = params.get("ApiPhone", "unknown")
    agent_call_id = params.get("ApiCallId", "unknown")

    logger.info(f"[נציג חייג 20] נציג {agent_phone} מבקש לענות לשיחה.")

    async with queue_lock:
        if not waiting_queue:
            logger.info(f"[אין שיחות בתור] נציג {agent_phone} נכנס, אך התור ריק.")
            return PlainTextResponse("id_list_message=t-אין שיחות ממתינות כרגע בתור&go_to_folder=/")

        free_room = find_free_room()
        if not free_room:
            logger.warning(f"[אין חדר פנוי] נציג {agent_phone} ניסה לענות אך כל החדרים תפוסים.")
            return PlainTextResponse("id_list_message=t-כל חדרי הועידה תפוסים כרגע&go_to_folder=/")

        # הוצאת המאזין הראשון לפי סדר כניסה (FIFO)
        caller = waiting_queue.pop(0)

        # תפיסת החדר
        free_room["busy"] = True
        free_room["call_id"] = caller.call_id
        caller.assigned_room = free_room

        # סימון הנציג כפעיל (דילוג עליו בצינתוקים)
        busy_agents.add(agent_phone)
        active_calls[agent_call_id] = {"type": "agent", "phone": agent_phone, "room": free_room}
        active_calls[caller.call_id] = {"type": "caller", "phone": caller.phone, "room": free_room}

        # שחרור המאזין מההמתנה כדי שיועבר מיד לשלוחת המאזין בחדר
        caller.event.set()

        logger.info(f"[שיחה חוברה בהצלחה] חדר {free_room['room_id']}: "
                    f"מאזין {caller.phone} -> {free_room['listener_ext']} | "
                    f"נציג {agent_phone} -> {free_room['admin_ext']}")

        # העברת הנציג לשלוחה שמפעילה את החדר
        return PlainTextResponse(f"go_to_folder={free_room['admin_ext']}")


@app.api_route("/hangup", methods=["GET", "POST"])
async def hangup_handler(request: Request):
    """
    קריאת Hangup עם ניתוק שיחה מכל שלוחה:
    הסרת מאזין מהתור / שחרור חדר ועידה / שחרור נציג לקבלת צינתוקים.
    """
    params = dict(request.query_params)
    if request.method == "POST":
        form_data = await request.form()
        params.update(dict(form_data))

    call_id = params.get("ApiCallId", "")
    phone = params.get("ApiPhone", "")

    async with queue_lock:
        # בדיקה האם מאזין עזב את התור בהמתנה
        initial_len = len(waiting_queue)
        waiting_queue[:] = [c for c in waiting_queue if c.call_id != call_id]
        if len(waiting_queue) < initial_len:
            logger.info(f"[מאזין ניתק מהתור] מאזין {phone} (מזהה {call_id}) עזב את התור. עודכנו המיקומים.")

        # בדיקה האם זו שיחה שהייתה בחדר ועידה
        if call_id in active_calls:
            call_info = active_calls.pop(call_id)
            room = call_info.get("room")
            if room:
                room["busy"] = False
                room["call_id"] = None
                logger.info(f"[שחרור חדר ועידה] חדר {room['room_id']} פנוי כעת בעקבות ניתוק {call_id}.")

            if call_info.get("type") == "agent":
                agent_phone = call_info.get("phone")
                busy_agents.discard(agent_phone)
                logger.info(f"[נציג התפנה] נציג {agent_phone} סיים את השיחה ויוכל לקבל שוב צינתוקים.")

    return PlainTextResponse("OK")
