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
from .remote import (
    ServerInfo,
    TaskCosTarget,
    WorkerTask,
    attach_worker_task,
    download_cos_task_results,
    wait_for_ssh,
)
from .task_store import (
    load_config,
    list_task_records,
    load_task_record,
    save_config,
    update_task_record,
)
from .utils.resource_utils import resolve_resource_requirements
from .utils.time_utils import parse_duration


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
        description=(
            "SuanQi：自动创建云服务器，"
            "运行 Python 计算任务并下载结果。"
        ),
        epilog=(
            "常用示例：\n"
            "  suanqi run main.py\n"
            "  suanqi run main.py --cpu 32\n"
            "  suanqi run main.py --memory 64\n"
            "  suanqi run main.py --cpu 32 --memory 64\n"
            "  suanqi run main.py --maxusetime 1h30m\n"
            "  suanqi run main.py -r requirements.txt\n"
            "  suanqi run main.py -i numpy -i pandas\n"
            "  suanqi run main.py --return result.xlsx,output.txt\n"
            "  suanqi run main.py --keep\n"
            "  suanqi --list\n"
            "  suanqi attach ins-xxxxxxxx\n"
            "  suanqi release ins-xxxxxxxx\n"
            "\n"
            "资源联动规则：\n"
            "  不指定 CPU 和内存：默认至少 16 核、16GB\n"
            "  只指定 --cpu 32：至少 32 核、32GB\n"
            "  只指定 --memory 64：至少 64 核、64GB\n"
            "  同时指定：CPU 和内存分别使用指定值\n"
            "\n"
            "时间格式：\n"
            "  30s      30 秒\n"
            "  20m      20 分钟\n"
            "  5h       5 小时\n"
            "  1h30m    1 小时 30 分钟\n"
            "  1d2h     1 天 2 小时\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    general_group = parser.add_argument_group(
        "全局选项"
    )

    general_group.add_argument(
        "-l",
        "--list",
        action="store_true",
        dest="list_instances",
        help="列出所有由 SuanQi 创建和管理的实例。",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        title="可用命令",
        metavar="COMMAND",
        description=(
            "使用 suanqi COMMAND -h "
            "查看某个命令的详细帮助。"
        ),
    )

    run_parser = subparsers.add_parser(
        "run",
        help="创建云服务器并运行 Python 文件。",
        description=(
            "自动筛选并创建云服务器，上传程序，"
            "由远程守护进程准备环境、安装依赖并运行任务。"
        ),
        epilog=(
            "示例：\n"
            "  suanqi run main.py\n"
            "  suanqi run main.py --cpu 32 --maxusetime 2h\n"
            "  suanqi run main.py -r requirements.txt\n"
            "  suanqi run main.py -i numpy -i pandas\n"
            "  suanqi run main.py --return result.xlsx,task.log\n"
            "\n"
            "注意：\n"
            "  --maxusetime 只计算用户程序真正运行的时间，\n"
            "  不包含创建服务器、等待 SSH、创建虚拟环境\n"
            "  和安装依赖的时间。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    run_parser.add_argument(
        "python_file",
        type=Path,
        metavar="PYTHON_FILE",
        help=(
            "需要上传并执行的 Python 文件，"
            "目前仅支持单个 .py 文件。"
        ),
    )

    environment_group = (
        run_parser.add_argument_group(
            "运行环境"
        )
    )

    environment_group.add_argument(
        "--provider",
        default="tencentcloud",
        choices=["tencentcloud"],
        help=(
            "云服务提供商，当前仅支持腾讯云"
            "（默认：tencentcloud）。"
        ),
    )

    environment_group.add_argument(
        "-r",
        "--requirements",
        type=Path,
        default=None,
        metavar="FILE",
        help="需要上传并安装的 requirements.txt 文件。",
    )

    environment_group.add_argument(
        "-i",
        "--install",
        action="append",
        default=[],
        metavar="PACKAGE",
        help=(
            "额外安装一个 Python 包，可重复使用，"
            "例如 -i numpy -i pandas。"
        ),
    )

    resource_group = run_parser.add_argument_group(
        "实例资源"
    )

    resource_group.add_argument(
        "--cpu",
        type=int,
        default=None,
        metavar="CORES",
        help=(
            "最低 CPU 核心数。只指定 CPU 时，"
            "最低内存 GB 数自动设为相同数值。"
        ),
    )

    resource_group.add_argument(
        "--memory",
        type=int,
        default=None,
        metavar="GB",
        help=(
            "最低内存大小，单位 GB。只指定内存时，"
            "最低 CPU 核心数自动设为相同数值。"
        ),
    )

    resource_group.add_argument(
        "--maximum-region-instances",
        type=int,
        default=10,
        metavar="COUNT",
        help=(
            "每个地域最多保留的候选机型数量"
            "（默认：10）。"
        ),
    )

    task_group = run_parser.add_argument_group(
        "任务控制"
    )

    task_group.add_argument(
        "--maxusetime",
        type=str,
        default="5h",
        metavar="DURATION",
        help=(
            "用户程序最大运行时间（默认：5h）。"
            "支持 30s、20m、5h、1h30m、1d2h。"
        ),
    )

    task_group.add_argument(
        "--return",
        dest="return_files",
        type=_parse_return_files,
        default=[],
        metavar="FILES",
        help=(
            "任务结束后下载的文件，"
            "多个文件使用逗号分隔。"
            "例如 --return result.xlsx,output.txt。"
        ),
    )

    task_group.add_argument(
        "--keep",
        action="store_true",
        help=(
            "任务结束后保留云服务器。"
            "启用后服务器可能继续产生费用。"
        ),
    )

    task_group.add_argument("--cos-bucket", default=None)
    task_group.add_argument("--cos-region", default=None)
    task_group.add_argument("--cos-prefix", default="tasks")
    task_group.add_argument("--cos-resource-owner", default=None)

    attach_parser = subparsers.add_parser(
        "attach",
        help="重新连接已经启动的远程任务。",
        description=(
            "根据实例 ID 读取本地任务记录，"
            "重新连接云服务器并继续显示远程任务日志。"
        ),
        epilog=(
            "示例：\n"
            "  suanqi attach ins-xxxxxxxx"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    attach_parser.add_argument(
        "instance_id",
        metavar="INSTANCE_ID",
        help=(
            "需要重新连接的实例 ID，"
            "例如 ins-ayx5jszt。"
        ),
    )

    release_parser = subparsers.add_parser(
        "release",
        help="强制释放指定的 SuanQi 实例。",
        description=(
            "强制释放由 SuanQi 创建和管理的腾讯云实例。"
            "实例中的未下载文件可能永久丢失。"
        ),
        epilog=(
            "示例：\n"
            "  suanqi release ins-xxxxxxxx\n"
            "  suanqi release ins-xxxxxxxx --yes"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    release_parser.add_argument(
        "instance_id",
        metavar="INSTANCE_ID",
        help=(
            "需要释放的腾讯云实例 ID，"
            "例如 ins-xxxxxxxx。"
        ),
    )

    release_parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过输入 RELEASE 的二次确认。",
    )

    history_parser = subparsers.add_parser(
        "history",
        help="显示本地历史任务。",
        description="列出保存在 ~/.suanqi/tasks 下的历史任务记录。",
    )

    fetch_parser = subparsers.add_parser(
        "fetch",
        help="从 COS 拉回任务结果。",
        description="根据任务 ID 和 COS 参数从对象存储下载任务结果。",
    )
    fetch_parser.add_argument("task_id", metavar="TASK_ID")
    fetch_parser.add_argument("--output", default=None)

    useos_parser = subparsers.add_parser(
        "useos",
        help="启用并持久化 COS 存储桶。",
        description="创建或绑定一个 SuanQi 专用 COS 存储桶配置。",
    )
    useos_parser.add_argument("--region", default="ap-nanjing")
    useos_parser.add_argument("--bucket", default=None)
    useos_parser.add_argument("--prefix", default="tasks")
    useos_parser.add_argument("--resource-owner", default=None)

    return parser


def _validate_run_arguments(
    parser: argparse.ArgumentParser,
    arguments: argparse.Namespace,
) -> None:
    """
    检查 run 命令参数，并计算最终资源要求。

    检查完成后会新增或更新：

    arguments.cpu：
        最终最低 CPU 核心数。

    arguments.memory：
        最终最低内存大小，单位 GB。

    arguments.max_use_seconds：
        最大运行时间，单位秒。
    """

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
                "requirements 文件不存在："
                f"{requirements_file}"
            )

        arguments.requirements = (
            requirements_file
        )

    try:
        minimum_cpu, minimum_memory_gb = (
            resolve_resource_requirements(
                cpu=arguments.cpu,
                memory_gb=arguments.memory,
            )
        )
    except ValueError as error:
        parser.error(
            str(error)
        )

    try:
        max_use_seconds = parse_duration(
            arguments.maxusetime
        )
    except ValueError as error:
        parser.error(
            str(error)
        )

    if arguments.maximum_region_instances <= 0:
        parser.error(
            "--maximum-region-instances 必须大于 0"
        )

    arguments.cpu = minimum_cpu
    # arguments.cpu 是最终最低 CPU 核心数

    arguments.memory = minimum_memory_gb
    # arguments.memory 是最终最低内存，单位 GB

    arguments.max_use_seconds = max_use_seconds
    # arguments.max_use_seconds 是最大运行秒数


def list_instances_command() -> int:
    """执行实例列表命令。"""

    gateway = TencentCloudGateway()

    print(
        "正在查询 SuanQi 管理的腾讯云实例……"
    )

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
    print(
        format_instance_table(instances)
    )
    print(
        f"\n共找到 {len(instances)} 台 "
        "SuanQi 管理的实例。"
    )

    failed_regions = (
        result.data.get("failed_regions")
        or []
    )

    if failed_regions:
        print(
            "\n以下地域查询失败："
        )

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

    print(
        "\n即将强制释放实例："
    )
    print(
        f"实例 ID：{selected.instance_id}"
    )
    print(
        f"实例名称：{selected.instance_name}"
    )
    print(
        f"状态：{selected.state}"
    )
    print(
        f"地域：{selected.region}"
    )
    print(
        f"可用区：{selected.zone or '-'}"
    )
    print(
        f"公网 IP：{selected.public_ip or '-'}"
    )
    print(
        f"创建时间：{selected.created_time or '-'}"
    )
    print(
        "\n警告：实例上的未上传日志和结果"
        "可能永久丢失。"
    )

    if not skip_confirmation:
        confirmation = input(
            "请输入 RELEASE 确认强制释放："
        ).strip()

        if confirmation != "RELEASE":
            print(
                "已取消释放实例。"
            )
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


def history_command() -> int:
    records = list_task_records()
    if not records:
        print("没有找到历史任务。")
        return 0

    for record in records:
        print(
            f"{record.get('task_id')} | "
            f"{record.get('instance_id')} | "
            f"{record.get('status')} | "
            f"{record.get('created_at') or record.get('updated_at')}"
        )
    return 0


def useos_command(region: str, bucket: str | None, prefix: str, resource_owner: str | None) -> int:
    gateway = TencentCloudGateway()
    app_id_result = gateway.get_user_app_id()
    if not app_id_result.success:
        print(f"获取 AppID 失败：{app_id_result.error_code}，{app_id_result.error_message}")
        return 1

    app_id = str(app_id_result.data.get("app_id") or "").strip()
    if not app_id:
        print("没有拿到 AppID。")
        return 1

    resolved_bucket = bucket or f"suanqi-{app_id}"
    resolved_owner = resource_owner or app_id

    bucket_result = gateway.ensure_cos_bucket(region, resolved_bucket)
    if not bucket_result.success:
        print(
            "创建或检查 COS Bucket 失败："
            f"{bucket_result.error_code}，{bucket_result.error_message}"
        )
        print("请确认 Bucket 名称必须是完整名称，例如 suanqi-1250000000，并且地域填写正确。")
        return 1

    role_result = gateway.ensure_suanqi_worker_role(
        cos_bucket=resolved_bucket,
        cos_region=region,
        cos_resource_owner=resolved_owner,
        cos_prefix=f"{prefix.strip('/') or 'tasks'}/*",
    )
    if not role_result.success:
        print(f"配置实例角色失败：{role_result.error_code}，{role_result.error_message}")
        return 1

    config = load_config()
    config["os"] = {
        "enabled": True,
        "region": region,
        "bucket": resolved_bucket,
        "prefix": prefix,
        "resource_owner": resolved_owner,
    }
    save_config(config)

    created_text = "已自动创建" if bucket_result.data.get("created") else "已存在"
    print(f"已启用专用 COS：{resolved_bucket} / {region} / {prefix}（Bucket {created_text}）")
    return 0


def fetch_command(task_id: str, output: str | None) -> int:
    record = None
    for item in list_task_records():
        if str(item.get("task_id") or "") == task_id:
            record = item
            break

    config = load_config().get("os") or {}
    cos_target = (record or {}).get("cos_target") or {}
    region = str(cos_target.get("region") or config.get("region") or "")
    bucket = str((record or {}).get("cos_bucket") or config.get("bucket") or "")
    prefix = str((record or {}).get("cos_prefix") or config.get("prefix") or "tasks")

    if not region or not bucket:
        print("缺少 COS 参数，且本地专用 COS 配置或任务绑定信息里也没有可用配置。")
        return 1

    gateway = TencentCloudGateway()
    local_dir = Path(output or (Path("suanqi-results") / task_id)).resolve()
    result = download_cos_task_results(
        gateway,
        region=region,
        bucket=bucket,
        task_id=task_id,
        local_directory=str(local_dir),
        root_prefix=prefix,
    )
    if not result.success:
        print(f"COS 下载失败：{result.error_code}，{result.error_message}")
        return 1
    print(f"已下载到：{local_dir}")
    return 0


def attach_command(
    instance_id: str,
) -> int:
    """根据实例 ID 重新连接远程任务。"""

    try:
        record = load_task_record(
            instance_id
        )

    except (
        FileNotFoundError,
        ValueError,
    ) as error:
        print(
            f"读取任务记录失败：{error}"
        )
        return 1

    provider = record.get(
        "provider"
    )

    if provider != "tencentcloud":
        print(
            "当前 attach 只支持腾讯云任务。"
        )
        return 1

    record_instance_id = str(
        record.get("instance_id") or ""
    )

    if record_instance_id != instance_id:
        print(
            "本地任务记录中的实例 ID 不一致："
            f"{record_instance_id}"
        )
        return 1

    required_fields = [
        "task_id",
        "region",
        "instance_id",
        "public_ip",
        "ssh_username",
        "ssh_port",
        "instance_password",
        "service_name",
        "task_root",
        "user_directory",
        "control_directory",
        "status_path",
        "task_log_path",
        "worker_log_path",
        "manifest_path",
    ]

    missing_fields = [
        field_name
        for field_name in required_fields
        if record.get(field_name) in (
            None,
            "",
        )
    ]

    if missing_fields:
        print(
            "任务记录缺少必要字段："
            + ", ".join(missing_fields)
        )
        return 1

    server = ServerInfo(
        provider="tencentcloud",
        region=str(
            record["region"]
        ),
        instance_id=str(
            record["instance_id"]
        ),
        public_ip=str(
            record["public_ip"]
        ),
        ssh_username=str(
            record["ssh_username"]
        ),
        ssh_port=int(
            record["ssh_port"]
        ),
        instance_password=str(
            record["instance_password"]
        ),
    )

    worker_task = WorkerTask(
        task_id=str(
            record["task_id"]
        ),
        service_name=str(
            record["service_name"]
        ),
        task_root=str(
            record["task_root"]
        ),
        user_directory=str(
            record["user_directory"]
        ),
        control_directory=str(
            record["control_directory"]
        ),
        status_path=str(
            record["status_path"]
        ),
        task_log_path=str(
            record["task_log_path"]
        ),
        worker_log_path=str(
            record["worker_log_path"]
        ),
        manifest_path=str(
            record["manifest_path"]
        ),
    )

    print(
        "正在连接服务器："
        f"{server.public_ip}"
    )

    try:
        wait_for_ssh(
            server
        )

        final_status = attach_worker_task(
            server=server,
            worker_task=worker_task,
        )

    except KeyboardInterrupt:
        print(
            "\n已停止本地日志跟踪。"
        )
        print(
            "远程任务不会因此停止。"
        )
        print(
            f"重新连接：suanqi attach {instance_id}"
        )
        return 0

    except Exception as error:
        print(
            "重新连接任务失败："
            f"{error.__class__.__name__}："
            f"{error}"
        )
        return 1

    status_name = (
        final_status.get("status")
        or final_status.get("state")
        or "UNKNOWN"
    )

    update_task_record(
        instance_id,
        status=status_name,
        exit_code=final_status.get(
            "exit_code"
        ),
        updated_at=final_status.get(
            "updated_at"
        ),
    )

    print(
        "\n任务已经结束："
        f"{status_name}"
    )

    exit_code = final_status.get(
        "exit_code"
    )

    return (
        0
        if (
            status_name == "SUCCESS"
            and exit_code == 0
        )
        else 1
    )


def run_command(
    parser: argparse.ArgumentParser,
    arguments: argparse.Namespace,
) -> int:
    """执行远程计算任务。"""

    _validate_run_arguments(
        parser,
        arguments,
    )

    print(
        "\n任务参数："
    )
    print(
        f"Python 文件：{arguments.python_file}"
    )
    print(
        f"最低 CPU：{arguments.cpu} 核"
    )
    print(
        f"最低内存：{arguments.memory} GB"
    )
    print(
        "最大运行时间："
        f"{arguments.maxusetime} "
        f"（{arguments.max_use_seconds} 秒）"
    )

    os_config = load_config().get("os") or {}
    cos_target = None
    if bool(os_config.get("enabled")):
        cos_target = TaskCosTarget(
            region=str(os_config.get("region") or ""),
            bucket=str(os_config.get("bucket") or ""),
            prefix=str(os_config.get("prefix") or "tasks"),
            resource_owner=str(os_config.get("resource_owner") or ""),
            enabled=True,
        )

    result = tencentcloud_run(
        python_file=arguments.python_file,
        requirements_file=(
            arguments.requirements
        ),
        packages=arguments.install,
        return_files=arguments.return_files,
        minimum_cpu=arguments.cpu,
        minimum_memory_gb=arguments.memory,
        maximum_region_instances=(
            arguments.maximum_region_instances
        ),
        keep_instance=arguments.keep,
        max_use_seconds=(
            arguments.max_use_seconds
        ),
        cos_target=cos_target,
    )

    if not result:
        return 1

    if result.get("detached"):
        print(
            "\n已断开本地日志跟踪。"
        )
        print(
            "远程任务仍在运行。"
        )
        print(
            "重新连接："
            f"suanqi attach "
            f"{result.get('instance_id')}"
        )
        return 0

    task_success = bool(
        result.get("success")
    )
    # task_success 表示远程任务是否成功完成

    if not task_success:
        print(
            "\n任务未成功完成："
            f"{result.get('error_message') or '未知错误'}"
        )

    print(
        "\n任务执行结束："
    )
    print(
        f"任务 ID：{result.get('task_id')}"
    )
    print(
        f"实例 ID：{result.get('instance_id')}"
    )
    print(
        "配置："
        f"{result.get('cpu')} 核 / "
        f"{result.get('memory_gb')} GB"
    )
    print(
        "任务状态："
        f"{result.get('state') or 'UNKNOWN'}"
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
        print(
            "\n已下载文件："
        )

        for item in downloaded_files:
            print(
                f"- {item['remote_path']} -> "
                f"{item['local_path']}"
            )

    if missing_files:
        print(
            "\n未找到的返回文件："
        )

        for item in missing_files:
            print(
                f"- {item}"
            )

    if result.get("instance_kept"):
        print(
            "\n服务器已保留，"
            "按量计费仍可能继续。"
        )

    return (
        0
        if task_success
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

    if arguments.command == "attach":
        return attach_command(
            instance_id=arguments.instance_id,
        )

    if arguments.command == "release":
        return release_instance_command(
            instance_id=arguments.instance_id,
            skip_confirmation=arguments.yes,
        )

    if arguments.command == "history":
        return history_command()

    if arguments.command == "useos":
        return useos_command(
            region=arguments.region,
            bucket=arguments.bucket,
            prefix=arguments.prefix,
            resource_owner=arguments.resource_owner,
        )

    if arguments.command == "fetch":
        return fetch_command(
            task_id=arguments.task_id,
            output=arguments.output,
        )

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
