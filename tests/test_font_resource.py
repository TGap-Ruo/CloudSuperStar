# -*- coding: utf-8 -*-
"""字体映射表定位的回归测试。

超星的「字体加密题」需要把题目里的怪字（如 巚巓巘）通过字体哈希表还原成正常汉字。
如果映射表定位失败，解密会退化成"保留原文"，日志里就会出现：

    【多选题】下巚巓巘巕巗问巙詶是?

而服务端会把工作目录切到账号数据目录，因此资源定位必须与 cwd 无关。
"""

from pathlib import Path

import chaoxing_core.cxsecret_font as cxfont
from chaoxing_core.cxsecret_font import FontHashDAO, resource_path


def test_resource_path_finds_font_table_from_any_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # 模拟服务端 chdir 到账号目录
    path = resource_path("resource/font_map_table.json")
    assert Path(path).is_file(), path


def test_resource_path_handles_both_directory_names(tmp_path, monkeypatch):
    """resource/ 与 resources/ 两种目录布局都要能找到。"""
    monkeypatch.chdir(tmp_path)
    assert Path(resource_path("resource/font_map_table.json")).is_file()
    assert Path(resource_path("resources/font_map_table.json")).is_file()


def test_font_hash_dao_loads(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dao = FontHashDAO()
    assert len(dao.char_map) > 10000
    assert len(dao.hash_map) > 10000


def test_module_level_dao_is_loaded():
    """模块级单例必须真的加载到映射表，否则解密退化成原文（乱码）。"""
    assert len(cxfont.fonthash_dao.hash_map) > 10000
