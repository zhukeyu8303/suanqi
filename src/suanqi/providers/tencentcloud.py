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
) -> dict[str, Any]:
    """创建腾讯云实例并运行任务。"""

    gateway = TencentCloudGateway()
    server = None
    task = None
    exit_code = None
    downloaded_files = []
    missing_files = []

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
            packages=packages or [],
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
        )

        print(
            "\n守护进程已经启动，"
            "SSH 断开不会终止远程程序。\n"
        )

        final_status = follow_worker_task(
            server,
            worker_task,
        )

        exit_code = final_status.get(
            "exit_code",
            1,
        )

        if return_files:
            downloaded_files, missing_files = (
                download_return_files(
                    server,
                    task,
                    return_files,
                )
            )

        return {
            "success": exit_code == 0,
            "error_message": (
                None
                if exit_code == 0
                else f"远程程序退出码为 {exit_code}"
            ),
            "provider": "tencentcloud",
            "region": server.region,
            "instance_id": server.instance_id,
            "public_ip": server.public_ip,
            "cpu": server.cpu,
            "memory_gb": server.memory_gb,
            "task_id": task.task_id,
            "exit_code": exit_code,
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

            result = gateway.terminate_instances(
                server.region,
                [server.instance_id],
            )

            if result.success:
                print("实例释放请求已提交")
            else:
                print(
                    "实例释放失败："
                    f"{result.error_code}，"
                    f"{result.error_message}"
                )

    return {
        "success": False,
        "error_message": error_message,
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
        "exit_code": exit_code,
        "downloaded_files": downloaded_files,
        "missing_files": missing_files,
        "instance_kept": keep_instance,
    }
