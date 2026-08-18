"""
pdf_extract.py
从下载下来的 PDF 附件里"尽量准"地提取三样东西：总金额、币种、container(集装箱)号。

设计原则：宁可拿不到（留空），也不要拿错。所以：
- 金额只在"总金额"类关键词（Grand Total / Invoice Total / Total Due / 合计 等）紧挨着的
  表格单元格 / 同一行文字里找，找不到就不猜。不同供应商的发票排版差异很大（同一批
  VenderInvoice 里，Starco/Kwality 这种把标签和数字分开排版的发票，提取程序很可能就是
  拿不到——这是预期行为，不是 bug，人工核对时按空白处理，不当真实数据用）。
- container 号先用正则找"4个字母+7位数字"的候选，再用 ISO 6346 校验位算法验证，
  验证不通过的候选会被丢弃，避免把普通编号/单号误判成 container 号。

用法：extract_invoice_info(pdf_bytes) -> {"amount": str|None, "currency": str|None,
                                          "container_numbers": [str, ...]}
"""

import io
import re

try:
    import pdfplumber

    _PDFPLUMBER_AVAILABLE = True
except ImportError:
    _PDFPLUMBER_AVAILABLE = False


_TOTAL_LABELS = [
    "grand total",
    "invoice total",
    "total due",
    "amount due",
    "please pay this amount",
    "total amount",
    "total",
    "合计",
    "总计",
    "应付金额",
    "应付款",
]

_CURRENCY_RE = re.compile(r"\b(USD|CAD|CNY|RMB|EUR|GBP|HKD)\b", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"\$?\s*([\d,]{1,12}\.\d{2})")
_CONTAINER_RE = re.compile(r"\b([A-Z]{4}\d{7})\b")


def _build_letter_values():
    """ISO 6346 字母转数字表：10,12,13,...,38，跳过 11 的倍数（11/22/33）。"""
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    result = {}
    v = 10
    for ch in letters:
        if v % 11 == 0:
            v += 1
        result[ch] = v
        v += 1
    return result


_LETTER_VALUES = _build_letter_values()


def _container_check_digit_valid(number):
    """按 ISO 6346 标准校验第 11 位校验位，通不过就不是真正的 container 号
    （避免把长得像的普通编号/工单号误判进去）。"""
    if len(number) != 11:
        return False
    body, check = number[:10], number[10]
    if not check.isdigit():
        return False
    total = 0
    for i, ch in enumerate(body):
        if ch.isalpha():
            val = _LETTER_VALUES.get(ch.upper())
            if val is None:
                return False
        elif ch.isdigit():
            val = int(ch)
        else:
            return False
        total += val * (2 ** i)
    remainder = total % 11
    expected = 0 if remainder == 10 else remainder
    return expected == int(check)


def extract_container_numbers(text):
    candidates = set(_CONTAINER_RE.findall(text or ""))
    valid = sorted(c for c in candidates if _container_check_digit_valid(c))
    return valid


def _extract_amount_from_tables(pdf):
    """优先从 PDF 里真正的表格结构找："总金额"标签所在单元格，同一行里其他单元格
    只要能解析出金额格式就当作命中——比逐行文字正则更不容易因为版面错位而拿错。"""
    for page in pdf.pages:
        try:
            tables = page.extract_tables()
        except Exception:
            continue
        for table in tables or []:
            for row in table or []:
                if not row:
                    continue
                cells = [c or "" for c in row]
                for i, cell in enumerate(cells):
                    label = cell.strip().lower()
                    if not label:
                        continue
                    if any(kw in label for kw in _TOTAL_LABELS):
                        for other in cells[i + 1:] + cells[:i]:
                            m = _AMOUNT_RE.search(other or "")
                            if m:
                                amount = m.group(1).replace(",", "")
                                cur_m = _CURRENCY_RE.search(other) or _CURRENCY_RE.search(label)
                                currency = cur_m.group(1).upper() if cur_m else None
                                return amount, currency
    return None, None


def _extract_amount_from_text(text):
    """表格里找不到时，退一步在纯文字里找——只在"总金额"关键词所在行 + 紧跟的下一行
    这个很窄的范围内找金额，避免把版面上其他不相关的数字拉进来。"""
    if not text:
        return None, None
    lines = text.splitlines()
    for i, line in enumerate(lines):
        low = line.strip().lower()
        for kw in _TOTAL_LABELS:
            if kw in low:
                window = line
                if i + 1 < len(lines):
                    window += " " + lines[i + 1]
                m = _AMOUNT_RE.search(window)
                if m:
                    amount = m.group(1).replace(",", "")
                    cur_m = _CURRENCY_RE.search(window)
                    currency = cur_m.group(1).upper() if cur_m else None
                    return amount, currency
    return None, None


def extract_invoice_info(pdf_bytes):
    """返回 {"amount": str|None, "currency": str|None, "container_numbers": [str,...]}。
    任何一步失败（包括没装 pdfplumber、PDF 是扫描图片没有文字层等）都静默返回全空，
    不影响附件本身的下载和记录。"""
    result = {"amount": None, "currency": None, "container_numbers": []}
    if not _PDFPLUMBER_AVAILABLE:
        return result
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            result["container_numbers"] = extract_container_numbers(full_text)

            amount, currency = _extract_amount_from_tables(pdf)
            if amount is None:
                amount, currency = _extract_amount_from_text(full_text)
            result["amount"] = amount
            result["currency"] = currency
    except Exception:
        pass
    return result
