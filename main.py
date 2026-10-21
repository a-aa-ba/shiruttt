from fastapi import FastAPI, Response, Request
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import asyncio
import datetime
import httpx
import time
import re

# ==================== הגדרות מערכת כלליות ====================
YEMOT_API_TOKEN = "0775311511:124578"          # החלף בטוקן ה-API שלך
BASE_FOLDER = "00"               # כרגע יושב תחת 00. כשתעביר לשלוחה הראשית, פשוט שנה ל-""
WAIT_TIMEOUT = 50                # שניות המתנה רציפה בבקשת HTTP בודדת
GRACE_PERIOD = WAIT_TIMEOUT + 30 
# ============================================================


def build_path(sub_path: str) -> str:
    """עוזר לבניית נתיב מלא בהתאם ל-BASE_FOLDER"""
    sub = sub_path.strip("/")
    if BASE_FOLDER:
        return f"{BASE_FOLDER}/{sub}"
    return sub


def normalize_phone(phone: str) -> str:
    """ניקוי מספר טלפון לפורמט ישראלי סטנדרטי (05xxxxxxxx)"""
    clean = re.sub(r'\D', '', str(phone or ""))
    if clean.startswith("972"):
        clean = "0" + clean[3:]
    return clean


# ==================== מבנה נתוני שיחה ותורים ====================
@dataclass
class QueuedCall:
    call_id: str
    phone: str
    entry_time: float
    dept_id: str
    queue_type: str         # "general" או "personal"
    target_manager_phone: Optional[str] = None  # רלוונטי רק בשיחה אישית


@dataclass
class PersonalLineConfig:
    sub_ext: str            # שלוחת השיחה האישית, למשל "1/4"
    name: str               # שם המנהל
    manager_phone: str      # מספר הנייד של המנהל שיקבל צינתוק


@dataclass
class DepartmentConfig:
    dept_id: str
    name: str
    general_queue_ext: str        # שלוחת התור הכללי, למשל "1/3"
    rooms_base: str               # בסיס החדרים, למשל "1/rooms"
    tzintuk_file_path: str         # קובץ רשימת נציגים לתור הכללי
    personal_lines: Dict[str, PersonalLineConfig] = field(default_factory=dict)
    max_rooms: int = 10


# ==================== הגדרת המחלקות והשלוחות ====================
# כאן אתה מגדיר את שלוחות המחלקה. הנתיבים ייבנו אוטומטית תחת BASE_FOLDER
DEPARTMENTS: Dict[str, DepartmentConfig] = {
    "1": DepartmentConfig(
        dept_id="1",
        name="חדר ביגוד",
        general_queue_ext="1/3",     # שלוחה 00/1/3 (תור כללי)
        rooms_base="1/rooms",        # חדרים ייפתחו ב-00/1/rooms/{חדר}/...
        tzintuk_file_path="ivr2:00/1/3/a.ini",  # קובץ נציגים כלליים
        personal_lines={
            # שלוחה 00/1/4: שיחה אישית למנהל ביגוד
            "1/4": PersonalLineConfig(
                sub_ext="1/4",
                name="מנהל חדר ביגוד",
                manager_phone="0501234567"  # המספר הנייד שיקבל צינתוק
            )
        }
    ),
    "2": DepartmentConfig(
        dept_id="2",
        name="חדר צצ\"א",
        general_queue_ext="2/3",
        rooms_base="2/rooms",
        tzintuk_file_path="ivr2:00/2/3/a.ini",
        personal_lines={
            # שלוחה 00/2/4: שיחה אישית למנהל צצ"א
            "2/4": PersonalLineConfig(
                sub_ext="2/4",
                name="מנהל חדר צצ\"א",
                manager_phone="0527654321"
            )
        }
    )
}

# שלוחת הנציג המרכזית (00/20)
CENTRAL_AGENT_EXT = "20"
# ============================================================


class SystemManager:
    """מנהל המערכת הכללי: תורים, מנעולים, שיחות וצינתוקים"""
    def __init__(self):
        # רשימת כל הממתינים בכל המערכת
        self.waiting_calls: Dict[str, QueuedCall] = {}
        # אירועים אסינכרוניים להערת המאזין
        self.waiting_events: Dict[str, asyncio.Event] = {}
        # יעד ההעברה של כל מאזין ששוחרר
        self.waiting_destinations: Dict[str, str] = {}
        # זמן אחרון שנצפתה פנייה מהמאזין
        self.last_seen: Dict[str, float] = {}
        # משימות צינתוקים פעילות לפי מפתח (למשל: "general_1" או "personal_0501234567")
        self.tzintuk_tasks: Dict[str, asyncio.Task] = {}
        # מנעול ראשי למניעת Race Conditions במשיכת שיחות והקצאת חדרים
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
        print(f"[ERROR] תקלת תקשורת ב-API ({endpoint}): {e}")
        return None


async def get_active_calls():
    data = await yemot_api_request("GetIncomingCalls")
    if data and data.get("responseStatus") == "OK":
        return data.get("calls", [])
    return None


