# borg cli interface / toplevel archiver code

import sys
import traceback

try:
    import argparse
    import collections
    import configparser
    import faulthandler
    import functools
    import hashlib
    import inspect
    import itertools
    import json
    import logging
    import os
    import re
    import shlex
    import shutil
    import signal
    import stat
    import subprocess
    import tarfile
    import textwrap
    import time
    from binascii import unhexlify
    from contextlib import contextmanager
    from datetime import datetime, timedelta

    from .logger import create_logger, setup_logging

    logger = create_logger()

    import borg
    from . import __version__
    from . import helpers
    from .algorithms.checksums import crc32
    from .archive import Archive, ArchiveChecker, ArchiveRecreater, Statistics, is_special
    from .archive import BackupError, BackupOSError, backup_io, OsOpen, stat_update_check
    from .archive import FilesystemObjectProcessors, MetadataCollector, ChunksProcessor
    from .cache import Cache, assert_secure, SecurityManager
    from .constants import *  # NOQA
    from .compress import CompressionSpec
    from .crypto.key import key_creator, key_argument_names, tam_required_file, tam_required, RepoKey, PassphraseKey
    from .crypto.keymanager import KeyManager
    from .helpers import EXIT_SUCCESS, EXIT_WARNING, EXIT_ERROR
    from .helpers import Error, NoManifestError, set_ec
    from .helpers import positive_int_validator, location_validator, archivename_validator, ChunkerParams, Location
    from .helpers import PrefixSpec, GlobSpec, SortBySpec, FilesCacheMode
    from .helpers import BaseFormatter, ItemFormatter, ArchiveFormatter
    from .helpers import format_timedelta, format_file_size, parse_file_size, format_archive
    from .helpers import safe_encode, remove_surrogates, bin_to_hex, prepare_dump_dict
    from .helpers import interval, prune_within, prune_split, PRUNING_PATTERNS
    from .helpers import timestamp
    from .helpers import get_cache_dir, os_stat
    from .helpers import Manifest, AI_HUMAN_SORT_KEYS
    from .helpers import hardlinkable
    from .helpers import StableDict
    from .helpers import check_python, check_extension_modules
    from .helpers import dir_is_tagged, is_slow_msgpack, is_supported_msgpack, yes, sysinfo
    from .helpers import log_multi
    from .helpers import signal_handler, raising_signal_handler, SigHup, SigTerm
    from .helpers import ErrorIgnoringTextIOWrapper
    from .helpers import ProgressIndicatorPercent
    from .helpers import basic_json_data, json_print
    from .helpers import replace_placeholders
    from .helpers import ChunkIteratorFileWrapper
    from .helpers import popen_with_error_handling, prepare_subprocess_env
    from .helpers import dash_open
    from .helpers import umount
    from .helpers import flags_root, flags_dir, flags_special_follow, flags_special
    from .helpers import msgpack
    from .nanorst import rst_to_terminal
    from .patterns import ArgparsePatternAction, ArgparseExcludeFileAction, ArgparsePatternFileAction, parse_exclude_pattern
    from .patterns import PatternMatcher
    from .item import Item
    from .platform import get_flags, get_process_id, SyncFile
    from .remote import RepositoryServer, RemoteRepository, cache_if_remote
    from .repository import Repository, LIST_SCAN_LIMIT, TAG_PUT, TAG_DELETE, TAG_COMMIT
    from .selftest import selftest
    from .upgrader import AtticRepositoryUpgrader, BorgRepositoryUpgrader
except BaseException:
    # an unhandled exception in the try-block would cause the borg cli command to exit with rc 1 due to python's
    # default behavior, see issue #4424.
    # as borg defines rc 1 as WARNING, this would be a mismatch, because a crash should be an ERROR (rc 2).
    traceback.print_exc()
    sys.exit(2)  # == EXIT_ERROR

assert EXIT_ERROR == 2, "EXIT_ERROR is not 2, as expected - fix assert AND exception handler right above this line."


STATS_HEADER = "                       Original size      Compressed size    Deduplicated size"


def argument(args, str_or_bool):
    """If bool is passed, return it. If str is passed, retrieve named attribute from args."""
    if isinstance(str_or_bool, str):
        return getattr(args, str_or_bool)
    if isinstance(str_or_bool, (list, tuple)):
        return any(getattr(args, item) for item in str_or_bool)
    return str_or_bool


def with_repository(fake=False, invert_fake=False, create=False, lock=True,
                    exclusive=False, manifest=True, cache=False, secure=True,
                    compatibility=None):
    """
    Method decorator for subcommand-handling methods: do_XYZ(self, args, repository, …)

    If a parameter (where allowed) is a str the attribute named of args is used instead.
    :param fake: (str or bool) use None instead of repository, don't do anything else
    :param create: create repository
    :param lock: lock repository
    :param exclusive: (str or bool) lock repository exclusively (for writing)
    :param manifest: load manifest and key, pass them as keyword arguments
    :param cache: open cache, pass it as keyword argument (implies manifest)
    :param secure: do assert_secure after loading manifest
    :param compatibility: mandatory if not create and (manifest or cache), specifies mandatory feature categories to check
    """

    if not create and (manifest or cache):
        if compatibility is None:
            raise AssertionError("with_repository decorator used without compatibility argument")
        if type(compatibility) is not tuple:
            raise AssertionError("with_repository decorator compatibility argument must be of type tuple")
    else:
        if compatibility is not None:
            raise AssertionError("with_repository called with compatibility argument but would not check" + repr(compatibility))
        if create:
            compatibility = Manifest.NO_OPERATION_CHECK

    def decorator(method):
        @functools.wraps(method)
        def wrapper(self, args, **kwargs):
            location = args.location  # note: 'location' must be always present in args
            append_only = getattr(args, 'append_only', False)
            storage_quota = getattr(args, 'storage_quota', None)
            make_parent_dirs = getattr(args, 'make_parent_dirs', False)
            if argument(args, fake) ^ invert_fake:
                return method(self, args, repository=None, **kwargs)
            elif location.proto == 'ssh':
                repository = RemoteRepository(location, create=create, exclusive=argument(args, exclusive),
                                              lock_wait=self.lock_wait, lock=lock, append_only=append_only,
                                              make_parent_dirs=make_parent_dirs, args=args)
            else:
                repository = Repository(location.path, create=create, exclusive=argument(args, exclusive),
                                        lock_wait=self.lock_wait, lock=lock, append_only=append_only,
                                        storage_quota=storage_quota, make_parent_dirs=make_parent_dirs)
            with repository:
                if manifest or cache:
                    kwargs['manifest'], kwargs['key'] = Manifest.load(repository, compatibility)
                    if 'compression' in args:
                        kwargs['key'].compressor = args.compression.compressor
                    if secure:
                        assert_secure(repository, kwargs['manifest'], self.lock_wait)
                if cache:
                    with Cache(repository, kwargs['key'], kwargs['manifest'],
                               progress=getattr(args, 'progress', False), lock_wait=self.lock_wait,
                               cache_mode=getattr(args, 'files_cache_mode', DEFAULT_FILES_CACHE_MODE),
                               consider_part_files=getattr(args, 'consider_part_files', False)) as cache_:
                        return method(self, args, repository=repository, cache=cache_, **kwargs)
                else:
                    return method(self, args, repository=repository, **kwargs)
        return wrapper
    return decorator


def with_archive(method):
    @functools.wraps(method)
    def wrapper(self, args, repository, key, manifest, **kwargs):
        archive = Archive(repository, key, manifest, args.location.archive,
                          numeric_owner=getattr(args, 'numeric_owner', False),
                          nobsdflags=getattr(args, 'nobsdflags', False),
                          cache=kwargs.get('cache'),
                          consider_part_files=args.consider_part_files, log_json=args.log_json)
        return method(self, args, repository=repository, manifest=manifest, key=key, archive=archive, **kwargs)
    return wrapper


def parse_storage_quota(storage_quota):
    parsed = parse_file_size(storage_quota)
    if parsed < parse_file_size('10M'):
        raise argparse.ArgumentTypeError('quota is too small (%s). At least 10M are required.' % storage_quota)
    return parsed


def get_func(args):
    # This works around http://bugs.python.org/issue9351
    # func is used at the leaf parsers of the argparse parser tree,
    # fallback_func at next level towards the root,
    # fallback2_func at the 2nd next level (which is root in our case).
    for name in 'func', 'fallback_func', 'fallback2_func':
        func = getattr(args, name, None)
        if func is not None:
            return func
    raise Exception('expected func attributes not found')


