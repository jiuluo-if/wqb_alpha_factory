"""Canonical structured command-line grammar."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class CLICommand:
    """The only command representation consumed by the runtime dispatch."""

    domain: str
    action: str
    config: str = "config.json"
    state_dir: str | None = None
    compact: bool = False
    json_output: bool = False
    task: str = "general"
    dry_run: bool = False
    offline: bool = False
    color_plan: str | None = None


def _set_command(parser, domain, action):
    parser.set_defaults(_domain=domain, _action=action)


def _add_offline(parser):
    parser.add_argument(
        "--offline",
        action="store_true",
        help=argparse.SUPPRESS,
    )


def build_parser():
    parser = argparse.ArgumentParser(
        description="面向 WorldQuant BRAIN 的 Alpha 研究 Agent"
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to config JSON (default: config.json)",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="Directory for ExecutionGuard and rebuildable cache (overrides config)",
    )
    commands = parser.add_subparsers(dest="_command", required=True)

    suggest = commands.add_parser(
        "suggest",
        help="形成假设并发现真实 fields，不运行 Simulation",
    )
    _set_command(suggest, "research", "suggest")

    diagnostics = commands.add_parser(
        "diagnostics", help="ExecutionGuard 与远端缓存的只读诊断"
    )
    diagnostic_commands = diagnostics.add_subparsers(dest="_diagnostic_command", required=True)
    doctor = diagnostic_commands.add_parser(
        "doctor", help="离线检查配置、ExecutionGuard 和缓存"
    )
    _add_offline(doctor)
    _set_command(doctor, "diagnostics", "doctor")
    audit = diagnostic_commands.add_parser(
        "audit", help="离线检查 ExecutionGuard 不变量"
    )
    _add_offline(audit)
    _set_command(audit, "diagnostics", "audit")

    smoke = commands.add_parser(
        "smoke", help="执行只读平台 smoke 检查"
    )
    _set_command(smoke, "smoke", "readonly")

    alpha = commands.add_parser(
        "alpha", help="Alpha 轻量元数据维护"
    )
    alpha_commands = alpha.add_subparsers(dest="_alpha_command", required=True)
    sync_colors = alpha_commands.add_parser(
        "sync-colors", help="同步 Alpha 顶层 color 元数据"
    )
    sync_colors.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览，不发送 PATCH",
    )
    sync_colors.add_argument(
        "--plan",
        required=True,
        dest="color_plan",
        help="已人工 review 的 preview_alpha_colors JSON plan",
    )
    _set_command(sync_colors, "alpha", "sync-colors")
    sync_feed = alpha_commands.add_parser(
        "sync-feed", help="只读拉取当周 Alpha 元数据"
    )
    _set_command(sync_feed, "alpha", "sync-feed")

    return parser


def parse_cli(argv: Sequence[str] | None = None) -> CLICommand:
    """Parse the canonical structured command grammar."""

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    namespace = parser.parse_args(raw_argv)
    return CLICommand(
        domain=namespace._domain,
        action=namespace._action,
        config=namespace.config,
        state_dir=namespace.state_dir,
        compact=getattr(namespace, "compact", False),
        json_output=getattr(namespace, "json_output", False),
        task=getattr(namespace, "task", "general"),
        dry_run=getattr(namespace, "dry_run", False),
        offline=getattr(namespace, "offline", False),
        color_plan=getattr(namespace, "color_plan", None),
    )
