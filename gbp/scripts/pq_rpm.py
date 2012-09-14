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

import errno
import os
import shutil
import sys
import re
import gzip
import subprocess
import gbp.tmpfile as tempfile
from gbp.config import (GbpOptionParserRpm, GbpOptionGroup)
from gbp.rpm.git import (GitRepositoryError, RpmGitRepository)
from gbp.git.modifier import GitModifier, GitTz
from gbp.command_wrappers import (Command, GitCommand, RunAtCommand,
                                  CommandExecFailed)
from gbp.errors import GbpError
import gbp.log
from gbp.patch_series import (PatchSeries, Patch)
from gbp.pkg import parse_archive_filename
from gbp.rpm import (SpecFile, guess_spec, string_to_int)
from gbp.scripts.common.pq import (is_pq_branch, pq_branch_name, pq_branch_base,
                                   parse_gbp_commands, format_patch,
                                   format_diff,
                                   switch_to_pq_branch, apply_single_patch,
                                   apply_and_commit_patch, drop_pq)


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


def generate_patches(repo, start, squash, end, outdir, options):
    """
    Generate patch files from git
    """
    gbp.log.info("Generating patches from git (%s..%s)" % (start, end))
    patches = []
    commands = {}
    for treeish in [start, end]:
        if not repo.has_treeish(treeish):
            raise GbpError('%s not a valid tree-ish' % treeish)

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
    if repo.get_merge_base(start_sha1, end_commit_sha1) != start_sha1:
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

    # Generate patches
    for commit in reversed(repo.get_commits(start, end_commit)):
        info = repo.get_commit_info(commit)
        cmds = parse_gbp_commands(info, 'gbp-rpm', ('ignore'), None)
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
    for n, p in spec.patches.iteritems():
        if p['autoupdate']:
            f = os.path.join(spec.specdir, p['filename'])
            gbp.log.debug("Removing '%s'" % f)
            try:
                os.unlink(f)
            except OSError, (e, msg):
                if e != errno.ENOENT:
                    raise GbpError, "Failed to remove patch: %s" % msg
                else:
                    gbp.log.debug("%s does not exist." % f)


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

    patches, _commands = generate_patches(repo, start, squash, end,
                                          spec.specdir, options)
    spec.update_patches(patches)
    spec.write_spec_file()


def export_patches(repo, branch, options):
    """Export patches from the pq branch into a packaging branch"""
    if is_pq_branch(branch, options):
        base = pq_branch_base(branch, options)
        gbp.log.info("On '%s', switching to '%s'" % (branch, base))
        branch = base
        repo.set_branch(branch)

    pq_branch = pq_branch_name(branch, options)

    # Find and parse .spec file
    try:
        if options.spec_file != 'auto':
            specfilename = options.spec_file
            options.packaging_dir = os.path.dirname(specfilename)
        else:
            specfilename = guess_spec(options.packaging_dir,
                                      True,
                                      os.path.basename(repo.path) + '.spec')
        spec = SpecFile(specfilename)
    except KeyError:
        raise GbpError, "Can't parse spec"

    # Find upstream version
    tag_str_fields = dict(upstreamversion=spec.upstreamversion, vendor="Upstream")
    upstream_commit = repo.find_version(options.upstream_tag, tag_str_fields)
    if not upstream_commit:
        raise GbpError, ("Couldn't find upstream version %s. Don't know on what base to import." % spec.upstreamversion)

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
    safequeue=PatchSeries()

    if len(queue) > 0:
        gbp.log.debug("Safeing patches '%s' in '%s'" % (os.path.dirname(queue[0].path), tmpdir))
        for p in queue:
            (base, archive_fmt, comp) = parse_archive_filename(p.path)
            if comp == 'gzip':
                gbp.log.debug("Uncompressing '%s'" % os.path.basename(p.path))
                src = gzip.open(p.path, 'r')
                dst_name = os.path.join(tmpdir, os.path.basename(base))
            elif comp:
                raise GbpError, ("Unsupported compression of a patch, giving up")
            else:
                src = open(p.path, 'r')
                dst_name = os.path.join(tmpdir, os.path.basename(p.path))

            dst = open(dst_name, 'w')
            dst.writelines(src)
            src.close()
            dst.close()

            safequeue.append(p)
            safequeue[-1].path = dst_name;

    return (tmpdir, safequeue)


def get_packager(spec):
    """Get packager information from spec"""
    if spec.packager:
        match = re.match('(?P<name>.*[^ ])\s*<(?P<email>\S*)>',
                         spec.packager.strip())
        if match:
            return GitModifier(match.group('name'), match.group('email'))
    return GitModifier()


def import_spec_patches(repo, branch, options):
    """
    apply a series of patches in a spec/packaging dir to branch
    the patch-queue branch for 'branch'

    @param repo: git repository to work on
    @param branch: branch to base pqtch queue on
    @param options: command options
    """
    tmpdir = None

    if is_pq_branch(branch, options):
        if options.force:
            branch = pq_branch_base(branch, options)
            pq_branch = pq_branch_name(branch, options)
            repo.checkout(branch)
        else:
            gbp.log.err("Already on a patch-queue branch '%s' - doing nothing." % branch)
            raise GbpError
    else:
        pq_branch = pq_branch_name(branch, options)

    if repo.has_branch(pq_branch):
        if options.force:
            drop_pq(repo, branch, options)
        else:
            raise GbpError, ("Patch queue branch '%s'. already exists. Try 'rebase' instead."
                             % pq_branch)

    # Find and parse .spec file
    try:
        if options.spec_file != 'auto':
            specfilename = options.spec_file
            options.packaging_dir = os.path.dirname(specfilename)
        else:
            specfilename = guess_spec(options.packaging_dir,
                                      True,
                                      os.path.basename(repo.path) + '.spec')
        spec = SpecFile(specfilename)
    except KeyError:
        raise GbpError, "Can't parse spec"

    # Find upstream version
    tag_str_fields = dict(upstreamversion=spec.upstreamversion, vendor="Upstream")
    commit = repo.find_version(options.upstream_tag, tag_str_fields)
    if commit:
        commits=[commit]
    else:
        raise GbpError, ("Couldn't find upstream version %s. Don't know on what base to import." % spec.upstreamversion)

    queue = spec.patchseries()
    packager = get_packager(spec)
    # Put patches in a safe place
    tmpdir, queue = safe_patches(queue, options.tmp_dir)
    for commit in commits:
        try:
            gbp.log.info("Trying to apply patches at '%s'" % commit)
            repo.create_branch(pq_branch, commit)
        except GitRepositoryError:
            raise GbpError, ("Cannot create patch-queue branch '%s'." % pq_branch)

        repo.set_branch(pq_branch)
        for patch in queue:
            gbp.log.debug("Applying %s" % patch.path)
            try:
                apply_and_commit_patch(repo, patch, packager)
            except (GbpError, GitRepositoryError):
                repo.set_branch(branch)
                repo.delete_branch(pq_branch)
                break
        else:
            # All patches applied successfully
            break
    else:
        raise GbpError, "Couldn't apply patches"

    repo.set_branch(branch)

    return os.path.basename(spec.specfile)


def rebase_pq(repo, branch, options):
    if is_pq_branch(branch, options):
        base = pq_branch_base(branch, options)
        gbp.log.info("On '%s', switching to '%s'" % (branch, base))
        branch = base
        repo.set_branch(branch)

    # Find and parse .spec file
    try:
        if options.spec_file != 'auto':
            specfilename = options.spec_file
            options.packaging_dir = os.path.dirname(specfilename)
        else:
            specfilename = guess_spec(options.packaging_dir,
                                      True,
                                      os.path.basename(repo.path) + '.spec')
        spec = SpecFile(specfilename)
    except KeyError:
        raise GbpError, "Can't parse spec"

    # Find upstream version
    tag_str_fields = dict(upstreamversion=spec.upstreamversion, vendor="Upstream")
    upstream_commit = repo.find_version(options.upstream_tag, tag_str_fields)
    if not upstream_commit:
        raise GbpError, ("Couldn't find upstream version %s. Don't know on what base to import." % spec.upstreamversion)

    switch_to_pq_branch(repo, branch, options)
    GitCommand("rebase")([upstream_commit])


def switch_pq(repo, current, options):
    """Switch to patch-queue branch if on base branch and vice versa"""
    if is_pq_branch(current, options):
        base = pq_branch_base(current, options)
        gbp.log.info("Switching to %s" % base)
        repo.checkout(base)
    else:
        switch_to_pq_branch(repo, current, options)


def main(argv):
    retval = 0

    parser = GbpOptionParserRpm(command=os.path.basename(argv[0]), prefix='',
                                usage="%prog [options] action - maintain patches on a patch queue branch\n"
        "Actions:\n"
        "  export         Export the patch queue / devel branch associated to the\n"
        "                 current branch into a patch series in and update the spec file\n"
        "  import         Create a patch queue / devel branch from spec file\n"
        "                 and patches in current dir.\n"
        "  rebase         Switch to patch queue / devel branch associated to the current\n"
        "                 branch and rebase against upstream.\n"
        "  drop           Drop (delete) the patch queue /devel branch associated to\n"
        "                 the current branch.\n"
        "  apply          Apply a patch\n"
        "  switch         Switch to patch-queue branch and vice versa")
    parser.add_boolean_config_file_option(option_name="patch-numbers", dest="patch_numbers")
    parser.add_option("-v", "--verbose", action="store_true", dest="verbose", default=False,
                      help="Verbose command execution")
    parser.add_option("--force", dest="force", action="store_true", default=False,
                      help="In case of import even import if the branch already exists")
    parser.add_config_file_option(option_name="vendor", action="store", dest="vendor")
    parser.add_config_file_option(option_name="color", dest="color", type='tristate')
    parser.add_config_file_option(option_name="tmp-dir", dest="tmp_dir")
    parser.add_config_file_option(option_name="upstream-tag", dest="upstream_tag")
    parser.add_config_file_option(option_name="spec-file", dest="spec_file")
    parser.add_config_file_option(option_name="packaging-dir", dest="packaging_dir")
    parser.add_config_file_option(option_name="packaging-branch",
                                  dest="packaging_branch",
                                  help="Branch the packaging is being maintained on. Only relevant if a invariable/single pq-branch is defined, in which case this is used as the 'base' branch. Default is '%(packaging-branch)s'")
    parser.add_config_file_option(option_name="pq-branch", dest="pq_branch")
    parser.add_option("--export-rev", action="store", dest="export_rev", default="",
                      help="Export patches from treeish object TREEISH instead "
                           "of head of patch-queue branch", metavar="TREEISH")
    parser.add_config_file_option("patch-export-compress",
                                  dest="patch_export_compress")
    parser.add_config_file_option("patch-export-squash-until", dest="patch_export_squash_until")
    parser.add_config_file_option("patch-export-ignore-path", dest="patch_export_ignore_path")

    (options, args) = parser.parse_args(argv)
    gbp.log.setup(options.color, options.verbose)
    options.patch_export_compress = string_to_int(options.patch_export_compress)

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
        # Create base temporary directory for this run
        options.tmp_dir = tempfile.mkdtemp(dir=options.tmp_dir,
                                           prefix='gbp-pq-rpm_')
        current = repo.get_branch()
        if action == "export":
            export_patches(repo, current, options)
        elif action == "import":
            specfile=import_spec_patches(repo, current, options)
            current = repo.get_branch()
            gbp.log.info("Patches listed in '%s' imported on '%s'" %
                          (specfile, current))
        elif action == "drop":
            drop_pq(repo, current, options)
        elif action == "rebase":
            rebase_pq(repo, current, options)
        elif action == "apply":
            patch = Patch(patchfile)
            apply_single_patch(repo, current, patch, None, options)
        elif action == "switch":
            switch_pq(repo, current, options)
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

