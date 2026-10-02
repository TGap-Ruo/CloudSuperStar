# -*- coding: utf-8 -*-
"""无人值守的刷课 / 答题编排。

本模块把 ``chaoxing_core`` 的能力组装成一次完整的服务端运行：

1. 准备账号运行时目录（Cookie / 缓存 / 日志 / 渲染后的 config.ini）
2. 登录（Cookie 优先，失败回退账号密码）
3. 读取课程列表并按配置过滤
4. 逐课程读取章节 -> 任务点 -> 执行视频/文档/测验/阅读/直播任务
5. 可选自动签到、可选增加章节学习次数
6. 汇总统计、写报告、发通知

与上游 ``main.py`` 的差别：没有任何 ``input()`` 交互，所有路径/日志都可配置，
任务支持软超时，结果结构化输出，便于被调度器与面板消费。
"""

from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from queue import PriorityQueue
from typing import Any, Optional

from server.config import AccountConfig, ServerConfig, StudyConfig, render_ini
from server.notify import send as notify_send
from server.paths import AccountPaths, DataPaths

try:  # Python 3.13+
    from queue import ShutDown  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - 旧版本 Python

    class ShutDown(Exception):  # type: ignore[no-redef]
        """兼容旧版 Python 的队列关闭异常。"""


MAX_CHAPTER_RETRIES = 5


class ChapterResult(Enum):
    SUCCESS = 0
    ERROR = 1
    NOT_OPEN = 2
    PENDING = 3
    SKIPPED = 4
    ABORTED = 5


@dataclass
class CourseStats:
    course_id: str
    clazz_id: str
    title: str
    chapters_total: int = 0
    chapters_finished: int = 0
    chapters_failed: int = 0
    status: str = "pending"
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "course_id": self.course_id,
            "clazz_id": self.clazz_id,
            "title": self.title,
            "chapters_total": self.chapters_total,
            "chapters_finished": self.chapters_finished,
            "chapters_failed": self.chapters_failed,
            "status": self.status,
            "message": self.message,
        }


@dataclass
class RunReport:
    account: str
    run_id: str
    started_at: str
    finished_at: str = ""
    status: str = "running"
    message: str = ""
    duration_seconds: float = 0.0
    courses: list[CourseStats] = field(default_factory=list)
    chapters_total: int = 0
    chapters_finished: int = 0
    chapters_failed: int = 0
    chapters_skipped: int = 0
    answer_total: int = 0
    answer_covered: int = 0
    sign_in: dict[str, Any] = field(default_factory=dict)
    learning_count_added: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False
    log_file: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "courses_total": len(self.courses),
            "courses_finished": sum(1 for c in self.courses if c.status == "success"),
            "chapters_total": self.chapters_total,
            "chapters_finished": self.chapters_finished,
            "chapters_failed": self.chapters_failed,
            "answer_total": self.answer_total,
            "answer_covered": self.answer_covered,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "message": self.message,
            "duration_seconds": round(self.duration_seconds, 1),
            "courses": [c.to_dict() for c in self.courses],
            "chapters_total": self.chapters_total,
            "chapters_finished": self.chapters_finished,
            "chapters_failed": self.chapters_failed,
            "chapters_skipped": self.chapters_skipped,
            "answer_total": self.answer_total,
            "answer_covered": self.answer_covered,
            "sign_in": self.sign_in,
            "learning_count_added": self.learning_count_added,
            "usage": dict(self.usage),
            "dry_run": self.dry_run,
            "log_file": self.log_file,
        }


class InstrumentedTiku:
    """统计题库查询次数与命中率的代理对象。

    核心代码只会用到 ``self.tiku`` 的属性与 ``query`` 方法，包一层代理即可在
    不改动核心的前提下拿到「搜到答案 / 总题数」。
    """

    def __init__(self, inner: Any):
        self._inner = inner
        self.queries = 0
        self.hits = 0
        self._lock = threading.Lock()

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)

    def query(self, q_info: dict) -> Optional[str]:
        answer = self._inner.query(q_info)
        self._count(1, 1 if answer else 0)
        return answer

    def query_all(self, q_list: list[dict], query_delay: float = 0.0) -> list[Optional[str]]:
        """核心的章节检测走批量接口，这里同样需要统计。"""
        inner_query_all = getattr(self._inner, "query_all", None)
        if callable(inner_query_all):
            answers = inner_query_all(q_list, query_delay=query_delay)
        else:  # pragma: no cover - 自定义题库未实现批量接口时的兜底
            answers = [self.query(item) for item in q_list]
            return answers
        self._count(len(q_list), sum(1 for answer in answers if answer))
        return answers

    def _count(self, queries: int, hits: int) -> None:
        with self._lock:
            self.queries += queries
            self.hits += hits


class Deadline:
    """软超时：超时后不再领取新章节，硬超时由调度器兜底。"""

    def __init__(self, timeout_minutes: int):
        self.timeout_minutes = max(0, int(timeout_minutes))
        self.expires_at = time.monotonic() + self.timeout_minutes * 60

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at

    def remaining_minutes(self) -> float:
        return max(0.0, (self.expires_at - time.monotonic()) / 60)


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def setup_runtime_environment(
    paths: AccountPaths,
    *,
    log_level: str = "INFO",
    chdir: bool = True,
) -> None:
    """准备账号运行时环境：日志文件、工作目录、tqdm 输出策略。"""
    paths.ensure()

    os.environ["CX_LOG_FILE"] = str(paths.log)
    os.environ.setdefault("CX_LOG_LEVEL", "TRACE")

    # 非交互环境（systemd / docker / cron）里关掉进度条，日志更干净
    if not sys.stderr.isatty():
        os.environ.setdefault("TQDM_DISABLE", "1")

    try:
        from chaoxing_core import logger as cx_logger

        cx_logger.reconfigure(
            log_file=str(paths.log),
            console_level=log_level,
            file_level="TRACE",
        )
    except Exception:  # pragma: no cover - 日志初始化失败不应阻塞运行
        pass

    if chdir:
        # 核心代码用 cookies.txt / cache.json 等相对路径，切到账号目录实现隔离
        os.chdir(paths.root)


def write_internal_config(config: ServerConfig, account: AccountConfig, paths: AccountPaths) -> Path:
    """把 YAML 配置渲染成核心可读的 config.ini。"""
    ini_text = render_ini(config, account)
    paths.ensure()
    paths.config_ini.write_text(ini_text, encoding="utf-8")
    return paths.config_ini


def load_tiku(account: AccountConfig, ini_path: Path) -> Any:
    """按渲染后的 config.ini 构建题库（含多题库回退）。"""
    import configparser

    from chaoxing_core.answer import Tiku
    from chaoxing_core.logger import logger

    parser = configparser.ConfigParser()
    parser.read(ini_path, encoding="utf-8")
    tiku_conf = dict(parser["tiku"]) if parser.has_section("tiku") else {}

    tiku = Tiku.get_tiku_from_config(tiku_conf, config_path=str(ini_path))
    tiku.init_tiku()

    if getattr(tiku, "DISABLE", False):
        logger.warning("题库未启用：答题环节将随机选择答案")
    else:
        logger.info("题库已启用: {}", ",".join(account.answer.provider_names) or "未知")

    if account.answer.check_llm_connection and any(
        name in {"AI", "SiliconFlow"} for name in account.answer.provider_names
    ):
        logger.info("正在校验大模型连通性（可用 answer.check_llm_connection: false 关闭）...")
        try:
            ok = tiku.check_llm_connection()
        except Exception as exc:  # noqa: BLE001
            logger.warning("大模型连通性校验异常: {}（继续运行）", exc)
            ok = True
        if not ok:
            logger.warning("大模型连通性校验失败，将继续运行，但答题质量可能下降")

    return tiku


def select_courses(
    all_courses: list[dict],
    study: StudyConfig,
    course_ids: Optional[list[str]] = None,
) -> list[dict]:
    """按 include/exclude 过滤课程，并按 (courseId, clazzId) 去重。"""
    include = [str(item) for item in (course_ids or study.include_courses)]
    deny = set(study.exclude_courses) | set(study.ignore_courses)

    selected: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for course in all_courses:
        course_id = str(course.get("courseId", ""))
        clazz_id = str(course.get("clazzId", ""))
        if include and course_id not in include:
            continue
        if course_id in deny:
            continue
        key = (course_id, clazz_id)
        if key in seen:
            continue
        seen.add(key)
        selected.append(course)
    return selected


@dataclass
class ChapterTask:
    index: int
    point: dict[str, Any]
    course: dict[str, Any]
    result: ChapterResult = ChapterResult.PENDING
    tries: int = 0

    def __lt__(self, other: "ChapterTask") -> bool:
        if not isinstance(other, ChapterTask):
            return NotImplemented
        return self.index < other.index


class JobProcessor:
    """章节任务队列（移植自上游 main.py，增加软超时与失败记录）。"""

    def __init__(
        self,
        chaoxing: Any,
        tasks: list[ChapterTask],
        config: dict[str, Any],
        *,
        deadline: Deadline | None = None,
    ):
        self.chaoxing = chaoxing
        self.speed = config.get("speed", 1.0)
        self.max_tries = int(config.get("max_tries", MAX_CHAPTER_RETRIES))
        self.notopen_action = config.get("notopen_action", "retry")
        self.retry_interval = config.get("retry_interval", 1.0)
        self.tasks = tasks
        self.failed_tasks: list[ChapterTask] = []
        self.skipped_tasks: list[ChapterTask] = []
        self.task_queue: PriorityQueue[ChapterTask] = PriorityQueue()
        self.retry_queue: PriorityQueue[ChapterTask] = PriorityQueue()
        self.threads: list[threading.Thread] = []
        self.worker_num = max(1, int(config.get("jobs", 4)))
        self.registration_lock = threading.Lock()
        self.deadline = deadline
        self._tiku_proxy: InstrumentedTiku | None = None
        # 未结束（既未成功也未判定失败/跳过）的任务数。
        # 不用 Queue.join()：任务在 retry_queue 与 task_queue 之间搬动时，
        # join() 可能出现瞬时"计数归零"，导致重试中的任务被丢弃。
        self._pending = len(tasks)
        self._done_condition = threading.Condition()

    def instrument(self, tiku: Any) -> InstrumentedTiku:
        """包裹题库对象，统计答题覆盖率，并让核心使用代理。"""
        proxy = InstrumentedTiku(tiku)
        self._tiku_proxy = proxy
        self.chaoxing.tiku = proxy  # type: ignore[attr-defined]
        return proxy

    @property
    def stats(self) -> dict[str, int]:
        if self._tiku_proxy is None:
            return {"queries": 0, "hits": 0}
        return {"queries": self._tiku_proxy.queries, "hits": self._tiku_proxy.hits}

    def run(self) -> None:
        from chaoxing_core.logger import logger

        for task in self.tasks:
            self.task_queue.put(task)

        for _ in range(self.worker_num):
            thread = threading.Thread(target=self.worker_thread, daemon=True)
            self.threads.append(thread)
            thread.start()

        retry_worker = threading.Thread(target=self.retry_thread, daemon=True)
        retry_worker.start()

        with self._done_condition:
            while self._pending > 0:
                if not self._done_condition.wait(timeout=1.0):
                    logger.debug("等待章节收尾中，剩余 {}", self._pending)

        # 所有任务都已判定最终状态，通知工作线程退出
        if hasattr(self.task_queue, "shutdown"):
            self.task_queue.shutdown(immediate=True)
        if hasattr(self.retry_queue, "shutdown"):
            self.retry_queue.shutdown(immediate=True)

        for thread in self.threads:
            thread.join(timeout=5)
        retry_worker.join(timeout=5)

    def _finish_task(self) -> None:
        """标记一个任务达到最终状态并唤醒主线程。"""
        with self._done_condition:
            self._pending = max(0, self._pending - 1)
            if self._pending == 0:
                self._done_condition.notify_all()

    def worker_thread(self) -> None:
        from chaoxing_core.logger import logger

        while True:
            try:
                task = self.task_queue.get()
            except ShutDown:
                logger.info("任务队列已关闭")
                return

            try:
                if self.deadline and self.deadline.expired:
                    task.result = ChapterResult.ABORTED
                    self._register_result(task, logger)
                    continue

                task.result = process_chapter(
                    self.chaoxing, task.course, task.point, self.speed
                )
                self._register_result(task, logger)
            except Exception as exc:  # noqa: BLE001 - 单章节异常不应终止整体
                logger.exception("章节处理异常 {}: {}", task.course.get("title"), exc)
                with self.registration_lock:
                    self.failed_tasks.append(task)
                self._finish_task()

    def _register_result(self, task: ChapterTask, logger: Any) -> None:
        with self.registration_lock:
            title = f"{task.course.get('title')} / {task.point.get('title')}"
            match task.result:
                case ChapterResult.SUCCESS:
                    logger.debug("章节完成: {}", title)
                case ChapterResult.NOT_OPEN:
                    if self.notopen_action == "continue":
                        logger.warning("章节未开启，按配置跳过: {}", title)
                        self.skipped_tasks.append(task)
                    else:
                        task.tries += 1
                        if task.tries >= self.max_tries:
                            logger.error(
                                "章节始终未开启（可能上一章的章节检测未完成）: {}", title
                            )
                            self.skipped_tasks.append(task)
                        else:
                            self.retry_queue.put(task)
                            return
                case ChapterResult.ABORTED:
                    logger.warning("已达软超时，跳过章节: {}", title)
                    self.skipped_tasks.append(task)
                case ChapterResult.SKIPPED:
                    logger.warning(
                        "章节在平台侧不可完成（作业已过期 / 资源已下架），跳过: {}", title
                    )
                    self.skipped_tasks.append(task)
                case ChapterResult.ERROR:
                    task.tries += 1
                    logger.warning(
                        "章节失败重试 {}/{}: {}", task.tries, self.max_tries, title
                    )
                    if task.tries >= self.max_tries:
                        logger.error("章节重试达到上限: {}", title)
                        self.failed_tasks.append(task)
                    else:
                        self.retry_queue.put(task)
                        return
                case _:
                    logger.error("章节状态异常 {}: {}", task.result, title)
                    self.failed_tasks.append(task)
        self._finish_task()

    def retry_thread(self) -> None:
        while True:
            try:
                task = self.retry_queue.get()
            except ShutDown:
                return
            if self.deadline and self.deadline.expired:
                with self.registration_lock:
                    task.result = ChapterResult.ABORTED
                    self.skipped_tasks.append(task)
                self._finish_task()
                continue
            self.task_queue.put(task)
            time.sleep(max(0.0, float(self.retry_interval or 0)) + random.uniform(0, 0.5))


