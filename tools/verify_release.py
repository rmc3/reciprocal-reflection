"""Verify the packaged edition and relative Markdown links; no network calls."""
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def excluded(path):
    """Ignore only Git metadata and generated Python bytecode."""
    relative = path.relative_to(ROOT)
    return (relative.parts[0] == ".git" or "__pycache__" in relative.parts
            or path.suffix == ".pyc")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify():
    entries = {}
    for line in (ROOT / "SHA256SUMS").read_text().splitlines():
        hash_value, name = line.split("  ", 1)
        path = ROOT / name
        if (path.is_symlink() or not path.is_file()
                or not path.resolve().is_relative_to(ROOT) or excluded(path)):
            raise ValueError(f"Invalid manifest path: {name}")
        if digest(path) != hash_value:
            raise ValueError(f"Hash mismatch: {name}")
        if name in entries:
            raise ValueError(f"Duplicate manifest entry: {name}")
        entries[name] = hash_value
    actual = {str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
              if p.is_file() and not excluded(p)}
    if actual != set(entries) | {"SHA256SUMS"}:
        raise ValueError("Missing or unexpected file in edition tree.")
    historical = json.loads((ROOT / "EXPORT-MANIFEST.json").read_text())["files"]
    for entry in historical:
        if digest(ROOT / entry["export"]) != entry["export_sha256"]:
            raise ValueError(f"Historical export mismatch: {entry['export']}")
    link_count = 0
    for path in ROOT.rglob("*.md"):
        if excluded(path):
            continue
        for href in re.findall(r"\[[^\]]*\]\(([^)\s]+)\)", path.read_text()):
            if href.startswith(("https://", "http://", "#", "mailto:")):
                continue
            target = path.parent / href.split("#", 1)[0]
            if not target.exists() or not target.resolve().is_relative_to(ROOT):
                raise ValueError(f"Broken or escaping local link in {path.name}: {href}")
            link_count += 1
    print(json.dumps({"files_hashed": len(entries), "historical_copies_checked": len(historical),
                      "relative_links_checked": link_count, "network_requests": 0,
                      "scope": "Hash consistency and local link targets, not full privacy or semantic review."},
                     indent=2))


if __name__ == "__main__":
    verify()
