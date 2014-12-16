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
"""Bitbake helper functionality"""

import os
import re
import shutil
import string
import subprocess
import sys
import tempfile
from collections import defaultdict

import gbp.log
from gbp.errors import GbpError
from gbp.git.repository import GitRepository, GitRepositoryError
from gbp.scripts.common.buildpackage import dump_tree

bb = None

#   pylint: disable=bad-continuation


def import_bb():
    """Import bitbake lib"""
    bb_bin = subprocess.Popen(['which', 'bitbake'], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE).communicate()[0]
    if bb_bin:
        bb_lib_path = os.path.dirname(bb_bin) + '/../lib'
        sys.path.insert(0, bb_lib_path)
    try:
        return __import__('bb')
    except ImportError:
        print "ERROR: Unable to find bitbake/lib, try initializing build " \
              "environment with the 'oe-init-build-env' script\n"
        # Return None instead of raising (ImportError) so that building of
        # this package succeeds in Debian. Otherwise dpkg-buildpackage fails
        # because of an import error in epydoc.
        return None

def init_tinfoil(config_only=False, tracking=False):
    """Initialize the Bitbake tinfoil module"""
    import bb.tinfoil
    try:
        tinfoil = bb.tinfoil.Tinfoil(tracking=tracking)
    except (SystemExit, bb.BBHandledException):
        raise GbpError("Failed to initialize tinfoil")
    tinfoil.prepare(config_only=config_only)
    return tinfoil


def pkg_version(data):
    """Get package version as a dict"""
    return {'upstreamversion': data.getVar('PV', True),
            'release': data.getVar('PR', True),
            'version': data.getVar('PV', True) + '-' + data.getVar('PR', True)}


class BBFile(object):
    """Class representing .bb meta data"""
    var_ops = r'\+=|=\+|\?=|\?\?=|:=|='
    vardef_re = re.compile(
            r'(^(?P<name>\w+)\s*(?P<op>%s)\s*)(?P<value>\S.*)' % var_ops)


    def __init__(self, path, cfg_data=None):
        self.bb_file = os.path.basename(path)
        self.bb_dir = os.path.abspath(os.path.dirname(path))

        self._pkg_data = None
        self._variables = {}
        self.includes = []
        self.localfiles = []

        if cfg_data is not None:
            self.parse_bb(path, cfg_data)
        else:
            self.naive_parse_bb(path)

    @property
    def version(self):
        """Get version information as a dict"""
        return {'upstreamversion': self.getVar('PV', True),
                'release': self.getVar('PR', True)}

    @property
    def bb_path(self):
        """Full path of the bb file"""
        return os.path.join(self.bb_dir, self.bb_file)

    def parse_bb(self, path, cfg_data):
        """Parse bb meta file"""
        self._pkg_data = bb.cache.Cache.loadDataFull(path, [], cfg_data)

        # Determine local packaging files
        uris = (self.getVar('SRC_URI', True) or "").split()
        fetcher = bb.fetch2.Fetch(uris, self._pkg_data)
        bb_dir = os.path.dirname(self.getVar('FILE'))
        # Also check for file existence as fetcher incorrecly returns some
        # non-existent .bbclass files under the recipe directory
        self.includes = [path for path in self.getVar('BBINCLUDED').split() if
                            path.startswith(bb_dir) and os.path.exists(path)]
        self.localfiles = [path for path in fetcher.localpaths() if
                            path.startswith(bb_dir)]

    def naive_parse_bb(self, path):
        """Naive parsing of standalone recipes"""
        # Some variable defaults
        # e.g. take package name and version directly from recipe file name
        self._variables['FILE'] = os.path.abspath(path)
        fn_base, _fn_ext = os.path.splitext(os.path.basename(path))
        split_base = fn_base.rsplit('_', 1)
        if len(split_base) == 2:
            self._variables['PN'] = split_base[0]
            self._variables['PV'] = split_base[1]
        else:
            self._variables['PN'] = fn_base
            self._variables['PV'] = '1.0'
        self._variables['PR'] = 'r0'

        def var_parse_cb(lines):
            """Callback function for parsing variables"""
            unwrapped = self.unwrap_lines(lines)
            match = self.vardef_re.match(unwrapped)
            if match:
                var = match.groupdict()
                value = self.unquote_val(var['value'])

                if (var['name'] not in self._variables or
                    var['op'] in ('=', ':=')):
                    self._variables[var['name']] = value
                elif var['op'] in ('+=', '=+'):
                    self._variables[var['name']] += ' ' + value
            else:
                splitted = unwrapped.split(None, 1)
                if (len(splitted) > 1 and
                    splitted[0] in ('include', 'require')):
                    inc_fname = splitted[1].strip()
                    inc_path = os.path.join(os.path.dirname(path),
                                            inc_fname)
                    self.includes.append(os.path.abspath(inc_path))
                    return lines + self.parse_file(inc_path, var_parse_cb)
            return lines

        # Parse variables from file
        self.parse_file(path, var_parse_cb)

        # Find local files
        filedirs = [self.getVar('PN') + '-' + self.getVar('PV'),
                    self.getVar('PN'), 'files']
        uris = (self.getVar('SRC_URI') or "").split()
        for uri_str in uris:
            uri = bb.fetch2.URI(uri_str)
            if uri.scheme == 'file':
                found = False
                for path in [os.path.join(self.bb_dir, dirn, uri.path) for dirn
                                in filedirs]:
                    if os.path.exists(path):
                        self.localfiles.append(path)
                        found = True
                        break
                if not found:
                    gbp.log.warn("Seemingly local file '%s' not found under "
                                 "'%s'" % (uri_str, self.bb_dir))

    def _expand_single(self, match):
        """Expand single occurrence of a variable reference"""
        if match.group(1) in self._variables:
            return self._variables[match.group(1)]
        return match.group(0)

    def expand_val(self, val, rec=0):
        """Expand variable"""
        expanded = re.sub(r'\${(\w+)}', self._expand_single, val)
        if expanded == val:
            return expanded
        elif rec < 20:
            return self.expand_val(expanded, rec +1)
        else:
            raise GbpError("Too many recursions when expanding variable value")

    def getVar(self, var, expand=True):
        """Get variable"""
        if self._pkg_data:
            return self._pkg_data.getVar(var, expand)
        elif var in self._variables:
            if expand:
                return self.expand_val(self._variables[var])
            else:
                return self._variables[var]
        return None

    @staticmethod
    def unquote_val(val):
        """Unquote / strip variable value"""
        return val.strip(string.whitespace + r'"\'\\')

    @staticmethod
    def unwrap_lines(lines):
        """Return a joined string of multiple lines"""
        return ''.join([re.sub(r'\\\s*$', '', line) for line in lines])

    @staticmethod
    def var_to_str(var, values, oper='+='):
        """Create a well formatted string buffer containing a multiline variable
           assignment"""
        indent = ' ' *  (len(var) + 2 + len(oper))
        linebuf = ['%s %s "%s \\\n' % (var, oper, values[0])]
        for val in values[1:]:
            linebuf.append(indent + ' ' + val + '\\\n')
        linebuf.append(indent + '"\n')
        return linebuf

    @staticmethod
    def parse_file(filepath, cb_func):
        """Parse recipe"""
        ret_buf = []
        with open(filepath) as fobj:
            multiline = []
            for line in fobj.readlines():
                stripped = line.rstrip()
                if not multiline:
                    if not stripped.endswith('\\'):
                        ret_buf.extend(cb_func([line]))
                    else:
                        multiline = [line]
                else:
                    multiline.append(line)
                    if not stripped.endswith('\\'):
                        ret_buf.extend(cb_func(multiline))
                        multiline = []
        return ret_buf

    @staticmethod
    def set_var_val(filepath, var, val):
        """Set variable value in a recipe"""
        class _Setter(object):
            """Class for handling variable injections"""
            def __init__(self):
                self.was_set = False

            def set_cb(self, lines):
                """Parser callback for setting variable value"""
                unwrapped = BBFile.unwrap_lines(lines)
                match = BBFile.vardef_re.match(unwrapped)
                if match and match.group('name') == var:
                    if not self.was_set:
                        self.was_set = True
                        print "Setting value %s = %s" % (var, val)
                        return ['%s = "%s"\n' % (var, val)]
                    else:
                        return []
                return lines

        # Parse file and set values
        setter = _Setter()
        linebuf = BBFile.parse_file(filepath, setter.set_cb)

        # Write file
        with open(filepath, 'w') as fobj:
            if not setter.was_set:
                fobj.write('%s = "%s"\n')
            fobj.writelines(linebuf)

    @staticmethod
    def substitute_var_val(filepath, var, pattern, repl):
        """Update variable in a recipe"""
        def subst_cb(lines):
            """Parser callback for substituting variable values"""
            unwrapped = BBFile.unwrap_lines(lines)
            match = BBFile.vardef_re.match(unwrapped)
            if match and match.group('name') == var:
                filtered = []
                for line in lines:
                    line = re.sub(pattern, repl, line)
                    # Drop empty lines
                    if not re.match(r'\s*\\\s*', line):
                        filtered.append(line)
                return filtered
            return lines

        # Parse file and substitute values
        linebuf = BBFile.parse_file(filepath, subst_cb)

        # Write file
        with open(filepath, 'w') as fobj:
            fobj.writelines(linebuf)

    @staticmethod
    def append_var_val(filepath, var, new_vals):
        """Update variable in a recipe"""
        if not new_vals:
            return

        class _Finder(object):
            """Class for recording definitions of variables"""
            def __init__(self):
                self.line_ind = 0
                self.last_occurrence = -1

            def find_last_occurrence_cb(self, lines):
                """Get the point of insertion for the variable"""
                unwrapped = BBFile.unwrap_lines(lines)
                match = BBFile.vardef_re.match(unwrapped)
                if match and match.group('name') == var:
                    self.last_occurrence = self.line_ind + len(lines) - 1
                self.line_ind += len(lines)
                return lines

        finder = _Finder()
        linebuf = BBFile.parse_file(filepath, finder.find_last_occurrence_cb)

        # Prepare for appending values
        quote = None
        if finder.last_occurrence >= 0:
            last_line = linebuf[finder.last_occurrence].rstrip()
            # Guess indentation
            match = BBFile.vardef_re.match(last_line)
            if match:
                indent = ' ' * (len(match.group(1)) + 1)
            else:
                indent = re.match(r'(\s*)', last_line).group(1)

            # Guess point of insertion for new values and mangle the last line
            if re.match(r'^\s*$', last_line[:-1]):
                # Insert before the last line if it's an empty line (with a
                # quotation character only)
                insert_ind = finder.last_occurrence
                indent += ' '
            else:
                # Else, remove the quotation character and append after the
                # last line
                quote = last_line[-1]
                last_line = last_line[:-1] + ' \\\n'
                linebuf[finder.last_occurrence] = last_line
                insert_ind = finder.last_occurrence + 1
        else:
            indent = ' ' * (len(var) + 4)

        # Write file
        with open(filepath, 'w') as fobj:
            if finder.last_occurrence > -1:
                fobj.writelines(linebuf[:insert_ind])
                for val in new_vals:
                    fobj.write(indent + val + ' \\\n')
                if quote:
                    fobj.write(indent + quote + '\n')
                fobj.writelines(linebuf[insert_ind:])
            else:
                fobj.writelines(BBFile.var_to_str(var, new_vals, '+='))
                fobj.writelines(linebuf)

def guess_bb_file(file_list, bbappend):
    """Guess bb recipe from a list of filenames"""
    recipes = []
    file_exts = ['.bb'] if not bbappend else ['.bb', '.bbappend']
    for ext in file_exts:
        for filepath in file_list:
            if filepath.endswith(ext):
                gbp.log.debug("Found bb recipe file %s" % filepath)
                recipes.append(filepath)
    if len(recipes) == 0:
        raise GbpError("No recipes found.")
    return sorted(recipes)[-1]

def bb_from_repo(cfg_data, repo, treeish, bb_path):
    """Get and parse a bb recipe from a Git treeish"""
    try:
        tmpdir = tempfile.mkdtemp(prefix='gbp-bb_')
        # Dump whole bb directory
        dump_tree(repo, tmpdir, '%s:%s' % (treeish, os.path.dirname(bb_path)),
                  False)
        fpath = os.path.join(tmpdir, os.path.basename(bb_path))
        return BBFile(fpath, cfg_data)
    except GitRepositoryError as err:
        raise GbpError("Git error: %s" % err)
    finally:
        shutil.rmtree(tmpdir)

def guess_bb_path_from_fs(topdir, recursive=True, bbappend=False):
    """Guess a bitbake recipe file"""
    file_list = []
    if not topdir:
        topdir = '.'
    for root, dirs, files in os.walk(topdir):
        file_list.extend([os.path.join(root, fname) for fname in files])
        if not recursive:
            del dirs[:]
        # Skip .git dir in any case
        if '.git' in dirs:
            dirs.remove('.git')
    return guess_bb_file(file_list, bbappend)

def guess_bb_path_from_repo(repo, treeish=None, topdir='', recursive=True,
                            bbappend=False):
    """Guess a bitbake recipe path from a git repository"""
    topdir = topdir.rstrip('/') + ('/') if topdir else ''
    # Search from working copy
    if not treeish:
        abspath = guess_bb_path_from_fs(os.path.join(repo.path, topdir),
                                        recursive, bbappend)
        return os.path.relpath(abspath, repo.path)

    # Search from treeish
    try:
        file_list = [nam for (mod, typ, sha, nam) in
                    repo.list_tree(treeish, recursive, topdir) if typ == 'blob']
    except GitRepositoryError as err:
        raise GbpError("Failed to search bb recipe from treeish %s, "
                       "Git error: %s" % (treeish, err))
    return guess_bb_file(file_list, bbappend)

def guess_bb_path(options, repo, treeish=None, bbappend=False):
    """Guess recipe path, relative to repo rootdir"""
    bb_path = options.bb_file
    if options.bb_file:
        if not treeish:
            path = os.path.join(repo.path, bb_path)
            if not os.path.exists(path):
                raise GbpError("'%s' does not exist" % bb_path)
        else:
            try:
                repo.show("%s:%s" % (treeish, bb_path))
            except GbpError as err:
                raise GbpError(str(err))
    else:
        bb_path = guess_bb_path_from_repo(repo, treeish, options.meta_dir,
                                          bbappend=bbappend)
    return bb_path

def parse_bb(cfg_data, options, repo, treeish=None, bbappend=False):
    """Find and parse a bb recipe from a repository"""
    try:
        bb_path = guess_bb_path(options, repo, treeish, bbappend=bbappend)
        gbp.log.debug("Using recipe '%s'" % bb_path)
        options.meta_dir = os.path.dirname(bb_path)
        if treeish:
            pkg_data = bb_from_repo(cfg_data, repo, treeish, bb_path)
        else:
            full_path = os.path.join(repo.path, bb_path)
            pkg_data = BBFile(full_path, cfg_data)
    except GbpError as err:
        raise GbpError("Can't parse bb recipe: %s" % err)
    return pkg_data


def guess_pkg_from_dir(pkg_dir, tinfoil):
    """Guess a package from a directory in configured bitbake environment"""
    abspath = os.path.abspath(pkg_dir)
    layer_dirs = tinfoil.config_data.getVar('BBLAYERS').split()
    gbp.log.debug("Checking if %s is in %s" % (abspath, layer_dirs))
    layer_dir = ''
    for path in layer_dirs:
        if abspath.startswith(path):
            layer_dir = path
    if not layer_dir:
        raise GbpError("%s not under configured layers" % abspath)

    bb_files = [path for path in tinfoil.cooker_data.pkg_fn
                    if os.path.dirname(path) == abspath]
    if len(bb_files):
        bb_file = bb_files[-1]
        gbp.log.debug("Found %d recipes in %s, choosing %s" %
                      (len(bb_files), pkg_dir, os.path.basename(bb_file)))
    else:
        raise GbpError("No recipes found in %s" % pkg_dir)
    return bb_file

def guess_pkg(tinfoil, pkg):
    """Guess package (recipe) from configured bitbake environment"""
    if pkg in tinfoil.cooker_data.pkg_pn:
        pkg_bb = tinfoil.cooker_data.pkg_pn[pkg][0]
    elif not os.path.isdir(pkg):
        abspath = os.path.abspath(pkg)
        if abspath in tinfoil.cooker_data.pkg_fn:
            pkg_bb = abspath
        else:
            raise GbpError("Package %s not found in any configured layer" % pkg)
    elif os.path.exists(pkg):
        pkg_bb = guess_pkg_from_dir(pkg, tinfoil)
    else:
        raise GbpError("Unable to find %s" % pkg)
    return pkg_bb


# Initialize module
bb = import_bb()
