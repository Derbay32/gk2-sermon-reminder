#!/usr/bin/env python3
"""GKSA-23 source-tool acceptance matrix (black-box CLI acceptance test).

This module is a TEST helper, never production code. It re-derives, at the
public CLI seam, the source-policy acceptance matrix that previously lived
inline in ``.github/workflows/ci.yml`` and extends it with resource,
prohibited-asset and reference-privacy cases. It never imports
``tools/source_check.py``: it invokes it as a subprocess against a disposable
copy of this repository's own versioned inputs inside a temporary git
repository (``git init`` plus ``git add -A`` only; no commits).

Failure paths enumerated BEFORE writing the assertions
------------------------------------------------------
Technical-registration exemption (the original matrix, cases a-o):
  a  unchanged canonical declaration                   -> PASS
  b  inline method returns the canonical title         -> FAIL (sentence in code)
  c  another .cs file holds the canonical title        -> FAIL
  d  another const holds the canonical title           -> FAIL
  e  duplicate PluginName canonical declaration        -> FAIL
  f  BepInPlugin attribute dropped                     -> FAIL
  g  another approved finalText copy                   -> FAIL
  h  approved finalText copy inside a comment          -> FAIL
  i  attribute moved into a line comment               -> FAIL
  j  attribute moved into a block comment              -> FAIL
  k  attribute moved into an ordinary string           -> FAIL
  l  attribute moved into a verbatim string            -> FAIL
  m  PluginName declaration commented out              -> FAIL
  n  PluginName value replaced by a non-canonical
     approved sentence                                 -> FAIL
  o  attribute moved to another class                  -> FAIL
Resource and localization constraints:
  r1  EmbeddedResource without WithCulture="false"     -> FAIL
  r2  EmbeddedResource without LogicalName             -> FAIL
  r3  EmbeddedResource source file removed             -> FAIL
  r4  key present in one catalog but not the other     -> FAIL (parity)
  r5  required HUD key absent from every catalog       -> FAIL
  r6  placeholder set differs across catalogs          -> FAIL
  r7  catalog value is not a string                    -> FAIL
  r8  catalog value is blank                           -> FAIL
  r9  catalog value differs from manifest finalText    -> FAIL
  r10 a supported language catalog is removed          -> FAIL
  r11 catalog file is not valid JSON                   -> FAIL
Source policy:
  p1  prohibited binary suffix tracked                 -> FAIL
  p2  doc outside docs/adr/                            -> FAIL
  p3  Reference without <Private>false</Private>       -> FAIL
  p4  Reference with <Private>true</Private>           -> FAIL
Positive controls:
  z1  doc under docs/adr/ is allowed                   -> PASS

``--manifest`` is an ordinary caller-supplied repository-relative path and
defaults to the settled aggregated index ``tests/e2e/manifest.json``. The
canonical title is read from the plugin source and the "approved copy" is read
from the supplied manifest, so the same matrix can guard any manifest. Running
with ``--manifest tests/e2e/gksa12.json`` proves the original matrix is green.
``--suite existing`` is not tied to a ticket manifest identity: it runs the 31
existing source-policy cases and derives their approved copy from whatever
manifest/index it is given, so it also runs against the migrated default index
once that file exists.

Suites
-------
``--suite existing`` runs the 31 existing source-policy cases above (original 15
kept 1:1). ``--suite v2`` runs the aggregated-index v2 guard cases below.
``--suite all`` (the default) runs both.

The v2 index schema is settled even though the guard is not implemented yet. A
v2 fixture is built from an *explicitly supplied seed* (a v1 ticket manifest now,
the v2 aggregated index after migration) plus a disposable copy of this
repository's own C#/resource inputs. The seed resolution is deliberately
strict:

  * ``--seed-manifest`` when given, otherwise ``--manifest``, is the sole seed;
  * a missing, unreadable or malformed seed is a harness setup failure with a
    written artifact -- there is no discovery of another ``tests/e2e/*.json``
    file and no fallback to the localization catalogs;
  * ``profiles.json`` must be exactly the settled flat map
    ``context id -> context``; nested ``{"profiles": ...}`` wrappers,
    non-object contexts and missing/empty ``finalText`` objects are setup
    errors, never silently coerced into an apparently valid fixture.

The disposable fixture copies this repository's own versioned inputs: the
``src`` tree (excluding ``bin``/``obj``/``__pycache__``/``.venv``) and every
``tests/e2e/**/*.json`` file, at the same relative path, creating parents as
needed. After copying, the builder drops the disposable fixture's copied
canonical scenario root and writes its synthetic tree there instead, so real
scenario ids/profiles never leak and a deliberately empty root really is empty;
the source repository is never mutated.

The fixture is intentionally synthetic; real scenario definitions are never
collected or redistributed:

  index    ``{kind: "gksr-e2e-manifest", manifestVersion: 2,
            manifestId: "gksr-full-mod", profilesFile: "profiles.json",
            scenarioRoot: "scenarios"}`` (paths relative to the index);
  profiles ``context id -> shared metadata`` (``languageMapping``/``validation``
            etc. preserved from the seed) with the seeded ``finalText`` split
            across contexts and ``validation.supportedCaptureVersions`` ``[2]``;
  scenario ``{kind: "gksr-e2e-scenarios", schemaVersion: 2, scenarios: [...]}``
            with exactly one synthetic scenario per context: unique id,
            ``requirements: ["GKSA-23"]``, synthetic prerequisites/steps, a
            non-empty ``expect`` (a synthetic boolean observation plus a
            ``finalTextKey`` that exists in that context's own ``finalText``) and
            an evidence object with boolean fields.

v2 fixture failure paths enumerated before writing the assertions:
  seed-missing          explicit seed path absent            -> setup failure
  seed-malformed        seed is not readable JSON            -> setup failure
  seed-unsupported      seed is neither v1 ticket manifest
                        nor kind=gksr-e2e-manifest v2 index   -> setup failure
  profiles-missing      v2 index profilesFile absent         -> setup failure
  profiles-malformed    profiles JSON unparseable            -> setup failure
  profiles-nested       profiles.json is a {"profiles":...}
                        wrapper instead of a flat map        -> setup failure
  profiles-empty-text   a context has no non-empty finalText -> setup failure
  finaltext-too-small   seed cannot fill the requested
                        context count                        -> setup failure

Required v2 cases (all expected RED against the current v1-only guard, which
silently accepts an unread v2 index instead of enforcing it):
  v2-valid-multi      aggregate finalText across contexts, a later context owns
                      a key the first context does not        -> PASS
  v2-valid-single     single-context valid v2 tree           -> PASS (control 2)
  v2-drift-later      catalog drifts from a later profile's
                      finalText                               -> REJECT
  v2-profile-missing  profilesFile absent                     -> REJECT
  v2-profile-malformed profiles JSON unparseable              -> REJECT
  v2-profile-not-object a profile value is not an object      -> REJECT
  v2-profile-unknown-ref scenario references an unknown id    -> REJECT
  v2-scenario-malformed scenario JSON unparseable             -> REJECT
  v2-tree-missing     scenarioRoot absent                     -> REJECT
  v2-tree-empty       scenarioRoot present but no scenarios   -> REJECT
  v2-ref-changed      referenced file edited without an index
                      edit is still checked                   -> REJECT
  v2-path-escape      profilesFile escapes the repository     -> REJECT
  v2-symlink-escape   scenarioRoot is a symlink out of tree   -> REJECT
  v2-secondary-finaltext-missing  a secondary context declares no
                      finalText                              -> REJECT
  v2-secondary-langmap-missing    a secondary context declares no
                      languageMapping                        -> REJECT
  v2-scenario-expect-empty        a scenario declares an empty
                      expect array                           -> REJECT
  v2-scenario-evidence-nonobject  a scenario declares a non-object
                      evidence field                         -> REJECT
  v2-datecheck-mode-unsupported   a scenario declares an unsupported
                      dateCheck mode                         -> REJECT
  v2-symlink-index    the requested index file itself is a
                      symlink to a valid index               -> REJECT

Every v2 case records how the tool actually behaved, so the RED artifact always
distinguishes "v2 not enforced" (accepted a fixture it must reject, or refused
v2 as an unknown kind) from a harness setup error or a tool crash. Two positive
controls keep a broken fixture from making every rejection case pass. A negative
pass requires exit 2, ``ok:false``, an explicit ``e2eVerdict: null`` and at least
one structured violation/error -- an arbitrary crash or a blanket "unsupported
v2" refusal never counts.

The two positive fixtures are additionally checked against the existing v1
manifest-shape contract by a disposable validation-only adapter (kept out of
this script, so it is never a maintained second definition): each context is
materialised as a v1 envelope (supported generic fixture identity, integer
``manifestVersion: 1``, ``supportedCaptureVersions: [1]``) in an ignored fixture
directory and fed to the unchanged ``tests/e2e/verify.py --check-manifest``.

Only the Python 3 standard library is used, no native build happens, and no
game is executed. The emitted artifact carries ``e2eVerdict: null``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PROJECT_REL = "src/GK2.SermonReminder/GK2.SermonReminder.csproj"
PLUGIN_REL = "src/GK2.SermonReminder/SermonReminderPlugin.cs"
EN_REL = "src/GK2.SermonReminder/Localization/gksr.en.json"
ZH_REL = "src/GK2.SermonReminder/Localization/gksr.zh_cn.json"

V2_INDEX_REL = "tests/e2e/manifest.json"
V2_PROFILES_REL = "tests/e2e/profiles.json"
V2_SCENARIO_ROOT_REL = "tests/e2e/scenarios"
V2_MANIFEST_ID = "gksr-full-mod"
V2_PROFILE_IDS = ("ctx-primary", "ctx-secondary")
V2_SCENARIO_KIND = "gksr-e2e-scenarios"
V2_REQUIREMENT_ID = "GKSA-23"

# Context metadata that is either seed-wide (kind/version/ticket) or rebuilt per
# context (finalText/scenarios); everything else is shared metadata preserved
# verbatim so the synthetic fixture keeps valid languageMapping/validation.
_V2_PROFILE_OMITTED = ("kind", "manifestVersion", "ticket", "scenarios", "finalText")

ATTRIBUTE_LINE = "    [BepInPlugin(PluginGuid, PluginName, PluginVersion)]"
_PLUGIN_NAME_DECL = re.compile(r'(\s*)public\s+const\s+string\s+PluginName\s*=\s*"[^"]*";')
_PLUGIN_NAME_VALUE = re.compile(r'public\s+const\s+string\s+PluginName\s*=\s*"([^"]*)"')

EXPECT_PASS = "pass"
EXPECT_SENTENCE = "sentence"
EXPECT_VIOLATION = "violation"


@dataclass(frozen=True)
class MatrixContext:
    """Inputs the matrix derives from the caller's repository and manifest."""

    canonical_title: str
    approved_copy: str


@dataclass(frozen=True)
class Case:
    """One black-box acceptance case for ``tools/source_check.py``."""

    case_id: str
    group: str
    description: str
    expectation: str
    apply: Callable[[Path, MatrixContext], None]
    expected_sentence: str | None = None
    expected_violation: str | None = None


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _copy_repo_inputs(repo: Path, fixture: Path) -> None:
    """Copy the versioned source/csproj/catalog and e2e JSON tree.

    Every ``*.json`` file under ``tests/e2e`` is copied at the same relative
    path (parents created), so nested scenario trees with duplicate basenames in
    distinct directories are preserved; ``src`` exclusions are unchanged.
    """
    src = repo / "src"
    if src.is_dir():
        shutil.copytree(
            src,
            fixture / "src",
            ignore=shutil.ignore_patterns("bin", "obj", "__pycache__", ".venv"),
        )
    e2e = repo / "tests" / "e2e"
    dest_e2e = fixture / "tests" / "e2e"
    dest_e2e.mkdir(parents=True, exist_ok=True)
    if e2e.is_dir():
        for json_file in sorted(e2e.rglob("*.json")):
            if not json_file.is_file():
                continue
            destination = dest_e2e / json_file.relative_to(e2e)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(json_file, destination)


def _git_init_add(fixture: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", str(fixture)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(fixture), "add", "-A"],
        check=True,
        capture_output=True,
        text=True,
    )


def _read(fixture: Path, rel: str) -> str:
    return (fixture / rel).read_text(encoding="utf-8")


def _write(fixture: Path, rel: str, text: str) -> None:
    target = fixture / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _read_json(fixture: Path, rel: str) -> object:
    return json.loads(_read(fixture, rel))


