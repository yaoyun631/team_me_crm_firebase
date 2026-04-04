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
    url_for, flash, session, Response, Blueprint
)
import os
import json
from datetime import datetime
import csv
from io import StringIO, BytesIO
import hmac
import hashlib
import base64
import re
from uuid import uuid4
from PIL import Image

import firebase_admin
from firebase_admin import credentials, firestore, storage  
from werkzeug.security import generate_password_hash, check_password_hash
from urllib.parse import urlparse, unquote
import time
from werkzeug.utils import secure_filename

print("Working directory:", os.getcwd())

# ========= Firestore + Storage 初始化（Render + 本機皆可用） =========
def init_firebase():
    """
    初始化 Firebase：
    - Firestore
    - Storage（bucket: team-me-98acf.firebasestorage.app）
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

    # 2️⃣ 本機：讀 serviceAccountKey.json
    if not cred and os.path.exists("serviceAccountKey.json"):
        cred = credentials.Certificate("serviceAccountKey.json")
        print("✅ 使用本機 serviceAccountKey.json 初始化 Firebase")

    if not cred:
        raise RuntimeError("找不到 Firebase 憑證：請設定 FIREBASE_CREDENTIALS 或放 serviceAccountKey.json")

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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
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
        "created_at": datetime.now().isoformat(),
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
        "updated_at": datetime.now().isoformat(),
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
        "contact_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "channel": "LINE",
        "content": content,
        "next_action": next_action,
        "next_contact_date": next_contact_date,
        "labels": dedupe_keep_order(["LINE紀錄"] + ensure_list(labels)),
        "created_at": datetime.now().isoformat(),
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
        "created_at": datetime.now().isoformat(),
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
        "updated_at": datetime.now().isoformat(),
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

    now = datetime.now().isoformat()
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
        "updated_at": datetime.now().isoformat(),
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

    now = datetime.now().isoformat()
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
    if target_type not in ("buyer", "seller", "development") or not target_id:
        return {"handled": False}

    if target_type == "buyer":
        collection_name = "buyers"
        label_text = "客需"
    elif target_type == "seller":
        collection_name = "sellers"
        label_text = "委託"
    else:
        collection_name = "developments"
        label_text = "開發"

    doc_ref = db.collection(collection_name).document(target_id)
    doc = doc_ref.get()
    if not doc.exists:
        return {"handled": True, "ok": False, "reply_text": "未寫入：引用的資料不存在"}

    labels = dedupe_keep_order(["LINE紀錄", "群組回覆註記"])
    reply_only_text = raw_text

    update_kwargs = {
        "target_type": target_type,
        "doc_ref": doc_ref,
        "content": reply_only_text,
        "labels": labels,
        "source": "LINE",
        "event": event,
    }
    if target_type == "development":
        update_kwargs["stage"] = ""
        update_kwargs["registered_address"] = ""
        update_kwargs["extra_updates"] = {}

    update_customer_note_and_labels(**update_kwargs)
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
    save_line_log(
        parsed,
        event,
        "success",
        target_type=target_type,
        target_id=target_id,
        sender_display_name=get_line_sender_display_name(event),
    )
    data = doc.to_dict() or {}
    return {
        "handled": True,
        "ok": True,
        "reply_text": f"已註記{label_text}：{data.get('name', '')}",
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

    now = datetime.now().isoformat()

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
        contact_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    now = datetime.now().isoformat()

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
            "updated_at": datetime.now().isoformat(),
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
            contact_time = datetime.now().strftime("%Y-%m-%d %H:%M")

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

    now = datetime.now().isoformat()

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
        contact_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    now = datetime.now().isoformat()

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
            "updated_at": datetime.now().isoformat(),
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
            contact_time = datetime.now().strftime("%Y-%m-%d %H:%M")

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
            "created_at": datetime.now().isoformat(),
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
        "updated_at": datetime.now().isoformat(),
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
        "contact_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "channel": "LINE",
        "content": content,
        "next_action": next_action,
        "next_contact_date": next_contact_date,
        "labels": dedupe_keep_order(["LINE紀錄"] + ensure_list(labels)),
        "created_at": datetime.now().isoformat(),
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
        "record_date": fields.get("record_date", "").strip() or datetime.now().strftime("%Y-%m-%d"),
        "note": "",
        "labels": labels,
        "updated_at": datetime.now().isoformat(),
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

    now = datetime.now().isoformat()
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
        "updated_at": datetime.now().isoformat(),
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
        "contact_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "channel": channel,
        "content": content,
        "next_action": next_action,
        "next_contact_date": next_contact_date,
        "labels": dedupe_keep_order(["LINE紀錄"] + ensure_list(labels)),
        "created_at": datetime.now().isoformat(),
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
        "record_date": fields.get("record_date", "").strip() or datetime.now().strftime("%Y-%m-%d"),
        "note": "",
        "labels": labels,
        "updated_at": datetime.now().isoformat(),
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

    now = datetime.now().isoformat()
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
    items = [doc_to_dict(d) for d in docs]
    source_options = sorted({(x.get("source") or "").strip() for x in items if (x.get("source") or "").strip()})

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
        "record_date": datetime.now().strftime("%Y-%m-%d"),
        "created_at": datetime.now().isoformat(),
        "created_by_id": session.get("user_id"),
        "created_by_name": session.get("user_name"),
        "updated_at": datetime.now().isoformat(),
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
            "contact_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "channel": "手動新增",
            "current_stage": current_stage,
            "stage": current_stage,
            "next_action": next_action,
            "next_action_date": next_action_date,
            "registered_address": data["registered_address"],
            "content": data["note"],
            "next_contact_date": next_action_date,
            "created_at": datetime.now().isoformat(),
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
        "updated_at": datetime.now().isoformat(),
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
    contact_time = request.form.get("contact_time", "").strip() or datetime.now().strftime("%Y-%m-%d %H:%M")
    channel = request.form.get("channel", "").strip()
    current_stage = normalize_development_status(request.form.get("current_stage", "").strip() or request.form.get("stage", "").strip())
    next_action = normalize_development_next_action(request.form.get("next_action", "").strip())
    next_action_date = request.form.get("next_action_date", "").strip() or request.form.get("next_contact_date", "").strip()
    registered_address = request.form.get("registered_address", "").strip()
    content = request.form.get("content", "").strip()
    note_extra = request.form.get("note", "").strip()
    if note_extra:
        content = (content + ("\n" if content else "") + note_extra).strip()

    now = datetime.now().isoformat()
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
            "updated_at": datetime.now().isoformat(),
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
        contact_time = request.form.get("contact_time", "").strip() or datetime.now().strftime("%Y-%m-%d %H:%M")
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
            "updated_at": datetime.now().isoformat(),
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



if __name__ == "__main__":
    app.run(debug=True)


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
        year = datetime.now().year
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
        fields['record_date'] = datetime.now().strftime('%Y-%m-%d')

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
        "record_date": fields.get("record_date", "").strip() or datetime.now().strftime("%Y-%m-%d"),
        "note": "",
        "labels": labels,
        "updated_at": datetime.now().isoformat(),
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

    now = datetime.now().isoformat()
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
        "updated_at": datetime.now().isoformat(),
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
