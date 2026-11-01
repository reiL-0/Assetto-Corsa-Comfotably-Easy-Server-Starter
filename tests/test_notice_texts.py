"""The messages the manager puts on the drivers' screens are translated by the website (OPR WP/banner_i18n.py). This test keeps a list of every text fragment the manager writes in a notice and fails when the list
changes: a new or edited message must get its rule (and its sample in OPR WP/ops/check_banner_i18n.py) there, and then the list here is refreshed with  NOTICE_TEXTS_WRITE=1 pytest tests/test_notice_texts.py."""
import ast
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIST = Path(__file__).with_name("notice_texts.json")
FILES = ("app/racecontrol.py", "app/live/nascar.py", "app/live/incidents.py")


def _pieces(node):
    """Every Spanish text fragment inside an expression: the constants and the literal parts of f-strings."""
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def fragments():
    out = set()
    for rel in FILES:
        tree = ast.parse((ROOT / rel).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
                if name in ("_say", "notify") and len(node.args) >= (2 if name == "_say" else 1):
                    out.update(_pieces(node.args[1] if name == "_say" else node.args[0]))
                elif name == "_propose" and node.args:
                    out.update(_pieces(node.args[-1]))
                elif name in ("_end_stage", "_chequered", "_ot_failed") and node.args:
                    out.update(_pieces(node.args[0]))
            elif isinstance(node, ast.Tuple) and len(node.elts) == 2 and isinstance(node.elts[0], ast.Constant) and node.elts[0].value == "say":
                out.update(_pieces(node.elts[1]))
        out.update(_pieces(next((n.value for n in tree.body if isinstance(n, ast.Assign) and any(getattr(t, "id", "") in ("SAY", "PITS_OPEN") for t in n.targets)), ast.Constant(value=""))))
    return sorted(s for s in out if s.strip())


def test_the_messages_of_the_screens_are_the_ones_the_website_translates():
    now = fragments()
    if os.environ.get("NOTICE_TEXTS_WRITE"):
        LIST.write_text(json.dumps(now, ensure_ascii=False, indent=1) + "\n")
    known = json.loads(LIST.read_text())
    new, gone = [s for s in now if s not in known], [s for s in known if s not in now]
    assert not new and not gone, (f"the texts the drivers read changed. New: {new}. Gone: {gone}. Add or adjust their rule in OPR WP/banner_i18n.py (and a sample in ops/check_banner_i18n.py), "
                                  "then refresh this list: NOTICE_TEXTS_WRITE=1 pytest tests/test_notice_texts.py")
    assert len(now) > 40, "the extraction found the messages"
