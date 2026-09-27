# -*- coding: utf-8 -*-
"""超星学习通 API 核心（服务端版）。

本包内容改造自 GPL-3.0 开源项目 Samueli924/chaoxing
(https://github.com/Samueli924/chaoxing)，仅做了服务端运行所必需的少量改动：

1. ``logger`` 的文件输出路径/级别改为由环境变量 ``CX_LOG_FILE`` /
   ``CX_LOG_LEVEL`` 控制，避免在只读或非交互环境中写死当前工作目录。

除以上改动外，登录、课程/章节解析、视频进度上报、作业答题、题库对接等
逻辑均与上游保持一致，方便后续跟进上游更新。
"""


def formatted_output(_status, _text, _data):
    return {"status": _status, "msg": _text, "data": _data}
