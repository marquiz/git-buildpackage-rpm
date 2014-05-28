# vim: set fileencoding=utf-8 :
#
# (C) 2006,2007,2010-2012,2015 Guido Guenther <agx@sigxcpu.org>
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
"""handles command line and config file option parsing for the gbp commands"""

from argparse import (ArgumentParser, ArgumentDefaultsHelpFormatter,
                      ArgumentTypeError)
from six.moves import configparser
from copy import copy
import errno
import os.path
import sys


try:
    from gbp.version import gbp_version
except ImportError:
    gbp_version = "[Unknown version]"
import gbp.tristate
import gbp.log
from gbp.git import GitRepositoryError, GitRepository

no_upstream_branch_msg = """
Repository does not have branch '%s' for upstream sources. If there is none see
file:///usr/share/doc/git-buildpackage/manual-html/gbp.import.html#GBP.IMPORT.CONVERT
on howto create it otherwise use --upstream-branch to specify it.
"""

class GbpConfig(object):
    """Handles gbp config files"""
    default_config_files = {'/etc/git-buildpackage/gbp.conf': 'system',
                            '~/.gbp.conf':                    'global',
                            '%(top_dir)s/.gbp.conf':          None,
                            '%(top_dir)s/debian/gbp.conf':    'debian',
                            '%(git_dir)s/gbp.conf':           None}

    defaults = {
            'debian-branch': 'master',
            'upstream-branch': 'upstream',
            'upstream-tree': 'TAG',
            'pristine-tar': 'False',
            'pristine-tar-commit': 'False',
            'filter-pristine-tar': 'False',
            'sign-tags': 'False',
            'force-create': 'False',
            'no-create-orig': 'False',
            'cleaner': '/bin/true',
            'keyid': '',
            'posttag': '',
            'postbuild': '',
            'prebuild': '',
            'postexport': '',
            'postimport': '',
            'hooks': 'True',
            'debian-tag': 'debian/%(version)s',
            'debian-tag-msg': '%(pkg)s Debian release %(version)s',
            'upstream-tag': 'upstream/%(version)s',
            'import-msg': 'Imported Upstream version %(version)s',
            'commit-msg': 'Update changelog for %(version)s release',
            'filter': [],
            'snapshot-number': 'snapshot + 1',
            'git-log': '--no-merges',
            'export': 'HEAD',
            'export-dir': '',
            'overlay': 'False',
            'tarball-dir': '',
            'ignore-new': 'False',
            'ignore-branch': 'False',
            'meta': 'True',
            'meta-closes': 'Closes|LP',
            'meta-closes-bugnum': r'(?:bug|issue)?\#?\s?\d+',
            'full': 'False',
            'id-length': '0',
            'git-author': 'False',
            'ignore-regex': '',
            'compression': 'auto',
            'compression-level': '9',
            'remote-url-pattern': 'ssh://git.debian.org/git/collab-maint/%(pkg)s.git',
            'multimaint': 'True',
            'multimaint-merge': 'False',
            'pbuilder': 'False',
            'qemubuilder': 'False',
            'dist': 'sid',
            'arch': '',
            'interactive': 'True',
            'color': 'auto',
            'color-scheme': '',
            'customizations': '',
            'spawn-editor': 'release',
            'patch-numbers': 'True',
            'patch-num-format': '%04d-',
            'renumber': 'False',
            'notify': 'auto',
            'merge': 'True',
            'merge-mode'      : 'merge',
            'track': 'True',
            'author-is-committer': 'False',
            'author-date-is-committer-date': 'False',
            'create-missing-branches': 'False',
            'submodules': 'False',
            'time-machine': 1,
            'pbuilder-autoconf': 'True',
            'pbuilder-options': '',
            'template-dir': '',
            'remote-config': '',
            'allow-unauthenticated': 'False',
            'symlink-orig': 'True',
            'purge': 'True',
            'drop': 'False',
            'commit': 'False',
            'upstream-vcs-tag': '',
         }

    default_helps = {
         'debian-branch':
              "Branch the Debian package is being developed on",
         'upstream-branch':
              "Upstream branch",
         'upstream-tree':
              "Where to generate the upstream tarball from (tag or branch)",
         'debian-tag':
              "Format string for debian tags",
         'debian-tag-msg':
              "Format string for signed debian-tag messages",
         'upstream-tag':
              "Format string for upstream tags",
         'sign-tags':
              "Whether to sign tags",
         'keyid':
              "GPG keyid to sign tags with",
         'import-msg':
              "Format string for commit message used to commit the upstream "
              "tarball",
         'commit-msg':
              "Format string for commit messag used to commit the changelog",
         'pristine-tar':
              "Use pristine-tar to create orig tarball",
         'pristine-tar-commit':
              "When generating a tarball commit it to the pristine-tar branch",
         'filter-pristine-tar':
              "Filter pristine-tar when filter option is used",
         'filter':
              "Files to filter out during import (can be given multiple times)",
         'git-author':
              "Use name and email from git-config for changelog trailer",
         'full':
              "Include the full commit message instead of only the first line",
         'meta':
              "Parse meta tags in commit messages",
         'meta-closes':
              "Meta tags for the bts close commands",
        'meta-closes-bugnum':
              "Meta bug number format",
         'ignore-new':
              "Build with uncommited changes in the source tree",
         'ignore-branch':
              "Build although debian-branch != current branch",
         'overlay':
              "extract orig tarball when using export-dir option",
         'remote-url-pattern':
              "Remote url pattern to create the repo at",
         'multimaint':
              "Note multiple maintainers",
         'multimaint-merge':
              "Merge commits by maintainer",
         'pbuilder':
              "Invoke git-pbuilder for building",
         'dist':
              "Build for this distribution when using git-pbuilder",
         'arch':
              "Build for this architecture when using git-pbuilder",
         'qemubuilder':
              "Invoke git-pbuilder with qemubuilder for building",
         'interactive':
              "Run command interactively",
         'color':
              "Whether to use colored output",
         'color-scheme':
              "Colors to use in output (when color is enabled), format is"
              "'<debug>:<info>:<warning>:<error>', e.g. " "'cyan:34::'. "
              "Numerical values and color names are accepted, empty fields "
              "indicate using the default.",
         'spawn-editor':
              "Whether to spawn an editor after adding the changelog entry",
         'patch-numbers':
              "Whether to number patch files",
         'patch-num-format':
              "The format specifier for patch number prefixes",
         'renumber':
              "Whether to renumber patches exported from patch queues, "
              "instead of preserving the number specified in 'Gbp-Pq: Name' "
              "tags",
         'notify':
              "Whether to send a desktop notification after the build",
         'merge':
              "After the import merge the result to the debian branch",
         'merge-mode':
              "Howto merge the new upstream sources onto the debian branch",
         'track':
              "Set up tracking for remote branches",
         'author-is-committer':
              "Use the authors's name also as the committer's name",
         'author-date-is-committer-date':
              "Use the authors's date as the committer's date",
         'create-missing-branches':
              "Create missing branches automatically",
         'submodules':
              "Transparently handle submodules in the upstream tree",
         'postimport':
              "hook run after a successful import",
         'hooks':
              "Enable running all hooks",
         'time-machine':
              "don't try head commit only to apply the patch queue "
              "but look TIME_MACHINE commits back",
         'pbuilder-autoconf':
              "Wheter to configure pbuilder automatically",
         'pbuilder-options':
              "Options to pass to pbuilder",
         'template-dir':
              "Template directory used by git init",
         'remote-config':
              "Remote defintion in gbp.conf used to create the remote "
              "repository",
         'allow-unauthenticated':
              "Don't verify integrity of downloaded source",
         'symlink-orig':
              "Whether to creat a symlink from the upstream tarball "
              "to the orig.tar.gz if needed",
          'purge':
              "Purge exported package build directory",
          'drop':
              "In case of 'export' drop the patch-queue branch after export.",
          'commit':
              "commit changes after export"
        }

    @classmethod
    def get_config_files(cls, no_local=False):
        """
        Get list of config files from the I{GBP_CONF_FILES} environment
        variable.

        @param no_local: don't return the per-repo configuration files
        @type no_local: C{bool}
        @return: list of config files we need to parse
        @rtype: C{list}

        >>> conf_backup = os.getenv('GBP_CONF_FILES')
        >>> if conf_backup is not None: del os.environ['GBP_CONF_FILES']
        >>> homedir = os.path.expanduser("~")
        >>> files = GbpConfig('prog').get_config_files()
        >>> files_mangled = [file.replace(homedir, 'HOME') for file in files]
        >>> sorted(files_mangled)
        ['%(git_dir)s/gbp.conf', '%(top_dir)s/.gbp.conf', '%(top_dir)s/debian/gbp.conf', '/etc/git-buildpackage/gbp.conf', 'HOME/.gbp.conf']
        >>> files = GbpConfig('prog').get_config_files(no_local=True)
        >>> files_mangled = [file.replace(homedir, 'HOME') for file in files]
        >>> sorted(files_mangled)
        ['/etc/git-buildpackage/gbp.conf', 'HOME/.gbp.conf']
        >>> os.environ['GBP_CONF_FILES'] = 'test1:test2'
        >>> GbpConfig('prog').get_config_files()
        ['test1', 'test2']
        >>> del os.environ['GBP_CONF_FILES']
        >>> if conf_backup is not None: os.environ['GBP_CONF_FILES'] = conf_backup
        """
        envvar = os.environ.get('GBP_CONF_FILES')
        files = envvar.split(':') if envvar else cls.default_config_files.keys()
        files = [os.path.expanduser(fname) for fname in files]
        if no_local:
            files = [fname for fname in files if fname.startswith('/')]
        return files

    def _read_config_file(self, parser, repo, filename):
        """Read config file"""
        str_fields = {}
        if repo:
            str_fields['git_dir'] = repo.git_dir
            if not repo.bare:
                str_fields['top_dir'] = repo.path
        try:
            filename = filename % str_fields
        except KeyError:
            # Skip if filename wasn't expanded, i.e. we're not in git repo
            return
        parser.read(filename)

    def __init__(self, command, extra_sections=None, config_files=None):
        self.command = os.path.basename(command)
        self.config = {}

        if not config_files:
            config_files = self.get_config_files()
        self._parse_config_files(config_files, extra_sections)

    def _warn_old_config_section(self, oldcmd, cmd):
        if not os.getenv("GBP_DISABLE_SECTION_DEPRECTATION"):
            gbp.log.warn("Old style config section [%s] found "
                         "please rename to [%s]" % (oldcmd, cmd))

    def _parse_config_files(self, config_files, extra_sections=None):
        """Parse the possible config files and take values from appropriate
        sections."""
        parser = configparser.SafeConfigParser()
        # Fill in the built in values
        self.config = dict(self.defaults)
        # Update with the values from the defaults section. This is needed
        # in case the config file doesn't have a [<command>] section at all
        try:
            repo = GitRepository(".")
        except GitRepositoryError:
            repo = None
        # Read all config files
        for filename in config_files:
            self._read_config_file(parser, repo, filename)
        self.config.update(dict(parser.defaults()))

        # Make sure we read any legacy sections prior to the real subcommands
        # section i.e. read [gbp-pull] prior to [pull]
        if (self.command.startswith('gbp-') or
            self.command.startswith('git-')):
            cmd = self.command[4:]
            oldcmd = self.command
            if parser.has_section(oldcmd):
                self.config.update(dict(parser.items(oldcmd, raw=True)))
                self._warn_old_config_section(oldcmd, cmd)
        else:
            cmd = self.command
            for prefix in ['gbp', 'git']:
                oldcmd = '%s-%s' % (prefix, self.command)
                if parser.has_section(oldcmd):
                    self.config.update(dict(parser.items(oldcmd, raw=True)))
                    self._warn_old_config_section(oldcmd, cmd)

        # Update with command specific settings
        if parser.has_section(cmd):
            # Don't use items() until we got rid of the compat sections
            # since this pulls in the defaults again
            self.config.update(dict(parser._sections[cmd].items()))

        if extra_sections:
            for section in extra_sections:
                if parser.has_section(section):
                    self.config.update(dict(parser._sections[section].items()))
                else:
                    raise configparser.NoSectionError(
                            "Mandatory section [%s] does not exist." % section)

        # filter can be either a list or a string, always build a list:
        if self.config['filter']:
            if self.config['filter'].startswith('['):
                self.config['filter'] = eval(self.config['filter'])
            else:
                self.config['filter'] = [self.config['filter']]
        else:
            self.config['filter'] = []

    def get_value(self, name):
        """Get a value from configuration"""
        return self.config[name]

    def get_bool_value(self, name):
        """Get a boolean value from configuration"""
        value_str = self.config[name]
        if value_str.lower() in ["true",  "1"]:
            value = True
        elif value_str.lower() in ["false", "0"]:
            value = False
        else:
            raise ValueError("Boolean options must be True or False")
        return value

    def get_dual_bool_value(self, name):
        """
        Get configuration file value for dual-boolean arguments.
        Handles no-foo=True and foo=False correctly.
        """
        try:
            value = self.get_bool_value(name)
        except KeyError:
            value = self.get_bool_value("no-%s" % name)
        return value

    def print_help(self, file=None):
        """
        Print an extended help message, listing all options and any
        help text provided with them, to 'file' (default stdout).
        """
        if file is None:
            file = sys.stdout
        encoding = self._get_encoding(file)
        try:
            file.write(self.format_help().encode(encoding, "replace"))
        except IOError as e:
            if e.errno != errno.EPIPE:
                raise

    @classmethod
    def _name_to_filename(cls, name):
        """
        Translate a name like 'system' to a config file name

        >>> GbpConfig._name_to_filename('foo')
        >>> GbpConfig._name_to_filename('system')
        '/etc/git-buildpackage/gbp.conf'
        >>> GbpConfig._name_to_filename('global')
        '~/.gbp.conf'
        >>> GbpConfig._name_to_filename('debian')
        '%(top_dir)s/debian/gbp.conf'
        """
        for k, v in cls.default_config_files.items():
            if name == v:
                return k
        else:
            return None

    @classmethod
    def _set_config_file_value(cls, section, option, value, name=None, filename=None):
        """
        Write a config value to a file creating it if needed

        On errors a ConfigParserError is raised
        """
        if not name and not filename:
            raise configparser.Error("Either 'name' or 'filename' must be given")
        if not filename:
            filename = os.path.expanduser(cls._name_to_filename(name))

        # Create e new config parser since we only operate on a single file
        cfg = configparser.RawConfigParser()
        cfg.read(filename)
        if not cfg.has_section(section):
            cfg.add_section(section)
        cfg.set(section, option, value)
        with open(filename, 'w') as fp:
            cfg.write(fp)


def path_type(arg_str):
    """Argument type for directory path strings"""
    value = os.path.expandvars(arg_str)
    return os.path.expanduser(value)

def tristate_type(arg_str):
    """Type for tristate arguments"""
    try:
        value = gbp.tristate.Tristate(arg_str)
    except TypeError:
        raise ArgumentTypeError("invalid value: %r" % arg_str)
    else:
        return value


class GbpConfArgParser(object):
    """
     This class adds GBP-specific feature of argument parser and argument
     groups, i.e. config file options and argument prefixing.  The class is
     basiclly a wrapper around argument parser and argument groups and adds the
     possibility to read defaults from a config file.
    """

    class _GbpArgParser(ArgumentParser):
        """The "real" argument parser"""
        def __init__(self, **kwargs):
            """The "real" argument parser"""
            prog = kwargs.get('prog')
            if (prog and not
                    (prog.startswith('git-') or prog.startswith('gbp-'))):
                kwargs['prog'] = "gbp %s" % prog
            if 'formatter_class' not in kwargs:
                kwargs['formatter_class'] = ArgumentDefaultsHelpFormatter
            ArgumentParser.__init__(self, **kwargs)
            self.command = prog if prog else self.prog
            self.register('type', 'tristate', tristate_type)
            self.register('type', 'path', path_type)

    def __init__(self, wrapped_instance, prefix, config=None,
                 conf_file_args=None):
        self.wrapped = wrapped_instance
        self.prefix = prefix
        if config:
            self.config = config
        else:
            self.config = GbpConfig(wrapped_instance.command)
        if conf_file_args is None:
            self.conf_file_args = set()
        else:
            self.conf_file_args = conf_file_args

    @classmethod
    def create_parser(cls, prefix='', config=None, **kwargs):
        """Create new GbpConfArgParser"""
        parser = cls._GbpArgParser(**kwargs)
        parser.add_argument('--version', action='version',
                            version='%s %s' % (parser.prog, gbp_version))
        return cls(parser, prefix=prefix, config=config)

    def _get_conf_key(self, *args):
        """Get name of the config file key for an argument"""
        # Use the first arg string by default
        key = args[0]
        # Search for the first "long argument name"
        for arg in args:
            if (len(arg) > 2 and
                arg[0:2] in [char*2 for char in self.wrapped.prefix_chars]):
                key = arg
                break
        return key.lstrip(self.wrapped.prefix_chars)

    @staticmethod
    def _is_boolean(**kwargs):
        """Is the to-be-added arg a boolean option"""
        if ('action' in kwargs and
            kwargs['action'] in ('store_true', 'store_false')):
            return True
        return False

    def add_arg(self, *args, **kwargs):
        """Add argument. Handles argument prefixing."""
        if not 'dest' in kwargs:
            kwargs['dest'] = self._get_conf_key(*args).replace('-', '_')
        args = [arg.replace('--', '--%s' % self.prefix, 1) for arg in args]
        return self.wrapped.add_argument(*args, **kwargs)

    def add_conf_file_arg(self, *args, **kwargs):
        """Add config file argument"""
        name = self._get_conf_key(*args)
        is_boolean = self._is_boolean(**kwargs)
        if is_boolean:
            kwargs['default'] = self.config.get_dual_bool_value(name)
        else:
            kwargs['default'] = self.config.get_value(name)
        self.conf_file_args.add(name)
        if not 'help' in kwargs and name in self.config.default_helps:
            kwargs['help'] = self.config.default_helps[name]
        new_arg = self.add_arg(*args, **kwargs)

        # Automatically add the inverse argument, with inverted default
        if is_boolean:
            kwargs['dest'] = new_arg.dest
            kwargs['help'] = "negates '--%s%s'" % (self.prefix, name)
            kwargs['action'] = 'store_false' \
                        if kwargs['action'] == 'store_true' else 'store_true'
            kwargs['default'] = not kwargs['default']
            self.add_arg('--no-%s' % name, **kwargs)

    def add_bool_conf_file_arg(self, *args, **kwargs):
        """Shortcut to adding boolean args"""
        kwargs['action'] = 'store_true'
        self.add_conf_file_arg(*args, **kwargs)

    def _wrap_generator(self, method, *args, **kwargs):
        """Helper for methods returning a new instance"""
        wrapped = self.wrapped.__getattribute__(method)(*args, **kwargs)
        return GbpConfArgParser(wrapped_instance=wrapped,
                                prefix=self.prefix,
                                config=self.config,
                                conf_file_args=self.conf_file_args)

    def add_argument_group(self, *args, **kwargs):
        """Add argument group"""
        return self._wrap_generator('add_argument_group', *args, **kwargs)

    def add_mutually_exclusive_group(self, *args, **kwargs):
        """Add group of mutually exclusive arguments"""
        return self._wrap_generator('add_mutually_exclusive_group',
                                    *args, **kwargs)

    def __getattr__(self, name):
        return self.wrapped.__getattribute__(name)

    def get_conf_file_value(self, option_name):
        """
        Query a single interpolated config file value.

        @param option_name: the config file option to look up
        @type option_name: string
        @returns: The config file option value or C{None} if it doesn't exist
        @rtype: C{str} or C{None}
        """
        try:
            return self.config.get_value(option_name)
        except KeyError:
            return None


class GbpConfigDebian(GbpConfig):
    """Config file parser for Debian tools"""
    defaults = dict(GbpConfig.defaults)
    defaults.update({
            'builder': 'debuild -i -I',
            })


class GbpConfArgParserDebian(GbpConfArgParser):
    """Joint config and arg parser for Debian tools"""

    def __init__(self, wrapped_instance, prefix, config=None,
                 conf_file_args=None):
        if not config:
            config = GbpConfigDebian(wrapped_instance.command)
        super(GbpConfArgParserDebian, self).__init__(wrapped_instance, prefix,
                                                     config, conf_file_args)


class GbpConfigRpm(GbpConfig):
    """Config file parser for the RPM tools"""
    defaults = dict(GbpConfig.defaults)
    defaults.update({
            'tmp-dir': '/var/tmp/gbp/',
            'vendor': 'Downstream',
            'packaging-branch': 'master',
            'packaging-dir': '',
            'packaging-tag-msg': '%(pkg)s (vendor)s release %(version)s',
            'packaging-tag': 'packaging/%(version)s',
            'export-sourcedir': 'SOURCES',
            'export-specdir': 'SPECS',
            'export-dir': '../rpmbuild',
            'builder': 'rpmbuild',
            'spec-file': '',
            'mock': 'False',
            'dist': '',
            'arch': '',
            'mock-root': '',
            'mock-options': '',
            'native': 'auto',
            })
    default_helps = dict(GbpConfig.default_helps)
    default_helps.update({
            'tmp-dir':
                "Base directory under which temporary directories are created",
            'vendor':
                "Distribution vendor name",
            'packaging-branch':
                "Branch the packaging is being maintained on, rpm counterpart "
                "of the 'debian-branch' option",
            'packaging-dir':
                "Subdir for RPM packaging files",
            'packaging-tag':
                "Format string for packaging tags, RPM counterpart of the "
                "'debian-tag' option",
            'packaging-tag-msg':
                "Format string for packaging tag messages",
            'spec-file':
                "Spec file to use, causes the packaging-dir option to be "
                "ignored",
            'export-sourcedir':
                "Subdir (under EXPORT_DIR) where packaging sources (other "
                "than the spec file) are exported",
            'export-specdir':
                "Subdir (under EXPORT_DIR) where package spec file is exported",
            'mock':
                "Invoke mock for building using gbp-builder-mock",
            'dist':
                "Build for this distribution when using mock. E.g.: epel-6",
            'arch':
                "Build for this architecture when using mock",
            'mock-root':
                "The mock root (-r) name for building with mock: <dist>-<arch>",
            'mock-options':
                "Options to pass to mock",
            'native':
                "Treat this package as native",
                })



class GbpConfArgParserRpm(GbpConfArgParser):
    """Joint config and arg parser for the RPM tools"""

    def __init__(self, wrapped_instance, prefix, config=None,
                 conf_file_args=None):
        if not config:
            config = GbpConfigRpm(wrapped_instance.command)
        super(GbpConfArgParserRpm, self).__init__(wrapped_instance, prefix,
                                                     config, conf_file_args)

# vim:et:ts=4:sw=4:et:sts=4:ai:set list listchars=tab\:»·,trail\:·:
