"""
db_utils.py
SQLite 同一时间只能有一个连接在写，就算开了 WAL + busy_timeout（见 app.py），高并发下
（后台同步线程一条条 commit，同时页面在轮询 /run/status、/manifest 读进度）还是有极小概率
在 busy_timeout 等满之后仍然拿不到锁，抛 "database is locked"。

commit_with_retry() 把这种"瞬时锁冲突"和"数据本身有问题"的报错分开处理：前者自动退避重试
几次，后者正常往外抛，不会被这里悄悄吞掉。用来替换代码里所有 db.session.commit()。
"""

import time

from sqlalchemy.exc import OperationalError

RETRY_TIMES = 5
BASE_DELAY_SECONDS = 0.3


def commit_with_retry(session, retries=RETRY_TIMES, base_delay=BASE_DELAY_SECONDS):
    """遇到 "database is locked" 就退避重试（0.3s, 0.6s, 1.2s ... 指数递增），
    重试次数用完还是锁着，或者是别的原因导致的 OperationalError，就正常抛出去，
    由调用方决定怎么处理（mail_sync.py 里外层已经有 try/except 会记日志+跳过）。"""
    last_error = None
    for attempt in range(retries):
        try:
            session.commit()
            return
        except OperationalError as e:
            session.rollback()
            last_error = e
            if "database is locked" not in str(e).lower():
                raise
            if attempt < retries - 1:
                time.sleep(base_delay * (2 ** attempt))
    raise last_error
