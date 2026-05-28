# -*- coding: utf-8 -*-
"""
完整主程式版本（對應最新需求）
- buyer 卡片式 + 顯示最後一筆追蹤
- seller 卡片式（寬度較窄，由 templates/sellers.html 控制）
- development 使用「目前狀況 / 下一步 / 下次時間」
- LINE Bot 回覆：已註記客需 / 已註記委託 / 已註記開發

注意：
1. 這支檔案請直接覆蓋你目前的主程式，不要再疊 patch。
2. 請搭配最新 templates/buyers.html、templates/sellers.html、templates/developments.html 使用。
"""

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, session, Response, Blueprint, send_file
)
import os
import sys
import json
from datetime import datetime
from zoneinfo import ZoneInfo
import csv
from io import StringIO, BytesIO
import hmac
import hashlib
import base64
import re
from uuid import uuid4
from PIL import Image
from copy import deepcopy

try:
    from docx import Document
    from docx.shared import Mm, Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
except Exception:
    Document = None
    Mm = Pt = None
    WD_ALIGN_PARAGRAPH = None
    WD_CELL_VERTICAL_ALIGNMENT = None

import firebase_admin
from firebase_admin import credentials, firestore, storage  
from werkzeug.security import generate_password_hash, check_password_hash
from urllib.parse import urlparse, unquote
import time
from werkzeug.utils import secure_filename

print("Working directory:", os.getcwd())

# ========= 專案路徑 + 台北時區設定 =========
# 打包成 exe 時，BASE_DIR 會指向 exe 所在資料夾；一般執行時，指向 app.py 所在資料夾。
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

def now_taipei():
    """統一取得台北時區時間，避免 Render / 伺服器使用 UTC 導致時間差 8 小時。"""
    return datetime.now(TAIPEI_TZ)


# ========= Firestore + Storage 初始化（Render + 本機皆可用） =========
def init_firebase():
    """
    初始化 Firebase：
    - Render / 雲端：優先從環境變數 FIREBASE_CREDENTIALS 讀取
    - 本機 / 打包 exe：自動尋找 serviceAccountKey.json
    - 備援：使用你指定的完整憑證路徑
    """
    # 如果已初始化過，就直接回傳 Firestore client
    if firebase_admin._apps:
        return firestore.client()

    cred = None

    # 1️⃣ Render / 伺服器：從環境變數讀 FIREBASE_CREDENTIALS
    cred_json = os.environ.get("FIREBASE_CREDENTIALS")
    if cred_json:
        try:
            cred_dict = json.loads(cred_json)
            cred = credentials.Certificate(cred_dict)
            print("✅ 使用 FIREBASE_CREDENTIALS 初始化 Firebase")
        except Exception as e:
            print("⚠️ 解析 FIREBASE_CREDENTIALS 失敗：", e)

    # 2️⃣ 本機 / exe：依序尋找憑證
    credential_paths = [
        os.path.join(BASE_DIR, "serviceAccountKey.json"),
        os.path.join(os.getcwd(), "serviceAccountKey.json"),
        r"C:\Users\ellen\Desktop\00_Workspace\08_程式設計\team_me_crm_firebase\serviceAccountKey.json",
    ]

    if not cred:
        for key_path in credential_paths:
            if key_path and os.path.exists(key_path):
                cred = credentials.Certificate(key_path)
                print(f"✅ 使用 Firebase 憑證初始化：{key_path}")
                break

    if not cred:
        checked = "\n".join(f"- {p}" for p in credential_paths)
        raise RuntimeError(
            "找不到 Firebase 憑證：請確認 serviceAccountKey.json 是否存在。\n"
            f"目前工作目錄：{os.getcwd()}\n"
            f"程式資料夾：{BASE_DIR}\n"
            f"已檢查路徑：\n{checked}\n"
            "或請在 Render 設定 FIREBASE_CREDENTIALS 環境變數。"
        )

    # ⭐ 這裡同時指定 Storage bucket
    firebase_admin.initialize_app(cred, {
        "storageBucket": "team-me-98acf.firebasestorage.app"
    })

    return firestore.client()


# 全域 Firestore client
db = init_firebase()

# ========= 圖片上傳相關設定 =========
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}

def allowed_image(filename: str) -> bool:
    if not filename:
        return False
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


# 後端統一壓縮設定
MAX_WIDTH = 1600
MAX_HEIGHT = 1600
MAX_BYTES = 800 * 1024   # 800 KB 以內（約 5 張 ~ 4MB 左右）
QUALITY_STEPS = [80, 70, 60, 50]   # 逐步降品質直到符合大小


def upload_image_to_storage(file, folder: str, object_id: str):
    """
    前端已壓縮一次，後端再保險壓縮一次，避免超肥圖炸 Render / Storage。
    回傳公開網址，失敗回傳 None。
    """
    if not file or not file.filename:
        return None

    try:
        # 讀入圖片
        img = Image.open(file.stream)

        # 統一轉成 RGB，避免 PNG / HEIC 等模式出問題
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        # 等比縮圖：不超過 1600x1600
        img.thumbnail((MAX_WIDTH, MAX_HEIGHT))

        best_buf = None
        best_size = None

        # 依序嘗試不同品質，挑一個 <= MAX_BYTES 或最後一個
        for q in QUALITY_STEPS:
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=q, optimize=True)
            size = buf.tell()

            # 先記住（以防全部都 > MAX_BYTES，也至少有一個）
            if best_buf is None or size < best_size:
                best_buf = buf
                best_size = size

            if size <= MAX_BYTES:
                break

        # 使用選到的 buffer
        best_buf.seek(0)

        # 產生檔名
        base_name = secure_filename(file.filename.rsplit(".", 1)[0]) or "image"
        filename = f"{base_name}_{int(time.time())}.jpg"
        blob_path = f"{folder}/{object_id}_{filename}"

        bucket = storage.bucket()
        blob = bucket.blob(blob_path)

        # 上傳
        blob.upload_from_file(best_buf, content_type="image/jpeg")
        # 如果你原本有用 make_public，就保留；沒有就用 token 方式
        blob.make_public()
        return blob.public_url

    except Exception as e:
        print("❌ 上傳圖片至 Storage 時發生錯誤：", e)
        return None


def delete_image_from_storage(folder: str, object_id: str):
    """
    刪除 Firebase Storage 裡這個 buyer/seller 的圖片
    - folder: "buyers" / "sellers"
    - object_id: Firestore 文件 id
    這裡會嘗試所有常見副檔名，有存在就刪掉。
    """
    try:
        bucket = storage.bucket()
        for ext in ALLOWED_IMAGE_EXTENSIONS:
            blob_path = f"{folder}/{object_id}.{ext}"
            blob = bucket.blob(blob_path)
            # 避免亂砍，先檢查有沒有存在
            if blob.exists():
                blob.delete()
                print("🗑️ 已刪除圖片：", blob_path)
    except Exception as e:
        print("⚠️ 刪除圖片時發生錯誤：", e)
        



# ========= Flask 基本設定 =========
app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET_KEY", "team_me_super_secret")

# BASE_DIR 已在檔案前段依本機 / exe 模式設定

# blog Blueprint 改成可選；如果專案裡沒有 blog.py，也能正常啟動
try:
    from blog import blog_bp
    app.register_blueprint(blog_bp)
    print("✅ blog blueprint 已載入")
except ModuleNotFoundError:
    print("ℹ️ 找不到 blog.py，略過 blog blueprint 載入")
except Exception as e:
    print(f"⚠️ blog blueprint 載入失敗：{e}")

# 限制單一請求最大 5MB（可依需求調整）
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024


# ========= LINE Webhook / 分類設定 =========
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "").strip()
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()

DEFAULT_LABEL_OPTIONS = [
    "開發紀錄",
    "插街",
    "待盤點客戶",
    "售-客戶需求",
    "租-客戶需求",
    "委託",
    "買賣委託",
    "出租委託",
    "廣告",
    "影片待剪/排程",
    "影片上架",
    "LINE紀錄",
    "群組回覆註記",
]


# ========= 小工具 =========
def doc_to_dict(doc):
    """把 Firestore Document 轉成 dict 並加上 id 欄位"""
    data = doc.to_dict()
    data["id"] = doc.id
    return data


def delete_by_field(collection_name, field_name, field_value):
    """
    把 collection_name 中 field_name == field_value 的文件全部刪掉
    用來刪掉某個客戶底下所有追蹤紀錄
    """
    ref = db.collection(collection_name).where(field_name, "==", field_value)
    docs = list(ref.stream())
    for d in docs:
        d.reference.delete()



def ensure_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()] if str(value).strip() else []


def parse_label_csv(text_value: str):
    if not text_value:
        return []
    parts = re.split(r"[，,、\n]+", text_value)
    return [p.strip() for p in parts if p.strip()]


def dedupe_keep_order(items):
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def normalize_phone(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def get_request_labels(form):
    labels = []
    labels.extend(form.getlist("labels"))
    labels.extend(parse_label_csv(form.get("labels_csv", "").strip()))
    return dedupe_keep_order([x.strip() for x in labels if x and str(x).strip()])


def get_label_options_from_docs(docs):
    label_set = set(DEFAULT_LABEL_OPTIONS)
    for item in docs:
        for label in ensure_list(item.get("labels")):
            if label:
                label_set.add(label)
    return sorted(label_set)


def append_note_block(old_note: str, content: str, source_label: str = "LINE"):
    stamp = now_taipei().strftime("%Y-%m-%d %H:%M")
    block = f"[{stamp}][{source_label}] {content}".strip()
    if old_note and old_note.strip():
        return old_note.rstrip() + "\n\n" + block
    return block


def verify_line_signature(raw_body: bytes, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET or not signature:
        return False
    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def line_api_headers():
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }


def reply_line_text(reply_token: str, text_message: str):
    if not LINE_CHANNEL_ACCESS_TOKEN or not reply_token:
        return

    payload = {
        "replyToken": reply_token,
        "messages": [
            {"type": "text", "text": text_message[:5000]}
        ],
    }

    try:
        import requests
        res = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers=line_api_headers(),
            json=payload,
            timeout=8,
        )
        print("LINE reply status:", res.status_code, res.text[:300])
    except Exception as e:
        print("⚠️ LINE reply 發生錯誤：", e)



def normalize_line_key(key: str):
    k = (key or "").strip().replace(" ", "")
    mapping = {
        "對象": "target_type",
        "類型": "target_type",
        "目標": "target_type",
        "客戶類型": "target_type",
        "電話": "phone",
        "手機": "phone",
        "電話號碼": "phone",
        "姓名": "name",
        "客戶": "name",
        "買方": "name",
        "賣方": "name",
        "客戶來源": "source",
        "來源": "source",
        "需求類型": "intent_type_raw",
        "委託類型": "deal_type_raw",
        "ID": "record_id",
        "客戶ID": "record_id",
        "buyer_id": "record_id",
        "seller_id": "record_id",
        "內容": "content",
        "紀錄": "content",
        "備註": "content",
        "說明": "content",
        "進度內容": "content",
        "進程": "stage",
        "階段": "stage",
        "狀態": "stage",
        "標籤": "labels",
        "分類": "labels",
        "labels": "labels",
        "下一步": "next_action",
        "下次行動": "next_action",
        "next_action": "next_action",
        "下次聯絡日": "next_contact_date",
        "下次聯絡日期": "next_contact_date",
        "next_contact_date": "next_contact_date",
        "地址": "address",
        "物件": "address",
        "案名": "address",
        "總價": "price",
        "價格": "price",
        "預算": "budget",
        "區域": "preferred_areas",
        "產品類型": "property_type",
        "房數": "room_range",
        "車位": "car_need",
        "開價": "expected_price",
        "底價": "min_price",
        "委託到期日": "contract_end_date",
        "筆數": "limit",
    }
    return mapping.get(k, k)


def normalize_target_type(value: str) -> str:
    v = (value or "").strip()
    if v in ("買方", "客需", "buyer", "buyers"):
        return "buyer"
    if v in ("賣方", "委託", "seller", "sellers", "屋主"):
        return "seller"
    return ""


def normalize_intent_type(value: str, fields=None) -> str:
    fields = fields or {}
    raw = (value or "").strip().lower()
    if raw in ("租", "租屋", "出租", "承租", "rent"):
        return "rent"
    if raw in ("買", "買房", "買賣", "購屋", "buy", "sale", "售屋"):
        return "buy"

    budget = str(fields.get("budget", "") or "")
    price = str(fields.get("price", "") or "")
    if "萬" in budget or "萬" in price:
        return "buy"
    digits = re.sub(r"\D", "", budget or price)
    if digits:
        try:
            num = int(digits)
            if num >= 1000000:
                return "buy"
            if num <= 100000:
                return "rent"
        except Exception:
            pass
    return ""


def normalize_deal_type(value: str) -> str:
    raw = (value or "").strip().lower()
    if raw in ("租", "出租", "委租", "rent"):
        return "rent"
    if raw in ("賣", "出售", "賣屋", "買賣", "委售", "sale", "sell"):
        return "sale"
    return ""


def parse_int_limit(value, default=10, max_value=30):
    try:
        num = int(str(value).strip())
        return max(1, min(max_value, num))
    except Exception:
        return default


def get_line_sender_display_name(event):
    source = event.get("source") or {}
    user_id = source.get("userId", "")
    if not user_id or not LINE_CHANNEL_ACCESS_TOKEN:
        return ""

    cache_key = f"{source.get('groupId', '')}:{source.get('roomId', '')}:{user_id}"
    cache = app.config.setdefault("LINE_PROFILE_CACHE", {})
    if cache_key in cache:
        return cache[cache_key]

    headers = line_api_headers()
    url = ""
    if source.get("groupId"):
        url = f"https://api.line.me/v2/bot/group/{source['groupId']}/member/{user_id}"
    elif source.get("roomId"):
        url = f"https://api.line.me/v2/bot/room/{source['roomId']}/member/{user_id}"
    else:
        url = f"https://api.line.me/v2/bot/profile/{user_id}"

    try:
        import requests
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code == 200:
            data = res.json() or {}
            display_name = data.get("displayName", "") or ""
            cache[cache_key] = display_name
            return display_name
    except Exception as e:
        print("⚠️ 取得 LINE 使用者名稱失敗：", e)

    return ""


def reply_line_text(reply_token: str, text_message: str):
    if not LINE_CHANNEL_ACCESS_TOKEN or not reply_token:
        return None

    payload = {
        "replyToken": reply_token,
        "messages": [
            {"type": "text", "text": (text_message or "")[:5000]}
        ],
    }

    try:
        import requests
        res = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers=line_api_headers(),
            json=payload,
            timeout=8,
        )
        print("LINE reply status:", res.status_code, res.text[:300])
        data = {}
        try:
            data = res.json() if res.text else {}
        except Exception:
            data = {}
        return {
            "status_code": res.status_code,
            "data": data,
            "sent_messages": (data or {}).get("sentMessages", []),
        }
    except Exception as e:
        print("⚠️ LINE reply 發生錯誤：", e)
        return None


def save_line_message_link(message_id: str, target_type: str, target_id: str, tag="", action="", customer_name="", phone="", source_event=None):
    if not message_id or not target_type or not target_id:
        return
    data = {
        "message_id": message_id,
        "target_type": target_type,
        "target_id": target_id,
        "tag": tag,
        "action": action,
        "customer_name": customer_name,
        "phone": phone,
        "created_at": now_taipei().isoformat(),
    }
    if source_event:
        source = source_event.get("source", {})
        data["line_group_id"] = source.get("groupId", "")
        data["line_room_id"] = source.get("roomId", "")
        data["line_user_id"] = source.get("userId", "")
        data["sender_display_name"] = get_line_sender_display_name(source_event)
    db.collection("line_message_links").document(str(message_id)).set(data, merge=True)


def get_line_message_link(message_id: str):
    if not message_id:
        return None
    doc = db.collection("line_message_links").document(str(message_id)).get()
    if doc.exists:
        return doc.to_dict() or {}
    return None


def build_line_operator_label(event):
    sender = get_line_sender_display_name(event) or "未知成員"
    return f"LINE/{sender}"


def build_line_summary(content: str, event):
    sender = get_line_sender_display_name(event) or "未知成員"
    text = (content or "").strip()
    return f"{sender}：{text}" if text else f"{sender}：LINE 更新"


def parse_line_formatted_message(text: str):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None

    first = lines[0]
    if not first.startswith("#"):
        return None

    tag = first.lstrip("#").strip()
    tag_map = {
        "新增客需": "create_buyer_need",
        "新增委託": "create_seller_listing",
        "買方追蹤": "buyer_followup",
        "賣方追蹤": "seller_followup",
        "客戶分類": "classify",
        "查詢紀錄": "query_records",
        "查詢委託到期": "query_contract_end",
        "帶看": "buyer_followup",
        "成交": "buyer_followup",
        "委託": "seller_followup",
        "紀錄": "generic_note",
    }
    action = tag_map.get(tag)
    if not action:
        return None

    fields = {}
    for line in lines[1:]:
        m = re.match(r"^([^:：]+)\s*[:：]\s*(.+)$", line)
        if not m:
            continue
        key = normalize_line_key(m.group(1))
        value = m.group(2).strip()
        if key == "labels":
            fields[key] = parse_label_csv(value)
        else:
            fields[key] = value

    if tag in ("買方追蹤", "帶看", "成交") and not fields.get("target_type"):
        fields["target_type"] = "buyer"
    if tag in ("賣方追蹤", "委託") and not fields.get("target_type"):
        fields["target_type"] = "seller"

    fields["target_type"] = normalize_target_type(fields.get("target_type", "")) or fields.get("target_type", "")
    fields["intent_type"] = normalize_intent_type(fields.get("intent_type_raw", ""), fields)
    fields["deal_type"] = normalize_deal_type(fields.get("deal_type_raw", ""))
    fields["limit"] = parse_int_limit(fields.get("limit", 10), default=10, max_value=30)

    if action == "create_buyer_need":
        if not (fields.get("name") and fields.get("phone") and fields.get("source")):
            return None
    elif action == "create_seller_listing":
        if not (fields.get("name") and fields.get("phone") and fields.get("source")):
            return None
    elif action in ("buyer_followup", "seller_followup", "classify", "query_records", "query_contract_end"):
        if not (fields.get("record_id") or fields.get("phone") or fields.get("name")):
            return None

    return {
        "tag": tag,
        "action": action,
        "fields": fields,
        "raw_text": text,
    }


def find_records_by_phone(collection_name: str, phone: str):
    normalized_phone = normalize_phone(phone)
    if not normalized_phone:
        return []
    matches = []
    for doc in db.collection(collection_name).stream():
        data = doc.to_dict() or {}
        if normalize_phone(data.get("phone", "")) == normalized_phone:
            matches.append(doc)
    return matches


def find_customer_record(target_type: str, record_id: str = "", phone: str = "", name: str = ""):
    collection_name = "buyers" if target_type == "buyer" else "sellers"

    if record_id:
        doc = db.collection(collection_name).document(record_id).get()
        if doc.exists:
            return doc

    if phone:
        matches = find_records_by_phone(collection_name, phone)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return None

    if name:
        docs = list(db.collection(collection_name).where("name", "==", name.strip()).limit(2).stream())
        if len(docs) == 1:
            return docs[0]

    return None


def resolve_customer_record(fields, preferred_target_type=""):
    record_id = fields.get("record_id", "")
    phone = fields.get("phone", "")
    name = fields.get("name", "")
    target_type = preferred_target_type or normalize_target_type(fields.get("target_type", ""))

    if target_type in ("buyer", "seller"):
        doc = find_customer_record(target_type, record_id, phone, name)
        return target_type, doc

    buyer_doc = find_customer_record("buyer", record_id, phone, name)
    seller_doc = find_customer_record("seller", record_id, phone, name)

    if buyer_doc and not seller_doc:
        return "buyer", buyer_doc
    if seller_doc and not buyer_doc:
        return "seller", seller_doc
    return "", None


def update_customer_note_and_labels(target_type: str, doc_ref, content: str, labels=None, stage="", source="LINE", event=None):
    labels = dedupe_keep_order(["LINE紀錄"] + ensure_list(labels))
    snapshot = doc_ref.get()
    current = snapshot.to_dict() or {}
    old_note = current.get("note", "")

    source_label = build_line_operator_label(event) if event else "LINE"
    updates = {
        "note": append_note_block(old_note, content, source_label),
        "labels": firestore.ArrayUnion(labels),
        "updated_at": now_taipei().isoformat(),
        "updated_by_id": "line_bot",
        "updated_by_name": "LINE Bot",
    }
    if stage:
        updates["stage"] = stage
    if source:
        updates["source"] = source

    doc_ref.update(updates)


def add_customer_followup(target_type: str, customer_id: str, content: str, next_action="", next_contact_date="", labels=None, line_event=None):
    collection_name = "buyer_followups" if target_type == "buyer" else "seller_followups"
    key_name = "buyer_id" if target_type == "buyer" else "seller_id"

    sender_display_name = get_line_sender_display_name(line_event) if line_event else ""
    data = {
        key_name: customer_id,
        "contact_time": now_taipei().strftime("%Y-%m-%d %H:%M"),
        "channel": "LINE",
        "content": content,
        "next_action": next_action,
        "next_contact_date": next_contact_date,
        "labels": dedupe_keep_order(["LINE紀錄"] + ensure_list(labels)),
        "created_at": now_taipei().isoformat(),
        "created_by_id": "line_bot",
        "created_by_name": "LINE Bot",
        "sender_display_name": sender_display_name,
    }

    if line_event:
        source = line_event.get("source", {})
        data["line_group_id"] = source.get("groupId", "")
        data["line_room_id"] = source.get("roomId", "")
        data["line_user_id"] = source.get("userId", "")
        data["line_message_id"] = (line_event.get("message") or {}).get("id", "")
        data["quoted_message_id"] = (line_event.get("message") or {}).get("quotedMessageId", "")

    db.collection(collection_name).add(data)


def save_line_log(parsed, event, status, target_type="", target_id="", note="", sender_display_name=""):
    source = event.get("source", {})
    msg = event.get("message") or {}
    db.collection("line_logs").add({
        "tag": parsed.get("tag", ""),
        "action": parsed.get("action", ""),
        "fields": parsed.get("fields", {}),
        "raw_text": parsed.get("raw_text", ""),
        "status": status,
        "target_type": target_type,
        "target_id": target_id,
        "note": note,
        "sender_display_name": sender_display_name,
        "line_group_id": source.get("groupId", ""),
        "line_room_id": source.get("roomId", ""),
        "line_user_id": source.get("userId", ""),
        "message_id": msg.get("id", ""),
        "quoted_message_id": msg.get("quotedMessageId", ""),
        "webhook_event_id": event.get("webhookEventId", ""),
        "created_at": now_taipei().isoformat(),
    })


def build_buyer_labels(intent_type: str, extra_labels=None):
    base = ["LINE紀錄", "租-客戶需求" if intent_type == "rent" else "售-客戶需求"]
    return dedupe_keep_order(base + ensure_list(extra_labels))


def build_seller_labels(deal_type: str, extra_labels=None):
    base = ["LINE紀錄", "委託", "出租委託" if deal_type == "rent" else "買賣委託"]
    return dedupe_keep_order(base + ensure_list(extra_labels))


def create_buyer_need(fields, event):
    sender_name = get_line_sender_display_name(event) or "未知成員"
    phone = fields.get("phone", "").strip()
    matches = find_records_by_phone("buyers", phone)

    intent_type = normalize_intent_type(fields.get("intent_type_raw", "") or fields.get("intent_type", ""), fields)
    if not intent_type:
        return {
            "handled": True,
            "ok": False,
            "reply_text": "未寫入：#新增客需 請填 需求類型: 租 或 買賣",
        }

    labels = build_buyer_labels(intent_type, fields.get("labels"))
    budget = fields.get("budget", "").strip()

    payload = {
        "name": fields.get("name", "").strip(),
        "phone": phone,
        "source": fields.get("source", "").strip(),
        "intent_type": intent_type,
        "stage": fields.get("stage", "").strip() or "接觸",
        "preferred_areas": fields.get("preferred_areas", "").strip(),
        "property_type": fields.get("property_type", "").strip(),
        "room_range": fields.get("room_range", "").strip(),
        "car_need": fields.get("car_need", "").strip(),
        "labels": labels,
        "updated_at": now_taipei().isoformat(),
        "updated_by_id": "line_bot",
        "updated_by_name": "LINE Bot",
    }

    if intent_type == "rent":
        payload["rent_max"] = fields.get("rent_max", "").strip() or budget
    else:
        payload["budget_max"] = fields.get("budget_max", "").strip() or budget

    extra_content = fields.get("content", "").strip()
    note_content = extra_content or "LINE 新增客需"
    note_content = build_line_summary(note_content, event)

    if len(matches) == 1:
        doc = matches[0]
        doc_ref = db.collection("buyers").document(doc.id)
        update_customer_note_and_labels(
            target_type="buyer",
            doc_ref=doc_ref,
            content=note_content,
            labels=labels,
            stage=payload["stage"],
            source=payload["source"],
            event=event,
        )
        doc_ref.update(payload)
        add_customer_followup(
            target_type="buyer",
            customer_id=doc.id,
            content=note_content,
            labels=labels,
            line_event=event,
        )
        updated_doc = doc_ref.get().to_dict() or {}
        return {
            "handled": True,
            "ok": True,
            "reply_text": f"已註記客需：{updated_doc.get('name', '')}",
            "target_type": "buyer",
            "target_id": doc.id,
            "customer_name": updated_doc.get("name", ""),
            "phone": updated_doc.get("phone", ""),
            "parsed_tag": "新增客需",
        }

    if len(matches) > 1:
        return {
            "handled": True,
            "ok": False,
            "reply_text": "未寫入：同電話有多位買方，請先到後台整理或改用客戶ID",
        }

    now = now_taipei().isoformat()
    payload.update({
        "created_at": now,
        "created_by_id": "line_bot",
        "created_by_name": "LINE Bot",
        "note": append_note_block("", note_content, build_line_operator_label(event)),
    })
    doc_ref = db.collection("buyers").document()
    doc_ref.set(payload)
    add_customer_followup(
        target_type="buyer",
        customer_id=doc_ref.id,
        content=note_content,
        labels=labels,
        line_event=event,
    )
    return {
        "handled": True,
        "ok": True,
        "reply_text": f"已註記客需：{payload['name']}（{'租' if intent_type == 'rent' else '買賣'}）",
        "target_type": "buyer",
        "target_id": doc_ref.id,
        "customer_name": payload["name"],
        "phone": payload["phone"],
        "parsed_tag": "新增客需",
    }


def create_seller_listing(fields, event):
    phone = fields.get("phone", "").strip()
    matches = find_records_by_phone("sellers", phone)

    deal_type = normalize_deal_type(fields.get("deal_type_raw", "") or fields.get("deal_type", ""))
    if not deal_type:
        return {
            "handled": True,
            "ok": False,
            "reply_text": "未寫入：#新增委託 請填 委託類型: 賣 或 出租",
        }

    labels = build_seller_labels(deal_type, fields.get("labels"))
    note_content = build_line_summary(fields.get("content", "").strip() or fields.get("address", "").strip() or "LINE 新增委託", event)

    payload = {
        "name": fields.get("name", "").strip(),
        "phone": phone,
        "source": fields.get("source", "").strip(),
        "address": fields.get("address", "").strip(),
        "property_type": fields.get("property_type", "").strip(),
        "stage": fields.get("stage", "").strip() or "委託中",
        "deal_type": deal_type,
        "expected_price": fields.get("expected_price", "").strip() or fields.get("price", "").strip(),
        "min_price": fields.get("min_price", "").strip(),
        "contract_end_date": fields.get("contract_end_date", "").strip(),
        "labels": labels,
        "updated_at": now_taipei().isoformat(),
        "updated_by_id": "line_bot",
        "updated_by_name": "LINE Bot",
    }

    if len(matches) == 1:
        doc = matches[0]
        doc_ref = db.collection("sellers").document(doc.id)
        update_customer_note_and_labels(
            target_type="seller",
            doc_ref=doc_ref,
            content=note_content,
            labels=labels,
            stage=payload["stage"],
            source=payload["source"],
            event=event,
        )
        doc_ref.update(payload)
        add_customer_followup(
            target_type="seller",
            customer_id=doc.id,
            content=note_content,
            labels=labels,
            line_event=event,
        )
        updated_doc = doc_ref.get().to_dict() or {}
        return {
            "handled": True,
            "ok": True,
            "reply_text": f"已註記委託：{updated_doc.get('name', '')}",
            "target_type": "seller",
            "target_id": doc.id,
            "customer_name": updated_doc.get("name", ""),
            "phone": updated_doc.get("phone", ""),
            "parsed_tag": "新增委託",
        }

    if len(matches) > 1:
        return {
            "handled": True,
            "ok": False,
            "reply_text": "未寫入：同電話有多位委託，請先到後台整理或改用客戶ID",
        }

    now = now_taipei().isoformat()
    payload.update({
        "created_at": now,
        "created_by_id": "line_bot",
        "created_by_name": "LINE Bot",
        "note": append_note_block("", note_content, build_line_operator_label(event)),
    })
    doc_ref = db.collection("sellers").document()
    doc_ref.set(payload)
    add_customer_followup(
        target_type="seller",
        customer_id=doc_ref.id,
        content=note_content,
        labels=labels,
        line_event=event,
    )
    return {
        "handled": True,
        "ok": True,
        "reply_text": f"已註記委託：{payload['name']}（{'出租' if deal_type == 'rent' else '買賣'}）",
        "target_type": "seller",
        "target_id": doc_ref.id,
        "customer_name": payload["name"],
        "phone": payload["phone"],
        "parsed_tag": "新增委託",
    }


def format_record_timeline(target_type: str, doc_snapshot, limit=10):
    data = doc_snapshot.to_dict() or {}
    record_id = doc_snapshot.id

    followup_collection = "buyer_followups" if target_type == "buyer" else "seller_followups"
    key_name = "buyer_id" if target_type == "buyer" else "seller_id"

    followups = []
    for d in db.collection(followup_collection).where(key_name, "==", record_id).stream():
        item = d.to_dict() or {}
        followups.append({
            "time": item.get("contact_time") or item.get("created_at") or "",
            "channel": item.get("channel", "LINE"),
            "text": (item.get("content") or "").strip(),
            "created_by_name": item.get("created_by_name", "") or "",
            "sender_display_name": item.get("sender_display_name", "") or "",
        })

    followups = [x for x in followups if x.get("text")]
    followups.sort(key=lambda x: x.get("time", ""), reverse=True)

    lines = ["客戶資訊"]

    if target_type == "buyer":
        intent_map = {
            "rent": "租屋",
            "buy": "買賣",
            "both": "租買皆可",
        }
        lines.extend([
            f"姓名: {data.get('name', '')}",
            f"電話: {data.get('phone', '')}",
            f"客源來源: {data.get('source', '') or '-'}",
            f"需求類型: {intent_map.get(data.get('intent_type', ''), data.get('intent_type', '') or '-')}",
            f"預算: {data.get('budget_min', '') or data.get('rent_min', '') or '-'} ~ {data.get('budget_max', '') or data.get('rent_max', '') or '-'}",
            f"偏好區域: {data.get('preferred_areas', '') or '-'}",
            f"產品類型: {data.get('property_type', '') or '-'}",
            f"房數需求: {data.get('room_range', '') or '-'}",
            f"車位需求: {data.get('car_need', '') or '-'}",
        ])
    else:
        deal_map = {
            "sale": "買賣",
            "rent": "出租",
        }
        lines.extend([
            f"姓名: {data.get('name', '')}",
            f"電話: {data.get('phone', '')}",
            f"客源來源: {data.get('source', '') or '-'}",
            f"委託類型: {deal_map.get(data.get('deal_type', ''), data.get('deal_type', '') or '-')}",
            f"地址: {data.get('address', '') or '-'}",
            f"產品類型: {data.get('property_type', '') or '-'}",
            f"開價: {data.get('expected_price', '') or '-'}",
            f"底價: {data.get('min_price', '') or '-'}",
            f"委託到期日: {data.get('contract_end_date', '') or '-'}",
        ])

    lines.append("")
    lines.append("追蹤進度")

    if not followups:
        lines.append("目前沒有追蹤紀錄")
    else:
        for item in followups[:limit]:
            header_parts = [item.get('time', ''), item.get('channel', 'LINE')]
            creator = (item.get("created_by_name") or "").strip()
            sender = (item.get("sender_display_name") or "").strip()
            if creator:
                header_parts.append(f"KEYIN: {creator}")
            if sender:
                header_parts.append(f"留言者: {sender}")
            lines.append("｜".join([x for x in header_parts if x]))
            lines.append(item.get("text", ""))
            lines.append("")

    output = "\n".join(lines).strip()
    return output[:4500]


def query_contract_end_text(fields):
    target_type, doc = resolve_customer_record(fields, preferred_target_type="seller")
    if target_type != "seller" or not doc:
        return False, "查無唯一委託資料，請提供正確電話或客戶ID", None

    data = doc.to_dict() or {}
    deal_type = data.get("deal_type", "")
    deal_label = "出租" if deal_type == "rent" else "買賣" if deal_type == "sale" else "未設定"
    text = "\n".join([
        "查詢委託到期",
        f"姓名: {data.get('name', '')}",
        f"電話: {data.get('phone', '')}",
        f"地址: {data.get('address', '')}",
        f"委託類型: {deal_label}",
        f"委託到期日: {data.get('contract_end_date', '') or '未填寫'}",
        f"目前進程: {data.get('stage', '') or '未填寫'}",
    ]).strip()
    return True, text[:4500], {
        "target_type": "seller",
        "target_id": doc.id,
        "customer_name": data.get("name", ""),
        "phone": data.get("phone", ""),
        "parsed_tag": "查詢委託到期",
    }


def process_quote_context_message(event):
    message = event.get("message") or {}
    quoted_message_id = message.get("quotedMessageId", "")
    raw_text = (message.get("text") or "").strip()
    if not quoted_message_id or not raw_text:
        return {"handled": False}

    link = get_line_message_link(quoted_message_id)
    if not link:
        return {"handled": False}

    target_type = link.get("target_type", "")
    target_id = link.get("target_id", "")
    if target_type not in ("buyer", "seller") or not target_id:
        return {"handled": False}

    collection_name = "buyers" if target_type == "buyer" else "sellers"
    doc_ref = db.collection(collection_name).document(target_id)
    doc = doc_ref.get()
    if not doc.exists:
        return {"handled": True, "ok": False, "reply_text": "未寫入：引用的客戶資料不存在"}

    labels = dedupe_keep_order(["LINE紀錄", "群組回覆註記"])
    # 引用/回覆型註記只記錄「新的回覆內容」；
    # 發話者姓名會由 note 的 source_label 與 followup 的 sender_display_name 另行保留。
    reply_only_text = raw_text

    update_customer_note_and_labels(
        target_type=target_type,
        doc_ref=doc_ref,
        content=reply_only_text,
        labels=labels,
        source="LINE",
        event=event,
    )
    add_customer_followup(
        target_type=target_type,
        customer_id=target_id,
        content=reply_only_text,
        labels=labels,
        line_event=event,
    )

    parsed = {
        "tag": "群組回覆註記",
        "action": "quoted_context_note",
        "fields": {"quoted_message_id": quoted_message_id},
        "raw_text": raw_text,
    }
    save_line_log(parsed, event, "success", target_type=target_type, target_id=target_id, sender_display_name=get_line_sender_display_name(event))
    return {
        "handled": True,
        "ok": True,
        "reply_text": f"已註記到{'客需' if target_type == 'buyer' else '委託'}：{(doc.to_dict() or {}).get('name', '')}",
        "target_type": target_type,
        "target_id": target_id,
        "customer_name": (doc.to_dict() or {}).get("name", ""),
        "phone": (doc.to_dict() or {}).get("phone", ""),
        "parsed_tag": "群組回覆註記",
    }


def process_line_message_event(event):
    message = event.get("message") or {}
    if message.get("type") != "text":
        return {"handled": False}

    sender_display_name = get_line_sender_display_name(event)

    parsed = parse_line_formatted_message(message.get("text", ""))
    if not parsed:
        quoted_result = process_quote_context_message(event)
        if quoted_result.get("handled"):
            return quoted_result
        return {"handled": False}

    fields = parsed["fields"]
    action = parsed["action"]

    if action == "create_buyer_need":
        result = create_buyer_need(fields, event)
    elif action == "create_seller_listing":
        result = create_seller_listing(fields, event)
    elif action == "query_records":
        target_type, doc = resolve_customer_record(fields)
        if not doc:
            result = {"handled": True, "ok": False, "reply_text": "查無唯一客戶，請補電話或客戶ID"}
        else:
            result = {
                "handled": True,
                "ok": True,
                "reply_text": format_record_timeline(target_type, doc, limit=fields.get("limit", 10)),
                "target_type": target_type,
                "target_id": doc.id,
                "customer_name": (doc.to_dict() or {}).get("name", ""),
                "phone": (doc.to_dict() or {}).get("phone", ""),
                "parsed_tag": parsed.get("tag", ""),
            }
    elif action == "query_contract_end":
        ok, text, ctx = query_contract_end_text(fields)
        result = {"handled": True, "ok": ok, "reply_text": text}
        if ctx:
            result.update(ctx)
    else:
        target_type = fields.get("target_type", "")
        if action == "buyer_followup":
            target_type = "buyer"
        elif action == "seller_followup":
            target_type = "seller"

        if target_type not in ("buyer", "seller"):
            result = {"handled": True, "ok": False, "reply_text": "請提供對象：買方 或 賣方"}
        else:
            doc = find_customer_record(
                target_type=target_type,
                record_id=fields.get("record_id", ""),
                phone=fields.get("phone", ""),
                name=fields.get("name", ""),
            )
            if not doc:
                result = {"handled": True, "ok": False, "reply_text": "找不到唯一客戶，請補客戶ID或正確電話"}
            else:
                doc_ref = db.collection("buyers" if target_type == "buyer" else "sellers").document(doc.id)
                labels = dedupe_keep_order(["LINE紀錄"] + ensure_list(fields.get("labels")))
                summary_parts = []
                if fields.get("content"):
                    summary_parts.append(fields["content"])
                if fields.get("address"):
                    summary_parts.append(f"地址/物件：{fields['address']}")
                if fields.get("price"):
                    summary_parts.append(f"價格：{fields['price']}")
                summary_text = build_line_summary("；".join(summary_parts).strip() or "LINE 更新", event)

                update_customer_note_and_labels(
                    target_type=target_type,
                    doc_ref=doc_ref,
                    content=summary_text,
                    labels=labels,
                    stage=fields.get("stage", ""),
                    source=fields.get("source", "LINE"),
                    event=event,
                )

                if action in ("buyer_followup", "seller_followup", "classify"):
                    add_customer_followup(
                        target_type=target_type,
                        customer_id=doc.id,
                        content=summary_text,
                        next_action=fields.get("next_action", ""),
                        next_contact_date=fields.get("next_contact_date", ""),
                        labels=labels,
                        line_event=event,
                    )

                current_data = doc_ref.get().to_dict() or {}
                result = {
                    "handled": True,
                    "ok": True,
                    "reply_text": f"已寫入{'買方' if target_type == 'buyer' else '賣方'}：{current_data.get('name', '')}",
                    "target_type": target_type,
                    "target_id": doc.id,
                    "customer_name": current_data.get("name", ""),
                    "phone": current_data.get("phone", ""),
                    "parsed_tag": parsed.get("tag", ""),
                }

    # 寫 line_logs
    save_line_log(
        parsed,
        event,
        "success" if result.get("ok") else "failed",
        target_type=result.get("target_type", ""),
        target_id=result.get("target_id", ""),
        note=result.get("reply_text", ""),
        sender_display_name=sender_display_name,
    )

    # 將使用者原始訊息與客戶關聯起來，方便之後引用回覆
    if result.get("ok") and result.get("target_type") and result.get("target_id"):
        incoming_message_id = message.get("id", "")
        save_line_message_link(
            incoming_message_id,
            result["target_type"],
            result["target_id"],
            tag=result.get("parsed_tag", ""),
            action=action,
            customer_name=result.get("customer_name", ""),
            phone=result.get("phone", ""),
            source_event=event,
        )

    return result


def build_label_options(*doc_lists):
    label_set = set(DEFAULT_LABEL_OPTIONS)
    for docs in doc_lists:
        for item in docs:
            for label in ensure_list(item.get("labels")):
                label_set.add(label)
    return sorted(label_set)


BUYER_STAGE_OPTIONS = [
    "待聯繫",
    "已聯繫",
    "帶看中",
    "持續追蹤",
    "成交",
    "無效",
]

SELLER_STAGE_OPTIONS = [
    "待聯繫",
    "已聯繫",
    "持續追蹤",
    "委託中",
    "成交",
    "無效",
]

def attach_latest_followup(records, followup_collection, key_name):
    followups = [doc_to_dict(d) for d in db.collection(followup_collection).stream()]
    latest_map = {}
    for f in followups:
        rid = f.get(key_name)
        if not rid:
            continue
        ts = f.get("contact_time") or f.get("created_at") or ""
        prev = latest_map.get(rid)
        prev_ts = (prev or {}).get("contact_time") or (prev or {}).get("created_at") or ""
        if (not prev) or ts > prev_ts:
            latest_map[rid] = f
    for item in records:
        item["latest_followup"] = latest_map.get(item.get("id"))
    return records







# ========= 登入保護 =========
def login_required(view_func):
    from functools import wraps

    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            flash("請先登入", "warning")
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapper


# ========= 登入 / 登出 =========
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            flash("請輸入帳號與密碼", "danger")
            return redirect(url_for("login"))

        users_ref = db.collection("users").where("email", "==", email).limit(1)
        docs = list(users_ref.stream())

        if not docs:
            flash("帳號或密碼錯誤", "danger")
            return redirect(url_for("login"))

        user_doc = docs[0]
        user = user_doc.to_dict()

        if not check_password_hash(user.get("password_hash", ""), password):
            flash("帳號或密碼錯誤", "danger")
            return redirect(url_for("login"))

        session["user_id"] = user_doc.id
        session["user_name"] = user.get("name") or user.get("email")
        session["user_email"] = user.get("email")

        flash(f"歡迎回來，{session['user_name']}！", "success")
        return redirect(url_for("buyers"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("已登出", "info")
    return redirect(url_for("login"))


# ========= 首頁 =========
@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("buyers"))
    return redirect(url_for("login"))


# ========= 買方列表 + 篩選 / 排序 =========
# ========= 買方列表 =========
@app.route("/buyers")
@login_required
def buyers():
    q = request.args.get("q", "").strip()
    level = request.args.get("level", "").strip()
    intent_type = request.args.get("intent_type", "").strip()
    stage = request.args.get("stage", "").strip()
    source = request.args.get("source", "").strip()
    label = request.args.get("label", "").strip()
    sort_by = request.args.get("sort_by", "created_at_desc")

    docs = db.collection("buyers").stream()
    all_buyers = [doc_to_dict(d) for d in docs]

    source_set = set()
    for b in all_buyers:
        s = (b.get("source") or "").strip()
        if s:
            source_set.add(s)
    source_options = sorted(source_set)
    label_options = build_label_options(all_buyers)

    buyers_list = list(all_buyers)

    if q:
        buyers_list = [
            b for b in buyers_list
            if q in (b.get("name") or "") or q in (b.get("phone") or "")
        ]

    if level:
        buyers_list = [b for b in buyers_list if b.get("level") == level]

    if intent_type:
        buyers_list = [b for b in buyers_list if b.get("intent_type") == intent_type]

    if stage:
        buyers_list = [b for b in buyers_list if (b.get("stage") or "") == stage]

    if source:
        buyers_list = [b for b in buyers_list if (b.get("source") or "") == source]

    if label:
        buyers_list = [b for b in buyers_list if label in ensure_list(b.get("labels"))]

    def parse_created_at(b):
        return b.get("created_at") or ""

    if sort_by == "created_at_asc":
        buyers_list.sort(key=parse_created_at)
    elif sort_by == "created_at_desc":
        buyers_list.sort(key=parse_created_at, reverse=True)
    elif sort_by == "name_asc":
        buyers_list.sort(key=lambda b: (b.get("name") or ""))
    elif sort_by == "name_desc":
        buyers_list.sort(key=lambda b: (b.get("name") or ""), reverse=True)

    buyers_list = attach_latest_followup(buyers_list, "buyer_followups", "buyer_id")

    return render_template(
        "buyers.html",
        buyers=buyers_list,
        q=q,
        level=level,
        intent_type=intent_type,
        stage=stage,
        source=source,
        source_options=source_options,
        label=label,
        label_options=label_options,
        sort_by=sort_by,
        buyer_stage_options=BUYER_STAGE_OPTIONS,
        total_count=len(all_buyers),
        filtered_count=len(buyers_list),
    )


# ========= 新增買方 =========
@app.route("/buyers/new", methods=["POST"])
@login_required
def buyers_new():
    form = request.form
    file = request.files.get("photo")   # ⭐ 新增：抓圖片

    name = form.get("name", "").strip()
    phone = form.get("phone", "").strip()
    email = form.get("email", "").strip()
    line_id = form.get("line_id", "").strip()
    source = form.get("source", "").strip()
    level = form.get("level", "").strip()
    intent_type = form.get("intent_type", "").strip()
    rent_min = form.get("rent_min", "").strip()
    rent_max = form.get("rent_max", "").strip()
    budget_min = form.get("budget_min", "").strip()
    budget_max = form.get("budget_max", "").strip()
    preferred_areas = form.get("preferred_areas", "").strip()
    property_type = form.get("property_type", "").strip()
    room_range = form.get("room_range", "").strip()
    car_need = form.get("car_need", "").strip()
    job = form.get("job", "").strip()
    family_info = form.get("family_info", "").strip()
    requirement_must = form.get("requirement_must", "").strip()
    requirement_nice = form.get("requirement_nice", "").strip()
    other_background = form.get("other_background", "").strip()
    note = form.get("note", "").strip()
    labels = get_request_labels(form)

    if not name:
        flash("買方姓名必填", "danger")
        return redirect(url_for("buyers"))

    now = now_taipei().isoformat()

    # ⭐ 先建立一個空的 document，拿到 id
    doc_ref = db.collection("buyers").document()
    buyer_id = doc_ref.id

    # ⭐ 如果有上傳圖片，就丟到 Storage
    photo_url = None
    if file and file.filename:
        photo_url = upload_image_to_storage(file, folder="buyers", object_id=buyer_id)

    data = {
        "name": name,
        "phone": phone,
        "email": email,
        "line_id": line_id,
        "source": source,
        "level": level,
        "intent_type": intent_type,
        "rent_min": rent_min,
        "rent_max": rent_max,
        "budget_min": budget_min,
        "budget_max": budget_max,
        "preferred_areas": preferred_areas,
        "property_type": property_type,
        "room_range": room_range,
        "car_need": car_need,
        "job": job,
        "family_info": family_info,
        "requirement_must": requirement_must,
        "requirement_nice": requirement_nice,
        "other_background": other_background,
        "note": note,
        "labels": labels,
        "created_at": now,
        "created_by_id": session.get("user_id"),
        "created_by_name": session.get("user_name"),
    }

    if photo_url:
        data["photo_url"] = photo_url   # ⭐ 存圖片網址

    doc_ref.set(data)

    flash("已新增買方", "success")
    return redirect(url_for("buyers"))


# ========= 買方詳細 =========
@app.route("/buyers/<buyer_id>")
@login_required
def buyer_detail(buyer_id):
    doc = db.collection("buyers").document(buyer_id).get()
    if not doc.exists:
        flash("找不到這位買方", "danger")
        return redirect(url_for("buyers"))

    buyer = doc_to_dict(doc)

    # 追蹤紀錄
    followups_ref = db.collection("buyer_followups").where("buyer_id", "==", buyer_id)
    followups = [doc_to_dict(f) for f in followups_ref.stream()]

    # 依 contact_time 排序（新到舊）
    followups.sort(key=lambda x: x.get("contact_time", ""), reverse=True)

    return render_template("buyer_detail.html", buyer=buyer, followups=followups)

@app.route("/buyers/<buyer_id>/quick-stage", methods=["POST"])
@login_required
def buyer_quick_stage(buyer_id):
    stage = (request.form.get("stage", "") or request.form.get("current_stage", "")).strip()
    if not stage:
        flash("請選擇進度", "warning")
        return redirect(request.referrer or url_for("buyers"))
    if stage not in BUYER_STAGE_OPTIONS:
        flash("買方進度不在可選清單內", "danger")
        return redirect(request.referrer or url_for("buyers"))
    updates = {
        "stage": stage,
        "updated_at": now_taipei().isoformat(),
        "updated_by_id": session.get("user_id"),
        "updated_by_name": session.get("user_name"),
    }
    db.collection("buyers").document(buyer_id).update(updates)
    flash("已更新買方進度", "success")
    return redirect(request.referrer or url_for("buyers"))


# ========= 新增買方追蹤紀錄 =========
@app.route("/buyers/<buyer_id>/followup", methods=["POST"])
@login_required
def add_buyer_followup(buyer_id):
    contact_time = request.form.get("contact_time", "").strip()
    channel = request.form.get("channel", "").strip()
    content = request.form.get("content", "").strip()
    next_action = request.form.get("next_action", "").strip()
    next_contact_date = request.form.get("next_contact_date", "").strip()

    if not contact_time:
        contact_time = now_taipei().strftime("%Y-%m-%d %H:%M")

    now = now_taipei().isoformat()

    db.collection("buyer_followups").add(
        {
            "buyer_id": buyer_id,
            "contact_time": contact_time,
            "channel": channel,
            "content": content,
            "next_action": next_action,
            "next_contact_date": next_contact_date,
            "created_at": now,
            "created_by_id": session.get("user_id"),
            "created_by_name": session.get("user_name"),
        }
    )

    flash("已新增追蹤紀錄", "success")
    return redirect(url_for("buyer_detail", buyer_id=buyer_id))


# ========= 編輯買方 =========
@app.route("/buyers/<buyer_id>/edit", methods=["GET", "POST"])
@login_required
def buyer_edit(buyer_id):
    # 先取得該買方文件
    doc_ref = db.collection("buyers").document(buyer_id)
    doc = doc_ref.get()

    if not doc.exists:
        flash("找不到這位買方", "danger")
        return redirect(url_for("buyers"))

    buyer = doc_to_dict(doc)

    # 處理送出編輯表單（POST）
    if request.method == "POST":
        form = request.form

        # ⭐ 必填姓名檢查
        name = form.get("name", "").strip()
        if not name:
            flash("姓名為必填", "danger")
            # 更新 buyer 物件，讓表單保留剛剛輸入的東西
            buyer.update({
                "name": name,
                "phone": form.get("phone", "").strip(),
                "email": form.get("email", "").strip(),
                "line_id": form.get("line_id", "").strip(),
                "source": form.get("source", "").strip(),
                "level": form.get("level", "").strip(),
                "intent_type": form.get("intent_type", "").strip(),
                "rent_min": form.get("rent_min", "").strip(),
                "rent_max": form.get("rent_max", "").strip(),
                "budget_min": form.get("budget_min", "").strip(),
                "budget_max": form.get("budget_max", "").strip(),
                "preferred_areas": form.get("preferred_areas", "").strip(),
                "property_type": form.get("property_type", "").strip(),
                "room_range": form.get("room_range", "").strip(),
                "car_need": form.get("car_need", "").strip(),
                "job": form.get("job", "").strip(),
                "family_info": form.get("family_info", "").strip(),
                "requirement_must": form.get("requirement_must", "").strip(),
                "requirement_nice": form.get("requirement_nice", "").strip(),
                "other_background": form.get("other_background", "").strip(),
                "note": form.get("note", "").strip(),
                "stage": form.get("stage", "").strip(),
                "labels": get_request_labels(form),
            })
            return render_template("buyer_edit.html", buyer=buyer)

        # ✅ 先處理一般文字欄位
        labels = get_request_labels(form)

        updated = {
            "name": name,
            "phone": form.get("phone", "").strip(),
            "email": form.get("email", "").strip(),
            "line_id": form.get("line_id", "").strip(),
            "source": form.get("source", "").strip(),
            "level": form.get("level", "").strip(),
            "intent_type": form.get("intent_type", "").strip(),
            "rent_min": form.get("rent_min", "").strip(),
            "rent_max": form.get("rent_max", "").strip(),
            "budget_min": form.get("budget_min", "").strip(),
            "budget_max": form.get("budget_max", "").strip(),
            "preferred_areas": form.get("preferred_areas", "").strip(),
            "property_type": form.get("property_type", "").strip(),
            "room_range": form.get("room_range", "").strip(),
            "car_need": form.get("car_need", "").strip(),
            "job": form.get("job", "").strip(),
            "family_info": form.get("family_info", "").strip(),
            "requirement_must": form.get("requirement_must", "").strip(),
            "requirement_nice": form.get("requirement_nice", "").strip(),
            "other_background": form.get("other_background", "").strip(),
            "note": form.get("note", "").strip(),
            "labels": labels,
            "stage": form.get("stage", "").strip(),  # 接觸/帶看/斡旋/成交
            "updated_at": now_taipei().isoformat(),
            "updated_by_id": session.get("user_id"),
            "updated_by_name": session.get("user_name"),
        }

        # ====== 圖片處理：多張刪除 + 多張新增 ======

        # 現在 Firestore 中的圖片列表：優先用 photo_urls，舊資料就用 photo_url
        current_photos = buyer.get("photo_urls") or []
        if not current_photos and buyer.get("photo_url"):
            current_photos = [buyer["photo_url"]]

        # 1️⃣ 要刪除的 index（來自 checkbox name="delete_photos"）
        delete_indexes_raw = form.getlist("delete_photos")
        delete_indexes = set()
        for idx in delete_indexes_raw:
            try:
                delete_indexes.add(int(idx))
            except ValueError:
                pass

        # 🔥 先記錄「要被刪除的 URL」（拿來刪 Storage）
        deleted_urls = [
            url for i, url in enumerate(current_photos)
            if i in delete_indexes
        ]

        # 保留沒勾選刪除的圖片
        new_photos = [
            url for i, url in enumerate(current_photos)
            if i not in delete_indexes
        ]

        # 2️⃣ 多張上傳：input name="photos" multiple
        files = request.files.getlist("photos")
        for f in files:
            if f and f.filename:
                photo_url = upload_image_to_storage(f, folder="buyers", object_id=buyer_id)
                if photo_url:
                    new_photos.append(photo_url)

        # 3️⃣ 寫回 Firestore：主要用 photo_urls，photo_url 當第一張給舊版用
        updated["photo_urls"] = new_photos
        if new_photos:
            updated["photo_url"] = new_photos[0]
        else:
            updated["photo_url"] = ""

        # ✅ 先更新 Firestore
        doc_ref.update(updated)

        # ✅ 再刪除 Firebase Storage 檔案
        if deleted_urls:
            delete_storage_files(deleted_urls)

        flash("已更新買方資料", "success")
        return redirect(url_for("buyer_detail", buyer_id=buyer_id))

    # GET：第一次進來編輯頁
    return render_template("buyer_edit.html", buyer=buyer)

# ========= 刪除買方（含追蹤） =========
@app.route("/buyers/<buyer_id>/delete", methods=["POST"])
@login_required
def buyer_delete(buyer_id):
    # 先刪除追蹤紀錄
    delete_by_field("buyer_followups", "buyer_id", buyer_id)
    # 再刪除買方本身
    db.collection("buyers").document(buyer_id).delete()

    flash("已刪除買方與相關追蹤紀錄", "info")
    return redirect(url_for("buyers"))


# ========= 買方追蹤紀錄：編輯 =========
@app.route("/buyers/<buyer_id>/followup/<followup_id>/edit", methods=["GET", "POST"])
@login_required
def buyer_followup_edit(buyer_id, followup_id):
    doc_ref = db.collection("buyer_followups").document(followup_id)
    doc = doc_ref.get()
    if not doc.exists:
        flash("找不到這筆追蹤紀錄", "danger")
        return redirect(url_for("buyer_detail", buyer_id=buyer_id))

    followup = doc_to_dict(doc)

    if request.method == "POST":
        contact_time = request.form.get("contact_time", "").strip()
        channel = request.form.get("channel", "").strip()
        content = request.form.get("content", "").strip()
        next_action = request.form.get("next_action", "").strip()
        next_contact_date = request.form.get("next_contact_date", "").strip()

        if not contact_time:
            contact_time = now_taipei().strftime("%Y-%m-%d %H:%M")

        doc_ref.update(
            {
                "contact_time": contact_time,
                "channel": channel,
                "content": content,
                "next_action": next_action,
                "next_contact_date": next_contact_date,
            }
        )

        flash("已更新追蹤紀錄", "success")
        return redirect(url_for("buyer_detail", buyer_id=buyer_id))

    return render_template("buyer_followup_edit.html", buyer_id=buyer_id, followup=followup)


# ========= 買方追蹤紀錄：刪除 =========
@app.route("/buyers/<buyer_id>/followup/<followup_id>/delete", methods=["POST"])
@login_required
def buyer_followup_delete(buyer_id, followup_id):
    db.collection("buyer_followups").document(followup_id).delete()
    flash("已刪除追蹤紀錄", "info")
    return redirect(url_for("buyer_detail", buyer_id=buyer_id))


# ========= 賣方列表 + 篩選 / 排序 =========
# ========= 賣方列表 =========
@app.route("/sellers")
@login_required
def sellers():
    q = request.args.get("q", "").strip()
    level = request.args.get("level", "").strip()
    stage = request.args.get("stage", "").strip()
    source = request.args.get("source", "").strip()
    label = request.args.get("label", "").strip()
    sort_by = request.args.get("sort_by", "created_at_desc")

    docs = db.collection("sellers").stream()
    all_sellers = [doc_to_dict(d) for d in docs]

    source_set = set()
    for s in all_sellers:
        val = (s.get("source") or "").strip()
        if val:
            source_set.add(val)
    source_options = sorted(source_set)
    label_options = build_label_options(all_sellers)

    sellers_list = list(all_sellers)

    if q:
        sellers_list = [
            s for s in sellers_list
            if q in (s.get("name") or "") or q in (s.get("phone") or "")
        ]

    if level:
        sellers_list = [s for s in sellers_list if s.get("level") == level]

    if stage:
        sellers_list = [s for s in sellers_list if (s.get("stage") or "") == stage]

    if source:
        sellers_list = [s for s in sellers_list if (s.get("source") or "") == source]

    if label:
        sellers_list = [s for s in sellers_list if label in ensure_list(s.get("labels"))]

    def parse_created_at(s):
        return s.get("created_at") or ""

    if sort_by == "created_at_asc":
        sellers_list.sort(key=parse_created_at)
    elif sort_by == "created_at_desc":
        sellers_list.sort(key=parse_created_at, reverse=True)
    elif sort_by == "name_asc":
        sellers_list.sort(key=lambda s: (s.get("name") or ""))
    elif sort_by == "name_desc":
        sellers_list.sort(key=lambda s: (s.get("name") or ""), reverse=True)

    sellers_list = attach_latest_followup(sellers_list, "seller_followups", "seller_id")

    return render_template(
        "sellers.html",
        sellers=sellers_list,
        q=q,
        level=level,
        stage=stage,
        source=source,
        source_options=source_options,
        label=label,
        label_options=label_options,
        sort_by=sort_by,
        seller_stage_options=SELLER_STAGE_OPTIONS,
        total_count=len(all_sellers),
        filtered_count=len(sellers_list),
    )


# ========= 新增賣方 =========
@app.route("/sellers/new", methods=["POST"])
@login_required
def sellers_new():
    form = request.form

    name = form.get("name", "").strip()
    phone = form.get("phone", "").strip()
    email = form.get("email", "").strip()
    line_id = form.get("line_id", "").strip()
    address = form.get("address", "").strip()
    property_type = form.get("property_type", "").strip()
    level = form.get("level", "").strip()
    stage = form.get("stage", "").strip()   # 進程
    reason = form.get("reason", "").strip()
    expected_price = form.get("expected_price", "").strip()
    min_price = form.get("min_price", "").strip()
    timeline = form.get("timeline", "").strip()
    occupancy_status = form.get("occupancy_status", "").strip()
    contract_end_date = form.get("contract_end_date", "").strip()
    next_action = form.get("next_action", "").strip()
    next_contact_date = form.get("next_contact_date", "").strip()
    note = form.get("note", "").strip()
    labels = get_request_labels(form)

    # ⭐ 加上“來源 source”
    source = form.get("source", "").strip()

    if not name:
        flash("賣方姓名必填", "danger")
        return redirect(url_for("sellers"))

    now = now_taipei().isoformat()

    # 先產生一個 document id → 用來放圖片
    sellers_collection = db.collection("sellers")
    doc_ref = sellers_collection.document()
    seller_id = doc_ref.id

    # ========== 圖片（多張上傳） ==========
    photo_urls = []
    files = request.files.getlist("photos")   # <input name="photos" multiple>

    for f in files:
        if f and f.filename:
            url = upload_image_to_storage(f, folder="sellers", object_id=seller_id)
            if url:
                photo_urls.append(url)


    # ========== Firestore 要存的資料 ==========
    data = {
        "name": name,
        "phone": phone,
        "email": email,
        "line_id": line_id,
        "address": address,
        "property_type": property_type,
        "level": level,
        "stage": stage,
        "reason": reason,
        "expected_price": expected_price,
        "min_price": min_price,
        "timeline": timeline,
        "occupancy_status": occupancy_status,
        "contract_end_date": contract_end_date,
        "next_action": next_action,
        "next_contact_date": next_contact_date,
        "note": note,
        "labels": labels,
        "source": source,               # ⭐ 加進 Firestore
        "created_at": now,
        "created_by_id": session.get("user_id"),
        "created_by_name": session.get("user_name"),
    }

    # 如果有圖片就寫入，沒有就給空
    if photo_urls:
        data["photo_urls"] = photo_urls
        data["photo_url"] = photo_urls[0]
    else:
        data["photo_urls"] = []
        data["photo_url"] = ""

    # 寫入 Firestore
    doc_ref.set(data)

    flash("已新增賣方", "success")
    return redirect(url_for("sellers"))





# ========= 賣方詳細 =========
@app.route("/sellers/<seller_id>")
@login_required
def seller_detail(seller_id):
    doc = db.collection("sellers").document(seller_id).get()
    if not doc.exists:
        flash("找不到這位賣方", "danger")
        return redirect(url_for("sellers"))

    seller = doc_to_dict(doc)

    followups_ref = db.collection("seller_followups").where("seller_id", "==", seller_id)
    followups = [doc_to_dict(f) for f in followups_ref.stream()]
    followups.sort(key=lambda x: x.get("contact_time", ""), reverse=True)

    return render_template("seller_detail.html", seller=seller, followups=followups)

@app.route("/sellers/<seller_id>/quick-stage", methods=["POST"])
@login_required
def seller_quick_stage(seller_id):
    stage = (request.form.get("stage", "") or request.form.get("current_stage", "")).strip()
    if not stage:
        flash("請選擇目前狀態", "warning")
        return redirect(request.referrer or url_for("sellers"))
    if stage not in SELLER_STAGE_OPTIONS:
        flash("委託狀態不在可選清單內", "danger")
        return redirect(request.referrer or url_for("sellers"))
    updates = {
        "stage": stage,
        "updated_at": now_taipei().isoformat(),
        "updated_by_id": session.get("user_id"),
        "updated_by_name": session.get("user_name"),
    }
    db.collection("sellers").document(seller_id).update(updates)
    flash("已更新委託狀態", "success")
    return redirect(request.referrer or url_for("sellers"))


# ========= 新增賣方追蹤紀錄 =========
@app.route("/sellers/<seller_id>/followup", methods=["POST"])
@login_required
def add_seller_followup(seller_id):
    contact_time = request.form.get("contact_time", "").strip()
    channel = request.form.get("channel", "").strip()
    content = request.form.get("content", "").strip()
    next_action = request.form.get("next_action", "").strip()
    next_contact_date = request.form.get("next_contact_date", "").strip()

    if not contact_time:
        contact_time = now_taipei().strftime("%Y-%m-%d %H:%M")

    now = now_taipei().isoformat()

    db.collection("seller_followups").add(
        {
            "seller_id": seller_id,
            "contact_time": contact_time,
            "channel": channel,
            "content": content,
            "next_action": next_action,
            "next_contact_date": next_contact_date,
            "created_at": now,
            "created_by_id": session.get("user_id"),
            "created_by_name": session.get("user_name"),
        }
    )

    flash("已新增追蹤紀錄", "success")
    return redirect(url_for("seller_detail", seller_id=seller_id))


# ========= 編輯賣方 =========
@app.route("/sellers/<seller_id>/edit", methods=["GET", "POST"])
@login_required
def seller_edit(seller_id):
    doc_ref = db.collection("sellers").document(seller_id)
    doc = doc_ref.get()
    if not doc.exists:
        flash("找不到這位賣方", "danger")
        return redirect(url_for("sellers"))

    seller = doc_to_dict(doc)

    if request.method == "POST":
        form = request.form

        labels = get_request_labels(form)

        updated = {
            "name": form.get("name", "").strip(),
            "phone": form.get("phone", "").strip(),
            "email": form.get("email", "").strip(),
            "line_id": form.get("line_id", "").strip(),
            "address": form.get("address", "").strip(),
            "property_type": form.get("property_type", "").strip(),
            "level": form.get("level", "").strip(),
            "stage": form.get("stage", "").strip(),  # 開發中 / 委託中 / 成交
            "reason": form.get("reason", "").strip(),
            "expected_price": form.get("expected_price", "").strip(),
            "min_price": form.get("min_price", "").strip(),
            "timeline": form.get("timeline", "").strip(),
            "occupancy_status": form.get("occupancy_status", "").strip(),
            "contract_end_date": form.get("contract_end_date", "").strip(),  # 委託到期日
            "note": form.get("note", "").strip(),
            "labels": labels,
            # ⭐ 新增：客源來源
            "source": form.get("source", "").strip(),
            "updated_at": now_taipei().isoformat(),
            "updated_by_id": session.get("user_id"),
            "updated_by_name": session.get("user_name"),
        }

        # ====== 圖片處理：多張刪除 + 多張新增 ======

        # 目前 Firestore 中的圖片列表（支援舊欄位 photo_url）
        current_photos = seller.get("photo_urls") or []
        if not current_photos and seller.get("photo_url"):
            current_photos = [seller["photo_url"]]

        # 1️⃣ 取得要刪除的 index（來自 checkbox）
        delete_indexes_raw = form.getlist("delete_photos")  # name="delete_photos"
        delete_indexes = set()
        for idx in delete_indexes_raw:
            try:
                delete_indexes.add(int(idx))
            except ValueError:
                pass

        # 把沒勾選的留下來
        new_photos = [
            url for i, url in enumerate(current_photos)
            if i not in delete_indexes
        ]

        # 2️⃣ 多張上傳：input name="photos" multiple
        files = request.files.getlist("photos")
        for f in files:
            if f and f.filename:
                photo_url = upload_image_to_storage(f, folder="sellers", object_id=seller_id)
                if photo_url:
                    new_photos.append(photo_url)

        # 3️⃣ 寫回 Firestore（主要用 photo_urls，photo_url 當第一張方便舊版使用）
        updated["photo_urls"] = new_photos
        if new_photos:
            updated["photo_url"] = new_photos[0]
        else:
            updated["photo_url"] = ""

        doc_ref.update(updated)
        flash("已更新賣方資料", "success")
        return redirect(url_for("seller_detail", seller_id=seller_id))

    # GET：首次載入編輯頁
    return render_template("seller_edit.html", seller=seller)


def delete_storage_file_by_url(url: str):
    """
    傳入 Firebase Storage 的檔案 URL（支援三種常見格式）：
    1) https://firebasestorage.googleapis.com/v0/b/<bucket>/o/<encoded_path>?...
    2) https://storage.googleapis.com/<bucket>/<path>
    3) gs://<bucket>/<path>
    自動解析出 bucket 與 blob path 並刪除。
    """
    if not url:
        return

    try:
        bucket = None
        blob_path = None

        # --- 格式 3：gs://bucket/path/to/file ---
        if url.startswith("gs://"):
            no_scheme = url[len("gs://"):]      # bucket/path/to/file
            parts = no_scheme.split("/", 1)
            bucket_name = parts[0]
            blob_path = parts[1] if len(parts) > 1 else ""
            bucket = storage.bucket(bucket_name)

        else:
            parsed = urlparse(url)
            netloc = parsed.netloc
            path = parsed.path  # e.g. /team-me-98acf.firebassestorage.app/sellers/xxx.jpg

            # --- 格式 1：firebasestorage.googleapis.com/v0/b/<bucket>/o/<encoded_path> ---
            if "firebasestorage.googleapis.com" in netloc and "/o/" in path:
                # 通常用預設 bucket 即可
                bucket = storage.bucket()
                encoded_blob_path = path.split("/o/", 1)[1]   # buyers%2Fabc%2Fxxx.jpg
                blob_path = unquote(encoded_blob_path)        # buyers/abc/xxx.jpg

            # --- 格式 2：storage.googleapis.com/<bucket>/<path> ---
            elif "storage.googleapis.com" in netloc:
                # path: /<bucket>/<blob_path>
                segments = path.lstrip("/").split("/", 1)
                if len(segments) == 2:
                    bucket_name, blob_path = segments
                    bucket = storage.bucket(bucket_name)

        if not bucket or not blob_path:
            print("⚠️ 無法解析 Storage URL：", url)
            return

        blob = bucket.blob(blob_path)
        if blob.exists():
            blob.delete()
            print(f"🔥 已刪除 Storage 檔案：{bucket.name}/{blob_path}")
        else:
            print(f"⚠️ 找不到 Storage 檔案：{bucket.name}/{blob_path}")

    except Exception as e:
        print("⚠️ 刪除 Storage 檔案發生錯誤：", e)


def delete_storage_files(urls: list):
    """一次刪多個 URL 對應的 Storage 檔案"""
    for url in urls:
        delete_storage_file_by_url(url)
# ========= 刪除賣方（含追蹤） =========
@app.route("/sellers/<seller_id>/delete", methods=["POST"])
@login_required
def seller_delete(seller_id):
    delete_by_field("seller_followups", "seller_id", seller_id)
    db.collection("sellers").document(seller_id).delete()

    flash("已刪除賣方與相關追蹤紀錄", "info")
    return redirect(url_for("sellers"))


# ========= 賣方追蹤紀錄：編輯 =========
@app.route("/sellers/<seller_id>/followup/<followup_id>/edit", methods=["GET", "POST"])
@login_required
def seller_followup_edit(seller_id, followup_id):
    doc_ref = db.collection("seller_followups").document(followup_id)
    doc = doc_ref.get()
    if not doc.exists:
        flash("找不到這筆追蹤紀錄", "danger")
        return redirect(url_for("seller_detail", seller_id=seller_id))

    followup = doc_to_dict(doc)

    if request.method == "POST":
        contact_time = request.form.get("contact_time", "").strip()
        channel = request.form.get("channel", "").strip()
        content = request.form.get("content", "").strip()
        next_action = request.form.get("next_action", "").strip()
        next_contact_date = request.form.get("next_contact_date", "").strip()

        if not contact_time:
            contact_time = now_taipei().strftime("%Y-%m-%d %H:%M")

        doc_ref.update(
            {
                "contact_time": contact_time,
                "channel": channel,
                "content": content,
                "next_action": next_action,
                "next_contact_date": next_contact_date,
            }
        )

        flash("已更新追蹤紀錄", "success")
        return redirect(url_for("seller_detail", seller_id=seller_id))

    return render_template("seller_followup_edit.html", seller_id=seller_id, followup=followup)


# ========= 賣方追蹤紀錄：刪除 =========
@app.route("/sellers/<seller_id>/followup/<followup_id>/delete", methods=["POST"])
@login_required
def seller_followup_delete(seller_id, followup_id):
    db.collection("seller_followups").document(followup_id).delete()
    flash("已刪除追蹤紀錄", "info")
    return redirect(url_for("seller_detail", seller_id=seller_id))




# ========= CSV：賣方 =========
@app.route("/sellers/download")
@login_required
def download_sellers():
    # 從 Firestore 抓全部賣方資料
    docs = db.collection("sellers").stream()
    sellers_list = [doc_to_dict(d) for d in docs]

    si = StringIO()
    writer = csv.writer(si)

    # 表頭（有進程 + 委託到期日）
    writer.writerow([
        "id",
        "姓名",
        "電話",
        "Email",
        "LINE ID",
        "物件地址",
        "產品類型",
        "客戶等級",
        "進程",              # 開發中 / 委託中 / 成交
        "出售原因",
        "期望售價(萬)",
        "可接受底價(萬)",
        "預計出售時程",
        "目前使用狀態",
        "委託到期日",
        "內部備註",
        "分類標籤",
        "建立時間",
        "建立者",
        "最後編輯時間",
        "最後編輯者",
    ])

    for s in sellers_list:
        writer.writerow([
            s.get("id", ""),
            s.get("name", ""),
            s.get("phone", ""),
            s.get("email", ""),
            s.get("line_id", ""),
            s.get("address", ""),
            s.get("property_type", ""),
            s.get("level", ""),
            s.get("stage", ""),                # 開發中 / 委託中 / 成交
            s.get("reason", ""),
            s.get("expected_price", ""),
            s.get("min_price", ""),
            s.get("timeline", ""),
            s.get("occupancy_status", ""),
            s.get("contract_end_date", ""),    # 委託到期日
            s.get("note", ""),
            "、".join(ensure_list(s.get("labels"))),
            s.get("created_at", ""),
            s.get("created_by_name", ""),
            s.get("updated_at", ""),
            s.get("updated_by_name", ""),
        ])

    csv_data = si.getvalue()
    csv_data = '\ufeff' + csv_data  # UTF-8 BOM

    filename = f"sellers.csv"
    response = Response(csv_data, mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response

# ========= CSV：買方 =========
@app.route("/buyers/download")
@login_required
def download_buyers():
    # 從 Firestore 抓全部買方資料
    docs = db.collection("buyers").stream()
    buyers_list = [doc_to_dict(d) for d in docs]

    # 用 StringIO 暫存 CSV 文字
    si = StringIO()
    writer = csv.writer(si)

    # 表頭（你可以自行調整順序 / 欄位）
    writer.writerow([
        "id",
        "姓名",
        "電話",
        "Email",
        "LINE ID",
        "客源來源",
        "客戶等級",
        "進程",          # 接觸 / 帶看 / 斡旋 / 成交
        "需求類型",      # 買房 / 租屋 / 租買皆可
        "預算最低(萬)",
        "預算最高(萬)",
        "租金最低",
        "租金最高",
        "偏好區域",
        "產品類型",
        "房數需求",
        "車位需求",
        "職業/收入",
        "家庭成員/生活型態",
        "必備條件(Must Have)",
        "加分條件(Nice to Have)",
        "背景補充",
        "內部備註",
        "分類標籤",
        "建立時間",
        "建立者",
        "最後編輯時間",
        "最後編輯者",
    ])

    for b in buyers_list:
        writer.writerow([
            b.get("id", ""),
            b.get("name", ""),
            b.get("phone", ""),
            b.get("email", ""),
            b.get("line_id", ""),
            b.get("source", ""),
            b.get("level", ""),
            b.get("stage", ""),               # 進程
            b.get("intent_type", ""),         # 原始值（buy/rent/both），你也可以改成中文後再匯出
            b.get("budget_min", ""),
            b.get("budget_max", ""),
            b.get("rent_min", ""),
            b.get("rent_max", ""),
            b.get("preferred_areas", ""),
            b.get("property_type", ""),
            b.get("room_range", ""),
            b.get("car_need", ""),
            b.get("job", ""),
            b.get("family_info", ""),
            b.get("requirement_must", ""),
            b.get("requirement_nice", ""),
            b.get("other_background", ""),
            b.get("note", ""),
            "、".join(ensure_list(b.get("labels"))),
            b.get("created_at", ""),
            b.get("created_by_name", ""),
            b.get("updated_at", ""),
            b.get("updated_by_name", ""),
        ])

    # 取出 CSV 字串，加上 BOM 讓 Excel 顯示中文不亂碼
    csv_data = si.getvalue()
    csv_data = '\ufeff' + csv_data  # UTF-8 BOM

    # 回傳 Response，讓瀏覽器下載
    filename = f"buyers.csv"
    response = Response(csv_data, mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response


# ========= LINE Webhook =========
@app.route("/line/ping")
def line_ping():
    return {"ok": True, "message": "line webhook ready"}, 200


@app.route("/line/webhook", methods=["POST"])
def line_webhook():
    raw_body = request.get_data(cache=False, as_text=False)
    signature = request.headers.get("x-line-signature", "")

    if not verify_line_signature(raw_body, signature):
        return "Invalid signature", 400

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as e:
        print("⚠️ LINE webhook JSON 解析失敗：", e)
        return "Bad Request", 400

    events = payload.get("events", [])
    for event in events:
        try:
            result = process_line_message_event(event)
            if not result or not result.get("handled"):
                continue

            if event.get("replyToken") and result.get("reply_text"):
                reply_result = reply_line_text(
                    event["replyToken"],
                    result["reply_text"] if result.get("ok") else result["reply_text"]
                )
                if result.get("ok") and result.get("target_type") and result.get("target_id") and reply_result:
                    for sent in reply_result.get("sent_messages", []):
                        sent_id = str(sent.get("id", "")).strip()
                        if sent_id:
                            save_line_message_link(
                                sent_id,
                                result["target_type"],
                                result["target_id"],
                                tag=result.get("parsed_tag", ""),
                                action="bot_reply",
                                customer_name=result.get("customer_name", ""),
                                phone=result.get("phone", ""),
                                source_event=event,
                            )
        except Exception as e:
            print("⚠️ 處理 LINE event 發生錯誤：", e)

    return "OK", 200

# ========= CLI：建立後台使用者 =========
@app.cli.command("create-user")
def create_user_cmd():
    """
    在命令列執行：
      flask --app team_me_firebase.py create-user
    然後依照提示輸入
    """
    import getpass

    email = input("Email: ").strip().lower()
    name = input("Name: ").strip()
    password = getpass.getpass("Password: ")

    if not email or not password:
        print("Email / Password 不可空白")
        return

    users_ref = db.collection("users").where("email", "==", email).limit(1)
    docs = list(users_ref.stream())
    if docs:
        print("此 Email 已存在")
        return

    pwd_hash = generate_password_hash(password)

    db.collection("users").add(
        {
            "email": email,
            "name": name or email,
            "password_hash": pwd_hash,
            "created_at": now_taipei().isoformat(),
        }
    )

    print("使用者建立完成")




# ========= 開發資料（developments） =========
def build_development_labels(extra=None):
    return dedupe_keep_order(["開發", "LINE紀錄"] + ensure_list(extra))


def normalize_line_key(key: str):
    k = (key or "").strip().replace(" ", "")
    mapping = {
        "對象": "target_type",
        "類型": "target_type",
        "目標": "target_type",
        "客戶類型": "target_type",
        "電話": "phone",
        "手機": "phone",
        "電話號碼": "phone",
        "姓名": "name",
        "客戶": "name",
        "買方": "name",
        "賣方": "name",
        "屋主": "name",
        "客戶來源": "source",
        "來源": "source",
        "需求類型": "intent_type_raw",
        "委託類型": "deal_type_raw",
        "ID": "record_id",
        "客戶ID": "record_id",
        "buyer_id": "record_id",
        "seller_id": "record_id",
        "development_id": "record_id",
        "內容": "content",
        "紀錄": "content",
        "備註": "content",
        "說明": "content",
        "進度內容": "content",
        "進程": "stage",
        "階段": "stage",
        "狀態": "stage",
        "標籤": "labels",
        "分類": "labels",
        "labels": "labels",
        "下一步": "next_action",
        "下次行動": "next_action",
        "next_action": "next_action",
        "下次聯絡日": "next_contact_date",
        "下次聯絡日期": "next_contact_date",
        "next_contact_date": "next_contact_date",
        "地址": "address",
        "物件": "address",
        "案名": "address",
        "總價": "price",
        "價格": "price",
        "預算": "budget",
        "區域": "preferred_areas",
        "產品類型": "property_type",
        "房數": "room_range",
        "車位": "car_need",
        "開價": "expected_price",
        "底價": "min_price",
        "委託到期日": "contract_end_date",
        "筆數": "limit",
        "網址": "url",
        "連結": "url",
        "網站": "url",
        "日期": "record_date",
    }
    return mapping.get(k, k)


def normalize_target_type(value: str) -> str:
    v = (value or "").strip()
    if v in ("買方", "客需", "buyer", "buyers"):
        return "buyer"
    if v in ("賣方", "委託", "seller", "sellers", "屋主"):
        return "seller"
    if v in ("開發", "development", "developments", "名單", "開發名單"):
        return "development"
    return ""


def parse_line_formatted_message(text: str):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None

    first = lines[0]
    if not first.startswith("#"):
        return None

    tag = first.lstrip("#").strip()
    tag_map = {
        "新增客需": "create_buyer_need",
        "新增委託": "create_seller_listing",
        "新增開發": "create_development",
        "買方追蹤": "buyer_followup",
        "賣方追蹤": "seller_followup",
        "開發追蹤": "development_followup",
        "客戶分類": "classify",
        "查詢紀錄": "query_records",
        "查詢委託到期": "query_contract_end",
        "查詢開發": "query_records",
        "帶看": "buyer_followup",
        "成交": "buyer_followup",
        "委託": "seller_followup",
        "紀錄": "generic_note",
    }
    action = tag_map.get(tag)
    if not action:
        return None

    fields = {}
    for line in lines[1:]:
        m = re.match(r"^([^:：]+)\s*[:：]\s*(.+)$", line)
        if not m:
            continue
        key = normalize_line_key(m.group(1))
        value = m.group(2).strip()
        if key == "labels":
            fields[key] = parse_label_csv(value)
        else:
            fields[key] = value

    if tag in ("買方追蹤", "帶看", "成交") and not fields.get("target_type"):
        fields["target_type"] = "buyer"
    if tag in ("賣方追蹤", "委託") and not fields.get("target_type"):
        fields["target_type"] = "seller"
    if tag in ("開發追蹤", "查詢開發") and not fields.get("target_type"):
        fields["target_type"] = "development"

    fields["target_type"] = normalize_target_type(fields.get("target_type", "")) or fields.get("target_type", "")
    fields["intent_type"] = normalize_intent_type(fields.get("intent_type_raw", ""), fields)
    fields["deal_type"] = normalize_deal_type(fields.get("deal_type_raw", ""))
    fields["limit"] = parse_int_limit(fields.get("limit", 10), default=10, max_value=30)

    if action == "create_buyer_need":
        if not (fields.get("name") and fields.get("phone") and fields.get("source")):
            return None
    elif action == "create_seller_listing":
        if not (fields.get("name") and fields.get("phone") and fields.get("source")):
            return None
    elif action == "create_development":
        if not ((fields.get("phone") or fields.get("name")) and (fields.get("url") or fields.get("content"))):
            return None
    elif action in ("buyer_followup", "seller_followup", "development_followup", "classify", "query_records", "query_contract_end"):
        if not (fields.get("record_id") or fields.get("phone") or fields.get("name")):
            return None

    return {
        "tag": tag,
        "action": action,
        "fields": fields,
        "raw_text": text,
    }


def parse_potential_development_freeform(text: str):
    raw = (text or "").strip()
    if not raw or raw.startswith("#"):
        return None
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) < 3:
        return None

    url = ""
    phone = ""
    name = ""
    note_lines = []

    for ln in lines:
        if not url:
            m = re.search(r"https?://\S+", ln)
            if m:
                url = m.group(0)
                continue
        cleaned = re.sub(r"[^0-9]", "", ln)
        if not phone and len(cleaned) >= 8 and len(cleaned) <= 12:
            phone = ln
            continue

    if not url or not phone:
        return None

    # 姓名：取電話上一行或下一行最像名字的那行
    try:
        phone_idx = next(i for i, ln in enumerate(lines) if re.sub(r"[^0-9]", "", ln) == re.sub(r"[^0-9]", "", phone))
    except StopIteration:
        phone_idx = -1

    candidate_indices = []
    if phone_idx > 0:
        candidate_indices.append(phone_idx - 1)
    if phone_idx + 1 < len(lines):
        candidate_indices.append(phone_idx + 1)
    for idx in candidate_indices:
        ln = lines[idx]
        if ln != url and ln != phone and not re.search(r"https?://", ln):
            if len(ln) <= 20:
                name = ln
                break
    if not name:
        name = "未填姓名"

    for ln in lines:
        if ln in (url, phone, name):
            continue
        if re.search(r"https?://", ln):
            continue
        note_lines.append(ln)

    return {
        "tag": "新增開發",
        "action": "create_development",
        "fields": {
            "name": name,
            "phone": phone,
            "source": "LINE",
            "url": url,
            "content": "\n".join(note_lines).strip(),
            "stage": "待追蹤",
        },
        "raw_text": raw,
    }


def find_customer_record(target_type: str, record_id: str = "", phone: str = "", name: str = ""):
    if target_type == "buyer":
        collection_name = "buyers"
    elif target_type == "seller":
        collection_name = "sellers"
    else:
        collection_name = "developments"

    if record_id:
        doc = db.collection(collection_name).document(record_id).get()
        if doc.exists:
            return doc

    if phone:
        matches = find_records_by_phone(collection_name, phone)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return None

    if name:
        docs = list(db.collection(collection_name).where("name", "==", name.strip()).limit(2).stream())
        if len(docs) == 1:
            return docs[0]

    return None


def resolve_customer_record(fields, preferred_target_type=""):
    record_id = fields.get("record_id", "")
    phone = fields.get("phone", "")
    name = fields.get("name", "")
    target_type = preferred_target_type or normalize_target_type(fields.get("target_type", ""))

    if target_type in ("buyer", "seller", "development"):
        doc = find_customer_record(target_type, record_id, phone, name)
        return target_type, doc

    candidates = []
    for t in ("buyer", "seller", "development"):
        doc = find_customer_record(t, record_id, phone, name)
        if doc:
            candidates.append((t, doc))
    if len(candidates) == 1:
        return candidates[0]
    return "", None


def update_customer_note_and_labels(target_type: str, doc_ref, content: str, labels=None, stage="", source="LINE", event=None):
    labels = dedupe_keep_order(["LINE紀錄"] + ensure_list(labels))
    snapshot = doc_ref.get()
    current = snapshot.to_dict() or {}
    old_note = current.get("note", "")

    source_label = build_line_operator_label(event) if event else "LINE"
    updates = {
        "note": append_note_block(old_note, content, source_label),
        "labels": firestore.ArrayUnion(labels),
        "updated_at": now_taipei().isoformat(),
        "updated_by_id": "line_bot",
        "updated_by_name": "LINE Bot",
    }
    if stage:
        updates["stage"] = stage
    if source:
        updates["source"] = source

    doc_ref.update(updates)


def add_customer_followup(target_type: str, customer_id: str, content: str, next_action="", next_contact_date="", labels=None, line_event=None):
    if target_type == "buyer":
        collection_name = "buyer_followups"
        key_name = "buyer_id"
    elif target_type == "seller":
        collection_name = "seller_followups"
        key_name = "seller_id"
    else:
        collection_name = "development_followups"
        key_name = "development_id"

    sender_display_name = get_line_sender_display_name(line_event) if line_event else ""
    data = {
        key_name: customer_id,
        "contact_time": now_taipei().strftime("%Y-%m-%d %H:%M"),
        "channel": "LINE",
        "content": content,
        "next_action": next_action,
        "next_contact_date": next_contact_date,
        "labels": dedupe_keep_order(["LINE紀錄"] + ensure_list(labels)),
        "created_at": now_taipei().isoformat(),
        "created_by_id": "line_bot",
        "created_by_name": "LINE Bot",
        "sender_display_name": sender_display_name,
    }

    if line_event:
        source = line_event.get("source", {})
        data["line_group_id"] = source.get("groupId", "")
        data["line_room_id"] = source.get("roomId", "")
        data["line_user_id"] = source.get("userId", "")
        data["line_message_id"] = (line_event.get("message") or {}).get("id", "")
        data["quoted_message_id"] = (line_event.get("message") or {}).get("quotedMessageId", "")

    db.collection(collection_name).add(data)


def create_development(fields, event):
    phone = fields.get("phone", "").strip()
    name = fields.get("name", "").strip() or "未填姓名"
    url = fields.get("url", "").strip()
    matches = find_records_by_phone("developments", phone) if phone else []

    labels = build_development_labels(fields.get("labels"))
    content_text = fields.get("content", "").strip() or fields.get("address", "").strip() or url or "LINE 新增開發"
    note_content = build_line_summary(content_text, event)

    payload = {
        "name": name,
        "phone": phone,
        "source": fields.get("source", "").strip() or "LINE",
        "url": url,
        "current_stage": normalize_development_status(fields.get("current_stage", "").strip() or fields.get("stage", "").strip() or "待聯繫"),
        "stage": normalize_development_status(fields.get("current_stage", "").strip() or fields.get("stage", "").strip() or "待聯繫"),
        "next_action": normalize_development_next_action(fields.get("next_action", "").strip()),
        "next_action_date": fields.get("next_action_date", "").strip() or fields.get("next_contact_date", "").strip(),
        "record_date": fields.get("record_date", "").strip() or now_taipei().strftime("%Y-%m-%d"),
        "note": "",
        "labels": labels,
        "updated_at": now_taipei().isoformat(),
        "updated_by_id": "line_bot",
        "updated_by_name": "LINE Bot",
        "sender_display_name": get_line_sender_display_name(event) or "",
    }

    if len(matches) == 1:
        doc = matches[0]
        doc_ref = db.collection("developments").document(doc.id)
        update_customer_note_and_labels(
            target_type="development",
            doc_ref=doc_ref,
            content=note_content,
            labels=labels,
            stage=payload["stage"],
            source=payload["source"],
            event=event,
        )
        doc_ref.update({k: v for k, v in payload.items() if v != "" and k != "note"})
        add_customer_followup(
            target_type="development",
            customer_id=doc.id,
            content=note_content,
            next_action=payload.get("next_action", ""),
            next_contact_date=payload.get("next_action_date", ""),
            labels=labels,
            line_event=event,
        )
        updated_doc = doc_ref.get().to_dict() or {}
        return {
            "handled": True,
            "ok": True,
            "reply_text": f"已註記開發：{updated_doc.get('name', '')}",
            "target_type": "development",
            "target_id": doc.id,
            "customer_name": updated_doc.get("name", ""),
            "phone": updated_doc.get("phone", ""),
            "parsed_tag": "新增開發",
        }

    if len(matches) > 1:
        return {
            "handled": True,
            "ok": False,
            "reply_text": "未寫入：同電話有多筆開發資料，請先到後台整理或改用客戶ID",
        }

    now = now_taipei().isoformat()
    payload.update({
        "created_at": now,
        "created_by_id": "line_bot",
        "created_by_name": "LINE Bot",
        "note": append_note_block("", note_content, build_line_operator_label(event)),
    })
    doc_ref = db.collection("developments").document()
    doc_ref.set(payload)
    add_customer_followup(
        target_type="development",
        customer_id=doc_ref.id,
        content=note_content,
        next_action=payload.get("next_action", ""),
        next_contact_date=payload.get("next_action_date", ""),
        labels=labels,
        line_event=event,
    )
    return {
        "handled": True,
        "ok": True,
        "reply_text": f"已註記開發：{payload['name']}",
        "target_type": "development",
        "target_id": doc_ref.id,
        "customer_name": payload["name"],
        "phone": payload["phone"],
        "parsed_tag": "新增開發",
    }


def format_record_timeline(target_type: str, doc_snapshot, limit=10):
    data = doc_snapshot.to_dict() or {}
    record_id = doc_snapshot.id

    if target_type == "buyer":
        followup_collection = "buyer_followups"
        key_name = "buyer_id"
    elif target_type == "seller":
        followup_collection = "seller_followups"
        key_name = "seller_id"
    else:
        followup_collection = "development_followups"
        key_name = "development_id"

    followups = []
    for d in db.collection(followup_collection).where(key_name, "==", record_id).stream():
        item = d.to_dict() or {}
        followups.append({
            "time": item.get("contact_time") or item.get("created_at") or "",
            "channel": item.get("channel", "LINE"),
            "text": (item.get("content") or "").strip(),
            "created_by_name": item.get("created_by_name", "") or "",
            "sender_display_name": item.get("sender_display_name", "") or "",
        })

    followups = [x for x in followups if x.get("text")]
    followups.sort(key=lambda x: x.get("time", ""), reverse=True)

    lines = ["客戶資訊"]

    if target_type == "buyer":
        intent_map = {"rent": "租屋", "buy": "買賣", "both": "租買皆可"}
        lines.extend([
            f"姓名: {data.get('name', '')}",
            f"電話: {data.get('phone', '')}",
            f"客源來源: {data.get('source', '') or '-'}",
            f"需求類型: {intent_map.get(data.get('intent_type', ''), data.get('intent_type', '') or '-')}",
            f"預算: {data.get('budget_min', '') or data.get('rent_min', '') or '-'} ~ {data.get('budget_max', '') or data.get('rent_max', '') or '-'}",
            f"偏好區域: {data.get('preferred_areas', '') or '-'}",
            f"產品類型: {data.get('property_type', '') or '-'}",
            f"房數需求: {data.get('room_range', '') or '-'}",
            f"車位需求: {data.get('car_need', '') or '-'}",
        ])
    elif target_type == "seller":
        deal_map = {"sale": "買賣", "rent": "出租"}
        lines.extend([
            f"姓名: {data.get('name', '')}",
            f"電話: {data.get('phone', '')}",
            f"客源來源: {data.get('source', '') or '-'}",
            f"委託類型: {deal_map.get(data.get('deal_type', ''), data.get('deal_type', '') or '-')}",
            f"地址: {data.get('address', '') or '-'}",
            f"產品類型: {data.get('property_type', '') or '-'}",
            f"開價: {data.get('expected_price', '') or '-'}",
            f"底價: {data.get('min_price', '') or '-'}",
            f"委託到期日: {data.get('contract_end_date', '') or '-'}",
        ])
    else:
        lines.extend([
            f"日期: {data.get('record_date', '') or '-'}",
            f"姓名: {data.get('name', '')}",
            f"電話: {data.get('phone', '') or '-'}",
            f"來源: {data.get('source', '') or '-'}",
            f"進度: {data.get('stage', '') or '-'}",
            f"網址: {data.get('url', '') or '-'}",
            f"主紀錄: {data.get('note', '').splitlines()[-1] if data.get('note') else '-'}",
        ])

    lines.append("")
    lines.append("追蹤進度")

    if not followups:
        lines.append("目前沒有追蹤紀錄")
    else:
        for item in followups[:limit]:
            header_parts = [item.get('time', ''), item.get('channel', 'LINE')]
            creator = (item.get("created_by_name") or "").strip()
            sender = (item.get("sender_display_name") or "").strip()
            if creator:
                header_parts.append(f"KEYIN: {creator}")
            if sender:
                header_parts.append(f"留言者: {sender}")
            lines.append("｜".join([p for p in header_parts if p]))
            lines.append(item.get("text", ""))
            lines.append("")

    return "\n".join(lines).strip()[:4500]


def query_contract_end_text(fields):
    target_type, doc = resolve_customer_record(fields, preferred_target_type="seller")
    if target_type != "seller" or not doc:
        return False, "查無唯一委託資料，請提供正確電話或客戶ID", None

    data = doc.to_dict() or {}
    deal_type = data.get("deal_type", "")
    deal_label = "出租" if deal_type == "rent" else "買賣" if deal_type == "sale" else "未設定"
    text = "\n".join([
        "查詢委託到期",
        f"姓名: {data.get('name', '')}",
        f"電話: {data.get('phone', '')}",
        f"地址: {data.get('address', '')}",
        f"委託類型: {deal_label}",
        f"委託到期日: {data.get('contract_end_date', '') or '未填寫'}",
        f"目前進程: {data.get('stage', '') or '未填寫'}",
    ]).strip()
    return True, text[:4500], {
        "target_type": "seller",
        "target_id": doc.id,
        "customer_name": data.get("name", ""),
        "phone": data.get("phone", ""),
        "parsed_tag": "查詢委託到期",
    }


def process_quote_context_message(event):
    message = event.get("message") or {}
    quoted_message_id = message.get("quotedMessageId", "")
    raw_text = (message.get("text") or "").strip()
    if not quoted_message_id or not raw_text:
        return {"handled": False}

    link = get_line_message_link(quoted_message_id)
    if not link:
        return {"handled": False}

    target_type = link.get("target_type", "")
    target_id = link.get("target_id", "")
    if target_type not in ("buyer", "seller", "development") or not target_id:
        return {"handled": False}

    if target_type == "buyer":
        collection_name = "buyers"
    elif target_type == "seller":
        collection_name = "sellers"
    else:
        collection_name = "developments"

    doc_ref = db.collection(collection_name).document(target_id)
    doc = doc_ref.get()
    if not doc.exists:
        return {"handled": True, "ok": False, "reply_text": "未寫入：引用的客戶資料不存在"}

    labels = dedupe_keep_order(["LINE紀錄", "群組回覆註記"])
    reply_only_text = raw_text

    update_customer_note_and_labels(
        target_type=target_type,
        doc_ref=doc_ref,
        content=reply_only_text,
        labels=labels,
        source="LINE",
        event=event,
    )
    add_customer_followup(
        target_type=target_type,
        customer_id=target_id,
        content=reply_only_text,
        labels=labels,
        line_event=event,
    )

    parsed = {
        "tag": "群組回覆註記",
        "action": "quoted_context_note",
        "fields": {"quoted_message_id": quoted_message_id},
        "raw_text": raw_text,
    }
    save_line_log(parsed, event, "success", target_type=target_type, target_id=target_id, sender_display_name=get_line_sender_display_name(event))
    target_label = "買方" if target_type == "buyer" else "賣方" if target_type == "seller" else "開發"
    return {
        "handled": True,
        "ok": True,
        "reply_text": f"已註記到{target_label}：{(doc.to_dict() or {}).get('name', '')}",
        "target_type": target_type,
        "target_id": target_id,
        "customer_name": (doc.to_dict() or {}).get("name", ""),
        "phone": (doc.to_dict() or {}).get("phone", ""),
        "parsed_tag": "群組回覆註記",
    }


def process_line_message_event(event):
    message = event.get("message") or {}
    if message.get("type") != "text":
        return {"handled": False}

    sender_display_name = get_line_sender_display_name(event)

    parsed = parse_line_formatted_message(message.get("text", ""))
    if not parsed:
        quoted_result = process_quote_context_message(event)
        if quoted_result.get("handled"):
            return quoted_result
        parsed = parse_potential_development_freeform(message.get("text", ""))
        if not parsed:
            return {"handled": False}

    fields = parsed["fields"]
    action = parsed["action"]

    if action == "create_buyer_need":
        result = create_buyer_need(fields, event)
    elif action == "create_seller_listing":
        result = create_seller_listing(fields, event)
    elif action == "create_development":
        result = create_development(fields, event)
    elif action == "query_records":
        target_type, doc = resolve_customer_record(fields)
        if not doc:
            result = {"handled": True, "ok": False, "reply_text": "查無唯一資料，請補電話或客戶ID"}
        else:
            result = {
                "handled": True,
                "ok": True,
                "reply_text": format_record_timeline(target_type, doc, limit=fields.get("limit", 10)),
                "target_type": target_type,
                "target_id": doc.id,
                "customer_name": (doc.to_dict() or {}).get("name", ""),
                "phone": (doc.to_dict() or {}).get("phone", ""),
                "parsed_tag": parsed.get("tag", ""),
            }
    elif action == "query_contract_end":
        ok, text, ctx = query_contract_end_text(fields)
        result = {"handled": True, "ok": ok, "reply_text": text}
        if ctx:
            result.update(ctx)
    else:
        target_type = fields.get("target_type", "")
        if action == "buyer_followup":
            target_type = "buyer"
        elif action == "seller_followup":
            target_type = "seller"
        elif action == "development_followup":
            target_type = "development"

        if target_type not in ("buyer", "seller", "development"):
            result = {"handled": True, "ok": False, "reply_text": "請提供對象：買方、賣方 或 開發"}
        else:
            doc = find_customer_record(
                target_type=target_type,
                record_id=fields.get("record_id", ""),
                phone=fields.get("phone", ""),
                name=fields.get("name", ""),
            )
            if not doc:
                target_label = "買方" if target_type == "buyer" else "賣方" if target_type == "seller" else "開發"
                result = {"handled": True, "ok": False, "reply_text": f"未寫入：找不到唯一一位{target_label}，請補客戶ID或正確電話"}
            else:
                content = fields.get("content", "").strip() or build_line_summary("LINE 更新", event)
                labels = fields.get("labels") or ["LINE紀錄"]
                collection_name = "buyers" if target_type == "buyer" else "sellers" if target_type == "seller" else "developments"
                doc_ref = db.collection(collection_name).document(doc.id)
                update_customer_note_and_labels(
                    target_type=target_type,
                    doc_ref=doc_ref,
                    content=content,
                    labels=labels,
                    stage=fields.get("stage", "").strip(),
                    source="LINE",
                    event=event,
                )
                add_customer_followup(
                    target_type=target_type,
                    customer_id=doc.id,
                    content=content,
                    next_action=fields.get("next_action", "").strip(),
                    next_contact_date=fields.get("next_contact_date", "").strip(),
                    labels=labels,
                    line_event=event,
                )
                data = doc_ref.get().to_dict() or {}
                target_label = "買方" if target_type == "buyer" else "賣方" if target_type == "seller" else "開發"
                result = {
                    "handled": True,
                    "ok": True,
                    "reply_text": f"已新增{target_label}追蹤：{data.get('name', '')}",
                    "target_type": target_type,
                    "target_id": doc.id,
                    "customer_name": data.get("name", ""),
                    "phone": data.get("phone", ""),
                    "parsed_tag": parsed.get("tag", ""),
                }

    if result.get("handled"):
        save_line_log(
            parsed=parsed,
            event=event,
            status="success" if result.get("ok") else "failed",
            target_type=result.get("target_type", ""),
            target_id=result.get("target_id", ""),
            note=result.get("reply_text", ""),
            sender_display_name=sender_display_name,
        )
    return result


def delete_by_field(collection_name, field_name, field_value):
    ref = db.collection(collection_name).where(field_name, "==", field_value)
    docs = list(ref.stream())
    for d in docs:
        d.reference.delete()




# ========= 開發模組加強：目前狀態 / 下一動作 / 下次日期、戶籍地址、批次掃街、LINE 即時更新 =========

DEVELOPMENT_STATUS_OPTIONS = [
    "待調謄本",
    "已調謄本",
    "待寄開發信",
    "已寄開發信待跑開發",
    "已跑開發",
    "待聯繫",
    "已聯繫",
    "持續追蹤",
    "已簽回",
    "已拋轉客需",
    "已拋轉委託",
    "無效",
]

DEVELOPMENT_NEXT_ACTION_OPTIONS = [
    "調謄本",
    "寄開發信",
    "跑開發",
    "電話聯繫",
    "LINE聯繫",
    "再次聯繫",
    "現場拜訪",
    "約面談",
    "簽回委託",
    "拋轉客需",
    "拋轉委託",
    "暫不處理",
    "結案",
]

DEVELOPMENT_HIDDEN_BY_DEFAULT = {"已簽回", "已拋轉客需", "已拋轉委託"}


def normalize_development_status(raw: str) -> str:
    v = (raw or "").strip()
    mapping = {
        "待調謄本": "待調謄本",
        "已調謄本": "已調謄本",
        "待寄開發信": "待寄開發信",
        "已寄開發信": "已寄開發信待跑開發",
        "已寄開發信待跑開發": "已寄開發信待跑開發",
        "寄出開發信": "已寄開發信待跑開發",
        "已跑開發": "已跑開發",
        "待聯絡": "待聯繫",
        "待聯繫": "待聯繫",
        "已聯絡": "已聯繫",
        "已連繫": "已聯繫",
        "已聯繫": "已聯繫",
        "再聯絡": "持續追蹤",
        "再聯繫": "持續追蹤",
        "持續追蹤": "持續追蹤",
        "已簽回": "已簽回",
        "已拋轉客需": "已拋轉客需",
        "已拋轉委託": "已拋轉委託",
        "無效": "無效",
    }
    return mapping.get(v, v)

def normalize_development_next_action(raw: str) -> str:
    v = (raw or "").strip()
    mapping = {
        "調謄本": "調謄本",
        "寄開發信": "寄開發信",
        "跑開發": "跑開發",
        "電話聯絡": "電話聯繫",
        "電話聯繫": "電話聯繫",
        "LINE聯絡": "LINE聯繫",
        "LINE聯繫": "LINE聯繫",
        "再次聯絡": "再次聯繫",
        "再次聯繫": "再次聯繫",
        "現場拜訪": "現場拜訪",
        "約面談": "約面談",
        "簽回委託": "簽回委託",
        "拋轉客需": "拋轉客需",
        "拋轉委託": "拋轉委託",
        "暫不處理": "暫不處理",
        "結案": "結案",
    }
    return mapping.get(v, v)


def normalize_line_key(key: str):
    k = (key or "").strip().replace(" ", "")
    mapping = {
        "對象": "target_type",
        "類型": "target_type",
        "目標": "target_type",
        "客戶類型": "target_type",
        "電話": "phone",
        "手機": "phone",
        "電話號碼": "phone",
        "姓名": "name",
        "客戶": "name",
        "買方": "name",
        "賣方": "name",
        "屋主": "name",
        "客戶來源": "source",
        "來源": "source",
        "需求類型": "intent_type_raw",
        "委託類型": "deal_type_raw",
        "ID": "record_id",
        "客戶ID": "record_id",
        "buyer_id": "record_id",
        "seller_id": "record_id",
        "development_id": "record_id",
        "內容": "content",
        "紀錄": "content",
        "備註": "content",
        "說明": "content",
        "進度內容": "content",
        "進度": "stage",
        "進程": "stage",
        "階段": "stage",
        "狀態": "stage",
        "目前狀態": "current_stage",
        "下一動作": "next_action",
        "下次日期": "next_action_date",
        "下次動作日期": "next_action_date",
        "標籤": "labels",
        "分類": "labels",
        "labels": "labels",
        "下一步": "next_action",
        "下次行動": "next_action",
        "next_action": "next_action",
        "下次聯絡日": "next_contact_date",
        "下次聯絡日期": "next_contact_date",
        "next_contact_date": "next_contact_date",
        "地址": "address",
        "物件": "address",
        "案名": "address",
        "總價": "price",
        "價格": "price",
        "預算": "budget",
        "區域": "preferred_areas",
        "產品類型": "property_type",
        "房數": "room_range",
        "車位": "car_need",
        "開價": "expected_price",
        "底價": "min_price",
        "委託到期日": "contract_end_date",
        "筆數": "limit",
        "網址": "url",
        "連結": "url",
        "網站": "url",
        "日期": "record_date",
        "戶籍地址": "registered_address",
        "戶籍地": "registered_address",
    }
    return mapping.get(k, k)


def normalize_target_type(value: str) -> str:
    v = (value or "").strip()
    if v in ("買方", "客需", "buyer", "buyers"):
        return "buyer"
    if v in ("賣方", "委託", "seller", "sellers", "屋主"):
        return "seller"
    if v in ("開發", "development", "developments", "名單", "開發名單"):
        return "development"
    return ""


def find_customer_record(target_type: str, record_id: str = "", phone: str = "", name: str = "", address: str = ""):
    if target_type == "buyer":
        collection_name = "buyers"
    elif target_type == "seller":
        collection_name = "sellers"
    else:
        collection_name = "developments"

    if record_id:
        doc = db.collection(collection_name).document(record_id).get()
        if doc.exists:
            return doc

    if phone:
        matches = find_records_by_phone(collection_name, phone)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return None

    if name:
        docs = list(db.collection(collection_name).where("name", "==", name.strip()).limit(2).stream())
        if len(docs) == 1:
            return docs[0]

    if target_type == "development" and address:
        docs = [d for d in db.collection(collection_name).stream() if (d.to_dict() or {}).get("address", "").strip() == address.strip()]
        if len(docs) == 1:
            return docs[0]

    return None


def resolve_customer_record(fields, preferred_target_type=""):
    record_id = fields.get("record_id", "")
    phone = fields.get("phone", "")
    name = fields.get("name", "")
    address = fields.get("address", "")
    target_type = preferred_target_type or normalize_target_type(fields.get("target_type", ""))

    if target_type in ("buyer", "seller", "development"):
        doc = find_customer_record(target_type, record_id, phone, name, address)
        return target_type, doc

    buyer_doc = find_customer_record("buyer", record_id, phone, name, address)
    seller_doc = find_customer_record("seller", record_id, phone, name, address)
    development_doc = find_customer_record("development", record_id, phone, name, address)

    hits = [(t, d) for t, d in [("buyer", buyer_doc), ("seller", seller_doc), ("development", development_doc)] if d]
    if len(hits) == 1:
        return hits[0][0], hits[0][1]
    return "", None



def update_customer_note_and_labels(target_type: str, doc_ref, content: str, labels=None, stage="", source="LINE", event=None, registered_address="", extra_updates=None):
    labels = dedupe_keep_order(["LINE紀錄"] + ensure_list(labels))
    snapshot = doc_ref.get()
    current = snapshot.to_dict() or {}
    old_note = current.get("note", "")

    updates = {
        "labels": firestore.ArrayUnion(labels),
        "updated_at": now_taipei().isoformat(),
        "updated_by_id": "line_bot",
        "updated_by_name": "LINE Bot",
    }
    if content:
        source_label = build_line_operator_label(event) if event else "LINE"
        updates["note"] = append_note_block(old_note, content, source_label)
    if stage:
        updates["stage"] = stage
    if source:
        updates["source"] = source
    if target_type == "development" and registered_address:
        updates["registered_address"] = registered_address
    if extra_updates:
        cleaned = {k: v for k, v in extra_updates.items() if v not in (None, "")}
    
    if target_type == "development":
        updates, note_text = parse_reply_field_updates(raw_text)

        extra_updates = {k: v for k, v in updates.items() if k in ("address", "phone", "name", "url", "current_stage", "next_action", "next_action_date")}
        if updates:
            update_customer_note_and_labels(
                target_type="development",
                doc_ref=doc_ref,
                content=note_text or reply_only_text,
                labels=labels,
                stage=updates.get("current_stage", ""),
                source=updates.get("source", "LINE"),
                event=event,
                registered_address=updates.get("registered_address", ""),
                extra_updates=extra_updates,
            )
        else:
            update_customer_note_and_labels(
                target_type="development",
                doc_ref=doc_ref,
                content=reply_only_text,
                labels=labels,
                source="LINE",
                event=event,
            )

        followup_text = note_text or reply_only_text or "更新開發資料"
        add_customer_followup(
            target_type="development",
            customer_id=target_id,
            content=followup_text,
            labels=labels,
            line_event=event,
            current_stage=updates.get("current_stage", "") if updates else "",
            registered_address=updates.get("registered_address", "") if updates else "",
            next_action=updates.get("next_action", "") if updates else "",
            next_action_date=updates.get("next_action_date", "") if updates else "",
        )
    else:
        collection_name = "development_followups"
        key_name = "development_id"

    sender_display_name = get_line_sender_display_name(line_event) if line_event else ""
    data = {
        key_name: customer_id,
        "contact_time": now_taipei().strftime("%Y-%m-%d %H:%M"),
        "channel": channel,
        "content": content,
        "next_action": next_action,
        "next_contact_date": next_contact_date,
        "labels": dedupe_keep_order(["LINE紀錄"] + ensure_list(labels)),
        "created_at": now_taipei().isoformat(),
        "created_by_id": "line_bot",
        "created_by_name": "LINE Bot",
        "sender_display_name": sender_display_name,
    }
    if target_type == "development":
        cs = normalize_development_status(current_stage or stage)
        na = normalize_development_next_action(next_action)
        data["current_stage"] = cs
        data["stage"] = cs
        data["registered_address"] = registered_address.strip() if registered_address else ""
        data["next_action"] = na
        data["next_action_date"] = (next_action_date or next_contact_date or "").strip()

    if line_event:
        source = line_event.get("source", {})
        data["line_group_id"] = source.get("groupId", "")
        data["line_room_id"] = source.get("roomId", "")
        data["line_user_id"] = source.get("userId", "")
        data["line_message_id"] = (line_event.get("message") or {}).get("id", "")
        data["quoted_message_id"] = (line_event.get("message") or {}).get("quotedMessageId", "")

    db.collection(collection_name).add(data)


def parse_line_formatted_message(text: str):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None

    first = lines[0]
    if not first.startswith("#"):
        return None

    tag = first.lstrip("#").strip()
    if tag == "新增開發批次":
        return {
            "tag": tag,
            "action": "create_development_batch",
            "fields": {},
            "raw_text": text,
        }

    tag_map = {
        "新增客需": "create_buyer_need",
        "新增委託": "create_seller_listing",
        "新增開發": "create_development",
        "買方追蹤": "buyer_followup",
        "賣方追蹤": "seller_followup",
        "開發追蹤": "development_followup",
        "客戶分類": "classify",
        "查詢紀錄": "query_records",
        "查詢開發": "query_records",
        "查詢委託到期": "query_contract_end",
        "帶看": "buyer_followup",
        "成交": "buyer_followup",
        "委託": "seller_followup",
        "紀錄": "generic_note",
    }
    action = tag_map.get(tag)
    if not action:
        return None

    fields = {}
    for line in lines[1:]:
        m = re.match(r"^([^:：]+)\s*[:：]\s*(.+)$", line)
        if not m:
            continue
        key = normalize_line_key(m.group(1))
        value = m.group(2).strip()
        if key == "labels":
            fields[key] = parse_label_csv(value)
        else:
            fields[key] = value

    if tag in ("買方追蹤", "帶看", "成交") and not fields.get("target_type"):
        fields["target_type"] = "buyer"
    if tag in ("賣方追蹤", "委託") and not fields.get("target_type"):
        fields["target_type"] = "seller"
    if tag in ("開發追蹤", "查詢開發") and not fields.get("target_type"):
        fields["target_type"] = "development"

    fields["target_type"] = normalize_target_type(fields.get("target_type", "")) or fields.get("target_type", "")
    fields["intent_type"] = normalize_intent_type(fields.get("intent_type_raw", ""), fields)
    fields["deal_type"] = normalize_deal_type(fields.get("deal_type_raw", ""))
    fields["limit"] = parse_int_limit(fields.get("limit", 10), default=10, max_value=30)
    if fields.get("stage"):
        fields["stage"] = normalize_development_status(fields.get("stage", ""))

    if action == "create_buyer_need":
        if not (fields.get("name") and fields.get("phone") and fields.get("source")):
            return None
    elif action == "create_seller_listing":
        if not (fields.get("name") and fields.get("phone") and fields.get("source")):
            return None
    elif action == "create_development":
        if not (fields.get("name") or fields.get("phone") or fields.get("address") or fields.get("url")):
            return None
    elif action in ("buyer_followup", "seller_followup", "development_followup", "classify", "query_records", "query_contract_end"):
        if not (fields.get("record_id") or fields.get("phone") or fields.get("name") or fields.get("address")):
            return None

    return {
        "tag": tag,
        "action": action,
        "fields": fields,
        "raw_text": text,
    }


def parse_potential_development_freeform(text: str):
    raw = (text or "").strip()
    if not raw or raw.startswith("#"):
        return None
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) < 3:
        return None

    url = ""
    phone = ""
    name = ""
    address = ""
    note_lines = []

    for ln in lines:
        if not url:
            m = re.search(r"https?://\S+", ln)
            if m:
                url = m.group(0)
                continue

    # 找電話
    for ln in lines:
        cleaned = re.sub(r"[^0-9]", "", ln)
        if len(cleaned) >= 8 and len(cleaned) <= 12:
            phone = ln
            break

    # 找地址：含台中/臺中/區/路/街/巷/號者優先
    for ln in lines:
        if re.search(r"(台中|臺中).*(區).*(路|街|巷).*(號)", ln) or re.search(r"(路|街|巷).*(號)", ln):
            address = ln
            break

    # 姓名：優先找先生/小姐/太太/女士或短字串
    for ln in lines:
        if ln in (url, phone, address):
            continue
        if re.search(r"(先生|小姐|太太|女士)$", ln):
            name = ln
            break
    if not name:
        for ln in lines:
            if ln in (url, phone, address):
                continue
            if len(ln) <= 12 and not re.search(r"https?://", ln):
                name = ln
                break

    for ln in lines:
        if ln in (url, phone, name, address):
            continue
        if re.search(r"https?://", ln):
            continue
        note_lines.append(ln)

    if not (phone or url or address):
        return None

    return {
        "tag": "新增開發",
        "action": "create_development",
        "fields": {
            "name": name or "未填姓名",
            "phone": phone,
            "address": address,
            "source": "LINE",
            "url": url,
            "content": "\n".join(note_lines).strip(),
            "stage": "待聯繫",
        },
        "raw_text": raw,
    }



def parse_development_batch_message(raw_text: str):
    text = (raw_text or "").replace("\r\n", "\n").strip()
    lines = text.split("\n")
    if lines and lines[0].strip().startswith("#"):
        lines = lines[1:]
    body = "\n".join(lines).strip()
    if not body:
        return []

    # 支援 --- 與 ---- 當分隔線；優先使用「獨立一行」的分隔，避免誤切備註內容。
    chunks = [c.strip() for c in re.split(r"(?m)^-{3,}\s*$", body) if c.strip()]
    items = []
    for chunk in chunks:
        fields = {}
        notes = []
        for line in chunk.split("\n"):
            ln = line.strip()
            if not ln:
                continue
            m = re.match(r"^([^:：]+)\s*[:：]\s*(.*)$", ln)
            if m:
                key = normalize_line_key(m.group(1))
                value = (m.group(2) or "").strip()
                if key == "labels":
                    fields[key] = parse_label_csv(value)
                else:
                    fields[key] = value
            else:
                notes.append(ln)
        if notes:
            fields["content"] = (fields.get("content", "") + ("\n" if fields.get("content") else "") + "\n".join(notes)).strip()

        if not fields.get("current_stage"):
            fields["current_stage"] = normalize_development_status(fields.get("stage", "") or "待聯繫")
        else:
            fields["current_stage"] = normalize_development_status(fields["current_stage"])
        fields["stage"] = fields["current_stage"]

        if fields.get("next_action"):
            fields["next_action"] = normalize_development_next_action(fields["next_action"])

        if fields.get("name") or fields.get("phone") or fields.get("address") or fields.get("url") or fields.get("content"):
            items.append(fields)
    return items


def infer_development_source(explicit_source: str, url: str) -> str:
    explicit = (explicit_source or "").strip()
    if explicit in ("掃街", "踩線"):
        return explicit
    if explicit and explicit.upper() != "LINE":
        return explicit
    return "踩線" if (url or "").strip() else "掃街"


def create_development(fields, event):
    sender_name = get_line_sender_display_name(event) or "未知成員"
    phone = fields.get("phone", "").strip()
    name = fields.get("name", "").strip() or "未填姓名"
    url = fields.get("url", "").strip()
    address = fields.get("address", "").strip()
    registered_address = fields.get("registered_address", "").strip()
    current_stage = normalize_development_status(fields.get("current_stage", "").strip() or fields.get("stage", "").strip() or "待聯繫")
    next_action = normalize_development_next_action(fields.get("next_action", "").strip())
    next_action_date = (fields.get("next_action_date", "") or fields.get("next_contact_date", "")).strip()
    source = infer_development_source(fields.get("source", ""), url)

    matches = []
    if phone:
        matches = find_records_by_phone("developments", phone)
    elif address:
        matches = [d for d in db.collection("developments").stream() if (d.to_dict() or {}).get("address", "").strip() == address]

    labels = build_development_labels(fields.get("labels"))
    summary_content = fields.get("content", "").strip() or address or url or "LINE 新增開發"
    note_content = build_line_summary(summary_content, event)

    payload = {
        "name": name,
        "phone": phone,
        "source": source,
        "url": url,
        "address": address,
        "registered_address": registered_address,
        "current_stage": current_stage,
        "stage": current_stage,
        "next_action": next_action,
        "next_action_date": next_action_date,
        "record_date": fields.get("record_date", "").strip() or now_taipei().strftime("%Y-%m-%d"),
        "note": "",
        "labels": labels,
        "updated_at": now_taipei().isoformat(),
        "updated_by_id": "line_bot",
        "updated_by_name": "LINE Bot",
        "sender_display_name": sender_name,
    }

    if len(matches) == 1:
        doc = matches[0]
        doc_ref = db.collection("developments").document(doc.id)
        update_customer_note_and_labels(
            target_type="development",
            doc_ref=doc_ref,
            content=note_content,
            labels=labels,
            stage=current_stage,
            source=source,
            event=event,
            registered_address=registered_address,
            extra_updates={
                "address": address,
                "url": url,
                "name": name,
                "current_stage": current_stage,
                "next_action": next_action,
                "next_action_date": next_action_date,
            },
        )
        add_customer_followup(
            target_type="development",
            customer_id=doc.id,
            content=note_content,
            labels=labels,
            line_event=event,
            current_stage=current_stage,
            registered_address=registered_address,
            next_action=next_action,
            next_action_date=next_action_date,
        )
        updated_doc = doc_ref.get().to_dict() or {}
        return {
            "handled": True,
            "ok": True,
            "reply_text": f"已註記開發：{updated_doc.get('name', '')}",
            "target_type": "development",
            "target_id": doc.id,
            "customer_name": updated_doc.get("name", ""),
            "phone": updated_doc.get("phone", ""),
            "parsed_tag": "新增開發",
        }

    if len(matches) > 1:
        return {
            "handled": True,
            "ok": False,
            "reply_text": "未寫入：同電話或同地址有多筆開發資料，請先到後台整理或改用客戶ID",
        }

    now = now_taipei().isoformat()
    payload.update({
        "created_at": now,
        "created_by_id": "line_bot",
        "created_by_name": "LINE Bot",
        "note": append_note_block("", note_content, build_line_operator_label(event)),
    })
    doc_ref = db.collection("developments").document()
    doc_ref.set(payload)
    add_customer_followup(
        target_type="development",
        customer_id=doc_ref.id,
        content=note_content,
        labels=labels,
        line_event=event,
        current_stage=current_stage,
        registered_address=registered_address,
        next_action=next_action,
        next_action_date=next_action_date,
    )
    return {
        "handled": True,
        "ok": True,
        "reply_text": f"已註記開發：{payload['name']}",
        "target_type": "development",
        "target_id": doc_ref.id,
        "customer_name": payload["name"],
        "phone": payload["phone"],
        "parsed_tag": "新增開發",
    }

def create_development_batch(raw_text, event):

    items = parse_development_batch_message(raw_text)
    if not items:
        return {"handled": True, "ok": False, "reply_text": "未寫入：#新增開發批次 沒有解析到有效資料"}

    ok_count = 0
    fail_count = 0
    last_result = None
    for fields in items:
        result = create_development(fields, event)
        last_result = result
        if result.get("ok"):
            ok_count += 1
        else:
            fail_count += 1

    reply_text = f"批量註記完成：成功 {ok_count} 筆，失敗 {fail_count} 筆"
    if last_result and last_result.get("target_type") and last_result.get("target_id"):
        return {
            "handled": True,
            "ok": ok_count > 0,
            "reply_text": reply_text,
            "target_type": last_result.get("target_type", ""),
            "target_id": last_result.get("target_id", ""),
            "customer_name": last_result.get("customer_name", ""),
            "phone": last_result.get("phone", ""),
            "parsed_tag": "新增開發批次",
        }
    return {"handled": True, "ok": ok_count > 0, "reply_text": reply_text}



def parse_reply_field_updates(text: str):
    updates = {}
    note_lines = []

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue

        if "：" in line:
            key, value = line.split("：", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        else:
            maybe_status = normalize_development_status(line)
            maybe_action = normalize_development_next_action(line)
            if maybe_status in DEVELOPMENT_STATUS_OPTIONS:
                updates["current_stage"] = maybe_status
            elif maybe_action in DEVELOPMENT_NEXT_ACTION_OPTIONS:
                updates["next_action"] = maybe_action
            else:
                note_lines.append(line)
            continue

        key = normalize_line_key(key.strip())
        value = value.strip()

        if key in ("stage", "current_stage"):
            updates["current_stage"] = normalize_development_status(value)
        elif key == "registered_address":
            updates["registered_address"] = value
        elif key == "next_action":
            updates["next_action"] = normalize_development_next_action(value)
        elif key in ("next_action_date", "next_contact_date"):
            updates["next_action_date"] = value
        elif key in ("address", "phone", "name", "url", "source"):
            updates[key] = value
        elif key == "content":
            note_lines.append(value)

    return updates, "\n".join(note_lines).strip()


def process_quote_context_message(event):
    message = event.get("message") or {}
    quoted_message_id = message.get("quotedMessageId", "")
    raw_text = (message.get("text") or "").strip()
    if not quoted_message_id or not raw_text:
        return {"handled": False}

    link = get_line_message_link(quoted_message_id)
    if not link:
        return {"handled": False}

    target_type = link.get("target_type", "")
    target_id = link.get("target_id", "")
    if target_type not in ("buyer", "seller", "development") or not target_id:
        return {"handled": False}

    if target_type == "buyer":
        collection_name = "buyers"
        label_text = "買方"
    elif target_type == "seller":
        collection_name = "sellers"
        label_text = "賣方"
    else:
        collection_name = "developments"
        label_text = "開發"

    doc_ref = db.collection(collection_name).document(target_id)
    doc = doc_ref.get()
    if not doc.exists:
        return {"handled": True, "ok": False, "reply_text": "未寫入：引用的資料不存在"}

    labels = dedupe_keep_order(["LINE紀錄", "群組回覆註記"])
    reply_only_text = raw_text
    sender_display_name = get_line_sender_display_name(event)

    if target_type == "development":
        updates = parse_reply_field_updates(raw_text)
        if updates:
            update_customer_note_and_labels(
                target_type="development",
                doc_ref=doc_ref,
                content=reply_only_text,
                labels=labels,
                stage=updates.get("stage", ""),
                source=updates.get("source", "LINE"),
                event=event,
                registered_address=updates.get("registered_address", ""),
                extra_updates={k: v for k, v in updates.items() if k in ("address", "phone", "name", "url")},
            )
        else:
            update_customer_note_and_labels(
                target_type="development",
                doc_ref=doc_ref,
                content=reply_only_text,
                labels=labels,
                source="LINE",
                event=event,
            )

        add_customer_followup(
            target_type="development",
            customer_id=target_id,
            content=reply_only_text,
            labels=labels,
            line_event=event,
            stage=updates.get("stage", "") if updates else "",
            registered_address=updates.get("registered_address", "") if updates else "",
        )
    else:
        update_customer_note_and_labels(
            target_type=target_type,
            doc_ref=doc_ref,
            content=reply_only_text,
            labels=labels,
            source="LINE",
            event=event,
        )
        add_customer_followup(
            target_type=target_type,
            customer_id=target_id,
            content=reply_only_text,
            labels=labels,
            line_event=event,
        )

    parsed = {
        "tag": "群組回覆註記",
        "action": "quoted_context_note",
        "fields": {"quoted_message_id": quoted_message_id},
        "raw_text": raw_text,
    }
    save_line_log(parsed, event, "success", target_type=target_type, target_id=target_id, sender_display_name=sender_display_name)
    return {
        "handled": True,
        "ok": True,
        "reply_text": f"已註記到{label_text}：{(doc.to_dict() or {}).get('name', '')}",
        "target_type": target_type,
        "target_id": target_id,
        "customer_name": (doc.to_dict() or {}).get("name", ""),
        "phone": (doc.to_dict() or {}).get("phone", ""),
        "parsed_tag": "群組回覆註記",
    }


def format_record_timeline(target_type: str, doc_snapshot, limit=10):
    data = doc_snapshot.to_dict() or {}
    record_id = doc_snapshot.id

    if target_type == "buyer":
        followup_collection = "buyer_followups"
        key_name = "buyer_id"
    elif target_type == "seller":
        followup_collection = "seller_followups"
        key_name = "seller_id"
    else:
        followup_collection = "development_followups"
        key_name = "development_id"

    followups = []
    for d in db.collection(followup_collection).where(key_name, "==", record_id).stream():
        item = d.to_dict() or {}
        followups.append({
            "time": item.get("contact_time") or item.get("created_at") or "",
            "channel": item.get("channel", "LINE"),
            "text": (item.get("content") or "").strip(),
            "created_by_name": item.get("created_by_name", "") or "",
            "sender_display_name": item.get("sender_display_name", "") or "",
            "stage": item.get("stage", "") or "",
            "registered_address": item.get("registered_address", "") or "",
        })

    followups = [x for x in followups if x.get("text") or x.get("registered_address")]
    followups.sort(key=lambda x: x.get("time", ""), reverse=True)

    lines = ["客戶資訊"]

    if target_type == "buyer":
        intent_map = {"rent": "租屋", "buy": "買賣", "both": "租買皆可"}
        lines.extend([
            f"姓名: {data.get('name', '')}",
            f"電話: {data.get('phone', '')}",
            f"客源來源: {data.get('source', '') or '-'}",
            f"需求類型: {intent_map.get(data.get('intent_type', ''), data.get('intent_type', '') or '-')}",
            f"預算: {data.get('budget_min', '') or data.get('rent_min', '') or '-'} ~ {data.get('budget_max', '') or data.get('rent_max', '') or '-'}",
            f"偏好區域: {data.get('preferred_areas', '') or '-'}",
            f"產品類型: {data.get('property_type', '') or '-'}",
            f"房數需求: {data.get('room_range', '') or '-'}",
            f"車位需求: {data.get('car_need', '') or '-'}",
        ])
    elif target_type == "seller":
        deal_map = {"sale": "買賣", "rent": "出租"}
        lines.extend([
            f"姓名: {data.get('name', '')}",
            f"電話: {data.get('phone', '')}",
            f"客源來源: {data.get('source', '') or '-'}",
            f"委託類型: {deal_map.get(data.get('deal_type', ''), data.get('deal_type', '') or '-')}",
            f"地址: {data.get('address', '') or '-'}",
            f"產品類型: {data.get('property_type', '') or '-'}",
            f"開價: {data.get('expected_price', '') or '-'}",
            f"底價: {data.get('min_price', '') or '-'}",
            f"委託到期日: {data.get('contract_end_date', '') or '-'}",
        ])
    else:
        lines.extend([
            f"姓名: {data.get('name', '')}",
            f"電話: {data.get('phone', '') or '-'}",
            f"來源: {data.get('source', '') or '-'}",
            f"地址: {data.get('address', '') or '-'}",
            f"戶籍地址: {data.get('registered_address', '') or '-'}",
            f"網址: {data.get('url', '') or '-'}",
            f"進度: {data.get('stage', '') or '-'}",
        ])

    lines.append("")
    lines.append("追蹤進度")

    if not followups:
        lines.append("目前沒有追蹤紀錄")
    else:
        for item in followups[:limit]:
            header_parts = [item.get('time', ''), item.get('channel', 'LINE')]
            if item.get("stage"):
                header_parts.append(item["stage"])
            creator = (item.get("created_by_name") or "").strip()
            sender = (item.get("sender_display_name") or "").strip()
            if creator:
                header_parts.append(f"KEYIN: {creator}")
            if sender:
                header_parts.append(f"留言者: {sender}")
            lines.append("｜".join([x for x in header_parts if x]))
            if item.get("text"):
                lines.append(item.get("text", ""))
            if item.get("registered_address"):
                lines.append(f"戶籍地址：{item['registered_address']}")
            lines.append("")

    output = "\n".join(lines).strip()
    return output[:4500]


def process_line_message_event(event):
    message = event.get("message") or {}
    if message.get("type") != "text":
        return {"handled": False}

    sender_display_name = get_line_sender_display_name(event)
    raw_text = message.get("text", "")

    parsed = parse_line_formatted_message(raw_text)
    if not parsed:
        quoted_result = process_quote_context_message(event)
        if quoted_result.get("handled"):
            return quoted_result

        freeform = parse_potential_development_freeform(raw_text)
        if freeform:
            parsed = freeform
        else:
            return {"handled": False}

    fields = parsed["fields"]
    action = parsed["action"]

    if action == "create_buyer_need":
        result = create_buyer_need(fields, event)
    elif action == "create_seller_listing":
        result = create_seller_listing(fields, event)
    elif action == "create_development":
        result = create_development(fields, event)
    elif action == "create_development_batch":
        result = create_development_batch(parsed.get("raw_text", raw_text), event)
    elif action == "query_records":
        target_type, doc = resolve_customer_record(fields)
        if not doc:
            result = {"handled": True, "ok": False, "reply_text": "查無唯一客戶，請補電話、地址或客戶ID"}
        else:
            result = {
                "handled": True,
                "ok": True,
                "reply_text": format_record_timeline(target_type, doc, limit=fields.get("limit", 10)),
                "target_type": target_type,
                "target_id": doc.id,
                "customer_name": (doc.to_dict() or {}).get("name", ""),
                "phone": (doc.to_dict() or {}).get("phone", ""),
                "parsed_tag": parsed.get("tag", ""),
            }
    elif action == "query_contract_end":
        ok, text, ctx = query_contract_end_text(fields)
        result = {"handled": True, "ok": ok, "reply_text": text}
        if ctx:
            result.update(ctx)
    else:
        target_type = fields.get("target_type", "")
        if action == "buyer_followup":
            target_type = "buyer"
        elif action == "seller_followup":
            target_type = "seller"
        elif action == "development_followup":
            target_type = "development"

        if target_type not in ("buyer", "seller", "development"):
            result = {"handled": True, "ok": False, "reply_text": "請提供對象：客需、委託 或 開發"}
        else:
            doc = find_customer_record(
                target_type=target_type,
                record_id=fields.get("record_id", ""),
                phone=fields.get("phone", ""),
                name=fields.get("name", ""),
                address=fields.get("address", ""),
            )
            if not doc:
                result = {"handled": True, "ok": False, "reply_text": "找不到唯一資料，請補客戶ID、正確電話或地址"}
            else:
                collection_name = "buyers" if target_type == "buyer" else ("sellers" if target_type == "seller" else "developments")
                doc_ref = db.collection(collection_name).document(doc.id)
                labels = dedupe_keep_order(["LINE紀錄"] + ensure_list(fields.get("labels")))
                summary_parts = []
                if fields.get("content"):
                    summary_parts.append(fields["content"])
                if fields.get("address") and target_type != "development":
                    summary_parts.append(f"地址/物件：{fields['address']}")
                if fields.get("price") and target_type != "development":
                    summary_parts.append(f"價格：{fields['price']}")
                if fields.get("registered_address") and target_type == "development":
                    summary_parts.append(f"戶籍地址：{fields['registered_address']}")
                summary_text = build_line_summary("；".join(summary_parts).strip() or "LINE 更新", event)

                update_kwargs = {
                    "target_type": target_type,
                    "doc_ref": doc_ref,
                    "content": summary_text,
                    "labels": labels,
                    "stage": normalize_development_status(fields.get("stage", "")) if target_type == "development" else fields.get("stage", ""),
                    "source": fields.get("source", "LINE"),
                    "event": event,
                }
                if target_type == "development":
                    update_kwargs["registered_address"] = fields.get("registered_address", "")
                    update_kwargs["extra_updates"] = {
                        "address": fields.get("address", ""),
                        "url": fields.get("url", ""),
                    }

                update_customer_note_and_labels(**update_kwargs)

                if action in ("buyer_followup", "seller_followup", "development_followup", "classify"):
                    add_customer_followup(
                        target_type=target_type,
                        customer_id=doc.id,
                        content=summary_text,
                        next_action=fields.get("next_action", ""),
                        next_contact_date=fields.get("next_contact_date", ""),
                        labels=labels,
                        line_event=event,
                        stage=normalize_development_status(fields.get("stage", "")) if target_type == "development" else "",
                        registered_address=fields.get("registered_address", "") if target_type == "development" else "",
                    )

                current_data = doc_ref.get().to_dict() or {}
                label_text = "客需" if target_type == "buyer" else ("委託" if target_type == "seller" else "開發")
                result = {
                    "handled": True,
                    "ok": True,
                    "reply_text": f"已寫入{label_text}：{current_data.get('name', '')}",
                    "target_type": target_type,
                    "target_id": doc.id,
                    "customer_name": current_data.get("name", ""),
                    "phone": current_data.get("phone", ""),
                    "parsed_tag": parsed.get("tag", ""),
                }

    save_line_log(
        parsed,
        event,
        "success" if result.get("ok") else "failed",
        target_type=result.get("target_type", ""),
        target_id=result.get("target_id", ""),
        note=result.get("reply_text", ""),
        sender_display_name=sender_display_name,
    )

    if result.get("ok") and result.get("target_type") and result.get("target_id"):
        incoming_message_id = message.get("id", "")
        save_line_message_link(
            incoming_message_id,
            result.get("target_type", ""),
            result.get("target_id", ""),
            customer_name=result.get("customer_name", ""),
            phone=result.get("phone", ""),
            source_event=event,
        )

    return result



@app.route("/developments")
@login_required
def developments():
    q = request.args.get("q", "").strip()
    current_stage = request.args.get("current_stage", "").strip()
    next_action = request.args.get("next_action", "").strip()
    source = request.args.get("source", "").strip()
    sort_by = request.args.get("sort_by", "created_at_desc")
    show_done = request.args.get("show_done", "").strip()

    docs = db.collection("developments").stream()
    all_items = [doc_to_dict(d) for d in docs]
    total_count = len(all_items)
    items = list(all_items)
    source_options = sorted({(x.get("source") or "").strip() for x in all_items if (x.get("source") or "").strip()})

    if q:
        items = [
            x for x in items
            if q in (x.get("name") or "")
            or q in (x.get("phone") or "")
            or q in (x.get("address") or "")
            or q in (x.get("registered_address") or "")
        ]

    if current_stage:
        items = [x for x in items if (x.get("current_stage") or x.get("stage") or "") == current_stage]
    if next_action:
        items = [x for x in items if (x.get("next_action") or "") == next_action]
    if source:
        items = [x for x in items if (x.get("source") or "") == source]
    if show_done != "1":
        items = [x for x in items if (x.get("current_stage") or x.get("stage") or "") not in DEVELOPMENT_HIDDEN_BY_DEFAULT]

    def parse_created_at(x):
        return x.get("created_at") or ""

    if sort_by == "created_at_asc":
        items.sort(key=parse_created_at)
    elif sort_by == "created_at_desc":
        items.sort(key=parse_created_at, reverse=True)
    elif sort_by == "name_asc":
        items.sort(key=lambda x: (x.get("name") or ""))
    elif sort_by == "name_desc":
        items.sort(key=lambda x: (x.get("name") or ""), reverse=True)
    else:
        items.sort(key=parse_created_at, reverse=True)

    return render_template(
        "developments.html",
        developments=items,
        q=q,
        current_stage=current_stage,
        next_action=next_action,
        source=source,
        source_options=source_options,
        show_done=show_done,
        sort_by=sort_by,
        development_current_stage_options=DEVELOPMENT_STATUS_OPTIONS,
        development_next_action_options=DEVELOPMENT_NEXT_ACTION_OPTIONS,
        total_count=total_count,
        filtered_count=len(items),
        label_docx_enabled=(next_action == "寄開發信"),
        label_docx_count=len([x for x in items if (x.get("registered_address") or "").strip()]),
    )


@app.route("/developments/new", methods=["POST"])
@login_required
def developments_new():
    form = request.form
    current_stage = normalize_development_status(form.get("current_stage", "").strip() or form.get("stage", "").strip() or "待聯繫")
    next_action = normalize_development_next_action(form.get("next_action", "").strip())
    next_action_date = form.get("next_action_date", "").strip()

    _manual_url = form.get("url", "").strip()
    data = {
        "name": form.get("name", "").strip() or "未填姓名",
        "phone": form.get("phone", "").strip(),
        "source": infer_development_source(form.get("source", "").strip(), _manual_url),
        "address": form.get("address", "").strip(),
        "registered_address": form.get("registered_address", "").strip(),
        "url": _manual_url,
        "current_stage": current_stage,
        "current_stage": current_stage,
        "stage": current_stage,
        "next_action": next_action,
        "next_action_date": next_action_date,
        "note": form.get("note", "").strip(),
        "record_date": now_taipei().strftime("%Y-%m-%d"),
        "created_at": now_taipei().isoformat(),
        "created_by_id": session.get("user_id"),
        "created_by_name": session.get("user_name"),
        "updated_at": now_taipei().isoformat(),
        "updated_by_id": session.get("user_id"),
        "updated_by_name": session.get("user_name"),
    }
    if not (data["name"] or data["phone"] or data["address"] or data["url"]):
        flash("至少請填姓名、電話、地址、網址其中一項", "danger")
        return redirect(url_for("developments"))

    doc_ref = db.collection("developments").document()
    doc_ref.set(data)
    if data["note"]:
        db.collection("development_followups").add({
            "development_id": doc_ref.id,
            "contact_time": now_taipei().strftime("%Y-%m-%d %H:%M"),
            "channel": "手動新增",
            "current_stage": current_stage,
            "stage": current_stage,
            "next_action": next_action,
            "next_action_date": next_action_date,
            "registered_address": data["registered_address"],
            "content": data["note"],
            "next_contact_date": next_action_date,
            "created_at": now_taipei().isoformat(),
            "created_by_id": session.get("user_id"),
            "created_by_name": session.get("user_name"),
            "sender_display_name": session.get("user_name"),
        })

    flash("已新增開發", "success")
    return redirect(url_for("developments"))


@app.route("/developments/<development_id>")
@login_required
def development_detail(development_id):
    doc = db.collection("developments").document(development_id).get()
    if not doc.exists:
        flash("找不到這筆開發", "danger")
        return redirect(url_for("developments"))

    development = doc_to_dict(doc)
    followups_ref = db.collection("development_followups").where("development_id", "==", development_id)
    followups = [doc_to_dict(f) for f in followups_ref.stream()]
    followups.sort(key=lambda x: x.get("contact_time", ""), reverse=True)
    return render_template(
        "development_detail.html",
        development=development,
        followups=followups,
        development_current_stage_options=DEVELOPMENT_STATUS_OPTIONS,
        development_next_action_options=DEVELOPMENT_NEXT_ACTION_OPTIONS,
    )


@app.route("/developments/<development_id>/quick-flow", methods=["POST"])
@app.route("/developments/<development_id>/quick-stage", methods=["POST"])
@login_required
def development_quick_flow(development_id):
    current_stage = normalize_development_status(request.form.get("current_stage", "").strip() or request.form.get("stage", "").strip())
    next_action = normalize_development_next_action(request.form.get("next_action", "").strip())
    next_action_date = request.form.get("next_action_date", "").strip()

    if not current_stage and not next_action and not next_action_date:
        flash("請至少調整一個欄位", "warning")
        return redirect(request.referrer or url_for("developments"))

    updates = {
        "updated_at": now_taipei().isoformat(),
        "updated_by_id": session.get("user_id"),
        "updated_by_name": session.get("user_name"),
    }
    if current_stage:
        updates["current_stage"] = current_stage
        updates["stage"] = current_stage
    if next_action:
        updates["next_action"] = next_action
    if next_action_date:
        updates["next_action_date"] = next_action_date

    db.collection("developments").document(development_id).update(updates)
    flash("已更新開發流程", "success")
    return redirect(request.referrer or url_for("developments"))


@app.route("/developments/<development_id>/followup", methods=["POST"])
@login_required
def add_development_followup(development_id):
    contact_time = request.form.get("contact_time", "").strip() or now_taipei().strftime("%Y-%m-%d %H:%M")
    channel = request.form.get("channel", "").strip()
    current_stage = normalize_development_status(request.form.get("current_stage", "").strip() or request.form.get("stage", "").strip())
    next_action = normalize_development_next_action(request.form.get("next_action", "").strip())
    next_action_date = request.form.get("next_action_date", "").strip() or request.form.get("next_contact_date", "").strip()
    registered_address = request.form.get("registered_address", "").strip()
    content = request.form.get("content", "").strip()
    note_extra = request.form.get("note", "").strip()
    if note_extra:
        content = (content + ("\n" if content else "") + note_extra).strip()

    now = now_taipei().isoformat()
    db.collection("development_followups").add({
        "development_id": development_id,
        "contact_time": contact_time,
        "channel": channel,
        "current_stage": current_stage,
        "current_stage": current_stage,
        "stage": current_stage,
        "next_action": next_action,
        "next_action_date": next_action_date,
        "registered_address": registered_address,
        "content": content,
        "next_contact_date": next_action_date,
        "created_at": now,
        "created_by_id": session.get("user_id"),
        "created_by_name": session.get("user_name"),
        "sender_display_name": session.get("user_name"),
    })

    updates = {
        "updated_at": now,
        "updated_by_id": session.get("user_id"),
        "updated_by_name": session.get("user_name"),
    }
    if current_stage:
        updates["current_stage"] = current_stage
        updates["stage"] = current_stage
    if next_action:
        updates["next_action"] = next_action
    if next_action_date:
        updates["next_action_date"] = next_action_date
    if registered_address:
        updates["registered_address"] = registered_address

    ref = db.collection("developments").document(development_id)
    snap = ref.get()
    current = snap.to_dict() or {}
    if content:
        updates["note"] = append_note_block(current.get("note", ""), content, session.get("user_name") or "後台")
    ref.update(updates)

    flash("已新增開發追蹤紀錄", "success")
    return redirect(url_for("development_detail", development_id=development_id))


@app.route("/developments/<development_id>/edit", methods=["GET", "POST"])
@login_required
def development_edit(development_id):
    doc_ref = db.collection("developments").document(development_id)
    doc = doc_ref.get()
    if not doc.exists:
        flash("找不到這筆開發", "danger")
        return redirect(url_for("developments"))

    development = doc_to_dict(doc)
    if request.method == "POST":
        form = request.form
        current_stage = normalize_development_status(form.get("current_stage", "").strip() or form.get("stage", "").strip())
        next_action = normalize_development_next_action(form.get("next_action", "").strip())
        next_action_date = form.get("next_action_date", "").strip()
        updated = {
            "name": form.get("name", "").strip() or "未填姓名",
            "phone": form.get("phone", "").strip(),
            "source": form.get("source", "").strip(),
            "address": form.get("address", "").strip(),
            "registered_address": form.get("registered_address", "").strip(),
            "url": form.get("url", "").strip(),
            "current_stage": current_stage,
            "stage": current_stage,
            "next_action": next_action,
            "next_action_date": next_action_date,
            "note": form.get("note", "").strip(),
            "updated_at": now_taipei().isoformat(),
            "updated_by_id": session.get("user_id"),
            "updated_by_name": session.get("user_name"),
        }
        doc_ref.update(updated)
        flash("已更新開發資料", "success")
        return redirect(url_for("development_detail", development_id=development_id))

    return render_template(
        "development_edit.html",
        development=development,
        development_current_stage_options=DEVELOPMENT_STATUS_OPTIONS,
        development_next_action_options=DEVELOPMENT_NEXT_ACTION_OPTIONS,
    )


@app.route("/developments/<development_id>/delete", methods=["POST"])
@login_required
def development_delete(development_id):
    delete_by_field("development_followups", "development_id", development_id)
    db.collection("developments").document(development_id).delete()
    flash("已刪除開發與相關追蹤紀錄", "info")
    return redirect(url_for("developments"))


@app.route("/developments/<development_id>/followup/<followup_id>/edit", methods=["GET", "POST"])
@login_required
def development_followup_edit(development_id, followup_id):
    doc_ref = db.collection("development_followups").document(followup_id)
    doc = doc_ref.get()
    if not doc.exists:
        flash("找不到這筆追蹤紀錄", "danger")
        return redirect(url_for("development_detail", development_id=development_id))

    followup = doc_to_dict(doc)
    if request.method == "POST":
        contact_time = request.form.get("contact_time", "").strip() or now_taipei().strftime("%Y-%m-%d %H:%M")
        channel = request.form.get("channel", "").strip()
        current_stage = normalize_development_status(request.form.get("current_stage", "").strip() or request.form.get("stage", "").strip())
        next_action = normalize_development_next_action(request.form.get("next_action", "").strip())
        next_action_date = request.form.get("next_action_date", "").strip() or request.form.get("next_contact_date", "").strip()
        registered_address = request.form.get("registered_address", "").strip()
        content = request.form.get("content", "").strip()

        doc_ref.update({
            "contact_time": contact_time,
            "channel": channel,
            "current_stage": current_stage,
            "stage": current_stage,
            "next_action": next_action,
            "next_action_date": next_action_date,
            "registered_address": registered_address,
            "content": content,
            "next_contact_date": next_action_date,
        })

        updates = {
            "updated_at": now_taipei().isoformat(),
            "updated_by_id": session.get("user_id"),
            "updated_by_name": session.get("user_name"),
        }
        if current_stage:
            updates["current_stage"] = current_stage
            updates["stage"] = current_stage
        if next_action:
            updates["next_action"] = next_action
        if next_action_date:
            updates["next_action_date"] = next_action_date
        if registered_address:
            updates["registered_address"] = registered_address
        db.collection("developments").document(development_id).update(updates)

        flash("已更新追蹤紀錄", "success")
        return redirect(url_for("development_detail", development_id=development_id))

    return render_template(
        "development_followup_edit.html",
        development_id=development_id,
        followup=followup,
        development_current_stage_options=DEVELOPMENT_STATUS_OPTIONS,
        development_next_action_options=DEVELOPMENT_NEXT_ACTION_OPTIONS,
    )


@app.route("/developments/<development_id>/followup/<followup_id>/delete", methods=["POST"])
@login_required
def development_followup_delete(development_id, followup_id):
    db.collection("development_followups").document(followup_id).delete()
    flash("已刪除追蹤紀錄", "info")
    return redirect(url_for("development_detail", development_id=development_id))




def _filter_development_items_from_args(args):
    q = (args.get("q") or "").strip()
    current_stage = (args.get("current_stage") or "").strip()
    next_action = (args.get("next_action") or "").strip()
    source = (args.get("source") or "").strip()
    sort_by = (args.get("sort_by") or "created_at_desc").strip()
    show_done = (args.get("show_done") or "").strip()

    items = [doc_to_dict(d) for d in db.collection("developments").stream()]
    if q:
        items = [
            x for x in items
            if q in (x.get("name") or "")
            or q in (x.get("phone") or "")
            or q in (x.get("address") or "")
            or q in (x.get("registered_address") or "")
        ]
    if current_stage:
        items = [x for x in items if (x.get("current_stage") or x.get("stage") or "") == current_stage]
    if next_action:
        items = [x for x in items if (x.get("next_action") or "") == next_action]
    if source:
        items = [x for x in items if (x.get("source") or "") == source]
    if show_done != "1":
        items = [x for x in items if (x.get("current_stage") or x.get("stage") or "") not in DEVELOPMENT_HIDDEN_BY_DEFAULT]

    def parse_created_at(x):
        return x.get("created_at") or ""

    if sort_by == "created_at_asc":
        items.sort(key=parse_created_at)
    elif sort_by == "created_at_desc":
        items.sort(key=parse_created_at, reverse=True)
    elif sort_by == "name_asc":
        items.sort(key=lambda x: (x.get("name") or ""))
    elif sort_by == "name_desc":
        items.sort(key=lambda x: (x.get("name") or ""), reverse=True)
    else:
        items.sort(key=parse_created_at, reverse=True)
    return items



def _build_label_recipient_text(item) -> str:
    """
    第二行優先顯示使用者輸入的標題，沒有的話才退回姓名。
    不再自動補「負責人收」。
    """
    if isinstance(item, dict):
        return (item.get("label_title") or item.get("name") or "").strip()
    return (item or "").strip()


def _build_development_label_rows(items):
    rows = []
    for item in items:
        # 開發列表目前有顯示的資料都要能印。
        # 優先用戶籍地址；若沒有戶籍地址，就退回一般地址。
        address = (item.get("registered_address") or item.get("address") or "").strip()
        if not address:
            continue
        rows.append({
            "address": address,
            "recipient": _build_label_recipient_text(item),
        })
    return rows


def _find_development_label_template_path():
    """
    以使用者提供的 Word 標籤模板為準，直接套用原始版型，
    讓字體、字級、段落間距、表格尺寸都跟模板完全一致。
    """
    candidates = []
    env_path = (os.environ.get("DEVELOPMENT_LABEL_TEMPLATE_PATH") or "").strip()
    if env_path:
        candidates.append(env_path)

    candidates.extend([
        os.path.join(BASE_DIR, "空白標籤輸入表.docx"),
        os.path.join(BASE_DIR, "development_label_template.docx"),
        "空白標籤輸入表.docx",
        "development_label_template.docx",
    ])

    for path in candidates:
        if path and os.path.exists(path):
            return path
    return ""


def _clear_paragraph_text_preserve_format(paragraph):
    for run in paragraph.runs:
        run.text = ""


def _write_text_into_template_paragraph(paragraph, text: str):
    text = text or ""
    if paragraph.runs:
        paragraph.runs[0].text = text
        for run in paragraph.runs[1:]:
            run.text = ""
    else:
        paragraph.add_run(text)


def _fill_label_cell_like_template(cell, address: str, recipient: str):
    """
    直接沿用模板 cell 內原有的段落結構：
    第 1 段空白
    第 2 段地址
    第 3 段空白
    第 4 段標題/姓名
    這樣字型、字級、粗細、每段之間的距離都會維持跟模板一致。
    """
    while len(cell.paragraphs) < 4:
        cell.add_paragraph()

    for p in cell.paragraphs:
        _clear_paragraph_text_preserve_format(p)

    _write_text_into_template_paragraph(cell.paragraphs[1], (address or "").strip())
    _write_text_into_template_paragraph(cell.paragraphs[3], (recipient or "").strip())


def _render_development_labels_docx(rows):
    """
    使用使用者提供的空白 Word 標籤模板直接填值，
    讓輸出格式與原模板完全一致（字體 / 字級 / 行距 / 欄距 / 表格尺寸）。
    """
    if Document is None:
        raise RuntimeError("python-docx 未安裝")

    template_path = _find_development_label_template_path()
    if not template_path:
        raise RuntimeError("找不到標籤模板，請把『空白標籤輸入表.docx』放在專案根目錄")

    doc = Document(template_path)
    body = doc._body._element

    labels_per_page = 16
    needed_pages = max(1, (len(rows) + labels_per_page - 1) // labels_per_page)

    if not doc.tables:
        raise RuntimeError("標籤模板內沒有表格，無法產生標籤")

    # 模板結構：每頁 1 個 8x2 表格，表格後接一個分頁段落
    body_children = list(body)
    if len(body_children) < 2:
        raise RuntimeError("標籤模板結構異常，請重新提供模板檔")

    proto_table = deepcopy(body_children[0])
    proto_separator = deepcopy(body_children[1])

    current_pages = len(doc.tables)
    while current_pages < needed_pages:
        insert_at = max(0, len(body) - 2)   # 保留最後的空白段落與 sectPr
        body.insert(insert_at, deepcopy(proto_table))
        body.insert(insert_at + 1, deepcopy(proto_separator))
        current_pages += 1

    # 只保留實際需要的頁數，其餘模板頁刪掉，避免印出空白頁
    keep_prefix_count = needed_pages * 2
    for el in list(body)[keep_prefix_count:-2]:
        body.remove(el)

    all_cells = []
    for table in doc.tables[:needed_pages]:
        for row in table.rows:
            for cell in row.cells:
                all_cells.append(cell)

    for idx, cell in enumerate(all_cells):
        if idx < len(rows):
            row = rows[idx]
            _fill_label_cell_like_template(
                cell,
                row.get("address", ""),
                row.get("recipient", ""),
            )
        else:
            _fill_label_cell_like_template(cell, "", "")

    bio = BytesIO()
    doc.save(bio)
    bio.seek(0)
    return bio


@app.route("/developments/labels-docx")
@login_required
def development_labels_docx():
    args = request.args.to_dict(flat=True)
    if not (args.get("next_action") or "").strip():
        args["next_action"] = "寄開發信"
    items = _filter_development_items_from_args(args)
    rows = _build_development_label_rows(items)
    if not rows:
        flash("目前沒有可列印的寄開發信標籤資料（需有戶籍地址或地址）", "warning")
        return redirect(request.referrer or url_for("developments", **args))
    try:
        bio = _render_development_labels_docx(rows)
    except Exception as e:
        flash(f"產生 Word 標籤失敗：{e}", "danger")
        return redirect(request.referrer or url_for("developments", **args))
    filename = f"開發信標籤_{now_taipei().strftime('%Y%m%d_%H%M%S')}.docx"
    return send_file(
        bio,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@app.route("/developments/download")
@login_required
def download_developments():
    docs = db.collection("developments").stream()
    rows = [doc_to_dict(d) for d in docs]
    si = StringIO()
    writer = csv.writer(si)
    writer.writerow(["id","日期","姓名","電話","地址","戶籍地址","網址","目前狀態","下一動作","下次日期","來源","內部備註","建立時間","建立者"])
    for r in rows:
        writer.writerow([
            r.get("id",""),
            r.get("record_date",""),
            r.get("name",""),
            r.get("phone",""),
            r.get("address",""),
            r.get("registered_address",""),
            r.get("url",""),
            r.get("current_stage","") or r.get("stage",""),
            r.get("next_action",""),
            r.get("next_action_date",""),
            r.get("source",""),
            r.get("note",""),
            r.get("created_at",""),
            r.get("created_by_name",""),
        ])
    csv_data = "\ufeff" + si.getvalue()
    response = Response(csv_data, mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = "attachment; filename=developments.csv"
    return response

# ========= 開發自由格式解析（新版覆蓋） =========
DEVELOPMENT_SOURCE_SELLER_SELF = "屋主自售/踩線"
DEVELOPMENT_SOURCE_STREET = "掃街"

def infer_development_source(explicit_source: str, url: str) -> str:
    explicit = (explicit_source or "").strip()
    if explicit in (DEVELOPMENT_SOURCE_STREET, DEVELOPMENT_SOURCE_SELLER_SELF):
        return explicit
    if explicit and explicit.upper() != "LINE":
        return explicit
    return DEVELOPMENT_SOURCE_SELLER_SELF if (url or "").strip() else DEVELOPMENT_SOURCE_STREET

def normalize_line_key(key: str):
    k = (key or "").strip().replace(" ", "")
    mapping = {
        "對象": "target_type",
        "類型": "target_type",
        "目標": "target_type",
        "客戶類型": "target_type",
        "電話": "phone",
        "手機": "phone",
        "電話號碼": "phone",
        "姓名": "name",
        "客戶": "name",
        "買方": "name",
        "賣方": "name",
        "屋主": "name",
        "客戶來源": "source",
        "來源": "source",
        "需求類型": "intent_type_raw",
        "委託類型": "deal_type_raw",
        "ID": "record_id",
        "客戶ID": "record_id",
        "buyer_id": "record_id",
        "seller_id": "record_id",
        "development_id": "record_id",
        "內容": "content",
        "紀錄": "content",
        "備註": "content",
        "說明": "content",
        "進度內容": "content",
        "進程": "current_stage",
        "階段": "current_stage",
        "狀態": "current_stage",
        "進度": "current_stage",
        "目前狀況": "current_stage",
        "目前狀態": "current_stage",
        "標籤": "labels",
        "分類": "labels",
        "labels": "labels",
        "下一步": "next_action",
        "下次行動": "next_action",
        "next_action": "next_action",
        "下一次時間": "next_action_date",
        "下次時間": "next_action_date",
        "下次日期": "next_action_date",
        "下次聯絡日": "next_action_date",
        "下次聯絡日期": "next_action_date",
        "next_contact_date": "next_action_date",
        "地址": "address",
        "物件": "address",
        "案名": "address",
        "戶籍地址": "registered_address",
        "戶籍地": "registered_address",
        "網址": "url",
        "連結": "url",
        "網站": "url",
        "日期": "record_date",
        "總價": "price",
        "價格": "price",
        "預算": "budget",
        "區域": "preferred_areas",
        "產品類型": "property_type",
        "房數": "room_range",
        "車位": "car_need",
        "開價": "expected_price",
        "底價": "min_price",
        "委託到期日": "contract_end_date",
        "筆數": "limit",
    }
    return mapping.get(k, k)

def _looks_like_url(line: str) -> bool:
    s = (line or "").strip()
    return bool(re.search(r'(https?://|www\.|591\.com|rakuya|house|chyi\.com)', s, re.I))

def _extract_first_url(line: str) -> str:
    m = re.search(r'(https?://\S+|www\.\S+)', line or '', re.I)
    return m.group(1).strip() if m else ''

def _looks_like_phone(line: str) -> bool:
    s = re.sub(r'\s+', '', line or '')
    return bool(re.search(r'(09\d{2}[- ]?\d{3}[- ]?\d{3}|0\d{1,2}[- ]?\d{6,8})', s))

def _extract_first_phone(line: str) -> str:
    m = re.search(r'(09\d{2}[- ]?\d{3}[- ]?\d{3}|0\d{1,2}[- ]?\d{6,8})', line or '')
    return m.group(1).strip() if m else ''

def _looks_like_address(line: str) -> bool:
    s = (line or '').strip()
    if not s:
        return False
    patterns = [
        r'[台臺]中市?.{0,15}(區|里|路|街|段|巷|弄|號)',
        r'.+(路|街|大道|段).*(巷|弄|號)',
        r'.+(路|街|大道|段).*(號)',
        r'.+(區).*(路|街|段|巷|弄|號)',
    ]
    return any(re.search(p, s) for p in patterns)

def _looks_like_name(line: str) -> bool:
    s = (line or '').strip()
    if not s or len(s) > 12:
        return False
    if _looks_like_url(s) or _looks_like_phone(s) or _looks_like_address(s):
        return False
    bad = ['目前狀況','目前狀態','下一步','下次','網址','地址','戶籍地址','備註','來源']
    if any(b in s for b in bad):
        return False
    return True

def _extract_date_text(line: str) -> str:
    s = (line or '').strip()
    m = re.search(r'(\d{4}-\d{1,2}-\d{1,2})', s)
    if m:
        return m.group(1)
    m = re.search(r'(\d{1,2}/\d{1,2})', s)
    if m:
        mm, dd = m.group(1).split('/')
        year = now_taipei().year
        return f"{year}-{int(mm):02d}-{int(dd):02d}"
    return ''

def _find_status_in_text(text: str) -> str:
    options = sorted(DEVELOPMENT_STATUS_OPTIONS, key=len, reverse=True)
    raw = text or ''
    for opt in options:
        if opt in raw:
            return opt
    aliases = {
        '已寄開發信': '已寄開發信待跑開發',
        '待聯絡': '待聯繫',
        '已聯絡': '已聯繫',
        '已連繫': '已聯繫',
        '再聯絡': '持續追蹤',
        '再聯繫': '持續追蹤',
    }
    for k, v in aliases.items():
        if k in raw:
            return v
    return ''

def _find_next_action_in_text(text: str) -> str:
    options = sorted(DEVELOPMENT_NEXT_ACTION_OPTIONS, key=len, reverse=True)
    raw = text or ''
    for opt in options:
        if opt in raw:
            return opt
    aliases = {
        '電話聯絡': '電話聯繫',
        'LINE聯絡': 'LINE聯繫',
        '再次聯絡': '再次聯繫',
    }
    for k, v in aliases.items():
        if k in raw:
            return v
    return ''

def parse_flexible_development_chunk(raw_text: str) -> dict:
    text = (raw_text or '').replace('\r\n', '\n').strip()
    if not text:
        return {}

    fields = {}
    notes = []
    candidate_names = []

    for raw_line in text.split('\n'):
        line = raw_line.strip()
        if not line:
            continue

        m = re.match(r'^([^:：]+)\s*[:：]\s*(.*)$', line)
        if m:
            key = normalize_line_key(m.group(1))
            value = (m.group(2) or '').strip()
            if key == 'labels':
                fields[key] = parse_label_csv(value)
            elif value:
                fields[key] = value
            continue

        url = _extract_first_url(line)
        phone = _extract_first_phone(line)
        date_txt = _extract_date_text(line)

        if url and not fields.get('url'):
            fields['url'] = url
            leftover = line.replace(url, '').strip()
            if leftover:
                notes.append(leftover)
            continue

        if phone and not fields.get('phone'):
            fields['phone'] = phone
            leftover = line.replace(phone, '').strip(' ：:')
            if leftover and _looks_like_name(leftover):
                candidate_names.append(leftover)
            elif leftover:
                notes.append(leftover)
            continue

        if _looks_like_address(line):
            if not fields.get('address'):
                fields['address'] = line
            elif not fields.get('registered_address') and ('戶籍' in line or '住址' in line):
                fields['registered_address'] = line
            else:
                notes.append(line)
            continue

        if date_txt and not fields.get('next_action_date') and any(word in line for word in ['下次','再','聯絡','時間','日期']):
            fields['next_action_date'] = date_txt
            leftover = line.replace(date_txt, '').strip(' ：:')
            if leftover and leftover not in ('下次','下次時間','下一次時間','下次日期'):
                notes.append(leftover)
            continue

        if _looks_like_name(line):
            candidate_names.append(line)
            continue

        notes.append(line)

    if not fields.get('name') and candidate_names:
        fields['name'] = candidate_names[0]

    joined_notes = '\n'.join(notes).strip()
    if joined_notes:
        fields['content'] = ((fields.get('content', '') + '\n' + joined_notes).strip() if fields.get('content') else joined_notes)

    # 自動從整段文字抓狀態 / 下一步 / 日期
    big_text = text
    if not fields.get('current_stage'):
        auto_stage = _find_status_in_text(big_text)
        fields['current_stage'] = normalize_development_status(auto_stage or '待聯繫')
    else:
        fields['current_stage'] = normalize_development_status(fields.get('current_stage'))

    if not fields.get('next_action'):
        fields['next_action'] = normalize_development_next_action(_find_next_action_in_text(big_text))
    else:
        fields['next_action'] = normalize_development_next_action(fields.get('next_action'))

    if not fields.get('next_action_date'):
        auto_date = _extract_date_text(big_text)
        if auto_date:
            fields['next_action_date'] = auto_date

    fields['source'] = infer_development_source(fields.get('source', ''), fields.get('url', ''))
    if not fields.get('record_date'):
        fields['record_date'] = now_taipei().strftime('%Y-%m-%d')

    has_any = any(fields.get(k) for k in ['name','phone','address','url','content'])
    return fields if has_any else {}

def parse_development_batch_message(raw_text: str):
    text = (raw_text or '').replace('\r\n', '\n').strip()
    lines = text.split('\n')
    if lines and lines[0].strip().startswith('#'):
        lines = lines[1:]
    body = '\n'.join(lines).strip()
    if not body:
        return []

    if re.search(r'(?m)^/{3,}\s*$', body):
        chunks = [c.strip() for c in re.split(r'(?m)^/{3,}\s*$', body) if c.strip()]
    elif re.search(r'(?m)^-{3,}\s*$', body):
        chunks = [c.strip() for c in re.split(r'(?m)^-{3,}\s*$', body) if c.strip()]
    else:
        # 沒有分隔線時，嘗試依地址行切段
        lines2 = [ln.rstrip() for ln in body.split('\n')]
        chunks, current = [], []
        seen_first = False
        for ln in lines2:
            s = ln.strip()
            if not s:
                continue
            if _looks_like_address(s) and seen_first and current:
                chunks.append('\n'.join(current).strip())
                current = [s]
            else:
                current.append(s)
                if _looks_like_address(s):
                    seen_first = True
        if current:
            chunks.append('\n'.join(current).strip())

    items = []
    for chunk in chunks:
        fields = parse_flexible_development_chunk(chunk)
        if fields:
            items.append(fields)
    return items

def parse_line_formatted_message(text: str):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None

    first = lines[0]
    if not first.startswith("#"):
        return None

    tag = first.lstrip("#").strip()
    tag_map = {
        "新增客需": "create_buyer_need",
        "新增委託": "create_seller_listing",
        "新增開發": "create_development",
        "新增開發批次": "create_development_batch",
        "開發追蹤": "development_followup",
        "買方追蹤": "buyer_followup",
        "賣方追蹤": "seller_followup",
        "客戶分類": "classify",
        "查詢紀錄": "query_records",
        "查詢委託到期": "query_contract_end",
        "帶看": "buyer_followup",
        "成交": "buyer_followup",
        "委託": "seller_followup",
        "紀錄": "generic_note",
    }
    action = tag_map.get(tag)
    if not action:
        return None

    raw_body = "\n".join(lines[1:]).strip()

    if action == "create_development_batch":
        return {
            "tag": tag,
            "action": action,
            "fields": {},
            "raw_text": text,
            "raw_body": raw_body,
        }

    if action == "create_development":
        fields = parse_flexible_development_chunk(raw_body)
        if not fields:
            return None
        return {
            "tag": tag,
            "action": action,
            "fields": fields,
            "raw_text": text,
            "raw_body": raw_body,
        }

    fields = {}
    for line in lines[1:]:
        m = re.match(r"^([^:：]+)\s*[:：]\s*(.+)$", line)
        if not m:
            continue
        key = normalize_line_key(m.group(1))
        value = m.group(2).strip()
        if key == "labels":
            fields[key] = parse_label_csv(value)
        else:
            fields[key] = value

    if tag in ("買方追蹤", "帶看", "成交") and not fields.get("target_type"):
        fields["target_type"] = "buyer"
    if tag in ("賣方追蹤", "委託") and not fields.get("target_type"):
        fields["target_type"] = "seller"
    if tag == "開發追蹤" and not fields.get("target_type"):
        fields["target_type"] = "development"

    fields["target_type"] = normalize_target_type(fields.get("target_type", "")) or fields.get("target_type", "")
    fields["intent_type"] = normalize_intent_type(fields.get("intent_type_raw", ""), fields)
    fields["deal_type"] = normalize_deal_type(fields.get("deal_type_raw", ""))
    fields["limit"] = parse_int_limit(fields.get("limit", 10), default=10, max_value=30)

    if action == "create_buyer_need":
        if not (fields.get("name") and fields.get("phone")):
            return None
    elif action == "create_seller_listing":
        if not (fields.get("name") and fields.get("phone")):
            return None
    elif action in ("buyer_followup", "seller_followup", "classify", "query_records", "query_contract_end", "development_followup"):
        if not (fields.get("record_id") or fields.get("phone") or fields.get("name") or fields.get("address")):
            return None

    return {
        "tag": tag,
        "action": action,
        "fields": fields,
        "raw_text": text,
        "raw_body": raw_body,
    }

def find_development_record(record_id: str = "", phone: str = "", name: str = "", address: str = ""):
    col = db.collection("developments")
    if record_id:
        doc = col.document(record_id).get()
        if doc.exists:
            return doc
    if phone:
        matches = find_records_by_phone("developments", phone)
        if len(matches) == 1:
            return matches[0]
    if name:
        docs = list(col.where("name", "==", name.strip()).limit(2).stream())
        if len(docs) == 1:
            return docs[0]
    if address:
        docs = list(col.where("address", "==", address.strip()).limit(2).stream())
        if len(docs) == 1:
            return docs[0]
    return None

def create_development(fields, event):
    phone = fields.get("phone", "").strip()
    name = fields.get("name", "").strip() or "未填姓名"
    url = fields.get("url", "").strip()
    source = infer_development_source(fields.get("source", ""), url)
    address = fields.get("address", "").strip()

    matches = find_records_by_phone("developments", phone) if phone else []
    if not matches and address:
        doc = find_development_record(address=address)
        if doc:
            matches = [doc]

    labels = build_development_labels(fields.get("labels"))
    content_text = fields.get("content", "").strip() or address or url or "LINE 新增開發"
    note_content = build_line_summary(content_text, event)

    payload = {
        "name": name,
        "phone": phone,
        "source": source,
        "url": url,
        "address": address,
        "registered_address": fields.get("registered_address", "").strip(),
        "current_stage": normalize_development_status(fields.get("current_stage", "").strip() or fields.get("stage", "").strip() or "待聯繫"),
        "stage": normalize_development_status(fields.get("current_stage", "").strip() or fields.get("stage", "").strip() or "待聯繫"),
        "next_action": normalize_development_next_action(fields.get("next_action", "").strip()),
        "next_action_date": fields.get("next_action_date", "").strip() or fields.get("next_contact_date", "").strip(),
        "record_date": fields.get("record_date", "").strip() or now_taipei().strftime("%Y-%m-%d"),
        "note": "",
        "labels": labels,
        "updated_at": now_taipei().isoformat(),
        "updated_by_id": "line_bot",
        "updated_by_name": "LINE Bot",
        "sender_display_name": get_line_sender_display_name(event) or "",
    }

    if len(matches) == 1:
        doc = matches[0]
        doc_ref = db.collection("developments").document(doc.id)
        update_customer_note_and_labels(
            target_type="development",
            doc_ref=doc_ref,
            content=note_content,
            labels=labels,
            stage=payload["stage"],
            source=payload["source"],
            event=event,
        )
        doc_ref.update({k: v for k, v in payload.items() if v != "" and k != "note"})
        add_customer_followup(
            target_type="development",
            customer_id=doc.id,
            content=note_content,
            next_action=payload.get("next_action", ""),
            next_contact_date=payload.get("next_action_date", ""),
            labels=labels,
            line_event=event,
        )
        updated_doc = doc_ref.get().to_dict() or {}
        return {
            "handled": True,
            "ok": True,
            "reply_text": f"已註記開發：{updated_doc.get('name', '')}",
            "target_type": "development",
            "target_id": doc.id,
            "customer_name": updated_doc.get("name", ""),
            "phone": updated_doc.get("phone", ""),
            "parsed_tag": "新增開發",
        }

    if len(matches) > 1:
        return {
            "handled": True,
            "ok": False,
            "reply_text": "未寫入：同電話有多筆開發資料，請補地址或客戶ID",
        }

    now = now_taipei().isoformat()
    payload.update({
        "created_at": now,
        "created_by_id": "line_bot",
        "created_by_name": "LINE Bot",
        "note": append_note_block("", note_content, build_line_operator_label(event)),
    })
    doc_ref = db.collection("developments").document()
    doc_ref.set(payload)
    add_customer_followup(
        target_type="development",
        customer_id=doc_ref.id,
        content=note_content,
        next_action=payload.get("next_action", ""),
        next_contact_date=payload.get("next_action_date", ""),
        labels=labels,
        line_event=event,
    )
    return {
        "handled": True,
        "ok": True,
        "reply_text": f"已註記開發：{name}",
        "target_type": "development",
        "target_id": doc_ref.id,
        "customer_name": name,
        "phone": phone,
        "parsed_tag": "新增開發",
    }

def create_development_batch(raw_text, event):
    items = parse_development_batch_message(raw_text)
    if not items:
        return {"handled": True, "ok": False, "reply_text": "未寫入：沒有解析到有效開發資料"}

    ok_count = 0
    fail_count = 0
    last_result = None
    for fields in items:
        result = create_development(fields, event)
        last_result = result
        if result.get("ok"):
            ok_count += 1
        else:
            fail_count += 1

    reply_text = f"批量註記完成：成功 {ok_count} 筆，失敗 {fail_count} 筆"
    if last_result and last_result.get("target_type") and last_result.get("target_id"):
        return {
            "handled": True,
            "ok": ok_count > 0,
            "reply_text": reply_text,
            "target_type": last_result.get("target_type", ""),
            "target_id": last_result.get("target_id", ""),
            "customer_name": last_result.get("customer_name", ""),
            "phone": last_result.get("phone", ""),
            "parsed_tag": "新增開發批次",
        }
    return {"handled": True, "ok": ok_count > 0, "reply_text": reply_text}

def add_development_followup_via_line(fields, event):
    doc = find_development_record(
        record_id=fields.get("record_id", ""),
        phone=fields.get("phone", ""),
        name=fields.get("name", ""),
        address=fields.get("address", ""),
    )
    if not doc:
        return {"handled": True, "ok": False, "reply_text": "找不到唯一開發資料，請補電話、地址或客戶ID"}

    content = (fields.get("content") or "").strip() or "LINE 更新"
    current_stage = normalize_development_status(fields.get("current_stage", "").strip() or fields.get("stage", "").strip())
    next_action = normalize_development_next_action(fields.get("next_action", "").strip())
    next_action_date = fields.get("next_action_date", "").strip() or fields.get("next_contact_date", "").strip()
    registered_address = fields.get("registered_address", "").strip()
    source = infer_development_source(fields.get("source",""), fields.get("url",""))

    doc_ref = db.collection("developments").document(doc.id)
    update_customer_note_and_labels(
        target_type="development",
        doc_ref=doc_ref,
        content=build_line_summary(content, event),
        labels=build_development_labels(fields.get("labels")),
        stage=current_stage,
        source=source or None,
        event=event,
    )
    updates = {
        "updated_at": now_taipei().isoformat(),
        "updated_by_id": "line_bot",
        "updated_by_name": "LINE Bot",
    }
    if current_stage:
        updates["current_stage"] = current_stage
        updates["stage"] = current_stage
    if next_action:
        updates["next_action"] = next_action
    if next_action_date:
        updates["next_action_date"] = next_action_date
    if registered_address:
        updates["registered_address"] = registered_address
    if source:
        updates["source"] = source
    doc_ref.update(updates)

    add_customer_followup(
        target_type="development",
        customer_id=doc.id,
        content=build_line_summary(content, event),
        next_action=next_action,
        next_contact_date=next_action_date,
        labels=build_development_labels(fields.get("labels")),
        line_event=event,
    )

    data = doc_ref.get().to_dict() or {}
    return {
        "handled": True,
        "ok": True,
        "reply_text": f"已註記開發：{data.get('name', '')}",
        "target_type": "development",
        "target_id": doc.id,
        "customer_name": data.get("name", ""),
        "phone": data.get("phone", ""),
        "parsed_tag": "開發追蹤",
    }

def _maybe_freeform_development(text: str):
    raw = (text or '').strip()
    if not raw:
        return None
    body = raw
    if raw.startswith('#'):
        return None
    if '///' in raw or re.search(r'(?m)^/{3,}\s*$', raw):
        items = parse_development_batch_message(raw)
        return {"batch": True, "items": items} if items else None
    fields = parse_flexible_development_chunk(raw)
    return {"batch": False, "fields": fields} if fields else None

def process_line_message_event(event):
    message = event.get("message") or {}
    if message.get("type") != "text":
        return {"handled": False}

    sender_display_name = get_line_sender_display_name(event)
    raw_text = message.get("text", "")

    parsed = parse_line_formatted_message(raw_text)
    if not parsed:
        free = _maybe_freeform_development(raw_text)
        if free:
            if free.get("batch"):
                result = create_development_batch(raw_text, event)
            else:
                result = create_development(free["fields"], event)
            save_line_log(
                {"tag": "自由格式開發", "action": "create_development_batch" if free.get("batch") else "create_development",
                 "fields": free.get("fields", {}), "raw_text": raw_text},
                event,
                "success" if result.get("ok") else "failed",
                target_type=result.get("target_type", ""),
                target_id=result.get("target_id", ""),
                note=result.get("reply_text", ""),
                sender_display_name=sender_display_name,
            )
            if result.get("ok") and result.get("target_type") and result.get("target_id"):
                incoming_message_id = message.get("id", "")
                save_line_message_link(
                    incoming_message_id,
                    result["target_type"],
                    result["target_id"],
                    tag=result.get("parsed_tag", "自由格式開發"),
                    action="create_development",
                    customer_name=result.get("customer_name", ""),
                    phone=result.get("phone", ""),
                    source_event=event,
                )
            return result

        quoted_result = process_quote_context_message(event)
        if quoted_result.get("handled"):
            return quoted_result
        return {"handled": False}

    fields = parsed["fields"]
    action = parsed["action"]

    if action == "create_buyer_need":
        result = create_buyer_need(fields, event)
    elif action == "create_seller_listing":
        result = create_seller_listing(fields, event)
    elif action == "create_development":
        result = create_development(fields, event)
    elif action == "create_development_batch":
        result = create_development_batch(parsed.get("raw_body") or raw_text, event)
    elif action == "development_followup":
        result = add_development_followup_via_line(fields, event)
    elif action == "query_records":
        target_type, doc = resolve_customer_record(fields)
        if not doc and (fields.get("record_id") or fields.get("phone") or fields.get("name") or fields.get("address")):
            doc = find_development_record(fields.get("record_id",""), fields.get("phone",""), fields.get("name",""), fields.get("address",""))
            if doc:
                target_type = "development"
        if not doc:
            result = {"handled": True, "ok": False, "reply_text": "查無唯一客戶，請補電話、地址或客戶ID"}
        else:
            result = {
                "handled": True,
                "ok": True,
                "reply_text": format_record_timeline(target_type, doc, limit=fields.get("limit", 10)),
                "target_type": target_type,
                "target_id": doc.id,
                "customer_name": (doc.to_dict() or {}).get("name", ""),
                "phone": (doc.to_dict() or {}).get("phone", ""),
                "parsed_tag": parsed.get("tag", ""),
            }
    elif action == "query_contract_end":
        ok, text, ctx = query_contract_end_text(fields)
        result = {"handled": True, "ok": ok, "reply_text": text}
        if ctx:
            result.update(ctx)
    else:
        target_type = fields.get("target_type", "")
        if action == "buyer_followup":
            target_type = "buyer"
        elif action == "seller_followup":
            target_type = "seller"

        if target_type not in ("buyer", "seller"):
            result = {"handled": True, "ok": False, "reply_text": "請提供對象：買方 或 賣方"}
        else:
            doc = find_customer_record(
                target_type=target_type,
                record_id=fields.get("record_id", ""),
                phone=fields.get("phone", ""),
                name=fields.get("name", ""),
            )
            if not doc:
                result = {"handled": True, "ok": False, "reply_text": "找不到唯一客戶，請補客戶ID或正確電話"}
            else:
                doc_ref = db.collection("buyers" if target_type == "buyer" else "sellers").document(doc.id)
                labels = dedupe_keep_order(["LINE紀錄"] + ensure_list(fields.get("labels")))
                summary_parts = []
                if fields.get("content"):
                    summary_parts.append(fields["content"])
                if fields.get("address"):
                    summary_parts.append(f"地址/物件：{fields['address']}")
                if fields.get("price"):
                    summary_parts.append(f"價格：{fields['price']}")
                summary_text = build_line_summary("；".join(summary_parts).strip() or "LINE 更新", event)

                update_customer_note_and_labels(
                    target_type=target_type,
                    doc_ref=doc_ref,
                    content=summary_text,
                    labels=labels,
                    stage=fields.get("stage", ""),
                    source=fields.get("source", "LINE"),
                    event=event,
                )

                if action in ("buyer_followup", "seller_followup", "classify"):
                    add_customer_followup(
                        target_type=target_type,
                        customer_id=doc.id,
                        content=summary_text,
                        next_action=fields.get("next_action", ""),
                        next_contact_date=fields.get("next_contact_date", ""),
                        labels=labels,
                        line_event=event,
                    )

                label_text = "客需" if target_type == "buyer" else "委託"
                current_data = doc_ref.get().to_dict() or {}
                result = {
                    "handled": True,
                    "ok": True,
                    "reply_text": f"已註記{label_text}：{current_data.get('name', '')}",
                    "target_type": target_type,
                    "target_id": doc.id,
                    "customer_name": current_data.get("name", ""),
                    "phone": current_data.get("phone", ""),
                    "parsed_tag": parsed.get("tag", ""),
                }

    save_line_log(
        parsed,
        event,
        "success" if result.get("ok") else "failed",
        target_type=result.get("target_type", ""),
        target_id=result.get("target_id", ""),
        note=result.get("reply_text", ""),
        sender_display_name=sender_display_name,
    )

    if result.get("ok") and result.get("target_type") and result.get("target_id"):
        incoming_message_id = message.get("id", "")
        save_line_message_link(
            incoming_message_id,
            result["target_type"],
            result["target_id"],
            tag=result.get("parsed_tag", ""),
            action=action,
            customer_name=result.get("customer_name", ""),
            phone=result.get("phone", ""),
            source_event=event,
        )

    return result


# ========= 引用回覆：開發以 phone / target_id 自動對標（新版覆蓋） =========

def find_developments_by_phone(phone: str):
    normalized = normalize_phone(phone)
    if not normalized:
        return []
    matches = []
    for doc in db.collection("developments").stream():
        data = doc.to_dict() or {}
        if normalize_phone(data.get("phone", "")) == normalized:
            matches.append(doc)
    return matches

def _status_alias_from_plain_text(text: str) -> str:
    raw = (text or "").strip()
    mapping = {
        "已連繫": "已聯繫",
        "已聯繫": "已聯繫",
        "已連絡": "已聯繫",
        "已聯絡": "已聯繫",
        "待聯繫": "待聯繫",
        "待聯絡": "待聯繫",
        "已寄開發信": "已寄開發信待跑開發",
        "已寄信": "已寄開發信待跑開發",
        "已開發": "已開發",
        "待調謄本": "待調謄本",
        "已調謄本": "已調謄本",
        "持續追蹤": "持續追蹤",
        "已簽回": "已簽回",
        "無效": "無效",
    }
    return mapping.get(raw, "")

def _parse_development_quote_updates(raw_text: str):
    text = (raw_text or "").strip()
    updates = {}
    note_lines = []
    field_hit = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^([^:：]+)\s*[:：]\s*(.*)$", line)
        if m:
            key = normalize_line_key(m.group(1))
            value = (m.group(2) or "").strip()
            field_hit = True

            if key in ("current_stage", "stage"):
                if value:
                    updates["current_stage"] = normalize_development_status(value)
            elif key == "next_action":
                updates["next_action"] = normalize_development_next_action(value)
            elif key == "next_action_date":
                updates["next_action_date"] = value
            elif key == "registered_address":
                updates["registered_address"] = value
            elif key == "source":
                updates["source"] = infer_development_source(value, updates.get("url", ""))
            elif key == "url":
                updates["url"] = value
                if not updates.get("source"):
                    updates["source"] = infer_development_source("", value)
            elif key in ("content", "address", "name", "phone"):
                note_lines.append(f"{m.group(1).strip()}: {value}" if value else m.group(1).strip())
            else:
                note_lines.append(line)
        else:
            note_lines.append(line)

    joined = "\n".join([x for x in note_lines if x]).strip()

    # 沒寫欄位，但直接回「已連繫」這種狀態
    if not updates.get("current_stage"):
        status = _status_alias_from_plain_text(text)
        if status:
            updates["current_stage"] = status

    # 沒寫欄位，但整句像地址，當戶籍地址
    if not updates.get("registered_address") and _looks_like_address(text):
        updates["registered_address"] = text

    followup_text = joined or text
    if field_hit and not followup_text:
        parts = []
        if updates.get("current_stage"):
            parts.append(f"目前狀況：{updates['current_stage']}")
        if updates.get("next_action"):
            parts.append(f"下一步：{updates['next_action']}")
        if updates.get("next_action_date"):
            parts.append(f"下一次時間：{updates['next_action_date']}")
        if updates.get("registered_address"):
            parts.append(f"戶籍地址：{updates['registered_address']}")
        followup_text = "；".join(parts)

    return updates, followup_text.strip()

def _describe_development(doc):
    data = doc.to_dict() or {}
    return f"{data.get('name','未填姓名')} / {data.get('phone','-')} / {data.get('address','-')}"

def _resolve_development_from_link(link):
    # 先用 target_id
    target_id = (link.get("target_id") or "").strip()
    if target_id:
        doc = db.collection("developments").document(target_id).get()
        if doc.exists:
            return doc, []

    # 再用 phone
    phone = (link.get("phone") or "").strip()
    matches = find_developments_by_phone(phone) if phone else []
    if len(matches) == 1:
        return matches[0], []
    if len(matches) > 1:
        return None, matches

    # 再用 name 當最後手段
    name = (link.get("customer_name") or "").strip()
    if name:
        docs = list(db.collection("developments").where("name", "==", name).limit(5).stream())
        if len(docs) == 1:
            return docs[0], []
        if len(docs) > 1:
            return None, docs

    return None, []

def process_quote_context_message(event):
    message = event.get("message") or {}
    quoted_message_id = message.get("quotedMessageId", "")
    raw_text = (message.get("text") or "").strip()
    if not quoted_message_id or not raw_text:
        return {"handled": False}

    link = get_line_message_link(quoted_message_id)
    if not link:
        return {"handled": False}

    target_type = link.get("target_type", "")

    # ===== 開發：用 target_id / phone 自動對標 =====
    if target_type == "development":
        doc, multi = _resolve_development_from_link(link)
        if multi:
            preview = "\n".join([f"- {_describe_development(d)}" for d in multi[:5]])
            return {
                "handled": True,
                "ok": False,
                "reply_text": f"找到同電話/同姓名多筆開發，請確認要註記哪一筆：\n{preview}",
            }
        if not doc:
            phone = (link.get("phone") or "").strip()
            who = (link.get("customer_name") or "").strip()
            hint = f"（電話：{phone}，姓名：{who}）" if phone or who else ""
            return {
                "handled": True,
                "ok": False,
                "reply_text": f"找不到對應的開發資料{hint}",
            }

        updates, followup_text = _parse_development_quote_updates(raw_text)
        doc_ref = db.collection("developments").document(doc.id)
        current = doc.to_dict() or {}

        payload = {
            "updated_at": now_taipei().isoformat(),
            "updated_by_id": "line_bot",
            "updated_by_name": "LINE Bot",
        }

        if updates.get("current_stage"):
            payload["current_stage"] = updates["current_stage"]
            payload["stage"] = updates["current_stage"]   # 舊欄位相容
        if updates.get("next_action"):
            payload["next_action"] = updates["next_action"]
        if updates.get("next_action_date"):
            payload["next_action_date"] = updates["next_action_date"]
        if updates.get("registered_address"):
            payload["registered_address"] = updates["registered_address"]
        if updates.get("source") or updates.get("url"):
            payload["source"] = infer_development_source(updates.get("source", current.get("source", "")), updates.get("url", current.get("url", "")))
        if updates.get("url"):
            payload["url"] = updates["url"]

        doc_ref.update(payload)

        # note 與 followup 都寫進去
        labels = dedupe_keep_order(["LINE紀錄", "群組回覆註記"])
        update_customer_note_and_labels(
            target_type="development",
            doc_ref=doc_ref,
            content=followup_text,
            labels=labels,
            stage=updates.get("current_stage", ""),
            source=payload.get("source", current.get("source", "")),
            event=event,
        )
        add_customer_followup(
            target_type="development",
            customer_id=doc.id,
            content=followup_text,
            next_action=updates.get("next_action", ""),
            next_contact_date=updates.get("next_action_date", ""),
            labels=labels,
            line_event=event,
        )

        latest = doc_ref.get().to_dict() or {}
        reply_name = latest.get("name", "") or "未填姓名"
        reply_phone = latest.get("phone", "") or "-"
        return {
            "handled": True,
            "ok": True,
            "reply_text": f"已註記開發：{reply_name}（{reply_phone}）",
            "target_type": "development",
            "target_id": doc.id,
            "customer_name": reply_name,
            "phone": reply_phone,
            "parsed_tag": "群組回覆註記",
        }

    # ===== 客需 / 委託維持原邏輯 =====
    target_id = link.get("target_id", "")
    if target_type not in ("buyer", "seller") or not target_id:
        return {"handled": False}

    collection_name = "buyers" if target_type == "buyer" else "sellers"
    doc_ref = db.collection(collection_name).document(target_id)
    doc = doc_ref.get()
    if not doc.exists:
        return {"handled": True, "ok": False, "reply_text": "未寫入：引用的客戶資料不存在"}

    labels = dedupe_keep_order(["LINE紀錄", "群組回覆註記"])
    reply_only_text = raw_text

    update_customer_note_and_labels(
        target_type=target_type,
        doc_ref=doc_ref,
        content=reply_only_text,
        labels=labels,
        source="LINE",
        event=event,
    )
    add_customer_followup(
        target_type=target_type,
        customer_id=target_id,
        content=reply_only_text,
        labels=labels,
        line_event=event,
    )

    parsed = {
        "tag": "群組回覆註記",
        "action": "quoted_context_note",
        "fields": {"quoted_message_id": quoted_message_id},
        "raw_text": raw_text,
    }
    save_line_log(parsed, event, "success", target_type=target_type, target_id=target_id, sender_display_name=get_line_sender_display_name(event))
    target_label = "客需" if target_type == "buyer" else "委託"
    data = doc.to_dict() or {}
    return {
        "handled": True,
        "ok": True,
        "reply_text": f"已註記{target_label}：{data.get('name', '')}（{data.get('phone','-')}）",
        "target_type": target_type,
        "target_id": target_id,
        "customer_name": data.get("name", ""),
        "phone": data.get("phone", ""),
        "parsed_tag": "群組回覆註記",
    }

def process_line_message_event(event):
    message = event.get("message") or {}
    if message.get("type") != "text":
        return {"handled": False}

    sender_display_name = get_line_sender_display_name(event)
    raw_text = message.get("text", "")

    # 先處理引用回覆，避免「已連繫」被當成新開發
    quoted_result = process_quote_context_message(event)
    if quoted_result.get("handled"):
        parsed = {
            "tag": "群組回覆註記",
            "action": "quoted_context_note",
            "fields": {"quoted_message_id": (message or {}).get("quotedMessageId", "")},
            "raw_text": raw_text,
        }
        save_line_log(
            parsed,
            event,
            "success" if quoted_result.get("ok") else "failed",
            target_type=quoted_result.get("target_type", ""),
            target_id=quoted_result.get("target_id", ""),
            note=quoted_result.get("reply_text", ""),
            sender_display_name=sender_display_name,
        )
        if quoted_result.get("ok") and quoted_result.get("target_type") and quoted_result.get("target_id"):
            incoming_message_id = message.get("id", "")
            save_line_message_link(
                incoming_message_id,
                quoted_result["target_type"],
                quoted_result["target_id"],
                tag=quoted_result.get("parsed_tag", ""),
                action="quoted_context_note",
                customer_name=quoted_result.get("customer_name", ""),
                phone=quoted_result.get("phone", ""),
                source_event=event,
            )
        return quoted_result

    parsed = parse_line_formatted_message(raw_text)
    if not parsed:
        free = _maybe_freeform_development(raw_text)
        if free:
            if free.get("batch"):
                result = create_development_batch(raw_text, event)
            else:
                result = create_development(free["fields"], event)
            save_line_log(
                {"tag": "自由格式開發", "action": "create_development_batch" if free.get("batch") else "create_development",
                 "fields": free.get("fields", {}), "raw_text": raw_text},
                event,
                "success" if result.get("ok") else "failed",
                target_type=result.get("target_type", ""),
                target_id=result.get("target_id", ""),
                note=result.get("reply_text", ""),
                sender_display_name=sender_display_name,
            )
            if result.get("ok") and result.get("target_type") and result.get("target_id"):
                incoming_message_id = message.get("id", "")
                save_line_message_link(
                    incoming_message_id,
                    result["target_type"],
                    result["target_id"],
                    tag=result.get("parsed_tag", "自由格式開發"),
                    action="create_development",
                    customer_name=result.get("customer_name", ""),
                    phone=result.get("phone", ""),
                    source_event=event,
                )
            return result
        return {"handled": False}

    fields = parsed["fields"]
    action = parsed["action"]

    if action == "create_buyer_need":
        result = create_buyer_need(fields, event)
    elif action == "create_seller_listing":
        result = create_seller_listing(fields, event)
    elif action == "create_development":
        result = create_development(fields, event)
    elif action == "create_development_batch":
        result = create_development_batch(parsed.get("raw_body") or raw_text, event)
    elif action == "development_followup":
        result = add_development_followup_via_line(fields, event)
    elif action == "query_records":
        target_type, doc = resolve_customer_record(fields)
        if not doc and (fields.get("record_id") or fields.get("phone") or fields.get("name") or fields.get("address")):
            doc = find_development_record(fields.get("record_id",""), fields.get("phone",""), fields.get("name",""), fields.get("address",""))
            if doc:
                target_type = "development"
        if not doc:
            result = {"handled": True, "ok": False, "reply_text": "查無唯一客戶，請補電話、地址或客戶ID"}
        else:
            result = {
                "handled": True,
                "ok": True,
                "reply_text": format_record_timeline(target_type, doc, limit=fields.get("limit", 10)),
                "target_type": target_type,
                "target_id": doc.id,
                "customer_name": (doc.to_dict() or {}).get("name", ""),
                "phone": (doc.to_dict() or {}).get("phone", ""),
                "parsed_tag": parsed.get("tag", ""),
            }
    elif action == "query_contract_end":
        ok, text, ctx = query_contract_end_text(fields)
        result = {"handled": True, "ok": ok, "reply_text": text}
        if ctx:
            result.update(ctx)
    else:
        target_type = fields.get("target_type", "")
        if action == "buyer_followup":
            target_type = "buyer"
        elif action == "seller_followup":
            target_type = "seller"

        if target_type not in ("buyer", "seller"):
            result = {"handled": True, "ok": False, "reply_text": "請提供對象：買方 或 賣方"}
        else:
            doc = find_customer_record(
                target_type=target_type,
                record_id=fields.get("record_id", ""),
                phone=fields.get("phone", ""),
                name=fields.get("name", ""),
            )
            if not doc:
                result = {"handled": True, "ok": False, "reply_text": "找不到唯一客戶，請補客戶ID或正確電話"}
            else:
                doc_ref = db.collection("buyers" if target_type == "buyer" else "sellers").document(doc.id)
                labels = dedupe_keep_order(["LINE紀錄"] + ensure_list(fields.get("labels")))
                summary_parts = []
                if fields.get("content"):
                    summary_parts.append(fields["content"])
                if fields.get("address"):
                    summary_parts.append(f"地址/物件：{fields['address']}")
                if fields.get("price"):
                    summary_parts.append(f"價格：{fields['price']}")
                summary_text = build_line_summary("；".join(summary_parts).strip() or "LINE 更新", event)

                update_customer_note_and_labels(
                    target_type=target_type,
                    doc_ref=doc_ref,
                    content=summary_text,
                    labels=labels,
                    stage=fields.get("stage", ""),
                    source=fields.get("source", "LINE"),
                    event=event,
                )

                if action in ("buyer_followup", "seller_followup", "classify"):
                    add_customer_followup(
                        target_type=target_type,
                        customer_id=doc.id,
                        content=summary_text,
                        next_action=fields.get("next_action", ""),
                        next_contact_date=fields.get("next_contact_date", ""),
                        labels=labels,
                        line_event=event,
                    )

                label_text = "客需" if target_type == "buyer" else "委託"
                current_data = doc_ref.get().to_dict() or {}
                result = {
                    "handled": True,
                    "ok": True,
                    "reply_text": f"已註記{label_text}：{current_data.get('name', '')}（{current_data.get('phone','-')}）",
                    "target_type": target_type,
                    "target_id": doc.id,
                    "customer_name": current_data.get("name", ""),
                    "phone": current_data.get("phone", ""),
                    "parsed_tag": parsed.get("tag", ""),
                }

    save_line_log(
        parsed,
        event,
        "success" if result.get("ok") else "failed",
        target_type=result.get("target_type", ""),
        target_id=result.get("target_id", ""),
        note=result.get("reply_text", ""),
        sender_display_name=sender_display_name,
    )

    if result.get("ok") and result.get("target_type") and result.get("target_id"):
        incoming_message_id = message.get("id", "")
        save_line_message_link(
            incoming_message_id,
            result["target_type"],
            result["target_id"],
            tag=result.get("parsed_tag", ""),
            action=action,
            customer_name=result.get("customer_name", ""),
            phone=result.get("phone", ""),
            source_event=event,
        )

    return result



# ========= quoted reply followup fix for buyer / seller / development =========

def _collection_key_by_target(target_type: str):
    if target_type == "buyer":
        return "buyers", "buyer_followups", "buyer_id", "客需"
    if target_type == "seller":
        return "sellers", "seller_followups", "seller_id", "委託"
    return "developments", "development_followups", "development_id", "開發"

def _find_docs_by_phone_in_collection(target_type: str, phone: str):
    normalized = normalize_phone(phone)
    if not normalized:
        return []
    collection_name, _, _, _ = _collection_key_by_target(target_type)
    hits = []
    for doc in db.collection(collection_name).stream():
        data = doc.to_dict() or {}
        if normalize_phone(data.get("phone", "")) == normalized:
            hits.append(doc)
    return hits

def _resolve_target_doc_from_link(link):
    target_type = (link.get("target_type") or "").strip()
    if target_type not in ("buyer", "seller", "development"):
        return target_type, None, []
    collection_name, _, _, _ = _collection_key_by_target(target_type)
    target_id = (link.get("target_id") or "").strip()
    if target_id:
        doc = db.collection(collection_name).document(target_id).get()
        if doc.exists:
            return target_type, doc, []
    phone = (link.get("phone") or "").strip()
    if phone:
        docs = _find_docs_by_phone_in_collection(target_type, phone)
        if len(docs) == 1:
            return target_type, docs[0], []
        if len(docs) > 1:
            return target_type, None, docs
    name = (link.get("customer_name") or "").strip()
    if name:
        docs = list(db.collection(collection_name).where("name", "==", name).limit(5).stream())
        if len(docs) == 1:
            return target_type, docs[0], []
        if len(docs) > 1:
            return target_type, None, docs
    return target_type, None, []

def _describe_doc_brief(target_type: str, doc):
    data = doc.to_dict() or {}
    return f"{data.get('name','未填姓名')} / {data.get('phone','-')} / {data.get('address','-')}"

def _parse_quoted_reply_updates(target_type: str, raw_text: str):
    text = (raw_text or "").strip()
    updates = {}
    note_lines = []
    touched_field = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^([^:：]+)\s*[:：]\s*(.*)$", line)
        if not m:
            note_lines.append(line)
            continue
        key = normalize_line_key(m.group(1))
        value = (m.group(2) or "").strip()
        touched_field = True
        if key in ("stage", "current_stage"):
            updates["stage"] = normalize_development_status(value) if target_type == "development" else value
        elif key == "next_action":
            updates["next_action"] = normalize_development_next_action(value) if target_type == "development" else value
        elif key in ("next_contact_date", "next_action_date"):
            updates["next_contact_date"] = value
        elif key == "registered_address" and target_type == "development":
            updates["registered_address"] = value
        elif key in ("address", "phone", "name", "url", "source"):
            updates[key] = value
        elif key == "content":
            if value:
                note_lines.append(value)
        else:
            note_lines.append(line)
    joined_note = "\n".join([x for x in note_lines if x]).strip()
    if target_type == "development":
        plain_status_map = {
            "已連繫": "已聯繫",
            "已聯繫": "已聯繫",
            "已連絡": "已聯繫",
            "已聯絡": "已聯繫",
            "待聯繫": "待聯繫",
            "待聯絡": "待聯繫",
            "已寄開發信": "已寄開發信待跑開發",
            "持續追蹤": "持續追蹤",
            "未接": "待聯繫",
            "未接，再聯繫": "待聯繫",
        }
        if not updates.get("stage"):
            s = plain_status_map.get(text, "")
            if s:
                updates["stage"] = s
        if not updates.get("registered_address") and _looks_like_address(text):
            updates["registered_address"] = text
    followup_text = joined_note or text
    return updates, followup_text

def process_quote_context_message(event):
    message = event.get("message") or {}
    quoted_message_id = message.get("quotedMessageId", "")
    raw_text = (message.get("text") or "").strip()
    if not quoted_message_id or not raw_text:
        return {"handled": False}
    link = get_line_message_link(quoted_message_id)
    if not link:
        return {"handled": False}
    target_type, doc, multi = _resolve_target_doc_from_link(link)
    if target_type not in ("buyer", "seller", "development"):
        return {"handled": False}
    if multi:
        preview = "\n".join([f"- {_describe_doc_brief(target_type, d)}" for d in multi[:5]])
        return {"handled": True, "ok": False, "reply_text": f"找到多筆同電話/同姓名資料，請確認要註記哪一筆：\n{preview}"}
    if not doc:
        label = "客需" if target_type == "buyer" else "委託" if target_type == "seller" else "開發"
        return {"handled": True, "ok": False, "reply_text": f"找不到對應的{label}資料"}
    collection_name, _, _, label_text = _collection_key_by_target(target_type)
    updates, followup_text = _parse_quoted_reply_updates(target_type, raw_text)
    doc_ref = db.collection(collection_name).document(doc.id)
    labels = dedupe_keep_order(["LINE紀錄", "群組回覆註記"])
    extra_updates = {}
    for k in ("address", "url", "phone", "name"):
        if updates.get(k):
            extra_updates[k] = updates[k]
    if updates.get("next_action") and target_type == "development":
        extra_updates["next_action"] = updates["next_action"]
    if updates.get("next_contact_date") and target_type == "development":
        extra_updates["next_action_date"] = updates["next_contact_date"]
    update_kwargs = {
        "target_type": target_type,
        "doc_ref": doc_ref,
        "content": build_line_summary(followup_text, event),
        "labels": labels,
        "stage": updates.get("stage", ""),
        "source": link.get("source", "LINE"),
        "event": event,
    }
    if target_type == "development":
        update_kwargs["registered_address"] = updates.get("registered_address", "")
        if extra_updates:
            update_kwargs["extra_updates"] = extra_updates
    elif extra_updates:
        update_kwargs["extra_updates"] = extra_updates
    update_customer_note_and_labels(**update_kwargs)
    add_customer_followup(
        target_type=target_type,
        customer_id=doc.id,
        content=build_line_summary(followup_text, event),
        next_action=updates.get("next_action", ""),
        next_contact_date=updates.get("next_contact_date", ""),
        labels=labels,
        line_event=event,
        stage=updates.get("stage", "") if target_type == "development" else "",
        registered_address=updates.get("registered_address", "") if target_type == "development" else "",
    )
    current = doc_ref.get().to_dict() or {}
    reply_name = current.get("name", "") or "未填姓名"
    reply_phone = current.get("phone", "") or "-"
    return {
        "handled": True,
        "ok": True,
        "reply_text": f"已註記{label_text}：{reply_name}（{reply_phone}）",
        "target_type": target_type,
        "target_id": doc.id,
        "customer_name": reply_name,
        "phone": reply_phone,
        "parsed_tag": "群組回覆註記",
    }

def process_line_message_event(event):
    message = event.get("message") or {}
    if message.get("type") != "text":
        return {"handled": False}
    sender_display_name = get_line_sender_display_name(event)
    raw_text = message.get("text", "")
    quoted_result = process_quote_context_message(event)
    if quoted_result.get("handled"):
        parsed = {"tag": "群組回覆註記", "action": "quoted_context_note", "fields": {"quoted_message_id": (message or {}).get("quotedMessageId", "")}, "raw_text": raw_text}
        save_line_log(parsed, event, "success" if quoted_result.get("ok") else "failed", target_type=quoted_result.get("target_type", ""), target_id=quoted_result.get("target_id", ""), note=quoted_result.get("reply_text", ""), sender_display_name=sender_display_name)
        if quoted_result.get("ok") and quoted_result.get("target_type") and quoted_result.get("target_id"):
            incoming_message_id = message.get("id", "")
            save_line_message_link(incoming_message_id, quoted_result["target_type"], quoted_result["target_id"], tag=quoted_result.get("parsed_tag", ""), action="quoted_context_note", customer_name=quoted_result.get("customer_name", ""), phone=quoted_result.get("phone", ""), source_event=event)
        return quoted_result
    parsed = parse_line_formatted_message(raw_text)
    if not parsed:
        free = _maybe_freeform_development(raw_text) if '_maybe_freeform_development' in globals() else None
        if free:
            if free.get("batch"):
                result = create_development_batch(raw_text, event)
            else:
                result = create_development(free["fields"], event)
            save_line_log({"tag": "自由格式開發", "action": "create_development_batch" if free.get("batch") else "create_development", "fields": free.get("fields", {}), "raw_text": raw_text}, event, "success" if result.get("ok") else "failed", target_type=result.get("target_type", ""), target_id=result.get("target_id", ""), note=result.get("reply_text", ""), sender_display_name=sender_display_name)
            if result.get("ok") and result.get("target_type") and result.get("target_id"):
                incoming_message_id = message.get("id", "")
                save_line_message_link(incoming_message_id, result["target_type"], result["target_id"], tag=result.get("parsed_tag", "自由格式開發"), action="create_development", customer_name=result.get("customer_name", ""), phone=result.get("phone", ""), source_event=event)
            return result
        return {"handled": False}
    fields = parsed["fields"]
    action = parsed["action"]
    if action == "create_buyer_need":
        result = create_buyer_need(fields, event)
    elif action == "create_seller_listing":
        result = create_seller_listing(fields, event)
    elif action == "create_development":
        result = create_development(fields, event)
    elif action == "create_development_batch":
        result = create_development_batch(parsed.get("raw_body") or raw_text, event)
    elif action == "development_followup":
        result = add_development_followup_via_line(fields, event)
    elif action == "query_records":
        target_type, doc = resolve_customer_record(fields)
        if not doc and (fields.get("record_id") or fields.get("phone") or fields.get("name") or fields.get("address")):
            doc = find_customer_record("development", fields.get("record_id",""), fields.get("phone",""), fields.get("name",""), fields.get("address",""))
            if doc:
                target_type = "development"
        if not doc:
            result = {"handled": True, "ok": False, "reply_text": "查無唯一客戶，請補電話、地址或客戶ID"}
        else:
            result = {"handled": True, "ok": True, "reply_text": format_record_timeline(target_type, doc, limit=fields.get("limit", 10)), "target_type": target_type, "target_id": doc.id, "customer_name": (doc.to_dict() or {}).get("name", ""), "phone": (doc.to_dict() or {}).get("phone", ""), "parsed_tag": parsed.get("tag", "")}
    elif action == "query_contract_end":
        ok, text, ctx = query_contract_end_text(fields)
        result = {"handled": True, "ok": ok, "reply_text": text}
        if ctx:
            result.update(ctx)
    else:
        target_type = fields.get("target_type", "")
        if action == "buyer_followup":
            target_type = "buyer"
        elif action == "seller_followup":
            target_type = "seller"
        if target_type not in ("buyer", "seller"):
            result = {"handled": True, "ok": False, "reply_text": "請提供對象：買方 或 賣方"}
        else:
            doc = find_customer_record(target_type=target_type, record_id=fields.get("record_id", ""), phone=fields.get("phone", ""), name=fields.get("name", ""))
            if not doc:
                result = {"handled": True, "ok": False, "reply_text": "找不到唯一客戶，請補客戶ID或正確電話"}
            else:
                doc_ref = db.collection("buyers" if target_type == "buyer" else "sellers").document(doc.id)
                labels = dedupe_keep_order(["LINE紀錄"] + ensure_list(fields.get("labels")))
                summary_parts = []
                if fields.get("content"):
                    summary_parts.append(fields["content"])
                if fields.get("address"):
                    summary_parts.append(f"地址/物件：{fields['address']}")
                if fields.get("price"):
                    summary_parts.append(f"價格：{fields['price']}")
                summary_text = build_line_summary("；".join(summary_parts).strip() or "LINE 更新", event)
                update_customer_note_and_labels(target_type=target_type, doc_ref=doc_ref, content=summary_text, labels=labels, stage=fields.get("stage", ""), source=fields.get("source", "LINE"), event=event)
                if action in ("buyer_followup", "seller_followup", "classify"):
                    add_customer_followup(target_type=target_type, customer_id=doc.id, content=summary_text, next_action=fields.get("next_action", ""), next_contact_date=fields.get("next_contact_date", ""), labels=labels, line_event=event)
                label_text = "客需" if target_type == "buyer" else "委託"
                current_data = doc_ref.get().to_dict() or {}
                result = {"handled": True, "ok": True, "reply_text": f"已註記{label_text}：{current_data.get('name', '')}（{current_data.get('phone','-')}）", "target_type": target_type, "target_id": doc.id, "customer_name": current_data.get("name", ""), "phone": current_data.get("phone", ""), "parsed_tag": parsed.get("tag", "")}
    save_line_log(parsed, event, "success" if result.get("ok") else "failed", target_type=result.get("target_type", ""), target_id=result.get("target_id", ""), note=result.get("reply_text", ""), sender_display_name=sender_display_name)
    if result.get("ok") and result.get("target_type") and result.get("target_id"):
        incoming_message_id = message.get("id", "")
        save_line_message_link(incoming_message_id, result["target_type"], result["target_id"], tag=result.get("parsed_tag", ""), action=action, customer_name=result.get("customer_name", ""), phone=result.get("phone", ""), source_event=event)
    return result


# ========= final reply / strict development patch =========

def _build_development_input_help(batch: bool = False) -> str:
    if batch:
        return (
            "新增開發批次格式錯誤，請用下面格式：\n"
            "#新增開發批次\n"
            "姓名: 王先生\n"
            "電話: 0911000111\n"
            "地址: 台中市沙鹿區A路1號\n"
            "戶籍地址: 台中市沙鹿區OO路OO號\n"
            "網址: https://...\n"
            "來源: 掃街\n"
            "目前狀況: 待聯繫\n"
            "下一步: 電話聯繫\n"
            "下次時間: 2026-04-10\n"
            "內容: 門口自售\n"
            "///\n"
            "姓名: 陳小姐\n"
            "電話: 0922000222\n"
            "地址: 台中市梧棲區B路2號"
        )
    return (
        "新增開發格式錯誤，請用下面格式：\n"
        "#新增開發\n"
        "姓名: 王先生\n"
        "電話: 0911000111\n"
        "地址: 台中市沙鹿區OO路OO號\n"
        "戶籍地址: 台中市沙鹿區OO路OO號\n"
        "網址: https://...\n"
        "來源: 掃街\n"
        "目前狀況: 待聯繫\n"
        "下一步: 電話聯繫\n"
        "下次時間: 2026-04-10\n"
        "內容: 門口自售"
    )


def _make_google_nav_url(address: str) -> str:
    addr = (address or '').strip()
    if not addr:
        return ''
    from urllib.parse import quote
    return f"https://www.google.com/maps/search/?api=1&query={quote(addr, safe='')}"


def _strict_parse_development_fields(raw_body: str):
    lines = [ln.strip() for ln in (raw_body or '').splitlines() if ln.strip()]
    if not lines:
        return None
    fields = {}
    invalid_lines = []
    for line in lines:
        m = re.match(r'^([^:：]+)\s*[:：]\s*(.+)$', line)
        if not m:
            invalid_lines.append(line)
            continue
        key = normalize_line_key(m.group(1))
        value = (m.group(2) or '').strip()
        if key == 'labels':
            fields[key] = parse_label_csv(value)
        else:
            fields[key] = value
    if invalid_lines:
        return None
    if not any(fields.get(k) for k in ('address', 'phone', 'name')):
        return None
    fields['target_type'] = 'development'
    if fields.get('current_stage') and not fields.get('stage'):
        fields['stage'] = fields['current_stage']
    if fields.get('next_action_date') and not fields.get('next_contact_date'):
        fields['next_contact_date'] = fields['next_action_date']
    return fields


def _strict_parse_development_batch(raw_body: str):
    body = (raw_body or '').strip()
    if not body:
        return []
    chunks = [c.strip() for c in re.split(r'(?m)^\s*(?:///+|---+)\s*$', body) if c.strip()]
    items = []
    for chunk in chunks:
        fields = _strict_parse_development_fields(chunk)
        if not fields:
            return []
        items.append(fields)
    return items


def _maybe_freeform_development(text: str):
    return None


def parse_line_formatted_message(text: str):
    lines = [ln.strip() for ln in (text or '').splitlines() if ln.strip()]
    if not lines:
        return None
    first = lines[0]
    if not first.startswith('#'):
        return None

    tag = first.lstrip('#').strip()
    tag_map = {
        '新增客需': 'create_buyer_need',
        '新增委託': 'create_seller_listing',
        '新增開發': 'create_development',
        '新增開發批次': 'create_development_batch',
        '開發追蹤': 'development_followup',
        '買方追蹤': 'buyer_followup',
        '賣方追蹤': 'seller_followup',
        '客戶分類': 'classify',
        '查詢紀錄': 'query_records',
        '查詢委託到期': 'query_contract_end',
        '帶看': 'buyer_followup',
        '成交': 'buyer_followup',
        '委託': 'seller_followup',
        '紀錄': 'generic_note',
    }
    action = tag_map.get(tag)
    if not action:
        return None

    raw_body = '\n'.join(lines[1:]).strip()

    if action == 'create_development':
        fields = _strict_parse_development_fields(raw_body)
        if not fields:
            return None
        return {'tag': tag, 'action': action, 'fields': fields, 'raw_text': text, 'raw_body': raw_body}

    if action == 'create_development_batch':
        items = _strict_parse_development_batch(raw_body)
        if not items:
            return None
        return {'tag': tag, 'action': action, 'fields': {}, 'raw_text': text, 'raw_body': raw_body}

    fields = {}
    for line in lines[1:]:
        m = re.match(r'^([^:：]+)\s*[:：]\s*(.+)$', line)
        if not m:
            continue
        key = normalize_line_key(m.group(1))
        value = m.group(2).strip()
        if key == 'labels':
            fields[key] = parse_label_csv(value)
        else:
            fields[key] = value

    if tag in ('買方追蹤', '帶看', '成交') and not fields.get('target_type'):
        fields['target_type'] = 'buyer'
    if tag in ('賣方追蹤', '委託') and not fields.get('target_type'):
        fields['target_type'] = 'seller'
    if tag == '開發追蹤' and not fields.get('target_type'):
        fields['target_type'] = 'development'

    fields['target_type'] = normalize_target_type(fields.get('target_type', '')) or fields.get('target_type', '')
    fields['intent_type'] = normalize_intent_type(fields.get('intent_type_raw', ''), fields)
    fields['deal_type'] = normalize_deal_type(fields.get('deal_type_raw', ''))
    fields['limit'] = parse_int_limit(fields.get('limit', 10), default=10, max_value=30)

    if action == 'create_buyer_need':
        if not (fields.get('name') and fields.get('phone')):
            return None
    elif action == 'create_seller_listing':
        if not (fields.get('name') and fields.get('phone')):
            return None
    elif action in ('buyer_followup', 'seller_followup', 'classify', 'query_records', 'query_contract_end', 'development_followup'):
        if not (fields.get('record_id') or fields.get('phone') or fields.get('name') or fields.get('address')):
            return None

    return {'tag': tag, 'action': action, 'fields': fields, 'raw_text': text, 'raw_body': raw_body}


def create_development(fields, event):
    phone = (fields.get('phone') or '').strip()
    name = (fields.get('name') or '').strip() or '未填姓名'
    url = (fields.get('url') or '').strip()
    source = infer_development_source(fields.get('source', ''), url)
    address = (fields.get('address') or '').strip()
    registered_address = (fields.get('registered_address') or '').strip()
    nav_url = _make_google_nav_url(registered_address or address)

    matches = find_records_by_phone('developments', phone) if phone else []
    if not matches and address:
        doc = find_development_record(address=address)
        if doc:
            matches = [doc]

    labels = build_development_labels(fields.get('labels'))
    content_text = (fields.get('content') or '').strip() or address or url or 'LINE 新增開發'
    note_content = build_line_summary(content_text, event)

    payload = {
        'name': name,
        'phone': phone,
        'source': source,
        'url': url,
        'address': address,
        'registered_address': registered_address,
        'registered_address_google_maps_url': nav_url,
        'current_stage': normalize_development_status((fields.get('current_stage') or '').strip() or (fields.get('stage') or '').strip() or '待聯繫'),
        'stage': normalize_development_status((fields.get('current_stage') or '').strip() or (fields.get('stage') or '').strip() or '待聯繫'),
        'next_action': normalize_development_next_action((fields.get('next_action') or '').strip()),
        'next_action_date': (fields.get('next_action_date') or '').strip() or (fields.get('next_contact_date') or '').strip(),
        'record_date': (fields.get('record_date') or '').strip() or now_taipei().strftime('%Y-%m-%d'),
        'labels': labels,
        'updated_at': now_taipei().isoformat(),
        'updated_by_id': 'line_bot',
        'updated_by_name': 'LINE Bot',
        'sender_display_name': get_line_sender_display_name(event) or '',
    }

    if len(matches) == 1:
        doc = matches[0]
        doc_ref = db.collection('developments').document(doc.id)
        update_customer_note_and_labels(
            target_type='development',
            doc_ref=doc_ref,
            content=note_content,
            labels=labels,
            stage=payload['stage'],
            source=payload['source'],
            event=event,
            registered_address=registered_address,
        )
        clean_updates = {k: v for k, v in payload.items() if v not in ('', None)}
        doc_ref.update(clean_updates)
        add_customer_followup(
            target_type='development',
            customer_id=doc.id,
            content=note_content + (f"\nGoogle導航: {nav_url}" if nav_url else ''),
            next_action=payload.get('next_action', ''),
            next_contact_date=payload.get('next_action_date', ''),
            labels=labels,
            line_event=event,
        )
        updated_doc = doc_ref.get().to_dict() or {}
        reply_text = f"已註記開發：{updated_doc.get('name', '')}（{updated_doc.get('phone', '-') or '-'}）"
        if nav_url:
            reply_text += f"\nGoogle導航: {nav_url}"
        return {
            'handled': True,
            'ok': True,
            'reply_text': reply_text[:5000],
            'target_type': 'development',
            'target_id': doc.id,
            'customer_name': updated_doc.get('name', ''),
            'phone': updated_doc.get('phone', ''),
            'parsed_tag': '新增開發',
        }

    if len(matches) > 1:
        return {'handled': True, 'ok': False, 'reply_text': '未寫入：同電話有多筆開發資料，請補地址或客戶ID'}

    now = now_taipei().isoformat()
    payload.update({
        'created_at': now,
        'created_by_id': 'line_bot',
        'created_by_name': 'LINE Bot',
        'note': append_note_block('', note_content, build_line_operator_label(event)),
    })
    doc_ref = db.collection('developments').document()
    doc_ref.set(payload)
    add_customer_followup(
        target_type='development',
        customer_id=doc_ref.id,
        content=note_content + (f"\nGoogle導航: {nav_url}" if nav_url else ''),
        next_action=payload.get('next_action', ''),
        next_contact_date=payload.get('next_action_date', ''),
        labels=labels,
        line_event=event,
    )
    reply_text = f"已註記開發：{name}（{phone or '-'}）"
    if nav_url:
        reply_text += f"\nGoogle導航: {nav_url}"
    return {
        'handled': True,
        'ok': True,
        'reply_text': reply_text[:5000],
        'target_type': 'development',
        'target_id': doc_ref.id,
        'customer_name': name,
        'phone': phone,
        'parsed_tag': '新增開發',
    }


def create_development_batch(raw_text, event):
    body = (raw_text or '').strip()
    if body.startswith('#新增開發批次'):
        body = '\n'.join(body.splitlines()[1:]).strip()
    items = _strict_parse_development_batch(body)
    if not items:
        return {'handled': True, 'ok': False, 'reply_text': _build_development_input_help(batch=True)}
    ok_count = 0
    fail_count = 0
    last_result = None
    for fields in items:
        result = create_development(fields, event)
        last_result = result
        if result.get('ok'):
            ok_count += 1
        else:
            fail_count += 1
    reply_text = f"批量註記完成：成功 {ok_count} 筆，失敗 {fail_count} 筆"
    if last_result and last_result.get('target_type') and last_result.get('target_id'):
        return {
            'handled': True,
            'ok': ok_count > 0,
            'reply_text': reply_text,
            'target_type': last_result.get('target_type', ''),
            'target_id': last_result.get('target_id', ''),
            'customer_name': last_result.get('customer_name', ''),
            'phone': last_result.get('phone', ''),
            'parsed_tag': '新增開發批次',
        }
    return {'handled': True, 'ok': ok_count > 0, 'reply_text': reply_text}


def process_quote_context_message(event):
    message = event.get('message') or {}
    quoted_message_id = (message.get('quotedMessageId') or '').strip()
    raw_text = (message.get('text') or '').strip()
    if not quoted_message_id or not raw_text:
        return {'handled': False}

    link = get_line_message_link(quoted_message_id)
    if not link:
        return {
            'handled': True,
            'ok': False,
            'reply_text': '這則回覆找不到對標資料，請直接回覆 bot 建立成功的那則訊息或已對標的追蹤訊息。'
        }

    target_type, doc, multi = _resolve_target_doc_from_link(link)
    if target_type not in ('buyer', 'seller', 'development'):
        return {
            'handled': True,
            'ok': False,
            'reply_text': '這則回覆目前無法判定對應客戶，請直接回覆 bot 建立成功的那則訊息。'
        }
    if multi:
        preview = '\n'.join([f"- {_describe_doc_brief(target_type, d)}" for d in multi[:5]])
        return {'handled': True, 'ok': False, 'reply_text': f"找到多筆同電話/同姓名資料，請確認要註記哪一筆：\n{preview}"}
    if not doc:
        label = '客需' if target_type == 'buyer' else '委託' if target_type == 'seller' else '開發'
        return {'handled': True, 'ok': False, 'reply_text': f'找不到對應的{label}資料'}

    collection_name, _, _, label_text = _collection_key_by_target(target_type)
    updates, followup_text = _parse_quoted_reply_updates(target_type, raw_text)
    doc_ref = db.collection(collection_name).document(doc.id)
    labels = dedupe_keep_order(['LINE紀錄', '群組回覆註記'])

    stage_for_note = updates.get('stage', '')
    source_for_note = updates.get('source', link.get('source', 'LINE'))
    registered_address = updates.get('registered_address', '') if target_type == 'development' else ''
    followup_content = build_line_summary(followup_text, event)

    update_customer_note_and_labels(
        target_type=target_type,
        doc_ref=doc_ref,
        content=followup_content,
        labels=labels,
        stage=stage_for_note,
        source=source_for_note,
        event=event,
        registered_address=registered_address,
    )

    manual_updates = {
        'updated_at': now_taipei().isoformat(),
        'updated_by_id': 'line_bot',
        'updated_by_name': 'LINE Bot',
    }
    for k in ('address', 'url', 'phone', 'name', 'source'):
        if updates.get(k):
            manual_updates[k] = updates[k]

    nav_url = ''
    if target_type == 'development':
        if updates.get('stage'):
            manual_updates['current_stage'] = updates['stage']
            manual_updates['stage'] = updates['stage']
        if updates.get('next_action'):
            manual_updates['next_action'] = updates['next_action']
        if updates.get('next_contact_date'):
            manual_updates['next_action_date'] = updates['next_contact_date']
        if updates.get('registered_address'):
            manual_updates['registered_address'] = updates['registered_address']
            nav_url = _make_google_nav_url(updates['registered_address'])
            if nav_url:
                manual_updates['registered_address_google_maps_url'] = nav_url
        elif doc.to_dict().get('registered_address'):
            nav_url = _make_google_nav_url(doc.to_dict().get('registered_address'))
    else:
        if updates.get('stage'):
            manual_updates['stage'] = updates['stage']

    clean_updates = {k: v for k, v in manual_updates.items() if v not in ('', None)}
    if clean_updates:
        doc_ref.update(clean_updates)

    followup_with_nav = followup_content + (f"\nGoogle導航: {nav_url}" if nav_url else '')
    add_customer_followup(
        target_type=target_type,
        customer_id=doc.id,
        content=followup_with_nav,
        next_action=updates.get('next_action', ''),
        next_contact_date=updates.get('next_contact_date', ''),
        labels=labels,
        line_event=event,
    )

    current = doc_ref.get().to_dict() or {}
    reply_name = current.get('name', '') or '未填姓名'
    reply_phone = current.get('phone', '') or '-'
    reply_text = f"已註記{label_text}：{reply_name}（{reply_phone}）"
    if nav_url and target_type == 'development':
        reply_text += f"\nGoogle導航: {nav_url}"
    return {
        'handled': True,
        'ok': True,
        'reply_text': reply_text[:5000],
        'target_type': target_type,
        'target_id': doc.id,
        'customer_name': reply_name,
        'phone': reply_phone,
        'parsed_tag': '群組回覆註記',
    }


def process_line_message_event(event):
    message = event.get('message') or {}
    if message.get('type') != 'text':
        return {'handled': False}

    sender_display_name = get_line_sender_display_name(event)
    raw_text = (message.get('text') or '').strip()
    quoted_message_id = (message.get('quotedMessageId') or '').strip()

    if quoted_message_id:
        quoted_result = process_quote_context_message(event)
        parsed = {'tag': '群組回覆註記', 'action': 'quoted_context_note', 'fields': {'quoted_message_id': quoted_message_id}, 'raw_text': raw_text}
        save_line_log(parsed, event, 'success' if quoted_result.get('ok') else 'failed', target_type=quoted_result.get('target_type', ''), target_id=quoted_result.get('target_id', ''), note=quoted_result.get('reply_text', ''), sender_display_name=sender_display_name)
        if quoted_result.get('ok') and quoted_result.get('target_type') and quoted_result.get('target_id'):
            incoming_message_id = message.get('id', '')
            save_line_message_link(incoming_message_id, quoted_result['target_type'], quoted_result['target_id'], tag=quoted_result.get('parsed_tag', ''), action='quoted_context_note', customer_name=quoted_result.get('customer_name', ''), phone=quoted_result.get('phone', ''), source_event=event)
        return quoted_result

    if raw_text.startswith('#新增開發批次'):
        parsed = parse_line_formatted_message(raw_text)
        if not parsed:
            result = {'handled': True, 'ok': False, 'reply_text': _build_development_input_help(batch=True)}
            save_line_log({'tag': '新增開發批次', 'action': 'create_development_batch', 'fields': {}, 'raw_text': raw_text}, event, 'failed', note=result['reply_text'], sender_display_name=sender_display_name)
            return result
    elif raw_text.startswith('#新增開發'):
        parsed = parse_line_formatted_message(raw_text)
        if not parsed:
            result = {'handled': True, 'ok': False, 'reply_text': _build_development_input_help(batch=False)}
            save_line_log({'tag': '新增開發', 'action': 'create_development', 'fields': {}, 'raw_text': raw_text}, event, 'failed', note=result['reply_text'], sender_display_name=sender_display_name)
            return result
    else:
        parsed = parse_line_formatted_message(raw_text)

    if not parsed:
        return {'handled': False}

    fields = parsed['fields']
    action = parsed['action']

    if action == 'create_buyer_need':
        result = create_buyer_need(fields, event)
    elif action == 'create_seller_listing':
        result = create_seller_listing(fields, event)
    elif action == 'create_development':
        result = create_development(fields, event)
    elif action == 'create_development_batch':
        result = create_development_batch(parsed.get('raw_body') or raw_text, event)
    elif action == 'development_followup':
        result = add_development_followup_via_line(fields, event)
    elif action == 'query_records':
        target_type, doc = resolve_customer_record(fields)
        if not doc and (fields.get('record_id') or fields.get('phone') or fields.get('name') or fields.get('address')):
            doc = find_development_record(fields.get('record_id',''), fields.get('phone',''), fields.get('name',''), fields.get('address',''))
            if doc:
                target_type = 'development'
        if not doc:
            result = {'handled': True, 'ok': False, 'reply_text': '查無唯一客戶，請補電話、地址或客戶ID'}
        else:
            result = {'handled': True, 'ok': True, 'reply_text': format_record_timeline(target_type, doc, limit=fields.get('limit', 10)), 'target_type': target_type, 'target_id': doc.id, 'customer_name': (doc.to_dict() or {}).get('name', ''), 'phone': (doc.to_dict() or {}).get('phone', ''), 'parsed_tag': parsed.get('tag', '')}
    elif action == 'query_contract_end':
        ok, text, ctx = query_contract_end_text(fields)
        result = {'handled': True, 'ok': ok, 'reply_text': text}
        if ctx:
            result.update(ctx)
    else:
        target_type = fields.get('target_type', '')
        if action == 'buyer_followup':
            target_type = 'buyer'
        elif action == 'seller_followup':
            target_type = 'seller'
        if target_type not in ('buyer', 'seller'):
            result = {'handled': True, 'ok': False, 'reply_text': '請提供對象：買方 或 賣方'}
        else:
            doc = find_customer_record(target_type=target_type, record_id=fields.get('record_id', ''), phone=fields.get('phone', ''), name=fields.get('name', ''), address=fields.get('address', ''))
            if not doc:
                result = {'handled': True, 'ok': False, 'reply_text': '找不到唯一客戶，請補客戶ID或正確電話'}
            else:
                doc_ref = db.collection('buyers' if target_type == 'buyer' else 'sellers').document(doc.id)
                labels = dedupe_keep_order(['LINE紀錄'] + ensure_list(fields.get('labels')))
                summary_parts = []
                if fields.get('content'):
                    summary_parts.append(fields['content'])
                if fields.get('address'):
                    summary_parts.append(f"地址/物件：{fields['address']}")
                if fields.get('price'):
                    summary_parts.append(f"價格：{fields['price']}")
                summary_text = build_line_summary('；'.join(summary_parts).strip() or 'LINE 更新', event)
                update_customer_note_and_labels(target_type=target_type, doc_ref=doc_ref, content=summary_text, labels=labels, stage=fields.get('stage', ''), source=fields.get('source', 'LINE'), event=event)
                if action in ('buyer_followup', 'seller_followup', 'classify'):
                    add_customer_followup(target_type=target_type, customer_id=doc.id, content=summary_text, next_action=fields.get('next_action', ''), next_contact_date=fields.get('next_contact_date', ''), labels=labels, line_event=event)
                label_text = '客需' if target_type == 'buyer' else '委託'
                current_data = doc_ref.get().to_dict() or {}
                result = {'handled': True, 'ok': True, 'reply_text': f"已註記{label_text}：{current_data.get('name', '')}（{current_data.get('phone','-')}）", 'target_type': target_type, 'target_id': doc.id, 'customer_name': current_data.get('name', ''), 'phone': current_data.get('phone', ''), 'parsed_tag': parsed.get('tag', '')}

    save_line_log(parsed, event, 'success' if result.get('ok') else 'failed', target_type=result.get('target_type', ''), target_id=result.get('target_id', ''), note=result.get('reply_text', ''), sender_display_name=sender_display_name)
    if result.get('ok') and result.get('target_type') and result.get('target_id'):
        incoming_message_id = message.get('id', '')
        save_line_message_link(incoming_message_id, result['target_type'], result['target_id'], tag=result.get('parsed_tag', ''), action=action, customer_name=result.get('customer_name', ''), phone=result.get('phone', ''), source_event=event)
    return result


# ========= final v3 patch: relaxed development header mode + reliable quote mapping =========

def update_customer_note_and_labels(target_type: str, doc_ref, content: str, labels=None, stage="", source="LINE", event=None, registered_address="", extra_updates=None):
    labels = dedupe_keep_order(["LINE紀錄"] + ensure_list(labels))
    snapshot = doc_ref.get()
    current = snapshot.to_dict() or {}
    old_note = current.get("note", "")

    updates = {
        "labels": firestore.ArrayUnion(labels),
        "updated_at": now_taipei().isoformat(),
        "updated_by_id": "line_bot",
        "updated_by_name": "LINE Bot",
    }
    if content:
        source_label = build_line_operator_label(event) if event else "LINE"
        updates["note"] = append_note_block(old_note, content, source_label)
    if stage:
        if target_type == 'development':
            normalized_stage = normalize_development_status(stage)
            updates['stage'] = normalized_stage
            updates['current_stage'] = normalized_stage
        else:
            updates['stage'] = stage
    if source:
        updates['source'] = source
    if registered_address:
        updates['registered_address'] = registered_address
        nav_url = _make_google_nav_url(registered_address)
        if nav_url:
            updates['registered_address_google_maps_url'] = nav_url
    if extra_updates:
        cleaned = {k: v for k, v in extra_updates.items() if v not in (None, "")}
        if cleaned:
            updates.update(cleaned)
    doc_ref.update(updates)


def add_customer_followup(target_type: str, customer_id: str, content: str, next_action="", next_contact_date="", labels=None, line_event=None, stage="", registered_address=""):
    if target_type == 'buyer':
        collection_name = 'buyer_followups'
        key_name = 'buyer_id'
    elif target_type == 'seller':
        collection_name = 'seller_followups'
        key_name = 'seller_id'
    else:
        collection_name = 'development_followups'
        key_name = 'development_id'

    sender_display_name = get_line_sender_display_name(line_event) if line_event else ''
    data = {
        key_name: customer_id,
        'contact_time': now_taipei().strftime('%Y-%m-%d %H:%M'),
        'channel': 'LINE',
        'content': content,
        'next_action': next_action,
        'next_contact_date': next_contact_date,
        'labels': dedupe_keep_order(['LINE紀錄'] + ensure_list(labels)),
        'created_at': now_taipei().isoformat(),
        'created_by_id': 'line_bot',
        'created_by_name': 'LINE Bot',
        'sender_display_name': sender_display_name,
    }
    if stage:
        data['stage'] = stage
        if target_type == 'development':
            data['current_stage'] = stage
    if registered_address:
        data['registered_address'] = registered_address
        nav_url = _make_google_nav_url(registered_address)
        if nav_url:
            data['registered_address_google_maps_url'] = nav_url
    if line_event:
        source = line_event.get('source', {})
        data['line_group_id'] = source.get('groupId', '')
        data['line_room_id'] = source.get('roomId', '')
        data['line_user_id'] = source.get('userId', '')
        data['line_message_id'] = (line_event.get('message') or {}).get('id', '')
        data['quoted_message_id'] = (line_event.get('message') or {}).get('quotedMessageId', '')
    db.collection(collection_name).add(data)


def _build_development_input_help(batch: bool = False) -> str:
    if batch:
        return (
            "新增開發批次格式範例（欄位可留空）：\n"
            "#新增開發批次\n"
            "姓名: 王先生\n"
            "電話: \n"
            "地址: 台中市沙鹿區A路1號\n"
            "戶籍地址: \n"
            "網址: \n"
            "來源: 掃街\n"
            "目前狀況: 待聯繫\n"
            "下一步: \n"
            "下次時間: \n"
            "內容: \n"
            "///\n"
            "姓名: 陳小姐\n"
            "地址: 台中市梧棲區B路2號"
        )
    return (
        "新增開發格式範例（只要有 #新增開發 就可以，欄位可留空）：\n"
        "#新增開發\n"
        "姓名: 王先生\n"
        "電話: \n"
        "地址: 台中市沙鹿區OO路OO號\n"
        "戶籍地址: \n"
        "網址: \n"
        "來源: 掃街\n"
        "目前狀況: 待聯繫\n"
        "下一步: \n"
        "下次時間: \n"
        "內容: "
    )


def _relaxed_parse_kv_block(raw_body: str):
    fields = {}
    note_lines = []
    lines = (raw_body or '').splitlines()
    saw_any = False
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        saw_any = True
        m = re.match(r'^([^:：]+)\s*[:：]\s*(.*)$', line)
        if m:
            key = normalize_line_key(m.group(1))
            value = (m.group(2) or '').strip()
            if key == 'labels':
                fields[key] = parse_label_csv(value)
            else:
                fields[key] = value
        else:
            note_lines.append(line)
    if note_lines:
        existing = (fields.get('content') or '').strip()
        combined = '\n'.join(note_lines).strip()
        fields['content'] = (existing + ('\n' if existing and combined else '') + combined).strip()
    return fields, saw_any


def _strict_parse_development_fields(raw_body: str):
    fields, saw_any = _relaxed_parse_kv_block(raw_body)
    if not saw_any and not (raw_body or '').strip():
        fields = {}
    fields['target_type'] = 'development'
    if fields.get('current_stage') and not fields.get('stage'):
        fields['stage'] = fields['current_stage']
    if fields.get('next_action_date') and not fields.get('next_contact_date'):
        fields['next_contact_date'] = fields['next_action_date']
    return fields


def _strict_parse_development_batch(raw_body: str):
    body = (raw_body or '').strip()
    if not body:
        return []
    chunks = [c.strip() for c in re.split(r'(?m)^\s*(?:///+|---+)\s*$', body) if c.strip()]
    items = []
    for chunk in chunks:
        fields = _strict_parse_development_fields(chunk)
        # skip completely empty chunk
        meaningful = any((str(v).strip() if not isinstance(v, list) else any(str(x).strip() for x in v)) for k, v in fields.items() if k != 'target_type')
        if not meaningful and not chunk:
            continue
        items.append(fields)
    return items


def parse_line_formatted_message(text: str):
    lines = [ln.rstrip() for ln in (text or '').splitlines()]
    nonempty = [ln.strip() for ln in lines if ln.strip()]
    if not nonempty:
        return None
    first = nonempty[0]
    if not first.startswith('#'):
        return None

    tag = first.lstrip('#').strip()
    tag_map = {
        '新增客需': 'create_buyer_need',
        '新增委託': 'create_seller_listing',
        '新增開發': 'create_development',
        '新增開發批次': 'create_development_batch',
        '開發追蹤': 'development_followup',
        '買方追蹤': 'buyer_followup',
        '賣方追蹤': 'seller_followup',
        '客戶分類': 'classify',
        '查詢紀錄': 'query_records',
        '查詢委託到期': 'query_contract_end',
        '帶看': 'buyer_followup',
        '成交': 'buyer_followup',
        '委託': 'seller_followup',
        '紀錄': 'generic_note',
    }
    action = tag_map.get(tag)
    if not action:
        return None

    # raw body should preserve blank values after header
    started = False
    body_lines = []
    for ln in lines:
        stripped = ln.strip()
        if not started:
            if stripped == first:
                started = True
            continue
        body_lines.append(ln)
    raw_body = '\n'.join(body_lines).strip('\n')

    if action == 'create_development':
        fields = _strict_parse_development_fields(raw_body)
        return {'tag': tag, 'action': action, 'fields': fields, 'raw_text': text, 'raw_body': raw_body}

    if action == 'create_development_batch':
        items = _strict_parse_development_batch(raw_body)
        if not items:
            return None
        return {'tag': tag, 'action': action, 'fields': {}, 'raw_text': text, 'raw_body': raw_body}

    fields = {}
    for raw in body_lines:
        line = raw.strip()
        if not line:
            continue
        m = re.match(r'^([^:：]+)\s*[:：]\s*(.*)$', line)
        if not m:
            continue
        key = normalize_line_key(m.group(1))
        value = (m.group(2) or '').strip()
        if key == 'labels':
            fields[key] = parse_label_csv(value)
        else:
            fields[key] = value

    if tag in ('買方追蹤', '帶看', '成交') and not fields.get('target_type'):
        fields['target_type'] = 'buyer'
    if tag in ('賣方追蹤', '委託') and not fields.get('target_type'):
        fields['target_type'] = 'seller'
    if tag == '開發追蹤' and not fields.get('target_type'):
        fields['target_type'] = 'development'

    fields['target_type'] = normalize_target_type(fields.get('target_type', '')) or fields.get('target_type', '')
    fields['intent_type'] = normalize_intent_type(fields.get('intent_type_raw', ''), fields)
    fields['deal_type'] = normalize_deal_type(fields.get('deal_type_raw', ''))
    fields['limit'] = parse_int_limit(fields.get('limit', 10), default=10, max_value=30)

    if action == 'create_buyer_need':
        if not (fields.get('name') and fields.get('phone')):
            return None
    elif action == 'create_seller_listing':
        if not (fields.get('name') and fields.get('phone')):
            return None
    elif action in ('buyer_followup', 'seller_followup', 'classify', 'query_records', 'query_contract_end', 'development_followup'):
        if not (fields.get('record_id') or fields.get('phone') or fields.get('name') or fields.get('address')):
            return None

    return {'tag': tag, 'action': action, 'fields': fields, 'raw_text': text, 'raw_body': raw_body}


def create_development(fields, event):
    phone = (fields.get('phone') or '').strip()
    name = (fields.get('name') or '').strip() or '未填姓名'
    url = (fields.get('url') or '').strip()
    source = infer_development_source(fields.get('source', ''), url)
    address = (fields.get('address') or '').strip()
    registered_address = (fields.get('registered_address') or '').strip()
    content_value = (fields.get('content') or '').strip()
    nav_url = _make_google_nav_url(registered_address or address)

    matches = find_records_by_phone('developments', phone) if phone else []
    if not matches and address:
        doc = find_development_record(address=address)
        if doc:
            matches = [doc]

    labels = build_development_labels(fields.get('labels'))
    content_text = content_value or registered_address or address or url or 'LINE 新增開發'
    note_content = build_line_summary(content_text, event)

    normalized_stage = normalize_development_status((fields.get('current_stage') or '').strip() or (fields.get('stage') or '').strip() or '待聯繫')
    normalized_next = normalize_development_next_action((fields.get('next_action') or '').strip())
    next_date = (fields.get('next_action_date') or '').strip() or (fields.get('next_contact_date') or '').strip()
    record_date = (fields.get('record_date') or '').strip() or now_taipei().strftime('%Y-%m-%d')

    payload = {
        'name': name,
        'phone': phone,
        'source': source,
        'url': url,
        'address': address,
        'registered_address': registered_address,
        'registered_address_google_maps_url': nav_url,
        'current_stage': normalized_stage,
        'stage': normalized_stage,
        'next_action': normalized_next,
        'next_action_date': next_date,
        'record_date': record_date,
        'labels': labels,
        'updated_at': now_taipei().isoformat(),
        'updated_by_id': 'line_bot',
        'updated_by_name': 'LINE Bot',
        'sender_display_name': get_line_sender_display_name(event) or '',
    }

    if len(matches) == 1:
        doc = matches[0]
        doc_ref = db.collection('developments').document(doc.id)
        update_customer_note_and_labels(
            target_type='development',
            doc_ref=doc_ref,
            content=note_content,
            labels=labels,
            stage=normalized_stage,
            source=source,
            event=event,
            registered_address=registered_address,
            extra_updates={k: v for k, v in payload.items() if k not in ('labels', 'updated_at', 'updated_by_id', 'updated_by_name') and v not in ('', None)}
        )
        add_customer_followup(
            target_type='development',
            customer_id=doc.id,
            content=note_content + (f"\nGoogle導航: {nav_url}" if nav_url else ''),
            next_action=normalized_next,
            next_contact_date=next_date,
            labels=labels,
            line_event=event,
            stage=normalized_stage,
            registered_address=registered_address,
        )
        updated_doc = doc_ref.get().to_dict() or {}
        reply_text = f"已註記開發：{updated_doc.get('name', '')}（{updated_doc.get('phone', '-') or '-'}）"
        if nav_url:
            reply_text += f"\nGoogle導航: {nav_url}"
        return {
            'handled': True,
            'ok': True,
            'reply_text': reply_text[:5000],
            'target_type': 'development',
            'target_id': doc.id,
            'customer_name': updated_doc.get('name', ''),
            'phone': updated_doc.get('phone', ''),
            'parsed_tag': '新增開發',
        }

    if len(matches) > 1:
        return {'handled': True, 'ok': False, 'reply_text': '未寫入：同電話有多筆開發資料，請補地址或客戶ID'}

    now = now_taipei().isoformat()
    payload.update({
        'created_at': now,
        'created_by_id': 'line_bot',
        'created_by_name': 'LINE Bot',
        'note': append_note_block('', note_content, build_line_operator_label(event)),
    })
    doc_ref = db.collection('developments').document()
    doc_ref.set(payload)
    add_customer_followup(
        target_type='development',
        customer_id=doc_ref.id,
        content=note_content + (f"\nGoogle導航: {nav_url}" if nav_url else ''),
        next_action=normalized_next,
        next_contact_date=next_date,
        labels=labels,
        line_event=event,
        stage=normalized_stage,
        registered_address=registered_address,
    )
    reply_text = f"已註記開發：{name}（{phone or '-'}）"
    if nav_url:
        reply_text += f"\nGoogle導航: {nav_url}"
    return {
        'handled': True,
        'ok': True,
        'reply_text': reply_text[:5000],
        'target_type': 'development',
        'target_id': doc_ref.id,
        'customer_name': name,
        'phone': phone,
        'parsed_tag': '新增開發',
    }


def create_development_batch(raw_text, event):
    body = (raw_text or '').strip()
    if body.startswith('#新增開發批次'):
        body = '\n'.join(body.splitlines()[1:]).strip()
    items = _strict_parse_development_batch(body)
    if not items:
        return {'handled': True, 'ok': False, 'reply_text': _build_development_input_help(batch=True)}
    ok_count = 0
    fail_count = 0
    last_result = None
    for fields in items:
        result = create_development(fields, event)
        last_result = result
        if result.get('ok'):
            ok_count += 1
        else:
            fail_count += 1
    reply_text = f'批量註記完成：成功 {ok_count} 筆，失敗 {fail_count} 筆'
    if last_result and last_result.get('target_type') and last_result.get('target_id'):
        return {
            'handled': True,
            'ok': ok_count > 0,
            'reply_text': reply_text,
            'target_type': last_result.get('target_type', ''),
            'target_id': last_result.get('target_id', ''),
            'customer_name': last_result.get('customer_name', ''),
            'phone': last_result.get('phone', ''),
            'parsed_tag': '新增開發批次',
        }
    return {'handled': True, 'ok': ok_count > 0, 'reply_text': reply_text}


def _parse_quoted_reply_updates(target_type: str, raw_text: str):
    text = (raw_text or '').strip()
    updates = {}
    note_lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r'^([^:：]+)\s*[:：]\s*(.*)$', line)
        if not m:
            note_lines.append(line)
            continue
        key = normalize_line_key(m.group(1))
        value = (m.group(2) or '').strip()
        if key in ('stage', 'current_stage'):
            updates['stage'] = normalize_development_status(value) if target_type == 'development' else value
        elif key == 'next_action':
            updates['next_action'] = normalize_development_next_action(value) if target_type == 'development' else value
        elif key in ('next_contact_date', 'next_action_date'):
            updates['next_contact_date'] = value
        elif key == 'registered_address':
            updates['registered_address'] = value
        elif key in ('address', 'phone', 'name', 'url', 'source'):
            updates[key] = value
        elif key == 'content':
            if value:
                note_lines.append(value)
        else:
            note_lines.append(line)

    joined_note = '\n'.join([x for x in note_lines if x]).strip()
    plain_status_map = {
        '已連繫': '已聯繫',
        '已聯繫': '已聯繫',
        '已連絡': '已聯繫',
        '已聯絡': '已聯繫',
        '待聯繫': '待聯繫',
        '待聯絡': '待聯繫',
        '已寄開發信': '已寄開發信待跑開發',
        '持續追蹤': '持續追蹤',
        '未接': '待聯繫',
        '未接，再聯繫': '待聯繫',
    }
    if target_type == 'development' and not updates.get('stage'):
        s = plain_status_map.get(text, '')
        if s:
            updates['stage'] = s
    if not updates.get('registered_address') and _looks_like_address(text):
        updates['registered_address'] = text
    followup_text = joined_note or text
    return updates, followup_text


def process_quote_context_message(event):
    message = event.get('message') or {}
    quoted_message_id = (message.get('quotedMessageId') or '').strip()
    raw_text = (message.get('text') or '').strip()
    if not quoted_message_id or not raw_text:
        return {'handled': False}

    link = get_line_message_link(quoted_message_id)
    if not link:
        return {
            'handled': True,
            'ok': False,
            'reply_text': '這則回覆找不到對標資料，請直接回覆 bot 建立成功的那則訊息，或確認這則原始訊息是用指令新增成功的。'
        }

    target_type, doc, multi = _resolve_target_doc_from_link(link)
    if target_type not in ('buyer', 'seller', 'development'):
        return {
            'handled': True,
            'ok': False,
            'reply_text': '這則回覆目前無法判定對應客戶，請直接回覆 bot 建立成功的那則訊息。'
        }
    if multi:
        preview = '\n'.join([f"- {_describe_doc_brief(target_type, d)}" for d in multi[:5]])
        return {'handled': True, 'ok': False, 'reply_text': f'找到多筆同電話/同姓名資料，請確認要註記哪一筆：\n{preview}'}
    if not doc:
        label = '客需' if target_type == 'buyer' else '委託' if target_type == 'seller' else '開發'
        return {'handled': True, 'ok': False, 'reply_text': f'找不到對應的{label}資料'}

    collection_name, _, _, label_text = _collection_key_by_target(target_type)
    updates, followup_text = _parse_quoted_reply_updates(target_type, raw_text)
    doc_ref = db.collection(collection_name).document(doc.id)
    labels = dedupe_keep_order(['LINE紀錄', '群組回覆註記'])

    extra_updates = {}
    for k in ('address', 'url', 'phone', 'name', 'source'):
        if updates.get(k):
            extra_updates[k] = updates[k]
    if target_type == 'development':
        if updates.get('stage'):
            extra_updates['stage'] = updates['stage']
            extra_updates['current_stage'] = updates['stage']
        if updates.get('next_action'):
            extra_updates['next_action'] = updates['next_action']
        if updates.get('next_contact_date'):
            extra_updates['next_action_date'] = updates['next_contact_date']
    else:
        if updates.get('stage'):
            extra_updates['stage'] = updates['stage']

    registered_address = updates.get('registered_address', '')
    followup_content = build_line_summary(followup_text, event)
    update_customer_note_and_labels(
        target_type=target_type,
        doc_ref=doc_ref,
        content=followup_content,
        labels=labels,
        stage=updates.get('stage', ''),
        source=updates.get('source', link.get('source', 'LINE')),
        event=event,
        registered_address=registered_address,
        extra_updates=extra_updates,
    )

    current_after_update = doc_ref.get().to_dict() or {}
    nav_url = ''
    if registered_address:
        nav_url = _make_google_nav_url(registered_address)
    elif current_after_update.get('registered_address'):
        nav_url = _make_google_nav_url(current_after_update.get('registered_address'))

    followup_with_nav = followup_content
    if nav_url:
        followup_with_nav += f"\nGoogle導航: {nav_url}"

    add_customer_followup(
        target_type=target_type,
        customer_id=doc.id,
        content=followup_with_nav,
        next_action=updates.get('next_action', ''),
        next_contact_date=updates.get('next_contact_date', ''),
        labels=labels,
        line_event=event,
        stage=updates.get('stage', ''),
        registered_address=registered_address,
    )

    current = doc_ref.get().to_dict() or {}
    reply_name = current.get('name', '') or '未填姓名'
    reply_phone = current.get('phone', '') or '-'
    reply_text = f"已註記{label_text}：{reply_name}（{reply_phone}）"
    if registered_address:
        reply_text += f"\n已更新戶籍地址：{registered_address}"
    if nav_url:
        reply_text += f"\nGoogle導航: {nav_url}"
    return {
        'handled': True,
        'ok': True,
        'reply_text': reply_text[:5000],
        'target_type': target_type,
        'target_id': doc.id,
        'customer_name': reply_name,
        'phone': reply_phone,
        'parsed_tag': '群組回覆註記',
    }


def process_line_message_event(event):
    message = event.get('message') or {}
    if message.get('type') != 'text':
        return {'handled': False}

    sender_display_name = get_line_sender_display_name(event)
    raw_text = (message.get('text') or '').strip()
    quoted_message_id = (message.get('quotedMessageId') or '').strip()

    if quoted_message_id:
        quoted_result = process_quote_context_message(event)
        parsed = {'tag': '群組回覆註記', 'action': 'quoted_context_note', 'fields': {'quoted_message_id': quoted_message_id}, 'raw_text': raw_text}
        save_line_log(parsed, event, 'success' if quoted_result.get('ok') else 'failed', target_type=quoted_result.get('target_type', ''), target_id=quoted_result.get('target_id', ''), note=quoted_result.get('reply_text', ''), sender_display_name=sender_display_name)
        if quoted_result.get('ok') and quoted_result.get('target_type') and quoted_result.get('target_id'):
            incoming_message_id = message.get('id', '')
            save_line_message_link(incoming_message_id, quoted_result['target_type'], quoted_result['target_id'], tag=quoted_result.get('parsed_tag', ''), action='quoted_context_note', customer_name=quoted_result.get('customer_name', ''), phone=quoted_result.get('phone', ''), source_event=event)
        return quoted_result

    if raw_text.startswith('#新增開發批次'):
        parsed = parse_line_formatted_message(raw_text)
        if not parsed:
            result = {'handled': True, 'ok': False, 'reply_text': _build_development_input_help(batch=True)}
            save_line_log({'tag': '新增開發批次', 'action': 'create_development_batch', 'fields': {}, 'raw_text': raw_text}, event, 'failed', note=result['reply_text'], sender_display_name=sender_display_name)
            return result
    elif raw_text.startswith('#新增開發'):
        parsed = parse_line_formatted_message(raw_text)
        if not parsed:
            result = {'handled': True, 'ok': False, 'reply_text': _build_development_input_help(batch=False)}
            save_line_log({'tag': '新增開發', 'action': 'create_development', 'fields': {}, 'raw_text': raw_text}, event, 'failed', note=result['reply_text'], sender_display_name=sender_display_name)
            return result
    else:
        parsed = parse_line_formatted_message(raw_text)

    if not parsed:
        return {'handled': False}

    fields = parsed['fields']
    action = parsed['action']

    if action == 'create_buyer_need':
        result = create_buyer_need(fields, event)
    elif action == 'create_seller_listing':
        result = create_seller_listing(fields, event)
    elif action == 'create_development':
        result = create_development(fields, event)
    elif action == 'create_development_batch':
        result = create_development_batch(parsed.get('raw_body') or raw_text, event)
    elif action == 'development_followup':
        result = add_development_followup_via_line(fields, event)
    elif action == 'query_records':
        target_type, doc = resolve_customer_record(fields)
        if not doc and (fields.get('record_id') or fields.get('phone') or fields.get('name') or fields.get('address')):
            doc = find_development_record(fields.get('record_id',''), fields.get('phone',''), fields.get('name',''), fields.get('address',''))
            if doc:
                target_type = 'development'
        if not doc:
            result = {'handled': True, 'ok': False, 'reply_text': '查無唯一客戶，請補電話、地址或客戶ID'}
        else:
            result = {'handled': True, 'ok': True, 'reply_text': format_record_timeline(target_type, doc, limit=fields.get('limit', 10)), 'target_type': target_type, 'target_id': doc.id, 'customer_name': (doc.to_dict() or {}).get('name', ''), 'phone': (doc.to_dict() or {}).get('phone', ''), 'parsed_tag': parsed.get('tag', '')}
    elif action == 'query_contract_end':
        ok, text, ctx = query_contract_end_text(fields)
        result = {'handled': True, 'ok': ok, 'reply_text': text}
        if ctx:
            result.update(ctx)
    else:
        target_type = fields.get('target_type', '')
        if action == 'buyer_followup':
            target_type = 'buyer'
        elif action == 'seller_followup':
            target_type = 'seller'
        if target_type not in ('buyer', 'seller'):
            result = {'handled': True, 'ok': False, 'reply_text': '請提供對象：買方 或 賣方'}
        else:
            doc = find_customer_record(target_type=target_type, record_id=fields.get('record_id', ''), phone=fields.get('phone', ''), name=fields.get('name', ''), address=fields.get('address', ''))
            if not doc:
                result = {'handled': True, 'ok': False, 'reply_text': '找不到唯一客戶，請補客戶ID或正確電話'}
            else:
                doc_ref = db.collection('buyers' if target_type == 'buyer' else 'sellers').document(doc.id)
                labels = dedupe_keep_order(['LINE紀錄'] + ensure_list(fields.get('labels')))
                summary_parts = []
                if fields.get('content'):
                    summary_parts.append(fields['content'])
                if fields.get('address'):
                    summary_parts.append(f"地址/物件：{fields['address']}")
                if fields.get('price'):
                    summary_parts.append(f"價格：{fields['price']}")
                summary_text = build_line_summary('；'.join(summary_parts).strip() or 'LINE 更新', event)
                update_customer_note_and_labels(target_type=target_type, doc_ref=doc_ref, content=summary_text, labels=labels, stage=fields.get('stage', ''), source=fields.get('source', 'LINE'), event=event)
                if action in ('buyer_followup', 'seller_followup', 'classify'):
                    add_customer_followup(target_type=target_type, customer_id=doc.id, content=summary_text, next_action=fields.get('next_action', ''), next_contact_date=fields.get('next_contact_date', ''), labels=labels, line_event=event, stage=fields.get('stage', ''))
                label_text = '客需' if target_type == 'buyer' else '委託'
                current_data = doc_ref.get().to_dict() or {}
                result = {'handled': True, 'ok': True, 'reply_text': f"已註記{label_text}：{current_data.get('name', '')}（{current_data.get('phone','-')}）", 'target_type': target_type, 'target_id': doc.id, 'customer_name': current_data.get('name', ''), 'phone': current_data.get('phone', ''), 'parsed_tag': parsed.get('tag', '')}

    save_line_log(parsed, event, 'success' if result.get('ok') else 'failed', target_type=result.get('target_type', ''), target_id=result.get('target_id', ''), note=result.get('reply_text', ''), sender_display_name=sender_display_name)
    if result.get('ok') and result.get('target_type') and result.get('target_id'):
        incoming_message_id = message.get('id', '')
        save_line_message_link(incoming_message_id, result['target_type'], result['target_id'], tag=result.get('parsed_tag', ''), action=action, customer_name=result.get('customer_name', ''), phone=result.get('phone', ''), source_event=event)
    return result



# ========= source display + no-LINE-default patch (v4) =========

def infer_development_source(explicit_source: str, url: str) -> str:
    explicit = (explicit_source or '').strip()
    if explicit and explicit.upper() != 'LINE':
        return explicit
    return '踩線/屋主自售' if (url or '').strip() else '掃街'


def _store_source_value(target_type: str, incoming_source: str = '', current_source: str = '', url: str = '') -> str:
    incoming = (incoming_source or '').strip()
    current = (current_source or '').strip()
    if target_type == 'development':
        return infer_development_source(incoming or current, url)
    if incoming and incoming.upper() != 'LINE':
        return incoming
    if current and current.upper() != 'LINE':
        return current
    return ''


def _display_source_value(target_type: str, data: dict) -> str:
    raw = (data.get('source') or '').strip()
    if target_type == 'development':
        return raw if raw and raw.upper() != 'LINE' else infer_development_source('', data.get('url', ''))
    return '' if raw.upper() == 'LINE' else raw


def update_customer_note_and_labels(target_type: str, doc_ref, content: str, labels=None, stage='', source=None, event=None, registered_address='', extra_updates=None):
    labels = dedupe_keep_order(['LINE紀錄'] + ensure_list(labels))
    snapshot = doc_ref.get()
    current = snapshot.to_dict() or {}
    old_note = current.get('note', '')

    updates = {
        'labels': firestore.ArrayUnion(labels),
        'updated_at': now_taipei().isoformat(),
        'updated_by_id': 'line_bot',
        'updated_by_name': 'LINE Bot',
    }
    if content:
        source_label = build_line_operator_label(event) if event else 'LINE'
        updates['note'] = append_note_block(old_note, content, source_label)
    if stage:
        if target_type == 'development':
            normalized_stage = normalize_development_status(stage)
            updates['stage'] = normalized_stage
            updates['current_stage'] = normalized_stage
        else:
            updates['stage'] = stage

    # source: buyer/seller 空白就不要寫 LINE；development 則自動推斷
    url_for_source = ''
    if extra_updates and extra_updates.get('url'):
        url_for_source = extra_updates.get('url', '')
    else:
        url_for_source = current.get('url', '')
    normalized_source = _store_source_value(target_type, incoming_source=(source or ''), current_source=current.get('source', ''), url=url_for_source)
    if normalized_source:
        updates['source'] = normalized_source
    elif target_type in ('buyer', 'seller') and ((source or '').strip() == '' or str(source).strip().upper() == 'LINE'):
        # buyer/seller 空白時不覆蓋成 LINE，也不強制清空既有來源
        pass

    if registered_address:
        updates['registered_address'] = registered_address
        nav_url = _make_google_nav_url(registered_address)
        if nav_url:
            updates['registered_address_google_maps_url'] = nav_url
    if extra_updates:
        cleaned = {k: v for k, v in extra_updates.items() if v not in (None, '') and k != 'source'}
        if cleaned:
            updates.update(cleaned)
    doc_ref.update(updates)


def create_buyer_need(fields, event):
    phone = (fields.get('phone') or '').strip()
    matches = find_records_by_phone('buyers', phone)

    intent_type = normalize_intent_type(fields.get('intent_type_raw', '') or fields.get('intent_type', ''), fields)
    if not intent_type:
        return {'handled': True, 'ok': False, 'reply_text': '未寫入：#新增客需 請填 需求類型: 租 或 買賣'}

    labels = build_buyer_labels(intent_type, fields.get('labels'))
    budget = (fields.get('budget') or '').strip()
    source_value = _store_source_value('buyer', incoming_source=fields.get('source', ''))

    payload = {
        'name': (fields.get('name') or '').strip(),
        'phone': phone,
        'source': source_value,
        'intent_type': intent_type,
        'stage': (fields.get('stage') or '').strip() or '接觸',
        'preferred_areas': (fields.get('preferred_areas') or '').strip(),
        'property_type': (fields.get('property_type') or '').strip(),
        'room_range': (fields.get('room_range') or '').strip(),
        'car_need': (fields.get('car_need') or '').strip(),
        'labels': labels,
        'updated_at': now_taipei().isoformat(),
        'updated_by_id': 'line_bot',
        'updated_by_name': 'LINE Bot',
    }
    if intent_type == 'rent':
        payload['rent_max'] = (fields.get('rent_max') or '').strip() or budget
    else:
        payload['budget_max'] = (fields.get('budget_max') or '').strip() or budget

    extra_content = (fields.get('content') or '').strip()
    note_content = build_line_summary(extra_content or '新增客需', event)

    if len(matches) == 1:
        doc = matches[0]
        doc_ref = db.collection('buyers').document(doc.id)
        update_customer_note_and_labels(target_type='buyer', doc_ref=doc_ref, content=note_content, labels=labels, stage=payload['stage'], source=source_value or None, event=event)
        clean_payload = {k: v for k, v in payload.items() if not (k == 'source' and not source_value)}
        doc_ref.update(clean_payload)
        add_customer_followup(target_type='buyer', customer_id=doc.id, content=note_content, labels=labels, line_event=event)
        updated_doc = doc_ref.get().to_dict() or {}
        return {'handled': True, 'ok': True, 'reply_text': f"已註記客需：{updated_doc.get('name', '')}", 'target_type': 'buyer', 'target_id': doc.id, 'customer_name': updated_doc.get('name', ''), 'phone': updated_doc.get('phone', ''), 'parsed_tag': '新增客需'}
    if len(matches) > 1:
        return {'handled': True, 'ok': False, 'reply_text': '未寫入：同電話有多位買方，請先到後台整理或改用客戶ID'}

    now = now_taipei().isoformat()
    payload.update({'created_at': now, 'created_by_id': 'line_bot', 'created_by_name': 'LINE Bot', 'note': append_note_block('', note_content, build_line_operator_label(event))})
    doc_ref = db.collection('buyers').document()
    doc_ref.set(payload)
    add_customer_followup(target_type='buyer', customer_id=doc_ref.id, content=note_content, labels=labels, line_event=event)
    return {'handled': True, 'ok': True, 'reply_text': f"已註記客需：{payload['name']}（{'租' if intent_type == 'rent' else '買賣'}）", 'target_type': 'buyer', 'target_id': doc_ref.id, 'customer_name': payload['name'], 'phone': payload['phone'], 'parsed_tag': '新增客需'}


def create_seller_listing(fields, event):
    phone = (fields.get('phone') or '').strip()
    matches = find_records_by_phone('sellers', phone)

    deal_type = normalize_deal_type(fields.get('deal_type_raw', '') or fields.get('deal_type', ''))
    if not deal_type:
        return {'handled': True, 'ok': False, 'reply_text': '未寫入：#新增委託 請填 委託類型: 賣 或 出租'}

    labels = build_seller_labels(deal_type, fields.get('labels'))
    source_value = _store_source_value('seller', incoming_source=fields.get('source', ''))
    note_content = build_line_summary((fields.get('content') or '').strip() or (fields.get('address') or '').strip() or '新增委託', event)

    payload = {
        'name': (fields.get('name') or '').strip(),
        'phone': phone,
        'source': source_value,
        'address': (fields.get('address') or '').strip(),
        'property_type': (fields.get('property_type') or '').strip(),
        'stage': (fields.get('stage') or '').strip() or '委託中',
        'deal_type': deal_type,
        'expected_price': (fields.get('expected_price') or '').strip() or (fields.get('price') or '').strip(),
        'min_price': (fields.get('min_price') or '').strip(),
        'contract_end_date': (fields.get('contract_end_date') or '').strip(),
        'labels': labels,
        'updated_at': now_taipei().isoformat(),
        'updated_by_id': 'line_bot',
        'updated_by_name': 'LINE Bot',
    }

    if len(matches) == 1:
        doc = matches[0]
        doc_ref = db.collection('sellers').document(doc.id)
        update_customer_note_and_labels(target_type='seller', doc_ref=doc_ref, content=note_content, labels=labels, stage=payload['stage'], source=source_value or None, event=event)
        clean_payload = {k: v for k, v in payload.items() if not (k == 'source' and not source_value)}
        doc_ref.update(clean_payload)
        add_customer_followup(target_type='seller', customer_id=doc.id, content=note_content, labels=labels, line_event=event)
        updated_doc = doc_ref.get().to_dict() or {}
        return {'handled': True, 'ok': True, 'reply_text': f"已註記委託：{updated_doc.get('name', '')}", 'target_type': 'seller', 'target_id': doc.id, 'customer_name': updated_doc.get('name', ''), 'phone': updated_doc.get('phone', ''), 'parsed_tag': '新增委託'}
    if len(matches) > 1:
        return {'handled': True, 'ok': False, 'reply_text': '未寫入：同電話有多位委託，請先到後台整理或改用客戶ID'}

    now = now_taipei().isoformat()
    payload.update({'created_at': now, 'created_by_id': 'line_bot', 'created_by_name': 'LINE Bot', 'note': append_note_block('', note_content, build_line_operator_label(event))})
    doc_ref = db.collection('sellers').document()
    doc_ref.set(payload)
    add_customer_followup(target_type='seller', customer_id=doc_ref.id, content=note_content, labels=labels, line_event=event)
    return {'handled': True, 'ok': True, 'reply_text': f"已註記委託：{payload['name']}（{'出租' if deal_type == 'rent' else '買賣'}）", 'target_type': 'seller', 'target_id': doc_ref.id, 'customer_name': payload['name'], 'phone': payload['phone'], 'parsed_tag': '新增委託'}


def create_development(fields, event):
    phone = (fields.get('phone') or '').strip()
    name = (fields.get('name') or '').strip() or '未填姓名'
    url = (fields.get('url') or '').strip()
    source_value = infer_development_source(fields.get('source', ''), url)
    address = (fields.get('address') or '').strip()
    registered_address = (fields.get('registered_address') or '').strip()
    nav_url = _make_google_nav_url(registered_address or address)

    matches = find_records_by_phone('developments', phone) if phone else []
    if not matches and address:
        doc = find_development_record(address=address)
        if doc:
            matches = [doc]

    labels = build_development_labels(fields.get('labels'))
    content_text = (fields.get('content') or '').strip() or registered_address or address or url or '新增開發'
    note_content = build_line_summary(content_text, event)

    payload = {
        'name': name,
        'phone': phone,
        'source': source_value,
        'url': url,
        'address': address,
        'registered_address': registered_address,
        'registered_address_google_maps_url': nav_url,
        'current_stage': normalize_development_status((fields.get('current_stage') or '').strip() or (fields.get('stage') or '').strip() or '待聯繫'),
        'stage': normalize_development_status((fields.get('current_stage') or '').strip() or (fields.get('stage') or '').strip() or '待聯繫'),
        'next_action': normalize_development_next_action((fields.get('next_action') or '').strip()),
        'next_action_date': (fields.get('next_action_date') or '').strip() or (fields.get('next_contact_date') or '').strip(),
        'record_date': (fields.get('record_date') or '').strip() or now_taipei().strftime('%Y-%m-%d'),
        'labels': labels,
        'updated_at': now_taipei().isoformat(),
        'updated_by_id': 'line_bot',
        'updated_by_name': 'LINE Bot',
        'sender_display_name': get_line_sender_display_name(event) or '',
    }

    if len(matches) == 1:
        doc = matches[0]
        doc_ref = db.collection('developments').document(doc.id)
        update_customer_note_and_labels(target_type='development', doc_ref=doc_ref, content=note_content, labels=labels, stage=payload['stage'], source=source_value, event=event, registered_address=registered_address)
        clean_updates = {k: v for k, v in payload.items() if v not in ('', None)}
        doc_ref.update(clean_updates)
        add_customer_followup(target_type='development', customer_id=doc.id, content=note_content, next_action=payload.get('next_action', ''), next_contact_date=payload.get('next_action_date', ''), labels=labels, line_event=event)
        updated_doc = doc_ref.get().to_dict() or {}
        reply_text = f"已註記開發：{updated_doc.get('name', '')}（{updated_doc.get('phone', '-') or '-'}）"
        if registered_address or nav_url:
            reply_text += '\n已更新戶籍地址與 Google導航連結'
        return {'handled': True, 'ok': True, 'reply_text': reply_text[:5000], 'target_type': 'development', 'target_id': doc.id, 'customer_name': updated_doc.get('name', ''), 'phone': updated_doc.get('phone', ''), 'parsed_tag': '新增開發'}
    if len(matches) > 1:
        return {'handled': True, 'ok': False, 'reply_text': '未寫入：同電話有多筆開發資料，請補地址或客戶ID'}

    now = now_taipei().isoformat()
    payload.update({'created_at': now, 'created_by_id': 'line_bot', 'created_by_name': 'LINE Bot', 'note': append_note_block('', note_content, build_line_operator_label(event))})
    doc_ref = db.collection('developments').document()
    doc_ref.set(payload)
    add_customer_followup(target_type='development', customer_id=doc_ref.id, content=note_content, next_action=payload.get('next_action', ''), next_contact_date=payload.get('next_action_date', ''), labels=labels, line_event=event)
    reply_text = f"已註記開發：{name}（{phone or '-'}）"
    if registered_address or nav_url:
        reply_text += '\n已更新戶籍地址與 Google導航連結'
    return {'handled': True, 'ok': True, 'reply_text': reply_text[:5000], 'target_type': 'development', 'target_id': doc_ref.id, 'customer_name': name, 'phone': phone, 'parsed_tag': '新增開發'}


def add_development_followup_via_line(fields, event):
    doc = find_development_record(record_id=fields.get('record_id', ''), phone=fields.get('phone', ''), name=fields.get('name', ''), address=fields.get('address', ''))
    if not doc:
        return {'handled': True, 'ok': False, 'reply_text': '找不到唯一開發資料，請補電話、地址或客戶ID'}

    current = doc.to_dict() or {}
    content = (fields.get('content') or '').strip() or '開發追蹤更新'
    current_stage = normalize_development_status((fields.get('current_stage') or '').strip() or (fields.get('stage') or '').strip())
    next_action = normalize_development_next_action((fields.get('next_action') or '').strip())
    next_action_date = (fields.get('next_action_date') or '').strip() or (fields.get('next_contact_date') or '').strip()
    registered_address = (fields.get('registered_address') or '').strip()
    source_value = infer_development_source(fields.get('source', '') or current.get('source', ''), (fields.get('url') or '').strip() or current.get('url', ''))

    doc_ref = db.collection('developments').document(doc.id)
    update_customer_note_and_labels(target_type='development', doc_ref=doc_ref, content=build_line_summary(content, event), labels=build_development_labels(fields.get('labels')), stage=current_stage, source=source_value, event=event, registered_address=registered_address)
    updates = {'updated_at': now_taipei().isoformat(), 'updated_by_id': 'line_bot', 'updated_by_name': 'LINE Bot'}
    if current_stage:
        updates['current_stage'] = current_stage
        updates['stage'] = current_stage
    if next_action:
        updates['next_action'] = next_action
    if next_action_date:
        updates['next_action_date'] = next_action_date
    if registered_address:
        updates['registered_address'] = registered_address
        nav_url = _make_google_nav_url(registered_address)
        if nav_url:
            updates['registered_address_google_maps_url'] = nav_url
    if source_value:
        updates['source'] = source_value
    doc_ref.update(updates)
    add_customer_followup(target_type='development', customer_id=doc.id, content=build_line_summary(content, event), next_action=next_action, next_contact_date=next_action_date, labels=build_development_labels(fields.get('labels')), line_event=event)

    data = doc_ref.get().to_dict() or {}
    reply_text = f"已註記開發：{data.get('name', '')}"
    if registered_address:
        reply_text += '\n已更新戶籍地址與 Google導航連結'
    return {'handled': True, 'ok': True, 'reply_text': reply_text, 'target_type': 'development', 'target_id': doc.id, 'customer_name': data.get('name', ''), 'phone': data.get('phone', ''), 'parsed_tag': '開發追蹤'}


def format_record_timeline(target_type: str, doc_snapshot, limit=10):
    data = doc_snapshot.to_dict() or {}
    record_id = doc_snapshot.id
    if target_type == 'buyer':
        followup_collection = 'buyer_followups'
        key_name = 'buyer_id'
    elif target_type == 'seller':
        followup_collection = 'seller_followups'
        key_name = 'seller_id'
    else:
        followup_collection = 'development_followups'
        key_name = 'development_id'

    followups = []
    for d in db.collection(followup_collection).where(key_name, '==', record_id).stream():
        item = d.to_dict() or {}
        followups.append({'time': item.get('contact_time') or item.get('created_at') or '', 'channel': item.get('channel', 'LINE'), 'text': (item.get('content') or '').strip(), 'created_by_name': item.get('created_by_name', '') or '', 'sender_display_name': item.get('sender_display_name', '') or '', 'stage': item.get('stage', '') or '', 'registered_address': item.get('registered_address', '') or ''})
    followups = [x for x in followups if x.get('text') or x.get('registered_address')]
    followups.sort(key=lambda x: x.get('time', ''), reverse=True)

    lines = ['客戶資訊']
    source_text = _display_source_value(target_type, data)
    if target_type == 'buyer':
        intent_map = {'rent': '租屋', 'buy': '買賣', 'both': '租買皆可'}
        lines.extend([
            f"姓名: {data.get('name', '')}",
            f"電話: {data.get('phone', '')}",
            f"客源來源: {source_text}",
            f"需求類型: {intent_map.get(data.get('intent_type', ''), data.get('intent_type', '') or '-')}",
            f"預算: {data.get('budget_min', '') or data.get('rent_min', '') or '-'} ~ {data.get('budget_max', '') or data.get('rent_max', '') or '-'}",
            f"偏好區域: {data.get('preferred_areas', '') or '-'}",
            f"產品類型: {data.get('property_type', '') or '-'}",
            f"房數需求: {data.get('room_range', '') or '-'}",
            f"車位需求: {data.get('car_need', '') or '-'}",
        ])
    elif target_type == 'seller':
        deal_map = {'sale': '買賣', 'rent': '出租'}
        lines.extend([
            f"姓名: {data.get('name', '')}",
            f"電話: {data.get('phone', '')}",
            f"客源來源: {source_text}",
            f"委託類型: {deal_map.get(data.get('deal_type', ''), data.get('deal_type', '') or '-')}",
            f"地址: {data.get('address', '') or '-'}",
            f"產品類型: {data.get('property_type', '') or '-'}",
            f"開價: {data.get('expected_price', '') or '-'}",
            f"底價: {data.get('min_price', '') or '-'}",
            f"委託到期日: {data.get('contract_end_date', '') or '-'}",
        ])
    else:
        lines.extend([
            f"姓名: {data.get('name', '')}",
            f"電話: {data.get('phone', '') or '-'}",
            f"來源: {source_text}",
            f"地址: {data.get('address', '') or '-'}",
            f"戶籍地址: {data.get('registered_address', '') or '-'}",
            f"網址: {data.get('url', '') or '-'}",
            f"進度: {data.get('stage', '') or '-'}",
        ])
    lines.append('')
    lines.append('追蹤進度')
    if not followups:
        lines.append('目前沒有追蹤紀錄')
    else:
        for item in followups[:limit]:
            header_parts = [item.get('time', ''), item.get('channel', 'LINE')]
            if item.get('stage'):
                header_parts.append(item['stage'])
            creator = (item.get('created_by_name') or '').strip()
            sender = (item.get('sender_display_name') or '').strip()
            if creator:
                header_parts.append(f"KEYIN: {creator}")
            if sender:
                header_parts.append(f"留言者: {sender}")
            lines.append('｜'.join([x for x in header_parts if x]))
            if item.get('text'):
                lines.append(item.get('text', ''))
            if item.get('registered_address'):
                lines.append(f"戶籍地址：{item['registered_address']}")
            lines.append('')
    return '\n'.join(lines).strip()[:4500]


def process_quote_context_message(event):
    message = event.get('message') or {}
    quoted_message_id = (message.get('quotedMessageId') or '').strip()
    raw_text = (message.get('text') or '').strip()
    if not quoted_message_id or not raw_text:
        return {'handled': False}

    link = get_line_message_link(quoted_message_id)
    if not link:
        return {'handled': True, 'ok': False, 'reply_text': '這則回覆找不到對標資料，請直接回覆 bot 建立成功的那則訊息，或確認這則原始訊息是用指令新增成功的。'}

    target_type, doc, multi = _resolve_target_doc_from_link(link)
    if target_type not in ('buyer', 'seller', 'development'):
        return {'handled': True, 'ok': False, 'reply_text': '這則回覆目前無法判定對應客戶，請直接回覆 bot 建立成功的那則訊息。'}
    if multi:
        preview = '\n'.join([f"- {_describe_doc_brief(target_type, d)}" for d in multi[:5]])
        return {'handled': True, 'ok': False, 'reply_text': f'找到多筆同電話/同姓名資料，請確認要註記哪一筆：\n{preview}'}
    if not doc:
        label = '客需' if target_type == 'buyer' else '委託' if target_type == 'seller' else '開發'
        return {'handled': True, 'ok': False, 'reply_text': f'找不到對應的{label}資料'}

    collection_name, _, _, label_text = _collection_key_by_target(target_type)
    updates, followup_text = _parse_quoted_reply_updates(target_type, raw_text)
    doc_ref = db.collection(collection_name).document(doc.id)
    labels = dedupe_keep_order(['LINE紀錄', '群組回覆註記'])

    current_before = doc.to_dict() or {}
    url_for_source = (updates.get('url') or '').strip() or current_before.get('url', '')
    source_for_note = _store_source_value(target_type, incoming_source=updates.get('source', ''), current_source=current_before.get('source', ''), url=url_for_source)

    extra_updates = {}
    for k in ('address', 'url', 'phone', 'name'):
        if updates.get(k):
            extra_updates[k] = updates[k]
    if source_for_note:
        extra_updates['source'] = source_for_note
    if target_type == 'development':
        if updates.get('stage'):
            extra_updates['stage'] = updates['stage']
            extra_updates['current_stage'] = updates['stage']
        if updates.get('next_action'):
            extra_updates['next_action'] = updates['next_action']
        if updates.get('next_contact_date'):
            extra_updates['next_action_date'] = updates['next_contact_date']
    else:
        if updates.get('stage'):
            extra_updates['stage'] = updates['stage']

    registered_address = updates.get('registered_address', '')
    followup_content = build_line_summary(followup_text, event)
    update_customer_note_and_labels(target_type=target_type, doc_ref=doc_ref, content=followup_content, labels=labels, stage=updates.get('stage', ''), source=source_for_note or None, event=event, registered_address=registered_address, extra_updates=extra_updates)

    add_customer_followup(target_type=target_type, customer_id=doc.id, content=followup_content, next_action=updates.get('next_action', ''), next_contact_date=updates.get('next_contact_date', ''), labels=labels, line_event=event, stage=updates.get('stage', ''), registered_address=registered_address if target_type == 'development' else '')

    current = doc_ref.get().to_dict() or {}
    reply_name = current.get('name', '') or '未填姓名'
    reply_phone = current.get('phone', '') or '-'
    reply_text = f"已註記{label_text}：{reply_name}（{reply_phone}）"
    if registered_address and target_type == 'development':
        reply_text += '\n已更新戶籍地址與 Google導航連結'
    return {'handled': True, 'ok': True, 'reply_text': reply_text[:5000], 'target_type': target_type, 'target_id': doc.id, 'customer_name': reply_name, 'phone': reply_phone, 'parsed_tag': '群組回覆註記'}


def process_line_message_event(event):
    message = event.get('message') or {}
    if message.get('type') != 'text':
        return {'handled': False}
    sender_display_name = get_line_sender_display_name(event)
    raw_text = (message.get('text') or '').strip()
    quoted_message_id = (message.get('quotedMessageId') or '').strip()

    if quoted_message_id:
        quoted_result = process_quote_context_message(event)
        parsed = {'tag': '群組回覆註記', 'action': 'quoted_context_note', 'fields': {'quoted_message_id': quoted_message_id}, 'raw_text': raw_text}
        save_line_log(parsed, event, 'success' if quoted_result.get('ok') else 'failed', target_type=quoted_result.get('target_type', ''), target_id=quoted_result.get('target_id', ''), note=quoted_result.get('reply_text', ''), sender_display_name=sender_display_name)
        if quoted_result.get('ok') and quoted_result.get('target_type') and quoted_result.get('target_id'):
            incoming_message_id = message.get('id', '')
            save_line_message_link(incoming_message_id, quoted_result['target_type'], quoted_result['target_id'], tag=quoted_result.get('parsed_tag', ''), action='quoted_context_note', customer_name=quoted_result.get('customer_name', ''), phone=quoted_result.get('phone', ''), source_event=event)
        return quoted_result

    if raw_text.startswith('#新增開發批次'):
        parsed = parse_line_formatted_message(raw_text)
        if not parsed:
            result = {'handled': True, 'ok': False, 'reply_text': _build_development_input_help(batch=True)}
            save_line_log({'tag': '新增開發批次', 'action': 'create_development_batch', 'fields': {}, 'raw_text': raw_text}, event, 'failed', note=result['reply_text'], sender_display_name=sender_display_name)
            return result
    elif raw_text.startswith('#新增開發'):
        parsed = parse_line_formatted_message(raw_text)
        if not parsed:
            result = {'handled': True, 'ok': False, 'reply_text': _build_development_input_help(batch=False)}
            save_line_log({'tag': '新增開發', 'action': 'create_development', 'fields': {}, 'raw_text': raw_text}, event, 'failed', note=result['reply_text'], sender_display_name=sender_display_name)
            return result
    else:
        parsed = parse_line_formatted_message(raw_text)

    if not parsed:
        return {'handled': False}

    fields = parsed['fields']
    action = parsed['action']

    if action == 'create_buyer_need':
        result = create_buyer_need(fields, event)
    elif action == 'create_seller_listing':
        result = create_seller_listing(fields, event)
    elif action == 'create_development':
        result = create_development(fields, event)
    elif action == 'create_development_batch':
        result = create_development_batch(parsed.get('raw_body') or raw_text, event)
    elif action == 'development_followup':
        result = add_development_followup_via_line(fields, event)
    elif action == 'query_records':
        target_type, doc = resolve_customer_record(fields)
        if not doc and (fields.get('record_id') or fields.get('phone') or fields.get('name') or fields.get('address')):
            doc = find_development_record(fields.get('record_id',''), fields.get('phone',''), fields.get('name',''), fields.get('address',''))
            if doc:
                target_type = 'development'
        if not doc:
            result = {'handled': True, 'ok': False, 'reply_text': '查無唯一客戶，請補電話、地址或客戶ID'}
        else:
            result = {'handled': True, 'ok': True, 'reply_text': format_record_timeline(target_type, doc, limit=fields.get('limit', 10)), 'target_type': target_type, 'target_id': doc.id, 'customer_name': (doc.to_dict() or {}).get('name', ''), 'phone': (doc.to_dict() or {}).get('phone', ''), 'parsed_tag': parsed.get('tag', '')}
    elif action == 'query_contract_end':
        ok, text, ctx = query_contract_end_text(fields)
        result = {'handled': True, 'ok': ok, 'reply_text': text}
        if ctx:
            result.update(ctx)
    else:
        target_type = fields.get('target_type', '')
        if action == 'buyer_followup':
            target_type = 'buyer'
        elif action == 'seller_followup':
            target_type = 'seller'
        if target_type not in ('buyer', 'seller'):
            result = {'handled': True, 'ok': False, 'reply_text': '請提供對象：買方 或 賣方'}
        else:
            doc = find_customer_record(target_type=target_type, record_id=fields.get('record_id', ''), phone=fields.get('phone', ''), name=fields.get('name', ''), address=fields.get('address', ''))
            if not doc:
                result = {'handled': True, 'ok': False, 'reply_text': '找不到唯一客戶，請補客戶ID或正確電話'}
            else:
                doc_ref = db.collection('buyers' if target_type == 'buyer' else 'sellers').document(doc.id)
                labels = dedupe_keep_order(['LINE紀錄'] + ensure_list(fields.get('labels')))
                summary_parts = []
                if fields.get('content'):
                    summary_parts.append(fields['content'])
                if fields.get('address'):
                    summary_parts.append(f"地址/物件：{fields['address']}")
                if fields.get('price'):
                    summary_parts.append(f"價格：{fields['price']}")
                summary_text = build_line_summary('；'.join(summary_parts).strip() or 'LINE 更新', event)
                source_value = _store_source_value(target_type, incoming_source=fields.get('source', ''), current_source=(doc.to_dict() or {}).get('source', ''), url=(doc.to_dict() or {}).get('url', ''))
                update_customer_note_and_labels(target_type=target_type, doc_ref=doc_ref, content=summary_text, labels=labels, stage=fields.get('stage', ''), source=source_value or None, event=event)
                if action in ('buyer_followup', 'seller_followup', 'classify'):
                    add_customer_followup(target_type=target_type, customer_id=doc.id, content=summary_text, next_action=fields.get('next_action', ''), next_contact_date=fields.get('next_contact_date', ''), labels=labels, line_event=event, stage=fields.get('stage', ''))
                label_text = '客需' if target_type == 'buyer' else '委託'
                current_data = doc_ref.get().to_dict() or {}
                result = {'handled': True, 'ok': True, 'reply_text': f"已註記{label_text}：{current_data.get('name', '')}（{current_data.get('phone','-')}）", 'target_type': target_type, 'target_id': doc.id, 'customer_name': current_data.get('name', ''), 'phone': current_data.get('phone', ''), 'parsed_tag': parsed.get('tag', '')}

    save_line_log(parsed, event, 'success' if result.get('ok') else 'failed', target_type=result.get('target_type', ''), target_id=result.get('target_id', ''), note=result.get('reply_text', ''), sender_display_name=sender_display_name)
    if result.get('ok') and result.get('target_type') and result.get('target_id'):
        incoming_message_id = message.get('id', '')
        save_line_message_link(incoming_message_id, result['target_type'], result['target_id'], tag=result.get('parsed_tag', ''), action=action, customer_name=result.get('customer_name', ''), phone=result.get('phone', ''), source_event=event)
    return result


# ========= LINE Bot 代辦事項指令 Patch =========
# 使用方式：
# #新增代辦
# 日期: 2026-05-29
# 事項: 打給王小姐確認貸款資料
# 備註: 可空白
#
# #今日代辦
# #查詢代辦
# 日期: 2026-05-29
#
# #完成代辦
# ID: abc123
#
# #清除代辦
# ID: abc123

from datetime import timedelta

LINE_TODO_COLLECTION = os.environ.get('LINE_TODO_COLLECTION', 'line_todos')


def _line_todo_target_from_event(event):
    """取得這則 LINE 訊息要綁定的提醒對象：群組、聊天室或個人。"""
    source = event.get('source') or {}
    if source.get('groupId'):
        return source.get('groupId'), 'group'
    if source.get('roomId'):
        return source.get('roomId'), 'room'
    return source.get('userId', ''), 'user'


def _parse_line_todo_date(value: str):
    """支援 今天/明天/後天、YYYY-MM-DD、YYYY/MM/DD、MM/DD。回傳 YYYY-MM-DD。"""
    raw = (value or '').strip()
    today = now_taipei().date()
    if not raw:
        return today.strftime('%Y-%m-%d')

    aliases = {
        '今天': 0,
        '今日': 0,
        '明天': 1,
        '明日': 1,
        '後天': 2,
    }
    if raw in aliases:
        return (today + timedelta(days=aliases[raw])).strftime('%Y-%m-%d')

    m = re.search(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})', raw)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return datetime(y, mo, d, tzinfo=TAIPEI_TZ).date().strftime('%Y-%m-%d')
        except Exception:
            return ''

    m = re.search(r'(?<!\d)(\d{1,2})[/-](\d{1,2})(?!\d)', raw)
    if m:
        mo, d = map(int, m.groups())
        y = today.year
        try:
            return datetime(y, mo, d, tzinfo=TAIPEI_TZ).date().strftime('%Y-%m-%d')
        except Exception:
            return ''

    return ''


def _split_inline_todo_text(text: str):
    """處理：#新增代辦 明天 打給客戶。"""
    s = (text or '').strip()
    if not s:
        return '', ''
    parts = s.split(maxsplit=1)
    possible_date = _parse_line_todo_date(parts[0]) if parts else ''
    if possible_date and len(parts) > 1:
        return possible_date, parts[1].strip()
    return '', s


def _parse_line_todo_fields(raw_text: str, tag: str):
    lines = [ln.rstrip() for ln in (raw_text or '').splitlines()]
    if not lines:
        return {}

    first = lines[0].strip()
    inline = first.replace('#' + tag, '', 1).strip() if first.startswith('#' + tag) else ''
    fields = {}
    note_lines = []

    key_map = {
        'ID': 'todo_id',
        'id': 'todo_id',
        '編號': 'todo_id',
        '代辦ID': 'todo_id',
        '事項': 'title',
        '代辦': 'title',
        '內容': 'title',
        '工作': 'title',
        '日期': 'todo_date_raw',
        '時間': 'todo_date_raw',
        '期限': 'todo_date_raw',
        '提醒日期': 'todo_date_raw',
        '備註': 'note',
        '說明': 'note',
    }

    if inline:
        inline_date, inline_title = _split_inline_todo_text(inline)
        if inline_date:
            fields['todo_date'] = inline_date
        if inline_title:
            if tag in ('完成代辦', '清除代辦', '刪除代辦'):
                fields['todo_id'] = inline_title
            else:
                fields['title'] = inline_title

    for raw in lines[1:]:
        line = raw.strip()
        if not line:
            continue
        m = re.match(r'^([^:：]+)\s*[:：]\s*(.*)$', line)
        if m:
            key = key_map.get(m.group(1).strip(), m.group(1).strip())
            value = (m.group(2) or '').strip()
            fields[key] = value
        else:
            note_lines.append(line)

    if note_lines:
        if tag in ('完成代辦', '清除代辦', '刪除代辦') and not fields.get('todo_id'):
            fields['todo_id'] = note_lines[0]
        elif not fields.get('title'):
            fields['title'] = note_lines[0]
            if len(note_lines) > 1:
                fields['note'] = '\n'.join(note_lines[1:]).strip()
        else:
            fields['note'] = (fields.get('note', '') + '\n' + '\n'.join(note_lines)).strip()

    if fields.get('todo_date_raw') and not fields.get('todo_date'):
        fields['todo_date'] = _parse_line_todo_date(fields.get('todo_date_raw'))
    elif not fields.get('todo_date'):
        fields['todo_date'] = now_taipei().strftime('%Y-%m-%d')

    return fields


def _format_line_todo_list(items, title):
    if not items:
        return f'{title}\n目前沒有未完成代辦。'

    lines = [title]
    for idx, item in enumerate(items, 1):
        data = item.to_dict() or {}
        short_id = item.id[:6]
        note = (data.get('note') or '').strip()
        line = f"{idx}. [{short_id}] {data.get('title', '')}"
        if note:
            line += f"\n   備註: {note}"
        lines.append(line)
    lines.append('')
    lines.append('完成請回：')
    lines.append('#完成代辦')
    lines.append('ID: 上方編號')
    return '\n'.join(lines)[:5000]


def _get_open_line_todos(todo_date='', target_id=''):
    query_date = todo_date or now_taipei().strftime('%Y-%m-%d')
    docs = list(db.collection(LINE_TODO_COLLECTION).where('todo_date', '==', query_date).stream())
    result = []
    for doc in docs:
        data = doc.to_dict() or {}
        if data.get('status', 'open') != 'open':
            continue
        if target_id and data.get('line_target_id') != target_id:
            continue
        result.append(doc)
    result.sort(key=lambda d: ((d.to_dict() or {}).get('created_at', ''), d.id))
    return result


def _find_line_todo(todo_key: str, target_id=''):
    key = (todo_key or '').strip()
    if not key:
        return None, '請提供代辦 ID 或事項關鍵字。'

    # 先用完整文件 ID 找
    direct = db.collection(LINE_TODO_COLLECTION).document(key).get()
    if direct.exists:
        data = direct.to_dict() or {}
        if target_id and data.get('line_target_id') != target_id:
            return None, '這筆代辦不是在目前這個 LINE 對話建立的。'
        return direct, ''

    matches = []
    for doc in db.collection(LINE_TODO_COLLECTION).stream():
        data = doc.to_dict() or {}
        if data.get('status', 'open') != 'open':
            continue
        if target_id and data.get('line_target_id') != target_id:
            continue
        title = data.get('title', '')
        if doc.id.startswith(key) or key in title:
            matches.append(doc)

    if len(matches) == 1:
        return matches[0], ''
    if len(matches) > 1:
        preview = '\n'.join([f"- [{d.id[:6]}] {(d.to_dict() or {}).get('title','')}" for d in matches[:8]])
        return None, '找到多筆代辦，請用 ID 完成：\n' + preview
    return None, '找不到這筆未完成代辦，請先輸入 #今日代辦 查看 ID。'


def create_line_todo(fields, event):
    title = (fields.get('title') or '').strip()
    todo_date = _parse_line_todo_date(fields.get('todo_date') or fields.get('todo_date_raw') or '')
    note = (fields.get('note') or '').strip()

    if not title:
        return {'handled': True, 'ok': False, 'reply_text': '未新增：請填「事項」。\n\n範例：\n#新增代辦\n日期: 明天\n事項: 打給王小姐確認貸款資料'}
    if not todo_date:
        return {'handled': True, 'ok': False, 'reply_text': '未新增：日期格式看不懂，請用 2026-05-29、5/29、今天、明天。'}

    target_id, target_type = _line_todo_target_from_event(event)
    source = event.get('source') or {}
    sender_display_name = get_line_sender_display_name(event)
    now = now_taipei().isoformat()

    doc_ref = db.collection(LINE_TODO_COLLECTION).document()
    doc_ref.set({
        'title': title,
        'todo_date': todo_date,
        'note': note,
        'status': 'open',
        'line_target_id': target_id,
        'line_target_type': target_type,
        'line_group_id': source.get('groupId', ''),
        'line_room_id': source.get('roomId', ''),
        'line_user_id': source.get('userId', ''),
        'sender_display_name': sender_display_name,
        'created_at': now,
        'created_by_id': 'line_bot',
        'created_by_name': sender_display_name or 'LINE Bot',
        'reminder_sent_dates': [],
    })

    return {
        'handled': True,
        'ok': True,
        'reply_text': f"已新增代辦：{title}\n日期：{todo_date}\nID：{doc_ref.id[:6]}\n\n當天提醒清單會包含這筆代辦。",
        'parsed_tag': '新增代辦',
    }


def query_line_todos(fields, event, force_today=False):
    target_id, _ = _line_todo_target_from_event(event)
    todo_date = now_taipei().strftime('%Y-%m-%d') if force_today else _parse_line_todo_date(fields.get('todo_date') or fields.get('todo_date_raw') or '')
    items = _get_open_line_todos(todo_date=todo_date, target_id=target_id)
    title = f'{todo_date} 代辦清單'
    return {'handled': True, 'ok': True, 'reply_text': _format_line_todo_list(items, title), 'parsed_tag': '查詢代辦'}


def complete_line_todo(fields, event):
    target_id, _ = _line_todo_target_from_event(event)
    todo_key = fields.get('todo_id') or fields.get('title') or ''
    doc, err = _find_line_todo(todo_key, target_id=target_id)
    if err:
        return {'handled': True, 'ok': False, 'reply_text': err, 'parsed_tag': '完成代辦'}

    data = doc.to_dict() or {}
    doc.reference.update({
        'status': 'done',
        'done_at': now_taipei().isoformat(),
        'done_by_id': 'line_bot',
        'done_by_name': get_line_sender_display_name(event) or 'LINE Bot',
        'updated_at': now_taipei().isoformat(),
    })
    return {'handled': True, 'ok': True, 'reply_text': f"已完成並從未完成清單移除：{data.get('title', '')}", 'parsed_tag': '完成代辦'}


def delete_line_todo(fields, event):
    target_id, _ = _line_todo_target_from_event(event)
    todo_key = fields.get('todo_id') or fields.get('title') or ''
    doc, err = _find_line_todo(todo_key, target_id=target_id)
    if err:
        return {'handled': True, 'ok': False, 'reply_text': err, 'parsed_tag': '清除代辦'}
    data = doc.to_dict() or {}
    doc.reference.delete()
    return {'handled': True, 'ok': True, 'reply_text': f"已清除代辦：{data.get('title', '')}", 'parsed_tag': '清除代辦'}


def process_line_todo_message_event(event):
    message = event.get('message') or {}
    if message.get('type') != 'text':
        return {'handled': False}

    raw_text = (message.get('text') or '').strip()
    if not raw_text.startswith('#'):
        return {'handled': False}

    command_table = [
        ('新增代辦', create_line_todo),
        ('今日代辦', lambda fields, ev: query_line_todos(fields, ev, force_today=True)),
        ('查詢代辦', query_line_todos),
        ('代辦', lambda fields, ev: query_line_todos(fields, ev, force_today=True)),
        ('完成代辦', complete_line_todo),
        ('清除代辦', delete_line_todo),
        ('刪除代辦', delete_line_todo),
    ]

    for tag, handler in command_table:
        if raw_text.startswith('#' + tag):
            fields = _parse_line_todo_fields(raw_text, tag)
            result = handler(fields, event)
            parsed = {'tag': result.get('parsed_tag', tag), 'action': 'line_todo', 'fields': fields, 'raw_text': raw_text}
            save_line_log(parsed, event, 'success' if result.get('ok') else 'failed', note=result.get('reply_text', ''), sender_display_name=get_line_sender_display_name(event))
            return result

    return {'handled': False}


def push_line_text(to_id: str, text_message: str):
    if not LINE_CHANNEL_ACCESS_TOKEN or not to_id:
        return False, 'LINE_CHANNEL_ACCESS_TOKEN 或 to_id 為空'
    payload = {
        'to': to_id,
        'messages': [{'type': 'text', 'text': (text_message or '')[:5000]}],
    }
    try:
        import requests
        res = requests.post(
            'https://api.line.me/v2/bot/message/push',
            headers=line_api_headers(),
            json=payload,
            timeout=8,
        )
        print('LINE push status:', res.status_code, res.text[:300])
        return res.status_code in (200, 202), res.text[:300]
    except Exception as e:
        print('⚠️ LINE push 發生錯誤：', e)
        return False, str(e)


def send_today_line_todo_reminders():
    today = now_taipei().strftime('%Y-%m-%d')
    docs = list(db.collection(LINE_TODO_COLLECTION).where('todo_date', '==', today).stream())
    grouped = {}

    for doc in docs:
        data = doc.to_dict() or {}
        if data.get('status', 'open') != 'open':
            continue
        sent_dates = data.get('reminder_sent_dates') or []
        if today in sent_dates:
            continue
        target_id = data.get('line_target_id', '')
        if not target_id:
            continue
        grouped.setdefault(target_id, []).append(doc)

    sent_count = 0
    failed = []
    for target_id, items in grouped.items():
        text = _format_line_todo_list(items, f'今天 {today} 要做的事情')
        ok, msg = push_line_text(target_id, text)
        if ok:
            sent_count += 1
            for doc in items:
                doc.reference.update({
                    'reminder_sent_dates': firestore.ArrayUnion([today]),
                    'last_reminded_at': now_taipei().isoformat(),
                })
        else:
            failed.append({'target_id': target_id, 'error': msg})

    return {'date': today, 'target_count': len(grouped), 'sent_count': sent_count, 'failed': failed}


@app.route('/line/todos/remind-today', methods=['GET', 'POST'])
def line_todos_remind_today():
    # 建議在 Render / Cron Job 設定環境變數 TODO_REMINDER_SECRET，並用 ?key=你的密鑰 呼叫。
    secret = os.environ.get('TODO_REMINDER_SECRET', '').strip()
    key = request.args.get('key', '').strip() or request.form.get('key', '').strip()
    if secret and key != secret:
        return {'ok': False, 'message': 'Invalid key'}, 403
    result = send_today_line_todo_reminders()
    return {'ok': True, 'result': result}, 200


# 保留原本 LINE 訊息處理流程；代辦指令先攔截，其它指令照舊走原本客需/委託/開發流程。
_process_line_message_event_before_todo = process_line_message_event


def process_line_message_event(event):
    todo_result = process_line_todo_message_event(event)
    if todo_result.get('handled'):
        return todo_result
    return _process_line_message_event_before_todo(event)

# ========= LINE Bot 代辦事項指令 Patch End =========

if __name__ == "__main__":
    app.run(debug=True)

# ========= LINE Bot 代辦事項「多行批次新增 / 用序號完成」強化 Patch v2 =========
# 貼在原本代辦事項 Patch 的最底部即可。
# 支援格式：
# #新增代辦
# 日期: 明天
# 厝米排版 土地現廣稿
# 拍水哥爸爸土地
# 遠6屋主
# 下午要去找刺青大哥
#
# 也支援：#完成代辦 1 2 3  或  #完成代辦\n1\n3


def _clean_todo_item_line(line: str) -> str:
    """把清單符號、序號拿掉，保留真正的代辦文字。"""
    s = (line or '').strip()
    s = re.sub(r'^[-*•●▪▫□☐✅✔]+\s*', '', s)
    s = re.sub(r'^\d+[\.\)）、．]\s*', '', s)
    return s.strip()


def _looks_like_date_text(text: str) -> bool:
    raw = (text or '').strip()
    if raw in ('今天', '今日', '明天', '明日', '後天'):
        return True
    if re.fullmatch(r'\d{4}[/-]\d{1,2}[/-]\d{1,2}', raw):
        return True
    if re.fullmatch(r'\d{1,2}[/-]\d{1,2}', raw):
        return True
    return False


def _parse_line_todo_bulk_fields(raw_text: str, tag: str = '新增代辦'):
    """
    批次新增解析：
    - 日期只要寫一次，下面每一行都會變成一筆代辦
    - 沒寫日期就預設今天
    - 仍相容舊格式「事項: xxx」
    """
    lines = [ln.strip() for ln in (raw_text or '').splitlines() if ln.strip()]
    if not lines:
        return []

    first = lines[0]
    inline = first.replace('#' + tag, '', 1).strip() if first.startswith('#' + tag) else ''

    default_date = now_taipei().strftime('%Y-%m-%d')
    default_note = ''
    items = []

    # 例如：#新增代辦 明天
    # 或：#新增代辦 明天 打給王小姐
    if inline:
        if _looks_like_date_text(inline):
            parsed_date = _parse_line_todo_date(inline)
            if parsed_date:
                default_date = parsed_date
        else:
            inline_date, inline_title = _split_inline_todo_text(inline)
            if inline_date and inline_title:
                default_date = inline_date
                items.append(inline_title)
            else:
                items.append(inline)

    key_map = {
        '日期': 'todo_date_raw',
        '時間': 'todo_date_raw',
        '期限': 'todo_date_raw',
        '提醒日期': 'todo_date_raw',
        '事項': 'title',
        '代辦': 'title',
        '內容': 'title',
        '工作': 'title',
        '備註': 'note',
        '說明': 'note',
    }

    for raw_line in lines[1:]:
        line = raw_line.strip()
        if not line:
            continue

        m = re.match(r'^([^:：]+)\s*[:：]\s*(.*)$', line)
        if m:
            key = key_map.get(m.group(1).strip(), m.group(1).strip())
            value = (m.group(2) or '').strip()
            if key == 'todo_date_raw':
                parsed_date = _parse_line_todo_date(value)
                if parsed_date:
                    default_date = parsed_date
            elif key == 'note':
                default_note = value
            elif key == 'title':
                title = _clean_todo_item_line(value)
                if title:
                    items.append(title)
            continue

        cleaned = _clean_todo_item_line(line)
        if not cleaned:
            continue

        # 允許這種格式：
        # #新增代辦
        # 明天
        # 打給王小姐
        if not items and _looks_like_date_text(cleaned):
            parsed_date = _parse_line_todo_date(cleaned)
            if parsed_date:
                default_date = parsed_date
                continue

        items.append(cleaned)

    # 去重但保留順序，避免同一行不小心貼兩次
    items = dedupe_keep_order(items)

    return [
        {
            'title': title,
            'todo_date': default_date,
            'note': default_note,
        }
        for title in items
        if title
    ]


def create_line_todos_bulk(fields_list, event):
    if not fields_list:
        return {
            'handled': True,
            'ok': False,
            'reply_text': (
                '未新增：請在 #新增代辦 下一行開始貼代辦事項。\n\n'
                '範例：\n'
                '#新增代辦\n'
                '日期: 明天\n'
                '厝米排版 土地現廣稿\n'
                '拍水哥爸爸土地\n'
                '遠6屋主\n'
                '下午要去找刺青大哥'
            ),
            'parsed_tag': '新增代辦',
        }

    ok_titles = []
    failed_msgs = []

    for fields in fields_list:
        result = create_line_todo(fields, event)
        if result.get('ok'):
            ok_titles.append((fields.get('todo_date', ''), fields.get('title', '')))
        else:
            failed_msgs.append(result.get('reply_text', '新增失敗'))

    if ok_titles:
        # 多數情況同一天，先拿第一筆日期當摘要
        date_label = ok_titles[0][0]
        lines = [f'已新增 {len(ok_titles)} 筆代辦', f'日期：{date_label}', '']
        for idx, (_, title) in enumerate(ok_titles, 1):
            lines.append(f'{idx}. {title}')
        lines.append('')
        lines.append('查詢請回：#今日代辦')
        lines.append('完成可回：#完成代辦 1')
        reply = '\n'.join(lines)
        if failed_msgs:
            reply += '\n\n有部分未新增：\n' + '\n'.join(failed_msgs[:3])
        return {'handled': True, 'ok': True, 'reply_text': reply[:5000], 'parsed_tag': '新增代辦'}

    return {'handled': True, 'ok': False, 'reply_text': '\n'.join(failed_msgs)[:5000], 'parsed_tag': '新增代辦'}


def _extract_todo_keys_from_command(raw_text: str, tag: str):
    """支援 #完成代辦 1 2 3、ID: xxx、或換行多個序號。"""
    lines = [ln.strip() for ln in (raw_text or '').splitlines() if ln.strip()]
    if not lines:
        return []

    first = lines[0]
    inline = first.replace('#' + tag, '', 1).strip() if first.startswith('#' + tag) else ''
    chunks = []
    if inline:
        chunks.append(inline)

    for line in lines[1:]:
        m = re.match(r'^([^:：]+)\s*[:：]\s*(.*)$', line)
        if m:
            key = m.group(1).strip()
            value = (m.group(2) or '').strip()
            if key in ('ID', 'id', '編號', '代辦ID', '事項', '代辦') and value:
                chunks.append(value)
            # 日期只用來判斷序號查哪一天，不當成 key
        else:
            chunks.append(line)

    keys = []
    for chunk in chunks:
        for part in re.split(r'[\s,，、]+', chunk):
            part = part.strip()
            if part:
                keys.append(part)
    return dedupe_keep_order(keys)


def _find_line_todo_v2(todo_key: str, target_id='', todo_date=''):
    key = (todo_key or '').strip()
    if not key:
        return None, '請提供代辦序號、ID 或事項關鍵字。'

    # 讓使用者可以直接用 #今日代辦 的序號完成，例如：#完成代辦 2
    if re.fullmatch(r'\d+', key):
        items = _get_open_line_todos(
            todo_date=todo_date or now_taipei().strftime('%Y-%m-%d'),
            target_id=target_id,
        )
        idx = int(key) - 1
        if 0 <= idx < len(items):
            return items[idx], ''
        return None, f'找不到第 {key} 筆代辦，請先輸入 #今日代辦 確認序號。'

    return _find_line_todo(key, target_id=target_id)


def complete_line_todos_bulk(fields, event, raw_text='', delete_mode=False):
    target_id, _ = _line_todo_target_from_event(event)
    tag = '清除代辦' if delete_mode else '完成代辦'
    todo_date = fields.get('todo_date') or fields.get('todo_date_raw') or now_taipei().strftime('%Y-%m-%d')
    todo_date = _parse_line_todo_date(todo_date) or now_taipei().strftime('%Y-%m-%d')

    keys = _extract_todo_keys_from_command(raw_text, tag)
    if not keys:
        fallback = fields.get('todo_id') or fields.get('title') or ''
        if fallback:
            keys = [fallback]

    if not keys:
        return {
            'handled': True,
            'ok': False,
            'reply_text': '請提供要完成的序號或 ID。\n例如：#完成代辦 1\n或：#完成代辦 1 3',
            'parsed_tag': tag,
        }

    done_titles = []
    errors = []

    for key in keys:
        doc, err = _find_line_todo_v2(key, target_id=target_id, todo_date=todo_date)
        if err:
            errors.append(f'{key}：{err}')
            continue

        data = doc.to_dict() or {}
        title = data.get('title', '')
        if delete_mode:
            doc.reference.delete()
            done_titles.append(f'已清除：{title}')
        else:
            doc.reference.update({
                'status': 'done',
                'done_at': now_taipei().isoformat(),
                'done_by_id': 'line_bot',
                'done_by_name': get_line_sender_display_name(event) or 'LINE Bot',
                'updated_at': now_taipei().isoformat(),
            })
            done_titles.append(f'已完成：{title}')

    lines = []
    if done_titles:
        lines.extend(done_titles)
    if errors:
        if lines:
            lines.append('')
        lines.append('未處理：')
        lines.extend(errors[:8])

    return {
        'handled': True,
        'ok': bool(done_titles),
        'reply_text': '\n'.join(lines)[:5000] if lines else '沒有完成任何代辦。',
        'parsed_tag': tag,
    }


# 覆寫代辦指令處理：新增代辦改成可批次，多筆完成/清除也可用序號。
def process_line_todo_message_event(event):
    message = event.get('message') or {}
    if message.get('type') != 'text':
        return {'handled': False}

    raw_text = (message.get('text') or '').strip()
    if not raw_text.startswith('#'):
        return {'handled': False}

    if raw_text.startswith('#新增代辦'):
        fields_list = _parse_line_todo_bulk_fields(raw_text, '新增代辦')
        result = create_line_todos_bulk(fields_list, event)
        parsed = {'tag': result.get('parsed_tag', '新增代辦'), 'action': 'line_todo_bulk_create', 'fields': {'items': fields_list}, 'raw_text': raw_text}
        save_line_log(parsed, event, 'success' if result.get('ok') else 'failed', note=result.get('reply_text', ''), sender_display_name=get_line_sender_display_name(event))
        return result

    if raw_text.startswith('#完成代辦'):
        fields = _parse_line_todo_fields(raw_text, '完成代辦')
        result = complete_line_todos_bulk(fields, event, raw_text=raw_text, delete_mode=False)
        parsed = {'tag': result.get('parsed_tag', '完成代辦'), 'action': 'line_todo_bulk_done', 'fields': fields, 'raw_text': raw_text}
        save_line_log(parsed, event, 'success' if result.get('ok') else 'failed', note=result.get('reply_text', ''), sender_display_name=get_line_sender_display_name(event))
        return result

    if raw_text.startswith('#清除代辦') or raw_text.startswith('#刪除代辦'):
        tag = '清除代辦' if raw_text.startswith('#清除代辦') else '刪除代辦'
        fields = _parse_line_todo_fields(raw_text, tag)
        result = complete_line_todos_bulk(fields, event, raw_text=raw_text, delete_mode=True)
        parsed = {'tag': result.get('parsed_tag', tag), 'action': 'line_todo_bulk_delete', 'fields': fields, 'raw_text': raw_text}
        save_line_log(parsed, event, 'success' if result.get('ok') else 'failed', note=result.get('reply_text', ''), sender_display_name=get_line_sender_display_name(event))
        return result

    command_table = [
        ('今日代辦', lambda fields, ev: query_line_todos(fields, ev, force_today=True)),
        ('查詢代辦', query_line_todos),
        ('代辦', lambda fields, ev: query_line_todos(fields, ev, force_today=True)),
    ]

    for tag, handler in command_table:
        if raw_text.startswith('#' + tag):
            fields = _parse_line_todo_fields(raw_text, tag)
            result = handler(fields, event)
            parsed = {'tag': result.get('parsed_tag', tag), 'action': 'line_todo_query', 'fields': fields, 'raw_text': raw_text}
            save_line_log(parsed, event, 'success' if result.get('ok') else 'failed', note=result.get('reply_text', ''), sender_display_name=get_line_sender_display_name(event))
            return result

    return {'handled': False}

# ========= LINE Bot 代辦事項強化 Patch v2 End =========

# ========= LINE Bot 代辦事項「逾期尚未完成 / 隔天延續提醒」強化 Patch v3 =========
# 貼在 v2 代辦事項 Patch 的最底部即可。
# 功能：
# 1. 今天以前未完成的代辦，隔天會出現在「尚未完成」區塊。
# 2. #今日代辦 會顯示：尚未完成（以前）＋ 今天要做。
# 3. 每日提醒也會推送：尚未完成（以前）＋ 今天要做。
# 4. #完成代辦 1 會依照畫面上的序號完成，包含尚未完成區塊。


def _todo_date_value(doc):
    data = doc.to_dict() or {}
    return (data.get('todo_date') or '').strip()


def _is_open_todo_doc(doc, target_id=''):
    data = doc.to_dict() or {}
    if data.get('status', 'open') != 'open':
        return False
    if target_id and data.get('line_target_id') != target_id:
        return False
    if not (data.get('todo_date') or '').strip():
        return False
    return True


def _sort_line_todo_docs(items):
    return sorted(
        items,
        key=lambda d: (
            (d.to_dict() or {}).get('todo_date', ''),
            (d.to_dict() or {}).get('created_at', ''),
            d.id,
        )
    )


def _get_open_line_todos(todo_date='', target_id='', include_overdue=False):
    """
    覆寫 v2 的查詢邏輯。
    include_overdue=False：只查指定日期。
    include_overdue=True：查指定日期以前含當天，讓未完成代辦隔天延續顯示。
    """
    query_date = todo_date or now_taipei().strftime('%Y-%m-%d')
    result = []

    # 用 stream + Python 過濾，避免 Firestore 複合索引問題，也能兼容舊資料。
    for doc in db.collection(LINE_TODO_COLLECTION).stream():
        if not _is_open_todo_doc(doc, target_id=target_id):
            continue
        d = _todo_date_value(doc)
        if include_overdue:
            if d <= query_date:
                result.append(doc)
        else:
            if d == query_date:
                result.append(doc)

    return _sort_line_todo_docs(result)


def _get_overdue_line_todos(todo_date='', target_id=''):
    query_date = todo_date or now_taipei().strftime('%Y-%m-%d')
    result = []
    for doc in db.collection(LINE_TODO_COLLECTION).stream():
        if not _is_open_todo_doc(doc, target_id=target_id):
            continue
        d = _todo_date_value(doc)
        if d < query_date:
            result.append(doc)
    return _sort_line_todo_docs(result)


def _get_display_line_todos(todo_date='', target_id=''):
    """回傳畫面顯示順序：逾期未完成在前，指定日期在後。"""
    query_date = todo_date or now_taipei().strftime('%Y-%m-%d')
    overdue_items = _get_overdue_line_todos(query_date, target_id=target_id)
    today_items = _get_open_line_todos(query_date, target_id=target_id, include_overdue=False)
    return overdue_items + today_items


def _format_line_todo_sections(overdue_items, today_items, title, today_label='今天'):
    if not overdue_items and not today_items:
        return f'{title}\n目前沒有未完成代辦。'

    lines = [title]
    idx = 1

    if overdue_items:
        lines.append('')
        lines.append('【尚未完成】')
        for doc in overdue_items:
            data = doc.to_dict() or {}
            short_id = doc.id[:6]
            note = (data.get('note') or '').strip()
            old_date = (data.get('todo_date') or '').strip()
            lines.append(f"{idx}. [{short_id}] {old_date}｜{data.get('title', '')}")
            if note:
                lines.append(f'   備註: {note}')
            idx += 1

    if today_items:
        lines.append('')
        lines.append(f'【{today_label}要做】')
        for doc in today_items:
            data = doc.to_dict() or {}
            short_id = doc.id[:6]
            note = (data.get('note') or '').strip()
            lines.append(f"{idx}. [{short_id}] {data.get('title', '')}")
            if note:
                lines.append(f'   備註: {note}')
            idx += 1

    lines.append('')
    lines.append('完成請回：#完成代辦 1')
    lines.append('一次完成多筆：#完成代辦 1 3')
    lines.append('清除請回：#清除代辦 1')
    return '\n'.join(lines)[:5000]


def query_line_todos(fields, event, force_today=False):
    target_id, _ = _line_todo_target_from_event(event)
    todo_date = now_taipei().strftime('%Y-%m-%d') if force_today else _parse_line_todo_date(fields.get('todo_date') or fields.get('todo_date_raw') or '')
    if not todo_date:
        todo_date = now_taipei().strftime('%Y-%m-%d')

    overdue_items = _get_overdue_line_todos(todo_date=todo_date, target_id=target_id)
    today_items = _get_open_line_todos(todo_date=todo_date, target_id=target_id, include_overdue=False)

    if todo_date == now_taipei().strftime('%Y-%m-%d'):
        title = f'{todo_date} 代辦清單'
        today_label = '今天'
    else:
        title = f'{todo_date} 代辦清單'
        today_label = todo_date

    return {
        'handled': True,
        'ok': True,
        'reply_text': _format_line_todo_sections(overdue_items, today_items, title, today_label=today_label),
        'parsed_tag': '查詢代辦',
    }


def _find_line_todo_v2(todo_key: str, target_id='', todo_date=''):
    """
    覆寫 v2 的序號完成邏輯。
    序號會依照 #今日代辦 畫面順序：尚未完成在前，今天要做在後。
    """
    key = (todo_key or '').strip()
    if not key:
        return None, '請提供代辦序號、ID 或事項關鍵字。'

    query_date = _parse_line_todo_date(todo_date or '') or now_taipei().strftime('%Y-%m-%d')

    if re.fullmatch(r'\d+', key):
        items = _get_display_line_todos(todo_date=query_date, target_id=target_id)
        idx = int(key) - 1
        if 0 <= idx < len(items):
            return items[idx], ''
        return None, f'找不到第 {key} 筆代辦，請先輸入 #今日代辦 確認序號。'

    return _find_line_todo(key, target_id=target_id)


def send_today_line_todo_reminders():
    """
    覆寫 v2 的每日提醒。
    今天提醒會包含：
    - 今天以前尚未完成的代辦
    - 今天要做的代辦
    每天只會對同一筆代辦提醒一次；隔天還沒完成，會再提醒一次。
    """
    today = now_taipei().strftime('%Y-%m-%d')
    grouped = {}

    for doc in db.collection(LINE_TODO_COLLECTION).stream():
        data = doc.to_dict() or {}
        if data.get('status', 'open') != 'open':
            continue
        todo_date = (data.get('todo_date') or '').strip()
        if not todo_date or todo_date > today:
            continue
        sent_dates = data.get('reminder_sent_dates') or []
        if today in sent_dates:
            continue
        target_id = data.get('line_target_id', '')
        if not target_id:
            continue
        grouped.setdefault(target_id, []).append(doc)

    sent_count = 0
    failed = []

    for target_id, items in grouped.items():
        overdue_items = _sort_line_todo_docs([d for d in items if _todo_date_value(d) < today])
        today_items = _sort_line_todo_docs([d for d in items if _todo_date_value(d) == today])
        text = _format_line_todo_sections(
            overdue_items,
            today_items,
            f'今日代辦提醒 {today}',
            today_label='今天',
        )
        ok, msg = push_line_text(target_id, text)
        if ok:
            sent_count += 1
            for doc in items:
                doc.reference.update({
                    'reminder_sent_dates': firestore.ArrayUnion([today]),
                    'last_reminded_at': now_taipei().isoformat(),
                })
        else:
            failed.append({'target_id': target_id, 'error': msg})

    return {'date': today, 'target_count': len(grouped), 'sent_count': sent_count, 'failed': failed}

# ========= LINE Bot 代辦事項強化 Patch v3 End =========



# ========= LINE Bot 代辦事項顯示日期 Patch v4：只顯示月/日 =========
# 貼在 v3 代辦事項 Patch 的最底部即可。
# 注意：Firestore 裡的 todo_date 仍然保留 YYYY-MM-DD，方便判斷逾期；只有 LINE 顯示文字改成 M/D。


def _todo_display_md(date_text: str) -> str:
    """把 YYYY-MM-DD / YYYY/MM/DD / MM-DD 轉成 M/D 顯示；解析失敗就回傳原文字。"""
    raw = (date_text or '').strip()
    if not raw:
        return ''

    # YYYY-MM-DD 或 YYYY/MM/DD -> M/D
    m = re.search(r'^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$', raw)
    if m:
        _, mo, d = m.groups()
        return f'{int(mo)}/{int(d)}'

    # MM-DD 或 MM/DD -> M/D
    m = re.search(r'^(\d{1,2})[/-](\d{1,2})$', raw)
    if m:
        mo, d = m.groups()
        return f'{int(mo)}/{int(d)}'

    return raw


def create_line_todo(fields, event):
    """覆寫新增回覆：日期顯示改成 M/D，但資料庫仍存 YYYY-MM-DD。"""
    title = (fields.get('title') or '').strip()
    todo_date = _parse_line_todo_date(fields.get('todo_date') or fields.get('todo_date_raw') or '')
    note = (fields.get('note') or '').strip()

    if not title:
        return {'handled': True, 'ok': False, 'reply_text': '未新增：請填「事項」。\n\n範例：\n#新增代辦\n日期: 明天\n事項: 打給王小姐確認貸款資料'}
    if not todo_date:
        return {'handled': True, 'ok': False, 'reply_text': '未新增：日期格式看不懂，請用 5/29、今天、明天。'}

    target_id, target_type = _line_todo_target_from_event(event)
    source = event.get('source') or {}
    sender_display_name = get_line_sender_display_name(event)
    now = now_taipei().isoformat()

    doc_ref = db.collection(LINE_TODO_COLLECTION).document()
    doc_ref.set({
        'title': title,
        'todo_date': todo_date,  # 資料庫維持完整日期
        'note': note,
        'status': 'open',
        'line_target_id': target_id,
        'line_target_type': target_type,
        'line_group_id': source.get('groupId', ''),
        'line_room_id': source.get('roomId', ''),
        'line_user_id': source.get('userId', ''),
        'sender_display_name': sender_display_name,
        'created_at': now,
        'created_by_id': 'line_bot',
        'created_by_name': sender_display_name or 'LINE Bot',
        'reminder_sent_dates': [],
    })

    return {
        'handled': True,
        'ok': True,
        'reply_text': f"已新增代辦：{title}\n日期：{_todo_display_md(todo_date)}\nID：{doc_ref.id[:6]}\n\n當天提醒清單會包含這筆代辦。",
        'parsed_tag': '新增代辦',
    }


def create_line_todos_bulk(fields_list, event):
    """覆寫批次新增摘要：日期顯示改成 M/D。"""
    if not fields_list:
        return {
            'handled': True,
            'ok': False,
            'reply_text': (
                '未新增：請在 #新增代辦 下一行開始貼代辦事項。\n\n'
                '範例：\n'
                '#新增代辦\n'
                '日期: 明天\n'
                '厝米排版 土地現廣稿\n'
                '拍水哥爸爸土地\n'
                '遠6屋主\n'
                '下午要去找刺青大哥'
            ),
            'parsed_tag': '新增代辦',
        }

    ok_titles = []
    failed_msgs = []

    for fields in fields_list:
        result = create_line_todo(fields, event)
        if result.get('ok'):
            ok_titles.append((fields.get('todo_date', ''), fields.get('title', '')))
        else:
            failed_msgs.append(result.get('reply_text', '新增失敗'))

    if ok_titles:
        date_label = _todo_display_md(ok_titles[0][0])
        lines = [f'已新增 {len(ok_titles)} 筆代辦', f'日期：{date_label}', '']
        for idx, (_, title) in enumerate(ok_titles, 1):
            lines.append(f'{idx}. {title}')
        lines.append('')
        lines.append('查詢請回：#今日代辦')
        lines.append('完成可回：#完成代辦 1')
        reply = '\n'.join(lines)
        if failed_msgs:
            reply += '\n\n有部分未新增：\n' + '\n'.join(failed_msgs[:3])
        return {'handled': True, 'ok': True, 'reply_text': reply[:5000], 'parsed_tag': '新增代辦'}

    return {'handled': True, 'ok': False, 'reply_text': '\n'.join(failed_msgs)[:5000], 'parsed_tag': '新增代辦'}


def _format_line_todo_sections(overdue_items, today_items, title, today_label='今天'):
    """覆寫清單顯示：日期只顯示 M/D。"""
    if not overdue_items and not today_items:
        return f'{title}\n目前沒有未完成代辦。'

    lines = [title]
    idx = 1

    if overdue_items:
        lines.append('')
        lines.append('【尚未完成】')
        for doc in overdue_items:
            data = doc.to_dict() or {}
            short_id = doc.id[:6]
            note = (data.get('note') or '').strip()
            old_date = _todo_display_md((data.get('todo_date') or '').strip())
            lines.append(f"{idx}. [{short_id}] {old_date}｜{data.get('title', '')}")
            if note:
                lines.append(f'   備註: {note}')
            idx += 1

    if today_items:
        lines.append('')
        lines.append(f'【{today_label}要做】')
        for doc in today_items:
            data = doc.to_dict() or {}
            short_id = doc.id[:6]
            note = (data.get('note') or '').strip()
            lines.append(f"{idx}. [{short_id}] {data.get('title', '')}")
            if note:
                lines.append(f'   備註: {note}')
            idx += 1

    lines.append('')
    lines.append('完成請回：#完成代辦 1')
    lines.append('一次完成多筆：#完成代辦 1 3')
    lines.append('清除請回：#清除代辦 1')
    return '\n'.join(lines)[:5000]


def _format_line_todo_list(items, title):
    """保險覆寫舊版清單格式。"""
    if not items:
        return f'{title}\n目前沒有未完成代辦。'

    lines = [title]
    for idx, item in enumerate(items, 1):
        data = item.to_dict() or {}
        short_id = item.id[:6]
        note = (data.get('note') or '').strip()
        line = f"{idx}. [{short_id}] {data.get('title', '')}"
        if note:
            line += f"\n   備註: {note}"
        lines.append(line)
    lines.append('')
    lines.append('完成請回：#完成代辦 1')
    return '\n'.join(lines)[:5000]


def query_line_todos(fields, event, force_today=False):
    """覆寫查詢標題：日期顯示改成 M/D。"""
    target_id, _ = _line_todo_target_from_event(event)
    todo_date = now_taipei().strftime('%Y-%m-%d') if force_today else _parse_line_todo_date(fields.get('todo_date') or fields.get('todo_date_raw') or '')
    if not todo_date:
        todo_date = now_taipei().strftime('%Y-%m-%d')

    overdue_items = _get_overdue_line_todos(todo_date=todo_date, target_id=target_id)
    today_items = _get_open_line_todos(todo_date=todo_date, target_id=target_id, include_overdue=False)

    display_date = _todo_display_md(todo_date)
    if todo_date == now_taipei().strftime('%Y-%m-%d'):
        title = f'{display_date} 代辦清單'
        today_label = '今天'
    else:
        title = f'{display_date} 代辦清單'
        today_label = display_date

    return {
        'handled': True,
        'ok': True,
        'reply_text': _format_line_todo_sections(overdue_items, today_items, title, today_label=today_label),
        'parsed_tag': '查詢代辦',
    }


def send_today_line_todo_reminders():
    """覆寫每日提醒標題：日期顯示改成 M/D，但提醒判斷仍使用 YYYY-MM-DD。"""
    today = now_taipei().strftime('%Y-%m-%d')
    grouped = {}

    for doc in db.collection(LINE_TODO_COLLECTION).stream():
        data = doc.to_dict() or {}
        if data.get('status', 'open') != 'open':
            continue
        todo_date = (data.get('todo_date') or '').strip()
        if not todo_date or todo_date > today:
            continue
        sent_dates = data.get('reminder_sent_dates') or []
        if today in sent_dates:
            continue
        target_id = data.get('line_target_id', '')
        if not target_id:
            continue
        grouped.setdefault(target_id, []).append(doc)

    sent_count = 0
    failed = []

    for target_id, items in grouped.items():
        overdue_items = _sort_line_todo_docs([d for d in items if _todo_date_value(d) < today])
        today_items = _sort_line_todo_docs([d for d in items if _todo_date_value(d) == today])
        text = _format_line_todo_sections(
            overdue_items,
            today_items,
            f'今日代辦提醒 {_todo_display_md(today)}',
            today_label='今天',
        )
        ok, msg = push_line_text(target_id, text)
        if ok:
            sent_count += 1
            for doc in items:
                doc.reference.update({
                    'reminder_sent_dates': firestore.ArrayUnion([today]),
                    'last_reminded_at': now_taipei().isoformat(),
                })
        else:
            failed.append({'target_id': target_id, 'error': msg})

    return {'date': today, 'target_count': len(grouped), 'sent_count': sent_count, 'failed': failed}

# ========= LINE Bot 代辦事項顯示日期 Patch v4 End =========

# ========= LINE Bot 代辦事項 Patch v5：隱藏 ID / 多日期摘要 / 明天與今天雙提醒 =========
# 貼在 v4 代辦事項 Patch 的最底部即可。
# 功能：
# 1. LINE 清單不顯示 Firestore 文件 ID，完成/清除一律用畫面序號。
# 2. 新增代辦回覆會顯示日期；若同批有不同日期，會依日期分組顯示。
# 3. 支援每一行前面直接寫日期，例如：5/29 厝米排版、明天 拍水哥爸爸土地。
# 4. 新增 /line/todos/remind-tomorrow，用於每天晚上 23:00 推播「明天代辦」。
# 5. 原本 /line/todos/remind-today 用於每天早上 08:00 推播「今日代辦＋尚未完成」。


def _split_todo_line_date_prefix(line: str):
    """
    支援單行指定日期：
    - 5/29 厝米排版
    - 5/29｜厝米排版
    - 2026-05-29 厝米排版
    - 明天 拍水哥爸爸土地
    - 後天：找刺青大哥
    回傳：(YYYY-MM-DD 或 '', 事項文字)
    """
    s = _clean_todo_item_line(line or '')
    if not s:
        return '', ''

    # YYYY-MM-DD / YYYY/MM/DD 開頭
    m = re.match(r'^(\d{4}[/-]\d{1,2}[/-]\d{1,2})\s*[｜|:：\-—、,，]?\s*(.+)$', s)
    if m:
        d = _parse_line_todo_date(m.group(1))
        title = _clean_todo_item_line(m.group(2))
        return d, title

    # M/D 或 M-D 開頭
    m = re.match(r'^(\d{1,2}[/-]\d{1,2})\s*[｜|:：\-—、,，]?\s*(.+)$', s)
    if m:
        d = _parse_line_todo_date(m.group(1))
        title = _clean_todo_item_line(m.group(2))
        return d, title

    # 今天 / 明天 / 後天 開頭
    m = re.match(r'^(今天|今日|明天|明日|後天)\s*[｜|:：\-—、,，]?\s*(.+)$', s)
    if m:
        d = _parse_line_todo_date(m.group(1))
        title = _clean_todo_item_line(m.group(2))
        return d, title

    return '', s


def _parse_line_todo_bulk_fields(raw_text: str, tag: str = '新增代辦'):
    """
    覆寫批次新增解析 v5：
    - 日期: 5/29 會作為下面代辦的預設日期。
    - 每一行也能自己帶日期，例如「5/30 拍水哥爸爸土地」。
    - 沒寫日期仍預設今天。
    """
    lines = [ln.strip() for ln in (raw_text or '').splitlines() if ln.strip()]
    if not lines:
        return []

    first = lines[0]
    inline = first.replace('#' + tag, '', 1).strip() if first.startswith('#' + tag) else ''

    default_date = now_taipei().strftime('%Y-%m-%d')
    default_note = ''
    default_remind_time = ''
    items = []

    # 例如：#新增代辦 明天
    # 或：#新增代辦 明天 打給王小姐
    if inline:
        if _looks_like_date_text(inline):
            parsed_date = _parse_line_todo_date(inline)
            if parsed_date:
                default_date = parsed_date
        else:
            inline_date, inline_title = _split_todo_line_date_prefix(inline)
            if inline_date and inline_title:
                items.append({'title': inline_title, 'todo_date': inline_date, 'note': default_note, 'remind_time': default_remind_time})
            elif inline_title:
                items.append({'title': inline_title, 'todo_date': default_date, 'note': default_note, 'remind_time': default_remind_time})

    key_map = {
        '日期': 'todo_date_raw',
        '時間': 'todo_date_raw',
        '期限': 'todo_date_raw',
        '提醒日期': 'todo_date_raw',
        '提醒時間': 'remind_time',
        '代辦時間': 'remind_time',
        '事項': 'title',
        '代辦': 'title',
        '內容': 'title',
        '工作': 'title',
        '備註': 'note',
        '說明': 'note',
    }

    for raw_line in lines[1:]:
        line = raw_line.strip()
        if not line:
            continue

        m = re.match(r'^([^:：]+)\s*[:：]\s*(.*)$', line)
        if m:
            key = key_map.get(m.group(1).strip(), m.group(1).strip())
            value = (m.group(2) or '').strip()
            if key == 'todo_date_raw':
                parsed_date = _parse_line_todo_date(value)
                if parsed_date:
                    default_date = parsed_date
            elif key == 'remind_time':
                default_remind_time = value
            elif key == 'note':
                default_note = value
            elif key == 'title':
                line_date, title = _split_todo_line_date_prefix(value)
                if title:
                    items.append({
                        'title': title,
                        'todo_date': line_date or default_date,
                        'note': default_note,
                        'remind_time': default_remind_time,
                    })
            continue

        cleaned = _clean_todo_item_line(line)
        if not cleaned:
            continue

        # 允許中途切換日期：
        # 5/29
        # A事項
        # 5/30
        # B事項
        if _looks_like_date_text(cleaned):
            parsed_date = _parse_line_todo_date(cleaned)
            if parsed_date:
                default_date = parsed_date
                continue

        line_date, title = _split_todo_line_date_prefix(cleaned)
        if title:
            items.append({
                'title': title,
                'todo_date': line_date or default_date,
                'note': default_note,
                'remind_time': default_remind_time,
            })

    deduped = []
    seen = set()
    for item in items:
        key = (item.get('todo_date', ''), item.get('title', ''), item.get('note', ''))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def create_line_todo(fields, event):
    """覆寫新增：不回傳 ID；仍把完整日期存入 Firestore。"""
    title = (fields.get('title') or '').strip()
    todo_date = _parse_line_todo_date(fields.get('todo_date') or fields.get('todo_date_raw') or '')
    note = (fields.get('note') or '').strip()
    remind_time = (fields.get('remind_time') or '').strip()

    if not title:
        return {'handled': True, 'ok': False, 'reply_text': '未新增：請填「事項」。\n\n範例：\n#新增代辦\n日期: 明天\n厝米排版 土地現廣稿\n拍水哥爸爸土地'}
    if not todo_date:
        return {'handled': True, 'ok': False, 'reply_text': '未新增：日期格式看不懂，請用 5/29、今天、明天。'}

    target_id, target_type = _line_todo_target_from_event(event)
    source = event.get('source') or {}
    sender_display_name = get_line_sender_display_name(event)
    now = now_taipei().isoformat()

    doc_ref = db.collection(LINE_TODO_COLLECTION).document()
    doc_ref.set({
        'title': title,
        'todo_date': todo_date,
        'note': note,
        'remind_time': remind_time,
        'status': 'open',
        'line_target_id': target_id,
        'line_target_type': target_type,
        'line_group_id': source.get('groupId', ''),
        'line_room_id': source.get('roomId', ''),
        'line_user_id': source.get('userId', ''),
        'sender_display_name': sender_display_name,
        'created_at': now,
        'created_by_id': 'line_bot',
        'created_by_name': sender_display_name or 'LINE Bot',
        'reminder_sent_dates': [],
        'tomorrow_reminder_sent_dates': [],
    })

    return {
        'handled': True,
        'ok': True,
        'reply_text': f"已新增代辦\n日期：{_todo_display_md(todo_date)}\n事項：{title}\n\n查詢請回：#今日代辦",
        'parsed_tag': '新增代辦',
    }


def create_line_todos_bulk(fields_list, event):
    """覆寫批次新增摘要：不顯示 ID；同批不同日期時自動分組。"""
    if not fields_list:
        return {
            'handled': True,
            'ok': False,
            'reply_text': (
                '未新增：請在 #新增代辦 下一行開始貼代辦事項。\n\n'
                '範例一：同一天\n'
                '#新增代辦\n'
                '日期: 明天\n'
                '厝米排版 土地現廣稿\n'
                '拍水哥爸爸土地\n\n'
                '範例二：不同天\n'
                '#新增代辦\n'
                '5/29 厝米排版 土地現廣稿\n'
                '5/30 拍水哥爸爸土地'
            ),
            'parsed_tag': '新增代辦',
        }

    ok_items = []
    failed_msgs = []

    for fields in fields_list:
        result = create_line_todo(fields, event)
        if result.get('ok'):
            ok_items.append({
                'todo_date': fields.get('todo_date', ''),
                'title': fields.get('title', ''),
            })
        else:
            failed_msgs.append(result.get('reply_text', '新增失敗'))

    if ok_items:
        grouped = {}
        for item in ok_items:
            d = item.get('todo_date', '') or now_taipei().strftime('%Y-%m-%d')
            grouped.setdefault(d, []).append(item.get('title', ''))

        lines = [f'已新增 {len(ok_items)} 筆代辦']
        if len(grouped) == 1:
            only_date = next(iter(grouped.keys()))
            lines.append(f'日期：{_todo_display_md(only_date)}')
            lines.append('')
            for idx, title in enumerate(grouped[only_date], 1):
                lines.append(f'{idx}. {title}')
        else:
            lines.append('')
            running = 1
            for d in sorted(grouped.keys()):
                lines.append(f'【{_todo_display_md(d)}】')
                for title in grouped[d]:
                    lines.append(f'{running}. {title}')
                    running += 1
                lines.append('')
            if lines and lines[-1] == '':
                lines.pop()

        lines.append('')
        lines.append('查詢請回：#今日代辦')
        lines.append('完成可回：#完成代辦 1')
        reply = '\n'.join(lines)
        if failed_msgs:
            reply += '\n\n有部分未新增：\n' + '\n'.join(failed_msgs[:3])
        return {'handled': True, 'ok': True, 'reply_text': reply[:5000], 'parsed_tag': '新增代辦'}

    return {'handled': True, 'ok': False, 'reply_text': '\n'.join(failed_msgs)[:5000], 'parsed_tag': '新增代辦'}


def _format_line_todo_sections(overdue_items, today_items, title, today_label='今天'):
    """覆寫清單顯示：完全不顯示 ID，只靠畫面序號完成。"""
    if not overdue_items and not today_items:
        return f'{title}\n目前沒有未完成代辦。'

    lines = [title]
    idx = 1

    if overdue_items:
        lines.append('')
        lines.append('【尚未完成】')
        for doc in overdue_items:
            data = doc.to_dict() or {}
            note = (data.get('note') or '').strip()
            old_date = _todo_display_md((data.get('todo_date') or '').strip())
            lines.append(f"{idx}. {old_date}｜{data.get('title', '')}")
            if note:
                lines.append(f'   備註: {note}')
            idx += 1

    if today_items:
        lines.append('')
        lines.append(f'【{today_label}要做】')
        for doc in today_items:
            data = doc.to_dict() or {}
            note = (data.get('note') or '').strip()
            lines.append(f"{idx}. {data.get('title', '')}")
            if note:
                lines.append(f'   備註: {note}')
            idx += 1

    lines.append('')
    lines.append('完成請回：#完成代辦 1')
    lines.append('一次完成多筆：#完成代辦 1 3')
    lines.append('清除請回：#清除代辦 1')
    return '\n'.join(lines)[:5000]


def _format_line_todo_list(items, title):
    """保險覆寫舊版清單格式：不顯示 ID。"""
    if not items:
        return f'{title}\n目前沒有未完成代辦。'

    lines = [title]
    for idx, item in enumerate(items, 1):
        data = item.to_dict() or {}
        note = (data.get('note') or '').strip()
        line = f"{idx}. {data.get('title', '')}"
        if note:
            line += f"\n   備註: {note}"
        lines.append(line)
    lines.append('')
    lines.append('完成請回：#完成代辦 1')
    return '\n'.join(lines)[:5000]


def _find_line_todo(todo_key: str, target_id=''):
    """覆寫搜尋錯誤訊息：避免提示 ID，優先建議用序號完成。"""
    key = (todo_key or '').strip()
    if not key:
        return None, '請提供代辦序號或事項關鍵字。'

    # 仍保留隱藏能力：若你剛好知道完整文件 ID，程式仍可處理，但 LINE 畫面不顯示。
    direct = db.collection(LINE_TODO_COLLECTION).document(key).get()
    if direct.exists:
        data = direct.to_dict() or {}
        if target_id and data.get('line_target_id') != target_id:
            return None, '這筆代辦不是在目前這個 LINE 對話建立的。'
        return direct, ''

    matches = []
    for doc in db.collection(LINE_TODO_COLLECTION).stream():
        data = doc.to_dict() or {}
        if data.get('status', 'open') != 'open':
            continue
        if target_id and data.get('line_target_id') != target_id:
            continue
        title = data.get('title', '')
        if doc.id.startswith(key) or key in title:
            matches.append(doc)

    if len(matches) == 1:
        return matches[0], ''
    if len(matches) > 1:
        preview = '\n'.join([f"- {(d.to_dict() or {}).get('title','')}" for d in matches[:8]])
        return None, '找到多筆相似代辦，請先輸入 #今日代辦，再用序號完成：\n' + preview
    return None, '找不到這筆未完成代辦，請先輸入 #今日代辦 查看序號。'


def send_tomorrow_line_todo_reminders():
    """
    晚上 23:00 使用：提醒明天要做的代辦。
    注意：這不會影響隔天早上 08:00 的今日提醒，兩者用不同欄位記錄。
    """
    tomorrow_date = (now_taipei().date() + timedelta(days=1)).strftime('%Y-%m-%d')
    grouped = {}

    for doc in db.collection(LINE_TODO_COLLECTION).stream():
        data = doc.to_dict() or {}
        if data.get('status', 'open') != 'open':
            continue
        todo_date = (data.get('todo_date') or '').strip()
        if todo_date != tomorrow_date:
            continue
        sent_dates = data.get('tomorrow_reminder_sent_dates') or []
        if tomorrow_date in sent_dates:
            continue
        target_id = data.get('line_target_id', '')
        if not target_id:
            continue
        grouped.setdefault(target_id, []).append(doc)

    sent_count = 0
    failed = []

    for target_id, items in grouped.items():
        tomorrow_items = _sort_line_todo_docs(items)
        text = _format_line_todo_sections(
            [],
            tomorrow_items,
            f'明天 {_todo_display_md(tomorrow_date)} 要做的事情',
            today_label='明天',
        )
        ok, msg = push_line_text(target_id, text)
        if ok:
            sent_count += 1
            for doc in items:
                doc.reference.update({
                    'tomorrow_reminder_sent_dates': firestore.ArrayUnion([tomorrow_date]),
                    'last_tomorrow_reminded_at': now_taipei().isoformat(),
                })
        else:
            failed.append({'target_id': target_id, 'error': msg})

    return {'date': tomorrow_date, 'target_count': len(grouped), 'sent_count': sent_count, 'failed': failed}


@app.route('/line/todos/remind-tomorrow', methods=['GET', 'POST'])
def line_todos_remind_tomorrow():
    # Render Cron Job / UptimeRobot 建議呼叫：/line/todos/remind-tomorrow?key=你的密鑰
    secret = os.environ.get('TODO_REMINDER_SECRET', '').strip()
    key = request.args.get('key', '').strip() or request.form.get('key', '').strip()
    if secret and key != secret:
        return {'ok': False, 'message': 'Invalid key'}, 403
    result = send_tomorrow_line_todo_reminders()
    return {'ok': True, 'result': result}, 200

# ========= LINE Bot 代辦事項 Patch v5 End =========



# ========= LINE Bot 代辦事項 Patch v6：用 LINE 指令設定提醒時間 =========
# 貼在 v5 代辦事項 Patch 的最底部即可。
# 重點：
# 1. LINE 指令只負責「儲存提醒設定」。
# 2. 真正定時發送由 /line/todos/reminder-check 搭配 Render Cron / UptimeRobot 每 5~10 分鐘呼叫。
# 3. 每個 LINE 對話（群組 / 聊天室 / 個人）可以有自己的提醒時間。

LINE_TODO_SETTINGS_COLLECTION = os.environ.get('LINE_TODO_SETTINGS_COLLECTION', 'line_todo_settings')


LINE_TODO_DEFAULT_REMINDER_SETTINGS = {
    'today_enabled': True,
    'today_reminder_time': '08:00',
    'tomorrow_enabled': True,
    'tomorrow_reminder_time': '23:00',
}


def _line_todo_setting_doc_id(target_id: str) -> str:
    """避免 LINE target_id 內有特殊字元，統一用 sha1 當 Firestore 文件 ID。"""
    raw = (target_id or '').strip()
    if not raw:
        raw = 'unknown'
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()


def _normalize_line_todo_time(value: str):
    """
    支援：
    - 08:00 / 8:00 / 0800
    - 8點 / 8點30 / 8時30分
    - 早上8點 / 上午8點
    - 晚上11點 / 下午11點 -> 23:00
    回傳 HH:MM；看不懂回傳空字串。
    """
    raw = (value or '').strip()
    if not raw:
        return ''

    raw = raw.translate(str.maketrans('０１２３４５６７８９：', '0123456789:'))
    raw = re.sub(r'\s+', '', raw)

    is_pm = any(x in raw for x in ['下午', '晚上', '晚間', '傍晚'])
    is_am = any(x in raw for x in ['上午', '早上', '清晨'])
    raw2 = raw
    for word in ['上午', '早上', '清晨', '下午', '晚上', '晚間', '傍晚', '中午']:
        raw2 = raw2.replace(word, '')

    hour = None
    minute = 0

    m = re.search(r'^(\d{1,2}):(\d{1,2})$', raw2)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
    else:
        m = re.search(r'^(\d{3,4})$', raw2)
        if m:
            digits = m.group(1)
            hour = int(digits[:-2])
            minute = int(digits[-2:])
        else:
            m = re.search(r'(\d{1,2})(?:點|時)(?:(\d{1,2})(?:分)?)?$', raw2)
            if m:
                hour = int(m.group(1))
                minute = int(m.group(2) or 0)
            else:
                m = re.search(r'^(\d{1,2})$', raw2)
                if m:
                    hour = int(m.group(1))
                    minute = 0

    if hour is None:
        return ''

    if is_pm and hour < 12:
        hour += 12
    if is_am and hour == 12:
        hour = 0

    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return ''

    return f'{hour:02d}:{minute:02d}'


def _time_to_minutes(hhmm: str, default='00:00'):
    t = _normalize_line_todo_time(hhmm) or default
    h, m = t.split(':')
    return int(h) * 60 + int(m)


def _get_line_todo_reminder_settings(target_id: str, target_type: str = ''):
    settings = dict(LINE_TODO_DEFAULT_REMINDER_SETTINGS)
    if not target_id:
        return settings

    doc = db.collection(LINE_TODO_SETTINGS_COLLECTION).document(_line_todo_setting_doc_id(target_id)).get()
    if doc.exists:
        data = doc.to_dict() or {}
        settings.update({k: v for k, v in data.items() if v is not None})

    settings['line_target_id'] = target_id
    if target_type:
        settings['line_target_type'] = target_type
    return settings


def _save_line_todo_reminder_settings(target_id: str, target_type: str, updates: dict, event=None):
    if not target_id:
        return False, '找不到目前 LINE 對話 ID，無法設定提醒。'

    now = now_taipei().isoformat()
    payload = {
        'line_target_id': target_id,
        'line_target_type': target_type,
        'updated_at': now,
        'updated_by_id': 'line_bot',
        'updated_by_name': get_line_sender_display_name(event) if event else 'LINE Bot',
    }
    payload.update(updates)
    db.collection(LINE_TODO_SETTINGS_COLLECTION).document(_line_todo_setting_doc_id(target_id)).set(payload, merge=True)
    return True, ''


def _parse_line_todo_reminder_setting_command(raw_text: str):
    """解析 #設定代辦提醒 / #設定提醒。"""
    text = (raw_text or '').strip()
    if text.startswith('#設定代辦提醒'):
        body = text.replace('#設定代辦提醒', '', 1).strip()
    elif text.startswith('#設定提醒'):
        body = text.replace('#設定提醒', '', 1).strip()
    else:
        body = text

    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    joined = ' '.join(lines)

    updates = {}
    errors = []

    key_aliases = {
        '今日': 'today_reminder_time',
        '今天': 'today_reminder_time',
        '今日提醒': 'today_reminder_time',
        '今天提醒': 'today_reminder_time',
        '今日代辦': 'today_reminder_time',
        '明日': 'tomorrow_reminder_time',
        '明天': 'tomorrow_reminder_time',
        '明日提醒': 'tomorrow_reminder_time',
        '明天提醒': 'tomorrow_reminder_time',
        '明天代辦': 'tomorrow_reminder_time',
    }

    # 多行 key: value 格式
    for line in lines:
        m = re.match(r'^([^:：]+)\s*[:：]\s*(.+)$', line)
        if not m:
            continue
        key = re.sub(r'\s+', '', m.group(1).strip())
        value = m.group(2).strip()
        field = key_aliases.get(key)
        if field:
            t = _normalize_line_todo_time(value)
            if t:
                updates[field] = t
            else:
                errors.append(f'{key} 的時間看不懂：{value}')

    # 一行自然語句格式，例如：#設定提醒 今日 08:00 明天 23:00
    patterns = [
        (r'(今日|今天|今日代辦|今天代辦|今日提醒|今天提醒)\D{0,10}((?:上午|早上|下午|晚上|晚間|傍晚|清晨)?\s*\d{1,2}(?::\d{1,2}|點\d{0,2}|時\d{0,2}分?)?)', 'today_reminder_time'),
        (r'(明日|明天|明日代辦|明天代辦|明日提醒|明天提醒)\D{0,10}((?:上午|早上|下午|晚上|晚間|傍晚|清晨)?\s*\d{1,2}(?::\d{1,2}|點\d{0,2}|時\d{0,2}分?)?)', 'tomorrow_reminder_time'),
    ]
    for pattern, field in patterns:
        m = re.search(pattern, joined)
        if m and field not in updates:
            t = _normalize_line_todo_time(m.group(2))
            if t:
                updates[field] = t

    return updates, errors


def set_line_todo_reminder_settings_from_command(raw_text: str, event):
    target_id, target_type = _line_todo_target_from_event(event)
    updates, errors = _parse_line_todo_reminder_setting_command(raw_text)

    if not updates:
        msg = (
            '請告訴我要設定的提醒時間。\n\n'
            '範例：\n'
            '#設定代辦提醒\n'
            '今日提醒: 08:00\n'
            '明天提醒: 23:00\n\n'
            '也可以：#設定提醒 今日 08:00 明天 23:00'
        )
        if errors:
            msg += '\n\n' + '\n'.join(errors[:3])
        return {'handled': True, 'ok': False, 'reply_text': msg, 'parsed_tag': '設定代辦提醒'}

    ok, err = _save_line_todo_reminder_settings(target_id, target_type, updates, event=event)
    if not ok:
        return {'handled': True, 'ok': False, 'reply_text': err, 'parsed_tag': '設定代辦提醒'}

    current = _get_line_todo_reminder_settings(target_id, target_type)
    lines = ['已更新代辦提醒設定']
    lines.append(f"今日代辦提醒：{'開啟' if current.get('today_enabled', True) else '關閉'}｜{current.get('today_reminder_time', '08:00')}")
    lines.append(f"明天代辦提醒：{'開啟' if current.get('tomorrow_enabled', True) else '關閉'}｜{current.get('tomorrow_reminder_time', '23:00')}")
    lines.append('')
    lines.append('查詢設定：#代辦提醒設定')
    return {'handled': True, 'ok': True, 'reply_text': '\n'.join(lines), 'parsed_tag': '設定代辦提醒'}


def query_line_todo_reminder_settings(event):
    target_id, target_type = _line_todo_target_from_event(event)
    current = _get_line_todo_reminder_settings(target_id, target_type)
    lines = [
        '目前代辦提醒設定',
        f"今日代辦提醒：{'開啟' if current.get('today_enabled', True) else '關閉'}｜{current.get('today_reminder_time', '08:00')}",
        f"明天代辦提醒：{'開啟' if current.get('tomorrow_enabled', True) else '關閉'}｜{current.get('tomorrow_reminder_time', '23:00')}",
        '',
        '修改範例：',
        '#設定代辦提醒',
        '今日提醒: 08:00',
        '明天提醒: 23:00',
        '',
        '關閉範例：#關閉代辦提醒 明天',
    ]
    return {'handled': True, 'ok': True, 'reply_text': '\n'.join(lines), 'parsed_tag': '代辦提醒設定'}


def switch_line_todo_reminder(raw_text: str, event, enabled: bool):
    target_id, target_type = _line_todo_target_from_event(event)
    body = raw_text.replace('#關閉代辦提醒', '', 1).replace('#開啟代辦提醒', '', 1).strip()
    body = body or '全部'

    updates = {}
    if any(x in body for x in ['今日', '今天']):
        updates['today_enabled'] = enabled
    if any(x in body for x in ['明日', '明天']):
        updates['tomorrow_enabled'] = enabled
    if not updates or '全部' in body or '全開' in body or '全關' in body:
        updates = {'today_enabled': enabled, 'tomorrow_enabled': enabled}

    ok, err = _save_line_todo_reminder_settings(target_id, target_type, updates, event=event)
    if not ok:
        return {'handled': True, 'ok': False, 'reply_text': err, 'parsed_tag': '代辦提醒設定'}

    current = _get_line_todo_reminder_settings(target_id, target_type)
    action = '開啟' if enabled else '關閉'
    lines = [
        f'已{action}代辦提醒',
        f"今日代辦提醒：{'開啟' if current.get('today_enabled', True) else '關閉'}｜{current.get('today_reminder_time', '08:00')}",
        f"明天代辦提醒：{'開啟' if current.get('tomorrow_enabled', True) else '關閉'}｜{current.get('tomorrow_reminder_time', '23:00')}",
    ]
    return {'handled': True, 'ok': True, 'reply_text': '\n'.join(lines), 'parsed_tag': '代辦提醒設定'}


def process_line_todo_reminder_settings_message_event(event):
    message = event.get('message') or {}
    if message.get('type') != 'text':
        return {'handled': False}

    raw_text = (message.get('text') or '').strip()
    if not raw_text.startswith('#'):
        return {'handled': False}

    if raw_text.startswith('#設定代辦提醒') or raw_text.startswith('#設定提醒'):
        result = set_line_todo_reminder_settings_from_command(raw_text, event)
        save_line_log({'tag': result.get('parsed_tag', '設定代辦提醒'), 'action': 'line_todo_reminder_setting_update', 'fields': {}, 'raw_text': raw_text}, event, 'success' if result.get('ok') else 'failed', note=result.get('reply_text', ''), sender_display_name=get_line_sender_display_name(event))
        return result

    if raw_text.startswith('#代辦提醒設定') or raw_text.startswith('#查詢代辦提醒') or raw_text.startswith('#提醒設定'):
        result = query_line_todo_reminder_settings(event)
        save_line_log({'tag': result.get('parsed_tag', '代辦提醒設定'), 'action': 'line_todo_reminder_setting_query', 'fields': {}, 'raw_text': raw_text}, event, 'success', note=result.get('reply_text', ''), sender_display_name=get_line_sender_display_name(event))
        return result

    if raw_text.startswith('#關閉代辦提醒'):
        result = switch_line_todo_reminder(raw_text, event, enabled=False)
        save_line_log({'tag': result.get('parsed_tag', '代辦提醒設定'), 'action': 'line_todo_reminder_setting_disable', 'fields': {}, 'raw_text': raw_text}, event, 'success' if result.get('ok') else 'failed', note=result.get('reply_text', ''), sender_display_name=get_line_sender_display_name(event))
        return result

    if raw_text.startswith('#開啟代辦提醒'):
        result = switch_line_todo_reminder(raw_text, event, enabled=True)
        save_line_log({'tag': result.get('parsed_tag', '代辦提醒設定'), 'action': 'line_todo_reminder_setting_enable', 'fields': {}, 'raw_text': raw_text}, event, 'success' if result.get('ok') else 'failed', note=result.get('reply_text', ''), sender_display_name=get_line_sender_display_name(event))
        return result

    return {'handled': False}


# 先攔截「提醒設定」指令，其它代辦指令與原本客需 / 委託 / 開發指令照舊。
_process_line_todo_message_event_before_reminder_settings = process_line_todo_message_event


def process_line_todo_message_event(event):
    setting_result = process_line_todo_reminder_settings_message_event(event)
    if setting_result.get('handled'):
        return setting_result
    return _process_line_todo_message_event_before_reminder_settings(event)


def _collect_line_todo_targets_with_open_items():
    targets = {}
    for doc in db.collection(LINE_TODO_COLLECTION).stream():
        data = doc.to_dict() or {}
        if data.get('status', 'open') != 'open':
            continue
        target_id = data.get('line_target_id', '')
        if not target_id:
            continue
        targets[target_id] = data.get('line_target_type', '')
    return targets


def send_due_line_todo_reminders_by_settings():
    """
    建議由 Cron 每 5~10 分鐘呼叫一次。
    會依各 LINE 對話儲存的提醒設定判斷是否該發：
    - 今日提醒：todo_date <= 今天，包含尚未完成
    - 明天提醒：todo_date == 明天
    每一種提醒每天只發一次。
    """
    now_dt = now_taipei()
    today = now_dt.strftime('%Y-%m-%d')
    tomorrow = (now_dt.date() + timedelta(days=1)).strftime('%Y-%m-%d')
    current_minutes = now_dt.hour * 60 + now_dt.minute

    targets = _collect_line_todo_targets_with_open_items()
    sent_count = 0
    failed = []
    checked_targets = 0

    for target_id, target_type in targets.items():
        checked_targets += 1
        settings = _get_line_todo_reminder_settings(target_id, target_type)

        # 今日提醒：尚未完成 + 今日要做
        if settings.get('today_enabled', True):
            today_time = settings.get('today_reminder_time', '08:00')
            if current_minutes >= _time_to_minutes(today_time, default='08:00'):
                items = []
                for doc in db.collection(LINE_TODO_COLLECTION).stream():
                    data = doc.to_dict() or {}
                    if data.get('status', 'open') != 'open':
                        continue
                    if data.get('line_target_id') != target_id:
                        continue
                    todo_date = (data.get('todo_date') or '').strip()
                    if not todo_date or todo_date > today:
                        continue
                    sent_dates = data.get('reminder_sent_dates') or []
                    if today in sent_dates:
                        continue
                    items.append(doc)

                if items:
                    overdue_items = _sort_line_todo_docs([d for d in items if _todo_date_value(d) < today])
                    today_items = _sort_line_todo_docs([d for d in items if _todo_date_value(d) == today])
                    text = _format_line_todo_sections(
                        overdue_items,
                        today_items,
                        f'今日代辦提醒 {_todo_display_md(today)}',
                        today_label='今天',
                    )
                    ok, msg = push_line_text(target_id, text)
                    if ok:
                        sent_count += 1
                        for doc in items:
                            doc.reference.update({
                                'reminder_sent_dates': firestore.ArrayUnion([today]),
                                'last_reminded_at': now_taipei().isoformat(),
                            })
                    else:
                        failed.append({'target_id': target_id, 'type': 'today', 'error': msg})

        # 明天提醒：明天要做
        if settings.get('tomorrow_enabled', True):
            tomorrow_time = settings.get('tomorrow_reminder_time', '23:00')
            if current_minutes >= _time_to_minutes(tomorrow_time, default='23:00'):
                items = []
                for doc in db.collection(LINE_TODO_COLLECTION).stream():
                    data = doc.to_dict() or {}
                    if data.get('status', 'open') != 'open':
                        continue
                    if data.get('line_target_id') != target_id:
                        continue
                    todo_date = (data.get('todo_date') or '').strip()
                    if todo_date != tomorrow:
                        continue
                    sent_dates = data.get('tomorrow_reminder_sent_dates') or []
                    if tomorrow in sent_dates:
                        continue
                    items.append(doc)

                if items:
                    tomorrow_items = _sort_line_todo_docs(items)
                    text = _format_line_todo_sections(
                        [],
                        tomorrow_items,
                        f'明天 {_todo_display_md(tomorrow)} 要做的事情',
                        today_label='明天',
                    )
                    ok, msg = push_line_text(target_id, text)
                    if ok:
                        sent_count += 1
                        for doc in items:
                            doc.reference.update({
                                'tomorrow_reminder_sent_dates': firestore.ArrayUnion([tomorrow]),
                                'last_tomorrow_reminded_at': now_taipei().isoformat(),
                            })
                    else:
                        failed.append({'target_id': target_id, 'type': 'tomorrow', 'error': msg})

    return {
        'now': now_dt.strftime('%Y-%m-%d %H:%M:%S'),
        'checked_targets': checked_targets,
        'sent_count': sent_count,
        'failed': failed,
    }


@app.route('/line/todos/reminder-check', methods=['GET', 'POST'])
def line_todos_reminder_check():
    # Render Cron / UptimeRobot 建議每 5~10 分鐘呼叫：/line/todos/reminder-check?key=你的密鑰
    secret = os.environ.get('TODO_REMINDER_SECRET', '').strip()
    key = request.args.get('key', '').strip() or request.form.get('key', '').strip()
    if secret and key != secret:
        return {'ok': False, 'message': 'Invalid key'}, 403
    result = send_due_line_todo_reminders_by_settings()
    return {'ok': True, 'result': result}, 200

# ========= LINE Bot 代辦事項 Patch v6 End =========

# ========= LINE Bot 代辦事項 Patch v7：固定每日提醒時間 + 自訂開頭 =========
# 貼在 v6 代辦事項 Patch 的最底部即可。
# 功能：
# 1. 固定每天「今日代辦提醒」時間，例如 08:00。
# 2. 固定每天「明天代辦提醒」時間，例如 23:00。
# 3. 可用 LINE 指令設定早上開頭 / 晚上開頭。
# 4. 提醒內容格式：開頭文字 -> 空一行 -> 代辦事項。

from datetime import timedelta

# 覆寫 v6 預設設定：加入開頭文字欄位。
LINE_TODO_DEFAULT_REMINDER_SETTINGS = {
    'today_enabled': True,
    'today_reminder_time': '08:00',
    'tomorrow_enabled': True,
    'tomorrow_reminder_time': '23:00',
    'today_opening_text': '各位厝米的夥伴早安 ☀️\n今天的代辦事項如下：',
    'tomorrow_opening_text': '各位厝米的夥伴晚安 🌙\n先看一下明天要完成的事情：',
}


def _line_todo_clean_opening_text(value: str) -> str:
    """整理提醒開頭文字。"""
    text = (value or '').strip()
    if text in ('無', '不用', '不要', '不要開頭', '關閉', '清空', 'none', 'None'):
        return ''
    # 避免太長塞爆 LINE，最多保留 600 字。
    return text[:600]


def _line_todo_preview_opening(value: str) -> str:
    text = (value or '').strip()
    if not text:
        return '未設定'
    one_line = ' / '.join([ln.strip() for ln in text.splitlines() if ln.strip()])
    return one_line[:120]


def _line_todo_add_opening(opening_text: str, todo_text: str) -> str:
    opening = (opening_text or '').strip()
    body = (todo_text or '').strip()
    if not opening:
        return body[:5000]
    return (opening + '\n\n' + body)[:5000]


def _parse_line_todo_reminder_setting_command(raw_text: str):
    """覆寫 v6：解析提醒時間 + 提醒開頭。"""
    text = (raw_text or '').strip()
    if text.startswith('#設定代辦提醒'):
        body = text.replace('#設定代辦提醒', '', 1).strip()
    elif text.startswith('#設定提醒'):
        body = text.replace('#設定提醒', '', 1).strip()
    else:
        body = text

    lines = [ln.rstrip() for ln in body.splitlines() if ln.strip()]
    joined = ' '.join([ln.strip() for ln in lines])

    updates = {}
    errors = []

    time_key_aliases = {
        '今日': 'today_reminder_time',
        '今天': 'today_reminder_time',
        '早上': 'today_reminder_time',
        '早安': 'today_reminder_time',
        '今日提醒': 'today_reminder_time',
        '今天提醒': 'today_reminder_time',
        '早上提醒': 'today_reminder_time',
        '早安提醒': 'today_reminder_time',
        '今日代辦': 'today_reminder_time',
        '今天代辦': 'today_reminder_time',
        '明日': 'tomorrow_reminder_time',
        '明天': 'tomorrow_reminder_time',
        '晚上': 'tomorrow_reminder_time',
        '晚安': 'tomorrow_reminder_time',
        '明日提醒': 'tomorrow_reminder_time',
        '明天提醒': 'tomorrow_reminder_time',
        '晚上提醒': 'tomorrow_reminder_time',
        '晚安提醒': 'tomorrow_reminder_time',
        '明天代辦': 'tomorrow_reminder_time',
    }

    opening_key_aliases = {
        '今日開頭': 'today_opening_text',
        '今天開頭': 'today_opening_text',
        '早上開頭': 'today_opening_text',
        '早安開頭': 'today_opening_text',
        '今日提醒開頭': 'today_opening_text',
        '今天提醒開頭': 'today_opening_text',
        '早上提醒開頭': 'today_opening_text',
        '今日代辦開頭': 'today_opening_text',
        '明天開頭': 'tomorrow_opening_text',
        '明日開頭': 'tomorrow_opening_text',
        '晚上開頭': 'tomorrow_opening_text',
        '晚安開頭': 'tomorrow_opening_text',
        '明天提醒開頭': 'tomorrow_opening_text',
        '明日提醒開頭': 'tomorrow_opening_text',
        '晚上提醒開頭': 'tomorrow_opening_text',
        '明天代辦開頭': 'tomorrow_opening_text',
    }

    # 多行 key: value 格式。
    for line in lines:
        m = re.match(r'^([^:：]+)\s*[:：]\s*(.*)$', line)
        if not m:
            continue
        key = re.sub(r'\s+', '', m.group(1).strip())
        value = (m.group(2) or '').strip()

        time_field = time_key_aliases.get(key)
        if time_field:
            t = _normalize_line_todo_time(value)
            if t:
                updates[time_field] = t
            else:
                errors.append(f'{key} 的時間看不懂：{value}')
            continue

        opening_field = opening_key_aliases.get(key)
        if opening_field:
            updates[opening_field] = _line_todo_clean_opening_text(value)
            continue

    # 一行自然語句格式，例如：#設定提醒 今日 08:00 明天 23:00
    patterns = [
        (r'(今日|今天|早上|早安|今日代辦|今天代辦|今日提醒|今天提醒|早上提醒|早安提醒)\D{0,10}((?:上午|早上|下午|晚上|晚間|傍晚|清晨)?\s*\d{1,2}(?::\d{1,2}|點\d{0,2}|時\d{0,2}分?)?)', 'today_reminder_time'),
        (r'(明日|明天|晚上|晚安|明日代辦|明天代辦|明日提醒|明天提醒|晚上提醒|晚安提醒)\D{0,10}((?:上午|早上|下午|晚上|晚間|傍晚|清晨)?\s*\d{1,2}(?::\d{1,2}|點\d{0,2}|時\d{0,2}分?)?)', 'tomorrow_reminder_time'),
    ]
    for pattern, field in patterns:
        m = re.search(pattern, joined)
        if m and field not in updates:
            t = _normalize_line_todo_time(m.group(2))
            if t:
                updates[field] = t

    return updates, errors


def _set_line_todo_opening_from_command(raw_text: str, event, target_field: str):
    """處理 #設定今日開頭 / #設定明天開頭。"""
    target_id, target_type = _line_todo_target_from_event(event)

    command_aliases = [
        '#設定今日開頭', '#設定今天開頭', '#設定早安開頭', '#設定早上開頭',
        '#設定明天開頭', '#設定明日開頭', '#設定晚安開頭', '#設定晚上開頭',
    ]
    body = (raw_text or '').strip()
    for cmd in command_aliases:
        if body.startswith(cmd):
            body = body.replace(cmd, '', 1).strip()
            break

    opening = _line_todo_clean_opening_text(body)
    if not opening and body not in ('無', '不用', '不要', '不要開頭', '關閉', '清空'):
        label = '今日提醒開頭' if target_field == 'today_opening_text' else '明天提醒開頭'
        example = '各位厝米的夥伴早安\n今天的代辦事項如下：' if target_field == 'today_opening_text' else '各位厝米的夥伴晚安\n先看一下明天要完成的事情：'
        return {
            'handled': True,
            'ok': False,
            'reply_text': f'請輸入要設定的{label}。\n\n範例：\n#設定{"今日" if target_field == "today_opening_text" else "明天"}開頭\n{example}',
            'parsed_tag': '設定代辦提醒開頭',
        }

    ok, err = _save_line_todo_reminder_settings(target_id, target_type, {target_field: opening}, event=event)
    if not ok:
        return {'handled': True, 'ok': False, 'reply_text': err, 'parsed_tag': '設定代辦提醒開頭'}

    label = '今日提醒開頭' if target_field == 'today_opening_text' else '明天提醒開頭'
    msg = f'已更新{label}：\n{opening or "未設定"}'
    return {'handled': True, 'ok': True, 'reply_text': msg[:5000], 'parsed_tag': '設定代辦提醒開頭'}


def set_line_todo_reminder_settings_from_command(raw_text: str, event):
    """覆寫 v6：設定時間時也可以同時設定開頭。"""
    target_id, target_type = _line_todo_target_from_event(event)
    updates, errors = _parse_line_todo_reminder_setting_command(raw_text)

    if not updates:
        msg = (
            '請告訴我要設定的固定每日提醒時間或開頭。\n\n'
            '範例：\n'
            '#設定代辦提醒\n'
            '今日提醒: 08:00\n'
            '今日開頭: 各位厝米的夥伴早安 ☀️\n'
            '明天提醒: 23:00\n'
            '明天開頭: 各位厝米的夥伴晚安 🌙\n\n'
            '也可以單獨設定：\n'
            '#設定今日開頭\n'
            '各位厝米的夥伴早安\n'
            '今天的代辦事項如下：'
        )
        if errors:
            msg += '\n\n' + '\n'.join(errors[:3])
        return {'handled': True, 'ok': False, 'reply_text': msg[:5000], 'parsed_tag': '設定代辦提醒'}

    ok, err = _save_line_todo_reminder_settings(target_id, target_type, updates, event=event)
    if not ok:
        return {'handled': True, 'ok': False, 'reply_text': err, 'parsed_tag': '設定代辦提醒'}

    current = _get_line_todo_reminder_settings(target_id, target_type)
    lines = ['已更新固定每日代辦提醒設定']
    lines.append(f"今日代辦提醒：{'開啟' if current.get('today_enabled', True) else '關閉'}｜{current.get('today_reminder_time', '08:00')}")
    lines.append(f"今日開頭：{_line_todo_preview_opening(current.get('today_opening_text', ''))}")
    lines.append(f"明天代辦提醒：{'開啟' if current.get('tomorrow_enabled', True) else '關閉'}｜{current.get('tomorrow_reminder_time', '23:00')}")
    lines.append(f"明天開頭：{_line_todo_preview_opening(current.get('tomorrow_opening_text', ''))}")
    lines.append('')
    lines.append('查詢設定：#代辦提醒設定')
    return {'handled': True, 'ok': True, 'reply_text': '\n'.join(lines)[:5000], 'parsed_tag': '設定代辦提醒'}


def query_line_todo_reminder_settings(event):
    """覆寫 v6：查詢設定時顯示開頭。"""
    target_id, target_type = _line_todo_target_from_event(event)
    current = _get_line_todo_reminder_settings(target_id, target_type)
    lines = [
        '目前固定每日代辦提醒設定',
        f"今日代辦提醒：{'開啟' if current.get('today_enabled', True) else '關閉'}｜{current.get('today_reminder_time', '08:00')}",
        f"今日開頭：{_line_todo_preview_opening(current.get('today_opening_text', ''))}",
        f"明天代辦提醒：{'開啟' if current.get('tomorrow_enabled', True) else '關閉'}｜{current.get('tomorrow_reminder_time', '23:00')}",
        f"明天開頭：{_line_todo_preview_opening(current.get('tomorrow_opening_text', ''))}",
        '',
        '修改範例：',
        '#設定代辦提醒',
        '今日提醒: 08:00',
        '今日開頭: 各位厝米的夥伴早安 ☀️',
        '明天提醒: 23:00',
        '明天開頭: 各位厝米的夥伴晚安 🌙',
        '',
        '單獨修改開頭：#設定今日開頭',
        '關閉範例：#關閉代辦提醒 明天',
    ]
    return {'handled': True, 'ok': True, 'reply_text': '\n'.join(lines)[:5000], 'parsed_tag': '代辦提醒設定'}


def switch_line_todo_reminder(raw_text: str, event, enabled: bool):
    """覆寫 v6：開關後也顯示開頭摘要。"""
    target_id, target_type = _line_todo_target_from_event(event)
    body = raw_text.replace('#關閉代辦提醒', '', 1).replace('#開啟代辦提醒', '', 1).strip()
    body = body or '全部'

    updates = {}
    if any(x in body for x in ['今日', '今天', '早上', '早安']):
        updates['today_enabled'] = enabled
    if any(x in body for x in ['明日', '明天', '晚上', '晚安']):
        updates['tomorrow_enabled'] = enabled
    if not updates or '全部' in body or '全開' in body or '全關' in body:
        updates = {'today_enabled': enabled, 'tomorrow_enabled': enabled}

    ok, err = _save_line_todo_reminder_settings(target_id, target_type, updates, event=event)
    if not ok:
        return {'handled': True, 'ok': False, 'reply_text': err, 'parsed_tag': '代辦提醒設定'}

    current = _get_line_todo_reminder_settings(target_id, target_type)
    action = '開啟' if enabled else '關閉'
    lines = [
        f'已{action}代辦提醒',
        f"今日代辦提醒：{'開啟' if current.get('today_enabled', True) else '關閉'}｜{current.get('today_reminder_time', '08:00')}",
        f"今日開頭：{_line_todo_preview_opening(current.get('today_opening_text', ''))}",
        f"明天代辦提醒：{'開啟' if current.get('tomorrow_enabled', True) else '關閉'}｜{current.get('tomorrow_reminder_time', '23:00')}",
        f"明天開頭：{_line_todo_preview_opening(current.get('tomorrow_opening_text', ''))}",
    ]
    return {'handled': True, 'ok': True, 'reply_text': '\n'.join(lines)[:5000], 'parsed_tag': '代辦提醒設定'}


def process_line_todo_reminder_settings_message_event(event):
    """覆寫 v6：新增 #設定今日開頭 / #設定明天開頭。"""
    message = event.get('message') or {}
    if message.get('type') != 'text':
        return {'handled': False}

    raw_text = (message.get('text') or '').strip()
    if not raw_text.startswith('#'):
        return {'handled': False}

    today_opening_commands = ('#設定今日開頭', '#設定今天開頭', '#設定早安開頭', '#設定早上開頭')
    tomorrow_opening_commands = ('#設定明天開頭', '#設定明日開頭', '#設定晚安開頭', '#設定晚上開頭')

    if raw_text.startswith(today_opening_commands):
        result = _set_line_todo_opening_from_command(raw_text, event, 'today_opening_text')
        save_line_log({'tag': result.get('parsed_tag', '設定代辦提醒開頭'), 'action': 'line_todo_today_opening_update', 'fields': {}, 'raw_text': raw_text}, event, 'success' if result.get('ok') else 'failed', note=result.get('reply_text', ''), sender_display_name=get_line_sender_display_name(event))
        return result

    if raw_text.startswith(tomorrow_opening_commands):
        result = _set_line_todo_opening_from_command(raw_text, event, 'tomorrow_opening_text')
        save_line_log({'tag': result.get('parsed_tag', '設定代辦提醒開頭'), 'action': 'line_todo_tomorrow_opening_update', 'fields': {}, 'raw_text': raw_text}, event, 'success' if result.get('ok') else 'failed', note=result.get('reply_text', ''), sender_display_name=get_line_sender_display_name(event))
        return result

    if raw_text.startswith('#設定代辦提醒') or raw_text.startswith('#設定提醒'):
        result = set_line_todo_reminder_settings_from_command(raw_text, event)
        save_line_log({'tag': result.get('parsed_tag', '設定代辦提醒'), 'action': 'line_todo_reminder_setting_update', 'fields': {}, 'raw_text': raw_text}, event, 'success' if result.get('ok') else 'failed', note=result.get('reply_text', ''), sender_display_name=get_line_sender_display_name(event))
        return result

    if raw_text.startswith('#代辦提醒設定') or raw_text.startswith('#查詢代辦提醒') or raw_text.startswith('#提醒設定'):
        result = query_line_todo_reminder_settings(event)
        save_line_log({'tag': result.get('parsed_tag', '代辦提醒設定'), 'action': 'line_todo_reminder_setting_query', 'fields': {}, 'raw_text': raw_text}, event, 'success', note=result.get('reply_text', ''), sender_display_name=get_line_sender_display_name(event))
        return result

    if raw_text.startswith('#關閉代辦提醒'):
        result = switch_line_todo_reminder(raw_text, event, enabled=False)
        save_line_log({'tag': result.get('parsed_tag', '代辦提醒設定'), 'action': 'line_todo_reminder_setting_disable', 'fields': {}, 'raw_text': raw_text}, event, 'success' if result.get('ok') else 'failed', note=result.get('reply_text', ''), sender_display_name=get_line_sender_display_name(event))
        return result

    if raw_text.startswith('#開啟代辦提醒'):
        result = switch_line_todo_reminder(raw_text, event, enabled=True)
        save_line_log({'tag': result.get('parsed_tag', '代辦提醒設定'), 'action': 'line_todo_reminder_setting_enable', 'fields': {}, 'raw_text': raw_text}, event, 'success' if result.get('ok') else 'failed', note=result.get('reply_text', ''), sender_display_name=get_line_sender_display_name(event))
        return result

    return {'handled': False}


def send_due_line_todo_reminders_by_settings():
    """
    覆寫 v6：依固定每日時間提醒，並在代辦清單前加上自訂開頭。
    建議 Cron 每 5~10 分鐘呼叫 /line/todos/reminder-check?key=你的密鑰。
    """
    now_dt = now_taipei()
    today = now_dt.strftime('%Y-%m-%d')
    tomorrow = (now_dt.date() + timedelta(days=1)).strftime('%Y-%m-%d')
    current_minutes = now_dt.hour * 60 + now_dt.minute

    targets = _collect_line_todo_targets_with_open_items()
    sent_count = 0
    failed = []
    checked_targets = 0

    for target_id, target_type in targets.items():
        checked_targets += 1
        settings = _get_line_todo_reminder_settings(target_id, target_type)

        # 今日提醒：尚未完成 + 今日要做。
        if settings.get('today_enabled', True):
            today_time = settings.get('today_reminder_time', '08:00')
            if current_minutes >= _time_to_minutes(today_time, default='08:00'):
                items = []
                for doc in db.collection(LINE_TODO_COLLECTION).stream():
                    data = doc.to_dict() or {}
                    if data.get('status', 'open') != 'open':
                        continue
                    if data.get('line_target_id') != target_id:
                        continue
                    todo_date = (data.get('todo_date') or '').strip()
                    if not todo_date or todo_date > today:
                        continue
                    sent_dates = data.get('reminder_sent_dates') or []
                    if today in sent_dates:
                        continue
                    items.append(doc)

                if items:
                    overdue_items = _sort_line_todo_docs([d for d in items if _todo_date_value(d) < today])
                    today_items = _sort_line_todo_docs([d for d in items if _todo_date_value(d) == today])
                    body = _format_line_todo_sections(
                        overdue_items,
                        today_items,
                        f'{_todo_display_md(today)} 今日代辦',
                        today_label='今天',
                    )
                    text = _line_todo_add_opening(settings.get('today_opening_text', ''), body)
                    ok, msg = push_line_text(target_id, text)
                    if ok:
                        sent_count += 1
                        for doc in items:
                            doc.reference.update({
                                'reminder_sent_dates': firestore.ArrayUnion([today]),
                                'last_reminded_at': now_taipei().isoformat(),
                            })
                    else:
                        failed.append({'target_id': target_id, 'type': 'today', 'error': msg})

        # 明天提醒：明天要做。
        if settings.get('tomorrow_enabled', True):
            tomorrow_time = settings.get('tomorrow_reminder_time', '23:00')
            if current_minutes >= _time_to_minutes(tomorrow_time, default='23:00'):
                items = []
                for doc in db.collection(LINE_TODO_COLLECTION).stream():
                    data = doc.to_dict() or {}
                    if data.get('status', 'open') != 'open':
                        continue
                    if data.get('line_target_id') != target_id:
                        continue
                    todo_date = (data.get('todo_date') or '').strip()
                    if todo_date != tomorrow:
                        continue
                    sent_dates = data.get('tomorrow_reminder_sent_dates') or []
                    if tomorrow in sent_dates:
                        continue
                    items.append(doc)

                if items:
                    tomorrow_items = _sort_line_todo_docs(items)
                    body = _format_line_todo_sections(
                        [],
                        tomorrow_items,
                        f'{_todo_display_md(tomorrow)} 明天代辦',
                        today_label='明天',
                    )
                    text = _line_todo_add_opening(settings.get('tomorrow_opening_text', ''), body)
                    ok, msg = push_line_text(target_id, text)
                    if ok:
                        sent_count += 1
                        for doc in items:
                            doc.reference.update({
                                'tomorrow_reminder_sent_dates': firestore.ArrayUnion([tomorrow]),
                                'last_tomorrow_reminded_at': now_taipei().isoformat(),
                            })
                    else:
                        failed.append({'target_id': target_id, 'type': 'tomorrow', 'error': msg})

    return {
        'now': now_dt.strftime('%Y-%m-%d %H:%M:%S'),
        'checked_targets': checked_targets,
        'sent_count': sent_count,
        'failed': failed,
    }

# ========= LINE Bot 代辦事項 Patch v7 End =========


# ========= LINE Bot 代辦事項 Patch v8：防止重複新增 + 清除既有重複代辦 =========
# 貼在 v7 patch 的最底部即可。
# 目的：
# 1. 同一個 LINE 訊息重送時，不會重複新增代辦。
# 2. 同一個 LINE 對話、同一天、同一個事項，已經有未完成代辦時，不再新增第二筆。
# 3. 清單顯示時會自動去重，避免同一事項出現兩次。
# 4. 可用 #清除重複代辦 一次整理目前 LINE 對話中的重複資料。

LINE_TODO_PROCESSED_COLLECTION = os.environ.get('LINE_TODO_PROCESSED_COLLECTION', 'line_todo_processed_messages')


def _line_todo_normalize_title_for_dedupe(value: str) -> str:
    """把代辦標題正規化，避免空白不同造成重複判斷失敗。"""
    text = (value or '').strip()
    text = re.sub(r'\s+', ' ', text)
    return text.lower()


def _line_todo_dedupe_key(target_id: str, todo_date: str, title: str) -> str:
    return f"{target_id}|{todo_date}|{_line_todo_normalize_title_for_dedupe(title)}"


def _line_todo_doc_dedupe_key(doc):
    data = doc.to_dict() or {}
    return _line_todo_dedupe_key(
        data.get('line_target_id', ''),
        (data.get('todo_date') or '').strip(),
        data.get('title', ''),
    )


def _find_existing_open_line_todo(target_id: str, todo_date: str, title: str):
    """找同一對話、同一天、同事項的未完成代辦。"""
    key = _line_todo_dedupe_key(target_id, todo_date, title)
    for doc in db.collection(LINE_TODO_COLLECTION).stream():
        data = doc.to_dict() or {}
        if data.get('status', 'open') != 'open':
            continue
        if data.get('line_target_id', '') != target_id:
            continue
        if (data.get('todo_date') or '').strip() != todo_date:
            continue
        existing_key = data.get('dedupe_key') or _line_todo_dedupe_key(
            data.get('line_target_id', ''),
            (data.get('todo_date') or '').strip(),
            data.get('title', ''),
        )
        if existing_key == key:
            return doc
    return None


def _line_todo_dedupe_docs_for_display(items):
    """顯示清單前去重；保留最早建立的那筆。"""
    sorted_items = sorted(
        list(items or []),
        key=lambda d: ((d.to_dict() or {}).get('created_at') or '', d.id)
    )
    seen = set()
    result = []
    for doc in sorted_items:
        data = doc.to_dict() or {}
        if data.get('status', 'open') != 'open':
            continue
        key = data.get('dedupe_key') or _line_todo_doc_dedupe_key(doc)
        if key in seen:
            continue
        seen.add(key)
        result.append(doc)
    return result


# 覆寫新增單筆：加入「同訊息防重送」與「同日期同事項防重複」。
def create_line_todo(fields, event):
    title = (fields.get('title') or '').strip()
    todo_date = _parse_line_todo_date(fields.get('todo_date') or fields.get('todo_date_raw') or '')
    note = (fields.get('note') or '').strip()
    remind_time = (fields.get('remind_time') or '').strip()

    if not title:
        return {
            'handled': True,
            'ok': False,
            'reply_text': '未新增：請填「事項」。\n\n範例：\n#新增代辦\n日期: 明天\n厝米排版 土地現廣稿\n拍水哥爸爸土地',
            'parsed_tag': '新增代辦',
        }
    if not todo_date:
        return {'handled': True, 'ok': False, 'reply_text': '未新增：日期格式看不懂，請用 5/29、今天、明天。', 'parsed_tag': '新增代辦'}

    target_id, target_type = _line_todo_target_from_event(event)
    duplicate_doc = _find_existing_open_line_todo(target_id, todo_date, title)
    if duplicate_doc:
        return {
            'handled': True,
            'ok': False,
            'duplicate': True,
            'reply_text': f'已存在，不重複新增：{_todo_display_md(todo_date)}｜{title}',
            'parsed_tag': '新增代辦',
        }

    source = event.get('source') or {}
    message = event.get('message') or {}
    sender_display_name = get_line_sender_display_name(event)
    now = now_taipei().isoformat()
    dedupe_key = _line_todo_dedupe_key(target_id, todo_date, title)

    doc_ref = db.collection(LINE_TODO_COLLECTION).document()
    doc_ref.set({
        'title': title,
        'todo_date': todo_date,
        'note': note,
        'remind_time': remind_time,
        'status': 'open',
        'dedupe_key': dedupe_key,
        'line_message_id': message.get('id', ''),
        'line_target_id': target_id,
        'line_target_type': target_type,
        'line_group_id': source.get('groupId', ''),
        'line_room_id': source.get('roomId', ''),
        'line_user_id': source.get('userId', ''),
        'sender_display_name': sender_display_name,
        'created_at': now,
        'created_by_id': 'line_bot',
        'created_by_name': sender_display_name or 'LINE Bot',
        'reminder_sent_dates': [],
        'tomorrow_reminder_sent_dates': [],
    })

    return {
        'handled': True,
        'ok': True,
        'todo_date': todo_date,
        'title': title,
        'reply_text': f"已新增代辦\n日期：{_todo_display_md(todo_date)}\n事項：{title}\n\n查詢請回：#今日代辦",
        'parsed_tag': '新增代辦',
    }


# 覆寫批次新增：若同批或資料庫已存在相同代辦，就跳過不新增。
def create_line_todos_bulk(fields_list, event):
    if not fields_list:
        return {
            'handled': True,
            'ok': False,
            'reply_text': (
                '未新增：請在 #新增代辦 下一行開始貼代辦事項。\n\n'
                '範例一：同一天\n'
                '#新增代辦\n'
                '日期: 明天\n'
                '厝米排版 土地現廣稿\n'
                '拍水哥爸爸土地\n\n'
                '範例二：不同天\n'
                '#新增代辦\n'
                '5/29 厝米排版 土地現廣稿\n'
                '5/30 拍水哥爸爸土地'
            ),
            'parsed_tag': '新增代辦',
        }

    ok_items = []
    skipped = []
    failed_msgs = []
    seen_in_this_message = set()
    target_id, _target_type = _line_todo_target_from_event(event)

    for fields in fields_list:
        title = (fields.get('title') or '').strip()
        todo_date = _parse_line_todo_date(fields.get('todo_date') or fields.get('todo_date_raw') or '')
        local_key = _line_todo_dedupe_key(target_id, todo_date, title)
        if title and todo_date and local_key in seen_in_this_message:
            skipped.append(f'{_todo_display_md(todo_date)}｜{title}')
            continue
        if title and todo_date:
            seen_in_this_message.add(local_key)

        result = create_line_todo(fields, event)
        if result.get('ok'):
            ok_items.append({
                'todo_date': result.get('todo_date') or todo_date,
                'title': result.get('title') or title,
            })
        elif result.get('duplicate'):
            skipped.append(f'{_todo_display_md(todo_date)}｜{title}')
        else:
            failed_msgs.append(result.get('reply_text', '新增失敗'))

    lines = []
    if ok_items:
        grouped = {}
        for item in ok_items:
            d = item.get('todo_date', '') or now_taipei().strftime('%Y-%m-%d')
            grouped.setdefault(d, []).append(item.get('title', ''))

        lines.append(f'已新增 {len(ok_items)} 筆代辦')
        if len(grouped) == 1:
            only_date = next(iter(grouped.keys()))
            lines.append(f'日期：{_todo_display_md(only_date)}')
            lines.append('')
            for idx, title in enumerate(grouped[only_date], 1):
                lines.append(f'{idx}. {title}')
        else:
            lines.append('')
            running = 1
            for d in sorted(grouped.keys()):
                lines.append(f'【{_todo_display_md(d)}】')
                for title in grouped[d]:
                    lines.append(f'{running}. {title}')
                    running += 1
                lines.append('')
            if lines and lines[-1] == '':
                lines.pop()

    if skipped:
        if lines:
            lines.append('')
        lines.append(f'已跳過 {len(skipped)} 筆重複代辦')
        for item in skipped[:8]:
            lines.append(f'- {item}')
        if len(skipped) > 8:
            lines.append(f'...另有 {len(skipped) - 8} 筆')

    if failed_msgs:
        if lines:
            lines.append('')
        lines.append('有部分未新增：')
        lines.extend(failed_msgs[:3])

    if lines:
        lines.append('')
        lines.append('查詢請回：#今日代辦')
        lines.append('完成可回：#完成代辦 1')
        return {'handled': True, 'ok': bool(ok_items), 'reply_text': '\n'.join(lines)[:5000], 'parsed_tag': '新增代辦'}

    return {'handled': True, 'ok': False, 'reply_text': '沒有新增任何代辦。', 'parsed_tag': '新增代辦'}


# 覆寫清單格式：顯示前去重，避免舊資料中已有重複時畫面重複。
def _format_line_todo_sections(overdue_items, today_items, title, today_label='今天'):
    overdue_items = _line_todo_dedupe_docs_for_display(overdue_items)
    today_items = _line_todo_dedupe_docs_for_display(today_items)

    if not overdue_items and not today_items:
        return f'{title}\n目前沒有未完成代辦。'

    lines = [title]
    idx = 1

    if overdue_items:
        lines.append('')
        lines.append('【尚未完成】')
        for doc in overdue_items:
            data = doc.to_dict() or {}
            note = (data.get('note') or '').strip()
            date_text = _todo_display_md(data.get('todo_date', ''))
            lines.append(f"{idx}. {date_text}｜{data.get('title', '')}")
            if note:
                lines.append(f'   備註: {note}')
            idx += 1

    if today_items:
        lines.append('')
        lines.append(f'【{today_label}要做】')
        for doc in today_items:
            data = doc.to_dict() or {}
            note = (data.get('note') or '').strip()
            lines.append(f"{idx}. {data.get('title', '')}")
            if note:
                lines.append(f'   備註: {note}')
            idx += 1

    lines.append('')
    lines.append('完成請回：#完成代辦 1')
    lines.append('一次完成多筆：#完成代辦 1 3')
    lines.append('清除請回：#清除代辦 1')
    return '\n'.join(lines)[:5000]


def _cleanup_duplicate_line_todos_for_target(event):
    """刪除目前 LINE 對話中重複的未完成代辦，保留最早建立的一筆。"""
    target_id, _target_type = _line_todo_target_from_event(event)
    docs = []
    for doc in db.collection(LINE_TODO_COLLECTION).stream():
        data = doc.to_dict() or {}
        if data.get('status', 'open') != 'open':
            continue
        if data.get('line_target_id') != target_id:
            continue
        docs.append(doc)

    docs = sorted(docs, key=lambda d: ((d.to_dict() or {}).get('created_at') or '', d.id))
    seen = {}
    deleted_titles = []
    updated_keys = 0

    for doc in docs:
        data = doc.to_dict() or {}
        key = data.get('dedupe_key') or _line_todo_doc_dedupe_key(doc)
        # 順手補上舊資料沒有的 dedupe_key，之後更好判斷。
        if not data.get('dedupe_key'):
            try:
                doc.reference.update({'dedupe_key': key})
                updated_keys += 1
            except Exception:
                pass

        if key in seen:
            title = data.get('title', '')
            date_text = _todo_display_md(data.get('todo_date', ''))
            doc.reference.delete()
            deleted_titles.append(f'{date_text}｜{title}')
        else:
            seen[key] = doc.id

    if not deleted_titles:
        msg = '目前沒有找到重複的未完成代辦。'
        if updated_keys:
            msg += f'\n已順手整理 {updated_keys} 筆代辦索引。'
        return {'handled': True, 'ok': True, 'reply_text': msg, 'parsed_tag': '清除重複代辦'}

    lines = [f'已清除 {len(deleted_titles)} 筆重複代辦']
    for item in deleted_titles[:12]:
        lines.append(f'- {item}')
    if len(deleted_titles) > 12:
        lines.append(f'...另有 {len(deleted_titles) - 12} 筆')
    lines.append('')
    lines.append('查詢請回：#今日代辦')
    return {'handled': True, 'ok': True, 'reply_text': '\n'.join(lines)[:5000], 'parsed_tag': '清除重複代辦'}


# 先攔截「清除重複代辦」與「同一 LINE 訊息重送」。
_process_line_todo_message_event_before_duplicate_guard = process_line_todo_message_event


def process_line_todo_message_event(event):
    message = event.get('message') or {}
    if message.get('type') != 'text':
        return {'handled': False}

    raw_text = (message.get('text') or '').strip()
    if not raw_text.startswith('#'):
        return {'handled': False}

    if raw_text.startswith('#清除重複代辦') or raw_text.startswith('#整理代辦'):
        result = _cleanup_duplicate_line_todos_for_target(event)
        save_line_log(
            {'tag': result.get('parsed_tag', '清除重複代辦'), 'action': 'line_todo_cleanup_duplicates', 'fields': {}, 'raw_text': raw_text},
            event,
            'success' if result.get('ok') else 'failed',
            note=result.get('reply_text', ''),
            sender_display_name=get_line_sender_display_name(event),
        )
        return result

    # LINE webhook 有時會 retry，同一 message_id 若已處理過，就不要再新增一次。
    if raw_text.startswith('#新增代辦'):
        message_id = message.get('id', '')
        if message_id:
            processed_ref = db.collection(LINE_TODO_PROCESSED_COLLECTION).document(str(message_id))
            processed_doc = processed_ref.get()
            if processed_doc.exists:
                return {
                    'handled': True,
                    'ok': True,
                    'reply_text': '這則新增代辦已經處理過，為避免重複新增，本次不再新增。\n\n查詢請回：#今日代辦',
                    'parsed_tag': '新增代辦',
                }

            result = _process_line_todo_message_event_before_duplicate_guard(event)
            try:
                processed_ref.set({
                    'message_id': message_id,
                    'raw_text': raw_text[:2000],
                    'ok': bool(result.get('ok')),
                    'created_at': now_taipei().isoformat(),
                }, merge=True)
            except Exception:
                pass
            return result

    return _process_line_todo_message_event_before_duplicate_guard(event)

# ========= LINE Bot 代辦事項 Patch v8 End =========


# ========= LINE Bot 代辦事項 Patch v9：台灣時間確認 + 設定後重置提醒標記 + 診斷指令 =========
# 使用方式：貼在 v8 patch 的最底部。
# 目的：
# 1. 明確顯示「目前台灣時間」與提醒時間判斷。
# 2. 設定提醒時間後，會清除該對話今天/明天的已提醒標記，避免你改時間後因為已提醒過而不再跳出。
# 3. 提供 #測試代辦時間 / #檢查代辦時間 指令，確認設定是否真的存到目前 LINE 對話。
# 4. /line/todos/reminder-check 回傳結果也會顯示 taipei_now 與 timezone。

from datetime import timezone


def _line_todo_taipei_now_text():
    dt = now_taipei()
    return dt.strftime('%m/%d %H:%M')


def _line_todo_current_minutes_taipei():
    dt = now_taipei()
    return dt.hour * 60 + dt.minute


def _line_todo_time_status_text(hhmm: str, default: str):
    now_minutes = _line_todo_current_minutes_taipei()
    target_minutes = _time_to_minutes(hhmm, default=default)
    return '已到提醒時間' if now_minutes >= target_minutes else '尚未到提醒時間'


def _line_todo_reset_sent_markers_for_target(target_id: str, reset_today=False, reset_tomorrow=False):
    """設定提醒時間後，清除目前對話的已提醒標記，避免改時間後被舊標記擋住。"""
    if not target_id:
        return 0

    now_dt = now_taipei()
    today = now_dt.strftime('%Y-%m-%d')
    tomorrow = (now_dt.date() + timedelta(days=1)).strftime('%Y-%m-%d')
    count = 0

    for doc in db.collection(LINE_TODO_COLLECTION).stream():
        data = doc.to_dict() or {}
        if data.get('status', 'open') != 'open':
            continue
        if data.get('line_target_id') != target_id:
            continue

        updates = {}
        if reset_today:
            updates['reminder_sent_dates'] = firestore.ArrayRemove([today])
        if reset_tomorrow:
            updates['tomorrow_reminder_sent_dates'] = firestore.ArrayRemove([tomorrow])

        if updates:
            try:
                doc.reference.update(updates)
                count += 1
            except Exception as e:
                print('⚠️ 重置代辦提醒標記失敗：', e)

    return count


def _line_todo_count_open_items_for_target(target_id: str):
    now_dt = now_taipei()
    today = now_dt.strftime('%Y-%m-%d')
    tomorrow = (now_dt.date() + timedelta(days=1)).strftime('%Y-%m-%d')
    overdue_or_today = 0
    tomorrow_count = 0

    for doc in db.collection(LINE_TODO_COLLECTION).stream():
        data = doc.to_dict() or {}
        if data.get('status', 'open') != 'open':
            continue
        if data.get('line_target_id') != target_id:
            continue
        todo_date = (data.get('todo_date') or '').strip()
        if todo_date and todo_date <= today:
            overdue_or_today += 1
        if todo_date == tomorrow:
            tomorrow_count += 1

    return overdue_or_today, tomorrow_count


def _line_todo_settings_debug_text(event):
    target_id, target_type = _line_todo_target_from_event(event)
    settings = _get_line_todo_reminder_settings(target_id, target_type)
    today_count, tomorrow_count = _line_todo_count_open_items_for_target(target_id)

    today_time = settings.get('today_reminder_time', '08:00')
    tomorrow_time = settings.get('tomorrow_reminder_time', '23:00')

    lines = [
        '目前固定每日代辦提醒設定',
        f'現在台灣時間：{_line_todo_taipei_now_text()}',
        '時區：Asia/Taipei',
        '',
        f"今日代辦提醒：{'開啟' if settings.get('today_enabled', True) else '關閉'}｜{today_time}｜{_line_todo_time_status_text(today_time, '08:00')}",
        f"今日開頭：{_line_todo_preview_opening(settings.get('today_opening_text', ''))}",
        f'今日＋尚未完成筆數：{today_count}',
        '',
        f"明天代辦提醒：{'開啟' if settings.get('tomorrow_enabled', True) else '關閉'}｜{tomorrow_time}｜{_line_todo_time_status_text(tomorrow_time, '23:00')}",
        f"明天開頭：{_line_todo_preview_opening(settings.get('tomorrow_opening_text', ''))}",
        f'明天代辦筆數：{tomorrow_count}',
        '',
        '提醒是用台灣時間判斷；如果到了時間仍沒跳，請確認排程器有每 5 分鐘呼叫：',
        '/line/todos/reminder-check?key=你的密鑰',
    ]
    return '\n'.join(lines)[:5000]


# 覆寫查詢設定：直接顯示台灣時間與是否已到提醒時間。
def query_line_todo_reminder_settings(event):
    return {
        'handled': True,
        'ok': True,
        'reply_text': _line_todo_settings_debug_text(event),
        'parsed_tag': '代辦提醒設定',
    }


# 覆寫設定提醒：設定時間後重置已提醒標記，並回覆目前台灣時間與判斷狀態。
def set_line_todo_reminder_settings_from_command(raw_text: str, event):
    target_id, target_type = _line_todo_target_from_event(event)
    updates, errors = _parse_line_todo_reminder_setting_command(raw_text)

    if not updates:
        msg = (
            '請告訴我要設定的固定每日提醒時間或開頭。\n\n'
            '範例：\n'
            '#設定代辦提醒\n'
            '今日提醒: 08:00\n'
            '今日開頭: 各位厝米的夥伴早安 ☀️\n'
            '明天提醒: 23:00\n'
            '明天開頭: 各位厝米的夥伴晚安 🌙'
        )
        if errors:
            msg += '\n\n' + '\n'.join(errors[:3])
        return {'handled': True, 'ok': False, 'reply_text': msg[:5000], 'parsed_tag': '設定代辦提醒'}

    ok, err = _save_line_todo_reminder_settings(target_id, target_type, updates, event=event)
    if not ok:
        return {'handled': True, 'ok': False, 'reply_text': err, 'parsed_tag': '設定代辦提醒'}

    reset_today = 'today_reminder_time' in updates
    reset_tomorrow = 'tomorrow_reminder_time' in updates
    reset_count = _line_todo_reset_sent_markers_for_target(target_id, reset_today=reset_today, reset_tomorrow=reset_tomorrow)

    lines = ['已更新固定每日代辦提醒設定']
    if reset_count:
        lines.append(f'已重新開放 {reset_count} 筆代辦的提醒判斷')
    lines.append('')
    lines.append(_line_todo_settings_debug_text(event))
    lines.append('')
    lines.append('如果現在已經超過設定時間，等排程器下一次呼叫 reminder-check 就會推播。')
    return {'handled': True, 'ok': True, 'reply_text': '\n'.join(lines)[:5000], 'parsed_tag': '設定代辦提醒'}


# 新增診斷指令：#測試代辦時間 / #檢查代辦時間 / #確認代辦時間。
_process_line_todo_reminder_settings_message_event_before_v9_time_debug = process_line_todo_reminder_settings_message_event


def process_line_todo_reminder_settings_message_event(event):
    message = event.get('message') or {}
    if message.get('type') != 'text':
        return {'handled': False}

    raw_text = (message.get('text') or '').strip()
    if raw_text.startswith(('#測試代辦時間', '#檢查代辦時間', '#確認代辦時間', '#代辦時間')):
        result = query_line_todo_reminder_settings(event)
        save_line_log(
            {'tag': '代辦提醒時間診斷', 'action': 'line_todo_reminder_time_debug', 'fields': {}, 'raw_text': raw_text},
            event,
            'success',
            note=result.get('reply_text', ''),
            sender_display_name=get_line_sender_display_name(event),
        )
        return result

    return _process_line_todo_reminder_settings_message_event_before_v9_time_debug(event)


# 覆寫 reminder-check route：回傳台灣時間，方便你用瀏覽器確認是不是用台灣時間。
# 注意：如果你原本已經有同 endpoint route，Flask 不允許重複註冊 endpoint。
# 所以這裡不重複註冊 route，只覆寫 view_functions 裡的 endpoint 函式。
def line_todos_reminder_check_v9():
    secret = os.environ.get('TODO_REMINDER_SECRET', '').strip()
    key = request.args.get('key', '').strip() or request.form.get('key', '').strip()
    if secret and key != secret:
        return {
            'ok': False,
            'message': 'Invalid key',
            'taipei_now': now_taipei().strftime('%Y-%m-%d %H:%M:%S'),
            'timezone': 'Asia/Taipei',
        }, 403

    result = send_due_line_todo_reminders_by_settings()
    result['taipei_now'] = now_taipei().strftime('%Y-%m-%d %H:%M:%S')
    result['timezone'] = 'Asia/Taipei'
    return {'ok': True, 'result': result}, 200


try:
    app.view_functions['line_todos_reminder_check'] = line_todos_reminder_check_v9
except Exception as e:
    print('⚠️ 套用 reminder-check v9 view 覆寫失敗：', e)

# ========= LINE Bot 代辦事項 Patch v9 End =========


# ========= LINE Bot 待辦事項 Patch v10：用字修正（代辦 → 待辦） =========
# 使用方式：貼在目前 v9 patch 的最底部。
# 目的：
# 1. LINE 顯示文字統一改成「待辦 / 待辦事項」。
# 2. 指令支援新寫法：#新增待辦、#今日待辦、#完成待辦、#清除待辦、#設定待辦提醒。
# 3. 舊指令仍相容：#新增代辦、#今日代辦、#完成代辦、#清除代辦、#設定代辦提醒。
# 4. Firestore collection / 既有資料不改，避免資料搬移風險。


def _line_todo_wording_fix_text(value):
    """只修正使用者看得到的文字，不動資料庫欄位。"""
    if not isinstance(value, str):
        return value
    return value.replace('代辦事項', '待辦事項').replace('代辦', '待辦')


def _line_todo_normalize_command_wording(value):
    """讓使用者輸入「待辦」時，內部仍可走原本 v9 的「代辦」判斷。"""
    if not isinstance(value, str):
        return value
    return value.replace('待辦事項', '代辦事項').replace('待辦', '代辦')


# 1) LINE push 推播文字修正，例如早上 / 晚上固定提醒。
try:
    _push_line_text_before_v10_wording = push_line_text

    def push_line_text(to_id: str, text_message: str):
        return _push_line_text_before_v10_wording(to_id, _line_todo_wording_fix_text(text_message))
except Exception as e:
    print('⚠️ 套用 push_line_text 待辦用字修正失敗：', e)


# 2) reply_line_text 保險修正：如果有直接呼叫 reply_line_text，也會顯示待辦。
try:
    _reply_line_text_before_v10_wording = reply_line_text

    def reply_line_text(reply_token: str, text_message: str):
        return _reply_line_text_before_v10_wording(reply_token, _line_todo_wording_fix_text(text_message))
except Exception as e:
    print('⚠️ 套用 reply_line_text 待辦用字修正失敗：', e)


# 3) 使用者輸入 #新增待辦 / #今日待辦 時，轉成舊版可辨識的 #新增代辦；回覆再改回待辦。
try:
    _process_line_message_event_before_v10_wording = process_line_message_event

    def process_line_message_event(event):
        patched_event = event
        try:
            message = (event or {}).get('message') or {}
            if message.get('type') == 'text':
                raw_text = message.get('text') or ''
                normalized_text = _line_todo_normalize_command_wording(raw_text)
                if normalized_text != raw_text:
                    # 複製 event，避免影響原始 webhook 內容太多。
                    patched_event = dict(event)
                    patched_message = dict(message)
                    patched_message['text'] = normalized_text
                    patched_event['message'] = patched_message
        except Exception as e:
            print('⚠️ 待辦指令文字正規化失敗：', e)
            patched_event = event

        result = _process_line_message_event_before_v10_wording(patched_event)
        try:
            if isinstance(result, dict) and 'reply_text' in result:
                result = dict(result)
                result['reply_text'] = _line_todo_wording_fix_text(result.get('reply_text', ''))
                if 'parsed_tag' in result:
                    result['parsed_tag'] = _line_todo_wording_fix_text(result.get('parsed_tag', ''))
        except Exception as e:
            print('⚠️ 待辦回覆文字修正失敗：', e)
        return result
except Exception as e:
    print('⚠️ 套用 process_line_message_event 待辦用字修正失敗：', e)


# 4) reminder-check 的 JSON 回傳也修正顯示文字，方便瀏覽器測試時看到「待辦」。
try:
    _line_todos_reminder_check_before_v10_wording = app.view_functions.get('line_todos_reminder_check')

    def line_todos_reminder_check_v10_wording():
        response = _line_todos_reminder_check_before_v10_wording()
        try:
            # Flask view 可能回傳 (dict, status_code) 或 dict。
            if isinstance(response, tuple) and response and isinstance(response[0], dict):
                data = response[0]
                status = response[1] if len(response) > 1 else 200
                data_text = _line_todo_wording_fix_text(json.dumps(data, ensure_ascii=False))
                return json.loads(data_text), status
            if isinstance(response, dict):
                data_text = _line_todo_wording_fix_text(json.dumps(response, ensure_ascii=False))
                return json.loads(data_text)
        except Exception as e:
            print('⚠️ reminder-check 待辦用字修正失敗：', e)
        return response

    if _line_todos_reminder_check_before_v10_wording:
        app.view_functions['line_todos_reminder_check'] = line_todos_reminder_check_v10_wording
except Exception as e:
    print('⚠️ 套用 reminder-check 待辦用字修正失敗：', e)

# ========= LINE Bot 待辦事項 Patch v10 End =========
