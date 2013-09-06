# vim: set fileencoding=utf-8 :
#
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
"""Test the classes under L{gbp.rpm}"""

import filecmp
import os
import shutil
import tempfile
from nose.tools import assert_raises

from gbp.errors import GbpError
from gbp.rpm import SrcRpmFile, SpecFile, parse_srpm, guess_spec, NoSpecError

DATA_DIR = os.path.abspath(os.path.splitext(__file__)[0] + '_data')
SRPM_DIR = os.path.join(DATA_DIR, 'srpms')
SPEC_DIR = os.path.join(DATA_DIR, 'specs')

class SpecFileTester(SpecFile):
    """Helper class for testing"""

    def protected(self, name):
        """Get a protected member"""
        return super(SpecFileTester, self).__getattribute__(name)


class TestSrcRpmFile(object):
    """Test L{gbp.rpm.SrcRpmFile}"""

    def setup(self):
        self.tmpdir = tempfile.mkdtemp(prefix='gbp_%s_' % __name__, dir='.')

    def teardown(self):
        shutil.rmtree(self.tmpdir)

    def test_srpm(self):
        """Test parsing of a source rpm"""
        srpm = SrcRpmFile(os.path.join(SRPM_DIR, 'gbp-test-1.0-1.src.rpm'))
        assert srpm.version ==  {'release': '1', 'upstreamversion': '1.0'}
        assert srpm.name == 'gbp-test'
        assert srpm.upstreamversion == '1.0'
        assert srpm.packager is None

    def test_srpm_2(self):
        """Test parsing of another source rpm"""
        srpm = SrcRpmFile(os.path.join(SRPM_DIR, 'gbp-test2-3.0-0.src.rpm'))
        assert srpm.version == {'release': '0', 'upstreamversion': '3.0',
                                'epoch': '2'}
        assert srpm.packager == 'Markus Lehtonen '\
                                '<markus.lehtonen@linux.intel.com>'

    def test_unpack_srpm(self):
        """Test unpacking of a source rpm"""
        srpm = SrcRpmFile(os.path.join(SRPM_DIR, 'gbp-test-1.0-1.src.rpm'))
        srpm.unpack(self.tmpdir)
        for fn in ['gbp-test-1.0.tar.bz2', 'foo.txt', 'bar.tar.gz', 'my.patch',
                   'my2.patch', 'my3.patch']:
            assert os.path.exists(os.path.join(self.tmpdir, fn)), \
                    "%s not found" % fn


class TestSpecFile(object):
    """Test L{gbp.rpm.SpecFile}"""

    def setup(self):
        self.tmpdir = tempfile.mkdtemp(prefix='gbp_%s_' % __name__, dir='.')

    def teardown(self):
        shutil.rmtree(self.tmpdir)

    def test_spec(self):
        """Test parsing of a valid spec file"""
        spec_filepath = os.path.join(SPEC_DIR, 'gbp-test.spec')
        spec = SpecFileTester(spec_filepath)

        # Test basic properties
        assert spec.specfile == os.path.basename(spec_filepath)
        assert spec.specdir == os.path.dirname(spec_filepath)

        assert spec.name == 'gbp-test'
        assert spec.packager is None

        assert spec.upstreamversion == '1.0'
        assert spec.release == '1'
        assert spec.epoch is None
        assert spec.version == {'release': '1', 'upstreamversion': '1.0'}

        orig = spec.orig_src
        assert orig['filename'] == 'gbp-test-1.0.tar.bz2'
        assert orig['filename_base'] == 'gbp-test-1.0'
        assert orig['archive_fmt'] == 'tar'
        assert orig['compression'] == 'bzip2'
        assert orig['prefix'] == 'gbp-test/'

    def test_spec_2(self):
        """Test parsing of another valid spec file"""
        spec_filepath = os.path.join(SPEC_DIR, 'gbp-test2.spec')
        spec = SpecFile(spec_filepath)

        # Test basic properties
        assert spec.name == 'gbp-test2'
        assert spec.packager == 'Markus Lehtonen ' \
                                '<markus.lehtonen@linux.intel.com>'

        assert spec.epoch == '2'
        assert spec.version == {'release': '0', 'upstreamversion': '3.0',
                                'epoch': '2'}

        orig = spec.orig_src
        assert orig['filename'] == 'gbp-test2-3.0.tar.gz'
        assert orig['archive_fmt'] == 'tar'
        assert orig['compression'] == 'gzip'
        assert orig['prefix'] == ''

    def test_spec_3(self):
        """Test parsing of yet another valid spec file"""
        spec_filepath = os.path.join(SPEC_DIR, 'gbp-test-native.spec')
        spec = SpecFile(spec_filepath)

        # Test basic properties
        assert spec.name == 'gbp-test-native'
        orig = spec.orig_src
        assert orig['filename'] == 'gbp-test-native-1.0.zip'
        assert orig['archive_fmt'] == 'zip'
        assert orig['compression'] == None
        assert orig['prefix'] == 'gbp-test-native-1.0/'

    def test_spec_4(self):
        """Test parsing of spec without orig tarball"""
        spec_filepath = os.path.join(SPEC_DIR, 'gbp-test-native2.spec')
        spec = SpecFile(spec_filepath)

        # Test basic properties
        assert spec.name == 'gbp-test-native2'
        assert spec.orig_src is None

    def test_update_spec(self):
        """Test spec autoupdate functionality"""
        # Create temporary spec file
        tmp_spec = os.path.join(self.tmpdir, 'gbp-test.spec')
        shutil.copy2(os.path.join(SPEC_DIR, 'gbp-test.spec'), tmp_spec)

        reference_spec = os.path.join(SPEC_DIR, 'gbp-test-reference.spec')
        spec = SpecFile(tmp_spec)
        spec.update_patches(['new.patch'], {})
        spec.write_spec_file()
        assert filecmp.cmp(tmp_spec, reference_spec) is True

        # Test adding the VCS tag
        reference_spec = os.path.join(SPEC_DIR, 'gbp-test-reference2.spec')
        spec.set_tag('VCS', None, 'myvcstag')
        spec.write_spec_file()
        assert filecmp.cmp(tmp_spec, reference_spec) is True

    def test_update_spec2(self):
        """Another test for spec autoupdate functionality"""
        tmp_spec = os.path.join(self.tmpdir, 'gbp-test.spec')
        shutil.copy2(os.path.join(SPEC_DIR, 'gbp-test2.spec'), tmp_spec)

        reference_spec = os.path.join(SPEC_DIR, 'gbp-test2-reference2.spec')
        spec = SpecFile(tmp_spec)
        spec.update_patches(['1.patch', '2.patch'],
                            {'1.patch': {'if': 'true'},
                             '2.patch': {'ifarch': '%ix86'}})
        spec.set_tag('VCS', None, 'myvcstag')
        spec.write_spec_file()
        assert filecmp.cmp(tmp_spec, reference_spec) is True

        # Test updating patches again and removing the VCS tag
        reference_spec = os.path.join(SPEC_DIR, 'gbp-test2-reference.spec')
        spec.update_patches(['new.patch'], {'new.patch': {'if': '1'}})
        spec.set_tag('VCS', None, '')
        spec.write_spec_file()
        assert filecmp.cmp(tmp_spec, reference_spec) is True

    def test_modifying(self):
        """Test updating/deleting of tags and macros"""
        tmp_spec = os.path.join(self.tmpdir, 'gbp-test.spec')
        shutil.copy2(os.path.join(SPEC_DIR, 'gbp-test-updates.spec'), tmp_spec)
        reference_spec = os.path.join(SPEC_DIR,
                                      'gbp-test-updates-reference.spec')
        spec = SpecFileTester(tmp_spec)

        # Mangle tags
        prev = spec.protected('_delete_tag')('Vendor', None)
        spec.protected('_set_tag')('License', None, 'new license', prev)
        spec.protected('_delete_tag')('source', 0)
        assert spec.sources() == {}
        spec.protected('_delete_tag')('patch', 0)
        spec.protected('_delete_tag')('patch', -1)
        assert spec.protected('_patches')() == {}
        prev = spec.protected('_delete_tag')('invalidtag', None)

        with assert_raises(GbpError):
            # Check that setting empty value fails
            spec.protected('_set_tag')('Version', None, '', prev)
        with assert_raises(GbpError):
            # Check that setting invalid tag with public method fails
            spec.set_tag('invalidtag', None, 'value')

        # Mangle macros
        prev = spec.protected('_delete_special_macro')('patch', -1)
        spec.protected('_delete_special_macro')('patch', 123)
        spec.protected('_set_special_macro')('patch', 0, 'my new args', prev)
        with assert_raises(GbpError):
            spec.protected('_delete_special_macro')('invalidmacro', 0)
        with assert_raises(GbpError):
            spec.protected('_set_special_macro')('invalidmacro', 0, 'args',
                           prev)

        # Check resulting spec file
        spec.write_spec_file()
        assert filecmp.cmp(tmp_spec, reference_spec) is True

    def test_quirks(self):
        """Test spec that is broken/has anomalities"""
        spec_filepath = os.path.join(SPEC_DIR, 'gbp-test-quirks.spec')
        spec = SpecFile(spec_filepath)

        # Check that we quess orig source and prefix correctly
        assert spec.orig_src['prefix'] == 'foobar/'

    def test_tags(self):
        """Test parsing of all the different tags of spec file"""
        spec_filepath = os.path.join(SPEC_DIR, 'gbp-test-tags.spec')
        spec = SpecFileTester(spec_filepath)

        # Check all the tags
        for name, val in spec.protected('_tags').iteritems():
            rval = None
            if name in ('version', 'release', 'epoch', 'nosource', 'nopatch'):
                rval = '0'
            elif name in ('autoreq', 'autoprov', 'autoreqprov'):
                rval = 'No'
            elif name not in spec.protected('_listtags'):
                rval = 'my_%s' % name
            if rval:
                assert val['value'] == rval, ("'%s:' is '%s', expecting '%s'" %
                                              (name, val['value'], rval))
            assert spec.ignorepatches == []
            # Check patch numbers and patch filenames
            patches = {}
            for patch in spec.protected('_tags')['patch']['lines']:
                patches[patch['num']] = patch['linevalue']

            assert patches == {0: 'my_patch0', -1: 'my_patch'}

    def test_patch_series(self):
        """Test the getting the patches as a patchseries"""
        spec_filepath = os.path.join(SPEC_DIR, 'gbp-test-native.spec')
        spec = SpecFileTester(spec_filepath)

        assert len(spec.patchseries()) == 0
        spec.update_patches(['1.patch', '2.patch', '3.patch'], {})
        assert len(spec.patchseries()) == 3
        spec.protected('_gbp_tags')['ignore-patches'].append({'args': "0"})
        spec.update_patches(['4.patch'], {})
        assert len(spec.patchseries()) == 1
        assert len(spec.patchseries(ignored=True)) == 2
        spec.protected('_delete_special_macro')('patch', 0)
        assert len(spec.patchseries(ignored=True)) == 1
        series = spec.patchseries(unapplied=True, ignored=True)
        assert len(series) == 2
        assert os.path.basename(series[-1].path) == '1.patch'

    def test_patch_series_quirks(self):
        """Patches are applied in order different from the patch numbering"""
        spec_filepath = os.path.join(SPEC_DIR, 'gbp-test-quirks.spec')
        spec = SpecFileTester(spec_filepath)

        # Check series is returned in the order the patches are applied
        files = [os.path.basename(patch.path) for patch in spec.patchseries()]
        assert files == ['05.patch', '01.patch']
        # Also ignored patches are returned in the correct order
        files = [os.path.basename(patch.path) for patch in
                    spec.patchseries(ignored=True)]
        assert files == ['05.patch', '02.patch', '01.patch']
        # Unapplied patches are added to the end of the series
        files = [os.path.basename(patch.path) for patch in
                    spec.patchseries(unapplied=True)]
        assert files == ['05.patch', '01.patch', '03.patch']
        # Return all patches (for which tag is found)
        files = [os.path.basename(patch.path) for patch in
                    spec.patchseries(unapplied=True, ignored=True)]
        assert files == ['05.patch', '02.patch', '01.patch', '03.patch',
                         '04.patch']


class TestUtilityFunctions(object):
    """Test utility functions of L{gbp.rpm}"""

    def test_parse_srpm(self):
        """Test parse_srpm() function"""
        parse_srpm(os.path.join(SRPM_DIR, 'gbp-test-1.0-1.src.rpm'))
        with assert_raises(GbpError):
            parse_srpm(os.path.join(DATA_DIR, 'notexists.src.rpm'))
        with assert_raises(GbpError):
            parse_srpm(os.path.join(SPEC_DIR, 'gbp-test.spec'))

    def test_guess_spec(self):
        """Test guess_spec() function"""
        # Spec not found
        with assert_raises(NoSpecError):
            guess_spec(DATA_DIR, recursive=False)
        # Multiple spec files
        with assert_raises(NoSpecError):
            guess_spec(DATA_DIR, recursive=True)
        with assert_raises(NoSpecError):
            guess_spec(SPEC_DIR, recursive=False)
        # Spec found
        spec = guess_spec(SPEC_DIR, recursive=False,
                             preferred_name = 'gbp-test2.spec')
        assert spec.specfile == 'gbp-test2.spec'
        assert spec.specdir == SPEC_DIR

# vim:et:ts=4:sw=4:et:sts=4:ai:set list listchars=tab\:»·,trail\:·:
