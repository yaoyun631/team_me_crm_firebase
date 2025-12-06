from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from datetime import datetime
from firebase_admin import firestore

blog_bp = Blueprint("blog", __name__, url_prefix="/blog")


# ========= Firestore 取用 =========
def get_db():
    return firestore.client()


def doc_to_dict(doc):
    d = doc.to_dict() or {}
    d["id"] = doc.id
    return d


def get_all_categories():
    """
    從所有文章中蒐集分類。
    同時支援舊欄位 category（字串）與新欄位 categories（list）。
    """
    db = get_db()
    docs = db.collection("blog_posts").stream()
    cat_set = set()

    for d in docs:
        data = d.to_dict() or {}

        # 新版：list
        cats = data.get("categories")
        if isinstance(cats, list):
            for c in cats:
                c = (c or "").strip()
                if c:
                    cat_set.add(c)
        else:
            # 舊版：單一欄位 category
            c = (data.get("category") or "").strip()
            if c:
                cat_set.add(c)

    return sorted(cat_set)


# ========= 文章列表 =========
@blog_bp.route("/")
def blog_index():
    db = get_db()

    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    status = request.args.get("status", "").strip()
    sort_by = request.args.get("sort_by", "created_at_desc")

    docs = db.collection("blog_posts").stream()
    posts = [doc_to_dict(d) for d in docs]

    # 先把舊欄位 category 轉成 categories list（只在程式裡用，不動資料庫）
    for p in posts:
        if not isinstance(p.get("categories"), list):
            c = (p.get("category") or "").strip()
            p["categories"] = [c] if c else []

    # 🔍 關鍵字搜尋（標題 / 內容 / 標籤 / 分類）
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

    # 🔍 分類篩選（多分類：有其中一個就算）
    if category:
        posts = [
            p for p in posts
            if category in (p.get("categories") or [])
        ]

    # 🔍 進度狀態篩選
    if status:
        posts = [p for p in posts if p.get("status") == status]

    # 排序
    if sort_by == "created_at_asc":
        reverse = False
    else:
        reverse = True

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
                "blog_form.html",
                post=form,
                mode="new",
                all_categories=all_categories,
            )

        content_html = form.get("content", "").strip()
        status = form.get("status", "").strip()
        tags = form.get("tags", "").strip()
        project = form.get("project", "").strip()

        # ✅ 已勾選的分類（右側 checkbox name="categories"）
        selected_categories = form.getlist("categories")

        # ✅ 新增分類（輸入框 name="new_categories"，逗號分隔）
        new_categories_str = form.get("new_categories", "").strip()
        if new_categories_str:
            extra = [c.strip() for c in new_categories_str.split(",") if c.strip()]
            selected_categories.extend(extra)

        # 去除重複 & 空白
        categories = []
        for c in selected_categories:
            c = c.strip()
            if c and c not in categories:
                categories.append(c)

        # 舊欄位：仍保留 primary category，方便之後需要
        primary_category = categories[0] if categories else ""

        # 純文字版內容（給搜尋用）
        content_text = (
            content_html.replace("\r", " ")
            .replace("\n", " ")
            .replace("<br>", " ")
            .replace("<br/>", " ")
        )

        now = datetime.now().isoformat()
        user_id = session.get("user_id")
        user_name = session.get("user_name", "系統")

        db.collection("blog_posts").add(
            {
                "title": title,
                "content": content_html,
                "content_text": content_text,
                "categories": categories,        # ⭐ 新欄位：list
                "category": primary_category,    # ⭐ 舊欄位：單一字串（兼容）
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
        "blog_form.html",
        post=None,
        mode="new",
        all_categories=all_categories,
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

    # 確保 categories 是 list
    if not isinstance(post.get("categories"), list):
        c = (post.get("category") or "").strip()
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

    # 確保 categories 是 list
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


# ========= 刪除文章 =========
@blog_bp.route("/<post_id>/delete", methods=["POST"])
def blog_delete(post_id):
    db = get_db()
    db.collection("blog_posts").document(post_id).delete()
    flash("已刪除文章", "info")
    return redirect(url_for("blog.blog_index"))
