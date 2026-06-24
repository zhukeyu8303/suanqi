# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import shlex
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import paramiko

from server_initializer import tencentcloud_creat
from suanqi.gateway.tencent_gateway import TencentCloudGateway


# 禁止Paramiko输出大量SSH底层日志
logging.getLogger("paramiko").setLevel(logging.CRITICAL)

# 腾讯云网关，用于释放实例等腾讯云API操作
gateway: TencentCloudGateway = TencentCloudGateway()


@dataclass(slots=True)
class ServerInfo:
    """云服务器连接信息。"""

    provider: str
    # provider表示云服务商，例如tencentcloud

    region: str
    # region表示服务器所在地域，例如ap-shanghai

    instance_id: str
    # instance_id表示腾讯云实例ID，例如ins-xxxxxxxx

    public_ip: str
    # public_ip表示服务器公网IP地址

    ssh_username: str
    # ssh_username表示SSH登录用户名，Ubuntu镜像通常为ubuntu

    ssh_port: int
    # ssh_port表示SSH端口，默认为22

    instance_password: str
    # instance_password表示服务器SSH登录密码


@dataclass(slots=True)
class RemoteCommandResult:
    """远程命令执行结果。"""

    command: str
    # command表示执行的远程命令

    exit_code: int
    # exit_code表示远程命令退出码，0通常表示成功

    stdout: str
    # stdout表示命令的标准输出内容

    stderr: str
    # stderr表示命令的错误输出内容


def parse_server_info(
    create_result: dict[str, Any],
) -> ServerInfo:
    """
    将服务器创建结果转换为统一连接信息。

    create_result：
        tencentcloud_creat函数返回的服务器创建结果。
    """

    if not create_result.get("success"):
        raise RuntimeError("服务器创建失败")

    public_ip = create_result.get("public_ip")

    if not public_ip:
        raise RuntimeError("服务器没有公网IP，无法建立SSH连接")

    instance_password = create_result.get(
        "instance_password"
    )

    if not instance_password:
        raise RuntimeError("服务器创建结果中没有SSH登录密码")

    return ServerInfo(
        provider=str(create_result["provider"]),
        region=str(create_result["region"]),
        instance_id=str(create_result["instance_id"]),
        public_ip=str(public_ip),
        ssh_username=str(
            create_result.get("ssh_username") or "ubuntu"
        ),
        ssh_port=int(
            create_result.get("ssh_port") or 22
        ),
        instance_password=str(instance_password),
    )


def create_ssh_client(
    server: ServerInfo,
) -> paramiko.SSHClient:
    """
    创建并连接SSH客户端。

    server：
        服务器连接信息。
    """

    ssh_client = paramiko.SSHClient()

    # 首次连接时自动接受服务器主机密钥
    # 当前适合临时云服务器，后续可以改成严格校验
    ssh_client.set_missing_host_key_policy(
        paramiko.AutoAddPolicy()
    )

    ssh_client.connect(
        hostname=server.public_ip,
        port=server.ssh_port,
        username=server.ssh_username,
        password=server.instance_password,
        timeout=15,
        auth_timeout=15,
        banner_timeout=15,
        allow_agent=False,
        look_for_keys=False,
    )

    return ssh_client


def wait_for_ssh(
    server: ServerInfo,
    timeout_seconds: int = 300,
    retry_interval_seconds: int = 5,
) -> None:
    """
    等待服务器SSH服务可以连接。

    timeout_seconds：
        最长等待时间，单位秒。

    retry_interval_seconds：
        两次连接尝试之间的间隔，单位秒。
    """

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds必须大于0")

    if retry_interval_seconds <= 0:
        raise ValueError(
            "retry_interval_seconds必须大于0"
        )

    start_time = time.monotonic()
    # start_time表示开始等待SSH的时间

    last_error: Exception | None = None
    # last_error记录最后一次SSH连接错误

    attempt_count = 0
    # attempt_count表示当前已经尝试连接的次数

    while (
        time.monotonic() - start_time
        < timeout_seconds
    ):
        attempt_count += 1

        try:
            ssh_client = create_ssh_client(server)
            ssh_client.close()
            return

        except (
            paramiko.SSHException,
            socket.timeout,
            ConnectionRefusedError,
            OSError,
        ) as error:
            last_error = error

            elapsed_seconds = int(
                time.monotonic() - start_time
            )
            # elapsed_seconds表示已经等待的秒数

            print(
                f"SSH暂未就绪，"
                f"第{attempt_count}次连接失败，"
                f"已等待{elapsed_seconds}秒……"
            )

            time.sleep(retry_interval_seconds)

    raise TimeoutError(
        f"等待SSH连接超时：{last_error}"
    )


def execute_remote_command(
    server: ServerInfo,
    command: str,
    timeout_seconds: int | None = None,
    check: bool = True,
) -> RemoteCommandResult:
    """
    通过SSH执行一条远程命令。

    server：
        服务器连接信息。

    command：
        要在服务器上执行的Shell命令。

    timeout_seconds：
        命令最长执行时间。None表示不主动限制。

    check：
        为True时，退出码非0会抛出异常。
    """

    if not command.strip():
        raise ValueError("远程命令不能为空")

    ssh_client = create_ssh_client(server)

    try:
        stdin_stream, stdout_stream, stderr_stream = (
            ssh_client.exec_command(
                command,
                timeout=timeout_seconds,
            )
        )

        stdin_stream.close()

        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        channel = stdout_stream.channel
        # channel表示远程命令对应的SSH通道

        start_time = time.monotonic()
        # start_time表示远程命令开始执行的时间

        while True:
            if channel.recv_ready():
                stdout_chunks.append(
                    channel.recv(65536)
                )

            if channel.recv_stderr_ready():
                stderr_chunks.append(
                    channel.recv_stderr(65536)
                )

            if (
                channel.exit_status_ready()
                and not channel.recv_ready()
                and not channel.recv_stderr_ready()
            ):
                break

            if (
                timeout_seconds is not None
                and time.monotonic() - start_time
                > timeout_seconds
            ):
                channel.close()

                raise TimeoutError(
                    f"远程命令执行超时：{command}"
                )

            time.sleep(0.05)

        exit_code = channel.recv_exit_status()
        # exit_code表示远程命令最终退出码

        stdout_text = b"".join(
            stdout_chunks
        ).decode(
            "utf-8",
            errors="replace",
        )

        stderr_text = b"".join(
            stderr_chunks
        ).decode(
            "utf-8",
            errors="replace",
        )

        result = RemoteCommandResult(
            command=command,
            exit_code=exit_code,
            stdout=stdout_text,
            stderr=stderr_text,
        )

        if check and exit_code != 0:
            raise RuntimeError(
                "远程命令执行失败\n"
                f"命令：{command}\n"
                f"退出码：{exit_code}\n"
                f"标准输出：\n{stdout_text}\n"
                f"错误输出：\n{stderr_text}"
            )

        return result

    finally:
        ssh_client.close()


def upload_file(
    server: ServerInfo,
    local_path: str | Path,
    remote_path: str,
) -> None:
    """
    通过SFTP上传单个文件。

    local_path：
        本地文件路径。

    remote_path：
        服务器目标文件路径。
    """

    local_file = Path(local_path).expanduser().resolve()
    # local_file表示整理后的本地文件绝对路径

    if not local_file.is_file():
        raise FileNotFoundError(
            f"本地文件不存在：{local_file}"
        )

    if not remote_path.startswith("/"):
        raise ValueError(
            "remote_path必须是服务器绝对路径"
        )

    ssh_client = create_ssh_client(server)

    try:
        sftp_client = ssh_client.open_sftp()

        try:
            sftp_client.put(
                str(local_file),
                remote_path,
            )
        finally:
            sftp_client.close()

    finally:
        ssh_client.close()


def download_file(
    server: ServerInfo,
    remote_path: str,
    local_path: str | Path,
) -> Path:
    """
    通过SFTP下载单个文件。

    remote_path：
        服务器文件路径。

    local_path：
        本地保存路径。
    """

    if not remote_path.startswith("/"):
        raise ValueError(
            "remote_path必须是服务器绝对路径"
        )

    local_file = Path(local_path).expanduser().resolve()
    # local_file表示文件在本地的保存位置

    local_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    ssh_client = create_ssh_client(server)

    try:
        sftp_client = ssh_client.open_sftp()

        try:
            sftp_client.get(
                remote_path,
                str(local_file),
            )
        finally:
            sftp_client.close()

    finally:
        ssh_client.close()

    return local_file


SERVER_INITIALIZATION_SCRIPT = r"""
set -euo pipefail

echo "[SuanQi] 开始初始化服务器"

export DEBIAN_FRONTEND=noninteractive

if ! id suanqi-task >/dev/null 2>&1; then
    useradd \
        --system \
        --create-home \
        --shell /usr/sbin/nologin \
        suanqi-task

    echo "[SuanQi] 已创建suanqi-task用户"
else
    echo "[SuanQi] suanqi-task用户已经存在"
fi

mkdir -p /opt/suanqi
mkdir -p /opt/suanqi/worker
mkdir -p /opt/suanqi/tasks

chown root:root /opt/suanqi
chmod 755 /opt/suanqi

chown root:root /opt/suanqi/worker
chmod 700 /opt/suanqi/worker

chown root:root /opt/suanqi/tasks
chmod 711 /opt/suanqi/tasks

echo "[SuanQi] 正在更新软件包索引"
apt-get update -y

echo "[SuanQi] 正在安装Python运行环境"
apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    ca-certificates

echo "[SuanQi] SUANQI_INITIALIZATION_SUCCESS"
"""


def initialize_server(
    server: ServerInfo,
    timeout_seconds: int = 900,
) -> RemoteCommandResult:
    """
    初始化SuanQi服务器运行环境。

    初始化内容：
        创建suanqi-task普通用户；
        创建root保护的worker目录；
        创建任务目录；
        安装Python、pip和venv。
    """

    command = (
        "sudo bash -c "
        + shlex.quote(SERVER_INITIALIZATION_SCRIPT)
    )

    result = execute_remote_command(
        server=server,
        command=command,
        timeout_seconds=timeout_seconds,
        check=True,
    )

    if (
        "SUANQI_INITIALIZATION_SUCCESS"
        not in result.stdout
    ):
        raise RuntimeError(
            "服务器初始化命令已经结束，"
            "但没有返回SuanQi初始化成功标记"
        )

    return result


def create_remote_task_directory(
    server: ServerInfo,
    task_id: str,
) -> dict[str, str]:
    """
    创建一个远程任务目录。

    task_id：
        当前任务的唯一编号。

    返回：
        用户目录和控制目录等远程路径。
    """

    safe_task_id = task_id.strip()

    if not safe_task_id:
        raise ValueError("task_id不能为空")

    allowed_characters = (
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789-_"
    )

    if any(
        character not in allowed_characters
        for character in safe_task_id
    ):
        raise ValueError(
            "task_id只能包含字母、数字、短横线和下划线"
        )

    task_root = (
        f"/opt/suanqi/tasks/{safe_task_id}"
    )
    # task_root表示当前任务的服务器根目录

    user_directory = f"{task_root}/user"
    # user_directory表示用户程序及结果文件目录

    control_directory = f"{task_root}/control"
    # control_directory表示root控制文件及日志目录

    setup_script = f"""
set -euo pipefail

mkdir -p {shlex.quote(user_directory)}
mkdir -p {shlex.quote(control_directory)}

chown -R suanqi-task:suanqi-task \
    {shlex.quote(user_directory)}

chmod 700 \
    {shlex.quote(user_directory)}

chown -R root:root \
    {shlex.quote(control_directory)}

chmod 700 \
    {shlex.quote(control_directory)}

chown root:root \
    {shlex.quote(task_root)}

chmod 711 \
    {shlex.quote(task_root)}

echo "SUANQI_TASK_DIRECTORY_SUCCESS"
"""

    result = execute_remote_command(
        server=server,
        command=(
            "sudo bash -c "
            + shlex.quote(setup_script)
        ),
        timeout_seconds=60,
        check=True,
    )

    if (
        "SUANQI_TASK_DIRECTORY_SUCCESS"
        not in result.stdout
    ):
        raise RuntimeError(
            "远程任务目录创建失败"
        )

    return {
        "task_root": task_root,
        "user_directory": user_directory,
        "control_directory": control_directory,
    }


def verify_server_permissions(
    server: ServerInfo,
) -> RemoteCommandResult:
    """
    验证服务器目录和用户权限是否符合预期。
    """

    verification_script = r"""
set -euo pipefail

echo "========== Python版本 =========="
python3 --version

echo "========== 任务用户 =========="
id suanqi-task

echo "========== SuanQi目录 =========="
ls -ld \
    /opt/suanqi \
    /opt/suanqi/worker \
    /opt/suanqi/tasks

echo "========== worker目录保护测试 =========="

if runuser \
    --user suanqi-task \
    -- \
    test -r /opt/suanqi/worker
then
    echo "ERROR: suanqi-task可以读取worker目录"
    exit 1
else
    echo "OK: suanqi-task无法读取worker目录"
fi

if runuser \
    --user suanqi-task \
    -- \
    test -w /opt/suanqi/worker
then
    echo "ERROR: suanqi-task可以修改worker目录"
    exit 1
else
    echo "OK: suanqi-task无法修改worker目录"
fi

echo "SUANQI_PERMISSION_VERIFICATION_SUCCESS"
"""

    result = execute_remote_command(
        server=server,
        command=(
            "sudo bash -c "
            + shlex.quote(verification_script)
        ),
        timeout_seconds=60,
        check=True,
    )

    if (
        "SUANQI_PERMISSION_VERIFICATION_SUCCESS"
        not in result.stdout
    ):
        raise RuntimeError(
            "服务器权限验证没有返回成功标记"
        )

    return result


def ask_release_instance(
    server: ServerInfo,
) -> None:
    """
    询问用户是否释放当前腾讯云实例。
    """

    while True:
        user_input = input(
            "\n是否释放实例？[Y/n]："
        ).strip().lower()

        if user_input in {
            "",
            "y",
            "yes",
        }:
            print("正在提交服务器释放请求……")

            terminate_result = gateway.terminate_instances(
                server.region,
                [server.instance_id],
            )

            if terminate_result.success:
                print(
                    "服务器释放请求已提交："
                    f"{server.instance_id}"
                )
            else:
                print("服务器释放请求提交失败")
                print(
                    "错误代码："
                    f"{terminate_result.error_code}"
                )
                print(
                    "错误信息："
                    f"{terminate_result.error_message}"
                )

            return

        if user_input in {
            "n",
            "no",
        }:
            print(
                "服务器暂未释放，请注意按量计费仍在继续。"
            )
            return

        print("请输入Y、Yes、N或No。")


def main() -> None:
    """
    SuanQi服务器创建与初始化测试入口。
    """

    server: ServerInfo | None = None
    # server表示当前已经创建的云服务器
    # 创建失败时保持为None

    try:
        print("正在搜索并创建腾讯云服务器……")

        create_result = tencentcloud_creat()
        # create_result表示腾讯云服务器创建结果

        if not create_result.get("success"):
            raise RuntimeError(
                "服务器创建失败："
                f"{create_result}"
            )

        server= parse_server_info(create_result)

        print(
            "服务器创建成功：\n"
            f"  实例ID：{server.instance_id}\n"
            f"  地域：{server.region}\n"
            f"  公网IP：{server.public_ip}\n"
            f"  SSH用户：{server.ssh_username}"
        )

        # 不输出instance_password，防止密码进入终端日志

        print("\n正在等待SSH服务启动……")

        wait_for_ssh(
            server=server,
            timeout_seconds=300,
            retry_interval_seconds=5,
        )

        print("SSH连接成功，正在检查服务器环境……")

        environment_result = execute_remote_command(
            server=server,
            command=(
                "uname -a && "
                "echo 'CPU核心数：' && "
                "nproc && "
                "echo '内存信息：' && "
                "free -h && "
                "echo 'Python版本：' && "
                "python3 --version"
            ),
            timeout_seconds=60,
            check=True,
        )

        print("\n服务器环境信息：")
        print(environment_result.stdout)

        if environment_result.stderr:
            print("环境检查错误输出：")
            print(environment_result.stderr)

        print("正在初始化SuanQi服务器环境……")

        initialization_result = initialize_server(
            server=server,
            timeout_seconds=900,
        )

        print(initialization_result.stdout)

        print("正在验证服务器权限配置……")

        permission_result = verify_server_permissions(
            server
        )

        print(permission_result.stdout)

        task_id = (
            "test-"
            + str(int(time.time()))
        )
        # task_id表示当前测试任务的唯一编号

        print(
            f"正在创建测试任务目录：{task_id}"
        )

        task_directories = (
            create_remote_task_directory(
                server=server,
                task_id=task_id,
            )
        )

        print("任务目录创建成功：")
        print(
            "  用户目录："
            f"{task_directories['user_directory']}"
        )
        print(
            "  控制目录："
            f"{task_directories['control_directory']}"
        )

        print(
            "\n服务器创建、SSH连接、初始化及"
            "权限隔离测试全部完成。"
        )

    except KeyboardInterrupt:
        print("\n用户中断了当前操作。")

    except Exception as error:
        print("\n程序执行失败：")
        print(
            f"{error.__class__.__name__}：{error}"
        )

    finally:
        if server is not None:
            ask_release_instance(server)


if __name__ == "__main__":
    main()

