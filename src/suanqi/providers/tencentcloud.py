# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone
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
    TaskCosTarget,
    start_worker_task,
    wait_for_ssh,
)
from suanqi.task_store import (
    save_task_record,
    update_task_record,
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
    cos_target: TaskCosTarget | None = None,
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
    """

    if max_use_seconds <= 0:
        raise ValueError(
            "max_use_seconds 必须大于 0"
        )

    gateway = TencentCloudGateway()

    server = None
    # server 保存腾讯云服务器信息

    task = None
    # task 保存远程任务目录与内部任务 ID

    worker_task = None
    # worker_task 保存远程守护进程路径

    worker_started = False
    # worker_started 表示 systemd 守护进程是否已经启动

    detached = False
    # detached 表示用户是否仅断开客户端日志跟踪

    exit_code: int | None = None
    # exit_code 是用户程序退出码

    final_state: str | None = None
    # final_state 是最终状态
    # SUCCESS、FAILED、TIMEOUT 或 WORKER_FAILED

    downloaded_files: list[dict[str, Any]] = []
    # downloaded_files 保存成功下载的文件

    missing_files: list[str] = []
    # missing_files 保存未找到的返回文件

    error_message: str | None = None
    # error_message 保存失败原因

    try:
        create_result = tencentcloud_creat(
            minimum_cpu=minimum_cpu,
            minimum_memory_gb=minimum_memory_gb,
            maximum_region_instances=(
                maximum_region_instances
            ),
            cam_role_name=(
                "SuanQiWorkerRole"
                if cos_target is not None and cos_target.enabled
                else None
            ),
        )

        if not create_result:
            return {
                "success": False,
                "detached": False,
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

        print(
            "\n正在等待 SSH 服务启动……"
        )

        wait_for_ssh(
            server
        )

        print(
            "正在初始化服务器……"
        )

        initialize_server(
            server
        )

        task = prepare_python_task(
            server=server,
            python_file=python_file,
            requirements_file=(
                requirements_file
            ),
        )

        worker_file = (
            Path(__file__).resolve().parents[1]
            / "worker.py"
        )

        worker_task = start_worker_task(
            server=server,
            task=task,
            return_files=(
                return_files or []
            ),
            local_worker_path=(
                worker_file
            ),
            cos_target=cos_target,
            packages=(
                packages or []
            ),
            max_use_seconds=(
                max_use_seconds
            ),
            terminate_grace_seconds=30,
            preparation_timeout_seconds=(
                2 * 60 * 60
            ),
        )

        worker_started = True

        task_record_path = save_task_record(
            server.instance_id,
            {
                "instance_id": (
                    server.instance_id
                ),

                "task_id": (
                    task.task_id
                ),

                "provider": (
                    "tencentcloud"
                ),

                "region": (
                    server.region
                ),

                "public_ip": (
                    server.public_ip
                ),

                "ssh_username": (
                    server.ssh_username
                ),

                "ssh_port": (
                    server.ssh_port
                ),

                "instance_password": (
                    server.instance_password
                ),

                "service_name": (
                    worker_task.service_name
                ),

                "task_root": (
                    worker_task.task_root
                ),

                "user_directory": (
                    worker_task.user_directory
                ),

                "control_directory": (
                    worker_task.control_directory
                ),

                "status_path": (
                    worker_task.status_path
                ),

                "task_log_path": (
                    worker_task.task_log_path
                ),

                "worker_log_path": (
                    worker_task.worker_log_path
                ),

                "manifest_path": (
                    worker_task.manifest_path
                ),

                "return_files": (
                    return_files or []
                ),

                "max_use_seconds": (
                    max_use_seconds
                ),

                "status": (
                    "STARTING"
                ),

                "created_at": (
                    datetime.now(
                        timezone.utc
                    ).isoformat()
                ),
            },
        )

        print(
            "\n本地实例记录已保存："
            f"{task_record_path}"
        )

        print(
            "重新连接命令："
            f"suanqi attach "
            f"{server.instance_id}"
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

        if not isinstance(
            final_status,
            dict,
        ):
            raise RuntimeError(
                "远程守护进程返回了无效状态数据"
            )

        final_state = (
            final_status.get("status")
            or final_status.get("state")
        )
        # worker.py 当前使用 status 字段
        # 同时兼容旧版 state 字段

        if final_state is not None:
            final_state = str(
                final_state
            ).strip().upper()

        exit_code_value = final_status.get(
            "exit_code"
        )

        if exit_code_value is not None:
            try:
                exit_code = int(
                    exit_code_value
                )
            except (
                TypeError,
                ValueError,
            ) as error:
                raise RuntimeError(
                    "远程任务返回了无效退出码："
                    f"{exit_code_value!r}"
                ) from error

        elif final_state == "SUCCESS":
            # 成功状态没有退出码时，兼容性处理为0
            exit_code = 0

        else:
            # 其他状态没有退出码时，统一视为失败
            exit_code = 1

        if return_files:
            (
                downloaded_files,
                missing_files,
            ) = download_return_files(
                server,
                task,
                return_files,
            )

        if final_state == "SUCCESS":
            if exit_code != 0:
                error_message = (
                    "远程状态为 SUCCESS，"
                    f"但程序退出码为 {exit_code}"
                )

        elif final_state == "TIMEOUT":
            error_message = (
                final_status.get(
                    "message"
                )
                or (
                    "远程程序超过最大运行时间，"
                    "已由守护进程停止"
                )
            )

        elif final_state == "WORKER_FAILED":
            error_message = (
                final_status.get(
                    "error_message"
                )
                or final_status.get(
                    "message"
                )
                or "远程守护进程运行失败"
            )

        elif final_state == "UPLOAD_FAILED":
            error_message = (
                final_status.get(
                    "message"
                )
                or "远程任务上传结果失败，实例已保留"
            )

        elif final_state == "FAILED":
            error_message = (
                final_status.get(
                    "message"
                )
                or (
                    "远程程序运行失败，"
                    f"退出码为 {exit_code}"
                )
            )

        elif final_state is None:
            error_message = (
                "远程状态文件中缺少 "
                "status 或 state 字段"
            )

        else:
            error_message = (
                "无法识别远程任务状态："
                f"{final_state}"
            )

        task_success = (
            final_state == "SUCCESS"
            and exit_code == 0
        )
        instance_kept = (
            keep_instance
            or final_state == "UPLOAD_FAILED"
        )

        try:
            update_task_record(
                server.instance_id,
                status=(
                    final_state
                    or "UNKNOWN"
                ),
                exit_code=(
                    exit_code
                ),
                error_message=(
                    None
                    if task_success
                    else error_message
                ),
                updated_at=(
                    final_status.get(
                        "updated_at"
                    )
                    or datetime.now(
                        timezone.utc
                    ).isoformat()
                ),
            )

        except Exception as record_error:
            print(
                "\n警告：更新本地实例记录失败："
                f"{record_error}"
            )

        return {
            "success": (
                task_success
            ),

            "detached": False,

            "error_message": (
                None
                if task_success
                else (
                    error_message
                    or "远程任务执行失败"
                )
            ),

            "provider": (
                "tencentcloud"
            ),

            "region": (
                server.region
            ),

            "instance_id": (
                server.instance_id
            ),

            "public_ip": (
                server.public_ip
            ),

            "cpu": (
                server.cpu
            ),

            "memory_gb": (
                server.memory_gb
            ),

            "task_id": (
                task.task_id
            ),

            "state": (
                final_state
            ),

            "exit_code": (
                exit_code
            ),

            "max_use_seconds": (
                max_use_seconds
            ),

            "downloaded_files": (
                downloaded_files
            ),

            "missing_files": (
                missing_files
            ),

            "instance_kept": (
                instance_kept
            ),
        }

    except KeyboardInterrupt:
        if (
            worker_started
            and server is not None
            and task is not None
        ):
            detached = True

            try:
                update_task_record(
                    server.instance_id,
                    status="DETACHED",
                    detached_at=(
                        datetime.now(
                            timezone.utc
                        ).isoformat()
                    ),
                )

            except Exception as record_error:
                print(
                    "\n警告：更新本地实例记录失败："
                    f"{record_error}"
                )

            print(
                "\n已停止本地日志跟踪。"
            )

            print(
                "远程任务仍在运行。"
            )

            print(
                "重新连接命令："
                f"suanqi attach "
                f"{server.instance_id}"
            )

            return {
                "success": True,
                "detached": True,
                "error_message": None,
                "provider": "tencentcloud",

                "region": (
                    server.region
                ),

                "instance_id": (
                    server.instance_id
                ),

                "public_ip": (
                    server.public_ip
                ),

                "cpu": (
                    server.cpu
                ),

                "memory_gb": (
                    server.memory_gb
                ),

                "task_id": (
                    task.task_id
                ),

                "state": (
                    "DETACHED"
                ),

                "exit_code": None,

                "max_use_seconds": (
                    max_use_seconds
                ),

                "downloaded_files": [],

                "missing_files": [],

                "instance_kept": True,
            }

        error_message = (
            "用户在远程守护进程启动前中断了操作"
        )

        print(
            f"\n{error_message}"
        )

    except Exception as error:
        error_message = (
            f"{error.__class__.__name__}："
            f"{error}"
        )

        print(
            "\n任务运行过程发生异常："
            f"{error_message}"
        )

    finally:
        should_release_instance = (
            server is not None
            and not keep_instance
            and not detached
            and final_state != "UPLOAD_FAILED"
        )

        if should_release_instance:
            print(
                "\n正在释放实例……"
            )

            release_result = (
                gateway.terminate_instances(
                    server.region,
                    [server.instance_id],
                )
            )

            if release_result.success:
                print(
                    "实例释放请求已提交"
                )

            else:
                print(
                    "实例释放失败："
                    f"{release_result.error_code}，"
                    f"{release_result.error_message}"
                )

        elif (
            server is not None
            and instance_kept
        ):
            print(
                "\n服务器已保留，"
                "按量计费仍可能继续。"
            )

    return {
        "success": False,

        "detached": False,

        "error_message": (
            error_message
            or "任务执行失败，但没有获得具体错误信息"
        ),

        "provider": (
            "tencentcloud"
        ),

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

        "state": (
            final_state
        ),

        "exit_code": (
            exit_code
        ),

        "max_use_seconds": (
            max_use_seconds
        ),

        "downloaded_files": (
            downloaded_files
        ),

        "missing_files": (
            missing_files
        ),

        "instance_kept": (
            keep_instance
            and server is not None
        ),
    }
