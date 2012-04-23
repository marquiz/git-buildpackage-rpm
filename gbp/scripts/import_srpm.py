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
import tempfile
import glob
import pipes
import time
import shutil
import errno
import urllib2

import gbp.command_wrappers as gbpc
from gbp.rpm import (parse_srpm, SrcRpmFile, SpecFile, guess_spec, NoSpecError,
                     parse_spec, RpmUpstreamSource)
from gbp.rpm.policy import RpmPkgPolicy
from gbp.rpm.git import (RpmGitRepository, GitRepositoryError)
from gbp.git.modifier import GitModifier
from gbp.config import GbpOptionParserRpm, GbpOptionGroup, no_upstream_branch_msg
from gbp.errors import GbpError
import gbp.log

no_packaging_branch_msg = """
Repository does not have branch '%s' for packaging/distribution sources. If there is none see
file:///usr/share/doc/git-buildpackage/manual-html/gbp.import.html#GBP.IMPORT.CONVERT
on howto create it otherwise use --packaging-branch to specify it.
"""


class SkipImport(Exception):
    pass


def download_source(pkg, dirs):
    if re.match(r'[a-z]{1,5}://', pkg):
        mode = 'python urllib2'
    else:
        mode = 'yumdownloader'

    dirs['download'] = os.path.abspath(tempfile.mkdtemp())
    gbp.log.info("Downloading '%s' using '%s'..." % (pkg, mode))
    if mode == 'yumdownloader':
        gbpc.RunAtCommand('yumdownloader',
                          ['--source', '--destdir=', '.', pkg],
                          shell=False)(dir=dirs['download'])
    else:
        try:
            url = urllib2.urlopen(pkg)
            local_fn = os.path.join(dirs['download'], os.path.basename(pkg))
            with open(local_fn, "wb") as local_file:
                local_file.write(url.read())
        except urllib2.HTTPError as err:
            raise GbpError("Download failed: %s" % err)
        except urllib2.URLError as err:
            raise GbpError("Download failed: %s" % err.reason)
    srpm = glob.glob(os.path.join(dirs['download'], '*.src.rpm'))[0]
    return srpm


def committer_from_author(author, options):
    """Get committer info based on options"""
    committer = GitModifier()
    if options.author_is_committer:
        committer.name = author.name
        committer.email = author.email
    return committer


def move_tag_stamp(repo, format, version, vendor):
    "Move tag out of the way appending the current timestamp"
    old = repo.version_to_tag(format, version, vendor)
    new = repo.version_to_tag('%s~%d' % (format, int(time.time())),
                              version, vendor)
    repo.move_tag(old, new)


def set_bare_repo_options(options):
    """Modify options for import into a bare repository"""
    if options.pristine_tar:
        gbp.log.info("Bare repository: setting %s option"
                      % (["", " '--no-pristine-tar'"][options.pristine_tar], ))
        options.pristine_tar = False


def parse_args(argv):
    try:
        parser = GbpOptionParserRpm(command=os.path.basename(argv[0]), prefix='',
                                    usage='%prog [options] /path/to/package.src.rpm')
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

    parser.add_option("-v", "--verbose", action="store_true", dest="verbose", default=False,
                      help="verbose command execution")
    parser.add_config_file_option(option_name="color", dest="color", type='tristate')
    parser.add_option("--download", action="store_true", dest="download", default=False,
                      help="download source package")
    parser.add_config_file_option(option_name="vendor", action="store", dest="vendor")
    branch_group.add_config_file_option(option_name="packaging-branch",
                      dest="packaging_branch")
    branch_group.add_config_file_option(option_name="upstream-branch",
                      dest="upstream_branch")
    branch_group.add_boolean_config_file_option(option_name="create-missing-branches",
                      dest="create_missing_branches")
    branch_group.add_option("--orphan-packaging", action="store_true",
                      dest="orphan_packaging", default=False,
                      help="The packaging branch doesn't base on upstream")
    branch_group.add_option("--native", action="store_true",
                      dest="native", default=False,
                      help="This is a dist native package, no separate upstream branch")

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
    import_group.add_boolean_config_file_option(option_name="author-is-committer",
                      dest="author_is_committer")
    import_group.add_config_file_option(option_name="packaging-dir",
                      dest="packaging_dir")
    (options, args) = parser.parse_args(argv[1:])
    gbp.log.setup(options.color, options.verbose)
    return options, args


def main(argv):
    dirs = dict(top=os.path.abspath(os.curdir))
    needs_repo = False
    ret = 0
    skipped = False
    parents = None

    options, args = parse_args(argv)

    try:
        if len(args) != 1:
            gbp.log.err("Need to give exactly one package to import. Try --help.")
            raise GbpError
        else:
            pkg = args[0]
            if options.download:
                srpm = download_source(pkg, dirs=dirs)
            else:
                srpm = pkg

            if os.path.isdir(pkg) or pkg.endswith(".spec"):
                if os.path.isdir(pkg):
                    gbp.log.debug("Trying to import an unpacked srpm from '%s'" % pkg)
                    dirs['src'] = os.path.abspath(pkg)
                    spec = parse_spec(guess_spec(pkg, True))
                else:
                    gbp.log.debug("Trying to import an srpm from '%s' with spec file '%s'" % (os.path.dirname(pkg), pkg))
                    dirs['src'] = os.path.abspath(os.path.dirname(pkg))
                    spec = parse_spec(pkg)

                pkgname = spec.name
                pkgver = dict(upstreamversion=spec.version, release=spec.release)
                upstream_version = spec.version
                packager = spec.packager
                unpacked = True
            else:
                gbp.log.debug("Trying to import an source rpm '%s'" % srpm)
                dirs['src'] = os.path.abspath(os.path.dirname(pkg))
                src = parse_srpm(srpm)
                pkgname = src.name
                pkgver = dict(upstreamversion=src.version)
                upstream_version = src.upstream_version
                packager = src.packager
                unpacked = False

            try:
                repo = RpmGitRepository('.')
                is_empty = repo.is_empty()

                (clean, out) = repo.is_clean()
                if not clean and not is_empty:
                    gbp.log.err("Repository has uncommitted changes, commit these first: ")
                    raise GbpError, out

            except GitRepositoryError:
                # no repo found, create one
                needs_repo = True
                is_empty = True

            if needs_repo:
                gbp.log.info("No git repository found, creating one.")
                repo = RpmGitRepository.create(pkgname)
                os.chdir(repo.path)

            if repo.bare:
                set_bare_repo_options(options)

            dirs['pkgextract'] = os.path.abspath(tempfile.mkdtemp(dir='..'))
            dirs['pkgextract-packaging'] = os.path.join(dirs['pkgextract'], options.packaging_dir)
            try:
                os.mkdir(dirs['pkgextract-packaging'])
            except OSError, (e, emsg):
                if e == errno.EEXIST:
                    pass
            dirs['srctarball'] = os.path.abspath(tempfile.mkdtemp(dir='..'))
            dirs['srcunpack'] = os.path.abspath(tempfile.mkdtemp(dir='..'))

            orig_tarball = None
            if unpacked:
                files = [os.path.basename(patch['filename']) for patch in spec.patches.itervalues()]
                for src in spec.sources.itervalues():
                    if src['filename'] != spec.orig_file:
                        files.append(src['filename'])
                files.append(spec.specfile)
                for fname in files:
                    fpath = os.path.join(dirs['src'], fname)
                    if os.path.exists(fpath):
                        shutil.copy2(fpath, dirs['pkgextract-packaging'])
                    else:
                        gbp.log.err("File '%s' listed in spec not found" % fname)
                        raise GbpError
                if spec.orig_base:
                    orig_tarball=os.path.join(dirs['src'], os.path.basename(spec.orig_file))
            else:
                gbp.log.info("Extracting src rpm...")
                src.unpack(dirs['pkgextract-packaging'], dirs['srctarball'])
                if src.orig_file:
                    orig_tarball = os.path.join(dirs['srctarball'], src.orig_file)

            if orig_tarball:
                upstream = RpmUpstreamSource(orig_tarball)
                upstream = upstream.unpack(dirs['srcunpack'], options.filters)
            else:
                upstream = None

            format = [(options.upstream_tag, "Upstream"), (options.packaging_tag, options.vendor)][options.native]
            tag = repo.version_to_tag(format[0], dict(upstreamversion=upstream_version), options.vendor)

            if repo.find_version(options.packaging_tag, pkgver, options.vendor):
                gbp.log.warn("Version %s already imported." % RpmPkgPolicy.compose_full_version(pkgver))
                if options.allow_same_version:
                    gbp.log.info("Moving tag of version '%s' since import forced" % RpmPkgPolicy.compose_full_version(pkgver))
                    move_tag_stamp(repo, options.packaging_tag, pkgver, options.vendor)
                else:
                    raise SkipImport

            if is_empty:
                options.create_missing_branches = True

            # Determine author and committer info, currently same info is used
            # for both upstream sources and packaging files
            author=None
            if packager:
                match = re.match('(?P<name>.*[^ ])\s*<(?P<email>\S*)>', packager.strip())
                if match:
                    author=GitModifier(match.group('name'), match.group('email'))
            if not author:
                author=GitModifier()
                gbp.log.debug("Couldn't determine packager info")
            committer = committer_from_author(author, options)

            # Import upstream sources
            if upstream:
                upstream_commit = repo.find_version(format[0], dict(upstreamversion=upstream_version), options.vendor)
                if not upstream_commit:
                    gbp.log.info("Tag %s not found, importing %s tarball" % (tag, format[1]))

                    branch = [options.upstream_branch,
                              options.packaging_branch][options.native]
                    if not repo.has_branch(branch):
                        if options.create_missing_branches:
                            gbp.log.info("Will create missing branch '%s'" % branch)
                        else:
                            gbp.log.err(no_upstream_branch_msg % branch +
                                "\nAlso check the --create-missing-branches option.")
                            raise GbpError

                    msg = "%s version %s" % (format[1], upstream_version)
                    upstream_commit = repo.commit_dir(upstream.unpacked,
                                                      "Imported %s" % msg,
                                                       branch,
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
                            repo.pristine_tar.commit(orig_tarball, 'refs/heads/%s' % options.upstream_branch)
                        parents = [ options.upstream_branch ]
            else:
                gbp.log.info("No source tarball imported")

            if not options.native or not upstream:
                # Import packaging files
                gbp.log.info("Importing packaging files...")
                branch = options.packaging_branch
                if not repo.has_branch(branch):
                    if options.create_missing_branches:
                        gbp.log.info("Will create missing branch '%s'" % branch)
                    else:
                        gbp.log.err(no_packaging_branch_msg % branch +
                                    "\nAlso check the --create-missing-branches option.")
                        raise GbpError

                tag = repo.version_to_tag(options.packaging_tag, pkgver, options.vendor)
                msg = "%s release %s" % (options.vendor, RpmPkgPolicy.compose_full_version(pkgver))

                if options.orphan_packaging or not upstream:
                    parents = []
                    commit = repo.commit_dir(dirs['pkgextract'],
                                                 "Imported %s" % msg,
                                                 branch,
                                                 author=author,
                                                 committer=committer,
                                                 create_missing_branch=options.create_missing_branches)
                else:
                    # Copy packaging files to the unpacked sources dir
                    try:
                        pkgsubdir = os.path.join(upstream.unpacked, options.packaging_dir)
                        os.mkdir(pkgsubdir)
                    except OSError, (e, emsg):
                        if e == errno.EEXIST:
                            pass
                        else:
                            raise
                    for f in os.listdir(dirs['pkgextract-packaging']):
                        shutil.copy2(os.path.join(dirs['pkgextract-packaging'], f),
                                     pkgsubdir)
                    commit = repo.commit_dir(upstream.unpacked,
                                                 "Imported %s" % msg,
                                                 branch,
                                                 other_parents=[upstream_commit],
                                                 author=author,
                                                 committer=committer,
                                                 create_missing_branch=options.create_missing_branches)

                repo.create_tag(name=tag,
                                msg=msg,
                                commit=commit,
                                sign=options.sign_tags,
                                keyid=options.keyid)

            if repo.get_branch() == options.packaging_branch:
                # Update HEAD if we modified the checked out branch
                repo.force_head(options.packaging_branch, hard=True)
            # Checkout packaging branch
            repo.set_branch(options.packaging_branch)


    except KeyboardInterrupt:
        ret = 1
        gbp.log.err("Interrupted. Aborting.")
    except gbpc.CommandExecFailed:
        ret = 1
    except GitRepositoryError, msg:
        gbp.log.err("Git command failed: %s" % msg)
        ret = 1
    except GbpError, err:
        if len(err.__str__()):
            gbp.log.err(err)
        ret = 1
    except NoSpecError, err:
        gbp.log.err("Failed determine spec file (%s)" % err)
        ret = 1
    except SkipImport:
        skipped = True
    finally:
        os.chdir(dirs['top'])

    for d in [ 'pkgextract', 'srctarball', 'srcunpack', 'download' ]:
        if dirs.has_key(d):
            gbpc.RemoveTree(dirs[d])()

    if not ret and not skipped:
        gbp.log.info("Version '%s' imported under '%s'" % (RpmPkgPolicy.compose_full_version(pkgver), pkgname))
    return ret

if __name__ == '__main__':
    sys.exit(main(sys.argv))

# vim:et:ts=4:sw=4:et:sts=4:ai:set list listchars=tab\:»·,trail\:·:
