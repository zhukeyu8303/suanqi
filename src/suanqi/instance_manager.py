# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .gateway.tencent_gateway import (
    GatewayResult,
    TencentCloudGateway,
)


DEFAULT_MANAGED_REGIONS = [
    "ap-beijing",
    "ap-chengdu",
    "ap-chongqing",
    "ap-guangzhou",
    "ap-hongkong",
    "ap-nanjing",
    "ap-shanghai",
]

SUANQI_INSTANCE_NAME_PREFIX = "suanqi-"


@dataclass(slots=True)
class ManagedInstance:
    """SuanQi 管理的腾讯云实例。"""

    instance_id: str
    instance_name: str
    state: str
    region: str
    zone: str | None
    instance_type: str | None
    public_ip: str | None
    private_ip: str | None
    charge_type: str | None
    created_time: str | None
    raw: dict[str, Any]


def _normalize_regions(
    regions: Iterable[str] | None,
) -> list[str]:
    """整理地域列表。"""

    source = (
        list(regions)
        if regions is not None
        else DEFAULT_MANAGED_REGIONS
    )

    result: list[str] = []
    seen: set[str] = set()

    for region in source:
        region = region.strip()

        if not region or region in seen:
            continue

        seen.add(region)
        result.append(region)

    return result


def _convert_instance(
    instance: dict[str, Any],
) -> ManagedInstance:
    """转换网关实例数据。"""

    public_ips = instance.get("public_ips") or []
    private_ips = instance.get("private_ips") or []

    return ManagedInstance(
        instance_id=str(
            instance.get("instance_id") or ""
        ),
        instance_name=str(
            instance.get("instance_name") or ""
        ),
        state=str(
            instance.get("state") or "UNKNOWN"
        ),
        region=str(
            instance.get("region") or ""
        ),
        zone=instance.get("zone"),
        instance_type=instance.get("instance_type"),
        public_ip=(
            str(public_ips[0])
            if public_ips
            else None
        ),
        private_ip=(
            str(private_ips[0])
            if private_ips
            else None
        ),
        charge_type=instance.get("charge_type"),
        created_time=instance.get("created_time"),
        raw=instance,
    )


def list_suanqi_instances(
    gateway: TencentCloudGateway,
    regions: Iterable[str] | None = None,
    instance_name_prefix: str = (
        SUANQI_INSTANCE_NAME_PREFIX
    ),
) -> GatewayResult:
    """跨地域查询 SuanQi 实例。"""

    managed: list[ManagedInstance] = []
    failed_regions: list[dict[str, str]] = []

    for region in _normalize_regions(regions):
        offset = 0
        limit = 100

        while True:
            result = gateway.describe_instances(
                region=region,
                limit=limit,
                offset=offset,
            )

            if not result.success:
                failed_regions.append(
                    {
                        "region": region,
                        "error_code": (
                            result.error_code
                            or "UnknownError"
                        ),
                        "error_message": (
                            result.error_message
                            or "未知错误"
                        ),
                    }
                )
                break

            instances = (
                result.data.get("instances")
                or []
            )

            for instance in instances:
                name = str(
                    instance.get("instance_name")
                    or ""
                )

                if not name.startswith(
                    instance_name_prefix
                ):
                    continue

                managed.append(
                    _convert_instance(instance)
                )

            total_count = int(
                result.data.get("total_count")
                or 0
            )

            offset += len(instances)

            if (
                not instances
                or offset >= total_count
            ):
                break

    managed.sort(
        key=lambda item: (
            item.region,
            item.created_time or "",
            item.instance_id,
        )
    )

    return GatewayResult(
        success=True,
        action="ListSuanQiInstances",
        data={
            "total_count": len(managed),
            "instances": managed,
            "failed_regions": failed_regions,
        },
    )


