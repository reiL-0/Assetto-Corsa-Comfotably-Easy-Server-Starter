"""The AC in-game apps run inside Assetto Corsa's embedded Python 3.3.5 (plan T5.2). There is no 3.3 here to run them, so this is the automated FILTER:
it parses every .py under the given folders and refuses what 3.3 cannot take. It does NOT prove the code works on 3.3 (stdlib behaviour, the embedded
build): that still needs the in-game test. Run: python ops/check_py33.py <folder> [<folder> ...]     (python ops/check_py33.py --selftest checks the filter itself)

Refused: f-strings, async/await, `@`, variable annotations, `{**a}` / `[*a]` / `f(*a, *b)` (3.5), walrus, positional-only parameters, `match`, `except*`,
underscores in numbers, numbers/names that need >3.3 stdlib (typing, asyncio, enum, pathlib, secrets, dataclasses, ... and subprocess.run,
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


def problems(path, src=None):
    src = Path(path).read_text(encoding="utf-8") if src is None else src
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        return [f"{path}:{e.lineno}: does not parse here: {e.msg}"]
    out = []

    def bad(node, why):
        out.append(f"{path}:{getattr(node, 'lineno', '?')}: {why}")

    # names the code gives to modules and to things imported from them, so `import subprocess as sp; sp.run` and `from subprocess import run` count too
    modules, imported = {}, {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                modules[(a.asname or a.name).split(".")[0]] = a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            for a in n.names:
                imported[a.asname or a.name] = (n.module.split(".")[0], a.name)

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
        elif isinstance(n, ast.Set) and any(isinstance(e, ast.Starred) for e in n.elts):
            bad(n, "{*a} unpacking in a display (3.5)")
        elif isinstance(n, (ast.List, ast.Tuple)) and isinstance(getattr(n, "ctx", None), ast.Load) and any(isinstance(e, ast.Starred) for e in n.elts):
            bad(n, "[*a] unpacking in a display (3.5)")
        elif isinstance(n, ast.Call):
            stars = [i for i, a in enumerate(n.args) if isinstance(a, ast.Starred)]
            if len(stars) > 1 or (stars and stars[0] != len(n.args) - 1):
                bad(n, "several *args, or a positional argument after *args, in one call (3.5)")
            if sum(1 for k in n.keywords if k.arg is None) > 1:
                bad(n, "several **kwargs in one call (3.5)")
            if isinstance(n.func, ast.Attribute) and n.func.attr == "hex" and not n.args:
                bad(n, "`.hex()` (bytes/bytearray/memoryview.hex is 3.5; use binascii.hexlify)")
            if isinstance(n.func, ast.Name) and imported.get(n.func.id) in NEW_ATTRS:
                bad(n, f"`{imported[n.func.id][0]}.{imported[n.func.id][1]}` is newer than Python 3.3")
        elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mod) and isinstance(n.left, ast.Constant) and isinstance(n.left.value, bytes):
            bad(n, "bytes % formatting (3.5)")
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
        elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and (modules.get(n.value.id, n.value.id), n.attr) in NEW_ATTRS:
            bad(n, f"`{modules.get(n.value.id, n.value.id)}.{n.attr}` is newer than Python 3.3")
    for t in tokenize.generate_tokens(io.StringIO(src).readline):
        if t.type == tokenize.NUMBER and "_" in t.string:
            out.append(f"{path}:{t.start[0]}: underscore in a number (3.6)")
    return out


# One snippet per refusal: --selftest proves the filter still refuses each of them (and accepts plain 3.3 code), so a gap cannot reopen unnoticed.
BAD = {
    "f-string": 'x = f"{1}"', "async/await": "async def g():\n    await g()", "`@` operator": "a = b @ c", "variable annotation": "x: int = 1",
    "walrus": "if (y := 1): pass", "{**a}": "d = {**a}", "{*a}": "s = {*a}", "[*a]": "l = [*a, 1]", "positional argument after *args": "f(*a, 1)",
    "several *args": "f(*a, *b)", "several **kwargs": "f(**a, **b)", ".hex()": "b.hex()", "bytes % formatting": 'x = b"%s" % y',
    "positional-only": "def g(a, /, b): pass", "match": "match x:\n    case 1: pass", "underscore in a number": "n = 1_000",
    "module `typing`": "import typing", "module `asyncio`": "import asyncio", "module `enum`": "from enum import Enum", "module `pathlib`": "import pathlib",
    "subprocess.run": "import subprocess\nsubprocess.run([])", "alias of subprocess.run": "import subprocess as sp\nsp.run([])",
    "from-import of subprocess.run": "from subprocess import run\nrun([])", "os.scandir": "import os\nos.scandir('.')", "math.inf": "import math\nmath.inf",
    "json.JSONDecodeError": "import json\njson.JSONDecodeError",
}
GOOD = ('import os, json, socket, struct, time, binascii\nfrom urllib.parse import urlsplit\n'
        'class A(object):\n    def f(self, a, b=1, *args, **kw):\n        return "%s %d" % (a, b) + "{0}".format(a) + u"x"\n'
        'try:\n    x = int("1")\nexcept (ValueError, TypeError) as e:\n    raise\n'
        'def g():\n    yield from range(3)\nprint("a", end="", file=None)\nos.makedirs("d", exist_ok=True)\nbinascii.hexlify(b"x")\n'
        'f(*a, **k)\nwith open("f") as a, open("g") as b:\n    pass\nd = {"a": 1}\nl = [1, 2]\n(1).to_bytes(2, "big")\nint.from_bytes(b"ab", "big")\n')


def selftest():
    for why, code in BAD.items():
        got = problems("<snippet>", code)
        if not got:
            sys.exit(f"the filter no longer refuses: {why}")
    if (got := problems("<good>", GOOD)):
        sys.exit("the filter refuses plain Python 3.3 code: " + "; ".join(got))
    print(f"python 3.3 filter selftest ok ({len(BAD)} refusals)")


def main(folders):
    files = [p for f in folders for p in sorted(Path(f).rglob("*.py")) if "__pycache__" not in p.parts]
    if not files:
        sys.exit("no .py files under " + ", ".join(folders))
    found = [msg for p in files for msg in problems(p)]
    if found:
        sys.exit("not Python 3.3-safe:\n  " + "\n  ".join(found))
    print(f"python 3.3 syntax filter ok ({len(files)} files)")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        selftest()
    else:
        main(sys.argv[1:] or ["."])
