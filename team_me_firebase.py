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
app.secret_key = "team_me_super_secret"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

from blog import blog_bp
app.register_blueprint(blog_bp)

# 限制單一請求最大 5MB（可依需求調整）
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024


# ========= LINE Webhook / 分類設定 =========
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "").strip()
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()

DEFAULT_LABEL_OPTIONS = [
    "開發紀錄",
    "掃街",
    "待盤點客戶",
    "售-客戶需求",
    "租-客戶需求",
    "廣告",
    "影片待剪/排程",
    "影片上架",
    "LINE紀錄",
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


def normalize_line_key(key: str) -> str:
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
        "來源": "source",
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
    }
    return mapping.get(k, k)


def parse_line_formatted_message(text: str):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None

    first = lines[0]
    if not first.startswith("#"):
        return None

    tag = first.lstrip("#").strip()
    tag_map = {
        "買方追蹤": "buyer_followup",
        "賣方追蹤": "seller_followup",
        "客戶分類": "classify",
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

    if action in ("buyer_followup", "seller_followup", "classify"):
        if not (fields.get("record_id") or fields.get("phone") or fields.get("name")):
            return None

    return {
        "tag": tag,
        "action": action,
        "fields": fields,
        "raw_text": text,
    }


def find_customer_record(target_type: str, record_id: str = "", phone: str = "", name: str = ""):
    collection_name = "buyers" if target_type == "buyer" else "sellers"

    if record_id:
        doc = db.collection(collection_name).document(record_id).get()
        if doc.exists:
            return doc

    normalized_phone = normalize_phone(phone)
    if normalized_phone:
        matches = []
        for doc in db.collection(collection_name).stream():
            data = doc.to_dict() or {}
            if normalize_phone(data.get("phone", "")) == normalized_phone:
                matches.append(doc)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return None

    if name:
        docs = list(db.collection(collection_name).where("name", "==", name.strip()).limit(2).stream())
        if len(docs) == 1:
            return docs[0]

    return None


def update_customer_note_and_labels(target_type: str, doc_ref, content: str, labels=None, stage="", source="LINE"):
    labels = dedupe_keep_order(["LINE紀錄"] + ensure_list(labels))
    snapshot = doc_ref.get()
    current = snapshot.to_dict() or {}
    old_note = current.get("note", "")

    updates = {
        "note": append_note_block(old_note, content, "LINE"),
        "labels": firestore.ArrayUnion(labels),
        "updated_at": datetime.now().isoformat(),
        "updated_by_id": "line_bot",
        "updated_by_name": "LINE Bot",
    }
    if stage:
        updates["stage"] = stage
    if source and not current.get("source"):
        updates["source"] = source

    doc_ref.update(updates)


def add_customer_followup(target_type: str, customer_id: str, content: str, next_action="", next_contact_date="", labels=None, line_event=None):
    collection_name = "buyer_followups" if target_type == "buyer" else "seller_followups"
    key_name = "buyer_id" if target_type == "buyer" else "seller_id"

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
    }

    if line_event:
        source = line_event.get("source", {})
        data["line_group_id"] = source.get("groupId", "")
        data["line_room_id"] = source.get("roomId", "")
        data["line_user_id"] = source.get("userId", "")

    db.collection(collection_name).add(data)


def save_line_log(parsed, event, status, target_type="", target_id="", note=""):
    source = event.get("source", {})
    db.collection("line_logs").add({
        "tag": parsed.get("tag", ""),
        "action": parsed.get("action", ""),
        "fields": parsed.get("fields", {}),
        "raw_text": parsed.get("raw_text", ""),
        "status": status,
        "target_type": target_type,
        "target_id": target_id,
        "note": note,
        "line_group_id": source.get("groupId", ""),
        "line_room_id": source.get("roomId", ""),
        "line_user_id": source.get("userId", ""),
        "message_id": (event.get("message") or {}).get("id", ""),
        "webhook_event_id": event.get("webhookEventId", ""),
        "created_at": datetime.now().isoformat(),
    })


