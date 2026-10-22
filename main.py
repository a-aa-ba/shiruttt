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


# ==================== הגדרת מערכת לוגים ====================
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "system.log")

log_formatter = logging.Formatter(
    fmt="[%(asctime)s] [%(levelname)-7s] %(message)s",
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
    assigned_room: Optional[str] = None     # חדר אישי ספציפי (אם הוגדר)


@dataclass
class ManagerLineInfo:
    name: str
    phone: str
    dept_id: str
    room: Optional[str] = None              # חדר אישי ייעודי


class SheetDataManager:
    """ניהול משיכת נתונים מ-Google Sheets לפי טאבים לכל מחלקה"""
    def __init__(self, sheet_url: str):
        self.sheet_url = sheet_url
        self.spreadsheet_id = self._extract_sheet_id(sheet_url)
        self.personal_lines: Dict[str, ManagerLineInfo] = {}
        self.dept_general_agents: Dict[str, Set[str]] = {}
        self.general_queue_extensions: Dict[str, str] = {}
        self.agent_to_depts: Dict[str, Set[str]] = {}
        self.phone_to_name: Dict[str, str] = {} # מיפוי מספר טלפון לשם

    def _extract_sheet_id(self, url: str) -> str:
        match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
        return match.group(1) if match else ""

    async def reload_from_sheets(self):
        if not self.spreadsheet_id:
            log_sheets.warning("קישור Google Sheets אינו מוגדר כראוי.")
            return

        log_sheets.info("מרענן נתונים מ-Google Sheets לפי מחלקות...")
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
                    reader = csv.DictReader(io.StringIO(content))

                    if dept_id not in new_dept_general_agents:
                        new_dept_general_agents[dept_id] = set()

                    for row in reader:
                        clean_row = {k.strip(): v.strip() for k, v in row.items() if k}
                        sub_ext = clean_row.get("שלוחה", "").strip("/")
                        name = clean_row.get("שם", "")
                        phone = normalize_phone(clean_row.get("טלפון", ""))
                        role = clean_row.get("תפקיד", "")
                        room = clean_row.get("חדר", "").strip()

                        if not sub_ext or not phone:
                            continue

                        if name:
                            new_phone_to_name[phone] = name

                        # ברירת מחדל לחדר אישי: עמודת חדר, או מספר השלוחה עצמה אם היא מספר
                        personal_room = room if room else (sub_ext if sub_ext.isdigit() else None)

                        if role in ["מנהל", "אישי"]:
                            new_personal_lines[sub_ext] = ManagerLineInfo(
                                name=name, 
                                phone=phone, 
                                dept_id=dept_id, 
                                room=personal_room
                            )
                        else:
                            new_dept_general_agents[dept_id].add(phone)
                            new_general_queues[sub_ext] = dept_id
                            if phone not in new_agent_to_depts:
                                new_agent_to_depts[phone] = set()
                            new_agent_to_depts[phone].add(dept_id)

                except Exception as e:
                    log_sheets.warning(f"שגיאה בקריאת טאב מחלקה {tab_name}: {e}")

        self.personal_lines = new_personal_lines
        self.dept_general_agents = new_dept_general_agents
        self.general_queue_extensions = new_general_queues
        self.agent_to_depts = new_agent_to_depts
        self.phone_to_name = new_phone_to_name

        total_agents = sum(len(v) for v in self.dept_general_agents.values())
        log_sheets.info(f"סנכרון Sheets הושלם: {len(self.personal_lines)} קווים אישיים, {total_agents} נציגים כלליים.")


sheet_data = SheetDataManager(GOOGLE_SHEET_URL)


class SystemManager:
    def __init__(self):
        self.waiting_calls: Dict[str, QueuedCall] = {}
        self.waiting_events: Dict[str, asyncio.Event] = {}
        self.waiting_destinations: Dict[str, str] = {}
        self.last_seen: Dict[str, float] = {}
        self.tzintuk_tasks: Dict[str, asyncio.Task] = {}
        self.call_history: Dict[str, dict] = {}       # היסטוריית שיחות לפי call_id
        self.last_call_by_phone: Dict[str, dict] = {} # היסטוריה לפי טלפון
        self.recording_counter = 0                    # מונה הקלטות ללוג (001.wav...)
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
                log_tzintuk.info(f"[צינתוקים] התור התרוקן - הצינתוקים של {target_key} נעצרו.")


manager = SystemManager()


# ==================== תקשורת API מול ימות המשיח ====================
async def yemot_api_request(endpoint: str, params: dict = None, method: str = "GET", data: dict = None):
    if not YEMOT_API_TOKEN or YEMOT_API_TOKEN == "your_actual_token_here":
        log_call.error("שגיאה: טוקן ה-API לא מוגדר בקוד! (YEMOT_API_TOKEN)")
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
                res = await client.post(url, data=post_data, timeout=10.0)
            else:
                res = await client.get(url, params=p, timeout=10.0)
            return res.json()
    except Exception as e:
        log_call.error(f"שגיאת תקשורת מול ימות המשיח ({endpoint}): {e}")
        return None


async def append_to_yemot_log(log_line: str):
    """רישום שורת לוג לקובץ 00/30/Log.ini בימות המשיח דרך AppendTextFile"""
    res = await yemot_api_request(
        "AppendTextFile",
        method="POST",
        data={
            "what": YEMOT_LOG_FILE_PATH,
            "contents": log_line + "\n"
        }
    )
    if res and res.get("responseStatus") == "OK":
        log_call.info(f"[לוג ימות] נרשם בהצלחה: {log_line}")
    else:
        log_call.warning(f"[לוג ימות] רישום שורה ל-Log.ini נכשל ({res})")


async def get_active_calls():
    data = await yemot_api_request("GetIncomingCalls")
    if data and data.get("responseStatus") == "OK":
        return data.get("calls", [])
    return None


async def is_agent_connected(phone: str) -> bool:
    """בודק האם הנציג הרים את השפופרת ונמצא כרגע בשיחה פעילה במערכת"""
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
        log_tzintuk.error(f"[צינתוק] מספר טלפון שגוי ({clean_phone}). הצינתוק בוטל.")
        return

    target_list = f"tzl:{clean_phone}"
    result = await yemot_api_request("RunTzintuk", params={
        "phones": target_list,
        "TzintukTimeOut": 10,
        "sayInfoOnAnswer": "true"
    })

    if result and result.get("responseStatus") == "OK":
        log_tzintuk.info(f"[צינתוק] צינתוק נשלח בהצלחה לטלפון: {clean_phone}")
    else:
        error_msg = result.get("message") if result else "אין תגובה מהשרת"
        log_tzintuk.warning(f"[צינתוק] שליחת צינתוק ל-{clean_phone} נכשלה ({error_msg})")


async def run_tzintuk_loop(target_key: str, get_phones_func):
    """לולאת צינתוקים מדורגת לפי בדיקת מענה של הנציג"""
    log_tzintuk.info(f"[צינתוקים] לולאת צינתוקים הופעלה עבור יעד: {target_key}")
    try:
        while True:
            if not manager.has_waiting_for_target(target_key):
                break

            phones = get_phones_func()
            if isinstance(phones, str):
                phones = [phones]

            if not phones:
                log_tzintuk.warning(f"[צינתוקים] לא נמצאו מספרי טלפון עבור {target_key}.")
                await asyncio.sleep(15)
                continue

            for phone in phones:
                if not manager.has_waiting_for_target(target_key):
                    return

                # 1. שליחת הצינתוק
                await send_tzintuk(phone)

                # 2. בדיקה האם הנציג ענה לצינתוק ונכנס לקו (עד 10 שניות)
                agent_entered = False
                for _ in range(10):
                    await asyncio.sleep(1)
                    if not manager.has_waiting_for_target(target_key):
                        return

                    if await is_agent_connected(phone):
                        agent_entered = True
                        log_tzintuk.info(f"[צינתוקים] נציג {phone} ענה לצינתוק ונכנס למערכת.")
                        break

                # 3. אם הנציג נכנס לקו - ממתינים עד 10 שניות לבדוק האם הוא מושך את השיחה
                if agent_entered:
                    log_tzintuk.info(f"[צינתוקים] ממתין לנציג {phone} שיקבל את השיחה בשלוחה 20...")
                    for _ in range(10):
                        await asyncio.sleep(1)
                        if not manager.has_waiting_for_target(target_key):
                            log_tzintuk.info(f"[צינתוקים] השיחה נענתה בהצלחה. סבב הצינתוקים הסתיים.")
                            return
                    log_tzintuk.warning(f"[צינתוקים] נציג {phone} לא לקח את השיחה תוך 10 שניות. עובר לנציג הבא.")

            await asyncio.sleep(10)

    except asyncio.CancelledError:
        log_tzintuk.info(f"[צינתוקים] משימת הצינתוקים של {target_key} נעצרה.")
    except Exception as e:
        log_tzintuk.exception(f"[צינתוקים] תקלה בלולאת הצינתוקים ({target_key}): {e}")
    finally:
        manager.tzintuk_tasks.pop(target_key, None)


def find_free_room(active_calls: list, dept_id: str) -> int:
    """מאתר חדר כללי פנוי בין 1 ל-5 בתוך התיקייה הראשית rooms/{dept_id}"""
    active_paths = [str(c.get("path", "")) for c in (active_calls or [])]
    rooms_base = f"{ROOMS_ROOT_FOLDER}/{dept_id}"
    
    for r in range(1, 6):
        listener_path = f"{rooms_base}/{r}/1"
        agent_path = f"{rooms_base}/{r}/2"
        if not any(listener_path in p or agent_path in p for p in active_paths):
            return r
    return 1


# ==================== משימות רקע (ניקוי שיחות בלבד) ====================
async def background_cleanup_loop():
    """בודק שיחות שנותקו ללא דיווח"""
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
                            log_sync.info(f"[סנכרון] שיחה {cid} נותקה ללא דיווח (Timeout). הוסרה מהתור.")
                            manager.waiting_calls.pop(cid, None)
                            manager.waiting_events.pop(cid, None)
                            manager.waiting_destinations.pop(cid, None)
                            manager.last_seen.pop(cid, None)
                            
                            t_key = f"personal_{call.target_manager_phone}" if call.queue_type == "personal" else f"general_{call.dept_id}"
                            manager.cancel_tzintuk_if_empty(t_key)
        except Exception as e:
            log_sync.exception(f"[סנכרון] שגיאה בלולאת הניקוי: {e}")
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

    # 1. טיפול מיידי בניתוק שיחה
    if hangup == "yes" and call_id:
        async with manager.lock:
            removed_call = manager.waiting_calls.pop(call_id, None)
            if call_id in manager.waiting_events:
                manager.waiting_events[call_id].set()
            manager.waiting_destinations.pop(call_id, None)
            manager.waiting_events.pop(call_id, None)

            caller_desc = f"({removed_call.phone})" if removed_call else ""
            log_call.info(f"[ניתוק] שיחה {call_id} {caller_desc} נותקה (hangup=yes).")

            if removed_call:
                t_key = f"personal_{removed_call.target_manager_phone}" if removed_call.queue_type == "personal" else f"general_{removed_call.dept_id}"
                manager.cancel_tzintuk_if_empty(t_key)

        return Response("noop=yes", media_type="text/plain; charset=utf-8")

    if not call_id:
        log_call.warning(f"[בקשה חריגה] התקבלה פנייה ללא מזהה שיחה.")
        return Response("id_list_message=t-מזהה שיחה חסר&go_to_folder=..", media_type="text/plain; charset=utf-8")

    # 2. Race Condition: אם המאזין כבר שוחרר לחדר טרם כניסתו להמתנה
    if call_id in manager.waiting_destinations:
        destination = manager.waiting_destinations.get(call_id)
        if call_id in manager.waiting_events:
            manager.waiting_events[call_id].set()
        log_call.info(f"[Race Condition] מאזין {caller_phone} כבר שודך לחדר. מעביר מיידית ל-{destination}")
        return Response(f"id_list_message=t-מעביר אותך לנציג&go_to_folder={destination}", media_type="text/plain; charset=utf-8")

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
            # שליפה ישירה של השיחה לפי מזהה השיחה הייחודי (או טלפון המאזין)
            rec = manager.call_history.get(call_id) or manager.last_call_by_phone.get(caller_phone)
            if rec:
                date_str = rec.get("date_str", "")
                time_str = rec.get("time_str", "")
                agent_name = rec.get("agent_name", "").strip()
                agent_phone = rec.get("agent_phone", "")
                customer_name = rec.get("caller_name", "").strip()
                customer_phone = rec.get("caller_phone", caller_phone)

                # אם יש שם – משמיע את השם, אם אין שם – מקריא את המספר ספרה-ספרה
                agent_display = f"t-{agent_name}" if agent_name else f"d-{agent_phone}"
                customer_display = f"t-{customer_name}" if customer_name else f"d-{customer_phone}"

                msg = f"t-פרטי השיחה. תאריך: {date_str}, בשעה: {time_str}. הנציג: {agent_display}. המאזין: {customer_display}."
                log_call.info(f"[שלוחה 30] משמיע פרטי שיחה למאזין {caller_phone}: נציג={agent_name or agent_phone}, שעה={time_str}")
                return Response(f"id_list_message={msg}&go_to_folder=hangup", media_type="text/plain; charset=utf-8")
            else:
                log_call.warning(f"[שלוחה 30] לא נמצאו פרטי שיחה קודמת עבור מזהה {call_id} (טלפון: {caller_phone})")
                return Response("id_list_message=t-לא נמצאו פרטי שיחה קודמת במערכת&go_to_folder=hangup", media_type="text/plain; charset=utf-8")
        else:
            return Response("read=t-לשמיעת פרטי השיחה הקישו 0=digits,1,1,1,7,No,No,0", media_type="text/plain; charset=utf-8")

    # ==============================================================
    # 4. שלוחת הנציגים/מנהלים הכללית (שלוחה 00/20)
    # ==============================================================
    agent_full_ext = build_path("20")
    if api_extension == agent_full_ext or api_extension == "20":
        log_agent.info(f"[כניסת נציג] נציג/מנהל התחבר לשלוחה 20 (טלפון: {caller_phone})")
        async with manager.lock:
            selected_call: Optional[QueuedCall] = None

            # עדיxxx x: שיחה אישית למנהל (רק המנהל הספציפי יכול למשוך!)
            personal_calls = [
                c for c in manager.waiting_calls.values()
                if c.queue_type == "personal" and c.target_manager_phone == caller_phone
            ]
            if personal_calls:
                personal_calls.sort(key=lambda x: x.entry_time)
                selected_call = personal_calls[0]
                log_agent.info(f"[שיחה אישית] מנהל זוהה! מושך שיחה אישית ממאזין {selected_call.phone}.")

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
                    log_agent.info(f"[שיחה כללית] נציג זוהה! מושך שיחה ממחלקה {selected_call.dept_id} (מאזין: {selected_call.phone}).")

            # שידוך לחדר (אם נקבע חדר אישי ספציפי – הולך אליו, אחרת מחפש חדר פנוי 1-5)
            if selected_call:
                if selected_call.assigned_room:
                    free_room = selected_call.assigned_room
                    log_agent.info(f"[חדר אישי] משדך לחדר אישי קבוע שהוגדר בשיטס: {free_room}")
                else:
                    active_calls = await get_active_calls()
                    free_room = find_free_room(active_calls, selected_call.dept_id)

                agent_dest = f"/{ROOMS_ROOT_FOLDER}/{selected_call.dept_id}/{free_room}/2"
                listener_dest = f"/{ROOMS_ROOT_FOLDER}/{selected_call.dept_id}/{free_room}/1"

                manager.waiting_calls.pop(selected_call.call_id, None)
                manager.waiting_destinations[selected_call.call_id] = listener_dest

                if selected_call.call_id in manager.waiting_events:
                    manager.waiting_events[selected_call.call_id].set()

                # תיעוד השיחה בזיכרון השרת ובקובץ Log.ini של ימות המשיח
                now_dt = datetime.datetime.now()
                manager.recording_counter += 1
                rec_filename = f"{manager.recording_counter:03d}.wav"
                
                agent_name = sheet_data.phone_to_name.get(caller_phone, "")
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

                # יצירת שורת הלוג בפורמט: 001.wav=24/08/2026|11:08|טלפון_נציג|שם_נציג|טלפון_מאזין|שם_מאזין
                log_entry = f"{rec_filename}={date_str}|{time_str}|{caller_phone}|{agent_name}|{selected_call.phone}|{customer_name}"
                asyncio.create_task(append_to_yemot_log(log_entry))

                t_key = f"personal_{selected_call.target_manager_phone}" if selected_call.queue_type == "personal" else f"general_{selected_call.dept_id}"
                manager.cancel_tzintuk_if_empty(t_key)

                call_type_desc = "לשיחה אישית" if selected_call.queue_type == "personal" else "לתור הממתינים"
                log_agent.info(f"[שידוך מוצלח] חדר: {free_room} | נציג ({caller_phone} - {agent_name or 'ללא שם'}) ⬅️ {agent_dest} | מאזין ({selected_call.phone} - {customer_name or 'ללא שם'}) ⬅️ {listener_dest}")
                return Response(f"id_list_message=t-מעביר אותך {call_type_desc} בחדר {free_room}&go_to_folder={agent_dest}", media_type="text/plain; charset=utf-8")
            else:
                log_agent.info(f"[נציג ממתין] נציג {caller_phone} נכנס לשלוחה 20, אך אין שיחות ממתינות עבורו.")
                return Response("id_list_message=t-אין שיחות ממתינות עבורך כעת&go_to_folder=..", media_type="text/plain; charset=utf-8")

    # ==============================================================
    # 5. כניסת מאזינים לתור
    # ==============================================================
    sub_ext = api_extension
    if BASE_FOLDER and sub_ext.startswith(f"{BASE_FOLDER}/"):
        sub_ext = sub_ext[len(BASE_FOLDER) + 1:]

    if len(manager.waiting_calls) == 0:
        log_queue.info("מאזין חדש נכנס לתור ריק - מרענן הגדרות מ-Google Sheets...")
        await sheet_data.reload_from_sheets()

    queue_type = None
    target_manager_phone = None
    dept_id = None
    assigned_room = None
    target_title = ""

    if sub_ext in sheet_data.personal_lines:
        p_info = sheet_data.personal_lines[sub_ext]
        queue_type = "personal"
        target_manager_phone = p_info.phone
        dept_id = p_info.dept_id
        assigned_room = p_info.room
        target_title = f"לשיחה אישית עם {p_info.name}" if p_info.name else f"לשיחה אישית"
    elif sub_ext in sheet_data.general_queue_extensions:
        dept_id = sheet_data.general_queue_extensions[sub_ext]
        queue_type = "general"
        target_title = f"לתור הכללי במחלקה {dept_id}"

    if not queue_type:
        log_call.warning(f"[שלוחה חריגה] שלוחה {api_extension} לא מוגדרת בשיטס (טלפון מאזין: {caller_phone})")
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
                sub_ext=sub_ext,
                assigned_room=assigned_room
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

            room_note = f" [חדר אישי: {assigned_room}]" if assigned_room else ""
            log_queue.info(f"[כניסת מאזין] מאזין {caller_phone} נכנס {target_title}{room_note} (מיקום בתור: {pos})")
            return Response(f"id_list_message=t-מיקומך בתור {target_title} הוא {pos}&go_to_folder=/{api_extension}", media_type="text/plain; charset=utf-8")

    # כניסה חוזרת – המתנה רציפה
    event = asyncio.Event()
    manager.waiting_events[call_id] = event

    try:
        await asyncio.wait_for(event.wait(), timeout=float(WAIT_TIMEOUT))
        default_room = assigned_room if assigned_room else "1"
        fallback_dest = f"/{ROOMS_ROOT_FOLDER}/{dept_id}/{default_room}/1"
        destination = manager.waiting_destinations.get(call_id, fallback_dest)
        log_queue.info(f"[שחרור מאזין] מאזין {caller_phone} שוחרר מההמתנה והועבר לנציג (יעד: {destination})")
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


# ==================== הפעלת השרת ====================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
