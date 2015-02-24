# vim: set fileencoding=utf-8 :
#
# (C) 2009, 2010, 2015 Guido Guenther <agx@sigxcpu.org>
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
#    along with this program; if not, please see
#    <http://www.gnu.org/licenses/>
#
# inspired by dom-git-checkout
#
"""Clone a Git repository and set it up for gbp"""

from six.moves import configparser
import sys
import os, os.path
from gbp.config import GbpConfArgParser
from gbp.deb.git import DebianGitRepository
from gbp.git import (GitRepository, GitRepositoryError)
from gbp.errors import GbpError
import gbp.log


def build_parser(name):
    try:
        parser = GbpConfArgParser.create_parser(prog=name,
                                description='clone a remote repository')
    except configparser.ParsingError as err:
        gbp.log.err(err)
        return None

    branch_group = parser.add_argument_group("branch options", "branch tracking and layout options")

    branch_group.add_arg("--all", action="store_true",
                    help="track all branches, not only debian and upstream")
    branch_group.add_conf_file_arg("--upstream-branch")
    branch_group.add_conf_file_arg("--debian-branch")
    branch_group.add_bool_conf_file_arg("--pristine-tar")
    branch_group.add_arg("--depth", action="store", default=0,
                    help="git history depth (for creating shallow clones)")
    branch_group.add_arg("--reference", action="store", dest="reference", default=None,
                    help="git reference repository (use local copies where possible)")

    parser.add_arg("-v", "--verbose", action="store_true",
                    help="verbose command execution")
    parser.add_conf_file_arg("--color", type='tristate')
    parser.add_conf_file_arg("--color-scheme")
    parser.add_argument("repository", metavar="REPOSITORY",
                    help="repository to clone")
    parser.add_argument("directory", metavar="DIRECTORY", nargs="?",
                    help="local directory to clone into")
    return parser


def parse_args (argv):
    parser = build_parser(os.path.basename(argv[0]))
    if not parser:
        return None

    options = parser.parse_args(argv[1:])
    gbp.log.setup(options.color, options.verbose, options.color_scheme)
    return options


def main(argv):
    retval = 0

    options = parse_args(argv)
    if not options:
        return 1

    clone_to, auto_name = (os.path.curdir, True) if not options.directory \
                     else (options.directory, False)
    try:
        GitRepository(clone_to)
        gbp.log.err("Can't run inside a git repository.")
        return 1
    except GitRepositoryError:
        pass

    try:
        repo = DebianGitRepository.clone(clone_to, options.repository,
                                         options.depth, auto_name=auto_name,
                                         reference=options.reference)
        os.chdir(repo.path)

        # Reparse the config files of the cloned repository so we pick up the
        # branch information from there:
        options = parse_args(argv)

        # Track all branches:
        if options.all:
            remotes = repo.get_remote_branches()
            for remote in remotes:
                local = remote.replace("origin/", "", 1)
                if not repo.has_branch(local) and \
                    local != "HEAD":
                        repo.create_branch(local, remote)
        else: # only track gbp's default branches
            branches = [ options.debian_branch, options.upstream_branch ]
            if options.pristine_tar:
                branches += [ repo.pristine_tar_branch ]
            gbp.log.debug('Will track branches: %s' % branches)
            for branch in branches:
                remote = 'origin/%s' % branch
                if repo.has_branch(remote, remote=True) and \
                    not repo.has_branch(branch):
                        repo.create_branch(branch, remote)

        repo.set_branch(options.debian_branch)

    except GitRepositoryError as err:
        gbp.log.err("Git command failed: %s" % err)
        retval = 1
    except GbpError as err:
        if str(err):
            gbp.log.err(err)
        retval = 1

    return retval

if __name__ == '__main__':
    sys.exit(main(sys.argv))

# vim:et:ts=4:sw=4:et:sts=4:ai:set list listchars=tab\:»·,trail\:·:
