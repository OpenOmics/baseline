# Standard Library
import json
import os
import sys
# Local imports
from shells import bash
from utils import err, exists, fatal


def prepare_cache(sif_cache, pl_name):
    """Creates the local SIF cache directory on the filesystem.
    Fails with an OSError if the provided path already exists as a file.
    @param sif_cache <str>:
        Path to the directory where local SIFs will be cached
    @param pl_name <str>:
        Name of the pipeline
    """
    if os.path.isfile(sif_cache):
        raise OSError(
            "\n\tFatal: Failed to create the provided SIF cache directory!\n"
            "\tThe --sif-cache PATH already exists on the filesystem as a file.\n"
            "\tPlease run {0} cache again with a different --sif-cache PATH.\n"
            .format(pl_name)
        )
    if not exists(sif_cache):
        os.makedirs(sif_cache)


def missing_images(images_config, sif_cache):
    """Finds images that are not yet cached locally on the filesystem.
    Compares images defined in the containers config against existing SIFs,
    and returns the URIs of any that still need to be pulled.
    @param images_config <str>:
        Path to the containers config file (config/containers.json)
    @param sif_cache <str>:
        Path to the directory where local SIFs are cached
    @return uris_to_pull <list[str]>:
        List of image URIs that do not yet exist in the cache
    """
    with open(images_config, 'r') as fh:
        data = json.load(fh)

    uris_to_pull = []
    for uri in data['images'].values():
        sif_name = '{0}.sif'.format(os.path.basename(uri).replace(':', '_'))
        sif_path = os.path.join(sif_cache, sif_name)
        if not exists(sif_path):
            err('Image will be pulled from "{0}".'.format(uri))
            uris_to_pull.append(uri)

    return uris_to_pull


def pull_images(repo_path, sif_cache, uris_to_pull):
    """Pulls remote containers and caches them locally as SIF files.
    Invokes the container cache script (src/cache.sh) and fails fatally
    if not all containers could be pulled successfully.
    @param repo_path <str>:
        Absolute path to the repository root
    @param sif_cache <str>:
        Path to the directory where local SIFs will be cached
    @param uris_to_pull <list[str]>:
        List of image URIs to pull from their remote registry
    """
    username = os.environ.get('USER', os.environ.get('USERNAME'))
    cache_script = os.path.join(repo_path, 'src', 'cache.sh')
    # Values are single-quoted to avoid shell injection.
    exitcode = bash(
        "{script} local"
        " -s '{cache}'"
        " -i '{images}'"
        " -t '{cache}/{user}/.singularity/'".format(
            script=cache_script,
            cache=sif_cache,
            images=','.join(uris_to_pull),
            user=username,
        )
    )
    if exitcode != 0:
        fatal('Fatal: Failed to pull all containers. Please try again!')
    print('Done: successfully pulled all software containers!')