# vim: set fileencoding=utf-8 :
#
# (C) 2006,2007 Guido Guenther <agx@sigxcpu.org>
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
"""provides some rpm source package related helpers"""

import commands
import sys
import os
import re
import tempfile
import glob
import shutil as shutil
from optparse import OptionParser

import gbp.command_wrappers as gbpc
from gbp.errors import GbpError
from gbp.git import GitRepositoryError
from gbp.patch_series import (PatchSeries, Patch)
import gbp.log
from gbp.pkg import (UpstreamSource, compressor_opts, parse_archive_filename)
from gbp.rpm.policy import RpmPkgPolicy

try:
    # Try to load special RPM lib to be used for GBP (only)
    rpm = __import__(RpmPkgPolicy.python_rpmlib_module_name)
except ImportError:
    gbp.log.debug("Failed to import '%s' as rpm python module, using host's default rpm library instead" % RpmPkgPolicy.python_rpmlib_module_name)
    import rpm

# define a large number to check the valid id of source file
MAX_SOURCE_NUMBER = 99999


class NoSpecError(Exception):
    """Spec file parsing error"""
    pass

class MacroExpandError(Exception):
    """Macro expansion in spec file failed"""
    pass


class RpmUpstreamSource(UpstreamSource):
    """Upstream source class for RPM packages"""
    def __init__(self, name, unpacked=None, **kwargs):
        super(RpmUpstreamSource, self).__init__(name,
                                                unpacked,
                                                RpmPkgPolicy,
                                                **kwargs)


class SrcRpmFile(object):
    """Keeps all needed data read from a source rpm"""
    def __init__(self, srpmfile):
        # Do not required signed packages to be able to import
        ts_vsflags = (rpm.RPMVSF_NOMD5HEADER | rpm.RPMVSF_NORSAHEADER |
                      rpm.RPMVSF_NOSHA1HEADER | rpm.RPMVSF_NODSAHEADER |
                      rpm.RPMVSF_NOMD5 | rpm.RPMVSF_NORSA | rpm.RPMVSF_NOSHA1 |
                      rpm.RPMVSF_NODSA)
        srpmfp = open(srpmfile)
        self.rpmhdr = rpm.ts(vsflags=ts_vsflags).hdrFromFdno(srpmfp.fileno())
        srpmfp.close()
        self.srpmfile = os.path.abspath(srpmfile)

    def _get_version(self):
        """
        Get the (downstream) version of the RPM
        """
        version = dict(upstreamversion = self.rpmhdr[rpm.RPMTAG_VERSION],
                       release = self.rpmhdr[rpm.RPMTAG_RELEASE])
        if self.rpmhdr[rpm.RPMTAG_EPOCH] is not None:
            version['epoch'] = str(self.rpmhdr[rpm.RPMTAG_EPOCH])
        return version
    version = property(_get_version)

    def _get_name(self):
        """
        Get the name of the RPM package
        """
        return self.rpmhdr[rpm.RPMTAG_NAME]
    name = property(_get_name)

    def _get_upstream_version(self):
        """
        Get the upstream version of the package
        """
        return self.rpmhdr[rpm.RPMTAG_VERSION]
    upstreamversion = property(_get_upstream_version)

    def _get_packager(self):
        """
        Get the packager of the RPM package
        """
        return self.rpmhdr[rpm.RPMTAG_PACKAGER]
    packager = property(_get_packager)

    def unpack(self, dest_dir):
        """
        Unpack the source rpm to tmpdir.
        Leave the cleanup to the caller in case of an error.
        """
        gbpc.RunAtCommand('rpm2cpio',
                          [self.srpmfile, '|', 'cpio', '-id'],
                          shell=True)(dir=dest_dir)


class SpecFile(object):
    """Class for parsing/modifying spec files"""
    tag_re = re.compile(r'^(?P<name>[a-z][a-z0-9]*)\s*:\s*(?P<value>\S(.*\S)?)\s*$', flags=re.I)
    source_re = re.compile(r'^Source(?P<srcnum>[0-9]+)?\s*:\s*(?P<name>[^\s].*[^\s])\s*$', flags=re.I)
    patchtag_re = re.compile(r'^Patch(?P<patchnum>[0-9]+)?\s*:\s*(?P<name>\S.*)$', flags=re.I)
    patchmacro_re = re.compile(r'^%patch(?P<patchnum>[0-9]+)?(\s+(?P<args>.*))?$')
    setupmacro_re = re.compile(r'^%setup(\s+(?P<args>.*))?$')
    marker_re = re.compile(r'^#\s+(?P<marker>>>|<<)\s+(?P<what>gbp-[^\s]+)\s*(?P<comment>.*)$')
    gbptag_re = re.compile(r'^\s*#\s*gbp(?P<tagname>[a-z]+)\s*:\s*(?P<data>\S.*)\s*$', flags=re.I)

    def __init__(self, specfile, skip_tags=("ExcludeArch", "ExcludeOS",
                                            "ExclusiveArch", "ExclusiveOS",
                                            "BuildArch")):
        with tempfile.NamedTemporaryFile(prefix='gbp') as temp:
            try:
                with open(specfile) as specf:
                    with open(temp.name, 'w') as filtered:
                        filtered.writelines(line for line in specf \
                            if line.split(":")[0].strip() not in skip_tags)
                        filtered.flush()
                        try:
                            self.specinfo = rpm.spec(temp.name)
                        except ValueError as err:
                            raise GbpError("RPM error while parsing spec: %s" % err)
            except IOError as err:
                raise NoSpecError("Unable to read spec file: %s" % err)

        source_header = self.specinfo.packages[0].header
        self.name = source_header[rpm.RPMTAG_NAME]
        self.upstreamversion = source_header[rpm.RPMTAG_VERSION]
        self.release = source_header[rpm.RPMTAG_RELEASE]
        # rpm-python returns epoch as 'long', convert that to string
        self.epoch = str(source_header[rpm.RPMTAG_EPOCH]) \
            if source_header[rpm.RPMTAG_EPOCH] != None else None
        self.packager = source_header[rpm.RPMTAG_PACKAGER]
        self.specfile = os.path.abspath(specfile)
        self.specdir = os.path.dirname(self.specfile)
        self.patches = {}
        self.sources = {}

        # Load and parse extra info from spec file
        f = file(self.specfile)
        self.content = f.readlines()
        f.close()
        loc = self.parse_content()


        # Find 'Packager' tag. Needed to circumvent a bug in python-rpm where
        # spec.sourceHeader[rpm.RPMTAG_PACKAGER] is not reset when a new spec
        # file is parsed
        if loc['packagertag'] is None:
            self.packager = None

        # Update sources info (basically possible macros expanded by spec.__init__()
        # And, double-check that we parsed spec content correctly
        for (name, num, typ) in self.specinfo.sources:
            # workaround rpm parsing bug
            if num >= MAX_SOURCE_NUMBER:
                num = 0
            if typ == 1:
                if num in self.sources:
                    self.sources[num]['full_path'] = name
                    self.sources[num]['filename'] = os.path.basename(name)
                    self.sources[num]['filename_base'],\
                    self.sources[num]['archive_fmt'],\
                    self.sources[num]['compression'] = parse_archive_filename(os.path.basename(name))
                    # Make a guess about the prefix in the archive
                    if self.sources[num]['archive_fmt']:
                        _name, _version = RpmPkgPolicy.guess_upstream_src_version(name)
                        if _name and _version:
                            self.sources[num]['prefix'] = "%s-%s/" % (_name, _version)
                        else:
                            self.sources[num]['prefix'] = self.sources[num]['filename_base'] + "/"
                else:
                    gbp.log.err("BUG: we didn't correctly parse all 'Source' tags!")
            if typ == 2:
                if num in self.patches:
                    self.patches[num]['filename'] = name
                else:
                    gbp.log.err("BUG: we didn't correctly parse all 'Patch' tags!")

        self.orig_src_num = self.guess_orig_file()


    def _get_version(self):
        """
        Get the (downstream) version
        """
        version = dict(upstreamversion = self.upstreamversion,
                       release = self.release)
        if self.epoch != None:
            version['epoch'] = self.epoch
        return version
    version = property(_get_version)

    def _get_orig_src(self):
        """
        Get the orig src
        """
        if self.orig_src_num != None:
            return self.sources[self.orig_src_num]
        return None
    orig_src = property(_get_orig_src)

    def _macro_replace(self, matchobj):
        macro_dict = {'name': self.name,
                      'version': self.upstreamversion,
                      'release': self.release}

        if matchobj.group(2) in macro_dict:
            return macro_dict[matchobj.group(2)]
        raise MacroExpandError("Unknown macro '%s'" % matchobj.group(0))


    def macro_expand(self, text):
        """
        Expand the rpm macros (that gbp knows of) in the given text.

        @param text: text to check for macros
        @type text: C{str}
        @return: text with macros expanded
        @rtype: C{str}
        """
        # regexp to match '%{macro}' and '%macro'
        macro_re = re.compile(r'%({)?(?P<macro_name>[a-z_][a-z0-9_]*)(?(1)})', flags=re.I)
        return macro_re.sub(self._macro_replace, text)


    def write_spec_file(self):
        """
        Write, possibly updated, spec to disk
        """
        tmpffd, tmpfpath = tempfile.mkstemp(suffix='.spec', dir='.')
        tmpf = os.fdopen(tmpffd, 'w')
        tmpf.writelines(self.content)
        tmpf.close()

        shutil.move(tmpfpath, self.specfile)


    def parse_content(self):
        """
        Go through spec file content line-by-line and (re-)parse info from it
        """
        # Check location of "interesting" tags and macros
        ret = {'nametag': None,
               'packagertag': None,
               'setupmacro': None,
               'prepmacro': None}

        # First, we parse the spec for special git-buildpackage tags, only
        ignorepatch = []
        for line in self.content:
            m = self.gbptag_re.match(line)
            if m:
                if m.group('tagname').lower() == 'ignorepatch':
                    dataitems = m.group('data').strip().split()
                    ignorepatch = sorted([int(num) for num in dataitems])
                else:
                    gbp.log.info("Found unrecognized Gbp tag on line %s: '%'" % (i, line))

        # Remove all autoupdate patches to be sure we're in sync
        for n in self.patches.keys():
            if not n in ignorepatch:
                del self.patches[n]

        # Parser for patch macros
        patchparser = OptionParser()
        patchparser.add_option("-p", dest="strip")
        patchparser.add_option("-s", dest="silence")
        patchparser.add_option("-P", dest="patchnum")
        patchparser.add_option("-b", dest="backup")
        patchparser.add_option("-E", dest="removeempty")

        # Parser for patch macros
        setupparser = OptionParser()
        setupparser.add_option("-n", dest="name")
        setupparser.add_option("-c", dest="create_dir", action="store_true")
        setupparser.add_option("-D", dest="no_delete_dir", action="store_true")
        setupparser.add_option("-T", dest="no_unpack_default", action="store_true")
        setupparser.add_option("-b", dest="unpack_before")
        setupparser.add_option("-a", dest="unpack_after")
        setupparser.add_option("-q", dest="quiet", action="store_true")

        numlines = len(self.content)
        for i in range(numlines):
            line = self.content[i]

            # Find special git-buildpackage tags
            m = self.gbptag_re.match(line)
            if m:
                if m.group('tagname').lower() == 'ignorepatch':
                    dataitems = m.group('data').strip().split()
                    ignorepatch = set([int(num) for num in dataitems])
                else:
                    gbp.log.info("Found unrecognized Gbp tag on line %s: '%'" % (i, line))

            # Find 'Source' tags
            m = self.source_re.match(line)
            if m:
                if m.group('srcnum'):
                    srcnum = int(m.group('srcnum'))
                else:
                    srcnum = 0
                if srcnum in self.sources:
                    self.sources[srcnum]['tag_linenum'] = i
                else:
                    self.sources[srcnum] = {'full_path': m.group('name'),
                                            'filename': os.path.basename(m.group('name')),
                                            'tag_linenum': i,
                                            'prefix': None,
                                            'setup_options': None, }
                continue

            # Find 'Patch' tags
            m = self.patchtag_re.match(line)
            if m:
                if m.group('patchnum'):
                    patchnum = int(m.group('patchnum'))
                else:
                    patchnum = 0
                if patchnum in self.patches:
                    # For non-autoupdate patches, we only update the line number
                    if patchnum in ignorepatch:
                        self.patches[patchnum]['tag_linenum'] = i
                    else:
                        gbp.log.err("Patch%s found multiple times, aborting as gbp spec/patch autoupdate likely fails" % patchnum)
                        raise GbpError, "RPM error while parsing spec, duplicate patches found"
                else:
                    new_patch = {'name': m.group('name').strip(),
                                 'filename': m.group('name'),
                                 'apply': False,
                                 'strip': '0',
                                 'macro_linenum': None,
                                 'autoupdate': not patchnum in ignorepatch,
                                 'tag_linenum': i}
                    self.patches[patchnum] = new_patch
                continue

            # Find patch macros
            m = self.patchmacro_re.match(line)
            if m:
                (options, args) = patchparser.parse_args(m.group('args').split())
                if m.group('patchnum'):
                    patchnum = int(m.group('patchnum'))
                elif options.patchnum:
                    patchnum = int(options.patchnum)
                else:
                    patchnum = 0

                if options.strip:
                    self.patches[patchnum]['strip'] = options.strip
                self.patches[patchnum]['macro_linenum'] = i
                self.patches[patchnum]['apply'] = True
                continue

            # Find setup macros
            m = self.setupmacro_re.match(line)
            if m:
                (options, args) = setupparser.parse_args(m.group('args').split())
                srcnum = None
                if options.no_unpack_default:
                    if options.unpack_before:
                        srcnum = int(options.unpack_before)
                    elif options.unpack_after:
                        srcnum = int(options.unpack_after)
                else:
                    srcnum = 0
                if srcnum != None and srcnum in self.sources:
                    self.sources[srcnum]['setup_options'] = options
                # Save the occurrence of last setup macro
                ret['setupmacro'] = i
                continue

            # Only search for the last occurrence of the following
            m = self.tag_re.match(line)
            if m:
                if m.group('name').lower() == 'name':
                    ret['nametag'] = i
                if m.group('name').lower() == 'packager':
                    ret['packagertag'] = i
                continue
            if re.match("^%prep(\s.*)?$", line):
                ret['prepmacro'] = i
                continue
        return ret


    def update_patches(self, patchfilenames):
        """
        Update spec with new patch tags and patch macros.
        """
        loc = self.parse_content()

        # Remove updatable patches and check the max patchnumber of non-autoupdate patches
        start_patch_tag_num = 0
        last_ignored_patch_tag_line = 0
        last_ignored_patch_macro_line = 0
        rm_tag_lines = []
        rm_macro_lines = []
        for n in self.patches.keys():
            p = self.patches[n]
            if p['autoupdate']:
                rm_tag_lines.append(p['tag_linenum'])
                # Remove a preceding comment line if it seems to originate from GBP
                if re.match("^\s*#.*patch.*auto-generated", self.content[p['tag_linenum']-1], flags=re.I):
                    rm_tag_lines.append(p['tag_linenum']-1)
                if p['macro_linenum']:
                    rm_macro_lines.append(p['macro_linenum'])
                    # Take a preceding comment line if it ends with '.patch' or '.diff'
                    if re.match("^\s*#.+(patch|diff)(\.(gz|bz2|xz|lzma))?\s*$", self.content[p['macro_linenum']-1], flags=re.I):
                        rm_macro_lines.append(p['macro_linenum']-1)
                # Remove autoupdate patches from list of patches
                del self.patches[n]
            else:
                if n >= start_patch_tag_num:
                    start_patch_tag_num = n + 1
                if p['tag_linenum'] >= last_ignored_patch_tag_line:
                    last_ignored_patch_tag_line = p['tag_linenum']
                if p['macro_linenum'] and p['macro_linenum'] > last_ignored_patch_macro_line:
                    last_ignored_patch_macro_line = p['macro_linenum']
        gbp.log.debug("Starting autoupdate patch macro numbering from %s" % start_patch_tag_num)

        rm_tag_lines.sort()
        rm_macro_lines.sort()

        # Add new patches
        patchnum = start_patch_tag_num
        for p in patchfilenames:
            self.patches[patchnum] = {'name': p, 'filename': p, 'apply': True, 'strip': '1', 'macro_linenum': None, 'autoupdate': True, 'tag_linenum': None}
            patchnum += 1

        # Determine where to add %patch macro lines
        if len(rm_macro_lines):
            gbp.log.debug("Will remove patch macro lines %s from spec file" % rm_macro_lines)
            linenum = rm_macro_lines[-1] + 1
        elif last_ignored_patch_macro_line:
            linenum = last_ignored_patch_macro_line + 1
        elif 'setupmacro' in loc:
            gbp.log.info("Didn't find any old '%patch' macros, adding new patches after the last '%setup' macro at line %s")
            linenum = loc['setupmacro'] + 1
        else:
            gbp.log.warn("Didn't find any old '%patch' macros or %setup macro, adding new patches directly after '%prep' macro at %s.")
            linenum = loc['prepmacro'] + 1

        # Add all patch macro lines to content, in reversed order
        for n in reversed(sorted(self.patches.keys())):
            patch = self.patches[n]
            if patch['autoupdate'] and patch['apply']:
                # We're adding from bottom to top...
                self.content.insert(linenum, "%%patch%d -p%s\n" % (n, patch['strip']))
                # Use 'name', that is filename with macros not expanded
                self.content.insert(linenum, "# %s\n" % patch['name'])
        # Remove all old patch macro lines
        for l in reversed(rm_macro_lines):
            gbp.log.debug("Removing line #%s from spec: '%s'" % (l, self.content[l].strip()))
            self.content.pop(l)

        # Determine where to add Patch tag lines
        if len(rm_tag_lines):
            gbp.log.debug("Will remove patch tag lines %s from spec file" % rm_tag_lines)
            linenum = rm_tag_lines[-1] + 1
        elif last_ignored_patch_tag_line:
            linenum = last_ignored_patch_tag_line + 1
        elif len(self.sources):
            gbp.log.info("Didn't find any old 'Patch' tags, adding new patches after the last 'Source' tag.")
            lastsource = sorted(self.sources.keys())[-1]
            linenum = self.sources[lastsource]['tag_linenum'] + 1
        else:
            gbp.log.info("Didn't find any old 'Patch' or 'Source' tags, adding new patches after the last 'Name' tag.")
            linenum = loc['nametag'] + 1

        # Add all patch tag lines to content, in reversed order
        patch_tags_added = 0
        for n in reversed(sorted(self.patches.keys())):
            patch = self.patches[n]
            if patch['autoupdate']:
                # "PatchXYZ:" text 12 chars wide, left aligned
                text = "%-12s%s" % ("Patch%d:" % n, patch['name'])
                self.content.insert(linenum, text + "\n")
                patch_tags_added += 1
        if patch_tags_added > 0:
            # Finally, add a comment indicating gbp generated patches
            self.content.insert(linenum, "# Patches auto-generated by "
                                         "git-buildpackage:\n")
        # Remove all old patch tag lines
        for l in reversed(rm_tag_lines):
            gbp.log.debug("Removing line #%s from spec: '%s'" % (l, self.content[l].strip()))
            self.content.pop(l)


    def patchseries(self):
        """
        Return patches of the RPM as a gbp patchseries
        """
        series = PatchSeries()
        patchdir = os.path.dirname(self.specfile)
        for n, p in sorted(self.patches.iteritems()):
            if p['autoupdate'] and p['apply']:
                series.append(Patch(os.path.join(patchdir, p['filename']), strip = int(p['strip'])))
        return series


    def guess_orig_file(self):
        """
        Try to guess the name of the primary upstream/source archive
        returns a tuple with full file path, filename base, archive format and
        compression method.
        """
        orig_num = None
        for (num, src) in sorted(self.sources.iteritems()):
            filename = os.path.basename(src['filename'])
            if filename.startswith(self.name):
                # Take the first archive that starts with pkg name
                if src['archive_fmt']:
                    orig_num = num
                    break
            # otherwise we take the first archive
            elif orig_num == None and src['archive_fmt']:
                orig_num = num
            # else don't accept

        # Refine our guess about the prefix
        if orig_num != None:
            orig = self.sources[orig_num]
            setup_opts = orig['setup_options']
            if setup_opts:
                if setup_opts.create_dir:
                    orig['prefix'] = ''
                elif setup_opts.name:
                    try:
                        orig['prefix'] = self.macro_expand(setup_opts.name) + \
                                         '/'
                    except MacroExpandError as err:
                        gbp.log.warn("Couldn't determine prefix from %%setup "\
                                     "macro (%s). Using filename base as a "  \
                                     "fallback" % err)
                        orig['prefix'] = orig['filename_base'] + '/'
                else:
                    # RPM default
                    orig['prefix'] = "%s-%s/" % (self.name,
                                                 self.upstreamversion)
        return orig_num


def parse_srpm(srpmfile):
    """parse srpm by creating a SrcRpmFile object"""
    try:
        srcrpm = SrcRpmFile(srpmfile)
    except IOError, err:
        raise GbpError, "Error reading src.rpm file: %s" % err
    except rpm.error, err:
        raise GbpError, "RPM error while reading src.rpm: %s" % err

    return srcrpm


def parse_spec(specfile):
    try:
        return SpecFile(specfile)
    except IOError, err:
        raise GbpError, "Error reading spec file: %s" % err


def guess_spec(topdir, recursive=True, preferred_name=None):
    """Guess a spec file"""
    specs = []
    abstop = os.path.abspath(topdir)
    for (root, dirs, files) in os.walk(abstop):
        for f in files:
            # Stop at the first file matching the preferred name
            if f == preferred_name:
                gbp.log.debug("Found a preferred spec file: %s in %s" % (f, root))
                specs = [os.path.join(root,f)]
                recursive = False
                break
            if f.endswith(".spec"):
                gbp.log.debug("Found spec file: %s in %s" % (f, root))
                specs.append(os.path.join(root,f))

        if not recursive:
            del dirs[:]
        # Skip .git dir in any case
        if '.git' in dirs:
            dirs.remove('.git')

    if len(specs) == 0:
        raise NoSpecError("No spec file found.")
    elif len(specs) > 1:
        filenames = [os.path.relpath(spec, abstop) for spec in specs]
        raise NoSpecError("Multiple spec files found (%s), don't know which "
                          "to use." % ', '.join(filenames))
    return specs[0]

def guess_spec_repo(repo, branch, packaging_dir):
    """
    @todo: implement this
    Try to find/parse the spec file from given branch in the git
    repository.
    """
    raise NoSpecError, "Searching spec from other branch not implemented yet"


def string_to_int(val_str):
    """
    Convert string of possible unit identifier to int.

    @param val_str: value to be converted
    @type val_str: C{str}
    @return: value as integer
    @rtype: C{int}

    >>> string_to_int("1234")
    1234
    >>> string_to_int("123k")
    125952
    >>> string_to_int("1234K")
    1263616
    >>> string_to_int("1M")
    1048576
    """
    units = {'k': 1024,
             'm': 1024**2,
             'g': 1024**3,
             't': 1024**4}

    if val_str[-1].lower() in units:
        return int(val_str[:-1]) * units[val_str[-1].lower()]
    else:
        return int(val_str)


# vim:et:ts=4:sw=4:et:sts=4:ai:set list listchars=tab\:»·,trail\:·:
