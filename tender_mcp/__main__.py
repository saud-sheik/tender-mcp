"""
tender_mcp/__main__.py  –  makes `python -m tender_mcp` work
"""
from tender_mcp.server import mcp
import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)

if __name__ == "__main__":
    mcp.run(transport="stdio")
