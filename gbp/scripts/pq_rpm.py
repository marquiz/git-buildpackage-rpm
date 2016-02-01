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
import re
import gzip
import bz2
import subprocess
import gbp.tmpfile as tempfile
from gbp.config import GbpOptionParserRpm
from gbp.rpm.git import GitRepositoryError, RpmGitRepository
from gbp.git.modifier import GitModifier, GitTz
from gbp.command_wrappers import GitCommand, CommandExecFailed
from gbp.errors import GbpError
import gbp.log
from gbp.patch_series import PatchSeries, Patch
from gbp.pkg import parse_archive_filename
from gbp.rpm import (SpecFile, NoSpecError, guess_spec, guess_spec_repo,
                     spec_from_repo, string_to_int)
from gbp.scripts.common.pq import (is_pq_branch, pq_branch_name, pq_branch_base,
                                   parse_gbp_commands, format_patch,
                                   format_diff, apply_and_commit_patch, drop_pq)
from gbp.scripts.common.buildpackage import dump_tree

USAGE_STRING = \
"""%prog [options] action - maintain patches on a patch queue branch
tions:
export         Export the patch queue / devel branch associated to the
               current branch into a patch series in and update the spec file
import         Create a patch queue / devel branch from spec file
               and patches in current dir.
rebase         Switch to patch queue / devel branch associated to the current
               branch and rebase against upstream.
drop           Drop (delete) the patch queue /devel branch associated to
               the current branch.
apply          Apply a patch
switch         Switch to patch-queue branch and vice versa.
convert        [experimental] Convert package from single-branch development
               model (packaging and source code changes in the same branch)
               into the orphan-packaging plus patch-queue / development branch
               development model."""

def compress_patches(patches, compress_size=0):
    """
    Rename and/or compress patches
    """
    ret_patches = []
    for patch in patches:
        # Compress if patch file is larger than "threshold" value
        suffix = ''
        if compress_size and os.path.getsize(patch) > compress_size:
            gbp.log.debug("Compressing %s" % os.path.basename(patch))
            subprocess.Popen(['gzip', '-n', patch]).communicate()
            suffix = '.gz'

        ret_patches.append(os.path.basename(patch) + suffix)
    return ret_patches

def is_ancestor(repo, parent, child):
    """Check if commit is ancestor of another"""
    parent_sha1 = repo.rev_parse("%s^0" % parent)
    child_sha1 = repo.rev_parse("%s^0" % child)
    try:
        merge_base = repo.get_merge_base(parent_sha1, child_sha1)
    except GitRepositoryError:
        merge_base = None
    return merge_base == parent_sha1

def generate_patches(repo, start, squash, end, outdir, options):
    """
    Generate patch files from git
    """
    gbp.log.info("Generating patches from git (%s..%s)" % (start, end))
    patches = []
    commands = {}
    for treeish in [start, end]:
        if not repo.has_treeish(treeish):
            raise GbpError('Invalid treeish object %s' % treeish)

    start_sha1 = repo.rev_parse("%s^0" % start)
    try:
        end_commit = end
        end_commit_sha1 = repo.rev_parse("%s^0" % end_commit)
    except GitRepositoryError:
        # In case of plain tree-ish objects, assume current branch head is the
        # last commit
        end_commit = "HEAD"
        end_commit_sha1 = repo.rev_parse("%s^0" % end_commit)

    start_sha1 = repo.rev_parse("%s^0" % start)

    if not is_ancestor(repo, start_sha1, end_commit_sha1):
        raise GbpError("Start commit '%s' not an ancestor of end commit "
                       "'%s'" % (start, end_commit))
    # Squash commits, if requested
    if squash[0]:
        if squash[0] == 'HEAD':
            squash[0] = end_commit
        squash_sha1 = repo.rev_parse("%s^0" % squash[0])
        if start_sha1 != squash_sha1:
            if not squash_sha1 in repo.get_commits(start, end_commit):
                raise GbpError("Given squash point '%s' not in the history "
                               "of end commit '%s'" % (squash[0], end_commit))
            # Shorten SHA1s
            squash_sha1 = repo.rev_parse(squash_sha1, short=7)
            start_sha1 = repo.rev_parse(start_sha1, short=7)
            gbp.log.info("Squashing commits %s..%s into one monolithic diff" %
                         (start_sha1, squash_sha1))
            patch_fn = format_diff(outdir, squash[1], repo,
                                   start_sha1, squash_sha1,
                                   options.patch_export_ignore_path)
            if patch_fn:
                patches.append(patch_fn)
                start = squash_sha1
    # Check for merge commits, yet another squash if merges found
    merges = repo.get_commits(start, end_commit, options=['--merges'])
    if merges:
        # Shorten SHA1s
        start_sha1 = repo.rev_parse(start, short=7)
        merge_sha1 = repo.rev_parse(merges[0], short=7)
        patch_fn = format_diff(outdir, None, repo, start_sha1, merge_sha1,
                               options.patch_export_ignore_path)
        if patch_fn:
            gbp.log.info("Merge commits found! Diff between %s..%s written "
                         "into one monolithic diff" % (start_sha1, merge_sha1))
            patches.append(patch_fn)
            start = merge_sha1
            print start

    # Generate patches
    for commit in reversed(repo.get_commits(start, end_commit)):
        info = repo.get_commit_info(commit)
        cmds = parse_gbp_commands(info, 'gbp-rpm', ('ignore'),
                                  ('if', 'ifarch'))[0]
        if not 'ignore' in cmds:
            patch_fn = format_patch(outdir, repo, info, patches,
                                    options.patch_numbers,
                                    options.patch_export_ignore_path)
            if patch_fn:
                commands[os.path.basename(patch_fn)] = cmds
        else:
            gbp.log.info('Ignoring commit %s' % info['id'])

    # Generate diff to the tree-ish object
    if end_commit != end:
        gbp.log.info("Generating diff file %s..%s" % (end_commit, end))
        patch_fn = format_diff(outdir, None, repo, end_commit, end,
                               options.patch_export_ignore_path)
        if patch_fn:
            patches.append(patch_fn)

    # Compress
    patches = compress_patches(patches, options.patch_export_compress)

    return patches, commands


def rm_patch_files(spec):
    """
    Delete the patch files listed in the spec files. Doesn't delete patches
    marked as not maintained by gbp.
    """
    # Remove all old patches from the spec dir
    for patch in spec.patchseries(unapplied=True):
        gbp.log.debug("Removing '%s'" % patch.path)
        try:
            os.unlink(patch.path)
        except OSError as err:
            if err.errno != errno.ENOENT:
                raise GbpError("Failed to remove patch: %s" % err)
            else:
                gbp.log.debug("Patch %s does not exist." % patch.path)


def update_patch_series(repo, spec, start, end, options):
    """
    Export patches to packaging directory and update spec file accordingly.
    """
    squash = options.patch_export_squash_until.split(':', 1)
    if len(squash) == 1:
        squash.append(None)
    else:
        squash[1] += '.diff'

    # Unlink old patch files and generate new patches
    rm_patch_files(spec)

    patches, commands = generate_patches(repo, start, squash, end,
                                         spec.specdir, options)
    spec.update_patches(patches, commands)
    spec.write_spec_file()
    return patches


def parse_spec(options, repo, treeish=None):
    """
    Find and parse spec file.

    If treeish is given, try to find the spec file from that. Otherwise, search
    for the spec file in the working copy.
    """
    try:
        if options.spec_file != 'auto':
            options.packaging_dir = os.path.dirname(options.spec_file)
            if not treeish:
                spec = SpecFile(options.spec_file)
            else:
                spec = spec_from_repo(repo, treeish, options.spec_file)
        else:
            preferred_name = os.path.basename(repo.path) + '.spec'
            if not treeish:
                spec = guess_spec(options.packaging_dir, True, preferred_name)
            else:
                spec = guess_spec_repo(repo, treeish, options.packaging_dir,
                                       True, preferred_name)
    except NoSpecError as err:
        raise GbpError("Can't parse spec: %s" % err)
    relpath = spec.specpath if treeish else os.path.relpath(spec.specpath,
                                                            repo.path)
    gbp.log.debug("Using '%s' from '%s'" % (relpath, treeish or 'working copy'))
    return spec


def find_upstream_commit(repo, spec, upstream_tag):
    """Find commit corresponding upstream version"""
    tag_str_fields = {'upstreamversion': spec.upstreamversion,
                      'vendor': 'Upstream'}
    upstream_commit = repo.find_version(upstream_tag, tag_str_fields)
    if not upstream_commit:
        raise GbpError("Couldn't find upstream version %s" %
                       spec.upstreamversion)
    return upstream_commit


def export_patches(repo, options):
    """Export patches from the pq branch into a packaging branch"""
    current = repo.get_branch()
    if is_pq_branch(current, options):
        base = pq_branch_base(current, options)
        gbp.log.info("On branch '%s', switching to '%s'" % (current, base))
        repo.set_branch(base)
        spec = parse_spec(options, repo)
        pq_branch = current
    else:
        spec = parse_spec(options, repo)
        pq_branch = pq_branch_name(current, options, spec.version)
    upstream_commit = find_upstream_commit(repo, spec, options.upstream_tag)

    export_treeish = options.export_rev if options.export_rev else pq_branch
    if not repo.has_treeish(export_treeish):
        raise GbpError('Invalid treeish object %s' % export_treeish)

    update_patch_series(repo, spec, upstream_commit, export_treeish, options)

    GitCommand('status')(['--', spec.specdir])


def safe_patches(queue, tmpdir_base):
    """
    Safe the current patches in a temporary directory
    below 'tmpdir_base'. Also, uncompress compressed patches here.

    @param queue: an existing patch queue
    @param tmpdir_base: base under which to create tmpdir
    @return: tmpdir and a safed queue (with patches in tmpdir)
    @rtype: tuple
    """

    tmpdir = tempfile.mkdtemp(dir=tmpdir_base, prefix='patchimport_')
    safequeue = PatchSeries()

    if len(queue) > 0:
        gbp.log.debug("Safeing patches '%s' in '%s'" %
                        (os.path.dirname(queue[0].path), tmpdir))
    for patch in queue:
        base, _archive_fmt, comp = parse_archive_filename(patch.path)
        uncompressors = {'gzip': gzip.open, 'bzip2': bz2.BZ2File}
        if comp in uncompressors:
            gbp.log.debug("Uncompressing '%s'" % os.path.basename(patch.path))
            src = uncompressors[comp](patch.path, 'r')
            dst_name = os.path.join(tmpdir, os.path.basename(base))
        elif comp:
            raise GbpError("Unsupported patch compression '%s', giving up"
                           % comp)
        else:
            src = open(patch.path, 'r')
            dst_name = os.path.join(tmpdir, os.path.basename(patch.path))

        dst = open(dst_name, 'w')
        dst.writelines(src)
        src.close()
        dst.close()

        safequeue.append(patch)
        safequeue[-1].path = dst_name

    return safequeue


def get_packager(spec):
    """Get packager information from spec"""
    if spec.packager:
        match = re.match(r'(?P<name>.*[^ ])\s*<(?P<email>\S*)>',
                         spec.packager.strip())
        if match:
            return GitModifier(match.group('name'), match.group('email'))
    return GitModifier()

def import_extra_files(repo, commitish, files, patch_ignore=True):
    """Import branch-specific gbp.conf files to current branch"""
    found = {}
    for fname in files:
        if fname:
            try:
                found[fname] = repo.show('%s:%s' % (commitish, fname))
            except GitRepositoryError:
                pass
    if found:
        gbp.log.info("Importing additional file(s) from branch '%s' into '%s'" %
                     (commitish, repo.get_branch()))
        for fname, content in found.iteritems():
            dirname = os.path.dirname(fname)
            if dirname and not os.path.exists(dirname):
                os.makedirs(dirname)
            with open(fname, 'w') as fobj:
                fobj.write(content)

        files = found.keys()
        gbp.log.debug('Adding/commiting %s' % files)
        repo.add_files(files, force=True)
        commit_msg = ("Auto-import file(s) from branch '%s':\n    %s\n" %
                      (commitish, '    '.join(files)))
        if patch_ignore:
            commit_msg += "\nGbp: Ignore\nGbp-Rpm: Ignore"
        repo.commit_files(files, msg=commit_msg)
    return found.keys()

def import_spec_patches(repo, options):
    """
    apply a series of patches in a spec/packaging dir to branch
    the patch-queue branch for 'branch'

    @param repo: git repository to work on
    @param options: command options
    """
    current = repo.get_branch()
    # Get spec and related information
    if is_pq_branch(current, options):
        base = pq_branch_base(current, options)
        if options.force:
            spec = parse_spec(options, repo, base)
            spec_treeish = base
        else:
            raise GbpError("Already on a patch-queue branch '%s' - doing "
                           "nothing." % current)
    else:
        spec = parse_spec(options, repo)
        spec_treeish = None
        base = current
    upstream_commit = find_upstream_commit(repo, spec, options.upstream_tag)
    packager = get_packager(spec)
    pq_branch = pq_branch_name(base, options, spec.version)

    # Create pq-branch
    if repo.has_branch(pq_branch) and not options.force:
        raise GbpError("Patch-queue branch '%s' already exists. "
                       "Try 'switch' instead." % pq_branch)
    try:
        if repo.get_branch() == pq_branch:
            repo.force_head(upstream_commit, hard=True)
        else:
            repo.create_branch(pq_branch, upstream_commit, force=True)
    except GitRepositoryError as err:
        raise GbpError("Cannot create patch-queue branch '%s': %s" %
                        (pq_branch, err))

    # Put patches in a safe place
    if spec_treeish:
        packaging_tmp = tempfile.mkdtemp(prefix='dump_', dir=options.tmp_dir)
        packaging_tree = '%s:%s' % (spec_treeish, options.packaging_dir)
        dump_tree(repo, packaging_tmp, packaging_tree, with_submodules=False,
                  recursive=False)
        spec.specdir = packaging_tmp
    in_queue = spec.patchseries()
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
            apply_and_commit_patch(repo, patch, packager)
    except (GbpError, GitRepositoryError) as err:
        repo.set_branch(base)
        repo.delete_branch(pq_branch)
        raise GbpError('Import failed: %s' % err)

    gbp.log.info("Patches listed in '%s' imported on '%s'" % (spec.specfile,
                                                              pq_branch))


def rebase_pq(repo, options):
    """Rebase pq branch on the correct upstream version (from spec file)."""
    current = repo.get_branch()
    if is_pq_branch(current, options):
        base = pq_branch_base(current, options)
        spec = parse_spec(options, repo, base)
    else:
        base = current
        spec = parse_spec(options, repo)
    upstream_commit = find_upstream_commit(repo, spec, options.upstream_tag)

    switch_to_pq_branch(repo, base, options)
    GitCommand("rebase")([upstream_commit])


def switch_pq(repo, options):
    """Switch to patch-queue branch if on base branch and vice versa"""
    current = repo.get_branch()
    if is_pq_branch(current, options):
        base = pq_branch_base(current, options)
        gbp.log.info("Switching to branch '%s'" % base)
        repo.checkout(base)
    else:
        switch_to_pq_branch(repo, current, options)


def drop_pq_rpm(repo, options):
    """Remove pq branch"""
    current = repo.get_branch()
    if is_pq_branch(current, options):
        base = pq_branch_base(current, options)
        spec = parse_spec(options, repo, base)
    else:
        spec = parse_spec(options, repo)
    drop_pq(repo, current, options, spec.version)


def switch_to_pq_branch(repo, branch, options):
    """
    Switch to patch-queue branch if not already there, create it if it
    doesn't exist yet
    """
    if is_pq_branch(branch, options):
        return

    spec = parse_spec(options, repo, branch)
    pq_branch = pq_branch_name(branch, options, spec.version)
    if not repo.has_branch(pq_branch):
        raise GbpError("Branch '%s' does not exist" % pq_branch)

    gbp.log.info("Switching to branch '%s'" % pq_branch)
    repo.set_branch(pq_branch)

def apply_single_patch(repo, patchfile, options):
    """Apply a single patch onto the pq branch"""
    current = repo.get_branch()
    if not is_pq_branch(current, options):
        switch_to_pq_branch(repo, current, options)
    patch = Patch(patchfile)
    apply_and_commit_patch(repo, patch, fallback_author=None)

def convert_package(repo, options):
    """Convert package to orphan-packaging model"""
    old_packaging = repo.get_branch()
    # Check if we're on pq branch, already
    err_msg_base = "Seems you're already using orphan-packaging model - "
    if is_pq_branch(old_packaging, options):
        raise GbpError(err_msg_base + "you're on patch-queue branch")
    # Check if a pq branch already exists
    spec = parse_spec(options, repo, treeish=old_packaging)
    pq_branch = pq_branch_name(old_packaging, options, spec.version)
    if repo.has_branch(pq_branch):
        pq_branch = pq_branch_name(old_packaging, options, spec.version)
        raise GbpError(err_msg_base + "pq branch %s already exists" % pq_branch)
    # Check that the current branch is based on upstream
    upstream_commit = find_upstream_commit(repo, spec, options.upstream_tag)
    if not is_ancestor(repo, upstream_commit, old_packaging):
        raise GbpError(err_msg_base + "%s is not based on upstream version %s" %
                       (old_packaging, spec.upstreamversion))
    # Check new branch
    new_branch = old_packaging + "-orphan"
    if repo.has_branch(new_branch):
        if not options.force:
            raise GbpError("Branch '%s' already exists!" % new_branch)
        else:
            gbp.log.info("Dropping branch '%s'" % new_branch)
            repo.delete_branch(new_branch)

    # Determine "history"
    if options.retain_history:
        # Find first commit that has the spec file and list commits from there
        try:
            repo.show('%s:%s' % (upstream_commit, spec.specpath))
            history = repo.get_commits(upstream_commit, old_packaging)
        except GitRepositoryError:
            history_start = repo.get_commits(upstream_commit, old_packaging,
                                             spec.specpath)[-1]
            history = repo.get_commits('%s^' % history_start, old_packaging)
    else:
        history = [repo.rev_parse(old_packaging)]
    history.reverse()

    # Do import
    gbp.log.info("Importing packaging files from branch '%s' to '%s'" %
                 (old_packaging, new_branch))
    convert_with_history(repo, upstream_commit, history, new_branch,
                         spec.specfile, options)
    # Copy extra files
    import_extra_files(repo, old_packaging, options.import_files,
                       patch_ignore=False)

    gbp.log.info("Package successfully converted to orphan-packaging.")
    gbp.log.info("You're now on the new '%s' packaging branch (the old "
            "packaging branch '%s' was left intact)." %
            (new_branch, old_packaging))
    gbp.log.info("Please check all files and test building the package!")


def convert_with_history(repo, upstream, commits, new_branch, spec_fn, options):
    """Auto-import packaging files and (auto-generated) patches"""

    # Dump and commit packaging files
    packaging_tree = '%s:%s' % (commits[0], options.packaging_dir)
    packaging_tmp = tempfile.mkdtemp(prefix='pack_', dir=options.tmp_dir)
    dump_packaging_dir = os.path.join(packaging_tmp, options.new_packaging_dir)
    dump_tree(repo, dump_packaging_dir, packaging_tree, with_submodules=False,
              recursive=False)

    msg = "Auto-import packaging files\n\n" \
          "Imported initial packaging files from commit '%s'" % (commits[0])
    new_tree = repo.create_tree(packaging_tmp)
    tip_commit = repo.commit_tree(new_tree, msg, [])

    # Generate initial patches
    spec = SpecFile(os.path.join(dump_packaging_dir, spec_fn))
    update_patch_series(repo, spec, upstream, commits[0], options)
    # Commit updated packaging files only if something was changed
    new_tree = repo.create_tree(packaging_tmp)
    if new_tree != repo.rev_parse(tip_commit + ':'):
        msg = "Auto-generate patches\n\n" \
              "Generated patches from\n'%s..%s'\n\n" \
              "updating spec file and possibly removing old patches." \
              % (upstream, commits[0])
        tip_commit = repo.commit_tree(new_tree, msg, [tip_commit])

    # Import rest of the commits
    for commit in commits[1:]:
        shutil.rmtree(dump_packaging_dir)
        packaging_tree = '%s:%s' % (commit, options.packaging_dir)
        dump_tree(repo, dump_packaging_dir, packaging_tree,
                  with_submodules=False, recursive=False)
        try:
            spec = SpecFile(os.path.join(dump_packaging_dir, spec_fn))
            update_patch_series(repo, spec, upstream, commit, options)
        except (NoSpecError, GbpError):
            gbp.log.warn("Failed to generate patches from '%s'" % commit)

        new_tree = repo.create_tree(packaging_tmp)
        if new_tree == repo.rev_parse(tip_commit + ':'):
            gbp.log.info("Skipping commit '%s' which generated no change" %
                         commit)
        else:
            info = repo.get_commit_info(commit)
            msg = "%s\n\n%sAuto-imported by gbp from '%s'" % (info['subject'],
                        info['body'], commit)
            tip_commit = repo.commit_tree(new_tree, msg, [tip_commit])

    repo.create_branch(new_branch, tip_commit)
    repo.set_branch(new_branch)


def opt_split_cb(option, opt_str, value, parser):
    """Split option string into a list"""
    setattr(parser.values, option.dest, value.split(','))


def main(argv):
    """Main function for the gbp pq-rpm command"""
    retval = 0

    try:
        parser = GbpOptionParserRpm(command=os.path.basename(argv[0]),
                                    prefix='', usage=USAGE_STRING)
    except ConfigParser.ParsingError as err:
        gbp.log.err('Invalid config file: %s' % err)
        return 1

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
    parser.add_config_file_option(option_name="spec-file", dest="spec_file")
    parser.add_config_file_option(option_name="packaging-dir",
            dest="packaging_dir")
    parser.add_option("--new-packaging-dir",
            help="Packaging directory in the new packaging branch. Only "
                 "relevant for the 'convert' action. If not defined, defaults "
                 "to '--packaging-dir'")
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
    parser.add_option("--retain-history", action="store_true",
            help="When doing convert, preserve as much of the git history as "
                 "possible, i.e. create one commit per commit. Only "
                 "relevant for the 'convert' action.")
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

    (options, args) = parser.parse_args(argv)
    gbp.log.setup(options.color, options.verbose, options.color_scheme)
    options.patch_export_compress = string_to_int(options.patch_export_compress)
    if options.new_packaging_dir is None:
        options.new_packaging_dir = options.packaging_dir

    if len(args) < 2:
        gbp.log.err("No action given.")
        return 1
    else:
        action = args[1]

    if args[1] in ["export", "import", "rebase", "drop", "switch", "convert"]:
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
        # Create base temporary directory for this run
        options.tmp_dir = tempfile.mkdtemp(dir=options.tmp_dir,
                                           prefix='gbp-pq-rpm_')
        if action == "export":
            export_patches(repo, options)
        elif action == "import":
            import_spec_patches(repo, options)
        elif action == "drop":
            drop_pq_rpm(repo, options)
        elif action == "rebase":
            rebase_pq(repo, options)
        elif action == "apply":
            apply_single_patch(repo, patchfile, options)
        elif action == "switch":
            switch_pq(repo, options)
        elif action == "convert":
            convert_package(repo, options)
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

