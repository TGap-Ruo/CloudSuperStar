import os
import sys

from loguru import logger
from tqdm import tqdm

tqdm_stream = sys.stderr

# 服务端运行时的日志文件与级别可以通过环境变量覆盖：
#   CX_LOG_FILE   日志文件路径，"none"/"off" 表示不写文件
#   CX_LOG_LEVEL  文件日志级别，默认 TRACE
LOG_FILE_ENV = "CX_LOG_FILE"
LOG_LEVEL_ENV = "CX_LOG_LEVEL"


def _resolve_log_file() -> str:
    # 未显式配置时不落盘：避免仅 import 就在当前工作目录生成日志文件。
    # 服务端会在启动时通过 reconfigure(log_file=...) 显式指定账号日志路径。
    return os.environ.get(LOG_FILE_ENV, "").strip()

# 日志缓冲区，用于在手动答题时缓存后台日志，答题结束后统一输出
log_buffer = []
MAX_LOG_BUFFER_SIZE = 1000


def tqdm_sink(msg):
    manual_locked = False
    try:
        # 动态获取 api.answer 模块中的 TikuManual 锁，避免循环导入
        if 'chaoxing_core.answer' in sys.modules:
            TikuManual = getattr(sys.modules['chaoxing_core.answer'], 'TikuManual', None)
            if TikuManual and getattr(TikuManual, '_manual_lock', None):
                manual_locked = TikuManual._manual_lock.locked()
    except (AttributeError, KeyError, ImportError):
        pass

    if manual_locked:
        if len(log_buffer) < MAX_LOG_BUFFER_SIZE:
            log_buffer.append(msg)
    else:
        if log_buffer:
            for buffered_msg in log_buffer:
                tqdm.write(buffered_msg.rstrip(), file=tqdm_stream)
            log_buffer.clear()
        tqdm.write(msg.rstrip(), file=tqdm_stream)
    tqdm_stream.flush()


def reconfigure(
    log_file: str | None = None,
    console_level: str = "INFO",
    file_level: str = "TRACE",
) -> None:
    """重建全部日志输出。

    :param log_file: 日志文件路径；``None`` 时读取 ``CX_LOG_FILE`` 环境变量，
        为空字符串 / ``none`` / ``off`` 表示不写文件。
    :param console_level: stderr 输出级别，支持 TRACE/DEBUG/INFO/WARNING/ERROR。
    :param file_level: 文件输出级别。
    """
    if log_file is None:
        log_file = _resolve_log_file()

    logger.remove()
    logger.add(tqdm_sink, colorize=True, enqueue=True, level=console_level)

    if log_file and log_file.lower() not in {"none", "off", "null"}:
        try:
            log_dir = os.path.dirname(os.path.abspath(log_file))
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            logger.add(
                log_file,
                rotation="10 MB",
                retention="14 days",
                encoding="utf-8",
                level=file_level,
            )
        except OSError:
            # 目录不可写时退化为仅控制台输出，不阻塞主流程
            pass


reconfigure()
