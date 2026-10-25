"""Portable AST indexer using Python AST and Tree-Sitter (zero C toolchain required)."""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any, Optional

from chronos.core.models import ASTEdge, ASTNode, utc_now_iso

SUPPORTED_EXTENSIONS: set[str] = {
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".go",
}

DEFAULT_IGNORE_DIRS: set[str] = {
    ".git",
    ".chronos",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".turbo",
    ".output",
    "coverage",
    "vendor",
    "target",
    ".cache",
}

EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
}

def is_supported_source_file(file_path: str | Path) -> bool:
    """Return True if the file has a supported polyglot source extension."""
    return Path(file_path).suffix.lower() in SUPPORTED_EXTENSIONS

def make_node_id(path: str, name: str, kind: str) -> str:
    norm_path = Path(path).as_posix()
    return f"{norm_path}::{name}::{kind}"

def clean_comment(raw: str | None) -> str | None:
    """Clean multi-line or single-line doc comment into plain docstring text."""
    if not raw:
        return None
    lines: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if line.endswith("*/"):
            line = line[:-2].strip()
        if line.startswith("/**"):
            line = line[3:].strip()
        elif line.startswith("/*"):
            line = line[2:].strip()
        elif line.startswith("//"):
            line = line[2:].strip()
        elif line.startswith("*"):
            line = line[1:].strip()
        if line:
            lines.append(line)
    return "\n".join(lines) if lines else None

def _extract_preceding_comment(node: Any) -> str | None:
    """Extract preceding doc comment from tree-sitter node or its parent declaration."""
    curr = node
    while curr.parent and curr.parent.type in (
        "export_statement",
        "type_declaration",
        "lexical_declaration",
        "variable_declaration",
    ):
        curr = curr.parent
    prev = curr.prev_sibling
    comments: list[str] = []
    while prev and prev.type in ("comment", "line_comment", "block_comment"):
        comments.insert(0, prev.text.decode("utf-8", errors="replace"))
        prev = prev.prev_sibling
    if not comments:
        return None
    return clean_comment("\n".join(comments))

def _extract_go_receiver_type(recv_node: Any) -> str | None:
    """Extract base receiver struct/type name from Go method receiver node."""
    if not recv_node:
        return None
    for child in recv_node.children:
        if child.type == "parameter_declaration":
            t_node = child.child_by_field_name("type")
            if t_node:
                if t_node.type == "pointer_type":
                    for sub in t_node.children:
                        if sub.type == "type_identifier":
                            return sub.text.decode("utf-8")
                elif t_node.type == "type_identifier":
                    return t_node.text.decode("utf-8")
                return t_node.text.decode("utf-8").lstrip("*")
    return None

def _extract_python_docstring(node: Any) -> str | None:
    """Extract docstring string literal from a tree-sitter Python block."""
    body = node.child_by_field_name("body")
    if not body:
        return None
    for child in body.children:
        if child.type == "expression_statement":
            for sub in child.children:
                if sub.type == "string":
                    text = sub.text.decode("utf-8", errors="replace").strip()
                    if text.startswith(('"""', "'''")):
                        text = text[3:-3].strip()
                    elif text.startswith(('"', "'")):
                        text = text[1:-1].strip()
                    return text or None
            break
        elif child.type not in ("comment",):
            break
    return None


# --- Tree-Sitter Language & Parser Registry ---

_HAVE_TREE_SITTER: Optional[bool] = None
_LANGUAGES: dict[str, Any] = {}
_PARSERS: dict[str, Any] = {}

def _check_tree_sitter() -> bool:
    global _HAVE_TREE_SITTER
    if _HAVE_TREE_SITTER is None:
        try:
            import tree_sitter  # noqa: F401
            _HAVE_TREE_SITTER = True
        except ImportError:
            _HAVE_TREE_SITTER = False
    return _HAVE_TREE_SITTER

def get_tree_sitter_language(lang_key: str) -> Any | None:
    """Lazily load Tree-Sitter Language object for a given language key."""
    if not _check_tree_sitter():
        return None
    if lang_key in _LANGUAGES:
        return _LANGUAGES[lang_key]

    import tree_sitter

    lang_obj = None
    try:
        if lang_key == "typescript":
            import tree_sitter_typescript
            lang_obj = tree_sitter.Language(tree_sitter_typescript.language_typescript())
        elif lang_key == "tsx":
            import tree_sitter_typescript
            lang_obj = tree_sitter.Language(tree_sitter_typescript.language_tsx())
        elif lang_key == "javascript":
            import tree_sitter_javascript
            lang_obj = tree_sitter.Language(tree_sitter_javascript.language())
        elif lang_key == "go":
            import tree_sitter_go
            lang_obj = tree_sitter.Language(tree_sitter_go.language())
        elif lang_key == "python":
            import tree_sitter_python
            lang_obj = tree_sitter.Language(tree_sitter_python.language())
    except (ImportError, Exception):
        lang_obj = None

    _LANGUAGES[lang_key] = lang_obj
    return lang_obj

def get_tree_sitter_parser(lang_key: str) -> Any | None:
    """Lazily load Tree-Sitter Parser for a given language key."""
    if lang_key in _PARSERS:
        return _PARSERS[lang_key]

    lang = get_tree_sitter_language(lang_key)
    if not lang:
        return None

    import tree_sitter
    parser = tree_sitter.Parser(lang)
    _PARSERS[lang_key] = parser
    return parser


# --- Python Native AST Extractor (Fast Path) ---

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


# --- Polyglot Tree-Sitter AST Extractor ---

class TreeSitterExtractor:
    """AST node and relation edge extractor using Tree-Sitter grammars."""

    def __init__(self, file_path: str, language_name: str, valid_at: str | None = None):
        self.file_path = Path(file_path).as_posix()
        self.language_name = language_name
        self.timestamp = valid_at or utc_now_iso()
        self.nodes: list[ASTNode] = []
        self.edges: list[ASTEdge] = []
        self._scope_stack: list[str] = []
        self._current_function: str | None = None

    def extract(self, root_node: Any) -> tuple[list[ASTNode], list[ASTEdge]]:
        """Walk AST and extract code nodes and relational edges."""
        if self.language_name in ("typescript", "tsx", "javascript"):
            self._visit_ts_js(root_node)
        elif self.language_name == "go":
            self._visit_go(root_node)
        elif self.language_name == "python":
            self._visit_python(root_node)
        return self.nodes, self.edges

    def _visit_ts_js(self, node: Any):
        t = node.type

        if t == "class_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = name_node.text.decode("utf-8")
                full_name = ".".join(self._scope_stack + [name]) if self._scope_stack else name
                class_id = make_node_id(self.file_path, full_name, "Class")
                doc = _extract_preceding_comment(node)
                self.nodes.append(
                    ASTNode(
                        id=class_id,
                        name=full_name,
                        path=self.file_path,
                        kind="Class",
                        language=self.language_name,
                        docstring=doc,
                    )
                )

                # Inheritance / implements
                heritage = [c for c in node.children if c.type == "class_heritage"]
                if heritage:
                    for h_child in heritage[0].children:
                        if h_child.type == "extends_clause":
                            for sub in h_child.children:
                                if sub.type in ("identifier", "type_identifier", "member_expression"):
                                    self.edges.append(
                                        ASTEdge(
                                            src=class_id,
                                            dst=sub.text.decode("utf-8"),
                                            type="INHERITS",
                                            valid_at=self.timestamp,
                                        )
                                    )
                        elif h_child.type == "implements_clause":
                            for sub in h_child.children:
                                if sub.type in ("identifier", "type_identifier"):
                                    self.edges.append(
                                        ASTEdge(
                                            src=class_id,
                                            dst=sub.text.decode("utf-8"),
                                            type="INHERITS",
                                            valid_at=self.timestamp,
                                        )
                                    )

                prev_scope = list(self._scope_stack)
                self._scope_stack.append(name)
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        self._visit_ts_js(child)
                self._scope_stack = prev_scope
                return

        elif t == "interface_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = name_node.text.decode("utf-8")
                full_name = ".".join(self._scope_stack + [name]) if self._scope_stack else name
                iface_id = make_node_id(self.file_path, full_name, "Interface")
                doc = _extract_preceding_comment(node)
                self.nodes.append(
                    ASTNode(
                        id=iface_id,
                        name=full_name,
                        path=self.file_path,
                        kind="Interface",
                        language=self.language_name,
                        docstring=doc,
                    )
                )
                for c in node.children:
                    if c.type == "extends_type_clause":
                        for sub in c.children:
                            if sub.type in ("identifier", "type_identifier"):
                                self.edges.append(
                                    ASTEdge(
                                        src=iface_id,
                                        dst=sub.text.decode("utf-8"),
                                        type="INHERITS",
                                        valid_at=self.timestamp,
                                    )
                                )
                return

        elif t == "type_alias_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = name_node.text.decode("utf-8")
                full_name = ".".join(self._scope_stack + [name]) if self._scope_stack else name
                t_id = make_node_id(self.file_path, full_name, "TypeAlias")
                doc = _extract_preceding_comment(node)
                self.nodes.append(
                    ASTNode(
                        id=t_id,
                        name=full_name,
                        path=self.file_path,
                        kind="TypeAlias",
                        language=self.language_name,
                        docstring=doc,
                    )
                )
                return

        elif t == "method_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                m_name = name_node.text.decode("utf-8")
                full_name = ".".join(self._scope_stack + [m_name]) if self._scope_stack else m_name
                m_id = make_node_id(self.file_path, full_name, "Method")
                doc = _extract_preceding_comment(node)
                params_node = node.child_by_field_name("parameters")
                params_text = params_node.text.decode("utf-8") if params_node else "()"
                ret_node = node.child_by_field_name("return_type")
                ret_text = ret_node.text.decode("utf-8") if ret_node else ""
                sig = f"{m_name}{params_text}{ret_text}"
                self.nodes.append(
                    ASTNode(
                        id=m_id,
                        name=full_name,
                        path=self.file_path,
                        kind="Method",
                        language=self.language_name,
                        docstring=doc,
                        signature=sig,
                    )
                )

                prev_fn = self._current_function
                self._current_function = m_id
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        self._visit_ts_js(child)
                self._current_function = prev_fn
                return

        elif t in ("function_declaration", "generator_function_declaration"):
            name_node = node.child_by_field_name("name")
            fn_name = name_node.text.decode("utf-8") if name_node else "anonymous"
            full_name = ".".join(self._scope_stack + [fn_name]) if self._scope_stack else fn_name
            fn_id = make_node_id(self.file_path, full_name, "Function")
            doc = _extract_preceding_comment(node)
            params_node = node.child_by_field_name("parameters")
            params_text = params_node.text.decode("utf-8") if params_node else "()"
            ret_node = node.child_by_field_name("return_type")
            ret_text = ret_node.text.decode("utf-8") if ret_node else ""
            sig = f"{fn_name}{params_text}{ret_text}"
            self.nodes.append(
                ASTNode(
                    id=fn_id,
                    name=full_name,
                    path=self.file_path,
                    kind="Function",
                    language=self.language_name,
                    docstring=doc,
                    signature=sig,
                )
            )

            prev_fn = self._current_function
            self._current_function = fn_id
            body = node.child_by_field_name("body")
            if body:
                for child in body.children:
                    self._visit_ts_js(child)
            self._current_function = prev_fn
            return

        elif t == "variable_declarator":
            val_node = node.child_by_field_name("value")
            if val_node and val_node.type in ("arrow_function", "function_expression"):
                name_node = node.child_by_field_name("name")
                fn_name = name_node.text.decode("utf-8") if name_node else "anonymous"
                full_name = ".".join(self._scope_stack + [fn_name]) if self._scope_stack else fn_name
                fn_id = make_node_id(self.file_path, full_name, "Function")
                doc = _extract_preceding_comment(node)
                params_node = val_node.child_by_field_name("parameters")
                params_text = params_node.text.decode("utf-8") if params_node else "()"
                ret_node = val_node.child_by_field_name("return_type")
                ret_text = ret_node.text.decode("utf-8") if ret_node else ""
                sig = f"{fn_name}{params_text}{ret_text}"
                self.nodes.append(
                    ASTNode(
                        id=fn_id,
                        name=full_name,
                        path=self.file_path,
                        kind="Function",
                        language=self.language_name,
                        docstring=doc,
                        signature=sig,
                    )
                )

                prev_fn = self._current_function
                self._current_function = fn_id
                body = val_node.child_by_field_name("body")
                if body:
                    if body.type == "statement_block":
                        for child in body.children:
                            self._visit_ts_js(child)
                    else:
                        self._visit_ts_js(body)
                self._current_function = prev_fn
                return

        elif t == "call_expression":
            if self._current_function is not None:
                fn_node = node.child_by_field_name("function")
                callee = None
                if fn_node:
                    if fn_node.type == "identifier":
                        callee = fn_node.text.decode("utf-8")
                    elif fn_node.type == "member_expression":
                        prop = fn_node.child_by_field_name("property")
                        callee = prop.text.decode("utf-8") if prop else fn_node.text.decode("utf-8")
                if callee:
                    self.edges.append(
                        ASTEdge(
                            src=self._current_function,
                            dst=callee,
                            type="CALLS",
                            valid_at=self.timestamp,
                        )
                    )

        for child in node.children:
            self._visit_ts_js(child)

    def _visit_go(self, node: Any):
        t = node.type

        if t == "type_declaration":
            for spec in node.children:
                if spec.type == "type_spec":
                    name_node = spec.child_by_field_name("name")
                    if not name_node:
                        continue
                    type_name = name_node.text.decode("utf-8")
                    t_node = spec.child_by_field_name("type")
                    if not t_node:
                        continue
                    doc = _extract_preceding_comment(spec)

                    if t_node.type == "struct_type":
                        s_id = make_node_id(self.file_path, type_name, "Struct")
                        self.nodes.append(
                            ASTNode(
                                id=s_id,
                                name=type_name,
                                path=self.file_path,
                                kind="Struct",
                                language="go",
                                docstring=doc,
                            )
                        )
                        # Check embedded structs
                        for flist in t_node.children:
                            if flist.type == "field_declaration_list":
                                for f in flist.children:
                                    if f.type == "field_declaration":
                                        n_field = f.child_by_field_name("name")
                                        t_field = f.child_by_field_name("type")
                                        if n_field is None and t_field is not None:
                                            emb = t_field.text.decode("utf-8").lstrip("*")
                                            self.edges.append(
                                                ASTEdge(
                                                    src=s_id,
                                                    dst=emb,
                                                    type="INHERITS",
                                                    valid_at=self.timestamp,
                                                )
                                            )

                    elif t_node.type == "interface_type":
                        i_id = make_node_id(self.file_path, type_name, "Interface")
                        self.nodes.append(
                            ASTNode(
                                id=i_id,
                                name=type_name,
                                path=self.file_path,
                                kind="Interface",
                                language="go",
                                docstring=doc,
                            )
                        )
                        # Check embedded interfaces
                        for c in t_node.children:
                            if c.type == "type_elem":
                                emb = c.text.decode("utf-8")
                                self.edges.append(
                                    ASTEdge(
                                        src=i_id,
                                        dst=emb,
                                        type="INHERITS",
                                        valid_at=self.timestamp,
                                    )
                                )
                    else:
                        t_id = make_node_id(self.file_path, type_name, "TypeAlias")
                        self.nodes.append(
                            ASTNode(
                                id=t_id,
                                name=type_name,
                                path=self.file_path,
                                kind="TypeAlias",
                                language="go",
                                docstring=doc,
                            )
                        )
            return

        elif t == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                fn_name = name_node.text.decode("utf-8")
                fn_id = make_node_id(self.file_path, fn_name, "Function")
                doc = _extract_preceding_comment(node)
                params_node = node.child_by_field_name("parameters")
                params_text = params_node.text.decode("utf-8") if params_node else "()"
                res_node = node.child_by_field_name("result")
                res_text = (" " + res_node.text.decode("utf-8")) if res_node else ""
                sig = f"func {fn_name}{params_text}{res_text}".strip()
                self.nodes.append(
                    ASTNode(
                        id=fn_id,
                        name=fn_name,
                        path=self.file_path,
                        kind="Function",
                        language="go",
                        docstring=doc,
                        signature=sig,
                    )
                )

                prev_fn = self._current_function
                self._current_function = fn_id
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        self._visit_go(child)
                self._current_function = prev_fn
                return

        elif t == "method_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                m_name = name_node.text.decode("utf-8")
                recv_node = node.child_by_field_name("receiver")
                recv_type = _extract_go_receiver_type(recv_node)
                full_name = f"{recv_type}.{m_name}" if recv_type else m_name
                m_id = make_node_id(self.file_path, full_name, "Method")
                doc = _extract_preceding_comment(node)
                recv_text = recv_node.text.decode("utf-8") if recv_node else ""
                params_node = node.child_by_field_name("parameters")
                params_text = params_node.text.decode("utf-8") if params_node else "()"
                res_node = node.child_by_field_name("result")
                res_text = (" " + res_node.text.decode("utf-8")) if res_node else ""
                sig = f"func {recv_text} {m_name}{params_text}{res_text}".strip()
                self.nodes.append(
                    ASTNode(
                        id=m_id,
                        name=full_name,
                        path=self.file_path,
                        kind="Method",
                        language="go",
                        docstring=doc,
                        signature=sig,
                    )
                )

                prev_fn = self._current_function
                self._current_function = m_id
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        self._visit_go(child)
                self._current_function = prev_fn
                return

        elif t == "call_expression":
            if self._current_function is not None:
                fn_node = node.child_by_field_name("function")
                callee = None
                if fn_node:
                    if fn_node.type == "identifier":
                        callee = fn_node.text.decode("utf-8")
                    elif fn_node.type == "selector_expression":
                        field_node = fn_node.child_by_field_name("field")
                        callee = field_node.text.decode("utf-8") if field_node else fn_node.text.decode("utf-8")
                if callee:
                    self.edges.append(
                        ASTEdge(
                            src=self._current_function,
                            dst=callee,
                            type="CALLS",
                            valid_at=self.timestamp,
                        )
                    )

        for child in node.children:
            self._visit_go(child)

    def _visit_python(self, node: Any):
        t = node.type

        if t == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = name_node.text.decode("utf-8")
                full_name = ".".join(self._scope_stack + [name]) if self._scope_stack else name
                class_id = make_node_id(self.file_path, full_name, "Class")
                doc = _extract_python_docstring(node)
                self.nodes.append(
                    ASTNode(
                        id=class_id,
                        name=full_name,
                        path=self.file_path,
                        kind="Class",
                        language="python",
                        docstring=doc,
                    )
                )

                superclasses = node.child_by_field_name("superclasses")
                if superclasses:
                    for arg in superclasses.children:
                        if arg.type in ("identifier", "attribute"):
                            self.edges.append(
                                ASTEdge(
                                    src=class_id,
                                    dst=arg.text.decode("utf-8"),
                                    type="INHERITS",
                                    valid_at=self.timestamp,
                                )
                            )

                prev_scope = list(self._scope_stack)
                self._scope_stack.append(name)
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        self._visit_python(child)
                self._scope_stack = prev_scope
                return

        elif t == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = name_node.text.decode("utf-8")
                full_name = ".".join(self._scope_stack + [name]) if self._scope_stack else name
                fn_id = make_node_id(self.file_path, full_name, "Function")
                doc = _extract_python_docstring(node)
                params_node = node.child_by_field_name("parameters")
                params_text = params_node.text.decode("utf-8") if params_node else "()"
                ret_node = node.child_by_field_name("return_type")
                ret_text = (" -> " + ret_node.text.decode("utf-8")) if ret_node else ""
                sig = f"{name}{params_text}{ret_text}"
                self.nodes.append(
                    ASTNode(
                        id=fn_id,
                        name=full_name,
                        path=self.file_path,
                        kind="Function",
                        language="python",
                        docstring=doc,
                        signature=sig,
                    )
                )

                prev_fn = self._current_function
                self._current_function = fn_id
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        self._visit_python(child)
                self._current_function = prev_fn
                return

        elif t == "call":
            if self._current_function is not None:
                fn_node = node.child_by_field_name("function")
                callee = None
                if fn_node:
                    if fn_node.type == "identifier":
                        callee = fn_node.text.decode("utf-8")
                    elif fn_node.type == "attribute":
                        attr = fn_node.child_by_field_name("attribute")
                        callee = attr.text.decode("utf-8") if attr else fn_node.text.decode("utf-8")
                if callee:
                    self.edges.append(
                        ASTEdge(
                            src=self._current_function,
                            dst=callee,
                            type="CALLS",
                            valid_at=self.timestamp,
                        )
                    )

        for child in node.children:
            self._visit_python(child)


# --- Public Indexing API ---

def index_file(file_path: str | Path, valid_at: str | None = None) -> tuple[list[ASTNode], list[ASTEdge]]:
    """Parse and extract AST nodes and relation edges from a polyglot source file."""
    path = Path(file_path).resolve()
    if not path.is_file():
        return [], []

    posix_path = path.as_posix()
    timestamp = valid_at or utc_now_iso()
    suffix = path.suffix.lower()

    if suffix not in SUPPORTED_EXTENSIONS:
        return [], []

    # 1. Native Python AST parser (fast, zero external dependency)
    if suffix in (".py", ".pyi"):
        try:
            content = path.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=posix_path)
            extractor = PythonASTExtractor(posix_path)
            extractor.visit(tree)
            for e in extractor.edges:
                e.valid_at = timestamp
            return extractor.nodes, extractor.edges
        except Exception:
            # Fall through to Tree-Sitter fallback if Python AST fails
            pass

    # 2. Tree-Sitter polyglot parser (TypeScript, TSX, JavaScript, Go, Python fallback)
    lang_key = EXTENSION_TO_LANGUAGE.get(suffix)
    if lang_key and _check_tree_sitter():
        parser = get_tree_sitter_parser(lang_key)
        if parser:
            try:
                content_bytes = path.read_bytes()
                tree = parser.parse(content_bytes)
                ts_extractor = TreeSitterExtractor(posix_path, lang_key, valid_at=timestamp)
                return ts_extractor.extract(tree.root_node)
            except Exception:
                return [], []

    return [], []

def index_repository(repo_path: str | Path, valid_at: str | None = None) -> tuple[list[ASTNode], list[ASTEdge]]:
    """Index an entire repository walking valid polyglot source files, pruning noise directories."""
    root = Path(repo_path).resolve()
    all_nodes: list[ASTNode] = []
    all_edges: list[ASTEdge] = []
    timestamp = valid_at or utc_now_iso()

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune ignored directories in-place so os.walk does not descend into them
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_IGNORE_DIRS]

        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext in SUPPORTED_EXTENSIONS:
                fpath = Path(dirpath) / fname
                nodes, edges = index_file(fpath, valid_at=timestamp)
                all_nodes.extend(nodes)
                all_edges.extend(edges)

    return all_nodes, all_edges
