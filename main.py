from fastapi import FastAPI, Response, Request
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Dict, List, Optional, Set
import asyncio
import datetime
import httpx
import time
import csv
import io
import re

# ==================== הגדרות מערכת כלליות ====================
YEMOT_API_TOKEN = "0775311511:124578"          # החלף בטוקן ה-API שלך
BASE_FOLDER = "00"               # תיקיית הבסיס (00), בעתיד ניתן לשנות ל-""

# הדבק כאן את קישור השיתוף של קובץ הגוגל שיטס שלך:
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1lSf6FK2wjCMwu-oZzrBsWlvPw5wXQ2RK6RJnV2h1GjM/edit?usp=sharing"

WAIT_TIMEOUT = 50                # שניות המתנה רציפה בבקשת HTTP
GRACE_PERIOD = WAIT_TIMEOUT + 30 
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
    target_manager_phone: Optional[str]     # רלוונטי רק בשיחה אישית
    sub_ext: str


@dataclass
class ManagerLineInfo:
    name: str
    phone: str
    dept_id: str


class SheetDataManager:
    """ניהול משיכת ועדכון נתונים מ-Google Sheets בזמן אמת"""
    def __init__(self, sheet_url: str):
        self.sheet_url = sheet_url
        self.csv_export_url = self._build_export_url(sheet_url)
        # שלוחה -> פרטי מנהל (למשל: "1/4" -> ManagerLineInfo)
        self.personal_lines: Dict[str, ManagerLineInfo] = {}
        # מחלקה -> רשימת מספרי נציגים כלליים
        self.dept_general_agents: Dict[str, Set[str]] = {}
        # שלוחות התור הכללי (למשל: {"1/3": "1", "2/3": "2"})
        self.general_queue_extensions: Dict[str, str] = {}
        # מיפוי מספר טלפון -> רשימת מחלקות שהוא מורשה בהן כנציג
        self.agent_to_depts: Dict[str, Set[str]] = {}

    def _build_export_url(self, url: str) -> str:
        match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
        if match:
            sheet_id = match.group(1)
            return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
        return url

    async def reload_from_sheets(self):
        """משיכת ה-CSV מ-Google Sheets ועדכון המילונים"""
        if "YOUR_SPREADSHEET_ID" in self.csv_export_url:
            return  # עדיין לא הוגדר קישור
            
        try:
            async with httpx.AsyncClient() as client:
                res = await client.get(self.csv_export_url, timeout=10.0, follow_redirects=True)
                if res.status_code != 200:
                    print(f"[SHEETS] שגיאה במשיכת הגיליון: קוד סטטוס {res.status_code}")
                    return

                content = res.content.decode('utf-8-sig')
                reader = csv.DictReader(io.StringIO(content))

                new_personal_lines = {}
                new_dept_general_agents = {}
                new_general_queues = {}
                new_agent_to_depts = {}

                for row in reader:
                    # ניקוי שמות העמודות מרווחים
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
                    else:  # נציג
                        new_dept_general_agents[dept].add(phone)
                        new_general_queues[sub_ext] = dept
                        if phone not in new_agent_to_depts:
                            new_agent_to_depts[phone] = set()
                        new_agent_to_depts[phone].add(dept)

                # החלפה אטומית בזיכרון
                self.personal_lines = new_personal_lines
                self.dept_general_agents = new_dept_general_agents
                self.general_queue_extensions = new_general_queues
                self.agent_to_depts = new_agent_to_depts

                log_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{log_time}] [SHEETS] הגיליון סונכרן בהצלחה! מנהלים: {len(self.personal_lines)}, נציגים: {sum(len(v) for v in self.dept_general_agents.values())}")
        except Exception as e:
            print(f"[SHEETS] תקלה בטעינת הגיליון: {e}")


sheet_data = SheetDataManager(GOOGLE_SHEET_URL)


class SystemManager:
    def __init__(self):
        self.waiting_calls: Dict[str, QueuedCall] = {}
        self.waiting_events: Dict[str, asyncio.Event] = {}
        self.waiting_destinations: Dict[str, str] = {}
        self.last_seen: Dict[str, float] = {}
        self.tzintuk_tasks: Dict[str, asyncio.Task] = {}
        self.lock = asyncio.Lock()


manager = SystemManager()


# ==================== תקשורת API מול ימות המשיח ====================
async def yemot_api_request(endpoint: str, params: dict = None):
    if YEMOT_API_TOKEN == "073":
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
        print(f"[ERROR] תקלת API ({endpoint}): {e}")
        return None


async def get_active_calls():
    data = await yemot_api_request("GetIncomingCalls")
    if data and data.get("responseStatus") == "OK":
        return data.get("calls", [])
    return None


async def send_tzintuk(phone: str):
    await yemot_api_request("RunTzintuk", params={
        "phones": f"tzl:{phone}",
        "TzintukTimeOut": 10,
        "sayInfoOnAnswer": "true"
    })


async def run_tzintuk_loop(target_key: str, get_phones_func):
    """שליחת צינתוקים כל עוד יש ממתינים לתור זה"""
    try:
        while True:
            has_waiting = False
            for call in manager.waiting_calls.values():
                if target_key.startswith("personal_") and call.target_manager_phone == target_key.replace("personal_", ""):
                    has_waiting = True
                    break
                elif target_key.startswith("general_") and f"general_{call.dept_id}" == target_key:
                    has_waiting = True
                    break

            if not has_waiting:
                break

            phones = get_phones_func()
            if isinstance(phones, str):
                phones = [phones]

            for phone in phones:
                await send_tzintuk(phone)
                await asyncio.sleep(10)
                
            await asyncio.sleep(20)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[ERROR] שגיאה בלולאת צינתוקים ({target_key}): {e}")
    finally:
        manager.tzintuk_tasks.pop(target_key, None)


def find_free_room(active_calls: list, dept_id: str) -> int:
    """איתור חדר פנוי (מוודא שגם המאזין וגם הנציג אינם בחדר)"""
    active_paths = [str(c.get("path", "")) for c in (active_calls or [])]
    rooms_base = build_path(f"{dept_id}/rooms")
    
    for r in range(1, 15):
        listener_path = f"{rooms_base}/{r}/1"
        agent_path = f"{rooms_base}/{r}/2"
        if not any(listener_path in p or agent_path in p for p in active_paths):
            return r
    return 1


# ==================== משימות רקע (סנכרון תור וגוגל שיטס) ====================
async def background_sync_loop():
    while True:
        try:
            # 1. רענון נתונים מגוגל שיטס
            await sheet_data.reload_from_sheets()

            # 2. ניקוי שיחות שנותקו ע"י המאזין
            active_calls = await get_active_calls()
            if active_calls is not None:
                active_ids = {str(c.get("id")) for c in active_calls}
                now = time.time()
                
                async with manager.lock:
                    to_remove = []
                    for cid, call in manager.waiting_calls.items():
                        if cid not in active_ids and (now - manager.last_seen.get(cid, 0)) >= GRACE_PERIOD:
                            to_remove.append(cid)
                            
                    for cid in to_remove:
                        print(f"[MONITOR] שיחה {cid} נותקה. הוסרה מהתור.")
                        manager.waiting_calls.pop(cid, None)
                        manager.waiting_events.pop(cid, None)
                        manager.waiting_destinations.pop(cid, None)
                        manager.last_seen.pop(cid, None)
        except Exception as e:
            print(f"[ERROR] שגיאה בלולאת הרקע: {e}")
        await asyncio.sleep(30)  # סנכרון כל 30 שניות


@asynccontextmanager
async def lifespan(app: FastAPI):
    # סנכרון ראשוני בהפעלת השרת
    await sheet_data.reload_from_sheets()
    task = asyncio.create_task(background_sync_loop())
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)


# ==================== נקודת הקצה הראשית (API) ====================
@app.get("/")
async def yemot_router(request: Request):
    params = dict(request.query_params)
    call_id = params.get("ApiCallId")
    hangup = params.get("hangup")
    api_extension = params.get("ApiExtension", "").strip("/")
    caller_phone = normalize_phone(params.get("ApiPhone", ""))

    if call_id:
        manager.last_seen[call_id] = time.time()

    # 1. ניתוק שיחה
    if hangup == "yes" and call_id:
        async with manager.lock:
            manager.waiting_calls.pop(call_id, None)
            if call_id in manager.waiting_events:
                manager.waiting_events[call_id].set()
            manager.waiting_destinations.pop(call_id, None)
            manager.waiting_events.pop(call_id, None)
        return Response("noop=yes", media_type="text/plain; charset=utf-8")

    if not call_id:
        return Response("id_list_message=t-מזהה שיחה חסר&go_to_folder=..", media_type="text/plain; charset=utf-8")

    # 2. פתרון Race Condition
    if call_id in manager.waiting_destinations:
        destination = manager.waiting_destinations.get(call_id)
        if call_id in manager.waiting_events:
            manager.waiting_events[call_id].set()
        return Response(f"id_list_message=t-מעביר אותך לנציג&go_to_folder={destination}", media_type="text/plain; charset=utf-8")

    # ==============================================================
    # 3. כניסת נציג/מנהל לשלוחה 20 הראשית
    # ==============================================================
    agent_full_ext = build_path("20")
    if api_extension == agent_full_ext or api_extension == "20":
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
                print(f"[AGENT] מנהל מזוהה ({caller_phone}) מושך שיחה אישית ראשונה.")

            # עדיפות 2: שיחה כללית במחלקות שהנציג מורשה בהן (לפי השיטס)
            if not selected_call:
                allowed_depts = sheet_data.agent_to_depts.get(caller_phone, set())
                general_calls = [
                    c for c in manager.waiting_calls.values()
                    if c.queue_type == "general" and c.dept_id in allowed_depts
                ]
                if general_calls:
                    general_calls.sort(key=lambda x: x.entry_time)
                    selected_call = general_calls[0]
                    print(f"[AGENT] נציג ({caller_phone}) מושך שיחה כללית ראשונה (מחלקה {selected_call.dept_id}).")

            # ניתוב לחדר פנוי
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

                msg = "לשיחה אישית" if selected_call.queue_type == "personal" else "לתור הממתינים"
                return Response(f"id_list_message=t-מעביר אותך {msg} בחדר {free_room}&go_to_folder={agent_dest}", media_type="text/plain; charset=utf-8")
            else:
                return Response("id_list_message=t-אין שיחות ממתינות עבורך כעת&go_to_folder=..", media_type="text/plain; charset=utf-8")

    # ==============================================================
    # 4. כניסת מאזינים (תור כללי או שיחה אישית)
    # ==============================================================
    # הסרת קידומת BASE_FOLDER אם קיימת, כדי לקבל את תת-השלוחה (למשל "1/3")
    sub_ext = api_extension
    if BASE_FOLDER and sub_ext.startswith(f"{BASE_FOLDER}/"):
        sub_ext = sub_ext[len(BASE_FOLDER) + 1:]

    queue_type = None
    target_manager_phone = None
    dept_id = None
    target_title = ""

    # בדיקה האם זו שלוחת שיחה אישית
    if sub_ext in sheet_data.personal_lines:
        p_info = sheet_data.personal_lines[sub_ext]
        queue_type = "personal"
        target_manager_phone = p_info.phone
        dept_id = p_info.dept_id
        target_title = f"לשיחה אישית עם {p_info.name}"

    # בדיקה האם זו שלוחת תור כללי
    elif sub_ext in sheet_data.general_queue_extensions:
        dept_id = sheet_data.general_queue_extensions[sub_ext]
        queue_type = "general"
        target_title = f"לתור הכללי במחלקה {dept_id}"

    if not queue_type:
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

            # חישוב מיקום בתור
            if queue_type == "personal":
                pos = sum(1 for c in manager.waiting_calls.values() if c.target_manager_phone == target_manager_phone)
                t_key = f"personal_{target_manager_phone}"
                phone_func = lambda: [target_manager_phone]
            else:
                pos = sum(1 for c in manager.waiting_calls.values() if c.dept_id == dept_id and c.queue_type == "general")
                t_key = f"general_{dept_id}"
                phone_func = lambda: list(sheet_data.dept_general_agents.get(dept_id, []))

            # הפעלת צינתוקים
            if t_key not in manager.tzintuk_tasks or manager.tzintuk_tasks[t_key].done():
                manager.tzintuk_tasks[t_key] = asyncio.create_task(run_tzintuk_loop(t_key, phone_func))

            log_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{log_time}] מאזין {call_id} הצטרף {target_title}. מיקום בתור: {pos}")
            return Response(f"id_list_message=t-מיקומך בתור {target_title} הוא {pos}&go_to_folder=/{api_extension}", media_type="text/plain; charset=utf-8")

    # כניסה חוזרת – מצב המתנה רציפה
    event = asyncio.Event()
    manager.waiting_events[call_id] = event

    try:
        await asyncio.wait_for(event.wait(), timeout=float(WAIT_TIMEOUT))
        default_room = f"/{build_path(f'{dept_id}/rooms/1/1')}"
        destination = manager.waiting_destinations.get(call_id, default_room)
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
