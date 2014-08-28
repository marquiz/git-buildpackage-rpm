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
#
"""Build an RPM package out of a Git repo with Bitbake meta data"""

import ConfigParser
import os, os.path
import sys
import shutil
import tempfile

import gbp.rpm as rpm
from gbp.rpm.policy import RpmPkgPolicy
from gbp.command_wrappers import Command, RunAtCommand, CommandExecFailed
from gbp.config import GbpOptionParserBB, GbpOptionGroup
from gbp.rpm.git import (GitRepositoryError, RpmGitRepository)
from gbp.errors import GbpError
import gbp.log
import gbp.notifications
from gbp.scripts.common.buildpackage import (index_name, wc_names, dump_tree,
                                             drop_index)
from gbp.scripts.buildpackage_rpm import (disable_hooks, get_tree,
        get_current_branch, get_upstream_tree, get_vcs_info,
        packaging_tag_name, create_packaging_tag, GbpAutoGenerateError)
from gbp.scripts.import_bb import recursive_copy
from gbp.scripts.pq_bb import update_patch_series
from gbp.scripts.common.pq import is_pq_branch, pq_branch_base
from gbp.bb import (bb, init_tinfoil, guess_bb_path, BBFile, bb_from_repo,
                    pkg_version, parse_bb)

#   pylint: disable=bad-continuation


def guess_export_params(repo, options):
    """Get commit and tree from where to export packaging and patches"""
    tree = None
    branch = None
    if options.export in wc_names.keys() + [index_name, 'HEAD']:
        branch = get_current_branch(repo)
    elif options.export in repo.get_local_branches():
        branch = options.export
    if branch:
        if is_pq_branch(branch, options):
            packaging_branch = pq_branch_base(branch, options)
            if repo.has_branch(packaging_branch):
                gbp.log.info("It seems you're building a development/patch-"
                             "queue branch. Export target changed to '%s' and "
                             "patch-export enabled!" % packaging_branch)
                options.patch_export = True
                if not options.patch_export_rev:
                    options.patch_export_rev = options.export
                options.export = packaging_branch
            else:
                gbp.log.warn("It seems you're building a development/patch-"
                             "queue branch. No corresponding packaging branch "
                             "found. Build may fail!")
    if tree is None:
        tree = get_tree(repo, options.export)

    # Get recipe path
    bb_path = guess_bb_path(options, repo, tree, bbappend=True)
    # Adjust meta-dir accordingly
    options.meta_dir = os.path.dirname(bb_path)

    # Filter out changes in recipe directory
    if options.patch_export:
        relpath = os.path.relpath(os.path.abspath(options.meta_dir), repo.path)
        if relpath != '.':
            gbp.log.info("Auto-excluding changes under meta-dir (%s/)" %
                         relpath)
            if options.patch_export_ignore_path:
                options.patch_export_ignore_path += '|' + relpath + '/*'
            else:
                options.patch_export_ignore_path = relpath + '/*'
    return tree

def guess_export_dir(options, tinfoil, repo, treeish):
    """Guess export directory"""
    if not tinfoil:
        gbp.log.err("Bitbake build environment (bb.tinfoil) not initialized, "
                    "unable to guess export directory")
        gbp.log.err("Please use --git-export-dir or try initializing bitbake "
                    "build environment with the 'oe-init-build-env' script")
        raise GbpError

    gbp.log.info('Guessing export directory')
    tinfoil.parseRecipes()

    # Parse recipe
    bb_path = guess_bb_path(options, repo, treeish, bbappend=True)
    #cfg_data = bb.data.createCopy(tinfoil.config_data)
    #bbfile = bb_from_repo(cfg_data, repo, treeish, bb_path)
    # Use naive parsing, at least for now as the file might be .bbappend
    bbfile = bb_from_repo(None, repo, treeish, bb_path)

    pkg_name = bbfile.getVar('PN', True)
    bb_name = os.path.basename(bb_path)
    if bb_name.endswith('.bb'):
        for name in tinfoil.cooker_data.pkg_fn:
            if os.path.basename(name) == bb_name and os.path.isabs(name):
                gbp.log.debug("Found matching recipe filename: %s" % name)
                return os.path.dirname(name)
    else:
        for name, appends in tinfoil.cooker.collection.appendlist.iteritems():
            print name, appends
            if name.rsplit('_', 1)[0] == pkg_name:
                gbp.log.debug("Found %s from appends" % name)
                for append_name in appends:
                    if  os.path.basename(append_name) == bb_name:
                        gbp.log.debug("Found matching recipe filename: %s" %
                                      append_name)
                        return os.path.dirname(append_name)
                export_dir = os.path.dirname(appends[-1])
                gbp.log.debug("Using existing appends directory %s" %
                              export_dir)
                return export_dir
    if pkg_name in tinfoil.cooker_data.pkg_pn:
        export_dir = os.path.dirname(tinfoil.cooker_data.pkg_pn[pkg_name][-1])
        gbp.log.debug("Using existing package directory %s" % export_dir)
        return export_dir
    else:
        pkg_ver = bbfile.getVar('PV', True)
        raise GbpError("Package %s-%s not found under any configured layer, "
                       "please use --git-export-dir to define the export "
                       "directory" % (pkg_name, pkg_ver))

def export_patches(repo, bbfile, export_treeish, options):
    """Generate patches and update recipe"""
    try:
        if bbfile.getVar('SRCREV', True):
            upstream_tree = bbfile.getVar('SRCREV', True)
        else:
            upstream_version = bbfile.getVar('PV', True)
            upstream_tree = get_upstream_tree(repo, upstream_version, options)
            update_patch_series(repo, bbfile, upstream_tree, export_treeish,
                                options)
    except (GitRepositoryError, GbpError) as err:
        raise GbpAutoGenerateError(str(err))


def is_native(repo, options):
    """Determine whether a package is native or non-native"""
    if options.native.is_auto():
        if repo.has_branch(options.upstream_branch):
            return False
        # Check remotes, too
        for remote_branch in repo.get_remote_branches():
            remote, branch = remote_branch.split('/', 1)
            if branch == options.upstream_branch:
                gbp.log.debug("Found upstream branch '%s' from remote '%s'" %
                               (remote, branch))
                return False
        return True

    return options.native.is_on()


def setup_builder(options, builder_args):
    """Setup everything to use git-pbuilder"""
    # TODO: placeholder for Bitbake: implement or remove entirely
    pass

def bb_get_local_files(bbfile, tgt_dir, whole_dir=False):
    """Get (local) packaging files"""
    if not whole_dir:
        for path in bbfile.localfiles + bbfile.includes + [bbfile.bb_path]:
            relpath = os.path.relpath(path, bbfile.bb_dir)
            subdir = os.path.join(tgt_dir, os.path.dirname(relpath))
            if not os.path.exists(subdir):
                os.makedirs(subdir)
            shutil.copy2(path, os.path.join(tgt_dir, relpath))
    else:
        # Simply copy whole meta dir, if requested
        recursive_copy(bbfile.bb_dir, tgt_dir)

def dump_meta(cfg_data, options, repo, treeish, dump_dir):
    """Parse and dump meta information from a treeish"""
    tmpdir = tempfile.mkdtemp(prefix='gbp-bb_')
    try:
        bb_path = guess_bb_path(options, repo, treeish, bbappend=True)
        # Dump whole meta directory
        dump_tree(repo, tmpdir, '%s:%s' % (treeish, os.path.dirname(bb_path)),
                  False)
        # Parse recipe
        full_path = os.path.join(tmpdir, os.path.basename(bb_path))
        bbfile = BBFile(full_path, cfg_data)
        bb_get_local_files(bbfile, dump_dir)
    except GitRepositoryError as err:
        raise GbpError("Git error: %s" % err)
    finally:
        shutil.rmtree(tmpdir)

    # Re-parse recipe from final location
    full_path = os.path.abspath(os.path.join(dump_dir,
                                             os.path.basename(bb_path)))
    return BBFile(full_path, cfg_data)


