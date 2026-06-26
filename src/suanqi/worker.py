# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import hmac
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_MAX_USE_SECONDS = 5 * 60 * 60
# 用户程序默认最大运行时间：5小时

DEFAULT_TERMINATE_GRACE_SECONDS = 30
# 发送 SIGTERM 后等待程序自行退出的时间：30秒

DEFAULT_PREPARATION_TIMEOUT_SECONDS = 30 * 60
# 创建虚拟环境和安装依赖默认最多允许30分钟

DEFAULT_PIP_INDEX_URL = (
    "https://mirrors.cloud.tencent.com/pypi/simple"
)
# 腾讯云 PyPI 镜像地址

DEFAULT_PIP_NETWORK_TIMEOUT_SECONDS = 120
# pip 单次连接或读取网络数据的超时时间：120秒

DEFAULT_PIP_RETRY_COUNT = 5
# pip 下载失败后的重试次数


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

    temporary_name: str | None = None

    try:
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

            temporary_file.flush()

            os.fsync(
                temporary_file.fileno()
            )

            temporary_name = (
                temporary_file.name
            )

        os.replace(
            temporary_name,
            file_path,
        )

    finally:
        if (
            temporary_name is not None
            and os.path.exists(temporary_name)
        ):
            try:
                os.unlink(
                    temporary_name
                )
            except OSError:
                pass


def load_task_config(
    config_path: Path,
) -> dict[str, Any]:
    """读取任务配置。"""

    with config_path.open(
        "r",
        encoding="utf-8",
    ) as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError(
            "任务配置必须是 JSON 对象"
        )

    return data


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

    data.update(
        extra
    )

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

    target.relative_to(
        base
    )

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
        requested_path = str(
            requested_path
        )

        target = validate_return_file(
            user_directory,
            requested_path,
        )

        exists = target.is_file()

        files.append(
            {
                "requested_path": (
                    requested_path
                ),
                "absolute_path": str(
                    target
                ),
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


def _cos_target_enabled(config: dict[str, Any]) -> bool:
    cos_target = config.get("cos_target") or {}
    return bool(cos_target.get("enabled"))


def _cos_target_upload_prefix(config: dict[str, Any]) -> str:
    cos_target = config.get("cos_target") or {}
    return str(cos_target.get("prefix") or "").strip().lstrip("/")



def metadata_get_text(path: str, timeout_seconds: int = 10) -> str:
    """
    读取腾讯云 CVM 实例元数据。

    path：元数据路径，例如 cam/security-credentials/。
    timeout_seconds：单次请求超时时间，单位秒。
    """

    normalized_path = path.strip().lstrip("/")
    url = (
        "http://metadata.tencentyun.com/latest/meta-data/"
        + normalized_path
    )

    with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
        return response.read().decode("utf-8").strip()


def get_instance_role_credentials(
    worker_log_path: Path,
    maximum_retry_count: int = 6,
) -> dict[str, Any]:
    """
    获取实例角色临时密钥。

    worker_log_path：worker 日志文件路径。
    maximum_retry_count：最大重试次数。
    """

    last_error: BaseException | None = None

    for attempt_number in range(maximum_retry_count):
        # attempt_number：当前尝试次数，从 0 开始
        try:
            role_list_text = metadata_get_text("cam/security-credentials/")
            role_names = [
                line.strip()
                for line in role_list_text.splitlines()
                if line.strip()
            ]

            if not role_names:
                raise RuntimeError("实例没有返回可用角色名称")

            role_name = role_names[0]
            credentials_text = metadata_get_text(
                "cam/security-credentials/" + role_name
            )
            credentials = json.loads(credentials_text)

            missing_keys = [
                key
                for key in ("TmpSecretId", "TmpSecretKey", "Token")
                if not credentials.get(key)
            ]

            if missing_keys:
                raise RuntimeError(
                    "临时密钥字段缺失：" + ", ".join(missing_keys)
                )

            credentials["RoleName"] = role_name
            return credentials

        except BaseException as error:
            last_error = error
            retry_delay_seconds = min(2 ** attempt_number, 15)
            append_worker_log(
                worker_log_path,
                (
                    "获取实例角色临时密钥失败，"
                    f"第 {attempt_number + 1} 次："
                    f"{error.__class__.__name__}：{error}，"
                    f"{retry_delay_seconds} 秒后重试"
                ),
            )
            time.sleep(retry_delay_seconds)

    raise RuntimeError(
        "无法获取实例角色临时密钥："
        f"{last_error.__class__.__name__ if last_error else 'Unknown'}："
        f"{last_error}"
    )


def is_temporary_credential_error(error: BaseException) -> bool:
    """判断异常是否大概率与临时密钥过期或无效有关。"""

    text = str(error).lower()
    keywords = (
        "expiredtoken",
        "invalidtoken",
        "token expired",
        "token has expired",
        "signature expired",
        "request has expired",
        "secretid",
        "signaturedoesnotmatch",
    )
    return any(keyword in text for keyword in keywords)


def sign_sha256(key: bytes, message: str) -> bytes:
    """HMAC-SHA256 签名。"""

    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def build_tencentcloud_authorization(
    secret_id: str,
    secret_key: str,
    service: str,
    payload: str,
    timestamp: int,
) -> str:
    """
    构造腾讯云 API 3.0 Authorization 头。

    secret_id：临时密钥 ID。
    secret_key：临时密钥 Key。
    service：服务名，例如 cvm。
    payload：请求 JSON 字符串。
    timestamp：Unix 秒级时间戳。
    """

    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
    http_request_method = "POST"
    canonical_uri = "/"
    canonical_query_string = ""
    canonical_headers = (
        "content-type:application/json; charset=utf-8\n"
        f"host:{service}.tencentcloudapi.com\n"
    )
    signed_headers = "content-type;host"
    hashed_request_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    canonical_request = "\n".join(
        [
            http_request_method,
            canonical_uri,
            canonical_query_string,
            canonical_headers,
            signed_headers,
            hashed_request_payload,
        ]
    )
    credential_scope = f"{date}/{service}/tc3_request"
    hashed_canonical_request = hashlib.sha256(
        canonical_request.encode("utf-8")
    ).hexdigest()
    string_to_sign = "\n".join(
        [
            "TC3-HMAC-SHA256",
            str(timestamp),
            credential_scope,
            hashed_canonical_request,
        ]
    )

    secret_date = sign_sha256(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = sign_sha256(secret_date, service)
    secret_signing = sign_sha256(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing,
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return (
        "TC3-HMAC-SHA256 "
        f"Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )


def call_tencentcloud_api(
    service: str,
    action: str,
    version: str,
    region: str,
    payload_data: dict[str, Any],
    credentials: dict[str, Any],
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    """
    使用临时密钥调用腾讯云 API。

    service：服务名，例如 cvm。
    action：接口名，例如 TerminateInstances。
    version：接口版本，例如 2017-03-12。
    region：地域，例如 ap-nanjing。
    payload_data：接口 JSON 参数。
    credentials：实例角色临时密钥。
    """

    endpoint = f"https://{service}.tencentcloudapi.com/"
    payload = json.dumps(payload_data, separators=(",", ":"))
    timestamp = int(time.time())
    headers = {
        "Authorization": build_tencentcloud_authorization(
            secret_id=str(credentials["TmpSecretId"]),
            secret_key=str(credentials["TmpSecretKey"]),
            service=service,
            payload=payload,
            timestamp=timestamp,
        ),
        "Content-Type": "application/json; charset=utf-8",
        "Host": f"{service}.tencentcloudapi.com",
        "X-TC-Action": action,
        "X-TC-Version": version,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Region": region,
        "X-TC-Token": str(credentials["Token"]),
    }
    request = urllib.request.Request(
        endpoint,
        data=payload.encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            response_text = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        response_text = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"腾讯云 API HTTP {error.code}：{response_text}"
        ) from error

    result = json.loads(response_text)
    response_data = result.get("Response") or {}
    if "Error" in response_data:
        api_error = response_data["Error"]
        raise RuntimeError(
            "腾讯云 API 错误："
            f"{api_error.get('Code')}，"
            f"{api_error.get('Message')}"
        )

    return response_data


def terminate_current_instance(
    config: dict[str, Any],
    worker_log_path: Path,
) -> bool:
    """
    使用实例角色临时密钥释放当前 CVM。

    config：任务配置。
    worker_log_path：worker 日志文件路径。
    """

    if not bool(config.get("enable_self_destroy")):
        append_worker_log(worker_log_path, "自销毁跳过：配置未启用")
        return False

    provider = str(config.get("provider") or "")
    if provider and provider != "tencentcloud":
        append_worker_log(worker_log_path, f"自销毁跳过：暂不支持云厂商 {provider}")
        return False

    instance_id = str(config.get("instance_id") or "").strip()
    region = str(config.get("instance_region") or "").strip()

    if not instance_id:
        try:
            instance_id = metadata_get_text("instance-id")
        except BaseException as error:
            append_worker_log(worker_log_path, f"自销毁失败：无法读取实例 ID：{error}")
            return False

    if not region:
        for metadata_path in ("placement/region", "region"):
            try:
                region = metadata_get_text(metadata_path)
                if region:
                    break
            except BaseException:
                pass

    if not region:
        append_worker_log(worker_log_path, "自销毁失败：无法确定 CVM 地域")
        return False

    try:
        credentials = get_instance_role_credentials(worker_log_path)
        response_data = call_tencentcloud_api(
            service="cvm",
            action="TerminateInstances",
            version="2017-03-12",
            region=region,
            payload_data={
                "InstanceIds": [instance_id],
                "ReleaseAddress": True,
            },
            credentials=credentials,
        )
        append_worker_log(
            worker_log_path,
            (
                "实例自销毁请求已提交："
                f"{instance_id}，RequestId={response_data.get('RequestId')}"
            ),
        )
        return True
    except BaseException as error:
        append_worker_log(
            worker_log_path,
            f"实例自销毁失败：{error.__class__.__name__}：{error}",
        )
        return False


def schedule_self_destroy(config: dict[str, Any], worker_log_path: Path) -> None:
    """
    启动后台延迟自销毁进程。

    修复点：
    1. 不再静默丢弃 stderr，所有异常写入 self_destroy.log。
    2. helper 不再 import 整个 worker.py，避免导入失败导致无日志。
    3. 直接在 helper 内实现元数据、临时密钥、签名和 TerminateInstances。
    4. 如果 helper 启动失败，会立刻在 worker.log 中显示。
    """

    delay_seconds = int(config.get("self_destroy_delay_seconds", 10))
    delay_seconds = max(0, min(delay_seconds, 300))
    helper_config_path = worker_log_path.parent / "self_destroy_config.json"
    helper_script_path = worker_log_path.parent / "self_destroy.py"
    helper_log_path = worker_log_path.parent / "self_destroy.log"

    atomic_write_json(helper_config_path, config)

    helper_script = SELF_DESTROY_HELPER_TEMPLATE
    helper_script = (
        helper_script
        .replace("__CONFIG_PATH__", repr(str(helper_config_path)))
        .replace("__WORKER_LOG_PATH__", repr(str(worker_log_path)))
        .replace("__SELF_LOG_PATH__", repr(str(helper_log_path)))
        .replace("__DELAY_SECONDS__", str(delay_seconds))
    )

    helper_script_path.write_text(helper_script, encoding="utf-8")
    os.chmod(helper_script_path, 0o700)

    with helper_log_path.open("a", encoding="utf-8") as helper_log_file:
        subprocess.Popen(
            [sys.executable, str(helper_script_path)],
            stdout=helper_log_file,
            stderr=helper_log_file,
            start_new_session=True,
        )

    append_worker_log(
        worker_log_path,
        (
            f"已启动延迟自销毁进程，延迟 {delay_seconds} 秒；"
            f"日志：{helper_log_path}"
        ),
    )


SELF_DESTROY_HELPER_TEMPLATE = '#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n"""SuanQi 服务器端自销毁辅助进程。"""\n\nimport hashlib\nimport hmac\nimport json\nimport pathlib\nimport time\nimport traceback\nimport urllib.error\nimport urllib.request\nfrom datetime import datetime, timezone\n\nCONFIG_PATH = pathlib.Path(__CONFIG_PATH__)\nWORKER_LOG_PATH = pathlib.Path(__WORKER_LOG_PATH__)\nSELF_LOG_PATH = pathlib.Path(__SELF_LOG_PATH__)\nDELAY_SECONDS = __DELAY_SECONDS__\n\nMETADATA_BASE_URL = "http://metadata.tencentyun.com/latest/meta-data"\n\n\ndef write_log(message):\n    timestamp = datetime.now(timezone.utc).isoformat()\n    line = f"[{timestamp}] {message}\\n"\n    for path in (SELF_LOG_PATH, WORKER_LOG_PATH):\n        try:\n            path.parent.mkdir(parents=True, exist_ok=True)\n            with path.open("a", encoding="utf-8") as f:\n                f.write(line)\n        except Exception:\n            pass\n\n\ndef read_json(path):\n    return json.loads(path.read_text(encoding="utf-8"))\n\n\ndef http_get_text(url, timeout_seconds=10):\n    request = urllib.request.Request(url, headers={"User-Agent": "suanqi-self-destroy/0.1.4"})\n    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:\n        return response.read().decode("utf-8").strip()\n\n\ndef metadata_get_text(path, timeout_seconds=10):\n    return http_get_text(f"{METADATA_BASE_URL}/{path.lstrip(\'/\')}", timeout_seconds=timeout_seconds)\n\n\ndef get_credentials(maximum_retry_count=8):\n    last_error = None\n    for attempt_number in range(maximum_retry_count):\n        try:\n            role_name = metadata_get_text("cam/security-credentials/").strip().splitlines()[0].strip()\n            if not role_name:\n                raise RuntimeError("实例没有返回 CAM 角色名称")\n            raw = metadata_get_text("cam/security-credentials/" + role_name)\n            data = json.loads(raw)\n            credentials = data.get("Credentials") if isinstance(data.get("Credentials"), dict) else data\n            if not isinstance(credentials, dict):\n                raise RuntimeError(f"临时密钥响应格式异常：{data}")\n            for key in ("TmpSecretId", "TmpSecretKey", "Token"):\n                if not credentials.get(key):\n                    raise RuntimeError(f"临时密钥缺少字段：{key}，响应：{data}")\n            write_log(f"已获取实例角色临时密钥，role={role_name}")\n            return credentials\n        except Exception as error:\n            last_error = error\n            wait_seconds = min(2 ** attempt_number, 10)\n            write_log(f"获取临时密钥失败，第 {attempt_number + 1} 次：{error}，{wait_seconds}s 后重试")\n            time.sleep(wait_seconds)\n    raise RuntimeError(f"无法获取实例角色临时密钥：{last_error}")\n\n\ndef sign_sha256(key, message):\n    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()\n\n\ndef build_authorization(secret_id, secret_key, service, payload, timestamp):\n    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")\n    canonical_request = "\\n".join([\n        "POST",\n        "/",\n        "",\n        "content-type:application/json; charset=utf-8\\n" + f"host:{service}.tencentcloudapi.com\\n",\n        "content-type;host",\n        hashlib.sha256(payload.encode("utf-8")).hexdigest(),\n    ])\n    credential_scope = f"{date}/{service}/tc3_request"\n    string_to_sign = "\\n".join([\n        "TC3-HMAC-SHA256",\n        str(timestamp),\n        credential_scope,\n        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),\n    ])\n    secret_date = sign_sha256(("TC3" + secret_key).encode("utf-8"), date)\n    secret_service = sign_sha256(secret_date, service)\n    secret_signing = sign_sha256(secret_service, "tc3_request")\n    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()\n    return (\n        "TC3-HMAC-SHA256 "\n        f"Credential={secret_id}/{credential_scope}, "\n        "SignedHeaders=content-type;host, "\n        f"Signature={signature}"\n    )\n\n\ndef call_api(service, action, version, region, payload_data, credentials, timeout_seconds=20):\n    payload = json.dumps(payload_data, separators=(",", ":"))\n    timestamp = int(time.time())\n    headers = {\n        "Authorization": build_authorization(\n            secret_id=str(credentials["TmpSecretId"]),\n            secret_key=str(credentials["TmpSecretKey"]),\n            service=service,\n            payload=payload,\n            timestamp=timestamp,\n        ),\n        "Content-Type": "application/json; charset=utf-8",\n        "Host": f"{service}.tencentcloudapi.com",\n        "X-TC-Action": action,\n        "X-TC-Version": version,\n        "X-TC-Timestamp": str(timestamp),\n        "X-TC-Region": region,\n        "X-TC-Token": str(credentials["Token"]),\n        "X-TC-Language": "zh-CN",\n    }\n    request = urllib.request.Request(\n        f"https://{service}.tencentcloudapi.com/",\n        data=payload.encode("utf-8"),\n        headers=headers,\n        method="POST",\n    )\n    try:\n        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:\n            response_text = response.read().decode("utf-8")\n    except urllib.error.HTTPError as error:\n        response_text = error.read().decode("utf-8", errors="replace")\n        raise RuntimeError(f"腾讯云 API HTTP {error.code}：{response_text}") from error\n    data = json.loads(response_text)\n    response_data = data.get("Response") or {}\n    if "Error" in response_data:\n        api_error = response_data["Error"]\n        raise RuntimeError(f"腾讯云 API 错误：{api_error.get(\'Code\')}，{api_error.get(\'Message\')}，RequestId={response_data.get(\'RequestId\')}")\n    return response_data\n\n\ndef main():\n    time.sleep(DELAY_SECONDS)\n    config = read_json(CONFIG_PATH)\n    if not bool(config.get("enable_self_destroy")):\n        write_log("自销毁跳过：配置未启用")\n        return 0\n\n    instance_id = str(config.get("instance_id") or "").strip()\n    region = str(config.get("instance_region") or "").strip()\n\n    if not instance_id:\n        instance_id = metadata_get_text("instance-id")\n    if not region:\n        for metadata_path in ("placement/region", "region"):\n            try:\n                region = metadata_get_text(metadata_path)\n                if region:\n                    break\n            except Exception as error:\n                write_log(f"读取地域 {metadata_path} 失败：{error}")\n\n    if not instance_id:\n        raise RuntimeError("无法确定当前实例 ID")\n    if not region:\n        raise RuntimeError("无法确定当前实例地域")\n\n    write_log(f"准备自销毁：region={region}，instance_id={instance_id}")\n    credentials = get_credentials()\n\n    try:\n        describe_response = call_api(\n            service="cvm",\n            action="DescribeInstances",\n            version="2017-03-12",\n            region=region,\n            payload_data={"InstanceIds": [instance_id]},\n            credentials=credentials,\n        )\n        total_count = describe_response.get("TotalCount")\n        write_log(f"DescribeInstances 成功：TotalCount={total_count}，RequestId={describe_response.get(\'RequestId\')}")\n    except Exception as error:\n        write_log(f"DescribeInstances 失败，但继续尝试 TerminateInstances：{error}")\n\n    terminate_response = call_api(\n        service="cvm",\n        action="TerminateInstances",\n        version="2017-03-12",\n        region=region,\n        payload_data={\n            "InstanceIds": [instance_id],\n            "ReleaseAddress": True,\n            "ReleasePrepaidDataDisks": False,\n        },\n        credentials=credentials,\n    )\n    write_log(f"实例自销毁请求已提交：{instance_id}，RequestId={terminate_response.get(\'RequestId\')}")\n    return 0\n\n\nif __name__ == "__main__":\n    try:\n        raise SystemExit(main())\n    except SystemExit:\n        raise\n    except Exception as error:\n        write_log(f"自销毁辅助进程异常：{error.__class__.__name__}：{error}")\n        try:\n            write_log(traceback.format_exc())\n        except Exception:\n            pass\n        raise SystemExit(1)\n'

def upload_task_artifacts(
    config: dict[str, Any],
    status_path: Path,
    task_log_path: Path,
    worker_log_path: Path,
    manifest_path: Path,
    user_directory: Path,
) -> None:
    if not _cos_target_enabled(config):
        return

    cos_target = config.get("cos_target") or {}
    region = str(cos_target.get("region") or "")
    bucket = str(cos_target.get("bucket") or "")
    prefix = _cos_target_upload_prefix(config)

    if not region or not bucket or not prefix:
        append_worker_log(worker_log_path, "COS 上传跳过：配置不完整")
        return

    try:
        from qcloud_cos import CosConfig, CosS3Client
    except Exception as error:
        append_worker_log(
            worker_log_path,
            f"COS SDK 未就绪，尝试自动安装：{error}",
        )
        try:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--break-system-packages",
                    "--disable-pip-version-check",
                    "-i",
                    "https://mirrors.cloud.tencent.com/pypi/simple",
                    "cos-python-sdk-v5",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=180,
                check=True,
            )
            from qcloud_cos import CosConfig, CosS3Client
        except Exception as install_error:
            append_worker_log(
                worker_log_path,
                (
                    "COS 上传失败：无法导入或安装 COS SDK "
                    f"{install_error}"
                ),
            )
            return

    artifact_root = user_directory.parent / "cos-artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)

    for source_path, target_name in (
        (status_path, "status.json"),
        (task_log_path, "task.log"),
        (worker_log_path, "worker.log"),
        (manifest_path, "manifest.json"),
    ):
        if source_path.is_file():
            (artifact_root / target_name).write_bytes(
                source_path.read_bytes()
            )

    task_root = artifact_root / "task"
    task_root.mkdir(parents=True, exist_ok=True)

    if user_directory.is_dir():
        for item in user_directory.iterdir():
            if item.name == ".venv":
                continue
            target = task_root / item.name
            if item.is_file():
                target.write_bytes(item.read_bytes())

    object_prefix = f"{prefix}/{config['task_id']}".strip("/")
    upload_ok = False
    last_error: BaseException | None = None

    for attempt_number in range(3):
        # attempt_number：当前上传尝试次数，从 0 开始
        try:
            credentials = get_instance_role_credentials(worker_log_path)
            cos_config = CosConfig(
                Region=region,
                SecretId=credentials["TmpSecretId"],
                SecretKey=credentials["TmpSecretKey"],
                Token=credentials["Token"],
                Scheme="https",
            )
            client = CosS3Client(cos_config)
            for file_path in artifact_root.rglob("*"):
                if not file_path.is_file():
                    continue
                relative_name = file_path.relative_to(artifact_root).as_posix()
                client.upload_file(
                    Bucket=bucket,
                    Key=f"{object_prefix}/{relative_name}",
                    LocalFilePath=str(file_path),
                )
            upload_ok = True
            break

        except Exception as error:
            last_error = error
            append_worker_log(
                worker_log_path,
                (
                    "COS 上传失败，"
                    f"第 {attempt_number + 1} 次："
                    f"{error.__class__.__name__}：{error}"
                ),
            )
            if attempt_number >= 2:
                break
            if is_temporary_credential_error(error):
                time.sleep(3)
            else:
                time.sleep(2 ** attempt_number)

    if upload_ok:
        append_worker_log(worker_log_path, "COS 上传完成")
    elif last_error is not None:
        append_worker_log(
            worker_log_path,
            (
                "COS 上传最终失败，但不会阻止实例自销毁："
                f"{last_error.__class__.__name__}：{last_error}"
            ),
        )


def graceful_upload_window(max_use_seconds: int) -> int:
    return max(30, min(300, max_use_seconds // 20))


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
        return (
            process.returncode,
            False,
        )

    try:
        process_group_id = os.getpgid(
            process.pid
        )

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
        return (
            process.poll(),
            False,
        )

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

        return (
            exit_code,
            False,
        )

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
        pass

    try:
        exit_code = process.wait(
            timeout=10
        )

    except subprocess.TimeoutExpired:
        exit_code = process.poll()

    append_worker_log(
        worker_log_path,
        (
            "用户程序已被强制结束，"
            f"退出码={exit_code}"
        ),
    )

    return (
        exit_code,
        True,
    )


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

    if timeout_seconds <= 0:
        raise TimeoutError(
            f"{description}没有剩余可用时间"
        )

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
            ).encode(
                "utf-8"
            )
        )

        task_log.flush()

        try:
            result = subprocess.run(
                full_command,
                cwd=str(
                    user_directory
                ),
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
            str(
                virtualenv_directory
            ),
        ],
        user_directory=user_directory,
        task_log_path=task_log_path,
        timeout_seconds=timeout_seconds,
        description=(
            "正在创建 Python 虚拟环境……"
        ),
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
    pip_index_url: str,
    pip_network_timeout_seconds: int,
    pip_retry_count: int,
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
            "未找到 requirements 文件："
            f"{requirements_path}"
        )

    run_preparation_command(
        command=[
            str(
                virtualenv_python
            ),
            "-m",
            "pip",
            "install",

            "--disable-pip-version-check",

            "--index-url",
            pip_index_url,

            "--timeout",
            str(
                pip_network_timeout_seconds
            ),

            "--retries",
            str(
                pip_retry_count
            ),

            "-r",
            str(
                requirements_path
            ),
        ],
        user_directory=user_directory,
        task_log_path=task_log_path,
        timeout_seconds=timeout_seconds,
        description=(
            "正在通过腾讯云镜像安装 "
            "requirements.txt……"
        ),
    )


def install_task_packages(
    virtualenv_python: Path,
    user_directory: Path,
    packages: list[str],
    task_log_path: Path,
    timeout_seconds: int,
    pip_index_url: str,
    pip_network_timeout_seconds: int,
    pip_retry_count: int,
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
            str(
                virtualenv_python
            ),
            "-m",
            "pip",
            "install",

            "--disable-pip-version-check",

            "--index-url",
            pip_index_url,

            "--timeout",
            str(
                pip_network_timeout_seconds
            ),

            "--retries",
            str(
                pip_retry_count
            ),

            *cleaned_packages,
        ],
        user_directory=user_directory,
        task_log_path=task_log_path,
        timeout_seconds=timeout_seconds,
        description=(
            "正在通过腾讯云镜像安装额外依赖："
            + ", ".join(
                cleaned_packages
            )
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
        datetime.now(
            timezone.utc
        )
    )

    pip_index_url = str(
        config.get(
            "pip_index_url",
            DEFAULT_PIP_INDEX_URL,
        )
    ).strip()

    pip_network_timeout_seconds = int(
        config.get(
            "pip_network_timeout_seconds",
            DEFAULT_PIP_NETWORK_TIMEOUT_SECONDS,
        )
    )

    pip_retry_count = int(
        config.get(
            "pip_retry_count",
            DEFAULT_PIP_RETRY_COUNT,
        )
    )

    if not pip_index_url:
        raise ValueError(
            "pip_index_url 不能为空"
        )

    if pip_network_timeout_seconds <= 0:
        raise ValueError(
            "pip_network_timeout_seconds 必须大于 0"
        )

    if pip_retry_count < 0:
        raise ValueError(
            "pip_retry_count 不能小于 0"
        )

    virtualenv_python = (
        create_task_virtualenv(
            user_directory=(
                user_directory
            ),
            task_log_path=(
                task_log_path
            ),
            timeout_seconds=(
                preparation_timeout_seconds
            ),
        )
    )

    elapsed_seconds = (
        datetime.now(
            timezone.utc
        )
        - preparation_started
    ).total_seconds()

    remaining_seconds = int(
        preparation_timeout_seconds
        - elapsed_seconds
    )

    if remaining_seconds <= 0:
        raise TimeoutError(
            "创建虚拟环境后已超过环境准备最大时间"
        )

    install_task_requirements(
        virtualenv_python=(
            virtualenv_python
        ),
        user_directory=(
            user_directory
        ),
        requirements_filename=(
            config.get(
                "requirements_filename"
            )
        ),
        task_log_path=(
            task_log_path
        ),
        timeout_seconds=(
            remaining_seconds
        ),
        pip_index_url=(
            pip_index_url
        ),
        pip_network_timeout_seconds=(
            pip_network_timeout_seconds
        ),
        pip_retry_count=(
            pip_retry_count
        ),
    )

    elapsed_seconds = (
        datetime.now(
            timezone.utc
        )
        - preparation_started
    ).total_seconds()

    remaining_seconds = int(
        preparation_timeout_seconds
        - elapsed_seconds
    )

    if remaining_seconds <= 0:
        raise TimeoutError(
            "安装 requirements.txt 后已超过"
            "环境准备最大时间"
        )

    install_task_packages(
        virtualenv_python=(
            virtualenv_python
        ),
        user_directory=(
            user_directory
        ),
        packages=(
            config.get("packages")
            or []
        ),
        task_log_path=(
            task_log_path
        ),
        timeout_seconds=(
            remaining_seconds
        ),
        pip_index_url=(
            pip_index_url
        ),
        pip_network_timeout_seconds=(
            pip_network_timeout_seconds
        ),
        pip_retry_count=(
            pip_retry_count
        ),
    )

    total_elapsed_seconds = (
        datetime.now(
            timezone.utc
        )
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

    preparation_started_at: str | None = None

    preparation_finished_at: str | None = None

    program_started_at: str | None = None

    max_use_seconds = (
        DEFAULT_MAX_USE_SECONDS
    )

    terminate_grace_seconds = (
        DEFAULT_TERMINATE_GRACE_SECONDS
    )

    preparation_timeout_seconds = (
        DEFAULT_PREPARATION_TIMEOUT_SECONDS
    )

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

        pip_index_url = str(
            config.get(
                "pip_index_url",
                DEFAULT_PIP_INDEX_URL,
            )
        ).strip()

        pip_network_timeout_seconds = int(
            config.get(
                "pip_network_timeout_seconds",
                DEFAULT_PIP_NETWORK_TIMEOUT_SECONDS,
            )
        )

        pip_retry_count = int(
            config.get(
                "pip_retry_count",
                DEFAULT_PIP_RETRY_COUNT,
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

        if not pip_index_url:
            raise ValueError(
                "pip_index_url 不能为空"
            )

        if pip_network_timeout_seconds <= 0:
            raise ValueError(
                "pip_network_timeout_seconds 必须大于 0"
            )

        if pip_retry_count < 0:
            raise ValueError(
                "pip_retry_count 不能小于 0"
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
                f"{terminate_grace_seconds} 秒，"
                "pip 镜像："
                f"{pip_index_url}，"
                "pip 网络超时："
                f"{pip_network_timeout_seconds} 秒，"
                "pip 重试次数："
                f"{pip_retry_count}"
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
                worker_pid=(
                    os.getpid()
                ),
                preparation_timeout_seconds=(
                    preparation_timeout_seconds
                ),
                max_use_seconds=(
                    max_use_seconds
                ),
                terminate_grace_seconds=(
                    terminate_grace_seconds
                ),
                pip_index_url=(
                    pip_index_url
                ),
                message=(
                    "守护进程正在启动"
                ),
            ),
        )

        preparation_started_at = (
            utc_now()
        )

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
                worker_pid=(
                    os.getpid()
                ),
                preparation_timeout_seconds=(
                    preparation_timeout_seconds
                ),
                max_use_seconds=(
                    max_use_seconds
                ),
                pip_index_url=(
                    pip_index_url
                ),
                message=(
                    "正在准备 Python 运行环境"
                ),
            ),
        )

        append_worker_log(
            worker_log_path,
            "开始准备 Python 运行环境",
        )

        virtualenv_python = (
            prepare_task_environment(
                config=config,
                user_directory=(
                    user_directory
                ),
                task_log_path=(
                    task_log_path
                ),
                preparation_timeout_seconds=(
                    preparation_timeout_seconds
                ),
            )
        )

        preparation_finished_at = (
            utc_now()
        )

        append_worker_log(
            worker_log_path,
            (
                "Python 运行环境准备完成，"
                f"解释器={virtualenv_python}"
            ),
        )

        main_filename = str(
            config.get(
                "main_filename",
                "main.py",
            )
        )

        main_path = (
            user_directory
            / main_filename
        ).resolve()

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
            str(
                virtualenv_python
            ),
            "-u",
            str(
                main_path
            ),
        ]

        environment = (
            os.environ.copy()
        )

        cpu_count = (
            os.cpu_count()
            or 1
        )

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

        timed_out = False

        forced_kill = False

        exit_code: int | None = None

        with task_log_path.open(
            "ab",
            buffering=0,
        ) as task_log:
            process = subprocess.Popen(
                command,
                cwd=str(
                    user_directory
                ),
                stdin=subprocess.DEVNULL,
                stdout=task_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=environment,
            )

            program_started_at = (
                utc_now()
            )

            pid_path.write_text(
                str(
                    process.pid
                ),
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
                    worker_pid=(
                        os.getpid()
                    ),
                    main_pid=(
                        process.pid
                    ),
                    max_use_seconds=(
                        max_use_seconds
                    ),
                    message=(
                        "用户程序正在运行"
                    ),
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
                exit_code = process.wait(
                    timeout=(
                        max_use_seconds
                    )
                )

            except subprocess.TimeoutExpired:
                timed_out = True

                timeout_at = utc_now()

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
                        timeout_at=(
                            timeout_at
                        ),
                        worker_pid=(
                            os.getpid()
                        ),
                        main_pid=(
                            process.pid
                        ),
                        max_use_seconds=(
                            max_use_seconds
                        ),
                        message=(
                            "任务达到最大运行时间，"
                            "正在停止用户程序"
                        ),
                    ),
                )

                (
                    exit_code,
                    forced_kill,
                ) = stop_process_group(
                    process=process,
                    grace_seconds=(
                        max(
                            terminate_grace_seconds,
                            graceful_upload_window(
                                max_use_seconds
                            ),
                        )
                    ),
                    worker_log_path=(
                        worker_log_path
                    ),
                )

        finished_at = utc_now()

        if timed_out:
            final_status = (
                "TIMEOUT"
            )

        elif exit_code == 0:
            final_status = (
                "SUCCESS"
            )

        else:
            final_status = (
                "FAILED"
            )

        (
            files,
            missing_files,
        ) = collect_return_files(
            config=config,
            user_directory=(
                user_directory
            ),
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
                finished_at=(
                    finished_at
                ),
                worker_pid=(
                    os.getpid()
                ),
                main_pid=(
                    process.pid
                    if process is not None
                    else None
                ),
                exit_code=(
                    exit_code
                ),
                max_use_seconds=(
                    max_use_seconds
                ),
                preparation_timeout_seconds=(
                    preparation_timeout_seconds
                ),
                terminate_grace_seconds=(
                    terminate_grace_seconds
                ),
                timed_out=(
                    timed_out
                ),
                forced_kill=(
                    forced_kill
                ),
                message=(
                    final_message
                ),
            ),
        )

        atomic_write_json(
            manifest_path,
            {
                "task_id": (
                    config["task_id"]
                ),
                "completed": True,
                "status": (
                    final_status
                ),
                "program_exit_code": (
                    exit_code
                ),

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

                "pip_index_url": (
                    pip_index_url
                ),

                "pip_network_timeout_seconds": (
                    pip_network_timeout_seconds
                ),

                "pip_retry_count": (
                    pip_retry_count
                ),

                "timed_out": (
                    timed_out
                ),

                "forced_kill": (
                    forced_kill
                ),

                "files": files,

                "missing_files": (
                    missing_files
                ),
            },
        )

        append_worker_log(
            worker_log_path,
            final_message,
        )

        upload_task_artifacts(
            config=config,
            status_path=status_path,
            task_log_path=task_log_path,
            worker_log_path=worker_log_path,
            manifest_path=manifest_path,
            user_directory=user_directory,
        )

        # v0.1.4 修复：不要再依赖后台 helper 进程。
        # 某些 SSH/远程执行环境会在 worker 退出时清理同一会话/进程组，
        # 导致 self_destroy.py 根本没有机会运行，self_destroy.log 为空。
        # 因此在 worker 退出前同步提交 TerminateInstances 请求。
        terminate_current_instance(
            config=config,
            worker_log_path=worker_log_path,
        )

        if final_status == "SUCCESS":
            return 0

        if final_status == "TIMEOUT":
            return 124

        return 1

    except BaseException as error:
        if process is not None:
            try:
                stop_process_group(
                    process=process,
                    grace_seconds=(
                        terminate_grace_seconds
                    ),
                    worker_log_path=(
                        worker_log_path
                        if worker_log_path
                        is not None
                        else Path(
                            "/tmp/suanqi-worker.log"
                        )
                    ),
                )
            except Exception:
                pass

        failed_at = utc_now()

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
                        failed_at=(
                            failed_at
                        ),
                        worker_pid=(
                            os.getpid()
                        ),
                        main_pid=(
                            process.pid
                            if process is not None
                            else None
                        ),
                        error_type=(
                            error.__class__.__name__
                        ),
                        error_message=(
                            str(error)
                        ),
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

        if (
            worker_log_path is not None
            and config
        ):
            try:
                if (
                    status_path is not None
                    and task_log_path is not None
                    and manifest_path is not None
                    and user_directory is not None
                ):
                    upload_task_artifacts(
                        config=config,
                        status_path=status_path,
                        task_log_path=task_log_path,
                        worker_log_path=worker_log_path,
                        manifest_path=manifest_path,
                        user_directory=user_directory,
                    )
                terminate_current_instance(
                    config=config,
                    worker_log_path=worker_log_path,
                )
            except Exception:
                pass

        return 1


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
