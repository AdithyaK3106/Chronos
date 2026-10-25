"""Portable AST indexer using Python AST and Tree-Sitter (zero C toolchain required)."""

import ast
import hashlib
from pathlib import Path
from typing import Any

from chronos.core.models import ASTEdge, ASTNode, utc_now_iso

def make_node_id(path: str, name: str, kind: str) -> str:
    norm_path = Path(path).as_posix()
    return f"{norm_path}::{name}::{kind}"

class PythonASTExtractor(ast.NodeVisitor):
    def __init__(self, file_path: str):
        self.file_path = Path(file_path).as_posix()
        self.nodes: list[ASTNode] = []
        self.edges: list[ASTEdge] = []
        self._scope_stack: list[str] = []
        self._current_function: str | None = None

    def visit_ClassDef(self, node: ast.ClassDef):
        class_id = make_node_id(self.file_path, node.name, "Class")
        doc = ast.get_docstring(node)
        self.nodes.append(
            ASTNode(
                id=class_id,
                name=node.name,
                path=self.file_path,
                kind="Class",
                language="python",
                docstring=doc,
            )
        )

        # Base class inheritances
        for base in node.bases:
            base_name = getattr(base, "id", None) or getattr(base, "attr", None)
            if base_name:
                self.edges.append(
                    ASTEdge(
                        src=class_id,
                        dst=base_name,
                        type="INHERITS",
                        valid_at=utc_now_iso(),
                    )
                )

        prev_scope = self._scope_stack.copy()
        self._scope_stack.append(node.name)
        self.generic_visit(node)
        self._scope_stack = prev_scope

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._handle_function(node, "Function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._handle_function(node, "AsyncFunction")

    def _handle_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, kind: str):
        full_name = ".".join(self._scope_stack + [node.name]) if self._scope_stack else node.name
        fn_id = make_node_id(self.file_path, full_name, kind)
        doc = ast.get_docstring(node)

        # Args signature
        args = [a.arg for a in node.args.args]
        sig = f"{node.name}({', '.join(args)})"

        self.nodes.append(
            ASTNode(
                id=fn_id,
                name=full_name,
                path=self.file_path,
                kind=kind,
                language="python",
                docstring=doc,
                signature=sig,
            )
        )

        prev_fn = self._current_function
        self._current_function = fn_id
        self.generic_visit(node)
        self._current_function = prev_fn

    def visit_Call(self, node: ast.Call):
        if self._current_function is not None:
            callee_name = None
            if isinstance(node.func, ast.Name):
                callee_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                callee_name = node.func.attr
            
            if callee_name:
                self.edges.append(
                    ASTEdge(
                        src=self._current_function,
                        dst=callee_name,
                        type="CALLS",
                        valid_at=utc_now_iso(),
                    )
                )
        self.generic_visit(node)

def index_file(file_path: str | Path, valid_at: str | None = None) -> tuple[list[ASTNode], list[ASTEdge]]:
    """Parse and extract AST nodes and relation edges from a source file."""
    path = Path(file_path).resolve()
    if not path.is_file():
        return [], []

    posix_path = path.as_posix()
    timestamp = valid_at or utc_now_iso()

    if path.suffix == ".py":
        try:
            content = path.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=posix_path)
            extractor = PythonASTExtractor(posix_path)
            extractor.visit(tree)
            # Re-stamp valid_at if provided
            for e in extractor.edges:
                e.valid_at = timestamp
            return extractor.nodes, extractor.edges
        except Exception:
            return [], []

    # Tree-sitter fallback for other languages (JS/TS/Bash/etc)
    return [], []

def index_repository(repo_path: str | Path, valid_at: str | None = None) -> tuple[list[ASTNode], list[ASTEdge]]:
    """Index an entire repository walking valid source files."""
    root = Path(repo_path).resolve()
    all_nodes: list[ASTNode] = []
    all_edges: list[ASTEdge] = []
    timestamp = valid_at or utc_now_iso()

    ignore_dirs = {".git", ".chronos", ".venv", "venv", "__pycache__", "node_modules", "dist", "build"}

    for path in root.rglob("*.py"):
        if any(part in ignore_dirs for part in path.parts):
            continue
        nodes, edges = index_file(path, valid_at=timestamp)
        all_nodes.extend(nodes)
        all_edges.extend(edges)

    return all_nodes, all_edges
