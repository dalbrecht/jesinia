# SPDX-License-Identifier: AGPL-3.0-or-later
"""Agent skills stay in step with the server: front matter, tool names, doc paths."""

import json
import re
from pathlib import Path

import pytest
import yaml

from jesinia.registry import modules

REPO = Path(__file__).resolve().parents[3]
SKILLS = REPO / "skills"
TEXTS = sorted(p for p in SKILLS.rglob("*.md"))
TOOL = re.compile(r"`((?:kb|search|agents|notes|tags|rss|review|issues|queues|queue)_[a-z_]+)`")
DOC = re.compile(r"`(docs/[\w./-]+\.md)`")


def live_tools() -> set[str]:
    return {t.name for m in modules() for t in m.mcp_tools()}


def test_there_are_skills_and_a_snippet() -> None:
    names = {p.relative_to(SKILLS).as_posix() for p in TEXTS}
    assert "claude-code/skills/jesinia-kb-conventions/SKILL.md" in names
    assert "claude-code/skills/jesinia-notes/SKILL.md" in names
    assert "claude-code/commands/jesinia-note.md" in names
    assert "codex/AGENTS.snippet.md" in names


@pytest.mark.parametrize("path", sorted(SKILLS.rglob("SKILL.md")), ids=lambda p: p.parent.name)
def test_skill_front_matter(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    meta = yaml.safe_load(text.split("---\n", 2)[1])
    assert meta["name"] == path.parent.name and len(meta["description"]) > 40


@pytest.mark.parametrize("path", TEXTS, ids=lambda p: p.relative_to(SKILLS).as_posix())
def test_every_tool_and_doc_mentioned_exists(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    unknown = set(TOOL.findall(text)) - live_tools()
    assert not unknown, f"{path.name} mentions tools the server doesn't have: {sorted(unknown)}"
    missing = [d for d in DOC.findall(text) if not (REPO / d).exists()]
    assert not missing, f"{path.name} references missing docs: {missing}"


def test_plugin_manifests_agree() -> None:
    plugin = json.loads((SKILLS / "claude-code/.claude-plugin/plugin.json").read_text())
    market = json.loads((SKILLS / "claude-code/.claude-plugin/marketplace.json").read_text())
    assert plugin["name"] == "jesinia" == market["name"] == market["plugins"][0]["name"]
    assert market["plugins"][0]["source"] == "./"


def test_codex_snippet_is_delimited() -> None:
    text = (SKILLS / "codex/AGENTS.snippet.md").read_text()
    assert text.startswith("<!-- jesinia:begin") and text.rstrip().endswith("<!-- jesinia:end -->")


@pytest.mark.parametrize(
    "path",
    ["claude-code/skills/jesinia-kb-conventions/SKILL.md", "codex/AGENTS.snippet.md"],
)
def test_agents_learn_the_read_only_rss_tools(path: str) -> None:
    text = (SKILLS / path).read_text(encoding="utf-8")
    assert "`rss_search_posts`" in text and "`rss_get_post`" in text
    # Keeping a post is the user's action: no skill may point an agent at a keep tool.
    assert not re.search(r"`rss_keep\w*`", text)
    assert "untrusted" in text.split("rss_search_posts", 1)[1][:600]


ISSUES_TOOLS = ("`issues_search`", "`issues_get`", "`queues_list`", "`queue_get`")


@pytest.mark.parametrize(
    "path",
    ["claude-code/skills/jesinia-kb-conventions/SKILL.md", "codex/AGENTS.snippet.md"],
)
def test_agents_learn_the_read_only_issues_tools(path: str) -> None:
    text = (SKILLS / path).read_text(encoding="utf-8")
    assert all(tool in text for tool in ISSUES_TOOLS)
    # Agents can't change GitHub or queues: no skill may point an agent at a write tool.
    assert not re.search(r"`(?:issues|queues?)_(?:apply|approve|write|add|move|close)\w*`", text)
    assert "untrusted" in text.split("issues_search", 1)[1][:600]


def test_the_mcp_convention_lists_the_issues_tools() -> None:
    text = (REPO / "docs/conventions/agents.md").read_text(encoding="utf-8")
    assert all(tool in text for tool in ISSUES_TOOLS)
