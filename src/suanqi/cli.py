# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
from pathlib import Path

from .gateway.tencent_gateway import TencentCloudGateway
from .instance_manager import (
    ManagedInstance,
    format_instance_table,
    list_suanqi_instances,
    release_suanqi_instance,
)
from .providers import tencentcloud_run


def _parse_return_files(value: str) -> list[str]:
    """解析 --return 参数。"""

    files = [
        item.strip()
        for item in value.split(",")
        if item.strip()
    ]

    if not files:
        raise argparse.ArgumentTypeError(
            "--return 至少需要填写一个文件"
        )

    return files


def build_parser() -> argparse.ArgumentParser:
    """创建命令行解析器。"""

    parser = argparse.ArgumentParser(
        prog="suanqi",
        description="创建云服务器并运行 Python 计算任务。",
    )

    parser.add_argument(
        "-l",
        "--list",
        action="store_true",
        dest="list_instances",
        help="列出所有由 SuanQi 管理的实例。",
    )

    subparsers = parser.add_subparsers(
        dest="command",
    )

    run_parser = subparsers.add_parser(
        "run",
        help="在云服务器上运行单个 Python 文件。",
    )

    run_parser.add_argument(
        "python_file",
        type=Path,
        help="需要上传并执行的 Python 文件。",
    )

    run_parser.add_argument(
        "--provider",
        default="tencentcloud",
        choices=["tencentcloud"],
        help="云服务提供商。",
    )

    run_parser.add_argument(
        "-r",
        "--requirements",
        type=Path,
        default=None,
        help="可选的 requirements.txt 文件。",
    )

    run_parser.add_argument(
        "-i",
        "--install",
        action="append",
        default=[],
        metavar="PACKAGE",
        help="额外安装一个 Python 包，可重复使用。",
    )

    run_parser.add_argument(
        "--return",
        dest="return_files",
        type=_parse_return_files,
        default=[],
        metavar="FILES",
        help="任务结束后下载的文件，多个文件用逗号分隔。",
    )

    run_parser.add_argument(
        "--cpu",
        type=int,
        default=16,
        help="实例最低 CPU 核心数，默认 16。",
    )

    run_parser.add_argument(
        "--memory",
        type=int,
        default=16,
        help="实例最低内存，单位 GB，默认 16。",
    )

    run_parser.add_argument(
        "--maximum-region-instances",
        type=int,
        default=10,
        help="每个地域最多保留的候选机型数量。",
    )

    run_parser.add_argument(
        "--keep",
        action="store_true",
        help="任务结束后保留实例。",
    )

    release_parser = subparsers.add_parser(
        "release",
        help="强制释放指定的 SuanQi 实例。",
    )

    release_parser.add_argument(
        "instance_id",
        help="需要释放的腾讯云实例 ID。",
    )

    release_parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过 RELEASE 二次确认。",
    )

    return parser


def _validate_run_arguments(
    parser: argparse.ArgumentParser,
    arguments: argparse.Namespace,
) -> None:
    """检查 run 命令参数。"""

    python_file = (
        arguments.python_file
        .expanduser()
        .resolve()
    )

    if not python_file.is_file():
        parser.error(
            f"Python 文件不存在：{python_file}"
        )

    if python_file.suffix.lower() != ".py":
        parser.error(
            f"当前只支持单个 .py 文件：{python_file}"
        )

    arguments.python_file = python_file

    if arguments.requirements is not None:
        requirements_file = (
            arguments.requirements
            .expanduser()
            .resolve()
        )

        if not requirements_file.is_file():
            parser.error(
                f"requirements 文件不存在：{requirements_file}"
            )

        arguments.requirements = requirements_file

    if arguments.cpu <= 0:
        parser.error("--cpu 必须大于 0")

    if arguments.memory <= 0:
        parser.error("--memory 必须大于 0")

    if arguments.maximum_region_instances <= 0:
        parser.error(
            "--maximum-region-instances 必须大于 0"
        )


def list_instances_command() -> int:
    """执行实例列表命令。"""

    gateway = TencentCloudGateway()

    print("正在查询 SuanQi 管理的腾讯云实例……")

    result = list_suanqi_instances(
        gateway=gateway,
    )

    if not result.success:
        print(
            "查询实例失败："
            f"{result.error_code}，"
            f"{result.error_message}"
        )
        return 1

    instances: list[ManagedInstance] = (
        result.data["instances"]
    )

    print()
    print(format_instance_table(instances))
    print(
        f"\n共找到 {len(instances)} 台 "
        "SuanQi 管理的实例。"
    )

    failed_regions = (
        result.data.get("failed_regions")
        or []
    )

    if failed_regions:
        print("\n以下地域查询失败：")

        for item in failed_regions:
            print(
                f"- {item['region']}："
                f"{item['error_code']}，"
                f"{item['error_message']}"
            )

    return 0


def release_instance_command(
    instance_id: str,
    skip_confirmation: bool,
) -> int:
    """执行实例释放命令。"""

    gateway = TencentCloudGateway()

    list_result = list_suanqi_instances(
        gateway=gateway,
    )

    if not list_result.success:
        print(
            "查询实例失败："
            f"{list_result.error_code}，"
            f"{list_result.error_message}"
        )
        return 1

    instances: list[ManagedInstance] = (
        list_result.data["instances"]
    )

    selected = next(
        (
            instance
            for instance in instances
            if instance.instance_id == instance_id
        ),
        None,
    )

    if selected is None:
        print(
            "没有找到该 SuanQi 实例。"
            "\n请先执行 suanqi --list。"
        )
        return 1

    print("\n即将强制释放实例：")
    print(f"实例 ID：{selected.instance_id}")
    print(f"实例名称：{selected.instance_name}")
    print(f"状态：{selected.state}")
    print(f"地域：{selected.region}")
    print(f"可用区：{selected.zone or '-'}")
    print(f"公网 IP：{selected.public_ip or '-'}")
    print(f"创建时间：{selected.created_time or '-'}")
    print(
        "\n警告：实例上的未上传日志和结果"
        "可能永久丢失。"
    )

    if not skip_confirmation:
        confirmation = input(
            "请输入 RELEASE 确认强制释放："
        ).strip()

        if confirmation != "RELEASE":
            print("已取消释放实例。")
            return 1

    result = release_suanqi_instance(
        gateway=gateway,
        instance_id=instance_id,
        require_suanqi_managed=True,
    )

    if not result.success:
        print(
            "释放实例失败："
            f"{result.error_code}，"
            f"{result.error_message}"
        )
        return 1

    print(
        f"实例释放请求已提交：{instance_id}"
    )
    return 0


def run_command(
    parser: argparse.ArgumentParser,
    arguments: argparse.Namespace,
) -> int:
    """执行远程计算任务。"""

    _validate_run_arguments(
        parser,
        arguments,
    )

    result = tencentcloud_run(
        python_file=arguments.python_file,
        requirements_file=arguments.requirements,
        packages=arguments.install,
        return_files=arguments.return_files,
        minimum_cpu=arguments.cpu,
        minimum_memory_gb=arguments.memory,
        maximum_region_instances=(
            arguments.maximum_region_instances
        ),
        keep_instance=arguments.keep,
    )

    if not result:
        return 1

    if not result.get("success"):
        print(
            "\n任务执行失败："
            f"{result.get('error_message') or '未知错误'}"
        )
        return 1

    print("\n任务执行完成：")
    print(f"任务 ID：{result.get('task_id')}")
    print(f"实例 ID：{result.get('instance_id')}")
    print(
        "配置："
        f"{result.get('cpu')} 核 / "
        f"{result.get('memory_gb')} GB"
    )
    print(
        f"程序退出码：{result.get('exit_code')}"
    )

    downloaded_files = (
        result.get("downloaded_files")
        or []
    )
    missing_files = (
        result.get("missing_files")
        or []
    )

    if downloaded_files:
        print("\n已下载文件：")

        for item in downloaded_files:
            print(
                f"- {item['remote_path']} -> "
                f"{item['local_path']}"
            )

    if missing_files:
        print("\n未找到的返回文件：")

        for item in missing_files:
            print(f"- {item}")

    if result.get("instance_kept"):
        print(
            "\n服务器已保留，按量计费仍可能继续。"
        )

    return (
        0
        if result.get("exit_code") == 0
        else 1
    )


def main() -> int:
    parser = build_parser()
    arguments = parser.parse_args()

    if arguments.list_instances:
        return list_instances_command()

    if arguments.command == "run":
        return run_command(
            parser,
            arguments,
        )

    if arguments.command == "release":
        return release_instance_command(
            instance_id=arguments.instance_id,
            skip_confirmation=arguments.yes,
        )

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
