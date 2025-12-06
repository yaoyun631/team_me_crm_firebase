from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, session, Response, Blueprint
)
import os
import json
from datetime import datetime
import csv
from io import StringIO

import firebase_admin
from firebase_admin import credentials, firestore
from werkzeug.security import generate_password_hash, check_password_hash
from blog import blog_bp

# ========= Firestore 初始化（Render + 本機皆可用） =========
def init_firestore():
    # 如果已初始化過，就直接回傳 client
    if firebase_admin._apps:
        return firestore.client()

    cred = None

    # 1️⃣ 先試著從環境變數讀（給 Render / 伺服器用）
    cred_json = os.environ.get("FIREBASE_CREDENTIALS")
    if cred_json:
        try:
            cred_dict = json.loads(cred_json)
            cred = credentials.Certificate(cred_dict)
            print("✅ 使用 FIREBASE_CREDENTIALS 初始化 Firestore")
        except Exception as e:
            print("⚠️ 解析 FIREBASE_CREDENTIALS 失敗：", e)

    # 2️⃣ 如果環境變數沒有或失敗，再退回用本機檔案（給你自己電腦用）
    if not cred and os.path.exists("serviceAccountKey.json"):
        cred = credentials.Certificate("serviceAccountKey.json")
        print("✅ 使用本機 serviceAccountKey.json 初始化 Firestore")

    # 3️⃣ 兩種都沒有，就丟錯
    if not cred:
        raise RuntimeError("找不到 Firestore 憑證：請設定環境變數 FIREBASE_CREDENTIALS 或放 serviceAccountKey.json")

    firebase_admin.initialize_app(cred)
    return firestore.client()


# 全域 Firestore client
db = init_firestore()




# ========= Flask 基本設定 =========
app = Flask(__name__)
app.secret_key = "team_me_super_secret"  # 可以自行修改

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

from blog import blog_bp
app.register_blueprint(blog_bp)

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
@app.route("/buyers")
@login_required
def buyers():
    q = request.args.get("q", "").strip()
    level = request.args.get("level", "").strip()
    intent_type = request.args.get("intent_type", "").strip()
    stage = request.args.get("stage", "").strip()  # ⬅ 進程：接觸/帶看/斡旋/成交
    sort_by = request.args.get("sort_by", "created_at_desc")

    docs = db.collection("buyers").stream()
    buyers_list = [doc_to_dict(d) for d in docs]

    # 關鍵字搜尋（姓名 / 電話）
    if q:
        buyers_list = [
            b for b in buyers_list
            if q in (b.get("name") or "") or q in (b.get("phone") or "")
        ]

    # 等級篩選
    if level:
        buyers_list = [b for b in buyers_list if b.get("level") == level]

    # 需求類型篩選（租/買/都可以）
    if intent_type:
        buyers_list = [b for b in buyers_list if b.get("intent_type") == intent_type]

    # 進程篩選
    if stage:
        buyers_list = [b for b in buyers_list if b.get("stage") == stage]

    # 排序
    reverse = True
    if sort_by == "created_at_asc":
        reverse = False
        key_func = lambda x: x.get("created_at", "")
    elif sort_by == "name_asc":
        reverse = False
        key_func = lambda x: x.get("name", "")
    elif sort_by == "name_desc":
        reverse = True
        key_func = lambda x: x.get("name", "")
    else:  # created_at_desc
        reverse = True
        key_func = lambda x: x.get("created_at", "")

    buyers_list.sort(key=key_func, reverse=reverse)

    return render_template(
        "buyers.html",
        buyers=buyers_list,
        q=q,
        level=level,
        intent_type=intent_type,
        stage=stage,          # ⬅ 記得一起丟給模板
        sort_by=sort_by,
    )


