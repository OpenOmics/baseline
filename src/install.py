# Standard library
import json
import os
from shutil import unpack_archive
# Local imports
from download import main as installer
from utils import cat, err


def read_install_config(pl_home):
    """Reads in the install config file from the config directory.
    Reference files and their MD5 checksums are defined in
    'config/install.json' on the local filesystem.
    @param pl_home <str>:
        Path to pipeline github repo
    @return install_config <dict>:
        Parsed contents of 'config/install.json'
    """
    config_file = os.path.join(pl_home, 'config', 'install.json')
    with open(config_file) as fh:
        return json.load(fh)


def has_downloads(targets):
    """Checks whether any install target defines files to download.
    A target with no chunks is treated as having nothing to download.
    @param targets <dict>:
        Mapping of target name to its {download_link: md5} chunks
    @return has_downloads <bool>:
        True if at least one target has chunks to download
    """
    return any(chunks for chunks in targets.values())


def download_target(sub_args, chunks):
    """Invokes 'src/download.py' to pull a single target's chunks.
    Sets the links, MD5 checksums, and output directory required by
    the download script, then passes them along via the args object.
    @param sub_args <parser.parse_args() object>:
        Parsed arguments for install sub-command
    @param chunks <dict>:
        Mapping of download link to its expected MD5 checksum
    """
    # download.py needs the links, MD5 checksums,
    # and the output directory
    sub_args.input = list(chunks.keys())
    sub_args.md5 = list(chunks.values())
    sub_args.output = sub_args.ref_path
    # respects the dryrun option
    installer(sub_args)


def assemble_target(sub_args, chunks):
    """Restores a target's tarball from its chunks, then extracts it.
    Concatenates the locally downloaded chunks to restore the tarball,
    extracts the archive, and deletes the chunks and tarball afterward
    to reduce the local diskspace footprint.
    @param sub_args <parser.parse_args() object>:
        Parsed arguments for install sub-command
    @param chunks <dict>:
        Mapping of download link to its expected MD5 checksum
    """
    local_chunks = [
        os.path.join(sub_args.ref_path, link.split('/')[-1])
        for link in chunks
    ]

    print('Merging chunks... {0}'.format(','.join(local_chunks)))
    tarball = cat(
        local_chunks,
        os.path.join(sub_args.ref_path, 'merged_chunks.tar.gz')
    )

    for chunk in local_chunks:
        _remove(chunk, 'Warning: failed to remove local download chunk... {}')

    print('Extracting tarball... {0}'.format(tarball))
    unpack_archive(tarball, sub_args.ref_path)

    _remove(tarball, 'Warning: failed to remove resource bundle tarball... {}')


def _remove(path, warning):
    """Removes a file, warning instead of failing if removal is unsuccessful.
    Used to clean up local download chunks and tarballs to reduce the
    diskspace footprint without halting the install on failure.
    @param path <str>:
        Path to the file to remove
    @param warning <str>:
        Format string for the warning message; takes the path as its argument
    """
    try:
        os.remove(path)
    except OSError:
        err("{0} {1}".format(warning, path))