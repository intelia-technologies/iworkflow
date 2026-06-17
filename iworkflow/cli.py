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
import re
import tomllib
from pathlib import Path

# The command an agent CLI will spawn for the MCP server. `iworkflow-mcp` is the
# console script installed alongside this package.
MCP_COMMAND = "iworkflow-mcp"

# iworkflow owns the region between these markers in .codex/config.toml, so a
# re-run updates in place (idempotent) and `unregister` can remove it cleanly —
# instead of blindly appending a second [mcp_servers.iworkflow] table.
CODEX_MARK_BEGIN = "# >>> iworkflow >>>"
CODEX_MARK_END = "# <<< iworkflow <<<"
_CODEX_BLOCK = (
    f"{CODEX_MARK_BEGIN}\n[mcp_servers.iworkflow]\n"
    f'command = "{MCP_COMMAND}"\n{CODEX_MARK_END}'
)
_CODEX_MARKER_RE = re.compile(
    re.escape(CODEX_MARK_BEGIN) + r".*?" + re.escape(CODEX_MARK_END), re.DOTALL)


class RegisterError(Exception):
    """register/unregister refused to edit a config (would corrupt or can't parse it)."""


def _toml_ok(text: str) -> bool:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    return True


def _strip_codex_block(text: str) -> str:
    """Remove iworkflow's marked region (if any), leaving the rest tidy."""
    if CODEX_MARK_BEGIN not in text:
        return text
    cleaned = _CODEX_MARKER_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip("\n")
    return cleaned + "\n" if cleaned else ""


def _append_codex_block(base: str) -> str:
    base = base.rstrip("\n")
    return f"{base}\n\n{_CODEX_BLOCK}\n" if base else f"{_CODEX_BLOCK}\n"


def register_claude(root: str) -> tuple[Path, bool]:
    """Add the iworkflow MCP server to <root>/.mcp.json (merge, never corrupt)."""
    path = Path(root) / ".mcp.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise RegisterError(f"{path} is not valid JSON; fix it first") from e
        if not isinstance(data, dict):
            raise RegisterError(f"{path} is not a JSON object; refusing to edit it")
    else:
        data = {}
    servers = data.setdefault("mcpServers", {})
    if servers.get("iworkflow") == {"command": MCP_COMMAND}:
        return path, False                              # already current — no write
    servers["iworkflow"] = {"command": MCP_COMMAND}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path, True


def register_codex(root: str) -> tuple[Path, bool]:
    """Register iworkflow in <root>/.codex/config.toml inside owned markers.

    Idempotent (re-run updates in place), self-healing (strips a stale managed
    block), and validating (never writes TOML that won't parse). If another tool
    already registered `mcp_servers.iworkflow` outside our markers, we leave it."""
    path = Path(root) / ".codex" / "config.toml"
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    if _CODEX_BLOCK in original and _toml_ok(original):
        return path, False                              # already present & file healthy
    stripped = _strip_codex_block(original)             # drop any iworkflow-managed block
    if stripped.strip() and not _toml_ok(stripped):
        raise RegisterError(
            f"{path} has TOML errors outside iworkflow's block; fix it first")
    parsed = tomllib.loads(stripped) if stripped.strip() else {}
    if "iworkflow" in parsed.get("mcp_servers", {}):
        new_text = stripped                             # managed elsewhere — don't add a 2nd
    else:
        new_text = _append_codex_block(stripped)
    if not _toml_ok(new_text):                          # validate BEFORE writing (rollback = don't)
        raise RegisterError(
            f"refusing to write {path} — the result would not parse as TOML")
    if new_text == original:
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")
    return path, True


def unregister_claude(root: str) -> tuple[Path, bool]:
    """Remove iworkflow from <root>/.mcp.json. Returns (path, changed)."""
    path = Path(root) / ".mcp.json"
    if not path.exists():
        return path, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RegisterError(f"{path} is not valid JSON; fix it first") from e
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
    if "iworkflow" not in servers:
        return path, False
    servers.pop("iworkflow")
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path, True


def unregister_codex(root: str) -> tuple[Path, bool]:
    """Remove iworkflow's marked block from <root>/.codex/config.toml (leaves a
    foreign-managed entry alone). Returns (path, changed)."""
    path = Path(root) / ".codex" / "config.toml"
    if not path.exists():
        return path, False
    original = path.read_text(encoding="utf-8")
    new_text = _strip_codex_block(original)
    if new_text == original:
        return path, False                              # nothing of ours to remove
    if new_text.strip() and not _toml_ok(new_text):
        raise RegisterError(f"removing iworkflow from {path} would break it; fix it first")
    path.write_text(new_text, encoding="utf-8")
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


