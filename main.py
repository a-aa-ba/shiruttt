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
import json

# ==================== הגדרות מערכת כלליות ====================
# טוקן הקו העכשווי והמרכזי (קו ההמתנה והתורים):
YEMOT_API_TOKEN = "0775311511:124578"

# הגדרות הקו השני (המערכת האחרת: שלוחות השיחות, הצינתוקים ומענה הנציגים):
SECONDARY_YEMOT_API_TOKEN = "0774360613:123456"
SECONDARY_SYSTEM_NUMBER = "0774360613"

# תיקיית הבסיס - שלוחה ראשית (ריק)
BASE_FOLDER = ""

# שם התיקייה בשלוחה הראשית שבה נמצאים החדרים:
ROOMS_ROOT_FOLDER = "rooms"

# נתיבי קבצי מערכת בימות המשיח:
YEMOT_LOG_FILE_PATH = "ivr2:30/Log.ini"
SECONDARY_CONF_LOG_FILE_PATH = "ivr2:Log.ini"
YEMOT_TRACKER_FILE_PATH = "ivr2:30/messages_tracker.ini"
SECONDARY_TZINTUK_REG_FILE_PATH = "ivr2:9/registered_agents.ini"
YEMOT_STATS_FILE_PATH = "ivr2:stats_data.json"

# כתובת Web App של Google Apps Script לעדכון עמודות הלוג בגיליון השיטס של הנציגים (פועל ברקע כשהמערכת פנויה)
GOOGLE_SHEETS_LOG_WEBHOOK_URL = os.environ.get("GOOGLE_SHEETS_LOG_WEBHOOK_URL", "") or os.environ.get("GOOGLE_STATS_WEBHOOK_URL", "")
STATS_SYNC_INTERVAL = 300

# ==== הגדרת שלוחות ההודעות למחלקות (לסריקת הודעות חדשות) ====
DEPT_MESSAGES_EXTENSIONS = {
    "1": "1/0/2",  
    "2": "2/0/2",  
    "3": "3/0/2",
    "4": "4/0/2",
}

# קישור ה-Google Sheets שלך
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1JfWZ0mUyqHznX-sOAiz4j71VRoVM_nx9Jf6ux962Dg4/edit?usp=sharing"

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


def get_secondary_system_number() -> str:
    explicit = normalize_phone(SECONDARY_SYSTEM_NUMBER)
    if explicit:
        return explicit
    if SECONDARY_YEMOT_API_TOKEN and ":" in SECONDARY_YEMOT_API_TOKEN:
        return normalize_phone(SECONDARY_YEMOT_API_TOKEN.split(":")[0])
    return ""


def is_secondary_configured() -> bool:
    return bool(
        SECONDARY_YEMOT_API_TOKEN
        and SECONDARY_YEMOT_API_TOKEN != "your_secondary_token_here"
        and get_secondary_system_number()
    )


def get_secondary_token() -> str:
    if SECONDARY_YEMOT_API_TOKEN and SECONDARY_YEMOT_API_TOKEN != "your_secondary_token_here":
        return SECONDARY_YEMOT_API_TOKEN
    return YEMOT_API_TOKEN


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
        self.dept_general_agents: Dict[str, List[str]] = {}
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

        headers_http = {
            "Cache-Control": "no-cache",
            "Pragma": "no-cache"
        }

        async with httpx.AsyncClient(headers=headers_http) as client:
            sheet1_content = None

            for dept_id, tab_name in DEPARTMENT_TABS_MAPPING.items():
                tab_encoded = urllib.parse.quote(tab_name)
                cache_buster = int(time.time() * 1000)
                url = f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}/gviz/tq?tqx=out:csv&sheet={tab_encoded}&t={cache_buster}"
                
                try:
                    res = await client.get(url, timeout=10.0, follow_redirects=True)
                    if res.status_code != 200:
                        root_logger.warning(f"⚠️ מחלקה {dept_id}: קריאת לשונית '{tab_name}' נכשלה (קוד HTTP {res.status_code})")
                        continue

                    content = res.content.decode('utf-8-sig').strip()
                    if not content or content.startswith("/*O_o*/"):
                        root_logger.warning(f"⚠️ מחלקה {dept_id}: לשונית '{tab_name}' אינה קיימת בגיליון!")
                        continue

                    # זיהוי הגנה מפני זיופי גוגל שיטס
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
                        new_dept_general_agents[dept_id] = []
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
                        if not raw_sub and len(r) >= 1:
                            raw_sub = r[0].strip().strip("/")

                        name = row_dict.get("שם", "").strip()
                        if not name and len(r) >= 2:
                            name = r[1].strip()

                        phone = normalize_phone(row_dict.get("טלפון", ""))
                        if not phone and len(r) >= 3:
                            phone = normalize_phone(r[2])

                        if not raw_sub or not phone:
                            continue
                        
                        # שלוחות הקלטה לא נרשמות כתור
                        if raw_sub in ["4/2/1", "4/2/2"]:
                            continue

                        custom_msg_ext = row_dict.get("שלוחת הודעות", "").strip("/")
                        if custom_msg_ext:
                            new_dept_messages_ext[dept_id] = custom_msg_ext

                        # 🎯 זיהוי קפדני ובלעדי מתוך עמודת "תפקיד" (העמודה הרביעית):
                        role_val = row_dict.get("תפקיד", "").strip()
                        if not role_val and len(r) >= 4:
                            role_val = r[3].strip()

                        is_personal = "אישי" in role_val
                        is_anonymous = "אנונימי" in role_val
                        is_manager_role = "מנהל" in role_val or "מנהל" in name

                        if name:
                            new_agent_names[phone] = name

                        # יצירת מפתחות חיפוש עבור השלוחה
                        keys_for_row = {raw_sub}
                        if "/" in raw_sub:
                            keys_for_row.add(raw_sub.split("/")[-1])
                        else:
                            keys_for_row.add(f"{dept_id}/{raw_sub}")

                        if is_personal:
                            for k in keys_for_row:
                                new_personal_lines[dept_id][k] = ManagerLineInfo(
                                    name=name, 
                                    phone=phone, 
                                    dept_id=dept_id,
                                    is_manager=is_manager_role
                                )
                            root_logger.info(f"   👤 קו אישי זוהה במחלקה {dept_id}: שלוחה {raw_sub} -> {name} ({phone})")
                        elif is_anonymous:
                            if phone not in new_dept_general_agents[dept_id]:
                                new_dept_general_agents[dept_id].append(phone)
                            for k in keys_for_row:
                                new_anonymous_queues[dept_id].add(k)
                            if phone not in new_agent_to_depts:
                                new_agent_to_depts[phone] = set()
                            new_agent_to_depts[phone].add(dept_id)
                        else:
                            # נציג כללי
                            if phone not in new_dept_general_agents[dept_id]:
                                new_dept_general_agents[dept_id].append(phone)
                            for k in keys_for_row:
                                new_general_queues[dept_id].add(k)
                            if phone not in new_agent_to_depts:
                                new_agent_to_depts[phone] = set()
                            new_agent_to_depts[phone].add(dept_id)

                except Exception as e:
                    root_logger.warning(f"⚠️ שגיאה בקריאת טאב מחלקה {tab_name}: {e}")

            # טעינת לשונית לקוחות
            c_tab_encoded = urllib.parse.quote(CONTACTS_TAB_NAME)
            url = f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}/gviz/tq?tqx=out:csv&sheet={c_tab_encoded}&t={int(time.time() * 1000)}"
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

        # ספירת קווים אישיים מדויקת (לפי כמות נציגים/שלוחות מקוריות ולא לפי כמות המפתחות)
        total_p = sum(len(set(info.phone for info in lines.values())) for lines in self.personal_lines.values())
        total_gen = sum(len(v) for v in self.dept_general_agents.values())
        root_logger.info(f"✅ סנכרון הושלם: {total_p} קווים אישיים, {total_gen} נציגים כלליים, {len(self.customer_contacts)} לקוחות.")
        
        for d_id in ["1", "2", "3", "4"]:
            agents = list(self.dept_general_agents.get(d_id, []))
            p_lines = len(set(info.phone for info in self.personal_lines.get(d_id, {}).values()))
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
        self.pending_secondary_transfers: Dict[str, Tuple[str, float, str]] = {}
        self.pending_secondary_anon_transfers: List[Tuple[str, float, str]] = []
        self.secondary_transferred_calls: Dict[str, str] = {}
        self.agent_call_rooms: Dict[str, Tuple[str, int]] = {}
        self.secondary_listener_call_rooms: Dict[str, Tuple[str, int]] = {}
        self.occupied_room_times: Dict[Tuple[str, int], float] = {}
        self.busy_agent_times: Dict[str, float] = {}
        self.registered_tzintuk_agents: Set[str] = set()
        self.tzintuk_reg_loaded: bool = False
        self.pending_tzintuk_reg_calls: Dict[str, str] = {}
        self.primary_known_dids: Set[str] = set()
        self.secondary_known_dids: Set[str] = set()
        self.lock = asyncio.Lock()

    def has_waiting_for_target(self, target_key: str) -> bool:
        for call in self.waiting_calls.values():
            if target_key.startswith("personal_") and normalize_phone(call.target_manager_phone or "") == target_key.replace("personal_", ""):
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


