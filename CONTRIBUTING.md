# Contributing to Chronos

Thank you for your interest in contributing to Chronos! We welcome contributions of all kinds: bug fixes, documentation improvements, new features, and feedback.

---

## 🧭 Code of Conduct

All contributors and participants are expected to adhere to our [Code of Conduct](CODE_OF_CONDUCT.md). Please treat others with respect and consideration.

---

## 🛠️ Development Setup

### 1. Prerequisites

- **Python:** 3.10, 3.11, or 3.12
- **Git** with submodule support
- **C/C++ Build Toolchain:**
  - **Linux / macOS:** GCC or Clang (`build-essential` on Ubuntu/Debian, Xcode Command Line Tools on macOS)
  - **Windows:** MSYS2 / MinGW-w64 (`pacman -S mingw-w64-ucrt-x86_64-gcc make`) or Visual Studio C++ Build Tools
- **ast-grep:** `pip install ast-grep-cli`
- **Open Policy Agent (OPA):** Optional for local Wedge 4 policy testing (`v1.19.0+`)

### 2. Clone and Install

```bash
# Clone the repository with submodules
git clone --recurse-submodules https://github.com/AdithyaK3106/Chronos.git
cd Chronos

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\Activate.ps1

# Install in editable mode with development/test dependencies
pip install --upgrade pip
pip install -e '.[test]'

# Build the local indexer (takes ~3-5 minutes on first run)
python -m chronos.build_cbm
```

### 3. Verify Environment Health

Run the diagnostic tool at any time:

```bash
chronos doctor
```

`chronos doctor` checks Python version, C toolchain, Kùzu graph driver, SQLite database integrity, and MCP server configuration.

---

## 🧪 Running Tests

Chronos uses `pytest` for running automated tests:

```bash
# Run all unit and integration suites
pytest

# Run tests with short progress
pytest tests/ -q

# Run a specific test suite directly
python tests/test_chronos.py
python tests/test_wedge2.py
python tests/test_wedge3.py
python tests/test_wedge4.py
python tests/test_unification.py
```

---

## 🏛️ Codebase Organization

Chronos is structured into four core architectural wedges and enterprise governance controls:

```
chronos/
├── __init__.py          # Package initialization & version
├── cli.py               # Main CLI commands (init, doctor, health, enforce, etc.)
├── server.py            # Unified MCP stdio server exposing 26 tools
├── curator.py           # AST indexing and temporal graph synchronization (Wedge 1)
├── reflector.py         # Self-healing policy playbook engine (Wedge 2)
├── ledger.py            # Sub-file AST node locking & intent ledger (Wedge 3)
├── enforcer.py          # Deterministic CI merge gate enforcer (Wedge 4)
├── auth.py              # Cryptographic identity, tokens, permissions (F1, F2)
├── anomaly.py           # Behavioral anomaly detection & baselines (F6)
├── audit.py             # Append-only SHA-256 hash-chained audit log (F4)
└── policies/            # OPA / Rego policy rules
```

---

## 📋 Pull Request Guidelines

1. **Create a topic branch:** Branch off `master` with a descriptive name (e.g. `feat/ast-cache`, `fix/lock-ttl`).
2. **Write tests:** Add tests in `tests/` covering any new functionality or regression fixes.
3. **Ensure tests pass:** Run `pytest tests/ -q` before opening a PR.
4. **Follow commit conventions:** Use conventional commit messages (`feat: ...`, `fix: ...`, `docs: ...`, `chore: ...`).
5. **Keep PRs focused:** Submit smaller, focused pull requests rather than monolithic changes.

---

## 🔒 Security

For responsible disclosure of security vulnerabilities, please refer to our [Security Policy](SECURITY.md). Do NOT report security issues via public GitHub issues.
