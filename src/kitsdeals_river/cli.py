"""CLI for kitsdeals-river.

The CLI is the **agent-facing API to profile.yaml + the watcher daemon**.
Humans rarely run these directly — the agent does, after the user says
"set this up for me" (via skills/onboarding.md) or "stop watching X" (via
skills/update-profile.md). The shape of every command is therefore:

    - Programmatic input (flags, JSON), no interactive prompts
    - Structured output (JSON when --json, plain text otherwise)
    - Stable exit codes (0 success, 1 user error, 2 internal)

Edits to profile.yaml stamp ``_meta.last_edited_by = "agent"`` so a
future read can tell whether the agent or a human last touched it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .profile import (
    NotifyConfig,
    OwnedProduct,
    Profile,
    SuppressConfig,
    TelegramConfig,
    Watch,
    WatchFilter,
)
from .watcher import RiverWatcher

logger = logging.getLogger("kitsdeals_river.cli")

DEFAULT_PROFILE_PATH = "~/.kitsdeals/profile.yaml"
DEFAULT_CURSOR_PATH = "~/.kitsdeals/cursor.json"
DEFAULT_DECISIONS_PATH = "~/.kitsdeals/decisions.jsonl"
DEFAULT_PID_PATH = "~/.kitsdeals/watcher.pid"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve(path: str) -> Path:
    return Path(path).expanduser()


def _load_profile_or_die(path: str) -> Profile:
    resolved = _resolve(path)
    if not resolved.exists():
        print(
            f"Error: profile not found at {resolved}.\n"
            f"Run `kitsdeals-river setup --profile <path>` to create one,\n"
            f"or paste this repo's URL to your Claude/Code agent and ask\n"
            f'it to "set up Kit\'s Deals for me — I\'m looking for X."',
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        return Profile.load(resolved)
    except ValidationError as e:
        print(f"Error: profile failed validation:\n{e}", file=sys.stderr)
        sys.exit(1)


def _print_json(payload: Any) -> None:
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


# ---------------------------------------------------------------------------
# setup — write a profile from a JSON spec
# ---------------------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> int:
    """Write a complete profile.yaml from a JSON spec.

    The agent runs the onboarding interview, collects answers, then calls
    this with --json '<the full profile>'. We validate the spec via the
    Pydantic schema before writing, so an invalid agent edit is rejected
    cleanly rather than corrupting the file.
    """
    if args.json:
        try:
            spec = json.loads(args.json)
        except json.JSONDecodeError as e:
            print(f"Error: --json is not valid JSON: {e}", file=sys.stderr)
            return 1
    elif args.from_file:
        try:
            with _resolve(args.from_file).open("r", encoding="utf-8") as f:
                spec = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"Error reading {args.from_file}: {e}", file=sys.stderr)
            return 1
    else:
        print("Error: pass --json '...' or --from-file <path>", file=sys.stderr)
        return 1

    try:
        profile = Profile.model_validate(spec)
    except ValidationError as e:
        print(f"Error: spec failed validation:\n{e}", file=sys.stderr)
        return 1

    profile.save(args.profile, edited_by="agent")
    print(f"Wrote profile to {_resolve(args.profile)}")
    return 0


# ---------------------------------------------------------------------------
# profile show — print current profile
# ---------------------------------------------------------------------------


def cmd_profile_show(args: argparse.Namespace) -> int:
    profile = _load_profile_or_die(args.profile)
    if args.json:
        _print_json(profile.model_dump(by_alias=True, mode="json"))
    else:
        # Plain-language summary the agent can show the human
        print(f"Profile for {profile.name} ({profile.notify.channel} notifications)")
        print(f"  Tone: {profile.tone}")
        print(f"  Watches ({len(profile.watches)}):")
        for w in profile.watches:
            print(f"    - {w.label} [{w.notify_threshold}]")
            f = w.filter.model_dump(exclude_none=True)
            for k, v in f.items():
                print(f"        {k}: {v}")
        print(f"  Already owned: {len(profile.suppress.already_owned)}")
        print(f"  Brand blacklist: {profile.suppress.brands_blacklist or '(none)'}")
        print(f"  Last edited: {profile.meta.last_edited_at} by {profile.meta.last_edited_by}")
    return 0


# ---------------------------------------------------------------------------
# profile validate — confirm well-formed
# ---------------------------------------------------------------------------


def cmd_profile_validate(args: argparse.Namespace) -> int:
    resolved = _resolve(args.profile)
    if not resolved.exists():
        print(f"Error: profile not found at {resolved}", file=sys.stderr)
        return 1
    try:
        Profile.load(resolved)
    except ValidationError as e:
        print(f"Invalid:\n{e}", file=sys.stderr)
        return 1
    print(f"OK ({resolved})")
    return 0


# ---------------------------------------------------------------------------
# profile update — programmatic edits
# ---------------------------------------------------------------------------


def cmd_profile_update(args: argparse.Namespace) -> int:
    """Apply structured edits to the profile.

    The agent computes a diff from the user's natural-language feedback
    ("stop showing me TVs", "I bought one of those") and translates to
    flags. Dry-run via ``--no-apply`` prints the diff without writing.

    Supports a few common edit patterns. Anything outside these is a
    "use --json to provide a full replacement profile" path — explicit,
    no clever inference.
    """
    profile = _load_profile_or_die(args.profile)
    original = profile.model_dump(by_alias=True, mode="json")

    if args.add_watch:
        try:
            watch_spec = json.loads(args.add_watch)
            watch = Watch.model_validate(watch_spec)
        except (json.JSONDecodeError, ValidationError) as e:
            print(f"Error: --add-watch invalid: {e}", file=sys.stderr)
            return 1
        # Replace by label if one exists with the same name; else append
        profile.watches = [w for w in profile.watches if w.label != watch.label]
        profile.watches.append(watch)

    if args.remove_watch:
        before = len(profile.watches)
        profile.watches = [w for w in profile.watches if w.label != args.remove_watch]
        if len(profile.watches) == before:
            print(f"Error: no watch named {args.remove_watch!r}", file=sys.stderr)
            return 1

    if args.blacklist_brand:
        for b in args.blacklist_brand:
            if b not in profile.suppress.brands_blacklist:
                profile.suppress.brands_blacklist.append(b)

    if args.add_owned:
        try:
            owned_spec = json.loads(args.add_owned)
            owned = OwnedProduct.model_validate(owned_spec)
        except (json.JSONDecodeError, ValidationError) as e:
            print(f"Error: --add-owned invalid: {e}", file=sys.stderr)
            return 1
        profile.suppress.already_owned.append(owned)

    new = profile.model_dump(by_alias=True, mode="json")
    if original == new:
        print("No changes")
        return 0

    if args.no_apply:
        # Show a structured diff in JSON — easier for the agent to read
        # than a human-style colored diff.
        _print_json({"before": original, "after": new})
        return 0

    profile.save(args.profile, edited_by="agent")
    print(f"Applied changes to {_resolve(args.profile)}")
    if not args.no_reload:
        _signal_reload(args)
    return 0


def _signal_reload(args: argparse.Namespace) -> None:
    """Best-effort SIGHUP to a running watcher daemon. Silent if no PID
    file (the daemon may not be running, which is fine)."""
    pid_path = _resolve(args.pid_file or DEFAULT_PID_PATH)
    if not pid_path.exists():
        return
    try:
        pid = int(pid_path.read_text().strip())
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, signal.SIGHUP)
        print(f"Sent SIGHUP to watcher (pid {pid})")
    except ProcessLookupError:
        # Stale PID file
        try:
            pid_path.unlink()
        except OSError:
            pass
    except OSError as e:
        logger.warning("reload signal failed: %s", e)


# ---------------------------------------------------------------------------
# reload — explicit signal-based reload
# ---------------------------------------------------------------------------


def cmd_reload(args: argparse.Namespace) -> int:
    pid_path = _resolve(args.pid_file or DEFAULT_PID_PATH)
    if not pid_path.exists():
        print(f"Error: no watcher running (no PID file at {pid_path})", file=sys.stderr)
        return 1
    try:
        pid = int(pid_path.read_text().strip())
    except (OSError, ValueError) as e:
        print(f"Error reading {pid_path}: {e}", file=sys.stderr)
        return 1
    try:
        os.kill(pid, signal.SIGHUP)
    except ProcessLookupError:
        print(f"Error: process {pid} no longer running (stale pid file)", file=sys.stderr)
        return 1
    print(f"Sent SIGHUP to watcher (pid {pid})")
    return 0


# ---------------------------------------------------------------------------
# status — daemon health snapshot
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    pid_path = _resolve(args.pid_file or DEFAULT_PID_PATH)
    cursor_path = _resolve(args.cursor or DEFAULT_CURSOR_PATH)
    decisions_path = _resolve(args.decisions or DEFAULT_DECISIONS_PATH)

    running = False
    pid = None
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)  # signal 0 = "is process alive?"
            running = True
        except (OSError, ValueError, ProcessLookupError):
            running = False

    cursor_state = {}
    if cursor_path.exists():
        try:
            cursor_state = json.loads(cursor_path.read_text())
        except (OSError, json.JSONDecodeError):
            cursor_state = {}

    decisions_count = 0
    if decisions_path.exists():
        with decisions_path.open("rb") as f:
            for _ in f:
                decisions_count += 1

    payload = {
        "running": running,
        "pid": pid if running else None,
        "pid_file": str(pid_path),
        "cursor": cursor_state,
        "decisions_count": decisions_count,
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"Watcher: {'running (pid ' + str(pid) + ')' if running else 'stopped'}")
        if cursor_state.get("last_occurred_at"):
            print(f"Cursor: last_occurred_at={cursor_state['last_occurred_at']}")
            print(f"        last_event_id={cursor_state.get('last_event_id', '?')}")
        else:
            print("Cursor: empty (no events processed yet)")
        print(f"Decisions logged: {decisions_count}")
    return 0


# ---------------------------------------------------------------------------
# run — the watcher daemon
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    profile = _load_profile_or_die(args.profile)
    pid_path = _resolve(args.pid_file or DEFAULT_PID_PATH)
    pid_path.parent.mkdir(parents=True, exist_ok=True)

    # Refuse to start if another watcher is already alive (PID file points
    # to a live process). The agent should reload, not double-launch.
    if pid_path.exists():
        try:
            existing = int(pid_path.read_text().strip())
            os.kill(existing, 0)
            print(
                f"Error: watcher already running (pid {existing}). "
                f"Use `kitsdeals-river reload` to apply config changes.",
                file=sys.stderr,
            )
            return 1
        except (OSError, ValueError, ProcessLookupError):
            # Stale PID file; safe to overwrite
            pass

    pid_path.write_text(str(os.getpid()))

    # Load filter from profile. The reload callback re-reads on SIGHUP.
    def reload_filter() -> WatchFilter | None:
        return _load_profile_or_die(args.profile).watches[0].filter if (
            _load_profile_or_die(args.profile).watches
        ) else None

    initial_filter = profile.watches[0].filter if profile.watches else None

    watcher = RiverWatcher(
        api_base=args.api_base or "https://api.kitsdeals.com",
        filter=initial_filter,
        cursor_path=args.cursor or DEFAULT_CURSOR_PATH,
        on_event=_make_default_handler(profile, args),
        reload_profile=reload_filter,
    )

    try:
        watcher.run()
    finally:
        try:
            pid_path.unlink()
        except OSError:
            pass
    return 0


def _make_default_handler(profile: Profile, args: argparse.Namespace):
    """Build the default per-event handler from the profile.

    Default behavior: log + notify via the configured channel for any
    deal event that passes the watch filter. No agent-evaluator wiring
    yet — that's what skills/onboarding.md will show the agent how to
    customize. PR 38 ships the dual-emit (telegram + spawn-claude)
    template based on Kit's setup.
    """
    from .notify import StdoutNotifier, TelegramNotifier

    if profile.notify.channel == "telegram" and profile.notify.telegram:
        cfg = profile.notify.telegram
        notifier = TelegramNotifier(
            bot_token_env=cfg.bot_token_env,
            chat_id_env=cfg.chat_id_env,
        )
    else:
        notifier = StdoutNotifier()

    async def handler(event):
        if not event.is_deal:
            return
        deal = event.data
        if profile.matches_owned(deal) or profile.matches_blacklist(deal):
            return
        brand = deal.get("brand", "?")
        ptype = deal.get("product_type", "")
        price = deal.get("current_price_cents", 0) / 100
        msg = f"{brand} {ptype} — ${price:.2f} ({deal.get('discount_pct', 0)}% off)"
        await notifier.send(msg, deal)

    return handler


# ---------------------------------------------------------------------------
# Argparse plumbing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kitsdeals-river", description=__doc__.split("\n")[0])
    p.add_argument(
        "--profile",
        default=DEFAULT_PROFILE_PATH,
        help=f"Path to profile.yaml (default: {DEFAULT_PROFILE_PATH})",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # setup
    sp = sub.add_parser("setup", help="Write a profile from a JSON spec")
    sp.add_argument("--json", help="Inline JSON spec for the profile")
    sp.add_argument("--from-file", help="Path to a JSON file with the spec")
    sp.set_defaults(func=cmd_setup)

    # profile (with subcommands)
    pf = sub.add_parser("profile", help="Inspect or edit the profile")
    pf_sub = pf.add_subparsers(dest="profile_command", required=True)

    pf_show = pf_sub.add_parser("show", help="Print the current profile")
    pf_show.add_argument("--json", action="store_true", help="JSON output")
    pf_show.set_defaults(func=cmd_profile_show)

    pf_val = pf_sub.add_parser("validate", help="Check the profile is well-formed")
    pf_val.set_defaults(func=cmd_profile_validate)

    pf_upd = pf_sub.add_parser("update", help="Apply structured edits to the profile")
    pf_upd.add_argument("--add-watch", help="JSON Watch spec to add (or replace by label)")
    pf_upd.add_argument("--remove-watch", help="Label of the watch to remove")
    pf_upd.add_argument(
        "--blacklist-brand",
        action="append",
        default=[],
        help="Brand to add to suppress.brands_blacklist (repeatable)",
    )
    pf_upd.add_argument(
        "--add-owned",
        help="JSON OwnedProduct spec to append to suppress.already_owned",
    )
    pf_upd.add_argument("--no-apply", action="store_true", help="Dry-run: print diff, don't write")
    pf_upd.add_argument("--no-reload", action="store_true", help="Don't signal the running watcher")
    pf_upd.add_argument("--pid-file", help=f"Default: {DEFAULT_PID_PATH}")
    pf_upd.set_defaults(func=cmd_profile_update)

    # reload
    rl = sub.add_parser("reload", help="Send SIGHUP to running watcher")
    rl.add_argument("--pid-file", help=f"Default: {DEFAULT_PID_PATH}")
    rl.set_defaults(func=cmd_reload)

    # status
    st = sub.add_parser("status", help="Print daemon + cursor health")
    st.add_argument("--json", action="store_true")
    st.add_argument("--pid-file", help=f"Default: {DEFAULT_PID_PATH}")
    st.add_argument("--cursor", help=f"Default: {DEFAULT_CURSOR_PATH}")
    st.add_argument("--decisions", help=f"Default: {DEFAULT_DECISIONS_PATH}")
    st.set_defaults(func=cmd_status)

    # run
    rn = sub.add_parser("run", help="Start the watcher (foreground)")
    rn.add_argument("--api-base", help="Default: https://api.kitsdeals.com")
    rn.add_argument("--cursor", help=f"Default: {DEFAULT_CURSOR_PATH}")
    rn.add_argument("--pid-file", help=f"Default: {DEFAULT_PID_PATH}")
    rn.set_defaults(func=cmd_run)

    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns an exit code.

    Catches SystemExit so callers (including pytest) get a clean integer
    rather than an exception. Shell invocation still exits naturally —
    the entry-point script in pyproject.toml does ``sys.exit(main())``.
    """
    logging.basicConfig(
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=os.environ.get("KITSDEALS_LOG_LEVEL", "INFO"),
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit as e:
        # Helpers like _load_profile_or_die call sys.exit; respect their code.
        code = e.code if isinstance(e.code, int) else 1
        return code


if __name__ == "__main__":
    sys.exit(main())
