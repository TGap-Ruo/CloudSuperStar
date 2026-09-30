import re


def check_single(answer):
    if answer is None:
        return False

    text = str(answer).strip()
    if not text:
        return False

    # 单选答案里的顿号/逗号常常只是中文句内标点（例如
    # "靠近数据源的边缘计算设备(如基站、智能网关)"），不能据此判定成多选，
    # 否则正确答案会被丢弃、退化成随机作答（实测踩过这个坑）。
    # 只有真正的“多段答案”分隔符才算非单选。
    strong_delimiters = ["\n", "|", "#", "\t", "\r"]
    for sep in strong_delimiters:
        parts = [p.strip() for p in text.split(sep) if p.strip()]
        if len(parts) > 1:
            return False

    # 形如 "AB"、"A、B"、"A,B" 的纯选项字母串 → 显然不是单选题的答案文本
    letters = re.sub(r'[\s,，、;；/|]+', '', text)
    if len(letters) > 1 and re.fullmatch(r'[A-Za-z]+', letters):
        return False

    return True


def check_multiple(answer):
    _t = cut(answer)
    if _t is not None and len(_t) > 0:
        return True
    return False


def check_judgement(answer, true_list, false_list):
    val = str(answer).strip().lower()
    if val in ['true', 't', '1', '对', '正确', '√', '是', 'yes', 'y'] or val in [x.lower() for x in true_list]:
        return 1
    elif val in ['false', 'f', '0', '错', '错误', '×', '否', 'no', 'n', '不对', '不正确'] or val in [x.lower() for x in
                                                                                                     false_list]:
        return 0
    else:
        return -1


def check_completion(answer):
    if len(answer) > 0:
        return True
    else:
        return False


def check_answer(answer, type, tiku):  # 只会写小杯代码，这里用个tiku感觉怪怪的，但先这么写着
    # 如果是手动模式或多题库回退包装器，直接信任
    # （手动模式豁免常规校验；回退包装器因其子题库在各自环节均已单独校验过，此处无需二次校验，以防二次过滤误杀）
    if getattr(tiku, 'is_manual', False) or getattr(tiku, 'skip_answer_validation', False):
        return True

    if type == 'single':
        if check_single(answer) and check_judgement(answer, tiku.true_list, tiku.false_list) == -1:
            return True
    elif type == 'multiple':
        if check_multiple(answer) and check_judgement(answer, tiku.true_list, tiku.false_list) == -1:
            return True
    elif type == 'completion':
        if check_completion(answer):
            return True
    elif type == 'judgement':
        if check_judgement(answer, tiku.true_list, tiku.false_list) != -1:
            return True
    else:  # 未知类型不匹配
        return True
    return False


def cut(answer):
    cut_char = [
        "\n",
        ",",
        "，",
        "|",
        "\r",
        "\t",
        "#",
        "*",
        "-",
        "_",
        "+",
        "@",
        "~",
        "/",
        "\\",
        ".",
        "&",
        " ",
        "、",
    ]
    if answer is None:
        return None

    answer = str(answer)
    for char in cut_char:
        if char not in answer:
            continue
        res = [opt.strip() for opt in answer.split(char) if opt.strip()]
        if res:
            return res
    stripped = answer.strip()
    return [stripped] if stripped else None
