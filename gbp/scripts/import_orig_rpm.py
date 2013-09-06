# vim: set fileencoding=utf-8 :
#
# (C) 2006, 2007, 2009, 2011 Guido Guenther <agx@sigxcpu.org>
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
#
"""Import a new upstream version into a git repository"""

import ConfigParser
import os
import sys
import gbp.tmpfile as tempfile
import gbp.command_wrappers as gbpc
import string
from gbp.pkg import parse_archive_filename
from gbp.rpm import (RpmUpstreamSource, SpecFile, NoSpecError, guess_spec,
                     guess_spec_repo)
from gbp.rpm.policy import RpmPkgPolicy
from gbp.rpm.git import (GitRepositoryError, RpmGitRepository)
from gbp.config import GbpOptionParserRpm, GbpOptionGroup, no_upstream_branch_msg
from gbp.errors import GbpError
import gbp.log
from gbp.scripts.common.import_orig import (cleanup_tmp_tree, ask_package_name,
                                            ask_package_version,
                                            prepare_sources)


def upstream_import_commit_msg(options, version):
    return options.import_msg % dict(version=version)


def detect_name_and_version(repo, source, options):
    # Guess defaults for the package name and version from the
    # original tarball.
    (guessed_package, guessed_version) = source.guess_version() or ('', '')

    # Try to find the source package name
    try:
        preferred_fn = os.path.basename(repo.path) + '.spec'
        spec = guess_spec(os.path.join(repo.path, options.packaging_dir), True,
                          preferred_fn)
        sourcepackage = spec.name
    except NoSpecError:
        try:
            # Check the spec file from the repository, in case
            # we're not on the packaging-branch (but upstream, for
            # example).
            spec = guess_spec_repo(repo, options.packaging_branch,
                                   options.packaging_dir, True, preferred_fn)
            sourcepackage = spec.name
        except NoSpecError:
            if options.interactive:
                sourcepackage = ask_package_name(guessed_package,
                                                 RpmPkgPolicy.is_valid_packagename,
                                                 RpmPkgPolicy.packagename_msg)
            else:
                if guessed_package:
                    sourcepackage = guessed_package
                else:
                    raise GbpError, "Couldn't determine upstream package name. Use --interactive."

    # Try to find the version.
    if options.version:
        version = options.version
    else:
        if options.interactive:
            version = ask_package_version(guessed_version,
                                          RpmPkgPolicy.is_valid_upstreamversion,
                                          RpmPkgPolicy.upstreamversion_msg)
        else:
            if guessed_version:
                version = guessed_version
            else:
                raise GbpError, "Couldn't determine upstream version. Use '-u<version>' or --interactive."

    return (sourcepackage, version)


def find_source(options, args):
    """Find the tarball to import
    @return: upstream source filename or None if nothing to import
    @rtype: string
    @raise GbpError: raised on all detected errors
    @todo: implement 'uscan' functionality (i.e. possibility to scan get from upstream source)
    """
    if len(args) > 1: # source specified
        raise GbpError, "More than one archive specified. Try --help."
    elif len(args) == 0:
        raise GbpError, "No archive to import specified. Try --help."
    else:
        return RpmUpstreamSource(args[0])


def pristine_tarball_name(source, pkg_name, pkg_version, pristine_name):
    old_filename = os.path.basename(source.path)
    base_name, _fmt, _comp = parse_archive_filename(old_filename)
    if pristine_name != 'auto':
        ext = string.replace(old_filename, base_name, '', 1)
        return pristine_name % {'name': pkg_name,
                                'version': pkg_version,
                                'upstreamversion': pkg_version,
                                'filename_base': base_name,
                                'filename_ext': ext}
    # Need to repack and mangle filename if the archive is not
    # pristine-tar-compatible -> we decide to create gz compressed tarball
    elif not source.is_tarball():
        return "%s.tar.gz" % base_name
    return old_filename


def set_bare_repo_options(options):
    """Modify options for import into a bare repository"""
    if options.pristine_tar or options.merge:
        gbp.log.info("Bare repository: setting %s%s options"
                      % (["", " '--no-pristine-tar'"][options.pristine_tar],
                         ["", " '--no-merge'"][options.merge]))
        options.pristine_tar = False
        options.merge = False


def parse_args(argv):
    try:
        parser = GbpOptionParserRpm(command=os.path.basename(argv[0]), prefix='',
                                    usage='%prog [options] /path/to/upstream-version.tar.gz | --uscan')
    except ConfigParser.ParsingError, err:
        gbp.log.err(err)
        return None, None

    import_group = GbpOptionGroup(parser, "import options",
                      "pristine-tar and filtering")
    tag_group = GbpOptionGroup(parser, "tag options",
                      "options related to git tag creation")
    branch_group = GbpOptionGroup(parser, "version and branch naming options",
                      "version number and branch layout options")
    cmd_group = GbpOptionGroup(parser, "external command options", "how and when to invoke external commands and hooks")

    for group in [import_group, branch_group, tag_group, cmd_group ]:
        parser.add_option_group(group)

    branch_group.add_option("-u", "--upstream-version", dest="version",
                      help="Upstream Version")
    branch_group.add_config_file_option(option_name="packaging-branch",
                      dest="packaging_branch")
    branch_group.add_config_file_option(option_name="upstream-branch",
                      dest="upstream_branch")
    branch_group.add_option("--upstream-vcs-tag", dest="vcs_tag",
                            help="Upstream VCS tag add to the merge commit")
    branch_group.add_boolean_config_file_option(option_name="merge", dest="merge")
    branch_group.add_config_file_option(option_name="packaging-dir", dest="packaging_dir")

    tag_group.add_boolean_config_file_option(option_name="sign-tags",
                      dest="sign_tags")
    tag_group.add_config_file_option(option_name="keyid",
                      dest="keyid")
    tag_group.add_config_file_option(option_name="upstream-tag",
                      dest="upstream_tag")
    import_group.add_config_file_option(option_name="filter",
                      dest="filters", action="append")
    import_group.add_boolean_config_file_option(option_name="pristine-tar",
                      dest="pristine_tar")
    import_group.add_boolean_config_file_option(option_name="filter-pristine-tar",
                      dest="filter_pristine_tar")
    import_group.add_config_file_option(option_name="pristine-tarball-name",
                      dest="pristine_tarball_name")
    import_group.add_config_file_option(option_name="orig-prefix",
                      dest="orig_prefix")
    import_group.add_config_file_option(option_name="import-msg",
                      dest="import_msg")
    cmd_group.add_config_file_option(option_name="postimport", dest="postimport")

    parser.add_boolean_config_file_option(option_name="interactive",
                                          dest='interactive')
    parser.add_option("-v", "--verbose", action="store_true", dest="verbose", default=False,
                      help="verbose command execution")
    parser.add_config_file_option(option_name="color", dest="color", type='tristate')
    parser.add_config_file_option(option_name="color-scheme",
                                  dest="color_scheme")
    parser.add_config_file_option(option_name="tmp-dir", dest="tmp_dir")

    (options, args) = parser.parse_args(argv[1:])
    gbp.log.setup(options.color, options.verbose, options.color_scheme)

    return options, args


def main(argv):
    ret = 0

    (options, args) = parse_args(argv)
    if not options:
        return 1

    tmpdir = tempfile.mkdtemp(dir=options.tmp_dir, prefix='import-orig-rpm_')
    try:
        source = find_source(options, args)
        try:
            repo = RpmGitRepository('.')
        except GitRepositoryError:
            raise GbpError, "%s is not a git repository" % (os.path.abspath('.'))

        # an empty repo has now branches:
        initial_branch = repo.get_branch()
        is_empty = False if initial_branch else True

        if not repo.has_branch(options.upstream_branch) and not is_empty:
            gbp.log.err(no_upstream_branch_msg % options.upstream_branch)
            raise GbpError

        (sourcepackage, version) = detect_name_and_version(repo, source, options)

        (clean, out) = repo.is_clean()
        if not clean and not is_empty:
            gbp.log.err("Repository has uncommitted changes, commit these first: ")
            raise GbpError, out

        if repo.bare:
            set_bare_repo_options(options)

        # Prepare sources for importing
        if options.pristine_tar:
            prepare_pristine = pristine_tarball_name(source, sourcepackage,
                                                  version,
                                                  options.pristine_tarball_name)
        else:
            prepare_pristine = None
        unpacked_orig, pristine_orig = \
                prepare_sources(source, sourcepackage, version,
                                prepare_pristine, options.filters,
                                options.filter_pristine_tar,
                                options.orig_prefix, tmpdir)

        # Don't mess up our repo with git metadata from an upstream tarball
        if os.path.isdir(os.path.join(unpacked_orig, '.git/')):
            raise GbpError("The orig tarball contains .git metadata - "
                           "giving up.")
        try:
            filter_msg = ["", " (filtering out %s)"
                              % options.filters][len(options.filters) > 0]
            gbp.log.info("Importing '%s' to branch '%s'%s..." % (source.path,
                                                                 options.upstream_branch,
                                                                 filter_msg))
            gbp.log.info("Source package is %s" % sourcepackage)
            gbp.log.info("Upstream version is %s" % version)

            msg = upstream_import_commit_msg(options, version)

            if options.vcs_tag:
                parents = [repo.rev_parse("%s^{}" % options.vcs_tag)]
            else:
                parents = None

            commit = repo.commit_dir(unpacked_orig,
                                     msg=msg,
                                     branch=options.upstream_branch,
                                     other_parents=parents,
                                     create_missing_branch=True,
                                     )
            if options.pristine_tar and pristine_orig:
                gbp.log.info("Pristine-tar: commiting %s" % pristine_orig)
                repo.pristine_tar.commit(pristine_orig, options.upstream_branch)

            tag_str_fields = dict(upstreamversion=version, vendor="Upstream")
            tag = repo.version_to_tag(options.upstream_tag, tag_str_fields)
            repo.create_tag(name=tag,
                            msg="Upstream version %s" % version,
                            commit=commit,
                            sign=options.sign_tags,
                            keyid=options.keyid)
            if options.merge:
                gbp.log.info("Merging to '%s'" % options.packaging_branch)
                if repo.has_branch(options.packaging_branch):
                    repo.set_branch(options.packaging_branch)
                    try:
                        repo.merge(tag)
                    except GitRepositoryError:
                        raise GbpError, """Merge failed, please resolve."""
                else:
                    repo.create_branch(options.packaging_branch, rev=options.upstream_branch)
                    if repo.get_branch() == options.packaging_branch:
                        repo.force_head(options.packaging_branch, hard=True)
                if options.postimport:
                    info = { 'upstreamversion': version }
                    env = { 'GBP_BRANCH': options.packaging_branch }
                    gbpc.Command(options.postimport % info, extra_env=env,
                                 shell=True)()
            # Update working copy and index if we've possibly updated the
            # checked out branch
            current_branch = repo.get_branch()
            if (current_branch == options.upstream_branch or
                current_branch == repo.pristine_tar_branch):
                repo.force_head(current_branch, hard=True)
        except (GitRepositoryError, gbpc.CommandExecFailed):
            raise GbpError, "Import of %s failed" % source.path
    except GbpError, err:
        if len(err.__str__()):
            gbp.log.err(err)
        ret = 1

    if tmpdir:
        cleanup_tmp_tree(tmpdir)

    if not ret:
        gbp.log.info("Successfully imported version %s of %s" % (version, source.path))
    return ret

if __name__ == "__main__":
    sys.exit(main(sys.argv))

# vim:et:ts=4:sw=4:et:sts=4:ai:set list listchars=tab\:»·,trail\:·:
