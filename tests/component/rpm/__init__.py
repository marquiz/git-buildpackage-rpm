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
"""Test module for RPM command line tools of the git-buildpackage suite"""

import os
from xml.dom import minidom

from tests.component import ComponentTestGitRepository

RPM_TEST_DATA_SUBMODULE = os.path.join('tests', 'component', 'rpm', 'data')
RPM_TEST_DATA_DIR = os.path.abspath(RPM_TEST_DATA_SUBMODULE)

class RepoManifest(object):
    """Class representing a test repo manifest file"""
    def __init__(self, filename=None):
        self._doc = minidom.Document()
        if filename:
            self._doc = minidom.parse(filename)
            if self._doc.firstChild.nodeName != 'gbp-test-manifest':
                raise Exception('%s is not a test repo manifest' % filename)
        else:
            self._doc.appendChild(self._doc.createElement("gbp-test-manifest"))

    def add_project(self, name, branches):
        """Add new project to the manifest"""
        prj_e = self._doc.createElement('project')
        prj_e.setAttribute('name', name)
        for branch, revision in branches.iteritems():
            br_e = self._doc.createElement('branch')
            br_e.setAttribute('name', branch)
            br_e.setAttribute('revision', revision)
            prj_e.appendChild(br_e)
        self._doc.firstChild.appendChild(prj_e)

    def projects_iter(self):
        """Return an iterator over projects"""
        for prj_e in self._doc.getElementsByTagName('project'):
            branches = {}
            for br_e in prj_e.getElementsByTagName('branch'):
                rev = br_e.getAttribute('revision')
                branches[br_e.getAttribute('name')] = rev
            yield prj_e.getAttribute('name'), branches


    def write(self, filename):
        """Write to file"""
        with open(filename, 'w') as fileobj:
            fileobj.write(self._doc.toprettyxml())

def setup():
    """Test Module setup"""
    ComponentTestGitRepository.check_testdata(RPM_TEST_DATA_SUBMODULE)

# vim:et:ts=4:sw=4:et:sts=4:ai:set list listchars=tab\:»·,trail\:·:
