import logging
import os
import shutil
import subprocess
from typing import Callable, Dict, Optional, Tuple, List

from acbs.base import ACBSPackageInfo, ACBSSourceInfo
from acbs.crypto import check_hash_hashlib, hash_url
from acbs.utils import guess_extension_name

fetcher_signature = Callable[[ACBSSourceInfo,
                              str, str], Optional[ACBSSourceInfo]]
processor_signature = Callable[[ACBSPackageInfo, int, str], None]
pair_signature = Tuple[fetcher_signature, processor_signature]
generate_mode = False


def fetch_source(info: List[ACBSSourceInfo], source_location: str, package_name: str) -> Optional[ACBSSourceInfo]:
    logging.info('Fetching required source files...')
    count = 0
    for i in info:
        count += 1
        logging.info('Fetching source ({}/{})...'.format(count, len(info)))
        if not i.enabled:
            logging.info('Source {} skipped.'.format(count))
        url_hash = hash_url(i.url)
        fetch_source_inner(i, source_location, url_hash)
    return None


def fetch_source_inner(info: ACBSSourceInfo, source_location: str, package_name: str) -> Optional[ACBSSourceInfo]:
    type_ = info.type
    retry = 0
    fetcher: Optional[pair_signature] = handlers.get(type_.upper())
    if not fetcher or not callable(fetcher[0]):
        raise NotImplementedError('Unsupported source type: {}'.format(type_))
    while retry < 5:
        retry += 1
        try:
            return fetcher[0](info, source_location, package_name)
        except Exception as ex:
            logging.exception(ex)
            logging.warning('Retrying ({}/5)...'.format(retry))
            continue
    raise RuntimeError(
        'Unable to fetch source files, failed 5 times in a row.')


def process_source(info: ACBSPackageInfo, source_name: str) -> None:
    idx = 0
    for source_uri in info.source_uri:
        type_ = source_uri.type
        fetcher: Optional[pair_signature] = handlers.get(type_.upper())
        if not fetcher or not callable(fetcher[1]):
            raise NotImplementedError(
                'Unsupported source type: {}'.format(type_))
        fetcher[1](info, idx, source_name)
        idx += 1
    return


# Fetchers implementations
def tarball_fetch(info: ACBSSourceInfo, source_location: str, name: str) -> Optional[ACBSSourceInfo]:
    if source_location:
        filename = ''
        if info.chksum[1]:
            filename = info.chksum[1]
        elif generate_mode:
            filename = hash_url(info.url)
        else:
            raise ValueError('No checksum found. Please specify the checksum!')
        full_path = os.path.join(source_location, filename)
        flag_path = os.path.join(source_location, '{}.dl'.format(filename))
        if os.path.exists(full_path) and not os.path.exists(flag_path):
            info.source_location = full_path
            return info
        try:
            # `touch ${flag_path}`, some servers may not support Range, so this is to ensure
            # if the download has finished successfully, we don't overwrite the downloaded file
            with open(flag_path, 'wb') as f:
                f.write(b'')
            subprocess.check_call(
                ['wget', '-c', info.url, '-O', full_path])
            info.source_location = full_path
            os.unlink(flag_path)  # delete the flag
            return info
        except Exception:
            raise AssertionError('Failed to fetch source with Wget!')
        return None
    return None


def tarball_processor_innner(package: ACBSPackageInfo, index: int, source_name: str, decompress=True) -> None:
    info = package.source_uri[index]
    if not info.source_location:
        raise ValueError('Where is the source file?')
    check_hash_hashlib(info.chksum, info.source_location)
    server_filename = os.path.basename(info.url)
    extension = guess_extension_name(server_filename)
    # this name is used in the build directory (will be seen by the build scripts)
    # the name will be, e.g. 'acbs-0.1.0.tar.gz'
    facade_name = info.source_name or '{name}-{version}{index}{extension}'.format(
        name=source_name, version=package.version, extension=extension,
        index=('' if index == 0 else ('-%s' % index)))
    os.symlink(info.source_location, os.path.join(
        package.build_location, facade_name))
    if not decompress:
        return
    # decompress
    logging.info('Extracting {}...'.format(facade_name))
    subprocess.check_call(['bsdtar', '-xf', facade_name],
                          cwd=package.build_location)
    return


def tarball_processor(package: ACBSPackageInfo, index: int, source_name: str) -> None:
    return tarball_processor_innner(package, index, source_name)


def blob_processor(package: ACBSPackageInfo, index: int, source_name: str) -> None:
    return tarball_processor_innner(package, index, source_name)


def git_fetch(info: ACBSSourceInfo, source_location: str, name: str) -> Optional[ACBSSourceInfo]:
    full_path = os.path.join(source_location, name)
    if not os.path.exists(full_path):
        subprocess.check_call(['git', 'clone', '--bare', info.url, full_path])
    else:
        logging.info('Updating repository...')
        subprocess.check_call(
            ['git', 'fetch', 'origin', '+refs/heads/*:refs/heads/*', '--prune'], cwd=full_path)
    info.source_location = full_path
    return info


def git_processor(package: ACBSPackageInfo, index: int, source_name: str) -> None:
    info = package.source_uri[index]
    if not info.revision:
        raise ValueError(
            'Please specify a specific git commit for this package. (GITCO not defined)')
    if not info.source_location:
        raise ValueError('Where is the git repository?')
    checkout_location = os.path.join(package.build_location, info.source_name or source_name)
    os.mkdir(checkout_location)
    logging.info('Checking out git repository at {}'.format(info.revision))
    subprocess.check_call(
        ['git', '--git-dir', info.source_location, '--work-tree', checkout_location,
         'checkout', '-f', info.revision or ''])
    logging.info('Fetching submodules (if any)...')
    subprocess.check_call(
        [
            'git', '--git-dir', info.source_location, '--work-tree', checkout_location,
            'submodule', 'update', '--init', '--recursive'
        ], cwd=checkout_location)
    with open(os.path.join(package.build_location, '.acbs-script'), 'wt') as f:
        f.write(
            'ACBS_SRC=\'%s\';acbs_copy_git(){ abinfo \'Copying git folder...\'; cp -ar "${ACBS_SRC}" .git/; sed -i \'s|bare = true|bare = false|\' \'.git/config\'; }' % (info.source_location))
    return None


def svn_fetch(info: ACBSSourceInfo, source_location: str, name: str) -> Optional[ACBSSourceInfo]:
    full_path = os.path.join(source_location, name)
    if not info.revision:
        raise ValueError(
            'Please specify a svn revision for this package. (SVNCO not defined)')
    logging.info(
        'Checking out subversion repository at r{}'.format(info.revision))
    if not os.path.exists(full_path):
        subprocess.check_call(
            ['svn', 'co', '--force', '-r', info.revision, info.url, full_path])
    else:
        subprocess.check_call(
            ['svn', 'up', '--force', '-r', info.revision], cwd=full_path)
    info.source_location = full_path
    return info


def svn_processor(package: ACBSPackageInfo, index: int, source_name: str) -> None:
    info = package.source_uri[index]
    if not info.source_location:
        raise ValueError('Where is the subversion repository?')
    checkout_location = os.path.join(package.build_location, info.source_name or source_name)
    logging.info('Copying subversion repository...')
    shutil.copytree(info.source_location, checkout_location)
    return


def hg_fetch(info: ACBSSourceInfo, source_location: str, name: str) -> Optional[ACBSSourceInfo]:
    full_path = os.path.join(source_location, name)
    if not os.path.exists(full_path):
        subprocess.check_call(['hg', 'clone', '-U', info.url, full_path])
    else:
        logging.info('Updating repository...')
        subprocess.check_call(['hg', 'pull'], cwd=full_path)
    info.source_location = full_path
    return info


def hg_processor(package: ACBSPackageInfo, index: int, source_name: str) -> None:
    info = package.source_uri[index]
    if not info.revision:
        raise ValueError(
            'Please specify a specific hg commit for this package. (HGCO not defined)')
    if not info.source_location:
        raise ValueError('Where is the hg repository?')
    checkout_location = os.path.join(package.build_location, info.source_name or source_name)
    logging.info('Copying hg repository...')
    shutil.copytree(info.source_location, checkout_location)
    logging.info('Checking out hg repository at {}'.format(info.revision))
    subprocess.check_call(
        ['hg', 'update', '-C', '-r', info.revision, '-R', checkout_location])
    return None


def dummy_fetch(info: ACBSSourceInfo, source_location: str, name: str) -> Optional[ACBSSourceInfo]:
    if source_location:
        logging.info('Not fetching any source as requested')
        return info
    return None


def dummy_processor(package: ACBSPackageInfo, index: int, source_name: str) -> None:
    return None


def bzr_fetch(info: ACBSSourceInfo, source_location: str, name: str) -> Optional[ACBSSourceInfo]:
    full_path = os.path.join(source_location, name)
    if not os.path.exists(full_path):
        subprocess.check_call(['bzr', 'branch', '--no-tree', info.url, full_path])
    else:
        logging.info('Updating repository...')
        subprocess.check_call(['bzr', 'pull'], cwd=full_path)
    info.source_location = full_path
    return info


def bzr_processor(package: ACBSPackageInfo, index: int, source_name: str) -> None:
    info = package.source_uri[index]
    if not info.revision:
        raise ValueError(
            'Please specify a specific bzr revision for this package. (BZRCO not defined)')
    if not info.source_location:
        raise ValueError('Where is the bzr repository?')
    checkout_location = os.path.join(package.build_location, info.source_name or source_name)
    logging.info('Copying bzr repository...')
    shutil.copytree(info.source_location, checkout_location)
    logging.info('Checking out bzr repository at {}'.format(info.revision))
    subprocess.check_call(
        ['bzr', 'co', '-r', info.revision], cwd=checkout_location)
    return None


handlers: Dict[str, pair_signature] = {
    'GIT': (git_fetch, git_processor),
    'SVN': (svn_fetch, svn_processor),
    'BZR': (bzr_fetch, bzr_processor),
    'HG': (hg_fetch, hg_processor),
    'TARBALL': (tarball_fetch, tarball_processor),
    'FILE': (tarball_fetch, blob_processor),
    'NONE': (dummy_fetch, dummy_processor),
}