def process_line_message_event(event):
    message = event.get("message") or {}
    if message.get("type") != "text":
        return False, "不是文字訊息"

    parsed = parse_line_formatted_message(message.get("text", ""))
    if not parsed:
        return False, "格式不符合"

    fields = parsed["fields"]
    action = parsed["action"]

    if action == "generic_note":
        save_line_log(parsed, event, "ignored", note="generic_note 暫未綁定資料")
        return True, "已收到紀錄"

    target_type = fields.get("target_type", "")
    if action == "buyer_followup":
        target_type = "buyer"
    elif action == "seller_followup":
        target_type = "seller"

    if target_type not in ("buyer", "seller"):
        save_line_log(parsed, event, "failed", note="缺少 target_type")
        return False, "請提供對象：買方 或 賣方"

    doc = find_customer_record(
        target_type=target_type,
        record_id=fields.get("record_id", ""),
        phone=fields.get("phone", ""),
        name=fields.get("name", ""),
    )
    if not doc:
        save_line_log(parsed, event, "failed", target_type=target_type, note="找不到唯一客戶")
        return False, "找不到唯一客戶，請補客戶ID或正確電話"

    doc_ref = db.collection("buyers" if target_type == "buyer" else "sellers").document(doc.id)
    labels = dedupe_keep_order(["LINE紀錄"] + ensure_list(fields.get("labels")))

    summary_parts = []
    if fields.get("content"):
        summary_parts.append(fields["content"])
    if fields.get("address"):
        summary_parts.append(f"地址/物件：{fields['address']}")
    if fields.get("price"):
        summary_parts.append(f"價格：{fields['price']}")
    summary_text = "；".join(summary_parts).strip() or "LINE 更新"

    update_customer_note_and_labels(
        target_type=target_type,
        doc_ref=doc_ref,
        content=summary_text,
        labels=labels,
        stage=fields.get("stage", ""),
        source=fields.get("source", "LINE"),
    )

    if action in ("buyer_followup", "seller_followup"):
        add_customer_followup(
            target_type=target_type,
            customer_id=doc.id,
            content=summary_text,
            next_action=fields.get("next_action", ""),
            next_contact_date=fields.get("next_contact_date", ""),
            labels=labels,
            line_event=event,
        )

    save_line_log(parsed, event, "success", target_type=target_type, target_id=doc.id)
    return True, f"已寫入{'買方' if target_type == 'buyer' else '賣方'}：{(doc.to_dict() or {}).get('name', '')}"


def build_label_options(*doc_lists):
    label_set = set(DEFAULT_LABEL_OPTIONS)
    for docs in doc_lists:
        for item in docs:
            for label in ensure_list(item.get("labels")):
                label_set.add(label)
    return sorted(label_set)







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
        buyers_list = [b for b in buyers_list if b.get("stage") == stage]

    if source:
        buyers_list = [b for b in buyers_list if (b.get("source") or "") == source]

    if label:
        buyers_list = [b for b in buyers_list if label in ensure_list(b.get("labels"))]

    def parse_created_at(b):
        v = b.get("created_at")
        if not v:
            return ""
        return v

    if sort_by == "created_at_asc":
        buyers_list.sort(key=parse_created_at)
    elif sort_by == "created_at_desc":
        buyers_list.sort(key=parse_created_at, reverse=True)
    elif sort_by == "name_asc":
        buyers_list.sort(key=lambda b: (b.get("name") or ""))
    elif sort_by == "name_desc":
        buyers_list.sort(key=lambda b: (b.get("name") or ""), reverse=True)

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
        sellers_list = [s for s in sellers_list if s.get("stage") == stage]

    if source:
        sellers_list = [s for s in sellers_list if (s.get("source") or "") == source]

    if label:
        sellers_list = [s for s in sellers_list if label in ensure_list(s.get("labels"))]

    def parse_created_at(s):
        v = s.get("created_at")
        if not v:
            return ""
        return v

    if sort_by == "created_at_asc":
        sellers_list.sort(key=parse_created_at)
    elif sort_by == "created_at_desc":
        sellers_list.sort(key=parse_created_at, reverse=True)
    elif sort_by == "name_asc":
        sellers_list.sort(key=lambda s: (s.get("name") or ""))
    elif sort_by == "name_desc":
        sellers_list.sort(key=lambda s: (s.get("name") or ""), reverse=True)

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
    app.logger.warning("LINE webhook hit")
    app.logger.warning("Headers: %s", dict(request.headers))
    app.logger.warning("Body: %s", request.get_data(as_text=True))
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


if __name__ == "__main__":
    app.run(debug=True)
