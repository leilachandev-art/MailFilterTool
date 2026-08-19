"""
app.py
发票邮件筛选小工具 - 网站版主程序（Flask），Zoho 原生搜索版。

同事访问网站 -> 点"用 Zoho 登录"完成一次性授权 -> 填几个筛选条件（主题包含 / 附件名包含 /
发件人包含 / 发件人排除 / 日期 / 是否要求带附件）-> 点"立即运行"。这几个条件里，
主题/附件名/发件人/日期/是否带附件会拼成 Zoho Mail 官方搜索语法交给 Zoho 服务端去搜
（跟你在网页邮箱搜索框里用的是同一套语法）；"发件人排除"这个反向条件 Zoho 不支持，
由这个网站自己在拿到结果后再筛一遍。

命中的邮件会在页面上列成表格：发件人名、发件人邮箱、附件标题、下载链接，
一行对应一个附件，点击就能单独下载；也可以导出成 Excel，或者把整次运行打包成 ZIP 下载。

部署说明见 README_WEBSITE.md。
"""

import io
import os
import secrets
import threading
import zipfile
from datetime import datetime

from flask import Flask, redirect, url_for, session, request, render_template, jsonify, flash, send_file
from sqlalchemy import inspect, text, func, event
from sqlalchemy.engine import Engine
from dotenv import load_dotenv

load_dotenv()

from models import db, User, ManifestEntry, ProcessedMessage, RunLog, RunStatus
import oauth_zoho as zoho_oauth
import crypto_util as token_crypto
import ai_extract
from mail_sync import run_sync_for_user, DOWNLOADS_ROOT
from db_utils import commit_with_retry

try:
    from flask_wtf import CSRFProtect

    _CSRF_AVAILABLE = True
except ImportError:
    _CSRF_AVAILABLE = False

try:
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter

    _OPENPYXL_AVAILABLE = True
except ImportError:
    _OPENPYXL_AVAILABLE = False


def _split_fields(text_value):
    """把用户填的"发票号, 金额, 币种"这种逗号分隔的字段名拆成列表，去空白、去空项。"""
    return [t.strip() for t in (text_value or "").split(",") if t.strip()]


def _admin_emails():
    return {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}


def _allowed_login_emails():
    return {e.strip().lower() for e in os.environ.get("ALLOWED_LOGIN_EMAILS", "").split(",") if e.strip()}


def _allowed_login_domains():
    return {d.strip().lower().lstrip("@") for d in os.environ.get("ALLOWED_LOGIN_DOMAINS", "").split(",") if d.strip()}


def _login_allowed(email):
    """网站部署到公网后，任何人只要有 Zoho 邮箱都能点"用 Zoho 登录"进来，登录以后就能
    用附件下载和 AI 提取字段这些功能——AI 提取是要花你 Anthropic 账号的钱的，不认识的人
    登录进来跑几次就是在花你的钱，不是"泄露 API Key"这种意义上的被盗，而是"被陌生人蹭着用"。
    这里用邮箱白名单/域名白名单兜底：两个环境变量都不填的话默认谁都能登录（没有变化，
    兼容老配置）；只要配置了其中一个，登录时就会拿邮箱地址比对，不在名单里的人直接被
    拦在登录页，压根创建不了账号，也就没机会触发扣费的功能。"""
    emails = _allowed_login_emails()
    domains = _allowed_login_domains()
    if not emails and not domains:
        return True
    email_lower = (email or "").lower()
    if email_lower in emails:
        return True
    domain = email_lower.split("@")[-1] if "@" in email_lower else ""
    return domain in domains