def process_job(chaoxing: Any, course: dict, job: dict, job_info: dict, speed: float) -> Any:
    """处理单个任务点。"""
    from chaoxing_core.base import StudyResult
    from chaoxing_core.live import Live
    from chaoxing_core.live_process import LiveProcessor
    from chaoxing_core.logger import logger

    job_type = job.get("type")

    if job_type == "video":
        logger.trace("识别到视频任务, 课程: {} 任务ID: {}", course.get("title"), job.get("jobid"))
        result = chaoxing.study_video(course, job, job_info, _speed=speed, _type="Video")
        if result.is_failure():
            logger.warning("视频任务失败，尝试按音频任务重试")
            result = chaoxing.study_video(course, job, job_info, _speed=speed, _type="Audio")
        if result.is_failure():
            logger.warning(
                "视频任务异常 -> 课程: {} 任务ID: {}, 已跳过",
                course.get("title"),
                job.get("jobid"),
            )
        return result

    if job_type == "document":
        logger.trace("识别到文档任务, 课程: {}", course.get("title"))
        return chaoxing.study_document(course, job)

    if job_type == "workid":
        logger.trace("识别到章节检测任务, 课程: {}", course.get("title"))
        return chaoxing.study_work(course, job, job_info)

    if job_type == "read":
        logger.trace("识别到阅读任务, 课程: {}", course.get("title"))
        return chaoxing.study_read(course, job, job_info)

    if job_type == "live":
        logger.trace("识别到直播任务, 课程: {}", course.get("title"))
        try:
            defaults = {
                "userid": chaoxing.get_uid(),
                "clazzId": course.get("clazzId"),
                "knowledgeid": job_info.get("knowledgeid"),
            }
            live = Live(
                attachment=job,
                defaults=defaults,
                course_id=course.get("courseId"),
            )
            thread = threading.Thread(
                target=LiveProcessor.run_live,
                args=(live, speed),
                daemon=True,
            )
            thread.start()
            thread.join()
            return StudyResult.SUCCESS
        except Exception as exc:  # noqa: BLE001
            logger.error("处理直播任务时出错: {}", exc)
            return StudyResult.ERROR

    logger.error("未知任务类型: {}", job_type)
    return StudyResult.ERROR


def process_chapter(
    chaoxing: Any, course: dict[str, Any], point: dict[str, Any], speed: float
) -> ChapterResult:
    """处理单个章节的所有任务点。"""
    from chaoxing_core.logger import logger

    logger.info("当前章节: {}", point.get("title"))
    if point.get("has_finished"):
        logger.info("章节 {} 已完成全部任务点", point.get("title"))
        return ChapterResult.SUCCESS

    chaoxing.rate_limiter.limit_rate(random_time=True, random_min=0, random_max=0.2)

    jobs, job_info = chaoxing.get_job_list(course, point)

    if job_info.get("notOpen", False):
        return ChapterResult.NOT_OPEN

    if not jobs:
        # 空页面任务在 get_job_list 内部已处理
        return ChapterResult.SUCCESS

    skipped = False
    for job in jobs:
        result = process_job(chaoxing, course, job, job_info, speed)
        if result.is_failure():
            return ChapterResult.ERROR
        if result.is_skipped():
            skipped = True

    # 有任务点被平台判定为不可完成（作业过期/资源下架）时，章节标记为"跳过"，
    # 不再进入 5 次章节重试——重试也不会成功，只会刷一堆错误日志。
    return ChapterResult.SKIPPED if skipped else ChapterResult.SUCCESS


def sign_in_courses(chaoxing: Any, courses: list[dict[str, Any]]) -> dict[str, Any]:
    """尽力自动签到（普通签到为主，手势/位置签到仅做尝试）。"""
    from chaoxing_core.logger import logger

    summary: dict[str, Any] = {"checked": 0, "signed": 0, "failed": 0, "results": []}

    for course in courses:
        try:
            activities = chaoxing.get_activity_list(course) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取签到活动失败 {}: {}", course.get("title"), exc)
            continue

        for activity in activities:
            summary["checked"] += 1
            if activity.get("activeType") != 2:
                continue
            if activity.get("status") != 1:
                continue
            if activity.get("userStatus") not in (0, None):
                continue

            activity_id = activity.get("id") or activity.get("activePrimaryId")
            if not activity_id:
                continue

            record: dict[str, Any] = {
                "course": course.get("title"),
                "activity_id": activity_id,
            }
            try:
                chaoxing.pre_sign(course, activity_id)
                message = str(chaoxing.sign_in_normal(course, activity_id) or "")
                record["message"] = message[:200]
                if "成功" in message or "已签到" in message:
                    summary["signed"] += 1
                    record["ok"] = True
                    logger.info("签到成功: {} -> {}", course.get("title"), message)
                else:
                    summary["failed"] += 1
                    record["ok"] = False
                    logger.warning("签到返回异常: {} -> {}", course.get("title"), message)
            except Exception as exc:  # noqa: BLE001
                summary["failed"] += 1
                record["ok"] = False
                record["message"] = str(exc)
                logger.warning("签到失败 {}: {}", course.get("title"), exc)

            summary["results"].append(record)
            time.sleep(random.uniform(0.5, 1.5))

    return summary


