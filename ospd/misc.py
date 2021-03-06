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

""" Miscellaneous classes and functions related to OSPD.
"""


# Needed to say that when we import ospd, we mean the package and not the
# module in that directory.
from __future__ import absolute_import
from __future__ import print_function

import argparse
import binascii
import collections
import logging
import logging.handlers
import os
import re
import socket
import struct
import sys
import time
import ssl
import uuid
import multiprocessing
import itertools
from enum import Enum

LOGGER = logging.getLogger(__name__)

# Default file locations as used by a OpenVAS default installation
KEY_FILE = "/usr/var/lib/gvm/private/CA/serverkey.pem"
CERT_FILE = "/usr/var/lib/gvm/CA/servercert.pem"
CA_FILE = "/usr/var/lib/gvm/CA/cacert.pem"

PORT = 1234
ADDRESS = "0.0.0.0"

class ScanStatus(Enum):
    """Scan status. """
    INIT = 0
    RUNNING = 1
    STOPPED = 2
    FINISHED = 3



class ScanCollection(object):

    """ Scans collection, managing scans and results read and write, exposing
    only needed information.

    Each scan has meta-information such as scan ID, current progress (from 0 to
    100), start time, end time, scan target and options and a list of results.

    There are 4 types of results: Alarms, Logs, Errors and Host Details.

    Todo:
    - Better checking for Scan ID existence and handling otherwise.
    - More data validation.
    - Mutex access per table/scan_info.

    """

    def __init__(self):
        """ Initialize the Scan Collection. """

        self.data_manager = None
        self.scans_table = dict()

    def add_result(self, scan_id, result_type, host='', name='', value='',
                   port='', test_id='', severity='', qod=''):
        """ Add a result to a scan in the table. """

        assert scan_id
        assert len(name) or len(value)
        result = dict()
        result['type'] = result_type
        result['name'] = name
        result['severity'] = severity
        result['test_id'] = test_id
        result['value'] = value
        result['host'] = host
        result['port'] = port
        result['qod'] = qod
        results = self.scans_table[scan_id]['results']
        results.append(result)
        # Set scan_info's results to propagate results to parent process.
        self.scans_table[scan_id]['results'] = results

    def set_progress(self, scan_id, progress):
        """ Sets scan_id scan's progress. """

        if progress > 0 and progress <= 100:
            self.scans_table[scan_id]['progress'] = progress
        if progress == 100:
            self.scans_table[scan_id]['end_time'] = int(time.time())

    def set_target_progress(self, scan_id, target, host, progress):
        """ Sets scan_id scan's progress. """
        if progress > 0 and progress <= 100:
            targets =  self.scans_table[scan_id]['target_progress']
            targets[target][host] = progress
            # Set scan_info's target_progress to propagate progresses
            # to parent process.
            self.scans_table[scan_id]['target_progress'] = targets

    def set_host_finished(self, scan_id, target, host):
        """ Add the host in a list of finished hosts """
        finished_hosts = self.scans_table[scan_id]['finished_hosts']
        finished_hosts[target].extend(host)
        self.scans_table[scan_id]['finished_hosts'] = finished_hosts

    def get_hosts_unfinished(self, scan_id):
        """ Get a list of finished hosts."""

        unfinished_hosts = list()
        for target in self.scans_table[scan_id]['finished_hosts']:
            unfinished_hosts.extend(target_str_to_list(target))
        for target in self.scans_table[scan_id]['finished_hosts']:
           for host in self.scans_table[scan_id]['finished_hosts'][target]:
               unfinished_hosts.remove(host)

        return unfinished_hosts

    def results_iterator(self, scan_id, pop_res):
        """ Returns an iterator over scan_id scan's results. If pop_res is True,
        it removed the fetched results from the list.
        """
        if pop_res:
            result_aux = self.scans_table[scan_id]['results']
            self.scans_table[scan_id]['results'] = list()
            return iter(result_aux)

        return iter(self.scans_table[scan_id]['results'])

    def ids_iterator(self):
        """ Returns an iterator over the collection's scan IDS. """

        return iter(self.scans_table.keys())

    def remove_single_result(self, scan_id, result):
        """Removes a single result from the result list in scan_table"""
        del self.scans_table[scan_id]['results'][result]

    def del_results_for_stopped_hosts(self, scan_id):
        """ Remove results from the result table for those host
        """
        unfinished_hosts = self.get_hosts_unfinished(scan_id)
        for result in self.results_iterator(scan_id, False):
            if result['host'] in unfinished_hosts:
                self.remove_single_result(scan_id, result)

    def resume_scan(self, scan_id, options):
        """ Reset the scan status in the scan_table to INIT.
        Also, overwrite the options, because a resume task cmd
        can add some new option. E.g. exclude hosts list.
        Parameters:
            scan_id (uuid): Scan ID to identify the scan process to be resumed.
            options (dict): Options for the scan to be resumed. This options
                            are not added to the already existent ones.
                            The old ones are removed

        Return:
            Scan ID which identifies the current scan.
        """
        self.scans_table[scan_id]['status'] = ScanStatus.INIT
        if options:
            self.scans_table[scan_id]['options'] = options

        self.del_results_for_stopped_hosts(scan_id)

        return scan_id

    def create_scan(self, scan_id='', targets='', options=None, vts=''):
        """ Creates a new scan with provided scan information. """

        if self.data_manager is None:
            self.data_manager = multiprocessing.Manager()

        # Check if it is possible to resume task. To avoid to resume, the
        # scan must be deleted from the scans_table.
        if scan_id and self.id_exists(scan_id) and (
                self.get_status(scan_id) == ScanStatus.STOPPED):
            return self.resume_scan(scan_id, options)

        if not options:
            options = dict()
        scan_info = self.data_manager.dict()
        scan_info['results'] = list()
        scan_info['finished_hosts'] = dict(
            [[target, []] for target, _, _ in targets])
        scan_info['progress'] = 0
        scan_info['target_progress'] = dict(
            [[target, {}] for target, _, _ in targets])
        scan_info['targets'] = targets
        scan_info['vts'] = vts
        scan_info['options'] = options
        scan_info['start_time'] = int(time.time())
        scan_info['end_time'] = "0"
        scan_info['status'] = ScanStatus.INIT
        if scan_id is None or scan_id == '':
            scan_id = str(uuid.uuid4())
        scan_info['scan_id'] = scan_id
        self.scans_table[scan_id] = scan_info
        return scan_id

    def set_status(self, scan_id, status):
        """ Sets scan_id scan's status. """
        self.scans_table[scan_id]['status'] = status

    def get_status(self, scan_id):
        """ Get scan_id scans's status."""

        return self.scans_table[scan_id]['status']

    def get_options(self, scan_id):
        """ Get scan_id scan's options list. """

        return self.scans_table[scan_id]['options']

    def set_option(self, scan_id, name, value):
        """ Set a scan_id scan's name option to value. """

        self.scans_table[scan_id]['options'][name] = value

    def get_progress(self, scan_id):
        """ Get a scan's current progress value. """

        return self.scans_table[scan_id]['progress']

    def get_target_progress(self, scan_id, target):
        """ Get a target's current progress value.
        The value is calculated with the progress of each single host
        in the target."""

        total_hosts = len(target_str_to_list(target))
        host_progresses = self.scans_table[scan_id]['target_progress'].get(target)
        try:
            t_prog = sum(host_progresses.values()) / total_hosts
        except ZeroDivisionError:
            LOGGER.error("Zero division error in ", get_target_progress.__name__)
            raise
        return t_prog

    def get_start_time(self, scan_id):
        """ Get a scan's start time. """

        return self.scans_table[scan_id]['start_time']

    def get_end_time(self, scan_id):
        """ Get a scan's end time. """

        return self.scans_table[scan_id]['end_time']

    def get_target_list(self, scan_id):
        """ Get a scan's target list. """

        target_list = []
        for target, _, _ in self.scans_table[scan_id]['targets']:
            target_list.append(target)
        return target_list

    def get_ports(self, scan_id, target):
        """ Get a scan's ports list. If a target is specified
        it will return the corresponding port for it. If not,
        it returns the port item of the first nested list in
        the target's list.
        """
        if target:
            for item in self.scans_table[scan_id]['targets']:
                if target == item[0]:
                    return item[1]

        return self.scans_table[scan_id]['targets'][0][1]

    def get_credentials(self, scan_id, target):
        """ Get a scan's credential list. It return dictionary with
        the corresponding credential for a given target.
        """
        if target:
            for item in self.scans_table[scan_id]['targets']:
                if target == item[0]:
                    return item[2]

    def get_vts(self, scan_id):
        """ Get a scan's vts list. """

        return self.scans_table[scan_id]['vts']

    def id_exists(self, scan_id):
        """ Check whether a scan exists in the table. """

        return self.scans_table.get(scan_id) is not None

    def delete_scan(self, scan_id):
        """ Delete a scan if fully finished. """

        if self.get_status(scan_id) == ScanStatus.RUNNING:
            return False
        self.scans_table.pop(scan_id)
        if len(self.scans_table) == 0:
            del self.data_manager
            self.data_manager = None
        return True


