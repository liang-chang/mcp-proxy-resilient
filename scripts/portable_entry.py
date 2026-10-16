"""Console entry point for the portable Windows distribution."""
# ruff: noqa: INP001

import ctypes
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()

    from mcp_proxy.__main__ import main

    # PyInstaller's DLL directory must not leak into external MCP child processes.
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        if not ctypes.windll.kernel32.SetDllDirectoryW(None):
            raise ctypes.WinError()

    main()
