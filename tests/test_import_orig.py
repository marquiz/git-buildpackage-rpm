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

import os
import tarfile
import unittest

from gbp.errors import GbpError
from gbp.scripts.import_orig import find_source


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
