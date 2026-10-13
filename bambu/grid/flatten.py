"""Resolve Bambu system profile 'inherits' chains into standalone JSONs for the CLI."""
import json, sys
from pathlib import Path
BBL = Path.home() / "Library/Application Support/BambuStudio/system/BBL"
def load(kind, name):
    d = json.loads((BBL / kind / f"{name}.json").read_text())
    if "inherits" in d:
        base = load(kind, d["inherits"]); base.update(d); d = base
    return d
for kind, name, out in [("machine", "Bambu Lab A1 0.4 nozzle", "machine.json"),
                        ("process", "0.20mm Standard @BBL A1", "process.json"),
                        ("filament", "Generic PETG @BBL A1", "filament.json")]:
    d = load(kind, name); d.pop("inherits", None); d["from"] = "system"; d["name"] = name
    Path(out).write_text(json.dumps(d, indent=1))
    print(out, d.get("type"))
