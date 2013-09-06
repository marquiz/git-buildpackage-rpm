# vim: set fileencoding=utf-8 :
#
# (C) 2006,2007,2011 Guido Guenther <agx@sigxcpu.org>
# (C) 2012 Intel Corporation <markus.lehtonen@linux.intel.com>
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
"""Import an RPM source package into a git repository"""

import ConfigParser
import sys
import re
import os
import glob
import time
import shutil
import errno
import urllib2

import gbp.tmpfile as tempfile
import gbp.command_wrappers as gbpc
from gbp.rpm import (parse_srpm, guess_spec, SpecFile, NoSpecError,
                    RpmUpstreamSource)
from gbp.rpm.policy import RpmPkgPolicy
from gbp.rpm.git import (RpmGitRepository, GitRepositoryError)
from gbp.git.modifier import GitModifier
from gbp.config import (GbpOptionParserRpm, GbpOptionGroup,
                       no_upstream_branch_msg)
from gbp.errors import GbpError
import gbp.log
from gbp.scripts.pq_rpm import safe_patches, rm_patch_files, get_packager
from gbp.scripts.common.pq import apply_and_commit_patch
from gbp.pkg import parse_archive_filename

no_packaging_branch_msg = """
Repository does not have branch '%s' for packaging/distribution sources. If there is none see
file:///usr/share/doc/git-buildpackage/manual-html/gbp.import.html#GBP.IMPORT.CONVERT
on howto create it otherwise use --packaging-branch to specify it.
"""

PATCH_AUTODELETE_COMMIT_MSG = """
Autoremove imported patches from packaging

Removed all imported patches from %s
and patch files from the packaging dir.
"""

class SkipImport(Exception):
    """Nothing imported"""
    pass

class PatchImportError(Exception):
    """Patch import failed"""
    pass


def download_source(pkg, dirs):
    """Download package from a remote location"""
    if re.match(r'[a-z]{1,5}://', pkg):
        mode = 'python urllib2'
    else:
        mode = 'yumdownloader'

    tmpdir = tempfile.mkdtemp(dir=dirs['tmp_base'], prefix='download_')
    gbp.log.info("Downloading '%s' using '%s'..." % (pkg, mode))
    if mode == 'yumdownloader':
        gbpc.RunAtCommand('yumdownloader',
                          ['--source', '--destdir=', '.', pkg],
                          shell=False)(dir=tmpdir)
    else:
        try:
            url = urllib2.urlopen(pkg)
            local_fn = os.path.join(tmpdir, os.path.basename(pkg))
            with open(local_fn, "wb") as local_file:
                local_file.write(url.read())
        except urllib2.HTTPError as err:
            raise GbpError("Download failed: %s" % err)
        except urllib2.URLError as err:
            raise GbpError("Download failed: %s" % err.reason)
    srpm = glob.glob(os.path.join(tmpdir, '*.src.rpm'))[0]
    return srpm


def committer_from_author(author, options):
    """Get committer info based on options"""
    committer = GitModifier()
    if options.author_is_committer:
        committer.name = author.name
        committer.email = author.email
    return committer


def move_tag_stamp(repo, tag_format, tag_str_fields):
    "Move tag out of the way appending the current timestamp"
    old = repo.version_to_tag(tag_format, tag_str_fields)
    new = repo.version_to_tag('%s~%d' % (tag_format, int(time.time())),
                              tag_str_fields)
    repo.move_tag(old, new)


def set_bare_repo_options(options):
    """Modify options for import into a bare repository"""
    if options.pristine_tar:
        gbp.log.info("Bare repository: setting %s option '--no-pristine-tar'")
        options.pristine_tar = False
    if options.patch_import:
        gbp.log.info("Bare repository: setting %s option '--no-patch-import')")
        options.patch_import = False


def import_spec_patches(repo, spec, dirs):
    """
    Import patches from a spec file to the current branch
    """
    queue = spec.patchseries()
    if len(queue) == 0:
        return

    gbp.log.info("Importing patches to '%s' branch" % repo.get_branch())
    tmpdir = tempfile.mkdtemp(dir=dirs['tmp_base'], prefix='import_')
    orig_head = repo.rev_parse("HEAD")
    packager = get_packager(spec)

    # Put patches in a safe place
    queue = safe_patches(queue, tmpdir)[1]
    for patch in queue:
        gbp.log.debug("Applying %s" % patch.path)
        try:
            apply_and_commit_patch(repo, patch, packager)
        except (GbpError, GitRepositoryError):
            repo.force_head(orig_head, hard=True)
            raise PatchImportError("Patch(es) didn't apply, you need apply "
                                   "and commit manually")

    # Remove patches from spec and packaging directory
    gbp.log.info("Removing imported patch files from spec and packaging dir")
    rm_patch_files(spec)
    try:
        spec.update_patches([], {})
        spec.write_spec_file()
    except GbpError:
        repo.force_head('HEAD', hard=True)
        raise PatchImportError("Unable to update spec file, you need to edit"
                               "and commit it  manually")
    repo.commit_all(msg=PATCH_AUTODELETE_COMMIT_MSG % spec.specfile)


def force_to_branch_head(repo, branch):
    if repo.get_branch() == branch:
        # Update HEAD if we modified the checked out branch
        repo.force_head(branch, hard=True)
    # Checkout packaging branch
    repo.set_branch(branch)


def parse_args(argv):
    """Parse commandline arguments"""
    try:
        parser = GbpOptionParserRpm(command=os.path.basename(argv[0]),
                                    prefix='',
                                    usage='%prog [options] /path/to/package'
                                          '.src.rpm')
    except ConfigParser.ParsingError, err:
        gbp.log.err(err)
        return None, None

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
    parser.add_option("--download", action="store_true", dest="download",
                      default=False, help="download source package")
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
    branch_group.add_option("--orphan-packaging", action="store_true",
                      dest="orphan_packaging", default=False,
                      help="The packaging branch doesn't base on upstream")
    branch_group.add_option("--native", action="store_true",
                      dest="native", default=False,
                      help="This is a dist native package, no separate "
                           "upstream branch")

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
    import_group.add_boolean_config_file_option(
                      option_name="author-is-committer",
                      dest="author_is_committer")
    import_group.add_config_file_option(option_name="packaging-dir",
                      dest="packaging_dir")
    import_group.add_boolean_config_file_option(option_name="patch-import",
                                                dest="patch_import")
    (options, args) = parser.parse_args(argv[1:])
    gbp.log.setup(options.color, options.verbose, options.color_scheme)
    return options, args


def main(argv):
    """Main function of the git-import-srpm script"""
    dirs = dict(top=os.path.abspath(os.curdir))

    ret = 0
    skipped = False

    options, args = parse_args(argv)

    if len(args) != 1:
        gbp.log.err("Need to give exactly one package to import. Try --help.")
        return 1
    try:
        dirs['tmp_base'] = tempfile.mkdtemp(dir=options.tmp_dir,
                                            prefix='import-srpm')
    except GbpError as err:
        gbp.log.err(err)
        return 1
    try:
        srpm = args[0]
        if options.download:
            srpm = download_source(srpm, dirs)

        # Real srpm, we need to unpack, first
        true_srcrpm = False
        if not os.path.isdir(srpm) and not srpm.endswith(".spec"):
            src = parse_srpm(srpm)
            true_srcrpm = True
            dirs['pkgextract'] = tempfile.mkdtemp(dir=dirs['tmp_base'],
                                                  prefix='pkgextract_')
            gbp.log.info("Extracting src rpm to '%s'" % dirs['pkgextract'])
            src.unpack(dirs['pkgextract'])
            preferred_spec = src.name + '.spec'
            srpm = dirs['pkgextract']
        elif os.path.isdir(srpm):
            preferred_spec = os.path.basename(srpm.rstrip('/')) + '.spec'
        else:
            preferred_spec = None

        # Find and parse spec file
        if os.path.isdir(srpm):
            gbp.log.debug("Trying to import an unpacked srpm from '%s'" % srpm)
            dirs['src'] = os.path.abspath(srpm)
            spec = guess_spec(srpm, True, preferred_spec)
        else:
            gbp.log.debug("Trying to import an srpm from '%s' with spec "\
                          "file '%s'" % (os.path.dirname(srpm), srpm))
            dirs['src'] = os.path.abspath(os.path.dirname(srpm))
            spec = SpecFile(srpm)

        # Check the repository state
        try:
            repo = RpmGitRepository('.')
            is_empty = repo.is_empty()

            (clean, out) = repo.is_clean()
            if not clean and not is_empty:
                gbp.log.err("Repository has uncommitted changes, commit "
                            "these first: ")
                raise GbpError, out

        except GitRepositoryError:
            gbp.log.info("No git repository found, creating one.")
            is_empty = True
            repo = RpmGitRepository.create(spec.name)
            os.chdir(repo.path)

        if repo.bare:
            set_bare_repo_options(options)

        # Create more tempdirs
        dirs['origsrc'] = tempfile.mkdtemp(dir=dirs['tmp_base'],
                                           prefix='origsrc_')
        dirs['packaging_base'] = tempfile.mkdtemp(dir=dirs['tmp_base'],
                                                  prefix='packaging_')
        dirs['packaging'] = os.path.join(dirs['packaging_base'],
                                         options.packaging_dir)
        try:
            os.mkdir(dirs['packaging'])
        except OSError as err:
            if err.errno != errno.EEXIST:
                raise

        if true_srcrpm:
            # For true src.rpm we just take everything
            files = os.listdir(dirs['src'])
        else:
            # Need to copy files to the packaging directory given by caller
            files = [os.path.basename(patch.path) \
                    for patch in spec.patchseries(unapplied=True, ignored=True)]
            for filename in spec.sources().values():
                files.append(filename)
            files.append(os.path.join(spec.specdir, spec.specfile))
        # Don't copy orig source archive, though
        if spec.orig_src and spec.orig_src['filename'] in files:
            files.remove(spec.orig_src['filename'])

        for fname in files:
            fpath = os.path.join(dirs['src'], fname)
            if os.path.exists(fpath):
                shutil.copy2(fpath, dirs['packaging'])
            else:
                gbp.log.err("File '%s' listed in spec not found" % fname)
                raise GbpError

        # Unpack orig source archive
        if spec.orig_src:
            orig_tarball = os.path.join(dirs['src'], spec.orig_src['filename'])
            upstream = RpmUpstreamSource(orig_tarball)
            upstream = upstream.unpack(dirs['origsrc'], options.filters)
        else:
            upstream = None

        tag_format = [(options.upstream_tag, "Upstream"),
                   (options.packaging_tag, options.vendor)][options.native]
        tag_str_fields = dict(spec.version, vendor=options.vendor)
        tag = repo.version_to_tag(tag_format[0], tag_str_fields)

        if repo.find_version(options.packaging_tag, tag_str_fields):
            gbp.log.warn("Version %s already imported." %
                         RpmPkgPolicy.compose_full_version(spec.version))
            if options.allow_same_version:
                gbp.log.info("Moving tag of version '%s' since import forced" %
                        RpmPkgPolicy.compose_full_version(spec.version))
                move_tag_stamp(repo, options.packaging_tag, tag_str_fields)
            else:
                raise SkipImport

        if is_empty:
            options.create_missing_branches = True

        # Determine author and committer info, currently same info is used
        # for both upstream sources and packaging files
        author = None
        if spec.packager:
            match = re.match('(?P<name>.*[^ ])\s*<(?P<email>\S*)>',
                             spec.packager.strip())
            if match:
                author = GitModifier(match.group('name'), match.group('email'))
        if not author:
            author = GitModifier()
            gbp.log.debug("Couldn't determine packager info")
        committer = committer_from_author(author, options)

        # Import upstream sources
        if upstream:
            upstream_commit = repo.find_version(tag_format[0], tag_str_fields)
            if not upstream_commit:
                gbp.log.info("Tag %s not found, importing %s upstream sources"
                             % (tag, tag_format[1]))

                branch = [options.upstream_branch,
                          options.packaging_branch][options.native]
                if not repo.has_branch(branch):
                    if options.create_missing_branches:
                        gbp.log.info("Will create missing branch '%s'" %
                                     branch)
                    else:
                        gbp.log.err(no_upstream_branch_msg % branch + "\n"
                            "Also check the --create-missing-branches option.")
                        raise GbpError

                msg = "%s version %s" % (tag_format[1], spec.upstreamversion)
                if options.vcs_tag:
                    parents = [repo.rev_parse("%s^{}" % options.vcs_tag)]
                else:
                    parents = None
                upstream_commit = repo.commit_dir(upstream.unpacked,
                                                  "Imported %s" % msg,
                                                  branch,
                                                  other_parents=parents,
                                                  author=author,
                                                  committer=committer,
                                                  create_missing_branch=options.create_missing_branches)
                repo.create_tag(name=tag,
                                msg=msg,
                                commit=upstream_commit,
                                sign=options.sign_tags,
                                keyid=options.keyid)

                if not options.native:
                    if options.pristine_tar:
                        archive_fmt = parse_archive_filename(orig_tarball)[1]
                        if archive_fmt == 'tar':
                            repo.pristine_tar.commit(orig_tarball,
                                                    'refs/heads/%s' %
                                                     options.upstream_branch)
                        else:
                            gbp.log.warn('Ignoring pristine-tar, %s archives '
                                         'not supported' % archive_fmt)
        else:
            gbp.log.info("No orig source archive imported")

        # Import packaging files. For native packages we assume that also
        # packaging files are found in the source tarball
        if not options.native or not upstream:
            gbp.log.info("Importing packaging files")
            branch = options.packaging_branch
            if not repo.has_branch(branch):
                if options.create_missing_branches:
                    gbp.log.info("Will create missing branch '%s'" % branch)
                else:
                    gbp.log.err(no_packaging_branch_msg % branch + "\n"
                                "Also check the --create-missing-branches "
                                "option.")
                    raise GbpError

            tag_str_fields = dict(spec.version, vendor=options.vendor)
            tag = repo.version_to_tag(options.packaging_tag, tag_str_fields)
            msg = "%s release %s" % (options.vendor,
                         RpmPkgPolicy.compose_full_version(spec.version))

            if options.orphan_packaging or not upstream:
                commit = repo.commit_dir(dirs['packaging_base'],
                                             "Imported %s" % msg,
                                             branch,
                                             author=author,
                                             committer=committer,
                                             create_missing_branch=options.create_missing_branches)
            else:
                # Copy packaging files to the unpacked sources dir
                try:
                    pkgsubdir = os.path.join(upstream.unpacked,
                                             options.packaging_dir)
                    os.mkdir(pkgsubdir)
                except OSError as err:
                    if err.errno != errno.EEXIST:
                        raise
                for fn in os.listdir(dirs['packaging']):
                    shutil.copy2(os.path.join(dirs['packaging'], fn),
                                 pkgsubdir)
                commit = repo.commit_dir(upstream.unpacked,
                                         "Imported %s" % msg,
                                         branch,
                                         other_parents=[upstream_commit],
                                         author=author,
                                         committer=committer,
                                         create_missing_branch=options.create_missing_branches)
                # Import patches on top of the source tree
                # (only for non-native packages with non-orphan packaging)
                force_to_branch_head(repo, options.packaging_branch)
                if options.patch_import:
                    spec = SpecFile(os.path.join(repo.path,
                                        options.packaging_dir, spec.specfile))
                    import_spec_patches(repo, spec, dirs)
                    commit = options.packaging_branch

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
    except NoSpecError as err:
        gbp.log.err("Failed determine spec file: %s" % err)
        ret = 1
    except PatchImportError as err:
        gbp.log.err(err)
        ret = 2
    except SkipImport:
        skipped = True
    finally:
        os.chdir(dirs['top'])
        gbpc.RemoveTree(dirs['tmp_base'])()

    if not ret and not skipped:
        gbp.log.info("Version '%s' imported under '%s'" %
                     (RpmPkgPolicy.compose_full_version(spec.version),
                      spec.name))
    return ret

if __name__ == '__main__':
    sys.exit(main(sys.argv))

# vim:et:ts=4:sw=4:et:sts=4:ai:set list listchars=tab\:»·,trail\:·:
