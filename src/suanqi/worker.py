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


DEFAULT_MAX_USE_SECONDS = 5 * 60 * 60
# 用户程序默认最大运行时间：5小时

DEFAULT_TERMINATE_GRACE_SECONDS = 30
# 发送 SIGTERM 后等待程序自行退出的时间：30秒

DEFAULT_PREPARATION_TIMEOUT_SECONDS = (
    0.5 * 60 * 60
)
# 创建虚拟环境和安装依赖默认最多允许0.5小时


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

        # 将 Python 缓冲区中的内容写入操作系统
        temporary_file.flush()

        # 尽量确保数据已经写入磁盘
        os.fsync(
            temporary_file.fileno()
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

    file_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

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

    # 如果 target 不在 base 目录中，
    # relative_to 会抛出 ValueError
    target.relative_to(base)

    return target


def collect_return_files(
    config: dict[str, Any],
    user_directory: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    检查用户要求返回的文件。

    返回：
        files：
            所有返回文件的检查结果。

        missing_files：
            服务器上没有找到的文件。
    """

    files: list[dict[str, Any]] = []
    missing_files: list[str] = []

    return_files = (
        config.get("return_files")
        or []
    )

    for requested_path in return_files:
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

    return files, missing_files


def stop_process_group(
    process: subprocess.Popen[bytes],
    grace_seconds: int,
    worker_log_path: Path,
) -> tuple[int | None, bool]:
    """
    停止整个用户程序进程组。

    首先发送 SIGTERM，让程序有机会保存数据并正常退出。

    如果等待 grace_seconds 秒后仍未退出，
    再发送 SIGKILL 强制结束。

    返回：
        exit_code：
            用户程序最终退出码。

        forced_kill：
            是否使用了 SIGKILL 强制结束。
    """

    if process.poll() is not None:
        return process.returncode, False

    try:
        process_group_id = os.getpgid(
            process.pid
        )
        # process_group_id 代表用户任务所属的进程组 ID

        append_worker_log(
            worker_log_path,
            (
                "正在向用户程序进程组发送 "
                f"SIGTERM，进程组={process_group_id}"
            ),
        )

        os.killpg(
            process_group_id,
            signal.SIGTERM,
        )

    except ProcessLookupError:
        # 发送信号之前程序已经退出
        return process.poll(), False

    try:
        exit_code = process.wait(
            timeout=grace_seconds
        )

        append_worker_log(
            worker_log_path,
            (
                "用户程序在宽限时间内退出，"
                f"退出码={exit_code}"
            ),
        )

        return exit_code, False

    except subprocess.TimeoutExpired:
        append_worker_log(
            worker_log_path,
            (
                f"等待 {grace_seconds} 秒后程序仍未退出，"
                "准备发送 SIGKILL"
            ),
        )

    try:
        process_group_id = os.getpgid(
            process.pid
        )

        os.killpg(
            process_group_id,
            signal.SIGKILL,
        )

    except ProcessLookupError:
        # 发送 SIGKILL 前程序刚好退出
        pass

    try:
        exit_code = process.wait(
            timeout=10
        )
    except subprocess.TimeoutExpired:
        # 正常情况下 SIGKILL 后会很快退出
        exit_code = process.poll()

    append_worker_log(
        worker_log_path,
        (
            "用户程序已被强制结束，"
            f"退出码={exit_code}"
        ),
    )

    return exit_code, True

def run_preparation_command(
    command: list[str],
    user_directory: Path,
    task_log_path: Path,
    timeout_seconds: int,
    description: str,
) -> None:
    """
    以 suanqi-task 用户身份执行环境准备命令。

    命令输出会写入 task.log，
    因此客户端可以实时看到安装过程。
    """

    full_command = [
        "runuser",
        "--user",
        "suanqi-task",
        "--",
        *command,
    ]

    with task_log_path.open(
        "ab",
        buffering=0,
    ) as task_log:
        task_log.write(
            (
                f"\n[SuanQi] {description}\n"
            ).encode("utf-8")
        )

        try:
            result = subprocess.run(
                full_command,
                cwd=str(user_directory),
                stdin=subprocess.DEVNULL,
                stdout=task_log,
                stderr=subprocess.STDOUT,
                timeout=timeout_seconds,
                check=False,
            )

        except subprocess.TimeoutExpired as error:
            raise TimeoutError(
                f"{description}超时"
            ) from error

    if result.returncode != 0:
        raise RuntimeError(
            f"{description}失败，"
            f"退出码={result.returncode}"
        )

def create_task_virtualenv(
    user_directory: Path,
    task_log_path: Path,
    timeout_seconds: int,
) -> Path:
    """由 worker 创建任务虚拟环境。"""

    virtualenv_directory = (
        user_directory / ".venv"
    )
    # virtualenv_directory 是任务独立虚拟环境目录

    virtualenv_python = (
        virtualenv_directory
        / "bin"
        / "python"
    )

    if virtualenv_python.is_file():
        return virtualenv_python

    run_preparation_command(
        command=[
            "/usr/bin/python3",
            "-m",
            "venv",
            str(virtualenv_directory),
        ],
        user_directory=user_directory,
        task_log_path=task_log_path,
        timeout_seconds=timeout_seconds,
        description="正在创建 Python 虚拟环境……",
    )

    if not virtualenv_python.is_file():
        raise RuntimeError(
            "虚拟环境创建完成后未找到 Python"
        )

    return virtualenv_python

def install_task_requirements(
    virtualenv_python: Path,
    user_directory: Path,
    requirements_filename: str | None,
    task_log_path: Path,
    timeout_seconds: int,
) -> None:
    """由 worker 安装 requirements.txt。"""

    if not requirements_filename:
        return

    requirements_path = (
        user_directory
        / requirements_filename
    ).resolve()

    requirements_path.relative_to(
        user_directory.resolve()
    )

    if not requirements_path.is_file():
        raise FileNotFoundError(
            f"未找到 requirements 文件："
            f"{requirements_path}"
        )

    run_preparation_command(
        command=[
            str(virtualenv_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(requirements_path),
        ],
        user_directory=user_directory,
        task_log_path=task_log_path,
        timeout_seconds=timeout_seconds,
        description="正在安装 requirements.txt……",
    )

def install_task_packages(
    virtualenv_python: Path,
    user_directory: Path,
    packages: list[str],
    task_log_path: Path,
    timeout_seconds: int,
) -> None:
    """由 worker 安装用户通过 -i 指定的软件包。"""

    cleaned_packages = [
        str(package).strip()
        for package in packages
        if str(package).strip()
    ]

    if not cleaned_packages:
        return

    run_preparation_command(
        command=[
            str(virtualenv_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            *cleaned_packages,
        ],
        user_directory=user_directory,
        task_log_path=task_log_path,
        timeout_seconds=timeout_seconds,
        description=(
            "正在安装额外依赖："
            + ", ".join(cleaned_packages)
        ),
    )

def prepare_task_environment(
    config: dict[str, Any],
    user_directory: Path,
    task_log_path: Path,
    preparation_timeout_seconds: int,
) -> Path:
    """
    准备用户程序运行环境。

    返回：
        虚拟环境中的 Python 路径。
    """

    preparation_started = (
        datetime.now(timezone.utc)
    )

    virtualenv_python = (
        create_task_virtualenv(
            user_directory=(
                user_directory
            ),
            task_log_path=task_log_path,
            timeout_seconds=(
                preparation_timeout_seconds
            ),
        )
    )

    elapsed_seconds = (
        datetime.now(timezone.utc)
        - preparation_started
    ).total_seconds()
    # elapsed_seconds 是环境准备已经使用的秒数

    remaining_seconds = max(
        1,
        int(
            preparation_timeout_seconds
            - elapsed_seconds
        ),
    )

    install_task_requirements(
        virtualenv_python=virtualenv_python,
        user_directory=user_directory,
        requirements_filename=config.get(
            "requirements_filename"
        ),
        task_log_path=task_log_path,
        timeout_seconds=remaining_seconds,
    )

    elapsed_seconds = (
        datetime.now(timezone.utc)
        - preparation_started
    ).total_seconds()

    remaining_seconds = max(
        1,
        int(
            preparation_timeout_seconds
            - elapsed_seconds
        ),
    )

    install_task_packages(
        virtualenv_python=virtualenv_python,
        user_directory=user_directory,
        packages=config.get("packages") or [],
        task_log_path=task_log_path,
        timeout_seconds=remaining_seconds,
    )

    total_elapsed_seconds = (
        datetime.now(timezone.utc)
        - preparation_started
    ).total_seconds()

    if (
        total_elapsed_seconds
        > preparation_timeout_seconds
    ):
        raise TimeoutError(
            "环境准备超过最大允许时间"
        )

    return virtualenv_python



def main() -> int:
    """
    远程守护进程主流程。

    流程：
        读取任务配置
        → 准备 Python 环境
        → 启动用户程序
        → 处理运行超时
        → 收集返回文件
        → 写入状态和 manifest
    """

    if len(sys.argv) != 2:
        return 2

    config_path = Path(
        sys.argv[1]
    ).resolve()

    config: dict[str, Any] = {}

    user_directory: Path | None = None
    control_directory: Path | None = None

    status_path: Path | None = None
    task_log_path: Path | None = None
    worker_log_path: Path | None = None
    manifest_path: Path | None = None
    pid_path: Path | None = None

    process: subprocess.Popen[bytes] | None = None

    worker_started_at = utc_now()
    # worker_started_at 代表守护进程启动时间

    preparation_started_at: str | None = None
    # preparation_started_at 代表环境准备开始时间

    preparation_finished_at: str | None = None
    # preparation_finished_at 代表环境准备完成时间

    program_started_at: str | None = None
    # program_started_at 代表 main.py 真正启动时间

    max_use_seconds = DEFAULT_MAX_USE_SECONDS
    # max_use_seconds 代表 main.py 最大运行秒数

    terminate_grace_seconds = (
        DEFAULT_TERMINATE_GRACE_SECONDS
    )
    # terminate_grace_seconds 代表发送 SIGTERM 后等待秒数

    preparation_timeout_seconds = (
        DEFAULT_PREPARATION_TIMEOUT_SECONDS
    )
    # preparation_timeout_seconds 代表环境准备最大秒数

    try:
        config = load_task_config(
            config_path
        )

        user_directory = Path(
            config["user_directory"]
        ).resolve()

        control_directory = Path(
            config["control_directory"]
        ).resolve()

        status_path = (
            control_directory
            / "status.json"
        )

        task_log_path = (
            control_directory
            / "task.log"
        )

        worker_log_path = (
            control_directory
            / "worker.log"
        )

        manifest_path = (
            control_directory
            / "manifest.json"
        )

        pid_path = (
            control_directory
            / "main.pid"
        )

        control_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        max_use_seconds = int(
            config.get(
                "max_use_seconds",
                DEFAULT_MAX_USE_SECONDS,
            )
        )

        terminate_grace_seconds = int(
            config.get(
                "terminate_grace_seconds",
                DEFAULT_TERMINATE_GRACE_SECONDS,
            )
        )

        preparation_timeout_seconds = int(
            config.get(
                "preparation_timeout_seconds",
                DEFAULT_PREPARATION_TIMEOUT_SECONDS,
            )
        )

        if max_use_seconds <= 0:
            raise ValueError(
                "max_use_seconds 必须大于 0"
            )

        if terminate_grace_seconds < 0:
            raise ValueError(
                "terminate_grace_seconds 不能小于 0"
            )

        if preparation_timeout_seconds <= 0:
            raise ValueError(
                "preparation_timeout_seconds 必须大于 0"
            )

        append_worker_log(
            worker_log_path,
            "守护进程启动",
        )

        append_worker_log(
            worker_log_path,
            (
                "环境准备最大时间："
                f"{preparation_timeout_seconds} 秒，"
                "程序最大运行时间："
                f"{max_use_seconds} 秒，"
                "终止宽限时间："
                f"{terminate_grace_seconds} 秒"
            ),
        )

        atomic_write_json(
            status_path,
            build_status(
                config,
                "STARTING",
                worker_started_at=(
                    worker_started_at
                ),
                worker_pid=os.getpid(),
                preparation_timeout_seconds=(
                    preparation_timeout_seconds
                ),
                max_use_seconds=(
                    max_use_seconds
                ),
                terminate_grace_seconds=(
                    terminate_grace_seconds
                ),
                message="守护进程正在启动",
            ),
        )

        # =========================
        # 第一阶段：准备 Python 环境
        # =========================

        preparation_started_at = utc_now()

        atomic_write_json(
            status_path,
            build_status(
                config,
                "PREPARING",
                worker_started_at=(
                    worker_started_at
                ),
                preparation_started_at=(
                    preparation_started_at
                ),
                worker_pid=os.getpid(),
                preparation_timeout_seconds=(
                    preparation_timeout_seconds
                ),
                max_use_seconds=(
                    max_use_seconds
                ),
                message="正在准备 Python 运行环境",
            ),
        )

        append_worker_log(
            worker_log_path,
            "开始准备 Python 运行环境",
        )

        virtualenv_python = (
            prepare_task_environment(
                config=config,
                user_directory=user_directory,
                task_log_path=task_log_path,
                preparation_timeout_seconds=(
                    preparation_timeout_seconds
                ),
            )
        )
        # virtualenv_python 是 worker 创建的虚拟环境 Python 路径

        preparation_finished_at = utc_now()

        append_worker_log(
            worker_log_path,
            (
                "Python 运行环境准备完成，"
                f"解释器={virtualenv_python}"
            ),
        )

        # =========================
        # 第二阶段：构造用户程序命令
        # =========================

        main_filename = str(
            config.get(
                "main_filename",
                "main.py",
            )
        )
        # main_filename 是需要运行的 Python 文件名

        main_path = (
            user_directory
            / main_filename
        ).resolve()

        # 防止 main_filename 使用 ../ 越过任务目录
        main_path.relative_to(
            user_directory.resolve()
        )

        if not main_path.is_file():
            raise FileNotFoundError(
                f"未找到用户程序：{main_path}"
            )

        command = [
            "runuser",
            "--user",
            "suanqi-task",
            "--",
            "nice",
            "-n",
            "5",
            str(virtualenv_python),
            "-u",
            str(main_path),
        ]

        environment = os.environ.copy()

        cpu_count = os.cpu_count() or 1
        # cpu_count 代表服务器检测到的逻辑 CPU 核心数

        usable_cpu_count = max(
            1,
            cpu_count - 1,
        )
        # usable_cpu_count 代表计算库可使用的线程数
        # 留一个逻辑核心给系统和 worker

        for variable_name in (
            "OMP_NUM_THREADS",
            "MKL_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
        ):
            environment[variable_name] = str(
                usable_cpu_count
            )

        # =========================
        # 第三阶段：启动用户程序
        # =========================

        timed_out = False
        # timed_out 表示用户程序是否运行超时

        forced_kill = False
        # forced_kill 表示是否最终使用了 SIGKILL

        exit_code: int | None = None
        # exit_code 是用户程序最终退出码

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

                # 创建独立会话和进程组
                # 后续可以一次停止 main.py 和所有子进程
                start_new_session=True,

                env=environment,
            )

            program_started_at = utc_now()

            pid_path.write_text(
                str(process.pid),
                encoding="utf-8",
            )

            atomic_write_json(
                status_path,
                build_status(
                    config,
                    "RUNNING",
                    worker_started_at=(
                        worker_started_at
                    ),
                    preparation_started_at=(
                        preparation_started_at
                    ),
                    preparation_finished_at=(
                        preparation_finished_at
                    ),
                    started_at=(
                        program_started_at
                    ),
                    worker_pid=os.getpid(),
                    main_pid=process.pid,
                    max_use_seconds=(
                        max_use_seconds
                    ),
                    message="用户程序正在运行",
                ),
            )

            append_worker_log(
                worker_log_path,
                (
                    "用户程序已经启动，"
                    f"PID={process.pid}"
                ),
            )

            try:
                # 最大运行时间从 main.py 启动后开始计算
                exit_code = process.wait(
                    timeout=max_use_seconds
                )

            except subprocess.TimeoutExpired:
                timed_out = True

                timeout_at = utc_now()
                # timeout_at 代表达到最大运行时间的时刻

                append_worker_log(
                    worker_log_path,
                    (
                        "用户程序达到最大运行时间，"
                        "开始执行超时终止流程"
                    ),
                )

                atomic_write_json(
                    status_path,
                    build_status(
                        config,
                        "TIMEOUT",
                        worker_started_at=(
                            worker_started_at
                        ),
                        preparation_started_at=(
                            preparation_started_at
                        ),
                        preparation_finished_at=(
                            preparation_finished_at
                        ),
                        started_at=(
                            program_started_at
                        ),
                        timeout_at=timeout_at,
                        worker_pid=os.getpid(),
                        main_pid=process.pid,
                        max_use_seconds=(
                            max_use_seconds
                        ),
                        message=(
                            "任务达到最大运行时间，"
                            "正在停止用户程序"
                        ),
                    ),
                )

                exit_code, forced_kill = (
                    stop_process_group(
                        process=process,
                        grace_seconds=(
                            terminate_grace_seconds
                        ),
                        worker_log_path=(
                            worker_log_path
                        ),
                    )
                )

        # =========================
        # 第四阶段：整理最终状态
        # =========================

        finished_at = utc_now()

        if timed_out:
            final_status = "TIMEOUT"

        elif exit_code == 0:
            final_status = "SUCCESS"

        else:
            final_status = "FAILED"

        files, missing_files = (
            collect_return_files(
                config=config,
                user_directory=user_directory,
            )
        )

        if final_status == "SUCCESS":
            final_message = (
                "用户程序运行成功"
            )

        elif final_status == "TIMEOUT":
            if forced_kill:
                final_message = (
                    "用户程序运行超时，"
                    "SIGTERM 后未退出，"
                    "已使用 SIGKILL 强制结束"
                )
            else:
                final_message = (
                    "用户程序运行超时，"
                    "已使用 SIGTERM 停止"
                )

        else:
            final_message = (
                "用户程序运行失败，"
                f"退出码={exit_code}"
            )

        atomic_write_json(
            status_path,
            build_status(
                config,
                final_status,
                worker_started_at=(
                    worker_started_at
                ),
                preparation_started_at=(
                    preparation_started_at
                ),
                preparation_finished_at=(
                    preparation_finished_at
                ),
                started_at=(
                    program_started_at
                ),
                finished_at=finished_at,
                worker_pid=os.getpid(),
                main_pid=(
                    process.pid
                    if process is not None
                    else None
                ),
                exit_code=exit_code,
                max_use_seconds=(
                    max_use_seconds
                ),
                preparation_timeout_seconds=(
                    preparation_timeout_seconds
                ),
                terminate_grace_seconds=(
                    terminate_grace_seconds
                ),
                timed_out=timed_out,
                forced_kill=forced_kill,
                message=final_message,
            ),
        )

        atomic_write_json(
            manifest_path,
            {
                "task_id": config["task_id"],
                "completed": True,
                "status": final_status,
                "program_exit_code": exit_code,

                "worker_started_at": (
                    worker_started_at
                ),

                "preparation_started_at": (
                    preparation_started_at
                ),

                "preparation_finished_at": (
                    preparation_finished_at
                ),

                "started_at": (
                    program_started_at
                ),

                "finished_at": (
                    finished_at
                ),

                "preparation_timeout_seconds": (
                    preparation_timeout_seconds
                ),

                "max_use_seconds": (
                    max_use_seconds
                ),

                "terminate_grace_seconds": (
                    terminate_grace_seconds
                ),

                "timed_out": timed_out,
                "forced_kill": forced_kill,

                "files": files,
                "missing_files": missing_files,
            },
        )

        append_worker_log(
            worker_log_path,
            final_message,
        )

        if final_status == "SUCCESS":
            return 0

        if final_status == "TIMEOUT":
            # 124 是 Linux 命令中常用的超时退出码
            return 124

        return 1

    except BaseException as error:
        # worker 出错时，尽量停止仍在运行的用户程序
        if process is not None:
            try:
                stop_process_group(
                    process=process,
                    grace_seconds=(
                        terminate_grace_seconds
                    ),
                    worker_log_path=(
                        worker_log_path
                        if worker_log_path is not None
                        else Path("/tmp/suanqi-worker.log")
                    ),
                )
            except Exception:
                pass

        failed_at = utc_now()

        # 只有路径已经成功解析后，才能写状态文件
        if (
            status_path is not None
            and config
        ):
            try:
                atomic_write_json(
                    status_path,
                    build_status(
                        config,
                        "WORKER_FAILED",
                        worker_started_at=(
                            worker_started_at
                        ),
                        preparation_started_at=(
                            preparation_started_at
                        ),
                        preparation_finished_at=(
                            preparation_finished_at
                        ),
                        started_at=(
                            program_started_at
                        ),
                        failed_at=failed_at,
                        worker_pid=os.getpid(),
                        main_pid=(
                            process.pid
                            if process is not None
                            else None
                        ),
                        error_type=(
                            error.__class__.__name__
                        ),
                        error_message=str(error),
                        message=(
                            "守护进程执行失败"
                        ),
                    ),
                )
            except Exception:
                pass

        if worker_log_path is not None:
            try:
                append_worker_log(
                    worker_log_path,
                    (
                        "守护进程异常："
                        f"{error.__class__.__name__}："
                        f"{error}"
                    ),
                )
            except Exception:
                pass

        return 1


if __name__ == "__main__":
    raise SystemExit(main())