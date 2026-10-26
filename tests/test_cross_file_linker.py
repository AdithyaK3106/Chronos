"""Unit and integration tests for Cross-File Import Linker & Symbol Disambiguation."""

import json
from pathlib import Path
import pytest

from chronos.core.db import get_connection
from chronos.context.graph import SQLiteGraphStore
from chronos.context.indexer import extract_file_ast, index_repository
from chronos.context.linker import CrossFileLinker


def test_typescript_cross_file_linking(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()

    # 1. Target export file
    auth_file = src / "auth.ts"
    auth_file.write_text("""
export class BaseAuth {}

export function getUser(id: string): string {
    return "user_" + id;
}
""", encoding="utf-8")

    # 2. Importer file
    service_file = src / "user_service.ts"
    service_file.write_text("""
import { getUser, BaseAuth } from './auth';

export class UserService extends BaseAuth {
    login(id: string): string {
        return getUser(id);
    }
}
""", encoding="utf-8")

    all_nodes, all_edges = index_repository(tmp_path)
    con = get_connection(tmp_path / "chronos.db")
    store = SQLiteGraphStore(con)
    store.ingest(all_nodes, all_edges)

    # Verify explicit IMPORTS edge
    import_edges = [e for e in all_edges if e.type == "IMPORTS"]
    assert any(
        e.src.endswith("user_service.ts") and "auth.ts::getUser::Function" in e.dst
        for e in import_edges
    )

    # Verify disambiguated CALLS edge
    call_edges = [e for e in all_edges if e.type == "CALLS"]
    assert any(
        e.src.endswith("UserService.login::Method")
        and e.dst.endswith("auth.ts::getUser::Function")
        for e in call_edges
    )

    # Verify disambiguated INHERITS edge
    inherit_edges = [e for e in all_edges if e.type == "INHERITS"]
    assert any(
        e.src.endswith("UserService::Class")
        and e.dst.endswith("auth.ts::BaseAuth::Class")
        for e in inherit_edges
    )

    # Verify as_of_callers returns UserService.login for getUser
    callers = store.as_of_callers("getUser")
    assert any(c["name"] == "UserService.login" for c in callers)


def test_tsconfig_path_alias_resolution(tmp_path: Path):
    # Setup tsconfig with @/* alias
    tsconfig = {
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {
                "@/*": ["src/*"],
                "@components/*": ["src/ui/components/*"],
            },
        }
    }
    (tmp_path / "tsconfig.json").write_text(json.dumps(tsconfig), encoding="utf-8")

    comp_dir = tmp_path / "src" / "ui" / "components"
    comp_dir.mkdir(parents=True)
    button_file = comp_dir / "Button.tsx"
    button_file.write_text("""
export const Button = () => {
    return null;
};
""", encoding="utf-8")

    page_dir = tmp_path / "src" / "pages"
    page_dir.mkdir(parents=True)
    page_file = page_dir / "Home.tsx"
    page_file.write_text("""
import { Button } from '@components/Button';

export const Home = () => {
    return Button();
};
""", encoding="utf-8")

    all_nodes, all_edges = index_repository(tmp_path)

    # Verify import edge was resolved via tsconfig alias
    import_edges = [e for e in all_edges if e.type == "IMPORTS"]
    assert any(
        e.src.endswith("Home.tsx") and "Button.tsx::Button::Function" in e.dst
        for e in import_edges
    )

    # Verify CALLS edge was disambiguated to Button.tsx
    call_edges = [e for e in all_edges if e.type == "CALLS"]
    assert any(
        e.src.endswith("Home::Function") and e.dst.endswith("Button.tsx::Button::Function")
        for e in call_edges
    )


def test_python_cross_file_linking(tmp_path: Path):
    pkg_dir = tmp_path / "myproj"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")

    core_dir = pkg_dir / "core"
    core_dir.mkdir()
    (core_dir / "__init__.py").write_text("", encoding="utf-8")
    db_file = core_dir / "db.py"
    db_file.write_text("""
def connect_db(uri):
    return uri
""", encoding="utf-8")

    services_dir = pkg_dir / "services"
    services_dir.mkdir()
    (services_dir / "__init__.py").write_text("", encoding="utf-8")
    worker_file = services_dir / "worker.py"
    worker_file.write_text("""
from ..core.db import connect_db

def run_worker():
    return connect_db("sqlite:///:memory:")
""", encoding="utf-8")

    all_nodes, all_edges = index_repository(tmp_path)
    con = get_connection(tmp_path / "chronos.db")
    store = SQLiteGraphStore(con)
    store.ingest(all_nodes, all_edges)

    # Verify explicit IMPORTS edge
    import_edges = [e for e in all_edges if e.type == "IMPORTS"]
    assert any(
        e.src.endswith("worker.py") and "db.py::connect_db::Function" in e.dst
        for e in import_edges
    )

    # Verify disambiguated CALLS edge
    call_edges = [e for e in all_edges if e.type == "CALLS"]
    assert any(
        e.src.endswith("run_worker::Function") and e.dst.endswith("db.py::connect_db::Function")
        for e in call_edges
    )

    # Verify as_of_callers matches run_worker
    callers = store.as_of_callers("connect_db")
    assert any(c["name"] == "run_worker" for c in callers)


