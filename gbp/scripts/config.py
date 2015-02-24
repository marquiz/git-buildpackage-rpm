# vim: set fileencoding=utf-8 :
#
# (C) 2014 Guido Guenther <agx@sigxcpu.org>
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
"""Query and display config file values"""

from six.moves import configparser
import sys
import os, os.path
from gbp.config import GbpConfArgParser, GbpConfigDebian
from gbp.scripts.supercommand import import_command
import gbp.log


def build_parser(name):
    usage = 'display configuration settings'
    try:
        parser = GbpConfArgParser.create_parser(prog=name, description=usage)
    except configparser.ParsingError as err:
        gbp.log.err(err)
        return None

    parser.add_arg("-v", "--verbose", action="store_true",
                      help="verbose command execution")
    parser.add_conf_file_arg("--color", type='tristate')
    parser.add_conf_file_arg("--color-scheme")
    parser.add_argument("query", metavar="QUERY",
                        help="command[.optionname] to show")
    return parser


def parse_args(argv):
    parser = build_parser(argv[0])
    if not parser:
        return None
    return parser.parse_args(argv[1:])


def build_cmd_parser(section):
    """
    Populate the parser to get a list of valid options
    """
    try:
        # Populate the parser to get a list of
        # valid options
        module = import_command(section)
        parser = module.build_parser(section)
    except (AttributeError, ImportError):
        # Use the default parser for section that don't
        # map to a command
        parser = GbpConfArgParser.create_parser(prog=section)
    return parser


def print_single_option(parser, option, printer):
    value = parser.get_conf_file_value(option)
    if value is not None:
        printer("%s.%s=%s" % (parser.command, option, value))
    else:
        return 2
    return 0


def print_all_options(parser, printer):
    if not parser.conf_file_args:
        return 2
    for opt in parser.conf_file_args:
        value = parser.get_conf_file_value(opt)
        printer("%s.%s=%s" % (parser.command, opt, value))
    return 0


def print_cmd_values(query, printer):
    """
    Print configuration values of a command

    @param query: the section to print the values for or section.option to
        print
    @param printer: the printer to output the values
    """
    if not query:
        return 2

    try:
        section, option = query.split('.')
    except ValueError:
        section = query
        option = None

    parser = build_cmd_parser(section)

    if option:  # Single option query
        return print_single_option(parser, option, printer)
    else:  # all options
        return print_all_options(parser, printer)


def value_printer(value):
    if (value):
        print(value)


def main(argv):
    retval = 1

    options = parse_args(argv)
    gbp.log.setup(options.color, options.verbose, options.color_scheme)

    return print_cmd_values(options.query, value_printer)

if __name__ == '__main__':
    sys.exit(main(sys.argv))

# vim:et:ts=4:sw=4:et:sts=4:ai:set list listchars=tab\:»·,trail\:·:
