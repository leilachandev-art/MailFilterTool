"""
crypto_util.py
数据库里存的 zoho_refresh_token / ms_refresh_token 拿到手就能读同事的邮箱、写同事的 OneDrive，
属于比较敏感的东西，所以落库前用对称加密包一层，不再是明文存在 SQLite 里。

密钥放在 TOKEN_ENCRYPTION_KEY 环境变量里。如果没配置，第一次启动会自动生成一把新的并写回 .env 文件，
这样重启网站还是用同一把钥匙，不会导致已经加密过的旧 token 解不开。

如果服务器没装 cryptography 包，会自动退化成明文存储（不加密），并不会导致网站崩溃，
只是起不到加密效果 —— 正式使用前建议 `pip install -r requirements.txt` 把 cryptography 装上。
"""

import os

try:
    from cryptography.fernet import Fernet

    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False


def _ensure_key():
    key = os.environ.get("TOKEN_ENCRYPTION_KEY", "").strip()
    if key:
        return key

    if not _CRYPTO_AVAILABLE:
        return ""

    new_key = Fernet.generate_key().decode()
    os.environ["TOKEN_ENCRYPTION_KEY"] = new_key

    # 尽量把新密钥写回 .env，这样下次重启还是同一把钥匙。写不进去也不影响这次运行。
    try:
        from dotenv import set_key

        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if not os.path.exists(env_path):
            open(env_path, "a", encoding="utf-8").close()
        set_key(env_path, "TOKEN_ENCRYPTION_KEY", new_key)
    except Exception:
        pass

    return new_key


_KEY = _ensure_key()
_fernet = Fernet(_KEY.encode()) if (_CRYPTO_AVAILABLE and _KEY) else None


def encrypt(value):
    if not value or not _fernet:
        return value
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value):
    if not value or not _fernet:
        return value
    try:
        return _fernet.decrypt(value.encode()).decode()
    except Exception:
        # 解密失败可能是两种情况：1) 加密上线之前存的还是明文，这里原样返回，不让老数据
        # 直接报错；2) TOKEN_ENCRYPTION_KEY 变了（比如部署平台没把它固定成环境变量，每次
        # 重启/重新部署都随机生成一把新钥匙），存的密文用现在这把新钥匙解不开，这种情况下
        # 原样返回的其实是一段还没解密的密文——调用方（mail_sync.py）会用 looks_encrypted()
        # 识别出这种情况，给用户一个"需要重新登录"的清晰提示，而不是把这坨密文当成真的
        # refresh_token 发给 Zoho，得到一个让人摸不着头脑的 400 错误。
        return value


def looks_encrypted(value):
    """粗略判断这个字符串是不是"还没解密成功的 Fernet 密文"，而不是真的 token 明文。
    Fernet 密文固定以版本字节 0x80 开头，编码成 base64 之后几乎总是以 "gAAAAA" 开头，
    真实的 Zoho refresh_token 不会长这样，用这个前缀基本能可靠地区分两者。"""
    return bool(value) and value.startswith("gAAAAA")