def test_go_cross_package_and_intra_package_linking(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module myorg/myrepo\n\ngo 1.22\n", encoding="utf-8")

    # 1. Package auth with two files (intra-package symbol sharing)
    auth_dir = tmp_path / "pkg" / "auth"
    auth_dir.mkdir(parents=True)

    user_go = auth_dir / "user.go"
    user_go.write_text("""
package auth

type User struct {
    ID string
}

func ValidateUser(u *User) bool {
    return true
}
""", encoding="utf-8")

    token_go = auth_dir / "token.go"
    token_go.write_text("""
package auth

// Notice: intra-package call without import statement
func GenerateToken(u *User) bool {
    return ValidateUser(u)
}
""", encoding="utf-8")

    # 2. Package main importing auth
    cmd_dir = tmp_path / "cmd" / "server"
    cmd_dir.mkdir(parents=True)
    main_go = cmd_dir / "main.go"
    main_go.write_text("""
package main

import (
    "fmt"
    "myorg/myrepo/pkg/auth"
)

func main() {
    u := &auth.User{ID: "123"}
    auth.ValidateUser(u)
    fmt.Println("ok")
}
""", encoding="utf-8")

    all_nodes, all_edges = index_repository(tmp_path)

    # Check 1: Intra-package linking in token.go -> user.go
    token_calls = [e for e in all_edges if "GenerateToken::Method" in e.src or "GenerateToken::Function" in e.src]
    assert any(
        "ValidateUser" in e.dst and "user.go" in e.dst
        for e in token_calls
    )

    # Check 2: Cross-package linking in main.go -> auth.ValidateUser
    main_calls = [e for e in all_edges if "main.go::main::Function" in e.src]
    assert any(
        "ValidateUser" in e.dst and "user.go" in e.dst
        for e in main_calls
    )

    # Check 3: External package tagging for fmt.Println
    assert any(
        e.dst == "external:fmt::Println"
        for e in main_calls
    )


def test_external_third_party_tagging(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    app_ts = src / "App.tsx"
    app_ts.write_text("""
import React, { useState } from 'react';
import axios from 'axios';

export const App = () => {
    const [data, setData] = useState(null);
    axios.get("/api");
    return null;
};
""", encoding="utf-8")

    all_nodes, all_edges = index_repository(tmp_path)

    # Verify external IMPORTS edges
    import_edges = [e for e in all_edges if e.type == "IMPORTS"]
    assert any(e.dst == "external:react" for e in import_edges)
    assert any(e.dst == "external:axios" for e in import_edges)

    # Verify external CALLS edges
    call_edges = [e for e in all_edges if e.type == "CALLS"]
    assert any(e.dst == "external:react::useState" for e in call_edges)
    assert any(e.dst == "external:axios::get" for e in call_edges)


def test_incremental_single_file_relinking(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()

    calc_ts = src / "calc.ts"
    calc_ts.write_text("""
export function add(a: number, b: number): number { return a + b; }
export function multiply(a: number, b: number): number { return a * b; }
""", encoding="utf-8")

    app_ts = src / "app.ts"
    app_ts.write_text("""
import { add } from './calc';

export function run(): number {
    return add(1, 2);
}
""", encoding="utf-8")

    con = get_connection(tmp_path / "chronos.db")
    store = SQLiteGraphStore(con)

    nodes, edges = index_repository(tmp_path)
    store.ingest(nodes, edges)

    # Initial state: run calls add in calc.ts
    callers_add = store.as_of_callers("add")
    assert any(c["name"] == "run" for c in callers_add)

    from datetime import datetime, timedelta, timezone
    t1 = edges[0].valid_at if edges else datetime.now(timezone.utc).isoformat()
    t2 = (datetime.fromisoformat(t1) + timedelta(minutes=5)).isoformat()

    # Edit app.ts to import and call multiply instead
    app_ts.write_text("""
import { multiply } from './calc';

export function run(): number {
    return multiply(3, 4);
}
""", encoding="utf-8")

    num_nodes, num_edges = store.update_file(app_ts, valid_at=t2)
    assert num_nodes >= 1

    # At t2: run calls multiply
    callers_mult = store.as_of_callers("multiply", as_of=t2)
    assert any(c["name"] == "run" for c in callers_mult)

    # Historical at t1: run called add
    callers_past = store.as_of_callers("add", as_of=t1)
    assert any(c["name"] == "run" for c in callers_past)
