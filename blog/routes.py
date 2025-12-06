from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from datetime import datetime
from firebase_admin import firestore

# 建立 Blueprint
blog_bp = Blueprint("blog", __name__, url_prefix="/blog")


# 取得 Firestore client（firebase_admin 在主程式已經 initialize 過）
def get_db():
    return firestore.client()


def doc_to_dict(doc):
    d = doc.to_dict() or {}
    d["id"] = doc.id
    return d


def get_all_categories():
    """從所有文章中蒐集已使用過的分類，給側邊欄 / 表單使用"""
    db = get_db()
    docs = db.collection("blog_posts").stream()
    cat_set = set()
    for d in docs:
        data = d.to_dict() or {}
        cats = data.get("categories") or []
        if isinstance(cats, list):
            for c in cats:
                c = (c or "").strip()
                if c:
                    cat_set.add(c)
    return sorted(cat_set)


# ========= 文章列表 =========
@blog_bp.route("/")
def blog_index():
    """文章列表 + 篩選 + 搜尋"""
    db = get_db()

    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    status = request.args.get("status", "").strip()
    sort_by = request.args.get("sort_by", "created_at_desc")

    docs = db.collection("blog_posts").stream()
    posts = [doc_to_dict(d) for d in docs]

    # 關鍵字搜尋（標題 / 內容 / 標籤 / 分類）
    if q:
        q_lower = q.lower()

        def match(p):
            cats = p.get("categories") or []
            cats_str = ", ".join(cats).lower()
            return (
                q_lower in (p.get("title", "").lower())
                or q_lower in (p.get("content_text", "").lower())
                or q_lower in (p.get("tags", "").lower())
                or q_lower in cats_str
            )

        posts = [p for p in posts if match(p)]

    # 分類篩選（多分類）
    if category:
        def in_cat(p):
            cats = p.get("categories") or []
            return category in cats

        posts = [p for p in posts if in_cat(p)]

    # 進度狀態篩選
    if status:
        posts = [p for p in posts if p.get("status") == status]

    # 排序
    if sort_by == "created_at_asc":
        reverse = False
    else:
        reverse = True
    posts.sort(key=lambda x: x.get("created_at", ""), reverse=reverse)

    # 分類 + 狀態列表（側邊欄 / 下拉用）
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
    )


# ========= 新增文章 =========
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
                "blog_form.html", post=form, mode="new", all_categories=all_categories
            )

        content_html = form.get("content", "").strip()
        status = form.get("status", "").strip()
        tags = form.get("tags", "").strip()
        project = form.get("project", "").strip()

        # 多分類：勾選 + 新增
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

        # 簡單文字版內容（給搜尋用）
        content_text = (
            content_html.replace("\r", " ")
            .replace("\n", " ")
            .replace("<br>", " ")
            .replace("<br/>", " ")
        )

        now = datetime.now().isoformat()
        user_id = session.get("user_id")
        user_name = session.get("user_name", "系統")

        doc_ref = db.collection("blog_posts").document()
        doc_ref.set(
            {
                "title": title,
                "content": content_html,
                "content_text": content_text,
                "categories": categories,
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
        )

        flash("已新增文章", "success")
        return redirect(url_for("blog.blog_index"))

    return render_template(
        "blog_form.html", post=None, mode="new", all_categories=all_categories
    )


# ========= 詳細頁 =========
@blog_bp.route("/<post_id>")
def blog_detail(post_id):
    db = get_db()
    doc = db.collection("blog_posts").document(post_id).get()
    if not doc.exists:
        flash("找不到這篇文章", "danger")
        return redirect(url_for("blog.blog_index"))

    post = doc_to_dict(doc)
    # 確保 categories 一定是 list
    if not isinstance(post.get("categories"), list):
        c = post.get("category") or ""
        post["categories"] = [c] if c else []

    return render_template("blog_detail.html", post=post)


# ========= 編輯文章 =========
@blog_bp.route("/<post_id>/edit", methods=["GET", "POST"])
def blog_edit(post_id):
    db = get_db()
    doc_ref = db.collection("blog_posts").document(post_id)
    doc = doc_ref.get()
    if not doc.exists:
        flash("找不到這篇文章", "danger")
        return redirect(url_for("blog.blog_index"))

    post = doc_to_dict(doc)
    if not isinstance(post.get("categories"), list):
        c = post.get("category") or ""
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
                "blog_form.html", post=post, mode="edit", all_categories=all_categories
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
            "title": title,
            "content": content_html,
            "content_text": content_text,
            "categories": categories,
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
        "blog_form.html", post=post, mode="edit", all_categories=all_categories
    )


# ========= 刪除文章 =========
@blog_bp.route("/<post_id>/delete", methods=["POST"])
def blog_delete(post_id):
    db = get_db()
    db.collection("blog_posts").document(post_id).delete()
    flash("已刪除文章", "info")
    return redirect(url_for("blog.blog_index"))

@blog_bp.route("/upload_image", methods=["POST"])
def upload_image():
    """接收本機圖片 → 上傳 Firebase Storage → 回傳 URL 給 TinyMCE"""

    from firebase_admin import storage
    import uuid

    file = request.files.get("file")
    if not file:
        return {"error": "沒有收到圖片"}, 400

    # Firebase Storage bucket
    bucket = storage.bucket()

    # 產生唯一檔名
    ext = file.filename.split(".")[-1].lower()
    blob_name = f"blog_images/{uuid.uuid4()}.{ext}"

    blob = bucket.blob(blob_name)
    blob.upload_from_file(file, content_type=file.content_type)

    # 設定公開讀取
    blob.make_public()

    return {"url": blob.public_url}
