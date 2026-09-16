import json
import os
import sys

from wqb_agent.cli import parse_cli
from wqb_agent.locking import acquire_single_instance_lock, release_single_instance_lock


def load_config(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        print(f"Config file '{path}' is unreadable or not valid JSON: {exc}")
        sys.exit(1)


def main(argv=None):
    command = parse_cli(argv)
    command_key = (command.domain, command.action)
    readonly_local = command_key in {
        ("state", "doctor"),
        ("state", "audit"),
        ("state", "preflight"),
        ("context", "show"),
    }

    config = None
    # Read-only diagnostics are intentionally runnable on a fresh checkout:
    # they use the checked-in example as a schema-safe fallback, while all
    # production actions still require the user-created config.json.
    if readonly_local and not os.path.exists(command.config):
        example = os.path.join(os.path.dirname(__file__), "config.example.json")
        config = load_config(example)
    else:
        config = load_config(command.config)
    if config is None:
        example = os.path.join(os.path.dirname(__file__), "config.example.json")
        print(
            f"Config file '{command.config}' not found. "
            f"Copy {example} to {command.config} and edit it."
        )
        sys.exit(1)

    # Validate raw JSON once before any client construction.  All subsequent
    # commands consume the typed AppConfig boundary and are not reparsed.
    from wqb_agent.config import apply_cli_overrides, normalize_config
    try:
        typed_config = normalize_config(config)
        typed_config = apply_cli_overrides(
            typed_config, state_dir=command.state_dir
        )
    except (TypeError, ValueError) as exc:
        print(f"配置无效: {exc}")
        sys.exit(1)

    if command_key == ("state", "doctor"):
        from wqb_agent.doctor import run_doctor
        print(json.dumps(run_doctor(typed_config, offline=True), ensure_ascii=False, indent=2))
        return
    if command_key == ("state", "audit"):
        from wqb_agent.audit import audit_state
        result = audit_state(
            typed_config.runtime.state_dir,
            lifecycle_persistent=True,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result.get("ok"):
            sys.exit(2)
        return
    if command_key == ("state", "preflight"):
        from wqb_agent.preflight import run_takeover_preflight
        result = run_takeover_preflight(typed_config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("status") != "READY":
            sys.exit(2)
        return
    if command_key == ("context", "show"):
        from wqb_agent.preflight import build_agent_context, render_agent_context
        context = build_agent_context(typed_config, task=command.task)
        print(render_agent_context(
            context,
            compact=command.compact,
            json_mode=command.json_output,
        ))
        return
    if command_key == ("smoke", "readonly"):
        from wqb_agent import WQBClient
        from wqb_agent.smoke import run_readonly_smoke
        try:
            client = WQBClient()
            print(json.dumps(run_readonly_smoke(client, typed_config), ensure_ascii=False, indent=2))
        except Exception as exc:
            print(json.dumps({"network_write": False, "status": "UNAVAILABLE", "reason": str(exc)}, ensure_ascii=False, indent=2))
            sys.exit(1)
        return

    if command_key == ("alpha", "sync-colors"):
        from wqb_agent import WQBClient, research_api

        state_dir = typed_config.runtime.state_dir
        lock_path = acquire_single_instance_lock(
            state_dir, operation="sync-alpha-colors"
        )
        if lock_path is None:
            sys.exit(1)
        try:
            client = WQBClient()
            research_api.refresh_remote_alphas(
                client=client, config=typed_config, state_dir=state_dir,
            )
            candidates = research_api.list_remote_alphas(
                config=typed_config, state_dir=state_dir,
            )
            changes = research_api.sync_alpha_colors(
                client=client, config=typed_config, state_dir=state_dir,
                dry_run=command.dry_run,
            )
            print(json.dumps({
                "dry_run": command.dry_run,
                "candidate_count": len(candidates),
                "change_count": len(changes),
                "network_write": not command.dry_run,
                "changes": changes,
            }, ensure_ascii=False, indent=2))
        except Exception as exc:
            print(json.dumps({
                "dry_run": command.dry_run,
                "network_write": not command.dry_run,
                "status": "FAILED",
                "reason": str(exc),
            }, ensure_ascii=False, indent=2))
            sys.exit(1)
        finally:
            release_single_instance_lock(lock_path)
        return

    if command_key in {("factory", "stop"), ("factory", "status")}:
        from wqb_agent.factory_runner import AIFactoryRunner

        state_dir = typed_config.runtime.state_dir
        if command_key == ("factory", "stop"):
            session = AIFactoryRunner.request_stop(state_dir)
            if session is None:
                print("未找到可停止的工厂会话")
                return
        else:
            session = AIFactoryRunner.status_view(state_dir)
        if command_key == ("factory", "stop"):
            session = AIFactoryRunner.status_view(state_dir)
        print(json.dumps(session or {"status": "NOT_STARTED"}, ensure_ascii=False, indent=2))
        return

    # Keep read-only factory control commands independent from the production
    # HTTP/client import chain.  This matters on an unattended host where a
    # status/stop operation must work even when credentials or requests are
    # unavailable.
    from wqb_agent import Agent, WQBClient

    try:
        client = WQBClient()
    except Exception as exc:
        print(f"Credentials error: {exc}")
        sys.exit(1)

    agent = Agent(client, typed_config)
    lock_path = None
    try:
        if command_key == ("research", "suggest"):
            # suggest 只做字段检索、不模拟，不占模拟实例锁
            agent.run_suggestion_round()
            return
        if command_key == ("alpha", "sync-feed"):
            feed_lock = acquire_single_instance_lock(
                typed_config.runtime.state_dir, operation="sync-alpha-feed"
            )
            if feed_lock is None:
                sys.exit(1)
            try:
                snapshot = agent.refresh_remote_alpha_feed(limit=100)
                print(json.dumps({
                    **snapshot,
                    "network_write": False,
                }, ensure_ascii=False, indent=2))
            finally:
                release_single_instance_lock(feed_lock)
            return
        operation_by_command = {
            ("factory", "run"): "factory-run",
            ("research", "run-proposals"): "run-proposals",
            ("recovery", "skip-stale"): "skip-stale",
            ("recovery", "skip-submit-unknown"): "skip-submit-unknown",
            ("recovery", "finalize-round"): "finalize-round",
            ("recovery", "settle-stale-trajectory"): "settle-stale-trajectory",
        }
        lock_path = acquire_single_instance_lock(
            typed_config.runtime.state_dir,
            operation=operation_by_command[command_key],
        )
        if lock_path is None:
            sys.exit(1)
        if command_key == ("recovery", "settle-stale-trajectory"):
            # Local-only trajectory settlement: no client, no Simulation POST.
            result = agent.settle_stale_trajectory(
                int(command.round_value)
                if command.round_value not in (None, "") else None,
                dry_run=command.dry_run,
            )
            print(json.dumps(result or {"status": "LOCAL_OWNER_BUSY"},
                             ensure_ascii=False, indent=2))
        elif command_key == ("factory", "run"):
            from wqb_agent.factory_runner import AIFactoryRunner

            factory_cfg = typed_config.runtime.factory
            hours = (
                command.hours
                if command.hours is not None
                else float(factory_cfg.get("max_runtime_sec", 86400)) / 3600
            )
            session = AIFactoryRunner(agent).run(
                duration_sec=max(0.0, hours) * 3600,
                max_rounds=factory_cfg.get("max_rounds", 0),
                idle_sleep_sec=factory_cfg.get("idle_sleep_sec", 30),
                max_simulations=factory_cfg.get("max_simulations", 240),
                daily_simulation_cap=factory_cfg.get(
                    "daily_simulation_cap",
                    factory_cfg.get("max_simulations", 240),
                ),
                weekly_simulation_cap=factory_cfg.get(
                    "weekly_simulation_cap",
                    factory_cfg.get("max_simulations", 240),
                ),
            )
            print(json.dumps(session, ensure_ascii=False, indent=2))
        elif command_key == ("research", "run-proposals"):
            agent.run_proposals(
                command.path,
                allow_unresolved_checkpoint=command.force_new_round,
            )
        elif command_key == ("recovery", "skip-stale"):
            agent.skip_stale_reconciled(int(command.round_value), command.identifier)
        elif command_key == ("recovery", "skip-submit-unknown"):
            agent.skip_submit_unknown_authorized(
                int(command.round_value), command.identifier
            )
        elif command_key == ("recovery", "finalize-round"):
            agent.finalize_recorded_round(command.round_value)
        else:
            print(
                "未指定研究动作。请使用：\n"
                "  python main.py suggest\n"
                "  python main.py run-proposals\n"
                "  python main.py factory run"
            )
            sys.exit(1)
    finally:
        release_single_instance_lock(lock_path)


if __name__ == "__main__":
    main()
