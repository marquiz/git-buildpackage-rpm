#!/usr/bin/python
# vim: set fileencoding=utf-8 :
#
# (C) 2013 Guido Günther <agx@sigxcpu.org>
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
"""Supercommand for all gbp commands"""

from __future__ import print_function

import glob
import os
import pkg_resources
import re
import sys

from gbp.errors import GbpError

def usage():
    print("""
Usage:
    gbp <command> [<args>]

The most commonly used commands are:

    buildpackage - build a Debian package
    import-orig  - import a new upstream tarball
    import-dsc   - import a single Debian source package
    import-dscs  - import multiple Debian source packages

Use '--list-cmds' to list all available commands.
""")

def version(prog):
    try:
        from gbp.version import gbp_version
    except ImportError:
        gbp_version = '[Unknown version]'
    print("%s %s" % (os.path.basename(prog), gbp_version))


def import_command(cmd):
    """
    Import the module that implements the given command
    """
    for entrypoint in pkg_resources.iter_entry_points(group='gbp_commands'):
        if entrypoint.name == cmd:
            return entrypoint.load()
    raise GbpError("'%s' is not a valid command." % cmd)


def get_available_commands():
    return [entrypoint.name for entrypoint in
            pkg_resources.iter_entry_points(group='gbp_commands')]


def list_available_commands():
    maxlen = 0

    print("Available commands:\n")
    cmds = sorted(get_available_commands())
    for cmd in cmds:
        if len(cmd) > maxlen:
            maxlen = len(cmd)
    for cmd in cmds:
        mod = import_command(cmd)
        doc = mod.__doc__
        print("    %s - %s" % (cmd.rjust(maxlen), doc))
    print('')


def supercommand(argv=None):
    argv = argv or sys.argv

    if len(argv) < 2:
        usage()
        return 1

    prg, cmd = argv[0:2]
    args = argv[1:]

    if cmd in ['--help', '-h']:
        usage()
        return 0
    elif cmd == 'help' and len(args) > 1:
        # Make the first argument after help the new commadn and
        # request it's help output
        cmd = args[1]
        args = [cmd, '--help']
    elif cmd == 'help':
        usage()
        return 0
    elif cmd in [ '--version', 'version' ]:
        version(argv[0])
        return 0
    elif cmd in [ '--list-cmds', 'list-cmds' ]:
        list_available_commands()
        return 0

    try:
        module = import_command(cmd)
    except GbpError as err:
        print(err, file=sys.stderr)
        usage()
        return 2

    return module.main(args)

if __name__ == '__main__':
    sys.exit(supercommand())

# vim:et:ts=4:sw=4:et:sts=4:ai:set list listchars=tab\:»·,trail\:·:
