from fastapi import FastAPI, Response, Request
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Dict, List, Optional, Set
import asyncio
import datetime
import logging
from logging.handlers import RotatingFileHandler
import os
import httpx
import time
import csv
import io
import re

# ==================== הגדרות מערכת כלליות ====================
# טוקן ה-API המלא שלך מימות המשיח:
YEMOT_API_TOKEN = "0775311511:124578"

# תיקיית הבסיס (כרגע "00", כשתעביר לראשית שנה ל-"")
BASE_FOLDER = "00"

# קישור השיתוף לגיליון גוגל שיטס:
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1lSf6FK2wjCMwu-oZzrBsWlvPw5wXQ2RK6RJnV2h1GjM/edit?usp=sharing"

WAIT_TIMEOUT = 50                # שניות המתנה רציפה בבקשת HTTP
GRACE_PERIOD = WAIT_TIMEOUT + 30 # זמן חסד לסנכרון שיחות מנותקות
# ============================================================


# ==================== הגדרת מערכת לוגים ====================
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "system.log")

log_formatter = logging.Formatter(
    fmt="[%(asctime)s] [%(levelname)-7s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
file_handler.setFormatter(log_formatter)

root_logger = logging.getLogger("IVR")
root_logger.setLevel(logging.INFO)
root_logger.handlers.clear()
root_logger.addHandler(console_handler)
root_logger.addHandler(file_handler)

log_sheets = logging.getLogger("IVR.SHEETS")
log_call = logging.getLogger("IVR.CALL")
log_queue = logging.getLogger("IVR.QUEUE")
log_agent = logging.getLogger("IVR.AGENT")
log_tzintuk = logging.getLogger("IVR.TZINTUK")
log_sync = logging.getLogger("IVR.SYNC")
# ============================================================


def build_path(sub_path: str) -> str:
    sub = sub_path.strip("/")
    return f"{BASE_FOLDER}/{sub}" if BASE_FOLDER else sub


def normalize_phone(phone: str) -> str:
    clean = re.sub(r'\D', '', str(phone or ""))
    if clean.startswith("972"):
        clean = "0" + clean[3:]
    return clean


# ==================== מבני נתונים ====================
@dataclass
class QueuedCall:
    call_id: str
    phone: str
    entry_time: float
    dept_id: str
    queue_type: str                         # "general" או "personal"
    target_manager_phone: Optional[str]     # רלוונטי לשיחה אישית
    sub_ext: str


@dataclass
class ManagerLineInfo:
    name: str
    phone: str
    dept_id: str


class SheetDataManager:
    """ניהול משיכת נתונים מ-Google Sheets לפי דרישה בלבד"""
    def __init__(self, sheet_url: str):
        self.sheet_url = sheet_url
        self.csv_export_url = self._build_export_url(sheet_url)
        self.personal_lines: Dict[str, ManagerLineInfo] = {}
        self.dept_general_agents: Dict[str, Set[str]] = {}
        self.general_queue_extensions: Dict[str, str] = {}
        self.agent_to_depts: Dict[str, Set[str]] = {}

    def _build_export_url(self, url: str) -> str:
        match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
        if match:
            sheet_id = match.group(1)
            return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
        return url

    async def reload_from_sheets(self):
        if not self.sheet_url or "YOUR_SPREADSHEET_ID" in self.csv_export_url:
            log_sheets.warning("קישור Google Sheets אינו מוגדר.")
            return
            
        try:
            log_sheets.info("מרענן נתונים מ-Google Sheets...")
            async with httpx.AsyncClient() as client:
                res = await client.get(self.csv_export_url, timeout=10.0, follow_redirects=True)
                if res.status_code != 200:
                    log_sheets.error(f"שגיאה בהורדת הגיליון: קוד סטטוס HTTP {res.status_code}")
                    return

                content = res.content.decode('utf-8-sig')
                reader = csv.DictReader(io.StringIO(content))

                new_personal_lines = {}
                new_dept_general_agents = {}
                new_general_queues = {}
                new_agent_to_depts = {}

                for row in reader:
                    clean_row = {k.strip(): v.strip() for k, v in row.items() if k}
                    sub_ext = clean_row.get("שלוחה", "").strip("/")
                    name = clean_row.get("שם", "")
                    phone = normalize_phone(clean_row.get("טלפון", ""))
                    role = clean_row.get("תפקיד", "")
                    dept = clean_row.get("מחלקה", "1")

                    if not sub_ext or not phone:
                        continue

                    if dept not in new_dept_general_agents:
                        new_dept_general_agents[dept] = set()

                    if role == "מנהל":
                        new_personal_lines[sub_ext] = ManagerLineInfo(name=name, phone=phone, dept_id=dept)
                    else:
                        new_dept_general_agents[dept].add(phone)
                        new_general_queues[sub_ext] = dept
                        if phone not in new_agent_to_depts:
                            new_agent_to_depts[phone] = set()
                        new_agent_to_depts[phone].add(dept)

                self.personal_lines = new_personal_lines
                self.dept_general_agents = new_dept_general_agents
                self.general_queue_extensions = new_general_queues
                self.agent_to_depts = new_agent_to_depts

                total_agents = sum(len(v) for v in self.dept_general_agents.values())
                log_sheets.info(f"הסנכרון הושלם: {len(self.personal_lines)} מנהלים, {total_agents} נציגים ב-{len(self.dept_general_agents)} מחלקות.")
        except Exception as e:
            log_sheets.exception(f"תקלה בקריאת קובץ השיטס: {e}")


sheet_data = SheetDataManager(GOOGLE_SHEET_URL)


class SystemManager:
    def __init__(self):
        self.waiting_calls: Dict[str, QueuedCall] = {}
        self.waiting_events: Dict[str, asyncio.Event] = {}
        self.waiting_destinations: Dict[str, str] = {}
        self.last_seen: Dict[str, float] = {}
        self.tzintuk_tasks: Dict[str, asyncio.Task] = {}
        self.lock = asyncio.Lock()

    def has_waiting_for_target(self, target_key: str) -> bool:
        """בודק אם יש מאזינים שממתינים ספציפית ליעד צינתוק זה"""
        for call in self.waiting_calls.values():
            if target_key.startswith("personal_") and call.target_manager_phone == target_key.replace("personal_", ""):
                return True
            elif target_key.startswith("general_") and f"general_{call.dept_id}" == target_key:
                return True
        return False

    def cancel_tzintuk_if_empty(self, target_key: str):
        """עוצר ומבטל מיידית את משימת הצינתוק אם אין עוד ממתינים ליעד זה"""
        if not self.has_waiting_for_target(target_key):
            task = self.tzintuk_tasks.pop(target_key, None)
            if task and not task.done():
                task.cancel()
                log_tzintuk.info(f"התור התרוקן לחלוטין - משימת הצינתוקים של {target_key} בוטלה מיד.")


manager = SystemManager()


# ==================== תקשורת API מול ימות המשיח ====================
async def yemot_api_request(endpoint: str, params: dict = None):
    if not YEMOT_API_TOKEN or YEMOT_API_TOKEN == "your_actual_token_here":
        log_call.error("שגיאה: טוקן ה-API לא מוגדר בקוד! (YEMOT_API_TOKEN)")
        return None

    url = f"https://private.call2all.co.il/ym/api/{endpoint}"
    p = {"token": YEMOT_API_TOKEN}
    if params:
        p.update(params)

    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, params=p, timeout=10.0)
            return res.json()
    except Exception as e:
        log_call.error(f"שגיאת תקשורת מול API ימות המשיח ({endpoint}): {e}")
        return None


