# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import json
import logging
import shlex
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import paramiko


logging.getLogger("paramiko").setLevel(
    logging.CRITICAL
)


@dataclass(slots=True)
class ServerInfo:
    """云服务器连接信息。"""

    provider: str
    region: str
    instance_id: str
    public_ip: str
    ssh_username: str
    ssh_port: int
    instance_password: str
    cpu: int | None = None
    memory_gb: int | None = None


@dataclass(slots=True)
class RemoteCommandResult:
    """远程命令执行结果。"""

    command: str
    exit_code: int
    stdout: str
    stderr: str


@dataclass(slots=True)
class PreparedTask:
    """已经上传完成、等待 worker 执行的任务。"""

    task_id: str
    task_root: str
    user_directory: str
    control_directory: str

    main_file: str
    # main_file 是服务器上的 main.py 完整路径

    requirements_file: str | None
    # requirements_file 是服务器上的 requirements.txt 完整路径
    # 没有 requirements.txt 时为 None

    virtualenv_python: str
    # virtualenv_python 是未来由 worker 创建的虚拟环境 Python 路径


@dataclass(slots=True)
class WorkerTask:

    task_id: str
    service_name: str
    task_root: str
    user_directory: str
    control_directory: str
    status_path: str
    task_log_path: str
    worker_log_path: str
    manifest_path: str


def parse_server_info(
    create_result: dict[str, Any],
) -> ServerInfo:
    """整理服务器创建结果。"""

    if not create_result.get("success"):
        raise RuntimeError("服务器创建失败")

    if not create_result.get("public_ip"):
        raise RuntimeError("服务器没有公网 IP")

    if not create_result.get("instance_password"):
        raise RuntimeError("服务器没有登录密码")

    return ServerInfo(
        provider=str(create_result["provider"]),
        region=str(create_result["region"]),
        instance_id=str(create_result["instance_id"]),
        public_ip=str(create_result["public_ip"]),
        ssh_username=str(
            create_result.get("ssh_username")
            or "ubuntu"
        ),
        ssh_port=int(
            create_result.get("ssh_port")
            or 22
        ),
        instance_password=str(
            create_result["instance_password"]
        ),
        cpu=create_result.get("cpu"),
        memory_gb=create_result.get("memory_gb"),
    )


def create_ssh_client(
    server: ServerInfo,
) -> paramiko.SSHClient:
    """创建 SSH 连接。"""

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(
        paramiko.AutoAddPolicy()
    )

    client.connect(
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

    return client


def wait_for_ssh(
    server: ServerInfo,
    timeout_seconds: int = 300,
    retry_interval_seconds: int = 5,
) -> None:
    """等待 SSH 服务就绪。"""

    start_time = time.monotonic()
    last_error: Exception | None = None

    while (
        time.monotonic() - start_time
        < timeout_seconds
    ):
        try:
            client = create_ssh_client(server)
            client.close()
            return

        except (
            paramiko.SSHException,
            socket.timeout,
            ConnectionRefusedError,
            OSError,
        ) as error:
            last_error = error
            time.sleep(retry_interval_seconds)

    raise TimeoutError(
        f"等待 SSH 超时：{last_error}"
    )


def execute_remote_command(
    server: ServerInfo,
    command: str,
    timeout_seconds: int | None = None,
    check: bool = True,
) -> RemoteCommandResult:
    """执行远程命令并收集输出。"""

    client = create_ssh_client(server)

    try:
        stdin_stream, stdout_stream, _ = (
            client.exec_command(command)
        )
        stdin_stream.close()

        channel = stdout_stream.channel
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        start_time = time.monotonic()

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

        result = RemoteCommandResult(
            command=command,
            exit_code=channel.recv_exit_status(),
            stdout=b"".join(
                stdout_chunks
            ).decode(
                "utf-8",
                errors="replace",
            ),
            stderr=b"".join(
                stderr_chunks
            ).decode(
                "utf-8",
                errors="replace",
            ),
        )

        if check and result.exit_code != 0:
            raise RuntimeError(
                "远程命令执行失败\n"
                f"命令：{command}\n"
                f"退出码：{result.exit_code}\n"
                f"标准输出：\n{result.stdout}\n"
                f"错误输出：\n{result.stderr}"
            )

        return result

    finally:
        client.close()


def upload_file(
    server: ServerInfo,
    local_path: str | Path,
    remote_path: str,
) -> None:
    """上传单个文件。"""

    local_file = Path(
        local_path
    ).expanduser().resolve()

    if not local_file.is_file():
        raise FileNotFoundError(
            f"本地文件不存在：{local_file}"
        )

    client = create_ssh_client(server)

    try:
        sftp = client.open_sftp()

        try:
            sftp.put(
                str(local_file),
                remote_path,
            )
        finally:
            sftp.close()
    finally:
        client.close()


def download_file(
    server: ServerInfo,
    remote_path: str,
    local_path: str | Path,
) -> Path:
    """下载单个文件。"""

    local_file = Path(
        local_path
    ).expanduser().resolve()

    local_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    client = create_ssh_client(server)

    try:
        sftp = client.open_sftp()

        try:
            sftp.get(
                remote_path,
                str(local_file),
            )
        finally:
            sftp.close()
    finally:
        client.close()

    return local_file


SERVER_INITIALIZATION_SCRIPT = r"""
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

if ! id suanqi-task >/dev/null 2>&1; then
    useradd \
        --system \
        --create-home \
        --shell /usr/sbin/nologin \
        suanqi-task
fi

mkdir -p /opt/suanqi/worker
mkdir -p /opt/suanqi/tasks

chown root:root /opt/suanqi
chmod 755 /opt/suanqi

chown root:root /opt/suanqi/worker
chmod 700 /opt/suanqi/worker

chown root:root /opt/suanqi/tasks
chmod 711 /opt/suanqi/tasks

apt-get update -y

apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    ca-certificates

echo "SUANQI_INITIALIZATION_SUCCESS"
"""


def initialize_server(
    server: ServerInfo,
) -> None:
    """初始化服务器。"""

    result = execute_remote_command(
        server,
        (
            "sudo bash -c "
            + shlex.quote(
                SERVER_INITIALIZATION_SCRIPT
            )
        ),
        timeout_seconds=900,
        check=True,
    )

    if (
        "SUANQI_INITIALIZATION_SUCCESS"
        not in result.stdout
    ):
        raise RuntimeError(
            "服务器初始化没有返回成功标记"
        )


def generate_task_id() -> str:
    """生成任务 ID。"""

    return (
        "task-"
        + time.strftime("%Y%m%d-%H%M%S")
        + "-"
        + uuid.uuid4().hex[:6]
    )


def create_remote_task_directory(
    server: ServerInfo,
    task_id: str,
) -> dict[str, str]:
    """创建远程任务目录。"""

    task_root = f"/opt/suanqi/tasks/{task_id}"
    user_directory = f"{task_root}/user"
    control_directory = f"{task_root}/control"

    script = f"""
set -euo pipefail

mkdir -p {shlex.quote(user_directory)}
mkdir -p {shlex.quote(control_directory)}

chown -R suanqi-task:suanqi-task \
    {shlex.quote(user_directory)}
chmod 700 {shlex.quote(user_directory)}

chown -R root:root \
    {shlex.quote(control_directory)}
chmod 700 {shlex.quote(control_directory)}

chown root:root {shlex.quote(task_root)}
chmod 711 {shlex.quote(task_root)}
"""

    execute_remote_command(
        server,
        (
            "sudo bash -c "
            + shlex.quote(script)
        ),
        timeout_seconds=60,
        check=True,
    )

    return {
        "task_root": task_root,
        "user_directory": user_directory,
        "control_directory": control_directory,
    }


def upload_task_file(
    server: ServerInfo,
    local_path: str | Path,
    user_directory: str,
    remote_filename: str,
) -> str:
    """上传任务文件。"""

    temporary_path = (
        f"/home/{server.ssh_username}/"
        f".suanqi-upload-{uuid.uuid4().hex}"
    )
    final_path = (
        f"{user_directory}/{remote_filename}"
    )

    try:
        upload_file(
            server,
            local_path,
            temporary_path,
        )

        execute_remote_command(
            server,
            (
                "sudo install "
                "-o suanqi-task "
                "-g suanqi-task "
                "-m 600 "
                f"{shlex.quote(temporary_path)} "
                f"{shlex.quote(final_path)}"
            ),
            timeout_seconds=60,
            check=True,
        )
    finally:
        execute_remote_command(
            server,
            f"rm -f {shlex.quote(temporary_path)}",
            check=False,
        )

    return final_path


def create_virtual_environment(
    server: ServerInfo,
    user_directory: str,
) -> str:
    """
    创建虚拟环境。

    注意：
        不自动升级 pip、setuptools 和 wheel，
        避免额外联网等待和失败点。
    """

    script = f"""
set -euo pipefail

cd {shlex.quote(user_directory)}

echo "[SuanQi] 开始创建虚拟环境"

python3 -m venv .venv

echo "[SuanQi] 虚拟环境创建完成"

.venv/bin/python --version
.venv/bin/python -m pip --version
"""

    result = execute_remote_command(
        server,
        (
            "sudo runuser "
            "--user suanqi-task "
            "-- "
            "bash -c "
            + shlex.quote(script)
        ),
        timeout_seconds=300,
        check=True,
    )

    print(result.stdout)

    return (
        f"{user_directory}/.venv/bin/python"
    )


def install_requirements(
    server: ServerInfo,
    user_directory: str,
) -> None:
    """安装 requirements.txt。"""

    script = f"""
set -euo pipefail
cd {shlex.quote(user_directory)}

.venv/bin/python -m pip install \
    --disable-pip-version-check \
    -r requirements.txt
"""

    result = execute_remote_command(
        server,
        (
            "sudo runuser "
            "--user suanqi-task "
            "-- "
            "bash -c "
            + shlex.quote(script)
        ),
        timeout_seconds=3600,
        check=True,
    )

    print(result.stdout)


def install_packages(
    server: ServerInfo,
    user_directory: str,
    packages: list[str],
) -> None:
    """安装 -i 指定的依赖。"""

    cleaned = [
        package.strip()
        for package in packages
        if package.strip()
    ]

    if not cleaned:
        return

    quoted_packages = " ".join(
        shlex.quote(package)
        for package in cleaned
    )

    script = f"""
set -euo pipefail
cd {shlex.quote(user_directory)}

.venv/bin/python -m pip install \
    --disable-pip-version-check \
    {quoted_packages}
"""

    result = execute_remote_command(
        server,
        (
            "sudo runuser "
            "--user suanqi-task "
            "-- "
            "bash -c "
            + shlex.quote(script)
        ),
        timeout_seconds=3600,
        check=True,
    )

    print(result.stdout)


def prepare_python_task(
    server: ServerInfo,
    python_file: str | Path,
    requirements_file: str | Path | None,
) -> PreparedTask:
    """
    上传任务文件。

    此函数只负责：

    1. 创建远程任务目录
    2. 上传 main.py
    3. 可选上传 requirements.txt

    不再负责：

    1. 创建虚拟环境
    2. 安装 requirements.txt
    3. 安装 -i 指定的软件包

    上述环境准备工作全部交给远程 worker。
    """

    task_id = generate_task_id()

    directories = create_remote_task_directory(
        server,
        task_id,
    )

    user_directory = directories[
        "user_directory"
    ]

    print("正在上传 Python 文件……")

    main_file = upload_task_file(
        server=server,
        local_path=python_file,
        user_directory=user_directory,
        remote_filename="main.py",
    )

    remote_requirements: str | None = None

    if requirements_file is not None:
        print("正在上传 requirements.txt……")

        remote_requirements = upload_task_file(
            server=server,
            local_path=requirements_file,
            user_directory=user_directory,
            remote_filename="requirements.txt",
        )

    virtualenv_python = (
        f"{user_directory}/.venv/bin/python"
    )
    # virtualenv_python 是 worker 创建虚拟环境后，
    # 虚拟环境中 Python 的固定路径

    return PreparedTask(
        task_id=task_id,
        task_root=directories["task_root"],
        user_directory=user_directory,
        control_directory=directories[
            "control_directory"
        ],
        main_file=main_file,
        requirements_file=remote_requirements,
        virtualenv_python=virtualenv_python,
    )

def write_remote_root_file(
    server: ServerInfo,
    remote_path: str,
    content: str,
    mode: int,
) -> None:
    """写入 root 文件。"""

    local_temp = (
        Path.cwd()
        / f".suanqi-{uuid.uuid4().hex}.tmp"
    )
    remote_temp = (
        f"/home/{server.ssh_username}/"
        f".suanqi-{uuid.uuid4().hex}.tmp"
    )

    try:
        local_temp.write_text(
            content,
            encoding="utf-8",
        )

        upload_file(
            server,
            local_temp,
            remote_temp,
        )

        execute_remote_command(
            server,
            (
                "sudo install "
                "-o root -g root "
                f"-m {mode:o} "
                f"{shlex.quote(remote_temp)} "
                f"{shlex.quote(remote_path)}"
            ),
            timeout_seconds=60,
            check=True,
        )
    finally:
        local_temp.unlink(
            missing_ok=True,
        )
        execute_remote_command(
            server,
            f"rm -f {shlex.quote(remote_temp)}",
            check=False,
        )

def attach_worker_task(
    server: ServerInfo,
    worker_task: WorkerTask,
) -> dict[str, Any]:
    """
    重新连接已经启动的任务并继续显示日志。

    从日志开头重新显示。
    """

    task_log_offset = 0
    # task_log_offset 是 task.log 已读取到的字节位置

    worker_log_offset = 0
    # worker_log_offset 是 worker.log 已读取到的字节位置

    terminal_statuses = {
        "SUCCESS",
        "FAILED",
        "TIMEOUT",
        "WORKER_FAILED",
    }

    last_status_name: str | None = None

    print(
        f"正在连接任务：{worker_task.task_id}"
    )

    while True:
        try:
            worker_data, worker_log_offset = (
                read_root_log_chunk(
                    server=server,
                    remote_path=(
                        worker_task.worker_log_path
                    ),
                    offset=worker_log_offset,
                )
            )

            if worker_data:
                print(
                    worker_data.decode(
                        "utf-8",
                        errors="replace",
                    ),
                    end="",
                    flush=True,
                )

            task_data, task_log_offset = (
                read_root_log_chunk(
                    server=server,
                    remote_path=(
                        worker_task.task_log_path
                    ),
                    offset=task_log_offset,
                )
            )

            if task_data:
                print(
                    task_data.decode(
                        "utf-8",
                        errors="replace",
                    ),
                    end="",
                    flush=True,
                )

            status_data = read_root_json_file(
                server=server,
                remote_path=(
                    worker_task.status_path
                ),
            )

            if status_data is not None:
                status_name = (
                    status_data.get("status")
                    or status_data.get("state")
                )

                if (
                    status_name
                    and status_name
                    != last_status_name
                ):
                    last_status_name = status_name

                    print(
                        "\n[SuanQi] 当前状态："
                        f"{status_name}"
                    )

                if status_name in terminal_statuses:
                    # 状态先写入、日志后写入时，
                    # 再读取一次，避免漏掉最后几行
                    worker_data, worker_log_offset = (
                        read_root_log_chunk(
                            server=server,
                            remote_path=(
                                worker_task.worker_log_path
                            ),
                            offset=worker_log_offset,
                        )
                    )

                    if worker_data:
                        print(
                            worker_data.decode(
                                "utf-8",
                                errors="replace",
                            ),
                            end="",
                            flush=True,
                        )

                    task_data, task_log_offset = (
                        read_root_log_chunk(
                            server=server,
                            remote_path=(
                                worker_task.task_log_path
                            ),
                            offset=task_log_offset,
                        )
                    )

                    if task_data:
                        print(
                            task_data.decode(
                                "utf-8",
                                errors="replace",
                            ),
                            end="",
                            flush=True,
                        )

                    return status_data

        except (
            paramiko.SSHException,
            socket.timeout,
            ConnectionRefusedError,
            OSError,
        ) as error:
            print(
                "\n[SuanQi] SSH 连接中断："
                f"{error}"
            )
            print(
                "[SuanQi] 正在重新连接……"
            )

            wait_for_ssh(
                server
            )

            print(
                "[SuanQi] 已重新连接，"
                "继续输出日志。"
            )

        time.sleep(1)



def start_worker_task(
    server: ServerInfo,
    task: PreparedTask,
    return_files: list[str],
    local_worker_path: str | Path,
    packages: list[str] | None = None,
    max_use_seconds: int = 5 * 60 * 60,
    terminate_grace_seconds: int = 30,
    preparation_timeout_seconds: int = 2 * 60 * 60,
) -> WorkerTask:
    """
    上传并启动 systemd 守护任务。

    参数：
        packages：
            用户通过 -i 指定的 Python 软件包。

        max_use_seconds：
            main.py 最大运行时间，单位秒。
            不包含创建虚拟环境和安装依赖的时间。

        terminate_grace_seconds：
            main.py 超时后发送 SIGTERM，
            等待程序自行退出的时间。

        preparation_timeout_seconds：
            创建虚拟环境和安装依赖的最大总时间。
            默认 2 小时。
    """

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

    cleaned_packages = [
        package.strip()
        for package in (packages or [])
        if package.strip()
    ]
    # cleaned_packages 是去除空字符串后的依赖列表

    worker_path = (
        "/opt/suanqi/worker/suanqi_worker.py"
    )

    write_remote_root_file(
        server=server,
        remote_path=worker_path,
        content=Path(
            local_worker_path
        ).read_text(
            encoding="utf-8"
        ),
        mode=0o700,
    )

    safe_return_files = [
        validate_return_path(path)
        for path in return_files
    ]

    config_path = (
        f"{task.control_directory}/task.json"
    )

    requirements_filename = (
        "requirements.txt"
        if task.requirements_file is not None
        else None
    )

    config = {
        "task_id": task.task_id,

        "user_directory": (
            task.user_directory
        ),

        "control_directory": (
            task.control_directory
        ),

        "main_filename": "main.py",

        "requirements_filename": (
            requirements_filename
        ),

        "packages": cleaned_packages,

        "virtualenv_python": (
            task.virtualenv_python
        ),

        "return_files": safe_return_files,

        "max_use_seconds": (
            max_use_seconds
        ),

        "terminate_grace_seconds": (
            terminate_grace_seconds
        ),

        "preparation_timeout_seconds": (
            preparation_timeout_seconds
        ),
    }

    write_remote_root_file(
        server=server,
        remote_path=config_path,
        content=json.dumps(
            config,
            ensure_ascii=False,
            indent=2,
        ),
        mode=0o600,
    )

    service_name = (
        "suanqi-"
        + task.task_id.replace("_", "-")
    )

    service_path = (
        f"/etc/systemd/system/"
        f"{service_name}.service"
    )

    service_content = f"""[Unit]
Description=SuanQi task {task.task_id}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory={task.control_directory}
ExecStart=/usr/bin/python3 {worker_path} {config_path}
Restart=no
Nice=-5

[Install]
WantedBy=multi-user.target
"""

    write_remote_root_file(
        server=server,
        remote_path=service_path,
        content=service_content,
        mode=0o644,
    )

    execute_remote_command(
        server,
        (
            "sudo systemctl daemon-reload && "
            f"sudo systemctl enable "
            f"{shlex.quote(service_name)}.service && "
            f"sudo systemctl start "
            f"{shlex.quote(service_name)}.service"
        ),
        timeout_seconds=60,
        check=True,
    )

    return WorkerTask(
        task_id=task.task_id,
        service_name=service_name,
        task_root=task.task_root,
        user_directory=task.user_directory,
        control_directory=task.control_directory,
        status_path=(
            f"{task.control_directory}/status.json"
        ),
        task_log_path=(
            f"{task.control_directory}/task.log"
        ),
        worker_log_path=(
            f"{task.control_directory}/worker.log"
        ),
        manifest_path=(
            f"{task.control_directory}/manifest.json"
        ),
    )

def read_root_json_file(
    server: ServerInfo,
    remote_path: str,
) -> dict[str, Any] | None:
    """读取 root JSON 文件。"""

    result = execute_remote_command(
        server,
        (
            "sudo test -f "
            f"{shlex.quote(remote_path)} "
            "&& sudo cat "
            f"{shlex.quote(remote_path)}"
        ),
        timeout_seconds=30,
        check=False,
    )

    if result.exit_code != 0:
        return None

    return json.loads(result.stdout)


def read_root_log_chunk(
    server: ServerInfo,
    remote_path: str,
    offset: int,
) -> tuple[bytes, int]:
    """从指定偏移量读取日志。"""

    script = f"""
import base64
from pathlib import Path

path = Path({remote_path!r})
offset = {offset}

if not path.exists():
    print("0:")
else:
    with path.open("rb") as file:
        file.seek(offset)
        data = file.read()
        new_offset = file.tell()

    print(
        str(new_offset)
        + ":"
        + base64.b64encode(data).decode("ascii")
    )
"""

    result = execute_remote_command(
        server,
        (
            "sudo python3 -c "
            + shlex.quote(script)
        ),
        timeout_seconds=30,
        check=True,
    )

    offset_text, encoded = (
        result.stdout.strip().split(
            ":",
            1,
        )
    )

    return (
        base64.b64decode(encoded)
        if encoded
        else b"",
        int(offset_text),
    )


def follow_worker_task(
    server: ServerInfo,
    worker_task: WorkerTask,
) -> dict[str, Any]:
    """实时读取任务日志，SSH 断开后自动重连。"""

    offset = 0
    # offset 代表已经读取到 task.log 的字节位置
    # SSH 重连后不会清零，所以不会重复输出旧日志

    terminal_statuses = {
        "SUCCESS",
        "FAILED",
        "TIMEOUT",
        "WORKER_FAILED",
    }

    while True:
        try:
            data, offset = read_root_log_chunk(
                server,
                worker_task.task_log_path,
                offset,
            )

            if data:
                print(
                    data.decode(
                        "utf-8",
                        errors="replace",
                    ),
                    end="",
                    flush=True,
                )

            status = read_root_json_file(
                server,
                worker_task.status_path,
            )

            if status is not None:
                status_name = (
                    status.get("status")
                    or status.get("state")
                )

                if status_name in terminal_statuses:
                    return status

        except (
            paramiko.SSHException,
            socket.timeout,
            ConnectionRefusedError,
            OSError,
        ) as error:
            print(
                "\n[SuanQi] SSH 连接中断，"
                "远程任务仍在运行。"
            )

            print(
                f"[SuanQi] 断开原因：{error}"
            )

            print(
                "[SuanQi] 正在重新连接……"
            )

            wait_for_ssh(server)

            print(
                "[SuanQi] 已重新连接，"
                "继续输出日志。"
            )

        time.sleep(1)

def validate_return_path(
    return_path: str,
) -> str:
    """验证返回文件相对路径。"""

    path = PurePosixPath(return_path)

    if path.is_absolute() or ".." in path.parts:
        raise ValueError(
            f"不安全的返回文件路径：{return_path}"
        )

    return str(path)


def download_return_files(
    server: ServerInfo,
    task: PreparedTask,
    return_files: list[str],
) -> tuple[list[dict[str, str]], list[str]]:
    """下载返回文件。"""

    downloaded = []
    missing = []

    local_root = (
        Path("suanqi-results")
        / task.task_id
    ).resolve()

    for requested_path in return_files:
        safe_path = validate_return_path(
            requested_path
        )

        remote_path = (
            f"{task.user_directory}/{safe_path}"
        )

        exists_result = execute_remote_command(
            server,
            (
                "sudo test -f "
                + shlex.quote(remote_path)
            ),
            check=False,
        )

        if exists_result.exit_code != 0:
            missing.append(safe_path)
            continue

        temporary_remote = (
            f"/home/{server.ssh_username}/"
            f".suanqi-download-{uuid.uuid4().hex}"
        )

        local_path = (
            local_root
            / Path(
                *PurePosixPath(
                    safe_path
                ).parts
            )
        )

        try:
            execute_remote_command(
                server,
                (
                    "sudo install "
                    f"-o {shlex.quote(server.ssh_username)} "
                    f"-g {shlex.quote(server.ssh_username)} "
                    "-m 600 "
                    f"{shlex.quote(remote_path)} "
                    f"{shlex.quote(temporary_remote)}"
                ),
                check=True,
            )

            downloaded_path = download_file(
                server,
                temporary_remote,
                local_path,
            )

            downloaded.append(
                {
                    "remote_path": safe_path,
                    "local_path": str(
                        downloaded_path
                    ),
                }
            )
        finally:
            execute_remote_command(
                server,
                f"rm -f {shlex.quote(temporary_remote)}",
                check=False,
            )

    return downloaded, missing
