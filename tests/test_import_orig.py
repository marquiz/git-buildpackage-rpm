# vim: set fileencoding=utf-8 :
#
# (C) 2013 Intel Corporation <markus.lehtonen@linux.intel.com>
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
"""Test import-orig functions"""
from . import context

import glob
import os
import tarfile
import tempfile
# Try unittest2 for CentOS
try:
    import unittest2 as unittest
except ImportError:
    import unittest

from gbp.errors import GbpError
from gbp.pkg import UpstreamSource
from gbp.scripts.common.import_orig import prepare_sources
from gbp.scripts.import_orig import find_source
from tests.testutils import ls_dir, ls_tar


class TestImportOrigBase(unittest.TestCase):
    """Base class for handling context"""
    @classmethod
    def setup_class(cls):
        """Class set-up, run only once"""
        cls._tmpdir = str(context.new_tmpdir(__name__))

    @classmethod
    def teardown_class(cls):
        """Class teardown, run only once"""
        context.teardown()


class TestFindSource(TestImportOrigBase):
    """Test the Debian-specific find_source() function"""

    def test_failure(self):
        """Test failure modes"""
        with self.assertRaisesRegexp(GbpError,
                                     "More than one archive specified"):
            find_source(False, ['too', 'much'])

        with self.assertRaisesRegexp(GbpError,
                                     "No archive to import specified"):
            find_source(False, [])

        with self.assertRaisesRegexp(GbpError,
                                "you can't pass both --uscan and a filename"):
            find_source(True, ['tarball'])

    def test_success(self):
        """Successfully get source archive"""
        tar_fn = 'tarball.tar'
        # Create dummy (empty) tarball
        tarfile.open(tar_fn, 'w' ).close()
        self.assertEqual(os.path.abspath(tar_fn),
                         find_source(False, [tar_fn]).path)


class TestPrepareSources(TestImportOrigBase):
    """Test the prepare_sources() function"""
    test_pkg_name = 'test'
    test_pkg_ver = '1.0'

    @staticmethod
    def _create_test_sources(destdir):
        """Create dummy source archives"""
        destdir = os.path.abspath(destdir)
        origs = {}

        # "Normall" gzipped tarball
        archive_fn = os.path.join(destdir, 'test-1.0.tar.gz')
        src_dir = os.path.join(context.projectdir, 'gbp')
        tarobj = tarfile.open(archive_fn, mode='w:gz')
        for fname in (glob.glob('%s/*.py' % src_dir) +
                      glob.glob('%s/pkg/*.py' % src_dir)):
            arcname = 'test-1.0/' + os.path.relpath(fname, src_dir)
            tarobj.add(fname, arcname=arcname)
        tarobj.close()
        origs['tar'] = archive_fn

        # Unpacked sources
        tarobj = tarfile.open(origs['tar'], 'r')
        tarobj.extractall(destdir)
        tarobj.close()
        origs['dir'] = os.path.join(destdir,'test-1.0')
        return origs

    @classmethod
    def setup_class(cls):
        """Class set-up, run only once"""
        super(TestPrepareSources, cls).setup_class()
        # Different source archives
        cls._origs = cls._create_test_sources(cls._tmpdir)

    def test_dir(self):
        """Basic test for unpacked sources, no filtering etc"""
        tmpdir = tempfile.mkdtemp(dir=self._tmpdir, prefix='dir_basic_')
        source = UpstreamSource(self._origs['dir'])
        orig, prist = prepare_sources(source, 'test', '1.0', None,
                                      None, False, None, tmpdir)
        self.assertEqual(ls_dir(self._origs['dir']), ls_dir(orig))
        self.assertEqual(prist, '')

    def test_dir_filter(self):
        """Test filtering of unpacked sources"""
        tmpdir = tempfile.mkdtemp(dir=self._tmpdir, prefix='dir_filter_')
        source = UpstreamSource(self._origs['dir'])
        orig, prist = prepare_sources(source, 'test', '1.0', None,
                                      ['pkg'], False, None, tmpdir)
        orig_filt_ref = set([fname for fname in ls_dir(self._origs['dir'])
                                    if not fname.startswith('pkg')])
        self.assertEqual(orig_filt_ref, ls_dir(orig))
        self.assertEqual(prist, '')

    def test_dir_pristine_nofilter(self):
        """Test filtering of unpacked sources, not filtering pristine-tar"""
        tmpdir = tempfile.mkdtemp(dir=self._tmpdir, prefix='dir_filter2_')
        source = UpstreamSource(self._origs['dir'])
        orig, prist = prepare_sources(source, 'test', '1.0', 'test.tar.gz',
                                      ['pkg'], False, None, tmpdir)
        src_ls = ls_dir(self._origs['dir'])
        orig_filt_ref = set([fname for fname in src_ls
                                if not fname.startswith('pkg')])
        prist_ref = set(['test-1.0/%s' % fname for fname in src_ls] +
                        ['test-1.0'])
        self.assertEqual(orig_filt_ref, ls_dir(orig))
        self.assertEqual(prist_ref, ls_tar(prist))

    def test_dir_pristine_filter(self):
        """Test filtering pristine-tar and mangling prefix"""
        tmpdir = tempfile.mkdtemp(dir=self._tmpdir, prefix='dir_filter3_')
        source = UpstreamSource(self._origs['dir'])
        orig, prist = prepare_sources(source, 'test', '1.0', 'test.tar.gz',
                                      ['pkg'], True, 'newpref', tmpdir)
        src_ls = ls_dir(self._origs['dir'])
        orig_filt_ref = set([fname for fname in src_ls
                                if not fname.startswith('pkg')])
        prist_ref = set(['newpref/%s' % fname for fname in orig_filt_ref] +
                        ['newpref'])
        self.assertEqual(orig_filt_ref, ls_dir(orig))
        self.assertEqual(prist_ref, ls_tar(prist))

    def test_tar(self):
        """Basic test for tarball sources, with pristine-tar"""
        tmpdir = tempfile.mkdtemp(dir=self._tmpdir, prefix='tar_basic_')
        source = UpstreamSource(self._origs['tar'])
        orig, prist = prepare_sources(source, 'test', '1.0', 'test.tgz',
                                      None, False, 'test-1.0', tmpdir)
        src_ls = ls_tar(self._origs['tar'])
        orig_ref = set([fname.replace('test-1.0/', '') for fname in src_ls
                        if fname != 'test-1.0'])
        self.assertEqual(orig_ref, ls_dir(orig))
        self.assertEqual(src_ls, ls_tar(prist))

    def test_tar_pristine_prefix(self):
        """Test tarball import with prefix mangling"""
        tmpdir = tempfile.mkdtemp(dir=self._tmpdir, prefix='tar_prefix_')
        source = UpstreamSource(self._origs['tar'])
        _orig, prist = prepare_sources(source, 'test', '1.0', 'test.tgz',
                                       None, False, 'np', tmpdir)
        src_ls = ls_tar(self._origs['tar'])
        prist_ref = set([fname.replace('test-1.0', 'np') for fname in src_ls])
        self.assertEqual(prist_ref, ls_tar(prist))

    def test_tar_filter_pristine_prefix(self):
        """Filter tarball, pristine-tar prefix mangling but not filter"""
        tmpdir = tempfile.mkdtemp(dir=self._tmpdir, prefix='tar_filter_')
        source = UpstreamSource(self._origs['tar'])
        orig, prist = prepare_sources(source, 'test', '1.0', 'test.tgz',
                                      ['pkg'], False, 'newp', tmpdir)
        src_ls = ls_tar(self._origs['tar'])
        orig_ref = set([fname.replace('test-1.0/', '') for fname in src_ls
            if fname != 'test-1.0' and not fname.startswith('test-1.0/pkg')])
        prist_ref = set([fname.replace('test-1.0', 'newp') for fname in src_ls])
        self.assertEqual(orig_ref, ls_dir(orig))
        self.assertEqual(prist_ref, ls_tar(prist))