class Archiver:

    def __init__(self, lock_wait=None, prog=None):
        self.exit_code = EXIT_SUCCESS
        self.lock_wait = lock_wait
        self.prog = prog

    def print_error(self, msg, *args):
        msg = args and msg % args or msg
        self.exit_code = EXIT_ERROR
        logger.error(msg)

    def print_warning(self, msg, *args):
        msg = args and msg % args or msg
        self.exit_code = EXIT_WARNING  # we do not terminate here, so it is a warning
        logger.warning(msg)

    def print_file_status(self, status, path):
        if self.output_list and (self.output_filter is None or status in self.output_filter):
            if self.log_json:
                print(json.dumps({
                    'type': 'file_status',
                    'status': status,
                    'path': remove_surrogates(path),
                }), file=sys.stderr)
            else:
                logging.getLogger('borg.output.list').info("%1s %s", status, remove_surrogates(path))

    @staticmethod
    def build_matcher(inclexcl_patterns, include_paths):
        matcher = PatternMatcher()
        matcher.add_inclexcl(inclexcl_patterns)
        matcher.add_includepaths(include_paths)
        return matcher

    def do_serve(self, args):
        """Start in server mode. This command is usually not used manually."""
        RepositoryServer(
            restrict_to_paths=args.restrict_to_paths,
            restrict_to_repositories=args.restrict_to_repositories,
            append_only=args.append_only,
            storage_quota=args.storage_quota,
        ).serve()
        return EXIT_SUCCESS

    @with_repository(create=True, exclusive=True, manifest=False)
    def do_init(self, args, repository):
        """Initialize an empty repository"""
        path = args.location.canonical_path()
        logger.info('Initializing repository at "%s"' % path)
        try:
            key = key_creator(repository, args)
        except (EOFError, KeyboardInterrupt):
            repository.destroy()
            return EXIT_WARNING
        manifest = Manifest(key, repository)
        manifest.key = key
        manifest.write()
        repository.commit(compact=False)
        with Cache(repository, key, manifest, warn_if_unencrypted=False):
            pass
        if key.tam_required:
            tam_file = tam_required_file(repository)
            open(tam_file, 'w').close()
            logger.warning(
                '\n'
                'By default repositories initialized with this version will produce security\n'
                'errors if written to with an older version (up to and including Borg 1.0.8).\n'
                '\n'
                'If you want to use these older versions, you can disable the check by running:\n'
                'borg upgrade --disable-tam %s\n'
                '\n'
                'See https://borgbackup.readthedocs.io/en/stable/changes.html#pre-1-0-9-manifest-spoofing-vulnerability '
                'for details about the security implications.', shlex.quote(path))

        if key.NAME != 'plaintext':
            if 'repokey' in key.NAME:
                logger.warning(
                    '\n'
                    'IMPORTANT: you will need both KEY AND PASSPHRASE to access this repo!\n'
                    'The key is included in the repository config, but should be backed up in case the repository gets corrupted.\n'
                    'Use "borg key export" to export the key, optionally in printable format.\n'
                    'Write down the passphrase. Store both at safe place(s).\n')
            else:
                logger.warning(
                    '\n'
                    'IMPORTANT: you will need both KEY AND PASSPHRASE to access this repo!\n'
                    'Use "borg key export" to export the key, optionally in printable format.\n'
                    'Write down the passphrase. Store both at safe place(s).\n')
        return self.exit_code

    @with_repository(exclusive=True, manifest=False)
    def do_check(self, args, repository):
        """Check repository consistency"""
        if args.repair:
            msg = ("'check --repair' is an experimental feature that might result in data loss." +
                   "\n" +
                   "Type 'YES' if you understand this and want to continue: ")
            if not yes(msg, false_msg="Aborting.", invalid_msg="Invalid answer, aborting.",
                       truish=('YES', ), retry=False,
                       env_var_override='BORG_CHECK_I_KNOW_WHAT_I_AM_DOING'):
                return EXIT_ERROR
        if args.repo_only and any((args.verify_data, args.first, args.last, args.prefix)):
            self.print_error("--repository-only contradicts --first, --last, --prefix and --verify-data arguments.")
            return EXIT_ERROR
        if args.repair and args.max_duration:
            self.print_error("--repair does not allow --max-duration argument.")
            return EXIT_ERROR
        if args.max_duration and not args.repo_only:
            # when doing a partial repo check, we can only check crc32 checksums in segment files,
            # we can't build a fresh repo index in memory to verify the on-disk index against it.
            # thus, we should not do an archives check based on a unknown-quality on-disk repo index.
            # also, there is no max_duration support in the archives check code anyway.
            self.print_error("--repository-only is required for --max-duration support.")
            return EXIT_ERROR
        if not args.archives_only:
            if not repository.check(repair=args.repair, save_space=args.save_space, max_duration=args.max_duration):
                return EXIT_WARNING
        if args.prefix:
            args.glob_archives = args.prefix + '*'
        if not args.repo_only and not ArchiveChecker().check(
                repository, repair=args.repair, archive=args.location.archive,
                first=args.first, last=args.last, sort_by=args.sort_by or 'ts', glob=args.glob_archives,
                verify_data=args.verify_data, save_space=args.save_space):
            return EXIT_WARNING
        return EXIT_SUCCESS

    @with_repository(compatibility=(Manifest.Operation.CHECK,))
    def do_change_passphrase(self, args, repository, manifest, key):
        """Change repository key file passphrase"""
        if not hasattr(key, 'change_passphrase'):
            print('This repository is not encrypted, cannot change the passphrase.')
            return EXIT_ERROR
        key.change_passphrase()
        logger.info('Key updated')
        if hasattr(key, 'find_key'):
            # print key location to make backing it up easier
            logger.info('Key location: %s', key.find_key())
        return EXIT_SUCCESS

    @with_repository(lock=False, exclusive=False, manifest=False, cache=False)
    def do_key_export(self, args, repository):
        """Export the repository key for backup"""
        manager = KeyManager(repository)
        manager.load_keyblob()
        if args.paper:
            manager.export_paperkey(args.path)
        else:
            if not args.path:
                self.print_error("output file to export key to expected")
                return EXIT_ERROR
            try:
                if args.qr:
                    manager.export_qr(args.path)
                else:
                    manager.export(args.path)
            except IsADirectoryError:
                self.print_error("'{}' must be a file, not a directory".format(args.path))
                return EXIT_ERROR
        return EXIT_SUCCESS

    @with_repository(lock=False, exclusive=False, manifest=False, cache=False)
    def do_key_import(self, args, repository):
        """Import the repository key from backup"""
        manager = KeyManager(repository)
        if args.paper:
            if args.path:
                self.print_error("with --paper import from file is not supported")
                return EXIT_ERROR
            manager.import_paperkey(args)
        else:
            if not args.path:
                self.print_error("input file to import key from expected")
                return EXIT_ERROR
            if args.path != '-' and not os.path.exists(args.path):
                self.print_error("input file does not exist: " + args.path)
                return EXIT_ERROR
            manager.import_keyfile(args)
        return EXIT_SUCCESS

    @with_repository(manifest=False)
    def do_migrate_to_repokey(self, args, repository):
        """Migrate passphrase -> repokey"""
        manifest_data = repository.get(Manifest.MANIFEST_ID)
        key_old = PassphraseKey.detect(repository, manifest_data)
        key_new = RepoKey(repository)
        key_new.target = repository
        key_new.repository_id = repository.id
        key_new.enc_key = key_old.enc_key
        key_new.enc_hmac_key = key_old.enc_hmac_key
        key_new.id_key = key_old.id_key
        key_new.chunk_seed = key_old.chunk_seed
        key_new.change_passphrase()  # option to change key protection passphrase, save
        logger.info('Key updated')
        return EXIT_SUCCESS

    def do_benchmark_crud(self, args):
        """Benchmark Create, Read, Update, Delete for archives."""
        def measurement_run(repo, path):
            archive = repo + '::borg-benchmark-crud'
            compression = '--compression=none'
            # measure create perf (without files cache to always have it chunking)
            t_start = time.monotonic()
            rc = self.do_create(self.parse_args(['create', compression, '--files-cache=disabled', archive + '1', path]))
            t_end = time.monotonic()
            dt_create = t_end - t_start
            assert rc == 0
            # now build files cache
            rc1 = self.do_create(self.parse_args(['create', compression, archive + '2', path]))
            rc2 = self.do_delete(self.parse_args(['delete', archive + '2']))
            assert rc1 == rc2 == 0
            # measure a no-change update (archive1 is still present)
            t_start = time.monotonic()
            rc1 = self.do_create(self.parse_args(['create', compression, archive + '3', path]))
            t_end = time.monotonic()
            dt_update = t_end - t_start
            rc2 = self.do_delete(self.parse_args(['delete', archive + '3']))
            assert rc1 == rc2 == 0
            # measure extraction (dry-run: without writing result to disk)
            t_start = time.monotonic()
            rc = self.do_extract(self.parse_args(['extract', '--dry-run', archive + '1']))
            t_end = time.monotonic()
            dt_extract = t_end - t_start
            assert rc == 0
            # measure archive deletion (of LAST present archive with the data)
            t_start = time.monotonic()
            rc = self.do_delete(self.parse_args(['delete', archive + '1']))
            t_end = time.monotonic()
            dt_delete = t_end - t_start
            assert rc == 0
            return dt_create, dt_update, dt_extract, dt_delete

        @contextmanager
        def test_files(path, count, size, random):
            path = os.path.join(path, 'borg-test-data')
            os.makedirs(path)
            for i in range(count):
                fname = os.path.join(path, 'file_%d' % i)
                data = b'\0' * size if not random else os.urandom(size)
                with SyncFile(fname, binary=True) as fd:  # used for posix_fadvise's sake
                    fd.write(data)
            yield path
            shutil.rmtree(path)

        if '_BORG_BENCHMARK_CRUD_TEST' in os.environ:
            tests = [
                ('Z-TEST', 1, 1, False),
                ('R-TEST', 1, 1, True),
            ]
        else:
            tests = [
                ('Z-BIG', 10, 100000000, False),
                ('R-BIG', 10, 100000000, True),
                ('Z-MEDIUM', 1000, 1000000, False),
                ('R-MEDIUM', 1000, 1000000, True),
                ('Z-SMALL', 10000, 10000, False),
                ('R-SMALL', 10000, 10000, True),
            ]

        for msg, count, size, random in tests:
            with test_files(args.path, count, size, random) as path:
                dt_create, dt_update, dt_extract, dt_delete = measurement_run(args.location.canonical_path(), path)
            total_size_MB = count * size / 1e06
            file_size_formatted = format_file_size(size)
            content = 'random' if random else 'all-zero'
            fmt = '%s-%-10s %9.2f MB/s (%d * %s %s files: %.2fs)'
            print(fmt % ('C', msg, total_size_MB / dt_create, count, file_size_formatted, content, dt_create))
            print(fmt % ('R', msg, total_size_MB / dt_extract, count, file_size_formatted, content, dt_extract))
            print(fmt % ('U', msg, total_size_MB / dt_update, count, file_size_formatted, content, dt_update))
            print(fmt % ('D', msg, total_size_MB / dt_delete, count, file_size_formatted, content, dt_delete))

        return 0

    @with_repository(fake='dry_run', exclusive=True, compatibility=(Manifest.Operation.WRITE,))
    def do_create(self, args, repository, manifest=None, key=None):
        """Create new archive"""
        matcher = PatternMatcher(fallback=True)
        matcher.add_inclexcl(args.patterns)

        def create_inner(archive, cache, fso):
            # Add cache dir to inode_skip list
            skip_inodes = set()
            try:
                st = os.stat(get_cache_dir())
                skip_inodes.add((st.st_ino, st.st_dev))
            except OSError:
                pass
            # Add local repository dir to inode_skip list
            if not args.location.host:
                try:
                    st = os.stat(args.location.path)
                    skip_inodes.add((st.st_ino, st.st_dev))
                except OSError:
                    pass
            logger.debug('Processing files ...')
            for path in args.paths:
                if path == '-':  # stdin
                    path = args.stdin_name
                    if not dry_run:
                        try:
                            status = fso.process_stdin(path=path, cache=cache)
                        except BackupOSError as e:
                            status = 'E'
                            self.print_warning('%s: %s', path, e)
                    else:
                        status = '-'
                    self.print_file_status(status, path)
                    continue
                path = os.path.normpath(path)
                parent_dir = os.path.dirname(path) or '.'
                name = os.path.basename(path)
                # note: for path == '/':  name == '' and parent_dir == '/'.
                # the empty name will trigger a fall-back to path-based processing in os_stat and os_open.
                with OsOpen(path=parent_dir, flags=flags_root, noatime=True, op='open_root') as parent_fd:
                    try:
                        st = os_stat(path=path, parent_fd=parent_fd, name=name, follow_symlinks=False)
                    except OSError as e:
                        self.print_warning('%s: %s', path, e)
                        continue
                    if args.one_file_system:
                        restrict_dev = st.st_dev
                    else:
                        restrict_dev = None
                    self._process(path=path, parent_fd=parent_fd, name=name,
                                  fso=fso, cache=cache, matcher=matcher,
                                  exclude_caches=args.exclude_caches, exclude_if_present=args.exclude_if_present,
                                  keep_exclude_tags=args.keep_exclude_tags, skip_inodes=skip_inodes,
                                  restrict_dev=restrict_dev, read_special=args.read_special, dry_run=dry_run)
            if not dry_run:
                if args.progress:
                    archive.stats.show_progress(final=True)
                archive.stats += fso.stats
                archive.save(comment=args.comment, timestamp=args.timestamp, stats=archive.stats)
                args.stats |= args.json
                if args.stats:
                    if args.json:
                        json_print(basic_json_data(manifest, cache=cache, extra={
                            'archive': archive,
                        }))
                    else:
                        log_multi(DASHES,
                                  str(archive),
                                  DASHES,
                                  STATS_HEADER,
                                  str(archive.stats),
                                  str(cache),
                                  DASHES, logger=logging.getLogger('borg.output.stats'))

        self.output_filter = args.output_filter
        self.output_list = args.output_list
        self.nobsdflags = args.nobsdflags
        self.exclude_nodump = args.exclude_nodump
        dry_run = args.dry_run
        t0 = datetime.utcnow()
        t0_monotonic = time.monotonic()
        logger.info('Creating archive at "%s"' % args.location.orig)
        if not dry_run:
            with Cache(repository, key, manifest, progress=args.progress,
                       lock_wait=self.lock_wait, permit_adhoc_cache=args.no_cache_sync,
                       cache_mode=args.files_cache_mode) as cache:
                archive = Archive(repository, key, manifest, args.location.archive, cache=cache,
                                  create=True, checkpoint_interval=args.checkpoint_interval,
                                  numeric_owner=args.numeric_owner, noatime=args.noatime, noctime=args.noctime,
                                  progress=args.progress,
                                  chunker_params=args.chunker_params, start=t0, start_monotonic=t0_monotonic,
                                  log_json=args.log_json)
                metadata_collector = MetadataCollector(noatime=args.noatime, noctime=args.noctime,
                    nobsdflags=args.nobsdflags, numeric_owner=args.numeric_owner, nobirthtime=args.nobirthtime)
                cp = ChunksProcessor(cache=cache, key=key,
                    add_item=archive.add_item, write_checkpoint=archive.write_checkpoint,
                    checkpoint_interval=args.checkpoint_interval, rechunkify=False)
                fso = FilesystemObjectProcessors(metadata_collector=metadata_collector, cache=cache, key=key,
                    process_file_chunks=cp.process_file_chunks, add_item=archive.add_item,
                    chunker_params=args.chunker_params, show_progress=args.progress)
                create_inner(archive, cache, fso)
        else:
            create_inner(None, None, None)
        return self.exit_code

    def _process(self, *, path, parent_fd=None, name=None,
                 fso, cache, matcher,
                 exclude_caches, exclude_if_present, keep_exclude_tags, skip_inodes,
                 restrict_dev, read_special=False, dry_run=False):
        """
        Process *path* (or, preferably, parent_fd/name) recursively according to the various parameters.

        This should only raise on critical errors. Per-item errors must be handled within this method.
        """
        try:
            recurse_excluded_dir = False
            if matcher.match(path):
                with backup_io('stat'):
                    st = os_stat(path=path, parent_fd=parent_fd, name=name, follow_symlinks=False)
            else:
                self.print_file_status('x', path)
                # get out here as quickly as possible:
                # we only need to continue if we shall recurse into an excluded directory.
                # if we shall not recurse, then do not even touch (stat()) the item, it
                # could trigger an error, e.g. if access is forbidden, see #3209.
                if not matcher.recurse_dir:
                    return
                with backup_io('stat'):
                    st = os_stat(path=path, parent_fd=parent_fd, name=name, follow_symlinks=False)
                recurse_excluded_dir = stat.S_ISDIR(st.st_mode)
                if not recurse_excluded_dir:
                    return

            if (st.st_ino, st.st_dev) in skip_inodes:
                return
            # if restrict_dev is given, we do not want to recurse into a new filesystem,
            # but we WILL save the mountpoint directory (or more precise: the root
            # directory of the mounted filesystem that shadows the mountpoint dir).
            recurse = restrict_dev is None or st.st_dev == restrict_dev
            status = None
            if self.exclude_nodump:
                # Ignore if nodump flag is set
                with backup_io('flags'):
                    if get_flags(path=path, st=st) & stat.UF_NODUMP:
                        self.print_file_status('x', path)
                        return
            if stat.S_ISREG(st.st_mode):
                if not dry_run:
                    status = fso.process_file(path=path, parent_fd=parent_fd, name=name, st=st, cache=cache)
            elif stat.S_ISDIR(st.st_mode):
                with OsOpen(path=path, parent_fd=parent_fd, name=name, flags=flags_dir,
                            noatime=True, op='dir_open') as child_fd:
                    with backup_io('fstat'):
                        st = stat_update_check(st, os.fstat(child_fd))
                    if recurse:
                        tag_names = dir_is_tagged(path, exclude_caches, exclude_if_present)
                        if tag_names:
                            # if we are already recursing in an excluded dir, we do not need to do anything else than
                            # returning (we do not need to archive or recurse into tagged directories), see #3991:
                            if not recurse_excluded_dir:
                                if keep_exclude_tags and not dry_run:
                                    fso.process_dir(path=path, fd=child_fd, st=st)
                                    for tag_name in tag_names:
                                        tag_path = os.path.join(path, tag_name)
                                        self._process(path=tag_path, parent_fd=child_fd, name=tag_name,
                                                      fso=fso, cache=cache, matcher=matcher,
                                                      exclude_caches=exclude_caches, exclude_if_present=exclude_if_present,
                                                      keep_exclude_tags=keep_exclude_tags, skip_inodes=skip_inodes,
                                                      restrict_dev=restrict_dev, read_special=read_special, dry_run=dry_run)
                                self.print_file_status('x', path)
                            return
                    if not dry_run:
                        if not recurse_excluded_dir:
                            status = fso.process_dir(path=path, fd=child_fd, st=st)
                    if recurse:
                        with backup_io('scandir'):
                            entries = helpers.scandir_inorder(path=path, fd=child_fd)
                        for dirent in entries:
                            normpath = os.path.normpath(os.path.join(path, dirent.name))
                            self._process(path=normpath, parent_fd=child_fd, name=dirent.name,
                                          fso=fso, cache=cache, matcher=matcher,
                                          exclude_caches=exclude_caches, exclude_if_present=exclude_if_present,
                                          keep_exclude_tags=keep_exclude_tags, skip_inodes=skip_inodes,
                                          restrict_dev=restrict_dev, read_special=read_special, dry_run=dry_run)
            elif stat.S_ISLNK(st.st_mode):
                if not dry_run:
                    if not read_special:
                        status = fso.process_symlink(path=path, parent_fd=parent_fd, name=name, st=st)
                    else:
                        try:
                            st_target = os.stat(name, dir_fd=parent_fd, follow_symlinks=True)
                        except OSError:
                            special = False
                        else:
                            special = is_special(st_target.st_mode)
                        if special:
                            status = fso.process_file(path=path, parent_fd=parent_fd, name=name, st=st_target,
                                                      cache=cache, flags=flags_special_follow)
                        else:
                            status = fso.process_symlink(path=path, parent_fd=parent_fd, name=name, st=st)
            elif stat.S_ISFIFO(st.st_mode):
                if not dry_run:
                    if not read_special:
                        status = fso.process_fifo(path=path, parent_fd=parent_fd, name=name, st=st)
                    else:
                        status = fso.process_file(path=path, parent_fd=parent_fd, name=name, st=st,
                                                  cache=cache, flags=flags_special)
            elif stat.S_ISCHR(st.st_mode):
                if not dry_run:
                    if not read_special:
                        status = fso.process_dev(path=path, parent_fd=parent_fd, name=name, st=st, dev_type='c')
                    else:
                        status = fso.process_file(path=path, parent_fd=parent_fd, name=name, st=st,
                                                  cache=cache, flags=flags_special)
            elif stat.S_ISBLK(st.st_mode):
                if not dry_run:
                    if not read_special:
                        status = fso.process_dev(path=path, parent_fd=parent_fd, name=name, st=st, dev_type='b')
                    else:
                        status = fso.process_file(path=path, parent_fd=parent_fd, name=name, st=st,
                                                  cache=cache, flags=flags_special)
            elif stat.S_ISSOCK(st.st_mode):
                # Ignore unix sockets
                return
            elif stat.S_ISDOOR(st.st_mode):
                # Ignore Solaris doors
                return
            elif stat.S_ISPORT(st.st_mode):
                # Ignore Solaris event ports
                return
            else:
                self.print_warning('Unknown file type: %s', path)
                return
        except (BackupOSError, BackupError) as e:
            self.print_warning('%s: %s', path, e)
            status = 'E'
        if status == 'C':
            self.print_warning('%s: file changed while we backed it up', path)
        # Status output
        if status is None:
            if not dry_run:
                status = '?'  # need to add a status code somewhere
            else:
                status = '-'  # dry run, item was not backed up

        if not recurse_excluded_dir:
            self.print_file_status(status, path)

    @staticmethod
    def build_filter(matcher, peek_and_store_hardlink_masters, strip_components):
        if strip_components:
            def item_filter(item):
                matched = matcher.match(item.path) and os.sep.join(item.path.split(os.sep)[strip_components:])
                peek_and_store_hardlink_masters(item, matched)
                return matched
        else:
            def item_filter(item):
                matched = matcher.match(item.path)
                peek_and_store_hardlink_masters(item, matched)
                return matched
        return item_filter

    @with_repository(compatibility=(Manifest.Operation.READ,))
    @with_archive
    def do_extract(self, args, repository, manifest, key, archive):
        """Extract archive contents"""
        # be restrictive when restoring files, restore permissions later
        if sys.getfilesystemencoding() == 'ascii':
            logger.warning('Warning: File system encoding is "ascii", extracting non-ascii filenames will not be supported.')
            if sys.platform.startswith(('linux', 'freebsd', 'netbsd', 'openbsd', 'darwin', )):
                logger.warning('Hint: You likely need to fix your locale setup. E.g. install locales and use: LANG=en_US.UTF-8')

        matcher = self.build_matcher(args.patterns, args.paths)

        progress = args.progress
        output_list = args.output_list
        dry_run = args.dry_run
        stdout = args.stdout
        sparse = args.sparse
        strip_components = args.strip_components
        dirs = []
        partial_extract = not matcher.empty() or strip_components
        hardlink_masters = {} if partial_extract else None

        def peek_and_store_hardlink_masters(item, matched):
            if (partial_extract and not matched and hardlinkable(item.mode) and
                    item.get('hardlink_master', True) and 'source' not in item):
                hardlink_masters[item.get('path')] = (item.get('chunks'), None)

        filter = self.build_filter(matcher, peek_and_store_hardlink_masters, strip_components)
        if progress:
            pi = ProgressIndicatorPercent(msg='%5.1f%% Extracting: %s', step=0.1, msgid='extract')
            pi.output('Calculating size')
            extracted_size = sum(item.get_size(hardlink_masters) for item in archive.iter_items(filter))
            pi.total = extracted_size
        else:
            pi = None

        for item in archive.iter_items(filter, partial_extract=partial_extract,
                                       preload=True, hardlink_masters=hardlink_masters):
            orig_path = item.path
            if strip_components:
                item.path = os.sep.join(orig_path.split(os.sep)[strip_components:])
            if not args.dry_run:
                while dirs and not item.path.startswith(dirs[-1].path):
                    dir_item = dirs.pop(-1)
                    try:
                        archive.extract_item(dir_item, stdout=stdout)
                    except BackupOSError as e:
                        self.print_warning('%s: %s', remove_surrogates(dir_item.path), e)
            if output_list:
                logging.getLogger('borg.output.list').info(remove_surrogates(orig_path))
            try:
                if dry_run:
                    archive.extract_item(item, dry_run=True, pi=pi)
                else:
                    if stat.S_ISDIR(item.mode):
                        dirs.append(item)
                        archive.extract_item(item, stdout=stdout, restore_attrs=False)
                    else:
                        archive.extract_item(item, stdout=stdout, sparse=sparse, hardlink_masters=hardlink_masters,
                                             stripped_components=strip_components, original_path=orig_path, pi=pi)
            except (BackupOSError, BackupError) as e:
                self.print_warning('%s: %s', remove_surrogates(orig_path), e)

        if pi:
            pi.finish()

        if not args.dry_run:
            pi = ProgressIndicatorPercent(total=len(dirs), msg='Setting directory permissions %3.0f%%',
                                          msgid='extract.permissions')
            while dirs:
                pi.show()
                dir_item = dirs.pop(-1)
                try:
                    archive.extract_item(dir_item, stdout=stdout)
                except BackupOSError as e:
                    self.print_warning('%s: %s', remove_surrogates(dir_item.path), e)
        for pattern in matcher.get_unmatched_include_patterns():
            self.print_warning("Include pattern '%s' never matched.", pattern)
        if pi:
            # clear progress output
            pi.finish()
        return self.exit_code

    @with_repository(compatibility=(Manifest.Operation.READ,))
    @with_archive
    def do_export_tar(self, args, repository, manifest, key, archive):
        """Export archive contents as a tarball"""
        self.output_list = args.output_list

        # A quick note about the general design of tar_filter and tarfile;
        # The tarfile module of Python can provide some compression mechanisms
        # by itself, using the builtin gzip, bz2 and lzma modules (and "tarmodes"
        # such as "w:xz").
        #
        # Doing so would have three major drawbacks:
        # For one the compressor runs on the same thread as the program using the
        # tarfile, stealing valuable CPU time from Borg and thus reducing throughput.
        # Then this limits the available options - what about lz4? Brotli? zstd?
        # The third issue is that systems can ship more optimized versions than those
        # built into Python, e.g. pigz or pxz, which can use more than one thread for
        # compression.
        #
        # Therefore we externalize compression by using a filter program, which has
        # none of these drawbacks. The only issue of using an external filter is
        # that it has to be installed -- hardly a problem, considering that
        # the decompressor must be installed as well to make use of the exported tarball!

        filter = None
        if args.tar_filter == 'auto':
            # Note that filter remains None if tarfile is '-'.
            if args.tarfile.endswith('.tar.gz'):
                filter = 'gzip'
            elif args.tarfile.endswith('.tar.bz2'):
                filter = 'bzip2'
            elif args.tarfile.endswith('.tar.xz'):
                filter = 'xz'
            logger.debug('Automatically determined tar filter: %s', filter)
        else:
            filter = args.tar_filter

        tarstream = dash_open(args.tarfile, 'wb')
        tarstream_close = args.tarfile != '-'

        if filter:
            # When we put a filter between us and the final destination,
            # the selected output (tarstream until now) becomes the output of the filter (=filterout).
            # The decision whether to close that or not remains the same.
            filterout = tarstream
            filterout_close = tarstream_close
            env = prepare_subprocess_env(system=True)
            # There is no deadlock potential here (the subprocess docs warn about this), because
            # communication with the process is a one-way road, i.e. the process can never block
            # for us to do something while we block on the process for something different.
            filterproc = popen_with_error_handling(filter, stdin=subprocess.PIPE, stdout=filterout,
                                                   log_prefix='--tar-filter: ', env=env)
            if not filterproc:
                return EXIT_ERROR
            # Always close the pipe, otherwise the filter process would not notice when we are done.
            tarstream = filterproc.stdin
            tarstream_close = True

        # The | (pipe) symbol instructs tarfile to use a streaming mode of operation
        # where it never seeks on the passed fileobj.
        tar = tarfile.open(fileobj=tarstream, mode='w|')

        self._export_tar(args, archive, tar)

        # This does not close the fileobj (tarstream) we passed to it -- a side effect of the | mode.
        tar.close()

        if tarstream_close:
            tarstream.close()

        if filter:
            logger.debug('Done creating tar, waiting for filter to die...')
            rc = filterproc.wait()
            if rc:
                logger.error('--tar-filter exited with code %d, output file is likely unusable!', rc)
                self.exit_code = EXIT_ERROR
            else:
                logger.debug('filter exited with code %d', rc)

            if filterout_close:
                filterout.close()

        return self.exit_code

    def _export_tar(self, args, archive, tar):
        matcher = self.build_matcher(args.patterns, args.paths)

        progress = args.progress
        output_list = args.output_list
        strip_components = args.strip_components
        partial_extract = not matcher.empty() or strip_components
        hardlink_masters = {} if partial_extract else None

        def peek_and_store_hardlink_masters(item, matched):
            if (partial_extract and not matched and hardlinkable(item.mode) and
                    item.get('hardlink_master', True) and 'source' not in item):
                hardlink_masters[item.get('path')] = (item.get('chunks'), None)

        filter = self.build_filter(matcher, peek_and_store_hardlink_masters, strip_components)

        if progress:
            pi = ProgressIndicatorPercent(msg='%5.1f%% Processing: %s', step=0.1, msgid='extract')
            pi.output('Calculating size')
            extracted_size = sum(item.get_size(hardlink_masters) for item in archive.iter_items(filter))
            pi.total = extracted_size
        else:
            pi = None

        def item_content_stream(item):
            """
            Return a file-like object that reads from the chunks of *item*.
            """
            chunk_iterator = archive.pipeline.fetch_many([chunk_id for chunk_id, _, _ in item.chunks])
            if pi:
                info = [remove_surrogates(item.path)]
                return ChunkIteratorFileWrapper(chunk_iterator,
                                                lambda read_bytes: pi.show(increase=len(read_bytes), info=info))
            else:
                return ChunkIteratorFileWrapper(chunk_iterator)

        def item_to_tarinfo(item, original_path):
            """
            Transform a Borg *item* into a tarfile.TarInfo object.

            Return a tuple (tarinfo, stream), where stream may be a file-like object that represents
            the file contents, if any, and is None otherwise. When *tarinfo* is None, the *item*
            cannot be represented as a TarInfo object and should be skipped.
            """

            # If we would use the PAX (POSIX) format (which we currently don't),
            # we can support most things that aren't possible with classic tar
            # formats, including GNU tar, such as:
            # atime, ctime, possibly Linux capabilities (security.* xattrs)
            # and various additions supported by GNU tar in POSIX mode.

            stream = None
            tarinfo = tarfile.TarInfo()
            tarinfo.name = item.path
            tarinfo.mtime = item.mtime / 1e9
            tarinfo.mode = stat.S_IMODE(item.mode)
            tarinfo.uid = item.uid
            tarinfo.gid = item.gid
            tarinfo.uname = item.user or ''
            tarinfo.gname = item.group or ''
            # The linkname in tar has the same dual use the 'source' attribute of Borg items,
            # i.e. for symlinks it means the destination, while for hardlinks it refers to the
            # file.
            # Since hardlinks in tar have a different type code (LNKTYPE) the format might
            # support hardlinking arbitrary objects (including symlinks and directories), but
            # whether implementations actually support that is a whole different question...
            tarinfo.linkname = ""

            modebits = stat.S_IFMT(item.mode)
            if modebits == stat.S_IFREG:
                tarinfo.type = tarfile.REGTYPE
                if 'source' in item:
                    source = os.sep.join(item.source.split(os.sep)[strip_components:])
                    if hardlink_masters is None:
                        linkname = source
                    else:
                        chunks, linkname = hardlink_masters.get(item.source, (None, source))
                    if linkname:
                        # Master was already added to the archive, add a hardlink reference to it.
                        tarinfo.type = tarfile.LNKTYPE
                        tarinfo.linkname = linkname
                    elif chunks is not None:
                        # The item which has the chunks was not put into the tar, therefore
                        # we do that now and update hardlink_masters to reflect that.
                        item.chunks = chunks
                        tarinfo.size = item.get_size()
                        stream = item_content_stream(item)
                        hardlink_masters[item.get('source') or original_path] = (None, item.path)
                else:
                    tarinfo.size = item.get_size()
                    stream = item_content_stream(item)
            elif modebits == stat.S_IFDIR:
                tarinfo.type = tarfile.DIRTYPE
            elif modebits == stat.S_IFLNK:
                tarinfo.type = tarfile.SYMTYPE
                tarinfo.linkname = item.source
            elif modebits == stat.S_IFBLK:
                tarinfo.type = tarfile.BLKTYPE
                tarinfo.devmajor = os.major(item.rdev)
                tarinfo.devminor = os.minor(item.rdev)
            elif modebits == stat.S_IFCHR:
                tarinfo.type = tarfile.CHRTYPE
                tarinfo.devmajor = os.major(item.rdev)
                tarinfo.devminor = os.minor(item.rdev)
            elif modebits == stat.S_IFIFO:
                tarinfo.type = tarfile.FIFOTYPE
            else:
                self.print_warning('%s: unsupported file type %o for tar export', remove_surrogates(item.path), modebits)
                set_ec(EXIT_WARNING)
                return None, stream
            return tarinfo, stream

        for item in archive.iter_items(filter, preload=True, hardlink_masters=hardlink_masters):
            orig_path = item.path
            if strip_components:
                item.path = os.sep.join(orig_path.split(os.sep)[strip_components:])
            tarinfo, stream = item_to_tarinfo(item, orig_path)
            if tarinfo:
                if output_list:
                    logging.getLogger('borg.output.list').info(remove_surrogates(orig_path))
                tar.addfile(tarinfo, stream)

        if pi:
            pi.finish()

        for pattern in matcher.get_unmatched_include_patterns():
            self.print_warning("Include pattern '%s' never matched.", pattern)
        return self.exit_code

    @with_repository(compatibility=(Manifest.Operation.READ,))
    @with_archive
    def do_diff(self, args, repository, manifest, key, archive):
        """Diff contents of two archives"""

        def print_output(diff, path):
            print("{:<19} {}".format(diff, path))

        archive1 = archive
        archive2 = Archive(repository, key, manifest, args.archive2,
                           consider_part_files=args.consider_part_files)

        can_compare_chunk_ids = archive1.metadata.get('chunker_params', False) == archive2.metadata.get(
            'chunker_params', True) or args.same_chunker_params
        if not can_compare_chunk_ids:
            self.print_warning('--chunker-params might be different between archives, diff will be slow.\n'
                               'If you know for certain that they are the same, pass --same-chunker-params '
                               'to override this check.')

        matcher = self.build_matcher(args.patterns, args.paths)

        diffs = Archive.compare_archives_iter(archive1, archive2, matcher, can_compare_chunk_ids=can_compare_chunk_ids)
        # Conversion to string and filtering for diff.equal to save memory if sorting
        diffs = ((path, str(diff)) for path, diff in diffs if not diff.equal)

        if args.sort:
            diffs = sorted(diffs)

        for path, diff in diffs:
            print_output(diff, path)

        for pattern in matcher.get_unmatched_include_patterns():
            self.print_warning("Include pattern '%s' never matched.", pattern)

        return self.exit_code

    @with_repository(exclusive=True, cache=True, compatibility=(Manifest.Operation.CHECK,))
    @with_archive
    def do_rename(self, args, repository, manifest, key, cache, archive):
        """Rename an existing archive"""
        name = replace_placeholders(args.name)
        archive.rename(name)
        manifest.write()
        repository.commit(compact=False)
        cache.commit()
        return self.exit_code

    @with_repository(exclusive=True, manifest=False)
    def do_delete(self, args, repository):
        """Delete an existing repository or archives"""
        archive_filter_specified = args.first or args.last or args.prefix or args.glob_archives
        explicit_archives_specified = args.location.archive or args.archives
        if archive_filter_specified and explicit_archives_specified:
            self.print_error('Mixing archive filters and explicitly named archives is not supported.')
            return self.exit_code
        if archive_filter_specified or explicit_archives_specified:
            return self._delete_archives(args, repository)
        else:
            return self._delete_repository(args, repository)

    def _delete_archives(self, args, repository):
        """Delete archives"""
        dry_run = args.dry_run

        manifest, key = Manifest.load(repository, (Manifest.Operation.DELETE,))

        if args.location.archive or args.archives:
            archives = list(args.archives)
            if args.location.archive:
                archives.insert(0, args.location.archive)
            archive_names = tuple(archives)
        else:
            archive_names = tuple(x.name for x in manifest.archives.list_considering(args))
            if not archive_names:
                return self.exit_code

        if args.forced == 2:
            deleted = False
            for i, archive_name in enumerate(archive_names, 1):
                try:
                    current_archive = manifest.archives.pop(archive_name)
                except KeyError:
                    self.exit_code = EXIT_WARNING
                    logger.warning('Archive {} not found ({}/{}).'.format(archive_name, i, len(archive_names)))
                else:
                    deleted = True
                    msg = 'Would delete: {} ({}/{})' if dry_run else 'Deleted archive: {} ({}/{})'
                    logger.info(msg.format(format_archive(current_archive), i, len(archive_names)))
            if dry_run:
                logger.info('Finished dry-run.')
            elif deleted:
                manifest.write()
                # note: might crash in compact() after committing the repo
                repository.commit(compact=False)
                logger.info('Done. Run "borg check --repair" to clean up the mess.')
            else:
                logger.warning('Aborted.')
            return self.exit_code

        stats = Statistics()
        with Cache(repository, key, manifest, progress=args.progress, lock_wait=self.lock_wait) as cache:
            for i, archive_name in enumerate(archive_names, 1):
                msg = 'Would delete archive: {} ({}/{})' if dry_run else 'Deleting archive: {} ({}/{})'
                logger.info(msg.format(format_archive(manifest.archives[archive_name]), i, len(archive_names)))
                if not dry_run:
                    archive = Archive(repository, key, manifest, archive_name, cache=cache,
                                      consider_part_files=args.consider_part_files)
                    archive.delete(stats, progress=args.progress, forced=args.forced)
            if not dry_run:
                manifest.write()
                repository.commit(compact=False, save_space=args.save_space)
                cache.commit()
            if args.stats:
                log_multi(DASHES,
                          STATS_HEADER,
                          stats.summary.format(label='Deleted data:', stats=stats),
                          str(cache),
                          DASHES, logger=logging.getLogger('borg.output.stats'))

        return self.exit_code

    def _delete_repository(self, args, repository):
        """Delete a repository"""
        dry_run = args.dry_run

        if not args.cache_only:
            msg = []
            try:
                manifest, key = Manifest.load(repository, Manifest.NO_OPERATION_CHECK)
            except NoManifestError:
                msg.append("You requested to completely DELETE the repository *including* all archives it may "
                           "contain.")
                msg.append("This repository seems to have no manifest, so we can't tell anything about its "
                           "contents.")
            else:
                msg.append("You requested to completely DELETE the repository *including* all archives it "
                           "contains:")
                for archive_info in manifest.archives.list(sort_by=['ts']):
                    msg.append(format_archive(archive_info))
            msg.append("Type 'YES' if you understand this and want to continue: ")
            msg = '\n'.join(msg)
            if not yes(msg, false_msg="Aborting.", invalid_msg='Invalid answer, aborting.', truish=('YES',),
                       retry=False, env_var_override='BORG_DELETE_I_KNOW_WHAT_I_AM_DOING'):
                self.exit_code = EXIT_ERROR
                return self.exit_code
            if not dry_run:
                repository.destroy()
                logger.info("Repository deleted.")
                SecurityManager.destroy(repository)
            else:
                logger.info("Would delete repository.")
        if not dry_run:
            Cache.destroy(repository)
            logger.info("Cache deleted.")
        else:
            logger.info("Would delete cache.")
        return self.exit_code

    def do_mount(self, args):
        """Mount archive or an entire repository as a FUSE filesystem"""
        # Perform these checks before opening the repository and asking for a passphrase.

        try:
            import borg.fuse
        except ImportError as e:
            self.print_error('borg mount not available: loading FUSE support failed [ImportError: %s]' % str(e))
            return self.exit_code

        if not os.path.isdir(args.mountpoint) or not os.access(args.mountpoint, os.R_OK | os.W_OK | os.X_OK):
            self.print_error('%s: Mountpoint must be a writable directory' % args.mountpoint)
            return self.exit_code

        return self._do_mount(args)

    @with_repository(compatibility=(Manifest.Operation.READ,))
    def _do_mount(self, args, repository, manifest, key):
        from .fuse import FuseOperations

        with cache_if_remote(repository, decrypted_cache=key) as cached_repo:
            operations = FuseOperations(key, repository, manifest, args, cached_repo)
            logger.info("Mounting filesystem")
            try:
                operations.mount(args.mountpoint, args.options, args.foreground)
            except RuntimeError:
                # Relevant error message already printed to stderr by FUSE
                self.exit_code = EXIT_ERROR
        return self.exit_code

    def do_umount(self, args):
        """un-mount the FUSE filesystem"""
        return umount(args.mountpoint)

    @with_repository(compatibility=(Manifest.Operation.READ,))
    def do_list(self, args, repository, manifest, key):
        """List archive or repository contents"""
        if args.location.archive:
            if args.json:
                self.print_error('The --json option is only valid for listing archives, not archive contents.')
                return self.exit_code
            return self._list_archive(args, repository, manifest, key)
        else:
            if args.json_lines:
                self.print_error('The --json-lines option is only valid for listing archive contents, not archives.')
                return self.exit_code
            return self._list_repository(args, repository, manifest, key)

    def _list_archive(self, args, repository, manifest, key):
        matcher = self.build_matcher(args.patterns, args.paths)
        if args.format is not None:
            format = args.format
        elif args.short:
            format = "{path}{NL}"
        else:
            format = "{mode} {user:6} {group:6} {size:8} {mtime} {path}{extra}{NL}"

        def _list_inner(cache):
            archive = Archive(repository, key, manifest, args.location.archive, cache=cache,
                              consider_part_files=args.consider_part_files)

            formatter = ItemFormatter(archive, format, json_lines=args.json_lines)
            for item in archive.iter_items(lambda item: matcher.match(item.path)):
                sys.stdout.write(formatter.format_item(item))

        # Only load the cache if it will be used
        if ItemFormatter.format_needs_cache(format):
            with Cache(repository, key, manifest, lock_wait=self.lock_wait) as cache:
                _list_inner(cache)
        else:
            _list_inner(cache=None)

        return self.exit_code

    def _list_repository(self, args, repository, manifest, key):
        if args.format is not None:
            format = args.format
        elif args.short:
            format = "{archive}{NL}"
        else:
            format = "{archive:<36} {time} [{id}]{NL}"
        formatter = ArchiveFormatter(format, repository, manifest, key, json=args.json)

        output_data = []

        for archive_info in manifest.archives.list_considering(args):
            if args.json:
                output_data.append(formatter.get_item_data(archive_info))
            else:
                sys.stdout.write(formatter.format_item(archive_info))

        if args.json:
            json_print(basic_json_data(manifest, extra={
                'archives': output_data
            }))

        return self.exit_code

    @with_repository(cache=True, compatibility=(Manifest.Operation.READ,))
    def do_info(self, args, repository, manifest, key, cache):
        """Show archive details such as disk space used"""
        if any((args.location.archive, args.first, args.last, args.prefix, args.glob_archives)):
            return self._info_archives(args, repository, manifest, key, cache)
        else:
            return self._info_repository(args, repository, manifest, key, cache)

    def _info_archives(self, args, repository, manifest, key, cache):
        def format_cmdline(cmdline):
            return remove_surrogates(' '.join(shlex.quote(x) for x in cmdline))

        if args.location.archive:
            archive_names = (args.location.archive,)
        else:
            archive_names = tuple(x.name for x in manifest.archives.list_considering(args))
            if not archive_names:
                return self.exit_code

        output_data = []

        for i, archive_name in enumerate(archive_names, 1):
            archive = Archive(repository, key, manifest, archive_name, cache=cache,
                              consider_part_files=args.consider_part_files)
            info = archive.info()
            if args.json:
                output_data.append(info)
            else:
                info['duration'] = format_timedelta(timedelta(seconds=info['duration']))
                info['command_line'] = format_cmdline(info['command_line'])
                print(textwrap.dedent("""
                Archive name: {name}
                Archive fingerprint: {id}
                Comment: {comment}
                Hostname: {hostname}
                Username: {username}
                Time (start): {start}
                Time (end): {end}
                Duration: {duration}
                Number of files: {stats[nfiles]}
                Command line: {command_line}
                Utilization of maximum supported archive size: {limits[max_archive_size]:.0%}
                ------------------------------------------------------------------------------
                                       Original size      Compressed size    Deduplicated size
                This archive:   {stats[original_size]:>20s} {stats[compressed_size]:>20s} {stats[deduplicated_size]:>20s}
                {cache}
                """).strip().format(cache=cache, **info))
            if self.exit_code:
                break
            if not args.json and len(archive_names) - i:
                print()

        if args.json:
            json_print(basic_json_data(manifest, cache=cache, extra={
                'archives': output_data,
            }))
        return self.exit_code

    def _info_repository(self, args, repository, manifest, key, cache):
        info = basic_json_data(manifest, cache=cache, extra={
            'security_dir': cache.security_manager.dir,
        })

        if args.json:
            json_print(info)
        else:
            encryption = 'Encrypted: '
            if key.NAME == 'plaintext':
                encryption += 'No'
            else:
                encryption += 'Yes (%s)' % key.NAME
            if key.NAME.startswith('key file'):
                encryption += '\nKey file: %s' % key.find_key()
            info['encryption'] = encryption

            print(textwrap.dedent("""
            Repository ID: {id}
            Location: {location}
            {encryption}
            Cache: {cache.path}
            Security dir: {security_dir}
            """).strip().format(
                id=bin_to_hex(repository.id),
                location=repository._location.canonical_path(),
                **info))
            print(DASHES)
            print(STATS_HEADER)
            print(str(cache))
        return self.exit_code

    @with_repository(exclusive=True, compatibility=(Manifest.Operation.DELETE,))
    def do_prune(self, args, repository, manifest, key):
        """Prune repository archives according to specified rules"""
        if not any((args.secondly, args.minutely, args.hourly, args.daily,
                    args.weekly, args.monthly, args.yearly, args.within)):
            self.print_error('At least one of the "keep-within", "keep-last", '
                             '"keep-secondly", "keep-minutely", "keep-hourly", "keep-daily", '
                             '"keep-weekly", "keep-monthly" or "keep-yearly" settings must be specified.')
            return self.exit_code
        if args.prefix:
            args.glob_archives = args.prefix + '*'
        checkpoint_re = r'\.checkpoint(\.\d+)?'
        archives_checkpoints = manifest.archives.list(glob=args.glob_archives,
                                                      match_end=r'(%s)?\Z' % checkpoint_re,
                                                      sort_by=['ts'], reverse=True)
        is_checkpoint = re.compile(r'(%s)\Z' % checkpoint_re).search
        checkpoints = [arch for arch in archives_checkpoints if is_checkpoint(arch.name)]
        # keep the latest checkpoint, if there is no later non-checkpoint archive
        if archives_checkpoints and checkpoints and archives_checkpoints[0] is checkpoints[0]:
            keep_checkpoints = checkpoints[:1]
        else:
            keep_checkpoints = []
        checkpoints = set(checkpoints)
        # ignore all checkpoint archives to avoid keeping one (which is an incomplete backup)
        # that is newer than a successfully completed backup - and killing the successful backup.
        archives = [arch for arch in archives_checkpoints if arch not in checkpoints]
        keep = []
        # collect the rule responsible for the keeping of each archive in this dict
        # keys are archive ids, values are a tuple
        #   (<rulename>, <how many archives were kept by this rule so far >)
        kept_because = {}

        # find archives which need to be kept because of the keep-within rule
        if args.within:
            keep += prune_within(archives, args.within, kept_because)

        # find archives which need to be kept because of the various time period rules
        for rule in PRUNING_PATTERNS.keys():
            num = getattr(args, rule, None)
            if num is not None:
                keep += prune_split(archives, rule, num, kept_because)

        to_delete = (set(archives) | checkpoints) - (set(keep) | set(keep_checkpoints))
        stats = Statistics()
        with Cache(repository, key, manifest, lock_wait=self.lock_wait) as cache:
            list_logger = logging.getLogger('borg.output.list')
            # set up counters for the progress display
            to_delete_len = len(to_delete)
            archives_deleted = 0
            pi = ProgressIndicatorPercent(total=len(to_delete), msg='Pruning archives %3.0f%%', msgid='prune')
            for archive in archives_checkpoints:
                if archive in to_delete:
                    pi.show()
                    if args.dry_run:
                        log_message = 'Would prune:'
                    else:
                        archives_deleted += 1
                        log_message = 'Pruning archive (%d/%d):' % (archives_deleted, to_delete_len)
                        archive = Archive(repository, key, manifest, archive.name, cache,
                                          consider_part_files=args.consider_part_files)
                        archive.delete(stats, forced=args.forced)
                else:
                    if is_checkpoint(archive.name):
                        log_message = 'Keeping checkpoint archive:'
                    else:
                        log_message = 'Keeping archive (rule: {rule} #{num}):'.format(
                            rule=kept_because[archive.id][0], num=kept_because[archive.id][1]
                        )
                if args.output_list:
                    list_logger.info("{message:<40} {archive}".format(
                        message=log_message, archive=format_archive(archive)
                    ))
            pi.finish()
            if to_delete and not args.dry_run:
                manifest.write()
                repository.commit(compact=False, save_space=args.save_space)
                cache.commit()
            if args.stats:
                log_multi(DASHES,
                          STATS_HEADER,
                          stats.summary.format(label='Deleted data:', stats=stats),
                          str(cache),
                          DASHES, logger=logging.getLogger('borg.output.stats'))
        return self.exit_code

    @with_repository(fake=('tam', 'disable_tam'), invert_fake=True, manifest=False, exclusive=True)
    def do_upgrade(self, args, repository, manifest=None, key=None):
        """upgrade a repository from a previous version"""
        if args.tam:
            manifest, key = Manifest.load(repository, (Manifest.Operation.CHECK,), force_tam_not_required=args.force)

            if not hasattr(key, 'change_passphrase'):
                print('This repository is not encrypted, cannot enable TAM.')
                return EXIT_ERROR

            if not manifest.tam_verified or not manifest.config.get(b'tam_required', False):
                # The standard archive listing doesn't include the archive ID like in borg 1.1.x
                print('Manifest contents:')
                for archive_info in manifest.archives.list(sort_by=['ts']):
                    print(format_archive(archive_info), '[%s]' % bin_to_hex(archive_info.id))
                manifest.config[b'tam_required'] = True
                manifest.write()
                repository.commit(compact=False)
            if not key.tam_required:
                key.tam_required = True
                key.change_passphrase(key._passphrase)
                print('Key updated')
                if hasattr(key, 'find_key'):
                    print('Key location:', key.find_key())
            if not tam_required(repository):
                tam_file = tam_required_file(repository)
                open(tam_file, 'w').close()
                print('Updated security database')
        elif args.disable_tam:
            manifest, key = Manifest.load(repository, Manifest.NO_OPERATION_CHECK, force_tam_not_required=True)
            if tam_required(repository):
                os.unlink(tam_required_file(repository))
            if key.tam_required:
                key.tam_required = False
                key.change_passphrase(key._passphrase)
                print('Key updated')
                if hasattr(key, 'find_key'):
                    print('Key location:', key.find_key())
            manifest.config[b'tam_required'] = False
            manifest.write()
            repository.commit(compact=False)
        else:
            # mainly for upgrades from Attic repositories,
            # but also supports borg 0.xx -> 1.0 upgrade.

            repo = AtticRepositoryUpgrader(args.location.path, create=False)
            try:
                repo.upgrade(args.dry_run, inplace=args.inplace, progress=args.progress)
            except NotImplementedError as e:
                print("warning: %s" % e)
            repo = BorgRepositoryUpgrader(args.location.path, create=False)
            try:
                repo.upgrade(args.dry_run, inplace=args.inplace, progress=args.progress)
            except NotImplementedError as e:
                print("warning: %s" % e)
        return self.exit_code

    @with_repository(cache=True, exclusive=True, compatibility=(Manifest.Operation.CHECK,))
    def do_recreate(self, args, repository, manifest, key, cache):
        """Re-create archives"""
        msg = ("recreate is an experimental feature.\n"
               "Type 'YES' if you understand this and want to continue: ")
        if not yes(msg, false_msg="Aborting.", truish=('YES',),
                   env_var_override='BORG_RECREATE_I_KNOW_WHAT_I_AM_DOING'):
            return EXIT_ERROR

        matcher = self.build_matcher(args.patterns, args.paths)
        self.output_list = args.output_list
        self.output_filter = args.output_filter
        recompress = args.recompress != 'never'
        always_recompress = args.recompress == 'always'

        recreater = ArchiveRecreater(repository, manifest, key, cache, matcher,
                                     exclude_caches=args.exclude_caches, exclude_if_present=args.exclude_if_present,
                                     keep_exclude_tags=args.keep_exclude_tags, chunker_params=args.chunker_params,
                                     compression=args.compression, recompress=recompress, always_recompress=always_recompress,
                                     progress=args.progress, stats=args.stats,
                                     file_status_printer=self.print_file_status,
                                     checkpoint_interval=args.checkpoint_interval,
                                     dry_run=args.dry_run)

        if args.location.archive:
            name = args.location.archive
            target = replace_placeholders(args.target) if args.target else None
            if recreater.is_temporary_archive(name):
                self.print_error('Refusing to work on temporary archive of prior recreate: %s', name)
                return self.exit_code
            if not recreater.recreate(name, args.comment, target):
                self.print_error('Nothing to do. Archive was not processed.\n'
                                 'Specify at least one pattern, PATH, --comment, re-compression or re-chunking option.')
        else:
            if args.target is not None:
                self.print_error('--target: Need to specify single archive')
                return self.exit_code
            for archive in manifest.archives.list(sort_by=['ts']):
                name = archive.name
                if recreater.is_temporary_archive(name):
                    continue
                print('Processing', name)
                if not recreater.recreate(name, args.comment):
                    logger.info('Skipped archive %s: Nothing to do. Archive was not processed.', name)
        if not args.dry_run:
            manifest.write()
            repository.commit(compact=False)
            cache.commit()
        return self.exit_code

    @with_repository(manifest=False, exclusive=True)
    def do_with_lock(self, args, repository):
        """run a user specified command with the repository lock held"""
        # for a new server, this will immediately take an exclusive lock.
        # to support old servers, that do not have "exclusive" arg in open()
        # RPC API, we also do it the old way:
        # re-write manifest to start a repository transaction - this causes a
        # lock upgrade to exclusive for remote (and also for local) repositories.
        # by using manifest=False in the decorator, we avoid having to require
        # the encryption key (and can operate just with encrypted data).
        data = repository.get(Manifest.MANIFEST_ID)
        repository.put(Manifest.MANIFEST_ID, data)
        # usually, a 0 byte (open for writing) segment file would be visible in the filesystem here.
        # we write and close this file, to rather have a valid segment file on disk, before invoking the subprocess.
        # we can only do this for local repositories (with .io), though:
        if hasattr(repository, 'io'):
            repository.io.close_segment()
        env = prepare_subprocess_env(system=True)
        try:
            # we exit with the return code we get from the subprocess
            return subprocess.call([args.command] + args.args, env=env)
        finally:
            # we need to commit the "no change" operation we did to the manifest
            # because it created a new segment file in the repository. if we would
            # roll back, the same file would be later used otherwise (for other content).
            # that would be bad if somebody uses rsync with ignore-existing (or
            # any other mechanism relying on existing segment data not changing).
            # see issue #1867.
            repository.commit(compact=False)

    @with_repository(manifest=False, exclusive=True)
    def do_compact(self, args, repository):
        """compact segment files in the repository"""
        # see the comment in do_with_lock about why we do it like this:
        data = repository.get(Manifest.MANIFEST_ID)
        repository.put(Manifest.MANIFEST_ID, data)
        repository.commit(compact=True, cleanup_commits=args.cleanup_commits)
        return EXIT_SUCCESS

    @with_repository(exclusive=True, manifest=False)
    def do_config(self, args, repository):
        """get, set, and delete values in a repository or cache config file"""

        def repo_validate(section, name, value=None, check_value=True):
            if section not in ['repository', ]:
                raise ValueError('Invalid section')
            if name in ['segments_per_dir', 'max_segment_size', 'storage_quota', ]:
                if check_value:
                    try:
                        int(value)
                    except ValueError:
                        raise ValueError('Invalid value') from None
                    if name == 'max_segment_size':
                        if int(value) >= MAX_SEGMENT_SIZE_LIMIT:
                            raise ValueError('Invalid value: max_segment_size >= %d' % MAX_SEGMENT_SIZE_LIMIT)
            elif name in ['additional_free_space', ]:
                if check_value:
                    try:
                        parse_file_size(value)
                    except ValueError:
                        raise ValueError('Invalid value') from None
            elif name in ['append_only', ]:
                if check_value and value not in ['0', '1']:
                    raise ValueError('Invalid value')
            elif name in ['id', ]:
                if check_value:
                    try:
                        bin_id = unhexlify(value)
                    except:
                        raise ValueError('Invalid value, must be 64 hex digits') from None
                    if len(bin_id) != 32:
                        raise ValueError('Invalid value, must be 64 hex digits')
            else:
                raise ValueError('Invalid name')

        def cache_validate(section, name, value=None, check_value=True):
            if section not in ['cache', ]:
                raise ValueError('Invalid section')
            if name in ['previous_location', ]:
                if check_value:
                    Location(value)
            else:
                raise ValueError('Invalid name')

        def list_config(config):
            default_values = {
                'version': '1',
                'segments_per_dir': str(DEFAULT_SEGMENTS_PER_DIR),
                'max_segment_size': str(MAX_SEGMENT_SIZE_LIMIT),
                'additional_free_space': '0',
                'storage_quota': repository.storage_quota,
                'append_only': repository.append_only
            }
            print('[repository]')
            for key in ['version', 'segments_per_dir', 'max_segment_size',
                        'storage_quota', 'additional_free_space', 'append_only',
                        'id']:
                value = config.get('repository', key, fallback=False)
                if value is None:
                    value = default_values.get(key)
                    if value is None:
                        raise Error('The repository config is missing the %s key which has no default value' % key)
                print('%s = %s' % (key, value))

        if not args.list:
            if args.name is None:
                self.print_error('No config key name was provided.')
                return self.exit_code

            try:
                section, name = args.name.split('.')
            except ValueError:
                section = args.cache and "cache" or "repository"
                name = args.name

        if args.cache:
            manifest, key = Manifest.load(repository, (Manifest.Operation.WRITE,))
            assert_secure(repository, manifest, self.lock_wait)
            cache = Cache(repository, key, manifest, lock_wait=self.lock_wait)

        try:
            if args.cache:
                cache.cache_config.load()
                config = cache.cache_config._config
                save = cache.cache_config.save
                validate = cache_validate
            else:
                config = repository.config
                save = lambda: repository.save_config(repository.path, repository.config)  # noqa
                validate = repo_validate

            if args.delete:
                validate(section, name, check_value=False)
                config.remove_option(section, name)
                if len(config.options(section)) == 0:
                    config.remove_section(section)
                save()
            elif args.list:
                list_config(config)
            elif args.value:
                validate(section, name, args.value)
                if section not in config.sections():
                    config.add_section(section)
                config.set(section, name, args.value)
                save()
            else:
                try:
                    print(config.get(section, name))
                except (configparser.NoOptionError, configparser.NoSectionError) as e:
                    print(e, file=sys.stderr)
                    return EXIT_WARNING
            return EXIT_SUCCESS
        finally:
            if args.cache:
                cache.close()

    def do_debug_info(self, args):
        """display system information for debugging / bug reports"""
        print(sysinfo())

        # Additional debug information
        print('CRC implementation:', crc32.__name__)
        print('Process ID:', get_process_id())
        return EXIT_SUCCESS

    @with_repository(compatibility=Manifest.NO_OPERATION_CHECK)
    def do_debug_dump_archive_items(self, args, repository, manifest, key):
        """dump (decrypted, decompressed) archive items metadata (not: data)"""
        archive = Archive(repository, key, manifest, args.location.archive,
                          consider_part_files=args.consider_part_files)
        for i, item_id in enumerate(archive.metadata.items):
            data = key.decrypt(item_id, repository.get(item_id))
            filename = '%06d_%s.items' % (i, bin_to_hex(item_id))
            print('Dumping', filename)
            with open(filename, 'wb') as fd:
                fd.write(data)
        print('Done.')
        return EXIT_SUCCESS

    @with_repository(compatibility=Manifest.NO_OPERATION_CHECK)
    def do_debug_dump_archive(self, args, repository, manifest, key):
        """dump decoded archive metadata (not: data)"""

        try:
            archive_meta_orig = manifest.archives.get_raw_dict()[safe_encode(args.location.archive)]
        except KeyError:
            raise Archive.DoesNotExist(args.location.archive)

        indent = 4

        def do_indent(d):
            return textwrap.indent(json.dumps(d, indent=indent), prefix=' ' * indent)

        def output(fd):
            # this outputs megabytes of data for a modest sized archive, so some manual streaming json output
            fd.write('{\n')
            fd.write('    "_name": ' + json.dumps(args.location.archive) + ",\n")
            fd.write('    "_manifest_entry":\n')
            fd.write(do_indent(prepare_dump_dict(archive_meta_orig)))
            fd.write(',\n')

            data = key.decrypt(archive_meta_orig[b'id'], repository.get(archive_meta_orig[b'id']))
            archive_org_dict = msgpack.unpackb(data, object_hook=StableDict)

            fd.write('    "_meta":\n')
            fd.write(do_indent(prepare_dump_dict(archive_org_dict)))
            fd.write(',\n')
            fd.write('    "_items": [\n')

            unpacker = msgpack.Unpacker(use_list=False, object_hook=StableDict)
            first = True
            for item_id in archive_org_dict[b'items']:
                data = key.decrypt(item_id, repository.get(item_id))
                unpacker.feed(data)
                for item in unpacker:
                    item = prepare_dump_dict(item)
                    if first:
                        first = False
                    else:
                        fd.write(',\n')
                    fd.write(do_indent(item))

            fd.write('\n')
            fd.write('    ]\n}\n')

        with dash_open(args.path, 'w') as fd:
            output(fd)
        return EXIT_SUCCESS

    @with_repository(compatibility=Manifest.NO_OPERATION_CHECK)
    def do_debug_dump_manifest(self, args, repository, manifest, key):
        """dump decoded repository manifest"""

        data = key.decrypt(None, repository.get(manifest.MANIFEST_ID))

        meta = prepare_dump_dict(msgpack.unpackb(data, object_hook=StableDict))

        with dash_open(args.path, 'w') as fd:
            json.dump(meta, fd, indent=4)
        return EXIT_SUCCESS

    @with_repository(manifest=False)
    def do_debug_dump_repo_objs(self, args, repository):
        """dump (decrypted, decompressed) repo objects, repo index MUST be current/correct"""
        from .crypto.key import key_factory

        def decrypt_dump(i, id, cdata, tag=None, segment=None, offset=None):
            if cdata is not None:
                give_id = id if id != Manifest.MANIFEST_ID else None
                data = key.decrypt(give_id, cdata)
            else:
                data = b''
            tag_str = '' if tag is None else '_' + tag
            segment_str = '_' + str(segment) if segment is not None else ''
            offset_str = '_' + str(offset) if offset is not None else ''
            id_str = '_' + bin_to_hex(id) if id is not None else ''
            filename = '%08d%s%s%s%s.obj' % (i, segment_str, offset_str, tag_str, id_str)
            print('Dumping', filename)
            with open(filename, 'wb') as fd:
                fd.write(data)

        if args.ghost:
            # dump ghosty stuff from segment files: not yet committed objects, deleted / superceded objects, commit tags

            # set up the key without depending on a manifest obj
            for id, cdata, tag, segment, offset in repository.scan_low_level():
                if tag == TAG_PUT:
                    key = key_factory(repository, cdata)
                    break
            i = 0
            for id, cdata, tag, segment, offset in repository.scan_low_level():
                if tag == TAG_PUT:
                    decrypt_dump(i, id, cdata, tag='put', segment=segment, offset=offset)
                elif tag == TAG_DELETE:
                    decrypt_dump(i, id, None, tag='del', segment=segment, offset=offset)
                elif tag == TAG_COMMIT:
                    decrypt_dump(i, None, None, tag='commit', segment=segment, offset=offset)
                i += 1
        else:
            # set up the key without depending on a manifest obj
            ids = repository.list(limit=1, marker=None)
            cdata = repository.get(ids[0])
            key = key_factory(repository, cdata)
            marker = None
            i = 0
            while True:
                result = repository.scan(limit=LIST_SCAN_LIMIT, marker=marker)  # must use on-disk order scanning here
                if not result:
                    break
                marker = result[-1]
                for id in result:
                    cdata = repository.get(id)
                    decrypt_dump(i, id, cdata)
                    i += 1
        print('Done.')
        return EXIT_SUCCESS

    @with_repository(manifest=False)
    def do_debug_search_repo_objs(self, args, repository):
        """search for byte sequences in repo objects, repo index MUST be current/correct"""
        context = 32

        def print_finding(info, wanted, data, offset):
            before = data[offset - context:offset]
            after = data[offset + len(wanted):offset + len(wanted) + context]
            print('%s: %s %s %s == %r %r %r' % (info, before.hex(), wanted.hex(), after.hex(),
                                                before, wanted, after))

        wanted = args.wanted
        try:
            if wanted.startswith('hex:'):
                wanted = unhexlify(wanted[4:])
            elif wanted.startswith('str:'):
                wanted = wanted[4:].encode()
            else:
                raise ValueError('unsupported search term')
        except (ValueError, UnicodeEncodeError):
            wanted = None
        if not wanted:
            self.print_error('search term needs to be hex:123abc or str:foobar style')
            return EXIT_ERROR

        from .crypto.key import key_factory
        # set up the key without depending on a manifest obj
        ids = repository.list(limit=1, marker=None)
        cdata = repository.get(ids[0])
        key = key_factory(repository, cdata)

        marker = None
        last_data = b''
        last_id = None
        i = 0
        while True:
            result = repository.scan(limit=LIST_SCAN_LIMIT, marker=marker)  # must use on-disk order scanning here
            if not result:
                break
            marker = result[-1]
            for id in result:
                cdata = repository.get(id)
                give_id = id if id != Manifest.MANIFEST_ID else None
                data = key.decrypt(give_id, cdata)

                # try to locate wanted sequence crossing the border of last_data and data
                boundary_data = last_data[-(len(wanted) - 1):] + data[:len(wanted) - 1]
                if wanted in boundary_data:
                    boundary_data = last_data[-(len(wanted) - 1 + context):] + data[:len(wanted) - 1 + context]
                    offset = boundary_data.find(wanted)
                    info = '%d %s | %s' % (i, last_id.hex(), id.hex())
                    print_finding(info, wanted, boundary_data, offset)

                # try to locate wanted sequence in data
                count = data.count(wanted)
                if count:
                    offset = data.find(wanted)  # only determine first occurance's offset
                    info = "%d %s #%d" % (i, id.hex(), count)
                    print_finding(info, wanted, data, offset)

                last_id, last_data = id, data
                i += 1
                if i % 10000 == 0:
                    print('%d objects processed.' % i)
        print('Done.')
        return EXIT_SUCCESS

    @with_repository(manifest=False)
    def do_debug_get_obj(self, args, repository):
        """get object contents from the repository and write it into file"""
        hex_id = args.id
        try:
            id = unhexlify(hex_id)
        except ValueError:
            print("object id %s is invalid." % hex_id)
        else:
            try:
                data = repository.get(id)
            except Repository.ObjectNotFound:
                print("object %s not found." % hex_id)
            else:
                with open(args.path, "wb") as f:
                    f.write(data)
                print("object %s fetched." % hex_id)
        return EXIT_SUCCESS

    @with_repository(manifest=False, exclusive=True)
    def do_debug_put_obj(self, args, repository):
        """put file(s) contents into the repository"""
        for path in args.paths:
            with open(path, "rb") as f:
                data = f.read()
            h = hashlib.sha256(data)  # XXX hardcoded
            repository.put(h.digest(), data)
            print("object %s put." % h.hexdigest())
        repository.commit(compact=False)
        return EXIT_SUCCESS

    @with_repository(manifest=False, exclusive=True)
    def do_debug_delete_obj(self, args, repository):
        """delete the objects with the given IDs from the repo"""
        modified = False
        for hex_id in args.ids:
            try:
                id = unhexlify(hex_id)
            except ValueError:
                print("object id %s is invalid." % hex_id)
            else:
                try:
                    repository.delete(id)
                    modified = True
                    print("object %s deleted." % hex_id)
                except Repository.ObjectNotFound:
                    print("object %s not found." % hex_id)
        if modified:
            repository.commit(compact=False)
        print('Done.')
        return EXIT_SUCCESS

    @with_repository(manifest=False, exclusive=True, cache=True, compatibility=Manifest.NO_OPERATION_CHECK)
    def do_debug_refcount_obj(self, args, repository, manifest, key, cache):
        """display refcounts for the objects with the given IDs"""
        for hex_id in args.ids:
            try:
                id = unhexlify(hex_id)
            except ValueError:
                print("object id %s is invalid." % hex_id)
            else:
                try:
                    refcount = cache.chunks[id][0]
                    print("object %s has %d referrers [info from chunks cache]." % (hex_id, refcount))
                except KeyError:
                    print("object %s not found [info from chunks cache]." % hex_id)
        return EXIT_SUCCESS

    def do_debug_convert_profile(self, args):
        """convert Borg profile to Python profile"""
        import marshal
        with args.output, args.input:
            marshal.dump(msgpack.mp_unpack(args.input, use_list=False, raw=False), args.output)
        return EXIT_SUCCESS

    @with_repository(lock=False, manifest=False)
    def do_break_lock(self, args, repository):
        """Break the repository lock (e.g. in case it was left by a dead borg."""
        repository.break_lock()
        Cache.break_lock(repository)
        return self.exit_code

    helptext = collections.OrderedDict()
    helptext['patterns'] = textwrap.dedent('''
        The path/filenames used as input for the pattern matching start from the
        currently active recursion root. You usually give the recursion root(s)
        when invoking borg and these can be either relative or absolute paths.

        So, when you give `relative/` as root, the paths going into the matcher
        will look like `relative/.../file.ext`. When you give `/absolute/` as root,
        they will look like `/absolute/.../file.ext`. This is meant when we talk
        about "full path" below.

        File patterns support these styles: fnmatch, shell, regular expressions,
        path prefixes and path full-matches. By default, fnmatch is used for
        ``--exclude`` patterns and shell-style is used for the experimental ``--pattern``
        option.

        If followed by a colon (':') the first two characters of a pattern are used as a
        style selector. Explicit style selection is necessary when a
        non-default style is desired or when the desired pattern starts with
        two alphanumeric characters followed by a colon (i.e. `aa:something/*`).

        `Fnmatch <https://docs.python.org/3/library/fnmatch.html>`_, selector `fm:`
            This is the default style for ``--exclude`` and ``--exclude-from``.
            These patterns use a variant of shell pattern syntax, with '\\*' matching
            any number of characters, '?' matching any single character, '[...]'
            matching any single character specified, including ranges, and '[!...]'
            matching any character not specified. For the purpose of these patterns,
            the path separator (backslash for Windows and '/' on other systems) is not
            treated specially. Wrap meta-characters in brackets for a literal
            match (i.e. `[?]` to match the literal character `?`). For a path
            to match a pattern, the full path must match, or it must match
            from the start of the full path to just before a path separator. Except
            for the root path, paths will never end in the path separator when
            matching is attempted.  Thus, if a given pattern ends in a path
            separator, a '\\*' is appended before matching is attempted.

        Shell-style patterns, selector `sh:`
            This is the default style for ``--pattern`` and ``--patterns-from``.
            Like fnmatch patterns these are similar to shell patterns. The difference
            is that the pattern may include `**/` for matching zero or more directory
            levels, `*` for matching zero or more arbitrary characters with the
            exception of any path separator.

        Regular expressions, selector `re:`
            Regular expressions similar to those found in Perl are supported. Unlike
            shell patterns regular expressions are not required to match the full
            path and any substring match is sufficient. It is strongly recommended to
            anchor patterns to the start ('^'), to the end ('$') or both. Path
            separators (backslash for Windows and '/' on other systems) in paths are
            always normalized to a forward slash ('/') before applying a pattern. The
            regular expression syntax is described in the `Python documentation for
            the re module <https://docs.python.org/3/library/re.html>`_.

        Path prefix, selector `pp:`
            This pattern style is useful to match whole sub-directories. The pattern
            `pp:root/somedir` matches `root/somedir` and everything therein.

        Path full-match, selector `pf:`
            This pattern style is (only) useful to match full paths.
            This is kind of a pseudo pattern as it can not have any variable or
            unspecified parts - the full path must be given.
            `pf:root/file.ext` matches `root/file.txt` only.

            Implementation note: this is implemented via very time-efficient O(1)
            hashtable lookups (this means you can have huge amounts of such patterns
            without impacting performance much).
            Due to that, this kind of pattern does not respect any context or order.
            If you use such a pattern to include a file, it will always be included
            (if the directory recursion encounters it).
            Other include/exclude patterns that would normally match will be ignored.
            Same logic applies for exclude.

        .. note::

            `re:`, `sh:` and `fm:` patterns are all implemented on top of the Python SRE
            engine. It is very easy to formulate patterns for each of these types which
            requires an inordinate amount of time to match paths. If untrusted users
            are able to supply patterns, ensure they cannot supply `re:` patterns.
            Further, ensure that `sh:` and `fm:` patterns only contain a handful of
            wildcards at most.

        Exclusions can be passed via the command line option ``--exclude``. When used
        from within a shell the patterns should be quoted to protect them from
        expansion.

        The ``--exclude-from`` option permits loading exclusion patterns from a text
        file with one pattern per line. Lines empty or starting with the number sign
        ('#') after removing whitespace on both ends are ignored. The optional style
        selector prefix is also supported for patterns loaded from a file. Due to
        whitespace removal paths with whitespace at the beginning or end can only be
        excluded using regular expressions.

        Examples::

            # Exclude '/home/user/file.o' but not '/home/user/file.odt':
            $ borg create -e '*.o' backup /

            # Exclude '/home/user/junk' and '/home/user/subdir/junk' but
            # not '/home/user/importantjunk' or '/etc/junk':
            $ borg create -e '/home/*/junk' backup /

            # Exclude the contents of '/home/user/cache' but not the directory itself:
            $ borg create -e /home/user/cache/ backup /

            # The file '/home/user/cache/important' is *not* backed up:
            $ borg create -e /home/user/cache/ backup / /home/user/cache/important

            # The contents of directories in '/home' are not backed up when their name
            # ends in '.tmp'
            $ borg create --exclude 're:^/home/[^/]+\\.tmp/' backup /

            # Load exclusions from file
            $ cat >exclude.txt <<EOF
            # Comment line
            /home/*/junk
            *.tmp
            fm:aa:something/*
            re:^/home/[^/]\\.tmp/
            sh:/home/*/.thumbnails
            EOF
            $ borg create --exclude-from exclude.txt backup /

        .. container:: experimental

            A more general and easier to use way to define filename matching patterns exists
            with the experimental ``--pattern`` and ``--patterns-from`` options. Using these, you
            may specify the backup roots (starting points) and patterns for inclusion/exclusion.
            A root path starts with the prefix `R`, followed by a path (a plain path, not a
            file pattern). An include rule starts with the prefix +, an exclude rule starts
            with the prefix -, an exclude-norecurse rule starts with !, all followed by a pattern.
            Inclusion patterns are useful to include paths that are contained in an excluded
            path. The first matching pattern is used so if an include pattern matches before
            an exclude pattern, the file is backed up. If an exclude-norecurse pattern matches
            a directory, it won't recurse into it and won't discover any potential matches for
            include rules below that directory.

            Note that the default pattern style for ``--pattern`` and ``--patterns-from`` is
            shell style (`sh:`), so those patterns behave similar to rsync include/exclude
            patterns. The pattern style can be set via the `P` prefix.

            Patterns (``--pattern``) and excludes (``--exclude``) from the command line are
            considered first (in the order of appearance). Then patterns from ``--patterns-from``
            are added. Exclusion patterns from ``--exclude-from`` files are appended last.

            Examples::

                # backup pics, but not the ones from 2018, except the good ones:
                # note: using = is essential to avoid cmdline argument parsing issues.
                borg create --pattern=+pics/2018/good --pattern=-pics/2018 repo::arch pics

                # use a file with patterns:
                borg create --patterns-from patterns.lst repo::arch

            The patterns.lst file could look like that::

                # "sh:" pattern style is the default, so the following line is not needed:
                P sh
                R /
                # can be rebuild
                - /home/*/.cache
                # they're downloads for a reason
                - /home/*/Downloads
                # susan is a nice person
                # include susans home
                + /home/susan
                # don't backup the other home directories
                - /home/*\n\n''')
    helptext['placeholders'] = textwrap.dedent('''
        Repository (or Archive) URLs, ``--prefix``, ``--glob-archives`` and ``--remote-path``
        values support these placeholders:

        {hostname}
            The (short) hostname of the machine.

        {fqdn}
            The full name of the machine.

        {reverse-fqdn}
            The full name of the machine in reverse domain name notation.

        {now}
            The current local date and time, by default in ISO-8601 format.
            You can also supply your own `format string <https://docs.python.org/3.5/library/datetime.html#strftime-and-strptime-behavior>`_, e.g. {now:%Y-%m-%d_%H:%M:%S}

        {utcnow}
            The current UTC date and time, by default in ISO-8601 format.
            You can also supply your own `format string <https://docs.python.org/3.5/library/datetime.html#strftime-and-strptime-behavior>`_, e.g. {utcnow:%Y-%m-%d_%H:%M:%S}

        {user}
            The user name (or UID, if no name is available) of the user running borg.

        {pid}
            The current process ID.

        {borgversion}
            The version of borg, e.g.: 1.0.8rc1

        {borgmajor}
            The version of borg, only the major version, e.g.: 1

        {borgminor}
            The version of borg, only major and minor version, e.g.: 1.0

        {borgpatch}
            The version of borg, only major, minor and patch version, e.g.: 1.0.8

        If literal curly braces need to be used, double them for escaping::

            borg create /path/to/repo::{{literal_text}}

        Examples::

            borg create /path/to/repo::{hostname}-{user}-{utcnow} ...
            borg create /path/to/repo::{hostname}-{now:%Y-%m-%d_%H:%M:%S} ...
            borg prune --prefix '{hostname}-' ...

        .. note::
            systemd uses a difficult, non-standard syntax for command lines in unit files (refer to
            the `systemd.unit(5)` manual page).

            When invoking borg from unit files, pay particular attention to escaping,
            especially when using the now/utcnow placeholders, since systemd performs its own
            %-based variable replacement even in quoted text. To avoid interference from systemd,
            double all percent signs (``{hostname}-{now:%Y-%m-%d_%H:%M:%S}``
            becomes ``{hostname}-{now:%%Y-%%m-%%d_%%H:%%M:%%S}``).\n\n''')
    helptext['compression'] = textwrap.dedent('''
        It is no problem to mix different compression methods in one repo,
        deduplication is done on the source data chunks (not on the compressed
        or encrypted data).

        If some specific chunk was once compressed and stored into the repo, creating
        another backup that also uses this chunk will not change the stored chunk.
        So if you use different compression specs for the backups, whichever stores a
        chunk first determines its compression. See also borg recreate.

        Compression is lz4 by default. If you want something else, you have to specify what you want.

        Valid compression specifiers are:

        none
            Do not compress.

        lz4
            Use lz4 compression. Very high speed, very low compression. (default)

        zstd[,L]
            Use zstd ("zstandard") compression, a modern wide-range algorithm.
            If you do not explicitly give the compression level L (ranging from 1
            to 22), it will use level 3.
            Archives compressed with zstd are not compatible with borg < 1.1.4.

        zlib[,L]
            Use zlib ("gz") compression. Medium speed, medium compression.
            If you do not explicitly give the compression level L (ranging from 0
            to 9), it will use level 6.
            Giving level 0 (means "no compression", but still has zlib protocol
            overhead) is usually pointless, you better use "none" compression.

        lzma[,L]
            Use lzma ("xz") compression. Low speed, high compression.
            If you do not explicitly give the compression level L (ranging from 0
            to 9), it will use level 6.
            Giving levels above 6 is pointless and counterproductive because it does
            not compress better due to the buffer size used by borg - but it wastes
            lots of CPU cycles and RAM.

        auto,C[,L]
            Use a built-in heuristic to decide per chunk whether to compress or not.
            The heuristic tries with lz4 whether the data is compressible.
            For incompressible data, it will not use compression (uses "none").
            For compressible data, it uses the given C[,L] compression - with C[,L]
            being any valid compression specifier.

        Examples::

            borg create --compression lz4 REPO::ARCHIVE data
            borg create --compression zstd REPO::ARCHIVE data
            borg create --compression zstd,10 REPO::ARCHIVE data
            borg create --compression zlib REPO::ARCHIVE data
            borg create --compression zlib,1 REPO::ARCHIVE data
            borg create --compression auto,lzma,6 REPO::ARCHIVE data
            borg create --compression auto,lzma ...\n\n''')

    def do_help(self, parser, commands, args):
        if not args.topic:
            parser.print_help()
        elif args.topic in self.helptext:
            print(rst_to_terminal(self.helptext[args.topic]))
        elif args.topic in commands:
            if args.epilog_only:
                print(commands[args.topic].epilog)
            elif args.usage_only:
                commands[args.topic].epilog = None
                commands[args.topic].print_help()
            else:
                commands[args.topic].print_help()
        else:
            msg_lines = []
            msg_lines += ['No help available on %s.' % args.topic]
            msg_lines += ['Try one of the following:']
            msg_lines += ['    Commands: %s' % ', '.join(sorted(commands.keys()))]
            msg_lines += ['    Topics: %s' % ', '.join(sorted(self.helptext.keys()))]
            parser.error('\n'.join(msg_lines))
        return self.exit_code

    def do_subcommand_help(self, parser, args):
        """display infos about subcommand"""
        parser.print_help()
        return EXIT_SUCCESS

    do_maincommand_help = do_subcommand_help

    def preprocess_args(self, args):
        deprecations = [
            # ('--old', '--new' or None, 'Warning: "--old" has been deprecated. Use "--new" instead.'),
        ]
        for i, arg in enumerate(args[:]):
            for old_name, new_name, warning in deprecations:
                if arg.startswith(old_name):
                    if new_name is not None:
                        args[i] = arg.replace(old_name, new_name)
                    print(warning, file=sys.stderr)
        return args

    class CommonOptions:
        """
        Support class to allow specifying common options directly after the top-level command.

        Normally options can only be specified on the parser defining them, which means
        that generally speaking *all* options go after all sub-commands. This is annoying
        for common options in scripts, e.g. --remote-path or logging options.

        This class allows adding the same set of options to both the top-level parser
        and the final sub-command parsers (but not intermediary sub-commands, at least for now).

        It does so by giving every option's target name ("dest") a suffix indicating its level
        -- no two options in the parser hierarchy can have the same target --
        then, after parsing the command line, multiple definitions are resolved.

        Defaults are handled by only setting them on the top-level parser and setting
        a sentinel object in all sub-parsers, which then allows one to discern which parser
        supplied the option.
        """

        def __init__(self, define_common_options, suffix_precedence):
            """
            *define_common_options* should be a callable taking one argument, which
            will be a argparse.Parser.add_argument-like function.

            *define_common_options* will be called multiple times, and should call
            the passed function to define common options exactly the same way each time.

            *suffix_precedence* should be a tuple of the suffixes that will be used.
            It is ordered from lowest precedence to highest precedence:
            An option specified on the parser belonging to index 0 is overridden if the
            same option is specified on any parser with a higher index.
            """
            self.define_common_options = define_common_options
            self.suffix_precedence = suffix_precedence

            # Maps suffixes to sets of target names.
            # E.g. common_options["_subcommand"] = {..., "log_level", ...}
            self.common_options = dict()
            # Set of options with the 'append' action.
            self.append_options = set()
            # This is the sentinel object that replaces all default values in parsers
            # below the top-level parser.
            self.default_sentinel = object()

        def add_common_group(self, parser, suffix, provide_defaults=False):
            """
            Add common options to *parser*.

            *provide_defaults* must only be True exactly once in a parser hierarchy,
            at the top level, and False on all lower levels. The default is chosen
            accordingly.

            *suffix* indicates the suffix to use internally. It also indicates
            which precedence the *parser* has for common options. See *suffix_precedence*
            of __init__.
            """
            assert suffix in self.suffix_precedence

            def add_argument(*args, **kwargs):
                if 'dest' in kwargs:
                    kwargs.setdefault('action', 'store')
                    assert kwargs['action'] in ('help', 'store_const', 'store_true', 'store_false', 'store', 'append')
                    is_append = kwargs['action'] == 'append'
                    if is_append:
                        self.append_options.add(kwargs['dest'])
                        assert kwargs['default'] == [], 'The default is explicitly constructed as an empty list in resolve()'
                    else:
                        self.common_options.setdefault(suffix, set()).add(kwargs['dest'])
                    kwargs['dest'] += suffix
                    if not provide_defaults:
                        # Interpolate help now, in case the %(default)d (or so) is mentioned,
                        # to avoid producing incorrect help output.
                        # Assumption: Interpolated output can safely be interpolated again,
                        # which should always be the case.
                        # Note: We control all inputs.
                        kwargs['help'] = kwargs['help'] % kwargs
                        if not is_append:
                            kwargs['default'] = self.default_sentinel

                common_group.add_argument(*args, **kwargs)

            common_group = parser.add_argument_group('Common options')
            self.define_common_options(add_argument)

        def resolve(self, args: argparse.Namespace):  # Namespace has "in" but otherwise is not like a dict.
            """
            Resolve the multiple definitions of each common option to the final value.
            """
            for suffix in self.suffix_precedence:
                # From highest level to lowest level, so the "most-specific" option wins, e.g.
                # "borg --debug create --info" shall result in --info being effective.
                for dest in self.common_options.get(suffix, []):
                    # map_from is this suffix' option name, e.g. log_level_subcommand
                    # map_to is the target name, e.g. log_level
                    map_from = dest + suffix
                    map_to = dest
                    # Retrieve value; depending on the action it may not exist, but usually does
                    # (store_const/store_true/store_false), either because the action implied a default
                    # or a default is explicitly supplied.
                    # Note that defaults on lower levels are replaced with default_sentinel.
                    # Only the top level has defaults.
                    value = getattr(args, map_from, self.default_sentinel)
                    if value is not self.default_sentinel:
                        # value was indeed specified on this level. Transfer value to target,
                        # and un-clobber the args (for tidiness - you *cannot* use the suffixed
                        # names for other purposes, obviously).
                        setattr(args, map_to, value)
                    try:
                        delattr(args, map_from)
                    except AttributeError:
                        pass

            # Options with an "append" action need some special treatment. Instead of
            # overriding values, all specified values are merged together.
            for dest in self.append_options:
                option_value = []
                for suffix in self.suffix_precedence:
                    # Find values of this suffix, if any, and add them to the final list
                    extend_from = dest + suffix
                    if extend_from in args:
                        values = getattr(args, extend_from)
                        delattr(args, extend_from)
                        option_value.extend(values)
                setattr(args, dest, option_value)

    def build_parser(self):
        # You can use :ref:`xyz` in the following usage pages. However, for plain-text view,
        # e.g. through "borg ... --help", define a substitution for the reference here.
        # It will replace the entire :ref:`foo` verbatim.
        rst_plain_text_references = {
            'a_status_oddity': '"I am seeing ‘A’ (added) status for a unchanged file!?"',
            'separate_compaction': '"Separate compaction"',
        }

        def process_epilog(epilog):
            epilog = textwrap.dedent(epilog).splitlines()
            try:
                mode = borg.doc_mode
            except AttributeError:
                mode = 'command-line'
            if mode in ('command-line', 'build_usage'):
                epilog = [line for line in epilog if not line.startswith('.. man')]
            epilog = '\n'.join(epilog)
            if mode == 'command-line':
                epilog = rst_to_terminal(epilog, rst_plain_text_references)
            return epilog

        def define_common_options(add_common_option):
            add_common_option('-h', '--help', action='help', help='show this help message and exit')
            add_common_option('--critical', dest='log_level',
                              action='store_const', const='critical', default='warning',
                              help='work on log level CRITICAL')
            add_common_option('--error', dest='log_level',
                              action='store_const', const='error', default='warning',
                              help='work on log level ERROR')
            add_common_option('--warning', dest='log_level',
                              action='store_const', const='warning', default='warning',
                              help='work on log level WARNING (default)')
            add_common_option('--info', '-v', '--verbose', dest='log_level',
                              action='store_const', const='info', default='warning',
                              help='work on log level INFO')
            add_common_option('--debug', dest='log_level',
                              action='store_const', const='debug', default='warning',
                              help='enable debug output, work on log level DEBUG')
            add_common_option('--debug-topic', metavar='TOPIC', dest='debug_topics', action='append', default=[],
                              help='enable TOPIC debugging (can be specified multiple times). '
                                   'The logger path is borg.debug.<TOPIC> if TOPIC is not fully qualified.')
            add_common_option('-p', '--progress', dest='progress', action='store_true',
                              help='show progress information')
            add_common_option('--log-json', dest='log_json', action='store_true',
                              help='Output one JSON object per log line instead of formatted text.')
            add_common_option('--lock-wait', metavar='SECONDS', dest='lock_wait', type=int, default=1,
                              help='wait at most SECONDS for acquiring a repository/cache lock (default: %(default)d).')
            add_common_option('--show-version', dest='show_version', action='store_true',
                              help='show/log the borg version')
            add_common_option('--show-rc', dest='show_rc', action='store_true',
                              help='show/log the return code (rc)')
            add_common_option('--umask', metavar='M', dest='umask', type=lambda s: int(s, 8), default=UMASK_DEFAULT,
                              help='set umask to M (local and remote, default: %(default)04o)')
            add_common_option('--remote-path', metavar='PATH', dest='remote_path',
                              help='use PATH as borg executable on the remote (default: "borg")')
            add_common_option('--remote-ratelimit', metavar='RATE', dest='remote_ratelimit', type=int,
                              help='set remote network upload rate limit in kiByte/s (default: 0=unlimited)')
            add_common_option('--consider-part-files', dest='consider_part_files', action='store_true',
                              help='treat part files like normal files (e.g. to list/extract them)')
            add_common_option('--debug-profile', metavar='FILE', dest='debug_profile', default=None,
                              help='Write execution profile in Borg format into FILE. For local use a Python-'
                                   'compatible file can be generated by suffixing FILE with ".pyprof".')
            add_common_option('--rsh', metavar='RSH', dest='rsh',
                              help="Use this command to connect to the 'borg serve' process (default: 'ssh')")

        def define_exclude_and_patterns(add_option, *, tag_files=False, strip_components=False):
            add_option('-e', '--exclude', metavar='PATTERN', dest='patterns',
                       type=parse_exclude_pattern, action='append',
                       help='exclude paths matching PATTERN')
            add_option('--exclude-from', metavar='EXCLUDEFILE', action=ArgparseExcludeFileAction,
                       help='read exclude patterns from EXCLUDEFILE, one per line')
            add_option('--pattern', metavar='PATTERN', action=ArgparsePatternAction,
                       help='experimental: include/exclude paths matching PATTERN')
            add_option('--patterns-from', metavar='PATTERNFILE', action=ArgparsePatternFileAction,
                       help='experimental: read include/exclude patterns from PATTERNFILE, one per line')

            if tag_files:
                add_option('--exclude-caches', dest='exclude_caches', action='store_true',
                           help='exclude directories that contain a CACHEDIR.TAG file '
                                '(http://www.bford.info/cachedir/spec.html)')
                add_option('--exclude-if-present', metavar='NAME', dest='exclude_if_present',
                           action='append', type=str,
                           help='exclude directories that are tagged by containing a filesystem object with '
                                'the given NAME')
                add_option('--keep-exclude-tags', dest='keep_exclude_tags',
                           action='store_true',
                           help='if tag objects are specified with ``--exclude-if-present``, '
                                'don\'t omit the tag objects themselves from the backup archive')

            if strip_components:
                add_option('--strip-components', metavar='NUMBER', dest='strip_components', type=int, default=0,
                           help='Remove the specified number of leading path elements. '
                                'Paths with fewer elements will be silently skipped.')

        def define_exclusion_group(subparser, **kwargs):
            exclude_group = subparser.add_argument_group('Exclusion options')
            define_exclude_and_patterns(exclude_group.add_argument, **kwargs)
            return exclude_group

        def define_archive_filters_group(subparser, *, sort_by=True, first_last=True):
            filters_group = subparser.add_argument_group('Archive filters',
                                                         'Archive filters can be applied to repository targets.')
            group = filters_group.add_mutually_exclusive_group()
            group.add_argument('-P', '--prefix', metavar='PREFIX', dest='prefix', type=PrefixSpec, default='',
                               help='only consider archive names starting with this prefix.')
            group.add_argument('-a', '--glob-archives', metavar='GLOB', dest='glob_archives',
                               type=GlobSpec, default=None,
                               help='only consider archive names matching the glob. '
                                    'sh: rules apply, see "borg help patterns". '
                                    '``--prefix`` and ``--glob-archives`` are mutually exclusive.')

            if sort_by:
                sort_by_default = 'timestamp'
                filters_group.add_argument('--sort-by', metavar='KEYS', dest='sort_by',
                                           type=SortBySpec, default=sort_by_default,
                                           help='Comma-separated list of sorting keys; valid keys are: {}; default is: {}'
                                           .format(', '.join(AI_HUMAN_SORT_KEYS), sort_by_default))

            if first_last:
                group = filters_group.add_mutually_exclusive_group()
                group.add_argument('--first', metavar='N', dest='first', default=0, type=positive_int_validator,
                                   help='consider first N archives after other filters were applied')
                group.add_argument('--last', metavar='N', dest='last', default=0, type=positive_int_validator,
                                   help='consider last N archives after other filters were applied')

        parser = argparse.ArgumentParser(prog=self.prog, description='Borg - Deduplicated Backups',
                                         add_help=False)
        # paths and patterns must have an empty list as default everywhere
        parser.set_defaults(fallback2_func=functools.partial(self.do_maincommand_help, parser),
                            paths=[], patterns=[])
        parser.common_options = self.CommonOptions(define_common_options,
                                                   suffix_precedence=('_maincommand', '_midcommand', '_subcommand'))
        parser.add_argument('-V', '--version', action='version', version='%(prog)s ' + __version__,
                            help='show version number and exit')
        parser.common_options.add_common_group(parser, '_maincommand', provide_defaults=True)

        common_parser = argparse.ArgumentParser(add_help=False, prog=self.prog)
        common_parser.set_defaults(paths=[], patterns=[])
        parser.common_options.add_common_group(common_parser, '_subcommand')

        mid_common_parser = argparse.ArgumentParser(add_help=False, prog=self.prog)
        mid_common_parser.set_defaults(paths=[], patterns=[])
        parser.common_options.add_common_group(mid_common_parser, '_midcommand')

        subparsers = parser.add_subparsers(title='required arguments', metavar='<command>')

        # borg benchmark
        benchmark_epilog = process_epilog("These commands do various benchmarks.")

        subparser = subparsers.add_parser('benchmark', parents=[mid_common_parser], add_help=False,
                                          description='benchmark command',
                                          epilog=benchmark_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='benchmark command')

        benchmark_parsers = subparser.add_subparsers(title='required arguments', metavar='<command>')
        subparser.set_defaults(fallback_func=functools.partial(self.do_subcommand_help, subparser))

        bench_crud_epilog = process_epilog("""
        This command benchmarks borg CRUD (create, read, update, delete) operations.

        It creates input data below the given PATH and backups this data into the given REPO.
        The REPO must already exist (it could be a fresh empty repo or an existing repo, the
        command will create / read / update / delete some archives named borg-benchmark-crud\\* there.

        Make sure you have free space there, you'll need about 1GB each (+ overhead).

        If your repository is encrypted and borg needs a passphrase to unlock the key, use:

        BORG_PASSPHRASE=mysecret borg benchmark crud REPO PATH

        Measurements are done with different input file sizes and counts.
        The file contents are very artificial (either all zero or all random),
        thus the measurement results do not necessarily reflect performance with real data.
        Also, due to the kind of content used, no compression is used in these benchmarks.

        C- == borg create (1st archive creation, no compression, do not use files cache)
              C-Z- == all-zero files. full dedup, this is primarily measuring reader/chunker/hasher.
              C-R- == random files. no dedup, measuring throughput through all processing stages.

        R- == borg extract (extract archive, dry-run, do everything, but do not write files to disk)
              R-Z- == all zero files. Measuring heavily duplicated files.
              R-R- == random files. No duplication here, measuring throughput through all processing
              stages, except writing to disk.

        U- == borg create (2nd archive creation of unchanged input files, measure files cache speed)
              The throughput value is kind of virtual here, it does not actually read the file.
              U-Z- == needs to check the 2 all-zero chunks' existence in the repo.
              U-R- == needs to check existence of a lot of different chunks in the repo.

        D- == borg delete archive (delete last remaining archive, measure deletion + compaction)
              D-Z- == few chunks to delete / few segments to compact/remove.
              D-R- == many chunks to delete / many segments to compact/remove.

        Please note that there might be quite some variance in these measurements.
        Try multiple measurements and having a otherwise idle machine (and network, if you use it).
        """)
        subparser = benchmark_parsers.add_parser('crud', parents=[common_parser], add_help=False,
                                                 description=self.do_benchmark_crud.__doc__,
                                                 epilog=bench_crud_epilog,
                                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                                 help='benchmarks borg CRUD (create, extract, update, delete).')
        subparser.set_defaults(func=self.do_benchmark_crud)

        subparser.add_argument('location', metavar='REPO',
                               type=location_validator(archive=False),
                               help='repo to use for benchmark (must exist)')

        subparser.add_argument('path', metavar='PATH', help='path were to create benchmark input data')

        # borg break-lock
        break_lock_epilog = process_epilog("""
        This command breaks the repository and cache locks.
        Please use carefully and only while no borg process (on any machine) is
        trying to access the Cache or the Repository.
        """)
        subparser = subparsers.add_parser('break-lock', parents=[common_parser], add_help=False,
                                          description=self.do_break_lock.__doc__,
                                          epilog=break_lock_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='break repository and cache locks')
        subparser.set_defaults(func=self.do_break_lock)
        subparser.add_argument('location', metavar='REPOSITORY', nargs='?', default='',
                               type=location_validator(archive=False),
                               help='repository for which to break the locks')

        # borg check
        check_epilog = process_epilog("""
        The check command verifies the consistency of a repository and the corresponding archives.

        First, the underlying repository data files are checked:

        - For all segments the segment magic (header) is checked
        - For all objects stored in the segments, all metadata (e.g. crc and size) and
          all data is read. The read data is checked by size and CRC. Bit rot and other
          types of accidental damage can be detected this way.
        - If we are in repair mode and a integrity error is detected for a segment,
          we try to recover as many objects from the segment as possible.
        - In repair mode, it makes sure that the index is consistent with the data
          stored in the segments.
        - If you use a remote repo server via ssh:, the repo check is executed on the
          repo server without causing significant network traffic.
        - The repository check can be skipped using the ``--archives-only`` option.
        - A repository check can be time consuming. Partial checks are possible with the ``--max-duration`` option.

        Second, the consistency and correctness of the archive metadata is verified:

        - Is the repo manifest present? If not, it is rebuilt from archive metadata
          chunks (this requires reading and decrypting of all metadata and data).
        - Check if archive metadata chunk is present. if not, remove archive from
          manifest.
        - For all files (items) in the archive, for all chunks referenced by these
          files, check if chunk is present.
          If a chunk is not present and we are in repair mode, replace it with a same-size
          replacement chunk of zeros.
          If a previously lost chunk reappears (e.g. via a later backup) and we are in
          repair mode, the all-zero replacement chunk will be replaced by the correct chunk.
          This requires reading of archive and file metadata, but not data.
        - If we are in repair mode and we checked all the archives: delete orphaned
          chunks from the repo.
        - if you use a remote repo server via ssh:, the archive check is executed on
          the client machine (because if encryption is enabled, the checks will require
          decryption and this is always done client-side, because key access will be
          required).
        - The archive checks can be time consuming, they can be skipped using the
          ``--repository-only`` option.

        The ``--max-duration`` option can be used to split a long-running repository check into multiple partial checks.
        After the given number of seconds the check is interrupted. The next partial check will continue where the
        previous one stopped, until the complete repository has been checked. Example: Assuming a full check took 7
        hours, then running a daily check with --max-duration=3600 (1 hour) would result in one full check per week.

        Attention: Partial checks can only do way less checks than a full check (only the CRC32 checks on segment file
        entries are done) and cannot be combined with ``--repair``. Partial checks may therefore be useful only with very
        large repositories where a full check would take too long. Doing a full repository check aborts a partial check;
        the next partial check will start from the beginning.

        The ``--verify-data`` option will perform a full integrity verification (as opposed to
        checking the CRC32 of the segment) of data, which means reading the data from the
        repository, decrypting and decompressing it. This is a cryptographic verification,
        which will detect (accidental) corruption. For encrypted repositories it is
        tamper-resistant as well, unless the attacker has access to the keys.

        It is also very slow.
        """)
        subparser = subparsers.add_parser('check', parents=[common_parser], add_help=False,
                                          description=self.do_check.__doc__,
                                          epilog=check_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='verify repository')
        subparser.set_defaults(func=self.do_check)
        subparser.add_argument('location', metavar='REPOSITORY_OR_ARCHIVE', nargs='?', default='',
                               type=location_validator(),
                               help='repository or archive to check consistency of')
        subparser.add_argument('--repository-only', dest='repo_only', action='store_true',
                               help='only perform repository checks')
        subparser.add_argument('--archives-only', dest='archives_only', action='store_true',
                               help='only perform archives checks')
        subparser.add_argument('--verify-data', dest='verify_data', action='store_true',
                               help='perform cryptographic archive data integrity verification '
                                    '(conflicts with ``--repository-only``)')
        subparser.add_argument('--repair', dest='repair', action='store_true',
                               help='attempt to repair any inconsistencies found')
        subparser.add_argument('--save-space', dest='save_space', action='store_true',
                               help='work slower, but using less space')
        subparser.add_argument('--max-duration', metavar='SECONDS', dest='max_duration',
                                   type=int, default=0,
                                   help='do only a partial repo check for max. SECONDS seconds (Default: unlimited)')
        define_archive_filters_group(subparser)

        # borg compact
        compact_epilog = process_epilog("""
        This command frees repository space by compacting segments.

        Use this regularly to avoid running out of space - you do not need to use this
        after each borg command though. It is especially useful after deleting archives,
        because only compaction will really free repository space.

        borg compact does not need a key, so it is possible to invoke it from the
        client or also from the server.

        Depending on the amount of segments that need compaction, it may take a while,
        so consider using the ``--progress`` option.

        When using ``--verbose``, borg will output an estimate of the freed space.

        After upgrading borg (server) to 1.2+, you can use ``borg compact --cleanup-commits``
        to clean up the numerous 17byte commit-only segments that borg 1.1 did not clean up
        due to a bug. It is enough to do that once per repository.

        See :ref:`separate_compaction` in Additional Notes for more details.
        """)
        subparser = subparsers.add_parser('compact', parents=[common_parser], add_help=False,
                                          description=self.do_compact.__doc__,
                                          epilog=compact_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='compact segment files / free space in repo')
        subparser.set_defaults(func=self.do_compact)
        subparser.add_argument('location', metavar='REPOSITORY',
                               type=location_validator(archive=False),
                               help='repository to compact')
        subparser.add_argument('--cleanup-commits', dest='cleanup_commits', action='store_true',
                               help='cleanup commit-only 17-byte segment files')

        # borg config
        config_epilog = process_epilog("""
        This command gets and sets options in a local repository or cache config file.
        For security reasons, this command only works on local repositories.

        To delete a config value entirely, use ``--delete``. To list the values
        of the configuration file or the default values, use ``--list``.  To get and existing
        key, pass only the key name. To set a key, pass both the key name and
        the new value. Keys can be specified in the format "section.name" or
        simply "name"; the section will default to "repository" and "cache" for
        the repo and cache configs, respectively.


        By default, borg config manipulates the repository config file. Using ``--cache``
        edits the repository cache's config file instead.
        """)
        subparser = subparsers.add_parser('config', parents=[common_parser], add_help=False,
                                          description=self.do_config.__doc__,
                                          epilog=config_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='get and set configuration values')
        subparser.set_defaults(func=self.do_config)
        subparser.add_argument('-c', '--cache', dest='cache', action='store_true',
                               help='get and set values from the repo cache')

        group = subparser.add_mutually_exclusive_group()
        group.add_argument('-d', '--delete', dest='delete', action='store_true',
                               help='delete the key from the config file')
        group.add_argument('-l', '--list', dest='list', action='store_true',
                               help='list the configuration of the repo')

        subparser.add_argument('location', metavar='REPOSITORY',
                               type=location_validator(archive=False, proto='file'),
                               help='repository to configure')
        subparser.add_argument('name', metavar='NAME', nargs='?',
                               help='name of config key')
        subparser.add_argument('value', metavar='VALUE', nargs='?',
                               help='new value for key')

        # borg create
        create_epilog = process_epilog("""
        This command creates a backup archive containing all files found while recursively
        traversing all paths specified. Paths are added to the archive as they are given,
        that means if relative paths are desired, the command has to be run from the correct
        directory.

        When giving '-' as path, borg will read data from standard input and create a
        file 'stdin' in the created archive from that data.

        The archive will consume almost no disk space for files or parts of files that
        have already been stored in other archives.

        The archive name needs to be unique. It must not end in '.checkpoint' or
        '.checkpoint.N' (with N being a number), because these names are used for
        checkpoints and treated in special ways.

        In the archive name, you may use the following placeholders:
        {now}, {utcnow}, {fqdn}, {hostname}, {user} and some others.

        Backup speed is increased by not reprocessing files that are already part of
        existing archives and weren't modified. The detection of unmodified files is
        done by comparing multiple file metadata values with previous values kept in
        the files cache.

        This comparison can operate in different modes as given by ``--files-cache``:

        - ctime,size,inode (default)
        - mtime,size,inode (default behaviour of borg versions older than 1.1.0rc4)
        - ctime,size (ignore the inode number)
        - mtime,size (ignore the inode number)
        - rechunk,ctime (all files are considered modified - rechunk, cache ctime)
        - rechunk,mtime (all files are considered modified - rechunk, cache mtime)
        - disabled (disable the files cache, all files considered modified - rechunk)

        inode number: better safety, but often unstable on network filesystems

        Normally, detecting file modifications will take inode information into
        consideration to improve the reliability of file change detection.
        This is problematic for files located on sshfs and similar network file
        systems which do not provide stable inode numbers, such files will always
        be considered modified. You can use modes without `inode` in this case to
        improve performance, but reliability of change detection might be reduced.

        ctime vs. mtime: safety vs. speed

        - ctime is a rather safe way to detect changes to a file (metadata and contents)
          as it can not be set from userspace. But, a metadata-only change will already
          update the ctime, so there might be some unnecessary chunking/hashing even
          without content changes. Some filesystems do not support ctime (change time).
        - mtime usually works and only updates if file contents were changed. But mtime
          can be arbitrarily set from userspace, e.g. to set mtime back to the same value
          it had before a content change happened. This can be used maliciously as well as
          well-meant, but in both cases mtime based cache modes can be problematic.

        The mount points of filesystems or filesystem snapshots should be the same for every
        creation of a new archive to ensure fast operation. This is because the file cache that
        is used to determine changed files quickly uses absolute filenames.
        If this is not possible, consider creating a bind mount to a stable location.

        The ``--progress`` option shows (from left to right) Original, Compressed and Deduplicated
        (O, C and D, respectively), then the Number of files (N) processed so far, followed by
        the currently processed path.

        When using ``--stats``, you will get some statistics about how much data was
        added - the "This Archive" deduplicated size there is most interesting as that is
        how much your repository will grow. Please note that the "All archives" stats refer to
        the state after creation. Also, the ``--stats`` and ``--dry-run`` options are mutually
        exclusive because the data is not actually compressed and deduplicated during a dry run.

        See the output of the "borg help patterns" command for more help on exclude patterns.
        See the output of the "borg help placeholders" command for more help on placeholders.

        .. man NOTES

        The ``--exclude`` patterns are not like tar. In tar ``--exclude`` .bundler/gems will
        exclude foo/.bundler/gems. In borg it will not, you need to use ``--exclude``
        '\\*/.bundler/gems' to get the same effect. See ``borg help patterns`` for
        more information.

        In addition to using ``--exclude`` patterns, it is possible to use
        ``--exclude-if-present`` to specify the name of a filesystem object (e.g. a file
        or folder name) which, when contained within another folder, will prevent the
        containing folder from being backed up.  By default, the containing folder and
        all of its contents will be omitted from the backup.  If, however, you wish to
        only include the objects specified by ``--exclude-if-present`` in your backup,
        and not include any other contents of the containing folder, this can be enabled
        through using the ``--keep-exclude-tags`` option.

        Item flags
        ++++++++++

        ``--list`` outputs a list of all files, directories and other
        file system items it considered (no matter whether they had content changes
        or not). For each item, it prefixes a single-letter flag that indicates type
        and/or status of the item.

        If you are interested only in a subset of that output, you can give e.g.
        ``--filter=AME`` and it will only show regular files with A, M or E status (see
        below).

        A uppercase character represents the status of a regular file relative to the
        "files" cache (not relative to the repo -- this is an issue if the files cache
        is not used). Metadata is stored in any case and for 'A' and 'M' also new data
        chunks are stored. For 'U' all data chunks refer to already existing chunks.

        - 'A' = regular file, added (see also :ref:`a_status_oddity` in the FAQ)
        - 'M' = regular file, modified
        - 'U' = regular file, unchanged
        - 'C' = regular file, it changed while we backed it up
        - 'E' = regular file, an error happened while accessing/reading *this* file

        A lowercase character means a file type other than a regular file,
        borg usually just stores their metadata:

        - 'd' = directory
        - 'b' = block device
        - 'c' = char device
        - 'h' = regular file, hardlink (to already seen inodes)
        - 's' = symlink
        - 'f' = fifo

        Other flags used include:

        - 'i' = backup data was read from standard input (stdin)
        - '-' = dry run, item was *not* backed up
        - 'x' = excluded, item was *not* backed up
        - '?' = missing status code (if you see this, please file a bug report!)
        """)

        subparser = subparsers.add_parser('create', parents=[common_parser], add_help=False,
                                          description=self.do_create.__doc__,
                                          epilog=create_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='create backup')
        subparser.set_defaults(func=self.do_create)

        dryrun_group = subparser.add_mutually_exclusive_group()
        dryrun_group.add_argument('-n', '--dry-run', dest='dry_run', action='store_true',
                               help='do not create a backup archive')
        dryrun_group.add_argument('-s', '--stats', dest='stats', action='store_true',
                               help='print statistics for the created archive')

        subparser.add_argument('--list', dest='output_list', action='store_true',
                               help='output verbose list of items (files, dirs, ...)')
        subparser.add_argument('--filter', metavar='STATUSCHARS', dest='output_filter',
                               help='only display items with the given status characters (see description)')
        subparser.add_argument('--json', action='store_true',
                               help='output stats as JSON. Implies ``--stats``.')
        subparser.add_argument('--no-cache-sync', dest='no_cache_sync', action='store_true',
                               help='experimental: do not synchronize the cache. Implies not using the files cache.')
        subparser.add_argument('--stdin-name', metavar='NAME', dest='stdin_name', default='stdin',
                               help='use NAME in archive for stdin data (default: "stdin")')

        exclude_group = define_exclusion_group(subparser, tag_files=True)
        exclude_group.add_argument('--exclude-nodump', dest='exclude_nodump', action='store_true',
                                   help='exclude files flagged NODUMP')

        fs_group = subparser.add_argument_group('Filesystem options')
        fs_group.add_argument('-x', '--one-file-system', dest='one_file_system', action='store_true',
                              help='stay in the same file system and do not store mount points of other file systems')
        fs_group.add_argument('--numeric-owner', dest='numeric_owner', action='store_true',
                              help='only store numeric user and group identifiers')
        fs_group.add_argument('--noatime', dest='noatime', action='store_true',
                              help='do not store atime into archive')
        fs_group.add_argument('--noctime', dest='noctime', action='store_true',
                              help='do not store ctime into archive')
        fs_group.add_argument('--nobirthtime', dest='nobirthtime', action='store_true',
                              help='do not store birthtime (creation date) into archive')
        fs_group.add_argument('--nobsdflags', dest='nobsdflags', action='store_true',
                              help='do not read and store bsdflags (e.g. NODUMP, IMMUTABLE) into archive')
        fs_group.add_argument('--files-cache', metavar='MODE', dest='files_cache_mode',
                              type=FilesCacheMode, default=DEFAULT_FILES_CACHE_MODE_UI,
                              help='operate files cache in MODE. default: %s' % DEFAULT_FILES_CACHE_MODE_UI)
        fs_group.add_argument('--read-special', dest='read_special', action='store_true',
                              help='open and read block and char device files as well as FIFOs as if they were '
                                   'regular files. Also follows symlinks pointing to these kinds of files.')

        archive_group = subparser.add_argument_group('Archive options')
        archive_group.add_argument('--comment', dest='comment', metavar='COMMENT', default='',
                                   help='add a comment text to the archive')
        archive_group.add_argument('--timestamp', metavar='TIMESTAMP', dest='timestamp',
                                   type=timestamp, default=None,
                                   help='manually specify the archive creation date/time (UTC, yyyy-mm-ddThh:mm:ss format). '
                                        'Alternatively, give a reference file/directory.')
        archive_group.add_argument('-c', '--checkpoint-interval', metavar='SECONDS', dest='checkpoint_interval',
                                   type=int, default=1800,
                                   help='write checkpoint every SECONDS seconds (Default: 1800)')
        archive_group.add_argument('--chunker-params', metavar='PARAMS', dest='chunker_params',
                                   type=ChunkerParams, default=CHUNKER_PARAMS,
                                   help='specify the chunker parameters (ALGO, CHUNK_MIN_EXP, CHUNK_MAX_EXP, '
                                        'HASH_MASK_BITS, HASH_WINDOW_SIZE). default: %s,%d,%d,%d,%d' % CHUNKER_PARAMS)
        archive_group.add_argument('-C', '--compression', metavar='COMPRESSION', dest='compression',
                                   type=CompressionSpec, default=CompressionSpec('lz4'),
                                   help='select compression algorithm, see the output of the '
                                        '"borg help compression" command for details.')

        subparser.add_argument('location', metavar='ARCHIVE',
                               type=location_validator(archive=True),
                               help='name of archive to create (must be also a valid directory name)')
        subparser.add_argument('paths', metavar='PATH', nargs='*', type=str,
                               help='paths to archive')

        # borg debug
        debug_epilog = process_epilog("""
        These commands are not intended for normal use and potentially very
        dangerous if used incorrectly.

        They exist to improve debugging capabilities without direct system access, e.g.
        in case you ever run into some severe malfunction. Use them only if you know
        what you are doing or if a trusted developer tells you what to do.""")

        subparser = subparsers.add_parser('debug', parents=[mid_common_parser], add_help=False,
                                          description='debugging command (not intended for normal use)',
                                          epilog=debug_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='debugging command (not intended for normal use)')

        debug_parsers = subparser.add_subparsers(title='required arguments', metavar='<command>')
        subparser.set_defaults(fallback_func=functools.partial(self.do_subcommand_help, subparser))

        debug_info_epilog = process_epilog("""
        This command displays some system information that might be useful for bug
        reports and debugging problems. If a traceback happens, this information is
        already appended at the end of the traceback.
        """)
        subparser = debug_parsers.add_parser('info', parents=[common_parser], add_help=False,
                                          description=self.do_debug_info.__doc__,
                                          epilog=debug_info_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='show system infos for debugging / bug reports (debug)')
        subparser.set_defaults(func=self.do_debug_info)

        debug_dump_archive_items_epilog = process_epilog("""
        This command dumps raw (but decrypted and decompressed) archive items (only metadata) to files.
        """)
        subparser = debug_parsers.add_parser('dump-archive-items', parents=[common_parser], add_help=False,
                                          description=self.do_debug_dump_archive_items.__doc__,
                                          epilog=debug_dump_archive_items_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='dump archive items (metadata) (debug)')
        subparser.set_defaults(func=self.do_debug_dump_archive_items)
        subparser.add_argument('location', metavar='ARCHIVE',
                               type=location_validator(archive=True),
                               help='archive to dump')

        debug_dump_archive_epilog = process_epilog("""
        This command dumps all metadata of an archive in a decoded form to a file.
        """)
        subparser = debug_parsers.add_parser('dump-archive', parents=[common_parser], add_help=False,
                                          description=self.do_debug_dump_archive.__doc__,
                                          epilog=debug_dump_archive_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='dump decoded archive metadata (debug)')
        subparser.set_defaults(func=self.do_debug_dump_archive)
        subparser.add_argument('location', metavar='ARCHIVE',
                               type=location_validator(archive=True),
                               help='archive to dump')
        subparser.add_argument('path', metavar='PATH', type=str,
                               help='file to dump data into')

        debug_dump_manifest_epilog = process_epilog("""
        This command dumps manifest metadata of a repository in a decoded form to a file.
        """)
        subparser = debug_parsers.add_parser('dump-manifest', parents=[common_parser], add_help=False,
                                          description=self.do_debug_dump_manifest.__doc__,
                                          epilog=debug_dump_manifest_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='dump decoded repository metadata (debug)')
        subparser.set_defaults(func=self.do_debug_dump_manifest)
        subparser.add_argument('location', metavar='REPOSITORY',
                               type=location_validator(archive=False),
                               help='repository to dump')
        subparser.add_argument('path', metavar='PATH', type=str,
                               help='file to dump data into')

        debug_dump_repo_objs_epilog = process_epilog("""
        This command dumps raw (but decrypted and decompressed) repo objects to files.
        """)
        subparser = debug_parsers.add_parser('dump-repo-objs', parents=[common_parser], add_help=False,
                                          description=self.do_debug_dump_repo_objs.__doc__,
                                          epilog=debug_dump_repo_objs_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='dump repo objects (debug)')
        subparser.set_defaults(func=self.do_debug_dump_repo_objs)
        subparser.add_argument('location', metavar='REPOSITORY',
                               type=location_validator(archive=False),
                               help='repo to dump')
        subparser.add_argument('--ghost', dest='ghost', action='store_true',
                               help='dump all segment file contents, including deleted/uncommitted objects and commits.')

        debug_search_repo_objs_epilog = process_epilog("""
        This command searches raw (but decrypted and decompressed) repo objects for a specific bytes sequence.
        """)
        subparser = debug_parsers.add_parser('search-repo-objs', parents=[common_parser], add_help=False,
                                          description=self.do_debug_search_repo_objs.__doc__,
                                          epilog=debug_search_repo_objs_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='search repo objects (debug)')
        subparser.set_defaults(func=self.do_debug_search_repo_objs)
        subparser.add_argument('location', metavar='REPOSITORY',
                               type=location_validator(archive=False),
                               help='repo to search')
        subparser.add_argument('wanted', metavar='WANTED', type=str,
                               help='term to search the repo for, either 0x1234abcd hex term or a string')

        debug_get_obj_epilog = process_epilog("""
        This command gets an object from the repository.
        """)
        subparser = debug_parsers.add_parser('get-obj', parents=[common_parser], add_help=False,
                                          description=self.do_debug_get_obj.__doc__,
                                          epilog=debug_get_obj_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='get object from repository (debug)')
        subparser.set_defaults(func=self.do_debug_get_obj)
        subparser.add_argument('location', metavar='REPOSITORY', nargs='?', default='',
                               type=location_validator(archive=False),
                               help='repository to use')
        subparser.add_argument('id', metavar='ID', type=str,
                               help='hex object ID to get from the repo')
        subparser.add_argument('path', metavar='PATH', type=str,
                               help='file to write object data into')

        debug_put_obj_epilog = process_epilog("""
        This command puts objects into the repository.
        """)
        subparser = debug_parsers.add_parser('put-obj', parents=[common_parser], add_help=False,
                                          description=self.do_debug_put_obj.__doc__,
                                          epilog=debug_put_obj_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='put object to repository (debug)')
        subparser.set_defaults(func=self.do_debug_put_obj)
        subparser.add_argument('location', metavar='REPOSITORY', nargs='?', default='',
                               type=location_validator(archive=False),
                               help='repository to use')
        subparser.add_argument('paths', metavar='PATH', nargs='+', type=str,
                               help='file(s) to read and create object(s) from')

        debug_delete_obj_epilog = process_epilog("""
        This command deletes objects from the repository.
        """)
        subparser = debug_parsers.add_parser('delete-obj', parents=[common_parser], add_help=False,
                                          description=self.do_debug_delete_obj.__doc__,
                                          epilog=debug_delete_obj_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='delete object from repository (debug)')
        subparser.set_defaults(func=self.do_debug_delete_obj)
        subparser.add_argument('location', metavar='REPOSITORY', nargs='?', default='',
                               type=location_validator(archive=False),
                               help='repository to use')
        subparser.add_argument('ids', metavar='IDs', nargs='+', type=str,
                               help='hex object ID(s) to delete from the repo')

        debug_refcount_obj_epilog = process_epilog("""
        This command displays the reference count for objects from the repository.
        """)
        subparser = debug_parsers.add_parser('refcount-obj', parents=[common_parser], add_help=False,
                                          description=self.do_debug_refcount_obj.__doc__,
                                          epilog=debug_refcount_obj_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='show refcount for object from repository (debug)')
        subparser.set_defaults(func=self.do_debug_refcount_obj)
        subparser.add_argument('location', metavar='REPOSITORY', nargs='?', default='',
                               type=location_validator(archive=False),
                               help='repository to use')
        subparser.add_argument('ids', metavar='IDs', nargs='+', type=str,
                               help='hex object ID(s) to show refcounts for')

        debug_convert_profile_epilog = process_epilog("""
        Convert a Borg profile to a Python cProfile compatible profile.
        """)
        subparser = debug_parsers.add_parser('convert-profile', parents=[common_parser], add_help=False,
                                          description=self.do_debug_convert_profile.__doc__,
                                          epilog=debug_convert_profile_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='convert Borg profile to Python profile (debug)')
        subparser.set_defaults(func=self.do_debug_convert_profile)
        subparser.add_argument('input', metavar='INPUT', type=argparse.FileType('rb'),
                               help='Borg profile')
        subparser.add_argument('output', metavar='OUTPUT', type=argparse.FileType('wb'),
                               help='Output file')

        # borg delete
        delete_epilog = process_epilog("""
        This command deletes an archive from the repository or the complete repository.

        Important: When deleting archives, repository disk space is **not** freed until
        you run ``borg compact``.

        If you delete the complete repository, the local cache for it (if any) is
        also deleted. Alternatively, you can delete just the local cache with the
        ``--cache-only`` option.

        When using ``--stats``, you will get some statistics about how much data was
        deleted - the "Deleted data" deduplicated size there is most interesting as
        that is how much your repository will shrink.
        Please note that the "All archives" stats refer to the state after deletion.

        You can delete multiple archives by specifying their common prefix, if they
        have one, using the ``--prefix PREFIX`` option. You can also specify a shell
        pattern to match multiple archives using the ``--glob-archives GLOB`` option
        (for more info on these patterns, see ``borg help patterns``). Note that these
        two options are mutually exclusive.

        To avoid accidentally deleting archives, especially when using glob patterns,
        it might be helpful to use the ``--dry-run`` to test out the command without
        actually making any changes to the repository.
        """)
        subparser = subparsers.add_parser('delete', parents=[common_parser], add_help=False,
                                          description=self.do_delete.__doc__,
                                          epilog=delete_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='delete archive')
        subparser.set_defaults(func=self.do_delete)
        subparser.add_argument('-n', '--dry-run', dest='dry_run', action='store_true',
                               help='do not change repository')
        subparser.add_argument('-s', '--stats', dest='stats', action='store_true',
                               help='print statistics for the deleted archive')
        subparser.add_argument('--cache-only', dest='cache_only', action='store_true',
                               help='delete only the local cache for the given repository')
        subparser.add_argument('--force', dest='forced',
                               action='count', default=0,
                               help='force deletion of corrupted archives, '
                                    'use ``--force --force`` in case ``--force`` does not work.')
        subparser.add_argument('--save-space', dest='save_space', action='store_true',
                               help='work slower, but using less space')
        subparser.add_argument('location', metavar='TARGET', nargs='?', default='',
                               type=location_validator(),
                               help='archive or repository to delete')
        subparser.add_argument('archives', metavar='ARCHIVE', nargs='*',
                               help='archives to delete')
        define_archive_filters_group(subparser)

        # borg diff
        diff_epilog = process_epilog("""
            This command finds differences (file contents, user/group/mode) between archives.

            A repository location and an archive name must be specified for REPO::ARCHIVE1.
            ARCHIVE2 is just another archive name in same repository (no repository location
            allowed).

            For archives created with Borg 1.1 or newer diff automatically detects whether
            the archives are created with the same chunker params. If so, only chunk IDs
            are compared, which is very fast.

            For archives prior to Borg 1.1 chunk contents are compared by default.
            If you did not create the archives with different chunker params,
            pass ``--same-chunker-params``.
            Note that the chunker params changed from Borg 0.xx to 1.0.

            See the output of the "borg help patterns" command for more help on exclude patterns.
            """)
        subparser = subparsers.add_parser('diff', parents=[common_parser], add_help=False,
                                          description=self.do_diff.__doc__,
                                          epilog=diff_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='find differences in archive contents')
        subparser.set_defaults(func=self.do_diff)
        subparser.add_argument('--numeric-owner', dest='numeric_owner', action='store_true',
                               help='only consider numeric user and group identifiers')
        subparser.add_argument('--same-chunker-params', dest='same_chunker_params', action='store_true',
                               help='Override check of chunker parameters.')
        subparser.add_argument('--sort', dest='sort', action='store_true',
                               help='Sort the output lines by file path.')
        subparser.add_argument('location', metavar='REPO::ARCHIVE1',
                               type=location_validator(archive=True),
                               help='repository location and ARCHIVE1 name')
        subparser.add_argument('archive2', metavar='ARCHIVE2',
                               type=archivename_validator(),
                               help='ARCHIVE2 name (no repository location allowed)')
        subparser.add_argument('paths', metavar='PATH', nargs='*', type=str,
                               help='paths of items inside the archives to compare; patterns are supported')
        define_exclusion_group(subparser)

        # borg export-tar
        export_tar_epilog = process_epilog("""
        This command creates a tarball from an archive.

        When giving '-' as the output FILE, Borg will write a tar stream to standard output.

        By default (``--tar-filter=auto``) Borg will detect whether the FILE should be compressed
        based on its file extension and pipe the tarball through an appropriate filter
        before writing it to FILE:

        - .tar.gz: gzip
        - .tar.bz2: bzip2
        - .tar.xz: xz

        Alternatively a ``--tar-filter`` program may be explicitly specified. It should
        read the uncompressed tar stream from stdin and write a compressed/filtered
        tar stream to stdout.

        The generated tarball uses the GNU tar format.

        export-tar is a lossy conversion:
        BSD flags, ACLs, extended attributes (xattrs), atime and ctime are not exported.
        Timestamp resolution is limited to whole seconds, not the nanosecond resolution
        otherwise supported by Borg.

        A ``--sparse`` option (as found in borg extract) is not supported.

        By default the entire archive is extracted but a subset of files and directories
        can be selected by passing a list of ``PATHs`` as arguments.
        The file selection can further be restricted by using the ``--exclude`` option.

        See the output of the "borg help patterns" command for more help on exclude patterns.

        ``--progress`` can be slower than no progress display, since it makes one additional
        pass over the archive metadata.
        """)
        subparser = subparsers.add_parser('export-tar', parents=[common_parser], add_help=False,
                                          description=self.do_export_tar.__doc__,
                                          epilog=export_tar_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='create tarball from archive')
        subparser.set_defaults(func=self.do_export_tar)
        subparser.add_argument('--tar-filter', dest='tar_filter', default='auto',
                               help='filter program to pipe data through')
        subparser.add_argument('--list', dest='output_list', action='store_true',
                               help='output verbose list of items (files, dirs, ...)')
        subparser.add_argument('location', metavar='ARCHIVE',
                               type=location_validator(archive=True),
                               help='archive to export')
        subparser.add_argument('tarfile', metavar='FILE',
                               help='output tar file. "-" to write to stdout instead.')
        subparser.add_argument('paths', metavar='PATH', nargs='*', type=str,
                               help='paths to extract; patterns are supported')
        define_exclusion_group(subparser, strip_components=True)

        # borg extract
        extract_epilog = process_epilog("""
        This command extracts the contents of an archive. By default the entire
        archive is extracted but a subset of files and directories can be selected
        by passing a list of ``PATHs`` as arguments. The file selection can further
        be restricted by using the ``--exclude`` option.

        See the output of the "borg help patterns" command for more help on exclude patterns.

        By using ``--dry-run``, you can do all extraction steps except actually writing the
        output data: reading metadata and data chunks from the repo, checking the hash/hmac,
        decrypting, decompressing.

        ``--progress`` can be slower than no progress display, since it makes one additional
        pass over the archive metadata.

        .. note::

            Currently, extract always writes into the current working directory ("."),
            so make sure you ``cd`` to the right place before calling ``borg extract``.
        """)
        subparser = subparsers.add_parser('extract', parents=[common_parser], add_help=False,
                                          description=self.do_extract.__doc__,
                                          epilog=extract_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='extract archive contents')
        subparser.set_defaults(func=self.do_extract)
        subparser.add_argument('--list', dest='output_list', action='store_true',
                               help='output verbose list of items (files, dirs, ...)')
        subparser.add_argument('-n', '--dry-run', dest='dry_run', action='store_true',
                               help='do not actually change any files')
        subparser.add_argument('--numeric-owner', dest='numeric_owner', action='store_true',
                               help='only obey numeric user and group identifiers')
        subparser.add_argument('--nobsdflags', dest='nobsdflags', action='store_true',
                               help='do not extract/set bsdflags (e.g. NODUMP, IMMUTABLE)')
        subparser.add_argument('--stdout', dest='stdout', action='store_true',
                               help='write all extracted data to stdout')
        subparser.add_argument('--sparse', dest='sparse', action='store_true',
                               help='create holes in output sparse file from all-zero chunks')
        subparser.add_argument('location', metavar='ARCHIVE',
                               type=location_validator(archive=True),
                               help='archive to extract')
        subparser.add_argument('paths', metavar='PATH', nargs='*', type=str,
                               help='paths to extract; patterns are supported')
        define_exclusion_group(subparser, strip_components=True)

        # borg help
        subparser = subparsers.add_parser('help', parents=[common_parser], add_help=False,
                                          description='Extra help')
        subparser.add_argument('--epilog-only', dest='epilog_only', action='store_true')
        subparser.add_argument('--usage-only', dest='usage_only', action='store_true')
        subparser.set_defaults(func=functools.partial(self.do_help, parser, subparsers.choices))
        subparser.add_argument('topic', metavar='TOPIC', type=str, nargs='?',
                               help='additional help on TOPIC')

        # borg info
        info_epilog = process_epilog("""
        This command displays detailed information about the specified archive or repository.

        Please note that the deduplicated sizes of the individual archives do not add
        up to the deduplicated size of the repository ("all archives"), because the two
        are meaning different things:

        This archive / deduplicated size = amount of data stored ONLY for this archive
        = unique chunks of this archive.
        All archives / deduplicated size = amount of data stored in the repo
        = all chunks in the repository.

        Borg archives can only contain a limited amount of file metadata.
        The size of an archive relative to this limit depends on a number of factors,
        mainly the number of files, the lengths of paths and other metadata stored for files.
        This is shown as *utilization of maximum supported archive size*.
        """)
        subparser = subparsers.add_parser('info', parents=[common_parser], add_help=False,
                                          description=self.do_info.__doc__,
                                          epilog=info_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='show repository or archive information')
        subparser.set_defaults(func=self.do_info)
        subparser.add_argument('location', metavar='REPOSITORY_OR_ARCHIVE', nargs='?', default='',
                               type=location_validator(),
                               help='archive or repository to display information about')
        subparser.add_argument('--json', action='store_true',
                               help='format output as JSON')
        define_archive_filters_group(subparser)

        # borg init
        init_epilog = process_epilog("""
        This command initializes an empty repository. A repository is a filesystem
        directory containing the deduplicated data from zero or more archives.

        Encryption can be enabled at repository init time. It cannot be changed later.

        It is not recommended to work without encryption. Repository encryption protects
        you e.g. against the case that an attacker has access to your backup repository.

        But be careful with the key / the passphrase:

        If you want "passphrase-only" security, use one of the repokey modes. The
        key will be stored inside the repository (in its "config" file). In above
        mentioned attack scenario, the attacker will have the key (but not the
        passphrase).

        If you want "passphrase and having-the-key" security, use one of the keyfile
        modes. The key will be stored in your home directory (in .config/borg/keys).
        In the attack scenario, the attacker who has just access to your repo won't
        have the key (and also not the passphrase).

        Make a backup copy of the key file (keyfile mode) or repo config file
        (repokey mode) and keep it at a safe place, so you still have the key in
        case it gets corrupted or lost. Also keep the passphrase at a safe place.
        The backup that is encrypted with that key won't help you with that, of course.

        Make sure you use a good passphrase. Not too short, not too simple. The real
        encryption / decryption key is encrypted with / locked by your passphrase.
        If an attacker gets your key, he can't unlock and use it without knowing the
        passphrase.

        Be careful with special or non-ascii characters in your passphrase:

        - Borg processes the passphrase as unicode (and encodes it as utf-8),
          so it does not have problems dealing with even the strangest characters.
        - BUT: that does not necessarily apply to your OS / VM / keyboard configuration.

        So better use a long passphrase made from simple ascii chars than one that
        includes non-ascii stuff or characters that are hard/impossible to enter on
        a different keyboard layout.

        You can change your passphrase for existing repos at any time, it won't affect
        the encryption/decryption key or other secrets.

        Encryption modes
        ++++++++++++++++

        .. nanorst: inline-fill

        +----------+---------------+------------------------+--------------------------+
        | Hash/MAC | Not encrypted | Not encrypted,         | Encrypted (AEAD w/ AES)  |
        |          | no auth       | but authenticated      | and authenticated        |
        +----------+---------------+------------------------+--------------------------+
        | SHA-256  | none          | `authenticated`        | repokey                  |
        |          |               |                        | keyfile                  |
        +----------+---------------+------------------------+--------------------------+
        | BLAKE2b  | n/a           | `authenticated-blake2` | `repokey-blake2`         |
        |          |               |                        | `keyfile-blake2`         |
        +----------+---------------+------------------------+--------------------------+

        .. nanorst: inline-replace

        `Marked modes` are new in Borg 1.1 and are not backwards-compatible with Borg 1.0.x.

        On modern Intel/AMD CPUs (except very cheap ones), AES is usually
        hardware-accelerated.
        BLAKE2b is faster than SHA256 on Intel/AMD 64-bit CPUs
        (except AMD Ryzen and future CPUs with SHA extensions),
        which makes `authenticated-blake2` faster than `none` and `authenticated`.

        On modern ARM CPUs, NEON provides hardware acceleration for SHA256 making it faster
        than BLAKE2b-256 there. NEON accelerates AES as well.

        Hardware acceleration is always used automatically when available.

        `repokey` and `keyfile` use AES-CTR-256 for encryption and HMAC-SHA256 for
        authentication in an encrypt-then-MAC (EtM) construction. The chunk ID hash
        is HMAC-SHA256 as well (with a separate key).
        These modes are compatible with Borg 1.0.x.

        `repokey-blake2` and `keyfile-blake2` are also authenticated encryption modes,
        but use BLAKE2b-256 instead of HMAC-SHA256 for authentication. The chunk ID
        hash is a keyed BLAKE2b-256 hash.
        These modes are new and *not* compatible with Borg 1.0.x.

        `authenticated` mode uses no encryption, but authenticates repository contents
        through the same HMAC-SHA256 hash as the `repokey` and `keyfile` modes (it uses it
        as the chunk ID hash). The key is stored like `repokey`.
        This mode is new and *not* compatible with Borg 1.0.x.

        `authenticated-blake2` is like `authenticated`, but uses the keyed BLAKE2b-256 hash
        from the other blake2 modes.
        This mode is new and *not* compatible with Borg 1.0.x.

        `none` mode uses no encryption and no authentication. It uses SHA256 as chunk
        ID hash. Not recommended, rather consider using an authenticated or
        authenticated/encrypted mode. This mode has possible denial-of-service issues
        when running ``borg create`` on contents controlled by an attacker.
        Use it only for new repositories where no encryption is wanted **and** when compatibility
        with 1.0.x is important. If compatibility with 1.0.x is not important, use
        `authenticated-blake2` or `authenticated` instead.
        This mode is compatible with Borg 1.0.x.
        """)
        subparser = subparsers.add_parser('init', parents=[common_parser], add_help=False,
                                          description=self.do_init.__doc__, epilog=init_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='initialize empty repository')
        subparser.set_defaults(func=self.do_init)
        subparser.add_argument('location', metavar='REPOSITORY', nargs='?', default='',
                               type=location_validator(archive=False),
                               help='repository to create')
        subparser.add_argument('-e', '--encryption', metavar='MODE', dest='encryption', required=True,
                               choices=key_argument_names(),
                               help='select encryption key mode **(required)**')
        subparser.add_argument('--append-only', dest='append_only', action='store_true',
                               help='create an append-only mode repository. Note that this only affects '
                                    'the low level structure of the repository, and running `delete` '
                                    'or `prune` will still be allowed. See :ref:`append_only_mode` in '
                                    'Additional Notes for more details.')
        subparser.add_argument('--storage-quota', metavar='QUOTA', dest='storage_quota', default=None,
                               type=parse_storage_quota,
                               help='Set storage quota of the new repository (e.g. 5G, 1.5T). Default: no quota.')
        subparser.add_argument('--make-parent-dirs', dest='make_parent_dirs', action='store_true',
                               help='create the parent directories of the repository directory, if they are missing.')

        # borg key
        subparser = subparsers.add_parser('key', parents=[mid_common_parser], add_help=False,
                                          description="Manage a keyfile or repokey of a repository",
                                          epilog="",
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='manage repository key')

        key_parsers = subparser.add_subparsers(title='required arguments', metavar='<command>')
        subparser.set_defaults(fallback_func=functools.partial(self.do_subcommand_help, subparser))

        key_export_epilog = process_epilog("""
        If repository encryption is used, the repository is inaccessible
        without the key. This command allows one to backup this essential key.
        Note that the backup produced does not include the passphrase itself
        (i.e. the exported key stays encrypted). In order to regain access to a
        repository, one needs both the exported key and the original passphrase.

        There are three backup formats. The normal backup format is suitable for
        digital storage as a file. The ``--paper`` backup format is optimized
        for printing and typing in while importing, with per line checks to
        reduce problems with manual input. The ``--qr-html`` creates a printable
        HTML template with a QR code and a copy of the ``--paper``-formatted key.

        For repositories using keyfile encryption the key is saved locally
        on the system that is capable of doing backups. To guard against loss
        of this key, the key needs to be backed up independently of the main
        data backup.

        For repositories using the repokey encryption the key is saved in the
        repository in the config file. A backup is thus not strictly needed,
        but guards against the repository becoming inaccessible if the file
        is damaged for some reason.
        """)
        subparser = key_parsers.add_parser('export', parents=[common_parser], add_help=False,
                                          description=self.do_key_export.__doc__,
                                          epilog=key_export_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='export repository key for backup')
        subparser.set_defaults(func=self.do_key_export)
        subparser.add_argument('location', metavar='REPOSITORY', nargs='?', default='',
                               type=location_validator(archive=False))
        subparser.add_argument('path', metavar='PATH', nargs='?', type=str,
                               help='where to store the backup')
        subparser.add_argument('--paper', dest='paper', action='store_true',
                               help='Create an export suitable for printing and later type-in')
        subparser.add_argument('--qr-html', dest='qr', action='store_true',
                               help='Create an html file suitable for printing and later type-in or qr scan')

        key_import_epilog = process_epilog("""
        This command restores a key previously backed up with the export command.

        If the ``--paper`` option is given, the import will be an interactive
        process in which each line is checked for plausibility before
        proceeding to the next line. For this format PATH must not be given.
        """)
        subparser = key_parsers.add_parser('import', parents=[common_parser], add_help=False,
                                          description=self.do_key_import.__doc__,
                                          epilog=key_import_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='import repository key from backup')
        subparser.set_defaults(func=self.do_key_import)
        subparser.add_argument('location', metavar='REPOSITORY', nargs='?', default='',
                               type=location_validator(archive=False))
        subparser.add_argument('path', metavar='PATH', nargs='?', type=str,
                               help='path to the backup (\'-\' to read from stdin)')
        subparser.add_argument('--paper', dest='paper', action='store_true',
                               help='interactively import from a backup done with ``--paper``')

        change_passphrase_epilog = process_epilog("""
        The key files used for repository encryption are optionally passphrase
        protected. This command can be used to change this passphrase.

        Please note that this command only changes the passphrase, but not any
        secret protected by it (like e.g. encryption/MAC keys or chunker seed).
        Thus, changing the passphrase after passphrase and borg key got compromised
        does not protect future (nor past) backups to the same repository.
        """)
        subparser = key_parsers.add_parser('change-passphrase', parents=[common_parser], add_help=False,
                                          description=self.do_change_passphrase.__doc__,
                                          epilog=change_passphrase_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='change repository passphrase')
        subparser.set_defaults(func=self.do_change_passphrase)
        subparser.add_argument('location', metavar='REPOSITORY', nargs='?', default='',
                               type=location_validator(archive=False))

        migrate_to_repokey_epilog = process_epilog("""
        This command migrates a repository from passphrase mode (removed in Borg 1.0)
        to repokey mode.

        You will be first asked for the repository passphrase (to open it in passphrase
        mode). This is the same passphrase as you used to use for this repo before 1.0.

        It will then derive the different secrets from this passphrase.

        Then you will be asked for a new passphrase (twice, for safety). This
        passphrase will be used to protect the repokey (which contains these same
        secrets in encrypted form). You may use the same passphrase as you used to
        use, but you may also use a different one.

        After migrating to repokey mode, you can change the passphrase at any time.
        But please note: the secrets will always stay the same and they could always
        be derived from your (old) passphrase-mode passphrase.
        """)
        subparser = key_parsers.add_parser('migrate-to-repokey', parents=[common_parser], add_help=False,
                                          description=self.do_migrate_to_repokey.__doc__,
                                          epilog=migrate_to_repokey_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='migrate passphrase-mode repository to repokey')
        subparser.set_defaults(func=self.do_migrate_to_repokey)
        subparser.add_argument('location', metavar='REPOSITORY', nargs='?', default='',
                               type=location_validator(archive=False))

        # borg list
        list_epilog = process_epilog("""
        This command lists the contents of a repository or an archive.

        See the "borg help patterns" command for more help on exclude patterns.

        .. man NOTES

        The following keys are available for ``--format``:


        """) + BaseFormatter.keys_help() + textwrap.dedent("""

        Keys for listing repository archives:

        """) + ArchiveFormatter.keys_help() + textwrap.dedent("""

        Keys for listing archive files:

        """) + ItemFormatter.keys_help()
        subparser = subparsers.add_parser('list', parents=[common_parser], add_help=False,
                                          description=self.do_list.__doc__,
                                          epilog=list_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='list archive or repository contents')
        subparser.set_defaults(func=self.do_list)
        subparser.add_argument('--short', dest='short', action='store_true',
                               help='only print file/directory names, nothing else')
        subparser.add_argument('--format', '--list-format', metavar='FORMAT', dest='format',
                               help='specify format for file listing '
                                    '(default: "{mode} {user:6} {group:6} {size:8d} {mtime} {path}{extra}{NL}")')
        subparser.add_argument('--json', action='store_true',
                               help='Only valid for listing repository contents. Format output as JSON. '
                                    'The form of ``--format`` is ignored, '
                                    'but keys used in it are added to the JSON output. '
                                    'Some keys are always present. Note: JSON can only represent text. '
                                    'A "barchive" key is therefore not available.')
        subparser.add_argument('--json-lines', action='store_true',
                               help='Only valid for listing archive contents. Format output as JSON Lines. '
                                    'The form of ``--format`` is ignored, '
                                    'but keys used in it are added to the JSON output. '
                                    'Some keys are always present. Note: JSON can only represent text. '
                                    'A "bpath" key is therefore not available.')
        subparser.add_argument('location', metavar='REPOSITORY_OR_ARCHIVE', nargs='?', default='',
                               type=location_validator(),
                               help='repository/archive to list contents of')
        subparser.add_argument('paths', metavar='PATH', nargs='*', type=str,
                               help='paths to list; patterns are supported')
        define_archive_filters_group(subparser)
        define_exclusion_group(subparser)

        # borg mount
        mount_epilog = process_epilog("""
        This command mounts an archive as a FUSE filesystem. This can be useful for
        browsing an archive or restoring individual files. Unless the ``--foreground``
        option is given the command will run in the background until the filesystem
        is ``umounted``.

        The command ``borgfs`` provides a wrapper for ``borg mount``. This can also be
        used in fstab entries:
        ``/path/to/repo /mnt/point fuse.borgfs defaults,noauto 0 0``

        To allow a regular user to use fstab entries, add the ``user`` option:
        ``/path/to/repo /mnt/point fuse.borgfs defaults,noauto,user 0 0``

        For FUSE configuration and mount options, see the mount.fuse(8) manual page.

        Additional mount options supported by borg:

        - versions: when used with a repository mount, this gives a merged, versioned
          view of the files in the archives. EXPERIMENTAL, layout may change in future.
        - allow_damaged_files: by default damaged files (where missing chunks were
          replaced with runs of zeros by borg check ``--repair``) are not readable and
          return EIO (I/O error). Set this option to read such files.
        - ignore_permissions: for security reasons the "default_permissions" mount
          option is internally enforced by borg. "ignore_permissions" can be given to
          not enforce "default_permissions".

        The BORG_MOUNT_DATA_CACHE_ENTRIES environment variable is meant for advanced users
        to tweak the performance. It sets the number of cached data chunks; additional
        memory usage can be up to ~8 MiB times this number. The default is the number
        of CPU cores.

        When the daemonized process receives a signal or crashes, it does not unmount.
        Unmounting in these cases could cause an active rsync or similar process
        to unintentionally delete data.

        When running in the foreground ^C/SIGINT unmounts cleanly, but other
        signals or crashes do not.
        """)

        if parser.prog == 'borgfs':
            parser.description = self.do_mount.__doc__
            parser.epilog = mount_epilog
            parser.formatter_class = argparse.RawDescriptionHelpFormatter
            parser.help = 'mount repository'
            subparser = parser
        else:
            subparser = subparsers.add_parser('mount', parents=[common_parser], add_help=False,
                                            description=self.do_mount.__doc__,
                                            epilog=mount_epilog,
                                            formatter_class=argparse.RawDescriptionHelpFormatter,
                                            help='mount repository')
        subparser.set_defaults(func=self.do_mount)
        subparser.add_argument('location', metavar='REPOSITORY_OR_ARCHIVE', type=location_validator(),
                            help='repository/archive to mount')
        subparser.add_argument('mountpoint', metavar='MOUNTPOINT', type=str,
                            help='where to mount filesystem')
        subparser.add_argument('-f', '--foreground', dest='foreground',
                            action='store_true',
                            help='stay in foreground, do not daemonize')
        subparser.add_argument('-o', dest='options', type=str,
                            help='Extra mount options')
        define_archive_filters_group(subparser)
        subparser.add_argument('paths', metavar='PATH', nargs='*', type=str,
                               help='paths to extract; patterns are supported')
        define_exclusion_group(subparser, strip_components=True)
        if parser.prog == 'borgfs':
            return parser

        # borg prune
        prune_epilog = process_epilog("""
        The prune command prunes a repository by deleting all archives not matching
        any of the specified retention options.

        Important: Repository disk space is **not** freed until you run ``borg compact``.

        This command is normally used by automated backup scripts wanting to keep a
        certain number of historic backups.

        Also, prune automatically removes checkpoint archives (incomplete archives left
        behind by interrupted backup runs) except if the checkpoint is the latest
        archive (and thus still needed). Checkpoint archives are not considered when
        comparing archive counts against the retention limits (``--keep-X``).

        If a prefix is set with -P, then only archives that start with the prefix are
        considered for deletion and only those archives count towards the totals
        specified by the rules.
        Otherwise, *all* archives in the repository are candidates for deletion!
        There is no automatic distinction between archives representing different
        contents. These need to be distinguished by specifying matching prefixes.

        If you have multiple sequences of archives with different data sets (e.g.
        from different machines) in one shared repository, use one prune call per
        data set that matches only the respective archives using the -P option.

        The ``--keep-within`` option takes an argument of the form "<int><char>",
        where char is "H", "d", "w", "m", "y". For example, ``--keep-within 2d`` means
        to keep all archives that were created within the past 48 hours.
        "1m" is taken to mean "31d". The archives kept with this option do not
        count towards the totals specified by any other options.

        A good procedure is to thin out more and more the older your backups get.
        As an example, ``--keep-daily 7`` means to keep the latest backup on each day,
        up to 7 most recent days with backups (days without backups do not count).
        The rules are applied from secondly to yearly, and backups selected by previous
        rules do not count towards those of later rules. The time that each backup
        starts is used for pruning purposes. Dates and times are interpreted in
        the local timezone, and weeks go from Monday to Sunday. Specifying a
        negative number of archives to keep means that there is no limit.

        The ``--keep-last N`` option is doing the same as ``--keep-secondly N`` (and it will
        keep the last N archives under the assumption that you do not create more than one
        backup archive in the same second).

        When using ``--stats``, you will get some statistics about how much data was
        deleted - the "Deleted data" deduplicated size there is most interesting as
        that is how much your repository will shrink.
        Please note that the "All archives" stats refer to the state after pruning.
        """)
        subparser = subparsers.add_parser('prune', parents=[common_parser], add_help=False,
                                          description=self.do_prune.__doc__,
                                          epilog=prune_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='prune archives')
        subparser.set_defaults(func=self.do_prune)
        subparser.add_argument('-n', '--dry-run', dest='dry_run', action='store_true',
                               help='do not change repository')
        subparser.add_argument('--force', dest='forced', action='store_true',
                               help='force pruning of corrupted archives')
        subparser.add_argument('-s', '--stats', dest='stats', action='store_true',
                               help='print statistics for the deleted archive')
        subparser.add_argument('--list', dest='output_list', action='store_true',
                               help='output verbose list of archives it keeps/prunes')
        subparser.add_argument('--keep-within', metavar='INTERVAL', dest='within', type=interval,
                               help='keep all archives within this time interval')
        subparser.add_argument('--keep-last', '--keep-secondly', dest='secondly', type=int, default=0,
                               help='number of secondly archives to keep')
        subparser.add_argument('--keep-minutely', dest='minutely', type=int, default=0,
                               help='number of minutely archives to keep')
        subparser.add_argument('-H', '--keep-hourly', dest='hourly', type=int, default=0,
                               help='number of hourly archives to keep')
        subparser.add_argument('-d', '--keep-daily', dest='daily', type=int, default=0,
                               help='number of daily archives to keep')
        subparser.add_argument('-w', '--keep-weekly', dest='weekly', type=int, default=0,
                               help='number of weekly archives to keep')
        subparser.add_argument('-m', '--keep-monthly', dest='monthly', type=int, default=0,
                               help='number of monthly archives to keep')
        subparser.add_argument('-y', '--keep-yearly', dest='yearly', type=int, default=0,
                               help='number of yearly archives to keep')
        define_archive_filters_group(subparser, sort_by=False, first_last=False)
        subparser.add_argument('--save-space', dest='save_space', action='store_true',
                               help='work slower, but using less space')
        subparser.add_argument('location', metavar='REPOSITORY', nargs='?', default='',
                               type=location_validator(archive=False),
                               help='repository to prune')

        # borg recreate
        recreate_epilog = process_epilog("""
        Recreate the contents of existing archives.

        This is an *experimental* feature. Do *not* use this on your only backup.

        Important: Repository disk space is **not** freed until you run ``borg compact``.

        ``--exclude``, ``--exclude-from``, ``--exclude-if-present``, ``--keep-exclude-tags``, and PATH
        have the exact same semantics as in "borg create". If PATHs are specified the
        resulting archive will only contain files from these PATHs.

        Note that all paths in an archive are relative, therefore absolute patterns/paths
        will *not* match (``--exclude``, ``--exclude-from``, PATHs).

        ``--recompress`` allows one to change the compression of existing data in archives.
        Due to how Borg stores compressed size information this might display
        incorrect information for archives that were not recreated at the same time.
        There is no risk of data loss by this.

        ``--chunker-params`` will re-chunk all files in the archive, this can be
        used to have upgraded Borg 0.xx or Attic archives deduplicate with
        Borg 1.x archives.

        **USE WITH CAUTION.**
        Depending on the PATHs and patterns given, recreate can be used to permanently
        delete files from archives.
        When in doubt, use ``--dry-run --verbose --list`` to see how patterns/PATHS are
        interpreted.

        The archive being recreated is only removed after the operation completes. The
        archive that is built during the operation exists at the same time at
        "<ARCHIVE>.recreate". The new archive will have a different archive ID.

        With ``--target`` the original archive is not replaced, instead a new archive is created.

        When rechunking (or recompressing), space usage can be substantial - expect
        at least the entire deduplicated size of the archives using the previous
        chunker (or compression) params.

        If you recently ran borg check --repair and it had to fix lost chunks with all-zero
        replacement chunks, please first run another backup for the same data and re-run
        borg check --repair afterwards to heal any archives that had lost chunks which are
        still generated from the input data.

        Important: running borg recreate to re-chunk will remove the chunks_healthy
        metadata of all items with replacement chunks, so healing will not be possible
        any more after re-chunking (it is also unlikely it would ever work: due to the
        change of chunking parameters, the missing chunk likely will never be seen again
        even if you still have the data that produced it).
        """)
        subparser = subparsers.add_parser('recreate', parents=[common_parser], add_help=False,
                                          description=self.do_recreate.__doc__,
                                          epilog=recreate_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help=self.do_recreate.__doc__)
        subparser.set_defaults(func=self.do_recreate)
        subparser.add_argument('--list', dest='output_list', action='store_true',
                               help='output verbose list of items (files, dirs, ...)')
        subparser.add_argument('--filter', metavar='STATUSCHARS', dest='output_filter',
                               help='only display items with the given status characters (listed in borg create --help)')
        subparser.add_argument('-n', '--dry-run', dest='dry_run', action='store_true',
                               help='do not change anything')
        subparser.add_argument('-s', '--stats', dest='stats', action='store_true',
                               help='print statistics at end')

        define_exclusion_group(subparser, tag_files=True)

        archive_group = subparser.add_argument_group('Archive options')
        archive_group.add_argument('--target', dest='target', metavar='TARGET', default=None,
                                   type=archivename_validator(),
                                   help='create a new archive with the name ARCHIVE, do not replace existing archive '
                                        '(only applies for a single archive)')
        archive_group.add_argument('-c', '--checkpoint-interval', dest='checkpoint_interval',
                                   type=int, default=1800, metavar='SECONDS',
                                   help='write checkpoint every SECONDS seconds (Default: 1800)')
        archive_group.add_argument('--comment', dest='comment', metavar='COMMENT', default=None,
                                   help='add a comment text to the archive')
        archive_group.add_argument('--timestamp', metavar='TIMESTAMP', dest='timestamp',
                                   type=timestamp, default=None,
                                   help='manually specify the archive creation date/time (UTC, yyyy-mm-ddThh:mm:ss format). '
                                        'alternatively, give a reference file/directory.')
        archive_group.add_argument('-C', '--compression', metavar='COMPRESSION', dest='compression',
                                   type=CompressionSpec, default=CompressionSpec('lz4'),
                                   help='select compression algorithm, see the output of the '
                                        '"borg help compression" command for details.')
        archive_group.add_argument('--recompress', metavar='MODE', dest='recompress', nargs='?',
                                   default='never', const='if-different', choices=('never', 'if-different', 'always'),
                                   help='recompress data chunks according to ``--compression``. '
                                        'MODE `if-different`: '
                                        'recompress if current compression is with a different compression algorithm '
                                        '(the level is not considered). '
                                        'MODE `always`: '
                                        'recompress even if current compression is with the same compression algorithm '
                                        '(use this to change the compression level). '
                                        'MODE `never` (default): '
                                        'do not recompress.')
        archive_group.add_argument('--chunker-params', metavar='PARAMS', dest='chunker_params',
                                   type=ChunkerParams, default=CHUNKER_PARAMS,
                                   help='specify the chunker parameters (ALGO, CHUNK_MIN_EXP, CHUNK_MAX_EXP, '
                                        'HASH_MASK_BITS, HASH_WINDOW_SIZE) or `default` to use the current defaults. '
                                        'default: %s,%d,%d,%d,%d' % CHUNKER_PARAMS)

        subparser.add_argument('location', metavar='REPOSITORY_OR_ARCHIVE', nargs='?', default='',
                               type=location_validator(),
                               help='repository/archive to recreate')
        subparser.add_argument('paths', metavar='PATH', nargs='*', type=str,
                               help='paths to recreate; patterns are supported')

        # borg rename
        rename_epilog = process_epilog("""
        This command renames an archive in the repository.

        This results in a different archive ID.
        """)
        subparser = subparsers.add_parser('rename', parents=[common_parser], add_help=False,
                                          description=self.do_rename.__doc__,
                                          epilog=rename_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='rename archive')
        subparser.set_defaults(func=self.do_rename)
        subparser.add_argument('location', metavar='ARCHIVE',
                               type=location_validator(archive=True),
                               help='archive to rename')
        subparser.add_argument('name', metavar='NEWNAME',
                               type=archivename_validator(),
                               help='the new archive name to use')

        # borg serve
        serve_epilog = process_epilog("""
        This command starts a repository server process. This command is usually not used manually.
        """)
        subparser = subparsers.add_parser('serve', parents=[common_parser], add_help=False,
                                          description=self.do_serve.__doc__, epilog=serve_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='start repository server process')
        subparser.set_defaults(func=self.do_serve)
        subparser.add_argument('--restrict-to-path', metavar='PATH', dest='restrict_to_paths', action='append',
                               help='restrict repository access to PATH. '
                                    'Can be specified multiple times to allow the client access to several directories. '
                                    'Access to all sub-directories is granted implicitly; PATH doesn\'t need to directly point to a repository.')
        subparser.add_argument('--restrict-to-repository', metavar='PATH', dest='restrict_to_repositories', action='append',
                                help='restrict repository access. Only the repository located at PATH '
                                     '(no sub-directories are considered) is accessible. '
                                     'Can be specified multiple times to allow the client access to several repositories. '
                                     'Unlike ``--restrict-to-path`` sub-directories are not accessible; '
                                     'PATH needs to directly point at a repository location. '
                                     'PATH may be an empty directory or the last element of PATH may not exist, in which case '
                                     'the client may initialize a repository there.')
        subparser.add_argument('--append-only', dest='append_only', action='store_true',
                               help='only allow appending to repository segment files. Note that this only '
                                    'affects the low level structure of the repository, and running `delete` '
                                    'or `prune` will still be allowed. See :ref:`append_only_mode` in Additional '
                                    'Notes for more details.')
        subparser.add_argument('--storage-quota', metavar='QUOTA', dest='storage_quota',
                               type=parse_storage_quota, default=None,
                               help='Override storage quota of the repository (e.g. 5G, 1.5T). '
                                    'When a new repository is initialized, sets the storage quota on the new '
                                    'repository as well. Default: no quota.')

        # borg umount
        umount_epilog = process_epilog("""
        This command un-mounts a FUSE filesystem that was mounted with ``borg mount``.

        This is a convenience wrapper that just calls the platform-specific shell
        command - usually this is either umount or fusermount -u.
        """)
        subparser = subparsers.add_parser('umount', parents=[common_parser], add_help=False,
                                          description=self.do_umount.__doc__,
                                          epilog=umount_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='umount repository')
        subparser.set_defaults(func=self.do_umount)
        subparser.add_argument('mountpoint', metavar='MOUNTPOINT', type=str,
                               help='mountpoint of the filesystem to umount')

        # borg upgrade
        upgrade_epilog = process_epilog("""
        Upgrade an existing, local Borg repository.

        When you do not need borg upgrade
        +++++++++++++++++++++++++++++++++

        Not every change requires that you run ``borg upgrade``.

        You do **not** need to run it when:

        - moving your repository to a different place
        - upgrading to another point release (like 1.0.x to 1.0.y),
          except when noted otherwise in the changelog
        - upgrading from 1.0.x to 1.1.x,
          except when noted otherwise in the changelog

        Borg 1.x.y upgrades
        +++++++++++++++++++

        Use ``borg upgrade --tam REPO`` to require manifest authentication
        introduced with Borg 1.0.9 to address security issues. This means
        that modifying the repository after doing this with a version prior
        to 1.0.9 will raise a validation error, so only perform this upgrade
        after updating all clients using the repository to 1.0.9 or newer.

        This upgrade should be done on each client for safety reasons.

        If a repository is accidentally modified with a pre-1.0.9 client after
        this upgrade, use ``borg upgrade --tam --force REPO`` to remedy it.

        If you routinely do this you might not want to enable this upgrade
        (which will leave you exposed to the security issue). You can
        reverse the upgrade by issuing ``borg upgrade --disable-tam REPO``.

        See
        https://borgbackup.readthedocs.io/en/stable/changes.html#pre-1-0-9-manifest-spoofing-vulnerability
        for details.

        Attic and Borg 0.xx to Borg 1.x
        +++++++++++++++++++++++++++++++

        This currently supports converting an Attic repository to Borg and also
        helps with converting Borg 0.xx to 1.0.

        Currently, only LOCAL repositories can be upgraded (issue #465).

        Please note that ``borg create`` (since 1.0.0) uses bigger chunks by
        default than old borg or attic did, so the new chunks won't deduplicate
        with the old chunks in the upgraded repository.
        See ``--chunker-params`` option of ``borg create`` and ``borg recreate``.

        ``borg upgrade`` will change the magic strings in the repository's
        segments to match the new Borg magic strings. The keyfiles found in
        $ATTIC_KEYS_DIR or ~/.attic/keys/ will also be converted and
        copied to $BORG_KEYS_DIR or ~/.config/borg/keys.

        The cache files are converted, from $ATTIC_CACHE_DIR or
        ~/.cache/attic to $BORG_CACHE_DIR or ~/.cache/borg, but the
        cache layout between Borg and Attic changed, so it is possible
        the first backup after the conversion takes longer than expected
        due to the cache resync.

        Upgrade should be able to resume if interrupted, although it
        will still iterate over all segments. If you want to start
        from scratch, use `borg delete` over the copied repository to
        make sure the cache files are also removed:

            borg delete borg

        Unless ``--inplace`` is specified, the upgrade process first creates a backup
        copy of the repository, in REPOSITORY.before-upgrade-DATETIME, using hardlinks.
        This requires that the repository and its parent directory reside on same
        filesystem so the hardlink copy can work.
        This takes longer than in place upgrades, but is much safer and gives
        progress information (as opposed to ``cp -al``). Once you are satisfied
        with the conversion, you can safely destroy the backup copy.

        WARNING: Running the upgrade in place will make the current
        copy unusable with older version, with no way of going back
        to previous versions. This can PERMANENTLY DAMAGE YOUR
        REPOSITORY!  Attic CAN NOT READ BORG REPOSITORIES, as the
        magic strings have changed. You have been warned.""")
        subparser = subparsers.add_parser('upgrade', parents=[common_parser], add_help=False,
                                          description=self.do_upgrade.__doc__,
                                          epilog=upgrade_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='upgrade repository format')
        subparser.set_defaults(func=self.do_upgrade)
        subparser.add_argument('-n', '--dry-run', dest='dry_run', action='store_true',
                               help='do not change repository')
        subparser.add_argument('--inplace', dest='inplace', action='store_true',
                               help='rewrite repository in place, with no chance of going back '
                                    'to older versions of the repository.')
        subparser.add_argument('--force', dest='force', action='store_true',
                               help='Force upgrade')
        subparser.add_argument('--tam', dest='tam', action='store_true',
                               help='Enable manifest authentication (in key and cache) (Borg 1.0.9 and later).')
        subparser.add_argument('--disable-tam', dest='disable_tam', action='store_true',
                               help='Disable manifest authentication (in key and cache).')
        subparser.add_argument('location', metavar='REPOSITORY', nargs='?', default='',
                               type=location_validator(archive=False),
                               help='path to the repository to be upgraded')

        # borg with-lock
        with_lock_epilog = process_epilog("""
        This command runs a user-specified command while the repository lock is held.

        It will first try to acquire the lock (make sure that no other operation is
        running in the repo), then execute the given command as a subprocess and wait
        for its termination, release the lock and return the user command's return
        code as borg's return code.

        .. note::

            If you copy a repository with the lock held, the lock will be present in
            the copy. Thus, before using borg on the copy from a different host,
            you need to use "borg break-lock" on the copied repository, because
            Borg is cautious and does not automatically remove stale locks made by a different host.
        """)
        subparser = subparsers.add_parser('with-lock', parents=[common_parser], add_help=False,
                                          description=self.do_with_lock.__doc__,
                                          epilog=with_lock_epilog,
                                          formatter_class=argparse.RawDescriptionHelpFormatter,
                                          help='run user command with lock held')
        subparser.set_defaults(func=self.do_with_lock)
        subparser.add_argument('location', metavar='REPOSITORY',
                               type=location_validator(archive=False),
                               help='repository to lock')
        subparser.add_argument('command', metavar='COMMAND',
                               help='command to run')
        subparser.add_argument('args', metavar='ARGS', nargs=argparse.REMAINDER,
                               help='command arguments')

        return parser

    def get_args(self, argv, cmd):
        """usually, just returns argv, except if we deal with a ssh forced command for borg serve."""
        result = self.parse_args(argv[1:])
        if cmd is not None and result.func == self.do_serve:
            # borg serve case:
            # - "result" is how borg got invoked (e.g. via forced command from authorized_keys),
            # - "client_result" (from "cmd") refers to the command the client wanted to execute,
            #   which might be different in the case of a forced command or same otherwise.
            client_argv = shlex.split(cmd)
            # Drop environment variables (do *not* interpret them) before trying to parse
            # the borg command line.
            client_argv = list(itertools.dropwhile(lambda arg: '=' in arg, client_argv))
            client_result = self.parse_args(client_argv[1:])
            if client_result.func == result.func:
                # make sure we only process like normal if the client is executing
                # the same command as specified in the forced command, otherwise
                # just skip this block and return the forced command (== result).
                # client is allowed to specify the whitelisted options,
                # everything else comes from the forced "borg serve" command (or the defaults).
                # stuff from blacklist must never be used from the client.
                blacklist = {
                    'restrict_to_paths',
                    'restrict_to_repositories',
                    'append_only',
                    'storage_quota',
                }
                whitelist = {
                    'debug_topics',
                    'lock_wait',
                    'log_level',
                    'umask',
                }
                not_present = object()
                for attr_name in whitelist:
                    assert attr_name not in blacklist, 'whitelist has blacklisted attribute name %s' % attr_name
                    value = getattr(client_result, attr_name, not_present)
                    if value is not not_present:
                        # note: it is not possible to specify a whitelisted option via a forced command,
                        # it always gets overridden by the value specified (or defaulted to) by the client commmand.
                        setattr(result, attr_name, value)

        return result

    def parse_args(self, args=None):
        # We can't use argparse for "serve" since we don't want it to show up in "Available commands"
        if args:
            args = self.preprocess_args(args)
        parser = self.build_parser()
        args = parser.parse_args(args or ['-h'])
        parser.common_options.resolve(args)
        func = get_func(args)
        if func == self.do_create and not args.paths:
            # need at least 1 path but args.paths may also be populated from patterns
            parser.error('Need at least one PATH argument.')
        return args

    def prerun_checks(self, logger):
        check_python()
        check_extension_modules()
        selftest(logger)

    def _setup_implied_logging(self, args):
        """ turn on INFO level logging for args that imply that they will produce output """
        # map of option name to name of logger for that option
        option_logger = {
            'output_list': 'borg.output.list',
            'show_version': 'borg.output.show-version',
            'show_rc': 'borg.output.show-rc',
            'stats': 'borg.output.stats',
            'progress': 'borg.output.progress',
        }
        for option, logger_name in option_logger.items():
            option_set = args.get(option, False)
            logging.getLogger(logger_name).setLevel('INFO' if option_set else 'WARN')

    def _setup_topic_debugging(self, args):
        """Turn on DEBUG level logging for specified --debug-topics."""
        for topic in args.debug_topics:
            if '.' not in topic:
                topic = 'borg.debug.' + topic
            logger.debug('Enabling debug topic %s', topic)
            logging.getLogger(topic).setLevel('DEBUG')

    def run(self, args):
        os.umask(args.umask)  # early, before opening files
        self.lock_wait = args.lock_wait
        func = get_func(args)
        # do not use loggers before this!
        is_serve = func == self.do_serve
        setup_logging(level=args.log_level, is_serve=is_serve, json=args.log_json)
        self.log_json = args.log_json
        args.progress |= is_serve
        self._setup_implied_logging(vars(args))
        self._setup_topic_debugging(args)
        if getattr(args, 'stats', False) and getattr(args, 'dry_run', False):
            logger.error("--stats does not work with --dry-run.")
            return self.exit_code
        if args.show_version:
            logging.getLogger('borg.output.show-version').info('borgbackup version %s' % __version__)
        self.prerun_checks(logger)
        if not is_supported_msgpack():
            logger.error("You do not have a supported version of the msgpack python package installed. Terminating.")
            logger.error("This should never happen as specific, supported versions are required by our setup.py.")
            logger.error("Do not contact borgbackup support about this.")
            return set_ec(EXIT_ERROR)
        if is_slow_msgpack():
            logger.warning("Using a pure-python msgpack! This will result in lower performance.")
        if args.debug_profile:
            # Import only when needed - avoids a further increase in startup time
            import cProfile
            import marshal
            logger.debug('Writing execution profile to %s', args.debug_profile)
            # Open the file early, before running the main program, to avoid
            # a very late crash in case the specified path is invalid.
            with open(args.debug_profile, 'wb') as fd:
                profiler = cProfile.Profile()
                variables = dict(locals())
                profiler.enable()
                try:
                    return set_ec(func(args))
                finally:
                    profiler.disable()
                    profiler.snapshot_stats()
                    if args.debug_profile.endswith('.pyprof'):
                        marshal.dump(profiler.stats, fd)
                    else:
                        # We use msgpack here instead of the marshal module used by cProfile itself,
                        # because the latter is insecure. Since these files may be shared over the
                        # internet we don't want a format that is impossible to interpret outside
                        # an insecure implementation.
                        # See scripts/msgpack2marshal.py for a small script that turns a msgpack file
                        # into a marshal file that can be read by e.g. pyprof2calltree.
                        # For local use it's unnecessary hassle, though, that's why .pyprof makes
                        # it compatible (see above).
                        # We do not use our msgpack wrapper here, but directly call mp_pack.
                        msgpack.mp_pack(profiler.stats, fd, use_bin_type=True)
        else:
            return set_ec(func(args))


def sig_info_handler(sig_no, stack):  # pragma: no cover
    """search the stack for infos about the currently processed file and print them"""
    with signal_handler(sig_no, signal.SIG_IGN):
        for frame in inspect.getouterframes(stack):
            func, loc = frame[3], frame[0].f_locals
            if func in ('process_file', '_process', ):  # create op
                path = loc['path']
                try:
                    pos = loc['fd'].tell()
                    total = loc['st'].st_size
                except Exception:
                    pos, total = 0, 0
                logger.info("{0} {1}/{2}".format(path, format_file_size(pos), format_file_size(total)))
                break
            if func in ('extract_item', ):  # extract op
                path = loc['item'].path
                try:
                    pos = loc['fd'].tell()
                except Exception:
                    pos = 0
                logger.info("{0} {1}/???".format(path, format_file_size(pos)))
                break


def sig_trace_handler(sig_no, stack):  # pragma: no cover
    print('\nReceived SIGUSR2 at %s, dumping trace...' % datetime.now().replace(microsecond=0), file=sys.stderr)
    faulthandler.dump_traceback()


def main():  # pragma: no cover
    # Make sure stdout and stderr have errors='replace' to avoid unicode
    # issues when print()-ing unicode file names
    sys.stdout = ErrorIgnoringTextIOWrapper(sys.stdout.buffer, sys.stdout.encoding, 'replace', line_buffering=True)
    sys.stderr = ErrorIgnoringTextIOWrapper(sys.stderr.buffer, sys.stderr.encoding, 'replace', line_buffering=True)

    # If we receive SIGINT (ctrl-c), SIGTERM (kill) or SIGHUP (kill -HUP),
    # catch them and raise a proper exception that can be handled for an
    # orderly exit.
    # SIGHUP is important especially for systemd systems, where logind
    # sends it when a session exits, in addition to any traditional use.
    # Output some info if we receive SIGUSR1 or SIGINFO (ctrl-t).

    # Register fault handler for SIGSEGV, SIGFPE, SIGABRT, SIGBUS and SIGILL.
    faulthandler.enable()
    with signal_handler('SIGINT', raising_signal_handler(KeyboardInterrupt)), \
         signal_handler('SIGHUP', raising_signal_handler(SigHup)), \
         signal_handler('SIGTERM', raising_signal_handler(SigTerm)), \
         signal_handler('SIGUSR1', sig_info_handler), \
         signal_handler('SIGUSR2', sig_trace_handler), \
         signal_handler('SIGINFO', sig_info_handler):
        archiver = Archiver()
        msg = msgid = tb = None
        tb_log_level = logging.ERROR
        try:
            args = archiver.get_args(sys.argv, os.environ.get('SSH_ORIGINAL_COMMAND'))
        except Error as e:
            msg = e.get_message()
            tb_log_level = logging.ERROR if e.traceback else logging.DEBUG
            tb = '%s\n%s' % (traceback.format_exc(), sysinfo())
            # we might not have logging setup yet, so get out quickly
            print(msg, file=sys.stderr)
            if tb_log_level == logging.ERROR:
                print(tb, file=sys.stderr)
            sys.exit(e.exit_code)
        try:
            exit_code = archiver.run(args)
        except Error as e:
            msg = e.get_message()
            msgid = type(e).__qualname__
            tb_log_level = logging.ERROR if e.traceback else logging.DEBUG
            tb = "%s\n%s" % (traceback.format_exc(), sysinfo())
            exit_code = e.exit_code
        except RemoteRepository.RPCError as e:
            important = e.exception_class not in ('LockTimeout', ) and e.traceback
            msgid = e.exception_class
            tb_log_level = logging.ERROR if important else logging.DEBUG
            if important:
                msg = e.exception_full
            else:
                msg = e.get_message()
            tb = '\n'.join('Borg server: ' + l for l in e.sysinfo.splitlines())
            tb += "\n" + sysinfo()
            exit_code = EXIT_ERROR
        except Exception:
            msg = 'Local Exception'
            msgid = 'Exception'
            tb_log_level = logging.ERROR
            tb = '%s\n%s' % (traceback.format_exc(), sysinfo())
            exit_code = EXIT_ERROR
        except KeyboardInterrupt:
            msg = 'Keyboard interrupt'
            tb_log_level = logging.DEBUG
            tb = '%s\n%s' % (traceback.format_exc(), sysinfo())
            exit_code = EXIT_ERROR
        except SigTerm:
            msg = 'Received SIGTERM'
            msgid = 'Signal.SIGTERM'
            tb_log_level = logging.DEBUG
            tb = '%s\n%s' % (traceback.format_exc(), sysinfo())
            exit_code = EXIT_ERROR
        except SigHup:
            msg = 'Received SIGHUP.'
            msgid = 'Signal.SIGHUP'
            exit_code = EXIT_ERROR
        if msg:
            logger.error(msg, msgid=msgid)
        if tb:
            logger.log(tb_log_level, tb)
        if args.show_rc:
            rc_logger = logging.getLogger('borg.output.show-rc')
            exit_msg = 'terminating with %s status, rc %d'
            if exit_code == EXIT_SUCCESS:
                rc_logger.info(exit_msg % ('success', exit_code))
            elif exit_code == EXIT_WARNING:
                rc_logger.warning(exit_msg % ('warning', exit_code))
            elif exit_code == EXIT_ERROR:
                rc_logger.error(exit_msg % ('error', exit_code))
            else:
                rc_logger.error(exit_msg % ('abnormal', exit_code or 666))
        sys.exit(exit_code)


if __name__ == '__main__':
    main()
