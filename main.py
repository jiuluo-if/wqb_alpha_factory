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
        ("diagnostics", "doctor"),
        ("diagnostics", "audit"),
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

    if command_key == ("diagnostics", "doctor"):
        from wqb_agent.doctor import run_doctor
        print(json.dumps(run_doctor(typed_config, offline=True), ensure_ascii=False, indent=2))
        return
    if command_key == ("diagnostics", "audit"):
        from wqb_agent.audit import audit_execution_surface
        result = audit_execution_surface(typed_config.runtime.state_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result.get("ok"):
            sys.exit(2)
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

    from wqb_agent import WQBClient, research_api

    try:
        client = WQBClient()
    except Exception as exc:
        print(f"Credentials error: {exc}")
        sys.exit(1)

    if command_key == ("research", "suggest"):
        # Suggestion is a read-only discovery projection; it does not build
        # the retired Agent runtime or emit a local proposals artifact.
        result = research_api.discover_fields(
            "", client=client, config=typed_config,
            state_dir=typed_config.runtime.state_dir,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if command_key == ("alpha", "sync-feed"):
        feed_lock = acquire_single_instance_lock(
            typed_config.runtime.state_dir, operation="sync-alpha-feed"
        )
        if feed_lock is None:
            sys.exit(1)
        try:
            snapshot = research_api.refresh_remote_alphas(
                client=client, config=typed_config,
                state_dir=typed_config.runtime.state_dir,
                limit=100,
            )
            print(json.dumps({
                **snapshot,
                "network_write": False,
            }, ensure_ascii=False, indent=2))
        finally:
            release_single_instance_lock(feed_lock)
        return
    print("未指定研究动作。请使用：\n  python main.py suggest")
    sys.exit(1)


if __name__ == "__main__":
    main()
