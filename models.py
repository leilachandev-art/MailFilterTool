"""
models.py
数据库模型。用户身份：Zoho OAuth 登录 + 令牌；筛选配置是基于 Zoho Mail 原生搜索语法的
几个结构化条件（主题包含/附件名包含/发件人包含/发件人排除/日期/是否要求带附件）；
另外存已处理邮件记录（去重用）、投递清单（附件溯源信息 + 下载）、运行日志。
"""

from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    # ---- Zoho 身份 + OAuth ----
    zoho_email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    zoho_account_id = db.Column(db.String(64), nullable=True)
    zoho_refresh_token = db.Column(db.Text, nullable=True)
    zoho_accounts_server = db.Column(db.String(255), default="https://accounts.zoho.com")
    zoho_api_domain = db.Column(db.String(255), default="https://mail.zoho.com")

    # ---- 筛选配置：对应 Zoho 邮箱网页里"复合搜索"用的那几个条件 ----
    # 主题/附件名/发件人这三项，会拼成 Zoho Mail 搜索 API 的 searchKey，交给 Zoho 自己在
    # 服务端搜（跟你在网页邮箱搜索框里用 contains / attachment name / from 是同一套语法）。
    # 逗号分隔多个值时，同一个条件内是"满足任意一个"（OR），不同条件之间是"必须同时满足"（AND）。
    search_subject_contains = db.Column(db.Text, default="")
    search_attachment_contains = db.Column(db.Text, default="")
    search_sender_contains = db.Column(db.Text, default="")
    search_content_contains = db.Column(db.Text, default="")  # 正文包含，对应 Zoho 的 content:

    # "发件人不包含"——Zoho 的搜索语法没有"排除/不包含"这种反向条件，没法交给 Zoho 处理，
    # 所以这个是我们自己的代码在拿到 Zoho 搜索结果之后，再筛一遍、把命中排除词的邮件跳过。
    search_sender_excludes = db.Column(db.Text, default="")

    search_since_date = db.Column(db.String(20), default="")  # YYYY-MM-DD，对应 Zoho 的 fromDate
    search_require_attachment = db.Column(db.Boolean, default=True)  # 对应 Zoho 的 has:attachment

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # ---- 访问统计：谁在用这个网站，用来在 /admin/users 里查 ----
    last_login_at = db.Column(db.DateTime, nullable=True)
    login_count = db.Column(db.Integer, default=0)


class ProcessedMessage(db.Model):
    """记录某个用户已经检查过的邮件 id，避免重复扫描。"""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    uid = db.Column(db.String(64), nullable=False)

    __table_args__ = (db.UniqueConstraint("user_id", "uid", name="uq_user_uid"),)


class ManifestEntry(db.Model):
    """命中的附件清单：一行 = 一个附件，用来在网页表格 / Excel 导出里清晰展示溯源信息。"""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    run_id = db.Column(db.String(64), nullable=True, index=True)
    uid = db.Column(db.String(64))  # 邮件 message id，去重/溯源用

    original_filename = db.Column(db.String(500))  # 附件原始文件名，用于显示
    saved_filename = db.Column(db.String(500))  # 实际存在服务器临时目录里的文件名（重名时会加序号后缀）

    sender_name = db.Column(db.String(500))  # 发件人显示名
    sender_email = db.Column(db.String(500))  # 发件人邮箱地址
    subject = db.Column(db.String(1000))
    mail_date = db.Column(db.String(200))
    message_id = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class RunLog(db.Model):
    """每次运行的日志行，前端轮询展示进度。"""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    run_id = db.Column(db.String(64), index=True)
    message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class RunStatus(db.Model):
    """当前用户是否有任务正在跑（简单的进程内状态记录）。"""

    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), primary_key=True)
    is_running = db.Column(db.Boolean, default=False)
    current_run_id = db.Column(db.String(64), nullable=True)
    stop_requested = db.Column(db.Boolean, default=False)
    checked_count = db.Column(db.Integer, default=0)
    saved_count = db.Column(db.Integer, default=0)
