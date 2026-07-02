"""命令行入口

启动 Web 管理界面（同时后台跑监控循环）。

Usage:
    python -m stock_monitor [--host 127.0.0.1] [--port 8765] [--interval 30] [--config PATH]
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import uvicorn

from .config import default_config_path

logger = logging.getLogger(__name__)


def _detect_lan_ip() -> str:
    """探测本机局域网 IP：优先用平台原生命令，回退到 UDP socket 技巧"""
    import socket
    import subprocess

    # macOS / BSD 优先用 ipconfig
    if sys.platform == "darwin":
        for iface in ("en0", "en1", "en2"):
            try:
                r = subprocess.run(["ipconfig", "getifaddr", iface],
                                   capture_output=True, text=True, timeout=2)
                ip = r.stdout.strip()
                if ip and not ip.startswith("127."):
                    return ip
            except Exception:
                pass
    # Linux
    if sys.platform.startswith("linux"):
        try:
            r = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2)
            for ip in r.stdout.split():
                if not ip.startswith("127."):
                    return ip
        except Exception:
            pass
    # 通用回退：UDP socket（不真正发包）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stock-monitor", description="股票监控 Web 管理界面")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0 局域网可访问）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--interval", type=int, default=30, help="监控检查间隔（秒）")
    parser.add_argument("--config", type=Path, default=None, help=f"配置文件路径（默认 {default_config_path()}）")
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    args = parser.parse_args(argv)

    # 配置 logging
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # 注入配置路径到 app 工厂
    from .webui.app import create_app
    app = create_app(config_path=args.config, interval_seconds=args.interval)

    # 取本机局域网 IP（仅 0.0.0.0 绑定时提示）
    lan_ip = ""
    if args.host in ("0.0.0.0", ""):
        lan_ip = _detect_lan_ip() or ""

    print("=" * 60)
    print(f"📈  Stock Monitor Admin")
    print(f"    本机访问: http://127.0.0.1:{args.port}")
    if lan_ip:
        print(f"    局域网:   http://{lan_ip}:{args.port}")
    print(f"    配置:     {args.config or default_config_path()}")
    print(f"    检查间隔: {args.interval}s")
    if args.host in ("0.0.0.0", ""):
        print()
        print("    ⚠️  服务绑定到 0.0.0.0，局域网内任何设备都能访问。")
        print("       当前未启用鉴权，请确保网络环境可信。")
    print("=" * 60)

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
