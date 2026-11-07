import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

MAX_SKILL_BYTES = 32768
SKILL_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")


def load_skills(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    if not root.is_dir():
        raise ValueError("Skill root must be a directory")
    base = root.resolve()
    result = []
    for folder in sorted(base.iterdir()):
        if not folder.is_dir():
            continue
        source = folder / "SKILL.md"
        if not source.exists():
            continue
        if not source.resolve().is_relative_to(base):
            raise ValueError("Skill resource escapes its root")
        if source.stat().st_size > MAX_SKILL_BYTES:
            raise ValueError("Skill exceeds size limit")
        content = source.read_text(encoding="utf-8")
        parts = content.split("---", 2)
        if len(parts) != 3 or parts[0].strip():
            raise ValueError("Skill requires YAML frontmatter")
        try:
            metadata = yaml.safe_load(parts[1])
        except yaml.YAMLError as exc:
            raise ValueError("Invalid Skill frontmatter") from exc
        if not isinstance(metadata, dict):
            raise ValueError("Invalid Skill metadata")
        name, description = metadata.get("name"), metadata.get("description")
        if not isinstance(name, str) or not SKILL_NAME.fullmatch(name) or name != folder.name:
            raise ValueError("Skill name must match its directory")
        if not isinstance(description, str) or not description.strip() or len(description) > 1000:
            raise ValueError("Skill requires a bounded description")
        result.append({
            "name": name,
            "description": description.strip(),
            "version": hashlib.sha256(content.encode()).hexdigest(),
            "content": content,
        })
    return result
