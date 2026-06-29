#!/usr/bin/env python3
# -*- coding: UTF-8 -*-

# Python standard library
import json
import os
import re
import subprocess
import sys
from shutil import copytree
# Local imports
from utils import (
    Colors,
    err,
    exists,
    fatal,
    git_commit_hash,
    join_jsons,
    which,
)
from . import version as __version__


# Constants
# Handling different input filetypes, i.e illumina
# fastq files, bam, cram, vcf, tsv, csv, ont fastqs,
# ont fast5, ont pod5, etc. The input_type is defined
# in config/config.json under options.input_type, so
# the type is never inferred from filenames.
SUPPORTED_INPUT_FILETYPES = {
    "illumina_fastq": {"fastq": True, "is_dir": False, "exts": [".R1.fastq.gz", ".R2.fastq.gz"]},
    "ont_fastq": {"fastq": True,  "is_dir": False, "exts": [".fastq.gz", ".fq.gz", ".fastq", ".fq"]},
    "ont_fast5": {"fastq": False, "is_dir": True, "exts": [".fast5"]},
    "ont_pod5":  {"fastq": False, "is_dir": True, "exts": [".pod5"]},
    "bam":  {"fastq": False, "is_dir": False, "exts": [".bam"]},
    "cram": {"fastq": False, "is_dir": False, "exts": [".cram"]},
    "vcf":  {"fastq": False, "is_dir": False, "exts": [".vcf.gz", ".vcf"]},
    "tsv":  {"fastq": False, "is_dir": False, "exts": [".tsv.gz", ".tsv"]},
    "csv":  {"fastq": False, "is_dir": False, "exts": [".csv.gz", ".csv"]},
}

# Supported read layouts for FastQ input types.
#  • paired: require both mates (R1/R2) per sample
#  • single: one file per sample, error if R2 exists
#  • auto: inferred from R2 presence
READ_LAYOUTS = ("paired", "single", "auto")

# Final renamed illumina mate file extensions,
# ensures downstream snakemake rules can rely
# on a consistent naming scheme for FastQ files,
# i.e {sample}.R1.fastq.gz and {sample}.R2.fastq.gz
ILLUMINA_R1 = ".R1.fastq.gz"
ILLUMINA_R2 = ".R2.fastq.gz"
ILLUMINA_MATE_RE = re.compile(r"\.R[12]\.fastq\.gz")

# Illumina regex patterns to rename user-provided
# FastQ names to final .R1/.R2.fastq.gz filename
ILLUMINA_FASTQ_RENAME = {
    r".R1.f(ast)?q.gz$": ILLUMINA_R1,
    r".R2.f(ast)?q.gz$": ILLUMINA_R2,
    # Lane-containing names, e.g. .R1.001.fastq.gz (lane captured as a group)
    r".R1.(?P<lane>...).f(ast)?q.gz$": ILLUMINA_R1,
    r".R2.(?P<lane>...).f(ast)?q.gz$": ILLUMINA_R2,
    # _1/_2 style, e.g. sample_1.fastq.gz
    r"_1\.f(ast)?q\.gz$": ILLUMINA_R1,
    r"_2\.f(ast)?q\.gz$": ILLUMINA_R2,
}

# Endedness signal written to config['project']['nends'].
NENDS_SINGLE = 1   # single-end / single-file-per-sample
NENDS_PAIRED = 2   # paired-end Illumina FastQ
NENDS_OTHER = -1   # non-fastq (bam, cram, vcf, tables, ont signal dirs)
NENDS_LABELS = {NENDS_SINGLE: "single-end", NENDS_PAIRED: "paired-end", NENDS_OTHER: "other"}

# Colorized output for user-facing messages
COLORS = Colors()


# Custom formatter for error messages
def _fatal_message(body):
    """Formats a user-facing fatal message with a consistent prefix/indent.
    Kept separate so the call sites read as logic rather than embedded prose.
    @param body <str>:
        Message text (may be multi-line)
    @return <str>:
        Formatted message suitable for an exception
    """
    indented = "\n\t".join(line.strip() for line in body.strip().splitlines())
    return f"\n\tFatal: {indented}\n"

# Build config and kick off the pipeline
def build_config(sub_args, pl_home):
    """Initialize the working directory and build the pipeline config.
    Copies required resources into the output directory, dynamically creates
    the config from user inputs and base templates, and resolves the
    docker/singularity bind paths.
    @param sub_args <parser.parse_args() object>:
        Parsed arguments for run sub-command
    @param pl_home <str>:
        Path to pipeline github repo
    @return config <dict>:
        Fully-resolved pipeline configuration, including bind paths
    """
    git_repo = pl_home
    input_type, layout = read_global_options(git_repo)

    input_files = init(
        repo_path=git_repo,
        output_path=sub_args.output,
        input_type=input_type,
        links=sub_args.input,
    )

    config = setup(
        sub_args,
        ifiles=input_files,
        repo_path=git_repo,
        output_path=sub_args.output,
        input_type=input_type,
        layout=layout,
    )

    config['bindpaths'] = bind(sub_args, config=config)
    return config


def save_config(config, output_path):
    """Saves the pipeline config to the output directory as config.json.
    @param config <dict>:
        Fully-resolved pipeline configuration
    @param output_path <str>:
        Path to the pipeline's output directory
    """
    config_file = os.path.join(output_path, 'config.json')
    with open(config_file, 'w') as fh:
        json.dump(config, fh, indent=4, sort_keys=True)


def launch_pipeline(sub_args, bindpaths, pl_home, pl_name):
    """Orchestrates pipeline execution and waits for it to complete.
    Runs the pipeline locally on a compute node for debugging, or submits the
    master job to the SLURM scheduler. This call is blocking, not asynchronous.
    @param sub_args <parser.parse_args() object>:
        Parsed arguments for run sub-command
    @param bindpaths <list[str]>:
        Resolved docker/singularity bind paths
    @param pl_home <str>:
        Path to pipeline github repo
    @param pl_name <str>:
        Name of the pipeline
    @return mjob <subprocess.Popen object>:
        The completed master job process
    """
    logfiles_dir = os.path.join(sub_args.output, 'logfiles')
    if not exists(logfiles_dir):
        os.makedirs(logfiles_dir)

    log_name = 'snakemake.log' if sub_args.mode == 'local' else 'master.log'
    log = os.path.join(logfiles_dir, log_name)

    with open(log, 'w') as logfh:
        mjob = runner(
            mode=sub_args.mode,
            outdir=sub_args.output,
            alt_cache=sub_args.singularity_cache,
            threads=int(sub_args.threads),
            jobname=sub_args.job_name,
            submission_script=os.path.join(pl_home, 'src', 'run.sh'),
            logger=logfh,
            additional_bind_paths=",".join(bindpaths),
            tmp_dir=sub_args.tmp_dir,
        )

        if not sub_args.silent:
            print("\nRunning {0} pipeline in '{1}' mode...".format(pl_name, sub_args.mode))
        mjob.wait()

    return mjob


def report_outcome(sub_args, mjob, pl_name):
    """Relays the pipeline outcome to the user.
    Reports the exit code for local runs, or the submitted master job id
    for SLURM runs.
    @param sub_args <parser.parse_args() object>:
        Parsed arguments for run sub-command
    @param mjob <subprocess.Popen object>:
        The completed master job process
    @param pl_name <str>:
        Name of the pipeline
    """
    if sub_args.mode == 'local':
        if int(mjob.returncode) == 0:
            print('{0} pipeline has successfully completed'.format(pl_name))
        else:
            fatal('{0} pipeline failed. Please see {1} for more information.'.format(
                pl_name, os.path.join(sub_args.output, 'logfiles', 'snakemake.log'))
            )
    elif sub_args.mode == 'slurm':
        jobid_file = os.path.join(sub_args.output, 'logfiles', 'mjobid.log')
        with open(jobid_file) as fh:
            jobid = fh.read().strip()

        if not sub_args.silent:
            if int(mjob.returncode) == 0:
                print('Successfully submitted master job: ', end="")
            else:
                fatal('Error occurred when submitting the master job.')
        print(jobid)


# Get input data-type and read layout
def read_global_options(repo_path):
    """Reads options.input_type and options.layout from the source
    config/config.json before the output directory is initialized. This must
    happen early because init()/sym_safe() need input_type to rename and
    validate inputs before the output config exists.
    @param repo_path <str>:
        Path to the pipeline installation (source) directory
    @return (input_type <str>, layout <str>):
        Validated input_type and layout (falls back to legacy defaults)
    """
    config_file = os.path.join(repo_path, "config", "config.json")
    input_type, layout = "illumina_fastq", "auto"  # backward-compatible defaults
    try:
        with open(config_file, "r") as fh:
            options = json.load(fh).get("options", {}) or {}
        input_type = options.get("input_type", input_type)
        layout = options.get("layout", layout)
    except (OSError, ValueError):
        # Missing or invalid config, fall back to
        # defaults for backward compatibility
        err(
            f"Warning: Could not read {config_file}, falling back to defaults:\n"
            f"options.input_type = '{input_type}', options.layout = '{layout}'"
        )
    return validate_input_type(input_type), validate_layout(layout)


def validate_input_type(input_type):
    """Ensures the declared input_type is supported.
    @param input_type <str>:
        Declared input type (e.g. illumina_fastq, bam, vcf)
    @return input_type <str>
    """
    if input_type not in SUPPORTED_INPUT_FILETYPES:
        raise ValueError(_fatal_message(
            f"Unsupported options.input_type '{input_type}' in config.json.\n"
            f"Supported types: {', '.join(sorted(SUPPORTED_INPUT_FILETYPES))}"
        ))
    return input_type


def validate_layout(layout):
    """Ensures the declared layout is supported.
    @param layout <str>:
        Declared layout (paired, single, or auto)
    @return layout <str>
    """
    if layout not in READ_LAYOUTS:
        raise ValueError(_fatal_message(
            f"Unsupported options.layout '{layout}' in config.json.\n"
            f"Supported layouts: {', '.join(READ_LAYOUTS)}"
        ))
    return layout


def extensions_for(input_type):
    """Returns the list of valid extensions for a declared input type."""
    return SUPPORTED_INPUT_FILETYPES[input_type]["exts"]


def matches_type(filename, input_type):
    """Returns True if filename has a valid extension for the declared input_type.
    @param filename <str>:
        File name or path to check
    @param input_type <str>:
        Declared input type from config
    @return <bool>
    """
    name = os.path.basename(filename).lower()
    return any(name.endswith(ext.lower()) for ext in extensions_for(input_type))


def strip_ext(filename, input_type):
    """Returns the sample basename for a file of the declared input_type.
    Illumina mates (R1/R2) collapse to a shared sample name; for other types the
    longest matching known extension is stripped.
    @param filename <str>:
        File name or path
    @param input_type <str>:
        Declared input type from config
    @return <str>:
        Sample basename
    """
    name = os.path.basename(filename)
    if input_type == "illumina_fastq":
        return ILLUMINA_MATE_RE.split(name)[0]
    for ext in sorted(extensions_for(input_type), key=len, reverse=True):
        if name.lower().endswith(ext.lower()):
            return name[: -len(ext)]
    return name


def rename(filename, input_type):
    """Normalizes Illumina FastQ names to canonical .R1/.R2.fastq.gz form. For
    all other declared types, validates the extension and passes the name
    through unchanged. Raises if a file does not match the declared input_type.
    @param filename <str>:
        Original name of file to be renamed
    @param input_type <str>:
        Declared input type from config
    @return <str>:
        Renamed (Illumina) or unchanged (other) filename
    """
    if input_type == "illumina_fastq":
        if filename.endswith(ILLUMINA_R1) or filename.endswith(ILLUMINA_R2):
            # FastQ files already named correctly, i.e:
            # {sample}.R1.fastq.gz and {sample}.R2.fastq.gz
            # Nothing to do, just return the filename as-is.
            return filename
        for regex, canonical_ext in ILLUMINA_FASTQ_RENAME.items():
            if re.search(regex, filename):
                return re.sub(regex, canonical_ext, filename)
        # User provided FastQ name does not match any known
        # Illumina FastQ file pattern/naming, raise an error.
        raise NameError(_fatal_message(
            f"Could not normalize Illumina FastQ name '{filename}'!\n"
            "Cannot determine the extension of the user provided input file.\n"
            "Here are examples of acceptable input file extensions:\n"
            "sampleName.R1.fastq.gz      sampleName.R2.fastq.gz\n"
            "sampleName_R1_001.fastq.gz  sampleName_R2_001.fastq.gz\n"
            "sampleName_1.fastq.gz       sampleName_2.fastq.gz\n"
            "Please also check that your input files are gzipped."
        ))

    if not matches_type(filename, input_type):
        # User provided file does not match the declared
        # input_type file naming convention, raise an error.
        raise NameError(_fatal_message(
            f"Input '{filename}' does not match declared input_type '{input_type}'!\n"
            f"Expected one of the following extensions: {', '.join(extensions_for(input_type))}\n"
            "Please correct options.input_type in config.json or remove this file."
        ))
    return filename


def validate_inputs(ifiles, input_type):
    """Ensures every input file matches the config-declared input_type. With a
    declared type, any non-matching file is an error.
    @param ifiles list[<str>]:
        Pipeline input files (renamed symlinks)
    @param input_type <str>:
        Declared input type from config
    """
    bad = [f for f in ifiles if not matches_type(f, input_type)]
    if bad:
        raise TypeError(_fatal_message(
            f"The following inputs do not match the declared input_type "
            f"'{input_type}' (expected extensions: {', '.join(extensions_for(input_type))}):\n"
            + "\n".join(bad) + "\n"
            "Either correct options.input_type in config.json or remove the "
            "offending files. The pipeline supports only one input type per run. "
            "If you believe mixed-type support should exist, feel free to open an "
            "issue on Github."
        ))


def get_nends(ifiles, input_type, layout):
    """Resolves the ended-ness from the declared input_type and layout.
    For Illumina FastQ, layout controls behavior:
     • paired: require both mates (R1/R2) per sample
     • single: one file per sample, error if R2 exists
     • auto: inferred from R2 presence
    @param ifiles list[<str>]:
        Pipeline input files (renamed symlinks)
    @param input_type <str>:
        Declared input type from config
    @param layout <str>:
        Declared layout (paired, single, auto)
    @return <int>:  NENDS_SINGLE, NENDS_PAIRED, or NENDS_OTHER
    """
    if not SUPPORTED_INPUT_FILETYPES[input_type]["fastq"]:
        return NENDS_OTHER

    if input_type == "ont_fastq":
        return NENDS_SINGLE  # ONT reads are single-file per sample

    # Check Illumina FastQ for R2 to determine
    # whether the data is paired-end
    has_r2 = any(f.endswith(ILLUMINA_R2) for f in ifiles)

    if layout == "single":
        if has_r2:
            raise TypeError(_fatal_message(
                "layout='single' was declared, but R2 (mate) files were detected "
                "in the input. Either set options.layout to 'paired' or 'auto', "
                "or provide only R1 files."
            ))
        return NENDS_SINGLE

    if layout == "paired" or (layout == "auto" and has_r2):
        _verify_mates(ifiles)
        return NENDS_PAIRED

    return NENDS_SINGLE  # auto + no R2 -> single-end


def _verify_mates(ifiles):
    """Ensures both R1 and R2 are present for every Illumina paired-end sample.
    @param ifiles list[<str>]:
        Pipeline input files (renamed symlinks)
    """
    mate_counts = {}
    for file in ifiles:
        if file.endswith(ILLUMINA_R1) or file.endswith(ILLUMINA_R2):
            sample = ILLUMINA_MATE_RE.split(os.path.basename(file))[0]
            mate_counts[sample] = mate_counts.get(sample, 0) + 1

    samples_missing_a_mate = [s for s, count in mate_counts.items() if count == 1]
    if samples_missing_a_mate:
        raise NameError(_fatal_message(
            "Detected paired-end data but a mate (R1 or R2) is missing for the "
            f"following samples:\n{samples_missing_a_mate}\n"
            "Please check that the basename for each sample is consistent across "
            "mates. Here is an example of a consistent basename across mates:\n"
            "consistent_basename.R1.fastq.gz\n"
            "consistent_basename.R2.fastq.gz\n"
            "Please do not run the pipeline with a mixture of single-end and "
            "paired-end samples. If this is a priority for your project, please "
            "run paired-end and single-end samples separately (in two separate "
            "output directories). If you feel like this functionality should "
            "exist, feel free to open an issue on Github."
        ))


# Output directory initialization
def init(repo_path, output_path, input_type, links=None, required=None):
    """Initialize the output directory. If the user provides an output path that
    already exists as a file, an OSError is raised. An existing directory is not
    recreated.
    @param repo_path <str>:
        Installation source code and its templates
    @param output_path <str>:
        Pipeline output path, created if it does not exist
    @param input_type <str>:
        Declared input type; controls renaming/validation
    @param links list[<str>]:
        Files to symlink into output_path
    @param required list[<str>]:
        Folders to copy over into output_path
    @return list[<str>]:
        Renamed input symlinks
    """
    links = links or []
    required = required or ["workflow", "resources", "config"]

    if not exists(output_path):
        os.makedirs(output_path)
    elif exists(output_path) and os.path.isfile(output_path):
        raise OSError(_fatal_message(
            "Failed to create provided pipeline output directory!\n"
            "User provided --output PATH already exists on the filesystem as a file.\n"
            f"Please run {sys.argv[0]} again with a different --output PATH."
        ))
    # Copy required folders to run the pipeline from
    # the github repo into the output directory.
    copy_safe(source=repo_path, target=output_path, resources=required)
    return sym_safe(input_data=links, target=output_path, input_type=input_type)


def copy_safe(source, target, resources=None):
    """Recursively copies each resource into the target location. Existing
    target paths are NOT overwritten.
    @param source <str>:
        Prefix PATH for each resource
    @param target <str>:
        Target path for templates and resources
    @param resources list[<str>]:
        Paths to copy over to the target location
    """
    resources = resources or []
    for resource in resources:
        destination = os.path.join(target, resource)
        if not exists(destination):
            copytree(os.path.join(source, resource), destination)


def sym_safe(input_data, target, input_type):
    """Creates re-named symlinks for each input file based on the declared
    input_type. Illumina FastQs are normalized to canonical .R1/.R2 names. All
    other supported types are validated and linked as-is. Existing symlinks are
    not recreated; relative source paths are converted to absolute paths.
    @param input_data list[<str>]:
        Input files to symlink to target location
    @param target <str>:
        Target path for the renamed symlinks
    @param input_type <str>:
        Declared input type from config
    @return list[<str>]:
        Renamed input files
    """
    renamed_inputs = []
    for file in input_data:
        filename = os.path.basename(file)
        renamed = os.path.join(target, rename(filename, input_type))
        renamed_inputs.append(renamed)
        if not exists(renamed):
            # Follow source symlinks to resolve any binding issues
            os.symlink(os.path.abspath(os.path.realpath(file)), renamed)
    return renamed_inputs


# Create main pipeline config file, i.e config.json,
# from other pipeline config files and user-provided
# inputs and other cli options
def setup(sub_args, ifiles, repo_path, output_path, input_type, layout):
    """Sets up the pipeline for execution and creates the master config from
    templates.
    @param sub_args:
        Parsed arguments for the run sub-command
    @param ifiles list[<str>]:
        Pipeline input files (renamed symlinks)
    @param repo_path <str>:
        Installation source code and its templates
    @param output_path <str>:
        Pipeline output path
    @param input_type <str>:
        Declared input type from config
    @param layout <str>:
        Declared layout from config (paired, single, auto)
    @return config <dict>:
        Metadata to run the pipeline
    """
    # Check that all input files match their excepted input_type.
    # If any file does not match, a TypeError is raised and the
    # pipeline will not run.
    validate_inputs(ifiles, input_type)

    config_dir = os.path.join(output_path, "config")
    template_files = [
        os.path.join(config_dir, "config.json"),      # base configuration
        os.path.join(config_dir, "containers.json"),  # container image uris
        os.path.join(config_dir, "genome.json"),      # genomic reference files
        os.path.join(config_dir, "modules.json"),     # tool or module information
    ]
    # Join configs and add user, rawdata, docker
    # image information, pipeline metadata, and
    # all cli optons to the final config file.
    config = join_jsons(template_files)
    config["project"] = {}
    config = add_user_information(config)
    config = add_rawdata_information(sub_args, config, ifiles, input_type, layout)
    config = image_cache(sub_args, config, output_path)
    config["project"]["version"] = __version__
    config["project"]["workpath"] = os.path.abspath(sub_args.output)
    config["project"]["git_commit_hash"] = git_commit_hash(repo_path)
    config["project"]["pipeline_path"] = repo_path
    # Record all CLI options for data provenance
    for option, value in vars(sub_args).items():
        if option == "func":
            continue  # skip the sub-command handler
        if not isinstance(value, (list, dict)):
            value = str(value)
        config["options"][option] = value

    # Add input data type and layout
    config["options"]["input_type"] = input_type
    config["options"]["layout"] = layout

    return config


def add_user_information(config):
    """Adds the invoking user's home directory and username to config.
    @param config <dict>:
        Config dictionary to update
    @return config <dict>:
        Updated config dictionary with user home and username
    """
    home = os.path.expanduser("~")
    config["project"]["userhome"] = home
    config["project"]["username"] = os.path.split(home)[-1]
    return config


def add_sample_metadata(input_files, config, input_type, group=None):
    """Adds each sample's basename to config. Basenames are extracted using the
    declared input_type (paired Illumina mates collapse to a single entry).
    @param input_files list[<str>]:
        Pipeline input files
    @param config <dict>:
        Config dictionary to update
    @param input_type <str>:
        Declared input type from config
    @param group <str>:
        Sample sheet (not yet implemented)
    @return config <dict>:
        Updated config with sample basenames
    """
    # TODO: support a user-provided sample sheet for basename/group/label.
    samples = []
    for file in input_files:
        sample = strip_ext(file, input_type)
        if sample not in samples:
            samples.append(sample)
    config["samples"] = samples
    return config


def add_rawdata_information(sub_args, config, ifiles, input_type, layout):
    """Adds rawdata metadata: endedness signal, declared type/layout, the set of
    rawdata directories to bind, and per-sample basenames.
    @param sub_args:
        Parsed arguments for the run sub-command
    @param config <dict>:
        Config dictionary to update
    @param ifiles list[<str>]:
        Pipeline input files (renamed symlinks)
    @param input_type <str>:
        Declared input type from config
    @param layout <str>:
        Declared layout from config (paired, single, auto)
    @return config <dict>:
        Updated config dictionary
    """
    nends = get_nends(ifiles, input_type, layout)
    config["project"]["nends"] = nends
    config["project"]["filetype"] = NENDS_LABELS[nends]

    # Explicit signals for downstream Snakemake rules to branch on.
    config["project"]["input_type"] = input_type
    config["project"]["layout"] = layout

    rawdata_paths = get_rawdata_bind_paths(input_files=sub_args.input)
    config["project"]["datapath"] = ",".join(rawdata_paths)

    config = add_sample_metadata(input_files=ifiles, config=config,
                                 input_type=input_type)
    return config


def image_cache(sub_args, config, repo_path):
    """Adds Docker image URIs, or local SIF paths to config when a singularity
    cache is provided. If a cache is provided but a local SIF does not exist, a
    warning is printed and the image will be pulled from the URI in
    config/containers.json.
    @param sub_args:
        Parsed arguments for the run sub-command
    @param config <dict>:
        Config dictionary to update
    @param repo_path <str>:
        Installation source code and its templates
    @return config <dict>:
        Updated config dictionary
    """
    images_file = os.path.join(repo_path, "config", "containers.json")
    with open(images_file, "r") as fh:
        data = json.load(fh)

    for image, uri in data["images"].items():
        if sub_args.sif_cache:
            sif_name = os.path.basename(uri).replace(":", "_")
            sif = os.path.join(sub_args.sif_cache, f"{sif_name}.sif")
            if not exists(sif):
                err(f'Warning: Local image "{sif}" does not exist in singularity cache. Image will be pulled from URI: {uri}')
            else:
                data["images"][image] = sif  # point at the local SIF

    config.update(data)
    return config


# Bind-path resolution
def unpacked(nested_dict):
    """Recursively yields every non-dict value in a nested dictionary.
    @param nested_dict <dict>:  dictionary to unpack
    @yields each leaf value
    """
    for value in nested_dict.values():
        if isinstance(value, dict):
            yield from unpacked(value)
        else:
            yield value


def get_fastq_screen_paths(fastq_screen_confs, match="DATABASE", file_index=-1):
    """Parses fastq_screen.conf files for each fastq_screen database path. These
    paths contain bowtie2 indices for the reference genome to screen against and
    are added as singularity bind points.
    @param fastq_screen_confs list[<str>]:
        Config files to parse
    @param match <str>:
        Keyword indicating a matching line [default: DATABASE]
    @param file_index <int>:
        Index of the token holding the database path
    @return list[<str>]:
        Fastq Screen database paths
    """
    databases = []
    for file in fastq_screen_confs:
        with open(file, "r") as fh:
            for line in fh:
                if line.startswith(match):
                    databases.append(line.strip().split()[file_index])
    return databases


def resolve_additional_bind_paths(search_paths):
    """Finds additional singularity bind paths from a list of paths. Paths are
    indexed by a composite key (the first two directories of an absolute path) to
    avoid collisions on the shared /gpfs filesystem; for each index a common path
    is found. Assumes absolute paths (the build sub-command writes absolute
    reference filenames).
    @param search_paths list[<str>]:
        Absolute file paths
    @return list[<str>]:
        Common shared bind paths
    """
    paths_by_index = {}
    for ref in search_paths:
        lowered = ref.lower()
        # Skip remote URIs and any non-absolute
        # strings or file-like paths.
        if lowered.startswith(("sftp://", "s3://", "gs://")) or not lowered.startswith(os.sep):
            continue

        path_tokens = os.path.abspath(ref).split(os.sep)
        try:
            # Create a composite index from the
            # first two directories avoids treating
            # unrelated roots (e.g. /scratch vs /data)
            # as sharing a common path.
            index = tuple(path_tokens[1:3])
        except IndexError:
            index = path_tokens[1]  # ref is directly under /
        paths_by_index.setdefault(index, []).append(os.sep.join(path_tokens))

    common_paths = []
    for paths in paths_by_index.values():
        common = os.path.dirname(os.path.commonprefix(paths))
        if common == os.sep:
            # Avoid binding / when given /tmp or
            # /scratch as input.
            common = os.path.commonprefix(paths)
        common_paths.append(common)

    return list(set(common_paths))


def bind(sub_args, config):
    """Resolves bind paths for singularity/docker images from the config.
    @param sub_args:
        Parsed arguments for the run sub-command
    @param config <dict>:
        Config dictionary generated by setup()
    @return list[<str>]:
        Singularity/docker bind paths
    """
    bindpaths = []
    for value in unpacked(config):
        if not isinstance(value, str) or not exists(value):
            continue
        path = os.path.dirname(value) if os.path.isfile(value) else value
        if path not in bindpaths:
            bindpaths.append(path)

    rawdata_bind_paths = [os.path.realpath(p) for p in config["project"]["datapath"].split(",")]
    working_directory = os.path.realpath(config["project"]["workpath"])
    genome_bind_paths = resolve_additional_bind_paths(bindpaths)

    all_bind_paths = [working_directory] + rawdata_bind_paths + genome_bind_paths
    return list(set(p for p in all_bind_paths if p != os.sep))


def get_rawdata_bind_paths(input_files):
    """Returns the set of directories containing the user-provided input files.
    @param input_files list[<str>]:
        User-provided input files
    @return list[<str>]:
        Rawdata bind paths
    """
    bindpaths = []
    for file in input_files:
        source_dir = os.path.dirname(os.path.abspath(os.path.realpath(file)))
        if source_dir not in bindpaths:
            bindpaths.append(source_dir)
    return bindpaths