def build_parser(name, prefix=None, git_treeish=None):
    """Create command line parser"""
    try:
        parser = GbpOptionParserBB(command=os.path.basename(name),
                                   prefix=prefix, git_treeish=git_treeish)
    except ConfigParser.ParsingError, err:
        gbp.log.err(err)
        return None

    tag_group = GbpOptionGroup(parser, "tag options",
                    "options related to git tag creation")
    branch_group = GbpOptionGroup(parser, "branch options",
                    "branch layout options")
    cmd_group = GbpOptionGroup(parser, "external command options",
                    "how and when to invoke external commands and hooks")
    orig_group = GbpOptionGroup(parser, "orig tarball options",
                    "options related to the creation of the orig tarball")
    export_group = GbpOptionGroup(parser, "export build-tree options",
                    "alternative build tree related options")
    parser.add_option_group(tag_group)
    parser.add_option_group(orig_group)
    parser.add_option_group(branch_group)
    parser.add_option_group(cmd_group)
    parser.add_option_group(export_group)

    parser.add_boolean_config_file_option(option_name = "ignore-untracked",
                    dest="ignore_untracked")
    parser.add_boolean_config_file_option(option_name = "ignore-new",
                    dest="ignore_new")
    parser.add_option("--git-verbose", action="store_true", dest="verbose",
                    help="verbose command execution")
    parser.add_config_file_option(option_name="tmp-dir", dest="tmp_dir")
    parser.add_config_file_option(option_name="color", dest="color",
                    type='tristate')
    parser.add_config_file_option(option_name="color-scheme",
                    dest="color_scheme")
    parser.add_config_file_option(option_name="notify", dest="notify",
                    type='tristate')
    parser.add_config_file_option(option_name="vendor", action="store",
                    dest="vendor")
    parser.add_config_file_option(option_name="native", dest="native",
                    type='tristate')
    tag_group.add_option("--git-tag", action="store_true", dest="tag",
                    help="create a tag after a successful build")
    tag_group.add_option("--git-tag-only", action="store_true", dest="tag_only",
                    help="don't build, only tag and run the posttag hook")
    tag_group.add_option("--git-retag", action="store_true", dest="retag",
                    help="don't fail if the tag already exists")
    tag_group.add_boolean_config_file_option(option_name="sign-tags",
                    dest="sign_tags")
    tag_group.add_config_file_option(option_name="keyid", dest="keyid")
    tag_group.add_config_file_option(option_name="packaging-tag",
                    dest="packaging_tag")
    tag_group.add_config_file_option(option_name="upstream-tag",
                    dest="upstream_tag")
    orig_group.add_config_file_option(option_name="upstream-tree",
                    dest="upstream_tree")
    branch_group.add_config_file_option(option_name="upstream-branch",
                    dest="upstream_branch")
    branch_group.add_config_file_option(option_name="packaging-branch",
                    dest="packaging_branch")
    branch_group.add_config_file_option(option_name="pq-branch",
                    dest="pq_branch")
    branch_group.add_boolean_config_file_option(option_name = "ignore-branch",
                    dest="ignore_branch")
    cmd_group.add_config_file_option(option_name="builder", dest="builder",
                    help="command to build the package, default is "
                         "'%(builder)s'")
    cmd_group.add_config_file_option(option_name="cleaner", dest="cleaner",
                    help="command to clean the working copy, default is "
                         "'%(cleaner)s'")
    cmd_group.add_config_file_option(option_name="prebuild", dest="prebuild",
                    help="command to run before a build, default is "
                         "'%(prebuild)s'")
    cmd_group.add_config_file_option(option_name="postexport",
                    dest="postexport",
                    help="command to run after exporting the source tree, "
                         "default is '%(postexport)s'")
    cmd_group.add_config_file_option(option_name="postbuild", dest="postbuild",
                    help="hook run after a successful build, default is "
                          "'%(postbuild)s'")
    cmd_group.add_config_file_option(option_name="posttag", dest="posttag",
                    help="hook run after a successful tag operation, default "
                         "is '%(posttag)s'")
    cmd_group.add_boolean_config_file_option(option_name="hooks", dest="hooks")
    export_group.add_option("--git-no-build", action="store_true",
                    dest="no_build",
                    help="Don't run builder or the associated hooks")
    export_group.add_config_file_option(option_name="export-dir",
                    dest="export_dir", type="path",
                    help="Build topdir, also export the sources under "
                         "EXPORT_DIR, default is '%(export-dir)s'")
    export_group.add_config_file_option("export", dest="export",
                    help="export treeish object TREEISH, default is "
                         "'%(export)s'", metavar="TREEISH")
    export_group.add_config_file_option(option_name="meta-dir",
                    dest="meta_dir")
    export_group.add_config_file_option(option_name="bb-file", dest="bb_file")
    export_group.add_boolean_config_file_option("patch-export",
                    dest="patch_export")
    export_group.add_option("--git-patch-export-rev", dest="patch_export_rev",
                    metavar="TREEISH",
                    help="[experimental] Export patches from treeish object "
                         "TREEISH")
    export_group.add_config_file_option("patch-export-ignore-path",
                    dest="patch_export_ignore_path")
    export_group.add_config_file_option("patch-export-compress",
                    dest="patch_export_compress")
    export_group.add_config_file_option("patch-export-squash-until",
                    dest="patch_export_squash_until")
    export_group.add_boolean_config_file_option(option_name="patch-numbers",
                    dest="patch_numbers")
    export_group.add_config_file_option("bb-vcs-info", dest="bb_vcs_info")
    return parser

def parse_args(argv, prefix, git_treeish=None):
    """Parse config and command line arguments"""
    args = [arg for arg in argv[1:] if arg.find('--%s' % prefix) == 0]
    builder_args = [arg for arg in argv[1:] if arg.find('--%s' % prefix) == -1]

    # We handle these although they don't have a --git- prefix
    for arg in ["--help", "-h", "--version"]:
        if arg in builder_args:
            args.append(arg)

    parser = build_parser(argv[0], prefix=prefix, git_treeish=git_treeish)
    if not parser:
        return None, None, None
    options, args = parser.parse_args(args)

    options.patch_export_compress = rpm.string_to_int(
                options.patch_export_compress)

    gbp.log.setup(options.color, options.verbose, options.color_scheme)
    if not options.hooks:
        disable_hooks(options)
    if options.retag:
        if not options.tag and not options.tag_only:
            gbp.log.err("'--%sretag' needs either '--%stag' or '--%stag-only'" %
                        (prefix, prefix, prefix))
            return None, None, None

    return options, args, builder_args


def main(argv):
    """Entry point for git-buildpackage-bb"""
    retval = 0
    prefix = "git-"
    bbfile = None
    dump_dir = None

    if not bb:
        return 1

    options, gbp_args, builder_args = parse_args(argv, prefix)
    if not options:
        return 1

    try:
        repo = RpmGitRepository(os.path.curdir)
    except GitRepositoryError:
        gbp.log.err("%s is not a git repository" % (os.path.abspath('.')))
        return 1

    # Determine tree-ish to be exported
    try:
        tree = get_tree(repo, options.export)
    except GbpError as err:
        gbp.log.err('Failed to determine export treeish: %s' % err)
        return 1
    # Re-parse config options with using the per-tree config file(s) from the
    # exported tree-ish
    options, gbp_args, builder_args = parse_args(argv, prefix, tree)

    branch = get_current_branch(repo)

    try:
        tinfoil = init_tinfoil(config_only=True)
        #bb_cfg_data = bb.data.createCopy(tinfoil.config_data)
    except GbpError:
        tinfoil = None

    # Use naive parsing because repository might only have .bb file
    gbp.log.info("Using naive standalone parsing of recipes in package repo.")
    bb_cfg_data = None

    try:
        tree = guess_export_params(repo, options)

        Command(options.cleaner, shell=True)()
        if not options.ignore_new:
            (ret, out) = repo.is_clean(options.ignore_untracked)
            if not ret:
                gbp.log.err("You have uncommitted changes in your source tree:")
                gbp.log.err(out)
                raise GbpError("Use --git-ignore-new or --git-ignore-untracked "
                               "to ignore.")

        if not options.ignore_new and not options.ignore_branch:
            if branch != options.packaging_branch:
                gbp.log.err("You are not on branch '%s' but on '%s'" %
                            (options.packaging_branch, branch))
                raise GbpError("Use --git-ignore-branch to ignore or "
                               "--git-packaging-branch to set the branch name.")

        if not options.tag_only:
            # Dump/parse meta to export dir
            if options.export_dir:
                export_dir = os.path.abspath(options.export_dir)
            else:
                export_dir = guess_export_dir(options, tinfoil, repo, tree)
            gbp.log.info("Dumping meta from tree '%s' to '%s'" %
                            (options.export, export_dir))
            bbfile = dump_meta(bb_cfg_data, options, repo, tree,
                                 export_dir)

            # Setup builder opts
            setup_builder(options, builder_args)

            if is_native(repo, options) and bbfile.getVar('SRCREV') == 'HEAD':
                # Update SRCREV for native packages that are exported from
                # pristine repository
                BBFile.set_var_val(bbfile.bb_path, 'SRCREV',
                                   repo.rev_parse(tree))

                # TODO: Re-design the handling of native packages. Updating
                #       SRCREV must probably be more explicit
            if options.patch_export:
                # Generate patches, if requested
                if options.patch_export_rev:
                    patch_tree = get_tree(repo, options.patch_export_rev)
                else:
                    patch_tree = tree
                export_patches(repo, bbfile, patch_tree, options)

            # Run postexport hook
            if options.postexport:
                RunAtCommand(options.postexport, shell=True,
                             extra_env={'GBP_GIT_DIR': repo.git_dir,
                                        'GBP_TMP_DIR': export_dir}
                             )(dir=export_dir)
            # Do actual build
            if not options.no_build:
                if options.prebuild:
                    RunAtCommand(options.prebuild, shell=True,
                                 extra_env={'GBP_GIT_DIR': repo.git_dir,
                                            'GBP_BUILD_DIR': export_dir}
                                 )(dir=export_dir)

                # Unlock cooker so that we are able to run external bitbake
                if options.builder == 'bitbake' and tinfoil:
                    bb.utils.unlockfile(tinfoil.cooker.lock)

                # Finally build the package:
                bb_path = bbfile.getVar('FILE', True)
                builder_args.extend(['-b', bb_path])
                RunAtCommand(options.builder, builder_args, shell=True,
                             extra_env={'GBP_BUILD_DIR': export_dir})()

                if options.postbuild:
                    Command(options.postbuild, shell=True,
                            extra_env={'GBP_BUILD_DIR': export_dir})()
        else:
            # Tag-only: we just need to parse the meta
            bbfile = parse_bb(bb_cfg_data, options, repo, tree)

        # Tag (note: tags the exported version)
        if options.tag or options.tag_only:
            version = pkg_version(bbfile)
            gbp.log.info("Tagging %s" %
                         RpmPkgPolicy.compose_full_version(version))
            commit_info = repo.get_commit_info(tree)
            tag = packaging_tag_name(repo, version, commit_info, options)
            if options.retag and repo.has_tag(tag):
                repo.delete_tag(tag)
            create_packaging_tag(repo, tag, commit=tree, version=version,
                                 options=options)
            vcs_info = get_vcs_info(repo, tag)
            if options.posttag:
                sha = repo.rev_parse("%s^{}" % tag)
                Command(options.posttag, shell=True,
                        extra_env={'GBP_TAG': tag,
                                   'GBP_BRANCH': branch,
                                   'GBP_SHA1': sha})()
        else:
            vcs_info = get_vcs_info(repo, tree)
        # TODO: Put VCS information to recipe
        if options.bb_vcs_info:
            raise GbpError("Injecting VCS info into recipe not yet supported")

    except CommandExecFailed:
        retval = 1
    except GitRepositoryError as err:
        gbp.log.err("Git command failed: %s" % err)
        retval = 1
    except GbpAutoGenerateError as err:
        if len(err.__str__()):
            gbp.log.err(err)
        retval = 2
    except GbpError, err:
        if len(err.__str__()):
            gbp.log.err(err)
        retval = 1
    finally:
        drop_index(repo)
        if dump_dir and os.path.exists(dump_dir):
            shutil.rmtree(dump_dir)

    if not options.tag_only:
        if bbfile and options.notify:
            summary = "GBP buildpackage-bb %s" % \
                        ["failed", "successful"][not retval]
            message = ("Build of %s %s %s" % (bbfile.getVar('PN', True),
                       RpmPkgPolicy.compose_full_version(pkg_version(bbfile)),
                       ["failed", "succeeded"][not retval]))
            if not gbp.notifications.notify(summary, message, options.notify):
                gbp.log.err("Failed to send notification")
                retval = 1

    return retval

if __name__ == '__main__':
    sys.exit(main(sys.argv))
