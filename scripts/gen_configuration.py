"""Generate docs/configuration.md from app/config.py's SPEC.

Every setting Aurum has, with its environment variable, its default, and the
reason it exists. The reason is not typed here: it is the comment already
standing above the entry in `app/config.py`, lifted verbatim. Writing this by
hand would mean maintaining a hundred rows beside the ones they describe, and
the pair would drift the first time a default moved - which is the failure this
document exists to stop.

Usage:
    python scripts/gen_configuration.py
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SOURCE = ROOT / "app" / "config.py"
OUT = ROOT / "docs" / "configuration.md"

#: Parser function name -> what an operator may write for it.
TYPES = {
    "_int": "integer",
    "_float": "number",
    "_fraction": "number, 0.0-1.0",
    "_non_negative": "number, >= 0",
    "_duty": "integer, 0-255",
    "_bool": "true / false",
    "_text": "text",
    "_optional_text": "text, may be unset",
    "_text_list": "comma-separated list",
}

#: Section headings, longest prefix first so the specific ones win.
GROUPS = [
    ("conveyor.camera", "Camera"),
    ("conveyor.detection", "Detection"),
    ("conveyor.tracking", "Tracking"),
    ("conveyor.weight", "Load cell and weighing"),
    ("conveyor.belt", "Belt"),
    ("conveyor.encoder", "Belt encoder"),
    ("conveyor.manual", "Manual belt speed"),
    ("conveyor.simulation", "Simulation profile"),
    ("conveyor.geometry", "Rig geometry"),
    ("conveyor.routing", "Routing and timing"),
    ("conveyor.servo", "Servos"),
    ("conveyor.arduino", "Board link"),
    ("conveyor.runtime", "Runtime mode"),
    ("conveyor.mode", "Runtime mode"),
    ("grading", "Grading and bins"),
    ("mass", "Mass plausibility"),
    ("assembly", "Assembly detection"),
    ("pricing", "Pricing"),
    ("metalprice", "Pricing"),
    ("price", "Pricing"),
    ("demo", "Demonstration aids"),
    ("server", "Server"),
    ("tracking", "Tracking"),
]


def _literal(node: ast.AST) -> str:
    """Render a default the way an operator would have to type it."""
    if isinstance(node, ast.Name):
        return "*unmeasured*" if node.id == "UNMEASURED" else f"`{node.id}`"
    try:
        value = ast.literal_eval(node)
    except ValueError:
        return "`?`"
    if value is None:
        return "*unset*"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, str):
        return f"`{value}`" if value else "*empty*"
    return f"`{value}`"


def _kind(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return TYPES.get(node.id, "value")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_one_of":
        allowed = " / ".join(f"`{ast.literal_eval(a)}`" for a in node.args)
        return f"one of {allowed}"
    return "value"


def comments_above(lines: list[str], lineno: int) -> str:
    """The contiguous `#` block directly above a line, as one paragraph.

    Stops at code. A rule of dashes is a separator, not a terminator: the file
    closes a themed block with one just above the entry it describes, so
    breaking there would throw away the description instead of reading it.
    Walking up ends at the previous entry either way.
    """
    out: list[str] = []
    i = lineno - 2  # 0-indexed, and the line above the entry
    while i >= 0:
        text = lines[i].strip()
        if not text.startswith("#"):
            break
        body = text.lstrip("#").strip()
        out.append("" if body and set(body) <= {"-"} else body)
        i -= 1
    out.reverse()
    # Blank comment lines separate paragraphs in the source; keep that.
    paragraphs: list[list[str]] = [[]]
    for line in out:
        if line:
            paragraphs[-1].append(line)
        elif paragraphs[-1]:
            paragraphs.append([])
    return _cell("<br><br>".join(" ".join(p) for p in paragraphs if p))


def _cell(text: str) -> str:
    """A pipe inside a comment would end the table column it sits in."""
    return text.replace("|", "\\|")


def yaml_comments() -> dict[str, str]:
    """Dotted key -> the comment standing above it in `configs/*.yaml`.

    A second source for the settings `app/config.py` lists without a comment of
    its own: the YAML files carry the operator-facing note for many of them, and
    a description already written twice should not be written a third time here.

    Indentation gives the nesting, which is enough for these files - they are
    plain nested maps with no lists of maps and no flow style.
    """
    found: dict[str, str] = {}
    for path in sorted((ROOT / "configs").glob("*.yaml")):
        stack: list[tuple[int, str]] = []
        pending: list[str] = []
        for raw in path.read_text().splitlines():
            text = raw.strip()
            if text.startswith("#"):
                body = text.lstrip("#").strip()
                pending.append("" if set(body) <= {"-"} and body else body)
                continue
            if not text:
                pending.clear()
                continue
            match = re.match(r"^(\s*)([A-Za-z_][\w.]*):\s*(.*)$", raw)
            if match is None:
                pending.clear()
                continue
            indent, name, value = len(match.group(1)), match.group(2), match.group(3)
            while stack and stack[-1][0] >= indent:
                stack.pop()
            dotted = ".".join([part for _, part in stack] + [name])
            stack.append((indent, name))
            if value and pending:
                paragraphs: list[list[str]] = [[]]
                for line in pending:
                    if line:
                        paragraphs[-1].append(line)
                    elif paragraphs[-1]:
                        paragraphs.append([])
                joined = "<br><br>".join(" ".join(p) for p in paragraphs if p)
                if joined:
                    found.setdefault(dotted, _cell(joined))
            pending.clear()
    return found


def group_for(key: str) -> str:
    for prefix, title in GROUPS:
        if key.startswith(prefix):
            return title
    return "Other"


def main() -> int:
    source = SOURCE.read_text()
    lines = source.splitlines()
    tree = ast.parse(source)

    spec = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "SPEC":
            spec = node.value
    if spec is None or not isinstance(spec, ast.Dict):
        raise SystemExit("app/config.py: SPEC dict not found")

    from_yaml = yaml_comments()
    rows: dict[str, list[tuple[str, str, str, str, str]]] = {}
    order: list[str] = []
    count = 0
    for key_node, value_node in zip(spec.keys, spec.values, strict=True):
        key = ast.literal_eval(key_node)
        parser, default, env = value_node.elts[0], value_node.elts[1], value_node.elts[2]
        title = group_for(key)
        if title not in rows:
            rows[title] = []
            order.append(title)
        rows[title].append(
            (
                key,
                ast.literal_eval(env),
                _kind(parser),
                _literal(default),
                comments_above(lines, key_node.lineno) or from_yaml.get(key, ""),
            )
        )
        count += 1

    L: list[str] = []
    add = L.append
    add("# Configuration\n")
    add(f"Every one of the **{count} settings** Aurum reads, what it defaults to, and")
    add("why it exists.\n")
    add("> Generated by `python scripts/gen_configuration.py` from `app/config.py`.")
    add("> The defaults and environment variable names are read out of the `SPEC`")
    add("> table itself, and each explanation is the comment standing above that")
    add("> entry in the source. Nothing here is typed by hand, so it cannot drift")
    add("> from the code it describes.\n")
    add("## How a value is resolved\n")
    add("Three sources, last one wins:\n")
    add("1. The default in this table.")
    add("2. `configs/*.yaml`, by the dotted key.")
    add("3. The environment variable.\n")
    add("A profile script such as `configs/bench-profile.sh` is just the third of")
    add("these: it exports environment variables and nothing more. `UNMEASURED` is a")
    add("real value meaning *nobody has measured this on the physical machine*, and")
    add("the code that consumes it refuses rather than guessing.\n")
    add("## Settings\n")

    for title in order:
        add(f"### {title}\n")
        add("| Key | Environment variable | Type | Default | Why |")
        add("|---|---|---|---|---|")
        for key, env, kind, default, why in rows[title]:
            add(f"| `{key}` | `{env}` | {kind} | {default} | {why} |")
        add("")

    OUT.write_text("\n".join(L).rstrip() + "\n")
    print(f"Wrote {OUT.relative_to(ROOT)} — {count} settings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
