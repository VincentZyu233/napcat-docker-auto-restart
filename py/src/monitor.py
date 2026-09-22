"""
NapCat 容器状态监控模块

职责：
1. 通过 WebSocket 检测 NapCat(OneBot11) 的在线状态（按 echo 精确匹配响应）
2. 通过 docker inspect 判断容器是否在运行、本次启动了多久
3. 根据「启动宽限期 / 重启冷却 / 最大重启次数」判断是否应该自动重启

之所以需要 2、3 两条保护，是因为实测 NapCat 容器从启动到
`[OneBot] [WebSocket Server] Server Started :::3000` 需要约 40 秒，
如果启动后立刻判定「离线」并重启，会陷入「永远重启、永远起不来」的死循环。
"""
import asyncio
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import websockets

from config import AppConfig, ContainerConfig

# 深度探测（假在线）判定：连续失败达到该次数才升级为「离线」
DEEP_FAIL_THRESHOLD = 3


@dataclass
class StatusResult:
    """一次在线检测的结果"""
    online: bool
    reason: str      # online / offline / connection_refused / timeout / connection_closed / unauthorized / api_error / unknown
    message: str     # 人类可读的说明


@dataclass
class ContainerInfo:
    """docker inspect 得到的容器运行信息"""
    name: str                        # 实际使用的容器名（可能与配置不同）
    found: bool                      # 是否找到了容器
    running: bool                    # 是否处于 running 状态
    started_at: Optional[datetime]   # 本次启动时间（UTC）
    message: str = ""                # 附加提示（容器名不匹配、SSH 失败等）


@dataclass
class RestartState:
    """单个容器的自动重启节流状态"""
    attempts: int = 0                        # 连续自动重启次数（恢复在线后清零）
    last_restart_at: Optional[float] = None  # 上次自动重启时刻（time.monotonic()）
    last_notify_at: Optional[float] = None   # 上次通知时刻
    last_notify_kind: str = ""               # 上次通知的类型（用于限流）
    needs_human: bool = False                # 是否处于「需要人工介入」状态
    deep_fail_count: int = 0                 # 深度探测连续失败次数

    def reset(self):
        """恢复在线后清零（含深度探测计数）"""
        self.attempts = 0
        self.last_restart_at = None
        self.needs_human = False
        self.deep_fail_count = 0

    def reset_restart_state(self):
        """只清「重启相关」状态，保留深度探测计数（它由探测结果单独维护）"""
        self.attempts = 0
        self.last_restart_at = None
        self.needs_human = False


def parse_docker_time(value: str) -> Optional[datetime]:
    """
    解析 docker 返回的 RFC3339 时间（例如 2026-09-20T08:03:40.467627621Z）

    docker 返回的时间带 9 位纳秒，而 Python 的 fromisoformat 只支持 6 位小数，
    所以先规整再解析；解析失败返回 None。
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        result = datetime.fromisoformat(text)
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


def _docker_remote(container: ContainerConfig, remote_cmd: str, timeout: float = 20.0) -> Tuple[bool, str, str]:
    """
    通过 SSH 在宿主机上执行 docker 命令

    Args:
        container: 容器配置
        remote_cmd: docker 子命令（例如 "restart napcat-awa"）
        timeout: SSH 超时时间（秒）

    Returns:
        (是否成功, stdout, stderr)
    """
    if container.use_sudo:
        docker_cmd = "/usr/bin/sudo /usr/bin/docker"
    else:
        docker_cmd = "/usr/bin/docker"

    ssh_cmd = f'ssh {container.ssh_user}@{container.ssh_host} "{docker_cmd} {remote_cmd}"'

    try:
        result = subprocess.run(
            ssh_cmd,
            shell=True,
            capture_output=True,
            timeout=timeout,
            encoding='utf-8',
            errors='replace'
        )
    except subprocess.TimeoutExpired:
        return False, "", "SSH 命令超时"
    except Exception as e:
        return False, "", f"执行 SSH 命令失败: {e}"

    if result.returncode != 0:
        return False, result.stdout or "", result.stderr or ""
    return True, result.stdout or "", result.stderr or ""


async def _recv_action_response(websocket, echo: str, max_frames: int = 50) -> dict:
    """
    读取消息直到拿到 echo 匹配的 API 响应

    NapCat 的连接上会持续推送 lifecycle / 心跳 / 消息事件，
    如果只 recv 一次，就可能把推送事件误当成 API 响应（曾因此误判掉线并重启在线容器），
    所以这里按 echo 精确匹配。
    """
    for _ in range(max_frames):
        raw = await websocket.recv()
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("echo") == echo:
            return payload
    raise ValueError(f"连续 {max_frames} 帧都没有收到 echo={echo} 的响应")


async def _ws_action_call(
    container: ContainerConfig, action: str, params: dict, timeout: float
) -> Tuple[Optional[dict], Optional[StatusResult]]:
    """
    通过 WebSocket 调用一次 OneBot action（按 echo 精确匹配响应）

    Returns:
        (响应JSON, 错误StatusResult) —— 成功时第二个元素为 None
    """
    uri = f"ws://{container.ssh_host}:{container.ws_port}?access_token={container.token}"
    echo = f"{action}_{int(time.time() * 1000)}"

    try:
        async with asyncio.timeout(timeout):
            async with websockets.connect(uri) as websocket:
                await websocket.send(json.dumps({
                    "action": action,
                    "params": params,
                    "echo": echo,
                }))
                return await _recv_action_response(websocket, echo), None
    except asyncio.TimeoutError:
        return None, StatusResult(False, "timeout", "连接超时（NapCat 可能还在启动中，或会话已卡死）")
    except ConnectionRefusedError:
        return None, StatusResult(False, "connection_refused", "连接被拒绝，OneBot 服务还没开始监听（容器启动约需 40 秒）")
    except OSError as e:
        return None, StatusResult(False, "connection_refused", f"连接失败: {e}")
    except websockets.exceptions.WebSocketException as e:
        if type(e).__name__ == "InvalidStatus":
            return None, StatusResult(False, "unauthorized", f"鉴权失败，请检查 access_token: {e}")
        return None, StatusResult(False, "connection_closed", f"连接被关闭: {e}")
    except ValueError as e:
        return None, StatusResult(False, "api_error", f"未收到有效的 API 响应: {e}")
    except Exception as e:
        return None, StatusResult(False, "unknown", f"未知错误: {e}")


async def check_container_status(container: ContainerConfig, timeout: float = 5.0) -> StatusResult:
    """
    通过 OneBot11 的 get_status 检查 Bot 是否在线

    注意：NapCat 的 get_status.online 取自内存里的 selfInfo.online，
    在"假在线"（连接还在但服务端已不响应）时可能停留在 true，
    所以还需要 deep_probe_session() 作为第二判据。
    """
    result, error = await _ws_action_call(container, "get_status", {}, timeout)
    if error is not None:
        return error

    if result.get("status") == "ok":
        data = result.get("data") or {}
        if data.get("online"):
            return StatusResult(True, "online", "")
        return StatusResult(False, "offline", "Bot 已离线（登录态可能已失效，需要密码回退或手动扫码）")

    message = result.get("message") or result.get("wording") or "unknown"
    return StatusResult(False, "api_error", f"API 返回错误: {message}")


def classify_deep_probe(payload: dict) -> StatusResult:
    """
    纯逻辑：判定「深度探测」的结果（便于单测）

    - 正常返回 → 在线
    - NapCat 不认识该接口（老版本）→ 视为 unsupported，忽略
    - 其它错误 → 判定为会话卡死（假在线）
    """
    if payload.get("status") == "ok":
        return StatusResult(True, "online", "")

    message = str(payload.get("message") or payload.get("wording") or "")
    lowered = message.lower()
    if "不支持" in message or "not support" in lowered or "unknown action" in lowered:
        return StatusResult(True, "unsupported", "")
    return StatusResult(False, "session_stuck", f"会话异常（疑似「假在线」，发包无响应）: {message or 'unknown'}")


async def deep_probe_session(container: ContainerConfig, timeout: float = 12.0,
                             attempts: int = 2) -> StatusResult:
    """
    深度探测：调用一个「需要服务端真实往返」的只读接口，识别假在线

    用 get_cookies 取 QQ 网页 Cookie —— 它必须由服务端签发，会话失效时会超时/报错，
    而 get_status 只看内存状态，识别不出这种「假在线」。

    注意（实测）：get_cookies 存在「冷缓存」现象 —— 首次调用可能 12 秒以上，
    之后缓存生效仅 0.2 秒左右。因此这里同一轮最多重试 attempts 次，
    **任意一次成功即视为健康**，避免冷启动被误判为「假在线」。
    """
    last = StatusResult(False, "unknown", "深度探测未执行")
    for _ in range(max(1, attempts)):
        result, error = await _ws_action_call(container, "get_cookies", {"domain": "qun.qq.com"}, timeout)
        last = error if error is not None else classify_deep_probe(result)
        if last.online:
            return last
    return last


def escalate_deep_failure(state: RestartState, threshold: int = DEEP_FAIL_THRESHOLD) -> bool:
    """
    纯逻辑：累计深度探测失败次数，达到阈值时返回 True（用于「告警」）

    注意：深度探测仅用于诊断/告警，**不会**触发自动重启
    （实测 get_cookies 有冷缓存、偶发长时间无响应，据此重启会误杀健康容器）。
    """
    state.deep_fail_count += 1
    return state.deep_fail_count >= threshold


def _list_container_names(container: ContainerConfig) -> List[str]:
    """列出宿主机上的所有容器名（用于容器改名后的兜底匹配）"""
    ok, out, _ = _docker_remote(container, "ps -a --format '{{.Names}}'", timeout=15.0)
    if not ok:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def inspect_container(container: ContainerConfig) -> ContainerInfo:
    """
    查询容器是否在运行、本次启动时间

    如果配置里的容器名不存在，会尝试模糊匹配主机上的容器名
    （容器被重建/改名后仍能自动找到）。
    """
    fmt = '{{.State.Running}}|{{.State.StartedAt}}'
    ok, out, err = _docker_remote(container, f"inspect --format '{fmt}' {container.name}")

    actual_name = container.name
    note = ""
    if not ok:
        candidates = [n for n in _list_container_names(container) if n == container.name or container.name in n]
        if candidates:
            actual_name = candidates[0]
            note = f"配置的容器名 {container.name} 不存在，已自动匹配到 {actual_name}"
            ok, out, err = _docker_remote(container, f"inspect --format '{fmt}' {actual_name}")

    if not ok:
        return ContainerInfo(container.name, False, False, None, f"无法获取容器状态: {(err or out).strip()}")

    running_text, _, started_text = out.strip().partition("|")
    return ContainerInfo(
        name=actual_name,
        found=True,
        running=running_text.strip().lower() == "true",
        started_at=parse_docker_time(started_text),
        message=note,
    )


def evaluate_restart(
    container: ContainerConfig,
    state: RestartState,
    info: ContainerInfo,
    now_monotonic: Optional[float] = None,
    now_utc: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """
    纯逻辑：判断当前是否应该自动重启容器

    顺序：启动宽限期 → 重启冷却 → 连续重启熔断 → 允许重启
    (now_monotonic / now_utc 仅用于测试注入)

    Returns:
        (是否重启, 说明)
    """
    now_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
    now_utc = datetime.now(timezone.utc) if now_utc is None else now_utc

    # 1) 启动宽限期：容器刚起来，NapCat 还在启动/登录，先给它时间
    if info.found and info.running and info.started_at is not None:
        uptime = (now_utc - info.started_at).total_seconds()
        if uptime < container.startup_grace_s:
            return False, (
                f"容器 {int(uptime)} 秒前刚启动，处于启动宽限期（{container.startup_grace_s}s）内，"
                "NapCat 可能还在登录，暂不重启"
            )

    # 2) 冷却：距离上次自动重启太近
    if state.last_restart_at is not None:
        elapsed = now_monotonic - state.last_restart_at
        if elapsed < container.restart_cooldown_s:
            return False, (
                f"距上次自动重启仅 {int(elapsed)} 秒，冷却中（还需 {int(container.restart_cooldown_s - elapsed)} 秒），暂不重启"
            )

    # 3) 熔断：连续重启多次仍未恢复，说明重启解决不了问题，交给人处理
    if state.attempts >= container.max_restart_attempts:
        return False, (
            f"已连续自动重启 {state.attempts} 次仍未恢复，暂停自动重启，请人工介入"
            "（若日志出现『登录态已失效』『你的用户身份已失效』，需要配置 NAPCAT_QUICK_PASSWORD 或手动扫码）"
        )

    if not info.found:
        return True, "未找到容器，尝试重启"
    if not info.running:
        return True, "容器未在运行，尝试重启"
    return True, "满足自动重启条件"


def restart_container(container: ContainerConfig, name: Optional[str] = None) -> Tuple[bool, str]:
    """
    通过 SSH 重启容器

    Args:
        container: 容器配置
        name: 实际容器名（默认为配置里的名字）

    Returns:
        (success, message)
    """
    target = name or container.name
    ok, _, err = _docker_remote(container, f"restart {target}", timeout=30.0)
    if ok:
        return True, f"容器 {target} 重启成功"
    return False, f"容器 {target} 重启失败: {(err or '').strip()}"


# 「需要人工介入」的离线信号：这些情况下重启没有意义，只会反复喂风控
LOGIN_FAIL_SIGNALS = [
    ("KickedOffLine", "账号被顶下线（在别处登录）"),
    ("账号状态变更为离线", "账号离线"),
    ("请扫描下面的二维码", "需要扫码登录"),
    ("快速登录错误", "快速登录失败"),
    ("密码回退需要验证码", "密码回退被要求短信验证码"),
    ("用户身份已失效", "登录态/用户身份已失效"),
]


def match_login_fail_signals(log_text: str) -> List[str]:
    """纯逻辑：从容器日志里匹配「需要人工介入」的信号（便于单测）"""
    hits: List[str] = []
    for marker, desc in LOGIN_FAIL_SIGNALS:
        if marker in log_text and desc not in hits:
            hits.append(desc)
    return hits


def collect_login_fail_signals(container: ContainerConfig, window_s: int = 600) -> List[str]:
    """
    回溯容器最近 window_s 秒的日志，判断是否属于「重启也解决不了」的故障
    （被顶下线 / 登录态失效 / 需要扫码 / 需要验证码）
    """
    ok, out, _ = _docker_remote(container, f"logs --since {int(window_s)}s {container.name} 2>&1", timeout=30.0)
    if not ok:
        return []
    return match_login_fail_signals(out)


def should_notify(state: RestartState, kind: str, min_interval_s: int,
                  now_monotonic: Optional[float] = None) -> bool:
    """纯逻辑：同类通知的限流判断（便于单测）"""
    now = time.monotonic() if now_monotonic is None else now_monotonic
    if state.last_notify_kind != kind or state.last_notify_at is None:
        return True
    return (now - state.last_notify_at) >= min_interval_s


def notify(config: AppConfig, title: str, message: str) -> bool:
    """
    发送通知（可选功能）

    - notify_webhook：POST JSON {"title", "text", "message", "content", "msg"}
    - notify_telegram_bot_token + notify_telegram_chat_id：Telegram Bot API
    """
    sent = False

    if config.notify_webhook:
        payload = json.dumps({
            "title": title,
            "text": message,
            "message": message,
            "content": message,
            "msg": message,
        }).encode("utf-8")
        request = urllib.request.Request(
            config.notify_webhook, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                sent = 200 <= response.status < 300
        except Exception as e:
            log(f"发送 webhook 通知失败: {e}", "WARN")

    if config.notify_telegram_bot_token and config.notify_telegram_chat_id:
        url = f"https://api.telegram.org/bot{config.notify_telegram_bot_token}/sendMessage"
        payload = json.dumps({
            "chat_id": config.notify_telegram_chat_id,
            "text": f"{title}\n{message}",
        }).encode("utf-8")
        request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                sent = sent or (200 <= response.status < 300)
        except Exception as e:
            log(f"发送 Telegram 通知失败: {e}", "WARN")

    return sent


def notify_container_event(config: AppConfig, state: RestartState, container: ContainerConfig,
                           kind: str, title: str, message: str):
    """带限流的通知封装（未配置通知渠道时静默跳过）"""
    if not (config.notify_webhook or config.notify_telegram_bot_token):
        return
    if not should_notify(state, kind, config.notify_min_interval_s):
        return
    if notify(config, f"[{container.name}] {title}", message):
        state.last_notify_kind = kind
        state.last_notify_at = time.monotonic()
        log(f"[{container.name}] 通知已发送: {title}", "INFO")


def log(message: str, level: str = "INFO"):
    """打印带时间戳的日志"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    level_icons = {
        "INFO": "ℹ️ ",
        "WARN": "⚠️ ",
        "ERROR": "❌",
        "SUCCESS": "✅",
    }
    icon = level_icons.get(level, "")
    print(f"[{timestamp}] {icon} {message}")