async def get_active_calls():
    data = await yemot_api_request("GetIncomingCalls")
    if data and data.get("responseStatus") == "OK":
        return data.get("calls", [])
    return None


async def send_tzintuk(phone: str):
    clean_phone = normalize_phone(phone)
    if len(clean_phone) not in [9, 10]:
        log_tzintuk.error(f"מספר טלפון שגוי ({clean_phone}). הצינתוק בוטל.")
        return

    # שליחה תמיד לרשימת צינתוקים שמספרה הוא מספר הטלפון של הנציג
    target_list = f"tzl:{clean_phone}"
    log_tzintuk.info(f"שולח פקודת RunTzintuk לימות המשיח (יעד: {target_list})...")

    result = await yemot_api_request("RunTzintuk", params={
        "phones": target_list,
        "TzintukTimeOut": 10,
        "sayInfoOnAnswer": "true"
    })

    if result:
        status = result.get("responseStatus")
        if status == "OK":
            log_tzintuk.info(f"הצינתוק לרשימה {target_list} נשלח בהצלחה! תגובת שרת: {result}")
        else:
            log_tzintuk.error(f"ימות המשיח דחתה את הצינתוק לרשימה {target_list}! תגובת שרת: {result}")
    else:
        log_tzintuk.error(f"לא התקבלה תשובה משרתי ימות המשיח עבור צינתוק ל-{target_list}.")


async def run_tzintuk_loop(target_key: str, get_phones_func):
    """לולאת צינתוקים שקוטעת את פעולתה מיידית ברגע שהמאזין מנתק"""
    log_tzintuk.info(f"לולאת צינתוקים הופעלה עבור יעד: {target_key}")
    try:
        while True:
            if not manager.has_waiting_for_target(target_key):
                break

            phones = get_phones_func()
            if isinstance(phones, str):
                phones = [phones]

            if not phones:
                log_tzintuk.warning(f"לא נמצאו מספרי טלפון להפצת צינתוק עבור {target_key}.")
                await asyncio.sleep(15)
                continue

            for phone in phones:
                # בדיקה מהירה לפני כל פנייה ל-API
                if not manager.has_waiting_for_target(target_key):
                    log_tzintuk.info(f"התור התרוקן. עוצר סבב צינתוקים עבור {target_key}.")
                    return

                await send_tzintuk(phone)

                # השהייה של 10 שניות הניתנת לביטול מיידי אם המאזין ינתק
                await asyncio.sleep(10)

            # השהייה של 20 שניות בין סבבים הניתנת לביטול מיידי
            await asyncio.sleep(20)

    except asyncio.CancelledError:
        log_tzintuk.info(f"משימת הצינתוקים של {target_key} נעצרה ובוטלה מיידית (עקב ניתוק/מענה).")
    except Exception as e:
        log_tzintuk.exception(f"תקלה בלולאת הצינתוקים ({target_key}): {e}")
    finally:
        manager.tzintuk_tasks.pop(target_key, None)


def find_free_room(active_calls: list, dept_id: str) -> int:
    active_paths = [str(c.get("path", "")) for c in (active_calls or [])]
    rooms_base = build_path(f"{dept_id}/rooms")
    
    for r in range(1, 15):
        listener_path = f"{rooms_base}/{r}/1"
        agent_path = f"{rooms_base}/{r}/2"
        if not any(listener_path in p or agent_path in p for p in active_paths):
            return r
    return 1


# ==================== משימות רקע (ניקוי שיחות בלבד) ====================
async def background_cleanup_loop():
    """בודק שיחות שנותקו ללא דיווח (סנכרון גוגל שיטס הוסר מכאן)"""
    while True:
        try:
            if manager.waiting_calls:
                active_calls = await get_active_calls()
                if active_calls is not None:
                    active_ids = {str(c.get("id")) for c in active_calls}
                    now = time.time()
                    
                    async with manager.lock:
                        to_remove = []
                        for cid, call in manager.waiting_calls.items():
                            if cid not in active_ids and (now - manager.last_seen.get(cid, 0)) >= GRACE_PERIOD:
                                to_remove.append(call)
                                
                        for call in to_remove:
                            cid = call.call_id
                            log_sync.info(f"שיחה {cid} נותקה ללא דיווח (עבר זמן החסד). מנקה מהתור.")
                            manager.waiting_calls.pop(cid, None)
                            manager.waiting_events.pop(cid, None)
                            manager.waiting_destinations.pop(cid, None)
                            manager.last_seen.pop(cid, None)
                            
                            t_key = f"personal_{call.target_manager_phone}" if call.queue_type == "personal" else f"general_{call.dept_id}"
                            manager.cancel_tzintuk_if_empty(t_key)
        except Exception as e:
            log_sync.exception(f"שגיאה בלולאת הניקוי: {e}")
        await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(app: FastAPI):
    root_logger.info("שרת התורים מופעל כעת.")
    # סנכרון ראשוני בלבד בעת הפעלת השרת
    await sheet_data.reload_from_sheets()
    task = asyncio.create_task(background_cleanup_loop())
    yield
    task.cancel()
    root_logger.info("שרת התורים כובה.")

app = FastAPI(lifespan=lifespan)


