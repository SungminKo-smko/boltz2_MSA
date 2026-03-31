"""Boltz-2 MCP Server — stdio 진입점 (로컬 개발용).

Usage:
    python -m boltz2_service.mcp.stdio

Claude Code 등록:
    claude mcp add boltz2 python3 -m boltz2_service.mcp.stdio
"""

from platform_core.db import init_db
from boltz2_service.mcp.server import mcp


def main() -> None:
    init_db()
    mcp.run()


if __name__ == "__main__":
    main()
