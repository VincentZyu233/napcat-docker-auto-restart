"""
配置文件加载模块
"""
import os
import sys
import yaml
from dataclasses import dataclass
from typing import List

@dataclass
class ContainerConfig:
    """单个容器的配置"""
    name: str
    ssh_user: str
    ssh_host: str
    ws_port: int
    token: str
    enabled: bool = True
    auto_restart: bool = True
    use_sudo: bool = False          # 是否使用 sudo（如果用户在 docker 组则设为 False）
    startup_grace_s: int = 120      # 容器刚启动后的宽限期（秒），期间只观察不重启
    restart_cooldown_s: int = 300   # 两次自动重启之间的最小间隔（秒）
    max_restart_attempts: int = 3   # 连续自动重启次数上限，超过后只告警不再重启

@dataclass
class AppConfig:
    """应用配置"""
    check_interval_ms: int          # 检测间隔（毫秒）
    stagger_interval_ms: int        # 多容器心跳错开间隔（毫秒）
    containers: List[ContainerConfig]

def load_config(config_path: str = None) -> AppConfig:
    """
    加载配置文件
    
    Args:
        config_path: 配置文件路径，默认为项目根目录的 config.yaml
    
    Returns:
        AppConfig 对象
    
    Raises:
        FileNotFoundError: 配置文件不存在时
    """
    if config_path is None:
        # 默认配置文件路径：py/config.yaml
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(base_dir, "config.yaml")
    
    if not os.path.exists(config_path):
        example_path = config_path.replace("config.yaml", "config.example.yaml")
        print("=" * 60)
        print("❌ 错误: 配置文件不存在!")
        print("=" * 60)
        print(f"\n请先创建配置文件:")
        print(f"  cp {example_path} {config_path}")
        print(f"\n然后编辑 {config_path} 填入你的配置信息")
        print("=" * 60)
        sys.exit(1)
    
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    
    containers = []
    for c in data.get("containers", []):
        containers.append(ContainerConfig(
            name=c["name"],
            ssh_user=c["ssh_user"],
            ssh_host=c["ssh_host"],
            ws_port=c["ws_port"],
            token=c["token"],
            enabled=c.get("enabled", True),
            auto_restart=c.get("auto_restart", True),
            use_sudo=c.get("use_sudo", False),
            startup_grace_s=c.get("startup_grace_s", 120),
            restart_cooldown_s=c.get("restart_cooldown_s", 300),
            max_restart_attempts=c.get("max_restart_attempts", 3)
        ))
    
    return AppConfig(
        check_interval_ms=data.get("check_interval_ms", 10000),
        stagger_interval_ms=data.get("stagger_interval_ms", 500),
        containers=containers
    )