class StatsManager:
    def __init__(self):
        self.total_calls: int = 0
        self.answered_calls: int = 0
        self.missed_calls: int = 0
        self.calls_by_dept: Dict[str, int] = {}
        self.answered_by_dept: Dict[str, int] = {}
        self.personal_calls_by_agent: Dict[str, int] = {}
        self.relevant_calls_by_agent: Dict[str, int] = {}
        self.answered_by_agent: Dict[str, int] = {}
        self.dirty: bool = False
        self.loaded: bool = False
        self.last_sync_ts: float = 0.0
        self.last_updated_str: str = ""

    def record_incoming(self, dept_id: str, queue_type: str, target_manager_phone: Optional[str]):
        self.total_calls += 1
        self.calls_by_dept[dept_id] = self.calls_by_dept.get(dept_id, 0) + 1
        if queue_type == "personal" and target_manager_phone:
            clean_p = normalize_phone(target_manager_phone)
            if clean_p:
                self.personal_calls_by_agent[clean_p] = self.personal_calls_by_agent.get(clean_p, 0) + 1
                self.relevant_calls_by_agent[clean_p] = self.relevant_calls_by_agent.get(clean_p, 0) + 1
        else:
            for ag in sheet_data.dept_general_agents.get(dept_id, []):
                clean_ag = normalize_phone(ag)
                if clean_ag:
                    self.relevant_calls_by_agent[clean_ag] = self.relevant_calls_by_agent.get(clean_ag, 0) + 1
        self.dirty = True

    def record_answered(self, dept_id: str, agent_phone: str):
        self.answered_calls += 1
        self.answered_by_dept[dept_id] = self.answered_by_dept.get(dept_id, 0) + 1
        clean_ag = normalize_phone(agent_phone)
        if clean_ag:
            self.answered_by_agent[clean_ag] = self.answered_by_agent.get(clean_ag, 0) + 1
        self.dirty = True

    def record_missed(self, dept_id: str):
        self.missed_calls += 1
        self.dirty = True

    def to_dict(self) -> dict:
        return {
            "total_calls": self.total_calls,
            "answered_calls": self.answered_calls,
            "missed_calls": self.missed_calls,
            "calls_by_dept": self.calls_by_dept,
            "answered_by_dept": self.answered_by_dept,
            "personal_calls_by_agent": self.personal_calls_by_agent,
            "relevant_calls_by_agent": self.relevant_calls_by_agent,
            "answered_by_agent": self.answered_by_agent,
            "last_updated": self.last_updated_str,
        }

    def merge_loaded(self, saved: dict):
        if not isinstance(saved, dict):
            return
        self.total_calls += int(saved.get("total_calls", 0) or 0)
        self.answered_calls += int(saved.get("answered_calls", 0) or 0)
        self.missed_calls += int(saved.get("missed_calls", 0) or 0)
        for target_dict, key in (
            (self.calls_by_dept, "calls_by_dept"),
            (self.answered_by_dept, "answered_by_dept"),
            (self.personal_calls_by_agent, "personal_calls_by_agent"),
            (self.relevant_calls_by_agent, "relevant_calls_by_agent"),
            (self.answered_by_agent, "answered_by_agent"),
        ):
            src = saved.get(key, {})
            if isinstance(src, dict):
                for k, v in src.items():
                    target_dict[k] = target_dict.get(k, 0) + int(v or 0)
        if not self.last_updated_str:
            self.last_updated_str = str(saved.get("last_updated", ""))

    async def load_from_yemot(self):
        if self.loaded:
            return
        try:
            res = await yemot_api_request("GetTextFile", params={"what": YEMOT_STATS_FILE_PATH}, timeout=4.0, token=YEMOT_API_TOKEN)
            if res is not None:
                if res.get("responseStatus") == "OK":
                    raw = res.get("contents", "").strip()
                    if raw:
                        self.merge_loaded(json.loads(raw))
                self.loaded = True
        except Exception:
            pass

    async def save_to_yemot(self):
        try:
            self.last_updated_str = get_israel_now().strftime("%d/%m/%Y %H:%M:%S")
            await yemot_api_request(
                "UploadTextFile",
                method="POST",
                data={
                    "what": YEMOT_STATS_FILE_PATH,
                    "contents": json.dumps(self.to_dict(), ensure_ascii=False)
                },
                timeout=5.0,
                token=YEMOT_API_TOKEN
            )
        except Exception:
            pass

    def build_csv_and_summary(self) -> Tuple[dict, str]:
        now_str = get_israel_now().strftime("%d/%m/%Y %H:%M:%S")
        self.last_updated_str = now_str
        dept_titles = {"1": "חדר ביגוד", "2": 'חדר צצ"א', "3": "הסכמי חנויות", "4": "עזרה לנזקקים"}

        dept_rows = []
        for d_id in ["1", "2", "3", "4"]:
            gen_agents = list(sheet_data.dept_general_agents.get(d_id, []))
            pers_agents = []
            for info in sheet_data.personal_lines.get(d_id, {}).values():
                p = normalize_phone(info.phone)
                if p and p not in pers_agents:
                    pers_agents.append(p)
            all_dept_agents = []
            for p in gen_agents + pers_agents:
                if p not in all_dept_agents:
                    all_dept_agents.append(p)
            reg_count = sum(1 for p in all_dept_agents if p in manager.registered_tzintuk_agents)
            dept_rows.append({
                "dept_id": d_id,
                "dept_name": dept_titles.get(d_id, f"מחלקה {d_id}"),
                "tab_name": DEPARTMENT_TABS_MAPPING.get(d_id, ""),
                "calls": self.calls_by_dept.get(d_id, 0),
                "answered": self.answered_by_dept.get(d_id, 0),
                "agents_count": len(all_dept_agents),
                "tzintuk_registered_count": reg_count,
            })

        all_agents = []
        seen_ag = set()
        for ag_map in (sheet_data.agent_names, self.answered_by_agent, self.relevant_calls_by_agent, self.personal_calls_by_agent):
            for p in ag_map.keys():
                cp = normalize_phone(p)
                if cp and cp not in seen_ag:
                    seen_ag.add(cp)
                    all_agents.append(cp)

        agent_rows = []
        for p in all_agents:
            depts = set(sheet_data.agent_to_depts.get(p, set()))
            for d_id, p_dict in sheet_data.personal_lines.items():
                if any(normalize_phone(info.phone) == p for info in p_dict.values()):
                    depts.add(d_id)
            dept_labels = ", ".join(dept_titles.get(d, d) for d in sorted(depts)) or "כללי"
            agent_rows.append({
                "name": sheet_data.agent_names.get(p, "ללא שם"),
                "phone": p,
                "depts": dept_labels,
                "dept_ids": sorted(depts),
                "tab_names": [DEPARTMENT_TABS_MAPPING[d] for d in sorted(depts) if d in DEPARTMENT_TABS_MAPPING],
                "personal_calls": self.personal_calls_by_agent.get(p, 0),
                "total_relevant_calls": self.relevant_calls_by_agent.get(p, 0),
                "answered_calls": self.answered_by_agent.get(p, 0),
                "tzintuk_registered": "כן" if p in manager.registered_tzintuk_agents else "לא",
            })

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["קטגוריה", "שם / פירוט", "שלוחה / טלפון", "כמות שיחות / קו אישי", "שיחות שנענו / משויכות", "נציגים / נענו ע\"י נציג", "רשום לצינתוקים"])
        writer.writerow(["סיכום כללי", "עדכון אחרון", now_str, "", "", "", ""])
        writer.writerow(["סיכום כללי", "סה\"כ שיחות נכנסות", "", self.total_calls, "", "", ""])
        writer.writerow(["סיכום כללי", "שיחות שנענו", "", self.answered_calls, "", "", ""])
        writer.writerow(["סיכום כללי", "שיחות שנותקו ללא מענה", "", self.missed_calls, "", "", ""])
        writer.writerow(["סיכום כללי", "שיחות ממתינות כעת", "", len(manager.waiting_calls), "", "", ""])
        writer.writerow(["סיכום כללי", "סה\"כ נציגים רשומים לצינתוקים", "", "", "", "", len(manager.registered_tzintuk_agents)])

        for row in dept_rows:
            writer.writerow(["שלוחה / מחלקה", row["dept_name"], f"שלוחה {row['dept_id']}", row["calls"], row["answered"], row["agents_count"], row["tzintuk_registered_count"]])

        for row in agent_rows:
            writer.writerow(["נציג", row["name"], f"{row['phone']} ({row['depts']})", row["personal_calls"], row["total_relevant_calls"], row["answered_calls"], row["tzintuk_registered"]])

        summary = {
            "last_updated": now_str,
            "total_calls": self.total_calls,
            "answered_calls": self.answered_calls,
            "missed_calls": self.missed_calls,
            "waiting_now": len(manager.waiting_calls),
            "tzintuk_registered_total": len(manager.registered_tzintuk_agents),
            "departments": dept_rows,
            "agents": agent_rows,
        }
        return summary, "\ufeff" + output.getvalue()


stats_manager = StatsManager()


# ==================== תקשורת API מול ימות המשיח ====================
async def yemot_api_request(endpoint: str, params: dict = None, method: str = "GET", data: dict = None, timeout: float = 3.0, token: str = None):
    active_token = token or YEMOT_API_TOKEN
    if not active_token or active_token in ("your_actual_token_here", "your_secondary_token_here"):
        root_logger.error("❌ שגיאה: טוקן ה-API לא מוגדר בקוד!")
        return None

    url = f"https://private.call2all.co.il/ym/api/{endpoint}"
    p = {"token": active_token}
    if params:
        p.update(params)

    try:
        async with httpx.AsyncClient() as client:
            if method == "POST":
                post_data = {"token": active_token}
                if data:
                    post_data.update(data)
                res = await client.post(url, data=post_data, timeout=timeout)
            else:
                res = await client.get(url, params=p, timeout=timeout)
            return res.json()
    except Exception as e:
        root_logger.error(f"❌ שגיאת תקשורת API מול ימות המשיח ({endpoint}): {e}")
        return None


def get_all_tokens() -> List[str]:
    tokens = []
    if YEMOT_API_TOKEN and YEMOT_API_TOKEN != "your_actual_token_here":
        tokens.append(YEMOT_API_TOKEN)
    if (
        SECONDARY_YEMOT_API_TOKEN
        and SECONDARY_YEMOT_API_TOKEN != "your_secondary_token_here"
        and SECONDARY_YEMOT_API_TOKEN not in tokens
    ):
        tokens.append(SECONDARY_YEMOT_API_TOKEN)
    return tokens


async def _append_log_to_single_token(tok: str, log_path: str, caller_phone: str, customer_name: str, agent_phone: str, agent_name: str, date_str: str, time_str: str):
    res_get = await yemot_api_request("GetTextFile", params={"what": log_path}, timeout=4.0, token=tok)
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
            "what": log_path,
            "contents": updated_content
        },
        timeout=5.0,
        token=tok
    )
    if res_upload and res_upload.get("responseStatus") == "OK":
        root_logger.info(f"📝 קובץ הלוג ({log_path}) עודכן בהצלחה: {new_entry}")


async def append_to_yemot_log(caller_phone: str, customer_name: str, agent_phone: str, agent_name: str, date_str: str, time_str: str, is_anonymous: bool):
    try:
        if is_secondary_configured():
            await _append_log_to_single_token(
                get_secondary_token(),
                SECONDARY_CONF_LOG_FILE_PATH,
                caller_phone, customer_name, agent_phone, agent_name, date_str, time_str
            )
        else:
            await _append_log_to_single_token(
                YEMOT_API_TOKEN,
                YEMOT_LOG_FILE_PATH,
                caller_phone, customer_name, agent_phone, agent_name, date_str, time_str
            )
    except Exception as e:
        root_logger.error(f"❌ שגיאה ברישום לוג בימות המשיח: {e}")


async def get_agent_last_heard_file(agent_phone: str) -> str:
    try:
        res = await yemot_api_request("GetTextFile", params={"what": YEMOT_TRACKER_FILE_PATH}, timeout=3.0, token=YEMOT_API_TOKEN)
        if res and res.get("responseStatus") == "OK":
            for line in res.get("contents", "").splitlines():
                if line.startswith(f"{agent_phone}="):
                    return line.split("=")[-1].strip()
    except Exception:
        pass
    return ""


async def save_agent_last_heard_file(agent_phone: str, latest_file: str):
    try:
        res = await yemot_api_request("GetTextFile", params={"what": YEMOT_TRACKER_FILE_PATH}, timeout=3.0, token=YEMOT_API_TOKEN)
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
            timeout=4.0,
            token=YEMOT_API_TOKEN
        )
        root_logger.info(f"📌 עודכן קובץ מעקב לנציג {agent_phone} במערכת הישנה: קובץ אחרון {latest_file}")
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

    res = await yemot_api_request("GetIVR2Dir", params={"path": f"ivr2:{msg_ext_full}"}, timeout=3.0, token=YEMOT_API_TOKEN)
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


async def get_active_calls(token: str = None):
    data = await yemot_api_request("GetIncomingCalls", timeout=1.5, token=token or get_secondary_token())
    if data and data.get("responseStatus") == "OK":
        return data.get("calls", [])
    return None


async def get_active_calls_both_systems():
    tokens = get_all_tokens()
    if not tokens:
        return None
    results = await asyncio.gather(*[get_active_calls(token=tok) for tok in tokens], return_exceptions=True)
    combined = []
    any_ok = False
    for r in results:
        if isinstance(r, list):
            any_ok = True
            combined.extend(r)
    return combined if any_ok else None


async def is_agent_connected(phone: str) -> bool:
    clean_target = normalize_phone(phone)
    active = await get_active_calls_both_systems()
    if active:
        for c in active:
            caller = normalize_phone(c.get("callerId") or c.get("callerIdNum") or "")
            if caller == clean_target:
                return True
    if clean_target in manager.busy_agents:
        if active is not None and (time.time() - manager.busy_agent_times.get(clean_target, 0)) > 25:
            manager.busy_agents.discard(clean_target)
        else:
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
    }, timeout=3.0, token=get_secondary_token())

    if result and result.get("responseStatus") == "OK":
        root_logger.info(f"✅ צינתוק נשלח בהצלחה לנציג: {clean_phone}{name_str}")
    else:
        root_logger.error(f"❌ שגיאה בשליחת צינתוק לנציג {clean_phone}: תשובת המערכת הייתה {result}")


async def load_tzintuk_registered_agents():
    if manager.tzintuk_reg_loaded:
        return
    res = await yemot_api_request(
        "GetTextFile",
        params={"what": SECONDARY_TZINTUK_REG_FILE_PATH},
        timeout=3.0,
        token=get_secondary_token()
    )
    if res is not None:
        if res.get("responseStatus") == "OK":
            for line in res.get("contents", "").splitlines():
                p = normalize_phone(line.strip().split("=")[0])
                if p:
                    manager.registered_tzintuk_agents.add(p)
        manager.tzintuk_reg_loaded = True


async def is_agent_tzintuk_registered(agent_phone: str) -> bool:
    clean_phone = normalize_phone(agent_phone)
    if not clean_phone:
        return True
    await load_tzintuk_registered_agents()
    return clean_phone in manager.registered_tzintuk_agents


