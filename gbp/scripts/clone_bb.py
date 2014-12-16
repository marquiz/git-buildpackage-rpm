# vim: set fileencoding=utf-8 :
#
# (C) 2009,2010 Guido Guenther <agx@sigxcpu.org>
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
# inspired by dom-git-checkout
#
"""Clone a package Git repository from a bitbake-based distro"""

import ConfigParser
import re
import sys
import os, os.path

from gbp.config import (GbpOptionParser, GbpOptionGroup)
from gbp.git import GitRepositoryError
from gbp.errors import GbpError
import gbp.log
from gbp.rpm.git import RpmGitRepository as GitRepository
from gbp.bb import bb, init_tinfoil, guess_pkg

#   pylint: disable=bad-continuation


def guess_remote(tinfoil, source):
    """Guess the remote repository URL"""
    # Try to determine if a remote URL is referenced
    if re.match(r'[a-z]{3,5}://', source) or re.match(r'\S+@\S+', source):
        return source, None

    # Get remote repo from recipe
    recipe = guess_pkg(tinfoil, source)
    appends = tinfoil.cooker.collection.get_file_appends(recipe)
    gbp.log.info("Using %s with appends %s" % (recipe, appends))
    pkg_data = bb.cache.Cache.loadDataFull(recipe, appends, tinfoil.config_data)
    uri = pkg_data.getVar('GBP_PACKAGING_REPO', True)
    if not uri:
        raise GbpError("GBP_PACKAGING_REPO not defined in recipe. Unable to "
                       "determine remote repo")
    rev = pkg_data.getVar('GBP_PACKAGING_REV', True)
    return uri, rev


def build_parser(name):
    """Create command line argument parser"""
    try:
        parser = GbpOptionParser(command=os.path.basename(name), prefix='',
                                 usage='%prog [options] repository - clone a '
                                       'remote per-package repository')
    except ConfigParser.ParsingError as err:
        gbp.log.err(err)
        return None

    branch_group = GbpOptionGroup(parser, "branch options",
                                  "branch tracking and layout options")
    parser.add_option_group(branch_group)

    branch_group.add_option("--all", action="store_true", dest="all",
                help="track all branches, not only packaging and upstream")
    branch_group.add_config_file_option(option_name="upstream-branch",
                dest="upstream_branch")
    branch_group.add_config_file_option(option_name="packaging-branch",
                dest="packaging_branch")
    branch_group.add_option("--depth", action="store", dest="depth", default=0,
                help="git history depth (for creating shallow clones)")

    parser.add_option("-v", "--verbose", action="store_true", dest="verbose",
                help="verbose command execution")
    parser.add_config_file_option(option_name="color", dest="color",
                type='tristate')
    parser.add_config_file_option(option_name="color-scheme",
                dest="color_scheme")
    return parser


def parse_args (argv):
    """Parse command line arguments"""
    parser = build_parser(argv[0])
    if not parser:
        return None, None

    (options, args) = parser.parse_args(argv)
    gbp.log.setup(options.color, options.verbose, options.color_scheme)
    return (options, args)


def main(argv):
    """Entry point for gbp-clone-bb"""
    retval = 0

    if not bb:
        return 1

    (options, args) = parse_args(argv)
    if not options:
        return 1

    if len(args) < 2:
        gbp.log.err("Need a package or repository to clone.")
        return 1

    # Determine target dir
    clone_to = os.path.curdir
    auto_name = False
    if len(args) < 3:
        if 'BUILDDIR' in os.environ:
            clone_to = os.path.join(os.environ['BUILDDIR'], 'devel')
        auto_name = True
    else:
        clone_to = args[2]

    try:
        tinfoil = init_tinfoil()

        source, revision = guess_remote(tinfoil, args[1])

        gbp.log.info("Cloning from %s..." % source)
        repo = GitRepository.clone(clone_to, source, options.depth,
                                   auto_name=auto_name)
        os.chdir(repo.path)

        # Reparse the config files of the cloned repository so we pick up the
        # branch information from there:
        (options, args) = parse_args(argv)

        # Track all branches:
        if options.all:
            remotes = repo.get_remote_branches()
            for remote in remotes:
                local = remote.replace("origin/", "", 1)
                if not repo.has_branch(local) and local != "HEAD":
                    repo.create_branch(local, remote)
        else: # only track gbp's default branches
            branches = [ options.packaging_branch, options.upstream_branch ]
            gbp.log.debug('Will track branches: %s' % branches)
            for branch in branches:
                remote = 'origin/%s' % branch
                if repo.has_branch(remote, remote=True) and \
                        not repo.has_branch(branch):
                    repo.create_branch(branch, remote)

        gbp.log.info("Successfully cloned into %s" % clone_to)
        if (revision and repo.rev_parse('HEAD') !=
                         repo.rev_parse('%s^0' % revision)):
            gbp.log.info("Checking out revision %s" % revision)
            repo.set_branch(revision)

    except GitRepositoryError as err:
        gbp.log.err("Git command failed: %s" % err)
        retval = 1
    except GbpError as err:
        if len(err.__str__()):
            gbp.log.err(err)
        retval = 1

    return retval

if __name__ == '__main__':
    sys.exit(main(sys.argv))

# vim:et:ts=4:sw=4:et:sts=4:ai:set list listchars=tab\:»·,trail\:·:
