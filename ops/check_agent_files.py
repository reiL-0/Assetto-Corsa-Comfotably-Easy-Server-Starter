"""The agent instructions and the shared quality skill travel with every checkout (plan T5.2): AGENTS.md, CLAUDE.md and `quality-guard` for both
tool families. The full guide lives in .agents/skills/quality-guard/SKILL.md; the .claude/ one is a pointer to it and must keep pointing.
Run: python ops/check_agent_files.py"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NEED = ["AGENTS.md", "CLAUDE.md", ".agents/skills/quality-guard/SKILL.md", ".claude/skills/quality-guard/SKILL.md"]
missing = [p for p in NEED if not (ROOT / p).is_file() or not (ROOT / p).read_text(encoding="utf-8").strip()]
if missing:
    sys.exit("missing or empty (they must be in every checkout): " + ", ".join(missing))
if len((ROOT / NEED[2]).read_text(encoding="utf-8")) < 2000:
    sys.exit(".agents/skills/quality-guard/SKILL.md is not the full guide")
if ".agents/skills/quality-guard/SKILL.md" not in (ROOT / NEED[3]).read_text(encoding="utf-8"):
    sys.exit(".claude/skills/quality-guard/SKILL.md must point to .agents/skills/quality-guard/SKILL.md")
print("agent files ok")
