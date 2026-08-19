"""
ai_extract.py
用 Claude 的文档理解能力（不是正则/关键词规则）从 PDF 附件里提取用户自己定义的字段，
思路跟 imagetotable.ai 的"定义列名，AI 自动提取"一致：你在网页上填想要哪些字段
（比如"发票号, 金额, 币种, container号"），每份 PDF 附件下载下来后都会调用一次
Claude，把这些字段的值抠出来，不管发票排版长什么样，AI 都是"读懂"了再填，
比之前那套关键词/表格正则准得多。

代价：每处理一份 PDF 附件都会调用一次 Anthropic API，有少量费用和几秒延迟，
需要在 .env 里配置 ANTHROPIC_API_KEY 才能用；没配置的话这个功能自动跳过
（附件本身照常下载，只是提取字段那几列留空)。

用法：extract_fields_from_pdf(pdf_bytes, field_names) -> (result_dict, error_message_or_None)
调用方（mail_sync.py）应该把 error_message 记到运行日志里——不然调用失败时用户只会看到
"提取字段全是空的"，完全不知道是哪一步出的问题（没装包？Key 不对？模型名不对？配额用完？）。

用法：diagnose() -> str|None，返回"当前配置不满足什么条件"的说明，None 表示配置齐全，
可以在运行前先检查一次，比每份 PDF 都失败一次才发现问题更快。
"""

import base64
import json
import os
import threading

try:
    import anthropic

    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

# claude-sonnet-5 是目前的主力模型，文档理解能力强，适合发票这种排版差异很大的场景。
# 如果想省钱可以在 .env 里把 ANTHROPIC_EXTRACT_MODEL 换成更便宜的模型试试效果。
DEFAULT_MODEL = "claude-sonnet-5"

# 部署成网站给多人用之后，可能好几个同事同时点"立即运行"，各自的 PDF 附件会并发
# 调用 Anthropic API——量一大容易撞到账号的限流（rate limit），也可能让服务器
# 短时间内开太多外部请求。这里用一个进程内的信号量把"同时在发起的 API 调用数"
# 卡住，默认最多 3 个同时在跑，多的排队等，不是失败。可以用 AI_EXTRACT_MAX_CONCURRENT
# 环境变量调整。注意：如果 Render 上开了多个 gunicorn worker 进程，这个上限是
# "每个进程"各自的上限，不是跨进程的全局上限（进程之间不共享内存）。
_MAX_CONCURRENT = max(1, int(os.environ.get("AI_EXTRACT_MAX_CONCURRENT", "3") or "3"))
_CONCURRENCY_GATE = threading.Semaphore(_MAX_CONCURRENT)


def _get_client():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or not _ANTHROPIC_AVAILABLE:
        return None
    return anthropic.Anthropic(api_key=api_key)


def is_configured():
    """给上层判断要不要在界面上提示"没配置 API Key，这几列会是空的"。"""
    return bool(os.environ.get("ANTHROPIC_API_KEY", "")) and _ANTHROPIC_AVAILABLE


def diagnose():
    """检查当前环境能不能跑 AI 提取，能跑返回 None，不能跑返回具体原因（中文，可直接展示/记日志）。
    常见的两个坑：requirements.txt 更新后没有重新 pip install，或者 .env 改了但没重启进程
    （load_dotenv 只在启动时读一次，改完 .env 必须重新跑 python app.py 才会生效）。"""
    if not _ANTHROPIC_AVAILABLE:
        return "没装 anthropic 这个 Python 包。请在项目的虚拟环境里执行：pip install -r requirements.txt（或者单独 pip install anthropic），然后重新启动 python app.py。"
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "环境变量 ANTHROPIC_API_KEY 是空的。确认 .env 里确实填了这一行，并且改完 .env 之后重新启动了 python app.py（改配置文件不会自动生效，必须重启进程）。"
    return None


def extract_fields_from_pdf(pdf_bytes, field_names):
    """field_names 是字段名列表，比如 ["发票号", "金额", "币种", "container号"]。
    返回 (result, error)：result 是 dict，每个字段的值是字符串或 None（文档里确实没有 /
    模型不确定）；error 是 None（成功）或者一句话错误描述（调用失败/解析失败），
    调用方应该把 error 记到运行日志里，方便排查，而不是默默吞掉。"""
    result = {name: None for name in field_names}
    if not field_names:
        return result, None

    reason = diagnose()
    if reason:
        return result, reason

    client = _get_client()
    if not client:
        return result, "无法创建 Anthropic 客户端（不应该发生，如果看到这条请检查 ANTHROPIC_API_KEY）。"

    field_list_str = "、".join(field_names)
    prompt = (
        f"这是一份物流/发票类 PDF 文档。请从中提取以下字段的值：{field_list_str}。\n"
        f"要求：\n"
        f"1. 只返回一个 JSON 对象，key 必须跟我给的字段名完全一致，value 是提取到的值（字符串）。\n"
        f"2. 如果某个字段在文档里确实找不到，或者你不确定，对应 value 填 null，不要瞎猜、不要编造。\n"
        f"3. 金额类字段只填数字本身（不要带货币符号、不要千分位逗号）；币种类字段填三位货币代码（如 USD/CAD/CNY）。\n"
        f"4. 如果某个字段在文档里有多个值（比如多个 container 号），用英文逗号分隔全部列出。\n"
        f"5. 除了这个 JSON，不要输出任何其他文字、不要加 ```json 代码块标记。"
    )

    error = None
    text = None
    try:
        b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
        model = os.environ.get("ANTHROPIC_EXTRACT_MODEL", DEFAULT_MODEL)
        with _CONCURRENCY_GATE:
            resp = client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": "application/pdf",
                                    "data": b64,
                                },
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        ).strip()
    except anthropic.APIStatusError as e:
        # API 明确拒绝了这次调用（Key 错、模型名不对、没余额、限流等），把它的原话带出去，
        # 比自己猜"哪里错了"靠谱得多。
        detail = ""
        try:
            detail = e.response.json().get("error", {}).get("message", "")
        except Exception:
            detail = str(e)
        error = f"Anthropic API 返回错误（状态码 {e.status_code}）：{detail or e}"
    except Exception as e:
        error = f"调用 Anthropic API 失败：{type(e).__name__}: {e}"

    if error:
        return result, error

    # 走到这里说明 API 调用成功了，剩下的是"模型返回的文本能不能解析成 JSON"这一步。
    try:
        # 万一模型还是加了 ```json 代码块标记，简单剥掉，避免 json.loads 报错。
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()

        data = json.loads(text)
        for name in field_names:
            val = data.get(name)
            if val is not None:
                val = str(val).strip()
                if val and val.lower() != "null":
                    result[name] = val
    except Exception as e:
        preview = (text or "")[:200]
        error = f"模型返回的内容不是合法 JSON，解析失败：{e}。原始返回（截断）：{preview!r}"

    return result, error