def _sqlite_column_ddl(column):
    """把 SQLAlchemy 的 Column 对象转成 ALTER TABLE ADD COLUMN 用的类型片段（函数名是
    历史遗留，实际上 SQLite / Postgres 都会用到这个函数，部署到 Render 用 Postgres 时
    这个自动迁移也要跑）。
    这里单独处理布尔值默认值的写法：SQLite 的布尔本质是整数，认 DEFAULT 1/0；但
    Postgres 的布尔是独立类型，DDL 里写 DEFAULT 1 会直接报"类型不匹配"报错，必须写
    DEFAULT TRUE/FALSE，两边语法不通用，不判断方言就硬编码 1/0 的话，部署到 Postgres
    后只要新增一个带默认值的布尔列，启动迁移就会直接崩。"""
    dialect = db.engine.dialect
    col_type = column.type.compile(dialect=dialect)
    parts = [col_type]
    default = column.default
    if default is not None and getattr(default, "is_scalar", False):
        default_val = default.arg
        if isinstance(default_val, bool):
            if dialect.name == "postgresql":
                parts.append(f"DEFAULT {'TRUE' if default_val else 'FALSE'}")
            else:
                parts.append(f"DEFAULT {1 if default_val else 0}")
        elif isinstance(default_val, (int, float)):
            parts.append(f"DEFAULT {default_val}")
        elif isinstance(default_val, str):
            escaped = default_val.replace("'", "''")
            parts.append(f"DEFAULT '{escaped}'")
    return " ".join(parts)


def _run_lightweight_migrations():
    """SQLite/Postgres 都没接 Alembic，db.create_all() 只会建不存在的新表，不会给已经存在的老表加新列。
    这里自动比对 models.py 里声明的列和数据库里实际的列，缺什么自动 ALTER TABLE 补上，
    这样以后再改字段就不用手动删库、丢掉已经保存的处理记录了。"""
    inspector = inspect(db.engine)
    for model in (User, ProcessedMessage, ManifestEntry, RunLog, RunStatus):
        table = model.__table__
        if not inspector.has_table(table.name):
            continue  # 全新的表，db.create_all() 已经按最新结构建好了
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        missing = [c for c in table.columns if c.name not in existing]
        if not missing:
            continue
        with db.engine.begin() as conn:
            for col in missing:
                ddl = _sqlite_column_ddl(col)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}'))
                print(f"[启动迁移] {table.name} 表补上了缺失的列：{col.name}")


def is_admin_user(user):
    """默认只有第一个注册（第一个用 Zoho 登录）的人算管理员；也可以用 ADMIN_EMAILS
    环境变量（逗号分隔的邮箱）显式指定。目前只用来控制谁能看 /admin/users 这个统计页面。"""
    if not user:
        return False
    configured = _admin_emails()
    if configured:
        return user.zoho_email.lower() in configured
    first_user = User.query.order_by(User.id.asc()).first()
    return bool(first_user and first_user.id == user.id)


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

    # Render 之类的平台是通过反向代理转发请求的（它们处理 https，转给应用的是内部 http）。
    # 加这个之后 Flask 才能正确识别出外面其实是 https、原始访问域名是什么，
    # 不加的话像 CSRF 的 Referer 校验之类的判断可能会出错。
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db_url = os.environ.get("DATABASE_URL", "sqlite:///app.db")
    # Render 等平台给的 Postgres 连接串有时是 postgres:// 开头，SQLAlchemy 2.x 需要 postgresql://
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    is_sqlite = db_url.startswith("sqlite")
    if is_sqlite:
        # SQLite 同一时间只能有一个连接在写，默认还不等锁就直接抛 "database is locked"。
        # 后台同步线程边跑边一条条 commit，同时页面还在轮询 /run/status、/manifest 读进度，
        # 默认设置下很容易撞上。这里把 busy_timeout 调大（撞锁了先等最多 30 秒再报错，
        # 不是立刻报错），下面 WAL 模式再让"读"不用等"写"，两个一起用才扛得住这种并发。
        # 注意：这只是"单机本地测试多个请求并发"的兜底，真要给多个同事同时在线用，
        # 部署到 Render 时务必配置 DATABASE_URL 用 Postgres（见 README_WEBSITE.md），
        # SQLite 这套设置只能缓解，没法从根上支持很多人同时写。
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"connect_args": {"timeout": 30}}
    else:
        # Postgres（比如 Render 免费实例）本身就支持多连接并发读写，不需要 WAL 那一套，
        # 但免费实例的最大连接数通常很有限（个位数到二十来个），而这个网站可能同时有
        # 好几个 gunicorn worker 进程，每个进程自己维护一个连接池，池子开太大容易把
        # 免费额度的连接数占满，导致后来的用户连不上库。这里把每个进程的池子调小一点。
        # pool_pre_ping=True 是为了防止连接闲置一段时间后被数据库那边悄悄断开
        # （免费实例常见），不加的话第一次用到断线的连接会直接报错，而不是自动换一个。
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "pool_pre_ping": True,
            "pool_size": 3,
            "max_overflow": 2,
        }

    # ---- Session / Cookie 安全加固 ----
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # 本机用 http 测试时不能开 Secure（否则 cookie 发不出去导致登录不上）；
    # 部署到有 https 的正式环境时，在 .env 里设 FORCE_HTTPS_COOKIES=1 打开。
    app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FORCE_HTTPS_COOKIES", "0") == "1"

    db.init_app(app)

    if is_sqlite:
        # 监听 SQLAlchemy 的通用 Engine（而不是 db.engine），是因为这里还没进入 app
        # context，此时取 db.engine 会报错；反正这个进程里只有这一个数据库引擎，效果一样。
        @event.listens_for(Engine, "connect")
        def _sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    # ---- CSRF 防护：所有会改数据的 POST 表单都强制校验 token ----
    if _CSRF_AVAILABLE:
        CSRFProtect(app)
    else:
        print("[警告] 没装 Flask-WTF，CSRF 防护未启用。建议执行 pip install -r requirements.txt 后重启。")
        # 没装的话模板里的 {{ csrf_token() }} 会报错，先注册一个空实现兜底，不让页面直接崩掉。
        app.jinja_env.globals.setdefault("csrf_token", lambda: "")

    with app.app_context():
        db.create_all()
        _run_lightweight_migrations()
        # 服务器重启前如果有任务还没跑完就被关掉了（比如 Ctrl+C 或崩溃），
        # 对应后台线程已经不存在了，但数据库里 is_running 还残留 True，
        # 不清理的话，网页一打开就会显示"运行中..."，看起来像是自动开始运行了。
        stale = RunStatus.query.filter_by(is_running=True).all()
        if stale:
            for s in stale:
                s.is_running = False
                s.stop_requested = False
            commit_with_retry(db.session)

    register_routes(app)
    return app


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return User.query.get(uid)


def _wants_json():
    """运行/停止/清空记录/保存配置这几个按钮改成了 AJAX 提交，不想让页面整个刷新跳回顶部。
    前端 fetch 请求会带这个头，后端据此决定是返回 JSON（给 AJAX 用）还是走老的 flash+redirect（没启用 JS 时的兜底）。"""
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _respond(message, ok=True, **extra):
    if _wants_json():
        payload = {"ok": ok, "message": message}
        payload.update(extra)
        return jsonify(payload)
    flash(message)
    return redirect(url_for("dashboard"))


RECORDS_PAGE_SIZE = 30


def _query_manifest_page(user_id, page):
    """处理记录分页查询：一行 = 一个附件，按时间倒序，供 dashboard 页面首次渲染、
    /manifest 这个 AJAX 刷新接口、以及 Excel 导出共用同一份查询逻辑。"""
    page = page if page and page >= 1 else 1

    q = ManifestEntry.query.filter_by(user_id=user_id).order_by(ManifestEntry.created_at.desc())
    total = q.count()
    total_pages = max(1, (total + RECORDS_PAGE_SIZE - 1) // RECORDS_PAGE_SIZE)
    page = min(page, total_pages)
    rows = q.offset((page - 1) * RECORDS_PAGE_SIZE).limit(RECORDS_PAGE_SIZE).all()

    return rows, page, total_pages, total


def register_routes(app):
    @app.route("/")
    def index():
        if current_user():
            return redirect(url_for("dashboard"))
        return render_template("login.html")

    # ---------------- Zoho 登录（同时也是网站的登录方式） ----------------

    @app.route("/login/zoho")
    def login_zoho():
        state = secrets.token_hex(16)
        session["zoho_oauth_state"] = state
        try:
            return redirect(zoho_oauth.build_authorize_url(state))
        except RuntimeError as e:
            flash(str(e))
            return redirect(url_for("index"))

    @app.route("/auth/zoho/callback")
    def auth_zoho_callback():
        error = request.args.get("error")
        if error:
            flash(f"Zoho 授权失败：{error}")
            return redirect(url_for("index"))

        state = request.args.get("state")
        if not state or state != session.get("zoho_oauth_state"):
            flash("授权状态校验失败，请重新登录。")
            return redirect(url_for("index"))

        code = request.args.get("code")
        accounts_server = request.args.get("accounts-server", zoho_oauth.DEFAULT_ACCOUNTS_SERVER)

        try:
            token_resp = zoho_oauth.exchange_code_for_token(code, accounts_server)
            access_token = token_resp["access_token"]
            refresh_token = token_resp.get("refresh_token")
            # 注意：Zoho Mail API 的域名和 token 响应里的 api_domain 字段无关，
            # 要从 accounts_server 推导（accounts.zoho.com -> mail.zoho.com）。
            api_domain = zoho_oauth.derive_mail_api_domain(accounts_server)
            email_addr, account_id = zoho_oauth.get_account_info(access_token, api_domain)
        except Exception as e:
            flash(f"Zoho 授权处理失败：{e}")
            return redirect(url_for("index"))

        if not _login_allowed(email_addr):
            flash(f"{email_addr} 不在允许登录的名单里，如果这是误拦请联系管理员。")
            return redirect(url_for("index"))

        user = User.query.filter_by(zoho_email=email_addr).first()
        if not user:
            user = User(zoho_email=email_addr)
            db.session.add(user)

        if refresh_token:
            user.zoho_refresh_token = token_crypto.encrypt(refresh_token)
        user.zoho_accounts_server = accounts_server
        user.zoho_api_domain = api_domain
        user.zoho_account_id = account_id
        user.last_login_at = datetime.utcnow()
        user.login_count = (user.login_count or 0) + 1
        commit_with_retry(db.session)

        session["user_id"] = user.id
        return redirect(url_for("dashboard"))

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    # ---------------- 仪表盘 / 配置 / 运行 ----------------

    @app.route("/dashboard")
    def dashboard():
        user = current_user()
        if not user:
            return redirect(url_for("index"))

        page = request.args.get("page", 1, type=int) or 1
        rows, page, total_pages, total_entries = _query_manifest_page(user.id, page)

        status = RunStatus.query.get(user.id)
        return render_template(
            "dashboard.html",
            user=user,
            entries=rows,
            status=status,
            is_admin=is_admin_user(user),
            page=page,
            total_pages=total_pages,
            total_entries=total_entries,
            extract_field_names=_split_fields(user.extract_fields),
            ai_problem=ai_extract.diagnose(),
        )

    @app.route("/admin/users")
    def admin_users():
        """查有多少人在用这个网站：每个 Zoho 登录过的账号，首次/最近登录时间、登录次数、
        保存过多少个附件。只有管理员（第一个登录的人，或者 ADMIN_EMAILS 里指定的邮箱）能看，
        因为这里面有其他同事的邮箱地址，不能对所有登录用户公开。"""
        user = current_user()
        if not user:
            return redirect(url_for("index"))
        if not is_admin_user(user):
            flash("这个页面只有管理员能看。")
            return redirect(url_for("dashboard"))

        rows = (
            db.session.query(
                User,
                func.count(ManifestEntry.id).label("saved_count"),
            )
            .outerjoin(ManifestEntry, ManifestEntry.user_id == User.id)
            .group_by(User.id)
            .order_by(User.created_at.asc())
            .all()
        )

        return render_template(
            "admin_users.html",
            user=user,
            rows=rows,
            total_users=len(rows),
        )

    @app.route("/manifest")
    def manifest_page():
        """给"处理记录"表格用的 AJAX 刷新接口：运行结束后前端拿这个把表格内容更新一遍，
        不用整页刷新（也不会把页面滚动位置弹回顶部）。"""
        user = current_user()
        if not user:
            return jsonify({"error": "not logged in"}), 401

        page = request.args.get("page", 1, type=int) or 1
        rows, page, total_pages, total_entries = _query_manifest_page(user.id, page)
        field_names = _split_fields(user.extract_fields)

        entries_json = [
            {
                "created_at": e.created_at.strftime("%Y-%m-%d %H:%M") if e.created_at else "",
                "sender_name": e.sender_name,
                "sender_email": e.sender_email,
                "subject": e.subject,
                "filename": e.original_filename,
                "download_url": url_for("download_attachment", entry_id=e.id) if e.saved_filename else None,
                "fields": [e.extracted_fields.get(name) for name in field_names],
            }
            for e in rows
        ]

        return jsonify(
            {
                "entries": entries_json,
                "field_names": field_names,
                "page": page,
                "total_pages": total_pages,
                "total_entries": total_entries,
            }
        )

    @app.route("/config", methods=["POST"])
    def save_config():
        user = current_user()
        if not user:
            return redirect(url_for("index"))

        user.search_subject_contains = request.form.get("search_subject_contains", "")
        user.search_attachment_contains = request.form.get("search_attachment_contains", "")
        user.search_sender_contains = request.form.get("search_sender_contains", "")
        user.search_content_contains = request.form.get("search_content_contains", "")
        user.search_sender_excludes = request.form.get("search_sender_excludes", "")
        user.search_since_date = request.form.get("search_since_date", "").strip()
        user.search_require_attachment = bool(request.form.get("search_require_attachment"))
        user.extract_fields = request.form.get("extract_fields", "")

        commit_with_retry(db.session)
        return _respond("配置已保存。")

    @app.route("/run", methods=["POST"])
    def run_now():
        user = current_user()
        if not user:
            return redirect(url_for("index"))

        status = RunStatus.query.get(user.id)
        if status and status.is_running:
            return _respond("已经有一个任务在运行了，请等它跑完。", ok=False)

        app = current_app_ref["app"]
        thread = threading.Thread(target=run_sync_for_user, args=(app, user.id), daemon=True)
        thread.start()

        return _respond("已开始运行，下面的日志会自动刷新。")

    @app.route("/run_titles_only", methods=["POST"])
    def run_titles_only():
        """只搜索、不下载附件的快速模式：命中的邮件同样会记进处理记录（标题/发件人/日期），
        但跳过附件元数据查询和下载这两步，命中几百封邮件也是秒级完成，适合只是想要
        批量导出邮件标题去做后续处理（比如从标题解析 container 号）的场景。"""
        user = current_user()
        if not user:
            return redirect(url_for("index"))

        status = RunStatus.query.get(user.id)
        if status and status.is_running:
            return _respond("已经有一个任务在运行了，请等它跑完。", ok=False)

        app = current_app_ref["app"]
        thread = threading.Thread(
            target=run_sync_for_user, args=(app, user.id), kwargs={"download_attachments": False}, daemon=True
        )
        thread.start()

        return _respond("已开始运行（仅导出标题模式，不下载附件），下面的日志会自动刷新。")

    @app.route("/download_attachment/<int:entry_id>")
    def download_attachment(entry_id):
        """单独下载某一个附件（表格里每一行的下载链接对应这个路由）。"""
        user = current_user()
        if not user:
            return redirect(url_for("index"))

        entry = ManifestEntry.query.filter_by(id=entry_id, user_id=user.id).first()
        if not entry:
            flash("找不到这个附件记录，可能已经被清空了。")
            return redirect(url_for("dashboard"))

        folder = os.path.join(DOWNLOADS_ROOT, str(user.id), entry.run_id or "")
        filepath = os.path.join(folder, entry.saved_filename or "")
        if not entry.saved_filename or not os.path.isfile(filepath):
            flash("这个附件的文件已经不在服务器上了（临时文件可能已被清理），建议重新运行一次。")
            return redirect(url_for("dashboard"))

        return send_file(filepath, as_attachment=True, download_name=entry.original_filename or "attachment")

    @app.route("/download/<run_id>")
    def download_run(run_id):
        """把某一次运行命中的附件打包成 ZIP，走浏览器正常下载流程发给用户 ——
        不管网站部署在哪台服务器上，下载下来的文件天然就落在用户自己电脑的下载文件夹里。"""
        user = current_user()
        if not user:
            return redirect(url_for("index"))

        exists = ManifestEntry.query.filter_by(user_id=user.id, run_id=run_id).first()
        if not exists:
            flash("找不到这次运行的记录。")
            return redirect(url_for("dashboard"))

        folder = os.path.join(DOWNLOADS_ROOT, str(user.id), run_id)
        if not os.path.isdir(folder) or not os.listdir(folder):
            flash("这次运行的文件已经不在服务器上了（临时文件可能已被清理），建议重新运行一次。")
            return redirect(url_for("dashboard"))

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in os.listdir(folder):
                fpath = os.path.join(folder, name)
                if os.path.isfile(fpath):
                    zf.write(fpath, arcname=name)
        buf.seek(0)

        return send_file(
            buf,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"invoices_{run_id}.zip",
        )

    @app.route("/export_excel")
    def export_excel():
        """把当前所有处理记录导出成一份 Excel：时间/发件人名/发件人邮箱/主题/附件标题/下载链接，
        后面再跟着你在"筛选条件"里配置的那几个 AI 提取字段（比如 金额/币种/container号，
        字段名和列顺序跟当前 extract_fields 配置一致）。一封命中的邮件不管有没有附件都会有
        一行，没有附件的行"附件标题"和"下载链接"留空——这样批量导出邮件标题不用依赖邮件
        必须带附件。AI 提取的这几列拿不准时会留空，不是每一行都有，建议当作辅助参考，
        重要数字自己核对一下原始附件。
        注意下载链接需要登录同一个网站才能打开，而且服务器上的临时文件超过 3 天会被自动清理，
        建议导出后尽快把需要的附件下载下来，不要把这份 Excel 当长期归档用。"""
        user = current_user()
        if not user:
            return redirect(url_for("index"))
        if not _OPENPYXL_AVAILABLE:
            flash("服务器还没装 openpyxl，导出不了 Excel。请执行 pip install -r requirements.txt 后重启。")
            return redirect(url_for("dashboard"))

        rows = ManifestEntry.query.filter_by(user_id=user.id).order_by(ManifestEntry.created_at.desc()).all()
        field_names = _split_fields(user.extract_fields)

        fixed_headers = ["时间", "发件人名", "发件人邮箱", "主题", "附件标题", "下载链接"]
        headers = fixed_headers + [f"{name}(AI提取)" for name in field_names]
        link_col = fixed_headers.index("下载链接") + 1

        wb = Workbook()
        ws = wb.active
        ws.title = "处理记录"
        ws.append(headers)
        for e in rows:
            link = url_for("download_attachment", entry_id=e.id, _external=True) if e.saved_filename else ""
            fields = e.extracted_fields
            ws.append(
                [
                    e.created_at.strftime("%Y-%m-%d %H:%M") if e.created_at else "",
                    e.sender_name or "",
                    e.sender_email or "",
                    e.subject or "",
                    e.original_filename or "",
                    link,
                ]
                + [fields.get(name) or "" for name in field_names]
            )
            if link:
                cell = ws.cell(row=ws.max_row, column=link_col)
                cell.hyperlink = link
                cell.style = "Hyperlink"

        fixed_widths = [16, 22, 28, 34, 34, 46]
        widths = fixed_widths + [18] * len(field_names)
        for i, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = width

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        return send_file(
            buf,
            as_attachment=True,
            download_name="处理记录.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.route("/stop", methods=["POST"])
    def stop_run():
        user = current_user()
        if not user:
            return redirect(url_for("index"))

        status = RunStatus.query.get(user.id)
        if status and status.is_running:
            status.stop_requested = True
            commit_with_retry(db.session)
            return _respond("已发送停止请求，正在停止当前运行 ...")
        return _respond("当前没有正在运行的任务。", ok=False)

    @app.route("/clear_processed", methods=["POST"])
    def clear_processed():
        user = current_user()
        if not user:
            return redirect(url_for("index"))

        status = RunStatus.query.get(user.id)
        if status and status.is_running:
            return _respond("有任务正在运行，请先停止或等它跑完再清空处理记录。", ok=False)

        deleted = ProcessedMessage.query.filter_by(user_id=user.id).delete()
        commit_with_retry(db.session)
        return _respond(f"已清空处理记录（{deleted} 条）。下次运行会重新扫描所有历史邮件，已经保存过的附件可能会重复保存一份。")

    @app.route("/clear_manifest", methods=["POST"])
    def clear_manifest():
        """清空页面底部那个可见的"处理记录"表格（ManifestEntry，已保存附件的溯源历史），
        跟"清空处理记录"是两码事——那个清的是内部去重用的 ProcessedMessage，不影响这张表。
        这个操作会导致对应的下载链接跟着失效（数据库记录没了，下载路由查不到）。"""
        user = current_user()
        if not user:
            return redirect(url_for("index"))

        status = RunStatus.query.get(user.id)
        if status and status.is_running:
            return _respond("有任务正在运行，请先停止或等它跑完再清空附件历史。", ok=False)

        deleted = ManifestEntry.query.filter_by(user_id=user.id).delete()
        commit_with_retry(db.session)
        return _respond(f"已清空附件历史（{deleted} 条）。之前的下载链接会跟着失效，不影响下次是否重新扫描邮件。")

    @app.route("/run/status")
    def run_status():
        user = current_user()
        if not user:
            return jsonify({"error": "not logged in"}), 401

        status = RunStatus.query.get(user.id)
        is_running = bool(status and status.is_running)
        run_id = status.current_run_id if status else None

        logs = []
        if run_id:
            rows = (
                RunLog.query.filter_by(user_id=user.id, run_id=run_id)
                .order_by(RunLog.created_at.asc())
                .limit(500)
                .all()
            )
            logs = [r.message for r in rows]

        download_url = None
        if run_id and (status.saved_count if status else 0):
            download_url = url_for("download_run", run_id=run_id)

        return jsonify(
            {
                "is_running": is_running,
                "logs": logs,
                "checked": status.checked_count if status else 0,
                "matched": status.matched_count if status else 0,
                "saved": status.saved_count if status else 0,
                "download_url": download_url,
            }
        )


# 用一个小 dict 存 app 引用，方便在 /run 路由里拿到 app 传给后台线程
# （避免 Flask 的 current_app 代理对象跨线程使用的问题）
current_app_ref = {}

app = create_app()
current_app_ref["app"] = app


if __name__ == "__main__":
    # Werkzeug 的 debug 交互调试器如果暴露在公网上有远程执行代码的风险，
    # 默认关闭；只有显式在 .env 里设 FLASK_DEBUG=1 才会打开（仅本机开发时用）。
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    # 每个同事都在自己电脑上单独跑一份，默认只监听 127.0.0.1（只有这台电脑自己能访问），
    # 不用 0.0.0.0（那样同一个 WiFi/局域网里的其他人也能连进来，等于把自己的邮箱工具暴露给了旁人）。
    # 以后如果真的要集中部署到云端服务器，再在 .env 里设 HOST=0.0.0.0。
    host = os.environ.get("HOST", "127.0.0.1")
    # threaded=True：不然 Flask 自带的开发服务器同一时间只能处理一个请求，界面会卡。
    app.run(host=host, port=int(os.environ.get("PORT", 5000)), debug=debug_mode, threaded=True)
