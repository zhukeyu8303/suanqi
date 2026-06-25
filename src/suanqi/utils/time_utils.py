# -*- coding: utf-8 -*-

import re


# 每种时间单位对应的秒数
TIME_UNIT_SECONDS = {
    "d": 24 * 60 * 60,  # 天
    "h": 60 * 60,       # 小时
    "m": 60,            # 分钟
    "s": 1,             # 秒
}


def parse_duration(duration_text: str) -> int:
    """
    将时间字符串转换为秒数。

    支持：
    30s
    20m
    5h
    1h30m
    1d2h
    1d2h30m10s

    参数：
        duration_text：用户输入的时间字符串

    返回：
        总秒数
    """

    if not isinstance(duration_text, str):
        raise ValueError("最大运行时间必须是字符串，例如 5h、30m")

    # 删除字符串前后的空格，并统一转为小写
    normalized_text = duration_text.strip().lower()

    if not normalized_text:
        raise ValueError("最大运行时间不能为空")

    # 匹配“数字 + 单位”，例如 1h、30m
    matches = list(re.finditer(r"(\d+)([dhms])", normalized_text))

    if not matches:
        raise ValueError(
            f"无法识别最大运行时间：{duration_text}，"
            "支持格式：30s、20m、5h、1h30m、1d2h"
        )

    # 确保整个字符串都被正确匹配，防止出现 1hABC30m
    matched_text = "".join(match.group(0) for match in matches)

    if matched_text != normalized_text:
        raise ValueError(
            f"最大运行时间格式错误：{duration_text}，"
            "支持格式：30s、20m、5h、1h30m、1d2h"
        )

    total_seconds = 0  # 最终计算出的总秒数
    used_units = set()  # 已经出现过的单位，用于阻止 1h2h 这种写法

    for match in matches:
        value = int(match.group(1))  # 时间数值，例如 30
        unit = match.group(2)        # 时间单位，例如 m

        if unit in used_units:
            raise ValueError(
                f"时间单位不能重复：{duration_text}"
            )

        used_units.add(unit)

        if value < 0:
            raise ValueError("最大运行时间不能小于 0")

        total_seconds += value * TIME_UNIT_SECONDS[unit]

    if total_seconds <= 0:
        raise ValueError("最大运行时间必须大于 0 秒")

    return total_seconds