class ResultType(object):

    """ Various scan results types values. """

    ALARM = 0
    LOG = 1
    ERROR = 2
    HOST_DETAIL = 3

    @classmethod
    def get_str(cls, result_type):
        """ Return string name of a result type. """
        if result_type == cls.ALARM:
            return "Alarm"
        elif result_type == cls.LOG:
            return "Log Message"
        elif result_type == cls.ERROR:
            return "Error Message"
        elif result_type == cls.HOST_DETAIL:
            return "Host Detail"
        else:
            assert False, "Erroneous result type {0}.".format(result_type)

    @classmethod
    def get_type(cls, result_name):
        """ Return string name of a result type. """
        if result_name == "Alarm":
            return cls.ALARM
        elif result_name == "Log Message":
            return cls.LOG
        elif result_name == "Error Message":
            return cls.ERROR
        elif result_name == "Host Detail":
            return cls.HOST_DETAIL
        else:
            assert False, "Erroneous result name {0}.".format(result_name)


__inet_pton = None


def inet_pton(address_family, ip_string):
    """ A platform independent version of inet_pton """
    global __inet_pton
    if __inet_pton is None:
        if hasattr(socket, 'inet_pton'):
            __inet_pton = socket.inet_pton
        else:
            from ospd import win_socket
            __inet_pton = win_socket.inet_pton

    return __inet_pton(address_family, ip_string)


__inet_ntop = None


def inet_ntop(address_family, packed_ip):
    """ A platform independent version of inet_ntop """
    global __inet_ntop
    if __inet_ntop is None:
        if hasattr(socket, 'inet_ntop'):
            __inet_ntop = socket.inet_ntop
        else:
            from ospd import win_socket
            __inet_ntop = win_socket.inet_ntop

    return __inet_ntop(address_family, packed_ip)


def target_to_ipv4(target):
    """ Attempt to return a single IPv4 host list from a target string. """

    try:
        inet_pton(socket.AF_INET, target)
        return [target]
    except socket.error:
        return None


def target_to_ipv6(target):
    """ Attempt to return a single IPv6 host list from a target string. """

    try:
        inet_pton(socket.AF_INET6, target)
        return [target]
    except socket.error:
        return None


def ipv4_range_to_list(start_packed, end_packed):
    """ Return a list of IPv4 entries from start_packed to end_packed. """

    new_list = list()
    start = struct.unpack('!L', start_packed)[0]
    end = struct.unpack('!L', end_packed)[0]
    for value in range(start, end + 1):
        new_ip = socket.inet_ntoa(struct.pack('!L', value))
        new_list.append(new_ip)
    return new_list


def target_to_ipv4_short(target):
    """ Attempt to return a IPv4 short range list from a target string. """

    splitted = target.split('-')
    if len(splitted) != 2:
        return None
    try:
        start_packed = inet_pton(socket.AF_INET, splitted[0])
        end_value = int(splitted[1])
    except (socket.error, ValueError):
        return None
    start_value = int(binascii.hexlify(bytes(start_packed[3])), 16)
    if end_value < 0 or end_value > 255 or end_value < start_value:
        return None
    end_packed = start_packed[0:3] + struct.pack('B', end_value)
    return ipv4_range_to_list(start_packed, end_packed)


