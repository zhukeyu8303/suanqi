# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


TASK_RECORD_DIRECTORY = (
    Path.home()
    / ".suanqi"
    / "tasks"
)
# TASK_RECORD_DIRECTORY 是本地任务记录保存目录
# Windows 下一般是：
# C:\Users\用户名\.suanqi\tasks


def validate_instance_id(
    instance_id: str,
) -> str:
    """检查任务 ID，防止路径穿越。"""

    cleaned_instance_id = instance_id.strip()
    # cleaned_instance_id 是去除前后空格后的任务 ID

    if not cleaned_instance_id:
        raise ValueError(
            "任务 ID 不能为空"
        )

    if (
        "/" in cleaned_instance_id
        or "\\" in cleaned_instance_id
        or ".." in cleaned_instance_id
    ):
        raise ValueError(
            f"非法任务 ID：{instance_id}"
        )

    return cleaned_instance_id


def get_task_record_path(
    instance_id: str,
) -> Path:
    """返回任务记录文件路径。"""

    safe_instance_id = validate_instance_id(
        instance_id
    )

    return (
        TASK_RECORD_DIRECTORY
    / f"{safe_instance_id}.json"
    )


CONFIG_DIRECTORY = Path.home() / ".suanqi"
CONFIG_PATH = CONFIG_DIRECTORY / "config.json"


def save_task_record(
    instance_id: str,
    record: dict[str, Any],
) -> Path:
    """原子保存本地任务记录。"""

    record_path = get_task_record_path(
        instance_id
    )
    # record_path 是本地任务 JSON 文件路径

    record_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=record_path.parent,
            delete=False,
        ) as temporary_file:
            json.dump(
                record,
                temporary_file,
                ensure_ascii=False,
                indent=2,
            )

            temporary_file.flush()
            os.fsync(
                temporary_file.fileno()
            )

            temporary_path = Path(
                temporary_file.name
            )

        os.replace(
            temporary_path,
            record_path,
        )

    finally:
        if (
            temporary_path is not None
            and temporary_path.exists()
        ):
            temporary_path.unlink(
                missing_ok=True
            )

    return record_path


def load_task_record(
    instance_id: str,
) -> dict[str, Any]:
    """根据实例 ID 读取本地记录。"""

    record_path = get_task_record_path(
        instance_id
    )

    if not record_path.is_file():
        raise FileNotFoundError(
            "没有找到该实例的本地记录："
            f"{instance_id}\n"
            f"记录目录：{TASK_RECORD_DIRECTORY}"
        )

    with record_path.open(
        "r",
        encoding="utf-8",
    ) as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError(
            f"本地记录格式错误：{record_path}"
        )

    return data


def list_task_records() -> list[dict[str, Any]]:
    """列出全部本地任务记录。"""

    if not TASK_RECORD_DIRECTORY.is_dir():
        return []

    records: list[dict[str, Any]] = []
    for record_path in sorted(TASK_RECORD_DIRECTORY.glob("*.json")):
        try:
            with record_path.open("r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict):
                data["_record_path"] = str(record_path)
                records.append(data)
        except Exception:
            continue

    records.sort(
        key=lambda item: str(item.get("created_at") or item.get("updated_at") or ""),
        reverse=True,
    )
    return records

def update_task_record(
    instance_id: str,
    **changes: Any,
) -> dict[str, Any]:
    """更新任务记录中的部分字段。"""

    record = load_task_record(
        instance_id
    )

    record.update(
        changes
    )

    save_task_record(
        instance_id,
        record,
    )

    return record


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        data = json.load(file)
    return data if isinstance(data, dict) else {}


def save_config(config: dict[str, Any]) -> Path:
    CONFIG_DIRECTORY.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=CONFIG_DIRECTORY,
            delete=False,
        ) as temporary_file:
            json.dump(config, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temporary_path = Path(temporary_file.name)
        os.replace(temporary_path, CONFIG_PATH)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)
    return CONFIG_PATH
