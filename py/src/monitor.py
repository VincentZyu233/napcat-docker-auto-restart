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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import websockets

from config import ContainerConfig


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

    def reset(self):
        """恢复在线后清零"""
        self.attempts = 0
        self.last_restart_at = None


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


async def check_container_status(container: ContainerConfig, timeout: float = 5.0) -> StatusResult:
    """
    通过 OneBot11 WebSocket 检查 Bot 是否在线

    Args:
        container: 容器配置
        timeout: 超时时间（秒）

    Returns:
        StatusResult
    """
    uri = f"ws://{container.ssh_host}:{container.ws_port}?access_token={container.token}"
    echo = f"status_{int(time.time() * 1000)}"

    try:
        async with asyncio.timeout(timeout):
            async with websockets.connect(uri) as websocket:
                await websocket.send(json.dumps({
                    "action": "get_status",
                    "params": {},
                    "echo": echo,
                }))
                result = await _recv_action_response(websocket, echo)
    except asyncio.TimeoutError:
        return StatusResult(False, "timeout", "连接超时（NapCat 可能还在启动中）")
    except ConnectionRefusedError:
        return StatusResult(False, "connection_refused", "连接被拒绝，OneBot 服务还没开始监听（容器启动约需 40 秒）")
    except OSError as e:
        return StatusResult(False, "connection_refused", f"连接失败: {e}")
    except websockets.exceptions.WebSocketException as e:
        if type(e).__name__ == "InvalidStatus":
            return StatusResult(False, "unauthorized", f"鉴权失败，请检查 access_token: {e}")
        return StatusResult(False, "connection_closed", f"连接被关闭: {e}")
    except ValueError as e:
        return StatusResult(False, "api_error", f"未收到有效的 API 响应: {e}")
    except Exception as e:
        return StatusResult(False, "unknown", f"未知错误: {e}")

    if result.get("status") == "ok":
        data = result.get("data") or {}
        if data.get("online"):
            return StatusResult(True, "online", "")
        return StatusResult(False, "offline", "Bot 已离线（登录态可能已失效，需要密码回退或手动扫码）")

    message = result.get("message") or result.get("wording") or "unknown"
    return StatusResult(False, "api_error", f"API 返回错误: {message}")


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
