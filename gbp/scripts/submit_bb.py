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
"""Create and push submit tag"""

import ConfigParser
import os
import sys
from datetime import datetime

import gbp.log
from gbp.config import GbpOptionParserBB
from gbp.errors import GbpError
from gbp.format import format_msg
from gbp.git import GitRepository, GitRepositoryError

#   pylint: disable=bad-continuation


def guess_remote(repo, options):
    """Guess remote where to push"""
    if options.remote:
        return options.remote

    remotes = repo.get_remotes()
    if not remotes:
        raise GbpError("Local repo has no remotes configured. Please add one "
                       "or use --remote to define the remote where to push.")
    elif len(remotes) == 1:
        return remotes.keys()[0]
    else:
        raise GbpError("Local repo has multiple remotes (%s). Don't know which "
                       "one to choose. Use --remote to define where to push." %
                       ', '.join(remotes.keys()))


def build_parser(name):
    """Build command line parser"""
    usage_str = "%prog [options] - create and push submit tag"
    try:
        parser = GbpOptionParserBB(command=os.path.basename(name), prefix='',
                                   usage=usage_str)
    except ConfigParser.ParsingError as err:
        gbp.log.err(err)
        return None

    parser.add_option("-v", "--verbose", action="store_true", dest="verbose",
                        help="verbose command execution")
    parser.add_config_file_option(option_name="color", dest="color",
                        type='tristate')
    parser.add_config_file_option(option_name="color-scheme",
                        dest="color_scheme")
    parser.add_option("-m", "--message", dest="message", help="tag message")
    parser.add_option("-c", "--commit", dest="commit", help="commit to submit",
                      default='HEAD')
    parser.add_option("-r", "--remote", dest="remote",
                        help="remote where to push")
    parser.add_config_file_option(option_name="submit-tag", dest="submit_tag")
    parser.add_config_file_option(option_name="target", dest="target")
    parser.add_boolean_config_file_option(option_name="sign-tags",
                        dest="sign_tags")
    parser.add_config_file_option(option_name="keyid", dest="keyid")

    return parser


def parse_args(argv):
    """Parse command line arguments"""
    parser = build_parser(argv[0])
    if not parser:
        return None, None
    options, args = parser.parse_args(argv)

    gbp.log.setup(options.color, options.verbose, options.color_scheme)

    return (options, args)


def main(argv):
    """Entry point for gbp-submit-bb"""
    retval = 0

    options, _args = parse_args(argv)
    if not options:
        return 1

    try:
        repo = GitRepository(os.path.curdir)
    except GitRepositoryError:
        gbp.log.err("The command must be run under a Git repository")
        return 1

    try:
        remote = guess_remote(repo, options)

        tag_fields = {'nowtime': datetime.now().strftime('%Y%m%d.%H%M%S'),
                      'target': options.target}
        tag_name = format_msg(options.submit_tag, tag_fields)

        gbp.log.info("Tagging %s" % tag_name)
        repo.create_tag(tag_name, msg=options.message, commit=options.commit,
                        sign=options.sign_tags, keyid=options.keyid,
                        annotate=True)

        gbp.log.info("Pushing to remote %s" % remote)
        try:
            repo.push_tag(remote, tag_name)
        except GitRepositoryError as err:
            gbp.log.err(err)
            gbp.log.info("Removing tag %s" % tag_name)
            repo.delete_tag(tag_name)
            raise GbpError("Git push failed!")

    except (GbpError, GitRepositoryError) as err:
        if len(err.__str__()):
            gbp.log.err(err)
        retval = 1

    return retval


if __name__ == '__main__':
    sys.exit(main(sys.argv))

