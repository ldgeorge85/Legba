"""
Filesystem tools: fs_read, fs_write, fs_list

When a SelfModEngine is provided, fs_write calls targeting /agent/* are
routed through propose_and_apply() for git tracking.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ....shared.schemas.tools import ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from ...selfmod.engine import SelfModEngine
    from ....shared.schemas.cycle import CycleState
    from ..registry import ToolRegistry


def get_definitions() -> list[tuple[ToolDefinition, Any]]:
    """Return (definition, handler) pairs for all filesystem tools."""
    return [
        (FS_READ_DEF, fs_read),
        (FS_WRITE_DEF, fs_write),
        (FS_LIST_DEF, fs_list),
    ]


def register(
    registry: ToolRegistry,
    *,
    selfmod: SelfModEngine | None = None,
    agent_prefix: str | None = None,
    state: CycleState | None = None,
) -> None:
    """Register filesystem tools. If selfmod is provided, fs_write intercepts /agent writes."""
    registry.register(FS_READ_DEF, fs_read)
    registry.register(FS_LIST_DEF, fs_list)

    if selfmod and agent_prefix and state:
        prefix = agent_prefix.rstrip("/")

        async def selfmod_fs_write(args: dict) -> str:
            path_str = args.get("path", "")
            if not path_str.startswith(prefix + "/") and path_str != prefix:
                return await fs_write(args)

            content = args.get("content", "")
            append = args.get("append", False)
            rel_path = path_str[len(prefix):].lstrip("/")
            if not rel_path:
                return "Error: cannot write to /agent directory itself"

            full_path = Path(path_str)
            if append and full_path.exists():
                existing = full_path.read_text(errors="replace")
                content = existing + content

            try:
                await selfmod.propose_and_apply(
                    file_path=rel_path,
                    new_content=content,
                    rationale="Agent-initiated file write via fs_write",
                    expected_outcome="File created/updated at /agent/" + rel_path,
                    cycle_number=state.cycle_number,
                )
                state.self_modifications += 1
                return (
                    f"Written {len(content)} bytes to {path_str} "
                    f"(self-modification tracked, git committed)"
                )
            except Exception as e:
                return f"Error writing {path_str}: {e}"

        registry.register(FS_WRITE_DEF, selfmod_fs_write)
    else:
        registry.register(FS_WRITE_DEF, fs_write)


FS_READ_DEF = ToolDefinition(
    name="fs_read",
    description="Read a file from the filesystem",
    parameters=[
        ToolParameter(name="path", type="string", description="Absolute path to the file"),
        ToolParameter(name="offset", type="number", description="Line number to start from (0-indexed)", required=False),
        ToolParameter(name="limit", type="number", description="Maximum number of lines to read", required=False),
    ],
)

FS_WRITE_DEF = ToolDefinition(
    name="fs_write",
    description="Write or create a file on the filesystem",
    parameters=[
        ToolParameter(name="path", type="string", description="Absolute path to the file"),
        ToolParameter(name="content", type="string", description="Content to write"),
        ToolParameter(name="append", type="boolean", description="Append instead of overwrite", required=False),
    ],
)

FS_LIST_DEF = ToolDefinition(
    name="fs_list",
    description="List directory contents",
    parameters=[
        ToolParameter(name="path", type="string", description="Absolute path to the directory"),
        ToolParameter(name="recursive", type="boolean", description="List recursively", required=False),
    ],
)


async def fs_read(args: dict) -> str:
    path = Path(args["path"])
    if not path.exists():
        return f"Error: File not found: {path}"
    if not path.is_file():
        return f"Error: Not a file: {path}"

    try:
        content = path.read_text(errors="replace")
        lines = content.split("\n")

        offset = int(args.get("offset", 0))
        limit = args.get("limit")

        if limit is not None:
            lines = lines[offset : offset + int(limit)]
        elif offset > 0:
            lines = lines[offset:]

        return "\n".join(lines)
    except Exception as e:
        return f"Error reading {path}: {e}"


async def fs_write(args: dict) -> str:
    path = Path(args["path"])
    content = args.get("content", "")
    append = args.get("append", False)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        path.write_text(content) if not append else path.open(mode).write(content)
        return f"Written {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error writing {path}: {e}"


async def fs_list(args: dict) -> str:
    path = Path(args["path"])
    if not path.exists():
        return f"Error: Directory not found: {path}"
    if not path.is_dir():
        return f"Error: Not a directory: {path}"

    try:
        recursive = args.get("recursive", False)
        entries = []

        if recursive:
            for item in sorted(path.rglob("*")):
                rel = item.relative_to(path)
                prefix = "d" if item.is_dir() else "f"
                size = item.stat().st_size if item.is_file() else 0
                entries.append(f"[{prefix}] {rel} ({size}B)")
        else:
            for item in sorted(path.iterdir()):
                prefix = "d" if item.is_dir() else "f"
                size = item.stat().st_size if item.is_file() else 0
                entries.append(f"[{prefix}] {item.name} ({size}B)")

        return "\n".join(entries) if entries else "(empty directory)"
    except Exception as e:
        return f"Error listing {path}: {e}"