def find_instance_across_regions(
    gateway: TencentCloudGateway,
    instance_id: str,
    regions: Iterable[str] | None = None,
) -> GatewayResult:
    """跨地域查找指定实例。"""

    for region in _normalize_regions(regions):
        result = gateway.describe_instances(
            region=region,
            instance_ids=[instance_id],
            limit=100,
            offset=0,
        )

        if not result.success:
            continue

        instances = (
            result.data.get("instances")
            or []
        )

        if instances:
            return GatewayResult(
                success=True,
                action="FindInstanceAcrossRegions",
                data={
                    "found": True,
                    "instance": _convert_instance(
                        instances[0]
                    ),
                },
                request_id=result.request_id,
            )

    return GatewayResult(
        success=True,
        action="FindInstanceAcrossRegions",
        data={
            "found": False,
            "instance": None,
        },
    )


def release_suanqi_instance(
    gateway: TencentCloudGateway,
    instance_id: str,
    regions: Iterable[str] | None = None,
    require_suanqi_managed: bool = True,
) -> GatewayResult:
    """查找并释放指定实例。"""

    find_result = find_instance_across_regions(
        gateway,
        instance_id,
        regions,
    )

    if not find_result.success:
        return find_result

    if not find_result.data["found"]:
        return GatewayResult(
            success=False,
            action="ReleaseSuanQiInstance",
            error_code="InstanceNotFound",
            error_message="没有找到指定实例。",
        )

    instance: ManagedInstance = (
        find_result.data["instance"]
    )

    if (
        require_suanqi_managed
        and not instance.instance_name.startswith(
            SUANQI_INSTANCE_NAME_PREFIX
        )
    ):
        return GatewayResult(
            success=False,
            action="ReleaseSuanQiInstance",
            error_code="NotSuanQiManaged",
            error_message=(
                "拒绝释放：该实例不是 SuanQi 管理的实例。"
            ),
        )

    result = gateway.terminate_instances(
        region=instance.region,
        instance_ids=[instance.instance_id],
        release_address=True,
        release_prepaid_data_disks=False,
    )

    if not result.success:
        return GatewayResult(
            success=False,
            action="ReleaseSuanQiInstance",
            error_code=result.error_code,
            error_message=result.error_message,
            request_id=result.request_id,
        )

    return GatewayResult(
        success=True,
        action="ReleaseSuanQiInstance",
        data={
            "instance": instance,
            "termination_requested": True,
        },
        request_id=result.request_id,
    )


def _display_width(text: str) -> int:
    """计算终端显示宽度。"""

    return sum(
        2 if ord(character) > 127 else 1
        for character in text
    )


def _pad_text(
    text: str,
    width: int,
) -> str:
    """按显示宽度补齐。"""

    return text + " " * max(
        0,
        width - _display_width(text),
    )


def format_instance_table(
    instances: list[ManagedInstance],
) -> str:
    """格式化实例表格。"""

    if not instances:
        return "没有找到由 SuanQi 管理的实例。"

    headers = [
        "序号",
        "实例ID",
        "名称",
        "状态",
        "地域",
        "可用区",
        "机型",
        "公网IP",
        "计费方式",
        "创建时间",
    ]

    rows: list[list[str]] = []

    for index, instance in enumerate(
        instances,
        1,
    ):
        rows.append(
            [
                str(index),
                instance.instance_id,
                instance.instance_name,
                instance.state,
                instance.region,
                instance.zone or "-",
                instance.instance_type or "-",
                instance.public_ip or "-",
                instance.charge_type or "-",
                instance.created_time or "-",
            ]
        )

    widths = []

    for column_index, header in enumerate(headers):
        width = _display_width(header)

        for row in rows:
            width = max(
                width,
                _display_width(
                    row[column_index]
                ),
            )

        widths.append(width)

    lines = [
        "  ".join(
            _pad_text(
                header,
                widths[index],
            )
            for index, header in enumerate(headers)
        ),
        "  ".join(
            "-" * width
            for width in widths
        ),
    ]

    for row in rows:
        lines.append(
            "  ".join(
                _pad_text(
                    value,
                    widths[index],
                )
                for index, value in enumerate(row)
            )
        )

    return "\n".join(lines)
