# vim: set fileencoding=utf-8 :
#
# (C) 2014 Intel Corporation <markus.lehtonen@linux.intel.com>
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program; if not, write to the Free Software
#    Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
"""Import an RPM package in Bitbake format"""

import ConfigParser
import sys
import os
import shutil

import gbp.tmpfile as tempfile
import gbp.command_wrappers as gbpc
import gbp.log
from gbp.rpm import RpmUpstreamSource
from gbp.rpm.policy import RpmPkgPolicy
from gbp.rpm.git import RpmGitRepository, GitRepositoryError
from gbp.config import (GbpOptionParserBB, GbpOptionGroup,
                        no_upstream_branch_msg)
from gbp.errors import GbpError
from gbp.pkg import parse_archive_filename
from gbp.scripts.import_srpm import move_tag_stamp, force_to_branch_head
from gbp.bb import bb, init_tinfoil, pkg_version

#   pylint: disable=bad-continuation

NO_PACKAGING_BRANCH_MSG = """
Repository does not have branch '%s' for meta/packaging files.
You need to reate it or use --packaging-branch to specify it.
"""

class SkipImport(Exception):
    """Nothing imported"""
    pass

def set_bare_repo_options(options):
    """Modify options for import into a bare repository"""
    if options.pristine_tar:
        gbp.log.info("Bare repository: setting %s option '--no-pristine-tar'")
        options.pristine_tar = False


def build_parser(name):
    """Create command line parser"""
    try:
        parser = GbpOptionParserBB(command=os.path.basename(name),
                                   prefix='',
                                   usage='%prog [options] /path/to/package'
                                          '.src.rpm')
    except ConfigParser.ParsingError, err:
        gbp.log.err(err)
        return None

    import_group = GbpOptionGroup(parser, "import options",
                      "pristine-tar and filtering")
    tag_group = GbpOptionGroup(parser, "tag options",
                      "options related to git tag creation")
    branch_group = GbpOptionGroup(parser, "version and branch naming options",
                      "version number and branch layout options")

    for group in [import_group, branch_group, tag_group ]:
        parser.add_option_group(group)

    parser.add_option("-v", "--verbose", action="store_true", dest="verbose",
                      default=False, help="verbose command execution")
    parser.add_config_file_option(option_name="color", dest="color",
                      type='tristate')
    parser.add_config_file_option(option_name="color-scheme",
                                  dest="color_scheme")
    parser.add_config_file_option(option_name="tmp-dir", dest="tmp_dir")
    parser.add_config_file_option(option_name="vendor", action="store",
                      dest="vendor")
    branch_group.add_config_file_option(option_name="packaging-branch",
                      dest="packaging_branch")
    branch_group.add_config_file_option(option_name="upstream-branch",
                      dest="upstream_branch")
    branch_group.add_option("--upstream-vcs-tag", dest="vcs_tag",
                            help="Upstream VCS tag on top of which to import "
                                 "the orig sources")
    branch_group.add_boolean_config_file_option(
                      option_name="create-missing-branches",
                      dest="create_missing_branches")

    tag_group.add_boolean_config_file_option(option_name="sign-tags",
                      dest="sign_tags")
    tag_group.add_config_file_option(option_name="keyid",
                      dest="keyid")
    tag_group.add_config_file_option(option_name="packaging-tag",
                      dest="packaging_tag")
    tag_group.add_config_file_option(option_name="upstream-tag",
                      dest="upstream_tag")

    import_group.add_config_file_option(option_name="filter",
                      dest="filters", action="append")
    import_group.add_boolean_config_file_option(option_name="pristine-tar",
                      dest="pristine_tar")
    import_group.add_option("--allow-same-version", action="store_true",
                      dest="allow_same_version", default=False,
                      help="allow to import already imported version")
    import_group.add_config_file_option(option_name="meta-dir",
                      dest="meta_dir")
    return parser

def parse_args(argv):
    """Parse commandline arguments"""
    parser = build_parser(argv[0])
    if not parser:
        return None, None

    (options, args) = parser.parse_args(argv[1:])
    gbp.log.setup(options.color, options.verbose, options.color_scheme)
    return options, args


def guess_bb(pkg_dir, tinfoil):
    """Guess a bb from a directory"""
    abspath = os.path.abspath(pkg_dir)
    layer_dirs = tinfoil.config_data.getVar('BBLAYERS').split()
    gbp.log.debug("Checking if %s is in %s" % (abspath, layer_dirs))
    layer_dir = ''
    for path in layer_dirs:
        if abspath.startswith(path):
            layer_dir = path
    if not layer_dir:
        raise GbpError("%s not under configured layers" % abspath)

    bb_files = [path for path in tinfoil.cooker_data.pkg_fn
                    if os.path.dirname(path) == abspath]
    if len(bb_files):
        bb_file = bb_files[-1]
        gbp.log.debug("Found %d recipes in %s, choosing %s" %
                      (len(bb_files), pkg_dir, os.path.basename(bb_file)))
    else:
        raise GbpError("No recipes found in %s" % pkg_dir)
    return bb_file

def guess_pkg(pkg, tinfoil):
    """Determine the package to import"""
    if pkg in tinfoil.cooker_data.pkg_pn:
        pkg_bb = tinfoil.cooker_data.pkg_pn[pkg][0]
    elif not os.path.isdir(pkg):
        abspath = os.path.abspath(pkg)
        if abspath in tinfoil.cooker_data.pkg_fn:
            pkg_bb = abspath
        else:
            raise GbpError("Package %s not found in any configured layer" % pkg)
    elif os.path.exists(pkg):
        pkg_bb = guess_bb(pkg, tinfoil)
    else:
        raise GbpError("Unable to find %s" % pkg)
    return pkg_bb

def init_repo(path):
    """Check and initialize Git repository"""
    try:
        repo = RpmGitRepository(path)
        clean, out = repo.is_clean()
        if not clean and not repo.is_empty():
            gbp.log.err("Repository has uncommitted changes, commit "
                        "these first:")
            gbp.log.err(out)
            raise GbpError
    except GitRepositoryError:
        gbp.log.info("No git repository found, creating one in %s" % path)
        repo = RpmGitRepository.create(path)
    return repo

def recursive_copy(src, dst):
    """Recursive copy, overwriting files and preserving symlinks"""
    # Remove existing destinations, if needed
    if os.path.isfile(dst) or os.path.islink(dst):
        os.unlink(dst)
    elif (os.path.isfile(src) or os.path.islink(src)) and os.path.isdir(dst):
        # Remove dst dir if src is a file
        shutil.rmtree(dst)

    try:
        if os.path.islink(src):
            os.symlink(os.readlink(src), dst)
        elif os.path.isdir(src):
            if not os.path.exists(dst):
                os.makedirs(dst)
            for fname in os.listdir(src):
                recursive_copy(os.path.join(src, fname),
                               os.path.join(dst, fname))
        else:
            shutil.copy2(src, dst)
    except (IOError, OSError) as err:
        raise GbpError("Error while copying '%s' to '%s': %s" % (src, dst, err))

def guess_upstream_source(pkg_data, remotes):
    """Guess the primary upstream source archive."""
    orig = None
    name = pkg_data.getVar('PN', True)

    for fetch_data in remotes:
        if fetch_data.type == 'git':
            orig = fetch_data
        else:
            path = fetch_data.localpath
            fname = os.path.basename(path)
            fn_base, archive_fmt, _ = parse_archive_filename(fname)
            if fn_base.startswith(name) and archive_fmt:
                # Take an archive that starts with pkg name
                orig = fetch_data
            # otherwise we take the first archive
            elif not orig and archive_fmt:
                orig = fetch_data
            # else don't accept
    return orig

def bb_get_files(pkg_data, tgt_dir, whole_dir=False, download=True):
    """Get (local) packaging files"""
    uris = (pkg_data.getVar('SRC_URI', True) or "").split()
    try:
        fetch = bb.fetch2.Fetch(uris, pkg_data)
        if download:
            gbp.log.info("Fetching sources...")
            fetch.download()
    except bb.fetch2.BBFetchException as err:
        raise GbpError("Failed to fetch packaging files: %s" % err)

    # Copy local files to target directory
    bb_dir = os.path.dirname(pkg_data.getVar('FILE', True))
    remote = []
    local = [path for path in pkg_data.getVar('BBINCLUDED', True).split() if
                path.startswith(bb_dir) and os.path.exists(path)]
    for url in fetch.urls:
        path = fetch.localpath(url)
        if path.startswith(bb_dir):
            if not whole_dir:
                gbp.log.debug("Found local meta file '%s'" % path)
                local.append(path)
        else:
            gbp.log.debug("Found remote file '%s'" % path)
            remote.append(fetch.ud[url])

    if whole_dir:
        # Simply copy whole meta dir, if requested
        recursive_copy(bb_dir, tgt_dir)
    else:
        for path in local:
            relpath = os.path.relpath(path, bb_dir)
            subdir = os.path.join(tgt_dir, os.path.dirname(relpath))
            if not os.path.exists(subdir):
                os.makedirs(subdir)
            shutil.copy2(path, os.path.join(tgt_dir, relpath))

    return remote

def import_upstream_archive(repo, pkg_data, fetch_data, dirs, options):
    """Import upstream sources from archive"""
    # Unpack orig source archive
    path = fetch_data.localpath
    sources = RpmUpstreamSource(path)
    sources = sources.unpack(dirs['origsrc'], options.filters)

    tag_str_fields = dict(pkg_version(pkg_data), vendor=options.vendor.lower())
    tag = repo.version_to_tag(options.upstream_tag, tag_str_fields)
    if not repo.has_tag(tag):
        gbp.log.info("Tag %s not found, importing upstream sources" % tag)
        branch = options.upstream_branch

        msg = "Upstream version %s" % tag_str_fields['upstreamversion']
        if options.vcs_tag:
            parents = [repo.rev_parse("%s^{}" % options.vcs_tag)]
        else:
            parents = None
        commit = repo.commit_dir(sources.unpacked, "Imported %s" % msg,
                        branch, other_parents=parents,
                        create_missing_branch=options.create_missing_branches)
        repo.create_tag(name=tag, msg=msg, commit=commit,
                        sign=options.sign_tags, keyid=options.keyid)

        if options.pristine_tar:
            archive_fmt = parse_archive_filename(path)[1]
            if archive_fmt == 'tar':
                repo.pristine_tar.commit(path, 'refs/heads/%s' % branch)
            else:
                gbp.log.warn('Ignoring pristine-tar, %s archives '
                             'not supported' % archive_fmt)
    return repo.rev_parse('%s^0' % tag)

def import_upstream_git(repo, fetch_data, options):
    """Import upstream sources from Git"""
    # Fetch from local cached repo
    for branch in fetch_data.branches.values():
        repo.fetch(repo=fetch_data.localpath, refspec=branch)

    commit = fetch_data.revision
    repo.update_ref('refs/heads/' + options.upstream_branch, commit)
    return commit

def import_upstream_sources(repo, pkg_data, remotes, dirs, options):
    """Import upstream sources to Git"""
    fetch_data = guess_upstream_source(pkg_data, remotes)
    if fetch_data:
        gbp.log.debug("Using upstream source '%s'" % fetch_data.url)
    else:
        gbp.log.info("No orig source archive imported")
        return

    if not repo.has_branch(options.upstream_branch):
        if options.create_missing_branches:
            gbp.log.info("Will create missing branch '%s'" %
                         options.upstream_branch)
        else:
            gbp.log.err(no_upstream_branch_msg % options.upstream_branch + "\n"
                "Also check the --create-missing-branches option.")
            raise GbpError

    if fetch_data.type == 'git':
        return import_upstream_git(repo, fetch_data, options)
    else:
        return import_upstream_archive(repo, pkg_data, fetch_data, dirs,
                                       options)


def main(argv):
    """Main function of the gbp import-bb script"""
    dirs = dict(top=os.path.abspath(os.curdir))
    ret = 0
    skipped = False

    if not bb:
        return 1

    options, args = parse_args(argv)

    if len(args) == 0 or len(args) > 2:
        gbp.log.err("Need to give exactly one package to import. Try --help.")
        return 1

    try:
        dirs['tmp_base'] = tempfile.mkdtemp(dir=options.tmp_dir,
                                            prefix='import-bb')
        tinfoil = init_tinfoil()
        pkg_bb = guess_pkg(args[0], tinfoil)
        dirs['src'] = os.path.abspath(os.path.dirname(pkg_bb))
        gbp.log.info("Importing '%s' from '%s'" %
                     (os.path.basename(pkg_bb), dirs['src']))

        pkg_data = bb.cache.Cache.loadDataFull(pkg_bb, [], tinfoil.config_data)

        # Determine target repo dir
        target_dir = ''
        if len(args) == 2:
            target_dir = args[1]
        else:
            if 'BUILDDIR' in os.environ:
                target_dir = os.path.join(os.environ['BUILDDIR'], 'devel')
            target_dir = os.path.join(target_dir, pkg_data.getVar('PN', True))

        # Check the Git repository state
        repo = init_repo(target_dir)
        if repo.bare:
            set_bare_repo_options(options)
        if repo.is_empty():
            options.create_missing_branches = True
        os.chdir(repo.path)

        # Create more tempdirs
        dirs['origsrc'] = tempfile.mkdtemp(dir=dirs['tmp_base'],
                                           prefix='origsrc_')
        dirs['packaging_base'] = tempfile.mkdtemp(dir=dirs['tmp_base'],
                                                  prefix='packaging_')
        dirs['packaging'] = os.path.join(dirs['packaging_base'],
                                         options.meta_dir)

        # Copy (local) packaging files to tmp dir
        remote_srcs = bb_get_files(pkg_data, dirs['packaging'])

        version_dict = pkg_version(pkg_data)
        tag_str_fields = dict(version_dict, vendor=options.vendor.lower())
        ver_str = RpmPkgPolicy.compose_full_version(version_dict)

        # Check if the same version of the package is already imported
        if repo.find_version(options.packaging_tag, tag_str_fields):
            gbp.log.warn("Version %s already imported." % ver_str)
            if options.allow_same_version:
                gbp.log.info("Moving tag of version '%s' since import forced" %
                             ver_str)
                move_tag_stamp(repo, options.packaging_tag, tag_str_fields)
            else:
                raise SkipImport

        # Import upstream sources
        import_upstream_sources(repo, pkg_data, remote_srcs, dirs, options)

        # Import packaging files
        gbp.log.info("Importing local meta/packaging files")
        branch = options.packaging_branch
        if not repo.has_branch(branch):
            if options.create_missing_branches:
                gbp.log.info("Will create missing branch '%s'" % branch)
            else:
                gbp.log.err(NO_PACKAGING_BRANCH_MSG % branch + "\n"
                            "Also check the --create-missing-branches "
                            "option.")
                raise GbpError

        tag = repo.version_to_tag(options.packaging_tag, tag_str_fields)
        msg = "%s release %s" % (options.vendor, ver_str)

        commit = repo.commit_dir(dirs['packaging_base'],
                    "Imported %s" % msg,
                    branch,
                    create_missing_branch=options.create_missing_branches)

        # Create packaging tag
        repo.create_tag(name=tag,
                        msg=msg,
                        commit=commit,
                        sign=options.sign_tags,
                        keyid=options.keyid)

        force_to_branch_head(repo, options.packaging_branch)

    except KeyboardInterrupt:
        ret = 1
        gbp.log.err("Interrupted. Aborting.")
    except gbpc.CommandExecFailed:
        ret = 1
    except GitRepositoryError as err:
        gbp.log.err("Git command failed: %s" % err)
        ret = 1
    except GbpError as err:
        if len(err.__str__()):
            gbp.log.err(err)
        ret = 1
    except SkipImport:
        skipped = True
    finally:
        os.chdir(dirs['top'])
        gbpc.RemoveTree(dirs['tmp_base'])()

    if not ret and not skipped:
        gbp.log.info("Version '%s' imported under '%s'" %
                     (ver_str, repo.path))
    return ret

if __name__ == '__main__':
    sys.exit(main(sys.argv))

# vim:et:ts=4:sw=4:et:sts=4:ai:set list listchars=tab\:»·,trail\:·:
