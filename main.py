from fastapi import FastAPI, Response, Request
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple
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
YEMOT_API_TOKEN = "0775311511:124578"

# תיקיית הבסיס - שלוחה ראשית (ריק)
BASE_FOLDER = ""

# שם התיקייה בשלוחה הראשית שבה נמצאים החדרים:
ROOMS_ROOT_FOLDER = "rooms"

# נתיבי קבצי מערכת בימות המשיח:
YEMOT_LOG_FILE_PATH = "ivr2:30/Log.ini"
YEMOT_TRACKER_FILE_PATH = "ivr2:30/messages_tracker.ini"

# ==== הגדרת שלוחות ההודעות למחלקות (לסריקת הודעות חדשות) ====
DEPT_MESSAGES_EXTENSIONS = {
    "1": "1/0/2",  
    "2": "2/0/2",  
    "3": "3/0/2",
    "4": "4/0/2",
}

# קישור ה-Google Sheets שלך
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1D_i2bqqZfEhc1CtSS2snoEeF_tZ1DVoXK4hWhGULJ5U/edit?usp=sharing"

# 📌 מיפוי שמות הלשוניות בדיוק לפי התמונה מהגיליון שלך!
DEPARTMENT_TABS_MAPPING = {
    "1": "חדר ביגוד",
    "2": 'חדר צצ"א',
    "3": "הסכמי חנויות",
    "4": "עזרה לנזקקים",
}

# שם לשונית הלקוחות
CONTACTS_TAB_NAME = "לקוחות"

# מיפוי שמות מחלקות לתיקיות חדרים (1, 2, 3, 4)
DEPT_NAME_TO_ID = {
    "1": "1",
    "ביגוד": "1",
    "חדר ביגוד": "1",

    "2": "2",
    "צצא": "2",
    'צצ"א': "2",
    "חדר צצא": "2",
    'חדר צצ"א': "2",

    "3": "3",
    "הסדרים": "3",
    "הסכמי חנויות": "3",
    "הסכם חנויות": "3",

    "4": "4",
    "עזרה": "4",
    "עזרה לנזקקים": "4",
}

WAIT_TIMEOUT = 50                
GRACE_PERIOD = WAIT_TIMEOUT + 30 
# ============================================================


# ==================== פונקציית שעון ישראל מדויק ====================
def get_israel_now() -> datetime.datetime:
    try:
        import zoneinfo
        return datetime.datetime.now(zoneinfo.ZoneInfo("Asia/Jerusalem"))
    except Exception:
        try:
            import pytz
            return datetime.datetime.now(pytz.timezone("Asia/Jerusalem"))
        except Exception:
            tz = datetime.timezone(datetime.timedelta(hours=3))
            return datetime.datetime.now(tz)
# ==================================================================


# ==================== הגדרת לוג נקי, ברור ומפורט ====================
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


def get_base_root_dest() -> str:
    return f"/{BASE_FOLDER}" if BASE_FOLDER else "/"


def normalize_phone(phone: str) -> str:
    clean = re.sub(r'\D', '', str(phone or ""))
    if clean.startswith("972"):
        clean = "0" + clean[3:]
    elif len(clean) in [8, 9] and not clean.startswith("0"):
        clean = "0" + clean
    return clean


def format_time_to_hebrew(time_str: str) -> str:
    parts = time_str.split(":")
    if len(parts) != 2:
        return f"בשעה {time_str}"
    try:
        h = int(parts[0])
        m = int(parts[1])
    except Exception:
        return f"בשעה {time_str}"

    hours_map = {
        0: "שתים עשרה בלילה", 1: "אחת", 2: "שתיים", 3: "שלוש", 4: "ארבע",
        5: "חמש", 6: "שש", 7: "שבע", 8: "שמונה", 9: "תשע", 10: "עשר",
        11: "אחת עשרה", 12: "שתים עשרה", 13: "אחת בצהריים", 14: "שתיים בצהריים",
        15: "שלוש בצהריים", 16: "ארבע", 17: "חמש", 18: "שש", 19: "שבע",
        20: "שמונה", 21: "תשע", 22: "עשר", 23: "אחת עשרה בלילה",
    }
    h_word = hours_map.get(h, str(h))

    if m == 0:
        return f"בשעה {h_word} בדיוק"

    units = ["", "אחת", "שתיים", "שלוש", "ארבע", "חמש", "שש", "שבע", "שמונה", "תשע"]
    teens = ["עשר", "אחת עשרה", "שתים עשרה", "שלוש עשרה", "ארבע עשרה", "חמש עשרה", "שש עשרה", "שבע עשרה", "שמונה עשרה", "תשע עשרה"]
    tens = ["", "", "עשרים", "שלושים", "ארבעים", "חמישים"]

    if 1 <= m <= 9:
        m_word = f"ו{units[m]} דקות"
    elif 10 <= m <= 19:
        m_word = f"ו{teens[m - 10]} דקות"
    else:
        ten_part = tens[m // 10]
        unit_part = units[m % 10]
        if unit_part:
            m_word = f"ו{ten_part} ו{unit_part} דקות"
        else:
            m_word = f"ו{ten_part} דקות"

    return f"בשעה {h_word} {m_word}"


def format_recording_details_tts(rec: dict) -> str:
    date_str = rec.get("date_str", "")
    time_str = rec.get("time_str", "")
    agent_name = rec.get("agent_name", "").strip()
    agent_phone = rec.get("agent_phone", "")
    customer_name = rec.get("caller_name", "").strip()
    customer_phone = rec.get("caller_phone", "")
    is_anon = rec.get("is_anonymous", False)

    date_parts = date_str.split("/")
    if len(date_parts) == 3:
        try:
            clean_date = f"{int(date_parts[0])} לחודש {int(date_parts[1])}"
        except Exception:
            clean_date = date_str.replace("/", " ")
    else:
        clean_date = date_str.replace("/", " ")

    time_phrase = format_time_to_hebrew(time_str)

    segments = []
    segments.append(f"t-בתאריך {clean_date} {time_phrase}")

    if is_anon:
        segments.append("t-שוחח המאזין שיחה אנונימית מספר חסוי")
    elif customer_name:
        segments.append(f"t-שוחח המאזין {customer_name}")
    elif customer_phone:
        segments.append("t-שוחח המאזין")
        segments.append(f"d-{customer_phone}")
    else:
        segments.append("t-שוחח המאזין")

    if agent_name:
        segments.append(f"t-עם הנציג {agent_name}")
    elif agent_phone:
        segments.append("t-עם הנציג")
        segments.append(f"d-{agent_phone}")
    else:
        segments.append("t-עם הנציג")

    return ".".join(segments)


# ==================== מבני נתונים ====================
@dataclass
class QueuedCall:
    call_id: str
    phone: str
    caller_name: str
    entry_time: float
    dept_id: str
    queue_type: str                         
    target_manager_phone: Optional[str]     
    sub_ext: str
    transfer_message: str = "מעביר אותך לשיחה עם נציג"
    is_anonymous: bool = False


@dataclass
class ManagerLineInfo:
    name: str
    phone: str
    dept_id: str
    is_manager: bool = False


class SheetDataManager:
    def __init__(self, sheet_url: str):
        self.sheet_url = sheet_url
        self.spreadsheet_id = self._extract_sheet_id(sheet_url)
        self.personal_lines: Dict[str, Dict[str, ManagerLineInfo]] = {}
        self.dept_general_agents: Dict[str, Set[str]] = {}
        self.general_queue_extensions: Dict[str, Set[str]] = {}
        self.anonymous_queue_extensions: Dict[str, Set[str]] = {}
        self.dept_messages_extension: Dict[str, str] = {}
        self.agent_to_depts: Dict[str, Set[str]] = {}
        self.agent_names: Dict[str, str] = {}
        self.customer_contacts: Dict[str, str] = {}

    def _extract_sheet_id(self, url: str) -> str:
        match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
        if match:
            return match.group(1)
        return ""

    async def reload_from_sheets(self):
        if not self.spreadsheet_id:
            root_logger.warning("⚠️ קישור Google Sheets אינו מוגדר.")
            return

        root_logger.info("🔄 מתחיל סנכרון נתונים מ-Google Sheets...")
        new_personal_lines = {}
        new_dept_general_agents = {}
        new_general_queues = {}
        new_anonymous_queues = {}
        new_dept_messages_ext = {}
        new_agent_to_depts = {}
        new_agent_names = {}
        new_customer_contacts = {}

        async with httpx.AsyncClient() as client:
            sheet1_content = None

            for dept_id, tab_name in DEPARTMENT_TABS_MAPPING.items():
                tab_encoded = urllib.parse.quote(tab_name)
                url = f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}/gviz/tq?tqx=out:csv&sheet={tab_encoded}"
                
                try:
                    res = await client.get(url, timeout=10.0, follow_redirects=True)
                    if res.status_code != 200:
                        root_logger.warning(f"⚠️ מחלקה {dept_id}: קריאת לשונית '{tab_name}' נכשלה (קוד HTTP {res.status_code})")
                        continue

                    content = res.content.decode('utf-8-sig').strip()
                    if not content or content.startswith("/*O_o*/"):
                        root_logger.warning(f"⚠️ מחלקה {dept_id}: לשונית '{tab_name}' אינה קיימת בגיליון!")
                        continue

                    if dept_id == "1":
                        sheet1_content = content
                    elif sheet1_content and content == sheet1_content:
                        root_logger.error(f"❌ שגיאה: הלשונית של מחלקה {dept_id} ('{tab_name}') לא נמצאה בגיליון! (גוגל החזיר במקומה את חדר הביגוד)")
                        continue

                    reader = csv.reader(io.StringIO(content))
                    rows = list(reader)
                    if len(rows) < 2:
                        continue

                    headers = [h.strip() for h in rows[0]]
                    if not any(h in ["שלוחה", "טלפון", "שם"] for h in headers):
                        root_logger.warning(f"⚠️ מחלקה {dept_id}: הלשונית '{tab_name}' אינה מכילה עמודות שלוחה/טלפון תקניות.")
                        continue

                    if dept_id not in new_dept_general_agents:
                        new_dept_general_agents[dept_id] = set()
                    if dept_id not in new_personal_lines:
                        new_personal_lines[dept_id] = {}
                    if dept_id not in new_general_queues:
                        new_general_queues[dept_id] = set()
                    if dept_id not in new_anonymous_queues:
                        new_anonymous_queues[dept_id] = set()

                    new_dept_messages_ext[dept_id] = DEPT_MESSAGES_EXTENSIONS.get(dept_id, f"{dept_id}/8")

                    for r in rows[1:]:
                        if not r or not any(r):
                            continue
                        
                        row_dict = {headers[i]: r[i].strip() for i in range(min(len(headers), len(r)))}

                        raw_sub = row_dict.get("שלוחה", "").strip("/")
                        name = row_dict.get("שם", "").strip()
                        phone = normalize_phone(row_dict.get("טלפון", ""))

                        if not raw_sub or not phone:
                            continue
                        
                        # דילוג על שלוחות ההקלטה 4/2/1 ו-4/2/2 כדי שלא יירשמו כתורי נציגים
                        if raw_sub in ["4/2/1", "4/2/2"]:
                            continue

                        custom_msg_ext = row_dict.get("שלוחת הודעות", "").strip("/")
                        if custom_msg_ext:
                            new_dept_messages_ext[dept_id] = custom_msg_ext

                        row_text = " ".join(r)
                        is_personal = any(word in row_text for word in ["אישי", "מנהל", "פרטי"])
                        is_manager_role = "מנהל" in row_text
                        is_anonymous = "אנונימי" in row_text

                        if name:
                            new_agent_names[phone] = name

                        possible_keys = {raw_sub}
                        if "/" not in raw_sub:
                            possible_keys.add(f"{dept_id}/{raw_sub}")

                        if is_personal:
                            for k in possible_keys:
                                new_personal_lines[dept_id][k] = ManagerLineInfo(
                                    name=name, 
                                    phone=phone, 
                                    dept_id=dept_id,
                                    is_manager=is_manager_role
                                )
                        elif is_anonymous:
                            new_dept_general_agents[dept_id].add(phone)
                            for k in possible_keys:
                                new_anonymous_queues[dept_id].add(k)
                            if phone not in new_agent_to_depts:
                                new_agent_to_depts[phone] = set()
                            new_agent_to_depts[phone].add(dept_id)
                        else:
                            new_dept_general_agents[dept_id].add(phone)
                            for k in possible_keys:
                                new_general_queues[dept_id].add(k)
                            if phone not in new_agent_to_depts:
                                new_agent_to_depts[phone] = set()
                            new_agent_to_depts[phone].add(dept_id)

                except Exception as e:
                    root_logger.warning(f"⚠️ שגיאה בקריאת טאב מחלקה {tab_name}: {e}")

            # טעינת לשונית לקוחות
            c_tab_encoded = urllib.parse.quote(CONTACTS_TAB_NAME)
            url = f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}/gviz/tq?tqx=out:csv&sheet={c_tab_encoded}"
            try:
                res = await client.get(url, timeout=10.0, follow_redirects=True)
                if res.status_code == 200 and not res.text.strip().startswith("/*O_o*/"):
                    content = res.content.decode('utf-8-sig')
                    reader = csv.reader(io.StringIO(content))
                    rows = [r for r in reader if any(cell.strip() for cell in r)]
                    if rows:
                        temp_contacts = {}
                        for r in rows:
                            if len(r) < 2:
                                continue
                            col_a = r[0].strip()
                            col_b = r[1].strip()

                            if col_a in ["שם", "שם לקוח", "שם מלא", "name"] or any(w in col_b for w in ["מספר", "טלפון", "נייד", "סלולרי", "פלאפון", "phone"]):
                                continue

                            digits_b = re.sub(r'\D', '', col_b)
                            digits_a = re.sub(r'\D', '', col_a)

                            if len(digits_b) >= 7:
                                cust_name = col_a
                                cust_phone = normalize_phone(digits_b)
                            elif len(digits_a) >= 7:
                                cust_name = col_b
                                cust_phone = normalize_phone(digits_a)
                            else:
                                continue

                            if cust_name and cust_phone:
                                temp_contacts[cust_phone] = cust_name

                        if temp_contacts:
                            new_customer_contacts = temp_contacts
            except Exception:
                pass

        self.personal_lines = new_personal_lines
        self.dept_general_agents = new_dept_general_agents
        self.general_queue_extensions = new_general_queues
        self.anonymous_queue_extensions = new_anonymous_queues
        self.dept_messages_extension = new_dept_messages_ext
        self.agent_to_depts = new_agent_to_depts
        self.agent_names = new_agent_names
        self.customer_contacts = new_customer_contacts

        total_p = sum(len(v) for v in self.personal_lines.values())
        total_gen = sum(len(v) for v in self.dept_general_agents.values())
        root_logger.info(f"✅ סנכרון הושלם: {total_p} קווים אישיים, {total_gen} נציגים כלליים, {len(self.customer_contacts)} לקוחות.")
        
        for d_id in ["1", "2", "3", "4"]:
            agents = list(self.dept_general_agents.get(d_id, set()))
            p_lines = len(self.personal_lines.get(d_id, {}))
            dept_title = {
                "1": "ביגוד", 
                "2": 'צצא', 
                "3": "הסכמי חנויות", 
                "4": "עזרה לנזקקים"
            }.get(d_id, f"מחלקה {d_id}")
            root_logger.info(f"   👥 מחלקה {d_id} ({dept_title}): נציגים כלליים ({len(agents)}) -> {agents} | קווים אישיים ({p_lines})")


sheet_data = SheetDataManager(GOOGLE_SHEET_URL)


class SystemManager:
    def __init__(self):
        self.waiting_calls: Dict[str, QueuedCall] = {}
        self.waiting_events: Dict[str, asyncio.Event] = {}
        self.waiting_destinations: Dict[str, str] = {}
        self.transferred_calls: Dict[str, str] = {}
        self.busy_agents: Set[str] = set()
        self.occupied_rooms: Dict[str, Set[int]] = {}
        self.last_seen: Dict[str, float] = {}
        self.tzintuk_tasks: Dict[str, asyncio.Task] = {}
        self.call_history: Dict[str, dict] = {}
        self.last_call_by_phone: Dict[str, dict] = {}
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
                root_logger.info(f"⏹️ התור התרוקן - סבב הצינתוקים עבור {target_key} הופסק.")


manager = SystemManager()


# ==================== תקשורת API מול ימות המשיח ====================
async def yemot_api_request(endpoint: str, params: dict = None, method: str = "GET", data: dict = None, timeout: float = 3.0):
    if not YEMOT_API_TOKEN or YEMOT_API_TOKEN == "your_actual_token_here":
        root_logger.error("❌ שגיאה: טוקן ה-API לא מוגדר בקוד!")
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
        root_logger.error(f"❌ שגיאת תקשורת API מול ימות המשיח ({endpoint}): {e}")
        return None


async def append_to_yemot_log(caller_phone: str, customer_name: str, agent_phone: str, agent_name: str, date_str: str, time_str: str, is_anonymous: bool):
    try:
        res_get = await yemot_api_request("GetTextFile", params={"what": YEMOT_LOG_FILE_PATH}, timeout=4.0)
        current_content = ""
        line_count = 0

        if res_get and res_get.get("responseStatus") == "OK":
            current_content = res_get.get("contents", "")
            lines = [l.strip() for l in current_content.splitlines() if l.strip()]
            line_count = len(lines)

        next_num = line_count + 1
        rec_filename = f"{next_num:03d}.wav"

        new_entry = f"{rec_filename}={date_str}|{time_str}|{agent_phone}|{agent_name}|{caller_phone}|{customer_name}"
        updated_content = current_content.rstrip() + ("\n" if current_content.strip() else "") + new_entry + "\n"

        res_upload = await yemot_api_request(
            "UploadTextFile",
            method="POST",
            data={
                "what": YEMOT_LOG_FILE_PATH,
                "contents": updated_content
            },
            timeout=5.0
        )
        if res_upload and res_upload.get("responseStatus") == "OK":
            root_logger.info(f"📝 קובץ הלוג Log.ini עודכן בהצלחה: {new_entry}")
    except Exception as e:
        root_logger.error(f"❌ שגיאה ברישום לוג בימות המשיח: {e}")


async def get_agent_last_heard_file(agent_phone: str) -> str:
    try:
        res = await yemot_api_request("GetTextFile", params={"what": YEMOT_TRACKER_FILE_PATH}, timeout=3.0)
        if res and res.get("responseStatus") == "OK":
            for line in res.get("contents", "").splitlines():
                if line.startswith(f"{agent_phone}="):
                    return line.split("=")[-1].strip()
    except Exception:
        pass
    return ""


async def save_agent_last_heard_file(agent_phone: str, latest_file: str):
    try:
        res = await yemot_api_request("GetTextFile", params={"what": YEMOT_TRACKER_FILE_PATH}, timeout=3.0)
        lines = []
        if res and res.get("responseStatus") == "OK":
            lines = [l.strip() for l in res.get("contents", "").splitlines() if l.strip() and not l.startswith(f"{agent_phone}=")]
        
        lines.append(f"{agent_phone}={latest_file}")
        
        await yemot_api_request(
            "UploadTextFile", 
            method="POST", 
            data={
                "what": YEMOT_TRACKER_FILE_PATH, 
                "contents": "\n".join(lines) + "\n"
            }, 
            timeout=4.0
        )
        root_logger.info(f"📌 עודכן קובץ מעקב לנציג {agent_phone}: קובץ אחרון {latest_file}")
    except Exception as e:
        root_logger.error(f"❌ שגיאה בעדכון קובץ מעקב: {e}")


async def check_new_messages_for_agent(agent_phone: str) -> Tuple[bool, str, str]:
    allowed_depts = sheet_data.agent_to_depts.get(agent_phone, set())
    for d_id, dept_dict in sheet_data.personal_lines.items():
        for info in dept_dict.values():
            if info.phone == agent_phone:
                allowed_depts.add(d_id)

    if not allowed_depts:
        return False, "", ""

    dept_id = list(allowed_depts)[0]
    msg_sub_ext = sheet_data.dept_messages_extension.get(dept_id, f"{dept_id}/8")
    msg_ext_full = build_path(msg_sub_ext)

    res = await yemot_api_request("GetIVR2Dir", params={"path": f"ivr2:{msg_ext_full}"}, timeout=3.0)
    if not res or res.get("responseStatus") != "OK":
        return False, msg_ext_full, ""

    wav_files = sorted([f.get("name") for f in res.get("files", []) if f.get("name", "").lower().endswith(".wav") and not f.get("name", "").startswith(".")])
    if not wav_files:
        return False, msg_ext_full, ""

    latest_file = wav_files[-1]
    last_heard = await get_agent_last_heard_file(agent_phone)
    if latest_file > last_heard:
        return True, msg_ext_full, latest_file

    return False, msg_ext_full, latest_file


async def get_active_calls():
    data = await yemot_api_request("GetIncomingCalls", timeout=1.5)
    if data and data.get("responseStatus") == "OK":
        return data.get("calls", [])
    return None


async def is_agent_connected(phone: str) -> bool:
    clean_target = normalize_phone(phone)
    if clean_target in manager.busy_agents:
        return True
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
        root_logger.warning(f"⚠️ דילוג על צינתוק: מספר טלפון אינו תקין ({phone})")
        return

    agent_name = sheet_data.agent_names.get(clean_phone, "")
    name_str = f" ({agent_name})" if agent_name else ""

    root_logger.info(f"📱 מנסה לשלוח צינתוק לנציג: {clean_phone}{name_str}...")
    result = await yemot_api_request("RunTzintuk", params={
        "phones": f"tzl:{clean_phone}",
        "TzintukTimeOut": 10,
        "sayInfoOnAnswer": "true"
    }, timeout=3.0)

    if result and result.get("responseStatus") == "OK":
        root_logger.info(f"✅ צינתוק נשלח בהצלחה לנציג: {clean_phone}{name_str}")
    else:
        root_logger.error(f"❌ שגיאה בשליחת צינתוק לנציג {clean_phone}: תשובת המערכת הייתה {result}")


async def run_tzintuk_loop(target_key: str, get_phones_func):
    root_logger.info(f"🚀 הופעלה הפצת צינתוקים עבור {target_key}")
    try:
        while True:
            if not manager.has_waiting_for_target(target_key):
                root_logger.info(f"ℹ️ אין יותר שיחות ממתינות עבור {target_key} - לולאת הצינתוקים הסתיימה.")
                break

            phones = get_phones_func()
            if isinstance(phones, str):
                phones = [phones]

            if not phones:
                root_logger.warning(f"⚠️ אין נציגים מוגדרים עבור {target_key}! ממתין 15 שניות לפני בדיקה חוזרת...")
                await asyncio.sleep(15)
                continue

            root_logger.info(f"🔄 מתחיל סבב צינתוקים עבור {target_key} (לנציגים: {phones})")
            for phone in phones:
                if not manager.has_waiting_for_target(target_key):
                    return

                if await is_agent_connected(phone):
                    agent_name = sheet_data.agent_names.get(phone, "")
                    root_logger.info(f"ℹ️ נציג {phone} ({agent_name or 'ללא שם'}) כבר בשיחה פעילה - מדלג עליו בסבב הנוכחי.")
                    continue

                await send_tzintuk(phone)

                agent_entered = False
                for _ in range(10):
                    await asyncio.sleep(1)
                    if not manager.has_waiting_for_target(target_key):
                        return
                    if await is_agent_connected(phone):
                        agent_entered = True
                        agent_name = sheet_data.agent_names.get(phone, "")
                        root_logger.info(f"📞 נציג {phone} ({agent_name or 'ללא שם'}) ענה לצינתוק ונכנס למערכת!")
                        break

                if agent_entered:
                    for _ in range(10):
                        await asyncio.sleep(1)
                        if not manager.has_waiting_for_target(target_key):
                            root_logger.info("🎉 השיחה נענתה בהצלחה. סבב הצינתוקים הסתיים.")
                            return

            if manager.has_waiting_for_target(target_key):
                root_logger.info(f"⏳ הסתיים סבב צינתוקים ללא מענה עבור {target_key}. ממתין 20 שניות לפני הסבב הבא...")
                await asyncio.sleep(20)

    except asyncio.CancelledError:
        root_logger.info(f"⏹️ סבב הצינתוקים עבור {target_key} בוטל.")
    except Exception as e:
        root_logger.error(f"❌ תקלה בסבב צינתוקים ({target_key}): {e}")
    finally:
        manager.tzintuk_tasks.pop(target_key, None)


async def find_free_room(dept_id: str) -> Tuple[str, int]:
    active_calls = await get_active_calls()
    active_paths = [str(c.get("path", "")) for c in (active_calls or [])]
    folders_to_check = [str(dept_id), "5"]

    for folder in folders_to_check:
        rooms_base = f"{ROOMS_ROOT_FOLDER}/{folder}"
        occupied_in_mem = manager.occupied_rooms.get(folder, set())
        for r in range(1, 6):
            p1 = f"{rooms_base}/{r}/1"
            p2 = f"{rooms_base}/{r}/2"
            if not any(p1 in p or p2 in p for p in active_paths) and r not in occupied_in_mem:
                return folder, r

    return str(dept_id), 1


def get_waiting_call_for_agent(caller_phone: str) -> Optional[QueuedCall]:
    personal_calls = [
        c for c in manager.waiting_calls.values()
        if c.queue_type == "personal" and c.target_manager_phone == caller_phone
    ]
    if personal_calls:
        personal_calls.sort(key=lambda x: x.entry_time)
        return personal_calls[0]

    allowed_depts = sheet_data.agent_to_depts.get(caller_phone, set())
    general_calls = [
        c for c in manager.waiting_calls.values()
        if c.queue_type == "general" and c.dept_id in allowed_depts
    ]
    if general_calls:
        general_calls.sort(key=lambda x: x.entry_time)
        return general_calls[0]

    return None


def format_caller_ident_for_tts(call: QueuedCall) -> str:
    if call.is_anonymous:
        return "מאזין בשיחה אנונימית"
    name = call.caller_name or sheet_data.customer_contacts.get(call.phone, "")
    if name:
        return f"המאזין {name}"
    if call.phone:
        spaced_phone = " ".join(call.phone)
        return f"המאזין מטלפון {spaced_phone}"
    return "מאזין ללא זיהוי"


async def bridge_call_for_agent(caller_phone: str, agent_name: str, selected_call: QueuedCall) -> str:
    chosen_folder, free_room = await find_free_room(selected_call.dept_id)

    agent_dest = f"/{ROOMS_ROOT_FOLDER}/{chosen_folder}/{free_room}/2"
    listener_dest = f"/{ROOMS_ROOT_FOLDER}/{chosen_folder}/{free_room}/1"

    manager.waiting_calls.pop(selected_call.call_id, None)
    manager.waiting_destinations[selected_call.call_id] = listener_dest
    manager.transferred_calls[selected_call.call_id] = listener_dest

    manager.busy_agents.add(caller_phone)
    if chosen_folder not in manager.occupied_rooms:
        manager.occupied_rooms[chosen_folder] = set()
    manager.occupied_rooms[chosen_folder].add(free_room)

    if selected_call.call_id in manager.waiting_events:
        manager.waiting_events[selected_call.call_id].set()

    now_dt = get_israel_now()
    customer_name = selected_call.caller_name or sheet_data.customer_contacts.get(selected_call.phone, "")
    date_str = now_dt.strftime("%d/%m/%Y")
    time_str = now_dt.strftime("%H:%M")

    call_record = {
        "call_id": selected_call.call_id,
        "caller_phone": selected_call.phone,
        "caller_name": customer_name,
        "agent_phone": caller_phone,
        "agent_name": agent_name,
        "dept_id": selected_call.dept_id,
        "chosen_folder": chosen_folder,
        "room": free_room,
        "queue_type": selected_call.queue_type,
        "date_str": date_str,
        "time_str": time_str,
        "is_anonymous": selected_call.is_anonymous,
        "timestamp": time.time()
    }
    manager.call_history[selected_call.call_id] = call_record
    manager.last_call_by_phone[selected_call.phone] = call_record

    asyncio.create_task(
        append_to_yemot_log(
            caller_phone=selected_call.phone,
            customer_name=customer_name,
            agent_phone=caller_phone,
            agent_name=agent_name,
            date_str=date_str,
            time_str=time_str,
            is_anonymous=selected_call.is_anonymous
        )
    )

    t_key = f"personal_{selected_call.target_manager_phone}" if selected_call.queue_type == "personal" else f"general_{selected_call.dept_id}"
    manager.cancel_tzintuk_if_empty(t_key)

    c_display = f"{customer_name} ({selected_call.phone})" if customer_name else selected_call.phone
    root_logger.info(f"🎉 שידוך הצליח! חדר {free_room} (תיקייה {chosen_folder}): נציג {caller_phone} ({agent_name}) <--> מאזין {c_display}")
    return agent_dest


async def background_cleanup_loop():
    while True:
        try:
            if manager.waiting_calls:
                active_calls = await get_active_calls()
                if active_calls is not None:
                    active_ids = {str(c.get("id")) for c in active_calls}
                    now = time.time()
                    async with manager.lock:
                        to_remove = [
                            call for cid, call in manager.waiting_calls.items()
                            if cid not in active_ids and (now - manager.last_seen.get(cid, 0)) >= GRACE_PERIOD
                        ]
                        for call in to_remove:
                            cid = call.call_id
                            root_logger.info(f"⚠️ שיחה {cid} ({call.phone}) נותקה ללא דיווח והוסרה מהתור.")
                            manager.waiting_calls.pop(cid, None)
                            manager.waiting_events.pop(cid, None)
                            manager.waiting_destinations.pop(cid, None)
                            manager.last_seen.pop(cid, None)
                            t_key = f"personal_{call.target_manager_phone}" if call.queue_type == "personal" else f"general_{call.dept_id}"
                            manager.cancel_tzintuk_if_empty(t_key)
        except Exception as e:
            root_logger.error(f"❌ שגיאה בלולאת רקע: {e}")
        await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(app: FastAPI):
    root_logger.info("🚀 שרת התורים והנציגים מופעל כעת.")
    await sheet_data.reload_from_sheets()
    task = asyncio.create_task(background_cleanup_loop())
    yield
    task.cancel()
    root_logger.info("🛑 שרת התורים כובה.")

app = FastAPI(lifespan=lifespan)


@app.get("/test")
async def self_test_endpoint():
    return Response(content="<h2 style='color: #2e7d32;'>תקין</h2>", media_type="text/html; charset=utf-8")


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
        return Response('{"status": "ok"}', media_type="application/json")

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

    root_logger.info(f"📥 פנייה מהמערכת: שלוחה [{api_extension or 'ראשי'}] | טלפון: {caller_phone or 'אנונימי'} | CallId: {call_id}")

    # 1. ניתוק שיחה (כשמתקבל דיווח ניתוק מימות המשיח)
    if hangup == "yes" and call_id:
        async with manager.lock:
            removed_call = manager.waiting_calls.pop(call_id, None)
            manager.waiting_events.pop(call_id, None)
            manager.waiting_destinations.pop(call_id, None)
            if caller_phone in manager.busy_agents:
                manager.busy_agents.discard(caller_phone)

            if call_id in manager.call_history:
                f_id = manager.call_history[call_id].get("chosen_folder")
                r_id = manager.call_history[call_id].get("room")
                if f_id and r_id and f_id in manager.occupied_rooms:
                    manager.occupied_rooms[f_id].discard(r_id)

            if removed_call:
                display_name = removed_call.caller_name or removed_call.phone
                root_logger.info(f"📴 מאזין {display_name} ניתק את השיחה ויצא מהתור.")
                t_key = f"personal_{removed_call.target_manager_phone}" if removed_call.queue_type == "personal" else f"general_{removed_call.dept_id}"
                manager.cancel_tzintuk_if_empty(t_key)

        return Response("noop=yes", media_type="text/plain; charset=utf-8")

    if not call_id:
        return Response("id_list_message=t-מזהה שיחה חסר&go_to_folder=..", media_type="text/plain; charset=utf-8")

    # 2. שידוך קיים - העברה מיידית לחדר
    if call_id in manager.waiting_destinations or call_id in manager.transferred_calls:
        destination = manager.waiting_destinations.get(call_id) or manager.transferred_calls.get(call_id)
        if call_id in manager.waiting_events:
            manager.waiting_events[call_id].set()
        root_logger.info(f"🔀 שיחה {call_id} כבר שודכה - מעביר מיידית ל-{destination}")
        return Response(f"id_list_message=t-מעביר אותך לשיחה&go_to_folder={destination}", media_type="text/plain; charset=utf-8")

    # ==============================================================
    # 3. שלוחת הניתוב החכמה לנציגים (שלוחה 10) - תור והודעות
    # ==============================================================
    gateway_ext = build_path("10")
    if api_extension == gateway_ext or api_extension == "10":
        
        is_agent = caller_phone in sheet_data.agent_names or caller_phone in sheet_data.agent_to_depts
        if not is_agent:
            for dept_dict in sheet_data.personal_lines.values():
                for info in dept_dict.values():
                    if info.phone == caller_phone:
                        is_agent = True
                        break

        if not is_agent:
            root_logger.warning(f"⛔ טלפון {caller_phone} ניסה להיכנס לשלוחה 10 אך אינו מוגדר כנציג בשיטס!")
            return Response(f"go_to_folder={get_base_root_dest()}", media_type="text/plain; charset=utf-8")

        agent_name = sheet_data.agent_names.get(caller_phone, "")
        root_logger.info(f"👤 נציג נכנס לשלוחה 10: {caller_phone} ({agent_name or 'ללא שם'})")

        # 🔍 בדיקה חיה מול ימות המשיח: ניקוי שיחות מנותקות בזמן אמת!
        waiting_call = None
        async with manager.lock:
            active_calls = await get_active_calls()
            if active_calls is not None:
                active_ids = {str(c.get("id")) for c in active_calls}
                ghost_ids = [cid for cid in manager.waiting_calls if cid not in active_ids]
                for cid in ghost_ids:
                    ghost_call = manager.waiting_calls.pop(cid, None)
                    manager.waiting_events.pop(cid, None)
                    manager.waiting_destinations.pop(cid, None)
                    manager.last_seen.pop(cid, None)
                    if ghost_call:
                        root_logger.info(f"🧹 שיחה {cid} ({ghost_call.phone}) נותקה בעבר ע\"י המאזין ונוקתה מיידית.")
                        t_key = f"personal_{ghost_call.target_manager_phone}" if ghost_call.queue_type == "personal" else f"general_{ghost_call.dept_id}"
                        manager.cancel_tzintuk_if_empty(t_key)

            waiting_call = get_waiting_call_for_agent(caller_phone)

        has_waiting_call = waiting_call is not None
        has_new_msg, msg_ext_full, latest_wav = await check_new_messages_for_agent(caller_phone)

        call_action = params.get("call_action")
        msg_action = params.get("msg_action")
        action = params.get("action") or params.get("digits") or params.get("data") or params.get("key") or ""

        # שלב א': אישור מענה לשיחה
        if call_action:
            root_logger.info(f"🔘 נציג {caller_phone} הקיש {call_action} בתפריט פרטי השיחה")
            if call_action == "1":
                async with manager.lock:
                    selected_call = get_waiting_call_for_agent(caller_phone)
                    if selected_call:
                        agent_dest = await bridge_call_for_agent(caller_phone, agent_name, selected_call)
                        return Response(f"id_list_message=t-מעביר אותך לשיחה&go_to_folder={agent_dest}", media_type="text/plain; charset=utf-8")
                    else:
                        root_logger.info(f"ℹ️ נציג {caller_phone} אישר מענה אך השיחה כבר נותקה ע\"י המאזין.")
                        return Response(f"id_list_message=t-השיחה כבר אינה ממתינה בתור&go_to_folder={get_base_root_dest()}", media_type="text/plain; charset=utf-8")
            elif call_action == "2":
                root_logger.info(f"↩️ נציג {caller_phone} בחר לדחות את השיחה ולחזור.")
                return Response(f"go_to_folder={get_base_root_dest()}", media_type="text/plain; charset=utf-8")

        # שלב ב': שמיעת הודעה
        if msg_action:
            root_logger.info(f"🔘 נציג {caller_phone} הקיש {msg_action} בתפריט שמיעת הודעות")
            if msg_action == "1" and has_new_msg:
                asyncio.create_task(save_agent_last_heard_file(caller_phone, latest_wav))
                root_logger.info(f"🎧 מעביר נציג {caller_phone} לשמיעת הודעה ב-{msg_ext_full} (קובץ: {latest_wav})")
                return Response(f"id_list_message=t-מעביר אותך לשמיעת ההודעה&go_to_folder=/{msg_ext_full}", media_type="text/plain; charset=utf-8")
            else:
                return Response(f"go_to_folder={get_base_root_dest()}", media_type="text/plain; charset=utf-8")

        # שלב ג': בחירה מתפריט שניהם יחד
        if action:
            root_logger.info(f"🔘 נציג {caller_phone} הקיש {action} בתפריט המשולב (שיחה + הודעה)")
            if action == "1" and has_waiting_call:
                ident = format_caller_ident_for_tts(waiting_call)
                return Response(f"read=t-שיחה ממתינה מאת {ident} למעבר לשיחה הקישו אחת לניתוק הקישו שתיים=call_action,no,1,1,7,Digits", media_type="text/plain; charset=utf-8")
            elif action == "2" and has_new_msg:
                asyncio.create_task(save_agent_last_heard_file(caller_phone, latest_wav))
                return Response(f"id_list_message=t-מעביר אותך לשמיעת ההודעה&go_to_folder=/{msg_ext_full}", media_type="text/plain; charset=utf-8")
            elif action == "3":
                return Response(f"go_to_folder={get_base_root_dest()}", media_type="text/plain; charset=utf-8")

        # תפריטי פתיחה
        if has_waiting_call and has_new_msg:
            root_logger.info(f"📢 מציג לנציג {caller_phone}: קיימת גם שיחה ממתינה וגם הודעה חדשה.")
            return Response("read=t-ישנה שיחה ממתינה וקיימת הודעה חדשה למענה לשיחה הקישו אחת לשמיעת ההודעה הקישו שתיים לכניסה רגילה הקישו שלוש=action,no,1,1,7,Digits", media_type="text/plain; charset=utf-8")
        elif has_waiting_call:
            ident = format_caller_ident_for_tts(waiting_call)
            root_logger.info(f"📢 מציג לנציג {caller_phone}: שיחה ממתינה מאת {ident}")
            return Response(f"read=t-שיחה ממתינה מאת {ident} למעבר לשיחה הקישו אחת לניתוק הקישו שתיים=call_action,no,1,1,7,Digits", media_type="text/plain; charset=utf-8")
        elif has_new_msg:
            root_logger.info(f"📢 מציג לנציג {caller_phone}: קיימת הודעה חדשה.")
            return Response("read=t-קיימת הודעה חדשה לשמיעת ההודעה הקישו אחת לכניסה רגילה הקישו שתיים=msg_action,no,1,1,7,Digits", media_type="text/plain; charset=utf-8")
        else:
            # ✨ שקט מוחלט: אין שיחות ואין הודעות - מעביר מיידית לתפריט הראשי ללא שום השמעה!
            root_logger.info(f"ℹ️ נציג {caller_phone} נכנס לשלוחה 10 - אין שיחות ואין הודעות. מעביר מיידית לתפריט הראשי.")
            return Response(f"go_to_folder={get_base_root_dest()}", media_type="text/plain; charset=utf-8")

    # ==============================================================
    # 4. שלוחה 30: השמעת פרטי השיחה בהקשה על 0
    # ==============================================================
    sub_ext_raw = api_extension
    if BASE_FOLDER and sub_ext_raw.startswith(f"{BASE_FOLDER}/"):
        sub_ext_raw = sub_ext_raw[len(BASE_FOLDER) + 1:]

    pressed_key = params.get("PressKey") or params.get("digits") or params.get("data") or params.get("key") or ""
    is_key_0 = pressed_key == "0" or api_extension.endswith("/0")
    what_file = params.get("what") or params.get("file") or params.get("filename") or ""

    is_ext_30 = (
        sub_ext_raw in ["30", "30/0"] or 
        api_extension in ["30", "30/0", build_path("30"), build_path("30/0")] or
        api_extension.endswith("/30") or
        api_extension.endswith("/30/0") or
        bool(what_file and not api_extension.startswith("4/0/1"))
    )

    if is_ext_30 and is_key_0:
        rec = None
        match_wav = re.search(r'(\d{3}\.wav)', what_file, re.IGNORECASE)
        target_wav = match_wav.group(1) if match_wav else ""

        if target_wav:
            res_log = await yemot_api_request("GetTextFile", params={"what": YEMOT_LOG_FILE_PATH}, timeout=4.0)
            if res_log and res_log.get("responseStatus") == "OK":
                for line in res_log.get("contents", "").splitlines():
                    if line.startswith(f"{target_wav}="):
                        parts = line.split("=")[-1].split("|")
                        if len(parts) >= 6:
                            rec = {
                                "date_str": parts[0],
                                "time_str": parts[1],
                                "agent_phone": parts[2],
                                "agent_name": parts[3],
                                "caller_phone": parts[4],
                                "caller_name": parts[5],
                                "is_anonymous": False
                            }
                            break

        if not rec:
            rec = manager.call_history.get(call_id) or manager.last_call_by_phone.get(caller_phone)

        if rec:
            tts_msg = format_recording_details_tts(rec)
            root_logger.info(f"🔊 הושמעו פרטי שיחה מוקלטת לנציג {caller_phone}: {tts_msg}")
            return Response(f"id_list_message={tts_msg}", media_type="text/plain; charset=utf-8")
        else:
            return Response("id_list_message=t-לא נמצאו פרטים על שיחה זו", media_type="text/plain; charset=utf-8")

    # ==============================================================
    # 🌟 4.1 שלוחות ההאזנה 4/0/1/1 ו-4/0/1/2: חיוג למשאיר ההודעה בלחיצה על 0
    # ==============================================================
    is_ext_4011 = api_extension.endswith("4/0/1/1") or api_extension.endswith("4/0/1/1/0") or sub_ext_raw.endswith("0/1/1")
    is_ext_4012 = api_extension.endswith("4/0/1/2") or api_extension.endswith("4/0/1/2/0") or sub_ext_raw.endswith("0/1/2")

    if (is_ext_4011 or is_ext_4012) and is_key_0:
        match_wav = re.search(r'(\d{3})', what_file)
        file_prefix = match_wav.group(1) if match_wav else "000"
        
        target_folder = "4/0/1/2" if is_ext_4012 else "4/0/1/1"
        txt_path = f"ivr2:{target_folder}/{file_prefix}.txt"
        
        root_logger.info(f"🔍 שולף פרטי מאזין מקובץ {txt_path}...")
        res_txt = await yemot_api_request("GetTextFile", params={"what": txt_path}, timeout=4.0)
        
        caller_phone_found = ""
        if res_txt and res_txt.get("responseStatus") == "OK":
            content = res_txt.get("contents", "")
            root_logger.info(f"📄 תוכן קובץ {file_prefix}.txt שהתקבל מימות המשיח: {content}")
            
            # חילוץ ישיר ומדויק: המספר שמופיע אחרי Phone- (מתעלם לחלוטין מ-CustomerDID!)
            m_phone = re.search(r'Phone-([0-9]+)', content)
            if m_phone:
                caller_phone_found = normalize_phone(m_phone.group(1))
            else:
                # גיבוי: חיפוש כללי
                m_back = re.search(r'(?:CallerId|Phone|Caller|caller_id|טלפון)[-=\s]*([0-9]+)', content, re.IGNORECASE)
                if m_back:
                    caller_phone_found = normalize_phone(m_back.group(1))

        if not caller_phone_found:
            root_logger.warning(f"⚠️ לא נמצא מספר טלפון תקין של מאזין ב-{txt_path}")
            return Response("id_list_message=t-לא נמצא מספר טלפון לחיוג חוזר עבור הודעה זו", media_type="text/plain; charset=utf-8")

        root_logger.info(f"🎯 אותר מספר המאזין שהשאיר את ההודעה: {caller_phone_found}")

        # א. שלוחה 4/0/1/2: חיוג אנונימי באמצעות ניטוביה (הנציג לא רואה ולא שומע את המספר!)
        if is_ext_4012:
            root_logger.info(f"🎭 מחייג אנונימית למשאיר ההודעה ({caller_phone_found}) דרך ניטוביה בשלוחה 4/0/1/2...")
            nitoviya_ext = f"{target_folder}/nitoviya"
            nitoviya_content = (
                "type=nitoviya\n"
                f"nitoviya_dial_to={caller_phone_found}\n"
                "nitoviya_say_dial_to=no\n"
            )
            await yemot_api_request("UploadTextFile", method="POST", data={
                "what": f"ivr2:{nitoviya_ext}/ext.ini",
                "contents": nitoviya_content
            }, timeout=4.0)

            return Response(f"id_list_message=t-מעביר אותך לחיוג אנונימי למשאיר ההודעה&go_to_folder=/{nitoviya_ext}", media_type="text/plain; charset=utf-8")

        # ב. שלוחה 4/0/1/1: חיוג רגיל גלוי
        else:
            root_logger.info(f"📞 מחייג ישירות למשאיר ההודעה: {caller_phone_found}")
            spaced_phone = " ".join(caller_phone_found)
            return Response(f"id_list_message=t-מחייג למשאיר ההודעה בטלפון {spaced_phone}&routing={caller_phone_found}", media_type="text/plain; charset=utf-8")

    # ==============================================================
    # 5. שלוחת הנציגים/מנהלים הכללית (שלוחה 20)
    # ==============================================================
    agent_full_ext = build_path("20")
    if api_extension == agent_full_ext or api_extension == "20":
        manager.busy_agents.discard(caller_phone)
        agent_name = sheet_data.agent_names.get(caller_phone, "")
        root_logger.info(f"👤 נציג נכנס ישירות לשלוחה 20: {caller_phone} ({agent_name or 'ללא שם'})")

        async with manager.lock:
            selected_call = get_waiting_call_for_agent(caller_phone)
            if selected_call:
                agent_dest = await bridge_call_for_agent(caller_phone, agent_name, selected_call)
                return Response(f"id_list_message=t-מעביר אותך לשיחה&go_to_folder={agent_dest}", media_type="text/plain; charset=utf-8")
            else:
                root_logger.info(f"⏳ נציג {caller_phone} ממתין בשלוחה 20 - אין שיחות ממתינות.")
                return Response(f"id_list_message=t-אין שיחות ממתינות כעת&go_to_folder=/{api_extension}", media_type="text/plain; charset=utf-8")

    # ==============================================================
    # 6. כניסת מאזינים לתור
    # ==============================================================
    sub_ext = api_extension
    if BASE_FOLDER and sub_ext.startswith(f"{BASE_FOLDER}/"):
        sub_ext = sub_ext[len(BASE_FOLDER) + 1:]

    # שלוחות הקלטה לא מנוהלות כתור נציגים!
    if sub_ext in ["4/2/1", "4/2/2"]:
        return Response("noop=yes", media_type="text/plain; charset=utf-8")

    if len(manager.waiting_calls) == 0:
        await sheet_data.reload_from_sheets()

    sub_ext_parts = sub_ext.split("/")
    possible_keys = [sub_ext]

    dept_id = None
    queue_type = None
    target_manager_phone = None
    transfer_msg = "מעביר אותך לשיחה עם נציג"
    is_anon = False

    preferred_dept = None
    if len(sub_ext_parts) >= 1:
        preferred_dept = DEPT_NAME_TO_ID.get(sub_ext_parts[0], sub_ext_parts[0])

    depts_to_search = []
    if preferred_dept and preferred_dept in sheet_data.dept_general_agents:
        depts_to_search.append(preferred_dept)
    for d in sorted(sheet_data.dept_general_agents.keys()):
        if d not in depts_to_search:
            depts_to_search.append(d)

    # 1. חיפוש קו אישי
    for d_id in depts_to_search:
        p_dict = sheet_data.personal_lines.get(d_id, {})
        for k in possible_keys:
            if k in p_dict:
                p_info = p_dict[k]
                dept_id = p_info.dept_id or d_id
                queue_type = "personal"
                target_manager_phone = p_info.phone
                if p_info.is_manager:
                    transfer_msg = "מעביר אותך לשיחה עם המנהל"
                elif p_info.name:
                    transfer_msg = f"מעביר אותך לשיחה עם {p_info.name}"
                break
        if queue_type:
            break

    # 2. חיפוש תור אנונימי
    if not queue_type:
        for d_id in depts_to_search:
            a_set = sheet_data.anonymous_queue_extensions.get(d_id, set())
            if any(k in a_set for k in possible_keys):
                dept_id = d_id
                queue_type = "general"
                transfer_msg = "מעביר אותך למענה אנונימי"
                is_anon = True
                break

    # 3. חיפוש תור כללי
    if not queue_type:
        for d_id in depts_to_search:
            g_set = sheet_data.general_queue_extensions.get(d_id, set())
            if any(k in g_set for k in possible_keys):
                dept_id = d_id
                queue_type = "general"
                transfer_msg = "מעביר אותך לשיחה עם נציג"
                break

    # 4. ברירת מחדל לפי תחילית השלוחה
    if not queue_type and preferred_dept and preferred_dept in sheet_data.dept_general_agents:
        dept_id = preferred_dept
        queue_type = "general"
        transfer_msg = "מעביר אותך לשיחה עם נציג"

    if not queue_type or not dept_id:
        root_logger.warning(f"⚠️ שלוחה [{api_extension}] אינה מוגדרת בשיטס באף מחלקה! (טלפון מאזין: {caller_phone})")
        return Response("id_list_message=t-שלוחה לא מוגדרת בשיטס&go_to_folder=..", media_type="text/plain; charset=utf-8")

    customer_name = sheet_data.customer_contacts.get(caller_phone, "")

    # כניסה ראשונית של המאזין לתור
    async with manager.lock:
        # ניקוי שיחה קודמת של אותו מאזין בחיוג חוזר
        same_phone_ghosts = [cid for cid, c in manager.waiting_calls.items() if c.phone == caller_phone and cid != call_id]
        for old_cid in same_phone_ghosts:
            old_call = manager.waiting_calls.pop(old_cid, None)
            manager.waiting_events.pop(old_cid, None)
            manager.waiting_destinations.pop(old_cid, None)
            if old_call:
                root_logger.info(f"🔄 הוסרה שיחה קודמת של מאזין {caller_phone} ({old_cid}) עקב חיוג מחדש.")

        if call_id not in manager.waiting_calls:
            q_call = QueuedCall(
                call_id=call_id,
                phone=caller_phone,
                caller_name=customer_name,
                entry_time=time.time(),
                dept_id=dept_id,
                queue_type=queue_type,
                target_manager_phone=target_manager_phone,
                sub_ext=sub_ext,
                transfer_message=transfer_msg,
                is_anonymous=is_anon
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
                root_logger.info(f"⚡ מפעיל סבב צינתוקים עבור {t_key}")
                manager.tzintuk_tasks[t_key] = asyncio.create_task(run_tzintuk_loop(t_key, phone_func))

            display_caller = f"{customer_name} ({caller_phone})" if customer_name else caller_phone
            root_logger.info(f"⏳ מאזין {display_caller} נכנס לתור מחלקה {dept_id} דרך שלוחה {api_extension} (מיקום בתור: {pos})")
            return Response(f"id_list_message=t-מיקומך בתור הוא {pos}&go_to_folder=/{api_extension}", media_type="text/plain; charset=utf-8")

    # המתנה רציפה
    event = asyncio.Event()
    manager.waiting_events[call_id] = event

    try:
        await asyncio.wait_for(event.wait(), timeout=float(WAIT_TIMEOUT))
        fallback_dest = f"/{ROOMS_ROOT_FOLDER}/{dept_id}/1/1"
        destination = manager.waiting_destinations.get(call_id, fallback_dest)
        display_caller = f"{customer_name} ({caller_phone})" if customer_name else caller_phone
        root_logger.info(f"✅ מאזין {display_caller} שוחרר מההמתנה ועובר ל-{destination} ({transfer_msg})")
        return Response(f"id_list_message=t-{transfer_msg}&go_to_folder={destination}", media_type="text/plain; charset=utf-8")
    except asyncio.TimeoutError:
        async with manager.lock:
            if queue_type == "personal":
                pos = sum(1 for c in manager.waiting_calls.values() if c.target_manager_phone == target_manager_phone and c.entry_time <= manager.waiting_calls[call_id].entry_time)
            else:
                pos = sum(1 for c in manager.waiting_calls.values() if c.dept_id == dept_id and c.queue_type == "general" and c.entry_time <= manager.waiting_calls[call_id].entry_time)
        display_caller = f"{customer_name} ({caller_phone})" if customer_name else caller_phone
        root_logger.info(f"⏳ מאזין {display_caller} ממשיך להמתין בתור {dept_id} (מיקום נוכחי: {pos})")
        return Response(f"id_list_message=t-מיקומך בתור הוא {pos}&go_to_folder=/{api_extension}", media_type="text/plain; charset=utf-8")
    finally:
        manager.waiting_events.pop(call_id, None)
        manager.waiting_destinations.pop(call_id, None)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)
