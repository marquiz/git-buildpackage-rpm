# vim: set fileencoding=utf-8 :
#
# (C) 2006,2007,2008,2011 Guido Guenther <agx@sigxcpu.org>
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
"""Accessing Git from python"""

import calendar
import datetime
import rfc822

from gbp.git.modifier import GitModifier
from gbp.git.commit import GitCommit
from gbp.git.errors import GitError
from gbp.git.repository import GitRepository, GitRepositoryError
from gbp.git.fastimport import FastImport
from gbp.git.args import GitArgs
from gbp.git.vfs import GitVfs


class FixedOffset(datetime.tzinfo):
    """Fixed offset in seconds east from UTC."""

    ZERO = datetime.timedelta(0)

    def __init__(self, offset):
        datetime.tzinfo.__init__(self)
        self._offset = datetime.timedelta(seconds=offset)

    def utcoffset(self, dtime):
        return self._offset + self.dst(dtime)

    def dst(self, dtime):
        assert dtime.tzinfo is self
        return self.ZERO


def rfc822_date_to_git(rfc822_date):
    """Parse a date in RFC822 format, and convert to a 'seconds tz' C{str}ing.

    >>> rfc822_date_to_git('Thu, 1 Jan 1970 00:00:01 +0000')
    '1 +0000'
    >>> rfc822_date_to_git('Thu, 20 Mar 2008 01:12:57 -0700')
    '1206000777 -0700'
    >>> rfc822_date_to_git('Sat, 5 Apr 2008 17:01:32 +0200')
    '1207407692 +0200'
    """
    parsed = rfc822.parsedate_tz(rfc822_date)
    date = datetime.datetime(*parsed[:6], tzinfo=FixedOffset(parsed[-1]))
    seconds = calendar.timegm(date.utctimetuple())
    tzone = date.strftime("%z")
    return '%d %s' % (seconds, tzone)

# vim:et:ts=4:sw=4:et:sts=4:ai:set list listchars=tab\:»·,trail\:·:
