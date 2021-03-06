# Copyright (C) 2014-2018 Greenbone Networks GmbH
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301 USA.

""" OSP class for handling errors.
"""

from ospd.xml import simple_response_str, get_result_xml

class OSPDError(Exception):

    """ This is an exception that will result in an error message to the
    client """

    def __init__(self, message, command='osp', status=400):
        super().__init__()
        self.message = message
        self.command = command
        self.status = status

    def as_xml(self):
        """ Return the error in xml format. """
        return simple_response_str(self.command, self.status, self.message)