def _targets(codex: bool, claude: bool, reg: bool) -> list[tuple[str, object]]:
    both = not (codex or claude)
    out: list[tuple[str, object]] = []
    if codex or both:
        out.append(("Codex", register_codex if reg else unregister_codex))
    if claude or both:
        out.append(("Claude", register_claude if reg else unregister_claude))
    return out


def _cmd_register(root: str, *, codex: bool, claude: bool) -> None:
    for name, fn in _targets(codex, claude, reg=True):
        try:
            path, wrote = fn(root)
            print(f"{'registered' if wrote else 'already present'}: {name} → {path}")
        except RegisterError as e:
            print(f"skipped {name}: {e}")
    gi_path, gi_wrote = ensure_gitignore(root)
    print(f"{'added to' if gi_wrote else 'already in'} .gitignore: .iworkflow/  → {gi_path}")
    print('\nDone. Agents in this repo can now call the "iworkflow" MCP tools '
          "(iworkflow_ping, iworkflow_workflow, iworkflow_list_workflows). Commit the "
          "config so every worktree inherits it.")


def _cmd_unregister(root: str, *, codex: bool, claude: bool) -> None:
    for name, fn in _targets(codex, claude, reg=False):
        try:
            path, changed = fn(root)
            print(f"{'removed' if changed else 'not present'}: {name} → {path}")
        except RegisterError as e:
            print(f"skipped {name}: {e}")


def _cmd_stats(journal_dir: str, run_id: str | None) -> None:
    from .stats import provider_stats, run_summary

    summary = run_summary(journal_dir, run_id)
    if summary:
        print("run summary:")
        print(json.dumps(summary, indent=2))
    print("\nper-provider (all runs):")
    print(json.dumps(provider_stats(journal_dir), indent=2))


def _cmd_workflows(recipe_dir: str | None) -> None:
    from .recipes import list_recipes

    for r in list_recipes(recipe_dir):
        params = ", ".join(r["params"]) or "—"
        print(f"  {r['name']:16} {r['description']}")
        print(f"  {'':16} params: {params}")


def _cmd_run(name: str | None, goal: str | None, params_json: str | None,
             spec_path: str | None, run_id: str, recipe_dir: str | None,
             cwd: str | None, timeout_s: float, caps_json: str | None,
             journal_dir: str) -> None:
    import asyncio

    from .mcp_server import run_workflow

    params = json.loads(params_json) if params_json else None
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8")) if spec_path else None
    caps = json.loads(caps_json) if caps_json else None
    result = asyncio.run(run_workflow(
        goal=goal,
        workflow=name,
        params=params,
        spec=spec,
        run_id=run_id,
        recipe_dir=recipe_dir,
        cwd=cwd,
        timeout_s=timeout_s,
        caps=caps,
        journal_dir=journal_dir,
    ))
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


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


def _cmd_graph(name: str | None, spec_path: str | None, html_path: str | None,
               publish: bool, recipe_dir: str | None, mermaid: bool = False) -> None:
    import os
    import subprocess
    import sys
    import tempfile
    import webbrowser
    from .recipes import get_recipe
    from .graph import spec_to_mermaid, spec_to_html

    if spec_path:
        spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    elif name:
        try:
            spec = get_recipe(name, recipe_dir)
        except KeyError as e:
            print(f"Error: {e}")
            return
    else:
        print("Error: must specify either a recipe name or --spec <path>")
        return

    # Raw Mermaid to stdout is opt-in: terminals and agent TUIs that auto-render
    # fenced ```mermaid blocks can hang or crash on cyclic (loop) or richly
    # styled graphs, so the default emits a self-contained HTML file that
    # mermaid.js renders in a browser instead.
    if mermaid:
        print("```mermaid")
        print(spec_to_mermaid(spec))
        print("```")
        return

    html_content = spec_to_html(spec)

    if publish:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_html = Path(tmpdir) / "index.html"
            tmp_html.write_text(html_content, encoding="utf-8")

            cmd = f"[ -f ~/.zshrc.local ] && source ~/.zshrc.local; nosdrop '{tmpdir}' --expires-in 24h"
            try:
                res = subprocess.run(
                    ["zsh", "-l", "-c", cmd],
                    capture_output=True,
                    text=True,
                    check=True
                )
                print(res.stdout.strip())
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                print(f"Failed to publish to nosdrop: {e}")
                if isinstance(e, subprocess.CalledProcessError):
                    print(e.stderr)

    if html_path:
        out_path = Path(html_path).resolve()
        out_path.write_text(html_content, encoding="utf-8")
        print(f"Generated HTML diagram: {out_path}")
    elif not publish:
        fd, tmp = tempfile.mkstemp(prefix="iworkflow-graph-", suffix=".html")
        os.close(fd)
        out_path = Path(tmp).resolve()
        out_path.write_text(html_content, encoding="utf-8")
        print(f"Generated HTML diagram: {out_path}")

    # Always attempt to open the browser for any generated local file (unless only publishing)
    if html_path or not publish:
        try:
            webbrowser.open(out_path.as_uri())
        except Exception:
            pass

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

    p_unreg = sub.add_parser("unregister", help="remove the iworkflow MCP server from a repo")
    p_unreg.add_argument("--root", default=".")
    p_unreg.add_argument("--codex", action="store_true", help="only unregister for Codex")
    p_unreg.add_argument("--claude", action="store_true", help="only unregister for Claude")

    p_cat = sub.add_parser("catalog", help="show the tool catalog discovered in a repo")
    p_cat.add_argument("--root", default=".")

    p_wf = sub.add_parser("workflows", help="list predefined workflow recipes")
    p_wf.add_argument("--recipe-dir", default=None,
                      help="extra dir of host *.json recipes (default .iworkflow/recipes)")

    p_run = sub.add_parser("run", help="run a recipe by name, --goal, or a --spec file")
    p_run.add_argument("name", nargs="?", default=None, help="recipe name (omit if --spec/--goal)")
    p_run.add_argument("--goal", default=None, help="shorthand for fan_synthesize over a question")
    p_run.add_argument("--params", default=None, help="JSON params object")
    p_run.add_argument("--spec", default=None, help="path to a declarative workflow spec JSON")
    p_run.add_argument("--run-id", default="cli")
    p_run.add_argument("--recipe-dir", default=None)
    p_run.add_argument("--cwd", default=None, help="working directory for provider CLIs")
    p_run.add_argument("--timeout", type=float, default=180, help="per-provider timeout (seconds)")
    p_run.add_argument("--caps", default=None, help='JSON caps object, e.g. {"codex":2}')
    p_run.add_argument("--journal-dir", default=".iworkflow")

    p_stats = sub.add_parser("stats", help="show telemetry from past runs (the logs)")
    p_stats.add_argument("--journal-dir", default=".iworkflow")
    p_stats.add_argument("--run-id", default=None)

    p_graph = sub.add_parser("graph", help="generate a visual diagram of a workflow (Mermaid or HTML)")
    p_graph.add_argument("name", nargs="?", default=None, help="recipe name (omit if --spec)")
    p_graph.add_argument("--spec", default=None, help="path to a declarative workflow spec JSON")
    p_graph.add_argument("--html", default=None, help="output file path for HTML diagram")
    p_graph.add_argument("--publish", action="store_true", help="publish the HTML diagram to a shareable URL via nosdrop")
    p_graph.add_argument("--mermaid", action="store_true", help="print raw Mermaid to stdout instead of generating HTML (may break terminals that auto-render mermaid)")
    p_graph.add_argument("--recipe-dir", default=None, help="extra recipe directory")

    args = parser.parse_args(argv)
    if args.cmd == "serve":
        from .mcp_server import main as serve
        serve()
    elif args.cmd == "register":
        _cmd_register(args.root, codex=args.codex, claude=args.claude)
    elif args.cmd == "unregister":
        _cmd_unregister(args.root, codex=args.codex, claude=args.claude)
    elif args.cmd == "catalog":
        _cmd_catalog(args.root)
    elif args.cmd == "workflows":
        _cmd_workflows(args.recipe_dir)
    elif args.cmd == "run":
        _cmd_run(args.name, args.goal, args.params, args.spec, args.run_id, args.recipe_dir, args.cwd, args.timeout, args.caps, args.journal_dir)
    elif args.cmd == "stats":
        _cmd_stats(args.journal_dir, args.run_id)
    elif args.cmd == "graph":
        _cmd_graph(args.name, args.spec, args.html, args.publish, args.recipe_dir, args.mermaid)


if __name__ == "__main__":
    main()