def target_to_ipv4_cidr(target):
    """ Attempt to return a IPv4 CIDR list from a target string. """

    splitted = target.split('/')
    if len(splitted) != 2:
        return None
    try:
        start_packed = inet_pton(socket.AF_INET, splitted[0])
        block = int(splitted[1])
    except (socket.error, ValueError):
        return None
    if block <= 0 or block > 30:
        return None
    start_value = int(binascii.hexlify(start_packed), 16) >> (32 - block)
    start_value = (start_value << (32 - block)) + 1
    end_value = (start_value | (0xffffffff >> block)) - 1
    start_packed = struct.pack('!I', start_value)
    end_packed = struct.pack('!I', end_value)
    return ipv4_range_to_list(start_packed, end_packed)


def target_to_ipv6_cidr(target):
    """ Attempt to return a IPv6 CIDR list from a target string. """

    splitted = target.split('/')
    if len(splitted) != 2:
        return None
    try:
        start_packed = inet_pton(socket.AF_INET6, splitted[0])
        block = int(splitted[1])
    except (socket.error, ValueError):
        return None
    if block <= 0 or block > 126:
        return None
    start_value = int(binascii.hexlify(start_packed), 16) >> (128 - block)
    start_value = (start_value << (128 - block)) + 1
    end_value = (start_value | (int('ff' * 16, 16) >> block)) - 1
    high = start_value >> 64
    low = start_value & ((1 << 64) - 1)
    start_packed = struct.pack('!QQ', high, low)
    high = end_value >> 64
    low = end_value & ((1 << 64) - 1)
    end_packed = struct.pack('!QQ', high, low)
    return ipv6_range_to_list(start_packed, end_packed)


def target_to_ipv4_long(target):
    """ Attempt to return a IPv4 long-range list from a target string. """

    splitted = target.split('-')
    if len(splitted) != 2:
        return None
    try:
        start_packed = inet_pton(socket.AF_INET, splitted[0])
        end_packed = inet_pton(socket.AF_INET, splitted[1])
    except socket.error:
        return None
    if end_packed < start_packed:
        return None
    return ipv4_range_to_list(start_packed, end_packed)


def ipv6_range_to_list(start_packed, end_packed):
    """ Return a list of IPv6 entries from start_packed to end_packed. """

    new_list = list()
    start = int(binascii.hexlify(start_packed), 16)
    end = int(binascii.hexlify(end_packed), 16)
    for value in range(start, end + 1):
        high = value >> 64
        low = value & ((1 << 64) - 1)
        new_ip = inet_ntop(socket.AF_INET6,
                           struct.pack('!2Q', high, low))
        new_list.append(new_ip)
    return new_list


def target_to_ipv6_short(target):
    """ Attempt to return a IPv6 short-range list from a target string. """

    splitted = target.split('-')
    if len(splitted) != 2:
        return None
    try:
        start_packed = inet_pton(socket.AF_INET6, splitted[0])
        end_value = int(splitted[1], 16)
    except (socket.error, ValueError):
        return None
    start_value = int(binascii.hexlify(start_packed[14:]), 16)
    if end_value < 0 or end_value > 0xffff or end_value < start_value:
        return None
    end_packed = start_packed[:14] + struct.pack('!H', end_value)
    return ipv6_range_to_list(start_packed, end_packed)


def target_to_ipv6_long(target):
    """ Attempt to return a IPv6 long-range list from a target string. """

    splitted = target.split('-')
    if len(splitted) != 2:
        return None
    try:
        start_packed = inet_pton(socket.AF_INET6, splitted[0])
        end_packed = inet_pton(socket.AF_INET6, splitted[1])
    except socket.error:
        return None
    if end_packed < start_packed:
        return None
    return ipv6_range_to_list(start_packed, end_packed)


def target_to_hostname(target):
    """ Attempt to return a single hostname list from a target string. """

    if len(target) == 0 or len(target) > 255:
        return None
    if not re.match(r'^[\w.-]+$', target):
        return None
    return [target]


def target_to_list(target):
    """ Attempt to return a list of single hosts from a target string. """

    # Is it an IPv4 address ?
    new_list = target_to_ipv4(target)
    # Is it an IPv6 address ?
    if not new_list:
        new_list = target_to_ipv6(target)
    # Is it an IPv4 CIDR ?
    if not new_list:
        new_list = target_to_ipv4_cidr(target)
    # Is it an IPv6 CIDR ?
    if not new_list:
        new_list = target_to_ipv6_cidr(target)
    # Is it an IPv4 short-range ?
    if not new_list:
        new_list = target_to_ipv4_short(target)
    # Is it an IPv4 long-range ?
    if not new_list:
        new_list = target_to_ipv4_long(target)
    # Is it an IPv6 short-range ?
    if not new_list:
        new_list = target_to_ipv6_short(target)
    # Is it an IPv6 long-range ?
    if not new_list:
        new_list = target_to_ipv6_long(target)
    # Is it a hostname ?
    if not new_list:
        new_list = target_to_hostname(target)
    return new_list


def target_str_to_list(target_str):
    """ Parses a targets string into a list of individual targets. """
    new_list = list()
    for target in target_str.split(','):
        target = target.strip()
        target_list = target_to_list(target)
        if target_list:
            new_list.extend(target_list)
        else:
            LOGGER.info("{0}: Invalid target value".format(target))
            return None
    return list(collections.OrderedDict.fromkeys(new_list))


def resolve_hostname(hostname):
    """ Returns IP of a hostname. """

    assert hostname
    try:
        return socket.gethostbyname(hostname)
    except socket.gaierror:
        return None


def port_range_expand(portrange):
    """
    Receive a port range and expands it in individual ports.

    @input Port range.
    e.g. "4-8"

    @return List of integers.
    e.g. [4, 5, 6, 7, 8]
    """
    if not portrange or '-' not in portrange:
        LOGGER.info("Invalid port range format")
        return None
    port_list = list()
    for single_port in range(int(portrange[:portrange.index('-')]),
                             int(portrange[portrange.index('-') + 1:]) + 1):
        port_list.append(single_port)
    return port_list


def port_str_arrange(ports):
    """ Gives a str in the format (always tcp listed first).
    T:<tcp ports/portrange comma separated>U:<udp ports comma separated>
    """
    b_tcp = ports.find("T")
    b_udp = ports.find("U")
    if (b_udp != -1 and b_tcp != -1) and b_udp < b_tcp:
        return ports[b_tcp:] + ports[b_udp:b_tcp]

    return ports


def ports_str_check_failed(port_str):
    """
    Check if the port string is well formed.
    Return True if fail, False other case.
    """

    pattern = r'[^TU:0-9, \-]'
    if (
        re.search(pattern, port_str)
        or port_str.count('T') > 1
        or port_str.count('U') > 1
        or port_str.count(':') < (port_str.count('T') + port_str.count('U'))
    ):
        return True
    return False


def ports_as_list(port_str):
    """
    Parses a ports string into two list of individual tcp and udp ports.

    @input string containing a port list
    e.g. T:1,2,3,5-8 U:22,80,600-1024

    @return two list of sorted integers, for tcp and udp ports respectively.
    """
    if not port_str:
        LOGGER.info("Invalid port value")
        return [None, None]

    if ports_str_check_failed(port_str):
        LOGGER.info("{0}: Port list malformed.")
        return [None, None]

    tcp_list = list()
    udp_list = list()
    ports = port_str.replace(' ', '')
    b_tcp = ports.find("T")
    b_udp = ports.find("U")

    if ports[b_tcp - 1] == ',':
        ports = ports[:b_tcp - 1] + ports[b_tcp:]
    if ports[b_udp - 1] == ',':
        ports = ports[:b_udp - 1] + ports[b_udp:]
    ports = port_str_arrange(ports)

    tports = ''
    uports = ''
    # TCP ports listed first, then UDP ports
    if b_udp != -1 and b_tcp != -1:
        tports = ports[ports.index('T:') + 2:ports.index('U:')]
        uports = ports[ports.index('U:') + 2:]
    # Only UDP ports
    elif b_tcp == -1 and b_udp != -1:
        uports = ports[ports.index('U:') + 2:]
    # Only TCP ports
    elif b_udp == -1 and b_tcp != -1:
        tports = ports[ports.index('T:') + 2:]
    else:
        tports = ports

    if tports:
        for port in tports.split(','):
            if '-' in port:
                tcp_list.extend(port_range_expand(port))
            else:
                tcp_list.append(int(port))
        tcp_list.sort()
    if uports:
        for port in uports.split(','):
            if '-' in port:
                udp_list.extend(port_range_expand(port))
            else:
                udp_list.append(int(port))
        udp_list.sort()

    return (tcp_list, udp_list)


