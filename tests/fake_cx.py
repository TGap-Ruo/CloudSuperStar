# -*- coding: utf-8 -*-
"""离线假超星服务：通过替换 ``requests.Session.send`` 拦截所有 HTTP 请求。

这样可以在不接触真实学习通、也不需要账号的前提下，完整跑通
登录 -> 课程列表 -> 章节列表 -> 任务点 -> 视频/答题 -> 提交 的全链路。
返回的 HTML/JSON 结构严格对照 ``chaoxing_core.decode`` 的解析要求构造。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

import requests


def make_response(
    request: requests.PreparedRequest,
    *,
    status: int = 200,
    text: str = "",
    json_data: Any = None,
    reason: str = "OK",
) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.reason = reason
    response.url = request.url
    response.request = request
    response.encoding = "utf-8"
    if json_data is not None:
        response.headers["Content-Type"] = "application/json; charset=utf-8"
        response._content = json.dumps(json_data, ensure_ascii=False).encode("utf-8")
    else:
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response._content = text.encode("utf-8")
    return response


COURSE_LIST_HTML = """
<html><body>
  <div class="course" id="course_2151141" info="测试课程" roleid="3">
    <input class="clazzId" value="107515845">
    <input class="courseId" value="2151141">
    <a href="https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse?courseid=2151141&clazzid=107515845&cpi=338350298&ut=s">
      <span class="course-name" title="测试课程"></span>
    </a>
    <p class="margint10" title="这是一门用于测试的课程"></p>
    <p class="color3" title="测试老师"></p>
  </div>
</body></html>
"""

INTERACTION_HTML = "<html><body><ul class='file-list'></ul></body></html>"

POINTS_HTML = """
<html><body>
  <div class="chapter_unit">
    <ul>
      <li><div id="cur1">
        <a class="clicktitle" href="#">第一章 视频任务</a>
        <input class="knowledgeJobCount" value="1">
      </div></li>
      <li><div id="cur2">
        <a class="clicktitle" href="#">第二章 章节检测</a>
        <input class="knowledgeJobCount" value="1">
      </div></li>
      <li><div id="cur3">
        <a class="clicktitle" href="#">第三章 已完成章节</a>
        <span class="bntHoverTips">已完成</span>
      </div></li>
    </ul>
  </div>
</body></html>
"""


def cards_html(chapter_id: str) -> str:
    """按章节 ID 生成任务点卡片页面。"""
    defaults = {
        "ktoken": f"ktoken-{chapter_id}",
        "mtEnc": "mtEnc-value",
        "reportTimeInterval": 60,
        "defenc": "defenc-value",
        "cardid": f"card-{chapter_id}",
        "cpi": "338350298",
        "qnenc": "qnenc-value",
        "knowledgeid": chapter_id,
    }

    if chapter_id == "1":
        attachments = [
            {
                "type": "video",
                "job": {"id": "job-video-1"},
                "jobid": "job-video-1",
                "mid": "mid-video-1",
                "objectId": "object-video-1",
                "otherInfo": "nodeId_1-rt_d",
                "playTime": 0,
                "isPassed": False,
                "property": {"name": "1.1 视频讲解", "rt": "0.9"},
                "attDuration": "",
                "attDurationEnc": "",
                "videoFaceCaptureEnc": "",
            }
        ]
    elif chapter_id == "2":
        attachments = [
            {
                "type": "workid",
                "job": {"id": "work-abc123"},
                "jobid": "work-abc123",
                "mid": "mid-work-1",
                "enc": "enc-work-1",
                "aid": "aid-work-1",
                "otherInfo": "nodeId_2-",
                "isPassed": False,
                "property": {"name": "章节检测"},
            }
        ]
    else:
        attachments = []

    payload = json.dumps(
        {"defaults": defaults, "attachments": attachments}, ensure_ascii=False
    )
    # 解析器会先去掉空格再匹配 mArg=...;
    return f"<html><body><script>var mArg={payload};</script></body></html>"


QUIZ_HTML = """
<html><body>
<form action="/mooc-ans/work/addStudentWorkNew" method="post">
  <input type="hidden" name="courseId" value="2151141">
  <input type="hidden" name="classId" value="107515845">
  <input type="hidden" name="enc" value="enc-work-1">

  <div class="singleQuesId" data="q1">
    <div class="TiMu" data="0">
      <div class="Zy_TItle"><div class="clearfix">1. 中国的首都是哪座城市？</div></div>
      <ul>
        <li aria-label="A、上海">A、上海</li>
        <li aria-label="B、北京">B、北京</li>
        <li aria-label="C、广州">C、广州</li>
      </ul>
    </div>
  </div>

  <div class="singleQuesId" data="q2">
    <div class="TiMu" data="3">
      <div class="Zy_TItle"><div class="clearfix">2. 地球是圆的。</div></div>
      <ul>
        <li aria-label="A、正确">A、正确</li>
        <li aria-label="B、错误">B、错误</li>
      </ul>
    </div>
  </div>

  <div class="singleQuesId" data="q3">
    <div class="TiMu" data="1">
      <div class="Zy_TItle"><div class="clearfix">3. 以下哪些是编程语言？</div></div>
      <ul>
        <li aria-label="A、Python">A、Python</li>
        <li aria-label="B、HTML">B、HTML</li>
        <li aria-label="C、Java">C、Java</li>
      </ul>
    </div>
  </div>

  <input type="hidden" name="submitType" value="1">
