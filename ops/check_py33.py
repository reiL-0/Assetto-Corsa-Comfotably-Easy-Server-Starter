"""The AC in-game apps run inside Assetto Corsa's embedded Python 3.3.5 (plan T5.2). There is no 3.3 here to run them, so this is the automated FILTER:
it parses every .py under the given folders and refuses what 3.3 cannot take. It does NOT prove the code works on 3.3 (stdlib behaviour, the embedded
build): that still needs the in-game test. Run: python ops/check_py33.py <folder> [<folder> ...]

Refused: f-strings, async/await, `@`, variable annotations, `{**a}` / `[*a]` / `f(*a, *b)` (3.5), walrus, positional-only parameters, `match`, `except*`,
underscores in numbers, `with (a, b):`, numbers/names that need >3.3 stdlib (typing, asyncio, enum, pathlib, secrets, dataclasses, ... and subprocess.run,
os.scandir, math.inf / isclose, json.JSONDecodeError, random.choices ...)."""
import ast
import io
import sys
import tokenize
from pathlib import Path

NEW_MODULES = {"typing", "asyncio", "enum", "pathlib", "statistics", "selectors", "tracemalloc", "secrets", "dataclasses", "contextvars", "zoneinfo",
               "tomllib", "graphlib", "importlib.resources", "concurrent.futures.thread_pool"}
NEW_ATTRS = {("subprocess", "run"), ("os", "scandir"), ("math", "inf"), ("math", "nan"), ("math", "isclose"), ("json", "JSONDecodeError"),
             ("random", "choices"), ("time", "time_ns"), ("shutil", "which")}   # shutil.which exists since 3.3; kept out below
NEW_ATTRS.discard(("shutil", "which"))


def problems(path):
    src = Path(path).read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        return [f"{path}:{e.lineno}: does not parse here: {e.msg}"]
    out = []

    def bad(node, why):
        out.append(f"{path}:{getattr(node, 'lineno', '?')}: {why}")

    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr):
            bad(n, "f-string (3.6)")
        elif isinstance(n, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith)):
            bad(n, "async/await (3.5)")
        elif isinstance(n, ast.MatMult):
            bad(n, "`@` operator (3.5)")
        elif isinstance(n, ast.AnnAssign):
            bad(n, "variable annotation (3.6)")
        elif isinstance(n, ast.NamedExpr):
            bad(n, "walrus := (3.8)")
        elif isinstance(n, ast.Dict) and any(k is None for k in n.keys):
            bad(n, "{**a} dict unpacking (3.5)")
        elif isinstance(n, (ast.List, ast.Tuple, ast.Set)) and any(isinstance(e, ast.Starred) for e in n.elts) and isinstance(n.ctx, ast.Load):
            bad(n, "[*a] unpacking in a display (3.5)")
        elif isinstance(n, ast.Call) and sum(isinstance(a, ast.Starred) for a in n.args) > 1:
            bad(n, "several *args in one call (3.5)")
        elif isinstance(n, ast.arguments) and n.posonlyargs:
            bad(n, "positional-only parameters (3.8)")
        elif isinstance(n, getattr(ast, "Match", ())) or isinstance(n, getattr(ast, "TryStar", ())):
            bad(n, "match / except* (3.10 / 3.11)")
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] in NEW_MODULES or a.name in NEW_MODULES:
                    bad(n, f"module `{a.name}` is not in Python 3.3")
        elif isinstance(n, ast.ImportFrom) and n.module and (n.module.split(".")[0] in NEW_MODULES or n.module in NEW_MODULES):
            bad(n, f"module `{n.module}` is not in Python 3.3")
        elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and (n.value.id, n.attr) in NEW_ATTRS:
            bad(n, f"`{n.value.id}.{n.attr}` is newer than Python 3.3")
        elif isinstance(n, ast.With) and len(n.items) == 1 and isinstance(n.items[0].context_expr, ast.Tuple) and n.items[0].optional_vars is None:
            bad(n, "`with (a, b):` (3.10 parenthesised form)")
    for t in tokenize.generate_tokens(io.StringIO(src).readline):
        if t.type == tokenize.NUMBER and "_" in t.string:
            out.append(f"{path}:{t.start[0]}: underscore in a number (3.6)")
    return out


def main(folders):
    files = [p for f in folders for p in sorted(Path(f).rglob("*.py")) if "__pycache__" not in p.parts]
    if not files:
        sys.exit("no .py files under " + ", ".join(folders))
    found = [msg for p in files for msg in problems(p)]
    if found:
        sys.exit("not Python 3.3-safe:\n  " + "\n  ".join(found))
    print(f"python 3.3 syntax filter ok ({len(files)} files)")


if __name__ == "__main__":
    main(sys.argv[1:] or ["."])
