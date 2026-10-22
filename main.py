from fastapi import FastAPI, Response, Request
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Dict, List, Optional, Set
import urllib.parse
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

# תיקיית הבסיס של התורים והנציגים (כרגע "00")
BASE_FOLDER = "00"

# שם התיקייה בשלוחה הראשית שבה נמצאים החדרים:
ROOMS_ROOT_FOLDER = "rooms"

# נתיב קובץ הלוג בתוך מערכת ימות המשיח:
YEMOT_LOG_FILE_PATH = "ivr2:00/30/Log.ini"

# קישור השיתוף לגיליון גוגל שיטס:
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1lSf6FK2wjCMwu-oZzrBsWlvPw5wXQ2RK6RJnV2h1GjM/edit?usp=sharing"

# שמות הטאבים (גליונות) בשיטס – גליון לכל מחלקה:
DEPARTMENT_TABS = ["1", "2", "3"]

WAIT_TIMEOUT = 50                # שניות המתנה רציפה בבקשת HTTP
GRACE_PERIOD = WAIT_TIMEOUT + 30 # זמן חסד לסנכרון שיחות מנותקות
# ============================================================


# ==================== הגדרת לוג נקי ומתומצת ====================
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "system.log")

log_formatter = logging.Formatter(
    fmt="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S"
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

logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
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
    """ניהול משיכת נתונים מ-Google Sheets לפי טאבים לכל מחלקה"""
    def __init__(self, sheet_url: str):
        self.sheet_url = sheet_url
        self.spreadsheet_id = self._extract_sheet_id(sheet_url)
        self.personal_lines: Dict[str, ManagerLineInfo] = {}
        self.dept_general_agents: Dict[str, Set[str]] = {}
        self.general_queue_extensions: Dict[str, str] = {}
        self.agent_to_depts: Dict[str, Set[str]] = {}
        self.phone_to_name: Dict[str, str] = {}

    def _extract_sheet_id(self, url: str) -> str:
        match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
        return match.group(1) if match else ""

    async def reload_from_sheets(self):
        if not self.spreadsheet_id:
            root_logger.warning("קישור Google Sheets אינו מוגדר.")
            return

        root_logger.info("מרענן נתוני נציגים ומחלקות מ-Google Sheets...")
        new_personal_lines = {}
        new_dept_general_agents = {}
        new_general_queues = {}
        new_agent_to_depts = {}
        new_phone_to_name = {}

        async with httpx.AsyncClient() as client:
            for tab_name in DEPARTMENT_TABS:
                dept_match = re.search(r'\d+', tab_name)
                dept_id = dept_match.group(0) if dept_match else tab_name
                
                tab_encoded = urllib.parse.quote(tab_name)
                url = f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}/gviz/tq?tqx=out:csv&sheet={tab_encoded}"
                
                try:
                    res = await client.get(url, timeout=10.0, follow_redirects=True)
                    if res.status_code != 200:
                        continue

                    content = res.content.decode('utf-8-sig')
                    reader = csv.reader(io.StringIO(content))
                    rows = list(reader)
                    if len(rows) < 2:
                        continue

                    headers = [h.strip() for h in rows[0]]

                    if dept_id not in new_dept_general_agents:
                        new_dept_general_agents[dept_id] = set()

                    for r in rows[1:]:
                        if not r or not any(r):
                            continue
                        row_dict = {headers[i]: r[i].strip() for i in range(min(len(headers), len(r)))}

                        sub_ext = row_dict.get("שלוחה", "").strip("/")
                        name = row_dict.get("שם", "").strip()
                        phone = normalize_phone(row_dict.get("טלפון", ""))
                        
                        # אם בעמודה האחרונה רשום "אישי"
                        last_col_val = r[-1].strip() if r else ""
                        is_personal = "אישי" in last_col_val or row_dict.get("תפקיד", "") == "אישי"

                        if not sub_ext or not phone:
                            continue

                        if name:
                            new_phone_to_name[phone] = name

                        if is_personal:
                            # שיחה אישית לנציג זה
                            new_personal_lines[sub_ext] = ManagerLineInfo(
                                name=name, 
                                phone=phone, 
                                dept_id=dept_id
                            )
                        else:
                            # נציג כללי במחלקה
                            new_dept_general_agents[dept_id].add(phone)
                            new_general_queues[sub_ext] = dept_id
                            if phone not in new_agent_to_depts:
                                new_agent_to_depts[phone] = set()
                            new_agent_to_depts[phone].add(dept_id)

                except Exception as e:
                    root_logger.warning(f"שגיאה בקריאת טאב מחלקה {tab_name}: {e}")

        self.personal_lines = new_personal_lines
        self.dept_general_agents = new_dept_general_agents
        self.general_queue_extensions = new_general_queues
        self.agent_to_depts = new_agent_to_depts
        self.phone_to_name = new_phone_to_name

        total_gen = sum(len(v) for v in self.dept_general_agents.values())
        root_logger.info(f"סנכרון Sheets הושלם: נטענו {len(self.personal_lines)} קווים אישיים ו-{total_gen} נציגים כלליים ב-{len(self.dept_general_agents)} מחלקות.")


sheet_data = SheetDataManager(GOOGLE_SHEET_URL)


class SystemManager:
    def __init__(self):
        self.waiting_calls: Dict[str, QueuedCall] = {}
        self.waiting_events: Dict[str, asyncio.Event] = {}
        self.waiting_destinations: Dict[str, str] = {}
        self.transferred_calls: Dict[str, str] = {}   # שמירת יעד שיחה ששודכה למניעת חזרה לתור
        self.occupied_rooms: Dict[str, Set[int]] = {} # מעקב חדרים תפוסים בזמן אמת בזיכרון השרת
        self.last_seen: Dict[str, float] = {}
        self.tzintuk_tasks: Dict[str, asyncio.Task] = {}
        self.call_history: Dict[str, dict] = {}
        self.last_call_by_phone: Dict[str, dict] = {}
        self.recording_counter = 0
        self.lock = asyncio.Lock()

    def has_waiting_for_target(self, target_key: str) -> bool:
        for call in self.waiting_calls.values():
            if target_key.startswith("personal_") and call.target_manager_phone == target_key.replace("personal_", ""):
                return True
            elif target_key.startswith("general_") and f"general_{call.dept_id}" == target_key:
                return True
        return False

    def cancel_tzintuk_if_empty(self, target_key: str):
        if not self.has_waiting_for_target(target_key):
            task = self.tzintuk_tasks.pop(target_key, None)
            if task and not task.done():
                task.cancel()
                root_logger.info(f"התור התרוקן - סבב הצינתוקים עבור {target_key} הופסק.")


manager = SystemManager()


# ==================== תקשורת API מול ימות המשיח ====================
async def yemot_api_request(endpoint: str, params: dict = None, method: str = "GET", data: dict = None, timeout: float = 3.0):
    if not YEMOT_API_TOKEN or YEMOT_API_TOKEN == "your_actual_token_here":
        root_logger.error("שגיאה: טוקן ה-API לא מוגדר בקוד!")
        return None

    url = f"https://private.call2all.co.il/ym/api/{endpoint}"
    p = {"token": YEMOT_API_TOKEN}
    if params:
        p.update(params)

    try:
        async with httpx.AsyncClient() as client:
            if method == "POST":
                post_data = {"token": YEMOT_API_TOKEN}
                if data:
                    post_data.update(data)
                res = await client.post(url, data=post_data, timeout=timeout)
            else:
                res = await client.get(url, params=p, timeout=timeout)
            return res.json()
    except Exception as e:
        return None


