"""Build the vendored codebase-memory-mcp indexer from source.

    python -m chronos.build_cbm

Upstream's Makefile is the build; this only locates a usable toolchain and
invokes it. On Windows that means MSYS2, which needs two things Git Bash does not
provide: the ucrt64 gcc on PATH, and a writable TMPDIR (without it the compiler
tries C:\\WINDOWS\\ and every compile fails with a confusing "Error 127").
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .indexer import BINARY, VENDOR

MSYS2_BASH = Path("C:/msys64/usr/bin/bash.exe")
JOBS = os.cpu_count() or 4


def _run_posix() -> int:
    make = shutil.which("make") or shutil.which("gmake")
    if not make:
        sys.exit("no `make` found; install build-essential (or Xcode CLI tools) and retry")
    if not (shutil.which("cc") or shutil.which("gcc")):
        sys.exit("no C compiler found; install gcc/clang and retry")
    return subprocess.call([make, "-f", "Makefile.cbm", "cbm", f"-j{JOBS}"], cwd=VENDOR)


def _run_windows() -> int:
    if not MSYS2_BASH.is_file():
        sys.exit(
            f"MSYS2 not found at {MSYS2_BASH}\n"
            "  install it from https://www.msys2.org/ then run:\n"
            "    pacman -S --needed mingw-w64-ucrt-x86_64-gcc make"
        )
    # Translate the vendor dir into an MSYS2 path, then build inside its shell so
    # `cc` resolves and TMPDIR is writable.
    win = str(VENDOR).replace("\\", "/")
    conv = subprocess.run([str(MSYS2_BASH), "-lc", f"cygpath -u '{win}'"],
                          capture_output=True, text=True)
    msys_dir = conv.stdout.strip() or win
    script = (
        f"cd '{msys_dir}' && export PATH=/ucrt64/bin:$PATH && "
        f"export TMPDIR=/tmp TMP=/tmp TEMP=/tmp && "
        f"make -f Makefile.cbm cbm -j{JOBS}"
    )
    return subprocess.call([str(MSYS2_BASH), "-lc", script])


def main() -> int:
    if not (VENDOR / "Makefile.cbm").is_file():
        sys.exit(
            f"vendored source missing at {VENDOR}\n"
            "  fetch it with:  git submodule update --init --depth 1"
        )
    print(f"building vendored indexer in {VENDOR} (-j{JOBS}); first build takes a few minutes")
    rc = _run_windows() if sys.platform == "win32" else _run_posix()
    if rc != 0:
        sys.exit(f"build failed (exit {rc})")

    built = BINARY if BINARY.is_file() else BINARY.with_suffix(".exe")
    if not built.is_file():
        sys.exit(f"build reported success but no binary at {BINARY}")
    print(f"built: {built} ({built.stat().st_size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