async def mark_agent_tzintuk_registered(agent_phone: str):
    clean_phone = normalize_phone(agent_phone)
    if not clean_phone:
        return
    manager.registered_tzintuk_agents.add(clean_phone)
    manager.tzintuk_reg_loaded = True
    try:
        contents = "\n".join(sorted(manager.registered_tzintuk_agents)) + "\n"
        await yemot_api_request(
            "UploadTextFile",
            method="POST",
            data={
                "what": SECONDARY_TZINTUK_REG_FILE_PATH,
                "contents": contents
            },
            timeout=4.0,
            token=get_secondary_token()
        )
        root_logger.info(f"📌 נציג {clean_phone} נרשם לראשונה במערכת הצינתוקים ונשמר ברשימה.")
    except Exception as e:
        root_logger.error(f"❌ שגיאה בשמירת רישום צינתוק לנציג {clean_phone}: {e}")


async def prepare_agent_tzintuk_folder(agent_phone: str, end_goto: str = "/10") -> str:
    clean_phone = normalize_phone(agent_phone)
    tz_folder = build_path(f"9/{clean_phone}")
    ext_content = (
        "type=tzintuk\n"
        f"list_tzintuk={clean_phone}\n"
        f"tzintuk_end={end_goto}\n"
    )
    await yemot_api_request(
        "UploadTextFile",
        method="POST",
        data={
            "what": f"ivr2:{tz_folder}/ext.ini",
            "contents": ext_content
        },
        timeout=4.0,
        token=get_secondary_token()
    )
    return f"/{tz_folder}"


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
    active_calls = await get_active_calls_both_systems()
    active_paths = [str(c.get("path", "")) for c in (active_calls or [])]
    folders_to_check = [str(dept_id), "5"]
    now = time.time()

    for folder in folders_to_check:
        rooms_base = f"{ROOMS_ROOT_FOLDER}/{folder}"
        occupied_in_mem = manager.occupied_rooms.get(folder, set())
        for r in range(1, 6):
            p1 = f"{rooms_base}/{r}/1"
            p2 = f"{rooms_base}/{r}/2"
            in_active = any(p1 in p or p2 in p for p in active_paths)
            if r in occupied_in_mem and active_calls is not None and not in_active:
                if (now - manager.occupied_room_times.get((folder, r), 0)) > 25:
                    occupied_in_mem.discard(r)
            if not in_active and r not in occupied_in_mem:
                return folder, r

    return str(dept_id), 1


def get_waiting_call_for_agent(caller_phone: str) -> Optional[QueuedCall]:
    clean_agent = normalize_phone(caller_phone)
    
    # 1. עדיפות עליונה: שיחה בקו אישי לנציג זה
    personal_calls = [
        c for c in manager.waiting_calls.values()
        if c.queue_type == "personal" and normalize_phone(c.target_manager_phone or "") == clean_agent
    ]
    if personal_calls:
        personal_calls.sort(key=lambda x: x.entry_time)
        return personal_calls[0]

    # 2. עדיפות שנייה: שיחה כללית במחלקות המורשות של הנציג
    allowed_depts = sheet_data.agent_to_depts.get(clean_agent, set())
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


async def bridge_call_for_agent(caller_phone: str, agent_name: str, selected_call: QueuedCall, agent_call_id: str = None) -> str:
    chosen_folder, free_room = await find_free_room(selected_call.dept_id)

    agent_dest = f"/{ROOMS_ROOT_FOLDER}/{chosen_folder}/{free_room}/2"
    listener_dest = f"/{ROOMS_ROOT_FOLDER}/{chosen_folder}/{free_room}/1"

    manager.waiting_calls.pop(selected_call.call_id, None)
    manager.waiting_destinations[selected_call.call_id] = listener_dest
    manager.transferred_calls[selected_call.call_id] = listener_dest

    manager.busy_agents.add(caller_phone)
    manager.busy_agent_times[caller_phone] = time.time()
    if chosen_folder not in manager.occupied_rooms:
        manager.occupied_rooms[chosen_folder] = set()
    manager.occupied_rooms[chosen_folder].add(free_room)
    manager.occupied_room_times[(chosen_folder, free_room)] = time.time()
    if agent_call_id:
        manager.agent_call_rooms[agent_call_id] = (chosen_folder, free_room)

    if is_secondary_configured():
        if selected_call.phone:
            manager.pending_secondary_transfers[selected_call.phone] = (listener_dest, time.time(), selected_call.call_id)
        else:
            manager.pending_secondary_anon_transfers.append((listener_dest, time.time(), selected_call.call_id))

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
    stats_manager.record_answered(selected_call.dept_id, caller_phone)

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

    t_key = f"personal_{normalize_phone(selected_call.target_manager_phone or '')}" if selected_call.queue_type == "personal" else f"general_{selected_call.dept_id}"
    manager.cancel_tzintuk_if_empty(t_key)

    c_display = f"{customer_name} ({selected_call.phone})" if customer_name else selected_call.phone
    root_logger.info(f"🎉 שידוך הצליח! חדר {free_room} (תיקייה {chosen_folder}): נציג {caller_phone} ({agent_name}) <--> מאזין {c_display}")
    return agent_dest


async def background_cleanup_loop():
    while True:
        try:
            now = time.time()
            if manager.waiting_calls:
                async with manager.lock:
                    to_remove = [
                        call for cid, call in manager.waiting_calls.items()
                        if (now - manager.last_seen.get(cid, 0)) >= GRACE_PERIOD
                    ]
                    for call in to_remove:
                        cid = call.call_id
                        root_logger.info(f"⚠️ שיחה {cid} ({call.phone}) נותקה ללא דיווח והוסרה מהתור (חלף Grace Period).")
                        manager.waiting_calls.pop(cid, None)
                        manager.waiting_events.pop(cid, None)
                        manager.waiting_destinations.pop(cid, None)
                        manager.last_seen.pop(cid, None)
                        t_key = f"personal_{normalize_phone(call.target_manager_phone or '')}" if call.queue_type == "personal" else f"general_{call.dept_id}"
                        manager.cancel_tzintuk_if_empty(t_key)
                        stats_manager.record_missed(call.dept_id)

            if manager.pending_secondary_transfers or manager.pending_secondary_anon_transfers:
                async with manager.lock:
                    stale_phones = [
                        p for p, (_, ts, _) in manager.pending_secondary_transfers.items()
                        if (now - ts) >= 120
                    ]
                    for p in stale_phones:
                        manager.pending_secondary_transfers.pop(p, None)
                    manager.pending_secondary_anon_transfers = [
                        item for item in manager.pending_secondary_anon_transfers
                        if (now - item[1]) < 120
                    ]

            if manager.occupied_rooms or manager.busy_agents:
                active_calls = await get_active_calls_both_systems()
                if active_calls is not None:
                    active_paths = [str(c.get("path", "")) for c in active_calls]
                    active_callers = {normalize_phone(c.get("callerId") or c.get("callerIdNum") or "") for c in active_calls}
                    async with manager.lock:
                        for folder, rooms_set in manager.occupied_rooms.items():
                            rooms_base = f"{ROOMS_ROOT_FOLDER}/{folder}"
                            for r in list(rooms_set):
                                p1 = f"{rooms_base}/{r}/1"
                                p2 = f"{rooms_base}/{r}/2"
                                if not any(p1 in p or p2 in p for p in active_paths):
                                    if (now - manager.occupied_room_times.get((folder, r), 0)) > 25:
                                        rooms_set.discard(r)
                        for ag in list(manager.busy_agents):
                            if ag not in active_callers and (now - manager.busy_agent_times.get(ag, 0)) > 25:
                                manager.busy_agents.discard(ag)
        except Exception as e:
            root_logger.error(f"❌ שגיאה בלולאת רקע: {e}")
        await asyncio.sleep(15)


async def load_system_dids():
    if YEMOT_API_TOKEN and ":" in YEMOT_API_TOKEN:
        p0 = normalize_phone(YEMOT_API_TOKEN.split(":")[0])
        if p0:
            manager.primary_known_dids.add(p0)
    res_pri = await yemot_api_request("GetCustomerData", timeout=4.0, token=YEMOT_API_TOKEN)
    if res_pri and res_pri.get("responseStatus") == "OK":
        for d in [res_pri.get("mainDid")] + list(res_pri.get("secondary_dids") or []):
            nd = normalize_phone(d)
            if nd:
                manager.primary_known_dids.add(nd)

    if is_secondary_configured():
        sec_num = get_secondary_system_number()
        if sec_num:
            manager.secondary_known_dids.add(sec_num)
        if ":" in SECONDARY_YEMOT_API_TOKEN:
            s0 = normalize_phone(SECONDARY_YEMOT_API_TOKEN.split(":")[0])
            if s0:
                manager.secondary_known_dids.add(s0)
        res_sec = await yemot_api_request("GetCustomerData", timeout=4.0, token=get_secondary_token())
        if res_sec and res_sec.get("responseStatus") == "OK":
            for d in [res_sec.get("mainDid")] + list(res_sec.get("secondary_dids") or []):
                nd = normalize_phone(d)
                if nd:
                    manager.secondary_known_dids.add(nd)


async def push_stats_to_google_sheets(summary: dict, csv_content: str):
    if not GOOGLE_SHEETS_LOG_WEBHOOK_URL:
        return
    payload = {
        "summary": summary,
        "departments": summary.get("departments", []),
        "agents": summary.get("agents", []),
        "csv": csv_content,
    }
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        res = await client.post(GOOGLE_SHEETS_LOG_WEBHOOK_URL, json=payload)
        if res.status_code == 200:
            root_logger.info("📊 עמודות הלוג עודכנו בהצלחה בגיליון Google Sheets של הנציגים.")


async def background_stats_loop():
    await asyncio.sleep(5)
    await stats_manager.load_from_yemot()
    while True:
        try:
            await asyncio.sleep(60)
            # עדיפות מוחלטת לפעילות המערכת: אם יש שיחות ממתינות או צינתוקים פעילים - מדלגים וממתינים לזמן פנוי
            if manager.waiting_calls or manager.tzintuk_tasks:
                continue

            now = time.time()
            if not stats_manager.dirty and (now - stats_manager.last_sync_ts) < STATS_SYNC_INTERVAL:
                continue

            await load_tzintuk_registered_agents()

            summary, csv_content = stats_manager.build_csv_and_summary()
            await stats_manager.save_to_yemot()

            if GOOGLE_SHEETS_LOG_WEBHOOK_URL:
                await push_stats_to_google_sheets(summary, csv_content)

            stats_manager.dirty = False
            stats_manager.last_sync_ts = now
        except asyncio.CancelledError:
            break
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    root_logger.info("🚀 שרת התורים והנציגים מופעל כעת.")
    await load_system_dids()
    if is_secondary_configured():
        root_logger.info(f"🔗 מצב דו-מערכתי פעיל: קו המתנה ראשי -> ניתוב לקו שני ({get_secondary_system_number()})")
    await sheet_data.reload_from_sheets()
    task = asyncio.create_task(background_cleanup_loop())
    stats_task = asyncio.create_task(background_stats_loop())
    yield
    task.cancel()
    stats_task.cancel()
    root_logger.info("🛑 שרת התורים כובה.")

app = FastAPI(lifespan=lifespan)


@app.get("/test")
async def self_test_endpoint():
    return Response(content="<h2 style='color: #2e7d32;'>תקין</h2>", media_type="text/html; charset=utf-8")


@app.get("/stats")
async def stats_endpoint(format: str = "json"):
    await load_tzintuk_registered_agents()
    summary, csv_content = stats_manager.build_csv_and_summary()
    if format == "csv":
        return Response(content=csv_content, media_type="text/csv; charset=utf-8")
    return Response(content=json.dumps(summary, ensure_ascii=False, indent=2), media_type="application/json; charset=utf-8")


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
    api_did = normalize_phone(params.get("ApiDID") or params.get("did") or "")
    primary_dids = manager.primary_known_dids or (
        {normalize_phone(YEMOT_API_TOKEN.split(":")[0])} if ":" in YEMOT_API_TOKEN else set()
    )
    sec_token_phone = normalize_phone(SECONDARY_YEMOT_API_TOKEN.split(":")[0]) if ":" in SECONDARY_YEMOT_API_TOKEN else ""
    secondary_dids = set(manager.secondary_known_dids)
    for d in (get_secondary_system_number(), sec_token_phone):
        if d:
            secondary_dids.add(d)

    is_from_secondary = is_secondary_configured() and bool(
        api_did and (api_did in secondary_dids or (primary_dids and api_did not in primary_dids))
    )

    if not call_id and (caller_phone or api_extension):
        call_id = f"gen_{caller_phone or 'anon'}_{int(time.time() * 1000)}"

    if call_id:
        manager.last_seen[call_id] = time.time()

    sys_label = "משנית" if is_from_secondary else "ראשית"
    root_logger.info(f"📥 פנייה מהמערכת ({sys_label} | DID: {api_did or '-'}): שלוחה [{api_extension or 'ראשי'}] | טלפון: {caller_phone or 'אנונימי'} | CallId: {call_id}")

    # 1. ניתוק שיחה (כשמתקבל דיווח ניתוק מיידי מימות המשיח)
    if hangup == "yes" and call_id:
        async with manager.lock:
            removed_call = manager.waiting_calls.pop(call_id, None)
            manager.waiting_events.pop(call_id, None)
            manager.waiting_destinations.pop(call_id, None)
            manager.pending_tzintuk_reg_calls.pop(call_id, None)
            if caller_phone in manager.busy_agents:
                manager.busy_agents.discard(caller_phone)

            if is_secondary_configured():
                room_info = manager.agent_call_rooms.pop(call_id, None) or manager.secondary_listener_call_rooms.pop(call_id, None)
                if room_info:
                    f_id, r_id = room_info
                    if f_id in manager.occupied_rooms:
                        manager.occupied_rooms[f_id].discard(r_id)
            else:
                if call_id in manager.call_history:
                    f_id = manager.call_history[call_id].get("chosen_folder")
                    r_id = manager.call_history[call_id].get("room")
                    if f_id and r_id and f_id in manager.occupied_rooms:
                        manager.occupied_rooms[f_id].discard(r_id)

            if removed_call:
                display_name = removed_call.caller_name or removed_call.phone
                root_logger.info(f"📴 מאזין {display_name} ניתק את השיחה ויצא מהתור.")
                t_key = f"personal_{normalize_phone(removed_call.target_manager_phone or '')}" if removed_call.queue_type == "personal" else f"general_{removed_call.dept_id}"
                manager.cancel_tzintuk_if_empty(t_key)
                stats_manager.record_missed(removed_call.dept_id)

        return Response("noop=yes", media_type="text/plain; charset=utf-8")

    if not call_id:
        return Response("id_list_message=t-מזהה שיחה חסר&go_to_folder=..", media_type="text/plain; charset=utf-8")

    # 2. שידוך קיים - העברה מיידית לחדר / למערכת השנייה
    if call_id in manager.secondary_transferred_calls:
        sec_dest = manager.secondary_transferred_calls[call_id]
        root_logger.info(f"🔀 שיחה {call_id} במערכת השנייה כבר שודכה - מעביר ישירות ל-{sec_dest}")
        return Response(f"go_to_folder={sec_dest}", media_type="text/plain; charset=utf-8")

    if call_id in manager.waiting_destinations or call_id in manager.transferred_calls:
        destination = manager.waiting_destinations.get(call_id) or manager.transferred_calls.get(call_id)
        if call_id in manager.waiting_events:
            manager.waiting_events[call_id].set()
        if is_secondary_configured() and not is_from_secondary:
            sec_num = get_secondary_system_number()
            root_logger.info(f"🔀 שיחה {call_id} כבר שודכה - מעביר לקו השני ({sec_num})")
            return Response(f"id_list_message=t-מעביר אותך לשיחה&routing_yemot={sec_num}", media_type="text/plain; charset=utf-8")
        root_logger.info(f"🔀 שיחה {call_id} כבר שודכה - מעביר מיידית ל-{destination}")
        return Response(f"id_list_message=t-מעביר אותך לשיחה&go_to_folder={destination}", media_type="text/plain; charset=utf-8")

    # 2.5 הגעת מאזין או נציג למערכת השנייה (הקו השני)
    what_file_early = params.get("what") or params.get("file") or params.get("filename") or ""
    is_secondary_admin_ext = api_extension in (
        "9", build_path("9"),
        "30", "30/0", build_path("30"), build_path("30/0"),
        "4/0/1/1", "4/0/1/1/0", "4/0/1/2", "4/0/1/2/0"
    ) or api_extension.startswith(("9/", f"{build_path('9')}/")) or api_extension.endswith(("/30", "/30/0", "4/0/1/1", "4/0/1/1/0", "4/0/1/2", "4/0/1/2/0")) or bool(what_file_early)

    is_secondary_agent_ext = is_secondary_admin_ext or api_extension in (
        "10", build_path("10"),
        "20", build_path("20"),
    )

    if is_secondary_configured() and not is_secondary_admin_ext and (
        api_extension in ("", "10", build_path("10"), "20", build_path("20")) or is_from_secondary
    ):
        matched_dest = None
        async with manager.lock:
            if caller_phone and caller_phone in manager.pending_secondary_transfers:
                matched_dest, _, _ = manager.pending_secondary_transfers.pop(caller_phone)
            elif not caller_phone and manager.pending_secondary_anon_transfers:
                matched_dest, _, _ = manager.pending_secondary_anon_transfers.pop(0)

            if matched_dest:
                manager.secondary_transferred_calls[call_id] = matched_dest
                parts_dest = matched_dest.strip("/").split("/")
                if len(parts_dest) >= 3 and parts_dest[0] == ROOMS_ROOT_FOLDER:
                    try:
                        manager.secondary_listener_call_rooms[call_id] = (parts_dest[1], int(parts_dest[2]))
                    except Exception:
                        pass

        if matched_dest:
            root_logger.info(f"🔀 מאזין {caller_phone or 'אנונימי'} הגיע למערכת השנייה - מכניס ישירות לחדר {matched_dest}")
            return Response(f"go_to_folder={matched_dest}", media_type="text/plain; charset=utf-8")

        if not is_secondary_agent_ext:
            is_agent_sec = caller_phone in sheet_data.agent_names or caller_phone in sheet_data.agent_to_depts
            if not is_agent_sec:
                for dept_dict in sheet_data.personal_lines.values():
                    for info in dept_dict.values():
                        if normalize_phone(info.phone) == caller_phone:
                            is_agent_sec = True
                            break

            if is_agent_sec:
                root_logger.info(f"👤 נציג {caller_phone} נכנס למערכת השנייה - מפנה מיד לשלוחה 10")
                return Response("go_to_folder=/10", media_type="text/plain; charset=utf-8")

            if is_from_secondary:
                root_logger.warning(f"⚠️ מאזין {caller_phone or 'אנונימי'} נכנס למערכת השנייה ללא שידוך ממתין.")
                return Response("id_list_message=t-אין שיחה ממתינה עבורך&hangup=yes", media_type="text/plain; charset=utf-8")

    # ==============================================================
    # 2.9 שלוחת רישום וניהול קבלת צינתוקים (שלוחה 9)
    # ==============================================================
    tzintuk_reg_ext = build_path("9")
    if api_extension == tzintuk_reg_ext or api_extension == "9":
        if not caller_phone:
            return Response("id_list_message=t-מספר הטלפון אינו מזוהה&hangup=yes", media_type="text/plain; charset=utf-8")
        dest_9 = await prepare_agent_tzintuk_folder(caller_phone, end_goto=get_base_root_dest())
        root_logger.info(f"⚙️ כניסה לשלוחה 9 (ניהול צינתוקים) עבור {caller_phone} -> מעביר ל-{dest_9}")
        return Response(f"go_to_folder={dest_9}", media_type="text/plain; charset=utf-8")

    # ==============================================================
    # 3. שלוחת הניתוב החכמה לנציגים (שלוחה 10) - תור והודעות
    # ==============================================================
    gateway_ext = build_path("10")
    if api_extension == gateway_ext or api_extension == "10":
        manager.busy_agents.discard(caller_phone)
        
        is_agent = caller_phone in sheet_data.agent_names or caller_phone in sheet_data.agent_to_depts
        if not is_agent:
            for dept_dict in sheet_data.personal_lines.values():
                for info in dept_dict.values():
                    if normalize_phone(info.phone) == caller_phone:
                        is_agent = True
                        break

        if not is_agent:
            root_logger.warning(f"⛔ טלפון {caller_phone} ניסה להיכנס לשלוחה 10 אך אינו מוגדר כנציג בשיטס!")
            if is_secondary_configured() and is_from_secondary:
                return Response("id_list_message=t-אינך מוגדר כנציג&hangup=yes", media_type="text/plain; charset=utf-8")
            return Response(f"go_to_folder={get_base_root_dest()}", media_type="text/plain; charset=utf-8")

        agent_name = sheet_data.agent_names.get(caller_phone, "")
        root_logger.info(f"👤 נציג נכנס לשלוחה 10: {caller_phone} ({agent_name or 'ללא שם'})")

        call_action = params.get("call_action")
        idle_action = params.get("idle_action")
        msg_action = params.get("msg_action")
        action = params.get("action") or params.get("digits") or params.get("data") or params.get("key") or ""

        # מצב דו-מערכתי: במערכת החדשה -> רישום ראשוני בשלוחה 9 + שיחה (כניסה/ניתוק/ניהול צינתוקים)
        if is_secondary_configured() and is_from_secondary:
            if call_id in manager.pending_tzintuk_reg_calls:
                manager.pending_tzintuk_reg_calls.pop(call_id, None)
                asyncio.create_task(mark_agent_tzintuk_registered(caller_phone))
            elif call_action is None and idle_action is None:
                if not await is_agent_tzintuk_registered(caller_phone):
                    manager.pending_tzintuk_reg_calls[call_id] = caller_phone
                    dest_9 = await prepare_agent_tzintuk_folder(caller_phone, end_goto=f"/{gateway_ext}")
                    root_logger.info(f"📋 כניסה ראשונה לנציג {caller_phone} במערכת החדשה - מעביר לשלוחת רישום צינתוקים {dest_9}")
                    return Response(f"go_to_folder={dest_9}", media_type="text/plain; charset=utf-8")

            async with manager.lock:
                waiting_call = get_waiting_call_for_agent(caller_phone)

            if call_action is not None:
                root_logger.info(f"🔘 נציג {caller_phone} הקיש {call_action} בתפריט השיחה במערכת החדשה")
                if call_action == "1":
                    async with manager.lock:
                        selected_call = get_waiting_call_for_agent(caller_phone)
                        if selected_call:
                            agent_dest = await bridge_call_for_agent(caller_phone, agent_name, selected_call, agent_call_id=call_id)
                            return Response(f"id_list_message=t-מעביר אותך לשיחה&go_to_folder={agent_dest}", media_type="text/plain; charset=utf-8")
                        else:
                            root_logger.info(f"ℹ️ נציג {caller_phone} אישר מענה אך השיחה כבר אינה ממתינה בתור.")
                            return Response("id_list_message=t-השיחה כבר אינה ממתינה בתור&hangup=yes", media_type="text/plain; charset=utf-8")
                elif call_action == "9":
                    dest_9 = await prepare_agent_tzintuk_folder(caller_phone, end_goto=f"/{gateway_ext}")
                    root_logger.info(f"⚙️ נציג {caller_phone} עובר מהשיחה לניהול קבלת הצינתוקים -> {dest_9}")
                    return Response(f"go_to_folder={dest_9}", media_type="text/plain; charset=utf-8")
                else:
                    root_logger.info(f"📴 נציג {caller_phone} בחר לנתק מהשיחה במערכת החדשה.")
                    return Response("hangup=yes", media_type="text/plain; charset=utf-8")

            if idle_action is not None:
                root_logger.info(f"🔘 נציג {caller_phone} הקיש [{idle_action}] בתפריט ללא שיחות במערכת החדשה")
                if idle_action == "9":
                    dest_9 = await prepare_agent_tzintuk_folder(caller_phone, end_goto=f"/{gateway_ext}")
                    root_logger.info(f"⚙️ נציג {caller_phone} עובר לניהול קבלת הצינתוקים -> {dest_9}")
                    return Response(f"go_to_folder={dest_9}", media_type="text/plain; charset=utf-8")
                else:
                    root_logger.info(f"📴 נציג {caller_phone} בחר לנתק מהמערכת החדשה (אין שיחות ממתינות).")
                    return Response("hangup=yes", media_type="text/plain; charset=utf-8")

            if waiting_call:
                ident = format_caller_ident_for_tts(waiting_call)
                root_logger.info(f"📢 מציג לנציג {caller_phone} במערכת החדשה: שיחה ממתינה מאת {ident}")
                return Response(f"read=t-שיחה ממתינה מאת {ident} למעבר לשיחה הקישו אחת לניתוק הקישו שתיים לניהול קבלת הצינתוקים הקישו תשע=call_action,no,1,1,7,Digits", media_type="text/plain; charset=utf-8")
            else:
                root_logger.info(f"ℹ️ נציג {caller_phone} נכנס לשלוחה 10 במערכת החדשה - אין שיחות ממתינות.")
                return Response("read=t-אין שיחות ממתינות במערכת לניתוק השיחה הקש אחת או המתן לניהול קבלת השיחות הקש תשע=idle_action,no,1,1,5,Digits", media_type="text/plain; charset=utf-8")

        # מצב דו-מערכתי: במערכת הישנה -> הודעות בלבד!
        if is_secondary_configured() and not is_from_secondary:
            has_new_msg, msg_ext_full, latest_wav = await check_new_messages_for_agent(caller_phone)

            if msg_action:
                root_logger.info(f"🔘 נציג {caller_phone} הקיש {msg_action} בתפריט שמיעת הודעות במערכת הישנה")
                if msg_action == "1" and has_new_msg:
                    asyncio.create_task(save_agent_last_heard_file(caller_phone, latest_wav))
                    root_logger.info(f"🎧 מעביר נציג {caller_phone} לשמיעת הודעה ב-{msg_ext_full} (קובץ: {latest_wav})")
                    return Response(f"id_list_message=t-מעביר אותך לשמיעת ההודעה&go_to_folder=/{msg_ext_full}", media_type="text/plain; charset=utf-8")
                else:
                    return Response(f"go_to_folder={get_base_root_dest()}", media_type="text/plain; charset=utf-8")

            if has_new_msg:
                root_logger.info(f"📢 מציג לנציג {caller_phone} במערכת הישנה: קיימת הודעה חדשה.")
                return Response("read=t-קיימת הודעה חדשה לשמיעת ההודעה הקישו אחת לכניסה רגילה הקישו שתיים=msg_action,no,1,1,7,Digits", media_type="text/plain; charset=utf-8")
            else:
                root_logger.info(f"ℹ️ נציג {caller_phone} נכנס לשלוחה 10 במערכת הישנה - אין הודעות חדשות. מעביר לתפריט הראשי.")
                return Response(f"go_to_folder={get_base_root_dest()}", media_type="text/plain; charset=utf-8")

        # מצב חד-מערכתי רגיל (כשלא מוגדרת מערכת שנייה)
        if call_id in manager.pending_tzintuk_reg_calls:
            manager.pending_tzintuk_reg_calls.pop(call_id, None)
            asyncio.create_task(mark_agent_tzintuk_registered(caller_phone))
        elif call_action is None and idle_action is None and msg_action is None and not action:
            if not await is_agent_tzintuk_registered(caller_phone):
                manager.pending_tzintuk_reg_calls[call_id] = caller_phone
                dest_9 = await prepare_agent_tzintuk_folder(caller_phone, end_goto=f"/{gateway_ext}")
                root_logger.info(f"📋 כניסה ראשונה לנציג {caller_phone} - מעביר לשלוחת רישום צינתוקים {dest_9}")
                return Response(f"go_to_folder={dest_9}", media_type="text/plain; charset=utf-8")

        waiting_call = None
        async with manager.lock:
            waiting_call = get_waiting_call_for_agent(caller_phone)

        has_waiting_call = waiting_call is not None
        has_new_msg, msg_ext_full, latest_wav = await check_new_messages_for_agent(caller_phone)

        # שלב א': אישור מענה לשיחה
        if call_action is not None:
            root_logger.info(f"🔘 נציג {caller_phone} הקיש {call_action} בתפריט פרטי השיחה")
            if call_action == "1":
                async with manager.lock:
                    selected_call = get_waiting_call_for_agent(caller_phone)
                    if selected_call:
                        agent_dest = await bridge_call_for_agent(caller_phone, agent_name, selected_call, agent_call_id=call_id)
                        return Response(f"id_list_message=t-מעביר אותך לשיחה&go_to_folder={agent_dest}", media_type="text/plain; charset=utf-8")
                    else:
                        root_logger.info(f"ℹ️ נציג {caller_phone} אישר מענה אך השיחה כבר אינה ממתינה בתור.")
                        return Response("id_list_message=t-השיחה כבר אינה ממתינה בתור&hangup=yes", media_type="text/plain; charset=utf-8")
            elif call_action == "9":
                dest_9 = await prepare_agent_tzintuk_folder(caller_phone, end_goto=f"/{gateway_ext}")
                root_logger.info(f"⚙️ נציג {caller_phone} עובר לניהול קבלת הצינתוקים -> {dest_9}")
                return Response(f"go_to_folder={dest_9}", media_type="text/plain; charset=utf-8")
            else:
                root_logger.info(f"📴 נציג {caller_phone} בחר לנתק מהשיחה.")
                return Response("hangup=yes", media_type="text/plain; charset=utf-8")

        if idle_action is not None:
            root_logger.info(f"🔘 נציג {caller_phone} הקיש [{idle_action}] בתפריט ללא שיחות")
            if idle_action == "9":
                dest_9 = await prepare_agent_tzintuk_folder(caller_phone, end_goto=f"/{gateway_ext}")
                root_logger.info(f"⚙️ נציג {caller_phone} עובר לניהול קבלת הצינתוקים -> {dest_9}")
                return Response(f"go_to_folder={dest_9}", media_type="text/plain; charset=utf-8")
            else:
                root_logger.info(f"📴 נציג {caller_phone} בחר לנתק (אין שיחות ממתינות).")
                return Response("hangup=yes", media_type="text/plain; charset=utf-8")

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
                return Response(f"read=t-שיחה ממתינה מאת {ident} למעבר לשיחה הקישו אחת לניתוק הקישו שתיים לניהול קבלת הצינתוקים הקישו תשע=call_action,no,1,1,7,Digits", media_type="text/plain; charset=utf-8")
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
            return Response(f"read=t-שיחה ממתינה מאת {ident} למעבר לשיחה הקישו אחת לניתוק הקישו שתיים לניהול קבלת הצינתוקים הקישו תשע=call_action,no,1,1,7,Digits", media_type="text/plain; charset=utf-8")
        elif has_new_msg:
            root_logger.info(f"📢 מציג לנציג {caller_phone}: קיימת הודעה חדשה.")
            return Response("read=t-קיימת הודעה חדשה לשמיעת ההודעה הקישו אחת לכניסה רגילה הקישו שתיים=msg_action,no,1,1,7,Digits", media_type="text/plain; charset=utf-8")
        else:
            root_logger.info(f"ℹ️ נציג {caller_phone} נכנס לשלוחה 10 - אין שיחות ממתינות.")
            return Response("read=t-אין שיחות ממתינות במערכת לניתוק השיחה הקש אחת או המתן לניהול קבלת השיחות הקש תשע=idle_action,no,1,1,5,Digits", media_type="text/plain; charset=utf-8")

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
            if is_secondary_configured() and is_from_secondary:
                log_candidates = [
                    (get_secondary_token(), SECONDARY_CONF_LOG_FILE_PATH),
                    (get_secondary_token(), YEMOT_LOG_FILE_PATH),
                    (YEMOT_API_TOKEN, YEMOT_LOG_FILE_PATH),
                ]
            elif is_secondary_configured():
                log_candidates = [
                    (YEMOT_API_TOKEN, YEMOT_LOG_FILE_PATH),
                    (get_secondary_token(), SECONDARY_CONF_LOG_FILE_PATH),
                ]
            else:
                log_candidates = [(YEMOT_API_TOKEN, YEMOT_LOG_FILE_PATH)]

            for tok, log_path_cand in log_candidates:
                if not tok:
                    continue
                res_log = await yemot_api_request("GetTextFile", params={"what": log_path_cand}, timeout=4.0, token=tok)
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
                if rec:
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
        
        sec_or_pri_token = get_secondary_token() if is_from_secondary else YEMOT_API_TOKEN
        root_logger.info(f"🔍 שולף פרטי מאזין מקובץ {txt_path}...")
        res_txt = await yemot_api_request("GetTextFile", params={"what": txt_path}, timeout=4.0, token=sec_or_pri_token)
        if (not res_txt or res_txt.get("responseStatus") != "OK") and is_secondary_configured():
            fallback_tok = YEMOT_API_TOKEN if sec_or_pri_token != YEMOT_API_TOKEN else get_secondary_token()
            res_txt_alt = await yemot_api_request("GetTextFile", params={"what": txt_path}, timeout=4.0, token=fallback_tok)
            if res_txt_alt and res_txt_alt.get("responseStatus") == "OK":
                res_txt = res_txt_alt
                sec_or_pri_token = fallback_tok
        
        caller_phone_found = ""
        if res_txt and res_txt.get("responseStatus") == "OK":
            content = res_txt.get("contents", "")
            root_logger.info(f"📄 תוכן קובץ {file_prefix}.txt שהתקבל מימות המשיח: {content}")
            
            m_phone = re.search(r'Phone-([0-9]+)', content)
            if m_phone:
                caller_phone_found = normalize_phone(m_phone.group(1))
            else:
                m_back = re.search(r'(?:CallerId|Phone|Caller|caller_id|טלפון)[-=\s]*([0-9]+)', content, re.IGNORECASE)
                if m_back:
                    caller_phone_found = normalize_phone(m_back.group(1))

        if not caller_phone_found:
            root_logger.warning(f"⚠️ לא נמצא מספר טלפון תקין של מאזין ב-{txt_path}")
            return Response("id_list_message=t-לא נמצא מספר טלפון לחיוג חוזר עבור הודעה זו", media_type="text/plain; charset=utf-8")

        root_logger.info(f"🎯 אותר מספר המאזין שהשאיר את ההודעה: {caller_phone_found}")

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
            }, timeout=4.0, token=sec_or_pri_token)

            return Response(f"id_list_message=t-מעביר אותך לחיוג אנונימי למשאיר ההודעה&go_to_folder=/{nitoviya_ext}", media_type="text/plain; charset=utf-8")
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
                agent_dest = await bridge_call_for_agent(caller_phone, agent_name, selected_call, agent_call_id=call_id)
                return Response(f"id_list_message=t-מעביר אותך לשיחה&go_to_folder={agent_dest}", media_type="text/plain; charset=utf-8")
            else:
                root_logger.info(f"⏳ נציג {caller_phone} ממתין בשלוחה 20 - אין שיחות ממתינות.")
                return Response(f"id_list_message=t-אין שיחות ממתינות כעת&go_to_folder=/{api_extension}", media_type="text/plain; charset=utf-8")

    # ==============================================================
    # 6. כניסת מאזינים לתור (זיהוי קפדני של קווים אישיים!)
    # ==============================================================
    sub_ext = api_extension
    if BASE_FOLDER and sub_ext.startswith(f"{BASE_FOLDER}/"):
        sub_ext = sub_ext[len(BASE_FOLDER) + 1:]

    if sub_ext in ["4/2/1", "4/2/2"]:
        return Response("noop=yes", media_type="text/plain; charset=utf-8")

    if len(manager.waiting_calls) == 0:
        await sheet_data.reload_from_sheets()

    sub_ext_parts = sub_ext.split("/")
    
    possible_keys = [sub_ext]
    if len(sub_ext_parts) >= 2:
        possible_keys.append(sub_ext_parts[-1])
        possible_keys.append(f"{sub_ext_parts[0]}/{sub_ext_parts[-1]}")

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

    # 1. חיפוש קו אישי (עדיפות עליונה)
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
                root_logger.info(f"🎯 זוהה קו אישי עבור {k}: {p_info.name} ({p_info.phone}) במחלקה {dept_id}")
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

    # 3. חיפוש תור כללי מפורש מהגיליון
    if not queue_type:
        for d_id in depts_to_search:
            g_set = sheet_data.general_queue_extensions.get(d_id, set())
            if any(k in g_set for k in possible_keys):
                dept_id = d_id
                queue_type = "general"
                transfer_msg = "מעביר אותך לשיחה עם נציג"
                break

    # 4. ברירת מחדל אך ורק אם המאזין חייג ישירות לשלוחה הראשית של המחלקה (1, 2, 3, 4)
    if not queue_type and sub_ext in ["1", "2", "3", "4"] and sub_ext in sheet_data.dept_general_agents:
        dept_id = sub_ext
        queue_type = "general"
        transfer_msg = "מעביר אותך לשיחה עם נציג"

    if not queue_type or not dept_id:
        root_logger.warning(f"⚠️ שלוחה [{api_extension}] אינה מוגדרת בשיטס באף מחלקה! (טלפון מאזין: {caller_phone})")
        return Response("id_list_message=t-שלוחה לא מוגדרת בשיטס&go_to_folder=..", media_type="text/plain; charset=utf-8")

    customer_name = sheet_data.customer_contacts.get(caller_phone, "")

    # כניסה ראשונית של המאזין לתור
    async with manager.lock:
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
            stats_manager.record_incoming(dept_id, queue_type, target_manager_phone)

            # הפעלת צינתוק: אישי -> לנציג היחיד! כללי -> לכל נציגי המחלקה
            if queue_type == "personal":
                clean_target = normalize_phone(target_manager_phone or "")
                pos = sum(1 for c in manager.waiting_calls.values() if normalize_phone(c.target_manager_phone or "") == clean_target)
                t_key = f"personal_{clean_target}"
                phone_func = lambda: [target_manager_phone]
                root_logger.info(f"📱 מפעיל סבב צינתוק אישי עבור הנציג {target_manager_phone} בלבד!")
            else:
                pos = sum(1 for c in manager.waiting_calls.values() if c.dept_id == dept_id and c.queue_type == "general")
                t_key = f"general_{dept_id}"
                phone_func = lambda: list(sheet_data.dept_general_agents.get(dept_id, []))
                root_logger.info(f"⚡ מפעיל סבב צינתוקים כללי עבור מחלקה {dept_id}")

            if t_key not in manager.tzintuk_tasks or manager.tzintuk_tasks[t_key].done():
                manager.tzintuk_tasks[t_key] = asyncio.create_task(run_tzintuk_loop(t_key, phone_func))

            display_caller = f"{customer_name} ({caller_phone})" if customer_name else caller_phone
            root_logger.info(f"⏳ מאזין {display_caller} נכנס לתור מחלקה {dept_id} (סוג: {queue_type}, שלוחה: {api_extension}, מיקום: {pos})")
            return Response(f"id_list_message=t-מיקומך בתור הוא {pos}&go_to_folder=/{api_extension}", media_type="text/plain; charset=utf-8")

    # המתנה רציפה
    event = asyncio.Event()
    manager.waiting_events[call_id] = event

    try:
        await asyncio.wait_for(event.wait(), timeout=float(WAIT_TIMEOUT))
        fallback_dest = f"/{ROOMS_ROOT_FOLDER}/{dept_id}/1/1"
        destination = manager.waiting_destinations.get(call_id, fallback_dest)
        display_caller = f"{customer_name} ({caller_phone})" if customer_name else caller_phone
        if is_secondary_configured():
            sec_num = get_secondary_system_number()
            root_logger.info(f"✅ מאזין {display_caller} שוחרר מההמתנה ועובר למערכת השנייה ({sec_num}) לחדר {destination} ({transfer_msg})")
            return Response(f"id_list_message=t-{transfer_msg}&routing_yemot={sec_num}", media_type="text/plain; charset=utf-8")
        root_logger.info(f"✅ מאזין {display_caller} שוחרר מההמתנה ועובר ל-{destination} ({transfer_msg})")
        return Response(f"id_list_message=t-{transfer_msg}&go_to_folder={destination}", media_type="text/plain; charset=utf-8")
    except asyncio.TimeoutError:
        async with manager.lock:
            if queue_type == "personal":
                clean_target = normalize_phone(target_manager_phone or "")
                pos = sum(1 for c in manager.waiting_calls.values() if normalize_phone(c.target_manager_phone or "") == clean_target and c.entry_time <= manager.waiting_calls[call_id].entry_time)
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
