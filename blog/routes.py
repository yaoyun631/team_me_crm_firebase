\
from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    current_app,
    abort,
    send_file,
)
from datetime import datetime
from firebase_admin import firestore, storage
from io import BytesIO
import os
import uuid


blog_bp = Blueprint("blog", __name__, url_prefix="/blog")

BLOG_COLLECTION = "blog_posts"
WORKSPACE_DEFAULT_ID = os.environ.get("WORKSPACE_DEFAULT_ID", "team_me").strip() or "team_me"
WORKSPACE_MEMBER_COLLECTION = os.environ.get("WORKSPACE_MEMBER_COLLECTION", "workspace_members")


# =============================================================================
# Workspace / 權限
# =============================================================================
def get_raw_db():
    """取得 Firebase 原生 client，僅供 Workspace membership 等系統資料查詢。"""
    try:
        raw = current_app.extensions.get("workspace_firestore_raw_db")
        if raw is not None:
            return raw
    except Exception:
        pass
    return firestore.client()


def get_db():
    """
    優先使用主程式提供的 Workspace-aware Firestore client。
    若本 Blueprint 被單獨測試，則退回 Firebase 原生 client；下方 route 本身仍會做 Workspace 檢查。
    """
    try:
        scoped = current_app.extensions.get("workspace_firestore_db")
        if scoped is not None:
            return scoped
    except Exception:
        pass
    return firestore.client()


def current_workspace_id():
    return (session.get("workspace_id") or "").strip()


def current_workspace_name():
    name = (session.get("workspace_name") or "").strip()
    if name:
        return name
    wid = current_workspace_id()
    if not wid:
        return ""
    try:
        snap = get_raw_db().collection("workspaces").document(wid).get()
        if snap.exists:
            return ((snap.to_dict() or {}).get("name") or wid).strip()
    except Exception:
        pass
    return wid


def _find_workspace_membership(workspace_id, user_id):
    workspace_id = (workspace_id or "").strip()
    user_id = (user_id or "").strip()
    if not workspace_id or not user_id:
        return {}
    try:
        docs = (
            get_raw_db()
            .collection(WORKSPACE_MEMBER_COLLECTION)
            .where("workspace_id", "==", workspace_id)
            .where("user_id", "==", user_id)
            .limit(2)
            .stream()
        )
        for snap in docs:
            data = snap.to_dict() or {}
            data["id"] = snap.id
            return data
    except Exception as exc:
        print("⚠️ Blog Workspace membership 查詢失敗：", exc)
    return {}


def _blog_user_can_access():
    uid = (session.get("user_id") or "").strip()
    wid = current_workspace_id()
    if not uid or not wid:
        return False
    membership = _find_workspace_membership(wid, uid)
    if not membership or membership.get("active", True) is False:
        return False
    role = (membership.get("role") or "member").strip()
    if role in ("owner", "admin"):
        return True
    modules = set(membership.get("modules") or [])
    return "all" in modules or "blog" in modules


@blog_bp.before_request
def blog_workspace_guard():
    """部落格是團隊資料：必須登入、已選 Workspace，而且該 Workspace 有 blog 權限。"""
    if not session.get("user_id"):
        flash("請先登入後再查看部落格。", "warning")
        return redirect(url_for("login"))
    if not current_workspace_id():
        flash("請先選擇工作區。", "warning")
        try:
            return redirect(url_for("workspace_home"))
        except Exception:
            return redirect("/workspace")
    if not _blog_user_can_access():
        flash("你在目前工作區沒有部落格使用權限。", "warning")
        abort(403)
    return None


def _workspace_matches(data):
    """
    舊文章沒有 workspace_id 時，只屬於預設厝米 Workspace；
    新文章必須 workspace_id == 目前 Workspace。
    """
    data = data or {}
    wid = current_workspace_id()
    doc_wid = (data.get("workspace_id") or "").strip()
    if not doc_wid:
        return wid == WORKSPACE_DEFAULT_ID
    return doc_wid == wid


def _workspace_payload():
    return {
        "workspace_id": current_workspace_id(),
        "workspace_name": current_workspace_name(),
    }


def doc_to_dict(doc):
    d = doc.to_dict() or {}
    d["id"] = doc.id
    return d


def _get_visible_post_snapshot(post_id):
    """詳細 / 編輯 / 刪除共用，避免直接輸入 post_id 跨 Workspace。"""
    if not post_id:
        return None
    try:
        doc = get_db().collection(BLOG_COLLECTION).document(post_id).get()
        if not doc.exists:
            return None
        data = doc.to_dict() or {}
        if not _workspace_matches(data):
            return None
        return doc
    except Exception as exc:
        print("⚠️ Blog 文章讀取失敗：", exc)
        return None


