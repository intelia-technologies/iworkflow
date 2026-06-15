"""`iworkflow` CLI — onboard the engine into any repo with one command.

  iworkflow serve                  # run the MCP server (stdio) — what agents spawn
  iworkflow register [--codex|--claude] [--root .]   # wire it into a repo's config
  iworkflow catalog [--root .]     # show the tool catalog discovered in a repo

`register` writes the MCP server block so Codex / Claude in that repo can DRIVE
iworkflow as a `workflow` tool — no manual config editing.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# The command an agent CLI will spawn for the MCP server. `iworkflow-mcp` is the
# console script installed alongside this package.
MCP_COMMAND = "iworkflow-mcp"


def register_claude(root: str) -> tuple[Path, bool]:
    """Add the iworkflow MCP server to <root>/.mcp.json (merge, don't clobber)."""
    path = Path(root) / ".mcp.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    servers = data.setdefault("mcpServers", {})
    if "iworkflow" in servers:
        return path, False
    servers["iworkflow"] = {"command": MCP_COMMAND}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path, True


def register_codex(root: str) -> tuple[Path, bool]:
    """Append [mcp_servers.iworkflow] to <root>/.codex/config.toml if absent."""
    path = Path(root) / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if "[mcp_servers.iworkflow]" in text:
        return path, False
    sep = "" if text.endswith("\n") or not text else "\n"
    block = f'{sep}\n[mcp_servers.iworkflow]\ncommand = "{MCP_COMMAND}"\n'
    path.write_text(text + block, encoding="utf-8")
    return path, True


def ensure_gitignore(root: str, entry: str = ".iworkflow/") -> tuple[Path, bool]:
    """Add `entry` to <root>/.gitignore if absent (so run logs aren't committed)."""
    path = Path(root) / ".gitignore"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if entry in (line.strip() for line in text.splitlines()):
        return path, False
    sep = "" if text.endswith("\n") or not text else "\n"
    path.write_text(f"{text}{sep}{entry}\n", encoding="utf-8")
    return path, True


def _cmd_register(root: str, *, codex: bool, claude: bool) -> None:
    targets = []
    if codex or not (codex or claude):
        targets.append(("Codex", register_codex))
    if claude or not (codex or claude):
        targets.append(("Claude", register_claude))
    for name, fn in targets:
        path, wrote = fn(root)
        print(f"{'registered' if wrote else 'already present'}: {name} → {path}")
    gi_path, gi_wrote = ensure_gitignore(root)
    print(f"{'added to' if gi_wrote else 'already in'} .gitignore: .iworkflow/  → {gi_path}")
    print('\nDone. Agents in this repo can now call the "iworkflow" MCP tools '
          "(iworkflow_ping, iworkflow_workflow). Commit the config so every worktree "
          "inherits it.")


def _cmd_stats(journal_dir: str, run_id: str | None) -> None:
    from .stats import provider_stats, run_summary

    summary = run_summary(journal_dir, run_id)
    if summary:
        print("run summary:")
        print(json.dumps(summary, indent=2))
    print("\nper-provider (all runs):")
    print(json.dumps(provider_stats(journal_dir), indent=2))


def _cmd_catalog(root: str) -> None:
    from collections import Counter

    from .catalog_loaders import load_project_catalog

    catalog = load_project_catalog(root)
    specs = catalog.all()
    kinds = Counter(s.kind.value for s in specs)
    print(f"catalog discovered in {root!r}: {len(specs)} tools {dict(kinds)}")
    for spec in specs[:40]:
        print(f"  [{spec.kind.value:7}] {spec.name}")
    if len(specs) > 40:
        print(f"  … and {len(specs) - 40} more")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="iworkflow",
        description="Subscription-only multi-agent workflow orchestrator.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run the iworkflow MCP server (stdio)")

    p_reg = sub.add_parser("register", help="wire the iworkflow MCP server into a repo")
    p_reg.add_argument("--root", default=".")
    p_reg.add_argument("--codex", action="store_true", help="only register for Codex")
    p_reg.add_argument("--claude", action="store_true", help="only register for Claude")

    p_cat = sub.add_parser("catalog", help="show the tool catalog discovered in a repo")
    p_cat.add_argument("--root", default=".")

    p_stats = sub.add_parser("stats", help="show telemetry from past runs (the logs)")
    p_stats.add_argument("--journal-dir", default=".iworkflow")
    p_stats.add_argument("--run-id", default=None)

    args = parser.parse_args(argv)
    if args.cmd == "serve":
        from .mcp_server import main as serve
        serve()
    elif args.cmd == "register":
        _cmd_register(args.root, codex=args.codex, claude=args.claude)
    elif args.cmd == "catalog":
        _cmd_catalog(args.root)
    elif args.cmd == "stats":
        _cmd_stats(args.journal_dir, args.run_id)


if __name__ == "__main__":
    main()
