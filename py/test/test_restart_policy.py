"""
重启策略与状态检测的单元测试（不需要真实服务器）

运行：
    cd py
    python test/test_restart_policy.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from config import ContainerConfig, load_config  # noqa: E402
from monitor import (  # noqa: E402
    ContainerInfo,
    RestartState,
    _recv_action_response,
    classify_deep_probe,
    escalate_deep_failure,
    evaluate_restart,
    match_login_fail_signals,
    parse_docker_time,
    should_notify,
)

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = ""):
    """记录一条断言结果"""
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name} {detail}")


def make_container(**kwargs) -> ContainerConfig:
    """构造一个用于测试的容器配置"""
    params = dict(
        name="napcat-test",
        ssh_user="root",
        ssh_host="127.0.0.1",
        ws_port=3000,
        token="test",
    )
    params.update(kwargs)
    return ContainerConfig(**params)


def test_parse_docker_time():
    print("parse_docker_time")
    dt = parse_docker_time("2026-09-20T08:03:40.467627621Z")
    check("9 位纳秒 + Z 可以解析", dt is not None, f"实际: {dt}")
    check(
        "解析结果正确",
        dt == datetime(2026, 9, 20, 8, 3, 40, 467627, tzinfo=timezone.utc),
        f"实际: {dt}",
    )
    check(
        "无小数秒可以解析",
        parse_docker_time("2026-09-20T08:03:40+00:00") == datetime(2026, 9, 20, 8, 3, 40, tzinfo=timezone.utc),
    )
    check("空字符串返回 None", parse_docker_time("") is None)
    check("非法字符串返回 None", parse_docker_time("not-a-time") is None)


def test_evaluate_restart():
    print("evaluate_restart")
    container = make_container(startup_grace_s=120, restart_cooldown_s=300, max_restart_attempts=3)
    now = datetime(2026, 9, 20, 8, 0, 0, tzinfo=timezone.utc)

    # 1) 启动宽限期内（30 秒前刚启动）→ 不重启
    info = ContainerInfo("napcat-test", True, True, now - timedelta(seconds=30))
    should, why = evaluate_restart(container, RestartState(), info, now_monotonic=1000.0, now_utc=now)
    check("宽限期内不重启", should is False and "启动宽限期" in why, f"实际: {should} / {why}")

    # 2) 宽限期外、无历史重启 → 允许重启
    info = ContainerInfo("napcat-test", True, True, now - timedelta(seconds=200))
    should, why = evaluate_restart(container, RestartState(), info, now_monotonic=1000.0, now_utc=now)
    check("宽限期外允许重启", should is True, f"实际: {should} / {why}")

    # 3) 冷却中（100 秒前刚重启过）→ 不重启
    state = RestartState(attempts=1, last_restart_at=900.0)
    should, why = evaluate_restart(container, state, info, now_monotonic=1000.0, now_utc=now)
    check("冷却期内不重启", should is False and "冷却中" in why, f"实际: {should} / {why}")

    # 4) 冷却结束 → 允许重启
    should, why = evaluate_restart(container, state, info, now_monotonic=1300.0, now_utc=now)
    check("冷却结束允许重启", should is True, f"实际: {should} / {why}")

    # 5) 连续重启达到上限 → 熔断（只告警）
    state = RestartState(attempts=3, last_restart_at=0.0)
    should, why = evaluate_restart(container, state, info, now_monotonic=10000.0, now_utc=now)
    check("达到最大次数后熔断", should is False and "暂停自动重启" in why, f"实际: {should} / {why}")

    # 6) 恢复在线后 reset 清零
    state.reset()
    check("reset 后计数清零", state.attempts == 0 and state.last_restart_at is None)

    # 7) 容器已停止（StartedAt 很新也不行，因为容器没在运行）→ 允许重启
    info = ContainerInfo("napcat-test", True, False, now - timedelta(seconds=5))
    should, why = evaluate_restart(container, RestartState(), info, now_monotonic=1000.0, now_utc=now)
    check("容器已停止时允许重启", should is True, f"实际: {should} / {why}")

    # 8) 找不到容器 → 允许重启
    info = ContainerInfo("napcat-test", False, False, None)
    should, why = evaluate_restart(container, RestartState(), info, now_monotonic=1000.0, now_utc=now)
    check("容器不存在时允许重启", should is True, f"实际: {should} / {why}")


def test_deep_probe_and_signals():
    print("classify_deep_probe / match_login_fail_signals / should_notify")
    # 深度探测：正常返回 → 在线
    r = classify_deep_probe({"status": "ok", "data": {}})
    check("深度探测正常→在线", r.online and r.reason == "online", f"实际: {r}")

    # 老版本 NapCat 不认识该接口 → 忽略该判据（不应误判为掉线）
    r = classify_deep_probe({"status": "failed", "message": "不支持的 API"})
    check("接口不支持→忽略（视为在线）", r.online and r.reason == "unsupported", f"实际: {r}")
    r = classify_deep_probe({"status": "failed", "message": "unknown action: get_cookies"})
    check("unknown action→忽略", r.online and r.reason == "unsupported", f"实际: {r}")

    # 其它错误 → 判定为「假在线」
    r = classify_deep_probe({"status": "failed", "message": "请求超时"})
    check("其它错误→判定会话卡死", (not r.online) and r.reason == "session_stuck", f"实际: {r}")

    # 容器日志信号匹配
    kk = "[KickedOffLine] [下线通知] 你的账号当前登录已失效，请重新登录。"
    qr = "请扫描下面的二维码，然后在手Q上授权登录："
    hits = match_login_fail_signals(kk + "\n" + qr)
    check("能识别『被顶下线』", any("顶下线" in h for h in hits), f"实际: {hits}")
    check("能识别『需要扫码』", any("扫码" in h for h in hits), f"实际: {hits}")
    check("正常日志不误报", match_login_fail_signals("15:00 [info] 接收 <- 群聊 [测试群]") == [])

    # 通知限流
    st = RestartState()
    check("首次可通知", should_notify(st, "needs_human", 1800, now_monotonic=100.0) is True)
    st.last_notify_kind = "needs_human"
    st.last_notify_at = 100.0
    check("同类未到间隔→不通知", should_notify(st, "needs_human", 1800, now_monotonic=200.0) is False)
    check("同类超过间隔→通知", should_notify(st, "needs_human", 1800, now_monotonic=2000.0) is True)
    check("不同类型→立即通知", should_notify(st, "breaker", 1800, now_monotonic=101.0) is True)

    # 深度探测失败升级：连续 2 次才判死（避免冷启动/抖动误判）
    st = RestartState()
    check("第 1 次深度探测失败→不判死", escalate_deep_failure(st, 2) is False)
    check("第 2 次深度探测失败→判死", escalate_deep_failure(st, 2) is True)
    st.reset()
    check("reset 后深度失败计数清零", st.deep_fail_count == 0)


class FakeWebSocket:
    """按顺序吐出预设帧的假 WebSocket，用于测试响应匹配逻辑"""

    def __init__(self, frames):
        self.frames = list(frames)

    async def recv(self):
        if not self.frames:
            raise AssertionError("没有更多帧了")
        return self.frames.pop(0)


def test_recv_action_response():
    print("_recv_action_response")
    frames = [
        '{"time":1,"post_type":"meta_event","meta_event_type":"lifecycle","sub_type":"connect"}',
        '{"post_type":"message","message":[{"type":"text","data":{"text":"wwxs"}}],"raw_message":"wwxs"}',
        '{"status":"ok","retcode":0,"data":{"online":true,"good":true},"echo":"status_1"}',
    ]
    result = asyncio.run(_recv_action_response(FakeWebSocket(frames), "status_1"))
    check(
        "能跳过推送事件拿到 API 响应",
        result.get("status") == "ok" and result["data"]["online"] is True,
        f"实际: {result}",
    )

    try:
        asyncio.run(_recv_action_response(
            FakeWebSocket(['{"post_type":"message","message":[]}'] * 3), "status_2", max_frames=3
        ))
        check("拿不到响应时应报错", False, "未抛出异常")
    except ValueError:
        check("拿不到响应时应报错", True)


def test_config_defaults():
    print("load_config")
    yaml_text = """
check_interval_ms: 10000
stagger_interval_ms: 500
containers:
  - name: c1
    ssh_user: root
    ssh_host: 1.2.3.4
    ws_port: 3000
    token: t
  - name: c2
    ssh_user: root
    ssh_host: 1.2.3.4
    ws_port: 3001
    token: t
    startup_grace_s: 30
    restart_cooldown_s: 60
    max_restart_attempts: 1
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(yaml_text)
        path = f.name

    try:
        config = load_config(path)
        c1, c2 = config.containers
        check("默认 startup_grace_s=120", c1.startup_grace_s == 120, f"实际: {c1.startup_grace_s}")
        check("默认 restart_cooldown_s=300", c1.restart_cooldown_s == 300, f"实际: {c1.restart_cooldown_s}")
        check("默认 max_restart_attempts=3", c1.max_restart_attempts == 3, f"实际: {c1.max_restart_attempts}")
        check(
            "三个新参数可被配置覆盖",
            (c2.startup_grace_s, c2.restart_cooldown_s, c2.max_restart_attempts) == (30, 60, 1),
            f"实际: {c2.startup_grace_s} / {c2.restart_cooldown_s} / {c2.max_restart_attempts}",
        )
        check(
            "原有字段保持兼容",
            config.check_interval_ms == 10000 and c1.token == "t" and c1.auto_restart is True and c1.use_sudo is False,
        )
        check("默认 deep_probe=True", c1.deep_probe is True, f"实际: {c1.deep_probe}")
        check("默认 confirm_delay_ms=3000", config.confirm_delay_ms == 3000, f"实际: {config.confirm_delay_ms}")
        check("默认 offline_log_window_s=600", config.offline_log_window_s == 600)
        check("默认未配置通知渠道", config.notify_webhook == "" and config.notify_telegram_bot_token == "")
    finally:
        os.remove(path)


if __name__ == "__main__":
    print("=" * 50)
    print("  napcat-docker-auto-restart 单元测试")
    print("=" * 50)
    test_parse_docker_time()
    test_evaluate_restart()
    test_deep_probe_and_signals()
    test_recv_action_response()
    test_config_defaults()
    print("-" * 50)
    print(f"结果: {PASSED} 通过 / {FAILED} 失败")
    sys.exit(1 if FAILED else 0)
