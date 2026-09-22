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
    DEEP_FAIL_THRESHOLD,
    RestartState,
    check_container_status,
    collect_login_fail_signals,
    deep_probe_session,
    escalate_deep_failure,
    evaluate_restart,
    inspect_container,
    notify_container_event,
    restart_container,
    log,
)


async def monitor_container(container: ContainerConfig, state: RestartState, config: AppConfig):
    """
    监控单个容器

    Args:
        container: 容器配置
        state: 该容器的自动重启节流状态
        config: 应用配置（用于通知与复检参数）
    """
    # 1) 基础探测：OneBot get_status
    result = await check_container_status(container)

    # 2) 深度探测：识别「假在线」（get_status 说在线，但服务端已经不响应）
    if result.online and container.deep_probe:
        deep = await deep_probe_session(container)
        if deep.online:
            state.deep_fail_count = 0
            if deep.reason == "unsupported":
                log(f"[{container.name}] 深度探测接口不被当前 NapCat 版本支持，跳过该判据", "INFO")
        else:
            # 深度探测只做「诊断」：失败只告警、绝不触发自动重启
            # （实测 get_cookies 存在冷缓存/偶发长时间无响应，若据此重启会误杀健康容器）
            escalated = escalate_deep_failure(state)
            log(f"[{container.name}] 深度探测失败({state.deep_fail_count}/{DEEP_FAIL_THRESHOLD} 次): {deep.message}", "WARN")
            if escalated:
                log(
                    f"[{container.name}] 深度探测连续失败 {DEEP_FAIL_THRESHOLD} 次，会话可能异常"
                    "（仅告警，不自动重启；如频繁出现可把该容器的 deep_probe 设为 false）",
                    "ERROR",
                )
                notify_container_event(
                    config, state, container, "deep_probe", "会话疑似异常（仅告警）",
                    f"{deep.message}\nget_status 仍显示在线，本项不会触发重启。\n"
                    "若频繁出现：可用 WebUI 检查登录状态，或把该容器的 deep_probe 设为 false"
                )

    if result.online:
        if state.attempts > 0 or state.needs_human:
            log(f"[{container.name}] 已恢复在线 ✓（此前自动重启 {state.attempts} 次）", "SUCCESS")
            notify_container_event(config, state, container, "recovered", "已恢复在线",
                                   "Bot 已恢复正常在线状态")
        else:
            log(f"[{container.name}] 在线 ✓", "SUCCESS")
        # 只清重启相关状态，保留深度探测计数（该计数由深度探测结果单独维护）
        state.reset_restart_state()
        return

    log(f"[{container.name}] 离线! 原因: {result.message}", "ERROR")

    # 3) 复检防抖：过滤瞬时抖动，避免误判导致的误重启
    if config.confirm_delay_ms > 0:
        await asyncio.sleep(config.confirm_delay_ms / 1000)
        confirm = await check_container_status(container)
        if confirm.online:
            log(f"[{container.name}] 复检已在线，判定为瞬时抖动，跳过本次处理", "WARN")
            state.reset()
            return
        result = confirm

    # 4) 查看容器本身状态（是否在运行、启动了多久）
    info = await asyncio.to_thread(inspect_container, container)
    if info.message:
        log(f"[{container.name}] {info.message}", "WARN")

    # 5) 识别「重启也解决不了」的故障（被顶下线 / 登录态失效 / 需要扫码 / 需要验证码）
    signals = await asyncio.to_thread(collect_login_fail_signals, container, config.offline_log_window_s)
    if signals:
        log(f"[{container.name}] 检测到需要人工介入的信号: {'、'.join(signals)}", "WARN")
        log(f"[{container.name}] 这类故障重启无效（只会反复触发风控），已跳过自动重启", "WARN")
        state.needs_human = True
        notify_container_event(
            config, state, container, "needs_human", "需要人工介入",
            f"信号: {'、'.join(signals)}\n探测原因: {result.message}\n"
            "建议: 打开 WebUI 扫码或完成短信验证；若为『被顶下线』请检查该 QQ 号是否在别处登录"
        )
        return

    if not container.auto_restart:
        log(f"[{container.name}] 已禁用自动重启，仅记录日志", "INFO")
        return

    # 6) 按「启动宽限期 → 重启冷却 → 连续重启熔断」决定是否重启
    should_restart, reason = evaluate_restart(container, state, info)
    if not should_restart:
        log(f"[{container.name}] 跳过自动重启: {reason}", "WARN")
        if "暂停自动重启" in reason:
            notify_container_event(config, state, container, "breaker", "已触发重启熔断",
                                   f"{reason}\n探测原因: {result.message}")
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
    log(f"离线复检等待: {config.confirm_delay_ms}ms / 人工介入信号回溯: {config.offline_log_window_s}s")
    notify_channels = []
    if config.notify_webhook:
        notify_channels.append("webhook")
    if config.notify_telegram_bot_token and config.notify_telegram_chat_id:
        notify_channels.append("telegram")
    log(f"通知渠道: {'、'.join(notify_channels) if notify_channels else '未配置（仅打印日志）'}")
    log(f"监控容器数量: {len(config.containers)}")

    for c in config.containers:
        status_str = "启用" if c.enabled else "禁用"
        probe_str = "开" if c.deep_probe else "关"
        log(
            f"  - {c.name} ({status_str}) @ {c.ssh_host}:{c.ws_port} "
            f"[宽限 {c.startup_grace_s}s / 冷却 {c.restart_cooldown_s}s / 最多 {c.max_restart_attempts} 次 / 深度探测 {probe_str}]"
        )

    print("-" * 50)

    # 每个容器一份重启节流状态
    states = {c.name: RestartState() for c in config.containers}

    while True:
        # 依次检测每个容器，错开间隔
        # 过滤掉未启用的容器
        enabled_containers = [c for c in config.containers if c.enabled]

        for i, container in enumerate(enabled_containers):
            await monitor_container(container, states[container.name], config)
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
