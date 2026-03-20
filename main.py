"""Entry point for the Ledger MCP server."""

from ledger.mcp.server import mcp


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