# ========= 新增買方 =========
@app.route("/buyers/new", methods=["POST"])
@login_required
def buyers_new():
    form = request.form

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

    if not name:
        flash("買方姓名必填", "danger")
        return redirect(url_for("buyers"))

    now = datetime.now().isoformat()

    db.collection("buyers").add(
        {
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
            "created_at": now,
            "created_by_id": session.get("user_id"),
            "created_by_name": session.get("user_name"),
        }
    )

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

        # ⭐ 如果你有必填欄位，可以在這裡檢查
        name = form.get("name", "").strip()
        if not name:
            flash("姓名為必填", "danger")
            # 重新渲染同一頁，帶回原本資料 + 表單內容
            # 這裡 buyer 也一起更新成使用者剛填的內容，避免重新打
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
            })
            return render_template("buyer_edit.html", buyer=buyer)

        # ✅ 正常更新資料
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
            "stage": form.get("stage", "").strip(),  # 接觸/帶看/斡旋/成交
            # 最後編輯資訊
            "updated_at": datetime.now().isoformat(),
            "updated_by_id": session.get("user_id"),
            "updated_by_name": session.get("user_name"),
        }

        doc_ref.update(updated)
        flash("已更新買方資料", "success")
        return redirect(url_for("buyer_detail", buyer_id=buyer_id))

    # GET：第一次進來編輯頁（還沒送出表單）
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
@app.route("/sellers")
@login_required
def sellers():
    q = request.args.get("q", "").strip()
    level = request.args.get("level", "").strip()
    stage = request.args.get("stage", "").strip()  # ⬅ 進程：開發中/委託中/成交
    sort_by = request.args.get("sort_by", "created_at_desc")

    docs = db.collection("sellers").stream()
    sellers_list = [doc_to_dict(d) for d in docs]

    if q:
        sellers_list = [
            s for s in sellers_list
            if q in (s.get("name") or "") or q in (s.get("phone") or "")
        ]

    if level:
        sellers_list = [s for s in sellers_list if s.get("level") == level]

    if stage:
        sellers_list = [s for s in sellers_list if s.get("stage") == stage]

    # 排序
    reverse = True
    if sort_by == "created_at_asc":
        reverse = False
        key_func = lambda x: x.get("created_at", "")
    elif sort_by == "name_asc":
        reverse = False
        key_func = lambda x: x.get("name", "")
    elif sort_by == "name_desc":
        reverse = True
        key_func = lambda x: x.get("name", "")
    else:  # created_at_desc
        reverse = True
        key_func = lambda x: x.get("created_at", "")

    sellers_list.sort(key=key_func, reverse=reverse)

    return render_template(
        "sellers.html",
        sellers=sellers_list,
        q=q,
        level=level,
        stage=stage,      # ⬅ 一樣丟給模板
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
    stage = form.get("stage", "").strip()  # ⬅ 進程
    reason = form.get("reason", "").strip()
    expected_price = form.get("expected_price", "").strip()
    min_price = form.get("min_price", "").strip()
    timeline = form.get("timeline", "").strip()
    occupancy_status = form.get("occupancy_status", "").strip()
    contract_end_date = form.get("contract_end_date", "").strip()  # ⬅ 新：委託到期日
    note = form.get("note", "").strip()

    if not name:
        flash("賣方姓名必填", "danger")
        return redirect(url_for("sellers"))

    now = datetime.now().isoformat()

    db.collection("sellers").add(
        {
            "name": name,
            "phone": phone,
            "email": email,
            "line_id": line_id,
            "address": address,
            "property_type": property_type,
            "level": level,
            "stage": stage,  # ⬅ 進程
            "reason": reason,
            "expected_price": expected_price,
            "min_price": min_price,
            "timeline": timeline,
            "occupancy_status": occupancy_status,
            "contract_end_date": contract_end_date,  # ⬅ 新：委託到期日
            "note": note,
            "created_at": now,
            "created_by_id": session.get("user_id"),
            "created_by_name": session.get("user_name"),
        }
    )

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

        updated = {
            "name": form.get("name", "").strip(),
            "phone": form.get("phone", "").strip(),
            "email": form.get("email", "").strip(),
            "line_id": form.get("line_id", "").strip(),
            "address": form.get("address", "").strip(),
            "property_type": form.get("property_type", "").strip(),
            "level": form.get("level", "").strip(),
            "stage": form.get("stage", "").strip(),  # ⬅ 進程
            "reason": form.get("reason", "").strip(),
            "expected_price": form.get("expected_price", "").strip(),
            "min_price": form.get("min_price", "").strip(),
            "timeline": form.get("timeline", "").strip(),
            "occupancy_status": form.get("occupancy_status", "").strip(),
            "contract_end_date": form.get("contract_end_date", "").strip(),  # ⬅ 委託到期日
            "note": form.get("note", "").strip(),
            "updated_at": datetime.now().isoformat(),
            "updated_by_id": session.get("user_id"),
            "updated_by_name": session.get("user_name"),
        }

        doc_ref.update(updated)
        flash("已更新賣方資料", "success")
        return redirect(url_for("seller_detail", seller_id=seller_id))

    return render_template("seller_edit.html", seller=seller)



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
