# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    """返回当前 UTC 时间。"""

    return datetime.now(
        timezone.utc
    ).isoformat()


def atomic_write_json(
    file_path: Path,
    data: dict[str, Any],
) -> None:
    """原子写入 JSON 文件。"""

    file_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=file_path.parent,
        delete=False,
    ) as temporary_file:
        json.dump(
            data,
            temporary_file,
            ensure_ascii=False,
            indent=2,
        )
        temporary_name = temporary_file.name

    os.replace(
        temporary_name,
        file_path,
    )


def load_task_config(
    config_path: Path,
) -> dict[str, Any]:
    """读取任务配置。"""

    with config_path.open(
        "r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


def append_worker_log(
    file_path: Path,
    message: str,
) -> None:
    """写入守护进程日志。"""

    with file_path.open(
        "a",
        encoding="utf-8",
    ) as file:
        file.write(
            f"[{utc_now()}] {message}\n"
        )
        file.flush()


def build_status(
    config: dict[str, Any],
    status: str,
    **extra: Any,
) -> dict[str, Any]:
    """生成任务状态数据。"""

    data = {
        "task_id": config["task_id"],
        "status": status,
        "updated_at": utc_now(),
    }
    data.update(extra)
    return data


def validate_return_file(
    user_directory: Path,
    return_path: str,
) -> Path:
    """验证返回文件没有越过任务目录。"""

    base = user_directory.resolve()
    target = (
        base / return_path
    ).resolve()

    target.relative_to(base)
    return target


def main() -> int:
    if len(sys.argv) != 2:
        return 2

    config_path = Path(sys.argv[1]).resolve()
    config = load_task_config(config_path)

    user_directory = Path(
        config["user_directory"]
    ).resolve()
    control_directory = Path(
        config["control_directory"]
    ).resolve()

    status_path = control_directory / "status.json"
    task_log_path = control_directory / "task.log"
    worker_log_path = control_directory / "worker.log"
    manifest_path = control_directory / "manifest.json"
    pid_path = control_directory / "main.pid"

    control_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    started_at = utc_now()

    append_worker_log(
        worker_log_path,
        "守护进程启动",
    )

    atomic_write_json(
        status_path,
        build_status(
            config,
            "STARTING",
            started_at=started_at,
            worker_pid=os.getpid(),
        ),
    )

    command = [
        "runuser",
        "--user",
        "suanqi-task",
        "--",
        "nice",
        "-n",
        "5",
        config["virtualenv_python"],
        "-u",
        config["main_filename"],
    ]

    environment = os.environ.copy()
    cpu_count = os.cpu_count() or 1
    usable_cpu_count = max(
        1,
        cpu_count - 1,
    )

    for variable_name in (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        environment[variable_name] = str(
            usable_cpu_count
        )

    process: subprocess.Popen[bytes] | None = None

    try:
        with task_log_path.open(
            "ab",
            buffering=0,
        ) as task_log:
            process = subprocess.Popen(
                command,
                cwd=str(user_directory),
                stdin=subprocess.DEVNULL,
                stdout=task_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=environment,
            )

            pid_path.write_text(
                str(process.pid),
                encoding="utf-8",
            )

            atomic_write_json(
                status_path,
                build_status(
                    config,
                    "RUNNING",
                    started_at=started_at,
                    worker_pid=os.getpid(),
                    main_pid=process.pid,
                ),
            )

            exit_code = process.wait()

        finished_at = utc_now()

        final_status = (
            "SUCCESS"
            if exit_code == 0
            else "FAILED"
        )

        atomic_write_json(
            status_path,
            build_status(
                config,
                final_status,
                started_at=started_at,
                finished_at=finished_at,
                worker_pid=os.getpid(),
                main_pid=process.pid,
                exit_code=exit_code,
            ),
        )

        files = []
        missing_files = []

        for requested_path in config["return_files"]:
            target = validate_return_file(
                user_directory,
                requested_path,
            )

            exists = target.is_file()

            files.append(
                {
                    "requested_path": requested_path,
                    "absolute_path": str(target),
                    "exists": exists,
                    "size": (
                        target.stat().st_size
                        if exists
                        else None
                    ),
                }
            )

            if not exists:
                missing_files.append(
                    requested_path
                )

        atomic_write_json(
            manifest_path,
            {
                "task_id": config["task_id"],
                "completed": True,
                "status": final_status,
                "program_exit_code": exit_code,
                "started_at": started_at,
                "finished_at": finished_at,
                "files": files,
                "missing_files": missing_files,
            },
        )

        append_worker_log(
            worker_log_path,
            f"用户程序结束，退出码={exit_code}",
        )

        return 0 if exit_code == 0 else 1

    except BaseException as error:
        if process is not None:
            try:
                os.killpg(
                    process.pid,
                    signal.SIGTERM,
                )
            except Exception:
                pass

        atomic_write_json(
            status_path,
            build_status(
                config,
                "WORKER_FAILED",
                started_at=started_at,
                error_type=error.__class__.__name__,
                error_message=str(error),
            ),
        )

        append_worker_log(
            worker_log_path,
            f"守护进程异常：{error}",
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(main())