def get_tcp_port_list(port_str):
    """ Return a list with tcp ports from a given port list in string format """
    return ports_as_list(port_str)[0]


def get_udp_port_list(port_str):
    """ Return a list with udp ports from a given port list in string format """
    return ports_as_list(port_str)[1]


def port_list_compress(port_list):
    """ Compress a port list and return a string. """

    if not port_list or len(port_list) == 0:
        LOGGER.info("Invalid or empty port list.")
        return ''

    port_list = sorted(set(port_list))
    compressed_list = []
    for key, group in itertools.groupby(enumerate(port_list),
                                        lambda t: t[1] - t[0]):
        group = list(group)
        if group[0][1] == group[-1][1]:
            compressed_list.append(str(group[0][1]))
        else:
            compressed_list.append(str(group[0][1]) + '-' + str(group[-1][1]))

    return ','.join(compressed_list)


def valid_uuid(value):
    """ Check if value is a valid UUID. """

    try:
        uuid.UUID(value, version=4)
        return True
    except (TypeError, ValueError, AttributeError):
        return False


def create_args_parser(description):
    """ Create a command-line arguments parser for OSPD. """

    parser = argparse.ArgumentParser(description=description)

    def network_port(string):
        """ Check if provided string is a valid network port. """

        value = int(string)
        if not 0 < value <= 65535:
            raise argparse.ArgumentTypeError(
                'port must be in ]0,65535] interval')
        return value

    def cacert_file(cacert):
        """ Check if provided file is a valid CA Certificate """
        try:
            context = ssl.create_default_context(cafile=cacert)
        except AttributeError:
            # Python version < 2.7.9
            return cacert
        except IOError:
            raise argparse.ArgumentTypeError('CA Certificate not found')
        try:
            not_after = context.get_ca_certs()[0]['notAfter']
            not_after = ssl.cert_time_to_seconds(not_after)
            not_before = context.get_ca_certs()[0]['notBefore']
            not_before = ssl.cert_time_to_seconds(not_before)
        except (KeyError, IndexError):
            raise argparse.ArgumentTypeError('CA Certificate is erroneous')
        if not_after < int(time.time()):
            raise argparse.ArgumentTypeError('CA Certificate expired')
        if not_before > int(time.time()):
            raise argparse.ArgumentTypeError('CA Certificate not active yet')
        return cacert

    def log_level(string):
        """ Check if provided string is a valid log level. """

        value = getattr(logging, string.upper(), None)
        if not isinstance(value, int):
            raise argparse.ArgumentTypeError(
                'log level must be one of {debug,info,warning,error,critical}')
        return value

    def filename(string):
        """ Check if provided string is a valid file path. """

        if not os.path.isfile(string):
            raise argparse.ArgumentTypeError(
                '%s is not a valid file path' % string)
        return string

    parser.add_argument('-p', '--port', default=PORT, type=network_port,
                        help='TCP Port to listen on. Default: {0}'.format(PORT))
    parser.add_argument('-b', '--bind-address', default=ADDRESS,
                        help='Address to listen on. Default: {0}'
                        .format(ADDRESS))
    parser.add_argument('-u', '--unix-socket',
                        help='Unix file socket to listen on.')
    parser.add_argument('-k', '--key-file', type=filename,
                        help='Server key file. Default: {0}'.format(KEY_FILE))
    parser.add_argument('-c', '--cert-file', type=filename,
                        help='Server cert file. Default: {0}'.format(CERT_FILE))
    parser.add_argument('--ca-file', type=cacert_file,
                        help='CA cert file. Default: {0}'.format(CA_FILE))
    parser.add_argument('-L', '--log-level', default='warning', type=log_level,
                        help='Wished level of logging. Default: WARNING')
    parser.add_argument('--foreground', action='store_true',
                        help='Run in foreground and logs all messages to console.')
    parser.add_argument('-l', '--log-file', type=filename,
                        help='Path to the logging file.')
    parser.add_argument('--version', action='store_true',
                        help='Print version then exit.')
    return parser


