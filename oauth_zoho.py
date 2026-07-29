"""
webapp_oauth_zoho.py
Zoho OAuth（Server-based Application，标准授权码流程）相关的辅助函数。
Client Secret 只存在服务器的环境变量里，不会出现在前端或发给同事的任何文件里。
"""

import os
import requests

# 用 .get(默认空字符串) 而不是 [key]，这样在还没配置 .env 的情况下，
# 网站至少能正常启动、打开登录页；真正点"用 Zoho 登录"时才会因为没配置而报错，
# 而不是直接让整个网站在启动阶段就崩掉。
ZOHO_CLIENT_ID = os.environ.get("ZOHO_CLIENT_ID", "")
ZOHO_CLIENT_SECRET = os.environ.get("ZOHO_CLIENT_SECRET", "")
ZOHO_SCOPE = os.environ.get(
    "ZOHO_SCOPE", "ZohoMail.accounts.READ,ZohoMail.messages.READ,ZohoMail.folders.READ"
)
ZOHO_REDIRECT_URI = os.environ.get("ZOHO_REDIRECT_URI", "")  # 例如 https://your-app.onrender.com/auth/zoho/callback

DEFAULT_ACCOUNTS_SERVER = "https://accounts.zoho.com"


def build_authorize_url(state):
    if not ZOHO_CLIENT_ID or not ZOHO_REDIRECT_URI:
        raise RuntimeError(
            "还没有配置 Zoho OAuth 环境变量（ZOHO_CLIENT_ID / ZOHO_REDIRECT_URI）。"
            "先按 README_WEBSITE.md 注册 Zoho 应用并填好 .env 再试。"
        )
    params = (
        f"client_id={ZOHO_CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={ZOHO_REDIRECT_URI}"
        f"&scope={ZOHO_SCOPE}"
        f"&access_type=offline"
        f"&prompt=consent"
        f"&state={state}"
    )
    return f"{DEFAULT_ACCOUNTS_SERVER}/oauth/v2/auth?{params}"


def exchange_code_for_token(code, accounts_server):
    """用授权码换 access_token / refresh_token。accounts_server 是 Zoho 回调 URL 里带的
    accounts-server 参数（不同数据中心的用户，这个地址不一样，必须用对应地址换 token）。"""
    url = f"{accounts_server}/oauth/v2/token"
    resp = requests.post(
        url,
        params={
            "code": code,
            "grant_type": "authorization_code",
            "client_id": ZOHO_CLIENT_ID,
            "client_secret": ZOHO_CLIENT_SECRET,
            "redirect_uri": ZOHO_REDIRECT_URI,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def refresh_access_token(refresh_token, accounts_server):
    url = f"{accounts_server}/oauth/v2/token"
    # (连接超时, 读取超时) 分开设置，避免网络/VPN 有问题时一直卡住不报错。
    resp = requests.post(
        url,
        params={
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "client_id": ZOHO_CLIENT_ID,
            "client_secret": ZOHO_CLIENT_SECRET,
        },
        timeout=(10, 20),
    )
    resp.raise_for_status()
    return resp.json()


def derive_mail_api_domain(accounts_server):
    """Zoho Mail 的 API 域名和账号系统(accounts.*)域名是对应关系（比如
    accounts.zoho.com -> mail.zoho.com，accounts.zoho.eu -> mail.zoho.eu）。
    不能直接用 token 响应里的 api_domain 字段，那个是给 CRM/Books 等其他 Zoho 产品用的
    通用域名（www.zohoapis.com），Mail 走的是单独的域名，用错了会报 404。"""
    return accounts_server.replace("accounts.", "mail.", 1)


def get_account_info(access_token, api_domain):
    """登录后拿一下账户信息，取主邮箱地址 + accountId（后面调 messages/folders API 都要用到）。"""
    url = f"{api_domain}/api/accounts"
    resp = requests.get(
        url,
        headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    accounts = data.get("data", [])
    if not accounts:
        raise RuntimeError(f"没有从 Zoho 拿到账户信息：{data}")
    primary = accounts[0]
    email = primary.get("primaryEmailAddress") or primary.get("mailboxAddress") or primary.get("displayName")
    account_id = primary.get("accountId")
    if not email or not account_id:
        raise RuntimeError(f"无法从账户信息里解析出邮箱地址/accountId：{primary}")
    return email, str(account_id)
