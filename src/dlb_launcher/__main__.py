"""CLI entry: ``dlb-launcher`` or ``python -m dlb_launcher``.

Usage:
    dlb-launcher --name alpha claude
    dlb-launcher --name bravo codex
    dlb-launcher --name worker-1 gemini --some-arg

The argument layout is deliberately "first positional + remainder" so that
arbitrary flags meant for the wrapped CLI pass through cleanly. The first
positional after `--name` is the child command; everything after it is
forwarded as the child's argv tail.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .launcher import run

USAGE_EXAMPLES = """\
Examples:
  dlb-launcher --name alpha claude
  dlb-launcher --name bravo codex
  dlb-launcher --name worker-1 gemini --model gemini-2.5-pro

Once the wrapped CLI is up, tell its LLM to register with DLB using the
SAME name you passed here. From then on, mail addressed to that name
wakes this session automatically (via a synthetic prompt injected when
the CLI is idle).

If --name is omitted, the launcher acts as a pure transparent PTY relay
with no wake mechanism — useful for testing the wrapping itself without
DLB involvement.
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="dlb-launcher",
        description=(
            "Wrap an AI-coding-agent CLI with a PTY+DLB watcher so it can be "
            "woken when another DLB-connected thread sends it mail."
        ),
        epilog=USAGE_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--name",
        "-n",
        default=None,
        help=(
            "DLB inbox name to watch for this session. Must match the name "
            "the LLM inside registers with via mcp__dlb__register. If omitted, "
            "no wake watcher runs (pure pass-through PTY)."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"dlb-launcher {__version__}",
    )
    parser.add_argument(
        "cli",
        nargs=argparse.REMAINDER,
        help="The CLI command to wrap, followed by its own arguments.",
    )
    ns = parser.parse_args()

    if not ns.cli:
        parser.error("missing CLI command (e.g. 'claude', 'codex', 'gemini')")

    return run(cli_argv=ns.cli, name=ns.name)


if __name__ == "__main__":
    sys.exit(main())
