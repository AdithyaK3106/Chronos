"""Cross-file module resolution and symbol disambiguation linker."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from chronos.core.models import ASTEdge, ASTNode, ImportBinding, utc_now_iso


def _parse_json_with_comments(raw: str) -> dict[str, Any]:
    """Parse JSON containing comments and trailing commas (standard in tsconfig/jsconfig)."""
    cleaned = re.sub(r"//.*?\n|/\*.*?\*/", "\n", raw, flags=re.S)
    cleaned = re.sub(r",\s*([\]}])", r"\1", cleaned)
    try:
        return json.loads(cleaned)
    except Exception:
        return {}


class CrossFileLinker:
    """Resolves cross-file module imports and disambiguates AST call/inheritance graph edges."""

    def __init__(self, repo_root: str | Path):
        self.repo_root = Path(repo_root).resolve()
        self.path_aliases = self._load_tsconfig_aliases()
        self.go_module_name = self._load_go_module_name()

    def _load_tsconfig_aliases(self) -> dict[str, list[str]]:
        """Load path alias mappings from tsconfig.json or jsconfig.json."""
        aliases: dict[str, list[str]] = {}

        # Default standard alias conventions
        aliases["@/*"] = ["src/*", "./*"]
        aliases["~/*"] = ["./*", "src/*"]

        for config_name in ("tsconfig.json", "jsconfig.json"):
            cfg_path = self.repo_root / config_name
            if cfg_path.is_file():
                try:
                    data = _parse_json_with_comments(cfg_path.read_text(encoding="utf-8"))
                    compiler_opts = data.get("compilerOptions", {})
                    base_url = compiler_opts.get("baseUrl", ".")
                    paths = compiler_opts.get("paths", {})
                    for alias_pattern, target_list in paths.items():
                        resolved_targets = []
                        for target in target_list:
                            clean_target = (Path(base_url) / target).as_posix()
                            resolved_targets.append(clean_target)
                        aliases[alias_pattern] = resolved_targets
                except Exception:
                    pass

        return aliases

    def _load_go_module_name(self) -> str | None:
        """Extract Go module name from root go.mod if present."""
        go_mod = self.repo_root / "go.mod"
        if go_mod.is_file():
            try:
                for line in go_mod.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("module "):
                        parts = line.split()
                        if len(parts) >= 2:
                            return parts[1]
            except Exception:
                pass
        return None

    def resolve_ts_js_module(self, source_file: Path, source_module: str) -> Path | str:
        """Resolve a TypeScript/JavaScript module specifier to a concrete file or external tag."""
        candidate: Path | None = None

        if source_module.startswith(("./", "../")):
            candidate = (source_file.parent / source_module).resolve()
        else:
            # Check tsconfig path aliases
            for alias_pat, target_paths in self.path_aliases.items():
                prefix = alias_pat.rstrip("*")
                if source_module.startswith(prefix):
                    rel = source_module[len(prefix):]
                    for tp in target_paths:
                        t_prefix = tp.rstrip("*")
                        cand = (self.repo_root / t_prefix / rel).resolve()
                        if self._probe_ts_js_file(cand):
                            candidate = cand
                            break
                    if candidate:
                        break

        if candidate:
            resolved = self._probe_ts_js_file(candidate)
            if resolved:
                return resolved

        # Unresolved in repository -> third-party / external
        return f"external:{source_module}"

    def _probe_ts_js_file(self, base_path: Path) -> Path | None:
        """Probe potential TS/JS file extensions and index files."""
        if base_path.is_file():
            return base_path

        for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            cand = base_path.with_suffix(ext)
            if cand.is_file():
                return cand

        if base_path.is_dir():
            for idx in ("index.ts", "index.tsx", "index.js", "index.jsx"):
                cand = base_path / idx
                if cand.is_file():
                    return cand

        return None

    def resolve_python_module(self, source_file: Path, source_module: str) -> Path | str:
        """Resolve a Python module specifier (relative or absolute) to a concrete file or external tag."""
        candidate: Path | None = None

        if source_module.startswith("."):
            # Relative import with leading dots
            num_dots = len(source_module) - len(source_module.lstrip("."))
            sub = source_module.lstrip(".")
            base_dir = source_file.parent
            for _ in range(num_dots - 1):
                base_dir = base_dir.parent

            if sub:
                parts = sub.split(".")
                candidate = base_dir.joinpath(*parts)
            else:
                candidate = base_dir
        else:
            # Absolute project import
            parts = source_module.split(".")
            cand1 = self.repo_root.joinpath(*parts)
            cand2 = (self.repo_root / "src").joinpath(*parts)
            for c in (cand1, cand2):
                if self._probe_python_file(c):
                    candidate = c
                    break

        if candidate:
            resolved = self._probe_python_file(candidate)
            if resolved:
                return resolved

        return f"external:{source_module}"

    def _probe_python_file(self, base_path: Path) -> Path | None:
        """Probe Python file or package __init__.py."""
        if base_path.is_file():
            return base_path

        cand = base_path.with_suffix(".py")
        if cand.is_file():
            return cand

        cand_init = base_path / "__init__.py"
        if cand_init.is_file():
            return cand_init

        return None

    def resolve_go_module(self, source_file: Path, source_module: str) -> list[Path] | str:
        """Resolve a Go module specifier to directory package files or external tag."""
        candidate_dir: Path | None = None

        if source_module.startswith(("./", "../")):
            candidate_dir = (source_file.parent / source_module).resolve()
        elif self.go_module_name and source_module.startswith(self.go_module_name):
            rel = source_module[len(self.go_module_name):].lstrip("/")
            candidate_dir = (self.repo_root / rel).resolve()
        else:
            cand = (self.repo_root / source_module).resolve()
            if cand.is_dir():
                candidate_dir = cand

        if candidate_dir and candidate_dir.is_dir():
            go_files = [f for f in candidate_dir.glob("*.go") if f.is_file()]
            if go_files:
                return go_files

        return f"external:{source_module}"

    def resolve_import_binding(
        self, source_file: Path, source_module: str, language: str
    ) -> Path | list[Path] | str:
        """Dispatch resolution based on language."""
        if language in ("typescript", "tsx", "javascript"):
            return self.resolve_ts_js_module(source_file, source_module)
        elif language == "python":
            return self.resolve_python_module(source_file, source_module)
        elif language == "go":
            return self.resolve_go_module(source_file, source_module)
        return f"external:{source_module}"

    def link_repository(
        self,
        nodes: list[ASTNode],
        edges: list[ASTEdge],
        imports: list[ImportBinding],
        valid_at: str | None = None,
    ) -> tuple[list[ASTNode], list[ASTEdge]]:
        """Perform two-phase cross-file linking over the entire repository AST."""
        timestamp = valid_at or utc_now_iso()

        # Phase 1: Build node and intra-package catalogs
        node_catalog: dict[str, dict[str, ASTNode]] = {}
        intra_package_catalog: dict[str, dict[str, ASTNode]] = {}

        for n in nodes:
            norm_path = Path(n.path).as_posix()
            if norm_path not in node_catalog:
                node_catalog[norm_path] = {}
            node_catalog[norm_path][n.name] = n
            # Also index by short symbol name if scoped (e.g. UserService.login -> login)
            if "." in n.name:
                short_name = n.name.split(".")[-1]
                if short_name not in node_catalog[norm_path]:
                    node_catalog[norm_path][short_name] = n

            # Intra-package directory catalog (for Go and same-dir sharing)
            dir_posix = Path(n.path).parent.as_posix()
            if dir_posix not in intra_package_catalog:
                intra_package_catalog[dir_posix] = {}
            intra_package_catalog[dir_posix][n.name] = n
            if "." in n.name:
                short_name = n.name.split(".")[-1]
                if short_name not in intra_package_catalog[dir_posix]:
                    intra_package_catalog[dir_posix][short_name] = n

        # Index imports by source file
        file_named_imports: dict[str, dict[str, ImportBinding]] = {}
        file_namespace_imports: dict[str, dict[str, ImportBinding]] = {}
        resolved_binding_targets: dict[str, dict[str, Path | list[Path] | str]] = {}

        import_edges: list[ASTEdge] = []

        for imp in imports:
            src_norm = Path(imp.source_file).as_posix()
            src_path = Path(imp.source_file).resolve()
            ext = src_path.suffix.lower()

            lang = "typescript"
            if ext in (".js", ".jsx", ".mjs", ".cjs"):
                lang = "javascript"
            elif ext in (".py", ".pyi"):
                lang = "python"
            elif ext == ".go":
                lang = "go"

            target = self.resolve_import_binding(src_path, imp.source_module, lang)

            if src_norm not in file_named_imports:
                file_named_imports[src_norm] = {}
                file_namespace_imports[src_norm] = {}
                resolved_binding_targets[src_norm] = {}

            resolved_binding_targets[src_norm][imp.local_alias] = target

            if imp.imported_symbol == "*":
                file_namespace_imports[src_norm][imp.local_alias] = imp
            else:
                file_named_imports[src_norm][imp.local_alias] = imp

            # Generate explicit IMPORTS edge
            if isinstance(target, Path):
                target_norm = target.as_posix()
                if imp.imported_symbol and imp.imported_symbol != "*":
                    if target_norm in node_catalog and imp.imported_symbol in node_catalog[target_norm]:
                        dst_id = node_catalog[target_norm][imp.imported_symbol].id
                    else:
                        dst_id = f"{target_norm}::{imp.imported_symbol}::Symbol"
                else:
                    dst_id = target_norm
                import_edges.append(
                    ASTEdge(src=src_norm, dst=dst_id, type="IMPORTS", valid_at=timestamp)
                )
            elif isinstance(target, list) and target:
                target_dir = target[0].parent.as_posix()
                import_edges.append(
                    ASTEdge(src=src_norm, dst=target_dir, type="IMPORTS", valid_at=timestamp)
                )
            elif isinstance(target, str) and target.startswith("external:"):
                import_edges.append(
                    ASTEdge(src=src_norm, dst=target, type="IMPORTS", valid_at=timestamp)
                )

        # Phase 2: Disambiguate CALLS and INHERITS edges
        linked_edges: list[ASTEdge] = []

        for edge in edges:
            if edge.type == "IMPORTS":
                linked_edges.append(edge)
                continue

            src_norm = edge.src.split("::")[0]
            callee = edge.dst

            # 1. Intra-file local symbol
            if src_norm in node_catalog and callee in node_catalog[src_norm]:
                resolved_dst = node_catalog[src_norm][callee].id
                linked_edges.append(
                    ASTEdge(
                        src=edge.src,
                        dst=resolved_dst,
                        type=edge.type,
                        valid_at=edge.valid_at or timestamp,
                    )
                )
                continue

            # 2. Named cross-file import in this file
            if src_norm in file_named_imports and callee in file_named_imports[src_norm]:
                binding = file_named_imports[src_norm][callee]
                target = resolved_binding_targets[src_norm].get(callee)
                resolved_dst = self._resolve_target_symbol_id(
                    target, binding.imported_symbol, node_catalog
                )
                linked_edges.append(
                    ASTEdge(
                        src=edge.src,
                        dst=resolved_dst,
                        type=edge.type,
                        valid_at=edge.valid_at or timestamp,
                    )
                )
                continue

            # 3. Namespace / Go package import / External module member (e.g. auth.ValidateUser or axios.get)
            if "." in callee:
                prefix, member = callee.split(".", 1)
                binding = None
                if src_norm in file_namespace_imports and prefix in file_namespace_imports[src_norm]:
                    binding = file_namespace_imports[src_norm][prefix]
                elif src_norm in file_named_imports and prefix in file_named_imports[src_norm]:
                    binding = file_named_imports[src_norm][prefix]

                if binding:
                    target = resolved_binding_targets[src_norm].get(prefix)
                    resolved_dst = self._resolve_target_symbol_id(
                        target, member, node_catalog
                    )
                    linked_edges.append(
                        ASTEdge(
                            src=edge.src,
                            dst=resolved_dst,
                            type=edge.type,
                            valid_at=edge.valid_at or timestamp,
                        )
                    )
                    continue
                else:
                    # Non-import prefix (e.g. obj.method() like s.doLocal() or db.query())
                    if src_norm in node_catalog and member in node_catalog[src_norm]:
                        linked_edges.append(
                            ASTEdge(
                                src=edge.src,
                                dst=node_catalog[src_norm][member].id,
                                type=edge.type,
                                valid_at=edge.valid_at or timestamp,
                            )
                        )
                        continue
                    if src_norm.endswith(".go"):
                        dir_posix = Path(src_norm).parent.as_posix()
                        if dir_posix in intra_package_catalog and member in intra_package_catalog[dir_posix]:
                            linked_edges.append(
                                ASTEdge(
                                    src=edge.src,
                                    dst=intra_package_catalog[dir_posix][member].id,
                                    type=edge.type,
                                    valid_at=edge.valid_at or timestamp,
                                )
                            )
                            continue

            # 4. Go intra-package symbol sharing (same directory, no import statement)
            if src_norm.endswith(".go"):
                dir_posix = Path(src_norm).parent.as_posix()
                if dir_posix in intra_package_catalog and callee in intra_package_catalog[dir_posix]:
                    resolved_dst = intra_package_catalog[dir_posix][callee].id
                    linked_edges.append(
                        ASTEdge(
                            src=edge.src,
                            dst=resolved_dst,
                            type=edge.type,
                            valid_at=edge.valid_at or timestamp,
                        )
                    )
                    continue

            # 5. External package fallback: if callee was imported from external module
            if src_norm in file_named_imports and callee in file_named_imports[src_norm]:
                binding = file_named_imports[src_norm][callee]
                linked_edges.append(
                    ASTEdge(
                        src=edge.src,
                        dst=f"external:{binding.source_module}::{callee}",
                        type=edge.type,
                        valid_at=edge.valid_at or timestamp,
                    )
                )
                continue

            # 6. Retain bare symbol destination if unresolved
            linked_edges.append(edge)

        all_edges = linked_edges + import_edges
        return nodes, all_edges

    def _resolve_target_symbol_id(
        self,
        target: Path | list[Path] | str | None,
        symbol_name: str,
        node_catalog: dict[str, dict[str, ASTNode]],
    ) -> str:
        """Resolve a symbol name to its target node ID or external tag."""
        if isinstance(target, Path):
            t_norm = target.as_posix()
            if t_norm in node_catalog and symbol_name in node_catalog[t_norm]:
                return node_catalog[t_norm][symbol_name].id
            return f"{t_norm}::{symbol_name}::Symbol"
        elif isinstance(target, list) and target:
            # Check package files
            for p_file in target:
                p_norm = p_file.as_posix()
                if p_norm in node_catalog and symbol_name in node_catalog[p_norm]:
                    return node_catalog[p_norm][symbol_name].id
            return f"{target[0].parent.as_posix()}::{symbol_name}::Symbol"
        elif isinstance(target, str) and target.startswith("external:"):
            return f"{target}::{symbol_name}"

        return symbol_name

    def link_file(
        self,
        file_path: Path,
        nodes: list[ASTNode],
        edges: list[ASTEdge],
        imports: list[ImportBinding],
        graph_store: Any,
        valid_at: str | None = None,
    ) -> tuple[list[ASTNode], list[ASTEdge]]:
        """Link a single modified file incrementally against an active GraphStore."""
        timestamp = valid_at or utc_now_iso()
        norm_source = file_path.resolve().as_posix()
        ext = file_path.suffix.lower()

        lang = "typescript"
        if ext in (".js", ".jsx", ".mjs", ".cjs"):
            lang = "javascript"
        elif ext in (".py", ".pyi"):
            lang = "python"
        elif ext == ".go":
            lang = "go"

        # Index local file nodes
        local_nodes = {n.name: n for n in nodes}
        for n in nodes:
            if "." in n.name:
                short = n.name.split(".")[-1]
                if short not in local_nodes:
                    local_nodes[short] = n

        import_edges: list[ASTEdge] = []
        file_named_imports: dict[str, ImportBinding] = {}
        file_namespace_imports: dict[str, ImportBinding] = {}
        resolved_targets: dict[str, Path | list[Path] | str] = {}

        for imp in imports:
            target = self.resolve_import_binding(file_path, imp.source_module, lang)
            resolved_targets[imp.local_alias] = target

            if imp.imported_symbol == "*":
                file_namespace_imports[imp.local_alias] = imp
            else:
                file_named_imports[imp.local_alias] = imp

            if isinstance(target, Path):
                t_norm = target.as_posix()
                dst_id = (
                    f"{t_norm}::{imp.imported_symbol}::Symbol"
                    if (imp.imported_symbol and imp.imported_symbol != "*")
                    else t_norm
                )
                import_edges.append(
                    ASTEdge(src=norm_source, dst=dst_id, type="IMPORTS", valid_at=timestamp)
                )
            elif isinstance(target, list) and target:
                import_edges.append(
                    ASTEdge(
                        src=norm_source,
                        dst=target[0].parent.as_posix(),
                        type="IMPORTS",
                        valid_at=timestamp,
                    )
                )
            elif isinstance(target, str) and target.startswith("external:"):
                import_edges.append(
                    ASTEdge(src=norm_source, dst=target, type="IMPORTS", valid_at=timestamp)
                )

        # Disambiguate outgoing edges
        linked_edges: list[ASTEdge] = []

        for edge in edges:
            callee = edge.dst

            # 1. Local symbol
            if callee in local_nodes:
                linked_edges.append(
                    ASTEdge(
                        src=edge.src,
                        dst=local_nodes[callee].id,
                        type=edge.type,
                        valid_at=edge.valid_at or timestamp,
                    )
                )
                continue

            # 2. Named cross-file import
            if callee in file_named_imports:
                binding = file_named_imports[callee]
                target = resolved_targets.get(callee)
                resolved_dst = self._resolve_with_store(
                    target, binding.imported_symbol, graph_store
                )
                linked_edges.append(
                    ASTEdge(
                        src=edge.src,
                        dst=resolved_dst,
                        type=edge.type,
                        valid_at=edge.valid_at or timestamp,
                    )
                )
                continue

            # 3. Namespace / Go package import / External module member
            if "." in callee:
                prefix, member = callee.split(".", 1)
                binding = file_namespace_imports.get(prefix) or file_named_imports.get(prefix)
                if binding:
                    target = resolved_targets.get(prefix)
                    resolved_dst = self._resolve_with_store(target, member, graph_store)
                    linked_edges.append(
                        ASTEdge(
                            src=edge.src,
                            dst=resolved_dst,
                            type=edge.type,
                            valid_at=edge.valid_at or timestamp,
                        )
                    )
                    continue
                else:
                    if member in local_nodes:
                        linked_edges.append(
                            ASTEdge(
                                src=edge.src,
                                dst=local_nodes[member].id,
                                type=edge.type,
                                valid_at=edge.valid_at or timestamp,
                            )
                        )
                        continue

            # 4. Go same-package (intra-package)
            if ext == ".go" and hasattr(graph_store, "get_nodes_by_name"):
                dir_posix = file_path.parent.as_posix()
                matching = graph_store.get_nodes_by_name(callee)
                same_dir_match = [
                    m for m in matching if Path(m.get("path", "")).parent.as_posix() == dir_posix
                ]
                if same_dir_match:
                    linked_edges.append(
                        ASTEdge(
                            src=edge.src,
                            dst=same_dir_match[0]["id"],
                            type=edge.type,
                            valid_at=edge.valid_at or timestamp,
                        )
                    )
                    continue

            # 5. Unresolved fallback
            linked_edges.append(edge)

        return nodes, linked_edges + import_edges

    def _resolve_with_store(
        self, target: Path | list[Path] | str | None, symbol_name: str, graph_store: Any
    ) -> str:
        """Resolve symbol ID using the graph store if available."""
        if isinstance(target, Path):
            t_norm = target.as_posix()
            if hasattr(graph_store, "get_nodes_by_name"):
                matches = graph_store.get_nodes_by_name(symbol_name)
                for m in matches:
                    if Path(m.get("path", "")).as_posix() == t_norm:
                        return m["id"]
            return f"{t_norm}::{symbol_name}::Symbol"
        elif isinstance(target, list) and target:
            paths = {p.as_posix() for p in target}
            if hasattr(graph_store, "get_nodes_by_name"):
                matches = graph_store.get_nodes_by_name(symbol_name)
                for m in matches:
                    if Path(m.get("path", "")).as_posix() in paths:
                        return m["id"]
            return f"{target[0].parent.as_posix()}::{symbol_name}::Symbol"
        elif isinstance(target, str) and target.startswith("external:"):
            return f"{target}::{symbol_name}"

        return symbol_name
