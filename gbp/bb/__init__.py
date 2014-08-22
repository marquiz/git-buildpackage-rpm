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
import subprocess
import sys

import gbp.log

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
    except bb.BBHandledException:
        raise GbpError("Failed to initialize tinfoil")
    tinfoil.prepare(config_only=config_only)
    return tinfoil


def pkg_version(data):
    """Get package version as a dict"""
    return {'upstreamversion': data.getVar('PV', True),
            'release': data.getVar('PR', True),
            'version': data.getVar('PV', True) + '-' + data.getVar('PR', True)}


# Initialize module
bb = import_bb()