def add_learning_count(chaoxing: Any, course: dict[str, Any], target_count: int) -> bool:
    """增加章节学习次数（可选功能）。"""
    from chaoxing_core.logger import logger
    from chaoxing_core.process import increase_learning_count_for_course

    try:
        increase_learning_count_for_course(
            chaoxing, course, {"target_count": target_count}
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("增加章节学习次数失败 {}: {}", course.get("title"), exc)
        return False


def run_account(
    config: ServerConfig,
    account: AccountConfig,
    *,
    course_ids: Optional[list[str]] = None,
    dry_run: bool = False,
    data_paths: DataPaths | None = None,
    chdir: bool = True,
    deadline: Deadline | None = None,
    run_id: Optional[str] = None,
) -> RunReport:
    """执行一次账号级刷课/答题，返回结构化报告。"""
    data = data_paths or config.data_paths()
    paths = data.account(account.name)
    setup_runtime_environment(paths, log_level=config.server.log_level, chdir=chdir)
    write_internal_config(config, account, paths)

    from chaoxing_core.base import Account, Chaoxing
    from chaoxing_core.exceptions import LoginError
    from chaoxing_core.logger import logger

    report = RunReport(
        account=account.name,
        run_id=run_id or new_run_id(),
        started_at=datetime.now().isoformat(timespec="seconds"),
        dry_run=dry_run,
        log_file=str(paths.log),
    )
    # 记录本次运行的大模型用量（token / 费用）。任务号与所属用户由调度器/网页
    # 通过环境变量传入，定时任务没有任务号时按账号聚合。
    usage_task_id = os.environ.get("CX_TASK_ID", "")
    usage_user = os.environ.get("CX_USER", "")
    usage_code = os.environ.get("CX_CODE", "")
    try:
        from server.usage import UsageContext, install_usage_hook

        install_usage_hook(
            data.db,
            UsageContext(
                user=usage_user,
                code=usage_code,
                account=account.name,
                task_id=usage_task_id,
                run_id=report.run_id,
            ),
            pricing=config.pricing or None,
            currency=config.server.currency,
        )
    except Exception as exc:  # noqa: BLE001 - 统计失败不影响刷课
        from chaoxing_core.logger import logger as _logger

        _logger.debug("初始化用量统计失败: {}", exc)
    started = time.monotonic()

    try:
        tiku = load_tiku(account, paths.config_ini)

        chaoxing = Chaoxing(
            account=Account(account.username, account.password),
            tiku=tiku,
            query_delay=account.answer.delay,
            work_max_retries=account.study.work_max_retries,
        )

        login_state = chaoxing.login(login_with_cookies=account.use_cookies)
        if not login_state.get("status"):
            raise LoginError(login_state.get("msg", "登录失败"))

        all_courses = chaoxing.get_course_list()
        courses = select_courses(all_courses, account.study, course_ids)
        if not courses:
            report.status = "success"
            report.message = "没有匹配到需要学习的课程"
            logger.warning(report.message)
            return report

        report.courses = [
            CourseStats(
                course_id=str(course.get("courseId", "")),
                clazz_id=str(course.get("clazzId", "")),
                title=str(course.get("title", "")),
            )
            for course in courses
        ]

        if dry_run:
            report.status = "success"
            report.message = f"dry-run：匹配到 {len(courses)} 门课程"
            return report

        if account.study.auto_sign:
            report.sign_in = sign_in_courses(chaoxing, courses)

        tasks: list[ChapterTask] = []
        index = 0
        for course, stat in zip(courses, report.courses):
            logger.info("正在读取课程章节: {}", course.get("title"))
            try:
                point_list = chaoxing.get_course_point(
                    course["courseId"], course["clazzId"], course["cpi"]
                )
            except Exception as exc:  # noqa: BLE001
                stat.status = "failed"
                stat.message = f"读取章节失败: {exc}"
                logger.error("读取课程章节失败 {}: {}", course.get("title"), exc)
                continue

            points = point_list.get("points", []) if point_list else []
            stat.chapters_total = len(points)
            if not points:
                stat.status = "success"
                stat.message = "无章节"
                continue

            for point in points:
                tasks.append(ChapterTask(index=index, point=point, course=course))
                index += 1
                if point.get("has_finished"):
                    stat.chapters_finished += 1

        if not tasks:
            report.status = "success"
            report.message = "所有课程均无可执行章节"
            return report

        processor = JobProcessor(
            chaoxing,
            tasks,
            {
                "speed": account.study.speed,
                "jobs": account.study.jobs,
                "notopen_action": account.study.notopen_action,
                "retry_interval": account.study.retry_interval,
            },
            deadline=deadline,
        )
        processor.instrument(tiku)

        logger.info("共 {} 个章节任务，开始执行（并发 {}）", len(tasks), processor.worker_num)
        processor.run()

        report.chapters_total = len(tasks)
        report.chapters_failed = len(processor.failed_tasks)
        report.chapters_skipped = len(processor.skipped_tasks)
        stats = processor.stats
        report.answer_total = stats["queries"]
        report.answer_covered = stats["hits"]

        per_course: dict[str, list[int]] = {}
        for task in tasks:
            bucket = per_course.setdefault(str(task.course.get("courseId")), [0, 0])
            if task.result == ChapterResult.SUCCESS:
                bucket[0] += 1
            elif task.result in {
                ChapterResult.ERROR,
                ChapterResult.NOT_OPEN,
                ChapterResult.ABORTED,
            }:
                bucket[1] += 1

        report.chapters_finished = sum(ok for ok, _ in per_course.values())
        for stat in report.courses:
            ok, fail = per_course.get(stat.course_id, [0, 0])
            stat.chapters_finished = max(stat.chapters_finished, ok)
            stat.chapters_failed = fail
            if stat.status == "failed":
                continue
            if fail:
                stat.status = "partial"
                stat.message = f"{fail} 个章节未完成"
            else:
                stat.status = "success"

        if account.study.add_learning_count:
            report.learning_count_added = all(
                add_learning_count(chaoxing, course, account.study.target_count)
                for course in courses
            )

        if report.chapters_failed:
            report.status = "partial"
            report.message = f"{report.chapters_failed} 个章节未完成"
        else:
            report.status = "success"
            report.message = "全部章节任务已完成"
        if report.chapters_skipped:
            # 跳过不是失败：作业已过期 / 视频已下架等原因，重试也无用
            report.message += f"（另有 {report.chapters_skipped} 个章节在平台侧不可完成，已跳过）"
        logger.info("运行结束: {} - {}", report.status, report.message)
        return report

    except Exception as exc:  # noqa: BLE001 - 统一转成失败报告
        report.status = "failed"
        report.message = f"{type(exc).__name__}: {exc}"
        try:
            from chaoxing_core.logger import logger

            logger.error("运行失败: {}", report.message)
            logger.error(traceback.format_exc())
        except Exception:  # pragma: no cover
            print(report.message, file=sys.stderr)
        return report
    finally:
        report.finished_at = datetime.now().isoformat(timespec="seconds")
        report.duration_seconds = time.monotonic() - started
        try:
            from server.usage import UsageRecorder

            with UsageRecorder(data.db) as usage_recorder:
                report.usage = (
                    usage_recorder.for_task(usage_task_id)
                    if usage_task_id
                    else usage_recorder.for_account(account.name)
                )
        except Exception:  # noqa: BLE001
            pass
        try:
            paths.ensure()
            paths.run_report(report.run_id).write_text(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass


def run_and_notify(
    config: ServerConfig,
    account: AccountConfig,
    *,
    course_ids: Optional[list[str]] = None,
    dry_run: bool = False,
    data_paths: DataPaths | None = None,
    run_id: Optional[str] = None,
) -> RunReport:
    """执行一次运行并按配置推送结果通知。"""
    timeout_minutes = config.server.run_timeout_minutes
    report = run_account(
        config,
        account,
        course_ids=course_ids,
        dry_run=dry_run,
        data_paths=data_paths,
        deadline=Deadline(timeout_minutes) if timeout_minutes else None,
        run_id=run_id,
    )

    notify = account.notify or config.notify
    should_notify = (report.status == "success" and notify.on_success) or (
        report.status != "success" and notify.on_failure
    )
    if should_notify and not dry_run:
        title = f"[超星] {account.name} {status_text(report.status)}"
        content = (
            f"账号: {account.name}\n"
            f"状态: {status_text(report.status)}\n"
            f"课程: {len(report.courses)} 门（完成 "
            f"{sum(1 for c in report.courses if c.status == 'success')} 门）\n"
            f"章节: 完成 {report.chapters_finished} / 总计 {report.chapters_total}"
            f"（失败 {report.chapters_failed}）\n"
            f"答题: 命中 {report.answer_covered} / 查询 {report.answer_total}\n"
            f"耗时: {report.duration_seconds / 60:.1f} 分钟\n"
            f"信息: {report.message}"
        )
        notify_send(notify, title, content)

    return report


def status_text(status: str) -> str:
    return {
        "success": "刷课完成 ✅",
        "partial": "部分完成 ⚠️",
        "failed": "运行失败 ❌",
        "timeout": "运行超时 ⏰",
        "skipped": "已跳过",
    }.get(status, status)