async def append_to_yemot_log(log_line: str):
    """רישום שורה ל-00/30/Log.ini בימות המשיח"""
    res = await yemot_api_request(
        "AppendTextFile",
        method="POST",
        data={
            "what": YEMOT_LOG_FILE_PATH,
            "contents": log_line + "\n"
        },
        timeout=3.0
    )
    if res and res.get("responseStatus") == "OK":
        root_logger.info(f"נרשם לוג במערכת: {log_line}")


async def get_active_calls():
    """שליפת שיחות פעילות עם Timeout מהיר של 1.5 שניות למניעת כל עיכוב"""
    data = await yemot_api_request("GetIncomingCalls", timeout=1.5)
    if data and data.get("responseStatus") == "OK":
        return data.get("calls", [])
    return None


async def is_agent_connected(phone: str) -> bool:
    clean_target = normalize_phone(phone)
    active = await get_active_calls()
    if active:
        for c in active:
            caller = normalize_phone(c.get("callerId") or c.get("callerIdNum") or "")
            if caller == clean_target:
                return True
    return False


async def send_tzintuk(phone: str):
    clean_phone = normalize_phone(phone)
    if len(clean_phone) not in [9, 10]:
        return

    agent_name = sheet_data.phone_to_name.get(clean_phone, "")
    name_str = f" ({agent_name})" if agent_name else ""

    result = await yemot_api_request("RunTzintuk", params={
        "phones": f"tzl:{clean_phone}",
        "TzintukTimeOut": 10,
        "sayInfoOnAnswer": "true"
    }, timeout=3.0)

    if result and result.get("responseStatus") == "OK":
        root_logger.info(f"נשלח צינתוק לנציג: {clean_phone}{name_str}")


async def run_tzintuk_loop(target_key: str, get_phones_func):
    """לולאת צינתוקים: צלצול לכל נציג, והמתנה של 20 שניות בין סבב לסבב"""
    root_logger.info(f"הופעלה הפצת צינתוקים עבור {target_key}")
    try:
        while True:
            if not manager.has_waiting_for_target(target_key):
                break

            phones = get_phones_func()
            if isinstance(phones, str):
                phones = [phones]

            if not phones:
                await asyncio.sleep(15)
                continue

            for phone in phones:
                if not manager.has_waiting_for_target(target_key):
                    return

                await send_tzintuk(phone)

                agent_entered = False
                for _ in range(10):
                    await asyncio.sleep(1)
                    if not manager.has_waiting_for_target(target_key):
                        return

                    if await is_agent_connected(phone):
                        agent_entered = True
                        agent_name = sheet_data.phone_to_name.get(phone, "")
                        root_logger.info(f"נציג {phone} ({agent_name or 'ללא שם'}) ענה לצינתוק ונכנס לקו.")
                        break

                if agent_entered:
                    for _ in range(10):
                        await asyncio.sleep(1)
                        if not manager.has_waiting_for_target(target_key):
                            root_logger.info("השיחה נענתה בהצלחה. סבב הצינתוקים הסתיים.")
                            return

            if manager.has_waiting_for_target(target_key):
                root_logger.info("הסתיים סבב צינתוקים ללא מענה. ממתין 20 שניות לפני סבב נוסף...")
                await asyncio.sleep(20)

    except asyncio.CancelledError:
        root_logger.info(f"סבב הצינתוקים עבור {target_key} הסתיים.")
    except Exception as e:
        root_logger.error(f"תקלה בסבב צינתוקים ({target_key}): {e}")
    finally:
        manager.tzintuk_tasks.pop(target_key, None)


async def find_free_room(dept_id: str) -> int:
    """מאתר חדר פנוי בין 1 ל-5 במחלקה בצורה מהירה ומיידית"""
    # 1. בדיקה מהירה מול API ימות המשיח
    active_calls = await get_active_calls()
    if active_calls is not None:
        active_paths = [str(c.get("path", "")) for c in active_calls]
        rooms_base = f"{ROOMS_ROOT_FOLDER}/{dept_id}"
        for r in range(1, 6):
            p1 = f"{rooms_base}/{r}/1"
            p2 = f"{rooms_base}/{r}/2"
            if not any(p1 in p or p2 in p for p in active_paths):
                return r

    # 2. גיבוי מזיכרון השרת (במקרה של עומס או אי-מענה מה-API)
    occupied = manager.occupied_rooms.get(dept_id, set())
    for r in range(1, 6):
        if r not in occupied:
            return r
    return 1


# ==================== משימות רקע (ניקוי שיחות) ====================
async def background_cleanup_loop():
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
                            root_logger.info(f"שיחה {cid} ({call.phone}) נותקה ללא דיווח והוסרה מהתור.")
                            manager.waiting_calls.pop(cid, None)
                            manager.waiting_events.pop(cid, None)
                            manager.waiting_destinations.pop(cid, None)
                            manager.last_seen.pop(cid, None)
                            
                            t_key = f"personal_{call.target_manager_phone}" if call.queue_type == "personal" else f"general_{call.dept_id}"
                            manager.cancel_tzintuk_if_empty(t_key)
        except Exception as e:
            root_logger.error(f"שגיאה בלולאת רקע: {e}")
        await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(app: FastAPI):
    root_logger.info("שרת התורים מופעל כעת.")
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

    if call_id:
        manager.last_seen[call_id] = time.time()

    # 1. ניתוק שיחה
    if hangup == "yes" and call_id:
        async with manager.lock:
            removed_call = manager.waiting_calls.pop(call_id, None)
            if call_id in manager.waiting_events:
                manager.waiting_events[call_id].set()
            manager.waiting_destinations.pop(call_id, None)
            manager.waiting_events.pop(call_id, None)

            if removed_call:
                root_logger.info(f"מאזין {removed_call.phone} ניתק את השיחה מהתור.")
                t_key = f"personal_{removed_call.target_manager_phone}" if removed_call.queue_type == "personal" else f"general_{removed_call.dept_id}"
                manager.cancel_tzintuk_if_empty(t_key)

        return Response("noop=yes", media_type="text/plain; charset=utf-8")

    if not call_id:
        return Response("id_list_message=t-מזהה שיחה חסר&go_to_folder=..", media_type="text/plain; charset=utf-8")

    # 2. מניעת חזרה לתור: אם המאזין כבר שודך לחדר פנוי – מעביר ישירות לחדר!
    if call_id in manager.waiting_destinations or call_id in manager.transferred_calls:
        destination = manager.waiting_destinations.get(call_id) or manager.transferred_calls.get(call_id)
        if call_id in manager.waiting_events:
            manager.waiting_events[call_id].set()
        root_logger.info(f"מאזין {caller_phone} שודך - מעביר מיידית ל-{destination}")
        return Response(f"id_list_message=t-מעביר אותך לנציג.g-{destination}&go_to_folder={destination}", media_type="text/plain; charset=utf-8")

    # ==============================================================
    # 3. שלוחה 30 / מקש 0: השמעת פרטי השיחה מתוך נתוני הלוג
    # ==============================================================
    sub_ext_raw = api_extension
    if BASE_FOLDER and sub_ext_raw.startswith(f"{BASE_FOLDER}/"):
        sub_ext_raw = sub_ext_raw[len(BASE_FOLDER) + 1:]

    is_ext_30 = (
        sub_ext_raw in ["30", "30/0"] or 
        api_extension in [build_path("30"), "30", build_path("30/0"), "30/0"] or
        api_extension.endswith("/30") or
        api_extension.endswith("/30/0")
    )
    pressed_key = params.get("digits") or params.get("data") or params.get("key") or ("0" if api_extension.endswith("/0") else "")

    if is_ext_30:
        if pressed_key == "0" or api_extension.endswith("/0"):
            rec = manager.call_history.get(call_id) or manager.last_call_by_phone.get(caller_phone)
            if rec:
                date_str = rec.get("date_str", "")
                time_str = rec.get("time_str", "")
                agent_name = rec.get("agent_name", "").strip()
                agent_phone = rec.get("agent_phone", "")
                customer_name = rec.get("caller_name", "").strip()
                customer_phone = rec.get("caller_phone", caller_phone)

                agent_display = f"t-{agent_name}" if agent_name else f"d-{agent_phone}"
                customer_display = f"t-{customer_name}" if customer_name else f"d-{customer_phone}"

                msg = f"t-פרטי השיחה. תאריך: {date_str}, בשעה: {time_str}. הנציג: {agent_display}. המאזין: {customer_display}."
                root_logger.info(f"שלוחה 30: הושמעו פרטי שיחה למאזין {caller_phone} (נציג: {agent_name or agent_phone})")
                return Response(f"id_list_message={msg}&go_to_folder=hangup", media_type="text/plain; charset=utf-8")
            else:
                return Response("id_list_message=t-לא נמצאו פרטי שיחה קודמת במערכת&go_to_folder=hangup", media_type="text/plain; charset=utf-8")
        else:
            return Response("read=t-לשמיעת פרטי השיחה הקישו 0=digits,1,1,1,7,No,No,0", media_type="text/plain; charset=utf-8")

    # ==============================================================
    # 4. שלוחת הנציגים/מנהלים הכללית (שלוחה 00/20)
    # ==============================================================
    agent_full_ext = build_path("20")
    if api_extension == agent_full_ext or api_extension == "20":
        agent_name = sheet_data.phone_to_name.get(caller_phone, "")
        root_logger.info(f"נציג התחבר לשלוחה 20: {caller_phone} ({agent_name or 'ללא שם'})")
        async with manager.lock:
            selected_call: Optional[QueuedCall] = None

            # עדיפות 1: שיחה אישית למנהל (רק המנהל הספציפי יכול למשוך!)
            personal_calls = [
                c for c in manager.waiting_calls.values()
                if c.queue_type == "personal" and c.target_manager_phone == caller_phone
            ]
            if personal_calls:
                personal_calls.sort(key=lambda x: x.entry_time)
                selected_call = personal_calls[0]
                root_logger.info(f"מושך שיחה אישית עבור מנהל {caller_phone} (מאזין: {selected_call.phone})")

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
                    root_logger.info(f"מושך שיחה כללית במחלקה {selected_call.dept_id} (מאזין: {selected_call.phone})")

            # שידוך לחדר פנוי כללי (1 עד 5) באותה מחלקה!
            if selected_call:
                free_room = await find_free_room(selected_call.dept_id)

                agent_dest = f"/{ROOMS_ROOT_FOLDER}/{selected_call.dept_id}/{free_room}/2"
                listener_dest = f"/{ROOMS_ROOT_FOLDER}/{selected_call.dept_id}/{free_room}/1"

                manager.waiting_calls.pop(selected_call.call_id, None)
                manager.waiting_destinations[selected_call.call_id] = listener_dest
                manager.transferred_calls[selected_call.call_id] = listener_dest

                # רישום החדר כתפוס בזיכרון השרת
                if selected_call.dept_id not in manager.occupied_rooms:
                    manager.occupied_rooms[selected_call.dept_id] = set()
                manager.occupied_rooms[selected_call.dept_id].add(free_room)

                if selected_call.call_id in manager.waiting_events:
                    manager.waiting_events[selected_call.call_id].set()

                # תיעוד השיחה ללוג ימות המשיח
                now_dt = datetime.datetime.now()
                manager.recording_counter += 1
                rec_filename = f"{manager.recording_counter:03d}.wav"
                
                customer_name = sheet_data.phone_to_name.get(selected_call.phone, "")
                date_str = now_dt.strftime("%d/%m/%Y")
                time_str = now_dt.strftime("%H:%M")

                call_record = {
                    "call_id": selected_call.call_id,
                    "caller_phone": selected_call.phone,
                    "caller_name": customer_name,
                    "agent_phone": caller_phone,
                    "agent_name": agent_name,
                    "dept_id": selected_call.dept_id,
                    "queue_type": selected_call.queue_type,
                    "room": free_room,
                    "date_str": date_str,
                    "time_str": time_str,
                    "timestamp": time.time()
                }
                manager.call_history[selected_call.call_id] = call_record
                manager.last_call_by_phone[selected_call.phone] = call_record

                # רישום השורה ל-Log.ini
                log_entry = f"{rec_filename}={date_str}|{time_str}|{caller_phone}|{agent_name}|{selected_call.phone}|{customer_name}"
                asyncio.create_task(append_to_yemot_log(log_entry))

                t_key = f"personal_{selected_call.target_manager_phone}" if selected_call.queue_type == "personal" else f"general_{selected_call.dept_id}"
                manager.cancel_tzintuk_if_empty(t_key)

                call_type_desc = "לשיחה אישית" if selected_call.queue_type == "personal" else "לתור הממתינים"
                root_logger.info(f"שידוך הצליח בחדר {free_room}: נציג {caller_phone} ומאזין {selected_call.phone}")
                return Response(f"id_list_message=t-מעביר אותך {call_type_desc} בחדר {free_room}.g-{agent_dest}&go_to_folder={agent_dest}", media_type="text/plain; charset=utf-8")
            else:
                root_logger.info(f"נציג {caller_phone} נכנס לשלוחה 20, אך אין שיחות ממתינות עבורו.")
                return Response("id_list_message=t-אין שיחות ממתינות עבורך כעת&go_to_folder=..", media_type="text/plain; charset=utf-8")

    # ==============================================================
    # 5. כניסת מאזינים לתור
    # ==============================================================
    sub_ext = api_extension
    if BASE_FOLDER and sub_ext.startswith(f"{BASE_FOLDER}/"):
        sub_ext = sub_ext[len(BASE_FOLDER) + 1:]

    if len(manager.waiting_calls) == 0:
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
        target_title = f"לשיחה אישית עם {p_info.name}" if p_info.name else f"לשיחה אישית"
    elif sub_ext in sheet_data.general_queue_extensions:
        dept_id = sheet_data.general_queue_extensions[sub_ext]
        queue_type = "general"
        target_title = f"לתור הכללי במחלקה {dept_id}"

    if not queue_type:
        root_logger.warning(f"שלוחה {api_extension} לא מוגדרת בשיטס (טלפון מאזין: {caller_phone})")
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

            if t_key not in manager.tzintuk_tasks or manager.tzintuk_tasks[t_key].done():
                manager.tzintuk_tasks[t_key] = asyncio.create_task(run_tzintuk_loop(t_key, phone_func))

            root_logger.info(f"מאזין {caller_phone} נכנס {target_title} (מיקום בתור: {pos})")
            return Response(f"id_list_message=t-מיקומך בתור {target_title} הוא {pos}&go_to_folder=/{api_extension}", media_type="text/plain; charset=utf-8")

    # כניסה חוזרת – המתנה רציפה
    event = asyncio.Event()
    manager.waiting_events[call_id] = event

    try:
        await asyncio.wait_for(event.wait(), timeout=float(WAIT_TIMEOUT))
        fallback_dest = f"/{ROOMS_ROOT_FOLDER}/{dept_id}/1/1"
        destination = manager.waiting_destinations.get(call_id, fallback_dest)
        root_logger.info(f"מאזין {caller_phone} שוחרר מההמתנה ועובר ל-{destination}")
        return Response(f"id_list_message=t-מעביר אותך לנציג.g-{destination}&go_to_folder={destination}", media_type="text/plain; charset=utf-8")
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


# ==================== הפעלת השרת ====================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)