async def fetch_general_agent_phones(file_path: str) -> List[str]:
    data = await yemot_api_request("GetTextFile", params={"what": file_path})
    if not data or data.get("responseStatus") != "OK":
        return []
    
    clean_numbers = []
    for line in data.get("contents", "").splitlines():
        match = re.search(r'\d{9,12}', line.strip())
        if match:
            num = normalize_phone(match.group(0))
            if num not in clean_numbers:
                clean_numbers.append(num)
    return clean_numbers


async def send_tzintuk(phone: str):
    await yemot_api_request("RunTzintuk", params={
        "phones": f"tzl:{phone}",
        "TzintukTimeOut": 10,
        "sayInfoOnAnswer": "true"
    })


async def run_tzintuk_loop(target_key: str, phone_provider):
    """משימת רקע השולחת צינתוקים כל 10 שניות כל עוד יש ממתינים לתור זה"""
    try:
        while True:
            # בדיקה האם יש עדיין ממתינים שרלוונטיים למפתח זה
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

            phones = await phone_provider() if callable(phone_provider) else phone_provider
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


def find_free_room(active_calls: list, dept_cfg: DepartmentConfig) -> int:
    """מוודא שגם המאזין וגם הנציג אינם בחדר כדי למנוע התנגשות"""
    active_paths = [str(c.get("path", "")) for c in (active_calls or [])]
    full_base = build_path(dept_cfg.rooms_base)
    
    for r in range(1, dept_cfg.max_rooms + 1):
        listener_path = f"{full_base}/{r}/1"
        agent_path = f"{full_base}/{r}/2"
        if not any(listener_path in p or agent_path in p for p in active_paths):
            return r
    return 1


# ==================== סנכרון רקע וניקוי ניתוקים ====================
async def sync_queue_monitor():
    while True:
        try:
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
                        print(f"[MONITOR] שיחה {cid} נותקה ע\"י המאזין. מסיר מהתור.")
                        manager.waiting_calls.pop(cid, None)
                        manager.waiting_events.pop(cid, None)
                        manager.waiting_destinations.pop(cid, None)
                        manager.last_seen.pop(cid, None)
        except Exception as e:
            print(f"[ERROR] שגיאה בסורק הרקע: {e}")
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(sync_queue_monitor())
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

    # 2. Race Condition: אם המאזין כבר שוחרר לחדר טרם הכניסה להמתנה
    if call_id in manager.waiting_destinations:
        destination = manager.waiting_destinations.get(call_id)
        if call_id in manager.waiting_events:
            manager.waiting_events[call_id].set()
        return Response(f"id_list_message=t-מעביר אותך לנציג&go_to_folder={destination}", media_type="text/plain; charset=utf-8")

    # ==============================================================
    # 3. שלוחת הנציגים/מנהלים הכללית (שלוחה 20)
    # ==============================================================
    agent_full_ext = build_path(CENTRAL_AGENT_EXT)
    if api_extension == agent_full_ext or api_extension == CENTRAL_AGENT_EXT:
        async with manager.lock:
            selected_call: Optional[QueuedCall] = None

            # שלב א': עדיפות עליונה לשיחות אישיות שמיועדות למספר זה! (לפי סדר כניסה FIFO)
            personal_calls_for_me = [
                c for c in manager.waiting_calls.values()
                if c.queue_type == "personal" and c.target_manager_phone == caller_phone
            ]
            if personal_calls_for_me:
                personal_calls_for_me.sort(key=lambda x: x.entry_time)
                selected_call = personal_calls_for_me[0]
                print(f"[AGENT] מנהל {caller_phone} זוהה! מושך שיחה אישית ראשונה בתור.")

            # שלב ב': אם אין שיחה אישית, בדיקה האם הוא מורשה כנציג בתורים כלליים
            if not selected_call:
                eligible_general_calls = []
                for c in manager.waiting_calls.values():
                    if c.queue_type == "general":
                        dept_cfg = DEPARTMENTS.get(c.dept_id)
                        if dept_cfg:
                            # קריאת הנציגים המורשים במחלקה זו
                            phones = await fetch_general_agent_phones(dept_cfg.tzintuk_file_path)
                            if caller_phone in phones:
                                eligible_general_calls.append(c)

                if eligible_general_calls:
                    eligible_general_calls.sort(key=lambda x: x.entry_time)
                    selected_call = eligible_general_calls[0]
                    print(f"[AGENT] נציג {caller_phone} מושך שיחה כללית ראשונה בתור (מחלקה {selected_call.dept_id}).")

            # שלב ג': אם נמצאה שיחה – העברה לחדר ועידה פנוי
            if selected_call:
                active_calls = await get_active_calls()
                dept_cfg = DEPARTMENTS[selected_call.dept_id]
                free_room = find_free_room(active_calls, dept_cfg)

                rooms_path = build_path(f"{dept_cfg.rooms_base}/{free_room}")
                agent_dest = f"/{rooms_path}/2"
                listener_dest = f"/{rooms_path}/1"

                # הסרה מהתור ושחרור המאזין
                manager.waiting_calls.pop(selected_call.call_id, None)
                manager.waiting_destinations[selected_call.call_id] = listener_dest

                if selected_call.call_id in manager.waiting_events:
                    manager.waiting_events[selected_call.call_id].set()

                log_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{log_time}] חיבור מוצלח! נציג הועבר ל-{agent_dest}, מאזין {selected_call.call_id} הועבר ל-{listener_dest}")

                msg = "לשיחה אישית" if selected_call.queue_type == "personal" else "לתור הממתינים"
                return Response(f"id_list_message=t-מעביר אותך {msg} בחדר {free_room}&go_to_folder={agent_dest}", media_type="text/plain; charset=utf-8")
            else:
                return Response("id_list_message=t-אין שיחות ממתינות עבורך כעת&go_to_folder=..", media_type="text/plain; charset=utf-8")

    # ==============================================================
    # 4. כניסת מאזינים: תור כללי או שיחה אישית למנהל
    # ==============================================================
    matched_dept: Optional[DepartmentConfig] = None
    queue_type = None
    target_manager_phone = None

    for dept_id, cfg in DEPARTMENTS.items():
        # בדיקת תור כללי של המחלקה
        if api_extension == build_path(cfg.general_queue_ext):
            matched_dept = cfg
            queue_type = "general"
            break
        # בדיקת שיחה אישית למנהל
        for p_key, p_cfg in cfg.personal_lines.items():
            if api_extension == build_path(p_cfg.sub_ext):
                matched_dept = cfg
                queue_type = "personal"
                target_manager_phone = normalize_phone(p_cfg.manager_phone)
                break
        if matched_dept:
            break

    if not matched_dept:
        return Response("id_list_message=t-שלוחה לא מוגדרת בשרת&go_to_folder=..", media_type="text/plain; charset=utf-8")

    # הוספת מאזין לתור (כניסה ראשונה)
    async with manager.lock:
        if call_id not in manager.waiting_calls:
            q_call = QueuedCall(
                call_id=call_id,
                phone=caller_phone,
                entry_time=time.time(),
                dept_id=matched_dept.dept_id,
                queue_type=queue_type,
                target_manager_phone=target_manager_phone
            )
            manager.waiting_calls[call_id] = q_call

            # חישוב מיקום המאזין בתת-התור הספציפי
            if queue_type == "personal":
                pos = sum(1 for c in manager.waiting_calls.values() if c.target_manager_phone == target_manager_phone)
                t_key = f"personal_{target_manager_phone}"
                provider = target_manager_phone
                title = "לשיחה אישית עם המנהל"
            else:
                pos = sum(1 for c in manager.waiting_calls.values() if c.dept_id == matched_dept.dept_id and c.queue_type == "general")
                t_key = f"general_{matched_dept.dept_id}"
                provider = lambda: fetch_general_agent_phones(matched_dept.tzintuk_file_path)
                title = f"במחלקת {matched_dept.name}"

            # הפעלת משימת צינתוקים אם אינה פועלת
            if t_key not in manager.tzintuk_tasks or manager.tzintuk_tasks[t_key].done():
                manager.tzintuk_tasks[t_key] = asyncio.create_task(run_tzintuk_loop(t_key, provider))

            log_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{log_time}] מאזין {call_id} הצטרף {title}. מיקום: {pos}")
            return Response(f"id_list_message=t-מיקומך בתור {title} הוא {pos}&go_to_folder=/{api_extension}", media_type="text/plain; charset=utf-8")

    # כניסה חוזרת של המאזין – מצב המתנה רציפה
    event = asyncio.Event()
    manager.waiting_events[call_id] = event

    try:
        await asyncio.wait_for(event.wait(), timeout=float(WAIT_TIMEOUT))
        destination = manager.waiting_destinations.get(call_id, f"/{build_path(matched_dept.rooms_base)}/1/1")
        return Response(f"id_list_message=t-מעביר אותך לנציג&go_to_folder={destination}", media_type="text/plain; charset=utf-8")
    except asyncio.TimeoutError:
        # פקיעת זמן המתנה - השמעת מיקום חוזרת
        async with manager.lock:
            if queue_type == "personal":
                pos = sum(1 for c in manager.waiting_calls.values() if c.target_manager_phone == target_manager_phone and c.entry_time <= manager.waiting_calls[call_id].entry_time)
            else:
                pos = sum(1 for c in manager.waiting_calls.values() if c.dept_id == matched_dept.dept_id and c.queue_type == "general" and c.entry_time <= manager.waiting_calls[call_id].entry_time)
        return Response(f"id_list_message=t-מיקומך בתור הוא {pos}&go_to_folder=/{api_extension}", media_type="text/plain; charset=utf-8")
    finally:
        manager.waiting_events.pop(call_id, None)
        manager.waiting_destinations.pop(call_id, None)
