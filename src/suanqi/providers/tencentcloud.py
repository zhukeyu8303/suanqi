# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from typing import Any

from suanqi.gateway.tencent_gateway import (
    TencentCloudGateway,
)
from suanqi.remote import (
    download_return_files,
    follow_worker_task,
    initialize_server,
    parse_server_info,
    prepare_python_task,
    start_worker_task,
    wait_for_ssh,
)

from .server_initializer import (
    tencentcloud_creat,
)


def tencentcloud_run(
    python_file: str | Path,
    requirements_file: str | Path | None = None,
    packages: list[str] | None = None,
    return_files: list[str] | None = None,
    minimum_cpu: int = 16,
    minimum_memory_gb: int = 16,
    maximum_region_instances: int = 10,
    keep_instance: bool = False,
    max_use_seconds: int = 5 * 60 * 60,
) -> dict[str, Any]:
    """
    创建腾讯云实例并运行任务。

    参数：
        python_file：
            需要上传并运行的 Python 文件。

        requirements_file：
            可选的 requirements.txt 文件。

        packages：
            通过 -i 参数指定的额外 Python 包。

        return_files：
            任务结束后需要下载的文件。

        minimum_cpu：
            实例最低 CPU 核心数。

        minimum_memory_gb：
            实例最低内存，单位 GB。

        maximum_region_instances：
            每个地域最多保留的候选机型数量。

        keep_instance：
            任务结束后是否保留实例。

        max_use_seconds：
            用户程序最大允许运行时间，单位秒。
            默认值为 18000 秒，也就是 5 小时。
    """

    if max_use_seconds <= 0:
        raise ValueError(
            "max_use_seconds 必须大于 0"
        )

    gateway = TencentCloudGateway()

    server = None
    # server 保存已经创建的腾讯云服务器信息

    task = None
    # task 保存远程任务信息，例如 task_id 和任务目录

    exit_code = None
    # exit_code 保存用户程序退出码

    final_state = None
    # final_state 保存任务最终状态
    # 例如 SUCCESS、FAILED、TIMEOUT、WORKER_FAILED

    downloaded_files: list[dict[str, Any]] = []
    # downloaded_files 保存已经成功下载的返回文件

    missing_files: list[str] = []
    # missing_files 保存服务器上没有找到的返回文件

    error_message: str | None = None
    # error_message 保存任务运行过程中的错误信息

    try:
        create_result = tencentcloud_creat(
            minimum_cpu=minimum_cpu,
            minimum_memory_gb=minimum_memory_gb,
            maximum_region_instances=(
                maximum_region_instances
            ),
        )

        if not create_result:
            return {
                "success": False,
                "error_message": "未创建实例",
                "provider": "tencentcloud",
                "region": None,
                "instance_id": None,
                "public_ip": None,
                "cpu": None,
                "memory_gb": None,
                "task_id": None,
                "state": None,
                "exit_code": None,
                "max_use_seconds": max_use_seconds,
                "downloaded_files": [],
                "missing_files": [],
                "instance_kept": False,
            }

        server = parse_server_info(
            create_result
        )

        print("\n正在等待 SSH 服务启动……")
        wait_for_ssh(server)

        print("正在初始化服务器……")
        initialize_server(server)

        task = prepare_python_task(
            server=server,
            python_file=python_file,
            requirements_file=requirements_file,
        )

        worker_file = (
            Path(__file__).resolve().parents[1]
            / "worker.py"
        )

        worker_task = start_worker_task(
            server=server,
            task=task,
            return_files=return_files or [],
            local_worker_path=worker_file,

            # 将 -i 指定的依赖交给远程 worker
            packages=packages or [],

            # main.py 的最大运行时间
            max_use_seconds=max_use_seconds,

            # main.py 超时后的优雅退出等待时间
            terminate_grace_seconds=30,

            # 创建虚拟环境和安装依赖最多允许2小时
            preparation_timeout_seconds=2 * 60 * 60,
        )

        print(
            "\n守护进程已经启动，"
            "SSH 断开不会终止远程程序。"
        )

        print(
            "用户程序最大运行时间："
            f"{max_use_seconds} 秒\n"
        )

        final_status = follow_worker_task(
            server,
            worker_task,
        )

        final_state = final_status.get(
            "state"
        )
        # state 是 worker 写入的最终任务状态

        exit_code = final_status.get(
            "exit_code"
        )

        if exit_code is None:
            # worker 没有返回退出码时，使用 1 表示失败
            exit_code = 1

        if return_files:
            downloaded_files, missing_files = (
                download_return_files(
                    server,
                    task,
                    return_files,
                )
            )

        if final_state == "TIMEOUT":
            error_message = (
                "远程程序超过最大运行时间，"
                "已由守护进程停止"
            )

        elif final_state == "WORKER_FAILED":
            error_message = (
                final_status.get("message")
                or "远程守护进程运行失败"
            )

        elif exit_code != 0:
            error_message = (
                f"远程程序退出码为 {exit_code}"
            )

        task_success = (
            final_state == "SUCCESS"
            and exit_code == 0
        )
        # 只有状态是 SUCCESS 且退出码为 0，
        # 才认为整个远程任务执行成功

        return {
            "success": task_success,
            "error_message": (
                None
                if task_success
                else error_message
            ),
            "provider": "tencentcloud",
            "region": server.region,
            "instance_id": server.instance_id,
            "public_ip": server.public_ip,
            "cpu": server.cpu,
            "memory_gb": server.memory_gb,
            "task_id": task.task_id,
            "state": final_state,
            "exit_code": exit_code,
            "max_use_seconds": max_use_seconds,
            "downloaded_files": downloaded_files,
            "missing_files": missing_files,
            "instance_kept": keep_instance,
        }

    except KeyboardInterrupt:
        error_message = "用户中断了任务"

    except Exception as error:
        error_message = (
            f"{error.__class__.__name__}：{error}"
        )

    finally:
        if (
            server is not None
            and not keep_instance
        ):
            print("\n正在释放实例……")

            release_result = gateway.terminate_instances(
                server.region,
                [server.instance_id],
            )

            if release_result.success:
                print("实例释放请求已提交")
            else:
                print(
                    "实例释放失败："
                    f"{release_result.error_code}，"
                    f"{release_result.error_message}"
                )

        elif (
            server is not None
            and keep_instance
        ):
            print(
                "\n服务器已保留，"
                "按量计费仍可能继续。"
            )

    return {
        "success": False,
        "error_message": (
            error_message
            or "任务执行失败"
        ),
        "provider": "tencentcloud",
        "region": (
            server.region
            if server is not None
            else None
        ),
        "instance_id": (
            server.instance_id
            if server is not None
            else None
        ),
        "public_ip": (
            server.public_ip
            if server is not None
            else None
        ),
        "cpu": (
            server.cpu
            if server is not None
            else None
        ),
        "memory_gb": (
            server.memory_gb
            if server is not None
            else None
        ),
        "task_id": (
            task.task_id
            if task is not None
            else None
        ),
        "state": final_state,
        "exit_code": exit_code,
        "max_use_seconds": max_use_seconds,
        "downloaded_files": downloaded_files,
        "missing_files": missing_files,
        "instance_kept": (
            keep_instance
            and server is not None
        ),
    }