</form>
</body></html>
"""


@dataclass
class FakeChaoxing:
    """极简的假超星服务。"""

    login_ok: bool = True
    work_submit_ok: bool = True
    work_submit_fail_times: int = 0  # 前 N 次提交故意失败，用于测试重试
    video_already_passed: bool = True
    recorded: dict[str, Any] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    extra_routes: dict[str, Callable[[requests.PreparedRequest], requests.Response]] = field(
        default_factory=dict
    )

    # ---------------------------------------------------------------- routing
    def handle(self, request: requests.PreparedRequest) -> requests.Response:
        parsed = urlparse(str(request.url))
        host, path = parsed.netloc, parsed.path
        query = parse_qs(parsed.query)
        self.calls.append(f"{request.method} {host}{path}")

        key = f"{request.method} {path}"
        if key in self.extra_routes:
            return self.extra_routes[key](request)

        if host == "passport2.chaoxing.com" and path == "/fanyalogin":
            if self.login_ok:
                return make_response(request, json_data={"status": True, "msg2": ""})
            return make_response(request, json_data={"status": False, "msg2": "账号或密码错误"})

        if path == "/mooc2-ans/visit/courselistdata":
            return make_response(request, text=COURSE_LIST_HTML)

        if path == "/mooc2-ans/visit/interaction":
            return make_response(request, text=INTERACTION_HTML)

        if path == "/mooc2-ans/mycourse/studentcourse":
            return make_response(request, text=POINTS_HTML)

        if path == "/mooc-ans/knowledge/cards":
            chapter_id = (query.get("knowledgeid") or [""])[0]
            num = int((query.get("num") or ["0"])[0])
            if num == 0:
                return make_response(request, text=cards_html(chapter_id))
            return make_response(request, text="<html><body></body></html>")

        if path.startswith("/ananas/status/"):
            return make_response(
                request,
                json_data={
                    "status": "success",
                    "dtoken": "dtoken-1",
                    "crc": "crc-1",
                    "key": "key-1",
                    "duration": 10,
                },
            )

        if path.startswith("/mooc-ans/multimedia/log/a/"):
            self.recorded["video_log"] = dict(query)
            return make_response(
                request, json_data={"isPassed": self.video_already_passed}
            )

        if path == "/ananas/job/document":
            return make_response(request, json_data={"msg": "文档任务完成"})

        if path == "/ananas/job/readv2":
            return make_response(request, json_data={"msg": "阅读任务完成"})

        if path == "/mooc-ans/api/work":
            return make_response(request, text=QUIZ_HTML)

        if path == "/mooc-ans/work/addStudentWorkNew":
            self.recorded["work_submit"] = parse_qs(
                (request.body or b"").decode("utf-8")
                if isinstance(request.body, bytes)
                else str(request.body or "")
            )
            fail = (not self.work_submit_ok) or (
                self.recorded.get("work_submit_count", 0) < self.work_submit_fail_times
            )
            self.recorded["work_submit_count"] = (
                self.recorded.get("work_submit_count", 0) + 1
            )
            return make_response(
                request,
                json_data={
                    "status": not fail,
                    "msg": "提交失败" if fail else "提交成功",
                },
            )

        if path == "/mooc-ans/mycourse/studentstudyAjax":
            return make_response(request, json_data={"msg": "空页面任务完成"})

        if path == "/v2/apis/active/student/activelist":
            return make_response(
                request,
                json_data={
                    "result": 1,
                    "data": {
                        "activeList": [
                            {
                                "id": "act-1",
                                "activeType": 2,
                                "status": 1,
                                "userStatus": 0,
                            }
                        ]
                    },
                },
            )

        if path == "/newsign/preSign":
            return make_response(request, text="<html>签到</html>")

        if path == "/pptSign/stuSignajax":
            return make_response(request, text="签到成功")

        return make_response(request, status=404, text="not found", reason="Not Found")


class FakeTransport:
    """替换 Session.send 的适配器。"""

    def __init__(self, fake: FakeChaoxing):
        self.fake = fake

    def __call__(self, session: requests.Session, request: requests.PreparedRequest, **kwargs: Any):
        response = self.fake.handle(request)
        if response.status_code >= 400:
            # 模拟 requests 在 4xx/5xx 时的行为：返回响应而不是抛异常
            pass
        # 把 Set-Cookie 合并进会话（模拟 requests 的 cookie 提取）
        if response.cookies:
            session.cookies.update(response.cookies)
        return response


def install(monkeypatch, fake: Optional[FakeChaoxing] = None) -> FakeChaoxing:
    """安装假服务并补齐登录后需要的 Cookie（_uid / fid）。"""
    fake = fake or FakeChaoxing()
    original = requests.sessions.Session.send

    def send(session, request, **kwargs):
        response = FakeTransport(fake)(session, request, **kwargs)
        # 登录成功后补上核心代码依赖的 _uid / fid（真实环境由响应 Set-Cookie 提供）
        if urlparse(str(request.url)).path == "/fanyalogin" and fake.login_ok:
            session.cookies.set("_uid", "123456")
            session.cookies.set("fid", "1024")
            session.cookies.set("_d", "dummy")
        return response

    monkeypatch.setattr(requests.sessions.Session, "send", send)
    return fake