# ==================== נקודת הקצה הראשית (API) ====================
@app.api_route("/", methods=["GET", "POST"])
async def yemot_router(request: Request):
    params = dict(request.query_params)
    if request.method == "POST":
        try:
            form_data = await request.form()
            params.update(dict(form_data))
        except Exception:
            pass

    if not params:
        return Response('{"status": "ok", "message": "Queue Server is Running"}', media_type="application/json")

    call_id = (
        params.get("ApiCallId") 
        or params.get("YemotCallID") 
        or params.get("call_id") 
        or params.get("callId")
        or next((v for k, v in params.items() if k.lower() in ("apicallid", "yemotcallid", "call_id", "callid")), None)
    )

    hangup = params.get("hangup")
    api_extension = params.get("ApiExtension", "").strip("/")
    
    raw_phone = (
        params.get("ApiPhone") 
        or params.get("callerId") 
        or params.get("Phone") 
        or next((v for k, v in params.items() if k.lower() in ("apiphone", "callerid", "phone")), "")
    )
    caller_phone = normalize_phone(raw_phone)

    if not call_id and (caller_phone or api_extension):
        call_id = f"gen_{caller_phone or 'anon'}_{int(time.time() * 1000)}"
        log_call.warning(f"נוצר מזהה חלופי: {call_id}")

    if call_id:
        manager.last_seen[call_id] = time.time()

    # 1. טיפול מיידי בניתוק שיחה (עוצר ומבטל את הצינתוקים באותה שנייה!)
    if hangup == "yes" and call_id:
        async with manager.lock:
            removed_call = manager.waiting_calls.pop(call_id, None)
            if call_id in manager.waiting_events:
                manager.waiting_events[call_id].set()
            manager.waiting_destinations.pop(call_id, None)
            manager.waiting_events.pop(call_id, None)

            log_call.info(f"התקבל דיווח ניתוק (hangup=yes) עבור שיחה {call_id}.")

            # ביטול מיידי של משימת הצינתוקים הפעילה אם התור התרוקן
            if removed_call:
                t_key = f"personal_{removed_call.target_manager_phone}" if removed_call.queue_type == "personal" else f"general_{removed_call.dept_id}"
                manager.cancel_tzintuk_if_empty(t_key)

        return Response("noop=yes", media_type="text/plain; charset=utf-8")

    if not call_id:
        log_call.warning(f"בקשה חסרת מזהה שיחה. פרמטרים: {params}")
        return Response("id_list_message=t-מזהה שיחה חסר&go_to_folder=..", media_type="text/plain; charset=utf-8")

    # 2. Race Condition: אם המאזין כבר שוחרר לחדר טרם הכניסה להמתנה
    if call_id in manager.waiting_destinations:
        destination = manager.waiting_destinations.get(call_id)
        if call_id in manager.waiting_events:
            manager.waiting_events[call_id].set()
        log_call.info(f"[Race Condition] מאזין {call_id} כבר הוקצה לשלוחה {destination}. מעביר מיידית!")
        return Response(f"id_list_message=t-מעביר אותך לנציג&go_to_folder={destination}", media_type="text/plain; charset=utf-8")

    # ==============================================================
    # 3. שלוחת הנציגים/מנהלים הכללית (שלוחה 20)
    # ==============================================================
    agent_full_ext = build_path("20")
    if api_extension == agent_full_ext or api_extension == "20":
        log_agent.info(f"נציג/מנהל התחבר לשלוחה 20! מספר: {caller_phone}")
        async with manager.lock:
            selected_call: Optional[QueuedCall] = None

            # עדיפות 1: שיחה אישית למנהל (FIFO)
            personal_calls = [
                c for c in manager.waiting_calls.values()
                if c.queue_type == "personal" and c.target_manager_phone == caller_phone
            ]
            if personal_calls:
                personal_calls.sort(key=lambda x: x.entry_time)
                selected_call = personal_calls[0]
                log_agent.info(f"מנהל זוהה! מושך שיחה אישית ראשונה (מזהה: {selected_call.call_id}).")

            # עדיפות 2: שיחה כללית במחלקות שהנציג מורשה בהן (FIFO)
            if not selected_call:
                allowed_depts = sheet_data.agent_to_depts.get(caller_phone, set())
                general_calls = [
                    c for c in manager.waiting_calls.values()
                    if c.queue_type == "general" and c.dept_id in allowed_depts
                ]
                if general_calls:
                    general_calls.sort(key=lambda x: x.entry_time)
                    selected_call = general_calls[0]
                    log_agent.info(f"נציג זוהה! מושך שיחה כללית במחלקה {selected_call.dept_id} (מזהה: {selected_call.call_id}).")

            # שידוך לחדר פנוי
            if selected_call:
                active_calls = await get_active_calls()
                free_room = find_free_room(active_calls, selected_call.dept_id)

                room_folder = build_path(f"{selected_call.dept_id}/rooms/{free_room}")
                agent_dest = f"/{room_folder}/2"
                listener_dest = f"/{room_folder}/1"

                manager.waiting_calls.pop(selected_call.call_id, None)
                manager.waiting_destinations[selected_call.call_id] = listener_dest

                if selected_call.call_id in manager.waiting_events:
                    manager.waiting_events[selected_call.call_id].set()

                # עצירת הצינתוקים מיד עם המענה
                t_key = f"personal_{selected_call.target_manager_phone}" if selected_call.queue_type == "personal" else f"general_{selected_call.dept_id}"
                manager.cancel_tzintuk_if_empty(t_key)

                log_agent.info(f"שידוך הצליח! חדר: {free_room}. נציג: {agent_dest}, מאזין: {listener_dest}")
                msg = "לשיחה אישית" if selected_call.queue_type == "personal" else "לתור הממתינים"
                return Response(f"id_list_message=t-מעביר אותך {msg} בחדר {free_room}&go_to_folder={agent_dest}", media_type="text/plain; charset=utf-8")
            else:
                log_agent.info(f"נציג {caller_phone} נכנס, אך אין שיחות ממתינות עבורו.")
                return Response("id_list_message=t-אין שיחות ממתינות עבורך כעת&go_to_folder=..", media_type="text/plain; charset=utf-8")

    # ==============================================================
    # 4. כניסת מאזינים לתור
    # ==============================================================
    sub_ext = api_extension
    if BASE_FOLDER and sub_ext.startswith(f"{BASE_FOLDER}/"):
        sub_ext = sub_ext[len(BASE_FOLDER) + 1:]

    # סנכרון מגוגל שיטס רק אם נכנס מאזין כשהתור היה ריק לחלוטין
    if len(manager.waiting_calls) == 0:
        log_queue.info("נכנס מאזין חדש לתור ריק - מרענן הגדרות מגוגל שיטס...")
        await sheet_data.reload_from_sheets()

    queue_type = None
    target_manager_phone = None
    dept_id = None
    target_title = ""

    if sub_ext in sheet_data.personal_lines:
        p_info = sheet_data.personal_lines[sub_ext]
        queue_type = "personal"
        target_manager_phone = p_info.phone
        dept_id = p_info.dept_id
        target_title = f"לשיחה אישית עם {p_info.name}"
    elif sub_ext in sheet_data.general_queue_extensions:
        dept_id = sheet_data.general_queue_extensions[sub_ext]
        queue_type = "general"
        target_title = f"לתור הכללי במחלקה {dept_id}"

    if not queue_type:
        log_call.warning(f"שלוחה לא מזוהה בשיטס: {api_extension} (תת-שלוחה: {sub_ext})")
        return Response("id_list_message=t-שלוחה לא מוגדרת בשיטס&go_to_folder=..", media_type="text/plain; charset=utf-8")

    # כניסה ראשונית של המאזין לתור
    async with manager.lock:
        if call_id not in manager.waiting_calls:
            q_call = QueuedCall(
                call_id=call_id,
                phone=caller_phone,
                entry_time=time.time(),
                dept_id=dept_id,
                queue_type=queue_type,
                target_manager_phone=target_manager_phone,
                sub_ext=sub_ext
            )
            manager.waiting_calls[call_id] = q_call

            if queue_type == "personal":
                pos = sum(1 for c in manager.waiting_calls.values() if c.target_manager_phone == target_manager_phone)
                t_key = f"personal_{target_manager_phone}"
                phone_func = lambda: [target_manager_phone]
            else:
                pos = sum(1 for c in manager.waiting_calls.values() if c.dept_id == dept_id and c.queue_type == "general")
                t_key = f"general_{dept_id}"
                phone_func = lambda: list(sheet_data.dept_general_agents.get(dept_id, []))

            # הפעלת לולאת צינתוקים
            if t_key not in manager.tzintuk_tasks or manager.tzintuk_tasks[t_key].done():
                manager.tzintuk_tasks[t_key] = asyncio.create_task(run_tzintuk_loop(t_key, phone_func))

            log_queue.info(f"מאזין חדש ({caller_phone}, מזהה: {call_id}) נכנס {target_title}. מיקום בתור: {pos}")
            return Response(f"id_list_message=t-מיקומך בתור {target_title} הוא {pos}&go_to_folder=/{api_extension}", media_type="text/plain; charset=utf-8")

    # כניסה חוזרת – המתנה רציפה
    event = asyncio.Event()
    manager.waiting_events[call_id] = event

    try:
        await asyncio.wait_for(event.wait(), timeout=float(WAIT_TIMEOUT))
        default_room = f"/{build_path(f'{dept_id}/rooms/1/1')}"
        destination = manager.waiting_destinations.get(call_id, default_room)
        log_queue.info(f"מאזין {call_id} שוחרר מההמתנה! מעביר ליעד: {destination}")
        return Response(f"id_list_message=t-מעביר אותך לנציג&go_to_folder={destination}", media_type="text/plain; charset=utf-8")
    except asyncio.TimeoutError:
        async with manager.lock:
            if queue_type == "personal":
                pos = sum(1 for c in manager.waiting_calls.values() if c.target_manager_phone == target_manager_phone and c.entry_time <= manager.waiting_calls[call_id].entry_time)
            else:
                pos = sum(1 for c in manager.waiting_calls.values() if c.dept_id == dept_id and c.queue_type == "general" and c.entry_time <= manager.waiting_calls[call_id].entry_time)
        return Response(f"id_list_message=t-מיקומך בתור הוא {pos}&go_to_folder=/{api_extension}", media_type="text/plain; charset=utf-8")
    finally:
        manager.waiting_events.pop(call_id, None)
        manager.waiting_destinations.pop(call_id, None)