# =============================================================================
# 分類
# =============================================================================
def get_all_categories():
    """只從目前 Workspace 的文章蒐集分類。"""
    db = get_db()
    docs = db.collection(BLOG_COLLECTION).stream()
    cat_set = set()

    for d in docs:
        data = d.to_dict() or {}
        if not _workspace_matches(data):
            continue

        cats = data.get("categories")
        if isinstance(cats, list):
            for c in cats:
                c = (c or "").strip()
                if c:
                    cat_set.add(c)
        else:
            c = (data.get("category") or "").strip()
            if c:
                cat_set.add(c)

    return sorted(cat_set)


# =============================================================================
# 文章列表
# =============================================================================
@blog_bp.route("/")
def blog_index():
    db = get_db()

    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    status = request.args.get("status", "").strip()
    sort_by = request.args.get("sort_by", "created_at_desc")

    docs = db.collection(BLOG_COLLECTION).stream()
    posts = []
    for d in docs:
        p = doc_to_dict(d)
        if not _workspace_matches(p):
            continue
        posts.append(p)

    # 舊欄位 category -> categories list（只在程式裡轉換）
    for p in posts:
        if not isinstance(p.get("categories"), list):
            c = (p.get("category") or "").strip()
            p["categories"] = [c] if c else []

    if q:
        q_lower = q.lower()

        def match(p):
            cats_str = ", ".join(p.get("categories", [])).lower()
            return (
                q_lower in (p.get("title", "").lower())
                or q_lower in (p.get("content_text", "").lower())
                or q_lower in (p.get("tags", "").lower())
                or q_lower in cats_str
            )

        posts = [p for p in posts if match(p)]

    if category:
        posts = [p for p in posts if category in (p.get("categories") or [])]

    if status:
        posts = [p for p in posts if p.get("status") == status]

    reverse = sort_by != "created_at_asc"
    posts.sort(key=lambda x: x.get("created_at", ""), reverse=reverse)

    all_categories = get_all_categories()
    all_statuses = sorted({p.get("status") for p in posts if p.get("status")})

    return render_template(
        "blog_list.html",
        posts=posts,
        q=q,
        category=category,
        status=status,
        sort_by=sort_by,
        all_categories=all_categories,
        all_statuses=all_statuses,
        current_workspace_id=current_workspace_id(),
        current_workspace_name=current_workspace_name(),
    )


# =============================================================================
# 新增文章
# =============================================================================
@blog_bp.route("/new", methods=["GET", "POST"])
def blog_new():
    db = get_db()
    all_categories = get_all_categories()

    if request.method == "POST":
        form = request.form
        title = form.get("title", "").strip()
        if not title:
            flash("標題為必填", "danger")
            return render_template(
                "blog_form.html",
                post=form,
                mode="new",
                all_categories=all_categories,
            )

        content_html = form.get("content", "").strip()
        status = form.get("status", "").strip()
        tags = form.get("tags", "").strip()
        project = form.get("project", "").strip()

        selected_categories = form.getlist("categories")
        new_categories_str = form.get("new_categories", "").strip()
        if new_categories_str:
            extra = [c.strip() for c in new_categories_str.split(",") if c.strip()]
            selected_categories.extend(extra)

        categories = []
        for c in selected_categories:
            c = c.strip()
            if c and c not in categories:
                categories.append(c)

        primary_category = categories[0] if categories else ""
        content_text = (
            content_html.replace("\r", " ")
            .replace("\n", " ")
            .replace("<br>", " ")
            .replace("<br/>", " ")
        )

        now = datetime.now().isoformat()
        user_id = session.get("user_id")
        user_name = session.get("user_name", "系統")

        payload = {
            **_workspace_payload(),
            "title": title,
            "content": content_html,
            "content_text": content_text,
            "categories": categories,
            "category": primary_category,
            "status": status,
            "project": project,
            "tags": tags,
            "created_at": now,
            "created_by_id": user_id,
            "created_by_name": user_name,
            "updated_at": now,
            "updated_by_id": user_id,
            "updated_by_name": user_name,
        }

        db.collection(BLOG_COLLECTION).add(payload)
        flash(f"已新增文章（{current_workspace_name()}）", "success")
        return redirect(url_for("blog.blog_index"))

    return render_template(
        "blog_form.html",
        post=None,
        mode="new",
        all_categories=all_categories,
    )


# =============================================================================
# 詳細頁
# =============================================================================
@blog_bp.route("/<post_id>")
def blog_detail(post_id):
    doc = _get_visible_post_snapshot(post_id)
    if not doc:
        flash("找不到這篇文章，或文章不屬於目前工作區。", "danger")
        return redirect(url_for("blog.blog_index"))

    post = doc_to_dict(doc)
    if not isinstance(post.get("categories"), list):
        c = (post.get("category") or "").strip()
        post["categories"] = [c] if c else []

    return render_template("blog_detail.html", post=post)


# =============================================================================
# 編輯文章
# =============================================================================
@blog_bp.route("/<post_id>/edit", methods=["GET", "POST"])
def blog_edit(post_id):
    db = get_db()
    doc = _get_visible_post_snapshot(post_id)
    if not doc:
        flash("找不到這篇文章，或文章不屬於目前工作區。", "danger")
        return redirect(url_for("blog.blog_index"))

    doc_ref = db.collection(BLOG_COLLECTION).document(post_id)
    post = doc_to_dict(doc)

    if not isinstance(post.get("categories"), list):
        c = (post.get("category") or "").strip()
        post["categories"] = [c] if c else []

    all_categories = get_all_categories()

    if request.method == "POST":
        form = request.form
        title = form.get("title", "").strip()
        if not title:
            flash("標題為必填", "danger")
            post.update(
                {
                    "title": title,
                    "content": form.get("content", ""),
                    "status": form.get("status", ""),
                    "tags": form.get("tags", ""),
                    "project": form.get("project", ""),
                    "categories": form.getlist("categories"),
                }
            )
            return render_template(
                "blog_form.html",
                post=post,
                mode="edit",
                all_categories=all_categories,
            )

        content_html = form.get("content", "").strip()
        status = form.get("status", "").strip()
        tags = form.get("tags", "").strip()
        project = form.get("project", "").strip()

        selected_categories = form.getlist("categories")
        new_categories_str = form.get("new_categories", "").strip()
        if new_categories_str:
            extra = [c.strip() for c in new_categories_str.split(",") if c.strip()]
            selected_categories.extend(extra)

        categories = []
        for c in selected_categories:
            c = c.strip()
            if c and c not in categories:
                categories.append(c)

        primary_category = categories[0] if categories else ""
        content_text = (
            content_html.replace("\r", " ")
            .replace("\n", " ")
            .replace("<br>", " ")
            .replace("<br/>", " ")
        )

        now = datetime.now().isoformat()
        user_id = session.get("user_id")
        user_name = session.get("user_name", "系統")

        updated = {
            **_workspace_payload(),
            "title": title,
            "content": content_html,
            "content_text": content_text,
            "categories": categories,
            "category": primary_category,
            "status": status,
            "tags": tags,
            "project": project,
            "updated_at": now,
            "updated_by_id": user_id,
            "updated_by_name": user_name,
        }

        doc_ref.update(updated)
        flash("已更新文章", "success")
        return redirect(url_for("blog.blog_detail", post_id=post_id))

    return render_template(
        "blog_form.html",
        post=post,
        mode="edit",
        all_categories=all_categories,
    )


# =============================================================================
# 刪除文章
# =============================================================================
@blog_bp.route("/<post_id>/delete", methods=["POST"])
def blog_delete(post_id):
    db = get_db()
    doc = _get_visible_post_snapshot(post_id)
    if not doc:
        flash("找不到這篇文章，或文章不屬於目前工作區。", "danger")
        return redirect(url_for("blog.blog_index"))

    db.collection(BLOG_COLLECTION).document(post_id).delete()
    flash("已刪除文章", "info")
    return redirect(url_for("blog.blog_index"))


# =============================================================================
# 圖片：新上傳圖片改存 Workspace 子目錄，並由登入權限路由讀取
# 舊文章原本的公開圖片 URL 不會受影響。
# =============================================================================
@blog_bp.route("/upload_image", methods=["POST"])
def upload_image():
    file = request.files.get("file")
    if not file:
        return {"error": "沒有收到圖片"}, 400

    wid = current_workspace_id()
    if not wid:
        return {"error": "尚未選擇工作區"}, 400

    original_name = file.filename or "image.jpg"
    ext = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else "jpg"
    allowed_ext = {"jpg", "jpeg", "png", "gif", "webp"}
    if ext not in allowed_ext:
        return {"error": "僅支援 JPG / PNG / GIF / WEBP 圖片"}, 400

    object_name = f"{uuid.uuid4()}.{ext}"
    blob_path = f"blog_images/{wid}/{object_name}"

    bucket = storage.bucket()
    blob = bucket.blob(blob_path)
    blob.upload_from_string(file.read(), content_type=file.content_type)

    # 不再 make_public；由下面的受保護路由讀取。
    image_url = url_for("blog.blog_image", filename=object_name)
    return {"url": image_url}


@blog_bp.route("/image/<filename>")
def blog_image(filename):
    # filename 只允許 basename，防止使用 ../ 或自行指定其他 Workspace 路徑。
    safe_name = os.path.basename(filename or "")
    if not safe_name or safe_name != filename:
        abort(404)

    wid = current_workspace_id()
    blob_path = f"blog_images/{wid}/{safe_name}"
    bucket = storage.bucket()
    blob = bucket.blob(blob_path)

    try:
        if not blob.exists():
            abort(404)
        data = blob.download_as_bytes()
        content_type = blob.content_type or "application/octet-stream"
        return send_file(BytesIO(data), mimetype=content_type, max_age=3600)
    except Exception as exc:
        print("⚠️ Blog Workspace 圖片讀取失敗：", exc)
        abort(404)
