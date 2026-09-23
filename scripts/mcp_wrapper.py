import sys
import runpy
import traceback

if __name__ == "__main__":
    try:
        with open("C:\\Users\\urbra\\OneDrive\\Desktop\\Projects\\New ortho\\mcp_debug.log", "a") as f:
            f.write("Starting chronos.server...\n")
        runpy.run_module('chronos.server', run_name='__main__')
    except Exception as e:
        with open("C:\\Users\\urbra\\OneDrive\\Desktop\\Projects\\New ortho\\mcp_debug.log", "a") as f:
            f.write("Exception:\n" + traceback.format_exc() + "\n")
        sys.exit(1)
