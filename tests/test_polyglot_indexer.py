"""Unit and integration tests for Polyglot Tree-Sitter Indexing (TypeScript, JavaScript, Go, Python)."""

import sqlite3
import pytest
from pathlib import Path

from chronos.core.db import get_connection
from chronos.context.graph import SQLiteGraphStore
from chronos.context.indexer import (
    SUPPORTED_EXTENSIONS,
    index_file,
    index_repository,
    is_supported_source_file,
)
from chronos.context.resolver import DeterministicContextResolver


def test_supported_extensions_membership():
    assert is_supported_source_file("app.py") is True
    assert is_supported_source_file("service.ts") is True
    assert is_supported_source_file("Component.tsx") is True
    assert is_supported_source_file("script.js") is True
    assert is_supported_source_file("module.mjs") is True
    assert is_supported_source_file("server.go") is True
    assert is_supported_source_file("Cargo.toml") is False
    assert is_supported_source_file("README.md") is False


def test_typescript_indexing(tmp_path: Path):
    ts_code = """
/**
 * Authentication contract for external identity providers.
 */
export interface AuthInterface {
    authenticate(token: string): boolean;
}

export class BaseAuth {
    validateToken(token: string): boolean {
        return true;
    }
}

/**
 * Enterprise OAuth2 service handler.
 */
export class OAuthService extends BaseAuth implements AuthInterface {
    /**
     * Authenticates an incoming bearer token.
     */
    async login(user: string, token: string): Promise<boolean> {
        this.logAttempt(user);
        return verifyCredentials(user, token);
    }
}

/**
 * Utility token generator.
 */
export const generateToken = (payload: string): string => {
    return signJwt(payload);
};

export type AuthToken = string;
"""
    ts_file = tmp_path / "auth_service.ts"
    ts_file.write_text(ts_code, encoding="utf-8")

    nodes, edges = index_file(ts_file)
    node_names = {n.name: n for n in nodes}

    # Verify nodes
    assert "AuthInterface" in node_names
    assert node_names["AuthInterface"].kind == "Interface"
    assert "Authentication contract" in (node_names["AuthInterface"].docstring or "")

    assert "BaseAuth" in node_names
    assert node_names["BaseAuth"].kind == "Class"

    assert "OAuthService" in node_names
    assert node_names["OAuthService"].kind == "Class"
    assert "Enterprise OAuth2 service handler" in (node_names["OAuthService"].docstring or "")

    assert "OAuthService.login" in node_names
    assert node_names["OAuthService.login"].kind == "Method"
    assert "Authenticates an incoming bearer token" in (node_names["OAuthService.login"].docstring or "")
    assert "login(user: string, token: string): Promise<boolean>" in (node_names["OAuthService.login"].signature or "")

    assert "generateToken" in node_names
    assert node_names["generateToken"].kind == "Function"
    assert "generateToken(payload: string): string" in (node_names["generateToken"].signature or "")

    assert "AuthToken" in node_names
    assert node_names["AuthToken"].kind == "TypeAlias"

    # Verify relational edges
    edge_pairs = [(e.src.split("::")[1], e.type, e.dst) for e in edges]
    assert ("OAuthService", "INHERITS", "BaseAuth") in edge_pairs
    assert ("OAuthService", "INHERITS", "AuthInterface") in edge_pairs
    assert ("OAuthService.login", "CALLS", "logAttempt") in edge_pairs
    assert ("OAuthService.login", "CALLS", "verifyCredentials") in edge_pairs
    assert ("generateToken", "CALLS", "signJwt") in edge_pairs


def test_javascript_indexing(tmp_path: Path):
    js_code = """
/**
 * Billing processor module.
 */
class PaymentProcessor {
    /**
     * Executes credit card charge.
     */
    charge(amount) {
        initGateway();
        return sendTransaction(amount);
    }
}

function createReceipt(txId) {
    return formatInvoice(txId);
}
"""
    js_file = tmp_path / "payment.js"
    js_file.write_text(js_code, encoding="utf-8")

    nodes, edges = index_file(js_file)
    node_names = {n.name: n for n in nodes}

    assert "PaymentProcessor" in node_names
    assert node_names["PaymentProcessor"].kind == "Class"
    assert "Billing processor module" in (node_names["PaymentProcessor"].docstring or "")

    assert "PaymentProcessor.charge" in node_names
    assert node_names["PaymentProcessor.charge"].kind == "Method"

    assert "createReceipt" in node_names
    assert node_names["createReceipt"].kind == "Function"

    edge_pairs = [(e.src.split("::")[1], e.type, e.dst) for e in edges]
    assert ("PaymentProcessor.charge", "CALLS", "initGateway") in edge_pairs
    assert ("PaymentProcessor.charge", "CALLS", "sendTransaction") in edge_pairs
    assert ("createReceipt", "CALLS", "formatInvoice") in edge_pairs


def test_go_indexing(tmp_path: Path):
    go_code = """
package storage

import "os"

// BaseStorage provides shared storage primitives.
type BaseStorage struct {
    rootDir string
}

// Storage defines the primary persistence interface.
type Storage interface {
    Get(key string) ([]byte, error)
}

// AdvancedStorage embeds Storage with batching.
type AdvancedStorage interface {
    Storage
    BatchGet(keys []string) ([][]byte, error)
}

// DiskStorage implements disk-backed persistent storage.
type DiskStorage struct {
    BaseStorage
    maxFiles int
}

// Save stores data to disk.
func (d *DiskStorage) Save(key string, data []byte) error {
    validateKey(key)
    return writeToDisk(key, data)
}

// NewDiskStorage creates an initialized DiskStorage.
func NewDiskStorage(dir string) *DiskStorage {
    ensureDirectory(dir)
    return &DiskStorage{}
}
"""
    go_file = tmp_path / "storage.go"
    go_file.write_text(go_code, encoding="utf-8")

    nodes, edges = index_file(go_file)
    node_names = {n.name: n for n in nodes}

    assert "BaseStorage" in node_names
    assert node_names["BaseStorage"].kind == "Struct"
    assert "BaseStorage provides shared storage primitives" in (node_names["BaseStorage"].docstring or "")

    assert "Storage" in node_names
    assert node_names["Storage"].kind == "Interface"

    assert "AdvancedStorage" in node_names
    assert node_names["AdvancedStorage"].kind == "Interface"

    assert "DiskStorage" in node_names
    assert node_names["DiskStorage"].kind == "Struct"
    assert "DiskStorage implements disk-backed persistent storage" in (node_names["DiskStorage"].docstring or "")

    assert "DiskStorage.Save" in node_names
    assert node_names["DiskStorage.Save"].kind == "Method"
    assert "Save stores data to disk" in (node_names["DiskStorage.Save"].docstring or "")
    assert "func (d *DiskStorage) Save(key string, data []byte) error" in (node_names["DiskStorage.Save"].signature or "")

    assert "NewDiskStorage" in node_names
    assert node_names["NewDiskStorage"].kind == "Function"
    assert "func NewDiskStorage(dir string) *DiskStorage" in (node_names["NewDiskStorage"].signature or "")

    edge_pairs = [(e.src.split("::")[1], e.type, e.dst) for e in edges]
    # Struct embedding inheritance
    assert ("DiskStorage", "INHERITS", "BaseStorage") in edge_pairs
    # Interface embedding inheritance
    assert ("AdvancedStorage", "INHERITS", "Storage") in edge_pairs
    # Method and function calls
    assert ("DiskStorage.Save", "CALLS", "validateKey") in edge_pairs
    assert ("DiskStorage.Save", "CALLS", "writeToDisk") in edge_pairs
    assert ("NewDiskStorage", "CALLS", "ensureDirectory") in edge_pairs


def test_polyglot_repository_indexing_and_directory_pruning(tmp_path: Path):
    # Set up mock full-stack repository structure
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "app.ts").write_text("export class App { run() { startServer(); } }", encoding="utf-8")

    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "worker.go").write_text("package worker\nfunc StartWorker() { doTask() }", encoding="utf-8")

    py_dir = tmp_path / "scripts"
    py_dir.mkdir()
    (py_dir / "deploy.py").write_text("def deploy():\n    trigger_pipeline()", encoding="utf-8")

    # Noise directories that must be pruned
    node_modules = tmp_path / "node_modules" / "some-lib"
    node_modules.mkdir(parents=True)
    (node_modules / "junk.js").write_text("function junk() {}", encoding="utf-8")

    vendor = tmp_path / "vendor" / "go-lib"
    vendor.mkdir(parents=True)
    (vendor / "junk.go").write_text("package junk\nfunc Junk() {}", encoding="utf-8")

    venv = tmp_path / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "site.py").write_text("def venv_func(): pass", encoding="utf-8")

    all_nodes, all_edges = index_repository(tmp_path)
    indexed_names = {n.name for n in all_nodes}

    # Verified indexed files
    assert "App" in indexed_names
    assert "App.run" in indexed_names
    assert "StartWorker" in indexed_names
    assert "deploy" in indexed_names

    # Verified ignored files from noise directories
    assert "junk" not in indexed_names
    assert "Junk" not in indexed_names
    assert "venv_func" not in indexed_names


def test_polyglot_incremental_sync_and_temporal_invalidation(tmp_path: Path):
    con = get_connection(tmp_path / "chronos.db")
    store = SQLiteGraphStore(con)

    ts_file = tmp_path / "engine.ts"
    ts_file.write_text("""
export class Engine {
    execute(): void {
        stepOne();
    }
}
""", encoding="utf-8")

    # 1. Ingest initial version at T1
    t1 = "2026-10-26T00:00:00Z"
    nodes, edges = index_file(ts_file, valid_at=t1)
    store.ingest(nodes, edges)

    callees_t1 = store.as_of_callees("Engine.execute", as_of=t1)
    assert [c["name"] for c in callees_t1] == ["stepOne"]

    # 2. Modify file at T2 to call stepTwo instead
    t2 = "2026-10-26T01:00:00Z"
    ts_file.write_text("""
export class Engine {
    execute(): void {
        stepTwo();
    }
}
""", encoding="utf-8")

    store.update_file(ts_file, valid_at=t2)

    # Historical query at T1 still shows stepOne
    callees_past = store.as_of_callees("Engine.execute", as_of=t1)
    assert [c["name"] for c in callees_past] == ["stepOne"]

    # Current query at T2 shows stepTwo
    callees_now = store.as_of_callees("Engine.execute", as_of=t2)
    assert [c["name"] for c in callees_now] == ["stepTwo"]


def test_zero_path_context_resolution_polyglot(tmp_path: Path):
    con = get_connection(tmp_path / "chronos.db")
    store = SQLiteGraphStore(con)

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    ts_file = src_dir / "user_service.ts"
    ts_file.write_text("""
/**
 * User billing and subscription manager.
 */
export class UserService {
    /**
     * Resolves user subscription tier.
     */
    resolveSubscription(userId: string): Promise<string> {
        return fetchTier(userId);
    }
}
""", encoding="utf-8")

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "user_service.test.ts").write_text("// test file", encoding="utf-8")

    nodes, edges = index_repository(tmp_path)
    store.ingest(nodes, edges)

    resolver = DeterministicContextResolver(repo_root=tmp_path, graph_store=store)
    result = resolver.resolve(
        prompt="Fix the timeout error in UserService.resolveSubscription when fetching subscription tier."
    )

    assert any("user_service.ts" in tf for tf in result.target_files)
    assert any("UserService" in sym for sym in result.seed_symbols)
    assert any("user_service.test.ts" in tf for tf in result.test_files)
    assert "user_service" in result.provenance_summary.lower()


def test_tsx_react_component_indexing(tmp_path: Path):
    tsx_code = """
import React from 'react';

/**
 * User card presentation component.
 */
export const UserCard = (props: { name: string }) => {
    return <button onClick={() => handleClick()}>{props.name}</button>;
};

export class Dashboard extends React.Component {
    render() {
        return <UserCard name="Chronos" />;
    }
}
"""
    tsx_file = tmp_path / "UserCard.tsx"
    tsx_file.write_text(tsx_code, encoding="utf-8")

    nodes, edges = index_file(tsx_file)
    node_names = {n.name: n for n in nodes}

    assert "UserCard" in node_names
    assert node_names["UserCard"].kind == "Function"
    assert "User card presentation component" in (node_names["UserCard"].docstring or "")
    assert "UserCard(props: { name: string })" in (node_names["UserCard"].signature or "")

    assert "Dashboard" in node_names
    assert node_names["Dashboard"].kind == "Class"

    edge_pairs = [(e.src.split("::")[1], e.type, e.dst) for e in edges]
    assert ("UserCard", "CALLS", "handleClick") in edge_pairs
    assert ("Dashboard", "INHERITS", "React.Component") in edge_pairs


def test_python_treesitter_direct_extractor():
    from chronos.context.indexer import TreeSitterExtractor, get_tree_sitter_parser

    py_code = b'''
class ModelEvaluator(BaseEvaluator):
    """Evaluates agent execution runs."""
    def evaluate(self, run_id: str) -> bool:
        return self.compute_metrics(run_id)
'''
    parser = get_tree_sitter_parser("python")
    assert parser is not None
    tree = parser.parse(py_code)
    extractor = TreeSitterExtractor("eval.py", "python")
    nodes, edges = extractor.extract(tree.root_node)

    names = {n.name: n for n in nodes}
    assert "ModelEvaluator" in names
    assert names["ModelEvaluator"].kind == "Class"
    assert "Evaluates agent execution runs" in (names["ModelEvaluator"].docstring or "")

    assert "ModelEvaluator.evaluate" in names
    assert "evaluate(self, run_id: str) -> bool" in (names["ModelEvaluator.evaluate"].signature or "")

    edge_pairs = [(e.src.split("::")[1], e.type, e.dst) for e in edges]
    assert ("ModelEvaluator", "INHERITS", "BaseEvaluator") in edge_pairs
    assert ("ModelEvaluator.evaluate", "CALLS", "compute_metrics") in edge_pairs

