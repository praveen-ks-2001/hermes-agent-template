#!/usr/bin/env python3
"""Apply template-owned safety guards to the pinned Hermes source tree.

The Railway template currently runs Hermes' local terminal backend in the same
PID namespace as the public controller.  Until those workloads are separated,
signalling PID 1 from an agent terminal would terminate the whole deployment.
Patch Hermes' unconditional command floor during the image build and fail the
build if the pinned upstream source has drifted away from the reviewed anchor.
"""

from __future__ import annotations

import argparse
from pathlib import Path


# Mirrors Hermes v2026.7.1's command-position anchor.  Keeping the complete
# expression here makes the exact injected regex directly unit-testable without
# importing Hermes and all of its runtime dependencies into this template.
COMMAND_POSITION_PATTERN = (
    r"(?:^|[;&|\n`]|\$\()"
    r"\s*"
    r"(?:sudo\s+(?:-[^\s]+\s+)*)?"
    r"(?:env\s+(?:\w+=\S*\s+)*)?"
    r"(?:(?:exec|nohup|setsid|time)\s+)*"
    r"\s*"
)

# Match a real kill command at a shell command position, including the common
# absolute binary paths and signal syntaxes.  PID 1 may appear anywhere in the
# target list, but PID prefixes such as 10/11 and command text passed as data do
# not match.
PID1_PATTERN = (
    COMMAND_POSITION_PATTERN
    + r"(?:command\s+|builtin\s+)*"
    + r"(?:/(?:usr/)?bin/)?kill\s+"
    + r"(?:(?:-(?:s|signal)|--signal)\s+\S+\s+|(?:-[^\s]+\s+|--\s+))*"
    + r"(?:[^\s;&|`]+\s+)*"
    + r"[\"']?1[\"']?(?=\s|$|[;&|)`])"
)
PID1_DESCRIPTION = "signal container init process (PID 1)"

# This exact line is present in the pinned v2026.7.1 approval floor.  An exact
# anchor is intentional: a Hermes version bump must re-review the safety layer
# instead of silently producing an image without the local hardening.
UPSTREAM_ANCHOR = (
    "    (r'\\bkill\\s+(-[^\\s]+\\s+)*-1\\b', \"kill all processes\"),\n"
)
PID1_ENTRY = f"    ({PID1_PATTERN!r}, {PID1_DESCRIPTION!r}),\n"


def patch_source(source: str) -> str:
    """Return approval.py with the PID 1 guard inserted exactly once."""
    if PID1_DESCRIPTION in source:
        return source

    anchor_count = source.count(UPSTREAM_ANCHOR)
    if anchor_count != 1:
        raise RuntimeError(
            "Hermes approval.py hardline anchor changed "
            f"(expected once, found {anchor_count}); review the new upstream "
            "guard implementation before rebuilding"
        )
    return source.replace(UPSTREAM_ANCHOR, UPSTREAM_ANCHOR + PID1_ENTRY, 1)


def patch_file(path: Path) -> bool:
    """Patch ``path`` atomically enough for an image-build layer.

    Returns True when the file changed and False when it was already hardened.
    """
    original = path.read_text(encoding="utf-8")
    patched = patch_source(original)
    if patched == original:
        return False
    path.write_text(patched, encoding="utf-8")
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("approval_file", type=Path)
    args = parser.parse_args()
    changed = patch_file(args.approval_file)
    action = "patched" if changed else "already hardened"
    print(f"[build-hardening] {action}: {args.approval_file}")


if __name__ == "__main__":
    main()
