# -*- coding: utf-8 -*-


DEFAULT_CPU = 16        # 用户未指定时，默认要求至少 16 核 CPU
DEFAULT_MEMORY_GB = 16  # 用户未指定时，默认要求至少 16GB 内存


def resolve_resource_requirements(
    cpu: int | None,
    memory_gb: int | None,
) -> tuple[int, int]:
    """
    根据用户输入计算最终的 CPU 和内存最低要求。

    规则：
    1. 都不指定：CPU=16，内存=16GB
    2. 只指定 CPU：内存最低值与 CPU 相同
    3. 只指定内存：CPU 最低值与内存相同
    4. 两个都指定：分别使用用户输入值
    """

    if cpu is not None and cpu <= 0:
        raise ValueError("CPU 核心数必须大于 0")

    if memory_gb is not None and memory_gb <= 0:
        raise ValueError("内存大小必须大于 0")

    if cpu is None and memory_gb is None:
        # 两个参数都没有指定，使用默认值
        final_cpu = DEFAULT_CPU
        final_memory_gb = DEFAULT_MEMORY_GB

    elif cpu is not None and memory_gb is None:
        # 只指定了 CPU，内存最低值与 CPU 数值相同
        final_cpu = cpu
        final_memory_gb = cpu

    elif cpu is None and memory_gb is not None:
        # 只指定了内存，CPU 最低值与内存数值相同
        final_cpu = memory_gb
        final_memory_gb = memory_gb

    else:
        # CPU 和内存都指定了
        final_cpu = cpu
        final_memory_gb = memory_gb

    return final_cpu, final_memory_gb