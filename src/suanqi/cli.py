# -*- coding: utf-8 -*-

import argparse

from .providers import tencentcloud_run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="suanqi",
        description=(
            "选择云服务器、创建实例并执行计算任务。"
        ),
    )

    parser.add_argument(
        "command",
        choices=[
            "run",
        ],
        help="要执行的操作。",
    )

    parser.add_argument(
        "--provider",
        default="tencentcloud",
        choices=[
            "tencentcloud",
        ],
        help="云服务提供商。",
    )

    return parser


def main() -> int:
    parser = build_parser()
    arguments = parser.parse_args()

    if arguments.command == "run":
        if arguments.provider == "tencentcloud":
            result = tencentcloud_run()

            if not result:
                return 1

            if not result.get("success"):
                print(
                    "实例创建失败："
                    f"{result.get('error_message')}"
                )
                return 1

            print("\n实例创建成功：")
            print(
                f"实例 ID："
                f"{result.get('instance_id')}"
            )
            print(
                f"公网 IP："
                f"{result.get('public_ip')}"
            )
            print(
                f"配置："
                f"{result.get('cpu')} 核 / "
                f"{result.get('memory_gb')} GB"
            )

            # 不要打印 instance_password
            return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())