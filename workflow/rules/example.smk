# =============================================================
# Template rules: copy this as the starting point for new rules
# =============================================================
#
# This file is meant to be included by the main Snakefile (via
# `include:`), not run on its own. It relies on several
# variables that the Snakefile defines & passes down into scope:
#
#   WORKPATH: the pipeline's output/working directory
#   SAMPLES:  A list containing each sample name
#   config:   the merged pipeline config (from config.json)
#   cluster:  the parsed cluster.json resource map
#   join:     os.path.join, imported in the Snakefile
#
# Because those come from the including Snakefile, the only
# thing this file imports for itself is the allocated() helper,
# which reads per-rule resources (mem/time/threads/partition/gres)
# from the cluster config.
#
# The example below assumes Illumina paired-end FastQ input,
# but the same structure adapts to other supported input types
# (BAM/CRAM, VCF, CSV/TSV) by changing the input/output paths
# and the shell command.
#
# Local imports
from scripts.common import (
    allocated
)


# Example scatter rule. In the rule below,
# the MD5 calculation will be scattered for
# every input sample that was provided.
rule scatter_fastq_md5:
    """
    Data-processing step to calculate an MD5 checksum for each
    input FastQ file. The checksum is written to an output file
    that can be referenced later for data-integrity/provenance
    tracking.
    @Input:
        Input FastQ file (scatter-per-sample)
    @Output:
        MD5 checksum output file
    """
    input:
        # Input files, the rule will not start
        # until these files exist on the filesystem.
        # Input files to the pipeline can be found
        # in the inputs folder in the output directory.
        r1 = join(WORKPATH, "inputs", "{name}.R1.fastq.gz"),
        r2 = join(WORKPATH, "inputs", "{name}.R2.fastq.gz"),
    output:
        # Output files created by the rule.
        md5_r1 = join(WORKPATH, "{name}", "md5", "{name}.R1.fastq.gz.md5"),
        md5_r2 = join(WORKPATH, "{name}", "md5", "{name}.R2.fastq.gz.md5"),
    params:
        # Pass or dynamically build args/options for
        # the shell directive. rname is an OpenOmics
        # convention for the rule's short name. Older
        # pipelines have canonically used this variable
        # to define rule job names and log file names;
        # however, this is not required if you are using
        # this version of the template. These values are
        # set based on the rule name.
        rname = "fqmd5",
    resources:
        # Every OpenOmics rule needs partition, mem,
        # time, gres for job submission. Copy/paste
        # these lines into new rules; only the hard-coded
        # rule name needs updating. Add a matching key
        # for this rule to config/cluster.json to
        # override the defaults; without it, jobs use
        # the "__default__" key. The provided cluster
        # config already has an example entry for this
        # rule that can be referenced as needed.
        partition = allocated("partition", "scatter_fastq_md5", cluster),
        mem       = allocated("mem",  "scatter_fastq_md5", cluster),
        time      = allocated("time", "scatter_fastq_md5", cluster),
        gres      = allocated("gres", "scatter_fastq_md5", cluster),
    threads:
        # Every OpenOmics rule needs a threads declaration
        # for resource allocation. Again, please just update
        # the hard-coded rule name (i.e "scatter_fastq_md5")
        # below when creating new rules in the future.
        int(allocated("threads", "scatter_fastq_md5", cluster))
    container:
        # Use containers for reproducibility/portability.
        # Add new docker image URIs to the container config
        # file (i.e config/containers.json) before testing
        # a dryrun. The "ubuntu-22.04" key below must exist
        # there first, or this lookup raises a KeyError.
        # Always prefer using official/LTS images. The
        # Biocontainers registry is a good source for
        # trusted bioinformatics images:
        # https://biocontainers.pro/registry
        config["images"]["ubuntu-22.04"]
    shell: """
    # Calculate MD5 checksum of the input FastQ files
    md5sum {input.r1} > {output.md5_r1}
    md5sum {input.r2} > {output.md5_r2}
    """


# Example gather rule. In contrast to the scatter
# rule above (which runs once per sample), this rule
# runs a single time and aggregates the per-sample
# MD5 files into one combined checksum file.
rule gather_fastq_md5:
    """
    Data-processing step to gather the per-sample MD5 checksum
    files produced by scatter_fastq_md5 and concatenate them
    into a single, project-level checksum file. The combined
    file can be referenced later for data-integrity/provenance
    tracking, or verified later with `md5sum -c`.
    @Input:
        Per-sample MD5 checksum files (gather-across-samples)
    @Output:
        Single combined MD5 checksum file
    """
    input:
        # expand() generates the list of per-sample output
        # filenames from the scatter rule above. This rule
        # depends on ALL of them, so it will not start until
        # every one exists, which is why it runs only once.
        md5s = expand(
            join(WORKPATH, "{name}", "md5", "{name}.{mate}.fastq.gz.md5"),
            name=SAMPLES,
            mate=["R1", "R2"],
        ),
    output:
        # A single combined checksum file for the whole project.
        combined = join(WORKPATH, "input_checksums.md5"),
    params:
        # Setting rname is not required for this template,
        # see explanation scatter rule above for info.
        rname = "gathermd5",
    resources:
        # Every OpenOmics rule needs partition, mem,
        # time, gres for job submission. Update
        # the hard-coded rule name below when copying
        # this into a new rule. Add a matching key for
        # this rule to config/cluster.json to override
        # the default allocations.
        partition = allocated("partition", "gather_fastq_md5", cluster),
        mem       = allocated("mem",  "gather_fastq_md5", cluster),
        time      = allocated("time", "gather_fastq_md5", cluster),
        gres      = allocated("gres", "gather_fastq_md5", cluster),
    threads:
        # Every OpenOmics rule needs a threads declaration.
        int(allocated("threads", "gather_fastq_md5", cluster))
    container:
        # Add the image URI to config/containers.json first;
        # the "ubuntu-22.04" key must exist there or this
        # lookup raises a KeyError.
        config["images"]["ubuntu-22.04"]
    shell: """
    # Concatenate every per-sample MD5 file into one combined
    # checksum file. Sorting keeps the output deterministic
    # regardless of the order Snakemake passes the inputs.
    cat {input.md5s} \\
        | awk -v OFS="  " '{{n=split($2, a, "/"); $2=a[n]; print}}' \\
        | sort -k2 \\
        > {output.combined}
    """