# Execution
def dryrun(outdir, config="config.json", snakefile=os.path.join("workflow", "Snakefile")):
    """Dry-runs the pipeline to surface errors before a real run.
    @param outdir <str>:
        Pipeline output PATH
    @return <bytes>:
        Byte-string output of the dryrun command
    """
    try:
        # Use a high core count so the dryrun reports
        # the true cores per rule, snakemake uses
        # min(--cores CORES, N)).
        return subprocess.check_output([
            "snakemake", "-npr",
            "-s", str(snakefile),
            "--use-singularity",
            "--rerun-incomplete",
            "--cores", str(256),
            f"--configfile={config}",
        ], cwd=outdir, stderr=subprocess.STDOUT)
    except OSError as e:
        # OSError [Errno 2] occurs when the snakemake
        # command is not found.
        if e.errno == 2 and not which("snakemake"):
            err("\n{COLORS.red}Error: Are snakemake AND singularity in your $PATH?{COLORS.end}")
            fatal("{COLORS.red}Please check before proceeding again!{COLORS.end}")
        else:
            raise
    except subprocess.CalledProcessError as e:
        print(e, e.output.decode("utf-8"))
        raise


def runner(mode, outdir, alt_cache, logger, additional_bind_paths="",
           threads=2, jobname="pl:master", submission_script="run.sh",
           tmp_dir="/lscratch/$SLURM_JOBID/"):
    """Runs the pipeline via the selected executor: local or slurm. 'local' runs
    serially on the current compute instance; 'slurm' submits jobs to the cluster
    via the SLURM scheduler. Support for other schedulers (PBS, SGE, LSF) may be
    added in the future.
    @param mode <str>:
        'local' or 'slurm'
    @param outdir <str>:
        Pipeline output PATH
    @param alt_cache <str>:
        Alternative singularity cache location
    @param logger <file-handle>:
        Open file handle for writing logs
    @param additional_bind_paths <str>:
        Comma-separated paths to bind (input paths)
    @param threads <int>:
        Threads for the local execution method
    @param jobname <str>:
        Name of the master job
    @param submission_script <str>:
        Path to the slurm submission script
    @param tmp_dir <str>:
        Base directory for temporary files
    @return <subprocess.Popen>:
        Master job process
    """
    outdir = os.path.abspath(outdir)

    # Default container bind paths (output dir
    # and tmp dir). These must be absolute paths
    # to mount the host filesystem into the container.
    default_binds = []
    temp = os.path.dirname(tmp_dir.rstrip("/"))
    if temp == os.sep:
        temp = tmp_dir.rstrip("/")
    if outdir not in additional_bind_paths.split(","):
        default_binds.append(outdir)
    if temp not in additional_bind_paths.split(","):
        default_binds.append(temp)
    bindpaths = ",".join(default_binds)

    # Point SINGULARITY_CACHEDIR at the output
    # directory (or an override).
    env = dict(os.environ)
    cache = os.path.join(outdir, ".singularity")
    env["SINGULARITY_CACHEDIR"] = cache
    if alt_cache:
        env["SINGULARITY_CACHEDIR"] = alt_cache
        cache = alt_cache

    if additional_bind_paths:
        if bindpaths:
            bindpaths = f",{bindpaths}"
        bindpaths = f"{additional_bind_paths}{bindpaths}"

    if not exists(os.path.join(outdir, "logfiles")):
        os.makedirs(os.path.join(outdir, "logfiles"))

    # Create the .singularity sandbox dir for
    # snakemake installs without setuid.
    if not exists(cache):
        os.makedirs(cache)

    if mode == "local":
        # Look into later: A direct snakemake API
        # call may be preferable to Popen:
        # https://snakemake.readthedocs.io/en/stable/api_reference/snakemake.html
        return subprocess.Popen([
            "snakemake", "-pr", "--rerun-incomplete",
            "--use-singularity",
            "--singularity-args", f"'-B {bindpaths}'",
            "--cores", str(threads),
            "--configfile=config.json",
        ], cwd=outdir, stderr=subprocess.STDOUT, stdout=logger, env=env)

    elif mode == "slurm":
        return subprocess.Popen([
            str(submission_script), mode,
            "-j", jobname, "-b", str(bindpaths),
            "-o", str(outdir), "-c", str(cache),
            "-t", f"'{tmp_dir}'",
        ], cwd=outdir, stderr=subprocess.STDOUT, stdout=logger, env=env)