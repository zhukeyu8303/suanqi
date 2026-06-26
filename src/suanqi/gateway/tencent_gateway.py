# -*- coding: utf-8 -*-
from __future__ import annotations

import ipaddress
import json
import os
import secrets
import string
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, TypeVar

from tencentcloud.common import credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.billing.v20180709 import billing_client, models as billing_models
from tencentcloud.region.v20220627 import region_client, models as region_models
from tencentcloud.cvm.v20170312 import cvm_client, models as cvm_models
from tencentcloud.vpc.v20170312 import vpc_client, models as vpc_models
from tencentcloud.cam.v20190116 import cam_client, models as cam_models
from qcloud_cos import CosConfig, CosS3Client
from qcloud_cos.cos_exception import CosServiceError

T = TypeVar("T")


class TencentGatewayError(RuntimeError):
    pass


@dataclass(slots=True)
class GatewayResult:
    success: bool
    action: str
    data: Any = None
    error_code: str | None = None
    error_message: str | None = None
    request_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def require_data(self) -> Any:
        if not self.success:
            raise TencentGatewayError(
                f"{self.action} 调用失败：{self.error_code or 'UnknownError'} - "
                f"{self.error_message or '未知错误'}"
            )
        return self.data


@dataclass(slots=True)
class InstanceConfig:
    region: str
    zone: str
    instance_type: str
    image_id: str
    vpc_id: str
    subnet_id: str
    security_group_ids: list[str] = field(default_factory=list)
    charge_type: str = "POSTPAID_BY_HOUR"
    system_disk_type: str = "CLOUD_BSSD"
    system_disk_size_gb: int = 50
    public_ip_assigned: bool = True
    internet_charge_type: str = "TRAFFIC_POSTPAID_BY_HOUR"
    internet_max_bandwidth_out_mbps: int = 10
    instance_count: int = 1
    instance_name: str = "suanqi-task"
    password: str | None = None
    spot_max_price: str | None = None
    client_token: str | None = None
    cam_role_name: str | None = None
    disable_api_termination: bool = False


class TencentCloudGateway:
    def __init__(self, secret_id: str | None = None, secret_key: str | None = None, timeout_seconds: int = 30) -> None:
        self.secret_id = secret_id or os.getenv("TENCENTCLOUD_SECRET_ID")
        self.secret_key = secret_key or os.getenv("TENCENTCLOUD_SECRET_KEY")
        self.timeout_seconds = timeout_seconds
        if not self.secret_id or not self.secret_key:
            raise TencentGatewayError(
                "未找到腾讯云密钥，请设置 TENCENTCLOUD_SECRET_ID 和 TENCENTCLOUD_SECRET_KEY。"
            )
        self.credential = credential.Credential(self.secret_id, self.secret_key)

    def _profile(self, endpoint: str) -> ClientProfile:
        http_profile = HttpProfile()
        http_profile.endpoint = endpoint
        http_profile.reqMethod = "POST"
        http_profile.reqTimeout = self.timeout_seconds
        http_profile.keepAlive = True
        client_profile = ClientProfile()
        client_profile.signMethod = "TC3-HMAC-SHA256"
        client_profile.httpProfile = http_profile
        return client_profile

    def _billing_client(self) -> billing_client.BillingClient:
        return billing_client.BillingClient(self.credential, "", self._profile("billing.tencentcloudapi.com"))

    def _region_client(self) -> region_client.RegionClient:
        return region_client.RegionClient(self.credential, "", self._profile("region.tencentcloudapi.com"))

    def _cvm_client(self, region: str) -> cvm_client.CvmClient:
        return cvm_client.CvmClient(self.credential, region, self._profile("cvm.tencentcloudapi.com"))

    def _vpc_client(self, region: str) -> vpc_client.VpcClient:
        return vpc_client.VpcClient(self.credential, region, self._profile("vpc.tencentcloudapi.com"))

    def _cam_client(self) -> cam_client.CamClient:
        return cam_client.CamClient(self.credential, "", self._profile("cam.tencentcloudapi.com"))

    def _cos_client(self, region: str) -> CosS3Client:
        config = CosConfig(
            Region=region,
            SecretId=self.secret_id,
            SecretKey=self.secret_key,
            Token=None,
            Scheme="https",
        )
        return CosS3Client(config)

    @staticmethod
    def _request(model_class: type[T], payload: dict[str, Any]) -> T:
        request = model_class()
        request.from_json_string(json.dumps(payload, ensure_ascii=False))
        return request

    @staticmethod
    def _response_to_dict(response: Any) -> dict[str, Any]:
        return json.loads(response.to_json_string())

    def _execute(self, action: str, function: Callable[[], Any], transformer: Callable[[dict[str, Any]], Any] | None = None) -> GatewayResult:
        try:
            response_dict = self._response_to_dict(function())
            return GatewayResult(
                success=True,
                action=action,
                data=transformer(response_dict) if transformer else response_dict,
                request_id=response_dict.get("RequestId"),
            )
        except TencentCloudSDKException as error:
            return GatewayResult(
                success=False,
                action=action,
                error_code=getattr(error, "code", "TencentCloudSDKException"),
                error_message=getattr(error, "message", str(error)),
                request_id=getattr(error, "requestId", getattr(error, "request_id", None)),
            )
        except Exception as error:
            return GatewayResult(False, action, error_code=error.__class__.__name__, error_message=str(error))

    @staticmethod
    def generate_password(length: int = 20) -> str:
        if length < 12:
            raise ValueError("密码长度不能小于 12 位。")
        uppercase = string.ascii_uppercase
        lowercase = string.ascii_lowercase
        digits = string.digits
        symbols = "!@#$%^*()-_+="
        chars = [secrets.choice(uppercase), secrets.choice(lowercase), secrets.choice(digits), secrets.choice(symbols)]
        all_chars = uppercase + lowercase + digits + symbols
        chars.extend(secrets.choice(all_chars) for _ in range(length - 4))
        secrets.SystemRandom().shuffle(chars)
        return "".join(chars)

    @staticmethod
    def generate_client_token(prefix: str = "suanqi") -> str:
        return f"{prefix}-{uuid.uuid4().hex}"

    @staticmethod
    def _find_available_subnet_cidr(vpc_cidr: str, existing_subnet_cidrs: list[str], preferred_prefix: int = 24) -> str:
        vpc_network = ipaddress.ip_network(vpc_cidr, strict=False)
        if vpc_network.version != 4:
            raise TencentGatewayError("当前自动创建子网功能只支持 IPv4 VPC。")
        new_prefix = max(preferred_prefix, vpc_network.prefixlen)
        existing_networks = []
        for cidr in existing_subnet_cidrs:
            if not cidr:
                continue
            try:
                network = ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                continue
            if network.version == 4:
                existing_networks.append(network)
        for candidate in vpc_network.subnets(new_prefix=new_prefix):
            if not any(candidate.overlaps(existing) for existing in existing_networks):
                return str(candidate)
        raise TencentGatewayError(f"VPC 网段 {vpc_cidr} 中没有可用的 /{new_prefix} 子网网段。")

    def get_account_balance(self, include_temp_credit: bool = True) -> GatewayResult:
        request = self._request(billing_models.DescribeAccountBalanceRequest, {"TempCredit": include_temp_credit})
        def transform(data: dict[str, Any]) -> dict[str, Any]:
            amount_fields = {
                "Balance": "balance", "RealBalance": "real_balance",
                "CashAccountBalance": "cash_balance", "PresentAccountBalance": "present_balance",
                "FreezeAmount": "freeze_amount", "OweAmount": "owe_amount",
                "CreditBalance": "credit_balance", "RealCreditBalance": "real_credit_balance",
                "TempCredit": "temp_credit",
            }
            result = {"uin": data.get("Uin"), "allow_arrears": data.get("IsAllowArrears"), "credit_limited": data.get("IsCreditLimited")}
            for source_name, target_name in amount_fields.items():
                value_cent = float(data.get(source_name) or 0)
                result[f"{target_name}_cent"] = value_cent
                result[f"{target_name}_yuan"] = round(value_cent / 100, 2)
            return result
        return self._execute("DescribeAccountBalance", lambda: self._billing_client().DescribeAccountBalance(request), transform)

    def get_user_app_id(self) -> GatewayResult:
        request = cam_models.GetUserAppIdRequest()

        def transform(data: dict[str, Any]) -> dict[str, Any]:
            return {
                "app_id": data.get("AppId"),
                "uin": data.get("Uin"),
            }

        return self._execute(
            "GetUserAppId",
            lambda: self._cam_client().GetUserAppId(request),
            transform,
        )

    def list_regions(self, product: str = "cvm", only_available: bool = True, scene: int = 1) -> GatewayResult:
        request = self._request(region_models.DescribeRegionsRequest, {"Product": product, "Scene": scene})
        def transform(data: dict[str, Any]) -> dict[str, Any]:
            regions = []
            for item in data.get("RegionSet") or []:
                state = item.get("RegionState")
                if only_available and state != "AVAILABLE":
                    continue
                regions.append({"region": item.get("Region"), "name": item.get("RegionName"), "state": state})
            return {"product": product, "total_count": len(regions), "regions": regions}
        return self._execute("DescribeRegions", lambda: self._region_client().DescribeRegions(request), transform)

    def list_zones(self, region: str, only_available: bool = True) -> GatewayResult:
        request = cvm_models.DescribeZonesRequest()
        def transform(data: dict[str, Any]) -> dict[str, Any]:
            zones = []
            for item in data.get("ZoneSet") or []:
                state = item.get("ZoneState")
                if only_available and state != "AVAILABLE":
                    continue
                zones.append({"zone": item.get("Zone"), "name": item.get("ZoneName"), "state": state})
            return {"region": region, "total_count": len(zones), "zones": zones}
        return self._execute("DescribeZones", lambda: self._cvm_client(region).DescribeZones(request), transform)

    def list_instance_types(self, region: str, zone: str | None = None, minimum_cpu: int | None = None, minimum_memory_gb: int | None = None, instance_type: str | None = None) -> GatewayResult:
        filters = []
        if zone:
            filters.append({"Name": "zone", "Values": [zone]})
        if instance_type:
            filters.append({"Name": "instance-type", "Values": [instance_type]})
        request = self._request(cvm_models.DescribeInstanceTypeConfigsRequest, {"Filters": filters} if filters else {})
        def transform(data: dict[str, Any]) -> dict[str, Any]:
            result = []
            seen = set()
            for item in data.get("InstanceTypeConfigSet") or []:
                name = item.get("InstanceType")
                cpu = int(item.get("CPU") or 0)
                memory = int(item.get("Memory") or 0)
                if not name or name in seen:
                    continue
                if minimum_cpu is not None and cpu < minimum_cpu:
                    continue
                if minimum_memory_gb is not None and memory < minimum_memory_gb:
                    continue
                seen.add(name)
                result.append({
                    "instance_type": name,
                    "instance_family": item.get("InstanceFamily"),
                    "gpu": item.get("GPU"),
                    "fpga": item.get("FPGA"),
                    "cpu": cpu,
                    "memory_gb": memory,
                })
            result.sort(key=lambda x: (x["cpu"], x["memory_gb"], x["instance_type"]))
            return {"region": region, "zone": zone, "total_count": len(result), "instance_types": result}
        return self._execute("DescribeInstanceTypeConfigs", lambda: self._cvm_client(region).DescribeInstanceTypeConfigs(request), transform)

    def list_images(self, region: str, image_type: str = "PUBLIC_IMAGE", platform: str | None = "Ubuntu", image_name_keyword: str | None = None, instance_type: str | None = None, limit: int = 100) -> GatewayResult:
        filters = []
        if image_type:
            filters.append({"Name": "image-type", "Values": [image_type]})
        payload: dict[str, Any] = {"Filters": filters, "Limit": limit, "Offset": 0}
        if instance_type:
            payload["InstanceType"] = instance_type
        request = self._request(cvm_models.DescribeImagesRequest, payload)
        def transform(data: dict[str, Any]) -> dict[str, Any]:
            images = []
            for item in data.get("ImageSet") or []:
                item_platform = str(item.get("Platform") or "")
                item_name = str(item.get("ImageName") or "")
                if platform and platform.lower() not in item_platform.lower() and platform.lower() not in item_name.lower():
                    continue
                if image_name_keyword and image_name_keyword.lower() not in item_name.lower():
                    continue
                images.append({
                    "image_id": item.get("ImageId"), "image_name": item_name,
                    "image_type": item.get("ImageType"), "platform": item_platform,
                    "architecture": item.get("Architecture"), "image_state": item.get("ImageState"),
                    "image_size_gb": item.get("ImageSize"), "created_time": item.get("CreatedTime"),
                })
            return {"region": region, "total_count": len(images), "images": images}
        return self._execute("DescribeImages", lambda: self._cvm_client(region).DescribeImages(request), transform)

    def list_vpcs(self, region: str, only_default: bool = False) -> GatewayResult:
        filters = [{"Name": "is-default", "Values": ["true"]}] if only_default else []
        payload: dict[str, Any] = {"Limit": "100", "Offset": "0"}
        if filters:
            payload["Filters"] = filters
        request = self._request(vpc_models.DescribeVpcsRequest, payload)
        def transform(data: dict[str, Any]) -> dict[str, Any]:
            vpcs = []
            for item in data.get("VpcSet") or []:
                is_default = bool(item.get("IsDefault"))
                if only_default and not is_default:
                    continue
                vpcs.append({
                    "vpc_id": item.get("VpcId"), "vpc_name": item.get("VpcName"),
                    "cidr_block": item.get("CidrBlock"), "ipv6_cidr_block": item.get("Ipv6CidrBlock"),
                    "is_default": is_default, "created_time": item.get("CreatedTime"),
                })
            return {"region": region, "total_count": len(vpcs), "vpcs": vpcs}
        return self._execute("DescribeVpcs", lambda: self._vpc_client(region).DescribeVpcs(request), transform)

    def get_default_vpc(self, region: str) -> GatewayResult:
        result = self.list_vpcs(region, only_default=True)
        if not result.success:
            return result
        vpcs = result.data["vpcs"]
        if not vpcs:
            return GatewayResult(False, "GetDefaultVpc", error_code="DefaultVpcNotFound", error_message=f"地域 {region} 中没有找到默认 VPC。", request_id=result.request_id)
        return GatewayResult(True, "GetDefaultVpc", data=vpcs[0], request_id=result.request_id)

    def list_subnets(self, region: str, vpc_id: str | None = None, zone: str | None = None, only_default: bool = False) -> GatewayResult:
        filters = []
        if vpc_id:
            filters.append({"Name": "vpc-id", "Values": [vpc_id]})
        if zone:
            filters.append({"Name": "zone", "Values": [zone]})
        if only_default:
            filters.append({"Name": "is-default", "Values": ["true"]})
        request = self._request(vpc_models.DescribeSubnetsRequest, {"Filters": filters, "Limit": "100", "Offset": "0"})
        def transform(data: dict[str, Any]) -> dict[str, Any]:
            subnets = []
            for item in data.get("SubnetSet") or []:
                item_zone = item.get("Zone")
                is_default = bool(item.get("IsDefault"))
                if zone and item_zone != zone:
                    continue
                if only_default and not is_default:
                    continue
                subnets.append({
                    "subnet_id": item.get("SubnetId"), "subnet_name": item.get("SubnetName"),
                    "vpc_id": item.get("VpcId"), "zone": item_zone, "cidr_block": item.get("CidrBlock"),
                    "ipv6_cidr_block": item.get("Ipv6CidrBlock"), "is_default": is_default,
                    "available_ip_address_count": item.get("AvailableIpAddressCount"),
                    "is_remote_vpc_snat": item.get("IsRemoteVpcSnat"), "created_time": item.get("CreatedTime"),
                })
            return {"region": region, "vpc_id": vpc_id, "zone": zone, "total_count": len(subnets), "subnets": subnets}
        return self._execute("DescribeSubnets", lambda: self._vpc_client(region).DescribeSubnets(request), transform)

    def create_suanqi_subnet(self, region: str, zone: str, vpc_id: str, vpc_cidr: str, existing_subnet_cidrs: list[str], subnet_prefix: int = 24, subnet_name: str | None = None) -> GatewayResult:
        try:
            subnet_cidr = self._find_available_subnet_cidr(vpc_cidr, existing_subnet_cidrs, subnet_prefix)
        except Exception as error:
            return GatewayResult(False, "CreateSubnet", error_code=error.__class__.__name__, error_message=str(error))
        resolved_name = subnet_name or f"suanqi-default-{zone}"
        request = self._request(vpc_models.CreateSubnetRequest, {
            "VpcId": vpc_id, "SubnetName": resolved_name, "CidrBlock": subnet_cidr, "Zone": zone,
        })
        def transform(data: dict[str, Any]) -> dict[str, Any]:
            subnet = data.get("Subnet") or {}
            return {
                "subnet_id": subnet.get("SubnetId") or data.get("SubnetId"),
                "subnet_name": subnet.get("SubnetName") or resolved_name,
                "vpc_id": subnet.get("VpcId") or vpc_id,
                "zone": subnet.get("Zone") or zone,
                "cidr_block": subnet.get("CidrBlock") or subnet_cidr,
                "is_default": bool(subnet.get("IsDefault", False)),
                "created_by_suanqi": True,
            }
        return self._execute("CreateSubnet", lambda: self._vpc_client(region).CreateSubnet(request), transform)

    def resolve_network_for_zone(self, region: str, zone: str, create_subnet_if_missing: bool = True, subnet_prefix: int = 24) -> GatewayResult:
        vpc_result = self.get_default_vpc(region)
        if not vpc_result.success:
            return vpc_result
        vpc = vpc_result.data
        vpc_id = vpc["vpc_id"]
        vpc_cidr = vpc.get("cidr_block")
        if not vpc_cidr:
            return GatewayResult(False, "ResolveNetworkForZone", error_code="VpcCidrMissing", error_message=f"默认 VPC {vpc_id} 没有返回 IPv4 CIDR。", request_id=vpc_result.request_id)

        zone_result = self.list_subnets(region, vpc_id=vpc_id, zone=zone, only_default=False)
        if not zone_result.success:
            return zone_result
        zone_subnets = zone_result.data["subnets"]
        if zone_subnets:
            zone_subnets.sort(key=lambda subnet: (not subnet.get("is_default", False), -int(subnet.get("available_ip_address_count") or 0)))
            return GatewayResult(True, "ResolveNetworkForZone", data={
                "region": region, "zone": zone, "vpc": vpc, "subnet": zone_subnets[0], "subnet_created": False,
            }, request_id=zone_result.request_id)

        if not create_subnet_if_missing:
            return GatewayResult(False, "ResolveNetworkForZone", error_code="ZoneSubnetNotFound", error_message=f"地域 {region}、可用区 {zone} 中没有子网。", request_id=zone_result.request_id)

        all_result = self.list_subnets(region, vpc_id=vpc_id, zone=None, only_default=False)
        if not all_result.success:
            return all_result
        existing_cidrs = [subnet["cidr_block"] for subnet in all_result.data["subnets"] if subnet.get("cidr_block")]
        create_result = self.create_suanqi_subnet(region, zone, vpc_id, vpc_cidr, existing_cidrs, subnet_prefix)
        if not create_result.success:
            return create_result
        return GatewayResult(True, "ResolveNetworkForZone", data={
            "region": region, "zone": zone, "vpc": vpc, "subnet": create_result.data, "subnet_created": True,
        }, request_id=create_result.request_id)

    def resolve_default_network(self, region: str, zone: str) -> GatewayResult:
        return self.resolve_network_for_zone(region, zone, True, 24)

    def list_security_groups(self, region: str, group_name: str | None = None) -> GatewayResult:
        filters = [{"Name": "security-group-name", "Values": [group_name]}] if group_name else []
        payload: dict[str, Any] = {"Limit": "100", "Offset": "0"}
        if filters:
            payload["Filters"] = filters
        request = self._request(vpc_models.DescribeSecurityGroupsRequest, payload)
        def transform(data: dict[str, Any]) -> dict[str, Any]:
            groups = []
            for item in data.get("SecurityGroupSet") or []:
                name = item.get("SecurityGroupName")
                if group_name and name != group_name:
                    continue
                groups.append({
                    "security_group_id": item.get("SecurityGroupId"), "security_group_name": name,
                    "description": item.get("SecurityGroupDesc"), "project_id": item.get("ProjectId"),
                    "is_default": item.get("IsDefault"), "created_time": item.get("CreatedTime"),
                })
            return {"region": region, "total_count": len(groups), "security_groups": groups}
        return self._execute("DescribeSecurityGroups", lambda: self._vpc_client(region).DescribeSecurityGroups(request), transform)

    def get_security_group_policies(self, region: str, security_group_id: str) -> GatewayResult:
        request = self._request(vpc_models.DescribeSecurityGroupPoliciesRequest, {"SecurityGroupId": security_group_id})
        def transform(data: dict[str, Any]) -> dict[str, Any]:
            policy_set = data.get("SecurityGroupPolicySet") or {}
            return {"region": region, "security_group_id": security_group_id, "version": policy_set.get("Version"), "ingress": policy_set.get("Ingress") or [], "egress": policy_set.get("Egress") or []}
        return self._execute("DescribeSecurityGroupPolicies", lambda: self._vpc_client(region).DescribeSecurityGroupPolicies(request), transform)

    @staticmethod
    def _default_security_rules(source_cidr: str, open_ssh: bool, open_rdp: bool) -> dict[str, list[dict[str, Any]]]:
        ingress = []
        if open_ssh:
            ingress.append({"Protocol": "TCP", "Port": "22", "CidrBlock": source_cidr, "Action": "ACCEPT", "PolicyDescription": "SuanQi SSH"})
        if open_rdp:
            ingress.append({"Protocol": "TCP", "Port": "3389", "CidrBlock": source_cidr, "Action": "ACCEPT", "PolicyDescription": "SuanQi RDP"})
        egress = [{"Protocol": "ALL", "Port": "ALL", "CidrBlock": "0.0.0.0/0", "Action": "ACCEPT", "PolicyDescription": "SuanQi allow outbound"}]
        return {"Ingress": ingress, "Egress": egress}

    @staticmethod
    def _policy_exists(policies: list[dict[str, Any]], expected: dict[str, Any]) -> bool:
        for policy in policies:
            if (
                str(policy.get("Protocol") or "").upper() == str(expected.get("Protocol") or "").upper()
                and str(policy.get("Port") or "") == str(expected.get("Port") or "")
                and str(policy.get("CidrBlock") or "") == str(expected.get("CidrBlock") or "")
                and str(policy.get("Action") or "").upper() == str(expected.get("Action") or "").upper()
            ):
                return True
        return False

    def create_security_group_with_policies(self, region: str, group_name: str = "suanqi-default", description: str = "SuanQi default security group", source_cidr: str = "0.0.0.0/0", open_ssh: bool = True, open_rdp: bool = True) -> GatewayResult:
        rules = self._default_security_rules(source_cidr, open_ssh, open_rdp)
        request = self._request(vpc_models.CreateSecurityGroupWithPoliciesRequest, {
            "GroupName": group_name, "GroupDescription": description, "ProjectId": "0", "SecurityGroupPolicySet": rules,
        })
        def transform(data: dict[str, Any]) -> dict[str, Any]:
            group = data.get("SecurityGroup") or {}
            return {"region": region, "security_group_id": group.get("SecurityGroupId"), "security_group_name": group.get("SecurityGroupName"), "description": group.get("SecurityGroupDesc"), "created": True}
        return self._execute("CreateSecurityGroupWithPolicies", lambda: self._vpc_client(region).CreateSecurityGroupWithPolicies(request), transform)

    def add_security_group_policies(self, region: str, security_group_id: str, ingress: list[dict[str, Any]] | None = None, egress: list[dict[str, Any]] | None = None) -> GatewayResult:
        policy_set = {}
        if ingress:
            policy_set["Ingress"] = ingress
        if egress:
            policy_set["Egress"] = egress
        if not policy_set:
            return GatewayResult(True, "CreateSecurityGroupPolicies", data={"security_group_id": security_group_id, "added": False})
        request = self._request(vpc_models.CreateSecurityGroupPoliciesRequest, {"SecurityGroupId": security_group_id, "SecurityGroupPolicySet": policy_set})
        return self._execute("CreateSecurityGroupPolicies", lambda: self._vpc_client(region).CreateSecurityGroupPolicies(request), lambda data: {"region": region, "security_group_id": security_group_id, "added": True})

    def ensure_default_security_group(self, region: str, group_name: str = "suanqi-default", source_cidr: str = "0.0.0.0/0", open_ssh: bool = True, open_rdp: bool = True) -> GatewayResult:
        groups_result = self.list_security_groups(region, group_name)
        if not groups_result.success:
            return groups_result
        groups = groups_result.data["security_groups"]
        if not groups:
            return self.create_security_group_with_policies(region, group_name, source_cidr=source_cidr, open_ssh=open_ssh, open_rdp=open_rdp)
        group = groups[0]
        group_id = group["security_group_id"]
        policies_result = self.get_security_group_policies(region, group_id)
        if not policies_result.success:
            return policies_result
        expected = self._default_security_rules(source_cidr, open_ssh, open_rdp)
        missing_ingress = [p for p in expected["Ingress"] if not self._policy_exists(policies_result.data["ingress"], p)]
        missing_egress = [p for p in expected["Egress"] if not self._policy_exists(policies_result.data["egress"], p)]
        if missing_ingress or missing_egress:
            add_result = self.add_security_group_policies(region, group_id, missing_ingress, missing_egress)
            if not add_result.success:
                return add_result
        return GatewayResult(True, "EnsureDefaultSecurityGroup", data={**group, "created": False, "missing_ingress_added": missing_ingress, "missing_egress_added": missing_egress}, request_id=policies_result.request_id)

    @staticmethod
    def _instance_payload(config: InstanceConfig, include_login: bool) -> dict[str, Any]:
        payload = {
            "Placement": {"Zone": config.zone},
            "InstanceType": config.instance_type,
            "ImageId": config.image_id,
            "InstanceChargeType": config.charge_type,
            "SystemDisk": {"DiskType": config.system_disk_type, "DiskSize": config.system_disk_size_gb},
            "VirtualPrivateCloud": {"VpcId": config.vpc_id, "SubnetId": config.subnet_id, "AsVpcGateway": False},
            "InternetAccessible": {"PublicIpAssigned": config.public_ip_assigned, "InternetChargeType": config.internet_charge_type, "InternetMaxBandwidthOut": config.internet_max_bandwidth_out_mbps},
            "SecurityGroupIds": config.security_group_ids,
            "InstanceCount": config.instance_count,
            "InstanceName": config.instance_name,
        }
        if config.client_token:
            payload["ClientToken"] = config.client_token
        if config.cam_role_name:
            payload["CamRoleName"] = config.cam_role_name
        payload["DisableApiTermination"] = config.disable_api_termination
        if config.charge_type == "SPOTPAID":
            if not config.spot_max_price:
                raise TencentGatewayError("竞价实例必须设置 spot_max_price。")
            payload["InstanceMarketOptions"] = {"MarketType": "spot", "SpotOptions": {"MaxPrice": config.spot_max_price, "SpotInstanceType": "one-time"}}
        if include_login:
            if not config.password:
                raise TencentGatewayError("正式创建实例前必须生成登录密码。")
            payload["LoginSettings"] = {"Password": config.password, "KeepImageLogin": "FALSE"}
        return payload

    def inquire_instance_price(self, config: InstanceConfig) -> GatewayResult:
        request = self._request(cvm_models.InquiryPriceRunInstancesRequest, self._instance_payload(config, False))
        return self._execute("InquiryPriceRunInstances", lambda: self._cvm_client(config.region).InquiryPriceRunInstances(request), lambda data: {
            "region": config.region, "zone": config.zone, "instance_type": config.instance_type,
            "charge_type": config.charge_type, "instance_count": config.instance_count, "price": data.get("Price"),
        })

    def run_instance(self, config: InstanceConfig, generate_password_if_missing: bool = True) -> GatewayResult:
        if not config.password and generate_password_if_missing:
            config.password = self.generate_password()
        if not config.client_token:
            config.client_token = self.generate_client_token()
        request = self._request(cvm_models.RunInstancesRequest, self._instance_payload(config, True))
        return self._execute("RunInstances", lambda: self._cvm_client(config.region).RunInstances(request), lambda data: {
            "region": config.region, "zone": config.zone, "instance_ids": data.get("InstanceIdSet") or [],
            "client_token": config.client_token, "password": config.password,
        })

    def describe_instances(self, region: str, instance_ids: list[str] | None = None, limit: int = 100, offset: int = 0) -> GatewayResult:
        payload: dict[str, Any] = {"Limit": limit, "Offset": offset}
        if instance_ids:
            payload["InstanceIds"] = instance_ids
        request = self._request(cvm_models.DescribeInstancesRequest, payload)
        def transform(data: dict[str, Any]) -> dict[str, Any]:
            instances = []
            for item in data.get("InstanceSet") or []:
                instances.append({
                    "instance_id": item.get("InstanceId"), "instance_name": item.get("InstanceName"),
                    "instance_type": item.get("InstanceType"), "state": item.get("InstanceState"),
                    "region": region, "zone": item.get("Placement", {}).get("Zone"),
                    "public_ips": item.get("PublicIpAddresses") or [], "private_ips": item.get("PrivateIpAddresses") or [],
                    "charge_type": item.get("InstanceChargeType"), "created_time": item.get("CreatedTime"),
                    "expired_time": item.get("ExpiredTime"), "latest_operation": item.get("LatestOperation"),
                    "latest_operation_state": item.get("LatestOperationState"), "latest_operation_request_id": item.get("LatestOperationRequestId"),
                    "raw": item,
                })
            return {"region": region, "total_count": data.get("TotalCount", 0), "instances": instances}
        return self._execute("DescribeInstances", lambda: self._cvm_client(region).DescribeInstances(request), transform)

    def wait_instance_running(self, region: str, instance_id: str, timeout_seconds: int = 300, poll_interval_seconds: int = 5) -> GatewayResult:
        start = time.monotonic()
        last_instance = None
        while time.monotonic() - start < timeout_seconds:
            result = self.describe_instances(region, [instance_id])
            if not result.success:
                return result
            instances = result.data["instances"]
            if not instances:
                return GatewayResult(False, "WaitInstanceRunning", error_code="InstanceNotFound", error_message=f"未找到实例 {instance_id}。")
            last_instance = instances[0]
            if last_instance["state"] == "RUNNING":
                return GatewayResult(True, "WaitInstanceRunning", data=last_instance, request_id=result.request_id)
            if last_instance["state"] == "LAUNCH_FAILED":
                return GatewayResult(False, "WaitInstanceRunning", data=last_instance, error_code="InstanceLaunchFailed", error_message=f"实例 {instance_id} 创建失败。", request_id=result.request_id)
            time.sleep(poll_interval_seconds)
        return GatewayResult(False, "WaitInstanceRunning", data=last_instance, error_code="WaitTimeout", error_message=f"等待实例 {instance_id} 进入 RUNNING 状态超时。")

    def terminate_instances(self, region: str, instance_ids: list[str], release_address: bool = True, release_prepaid_data_disks: bool = False) -> GatewayResult:
        if not instance_ids:
            return GatewayResult(False, "TerminateInstances", error_code="EmptyInstanceIds", error_message="instance_ids 不能为空。")
        request = self._request(cvm_models.TerminateInstancesRequest, {
            "InstanceIds": instance_ids, "ReleaseAddress": release_address, "ReleasePrepaidDataDisks": release_prepaid_data_disks,
        })
        return self._execute("TerminateInstances", lambda: self._cvm_client(region).TerminateInstances(request), lambda data: {"region": region, "instance_ids": instance_ids, "termination_requested": True})


    # ------------------------------------------------------------------
    # CAM 角色与策略
    # ------------------------------------------------------------------

    @staticmethod
    def build_cvm_trust_policy() -> dict[str, Any]:
        """
        构造允许腾讯云 CVM 服务扮演角色的信任策略。
        """
        return {
            "version": "2.0",
            "statement": [
                {
                    "effect": "allow",
                    "action": "name/sts:AssumeRole",
                    "principal": {
                        "service": [
                            "cvm.qcloud.com"
                        ]
                    },
                }
            ],
        }

    @staticmethod
    def build_suanqi_worker_policy(
        cos_bucket: str,
        cos_region: str,
        cos_resource_owner: str,
        cos_prefix: str = "tasks/*",
        allow_describe_instances: bool = True,
    ) -> dict[str, Any]:
        """
        构造 SuanQi 工作实例的最小权限策略。

        cos_bucket：
            完整 Bucket 名称，例如 suanqi-result-1250000000。

        cos_region：
            COS 地域，例如 ap-shanghai。

        cos_resource_owner：
            COS 资源所有者标识。通常填写主账号 APPID 或文档要求的 uid。

        cos_prefix：
            允许实例写入的 COS 对象前缀。
        """
        normalized_prefix = cos_prefix.strip().lstrip("/")
        if not normalized_prefix:
            normalized_prefix = "*"

        cos_resource = (
            f"qcs::cos:{cos_region}:uid/{cos_resource_owner}:"
            f"{cos_bucket}/{normalized_prefix}"
        )

        cvm_actions = ["cvm:TerminateInstances"]
        if allow_describe_instances:
            cvm_actions.append("cvm:DescribeInstances")

        return {
            "version": "2.0",
            "statement": [
                {
                    "effect": "allow",
                    "action": [
                        "cos:PutObject",
                        "cos:PostObject",
                        "cos:InitiateMultipartUpload",
                        "cos:UploadPart",
                        "cos:CompleteMultipartUpload",
                        "cos:AbortMultipartUpload",
                    ],
                    "resource": [cos_resource],
                },
                {
                    "effect": "allow",
                    "action": cvm_actions,
                    "resource": ["*"],
                },
            ],
        }

    def get_role(self, role_name: str) -> GatewayResult:
        request = self._request(
            cam_models.GetRoleRequest,
            {"RoleName": role_name},
        )

        def transform(data: dict[str, Any]) -> dict[str, Any]:
            role_info = data.get("RoleInfo") or {}
            return {
                "role_id": role_info.get("RoleId"),
                "role_name": role_info.get("RoleName") or role_name,
                "policy_document": role_info.get("PolicyDocument"),
                "description": role_info.get("Description"),
                "add_time": role_info.get("AddTime"),
                "update_time": role_info.get("UpdateTime"),
                "role_type": role_info.get("RoleType"),
                "role_arn": role_info.get("RoleArn"),
            }

        return self._execute(
            "GetRole",
            lambda: self._cam_client().GetRole(request),
            transform,
        )

    def create_role(
        self,
        role_name: str,
        policy_document: dict[str, Any] | str | None = None,
        description: str = "SuanQi CVM worker role",
        session_duration_seconds: int = 43200,
    ) -> GatewayResult:
        if isinstance(policy_document, dict):
            policy_text = json.dumps(
                policy_document,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        elif isinstance(policy_document, str):
            policy_text = policy_document
        else:
            policy_text = json.dumps(
                self.build_cvm_trust_policy(),
                ensure_ascii=False,
                separators=(",", ":"),
            )

        request = self._request(
            cam_models.CreateRoleRequest,
            {
                "RoleName": role_name,
                "PolicyDocument": policy_text,
                "Description": description,
                "ConsoleLogin": 0,
                "SessionDuration": session_duration_seconds,
            },
        )

        return self._execute(
            "CreateRole",
            lambda: self._cam_client().CreateRole(request),
            lambda data: {
                "role_id": data.get("RoleId"),
                "role_name": role_name,
                "created": True,
            },
        )

    def list_policies(
        self,
        keyword: str | None = None,
        scope: str = "Local",
        page: int = 1,
        rows_per_page: int = 200,
    ) -> GatewayResult:
        payload: dict[str, Any] = {
            "Page": page,
            "Rp": rows_per_page,
            "Scope": scope,
        }
        if keyword:
            payload["Keyword"] = keyword

        request = self._request(
            cam_models.ListPoliciesRequest,
            payload,
        )

        def transform(data: dict[str, Any]) -> dict[str, Any]:
            policies = []
            for item in data.get("List") or []:
                policies.append(
                    {
                        "policy_id": item.get("PolicyId"),
                        "policy_name": item.get("PolicyName"),
                        "description": item.get("Description"),
                        "add_time": item.get("AddTime"),
                        "type": item.get("Type"),
                        "service_type": item.get("ServiceType"),
                        "create_mode": item.get("CreateMode"),
                    }
                )
            return {
                "total_count": data.get("TotalNum", len(policies)),
                "policies": policies,
            }

        return self._execute(
            "ListPolicies",
            lambda: self._cam_client().ListPolicies(request),
            transform,
        )

    def get_policy(self, policy_id: int) -> GatewayResult:
        request = self._request(
            cam_models.GetPolicyRequest,
            {"PolicyId": policy_id},
        )

        def transform(data: dict[str, Any]) -> dict[str, Any]:
            return {
                "policy_id": data.get("PolicyId"),
                "policy_name": data.get("PolicyName"),
                "description": data.get("Description"),
                "policy_document": data.get("PolicyDocument"),
                "update_time": data.get("UpdateTime"),
                "add_time": data.get("AddTime"),
                "type": data.get("Type"),
            }

        return self._execute(
            "GetPolicy",
            lambda: self._cam_client().GetPolicy(request),
            transform,
        )

    def create_policy(
        self,
        policy_name: str,
        policy_document: dict[str, Any] | str,
        description: str = "SuanQi worker minimum permission policy",
    ) -> GatewayResult:
        if isinstance(policy_document, dict):
            policy_text = json.dumps(
                policy_document,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        else:
            policy_text = policy_document

        request = self._request(
            cam_models.CreatePolicyRequest,
            {
                "PolicyName": policy_name,
                "PolicyDocument": policy_text,
                "Description": description,
            },
        )

        return self._execute(
            "CreatePolicy",
            lambda: self._cam_client().CreatePolicy(request),
            lambda data: {
                "policy_id": data.get("PolicyId"),
                "policy_name": policy_name,
                "created": True,
            },
        )

    def update_policy(
        self,
        policy_id: int,
        policy_document: dict[str, Any] | str,
        description: str | None = None,
    ) -> GatewayResult:
        if isinstance(policy_document, dict):
            policy_text = json.dumps(
                policy_document,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        else:
            policy_text = policy_document

        payload: dict[str, Any] = {
            "PolicyId": policy_id,
            "PolicyDocument": policy_text,
        }
        if description is not None:
            payload["Description"] = description

        request = self._request(
            cam_models.UpdatePolicyRequest,
            payload,
        )

        return self._execute(
            "UpdatePolicy",
            lambda: self._cam_client().UpdatePolicy(request),
            lambda data: {
                "policy_id": policy_id,
                "updated": True,
            },
        )

    def list_attached_role_policies(
        self,
        role_name: str,
        page: int = 1,
        rows_per_page: int = 200,
    ) -> GatewayResult:
        request = self._request(
            cam_models.ListAttachedRolePoliciesRequest,
            {
                "RoleName": role_name,
                "Page": page,
                "Rp": rows_per_page,
            },
        )

        def transform(data: dict[str, Any]) -> dict[str, Any]:
            policies = []
            for item in data.get("List") or []:
                policies.append(
                    {
                        "policy_id": item.get("PolicyId"),
                        "policy_name": item.get("PolicyName"),
                        "policy_type": item.get("PolicyType"),
                        "description": item.get("Description"),
                        "add_time": item.get("AddTime"),
                    }
                )
            return {
                "role_name": role_name,
                "total_count": data.get("TotalNum", len(policies)),
                "policies": policies,
            }

        return self._execute(
            "ListAttachedRolePolicies",
            lambda: self._cam_client().ListAttachedRolePolicies(request),
            transform,
        )

    def attach_role_policy(
        self,
        role_name: str,
        policy_id: int | None = None,
        policy_name: str | None = None,
    ) -> GatewayResult:
        if policy_id is None and not policy_name:
            return GatewayResult(
                False,
                "AttachRolePolicy",
                error_code="MissingPolicy",
                error_message="policy_id 与 policy_name 至少填写一个。",
            )

        payload: dict[str, Any] = {
            "AttachRoleName": role_name,
        }
        if policy_id is not None:
            payload["PolicyId"] = policy_id
        else:
            payload["PolicyName"] = policy_name

        request = self._request(
            cam_models.AttachRolePolicyRequest,
            payload,
        )

        return self._execute(
            "AttachRolePolicy",
            lambda: self._cam_client().AttachRolePolicy(request),
            lambda data: {
                "role_name": role_name,
                "policy_id": policy_id,
                "policy_name": policy_name,
                "attached": True,
            },
        )

    def ensure_suanqi_worker_role(
        self,
        cos_bucket: str,
        cos_region: str,
        cos_resource_owner: str,
        cos_prefix: str = "tasks/*",
        role_name: str = "SuanQiWorkerRole",
        policy_name: str = "SuanQiWorkerPolicy",
        update_existing_policy: bool = True,
    ) -> GatewayResult:
        """
        确保 SuanQi 工作角色及最小权限策略存在并已绑定。
        """
        role_created = False
        policy_created = False
        policy_updated = False
        policy_attached = False

        role_result = self.get_role(role_name)
        if not role_result.success:
            if role_result.error_code not in {
                "InvalidParameter.RoleNotExist",
                "ResourceNotFound.RoleNotExist",
                "RoleNotExist",
            }:
                return role_result

            create_role_result = self.create_role(role_name)
            if not create_role_result.success:
                return create_role_result
            role_created = True

        desired_policy = self.build_suanqi_worker_policy(
            cos_bucket=cos_bucket,
            cos_region=cos_region,
            cos_resource_owner=cos_resource_owner,
            cos_prefix=cos_prefix,
        )

        policies_result = self.list_policies(keyword=policy_name)
        if not policies_result.success:
            return policies_result

        matching_policy = next(
            (
                item
                for item in policies_result.data["policies"]
                if item.get("policy_name") == policy_name
            ),
            None,
        )

        if matching_policy is None:
            create_policy_result = self.create_policy(
                policy_name=policy_name,
                policy_document=desired_policy,
            )
            if not create_policy_result.success:
                return create_policy_result
            policy_id = int(create_policy_result.data["policy_id"])
            policy_created = True
        else:
            policy_id = int(matching_policy["policy_id"])
            if update_existing_policy:
                update_result = self.update_policy(
                    policy_id=policy_id,
                    policy_document=desired_policy,
                    description="SuanQi worker minimum permission policy",
                )
                if not update_result.success:
                    return update_result
                policy_updated = True

        attached_result = self.list_attached_role_policies(role_name)
        if not attached_result.success:
            return attached_result

        already_attached = any(
            int(item.get("policy_id") or 0) == policy_id
            for item in attached_result.data["policies"]
        )

        if not already_attached:
            attach_result = self.attach_role_policy(
                role_name=role_name,
                policy_id=policy_id,
            )
            if not attach_result.success:
                return attach_result
            policy_attached = True

        return GatewayResult(
            True,
            "EnsureSuanQiWorkerRole",
            data={
                "role_name": role_name,
                "policy_name": policy_name,
                "policy_id": policy_id,
                "role_created": role_created,
                "policy_created": policy_created,
                "policy_updated": policy_updated,
                "policy_attached": policy_attached,
                "cos_bucket": cos_bucket,
                "cos_region": cos_region,
                "cos_prefix": cos_prefix,
            },
        )


    def cos_bucket_exists(
        self,
        region: str,
        bucket: str,
    ) -> GatewayResult:
        """检查 COS Bucket 是否存在。bucket 必须是完整名称，例如 suanqi-1250000000。"""
        try:
            self._cos_client(region).head_bucket(Bucket=bucket)
            return GatewayResult(
                True,
                "COSHeadBucket",
                data={"region": region, "bucket": bucket, "exists": True},
            )
        except CosServiceError as error:
            code = getattr(error, "get_error_code", lambda: "CosServiceError")()
            status = getattr(error, "get_status_code", lambda: None)()
            if code in {"NoSuchBucket", "404"} or status == 404:
                return GatewayResult(
                    False,
                    "COSHeadBucket",
                    error_code="NoSuchBucket",
                    error_message="COS Bucket 不存在",
                    data={"region": region, "bucket": bucket, "exists": False},
                    request_id=getattr(error, "get_request_id", lambda: None)(),
                )
            return GatewayResult(
                False,
                "COSHeadBucket",
                error_code=code,
                error_message=getattr(error, "get_error_msg", lambda: str(error))(),
                request_id=getattr(error, "get_request_id", lambda: None)(),
            )
        except Exception as error:
            return GatewayResult(
                False,
                "COSHeadBucket",
                error_code=error.__class__.__name__,
                error_message=str(error),
            )

    def ensure_cos_bucket(
        self,
        region: str,
        bucket: str,
    ) -> GatewayResult:
        """确保 COS Bucket 存在；不存在则创建。"""
        exists_result = self.cos_bucket_exists(region, bucket)
        if exists_result.success:
            return GatewayResult(
                True,
                "COSEnsureBucket",
                data={"region": region, "bucket": bucket, "created": False},
            )
        if exists_result.error_code != "NoSuchBucket":
            return exists_result

        try:
            self._cos_client(region).create_bucket(Bucket=bucket)
            return GatewayResult(
                True,
                "COSEnsureBucket",
                data={"region": region, "bucket": bucket, "created": True},
            )
        except CosServiceError as error:
            code = getattr(error, "get_error_code", lambda: "CosServiceError")()
            status = getattr(error, "get_status_code", lambda: None)()
            # 并发或重复执行时，可能刚好已经创建成功。
            if code in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"} or status == 409:
                return GatewayResult(
                    True,
                    "COSEnsureBucket",
                    data={"region": region, "bucket": bucket, "created": False},
                )
            return GatewayResult(
                False,
                "COSEnsureBucket",
                error_code=code,
                error_message=getattr(error, "get_error_msg", lambda: str(error))(),
                request_id=getattr(error, "get_request_id", lambda: None)(),
            )
        except Exception as error:
            return GatewayResult(
                False,
                "COSEnsureBucket",
                error_code=error.__class__.__name__,
                error_message=str(error),
            )

    def cos_upload_object(
        self,
        region: str,
        bucket: str,
        key: str,
        local_path: str,
    ) -> GatewayResult:
        normalized_key = key.lstrip("/")
        source_path = os.path.abspath(local_path)
        if not os.path.isfile(source_path):
            return GatewayResult(
                False,
                "COSUploadObject",
                error_code="FileNotFoundError",
                error_message=f"本地文件不存在：{source_path}",
            )

        try:
            self._cos_client(region).upload_file(
                Bucket=bucket,
                Key=normalized_key,
                LocalFilePath=source_path,
            )
            return GatewayResult(
                True,
                "COSUploadObject",
                data={
                    "region": region,
                    "bucket": bucket,
                    "key": normalized_key,
                    "local_path": source_path,
                    "size": os.path.getsize(source_path),
                },
            )
        except CosServiceError as error:
            return GatewayResult(
                False,
                "COSUploadObject",
                error_code=getattr(error, "get_error_code", lambda: "CosServiceError")(),
                error_message=getattr(error, "get_error_msg", lambda: str(error))(),
                request_id=getattr(error, "get_request_id", lambda: None)(),
            )
        except Exception as error:
            return GatewayResult(
                False,
                "COSUploadObject",
                error_code=error.__class__.__name__,
                error_message=str(error),
            )

    def cos_upload_directory(
        self,
        region: str,
        bucket: str,
        prefix: str,
        local_directory: str,
    ) -> GatewayResult:
        normalized_prefix = prefix.strip().lstrip("/")
        root = os.path.abspath(local_directory)
        uploaded_files = []
        failed_files = []

        if not os.path.isdir(root):
            return GatewayResult(
                False,
                "COSUploadDirectory",
                error_code="FileNotFoundError",
                error_message=f"本地目录不存在：{root}",
            )

        for dirpath, _, filenames in os.walk(root):
            for filename in filenames:
                full_path = os.path.join(dirpath, filename)
                relative_path = os.path.relpath(full_path, root).replace(os.sep, "/")
                object_key = f"{normalized_prefix}/{relative_path}" if normalized_prefix else relative_path
                upload_result = self.cos_upload_object(region, bucket, object_key, full_path)
                if upload_result.success:
                    uploaded_files.append(upload_result.data)
                else:
                    failed_files.append(
                        {
                            "key": object_key,
                            "error_code": upload_result.error_code,
                            "error_message": upload_result.error_message,
                        }
                    )

        return GatewayResult(
            len(failed_files) == 0,
            "COSUploadDirectory",
            data={
                "region": region,
                "bucket": bucket,
                "prefix": normalized_prefix,
                "local_directory": root,
                "uploaded_files": uploaded_files,
                "failed_files": failed_files,
            },
            error_code=None if not failed_files else "PartialUploadFailure",
            error_message=None if not failed_files else f"{len(failed_files)} 个 COS 对象上传失败。",
        )

    # ------------------------------------------------------------------
    # COS 结果读取
    # ------------------------------------------------------------------

    def cos_head_object(
        self,
        region: str,
        bucket: str,
        key: str,
    ) -> GatewayResult:
        normalized_key = key.lstrip("/")

        try:
            response = self._cos_client(region).head_object(
                Bucket=bucket,
                Key=normalized_key,
            )
            return GatewayResult(
                True,
                "COSHeadObject",
                data={
                    "region": region,
                    "bucket": bucket,
                    "key": normalized_key,
                    "exists": True,
                    "etag": response.get("ETag"),
                    "content_length": int(
                        response.get("Content-Length") or 0
                    ),
                    "last_modified": response.get("Last-Modified"),
                    "content_type": response.get("Content-Type"),
                    "metadata": response,
                },
            )
        except CosServiceError as error:
            status_code = getattr(error, "get_status_code", lambda: None)()
            if status_code == 404:
                return GatewayResult(
                    True,
                    "COSHeadObject",
                    data={
                        "region": region,
                        "bucket": bucket,
                        "key": normalized_key,
                        "exists": False,
                    },
                )
            return GatewayResult(
                False,
                "COSHeadObject",
                error_code=getattr(
                    error,
                    "get_error_code",
                    lambda: "CosServiceError",
                )(),
                error_message=getattr(
                    error,
                    "get_error_msg",
                    lambda: str(error),
                )(),
                request_id=getattr(
                    error,
                    "get_request_id",
                    lambda: None,
                )(),
            )
        except Exception as error:
            return GatewayResult(
                False,
                "COSHeadObject",
                error_code=error.__class__.__name__,
                error_message=str(error),
            )

    def cos_object_exists(
        self,
        region: str,
        bucket: str,
        key: str,
    ) -> GatewayResult:
        return self.cos_head_object(region, bucket, key)

    def cos_get_object_bytes(
        self,
        region: str,
        bucket: str,
        key: str,
    ) -> GatewayResult:
        normalized_key = key.lstrip("/")

        try:
            response = self._cos_client(region).get_object(
                Bucket=bucket,
                Key=normalized_key,
            )
            body = response["Body"].get_raw_stream().read()
            return GatewayResult(
                True,
                "COSGetObject",
                data={
                    "region": region,
                    "bucket": bucket,
                    "key": normalized_key,
                    "content": body,
                    "size": len(body),
                    "etag": response.get("ETag"),
                    "content_type": response.get("Content-Type"),
                },
            )
        except CosServiceError as error:
            return GatewayResult(
                False,
                "COSGetObject",
                error_code=getattr(
                    error,
                    "get_error_code",
                    lambda: "CosServiceError",
                )(),
                error_message=getattr(
                    error,
                    "get_error_msg",
                    lambda: str(error),
                )(),
                request_id=getattr(
                    error,
                    "get_request_id",
                    lambda: None,
                )(),
            )
        except Exception as error:
            return GatewayResult(
                False,
                "COSGetObject",
                error_code=error.__class__.__name__,
                error_message=str(error),
            )

    def cos_get_json(
        self,
        region: str,
        bucket: str,
        key: str,
    ) -> GatewayResult:
        result = self.cos_get_object_bytes(region, bucket, key)
        if not result.success:
            return result

        try:
            parsed = json.loads(
                result.data["content"].decode("utf-8")
            )
        except Exception as error:
            return GatewayResult(
                False,
                "COSGetJson",
                error_code=error.__class__.__name__,
                error_message=f"COS 对象不是有效 JSON：{error}",
            )

        return GatewayResult(
            True,
            "COSGetJson",
            data={
                "region": region,
                "bucket": bucket,
                "key": key.lstrip("/"),
                "json": parsed,
            },
        )

    def cos_download_object(
        self,
        region: str,
        bucket: str,
        key: str,
        local_path: str,
    ) -> GatewayResult:
        normalized_key = key.lstrip("/")
        target_path = os.path.abspath(local_path)
        parent_directory = os.path.dirname(target_path)
        if parent_directory:
            os.makedirs(parent_directory, exist_ok=True)

        try:
            self._cos_client(region).download_file(
                Bucket=bucket,
                Key=normalized_key,
                DestFilePath=target_path,
            )
            return GatewayResult(
                True,
                "COSDownloadObject",
                data={
                    "region": region,
                    "bucket": bucket,
                    "key": normalized_key,
                    "local_path": target_path,
                    "size": os.path.getsize(target_path),
                },
            )
        except CosServiceError as error:
            return GatewayResult(
                False,
                "COSDownloadObject",
                error_code=getattr(
                    error,
                    "get_error_code",
                    lambda: "CosServiceError",
                )(),
                error_message=getattr(
                    error,
                    "get_error_msg",
                    lambda: str(error),
                )(),
                request_id=getattr(
                    error,
                    "get_request_id",
                    lambda: None,
                )(),
            )
        except Exception as error:
            return GatewayResult(
                False,
                "COSDownloadObject",
                error_code=error.__class__.__name__,
                error_message=str(error),
            )

    def cos_list_objects(
        self,
        region: str,
        bucket: str,
        prefix: str,
        maximum_objects: int = 1000,
    ) -> GatewayResult:
        normalized_prefix = prefix.lstrip("/")
        objects: list[dict[str, Any]] = []
        marker_value = ""

        try:
            client = self._cos_client(region)

            while len(objects) < maximum_objects:
                response = client.list_objects(
                    Bucket=bucket,
                    Prefix=normalized_prefix,
                    Marker=marker_value,
                    MaxKeys=min(
                        1000,
                        maximum_objects - len(objects),
                    ),
                )

                for item in response.get("Contents") or []:
                    objects.append(
                        {
                            "key": item.get("Key"),
                            "size": int(item.get("Size") or 0),
                            "etag": item.get("ETag"),
                            "last_modified": item.get("LastModified"),
                            "storage_class": item.get("StorageClass"),
                        }
                    )

                if response.get("IsTruncated") != "true":
                    break

                marker_value = response.get("NextMarker") or ""
                if not marker_value:
                    break

            return GatewayResult(
                True,
                "COSListObjects",
                data={
                    "region": region,
                    "bucket": bucket,
                    "prefix": normalized_prefix,
                    "total_count": len(objects),
                    "objects": objects,
                },
            )
        except CosServiceError as error:
            return GatewayResult(
                False,
                "COSListObjects",
                error_code=getattr(
                    error,
                    "get_error_code",
                    lambda: "CosServiceError",
                )(),
                error_message=getattr(
                    error,
                    "get_error_msg",
                    lambda: str(error),
                )(),
                request_id=getattr(
                    error,
                    "get_request_id",
                    lambda: None,
                )(),
            )
        except Exception as error:
            return GatewayResult(
                False,
                "COSListObjects",
                error_code=error.__class__.__name__,
                error_message=str(error),
            )

    def cos_download_prefix(
        self,
        region: str,
        bucket: str,
        prefix: str,
        local_directory: str,
    ) -> GatewayResult:
        normalized_prefix = prefix.strip().lstrip("/")
        list_result = self.cos_list_objects(
            region=region,
            bucket=bucket,
            prefix=normalized_prefix,
        )
        if not list_result.success:
            return list_result

        downloaded_files = []
        failed_files = []

        for item in list_result.data["objects"]:
            object_key = item["key"]
            relative_name = object_key[len(normalized_prefix):].lstrip("/")
            if not relative_name:
                continue

            local_path = os.path.join(
                local_directory,
                *relative_name.split("/"),
            )

            download_result = self.cos_download_object(
                region=region,
                bucket=bucket,
                key=object_key,
                local_path=local_path,
            )

            if download_result.success:
                downloaded_files.append(download_result.data)
            else:
                failed_files.append(
                    {
                        "key": object_key,
                        "error_code": download_result.error_code,
                        "error_message": download_result.error_message,
                    }
                )

        return GatewayResult(
            len(failed_files) == 0,
            "COSDownloadPrefix",
            data={
                "region": region,
                "bucket": bucket,
                "prefix": normalized_prefix,
                "local_directory": os.path.abspath(local_directory),
                "downloaded_files": downloaded_files,
                "failed_files": failed_files,
            },
            error_code=(
                None
                if not failed_files
                else "PartialDownloadFailure"
            ),
            error_message=(
                None
                if not failed_files
                else f"{len(failed_files)} 个 COS 对象下载失败。"
            ),
        )

    def get_task_manifest(
        self,
        region: str,
        bucket: str,
        task_id: str,
        root_prefix: str = "tasks",
    ) -> GatewayResult:
        manifest_key = (
            f"{root_prefix.strip('/')}/{task_id}/manifest.json"
        )
        return self.cos_get_json(
            region=region,
            bucket=bucket,
            key=manifest_key,
        )

    def download_task_results(
        self,
        region: str,
        bucket: str,
        task_id: str,
        local_directory: str,
        root_prefix: str = "tasks",
    ) -> GatewayResult:
        task_prefix = (
            f"{root_prefix.strip('/')}/{task_id}/"
        )
        return self.cos_download_prefix(
            region=region,
            bucket=bucket,
            prefix=task_prefix,
            local_directory=local_directory,
        )

    def check_instance_available(
            self,
            region: str,
            zone: str,
            instance_type: str,
            charge_type: str = "POSTPAID_BY_HOUR",
    ) -> GatewayResult:
        """
        查询指定地域、可用区和机型当前是否可售。

        region：
            地域代码，例如 ap-beijing。

        zone：
            可用区代码，例如 ap-beijing-7。

        instance_type：
            实例机型，例如 S5.LARGE8。

        charge_type：
            计费方式，默认按量计费。
        """

        request = self._request(
            cvm_models.DescribeZoneInstanceConfigInfosRequest,
            {
                "Filters": [
                    {
                        "Name": "zone",
                        "Values": [zone],
                    },
                    {
                        "Name": "instance-type",
                        "Values": [instance_type],
                    },
                    {
                        "Name": "instance-charge-type",
                        "Values": [charge_type],
                    },
                ]
            },
        )

        def transform(data: dict) -> dict:
            quota_items = (
                    data.get("InstanceTypeQuotaSet")
                    or []
            )

            matching_items = []

            for item in quota_items:
                if (
                        item.get("Zone") == zone
                        and item.get("InstanceType")
                        == instance_type
                ):
                    matching_items.append(
                        {
                            "zone": item.get("Zone"),
                            "instance_type": item.get(
                                "InstanceType"
                            ),
                            "instance_charge_type": item.get(
                                "InstanceChargeType"
                            ),
                            "status": item.get("Status"),
                            "cpu": item.get("Cpu"),
                            "memory_gb": item.get(
                                "Memory"
                            ),
                            "instance_family": item.get(
                                "InstanceFamily"
                            ),
                            "price": item.get("Price"),
                        }
                    )

            available_items = [
                item
                for item in matching_items
                if item.get("status") == "SELL"
            ]

            return {
                "region": region,
                "zone": zone,
                "instance_type": instance_type,
                "charge_type": charge_type,

                "available": bool(available_items),

                "status": (
                    available_items[0]["status"]
                    if available_items
                    else (
                        matching_items[0]["status"]
                        if matching_items
                        else "NOT_FOUND"
                    )
                ),

                "items": matching_items,
            }

        return self._execute(
            "DescribeZoneInstanceConfigInfos",
            lambda: self._cvm_client(
                region
            ).DescribeZoneInstanceConfigInfos(
                request
            ),
            transform,
        )
