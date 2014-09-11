# vim: set fileencoding=utf-8 :
#
# (C) 2011 Guido Günther <agx@sigxcpu.org>
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
"""manage patches in a patch queue"""

import ConfigParser
import errno
import os
import shutil
import sys

import gbp.tmpfile as tempfile
from gbp.config import GbpOptionParserBB
from gbp.rpm.git import GitRepositoryError, RpmGitRepository
from gbp.command_wrappers import GitCommand, CommandExecFailed
from gbp.errors import GbpError
import gbp.log
from gbp.patch_series import PatchSeries, Patch
from gbp.rpm import string_to_int
from gbp.scripts.common.pq import (is_pq_branch, pq_branch_name, pq_branch_base,
                                   apply_and_commit_patch, drop_pq)
from gbp.scripts.pq_rpm import (generate_patches, safe_patches,
                                import_extra_files)
from gbp.bb import bb, init_tinfoil, parse_bb, pkg_version

#   pylint: disable=bad-continuation

USAGE_STRING = \
"""%prog [options] action - maintain patches on a patch queue branch
tions:
export         Export the patch queue / devel branch associated to the
               current branch into a patch series in and update the recipe file
import         Create a patch queue / devel branch from recipe file
               and patches in current dir.
rebase         Switch to patch queue / devel branch associated to the current
               branch and rebase against upstream.
drop           Drop (delete) the patch queue /devel branch associated to
               the current branch.
apply          Apply a patch
switch         Switch to patch-queue branch and vice versa."""


def rm_patch_files(bbfile):
    """Delete the patch files listed in the pkg meta data."""
    unlinked = set()

    # Go through local files
    for path in bbfile.localfiles:
        if path.endswith('.patch'):
            gbp.log.debug("Removing patch '%s'" % path)
            unlinked.add(os.path.basename(path))
            try:
                os.unlink(path)
            except OSError as err:
                if err.errno != errno.ENOENT:
                    raise GbpError("Failed to remove patch: %s" % err)
                else:
                    gbp.log.debug("Patch %s does not exist." % path)
        else:
            gbp.log.debug("Unlink skipping non-local/non-patch file %s" % path)
    uris = (bbfile.getVar('SRC_URI', False) or "").split()
    return [uri for uri in uris if os.path.basename(uri) not in unlinked]


def update_patch_series(repo, bbfile, start, end, options):
    """Export patches to packaging directory and update recipe file"""
    squash = options.patch_export_squash_until.split(':', 1)
    if len(squash) == 1:
        squash.append(None)
    else:
        squash[1] += '.diff'

    # Unlink old (local) patch files and generate new patches
    rm_patch_files(bbfile)

    # Guess patch subdir
    bb_dir = os.path.dirname(bbfile.getVar('FILE', True))
    pkg_name = bbfile.getVar('PN', True)
    pkg_ver = bbfile.getVar('PV', True)
    subdir = pkg_name + '-' + pkg_ver
    if not os.path.isdir(os.path.join(bb_dir, subdir)):
        if os.path.isdir(os.path.join(bb_dir, pkg_name)):
            subdir = pkg_name
        elif os.path.isdir(os.path.join(bb_dir, 'files')):
            subdir = 'files'
    tgt_dir = os.path.join(bb_dir, subdir)

    patches, _commands = generate_patches(repo, start, squash, end,
                                          tgt_dir, options)
    # TODO: implement commands processing (e.g. topic)
    new_uris = ['file://' + patch for patch in patches]
    bbfile.substitute_var_val(bbfile.bb_path, 'SRC_URI', r'file://\S+.\.patch',
                              '')
    bbfile.append_var_val(bbfile.bb_path, 'SRC_URI', new_uris)
    return patches

def var_to_str(var, value):
    """Create a well formatted string buffer for a variable assignment"""
    indent = ' ' *  (len(var) + 3)
    linebuf = ['%s = "%s \\\n' % (var, value[0])]
    for val in value[1:]:
        linebuf.append(indent + ' ' + val + '\\\n')
    linebuf.append(indent + '"\n')
    return linebuf


def find_upstream_commit(repo, bbfile, upstream_tag):
    """Find commit corresponding upstream version"""
    src_rev = bbfile.getVar('SRCREV', True)
    if src_rev and src_rev != 'INVALID':
        return bbfile.getVar('SRCREV', True)

    # Find tag
    upstreamversion = bbfile.getVar('PV', True)
    tag_str_fields = {'upstreamversion': upstreamversion,
                      'vendor': 'Upstream'}
    upstream_commit = repo.find_version(upstream_tag, tag_str_fields)
    if not upstream_commit:
        raise GbpError("Couldn't find upstream version %s" % upstreamversion)
    return upstream_commit


def export_patches(cfg, repo, options):
    """Export patches from the pq branch into a packaging branch"""
    current = repo.get_branch()
    if is_pq_branch(current, options):
        base = pq_branch_base(current, options)
        gbp.log.info("On branch '%s', switching to '%s'" % (current, base))
        repo.set_branch(base)
        bbfile = parse_bb(cfg, options, repo)
        pq_branch = current
    else:
        bbfile = parse_bb(cfg, options, repo)
        pq_branch = pq_branch_name(current, options, pkg_version(bbfile))
    upstream_commit = find_upstream_commit(repo, bbfile, options.upstream_tag)

    export_treeish = options.export_rev if options.export_rev else pq_branch

    update_patch_series(repo, bbfile, upstream_commit, export_treeish, options)

    bb_dir = os.path.dirname(bbfile.getVar('FILE', True))
    GitCommand('status')(['--', bb_dir])


def bb_to_patch_series(bbfile):
    """Get all local patches as a series"""
    series = PatchSeries()
    for path in bbfile.localfiles:
        if path.endswith('.patch'):
            series.append(Patch(path))
    return series


def import_bb_patches(cfg, repo, options):
    """Apply a series of patches in a recipe to branch onto a pq branch"""
    current = repo.get_branch()

    if is_pq_branch(current, options):
        base = pq_branch_base(current, options)
        raise GbpError("Already on a patch-queue branch '%s' - doing "
                       "nothing." % current)
    else:
        bbfile = parse_bb(cfg, options, repo)
        base = current
    upstream_commit = find_upstream_commit(repo, bbfile, options.upstream_tag)
    pq_branch = pq_branch_name(base, options, pkg_version(bbfile))

    # Create pq-branch
    if repo.has_branch(pq_branch) and not options.force:
        raise GbpError("Patch-queue branch '%s' already exists. "
                       "Try 'rebase' instead." % pq_branch)
    try:
        if repo.get_branch() == pq_branch:
            repo.force_head(upstream_commit, hard=True)
        else:
            repo.create_branch(pq_branch, upstream_commit, force=True)
    except GitRepositoryError as err:
        raise GbpError("Cannot create patch-queue branch '%s': %s" %
                        (pq_branch, err))

    # Put patches in a safe place
    in_queue = bb_to_patch_series(bbfile)
    queue = safe_patches(in_queue, options.tmp_dir)
    # Do import
    try:
        gbp.log.info("Switching to branch '%s'" % pq_branch)
        repo.set_branch(pq_branch)
        import_extra_files(repo, base, options.import_files)

        if not queue:
            return
        gbp.log.info("Trying to apply patches from branch '%s' onto '%s'" %
                        (base, upstream_commit))
        for patch in queue:
            gbp.log.debug("Applying %s" % patch.path)
            apply_and_commit_patch(repo, patch, fallback_author=None)
    except (GbpError, GitRepositoryError) as err:
        gbp.log.err('Import failed: %s' % err)
        repo.force_head('HEAD', hard=True)
        repo.set_branch(base)
        repo.delete_branch(pq_branch)
        raise

    recipe_fn = os.path.basename(bbfile.getVar('FILE', True))
    gbp.log.info("Patches listed in '%s' imported on '%s'" % (recipe_fn,
                                                              pq_branch))


def rebase_pq(cfg, repo, options):
    """Rebase pq branch on the correct upstream version"""
    current = repo.get_branch()
    if is_pq_branch(current, options):
        base = pq_branch_base(current, options)
        bbfile = parse_bb(cfg, options, repo, base)
    else:
        base = current
        bbfile = parse_bb(cfg, options, repo)
    upstream_commit = find_upstream_commit(repo, bbfile, options.upstream_tag)

    switch_to_pq_branch(cfg, repo, base, options)
    GitCommand("rebase")([upstream_commit])


def switch_pq(cfg, repo, options):
    """Switch to patch-queue branch if on base branch and vice versa"""
    current = repo.get_branch()
    if is_pq_branch(current, options):
        base = pq_branch_base(current, options)
        gbp.log.info("Switching to branch '%s'" % base)
        repo.checkout(base)
    else:
        switch_to_pq_branch(cfg, repo, current, options)


def drop_pq_bb(cfg, repo, options):
    """Remove pq branch"""
    current = repo.get_branch()
    if is_pq_branch(current, options):
        base = pq_branch_base(current, options)
        bbfile = parse_bb(cfg, options, repo, base)
    else:
        bbfile = parse_bb(cfg, options, repo)
    drop_pq(repo, current, options, pkg_version(bbfile))


def switch_to_pq_branch(cfg, repo, branch, options):
    """
    Switch to patch-queue branch if not already there, create it if it
    doesn't exist yet
    """
    if is_pq_branch(branch, options):
        return

    bbfile = parse_bb(cfg, options, repo, branch)
    pq_branch = pq_branch_name(branch, options, pkg_version(bbfile))
    if not repo.has_branch(pq_branch):
        raise GbpError("Branch '%s' does not exist" % pq_branch)

    gbp.log.info("Switching to branch '%s'" % pq_branch)
    repo.set_branch(pq_branch)

def apply_single_patch(cfg, repo, patchfile, options):
    """Apply a single patch onto the pq branch"""
    current = repo.get_branch()
    if not is_pq_branch(current, options):
        switch_to_pq_branch(cfg, repo, current, options)
    patch = Patch(patchfile)
    apply_and_commit_patch(repo, patch, fallback_author=None)

def opt_split_cb(option, opt_str, value, parser):
    """Split option string into a list"""
    setattr(parser.values, option.dest, value.split(','))

def build_parser(name):
    """Create command line argument parser"""
    try:
        parser = GbpOptionParserBB(command=os.path.basename(name),
                                   prefix='', usage=USAGE_STRING)
    except ConfigParser.ParsingError as err:
        gbp.log.err(err)
        return None

    parser.add_boolean_config_file_option(option_name="patch-numbers",
            dest="patch_numbers")
    parser.add_option("-v", "--verbose", action="store_true", dest="verbose",
            default=False, help="Verbose command execution")
    parser.add_option("--force", dest="force", action="store_true",
            default=False,
            help="In case of import even import if the branch already exists")
    parser.add_config_file_option(option_name="vendor", action="store",
            dest="vendor")
    parser.add_config_file_option(option_name="color", dest="color",
            type='tristate')
    parser.add_config_file_option(option_name="color-scheme",
            dest="color_scheme")
    parser.add_config_file_option(option_name="tmp-dir", dest="tmp_dir")
    parser.add_config_file_option(option_name="upstream-tag",
            dest="upstream_tag")
    parser.add_config_file_option(option_name="bb-file", dest="bb_file")
    parser.add_config_file_option(option_name="meta-dir",
            dest="meta_dir")
    parser.add_config_file_option(option_name="packaging-branch",
            dest="packaging_branch",
            help="Branch the packaging is being maintained on. Only relevant "
                 "if a invariable/single pq-branch is defined, in which case "
                 "this is used as the 'base' branch. Default is "
                 "'%(packaging-branch)s'")
    parser.add_config_file_option(option_name="pq-branch", dest="pq_branch")
    parser.add_config_file_option(option_name="import-files",
            dest="import_files", type="string", action="callback",
            callback=opt_split_cb)
    parser.add_option("--export-rev", action="store", dest="export_rev",
            default="",
            help="Export patches from treeish object TREEISH instead of head "
                 "of patch-queue branch", metavar="TREEISH")
    parser.add_config_file_option("patch-export-compress",
            dest="patch_export_compress")
    parser.add_config_file_option("patch-export-squash-until",
            dest="patch_export_squash_until")
    parser.add_config_file_option("patch-export-ignore-path",
            dest="patch_export_ignore_path")
    return parser

def parse_args(argv):
    """Parse command line arguments"""
    parser = build_parser(argv[0])
    if not parser:
        return None, None

    options, args = parser.parse_args(argv)
    gbp.log.setup(options.color, options.verbose, options.color_scheme)
    options.patch_export_compress = string_to_int(options.patch_export_compress)

    return options, args


def main(argv):
    """Main function for the gbp pq-rpm command"""
    retval = 0

    if not bb:
        return 1

    options, args = parse_args(argv)
    if not options:
        return 1

    if len(args) < 2:
        gbp.log.err("No action given.")
        return 1
    else:
        action = args[1]

    if args[1] in ["export", "import", "rebase", "drop", "switch"]:
        pass
    elif args[1] in ["apply"]:
        if len(args) != 3:
            gbp.log.err("No patch name given.")
            return 1
        else:
            patchfile = args[2]
    else:
        gbp.log.err("Unknown action '%s'." % args[1])
        return 1

    try:
        repo = RpmGitRepository(os.path.curdir)
    except GitRepositoryError:
        gbp.log.err("%s is not a git repository" % (os.path.abspath('.')))
        return 1

    if os.path.abspath('.') != repo.path:
        gbp.log.warn("Switching to topdir before running commands")
        os.chdir(repo.path)

    try:
        # Initialize BitBake
        tinfoil = init_tinfoil(config_only=True, tracking=True)
        bb_cfg_data = bb.data.createCopy(tinfoil.config_data)

        # Create base temporary directory for this run
        options.tmp_dir = tempfile.mkdtemp(dir=options.tmp_dir,
                                           prefix='gbp-pq-bb_')
        if action == "export":
            export_patches(bb_cfg_data, repo, options)
        elif action == "import":
            import_bb_patches(bb_cfg_data, repo, options)
        elif action == "drop":
            drop_pq_bb(bb_cfg_data, repo, options)
        elif action == "rebase":
            rebase_pq(bb_cfg_data, repo, options)
        elif action == "apply":
            apply_single_patch(bb_cfg_data, repo, patchfile, options)
        elif action == "switch":
            switch_pq(bb_cfg_data, repo, options)
    except CommandExecFailed:
        retval = 1
    except GitRepositoryError as err:
        gbp.log.err("Git command failed: %s" % err)
        retval = 1
    except GbpError, err:
        if len(err.__str__()):
            gbp.log.err(err)
        retval = 1
    finally:
        shutil.rmtree(options.tmp_dir, ignore_errors=True)

    return retval

if __name__ == '__main__':
    sys.exit(main(sys.argv))

