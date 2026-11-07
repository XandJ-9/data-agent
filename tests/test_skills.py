"""Filesystem and validation contracts for startup skill snapshots."""

import hashlib
from pathlib import Path

import pytest

from data_agent.skills import load_skills


def write_skill(
    root: Path,
    folder: str = "revenue",
    *,
    name: str = "revenue",
    description: str = "Revenue analysis",
    body: str = "# Revenue\n使用净收入。\n",
) -> Path:
    directory = root / folder
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}", encoding="utf-8")
    return path


def test_missing_root_returns_empty_snapshot(tmp_path: Path) -> None:
    assert load_skills(tmp_path / "missing") == []


def test_valid_yaml_skill_has_content_hash_and_changes_version_on_edit(tmp_path: Path) -> None:
    path = write_skill(tmp_path, description='"Revenue: 北京 and 上海"')
    (first,) = load_skills(tmp_path)
    assert first["name"] == "revenue"
    assert first["description"] == "Revenue: 北京 and 上海"
    assert "使用净收入。" in first["content"]
    assert first["version"] == hashlib.sha256(first["content"].encode("utf-8")).hexdigest()
    path.write_text(path.read_text(encoding="utf-8") + "Include refunds.\n", encoding="utf-8")
    (second,) = load_skills(tmp_path)
    assert second["version"] != first["version"]
    assert "Include refunds." not in first["content"]
    assert second["version"] == hashlib.sha256(second["content"].encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "document",
    [
        "No YAML frontmatter",
        "---\nname: [broken\n---\nBody",
        "---\n- revenue\n---\nBody",
        "---\nname: revenue\n---\nBody",
        "---\nname: revenue\ndescription: ''\n---\nBody",
        "---\nname: revenue\ndescription: '   '\n---\nBody",
        "---\nname: revenue\ndescription: 123\n---\nBody",
        "---\nname: revenue\ndescription: !!python/object/apply:builtins.str [unsafe]\n---\nBody",
    ],
)
def test_malformed_or_unsafe_yaml_fails_startup(tmp_path: Path, document: str) -> None:
    path = write_skill(tmp_path)
    path.write_text(document, encoding="utf-8")
    with pytest.raises(ValueError):
        load_skills(tmp_path)


@pytest.mark.parametrize(
    ("folder", "name"),
    [
        ("Revenue", "Revenue"),
        ("bad_name", "bad_name"),
        ("1revenue", "1revenue"),
        ("a" * 65, "a" * 65),
        ("revenue", "another-name"),
        ("revenue", "null"),
        ("revenue", "[revenue]"),
    ],
)
def test_names_are_strict_unique_and_match_folder(tmp_path: Path, folder: str, name: str) -> None:
    write_skill(tmp_path, folder=folder, name=name)
    with pytest.raises(ValueError):
        load_skills(tmp_path)
    # A repeated declared name in a second directory must not silently overwrite.
    other = tmp_path / "duplicates"
    write_skill(other)
    write_skill(other, folder="second", name="revenue")
    with pytest.raises(ValueError):
        load_skills(other)


def test_content_bound_counts_utf8_bytes_and_accepts_maximum_length_name(tmp_path: Path) -> None:
    name = "a" * 64
    path = write_skill(tmp_path, folder=name, name=name, body="界" * 10000)
    (valid,) = load_skills(tmp_path)
    assert valid["name"] == name
    assert len(valid["content"].encode("utf-8")) <= 32768
    write_skill(tmp_path, folder=name, name=name, body="界" * 11000)
    assert path.stat().st_size > 32768
    with pytest.raises(ValueError):
        load_skills(tmp_path)


@pytest.mark.parametrize("escape", ["directory", "file", "root-is-file"])
def test_root_checks_reject_files_and_symlinks_escaping_root(tmp_path: Path, escape: str) -> None:
    root = tmp_path / "skills"
    if escape == "root-is-file":
        root.write_text("not a directory", encoding="utf-8")
    else:
        root.mkdir()
        outside = tmp_path / "outside"
        external = write_skill(outside)
        if escape == "directory":
            (root / "revenue").symlink_to(external.parent, target_is_directory=True)
        else:
            (root / "revenue").mkdir()
            (root / "revenue" / "SKILL.md").symlink_to(external)
    with pytest.raises(ValueError):
        load_skills(root)