def go_to_background():
    """ Daemonize the running process. """
    try:
        if os.fork():
            sys.exit()
    except OSError as errmsg:
        LOGGER.error('Fork failed: {0}'.format(errmsg))
        sys.exit('Fork failed')


def get_common_args(parser, args=None):
    """ Return list of OSPD common command-line arguments from parser, after
    validating provided values or setting default ones.

    """

    options = parser.parse_args(args)
    # TCP Port to listen on.
    port = options.port

    # Network address to bind listener to
    address = options.bind_address

    # Unix file socket to listen on
    unix_socket = options.unix_socket

    # Debug level.
    log_level = options.log_level

    # Server key path.
    keyfile = options.key_file or KEY_FILE

    # Server cert path.
    certfile = options.cert_file or CERT_FILE

    # CA cert path.
    cafile = options.ca_file or CA_FILE

    common_args = dict()
    common_args['port'] = port
    common_args['address'] = address
    common_args['unix_socket'] = unix_socket
    common_args['keyfile'] = keyfile
    common_args['certfile'] = certfile
    common_args['cafile'] = cafile
    common_args['log_level'] = log_level
    common_args['foreground'] = options.foreground
    common_args['log_file'] = options.log_file
    common_args['version'] = options.version

    return common_args


def print_version(wrapper):
    """ Prints the server version and license information."""

    scanner_name = wrapper.get_scanner_name()
    server_version = wrapper.get_server_version()
    print("OSP Server for {0} version {1}".format(scanner_name, server_version))
    protocol_version = wrapper.get_protocol_version()
    print("OSP Version: {0}".format(protocol_version))
    daemon_name = wrapper.get_daemon_name()
    daemon_version = wrapper.get_daemon_version()
    print("Using: {0} {1}".format(daemon_name, daemon_version))
    print("Copyright (C) 2014, 2015 Greenbone Networks GmbH\n"
          "License GPLv2+: GNU GPL version 2 or later\n"
          "This is free software: you are free to change"
          " and redistribute it.\n"
          "There is NO WARRANTY, to the extent permitted by law.")


def main(name, klass):
    """ OSPD Main function. """

    # Common args parser.
    parser = create_args_parser(name)

    # Common args
    cargs = get_common_args(parser)
    logging.getLogger().setLevel(cargs['log_level'])
    wrapper = klass(certfile=cargs['certfile'], keyfile=cargs['keyfile'],
                    cafile=cargs['cafile'])

    if cargs['version']:
        print_version(wrapper)
        sys.exit()

    if cargs['foreground']:
        console = logging.StreamHandler()
        console.setFormatter(
            logging.Formatter(
                '%(asctime)s %(name)s: %(levelname)s: %(message)s'))
        logging.getLogger().addHandler(console)
    elif cargs['log_file']:
        logfile = logging.handlers.WatchedFileHandler(cargs['log_file'])
        logfile.setFormatter(
            logging.Formatter(
                '%(asctime)s %(name)s: %(levelname)s: %(message)s'))
        logging.getLogger().addHandler(logfile)
        go_to_background()
    else:
        syslog = logging.handlers.SysLogHandler('/dev/log')
        syslog.setFormatter(
            logging.Formatter('%(name)s: %(levelname)s: %(message)s'))
        logging.getLogger().addHandler(syslog)
        # Duplicate syslog's file descriptor to stout/stderr.
        syslog_fd = syslog.socket.fileno()
        os.dup2(syslog_fd, 1)
        os.dup2(syslog_fd, 2)
        go_to_background()

    if not wrapper.check():
        return 1
    return wrapper.run(cargs['address'], cargs['port'], cargs['unix_socket'])
