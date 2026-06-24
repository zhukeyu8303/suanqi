# -*- coding: utf-8 -*-

import random
import threading
import time
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
)
from typing import Any

from datetime import datetime, timezone

from suanqi.gateway import (
    InstanceConfig,
    TencentCloudGateway,
)


# ============================================================
# 1. 用户配置
# ============================================================




MAINLAND_CHINA_REGIONS = {
    "ap-beijing",
    "ap-chengdu",
    "ap-chongqing",
    "ap-guangzhou",
    "ap-nanjing",
    "ap-shanghai",
}

HONG_KONG_REGIONS = {
    "ap-hongkong",
}

EAST_CHINA = {"ap-shanghai","ap-nanjing",}
SOUCH_CHINA = {"ap-guangzhou",}
NORTH_CHINA = {"ap-beijing",}
SOUTHWEST_CHINA = {"ap-chongqing","ap-chengdu",}

allowed_regions = EAST_CHINA

INSTANCE_CHARGE_TYPE = "SPOTPAID"
# 实例计费方式：竞价实例

SYSTEM_DISK_TYPE = "CLOUD_BSSD"
# 系统盘类型：通用型 SSD 云硬盘

SYSTEM_DISK_SIZE_GB = 30
# Linux 系统盘大小，单位 GiB

PUBLIC_IP_ASSIGNED = True
# 是否分配公网 IP

INTERNET_CHARGE_TYPE = "TRAFFIC_POSTPAID_BY_HOUR"
# 公网流量按量计费

INTERNET_MAX_BANDWIDTH_MBPS = 10
# 公网出带宽上限，单位 Mbps

INSTANCE_COUNT = 1
# 每次创建的实例数量

INSTANCE_NAME = "suanqi-task"
# 实例名称

SPOT_MAX_PRICE = "10.000"
# 竞价实例最高愿意支付的价格



#=============================================
def _select_candidate(
    candidates: list[dict],
) -> dict | None:
    """
    让用户从候选实例列表中选择一个配置。

    返回：
        选择成功时返回候选配置字典；
        用户取消时返回 None。
    """

    while True:
        user_input = input(
            "\n请输入要创建的实例序号，"
            "输入 q 取消："
        ).strip()

        if user_input.lower() in {
            "q",
            "quit",
            "exit",
        }:
            return None

        try:
            selected_index = int(user_input)
        except ValueError:
            print("输入错误，请输入实例序号。")
            continue

        if not 1 <= selected_index <= len(candidates):
            print(
                f"序号超出范围，请输入 "
                f"1～{len(candidates)}。"
            )
            continue

        return candidates[selected_index - 1]

def _build_instance_config(
    candidate: dict,
    instance_name: str = INSTANCE_NAME,
) -> InstanceConfig:
    """
    根据候选实例生成统一的 InstanceConfig。

    candidate：
        已经包含地域、可用区、机型、镜像、
        VPC、子网和安全组信息的候选配置。
    """

    return InstanceConfig(
        region=candidate["region_code"],
        zone=candidate["zone_code"],

        instance_type=candidate["instance_type"],
        image_id=candidate["image_id"],

        vpc_id=candidate["vpc_id"],
        subnet_id=candidate["subnet_id"],

        security_group_ids=[
            candidate["security_group_id"]
        ],

        charge_type=INSTANCE_CHARGE_TYPE,

        system_disk_type=SYSTEM_DISK_TYPE,
        system_disk_size_gb=SYSTEM_DISK_SIZE_GB,

        public_ip_assigned=PUBLIC_IP_ASSIGNED,

        internet_charge_type=INTERNET_CHARGE_TYPE,

        internet_max_bandwidth_out_mbps=(
            INTERNET_MAX_BANDWIDTH_MBPS
        ),

        instance_count=INSTANCE_COUNT,
        instance_name=instance_name,

        spot_max_price=SPOT_MAX_PRICE,

        password=None,
    )

#==============================================
def tencentcloud_creat(
    minimum_cpu=16,
    minimum_memory_gb = 16,
    maximum_region_instances = 10,
    image_platform = "Ubuntu",
    image_name_keyword = "24.04",
    maximum_workers = 4,
    maximum_requests_per_second = 3,
    maximum_retry_count = 4,
) -> dict[Any, Any]:
    """执行腾讯云实例筛选、资源准备和并发询价。

    minimum_cpu:
        实例最低 CPU 核心数

    minimum_memory_gb:
        实例最低内存，单位 GB

    maximum_region_instances:
        每个地域最多保留的机型数量

    image_platform:
        镜像平台

    image_name_keyword:
        镜像名称关键字

    maximum_workers:
        最大并发线程数量

    maximum_requests_per_second:
        全局每秒最多发起询价请求次数

    maximum_retry_count:
        限频或暂时性错误时，最多尝试次数
    """

    gateway = TencentCloudGateway()
    # ========================================================
    # 2. 查询账户余额
    # ========================================================

    balance_result = gateway.get_account_balance()

    if balance_result.success:
        print(
            f"用户 UIN："
            f"{balance_result.data['uin']}"
        )

        print(
            f"用户余额："
            f"{balance_result.data['real_balance_yuan']} 元"
        )
    else:
        print(
            "查询账户余额失败："
            f"{balance_result.error_code}，"
            f"{balance_result.error_message}"
        )

    # ========================================================
    # 3. 查询 CVM 地域
    # ========================================================

    regions_result = gateway.list_regions(
        product="cvm",
        only_available=True,
    )

    if not regions_result.success:
        print(
            "查询 CVM 地域失败："
            f"{regions_result.error_code}，"
            f"{regions_result.error_message}"
        )
        return {}

    region_list = [
        {
            "region_code": item["region"],
            "region_name": item["name"],
        }
        for item in regions_result.data["regions"]
        if item["region"] in allowed_regions
    ]

    if not region_list:
        print("允许的地域中没有可用地域。")
        return {}

    print("\n本次查询地域：")

    for item in region_list:
        print(
            f"- {item['region_name']} "
            f"({item['region_code']})"
        )

    # ========================================================
    # 4. 查询地域级实例机型
    # ========================================================

    candidate_instance_types = []

    for region_info in region_list:
        region_code = region_info["region_code"]
        region_name = region_info["region_name"]

        print(
            f"\n正在查询地域机型："
            f"{region_name} ({region_code})"
        )

        instance_types_result = (
            gateway.list_instance_types(
                region=region_code,
                minimum_cpu=minimum_cpu,
                minimum_memory_gb=minimum_memory_gb,
            )
        )

        if not instance_types_result.success:
            print(
                f"{region_code} 查询机型失败："
                f"{instance_types_result.error_code}，"
                f"{instance_types_result.error_message}"
            )
            continue

        all_instance_types = (
            instance_types_result.data[
                "instance_types"
            ]
        )

        selected_instance_types = (
            all_instance_types[
                :maximum_region_instances
            ]
        )

        for instance_info in selected_instance_types:
            candidate_instance_types.append(
                {
                    "region_code": region_code,
                    "region_name": region_name,

                    "instance_type": (
                        instance_info["instance_type"]
                    ),

                    "cpu": instance_info["cpu"],

                    "memory_gb": (
                        instance_info["memory_gb"]
                    ),

                    "gpu": instance_info.get(
                        "gpu",
                        0,
                    ),

                    "instance_family": (
                        instance_info.get(
                            "instance_family"
                        )
                    ),
                }
            )

    if not candidate_instance_types:
        print("没有符合要求的机型。")
        return {}

    print(
        f"\n地域级候选机型数量："
        f"{len(candidate_instance_types)}"
    )

    for index, item in enumerate(
        candidate_instance_types,
        1,
    ):
        print(
            f"{index}. "
            f"{item['region_name']} / "
            f"{item['instance_type']} / "
            f"{item['cpu']} 核 / "
            f"{item['memory_gb']} GB"
        )

    # ========================================================
    # 5. 查询目标镜像
    # ========================================================

    valid_region_images = {}

    candidate_regions = {
        item["region_code"]
        for item in candidate_instance_types
    }

    for region_code in candidate_regions:
        images_result = gateway.list_images(
            region=region_code,
            image_type="PUBLIC_IMAGE",
            platform=image_platform,
            image_name_keyword=image_name_keyword,
        )

        if not images_result.success:
            print(
                f"{region_code} 查询镜像失败："
                f"{images_result.error_code}，"
                f"{images_result.error_message}"
            )
            continue

        images = [
            image
            for image in images_result.data["images"]
            if image.get("architecture") == "x86_64"
        ]

        if not images:
            print(
                f"{region_code} 没有符合要求的镜像。"
            )
            continue

        selected_image = images[0]

        valid_region_images[region_code] = {
            "image_id": (
                selected_image["image_id"]
            ),

            "image_name": (
                selected_image["image_name"]
            ),

            "image_platform": (
                selected_image["platform"]
            ),

            "image_architecture": (
                selected_image["architecture"]
            ),
        }

        print(
            f"{region_code} 使用镜像："
            f"{selected_image['image_name']} "
            f"({selected_image['image_id']})"
        )

    # 删除没有目标镜像的地域机型
    candidate_instance_types = [
        item
        for item in candidate_instance_types
        if item["region_code"]
        in valid_region_images
    ]

    # 给每个候选机型附加镜像信息
    for item in candidate_instance_types:
        item.update(
            valid_region_images[
                item["region_code"]
            ]
        )

    if not candidate_instance_types:
        print("没有通过镜像检查的候选机型。")
        return {}

    # ========================================================
    # 6. 查询可用区
    # ========================================================

    region_zones = {}

    candidate_regions = {
        item["region_code"]
        for item in candidate_instance_types
    }

    for region_code in candidate_regions:
        zones_result = gateway.list_zones(
            region_code,
            only_available=True,
        )

        if not zones_result.success:
            print(
                f"{region_code} 查询可用区失败："
                f"{zones_result.error_code}，"
                f"{zones_result.error_message}"
            )
            continue

        zones = zones_result.data["zones"]

        if not zones:
            print(
                f"{region_code} 没有可用区。"
            )
            continue

        region_zones[region_code] = zones

    # ========================================================
    # 7. 将机型和可用区组合
    # ========================================================

    candidate_zone_instances = []

    for item in candidate_instance_types:
        zones = region_zones.get(
            item["region_code"],
            [],
        )

        for zone in zones:
            candidate_zone_instances.append(
                {
                    **item,
                    "zone_code": zone["zone"],
                    "zone_name": zone["name"],
                }
            )

    if not candidate_zone_instances:
        print("没有生成可用区级候选配置。")
        return {}

    print(
        f"\n可用区级候选配置数量："
        f"{len(candidate_zone_instances)}"
    )

    # ========================================================
    # 8. 多线程检查机型是否可售
    # ========================================================

    availability_thread_local_data = threading.local()
    # 每个检查线程独立保存自己的 TencentCloudGateway

    availability_rate_limit_lock = threading.Lock()
    # 多个检查线程共用的限速锁

    availability_request_interval = (
        1.0 / maximum_requests_per_second
    )
    # 两次可售状态请求开始时间之间的最小间隔

    availability_last_request_time = 0.0
    # 上一次开始可售状态请求的时间

    def get_availability_thread_gateway(
    ) -> TencentCloudGateway:
        """
        为每个可售检查线程创建独立网关。

        同一线程后续任务会复用自己的网关，
        不会与其他线程共享腾讯云 SDK Client。
        """

        if not hasattr(
            availability_thread_local_data,
            "gateway",
        ):
            availability_thread_local_data.gateway = (
                TencentCloudGateway()
            )

        return availability_thread_local_data.gateway

    def wait_for_availability_request_slot() -> None:
        """
        全局限制可售状态检查请求开始频率。

        即使设置了多个线程，所有线程仍然必须
        按照 maximum_requests_per_second 限速。
        """

        nonlocal availability_last_request_time

        with availability_rate_limit_lock:
            current_time = time.monotonic()

            elapsed_time = (
                current_time
                - availability_last_request_time
            )

            wait_time = (
                availability_request_interval
                - elapsed_time
            )

            if wait_time > 0:
                time.sleep(wait_time)

            availability_last_request_time = (
                time.monotonic()
            )

    def check_candidate_availability(
        candidate: dict,
    ) -> dict:
        """
        检查一个候选实例是否处于可售状态。
        """

        thread_gateway = (
            get_availability_thread_gateway()
        )

        last_availability_result = None

        for retry_index in range(
            maximum_retry_count
        ):
            wait_for_availability_request_slot()

            availability_result = (
                thread_gateway
                .check_instance_available(
                    region=candidate[
                        "region_code"
                    ],
                    zone=candidate[
                        "zone_code"
                    ],
                    instance_type=candidate[
                        "instance_type"
                    ],
                    charge_type=(
                        INSTANCE_CHARGE_TYPE
                    ),
                )
            )

            last_availability_result = (
                availability_result
            )

            if availability_result.success:
                availability_data = (
                    availability_result.data
                    or {}
                )

                available = bool(
                    availability_data.get(
                        "available",
                        False,
                    )
                )

                status = (
                    availability_data.get(
                        "status"
                    )
                    or "UNKNOWN"
                )

                return {
                    "success": True,
                    "available": available,
                    "status": status,
                    "candidate": candidate,
                    "availability_data": (
                        availability_data
                    ),
                }

            error_code = (
                availability_result.error_code
                or ""
            )

            error_message = (
                availability_result.error_message
                or ""
            )

            normalized_error = (
                f"{error_code} "
                f"{error_message}"
            ).lower()

            retryable_error = any(
                keyword in normalized_error
                for keyword in (
                    "requestlimitexceeded",
                    "request limit exceeded",
                    "每秒请求",
                    "频率上限",
                    "频率",
                    "限频",
                    "internalerror",
                    "internal error",
                    "serviceunavailable",
                    "service unavailable",
                    "temporarily unavailable",
                    "暂时不可用",
                    "network",
                    "timeout",
                    "超时",
                )
            )

            if not retryable_error:
                return {
                    "success": False,
                    "available": False,
                    "status": "CHECK_FAILED",
                    "candidate": candidate,
                    "error_code": error_code,
                    "error_message": error_message,
                    "retry_count": retry_index,
                }

            if retry_index >= (
                maximum_retry_count - 1
            ):
                break

            retry_wait_seconds = (
                2 ** retry_index
                + random.uniform(0.3, 0.9)
            )

            time.sleep(retry_wait_seconds)

        return {
            "success": False,
            "available": False,
            "status": "CHECK_FAILED",
            "candidate": candidate,

            "error_code": (
                last_availability_result.error_code
                if last_availability_result
                else "UnknownError"
            ),

            "error_message": (
                last_availability_result.error_message
                if last_availability_result
                else "可售状态请求未返回结果"
            ),

            "retry_count": (
                maximum_retry_count
            ),
        }

    available_zone_instances = []
    # 只保存当前处于可售状态的候选配置

    print(
        f"\n开始检查 "
        f"{len(candidate_zone_instances)} "
        f"个候选配置的可售状态。"
    )

    with ThreadPoolExecutor(
        max_workers=maximum_workers
    ) as executor:

        future_to_candidate = {
            executor.submit(
                check_candidate_availability,
                candidate,
            ): candidate
            for candidate in candidate_zone_instances
        }

        completed_count = 0
        total_count = len(
            future_to_candidate
        )

        for future in as_completed(
            future_to_candidate
        ):
            candidate = (
                future_to_candidate[future]
            )

            completed_count += 1

            try:
                result = future.result()

            except Exception as error:
                print(
                    f"\n可售检查任务异常 "
                    f"[{completed_count}/{total_count}]："
                    f"{candidate['region_name']} / "
                    f"{candidate['zone_name']} / "
                    f"{candidate['instance_type']} / "
                    f"{error}"
                )
                continue

            if not result["success"]:
                print(
                    f"\n可售检查失败 "
                    f"[{completed_count}/{total_count}]："
                    f"{candidate['region_name']} / "
                    f"{candidate['zone_name']} / "
                    f"{candidate['instance_type']} / "
                    f"{result['error_code']}，"
                    f"{result['error_message']}"
                )
                continue

            if not result["available"]:
                print(
                    f"\n当前不可售 "
                    f"[{completed_count}/{total_count}]："
                    f"{candidate['region_name']} / "
                    f"{candidate['zone_name']} / "
                    f"{candidate['instance_type']} / "
                    f"状态：{result['status']}"
                )
                continue

            available_zone_instances.append(
                {
                    **candidate,
                    "sale_status": (
                        result["status"]
                    ),
                }
            )

            print(
                f"\n当前可售 "
                f"[{completed_count}/{total_count}]："
                f"{candidate['region_name']} / "
                f"{candidate['zone_name']} / "
                f"{candidate['instance_type']} / "
                f"状态：{result['status']}"
            )

    if not available_zone_instances:
        print(
            "\n没有通过可售状态检查的候选实例。"
        )
        return {}

    print(
        f"\n可售状态检查结束，通过 "
        f"{len(available_zone_instances)} / "
        f"{len(candidate_zone_instances)} 个。"
    )

    # ========================================================
    # 9. 单线程准备网络和安全组
    # ========================================================

    network_cache = {}
    # key：
    # (region_code, zone_code)
    #
    # value：
    # 当前可用区的 VPC 和子网信息

    security_group_cache = {}
    # key：
    # region_code
    #
    # value：
    # 当前地域的安全组 ID

    prepared_candidates = []
    # 已补充 VPC、子网和安全组的候选配置

    for current_index, candidate in enumerate(
        available_zone_instances,
        1,
    ):
        region_code = candidate["region_code"]
        zone_code = candidate["zone_code"]

        print(
            f"\n正在准备配置 "
            f"[{current_index}/"
            f"{len(available_zone_instances)}]："
            f"{candidate['region_name']} / "
            f"{candidate['zone_name']} / "
            f"{candidate['instance_type']}"
        )

        # ----------------------------------------------------
        # 9.1 获取当前可用区网络
        # ----------------------------------------------------

        network_key = (
            region_code,
            zone_code,
        )

        if network_key not in network_cache:
            network_result = (
                gateway.resolve_network_for_zone(
                    region=region_code,
                    zone=zone_code,
                    create_subnet_if_missing=True,
                    subnet_prefix=24,
                )
            )

            if not network_result.success:
                print(
                    "解析网络失败："
                    f"{network_result.error_code}，"
                    f"{network_result.error_message}"
                )

                network_cache[network_key] = None

            else:
                network_cache[network_key] = {
                    "vpc_id": (
                        network_result.data[
                            "vpc"
                        ]["vpc_id"]
                    ),

                    "subnet_id": (
                        network_result.data[
                            "subnet"
                        ]["subnet_id"]
                    ),

                    "subnet_name": (
                        network_result.data[
                            "subnet"
                        ].get("subnet_name")
                    ),

                    "subnet_cidr": (
                        network_result.data[
                            "subnet"
                        ].get("cidr_block")
                    ),

                    "subnet_created": (
                        network_result.data.get(
                            "subnet_created",
                            False,
                        )
                    ),
                }

                if network_cache[
                    network_key
                ]["subnet_created"]:
                    print(
                        "已创建 SuanQi 专属子网："
                        f"{network_cache[network_key]['subnet_name']} / "
                        f"{network_cache[network_key]['subnet_cidr']}"
                    )

        network_info = network_cache[
            network_key
        ]

        if network_info is None:
            continue

        # ----------------------------------------------------
        # 9.2 获取或创建安全组
        # ----------------------------------------------------

        if region_code not in security_group_cache:
            group_result = (
                gateway.ensure_default_security_group(
                    region=region_code,
                    group_name="suanqi-default",
                    source_cidr="0.0.0.0/0",
                    open_ssh=True,
                    open_rdp=True,
                )
            )

            if not group_result.success:
                print(
                    "安全组失败："
                    f"{group_result.error_code}，"
                    f"{group_result.error_message}"
                )

                security_group_cache[
                    region_code
                ] = None

            else:
                security_group_cache[
                    region_code
                ] = group_result.data[
                    "security_group_id"
                ]

        security_group_id = (
            security_group_cache[
                region_code
            ]
        )

        if security_group_id is None:
            continue

        # ----------------------------------------------------
        # 9.3 生成可以直接询价的候选配置
        # ----------------------------------------------------

        prepared_candidates.append(
            {
                **candidate,

                "vpc_id": (
                    network_info["vpc_id"]
                ),

                "subnet_id": (
                    network_info["subnet_id"]
                ),

                "subnet_name": (
                    network_info["subnet_name"]
                ),

                "subnet_cidr": (
                    network_info["subnet_cidr"]
                ),

                "security_group_id": (
                    security_group_id
                ),
            }
        )

    if not prepared_candidates:
        print("\n没有可用于询价的候选配置。")
        return {}

    print(
        f"\n配置准备完成，共 "
        f"{len(prepared_candidates)} 个候选，"
        f"开始并发询价。"
    )

    # ========================================================
    # 10. 多线程网关和全局限速器
    # ========================================================

    thread_local_data = threading.local()
    # 每个线程独立保存自己的 TencentCloudGateway

    rate_limit_lock = threading.Lock()
    # 多个线程共用的限速锁

    request_interval = (
        1.0 / maximum_requests_per_second
    )
    # 两次询价请求开始时间之间的最小间隔

    last_request_time = 0.0
    # 上一次开始请求的时间

    def get_thread_gateway() -> TencentCloudGateway:
        """
        为每个工作线程创建独立网关。

        同一线程后续任务会复用自己的网关，
        不会与其他线程共享腾讯云 SDK Client。
        """

        if not hasattr(
            thread_local_data,
            "gateway",
        ):
            thread_local_data.gateway = (
                TencentCloudGateway()
            )

        return thread_local_data.gateway

    def wait_for_request_slot() -> None:
        """
        全局限制询价请求开始频率。

        即使设置了 4 个线程，所有线程仍然必须
        按照 maximum_requests_per_second 限速。
        """

        nonlocal last_request_time

        with rate_limit_lock:
            current_time = time.monotonic()

            elapsed_time = (
                current_time - last_request_time
            )

            wait_time = (
                request_interval - elapsed_time
            )

            if wait_time > 0:
                time.sleep(wait_time)

            last_request_time = time.monotonic()

    # ========================================================
    # 11. 单个候选配置询价函数
    # ========================================================

    def inquire_candidate_price(
        candidate: dict,
    ) -> dict:
        """
        查询一个候选实例的竞价价格。

        注意：
        所有配置均从 candidate 中读取，
        不再使用外层循环遗留的 region_code、
        zone_code、network_info 等变量。
        """

        thread_gateway = get_thread_gateway()

        config = _build_instance_config(
            candidate,
            instance_name="suanqi-price-test",
        )

        last_price_result = None

        for retry_index in range(
            maximum_retry_count
        ):
            wait_for_request_slot()

            price_result = (
                thread_gateway
                .inquire_instance_price(config)
            )

            last_price_result = price_result

            if price_result.success:
                price_data = (
                    price_result.data.get("price")
                    or {}
                )

                print(price_data)

                instance_price = (
                    price_data.get(
                        "InstancePrice"
                    )
                    or {}
                )

                effective_price = (
                    instance_price.get(
                        "UnitPriceDiscount"
                    )
                )

                if effective_price is None:
                    effective_price = (
                        instance_price.get(
                            "UnitPrice"
                        )
                    )

                return {
                    "success": True,

                    "price_candidate": {
                        **candidate,

                        "effective_hourly_price": (
                            effective_price
                        ),

                        "price": price_data,
                    },
                }

            error_code = (
                price_result.error_code
                or ""
            )

            error_message = (
                price_result.error_message
                or ""
            )

            normalized_error = (
                f"{error_code} "
                f"{error_message}"
            ).lower()

            # 只重试限频和暂时性服务错误
            retryable_error = any(
                keyword in normalized_error
                for keyword in (
                    "requestlimitexceeded",
                    "request limit exceeded",
                    "每秒请求",
                    "频率上限",
                    "频率",
                    "限频",
                    "internalerror",
                    "internal error",
                    "serviceunavailable",
                    "service unavailable",
                    "temporarily unavailable",
                    "暂时不可用",
                    "network",
                    "timeout",
                    "超时",
                )
            )

            if not retryable_error:
                # 不售卖、无库存、机型不支持、
                # 参数错误等不进行重复请求。
                return {
                    "success": False,
                    "candidate": candidate,
                    "error_code": error_code,
                    "error_message": error_message,
                    "retry_count": retry_index,
                }

            if retry_index >= (
                maximum_retry_count - 1
            ):
                break

            retry_wait_seconds = (
                2 ** retry_index
                + random.uniform(0.3, 0.9)
            )

            time.sleep(retry_wait_seconds)

        return {
            "success": False,
            "candidate": candidate,

            "error_code": (
                last_price_result.error_code
                if last_price_result
                else "UnknownError"
            ),

            "error_message": (
                last_price_result.error_message
                if last_price_result
                else "询价请求未返回结果"
            ),

            "retry_count": (
                maximum_retry_count
            ),
        }

    # ========================================================
    # 12. 启动多线程询价
    # ========================================================

    price_candidates = []

    with ThreadPoolExecutor(
        max_workers=maximum_workers
    ) as executor:

        future_to_candidate = {
            executor.submit(
                inquire_candidate_price,
                candidate,
            ): candidate
            for candidate in prepared_candidates
        }

        completed_count = 0
        total_count = len(
            future_to_candidate
        )

        for future in as_completed(
            future_to_candidate
        ):
            candidate = (
                future_to_candidate[future]
            )

            completed_count += 1

            try:
                result = future.result()

            except Exception as error:
                print(
                    f"\n询价任务异常 "
                    f"[{completed_count}/{total_count}]："
                    f"{candidate['region_name']} / "
                    f"{candidate['zone_name']} / "
                    f"{candidate['instance_type']} / "
                    f"{error}"
                )
                continue

            if not result["success"]:
                print(
                    f"\n询价失败 "
                    f"[{completed_count}/{total_count}]："
                    f"{candidate['region_name']} / "
                    f"{candidate['zone_name']} / "
                    f"{candidate['instance_type']} / "
                    f"{result['error_code']}，"
                    f"{result['error_message']}"
                )
                continue

            price_candidate = result[
                "price_candidate"
            ]

            price_candidates.append(
                price_candidate
            )

            effective_price = (
                price_candidate[
                    "effective_hourly_price"
                ]
            )

            print(
                f"\n询价成功 "
                f"[{completed_count}/{total_count}]："
                f"{candidate['region_name']} / "
                f"{candidate['zone_name']} / "
                f"{candidate['instance_type']} / "
                f"{effective_price} 元/小时"
            )

    # ========================================================
    # 13. 排序并输出结果
    # ========================================================

    if not price_candidates:
        print("\n没有询价成功的候选实例。")
        return {}

    price_candidates.sort(
        key=lambda item: (
            item["effective_hourly_price"]
            if item[
                "effective_hourly_price"
            ] is not None
            else float("inf")
        )
    )


    print(
        f"\n询价结束，成功 "
        f"{len(price_candidates)} / "
        f"{len(prepared_candidates)} 个。"
    )

    print("\n按价格从低到高排序：")

    for index, item in enumerate(
        price_candidates,
        1,
    ):
        print(
            f"{index}. "
            f"{item['region_name']} / "
            f"{item['cpu']} 核 / "
            f"{item['memory_gb']} GB / "
            f"{item['effective_hourly_price']} 元/小时"
        )

    selected_candidate = _select_candidate(
        price_candidates
    )

    if selected_candidate is None:
        print("已取消创建实例。")
        return {}

    # ============================================================
    # 创建前二次确认
    # ============================================================

    hourly_price = selected_candidate.get(
        "effective_hourly_price"
    )

    print("\n" + "=" * 70)
    print("即将创建以下腾讯云实例")
    print("=" * 70)

    print(
        f"地域："
        f"{selected_candidate['region_name']} "
        f"({selected_candidate['region_code']})"
    )

    print(
        f"可用区："
        f"{selected_candidate['zone_name']} "
        f"({selected_candidate['zone_code']})"
    )

    print(
        f"机型："
        f"{selected_candidate['instance_type']}"
    )

    print(
        f"配置："
        f"{selected_candidate['cpu']} 核 / "
        f"{selected_candidate['memory_gb']} GB"
    )

    print(
        f"镜像："
        f"{selected_candidate['image_name']} "
        f"({selected_candidate['image_id']})"
    )

    print(
        f"系统盘：{SYSTEM_DISK_TYPE} / {SYSTEM_DISK_SIZE_GB} GiB"
    )

    print(
        f"公网：{"分配" if PUBLIC_IP_ASSIGNED else "不分配"}公网 IP / "
        f"{"按流量计费" if INTERNET_CHARGE_TYPE=="TRAFFIC_POSTPAID_BY_HOUR" else "带宽计费"} / {INTERNET_MAX_BANDWIDTH_MBPS} Mbps 上限"
    )

    print(
        f"询价结果：{hourly_price} 元/小时"
    )

    print(
        f"VPC：{selected_candidate['vpc_id']}"
    )

    print(
        f"子网："
        f"{selected_candidate['subnet_name']} / "
        f"{selected_candidate['subnet_cidr']}"
    )

    print(
        f"安全组："
        f"{selected_candidate['security_group_id']}"
    )

    print("\n警告：确认后将创建真实收费资源。")

    confirmation = input(
        "请输入 QIDONG（或“启动”） 确认创建，"
        "输入其他内容取消："
    ).strip()

    if confirmation != "QIDONG":
        print("已取消创建实例。")
        return {}

    # ============================================================
    # 创建前再次检查是否可售
    # ============================================================

    print(
        "\n正在进行创建前最终可售状态检查……"
    )

    final_availability_result = (
        gateway.check_instance_available(
            region=selected_candidate[
                "region_code"
            ],
            zone=selected_candidate[
                "zone_code"
            ],
            instance_type=selected_candidate[
                "instance_type"
            ],
            charge_type=(
                INSTANCE_CHARGE_TYPE
            ),
        )
    )

    if not final_availability_result.success:
        print(
            "创建前可售状态检查失败："
            f"{final_availability_result.error_code}，"
            f"{final_availability_result.error_message}"
        )
        return {}

    final_availability_data = (
        final_availability_result.data
        or {}
    )

    final_available = bool(
        final_availability_data.get(
            "available",
            False,
        )
    )

    final_sale_status = (
        final_availability_data.get(
            "status"
        )
        or "UNKNOWN"
    )

    if not final_available:
        print(
            "实例当前已不可售，取消创建："
            f"{selected_candidate['region_name']} / "
            f"{selected_candidate['zone_name']} / "
            f"{selected_candidate['instance_type']} / "
            f"状态：{final_sale_status}"
        )
        return {}

    print(
        f"创建前检查通过，"
        f"当前状态：{final_sale_status}"
    )

    create_config = _build_instance_config(
        selected_candidate,
        instance_name="suanqi-task",
    )



    # ============================================================
    # 正式创建实例
    # ============================================================

    print("\n正在提交实例创建请求……")

    create_result = gateway.run_instance(
        config=create_config,
        generate_password_if_missing=True,
    )

    print(create_result)

    if not create_result.success:
        print(
            "创建实例失败："
            f"{create_result.error_code}，"
            f"{create_result.error_message}"
        )
        return {}

    instance_ids = (
            create_result.data.get("instance_ids")
            or []
    )

    if not instance_ids:
        print(
            "创建接口调用成功，"
            "但没有返回实例 ID。"
        )
        return {}

    instance_id = instance_ids[0]

    instance_password = create_result.data.get(
        "password"
    )

    client_token = create_result.data.get(
        "client_token"
    )

    print("\n实例创建请求已提交。")
    print(f"实例 ID：{instance_id}")
    print(f"幂等令牌：{client_token}")

    print("\n请立即保存以下登录信息：")
    print(f"用户名：ubuntu")
    print(f"密码：{instance_password}")

    # ============================================================
    # 等待实例创建完成
    # ============================================================

    print(
        "\n实例正在创建，等待进入 RUNNING 状态……"
    )

    running_result = gateway.wait_instance_running(
        region=create_config.region,
        instance_id=instance_id,
        timeout_seconds=600,
        poll_interval_seconds=5,
    )

    if not running_result.success:
        print(
            "等待实例运行失败："
            f"{running_result.error_code}，"
            f"{running_result.error_message}"
        )

        print(
            f"实例已经提交创建，请前往腾讯云控制台"
            f"检查实例 {instance_id}。"
        )
        return {}

    instance_info = running_result.data

    public_ips = (
            instance_info.get("public_ips")
            or []
    )

    private_ips = (
            instance_info.get("private_ips")
            or []
    )

    public_ip = (
        public_ips[0]
        if public_ips
        else None
    )

    private_ip = (
        private_ips[0]
        if private_ips
        else None
    )

    print("\n" + "=" * 70)
    print("实例创建成功")
    print("=" * 70)

    print(f"实例 ID：{instance_id}")
    print(f"实例状态：{instance_info['state']}")
    print(f"公网 IP：{public_ip or '暂未分配'}")
    print(f"内网 IP：{private_ip or '暂未分配'}")
    print(f"登录密码：{instance_password}")

    if public_ip:
        print("\n可以尝试以下 SSH 命令：")
        print(f"ssh ubuntu@{public_ip}")
    else:
        print(
            "\n实例已运行，但暂未查询到公网 IP。"
        )

    return {
        # 执行结果
        "success": True,

        # 账户信息
        "user_uin": balance_result.data["uin"],

        # 云厂商信息
        "provider": "tencentcloud",

        # 实例定位信息
        "region": create_config.region,
        "zone": create_config.zone,

        # 实例基本信息
        "instance_id": instance_id,
        "instance_name": create_config.instance_name,
        "instance_type": create_config.instance_type,
        "instance_status": instance_info["state"],

        # 实例硬件信息
        "cpu": selected_candidate.get("cpu"),
        "memory_gb": selected_candidate.get("memory_gb"),

        # 网络信息
        "public_ip": public_ip or None,
        "private_ip": private_ip or None,
        "vpc_id": create_config.vpc_id,
        "subnet_id": create_config.subnet_id,
        "security_group_ids": list(
            create_config.security_group_ids or []
        ),

        # 登录信息
        "ssh_username": "ubuntu",
        "ssh_port": 22,
        "instance_password": instance_password,

        # 镜像信息
        "image_id": create_config.image_id,
        "image_name": selected_candidate.get("image_name"),
        "image_platform": selected_candidate.get(
            "image_platform"
        ),
        "image_architecture": selected_candidate.get(
            "image_architecture"
        ),

        # 计费信息
        "charge_type": create_config.charge_type,
        "hourly_price": selected_candidate.get(
            "effective_hourly_price"
        ),

        # 系统盘信息
        "system_disk_type": create_config.system_disk_type,
        "system_disk_size_gb": (
            create_config.system_disk_size_gb
        ),

        # 公网信息
        "public_ip_assigned": (
            create_config.public_ip_assigned
        ),
        "internet_charge_type": (
            create_config.internet_charge_type
        ),
        "internet_max_bandwidth_out_mbps": (
            create_config
            .internet_max_bandwidth_out_mbps
        ),

        # 创建信息
        "created_at": datetime.now(
            timezone.utc
        ).isoformat(),

        # 请求追踪信息
        "client_token": client_token,
        "request_id": create_result.request_id,
    }

if __name__ == "__main__":
    tencentcloud_creat()