def _write_json(fixture: Path, rel: str, data: object) -> None:
    _write(fixture, rel, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _inject(text: str, block: str) -> str:
    index = text.rstrip().rfind("}")
    if index < 0:
        raise ValueError("no closing brace to inject before")
    return text[:index] + block + "\n" + text[index:]


def _replace_once(text: str, old: str, new: str) -> str:
    if old not in text:
        raise ValueError(f"fixture marker not found: {old!r}")
    return text.replace(old, new, 1)


def _plugin_case(fn: Callable[[str, MatrixContext], str]) -> Callable[[Path, MatrixContext], None]:
    def apply(fixture: Path, ctx: MatrixContext) -> None:
        _write(fixture, PLUGIN_REL, fn(_read(fixture, PLUGIN_REL), ctx))

    return apply


def _declaration_span(text: str) -> re.Match[str]:
    match = _PLUGIN_NAME_DECL.search(text)
    if match is None:
        raise ValueError("PluginName declaration not found in plugin source")
    return match


# ---------------------------------------------------------------------------
# Registration-exemption mutations (ported 1:1 from the CI matrix)
# ---------------------------------------------------------------------------


def _mut_inline_title(text: str, ctx: MatrixContext) -> str:
    return _inject(text, '        private string Title() { return "%s"; }' % ctx.canonical_title)


def _mut_other_constant(text: str, ctx: MatrixContext) -> str:
    return _inject(text, '        private const string DisplayTitle = "%s";' % ctx.canonical_title)


def _mut_duplicate_declaration(text: str, ctx: MatrixContext) -> str:
    return _inject(text, '        public const string PluginName = "%s";' % ctx.canonical_title)


def _mut_drop_attribute(text: str, _ctx: MatrixContext) -> str:
    return _replace_once(text, ATTRIBUTE_LINE, "")


def _mut_other_approved_copy(text: str, ctx: MatrixContext) -> str:
    return _inject(text, '        private const string ReadyCopy = "%s";' % ctx.approved_copy)


def _mut_comment_copy(text: str, ctx: MatrixContext) -> str:
    return _inject(text, "        // manifest copy illustrated here only: " + ctx.approved_copy)


def _mut_attribute_in_line_comment(text: str, _ctx: MatrixContext) -> str:
    return _replace_once(text, ATTRIBUTE_LINE, "    // " + ATTRIBUTE_LINE.strip())


def _mut_attribute_in_block_comment(text: str, _ctx: MatrixContext) -> str:
    return _replace_once(text, ATTRIBUTE_LINE, "    /* " + ATTRIBUTE_LINE.strip() + " */")


def _mut_attribute_in_string(text: str, _ctx: MatrixContext) -> str:
    return _replace_once(
        text,
        ATTRIBUTE_LINE,
        '    private readonly string registrationNote = "' + ATTRIBUTE_LINE.strip() + '";',
    )


def _mut_attribute_in_verbatim_string(text: str, _ctx: MatrixContext) -> str:
    return _replace_once(
        text,
        ATTRIBUTE_LINE,
        '    private readonly string registrationNote = @"' + ATTRIBUTE_LINE.strip() + '";',
    )


def _mut_commented_declaration(text: str, _ctx: MatrixContext) -> str:
    match = _declaration_span(text)
    commented = match.group(1) + "// " + match.group(0).strip()
    return text[: match.start()] + commented + text[match.end() :]


def _mut_noncanonical_value(text: str, ctx: MatrixContext) -> str:
    match = _declaration_span(text)
    replacement = match.group(1) + 'public const string PluginName = "%s";' % ctx.approved_copy
    return text[: match.start()] + replacement + text[match.end() :]


def _mut_attribute_on_other_class(text: str, _ctx: MatrixContext) -> str:
    text = _replace_once(text, ATTRIBUTE_LINE + "\n", "")
    return _inject(
        text,
        ATTRIBUTE_LINE + "\n    internal sealed class OtherRegistration\n    {\n    }",
    )


# ---------------------------------------------------------------------------
# Resource / source-policy mutations
# ---------------------------------------------------------------------------


def _mut_withculture_missing(fixture: Path, _ctx: MatrixContext) -> None:
    text = _read(fixture, PROJECT_REL)
    _write(fixture, PROJECT_REL, _replace_once(text, ' WithCulture="false"', ""))


def _mut_logicalname_missing(fixture: Path, _ctx: MatrixContext) -> None:
    text = _read(fixture, PROJECT_REL)
    new = text.replace("<LogicalName>", "").replace("</LogicalName>", "")
    if new == text:
        raise ValueError("LogicalName markers not found")
    _write(fixture, PROJECT_REL, new)


def _mut_resource_file_missing(fixture: Path, _ctx: MatrixContext) -> None:
    (fixture / ZH_REL).unlink()


def _drop_catalog_key(fixture: Path, rel: str, key: str) -> None:
    data = _read_json(fixture, rel)
    if not isinstance(data, dict) or key not in data:
        raise ValueError(f"catalog key {key!r} not found in {rel}")
    del data[key]
    _write_json(fixture, rel, data)


def _mut_key_parity(fixture: Path, _ctx: MatrixContext) -> None:
    _drop_catalog_key(fixture, EN_REL, "gksr.settings.group.title")


def _mut_required_hud_key(fixture: Path, _ctx: MatrixContext) -> None:
    _drop_catalog_key(fixture, EN_REL, "gksr.hud.sermonReminder")
    _drop_catalog_key(fixture, ZH_REL, "gksr.hud.sermonReminder")


def _set_catalog_value(fixture: Path, rel: str, key: str, value: object) -> None:
    data = _read_json(fixture, rel)
    if not isinstance(data, dict) or key not in data:
        raise ValueError(f"catalog key {key!r} not found in {rel}")
    data[key] = value
    _write_json(fixture, rel, data)


def _mut_placeholder_parity(fixture: Path, _ctx: MatrixContext) -> None:
    _set_catalog_value(fixture, EN_REL, "gksr.hud.sermonCountdown.other", "days before the Sermon Day.")


def _mut_nonstring_value(fixture: Path, _ctx: MatrixContext) -> None:
    _set_catalog_value(fixture, EN_REL, "gksr.hud.sermonReminder", 5)


def _mut_blank_value(fixture: Path, _ctx: MatrixContext) -> None:
    _set_catalog_value(fixture, EN_REL, "gksr.hud.sermonReminder", "   ")


def _mut_manifest_mismatch(fixture: Path, _ctx: MatrixContext) -> None:
    _set_catalog_value(fixture, EN_REL, "gksr.hud.sermonReminder", "Altered sentence")


def _mut_supported_language_dropped(fixture: Path, _ctx: MatrixContext) -> None:
    text = _read(fixture, PROJECT_REL)
    pattern = re.compile(
        r'\s*<EmbeddedResource Include="Localization/gksr\.zh_cn\.json"[^>]*>'
        r"\s*<LogicalName>[^<]*</LogicalName>\s*</EmbeddedResource>"
    )
    new, count = pattern.subn("", text)
    if count == 0:
        raise ValueError("zh_cn EmbeddedResource entry not found")
    _write(fixture, PROJECT_REL, new)


def _mut_catalog_unparsable(fixture: Path, _ctx: MatrixContext) -> None:
    _write(fixture, EN_REL, "{ not valid json")


def _mut_prohibited_binary(fixture: Path, _ctx: MatrixContext) -> None:
    _write(fixture, "vendor/native-copy.dll", "MZ not a real assembly")


def _mut_prohibited_doc(fixture: Path, _ctx: MatrixContext) -> None:
    _write(fixture, "docs/notes.md", "# not an ADR\n")


def _mut_reference_private_missing(fixture: Path, _ctx: MatrixContext) -> None:
    text = _read(fixture, PROJECT_REL)
    _write(fixture, PROJECT_REL, _replace_once(text, "<Private>false</Private>", ""))


def _mut_reference_private_true(fixture: Path, _ctx: MatrixContext) -> None:
    text = _read(fixture, PROJECT_REL)
    _write(
        fixture,
        PROJECT_REL,
        _replace_once(text, "<Private>false</Private>", "<Private>true</Private>"),
    )


def _add_adr_doc(fixture: Path, _ctx: MatrixContext) -> None:
    _write(fixture, "docs/adr/0001-source-matrix-probe.md", "# ADR probe\n")


def _add_extra_plugin_copy(fixture: Path, ctx: MatrixContext) -> None:
    _write(
        fixture,
        "src/GK2.SermonReminder/ExtraCopy.cs",
        'internal static class ExtraCopy\n{\n    internal const string Title = "%s";\n}\n' % ctx.canonical_title,
    )


def _noop(_fixture: Path, _ctx: MatrixContext) -> None:
    return None


# ---------------------------------------------------------------------------
# Case catalogue
# ---------------------------------------------------------------------------


def _build_cases() -> list[Case]:
    sentence_group = "registration-exemption"
    resource_group = "resource-constraints"
    policy_group = "source-policy"
    positive_group = "positive-control"
    return [
        Case("a", sentence_group, "unchanged canonical declaration passes", EXPECT_PASS, _noop),
        Case(
            "b",
            sentence_group,
            "inline method duplicates canonical title",
            EXPECT_SENTENCE,
            _plugin_case(_mut_inline_title),
        ),
        Case(
            "c", sentence_group, "another .cs file duplicates canonical title", EXPECT_SENTENCE, _add_extra_plugin_copy
        ),
        Case(
            "d",
            sentence_group,
            "another const duplicates canonical title",
            EXPECT_SENTENCE,
            _plugin_case(_mut_other_constant),
        ),
        Case(
            "e",
            sentence_group,
            "duplicate PluginName declaration",
            EXPECT_SENTENCE,
            _plugin_case(_mut_duplicate_declaration),
        ),
        Case("f", sentence_group, "BepInPlugin attribute dropped", EXPECT_SENTENCE, _plugin_case(_mut_drop_attribute)),
        Case(
            "g",
            sentence_group,
            "another approved finalText copy",
            EXPECT_SENTENCE,
            _plugin_case(_mut_other_approved_copy),
        ),
        Case("h", sentence_group, "approved copy inside a comment", EXPECT_SENTENCE, _plugin_case(_mut_comment_copy)),
        Case(
            "i",
            sentence_group,
            "attribute inside a line comment",
            EXPECT_SENTENCE,
            _plugin_case(_mut_attribute_in_line_comment),
        ),
        Case(
            "j",
            sentence_group,
            "attribute inside a block comment",
            EXPECT_SENTENCE,
            _plugin_case(_mut_attribute_in_block_comment),
        ),
        Case(
            "k",
            sentence_group,
            "attribute inside an ordinary string",
            EXPECT_SENTENCE,
            _plugin_case(_mut_attribute_in_string),
        ),
        Case(
            "l",
            sentence_group,
            "attribute inside a verbatim string",
            EXPECT_SENTENCE,
            _plugin_case(_mut_attribute_in_verbatim_string),
        ),
        Case(
            "m",
            sentence_group,
            "PluginName declaration commented out",
            EXPECT_SENTENCE,
            _plugin_case(_mut_commented_declaration),
        ),
        Case(
            "n",
            sentence_group,
            "non-canonical PluginName value",
            EXPECT_SENTENCE,
            _plugin_case(_mut_noncanonical_value),
        ),
        Case(
            "o",
            sentence_group,
            "attribute moved to another class",
            EXPECT_SENTENCE,
            _plugin_case(_mut_attribute_on_other_class),
        ),
        Case(
            "r1",
            resource_group,
            "EmbeddedResource without WithCulture=false",
            EXPECT_VIOLATION,
            _mut_withculture_missing,
        ),
        Case("r2", resource_group, "EmbeddedResource without LogicalName", EXPECT_VIOLATION, _mut_logicalname_missing),
        Case(
            "r3", resource_group, "EmbeddedResource source file removed", EXPECT_VIOLATION, _mut_resource_file_missing
        ),
        Case("r4", resource_group, "catalog key parity failure", EXPECT_VIOLATION, _mut_key_parity),
        Case("r5", resource_group, "required HUD key absent", EXPECT_VIOLATION, _mut_required_hud_key),
        Case("r6", resource_group, "placeholder parity failure", EXPECT_VIOLATION, _mut_placeholder_parity),
        Case("r7", resource_group, "catalog value is not a string", EXPECT_VIOLATION, _mut_nonstring_value),
        Case("r8", resource_group, "catalog value is blank", EXPECT_VIOLATION, _mut_blank_value),
        Case("r9", resource_group, "catalog value differs from manifest", EXPECT_VIOLATION, _mut_manifest_mismatch),
        Case(
            "r10",
            resource_group,
            "supported language catalog removed",
            EXPECT_VIOLATION,
            _mut_supported_language_dropped,
        ),
        Case("r11", resource_group, "catalog file is not valid JSON", EXPECT_VIOLATION, _mut_catalog_unparsable),
        Case("p1", policy_group, "prohibited binary suffix tracked", EXPECT_VIOLATION, _mut_prohibited_binary),
        Case("p2", policy_group, "doc outside docs/adr", EXPECT_VIOLATION, _mut_prohibited_doc),
        Case("p3", policy_group, "Reference without Private=false", EXPECT_VIOLATION, _mut_reference_private_missing),
        Case("p4", policy_group, "Reference with Private=true", EXPECT_VIOLATION, _mut_reference_private_true),
        Case("z1", positive_group, "doc under docs/adr is allowed", EXPECT_PASS, _add_adr_doc),
    ]


_EXPECTED_SENTENCES: dict[str, str] = {
    "b": "canonical",
    "c": "canonical",
    "d": "canonical",
    "e": "canonical",
    "f": "canonical",
    "g": "approved",
    "h": "approved",
    "i": "canonical",
    "j": "canonical",
    "k": "canonical",
    "l": "canonical",
    "m": "canonical",
    "n": "approved",
    "o": "canonical",
}

_EXPECTED_VIOLATIONS: dict[str, str] = {
    "r1": 'must set WithCulture="false"',
    "r2": "missing an explicit LogicalName",
    "r3": "source file not found",
    "r4": "key parity failure",
    "r5": "required HUD key not implemented",
    "r6": "placeholder parity failure",
    "r7": "must be a nonempty string",
    "r8": "must be a nonempty string",
    "r9": "does not match accepted manifest finalText",
    "r10": "supported language catalog missing",
    "r11": "cannot parse JSON",
    "p1": "prohibited binary",
    "p2": "prohibited doc",
    "p3": "must set <Private>false</Private>",
    "p4": "must set <Private>false</Private>",
}


# ---------------------------------------------------------------------------
# Derivation and reporting
# ---------------------------------------------------------------------------


def _derive_canonical_title(plugin_text: str) -> str:
    match = _PLUGIN_NAME_VALUE.search(plugin_text)
    if match is None:
        raise ValueError('public const string PluginName = "..."; not found')
    value = match.group(1)
    if not value:
        raise ValueError("canonical PluginName value is empty")
    return value


def _final_text_values(document: object) -> list[str]:
    values: list[str] = []
    if not isinstance(document, dict):
        return values
    final_text = document.get("finalText")
    if not isinstance(final_text, dict):
        return values
    for key in sorted(final_text):
        entry = final_text[key]
        if not isinstance(entry, dict):
            continue
        for label in ("en", "zh-CN"):
            value = entry.get(label)
            if isinstance(value, str) and value.strip():
                values.append(value)
    return values


def _read_json_file(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _profile_documents(profiles_document: object) -> list[object]:
    # Settled profiles.json is exactly a flat map context id -> context. A nested
    # {"profiles": ...} wrapper is unsupported and is not unwrapped here either.
    if isinstance(profiles_document, dict):
        return list(profiles_document.values())
    return []


def _derive_approved_copy(repo: Path, manifest_rel: str, canonical_title: str) -> str:
    manifest_path = repo / manifest_rel
    manifest = _read_json_file(manifest_path)
    candidates = _final_text_values(manifest)
    # The settled aggregated index keeps the shared finalText per context rather
    # than inline; the approved copy is read from the index it was given (never a
    # discovered or catalog fallback), so the existing suite works against the
    # migrated default index once that file exists.
    if not candidates and isinstance(manifest, dict):
        profiles_file = manifest.get("profilesFile")
        if isinstance(profiles_file, str) and profiles_file:
            profiles = _read_json_file(manifest_path.parent / profiles_file)
            for profile in _profile_documents(profiles):
                candidates.extend(_final_text_values(profile))
    for value in candidates:
        if value != canonical_title:
            return value
    return "__matrix_approved_copy_unavailable__"


def _flatten_violations(report: dict[str, object]) -> list[str]:
    flattened: list[str] = []
    prohibited = report.get("prohibitedBinaries")
    if isinstance(prohibited, list):
        flattened.extend(f"prohibited binary: {item}" for item in prohibited)
    prohibited_docs = report.get("prohibitedDocs")
    if isinstance(prohibited_docs, list):
        flattened.extend(f"prohibited doc: {item}" for item in prohibited_docs)
    reference_privacy = report.get("referencePrivacy")
    if isinstance(reference_privacy, dict):
        violations = reference_privacy.get("violations")
        if isinstance(violations, list):
            flattened.extend(str(item) for item in violations)
    localization = report.get("localization")
    if isinstance(localization, dict):
        violations = localization.get("violations")
        if isinstance(violations, list):
            flattened.extend(str(item) for item in violations)
    sentences = report.get("sentencesInCode")
    if isinstance(sentences, list):
        flattened.extend(str(item) for item in sentences)
    errors = report.get("errors")
    if isinstance(errors, list):
        flattened.extend(str(item) for item in errors)
    return flattened


def _load_report(path: Path) -> dict[str, object]:
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(data, dict):
        return {str(key): value for key, value in data.items()}
    return {}


def _case_expected(case: Case, ctx: MatrixContext) -> str | None:
    if case.expectation == EXPECT_SENTENCE:
        source = _EXPECTED_SENTENCES.get(case.case_id)
        if source == "canonical":
            return ctx.canonical_title
        if source == "approved":
            return ctx.approved_copy
        return None
    if case.expectation == EXPECT_VIOLATION:
        return _EXPECTED_VIOLATIONS.get(case.case_id)
    return None


def _run_case(
    case: Case,
    repo: Path,
    script: Path,
    manifest_rel: str,
    ctx: MatrixContext,
    fixture_root: Path,
    cases_dir: Path,
) -> dict[str, object]:
    fixture = fixture_root / case.case_id
    shutil.rmtree(fixture, ignore_errors=True)
    setup_error: str | None = None
    try:
        _copy_repo_inputs(repo, fixture)
        case.apply(fixture, ctx)
        _git_init_add(fixture)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        setup_error = f"fixture setup failed: {exc}"

    case_output = cases_dir / f"source-check-matrix-{case.case_id}.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--repo-root",
            str(fixture),
            "--project",
            PROJECT_REL,
            "--manifest",
            manifest_rel,
            "--output",
            str(case_output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    report = _load_report(case_output)
    raw_hits = report.get("sentencesInCode")
    hits = [str(item) for item in raw_hits] if isinstance(raw_hits, list) else []
    violations = _flatten_violations(report)
    ok_flag = report.get("ok") is True
    e2e_null = "e2eVerdict" in report and report.get("e2eVerdict") is None
    expected = _case_expected(case, ctx)

    if case.expectation == EXPECT_PASS:
        passed = completed.returncode == 0 and ok_flag and not violations and e2e_null
    elif case.expectation == EXPECT_SENTENCE:
        passed = (
            completed.returncode == 2 and not ok_flag and expected is not None and any(expected in hit for hit in hits)
        )
    elif case.expectation == EXPECT_VIOLATION:
        passed = (
            completed.returncode == 2
            and not ok_flag
            and expected is not None
            and any(expected in violation for violation in violations)
        )
    else:
        passed = False

    if setup_error is not None:
        passed = False

    failure = ""
    if not passed:
        failure = (
            f"exit={completed.returncode} ok={ok_flag} "
            f"expected={case.expectation}:{expected!r} "
            f"hits={hits!r} violations={violations!r}"
        )
        if setup_error is not None:
            failure = setup_error + "; " + failure

    return {
        "case": case.case_id,
        "suite": "existing",
        "group": case.group,
        "description": case.description,
        "expectation": case.expectation,
        "expectedEvidence": expected,
        "exit": completed.returncode,
        "ok": ok_flag,
        "e2eVerdictNull": e2e_null,
        "sentencesInCode": hits,
        "violations": violations,
        "reportPath": str(case_output),
        "passed": passed,
        "failure": failure,
    }


# ---------------------------------------------------------------------------
# v2 aggregated-index fixture builder and cases
# ---------------------------------------------------------------------------


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_object_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


class V2SeedError(Exception):
    """The supplied v2 seed cannot produce a valid fixture (setup failure)."""


@dataclass(frozen=True)
class V2Seed:
    """Metadata a synthetic v2 fixture is derived from.

    The seed is exactly the caller-supplied manifest/index (``--seed-manifest``
    when given, otherwise ``--manifest``): a v1 ticket manifest now, the settled
    aggregated v2 index after migration. Real scenario definitions are never
    collected or redistributed; only shared metadata and approved finalText
    wording are read.
    """

    shared: dict[str, object]
    final_text: dict[str, object]
    source: str


@dataclass(frozen=True)
class V2Case:
    """One black-box v2 source-guard acceptance case."""

    case_id: str
    description: str
    expectation: str
    variant: str
    manifest_rel: str = V2_INDEX_REL


def _strip_profile_shared(document: object) -> dict[str, object]:
    return {key: value for key, value in _as_object_dict(document).items() if key not in _V2_PROFILE_OMITTED}


def _collect_final_text(document: object) -> dict[str, object]:
    return _as_object_dict(_as_object_dict(document).get("finalText"))


def _seed_from_v1(document: object) -> V2Seed:
    data = _as_object_dict(document)
    return V2Seed(
        shared=_strip_profile_shared(data),
        final_text=_collect_final_text(data),
        source="v1-ticket-manifest",
    )


def _seed_from_v2_index(manifest_path: Path, document: object) -> V2Seed:
    data = _as_object_dict(document)
    profiles_file = data.get("profilesFile")
    if not isinstance(profiles_file, str) or not profiles_file.strip():
        raise V2SeedError("v2 index must declare a non-empty profilesFile string")
    profiles_path = manifest_path.parent / profiles_file
    if not profiles_path.is_file():
        raise V2SeedError(f"v2 index profilesFile not found: {profiles_file}")
    profiles_document = _read_json_file(profiles_path)
    if not isinstance(profiles_document, dict) or not profiles_document:
        raise V2SeedError(f"v2 profiles file {profiles_file} must be a non-empty object of contexts")
    shared: dict[str, object] = {}
    final_text: dict[str, object] = {}
    for profile_id in sorted(profiles_document):
        profile = profiles_document[profile_id]
        if not isinstance(profile, dict):
            raise V2SeedError(f"v2 context {profile_id!r} must be an object, got {type(profile).__name__}")
        profile_final_text = profile.get("finalText")
        if not isinstance(profile_final_text, dict) or not profile_final_text:
            raise V2SeedError(f"v2 context {profile_id!r} must declare a non-empty finalText object")
        if not shared:
            shared = _strip_profile_shared(profile)
        for key, value in _as_object_dict(profile_final_text).items():
            final_text.setdefault(key, value)
    return V2Seed(shared=shared, final_text=final_text, source="v2-aggregated-index")


def _catalog_maps(repo: Path) -> tuple[dict[str, object], dict[str, object]]:
    return _as_object_dict(_read_json_file(repo / EN_REL)), _as_object_dict(_read_json_file(repo / ZH_REL))


def _load_v2_seed(repo: Path, manifest_rel: str) -> V2Seed:
    """Resolve the one explicit seed; never discover or fall back to another file."""
    manifest_path = repo / manifest_rel
    if not manifest_path.is_file():
        raise V2SeedError(f"seed manifest not found: {manifest_rel}")
    document = _read_json_file(manifest_path)
    if document is None:
        raise V2SeedError(f"seed manifest is not readable JSON: {manifest_rel}")
    data = _as_object_dict(document)
    if data.get("kind") == "gksr-e2e-manifest" and data.get("manifestVersion") == 2:
        return _seed_from_v2_index(manifest_path, data)
    final_text = data.get("finalText")
    if data.get("manifestVersion") == 1 and isinstance(final_text, dict) and final_text:
        return _seed_from_v1(data)
    raise V2SeedError(
        f"seed manifest {manifest_rel} is neither a manifestVersion 1 ticket manifest nor a "
        "kind='gksr-e2e-manifest' manifestVersion 2 index"
    )


def _split_final_text(final_text: dict[str, object], count: int) -> list[dict[str, object]]:
    keys = sorted(final_text)
    if count <= 1:
        return [dict(final_text)]
    if len(keys) < count:
        raise V2SeedError(f"seed finalText has {len(keys)} key(s); need at least {count} to build {count} contexts")
    mid = len(keys) // 2
    if mid < 1 or mid >= len(keys):
        raise V2SeedError(f"cannot split {len(keys)} finalText keys across {count} contexts")
    return [
        {key: final_text[key] for key in keys[:mid]},
        {key: final_text[key] for key in keys[mid:]},
    ]


def _profile_truthy_final_text_key(final_text: dict[str, object]) -> str | None:
    for key in sorted(final_text):
        entry = _as_object_dict(final_text[key])
        for label in ("en", "zh-CN"):
            value = entry.get(label)
            if isinstance(value, str) and value.strip():
                return key
    return None


def _synthetic_scenario(profile_id: str, final_text: dict[str, object]) -> dict[str, object]:
    """One valid but never-executed v2 scenario for a synthetic context.

    The fixture is structural only: it is written to a disposable manifest and
    never executed, so its observation is an explicit ``fixture.*`` marker. When
    the context owns usable finalText wording, a second expectation references a
    ``finalTextKey`` that exists in *that* context's own finalText.
    """
    expect: list[dict[str, object]] = [{"observation": "fixture.flag", "equals": True}]
    local_key = _profile_truthy_final_text_key(final_text)
    if local_key is not None:
        expect.append({"observation": "fixture.sentence", "finalTextKey": local_key, "language": "en"})
    return {
        "id": f"{profile_id}-synthetic",
        "title": "synthetic v2 fixture scenario (never executed)",
        "category": "structure",
        "prerequisites": ["Disposable synthetic fixture; no game is loaded or run."],
        "steps": ["Structural fixture only; the matrix never executes a scenario."],
        "expect": expect,
        "evidence": {"screenshot": False, "log": False, "measurement": False},
        "profileId": profile_id,
        "requirements": [V2_REQUIREMENT_ID],
    }


def _v2_profile(shared: dict[str, object], final_text: dict[str, object]) -> dict[str, object]:
    profile = _as_object_dict(copy.deepcopy(shared))
    profile["finalText"] = copy.deepcopy(final_text)
    validation = _as_object_dict(profile.get("validation"))
    validation["supportedCaptureVersions"] = [2]
    profile["validation"] = validation
    return profile


def _standard_v2_tree(seed: V2Seed, context_count: int) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    if context_count <= 1:
        profile_ids = (V2_PROFILE_IDS[0],)
    else:
        profile_ids = V2_PROFILE_IDS
    subsets = _split_final_text(seed.final_text, len(profile_ids))
    profiles: dict[str, object] = {
        profile_id: _v2_profile(seed.shared, subset) for profile_id, subset in zip(profile_ids, subsets)
    }
    scenario_docs: dict[str, dict[str, object]] = {
        profile_id: {
            "kind": V2_SCENARIO_KIND,
            "schemaVersion": 2,
            "scenarios": [_synthetic_scenario(profile_id, subset)],
        }
        for profile_id, subset in zip(profile_ids, subsets)
    }
    return profiles, scenario_docs


def _v2_index(profiles_file: str = "profiles.json", scenario_root: str = "scenarios") -> dict[str, object]:
    return {
        "kind": "gksr-e2e-manifest",
        "manifestVersion": 2,
        "manifestId": V2_MANIFEST_ID,
        "profilesFile": profiles_file,
        "scenarioRoot": scenario_root,
    }


def _drift_secondary_profile(repo: Path, profiles: dict[str, object]) -> None:
    secondary = _as_object_dict(profiles.get(V2_PROFILE_IDS[1]))
    final_text = _as_object_dict(secondary.get("finalText"))
    en_map, _zh_map = _catalog_maps(repo)
    catalog_keys = [key for key in sorted(final_text) if key in en_map]
    target = catalog_keys[0] if catalog_keys else (sorted(final_text)[0] if final_text else "")
    if target:
        entry = _as_object_dict(final_text.get(target))
        entry["en"] = "drifted copy " + str(entry.get("en", ""))
        final_text[target] = entry
    secondary["finalText"] = final_text
    profiles[V2_PROFILE_IDS[1]] = secondary


def _unknown_profile_ref(scenario_docs: dict[str, dict[str, object]]) -> None:
    scenarios = scenario_docs[V2_PROFILE_IDS[1]].get("scenarios")
    if isinstance(scenarios, list) and scenarios:
        first = _as_object_dict(scenarios[0])
        first["profileId"] = "ctx-ghost"
        scenarios[0] = first


def _mutate_secondary_scenario(
    scenario_docs: dict[str, dict[str, object]],
    mutator: Callable[[dict[str, object]], None],
) -> None:
    """Apply one targeted mutation to the secondary context's only scenario.

    The fixture starts from the otherwise-valid synthetic tree, so a rejection
    proves the mutated field is enforced rather than the fixture being broken.
    """
    doc = _as_object_dict(scenario_docs[V2_PROFILE_IDS[1]])
    entries = doc.get("scenarios")
    if not isinstance(entries, list) or not entries:
        raise V2SeedError("secondary scenario document has no scenarios")
    first = _as_object_dict(entries[0])
    mutator(first)
    entries[0] = first
    doc["scenarios"] = entries
    scenario_docs[V2_PROFILE_IDS[1]] = doc


def _write_outside_profiles(fixture: Path, profiles: dict[str, object]) -> None:
    # Deliberately valid contexts written outside the fixture root, so a guard
    # that follows the escaping path is rejected for the escape itself rather
    # than because the out-of-tree file happens to be malformed.
    outside = fixture.parent / "outside-profiles.json"
    outside.write_text(json.dumps(profiles, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _replace_scenarios_with_symlink(fixture: Path) -> None:
    scenario_dir = fixture / V2_SCENARIO_ROOT_REL
    shutil.rmtree(scenario_dir, ignore_errors=True)
    outside = fixture.parent / "outside-scenarios"
    outside.mkdir(parents=True, exist_ok=True)
    doc = {
        "kind": V2_SCENARIO_KIND,
        "schemaVersion": 2,
        "scenarios": [_synthetic_scenario("ctx-primary", {})],
    }
    (outside / "symlinked.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    scenario_dir.symlink_to(outside)


def _replace_index_with_symlink(fixture: Path) -> None:
    """The explicitly requested index path is itself a symlink.

    The link target is a complete valid v2 index in the same directory, so the
    rejection must come from the requested index file being a symlink and not
    from a malformed target. Only the explicit index path and referenced paths
    are policy-checked; unrelated OS ancestor aliases are out of scope.
    """
    index_path = fixture / V2_INDEX_REL
    target = index_path.with_name("manifest-real.json")
    target.write_text(index_path.read_text(encoding="utf-8"), encoding="utf-8")
    index_path.unlink()
    index_path.symlink_to(target.name)


def _build_v2_fixture(fixture: Path, repo: Path, seed: V2Seed, variant: str) -> None:
    _copy_repo_inputs(repo, fixture)
    # The copier now mirrors the repository's real e2e tree, canonical scenario
    # root included. Replace that copied subtree with the synthetic one so real
    # scenario ids/profiles never leak and a deliberately empty root really is
    # empty; only this disposable fixture is touched, never the source repo.
    shutil.rmtree(fixture / V2_SCENARIO_ROOT_REL, ignore_errors=True)
    context_count = 1 if variant == "v2-valid-single" else 2
    profiles, scenario_docs = _standard_v2_tree(seed, context_count)
    index = _v2_index()
    write_profiles = True
    write_scenarios = True

    if variant == "v2-drift-later":
        _drift_secondary_profile(repo, profiles)
    elif variant == "v2-profile-missing":
        index = _v2_index(profiles_file="missing-profiles.json")
        write_profiles = False
    elif variant == "v2-profile-not-object":
        profiles[V2_PROFILE_IDS[1]] = "not-an-object"
    elif variant == "v2-profile-unknown-ref":
        _unknown_profile_ref(scenario_docs)
    elif variant == "v2-tree-missing":
        index = _v2_index(scenario_root="scenarios-absent")
        write_scenarios = False
    elif variant == "v2-tree-empty":
        write_scenarios = False
    elif variant == "v2-path-escape":
        index = _v2_index(profiles_file="../../../outside-profiles.json")
        _write_outside_profiles(fixture, profiles)
        write_profiles = False
    elif variant == "v2-secondary-finaltext-missing":
        secondary = _as_object_dict(profiles.get(V2_PROFILE_IDS[1]))
        secondary.pop("finalText", None)
        profiles[V2_PROFILE_IDS[1]] = secondary
    elif variant == "v2-secondary-langmap-missing":
        secondary = _as_object_dict(profiles.get(V2_PROFILE_IDS[1]))
        secondary.pop("languageMapping", None)
        profiles[V2_PROFILE_IDS[1]] = secondary
    elif variant == "v2-scenario-expect-empty":
        _mutate_secondary_scenario(scenario_docs, lambda s: s.__setitem__("expect", []))
    elif variant == "v2-scenario-evidence-nonobject":
        _mutate_secondary_scenario(scenario_docs, lambda s: s.__setitem__("evidence", "not-an-object"))
    elif variant == "v2-datecheck-mode-unsupported":
        _mutate_secondary_scenario(
            scenario_docs,
            lambda s: s.__setitem__("expect", [{"dateCheck": {"mode": "unsupported-mode"}}]),
        )

    _write_json(fixture, V2_INDEX_REL, index)
    if write_profiles:
        _write_json(fixture, V2_PROFILES_REL, profiles)
    if variant == "v2-tree-empty":
        (fixture / V2_SCENARIO_ROOT_REL).mkdir(parents=True, exist_ok=True)
    elif write_scenarios:
        for name, doc in scenario_docs.items():
            _write_json(fixture, f"{V2_SCENARIO_ROOT_REL}/{name}.json", doc)

    if variant == "v2-profile-malformed":
        _write(fixture, V2_PROFILES_REL, "{ this is not valid json\n")
    elif variant == "v2-scenario-malformed":
        _write(fixture, f"{V2_SCENARIO_ROOT_REL}/{V2_PROFILE_IDS[1]}.json", "{ not valid json\n")
    elif variant == "v2-ref-changed":
        scenarios_file = fixture / V2_SCENARIO_ROOT_REL / f"{V2_PROFILE_IDS[1]}.json"
        doc = _as_object_dict(_read_json_file(scenarios_file))
        entries = doc.get("scenarios")
        if isinstance(entries, list) and entries:
            first = _as_object_dict(entries[0])
            first["profileId"] = "ctx-ghost"
            entries[0] = first
            doc["scenarios"] = entries
            scenarios_file.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elif variant == "v2-symlink-escape":
        _replace_scenarios_with_symlink(fixture)
    elif variant == "v2-symlink-index":
        _replace_index_with_symlink(fixture)


def _build_v2_cases() -> list[V2Case]:
    return [
        V2Case(
            "v2-valid-multi",
            "aggregate finalText across contexts incl. a later-only key",
            EXPECT_PASS,
            "v2-valid-multi",
        ),
        V2Case("v2-valid-single", "single-context valid v2 index", EXPECT_PASS, "v2-valid-single"),
        V2Case(
            "v2-drift-later",
            "later context finalText drifts from the catalog",
            EXPECT_VIOLATION,
            "v2-drift-later",
        ),
        V2Case("v2-profile-missing", "profilesFile is absent", EXPECT_VIOLATION, "v2-profile-missing"),
        V2Case("v2-profile-malformed", "profiles JSON is unparseable", EXPECT_VIOLATION, "v2-profile-malformed"),
        V2Case(
            "v2-profile-not-object",
            "a profile value is not an object",
            EXPECT_VIOLATION,
            "v2-profile-not-object",
        ),
        V2Case(
            "v2-profile-unknown-ref",
            "a scenario references an unknown profile id",
            EXPECT_VIOLATION,
            "v2-profile-unknown-ref",
        ),
        V2Case(
            "v2-scenario-malformed",
            "a scenario document is unparseable",
            EXPECT_VIOLATION,
            "v2-scenario-malformed",
        ),
        V2Case("v2-tree-missing", "scenarioRoot is absent", EXPECT_VIOLATION, "v2-tree-missing"),
        V2Case("v2-tree-empty", "scenarioRoot has no scenario documents", EXPECT_VIOLATION, "v2-tree-empty"),
        V2Case(
            "v2-ref-changed",
            "a referenced file edited without an index edit is still checked",
            EXPECT_VIOLATION,
            "v2-ref-changed",
        ),
        V2Case(
            "v2-path-escape",
            "profilesFile escapes the repository root",
            EXPECT_VIOLATION,
            "v2-path-escape",
        ),
        V2Case(
            "v2-symlink-escape",
            "scenarioRoot symlinks outside the repository",
            EXPECT_VIOLATION,
            "v2-symlink-escape",
        ),
        V2Case(
            "v2-secondary-finaltext-missing",
            "a secondary context declares no finalText",
            EXPECT_VIOLATION,
            "v2-secondary-finaltext-missing",
        ),
        V2Case(
            "v2-secondary-langmap-missing",
            "a secondary context declares no languageMapping",
            EXPECT_VIOLATION,
            "v2-secondary-langmap-missing",
        ),
        V2Case(
            "v2-scenario-expect-empty",
            "a scenario declares an empty expect array",
            EXPECT_VIOLATION,
            "v2-scenario-expect-empty",
        ),
        V2Case(
            "v2-scenario-evidence-nonobject",
            "a scenario declares a non-object evidence field",
            EXPECT_VIOLATION,
            "v2-scenario-evidence-nonobject",
        ),
        V2Case(
            "v2-datecheck-mode-unsupported",
            "a scenario declares an unsupported dateCheck mode",
            EXPECT_VIOLATION,
            "v2-datecheck-mode-unsupported",
        ),
        V2Case(
            "v2-symlink-index",
            "the requested index file itself is a symlink to a valid index",
            EXPECT_VIOLATION,
            "v2-symlink-index",
        ),
    ]


def _v2_unsupported(text: str) -> bool:
    lowered = text.lower()
    return any(
        token in lowered
        for token in (
            "unsupported manifest kind",
            "unsupported kind",
            "unknown manifest kind",
            "manifest kind is not supported",
            "not supported",
        )
    )


def _run_v2_case(
    case: V2Case,
    repo: Path,
    script: Path,
    seed: V2Seed,
    fixture_root: Path,
    cases_dir: Path,
) -> dict[str, object]:
    fixture = fixture_root / case.case_id
    shutil.rmtree(fixture, ignore_errors=True)
    setup_error: str | None = None
    index_sha256: str | None = None
    try:
        _build_v2_fixture(fixture, repo, seed, case.variant)
        _git_init_add(fixture)
        index_path = fixture / case.manifest_rel
        if index_path.is_file():
            index_sha256 = _sha256_path(index_path)
    except (OSError, ValueError, V2SeedError, subprocess.CalledProcessError) as exc:
        setup_error = f"v2 fixture setup failed: {exc}"

    case_output = cases_dir / f"source-check-matrix-{case.case_id}.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--repo-root",
            str(fixture),
            "--project",
            PROJECT_REL,
            "--manifest",
            case.manifest_rel,
            "--output",
            str(case_output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    report = _load_report(case_output)
    raw_hits = report.get("sentencesInCode")
    hits = [str(item) for item in raw_hits] if isinstance(raw_hits, list) else []
    violations = _flatten_violations(report)
    ok_flag = report.get("ok") is True
    e2e_null = "e2eVerdict" in report and report.get("e2eVerdict") is None
    diagnostic = (completed.stdout or "") + (completed.stderr or "")

    if setup_error is not None:
        observed = "setup-error"
        failure_class = "harness-setup-error"
    elif completed.returncode not in (0, 2) or not report:
        observed = "tool-crash"
        failure_class = "tool-error"
    elif completed.returncode == 2 and not ok_flag:
        observed = "rejected"
        if _v2_unsupported(diagnostic):
            failure_class = "v2-unsupported"
        elif case.expectation == EXPECT_PASS:
            failure_class = "rejected-unexpectedly"
        else:
            failure_class = "rejected-as-expected"
    elif completed.returncode == 0 and ok_flag:
        observed = "accepted"
        failure_class = "accepted-as-expected" if case.expectation == EXPECT_PASS else "v2-not-enforced"
    else:
        observed = "indeterminate"
        failure_class = "indeterminate"

    if case.expectation == EXPECT_PASS:
        passed = setup_error is None and completed.returncode == 0 and ok_flag and not violations and e2e_null
    else:
        # A negative only passes when the tool rejected the fixture with a
        # structured violation/error: an arbitrary crash (classified tool-error),
        # a missing explicit e2eVerdict: null, or a blanket "unsupported v2"
        # refusal never counts.
        passed = (
            setup_error is None
            and completed.returncode == 2
            and not ok_flag
            and e2e_null
            and failure_class == "rejected-as-expected"
            and bool(violations)
        )

    failure = ""
    if not passed:
        failure = (
            f"exit={completed.returncode} ok={ok_flag} observed={observed} class={failure_class} "
            f"violations={violations!r}"
        )
        if setup_error is not None:
            failure = setup_error + "; " + failure

    return {
        "case": case.case_id,
        "suite": "v2",
        "group": "v2-source-guard",
        "description": case.description,
        "expectation": case.expectation,
        "variant": case.variant,
        "manifest": case.manifest_rel,
        "exit": completed.returncode,
        "ok": ok_flag,
        "observed": observed,
        "v2FailureClass": failure_class,
        "e2eVerdictNull": e2e_null,
        "indexSha256": index_sha256,
        "sentencesInCode": hits,
        "violations": violations,
        "reportPath": str(case_output),
        "passed": passed,
        "failure": failure,
    }


def _v2_setup_result(case: V2Case, manifest_rel: str, error: str) -> dict[str, object]:
    """Per-case setup failure when the explicit seed cannot build any fixture."""
    return {
        "case": case.case_id,
        "suite": "v2",
        "group": "v2-source-guard",
        "description": case.description,
        "expectation": case.expectation,
        "variant": case.variant,
        "manifest": manifest_rel,
        "exit": None,
        "ok": False,
        "observed": "setup-error",
        "v2FailureClass": "harness-setup-error",
        "e2eVerdictNull": False,
        "indexSha256": None,
        "sentencesInCode": [],
        "violations": [],
        "reportPath": None,
        "passed": False,
        "failure": error,
    }


def _git_head(repo: Path) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    head = completed.stdout.strip()
    return head or None


def _write_matrix_report(output: Path, report: dict[str, object]) -> None:
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    written = _load_report(output)
    if "e2eVerdict" not in written or written.get("e2eVerdict") is not None:
        raise ValueError("written source matrix artifact lacks an explicit e2eVerdict: null")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GKSA-23 source-tool acceptance matrix.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument(
        "--manifest",
        default=V2_INDEX_REL,
        help=(
            "Repository-relative accepted manifest. The existing source-policy suite "
            "reads the approved finalText from it (a v1 ticket manifest such as "
            "tests/e2e/gksa12.json now, the migrated tests/e2e/manifest.json after "
            "migration); it is not tied to a ticket identity. The v2 suite uses this "
            "path as its sole seed unless --seed-manifest overrides it, and writes the "
            "settled aggregated index at this path inside each disposable fixture."
        ),
    )
    parser.add_argument(
        "--seed-manifest",
        default="",
        help=(
            "Explicit seed manifest for the v2 fixture builder. When empty, --manifest "
            "is the sole seed. There is no discovery of another tests/e2e/*.json file "
            "and no localization-catalog fallback: a missing or malformed seed is a "
            "harness setup failure."
        ),
    )
    parser.add_argument(
        "--suite",
        choices=("existing", "v2", "all"),
        default="all",
        help=("existing: the 31 existing source-policy cases; v2: the aggregated-index cases; all: both (default)."),
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    repo = Path(args.repo_root).resolve()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    cases_dir = output.parent / (output.stem + "-cases")
    cases_dir.mkdir(parents=True, exist_ok=True)

    script = repo / "tools" / "source_check.py"
    plugin_path = repo / PLUGIN_REL
    manifest_present = (repo / args.manifest).is_file()

    setup_errors: list[str] = []
    if not script.is_file():
        setup_errors.append(f"source_check.py not found: {script}")

    canonical_title = ""
    try:
        canonical_title = _derive_canonical_title(plugin_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        setup_errors.append(f"cannot derive canonical PluginName: {exc}")

    approved_copy = _derive_approved_copy(repo, args.manifest, canonical_title)
    ctx = MatrixContext(
        canonical_title=canonical_title or "__matrix_canonical_title_unavailable__",
        approved_copy=approved_copy,
    )

    run_existing = args.suite in ("existing", "all")
    run_v2 = args.suite in ("v2", "all")

    existing_results: list[dict[str, object]] = []
    existing_failures: list[str] = []
    v2_results: list[dict[str, object]] = []
    v2_failures: list[str] = []
    v2_classification: dict[str, int] = {}
    seed_meta: dict[str, object] = {}

    with tempfile.TemporaryDirectory(prefix="gksa23-source-matrix-") as tmp:
        fixture_root = Path(tmp)
        if run_existing:
            for case in _build_cases():
                result = _run_case(case, repo, script, args.manifest, ctx, fixture_root, cases_dir)
                existing_results.append(result)
                if result["passed"] is not True:
                    existing_failures.append(f"{case.case_id}: {result['failure']}")
        if run_v2:
            seed_rel = args.seed_manifest or args.manifest
            seed: V2Seed | None = None
            seed_error: str | None = None
            try:
                seed = _load_v2_seed(repo, seed_rel)
                # The multi-context control must be buildable; an unfillable seed is
                # a harness setup failure, not a silently truncated fixture.
                _split_final_text(seed.final_text, len(V2_PROFILE_IDS))
            except V2SeedError as exc:
                seed = None
                seed_error = f"v2 seed unavailable: {exc}"
                setup_errors.append(seed_error)
                seed_meta = {"requestedManifest": seed_rel, "source": None, "error": str(exc)}
            if seed is not None:
                seed_meta = {
                    "requestedManifest": seed_rel,
                    "source": seed.source,
                    "finalTextKeys": sorted(seed.final_text),
                    "contextProfileIds": list(V2_PROFILE_IDS),
                }
            for case in _build_v2_cases():
                if seed is None:
                    result = _v2_setup_result(case, seed_rel, seed_error or "v2 seed unavailable")
                else:
                    result = _run_v2_case(case, repo, script, seed, fixture_root, cases_dir)
                v2_results.append(result)
                classification = str(result["v2FailureClass"])
                v2_classification[classification] = v2_classification.get(classification, 0) + 1
                if result["passed"] is not True:
                    v2_failures.append(f"{case.case_id}: {result['failure']}")

    failures = setup_errors + existing_failures + v2_failures
    ok = not failures and bool(existing_results or v2_results)
    note = (
        "Source-policy CLI acceptance only. Every fixture is a disposable copy of "
        "this repository's own versioned inputs inside a temporary git repository "
        "(no commits). No native build, no game execution, no E2E verdict."
    )
    if not manifest_present:
        note += (
            " The requested --manifest index is absent, so the fail-closed RED run "
            "documents the future aggregated index rather than a crash."
        )
    if run_v2:
        note += (
            " v2 cases synthesise the settled aggregated-index schema without migrating "
            "real data and record the observed behaviour, so 'v2 not enforced' is "
            "distinguishable from a harness error or a tool crash."
        )
    report: dict[str, object] = {
        "artifactVersion": 2,
        "kind": "gksa23-source-tool-acceptance-matrix",
        "assertion": "source-policy-only",
        "synthetic": True,
        "toolOnly": True,
        "e2eVerdict": None,
        "ok": ok,
        "suite": args.suite,
        "commit": _git_head(repo),
        "command": [sys.executable, *sys.argv],
        "exit": 0 if ok else 1,
        "repositoryRoot": str(repo),
        "manifest": args.manifest,
        "manifestPresent": manifest_present,
        "canonicalTitle": canonical_title,
        "approvedCopy": approved_copy,
        "python": sys.executable,
        "originalCaseCount": 15,
        "existingCaseCount": len(existing_results),
        "v2CaseCount": len(v2_results),
        "caseCount": len(existing_results) + len(v2_results),
        "existingFailures": existing_failures,
        "v2Seed": seed_meta,
        "v2Classification": v2_classification,
        "cases": [*existing_results, *v2_results],
        "failures": failures,
        "unexecuted": [],
        "note": note,
    }
    _write_matrix_report(output, report)

    if failures:
        for failure in failures:
            sys.stderr.write(f"FAIL [source-matrix] {failure}\n")
        sys.stderr.write(
            f"RESULT: FAIL (source-tool acceptance matrix; suite={args.suite}; "
            f"existing={len(existing_results)} v2={len(v2_results)} cases; manifest={args.manifest})\n"
        )
        return 1

    sys.stderr.write(
        "RESULT: PASS (source-tool acceptance matrix; "
        f"suite={args.suite}; {len(existing_results) + len(v2_results)} cases; "
        f"manifest={args.manifest}; no game execution asserted)\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
