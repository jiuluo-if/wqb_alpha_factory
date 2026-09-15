"""Structured command-line grammar and legacy CLI normalization."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass

DEFAULT_PROPOSALS_PATH = ".wqb_state/proposals.json"


@dataclass(frozen=True)
class CLICommand:
    """The only command representation consumed by the runtime dispatch."""

    domain: str
    action: str
    config: str = "config.json"
    state_dir: str | None = None
    path: str = DEFAULT_PROPOSALS_PATH
    hours: float | None = None
    force_new_round: bool = False
    compact: bool = False
    json_output: bool = False
    task: str = "general"
    dry_run: bool = False
    round_value: str | int | None = None
    identifier: str | None = None
    offline: bool = False
    legacy: bool = False


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
        help="Directory for memory/trajectory state (overrides config)",
    )
    commands = parser.add_subparsers(dest="_command", required=True)

    suggest = commands.add_parser(
        "suggest",
        help="形成假设并发现真实 fields，不运行 Simulation",
    )
    _set_command(suggest, "research", "suggest")

    proposals = commands.add_parser(
        "run-proposals",
        help="沿安全路径执行 proposals 中的真实 Simulation",
    )
    proposals.add_argument(
        "path",
        nargs="?",
        default=DEFAULT_PROPOSALS_PATH,
        metavar="PATH",
        help=f"proposals JSON path (default: {DEFAULT_PROPOSALS_PATH})",
    )
    proposals.add_argument(
        "--force-new-round",
        action="store_true",
        help="保留未解决的旧 checkpoint 并开启新研究轮次",
    )
    _set_command(proposals, "research", "run-proposals")

    factory = commands.add_parser(
        "factory",
        help="兼容的有界研究编排控制面",
    )
    factory_commands = factory.add_subparsers(
        dest="_factory_command", required=True
    )
    factory_run = factory_commands.add_parser(
        "run", help="运行有界研究编排"
    )
    factory_run.add_argument(
        "--hours",
        type=float,
        default=None,
        help="运行时长（小时，默认读取配置）",
    )
    _set_command(factory_run, "factory", "run")
    factory_stop = factory_commands.add_parser(
        "stop", help="请求工厂在安全边界停止"
    )
    _set_command(factory_stop, "factory", "stop")
    factory_status = factory_commands.add_parser(
        "status", help="读取工厂会话状态"
    )
    _set_command(factory_status, "factory", "status")

    state = commands.add_parser(
        "state", help="本地状态诊断与接管检查"
    )
    state_commands = state.add_subparsers(dest="_state_command", required=True)
    state_doctor = state_commands.add_parser(
        "doctor", help="只读检查配置、状态和能力"
    )
    _add_offline(state_doctor)
    _set_command(state_doctor, "state", "doctor")
    state_audit = state_commands.add_parser(
        "audit", help="只读检查本地状态不变量"
    )
    _add_offline(state_audit)
    _set_command(state_audit, "state", "audit")
    state_preflight = state_commands.add_parser(
        "preflight", help="只读汇总 Agent 接管前阻塞项"
    )
    _add_offline(state_preflight)
    _set_command(state_preflight, "state", "preflight")

    context = commands.add_parser(
        "context", help="输出低噪声、只读的 Agent 接管上下文"
    )
    context.add_argument("--compact", action="store_true", help="压缩输出")
    context.add_argument("--json", dest="json_output", action="store_true", help="输出 JSON")
    context.add_argument(
        "--task",
        default="general",
        help="任务路由：general/state-recovery/execution/config/expression",
    )
    _add_offline(context)
    _set_command(context, "context", "show")

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
    _set_command(sync_colors, "alpha", "sync-colors")
    sync_feed = alpha_commands.add_parser(
        "sync-feed", help="只读拉取当周 Alpha 元数据"
    )
    _set_command(sync_feed, "alpha", "sync-feed")

    recovery = commands.add_parser(
        "recovery", help="人工授权的恢复与收尾动作"
    )
    recovery_commands = recovery.add_subparsers(
        dest="_recovery_command", required=True
    )
    skip_stale = recovery_commands.add_parser(
        "skip-stale", help="跳过已完成只读对账的远端作业"
    )
    skip_stale.add_argument("round_value", metavar="ROUND")
    skip_stale.add_argument("identifier", metavar="SIMULATION_ID")
    _set_command(skip_stale, "recovery", "skip-stale")
    skip_unknown = recovery_commands.add_parser(
        "skip-submit-unknown", help="人工授权跳过 SUBMIT_UNKNOWN 或无 progress URL 的 UNKNOWN"
    )
    skip_unknown.add_argument("round_value", metavar="ROUND")
    skip_unknown.add_argument("identifier", metavar="PROPOSAL_ID")
    _set_command(skip_unknown, "recovery", "skip-submit-unknown")
    finalize = recovery_commands.add_parser(
        "finalize-round", help="收尾已记录轮次，不提交 Simulation"
    )
    finalize.add_argument("round_value", type=int, metavar="ROUND")
    _set_command(finalize, "recovery", "finalize-round")
    settle_stale = recovery_commands.add_parser(
        "settle-stale-trajectory",
        help="把已关闭 checkpoint 的终态补写进 trajectory（只写本地，不重发 POST）",
    )
    settle_stale.add_argument(
        "round_value", nargs="?", default=None, metavar="ROUND",
        help="可选：只处理指定轮次",
    )
    settle_stale.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览，不写 trajectory",
    )
    _set_command(settle_stale, "recovery", "settle-stale-trajectory")

    return parser


_LEGACY_MODES = {
    "--suggest",
    "--run-proposals",
    "--factory-run",
    "--factory-stop",
    "--factory-status",
    "--doctor",
    "--audit-state",
    "--takeover-preflight",
    "--smoke-readonly",
    "--agent-context",
    "--sync-alpha-colors",
    "--sync-alpha-feed",
    "--skip-stale",
    "--skip-submit-unknown",
    "--finalize-recorded-round",
}


def _option_name(token):
    return token.split("=", 1)[0]


def _legacy_value(argv, index, option, parser, *, allow_negative=False):
    token = argv[index]
    if token.startswith(option + "="):
        value = token.split("=", 1)[1]
        if value:
            return value, index
    if index + 1 >= len(argv):
        parser.error(f"{option} requires a value")
    next_value = argv[index + 1]
    if next_value.startswith("-") and not (
        allow_negative and re.fullmatch(r"-\d+(?:\.\d+)?", next_value)
    ):
        parser.error(f"{option} requires a value")
    return next_value, index + 1


def _legacy_argv(argv, parser):
    modes = []
    global_args = []
    flags = set()
    values = {}
    leftovers = []
    index = 0
    while index < len(argv):
        token = argv[index]
        name = _option_name(token)
        if name in {"--config", "--state-dir"}:
            value, index = _legacy_value(argv, index, name, parser)
            global_args.extend([name, value])
        elif name in _LEGACY_MODES:
            if token != name and name not in {
                "--run-proposals",
                "--finalize-recorded-round",
            }:
                parser.error(f"{name} does not take an inline value")
            modes.append(name)
            if name == "--run-proposals":
                path = DEFAULT_PROPOSALS_PATH
                if token.startswith(name + "="):
                    path = token.split("=", 1)[1]
                    if not path:
                        parser.error(f"{name} requires a path")
                elif index + 1 < len(argv) and not argv[index + 1].startswith("-"):
                    path = argv[index + 1]
                    index += 1
                values["path"] = path
            elif name in {"--skip-stale", "--skip-submit-unknown"}:
                if token != name:
                    parser.error(f"{name} requires ROUND and an identifier")
                if index + 2 >= len(argv):
                    parser.error(f"{name} requires ROUND and an identifier")
                values["round_value"] = argv[index + 1]
                values["identifier"] = argv[index + 2]
                index += 2
            elif name == "--finalize-recorded-round":
                values["round_value"], index = _legacy_value(
                    argv, index, name, parser, allow_negative=True
                )
        elif token in {
            "--offline",
            "--compact",
            "--json",
            "--dry-run",
            "--force-new-round",
        }:
            flags.add(token)
        elif name in {"--factory-hours", "--task"}:
            values[name] = _legacy_value(
                argv,
                index,
                name,
                parser,
                allow_negative=name == "--factory-hours",
            )[0]
            if not token.startswith(name + "="):
                index += 1
        else:
            leftovers.append(token)
        index += 1

    if len(modes) != 1:
        if not modes:
            parser.error("未指定 legacy 研究动作")
        parser.error("legacy 语法只能选择一个研究动作")
    mode = modes[0]
    if leftovers:
        parser.error(f"unrecognized arguments: {' '.join(leftovers)}")

    if "--offline" in flags and mode not in {
        "--doctor",
        "--audit-state",
        "--takeover-preflight",
        "--agent-context",
    }:
        parser.error("--offline 只能与只读诊断动作一起使用")
    if "--dry-run" in flags and mode != "--sync-alpha-colors":
        parser.error("--dry-run 只能与 --sync-alpha-colors 一起使用")
    if "--force-new-round" in flags and mode != "--run-proposals":
        parser.error("--force-new-round 只能与 --run-proposals 一起使用")
    if "--compact" in flags or "--json" in flags or "--task" in values:
        if mode != "--agent-context":
            parser.error("context options 只能与 --agent-context 一起使用")
    if mode in {"--factory-stop", "--factory-status"} and "--factory-hours" in values:
        parser.error("工厂状态/停止动作不能携带 --factory-hours")
    if mode != "--factory-run" and "--factory-hours" in values:
        parser.error("--factory-hours 只能与 --factory-run 一起使用")

    command_args = list(global_args)
    if mode == "--suggest":
        command_args.extend(["suggest"])
    elif mode == "--run-proposals":
        command_args.extend(
            ["run-proposals", values.get("path", DEFAULT_PROPOSALS_PATH)]
        )
    elif mode == "--factory-run":
        command_args.extend(["factory", "run"])
    elif mode == "--factory-stop":
        command_args.extend(["factory", "stop"])
    elif mode == "--factory-status":
        command_args.extend(["factory", "status"])
    elif mode == "--doctor":
        command_args.extend(["state", "doctor"])
    elif mode == "--audit-state":
        command_args.extend(["state", "audit"])
    elif mode == "--takeover-preflight":
        command_args.extend(["state", "preflight"])
    elif mode == "--smoke-readonly":
        command_args.extend(["smoke"])
    elif mode == "--agent-context":
        command_args.extend(["context"])
    elif mode == "--sync-alpha-colors":
        command_args.extend(["alpha", "sync-colors"])
    elif mode == "--sync-alpha-feed":
        command_args.extend(["alpha", "sync-feed"])
    elif mode == "--skip-stale":
        command_args.extend(
            ["recovery", "skip-stale", values["round_value"], values["identifier"]]
        )
    elif mode == "--skip-submit-unknown":
        command_args.extend(
            [
                "recovery",
                "skip-submit-unknown",
                values["round_value"],
                values["identifier"],
            ]
        )
    elif mode == "--finalize-recorded-round":
        command_args.extend(["recovery", "finalize-round", values["round_value"]])

    if mode == "--factory-run" and "--factory-hours" in values:
        command_args.extend(["--hours", values["--factory-hours"]])
    if "--force-new-round" in flags:
        command_args.append("--force-new-round")
    if mode == "--agent-context":
        if "--compact" in flags:
            command_args.append("--compact")
        if "--json" in flags:
            command_args.append("--json")
        if "--task" in values:
            command_args.extend(["--task", values["--task"]])
    if mode == "--sync-alpha-colors" and "--dry-run" in flags:
        command_args.append("--dry-run")
    if "--offline" in flags:
        command_args.append("--offline")
    return command_args


def _is_legacy(argv):
    return any(_option_name(token) in _LEGACY_MODES for token in argv)


def parse_cli(argv: Sequence[str] | None = None) -> CLICommand:
    """Parse canonical commands, normalizing supported legacy forms first."""

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    legacy = _is_legacy(raw_argv)
    if legacy:
        raw_argv = _legacy_argv(raw_argv, parser)
        print(
            "Warning: legacy CLI flags are deprecated; use structured subcommands.",
            file=sys.stderr,
        )
    namespace = parser.parse_args(raw_argv)
    return CLICommand(
        domain=namespace._domain,
        action=namespace._action,
        config=namespace.config,
        state_dir=namespace.state_dir,
        path=getattr(namespace, "path", DEFAULT_PROPOSALS_PATH),
        hours=getattr(namespace, "hours", None),
        force_new_round=getattr(namespace, "force_new_round", False),
        compact=getattr(namespace, "compact", False),
        json_output=getattr(namespace, "json_output", False),
        task=getattr(namespace, "task", "general"),
        dry_run=getattr(namespace, "dry_run", False),
        round_value=getattr(namespace, "round_value", None),
        identifier=getattr(namespace, "identifier", None),
        offline=getattr(namespace, "offline", False),
        legacy=legacy,
    )
