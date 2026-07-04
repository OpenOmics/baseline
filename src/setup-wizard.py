#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
"""
setup-wizard.py: An interactive setup wizard for the OpenOmics/baseline
pipeline template. This interactive setup wizard can be used for quickly
creating new pipelines from the baseline template. It replaces the old,
manual find/sed/mv instructions with a guided, interactive experience.

At a high-level, the wizard:
  1. Collects the new pipeline name (used to replace any instances of
     the string `baseline` and renaming pipeline cli).
  2. Collects documentation placeholder values (i.e long name, mission
     statement, etc.) from the user
  3. Collects the data type of the pipelines input files (i.e fastq,
     CRAM/BAM, VCF, CSV, TSV, etc).
  4. Renames every occurrence of `baseline` to the new pipeline name.
  5. Renames the main executable (`baseline` -> <new_pipeline_name>).
  6. Updates config/config.json with the chosen input data type.
  7. If the input type is not illumina_fastq, rewrites FastQ references
     throughout the tree to the chosen file type / extension.
  8. Fills in the {{ placeholder }} template strings.

3rd-party Dependencies:
    pip install questionary

Usage:
    # Dry-run the wizard to see what would
    # change before actually applying it.
    python baseline_wizard.py --repo /path/to/baseline --dry-run
    # Run the wizard interactively to apply
    # changes to the baseline template in-place.
    python baseline_wizard.py --repo /path/to/baseline
"""
# Standard library
from __future__ import annotations
import argparse
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 3rd-party imports
try:
    # questionary drives the interactive prompts
    import questionary
    from questionary import Validator, ValidationError
except ImportError:
    sys.exit(
        "Error: Missing required python package! This wizard needs the 'questionary' python package.\n\n"
        "Please install it with the commands below and try again:\n"
        "  python3 -m venv .venv && source .venv/bin/activate\n"
        "  pip install -U pip\n"
        "  pip install questionary"
    )


# Pipeline configuration
# Mirrors baseline's SUPPORTED_INPUT_FILETYPES. The "label" is what fastq text
# gets replaced with (upper-case datatype), and "primary_ext" is the canonical
# extension used to replace `.fastq.gz`. Keeping this table in lock-step with the
# pipeline's own definition is what lets the wizard rewrite the template safely.
SUPPORTED_INPUT_FILETYPES = {
    "illumina_fastq": {"fastq": True,  "exts": [".R1.fastq.gz", ".R2.fastq.gz"], "primary_ext": ".R1.fastq.gz", "label": "FastQ", "paired": True},
    "ont_fastq":      {"fastq": True,  "exts": [".fastq.gz", ".fq.gz", ".fastq", ".fq"], "primary_ext": ".fastq.gz", "label": "FastQ", "paired": False},
    "bam":            {"fastq": False, "exts": [".bam"],  "primary_ext": ".bam",  "label": "BAM", "paired": False},
    "cram":           {"fastq": False, "exts": [".cram"], "primary_ext": ".cram", "label": "CRAM", "paired": False},
    "vcf":            {"fastq": False, "exts": [".vcf.gz", ".vcf"], "primary_ext": ".vcf.gz", "label": "VCF", "paired": False},
    "tsv":            {"fastq": False, "exts": [".tsv.gz", ".tsv"], "primary_ext": ".tsv.gz", "label": "TSV", "paired": False},
    "csv":            {"fastq": False, "exts": [".csv.gz", ".csv"], "primary_ext": ".csv.gz", "label": "CSV", "paired": False},
}

# Number of test sample files to create
# in .tests for the CI dry-run.
TEST_SAMPLE_COUNT = 4

# Paths (relative to repo root) that should never be touched, matching the
# exclusions in the original find/sed one-liners. `src`/`workflow` hold pipeline
# logic that legitimately uses the word "baseline" as a term of art, and `.git`
# / version / changelog files must stay byte-for-byte intact.
EXCLUDE_PATH_PARTS = {".git", "src", "workflow", ".baseline_version", "CHANGELOG.md"}

# Specific files (relative to repo root) to leave entirely untouched. The
# release-please workflow checks if repo is baseline template before it gets
# triggered. We need to ignore this or it will never run for new pipelines.
EXCLUDE_RELATIVE_FILES = {
    Path(".github/workflows/release-please.yaml"),
    Path("src/setup-wizard.py"),                    # this script itself!
}

# Extensions we treat as binary and skip entirely. Attempting a text
# search/replace on these would corrupt them, and they never contain the
# template tokens we care about anyway.
BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".gz", ".bam", ".cram",
    ".zip", ".tar", ".whl", ".so", ".dylib", ".o", ".pyc", ".woff", ".woff2",
    ".ttf", ".eot",
}

# The template placeholders and the prompts used to collect them.
# Order matters -> shown to the user in this order. Each entry carries an
# `example` so the prompt can show the user the shape of a good answer.
TEMPLATE_FIELDS = [
    {
        "key": "pipeline_long_name",
        "message": "Pipeline long name (a short human-readable title):",
        "example": "Whole Genome and Exome Clinical Sequencing Pipeline",
    },
    {
        "key": "pipeline_mission_statement",
        "message": "Mission statement (completes 'Its long-term goals: <...>!'):",
        "example": "to accurately call germline and somatic variants, to infer SVs & CNVs",
    },
    {
        "key": "pipeline_long_description",
        "message": "Long description (completes '<name> is a comprehensive <...>'):",
        "example": "clinical WGS and WES pipeline focused on speed without sacrificing accuracy",
    },
]


# Custom Questionary Validators
class PipelineNameValidator(Validator):
    """Validates the pipeline name as it is typed at the prompt.
    The name doubles as the CLI command and a filename, so we restrict it to
    alpha-numerics and hyphens (no spaces) to keep it shell- and path-safe.
    """

    # Anchored pattern: first char must be alpha-numeric (never a leading
    # hyphen, which would read as a CLI flag), remaining chars may include '-'.
    _pat = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")

    def validate(self, document):  # noqa: D102
        """Raises a ValidationError when the typed name is unusable.
        Called live by questionary on every keystroke/submit.
        @param document <questionary.Document>:
            The prompt buffer; `document.text` holds the current input
        @return <None>:
            Returns nothing on success; raises ValidationError on failure
        """
        text = document.text.strip()
        # Empty input is the most common mistake, so it gets its own message.
        if not text:
            raise ValidationError(message="Name cannot be empty.")
        if not self._pat.match(text):
            raise ValidationError(
                message="Use only alpha-numeric characters and hyphens "
                        "(no spaces), and don't start with a hyphen."
            )


class NonEmptyValidator(Validator):
    """Rejects blank/whitespace-only answers for free-text prompts.
    Used for the documentation fields, which must contain real prose to
    fill in the template placeholders sensibly.
    """

    def validate(self, document):  # noqa: D102
        """Raises a ValidationError when the field is blank.
        @param document <questionary.Document>:
            The prompt buffer; `document.text` holds the current input
        @return <None>:
            Returns nothing on success; raises ValidationError on failure
        """
        if not document.text.strip():
            raise ValidationError(message="This field cannot be empty.")


# Answer container
@dataclass
class WizardAnswers:
    """Bundles every choice the user made into one passable object.
    Storing the raw answers plus derived properties keeps the transformation
    functions free of repeated lookups into SUPPORTED_INPUT_FILETYPES.
    @attr pipeline_name <str>:
        The new pipeline / CLI name that replaces `baseline`
    @attr input_type <str>:
        Key into SUPPORTED_INPUT_FILETYPES (e.g. 'bam', 'ont_fastq')
    @attr template_values <dict>:
        Placeholder key -> user-supplied text, for {{ token }} substitution
    """
    pipeline_name: str = ""
    input_type: str = "illumina_fastq"
    template_values: dict = field(default_factory=dict)

    @property
    def is_fastq(self) -> bool:
        """Reports whether the chosen input type is a FastQ variant.
        Drives whether FastQ references need rewriting at all.
        @return <bool>:
            True if the selected input type is fastq-based
        """
        return SUPPORTED_INPUT_FILETYPES[self.input_type]["fastq"]

    @property
    def filetype_label(self) -> str:
        """The human/upper-case label the word 'fastq' is replaced with.
        @return <str>:
            e.g. 'BAM', 'CRAM', 'VCF' for the chosen datatype
        """
        return SUPPORTED_INPUT_FILETYPES[self.input_type]["label"]

    @property
    def primary_ext(self) -> str:
        """The canonical extension used to replace `.fastq.gz`.
        @return <str>:
            e.g. '.bam', '.vcf.gz', '.R1.fastq.gz'
        """
        return SUPPORTED_INPUT_FILETYPES[self.input_type]["primary_ext"]

    @property
    def is_paired(self) -> bool:
        """Whether the datatype produces paired (R1/R2) files.
        Determines how many files per sample are created in .tests.
        @return <bool>:
            True for paired datatypes (currently only illumina_fastq)
        """
        return SUPPORTED_INPUT_FILETYPES[self.input_type]["paired"]

    @property
    def needs_file_swap(self) -> bool:
        """Whether .tests files must be regenerated.

        True for any type other than illumina_fastq. illumina is the template's
        native type, so its paired R1/R2 files are already correct. Everything
        else — including ONT (single-end fastq) — needs the files replaced.
        @return <bool>:
            True when the .tests files no longer match the chosen input type
        """
        return self.input_type != "illumina_fastq"


# Questionary Prompting
def collect_answers() -> WizardAnswers:
    """Runs the interactive prompts and returns the user's choices.
    Centralizing all questionary interaction here keeps the rest of the wizard
    non-interactive and therefore easy to test. Any Ctrl-C / cancel (questionary
    returns None) aborts immediately rather than proceeding with partial data.
    @return <WizardAnswers>:
        A fully populated answer object ready to drive the transformations
    """
    questionary.print("\nOpenOmics baseline setup wizard\n", style="bold italic")

    # 1. Pipeline name: Validated against the CLI-safe pattern as it is typed.
    name = questionary.text(
        "New pipeline name (this is also your CLI name; alpha-num & hyphens only):",
        validate=PipelineNameValidator,
    ).ask()
    # questionary returns None when the user cancels (Ctrl-C), bail out.
    if name is None:
        sys.exit("Setup Wizard Aborted! Please try again.")
    name = name.strip()

    # 2) Documentation placeholders: Asked in TEMPLATE_FIELDS order, each shown
    #    with an example so the user understands the expected shape of an answer.
    template_values = {}
    for fld in TEMPLATE_FIELDS:
        ans = questionary.text(
            f"{fld['message']}  (e.g. {fld['example']})",
            validate=NonEmptyValidator,
        ).ask()
        if ans is None:
            sys.exit("Setup Wizard Aborted! Please try again.")
        template_values[fld["key"]] = ans.strip()

    # 3) Primary input file type: Restricted to the known, supported set.
    input_type = questionary.select(
        "Primary input file type:",
        choices=list(SUPPORTED_INPUT_FILETYPES.keys()),
        default="illumina_fastq",
    ).ask()
    if input_type is None:
        sys.exit("Setup Wizard Aborted! Please try again.")

    # pipeline_name is itself a placeholder in some docs, so make it available
    # to the {{ pipeline_name }} substitution alongside the doc fields.
    template_values["pipeline_name"] = name

    return WizardAnswers(
        pipeline_name=name,
        input_type=input_type,
        template_values=template_values,
    )


# File discovery
def iter_repo_files(repo: Path):
    """Yields every text file under `repo`, honoring the exclusion rules.
    Walking once and filtering here keeps the per-file transformation loop clean,
    it never has to reason about what to skip. Excluded directories are pruned
    in-place so os.walk never descends into them (cheaper than filtering later).
    @param repo <Path>:
        Absolute path to the repo root to scan
    @return <Iterator[Path]>:
        Yields Path objects for each candidate text file
    """
    for root, dirs, files in os.walk(repo):
        # Prune excluded directories in-place so os.walk skips them entirely.
        dirs[:] = [d for d in dirs if d not in EXCLUDE_PATH_PARTS]
        for fname in files:
            path = Path(root) / fname
            rel = path.relative_to(repo)
            # Skip files excluded by exact relative path (e.g. release-please).
            if rel in EXCLUDE_RELATIVE_FILES:
                continue
            # Skip if any path component is on the exclude list (defensive: a
            # pruned dir cannot appear here, but a file could sit under one that
            # slipped through, so we re-check the full relative path.)
            rel_parts = set(rel.parts)
            if rel_parts & EXCLUDE_PATH_PARTS:
                continue
            # Skip files whose own name matches an excluded token (e.g. CHANGELOG.md).
            if path.name in EXCLUDE_PATH_PARTS:
                continue
            # Skip known-binary extensions to avoid corrupting them.
            if path.suffix.lower() in BINARY_EXTS:
                continue
            yield path


def read_text(path: Path) -> str | None:
    """Reads a file as UTF-8 text, returning None if it isn't text.
    Some files pass the extension filter but are still binary or unreadable;
    returning None (instead of raising) lets the caller simply skip them.
    @param path <Path>:
        The file to read
    @return <str | None>:
        The file contents, or None if the file is binary/unreadable
    """
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None  # binary or unreadable -> skip


# Template Transformations
def replace_baseline(text: str, new_name: str) -> str:
    """Replaces the literal string `baseline` with the new pipeline name.
    Uses a plain substitution to mirror the original `sed s@baseline@name@g`
    behaviour exactly (no word boundaries), so the result matches what the old
    manual setup produced.
    @param text <str>:
        The file contents to transform
    @param new_name <str>:
        The user's chosen pipeline name
    @return <str>:
        The text with every `baseline` occurrence replaced
    """
    return text.replace("baseline", new_name)


def replace_fastq_references(text: str, answers: WizardAnswers) -> str:
    """Rewrites FastQ references to the chosen non-fastq datatype.

    Two-pass, matching the described approach:
      1. Case-sensitive replace of `.fastq.gz` (and other fastq exts) with the
         datatype's primary extension.
      2. Case-insensitive replace of the word `fastq`/`fq` with the upper-case
         datatype label (BAM, CRAM, VCF, ...).

    Extensions are handled before the standalone word so that the ".fastq" in
    a path isn't turned into a bare label mid-extension. Word boundaries on the
    second pass stop us from mangling substrings inside unrelated identifiers.
    @param text <str>:
        The file contents to transform
    @param answers <WizardAnswers>:
        Supplies the target extension and label for the chosen datatype
    @return <str>:
        The text with FastQ extensions and words rewritten
    """
    ext = answers.primary_ext
    label = answers.filetype_label

    # Pass 1: extensions first (longest first so .fastq.gz beats .fastq,
    # otherwise the shorter match would leave a dangling ".gz").
    for fastq_ext in (".fastq.gz", ".fq.gz", ".fastq", ".fq"):
        text = text.replace(fastq_ext, ext)

    # Pass 2: the standalone word, case-insensitive, on word boundaries so we
    # don't clobber substrings inside unrelated identifiers.
    text = re.sub(r"(?i)\bfastq\b", label, text)
    text = re.sub(r"(?i)\bfq\b", label, text)

    return text


def apply_template_placeholders(text: str, values: dict) -> str:
    """Replaces {{ placeholder }} tokens with the collected values.
    Whitespace inside the braces is tolerated. Unknown keys are deliberately
    left untouched (the original token is returned) so a typo in the template
    surfaces as a visible {{ token }} rather than a silent blank.
    @param text <str>:
        The file contents to transform
    @param values <dict>:
        Placeholder key -> replacement text
    @return <str>:
        The text with recognized placeholders substituted
    """
    def _sub(match: re.Match) -> str:
        # Strip whitespace so '{{ pipeline_name }}' and '{{pipeline_name}}'
        # resolve to the same key.
        key = match.group(1).strip()
        # Fall back to the original matched token when the key is unknown.
        return values.get(key, match.group(0))

    return re.sub(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}", _sub, text)


def update_config_json(repo: Path, answers: WizardAnswers, dry_run: bool) -> None:
    """Sets options.input_type in config/config.json to the chosen type.
    Editing the JSON via the parser (rather than a text replace) preserves it as
    valid JSON and lets us short-circuit when the value is already correct.
    @param repo <Path>:
        The repo root containing config/config.json
    @param answers <WizardAnswers>:
        Supplies the input_type to write
    @param dry_run <bool>:
        When True, report the intended change without writing
    @return <None>:
        Writes the file (or prints intent) as a side effect
    """
    cfg_path = repo / "config" / "config.json"
    # A missing config is a warning, not a fatal error: the wizard can still do
    # the rest of its job on an unusual layout.
    if not cfg_path.exists():
        print(f"  [warn] {cfg_path} not found; skipping config update.")
        return

    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    # Ensure the 'options' object exists before assigning into it.
    data.setdefault("options", {})
    old = data["options"].get("input_type")
    data["options"]["input_type"] = answers.input_type

    # Nothing to do (and nothing to report as a change) if it already matches.
    if old == answers.input_type:
        print(f"  config/config.json input_type already '{old}'.")
        return

    # Re-serialize with a trailing newline to keep the file POSIX-friendly and
    # diff-clean.
    rendered = json.dumps(data, indent=4) + "\n"
    if dry_run:
        print(f"  [dry-run] would set config/config.json input_type "
              f"'{old}' -> '{answers.input_type}'")
    else:
        cfg_path.write_text(rendered, encoding="utf-8")
        print(f"  config/config.json input_type: '{old}' -> "
              f"'{answers.input_type}'")


def swap_test_files(repo: Path, answers: WizardAnswers, dry_run: bool) -> None:
    """Replaces .tests fastq files with files matching the chosen datatype.

    CI runs a dry-run of the pipeline against .tests, so the files must match
    the declared input_type's extension/glob or the dry-run will find no input.

    Runs for every type except illumina_fastq (gated by needs_file_swap):
      - Non-fastq types (bam/cram/vcf/...) get single files with the new ext.
      - ONT is single-end fastq, so the paired R1/R2 files are deleted and
        replaced with single WT_S{n}.fastq.gz files.

    Only fastq files are deleted; any other test files in .tests are left
    alone. Empty (touch-ed) files are sufficient because CI only performs a
    dry-run and checks that inputs are discovered, not read.
    @param repo <Path>:
        The repo root containing the .tests directory
    @param answers <WizardAnswers>:
        Supplies the target extension and pairing for new files
    @param dry_run <bool>:
        When True, report intended deletions/creations without touching disk
    @return <None>:
        Mutates the .tests directory (or prints intent) as a side effect
    """
    tests_dir = repo / ".tests"
    if not tests_dir.is_dir():
        print(f"  [warn] {tests_dir} not found; skipping file swap.")
        return

    # 1. Delete existing fastq files (only the fastq ones, nothing else).
    #    A set comprehension dedupes across overlapping globs, and sorting keeps
    #    the printed output deterministic.
    fastq_globs = ("*.fastq.gz", "*.fq.gz", "*.fastq", "*.fq")
    to_delete = sorted({p for g in fastq_globs for p in tests_dir.glob(g)})
    for p in to_delete:
        if dry_run:
            print(f"  [dry-run] would delete file .tests/{p.name}")
        else:
            p.unlink()
            print(f"  deleted file .tests/{p.name}")

    # 2. Create placeholder files with the correct extension(s).
    #    For dry-run CI, empty files are enough to satisfy the input glob.
    ext = answers.primary_ext
    created = []
    for i in range(1, TEST_SAMPLE_COUNT + 1):
        sample = f"WT_S{i}"
        if answers.is_paired:
            # For paired fastq the R1/R2 marker already lives inside the
            # extension, so we derive the two members by swapping R1<->R2.
            if ".R1." in ext or ".R2." in ext:
                r1 = tests_dir / f"{sample}{ext.replace('.R2.', '.R1.')}"
                r2 = tests_dir / f"{sample}{ext.replace('.R1.', '.R2.')}"
            else:
                # Fallback for a hypothetical paired non-fastq type: append
                # explicit _R1/_R2 before the extension. (None exist today.)
                r1 = tests_dir / f"{sample}_R1{ext}"
                r2 = tests_dir / f"{sample}_R2{ext}"
            created.extend([r1, r2])
        else:
            # Single-end / single-file datatypes get one file per sample.
            created.append(tests_dir / f"{sample}{ext}")

    for p in created:
        if dry_run:
            print(f"  [dry-run] would create file .tests/{p.name}")
        else:
            p.touch()
            print(f"  created file .tests/{p.name}")


def strip_setup_comment(repo: Path, dry_run: bool) -> None:
    """Removes the manual-setup HTML comment block from the README.

    The template README carries a <!--- ... --> comment documenting the old
    manual find/sed/mv setup procedure. The wizard supersedes it, so we delete
    it. It must run BEFORE the string replace rewrites 'baseline' -> new name so
    the marker text is still intact when matched.

    The pattern matches any multi-line HTML comment (single-line comments like
    <!-- TODO --> are intentionally preserved) so unrelated one-liners survive.
    @param repo <Path>:
        The repo root containing README.md
    @param dry_run <bool>:
        When True, report how many comments would be removed without writing
    @return <None>:
        Rewrites README.md (or prints intent) as a side effect
    """
    readme = repo / "README.md"
    if not readme.exists():
        print("  [warn] README.md not found; skipping setup-comment removal.")
        return

    text = readme.read_text(encoding="utf-8")

    # Match any multi-line HTML comment: <!-- ... --> or <!--- ... --> whose
    # body spans more than one line. DOTALL lets .*? cross newlines; the
    # explicit \n inside the body requires at least one line break so that
    # single-line comments (e.g. <!-- TODO -->) are left untouched. The
    # trailing \s* also swallows the blank line the comment leaves behind.
    pattern = re.compile(
        r"<!--+.*?\n.*?--+>\s*",
        re.DOTALL,
    )
    new_text, n = pattern.subn("", text)

    # subn reports how many substitutions happened; zero means nothing matched.
    if n == 0:
        print("  no multi-line HTML comments found in README.md.")
        return

    if dry_run:
        print(f"  [dry-run] would remove {n} multi-line HTML comment(s) from README.md")
    else:
        readme.write_text(new_text, encoding="utf-8")
        print(f"  removed {n} multi-line HTML comment(s) from README.md")


def rename_executable(repo: Path, new_name: str, dry_run: bool) -> None:
    """Renames the main entry point `baseline` -> <new_name>, preserving +x.
    The rename mirrors the old `mv baseline <name>` step. We explicitly re-apply
    the execute bits after the move so the new CLI stays runnable regardless of
    how the filesystem preserved the original mode.
    @param repo <Path>:
        The repo root containing the `baseline` executable
    @param new_name <str>:
        The user's chosen pipeline name (the new filename)
    @param dry_run <bool>:
        When True, report the intended rename without touching disk
    @return <None>:
        Renames the file (or prints intent) as a side effect
    """
    src = repo / "baseline"
    dst = repo / new_name
    # Missing executable is a warning rather than fatal so the wizard can still
    # finish the textual work on an atypical checkout.
    if not src.exists():
        print(f"  [warn] executable '{src}' not found; skipping rename.")
        return
    if dry_run:
        print(f"  [dry-run] would rename executable 'baseline' -> '{new_name}'")
        return
    # Capture the current mode, rename, then guarantee the exec bits survive.
    mode = src.stat().st_mode
    src.rename(dst)
    dst.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"  renamed executable 'baseline' -> '{new_name}'")


# Orchestration
def process_repo(repo: Path, answers: WizardAnswers, dry_run: bool) -> None:
    """Applies every transformation to the repo in the correct order.
    Ordering matters: the README setup comment is stripped first (while it still
    contains the literal 'baseline' marker), then the per-file text rewrites run,
    then config/files/executable are updated. Grouping the sequence here gives
    one place to reason about dependencies between steps.
    @param repo <Path>:
        The repo root to transform
    @param answers <WizardAnswers>:
        The user's collected choices
    @param dry_run <bool>:
        When True, every step reports intent without writing
    @return <None>:
        Performs the setup as a series of side effects
    """
    # FastQ rewriting only applies when moving AWAY from a fastq datatype. For
    # fastq targets the template text is already correct.
    do_fastq = not answers.is_fastq  # only rewrite when leaving fastq

    # Strip the manual-setup README comment FIRST, while it still contains the
    # literal 'baseline' marker (the file loop below rewrites that string).
    print("• Removing manual setup instructions")
    strip_setup_comment(repo, dry_run)

    # Main text pass: rename baseline, fill placeholders, and (if needed) rewrite
    # fastq references across every eligible text file.
    print("\n• Rewriting files")
    changed = 0
    scanned = 0
    for path in iter_repo_files(repo):
        original = read_text(path)
        if original is None:
            continue  # binary/unreadable — skip silently
        scanned += 1

        # Apply the transformations in sequence on the in-memory copy so a file
        # is written at most once, only if something actually changed.
        text = original
        text = replace_baseline(text, answers.pipeline_name)
        text = apply_template_placeholders(text, answers.template_values)
        if do_fastq:
            text = replace_fastq_references(text, answers)

        # Only write files whose content actually changed, to keep the diff and
        # the printed log limited to genuinely affected files.
        if text != original:
            changed += 1
            rel = path.relative_to(repo)
            if dry_run:
                print(f"  [dry-run] would modify {rel}")
            else:
                path.write_text(text, encoding="utf-8")
                print(f"  modified {rel}")

    print(f"\n  scanned {scanned} text files, "
          f"{'would modify' if dry_run else 'modified'} {changed}.")

    # Point config/config.json at the chosen datatype.
    print("\n• Updating config")
    update_config_json(repo, answers, dry_run)

    # Regenerate CI files only when the chosen type no longer matches the
    # template's native illumina_fastq layout.
    if answers.needs_file_swap:
        print("\n• Swapping .tests ci files")
        swap_test_files(repo, answers, dry_run)

    # Finally rename the CLI entry point to the new pipeline name.
    print("\n• Renaming pipleine executable")
    rename_executable(repo, answers.pipeline_name, dry_run)


def summarize(answers: WizardAnswers) -> None:
    """Prints a human-readable recap of the collected answers.
    Shown before the confirmation prompt so the user can catch mistakes before
    anything is written. pipeline_name is skipped in the loop because it is
    already displayed on its own line above.
    @param answers <WizardAnswers>:
        The choices to summarize
    @return <None>:
        Prints to stdout
    """
    ft = SUPPORTED_INPUT_FILETYPES[answers.input_type]
    print("\n--- Summary -----------")
    print(f"  Pipeline / CLI name : {answers.pipeline_name}")
    print(f"  Input type          : {answers.input_type} "
          f"(fastq={ft['fastq']}, ext={answers.primary_ext})")
    for k, v in answers.template_values.items():
        # pipeline_name is a template value too, but it's already shown above.
        if k == "pipeline_name":
            continue
        print(f"  {k:20}: {v}")
    print("-----------------------\n")


def main() -> None:
    """CLI entry point: parses args, gathers answers, and runs the wizard.
    Also performs the up-front sanity checks (repo exists, looks like a baseline
    checkout) and the optional interactive confirmation before applying changes.
    @return <None>:
        Drives the whole program; exits via sys.exit on error/abort
    """
    parser = argparse.ArgumentParser(
        description="Interactive setup wizard for the OpenOmics/baseline template."
    )
    parser.add_argument(
        "--repo", type=Path, default=Path.cwd(),
        help="Path to the cloned baseline repo (default: current directory).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would change without writing anything.",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Skip the final confirmation prompt.",
    )
    args = parser.parse_args()

    # Resolve to an absolute path so all downstream relative_to() calls are safe.
    repo = args.repo.resolve()
    if not repo.is_dir():
        sys.exit(f"Repo path not found or not a directory: {repo}")
    # A missing 'baseline' executable usually means the wrong directory was
    # passed; warn but continue, since the user may have an unusual layout.
    if not (repo / "baseline").exists():
        print(f"[warn] no 'baseline' executable found at {repo} — "
              f"is this the right repo root?")

    # Gather all input, then echo it back for review.
    answers = collect_answers()
    summarize(answers)

    # Require explicit confirmation for a real (non-dry-run) apply unless the
    # user passed --yes. Dry runs never prompt because they change nothing.
    if not args.yes and not args.dry_run:
        proceed = questionary.confirm(
            f"Apply these changes in-place to {repo}?", default=False
        ).ask()
        if not proceed:
            sys.exit("Aborted — no changes made.")

    process_repo(repo, answers, args.dry_run)

    # Closing guidance differs between preview and real runs.
    if args.dry_run:
        print("\nDry run complete. Re-run without --dry-run to apply.")
    else:
        print(f"\nDone. Your pipeline '{answers.pipeline_name}' is ready.")
        print(f"Try:  {repo / answers.pipeline_name} --help")


if __name__ == "__main__":
    main()
