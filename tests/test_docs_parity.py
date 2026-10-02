"""Docs parity gate: every route the HTTP server handles must be documented.

Routes are registered by hand in agora/server.py (``if path == '/x'`` /
``path in ('/x', '/y')`` inside ``AgoraHTTPHandler._do_GET`` / ``_do_POST``),
not by decorators, so this test walks the AST of those two methods and
collects every string literal ``path`` is compared against, plus the
``/referee/corporate/<action>`` family from its ``allowed_actions`` tuple.
Each collected path must appear literally in ``public/documentation.html``.
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = ROOT / "agora" / "server.py"
DOCS = ROOT / "public" / "documentation.html"

# Not API surface: bare root alias of the terminal page.
IGNORED = {""}


def _strings(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [v for e in node.elts for v in _strings(e)]
    return []


def registered_routes():
    tree = ast.parse(SERVER.read_text())
    handler = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef) and n.name == "AgoraHTTPHandler")
    routes = set()
    for fn in handler.body:
        if not (isinstance(fn, ast.FunctionDef) and fn.name in ("_do_GET", "_do_POST")):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                    and node.left.id == "path"):
                for comp in node.comparators:
                    routes.update(_strings(comp))
            elif (isinstance(node, ast.Assign) and fn.name == "_do_POST"
                  and any(isinstance(t, ast.Name) and t.id == "allowed_actions" for t in node.targets)):
                routes.update("/referee/corporate/" + a for a in _strings(node.value))
    return {r for r in routes if r not in IGNORED and r.startswith("/")}


def test_routes_are_discovered():
    routes = registered_routes()
    # Guard against the extractor silently finding nothing after a refactor.
    assert len(routes) > 60, routes
    for expected in ("/referee/orders", "/ws/terminal", "/salvage/claim",
                     "/referee/corporate/directive", "/stations/prices"):
        assert expected in routes


def test_every_registered_route_is_documented():
    html = DOCS.read_text()
    missing = sorted(r for r in registered_routes() if r not in html)
    assert not missing, (
        "Routes registered in agora/server.py but missing from public/documentation.html "
        "(update the /documentation API catalog): " + ", ".join(missing))
