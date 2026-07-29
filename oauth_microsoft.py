"""
webapp_oauth_microsoft.py
Microsoft OAuth（连 OneDrive）相关辅助函数，用标准的服务器端授权码流程（ConfidentialClientApplication）。
Client Secret 只存在服务器环境变量里。

重要：Azure 应用要注册成"多租户 + 支持个人账号"（Accounts in any organizational directory and
personal Microsoft accounts），并且要注册在一个正常托管的租户下（比如你自己的个人 Microsoft 账号），
不要注册在同事公司那个"未托管"的租户里——否则同事登录时会报 AADSTS650051。
多租户应用不受这个限制，因为应用本身不需要"落户"在同事公司的租户里，只是拿到同事的委托授权。
"""

import os
import msal

# 用 .get(默认空字符串) 而不是 [key]，避免没配置 .env 时网站在启动阶段就崩掉。
MS_CLIENT_ID = os.environ.get("MS_CLIENT_ID", "")
MS_CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "")
MS_REDIRECT_URI = os.environ.get("MS_REDIRECT_URI", "")  # 例如 https://your-app.onrender.com/auth/microsoft/callback
MS_SCOPES = ["https://graph.microsoft.com/Files.ReadWrite", "https://graph.microsoft.com/User.Read"]

AUTHORITY = "https://login.microsoftonline.com/common"


def _msal_app():
    return msal.ConfidentialClientApplication(
        client_id=MS_CLIENT_ID,
        client_credential=MS_CLIENT_SECRET,
        authority=AUTHORITY,
    )


def build_authorize_url(state):
    if not MS_CLIENT_ID or not MS_REDIRECT_URI:
        raise RuntimeError(
            "还没有配置 Microsoft OAuth 环境变量（MS_CLIENT_ID / MS_REDIRECT_URI）。"
            "先按 README_WEBSITE.md 注册 Azure 应用并填好 .env 再试。"
        )
    app = _msal_app()
    return app.get_authorization_request_url(
        MS_SCOPES,
        state=state,
        redirect_uri=MS_REDIRECT_URI,
        prompt="select_account",
    )


def exchange_code_for_token(code):
    app = _msal_app()
    result = app.acquire_token_by_authorization_code(
        code, scopes=MS_SCOPES, redirect_uri=MS_REDIRECT_URI
    )
    if "access_token" not in result:
        raise RuntimeError(f"获取 Microsoft token 失败：{result.get('error_description') or result}")
    return result


def refresh_access_token(refresh_token):
    app = _msal_app()
    result = app.acquire_token_by_refresh_token(refresh_token, scopes=MS_SCOPES)
    if "access_token" not in result:
        raise RuntimeError(f"刷新 Microsoft token 失败：{result.get('error_description') or result}")
    return result
