"""
NapCat Docker 自动重启监控主程序

功能：
- 定时检测 NapCat 容器的在线状态
- 如果检测到离线，按「启动宽限期 → 重启冷却 → 连续重启熔断」的策略自动重启容器
"""
import asyncio
import sys
import time

from config import load_config, AppConfig, ContainerConfig
from monitor import (
    RestartState,
    check_container_status,
    evaluate_restart,
    inspect_container,
    restart_container,
    log,
)


async def monitor_container(container: ContainerConfig, state: RestartState):
    """
    监控单个容器

    Args:
        container: 容器配置
        state: 该容器的自动重启节流状态
    """
    result = await check_container_status(container)

    if result.online:
        if state.attempts > 0:
            log(f"[{container.name}] 已恢复在线 ✓（此前自动重启 {state.attempts} 次）", "SUCCESS")
        else:
            log(f"[{container.name}] 在线 ✓", "SUCCESS")
        state.reset()
        return

    log(f"[{container.name}] 离线! 原因: {result.message}", "ERROR")

    if not container.auto_restart:
        return

    # 重启前先看容器本身的状态（是否在运行、启动了多久）
    info = await asyncio.to_thread(inspect_container, container)
    if info.message:
        log(f"[{container.name}] {info.message}", "WARN")

    should_restart, reason = evaluate_restart(container, state, info)
    if not should_restart:
        log(f"[{container.name}] 跳过自动重启: {reason}", "WARN")
        return

    log(f"[{container.name}] 正在尝试重启容器 {info.name}（{reason}）...", "WARN")
    success, msg = await asyncio.to_thread(restart_container, container, info.name)

    # 无论成功与否都记账，避免 SSH 失败时被高频重试
    state.attempts += 1
    state.last_restart_at = time.monotonic()

    log(f"[{container.name}] {msg}", "SUCCESS" if success else "ERROR")


async def run_monitor(config: AppConfig):
    """
    运行监控循环

    Args:
        config: 应用配置
    """
    log(f"启动监控，检测间隔: {config.check_interval_ms}ms")
    log(f"容器心跳错开间隔: {config.stagger_interval_ms}ms")
    log(f"监控容器数量: {len(config.containers)}")

    for c in config.containers:
        status_str = "启用" if c.enabled else "禁用"
        log(
            f"  - {c.name} ({status_str}) @ {c.ssh_host}:{c.ws_port} "
            f"[宽限 {c.startup_grace_s}s / 冷却 {c.restart_cooldown_s}s / 最多 {c.max_restart_attempts} 次]"
        )

    print("-" * 50)

    # 每个容器一份重启节流状态
    states = {c.name: RestartState() for c in config.containers}

    while True:
        # 依次检测每个容器，错开间隔
        # 过滤掉未启用的容器
        enabled_containers = [c for c in config.containers if c.enabled]

        for i, container in enumerate(enabled_containers):
            await monitor_container(container, states[container.name])
            # 如果不是最后一个容器，等待错开间隔
            if i < len(enabled_containers) - 1:
                await asyncio.sleep(config.stagger_interval_ms / 1000)

        print("-" * 50)

        # 等待下一次检测周期
        await asyncio.sleep(config.check_interval_ms / 1000)


def main():
    """主函数"""
    print("=" * 50)
    print("  NapCat Docker 自动重启监控")
    print("=" * 50)

    # 加载配置
    config = load_config()

    if not config.containers:
        log("配置文件中没有容器配置!", "ERROR")
        sys.exit(1)

    # 运行监控
    try:
        asyncio.run(run_monitor(config))
    except KeyboardInterrupt:
        print()  # 换行，让输出更整洁
        log("收到退出信号，监控已停止", "WARN")


if __name__ == "__main__":
    main()
