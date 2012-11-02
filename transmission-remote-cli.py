#!/usr/bin/env python
########################################################################
# This is transmission-remote-cli, whereas 'cli' stands for 'Curses    #
# Luminous Interface', a client for the daemon of the BitTorrent       #
# client Transmission.                                                 #
#                                                                      #
# This program is free software: you can redistribute it and/or modify #
# it under the terms of the GNU General Public License as published by #
# the Free Software Foundation, either version 3 of the License, or    #
# (at your option) any later version.                                  #
#                                                                      #
# This program is distributed in the hope that it will be useful,      #
# but WITHOUT ANY WARRANTY; without even the implied warranty of       #
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the        #
# GNU General Public License for more details:                         #
# http://www.gnu.org/licenses/gpl-3.0.txt                              #
########################################################################

VERSION='0.8.5'

TRNSM_VERSION_MIN = '1.80'
TRNSM_VERSION_MAX = '2.30'
RPC_VERSION_MIN = 7
RPC_VERSION_MAX = 13

# error codes
CONNECTION_ERROR = 1
JSON_ERROR       = 2
CONFIGFILE_ERROR = 3


# use simplejson if available because it seems to be faster
try:
    import simplejson as json
except ImportError:
    try:
        # Python 2.6 comes with a json module ...
        import json
        # ...but there is also an old json module that doesn't support .loads/.dumps.
        json.dumps ; json.dumps
    except (ImportError,AttributeError):
        quit("Please install simplejson or Python 2.6 or higher.")

import time
import re
import base64
import httplib
import urllib2
import socket
socket.setdefaulttimeout(None)
import ConfigParser
from optparse import OptionParser, SUPPRESS_HELP
import sys
import os
import signal
import locale
locale.setlocale(locale.LC_ALL, '')
import curses
from textwrap import wrap
from subprocess import call


# optional features provided by non-standard modules
features = {'dns':False, 'geoip':False, 'ipy':False}
try:   import adns; features['dns'] = True     # resolve IP to host name
except ImportError: features['dns'] = False

try:   import GeoIP; features['geoip'] = True  # show country peer seems to be in
except ImportError:  features['geoip'] = False

try:   import IPy;  features['ipy'] = True  # extract ipv4 from ipv6 addresses
except ImportError: features['ipy'] = False


if features['ipy']:
    IPV6_RANGE_6TO4 = IPy.IP('2002::/16')
    IPV6_RANGE_TEREDO = IPy.IP('2001::/32')
    IPV4_ONES = 0xffffffff

if features['geoip']:
    def country_code_by_addr_vany(geo_ip, geo_ip6, addr):
        if '.' in addr:
            return geo_ip.country_code_by_addr(addr)
        if not ':' in addr:
            return None
        if features['ipy']:
            ip = IPy.IP(addr)
            if ip in IPV6_RANGE_6TO4:
              addr = str(IPy.IP(ip.int() >> 80 & IPV4_ONES))
              return geo_ip.country_code_by_addr(addr)
            elif ip in IPV6_RANGE_TEREDO:
              addr = str(IPy.IP(ip.int() & IPV4_ONES ^ IPV4_ONES))
              return geo_ip.country_code_by_addr(addr)
        if hasattr(geo_ip6, 'country_code_by_addr_v6'):
            return geo_ip6.country_code_by_addr_v6(addr)


# define config defaults
config = ConfigParser.SafeConfigParser()
config.add_section('Connection')
config.set('Connection', 'password', '')
config.set('Connection', 'username', '')
config.set('Connection', 'port', '9091')
config.set('Connection', 'host', 'localhost')
config.add_section('Sorting')
config.set('Sorting', 'order',   'name')
config.set('Sorting', 'reverse', 'False')
config.add_section('Filtering')
config.set('Filtering', 'filter', '')
config.set('Filtering', 'invert', 'False')


authhandler = None
session_id = 0

# Handle communication with Transmission server.
class TransmissionRequest:
    def __init__(self, host, port, method=None, tag=None, arguments=None):
        self.url = 'http://%s:%d/transmission/rpc' % (host, port)
        self.open_request  = None
        self.last_update   = 0
        if method and tag:
            self.set_request_data(method, tag, arguments)

    def set_request_data(self, method, tag, arguments=None):
        request_data = {'method':method, 'tag':tag}
        if arguments: request_data['arguments'] = arguments
        self.http_request = urllib2.Request(url=self.url, data=json.dumps(request_data))

    def send_request(self):
        """Ask for information from server OR submit command."""

        global session_id
        try:
            if session_id:
                self.http_request.add_header('X-Transmission-Session-Id', session_id)
            self.open_request = urllib2.urlopen(self.http_request)
        except AttributeError:
            # request data (http_request) isn't specified yet -- data will be available on next call
            pass

        # do we still need this?
        # except httplib.BadStatusLine, msg:
        #     # server sends something httplib doesn't understand.
        #     # (happens sometimes with high cpu load[?])
        #     pass

        # authentication
        except urllib2.HTTPError, e:
            try:
                msg = html2text(str(e.read()))
            except:
                msg = str(e)

            # extract session id and send request again
            m = re.search('X-Transmission-Session-Id:\s*(\w+)', msg)
            try:
                session_id = m.group(1)
                self.send_request()
            except AttributeError:
                quit(str(msg) + "\n", CONNECTION_ERROR)

        except urllib2.URLError, msg:
            try:
                reason = msg.reason[1]
            except IndexError:
                reason = str(msg.reason)
            quit("Cannot connect to %s: %s\n" % (self.http_request.host, reason), CONNECTION_ERROR)

    def get_response(self):
        """Get response to previously sent request."""

        if self.open_request == None:
            return {'result': 'no open request'}
        response = self.open_request.read()
        # work around regression in Python 2.6.5, caused by http://bugs.python.org/issue8797
        if authhandler:
            authhandler.retried = 0
        try:
            data = json.loads(response)
        except ValueError:
            quit("Cannot not parse response: %s\n" % response, JSON_ERROR)
        self.open_request = None
        return data


# End of Class TransmissionRequest


# Higher level of data exchange
class Transmission:
    STATUS_CHECK_WAIT = 1 << 0
    STATUS_CHECK      = 1 << 1
    STATUS_DOWNLOAD   = 1 << 2
    STATUS_SEED       = 1 << 3
    STATUS_STOPPED    = 1 << 4

    TAG_TORRENT_LIST    = 7
    TAG_TORRENT_DETAILS = 77
    TAG_SESSION_STATS   = 21
    TAG_SESSION_GET     = 22

    LIST_FIELDS = [ 'id', 'name', 'downloadDir', 'status', 'trackerStats', 'desiredAvailable',
                    'rateDownload', 'rateUpload', 'eta', 'uploadRatio',
                    'sizeWhenDone', 'haveValid', 'haveUnchecked', 'addedDate',
                    'uploadedEver', 'errorString', 'recheckProgress',
                    'peersConnected', 'uploadLimit', 'downloadLimit',
                    'uploadLimited', 'downloadLimited', 'bandwidthPriority',
                    'peersSendingToUs', 'peersGettingFromUs']

    DETAIL_FIELDS = [ 'files', 'priorities', 'wanted', 'peers', 'trackers',
                      'activityDate', 'dateCreated', 'startDate', 'doneDate',
                      'totalSize', 'leftUntilDone', 'comment', 'isPrivate',
                      'hashString', 'pieceCount', 'pieceSize', 'pieces',
                      'downloadedEver', 'corruptEver', 'peersFrom' ] + LIST_FIELDS

    def __init__(self, host, port, username, password):
        self.host = host
        self.port = port

        if username and password:
            password_mgr = urllib2.HTTPPasswordMgrWithDefaultRealm()
            url = 'http://%s:%d/transmission/rpc' % (host, port)
            password_mgr.add_password(None, url, username, password)
            global authhandler
            authhandler = urllib2.HTTPBasicAuthHandler(password_mgr)
            opener = urllib2.build_opener(authhandler)
            urllib2.install_opener(opener)

        # check rpc version
        request = TransmissionRequest(host, port, 'session-get', self.TAG_SESSION_GET)
        request.send_request()
        response = request.get_response()

        # rpc version too old?
        version_error = "Unsupported Transmission version: " + str(response['arguments']['version']) + \
            " -- RPC protocol version: " + str(response['arguments']['rpc-version']) + "\n"

        min_msg = "Please install Transmission version " + TRNSM_VERSION_MIN + " or higher.\n"
        try:
            if response['arguments']['rpc-version'] < RPC_VERSION_MIN:
                quit(version_error + min_msg)
        except KeyError:
            quit(version_error + min_msg)

        # rpc version too new?
        if response['arguments']['rpc-version'] > RPC_VERSION_MAX:
            quit(version_error + "Please install Transmission version " + TRNSM_VERSION_MAX + " or lower.\n")


        # set up request list
        self.requests = {'torrent-list':
                             TransmissionRequest(host, port, 'torrent-get', self.TAG_TORRENT_LIST, {'fields': self.LIST_FIELDS}),
                         'session-stats':
                             TransmissionRequest(host, port, 'session-stats', self.TAG_SESSION_STATS, 21),
                         'session-get':
                             TransmissionRequest(host, port, 'session-get', self.TAG_SESSION_GET),
                         'torrent-details':
                             TransmissionRequest(host, port)}

        self.torrent_cache = []
        self.status_cache  = dict()
        self.torrent_details_cache = dict()
        self.peer_progress_cache   = dict()
        self.hosts_cache   = dict()
        self.geo_ips_cache = dict()
        if features['dns']:   self.resolver = adns.init()
        if features['geoip']:
            self.geo_ip = GeoIP.new(GeoIP.GEOIP_MEMORY_CACHE)
            try:
                self.geo_ip6 = GeoIP.open_type(GeoIP.GEOIP_COUNTRY_EDITION_V6, GeoIP.GEOIP_MEMORY_CACHE);
            except AttributeError: self.geo_ip6 = None
            except GeoIP.error: self.geo_ip6 = None

        # make sure there are no undefined values
        self.wait_for_torrentlist_update()
        self.requests['torrent-details'] = TransmissionRequest(self.host, self.port)


    def update(self, delay, tag_waiting_for=0):
        """Maintain up-to-date data."""

        tag_waiting_for_occurred = False

        for request in self.requests.values():
            if time.time() - request.last_update >= delay:
                request.last_update = time.time()
                response = request.get_response()

                if response['result'] == 'no open request':
                    request.send_request()

                elif response['result'] == 'success':
                    tag = self.parse_response(response)
                    if tag == tag_waiting_for:
                        tag_waiting_for_occurred = True

        if tag_waiting_for:
            return tag_waiting_for_occurred
        else:
            return None



    def parse_response(self, response):
        # response is a reply to torrent-get
        if response['tag'] == self.TAG_TORRENT_LIST or response['tag'] == self.TAG_TORRENT_DETAILS:
            for t in response['arguments']['torrents']:
                t['uploadRatio'] = round(float(t['uploadRatio']), 2)
                t['percent_done'] = percent(float(t['sizeWhenDone']),
                                            float(t['haveValid'] + t['haveUnchecked']))
                try:
                    t['seeders']  = max(map(lambda x: x['seederCount'],  t['trackerStats']))
                    t['leechers'] = max(map(lambda x: x['leecherCount'], t['trackerStats']))
                except ValueError:
                    t['seeders']  = t['leechers'] = -1

                t['available'] = t['desiredAvailable'] + t['haveValid'] + t['haveUnchecked']

            if response['tag'] == self.TAG_TORRENT_LIST:
                self.torrent_cache = response['arguments']['torrents']

            elif response['tag'] == self.TAG_TORRENT_DETAILS:
                # torrent list may be empty sometimes after deleting
                # torrents.  no idea why and why the server sends us
                # TAG_TORRENT_DETAILS, but just passing seems to help.(?)
                try:
                    torrent_details = response['arguments']['torrents'][0]
                    torrent_details['pieces'] = base64.decodestring(torrent_details['pieces'])
                    self.torrent_details_cache = torrent_details
                    self.upgrade_peerlist()
                except IndexError:
                    pass

        elif response['tag'] == self.TAG_SESSION_STATS:
            self.status_cache.update(response['arguments'])

        elif response['tag'] == self.TAG_SESSION_GET:
            self.status_cache.update(response['arguments'])

        return response['tag']

    def upgrade_peerlist(self):
        for index,peer in enumerate(self.torrent_details_cache['peers']):
            ip = peer['address']
            peerid = ip + self.torrent_details_cache['hashString']

            # make sure peer cache exists
            if not self.peer_progress_cache.has_key(peerid):
                self.peer_progress_cache[peerid] = {'last_progress':peer['progress'], 'last_update':time.time(),
                                                    'download_speed':0, 'time_left':0}

            # estimate how fast a peer is downloading
            if peer['progress'] < 1:
                this_time = time.time()
                time_diff = this_time - self.peer_progress_cache[peerid]['last_update']
                progress_diff = peer['progress'] - self.peer_progress_cache[peerid]['last_progress']
                if self.peer_progress_cache[peerid]['last_progress'] and progress_diff > 0 and time_diff > 5:
                    downloaded = self.torrent_details_cache['totalSize'] * progress_diff
                    avg_speed  = downloaded / time_diff
                    # debug("%s:\n" % peerid +\
                    #           "\tlast_time.....: %-13s   this_time.......: %-13s  diff: %s\n" \
                    #           % (self.peer_progress_cache[peerid]['last_update'], this_time, time_diff) +\
                    #           "\tlast_progress.: %-13s   this_progress...: %-13s  diff: %s\n" \
                    #           % (self.peer_progress_cache[peerid]['last_progress'], peer['progress'], progress_diff) +\
                    #           "\tformula: (%s * %s) / %s = %s/s\n" \
                    #           % (self.torrent_details_cache['totalSize'], progress_diff, time_diff, scale_bytes(avg_speed)))

                    if self.peer_progress_cache[peerid]['download_speed'] > 0:  # make it less jumpy
                        avg_speed = (self.peer_progress_cache[peerid]['download_speed'] + avg_speed) /2

                    download_left = self.torrent_details_cache['totalSize'] - \
                        (self.torrent_details_cache['totalSize']*peer['progress'])
                    time_left  = download_left / avg_speed
                    # debug("  %s  --  will finish %s\n\n" % (timestamp(this_time), timestamp(time_left + this_time)))

                    self.peer_progress_cache[peerid]['last_update']    = this_time  # remember update time
                    self.peer_progress_cache[peerid]['download_speed'] = avg_speed
                    self.peer_progress_cache[peerid]['time_left']      = time_left

                self.peer_progress_cache[peerid]['last_progress'] = peer['progress']  # remember progress
            self.torrent_details_cache['peers'][index].update(self.peer_progress_cache[peerid])

            # resolve and locate peer's ip
            if features['dns'] and not self.hosts_cache.has_key(ip):
                try:
                    self.hosts_cache[ip] = self.resolver.submit_reverse(ip, adns.rr.PTR)
                except adns.Error:
                    pass
            if features['geoip'] and not self.geo_ips_cache.has_key(ip):
                self.geo_ips_cache[ip] = country_code_by_addr_vany(self.geo_ip, self.geo_ip6, ip)
                if self.geo_ips_cache[ip] == None:
                    self.geo_ips_cache[ip] = '?'


    def get_global_stats(self):
        return self.status_cache

    def get_torrent_list(self, sort_orders, reverse=False):
        try:
            for sort_order in sort_orders:
                if isinstance(self.torrent_cache[0][sort_order], (str, unicode)):
                    self.torrent_cache.sort(key=lambda x: x[sort_order].lower(), reverse=reverse)
                else:
                    self.torrent_cache.sort(key=lambda x: x[sort_order], reverse=reverse)
        except IndexError:
            return []
        return self.torrent_cache

    def get_torrent_by_id(self, id):
        i = 0
        while self.torrent_cache[i]['id'] != id:  i += 1
        if self.torrent_cache[i]['id'] == id:
            return self.torrent_cache[i]
        else:
            return None


    def get_torrent_details(self):
        return self.torrent_details_cache
    def set_torrent_details_id(self, id):
        if id < 0:
            self.requests['torrent-details'] = TransmissionRequest(self.host, self.port)
        else:
            self.requests['torrent-details'].set_request_data('torrent-get', self.TAG_TORRENT_DETAILS,
                                                              {'ids':id, 'fields': self.DETAIL_FIELDS})

    def get_hosts(self):
        return self.hosts_cache

    def get_geo_ips(self):
        return self.geo_ips_cache


    def set_option(self, option_name, option_value):
        request = TransmissionRequest(self.host, self.port, 'session-set', 1, {option_name: option_value})
        request.send_request()
        self.wait_for_status_update()


    def set_rate_limit(self, direction, new_limit, torrent_id=-1):
        data = dict()
        if new_limit < 0:
            return
        elif new_limit == 0:
            new_limit     = None
            limit_enabled = False
        else:
            limit_enabled = True

        if torrent_id < 0:
            type = 'session-set'
            data['speed-limit-'+direction]            = new_limit
            data['speed-limit-'+direction+'-enabled'] = limit_enabled
        else:
            type = 'torrent-set'
            data['ids'] = [torrent_id]
            data[direction+'loadLimit']   = new_limit
            data[direction+'loadLimited'] = limit_enabled

        request = TransmissionRequest(self.host, self.port, type, 1, data)
        request.send_request()
        self.wait_for_torrentlist_update()


    def increase_bandwidth_priority(self, torrent_id):
        torrent = self.get_torrent_by_id(torrent_id)
        if torrent == None or torrent['bandwidthPriority'] >= 1:
            return False
        else:
            new_priority = torrent['bandwidthPriority'] + 1
            request = TransmissionRequest(self.host, self.port, 'torrent-set', 1,
                                          {'ids': [torrent_id], 'bandwidthPriority':new_priority})
            request.send_request()
            self.wait_for_torrentlist_update()

    def decrease_bandwidth_priority(self, torrent_id):
        torrent = self.get_torrent_by_id(torrent_id)
        if torrent == None or torrent['bandwidthPriority'] <= -1:
            return False
        else:
            new_priority = torrent['bandwidthPriority'] - 1
            request = TransmissionRequest(self.host, self.port, 'torrent-set', 1,
                                          {'ids': [torrent_id], 'bandwidthPriority':new_priority})
            request.send_request()
            self.wait_for_torrentlist_update()


    def toggle_turtle_mode(self):
        self.set_option('alt-speed-enabled', not self.status_cache['alt-speed-enabled'])


    def add_torrent(self, location):
        request = TransmissionRequest(self.host, self.port, 'torrent-add', 1, {'filename': location})
        request.send_request()
        response = request.get_response()
        if response['result'] != 'success':
            return response['result']
        else:
            return ''

    def stop_torrent(self, id):
        request = TransmissionRequest(self.host, self.port, 'torrent-stop', 1, {'ids': [id]})
        request.send_request()
        self.wait_for_torrentlist_update()

    def start_torrent(self, id):
        request = TransmissionRequest(self.host, self.port, 'torrent-start', 1, {'ids': [id]})
        request.send_request()
        self.wait_for_torrentlist_update()

    def verify_torrent(self, id):
        request = TransmissionRequest(self.host, self.port, 'torrent-verify', 1, {'ids': [id]})
        request.send_request()
        self.wait_for_torrentlist_update()

    def reannounce_torrent(self, id):
        request = TransmissionRequest(self.host, self.port, 'torrent-reannounce', 1, {'ids': [id]})
        request.send_request()
        self.wait_for_torrentlist_update()

    def move_torrent(self, torrent_id, new_location):
        request = TransmissionRequest(self.host, self.port, 'torrent-set-location', 1,
                                      {'ids': torrent_id, 'location': new_location, 'move': True})
        request.send_request()
        self.wait_for_torrentlist_update()

    def remove_torrent(self, id):
        request = TransmissionRequest(self.host, self.port, 'torrent-remove', 1, {'ids': [id]})
        request.send_request()
        self.wait_for_torrentlist_update()

    def remove_torrent_local_data(self, id):
        request = TransmissionRequest(self.host, self.port, 'torrent-remove', 1, {'ids': [id], 'delete-local-data':True})
        request.send_request()
        self.wait_for_torrentlist_update()

    def increase_file_priority(self, file_nums):
        file_nums = list(file_nums)
        ref_num = file_nums[0]
        for num in file_nums:
            if not self.torrent_details_cache['wanted'][num]:
                ref_num = num
                break
            elif self.torrent_details_cache['priorities'][num] < \
                    self.torrent_details_cache['priorities'][ref_num]:
                ref_num = num
        current_priority = self.torrent_details_cache['priorities'][ref_num]
        if not self.torrent_details_cache['wanted'][ref_num]:
            self.set_file_priority(self.torrent_details_cache['id'], file_nums, 'low')
        elif current_priority == -1:
            self.set_file_priority(self.torrent_details_cache['id'], file_nums, 'normal')
        elif current_priority == 0:
            self.set_file_priority(self.torrent_details_cache['id'], file_nums, 'high')

    def decrease_file_priority(self, file_nums):
        file_nums = list(file_nums)
        ref_num = file_nums[0]
        for num in file_nums:
            if self.torrent_details_cache['priorities'][num] > \
                    self.torrent_details_cache['priorities'][ref_num]:
                ref_num = num
        current_priority = self.torrent_details_cache['priorities'][ref_num]
        if current_priority >= 1:
            self.set_file_priority(self.torrent_details_cache['id'], file_nums, 'normal')
        elif current_priority == 0:
            self.set_file_priority(self.torrent_details_cache['id'], file_nums, 'low')
        elif current_priority == -1:
            self.set_file_priority(self.torrent_details_cache['id'], file_nums, 'off')


    def set_file_priority(self, torrent_id, file_nums, priority):
        request_data = {'ids': [torrent_id]}
        if priority == 'off':
            request_data['files-unwanted'] = file_nums
        else:
            request_data['files-wanted'] = file_nums
            request_data['priority-' + priority] = file_nums
        request = TransmissionRequest(self.host, self.port, 'torrent-set', 1, request_data)
        request.send_request()
        self.wait_for_details_update()

    def get_file_priority(self, torrent_id, file_num):
        priority = self.torrent_details_cache['priorities'][file_num]
        if not self.torrent_details_cache['wanted'][file_num]: return 'off'
        elif priority <= -1: return 'low'
        elif priority == 0:  return 'normal'
        elif priority >= 1:  return 'high'
        return '?'


    def wait_for_torrentlist_update(self):
        self.wait_for_update(7)
    def wait_for_details_update(self):
        self.wait_for_update(77)
    def wait_for_status_update(self):
        self.wait_for_update(22)
    def wait_for_update(self, update_id):
        self.update(0) # send request
        while True:    # wait for response
            if self.update(0, update_id): break
            time.sleep(0.1)


    def get_status(self, torrent):
        if torrent['status'] == Transmission.STATUS_CHECK_WAIT:
            status = 'will verify'
        elif torrent['status'] == Transmission.STATUS_CHECK:
            status = "verifying"
        elif torrent['status'] == Transmission.STATUS_SEED:
            status = 'seeding'
        elif torrent['status'] == Transmission.STATUS_DOWNLOAD:
            status = ('idle','downloading')[torrent['rateDownload'] > 0]
        elif torrent['status'] == Transmission.STATUS_STOPPED:
            status = 'paused'
        else:
            status = 'unknown state'
        return status

    def get_bandwidth_priority(self, torrent):
        if torrent['bandwidthPriority'] == -1:
            return '-'
        elif torrent['bandwidthPriority'] == 0:
            return ' '
        elif torrent['bandwidthPriority'] == 1:
            return '+'
        else:
            return '?'

# End of Class Transmission





# User Interface
class Interface:
    TRACKER_ITEM_HEIGHT = 6

    def __init__(self, server):
        self.server = server

        self.filter_list    = config.get('Filtering', 'filter')
        self.filter_inverse = config.getboolean('Filtering', 'invert')

        self.sort_orders  = config.get('Sorting', 'order').split(',') #['name']
        self.sort_reverse = config.getboolean('Sorting', 'reverse')

        self.torrents         = self.server.get_torrent_list(self.sort_orders, self.sort_reverse)
        self.stats            = self.server.get_global_stats()
        self.torrent_details  = []
        self.selected_torrent = -1  # changes to >-1 when focus >-1 & user hits return
        self.all_paused = False

        self.focus     = -1  # -1: nothing focused; 0: top of list; <# of torrents>-1: bottom of list
        self.scrollpos = 0   # start of torrentlist
        self.torrents_per_page  = 0 # will be set by manage_layout()
        self.rateDownload_width = self.rateUpload_width = 2

        self.details_category_focus = 0  # overview/files/peers/tracker in details
        self.focus_detaillist       = -1 # same as focus but for details
        self.selected_files         = [] # marked files in details
        self.scrollpos_detaillist   = 0  # same as scrollpos but for details

        self.keybindings = {
            ord('?'):               self.call_list_key_bindings,
            curses.KEY_F1:          self.call_list_key_bindings,
            27:                     self.go_back_or_unfocus,
            curses.KEY_BREAK:       self.go_back_or_unfocus,
            12:                     self.go_back_or_unfocus,
            curses.KEY_BACKSPACE:   self.leave_details,
            ord('q'):               self.go_back_or_quit,
            ord('o'):               self.o_key,
            ord('\n'):              self.select_torrent_detail_view,
            curses.KEY_RIGHT:       self.right_key,
            ord('l'):               self.l_key,
            ord('s'):               self.show_sort_order_menu,
            ord('f'):               self.f_key,
            ord('u'):               self.global_upload,
            ord('d'):               self.global_download,
            ord('U'):               self.torrent_upload,
            ord('D'):               self.torrent_download,
            ord('t'):               self.t_key,
            ord('+'):               self.bandwidth_priority,
            ord('-'):               self.bandwidth_priority,
            ord('p'):               self.pause_unpause_torrent,
            ord('P'):               self.pause_unpause_all_torrent,
            ord('v'):               self.verify_torrent,
            ord('y'):               self.verify_torrent,
            ord('r'):               self.remove_torrent,
            curses.KEY_DC:          self.remove_torrent,
            ord('R'):               self.remove_torrent_local_data,
            curses.KEY_SDC:         self.remove_torrent_local_data,
            curses.KEY_UP:          self.movement_keys,
            ord('k'):               self.movement_keys,
            curses.KEY_DOWN:        self.movement_keys,
            ord('j'):               self.movement_keys,
            curses.KEY_PPAGE:       self.movement_keys,
            curses.KEY_NPAGE:       self.movement_keys,
            curses.KEY_HOME:        self.movement_keys,
            curses.KEY_END:         self.movement_keys,
            ord("\t"):              self.move_in_details,
            curses.KEY_BTAB:        self.move_in_details,
            ord('e'):               self.move_in_details,
            ord('c'):               self.move_in_details,
            ord('h'):               self.file_pritority_or_switch_details,
            curses.KEY_LEFT:        self.file_pritority_or_switch_details,
            ord(' '):               self.select_unselect_file,
            ord('a'):               self.a_key,
            ord('m'):               self.move_torrent,
            ord('n'):               self.reannounce_torrent
        }

        try:
            self.init_screen()
            self.run()
        except:
            self.restore_screen()
            (exc_type, exc_value, exc_traceback) = sys.exc_info()
            raise exc_type, exc_value, exc_traceback
        else:
            self.restore_screen()


    def init_screen(self):
        os.environ['ESCDELAY'] = '0' # make escape usable
        self.screen = curses.initscr()
        curses.noecho() ; curses.cbreak() ; self.screen.keypad(1)
        curses.halfdelay(10) # STDIN timeout

        try: curses.curs_set(0)   # hide cursor if possible
        except curses.error: pass # some terminals seem to have problems with that

        # enable colors if available
        try:
            curses.start_color()
            curses.init_pair(1,  curses.COLOR_BLACK,    curses.COLOR_BLUE)   # download rate
            curses.init_pair(2,  curses.COLOR_BLACK,    curses.COLOR_RED)    # upload rate
            curses.init_pair(3,  curses.COLOR_BLUE,     curses.COLOR_BLACK)  # unfinished progress
            curses.init_pair(4,  curses.COLOR_GREEN,    curses.COLOR_BLACK)  # finished progress
            curses.init_pair(5,  curses.COLOR_BLACK,    curses.COLOR_WHITE)  # eta/ratio
            curses.init_pair(6,  curses.COLOR_CYAN,     curses.COLOR_BLACK)  # idle progress
            curses.init_pair(7,  curses.COLOR_MAGENTA,  curses.COLOR_BLACK)  # verifying
            curses.init_pair(8,  curses.COLOR_WHITE,    curses.COLOR_BLACK)  # button
            curses.init_pair(9,  curses.COLOR_BLACK,    curses.COLOR_WHITE)  # focused button
            curses.init_pair(10, curses.COLOR_WHITE,    curses.COLOR_RED)    # stats filter
            curses.init_pair(11, curses.COLOR_RED,      curses.COLOR_BLACK)  # high   file priority
            curses.init_pair(12, curses.COLOR_WHITE,    curses.COLOR_BLACK)  # normal file priority
            curses.init_pair(13, curses.COLOR_YELLOW,   curses.COLOR_BLACK)  # low    file priority
            curses.init_pair(14, curses.COLOR_BLUE,     curses.COLOR_BLACK)  # off    file priority
        except:
            pass

        signal.signal(signal.SIGWINCH, lambda y,frame: self.get_screen_size())
        self.get_screen_size()

    def restore_screen(self):
        curses.endwin()



    def get_screen_size(self):
        time.sleep(0.1) # prevents curses.error on rapid resizing
        while True:
            curses.endwin()
            self.screen.refresh()
            self.height, self.width = self.screen.getmaxyx()
            if self.width < 60 or self.height < 16:
                self.screen.erase()
                self.screen.addstr(0,0, "Terminal too small", curses.A_REVERSE + curses.A_BOLD)
                time.sleep(1)
            else:
                break
        self.manage_layout()


    def manage_layout(self):
        self.pad_height = max((len(self.torrents)+1)*3, self.height)
        self.pad = curses.newpad(self.pad_height, self.width)
        self.mainview_height = self.height - 2
        self.torrents_per_page = self.mainview_height/3
        self.detaillistitems_per_page = self.height - 8

        if self.selected_torrent > -1:
            self.rateDownload_width = self.get_rateDownload_width([self.torrent_details])
            self.rateUpload_width   = self.get_rateUpload_width([self.torrent_details])
            self.torrent_title_width = self.width - self.rateUpload_width - 2
            # show downloading column only if torrents is downloading
            if self.torrent_details['status'] == Transmission.STATUS_DOWNLOAD:
                self.torrent_title_width -= self.rateDownload_width + 2

        elif self.torrents:
            visible_torrents = self.torrents[self.scrollpos/3 : self.scrollpos/3 + self.torrents_per_page + 1]
            self.rateDownload_width = self.get_rateDownload_width(visible_torrents)
            self.rateUpload_width   = self.get_rateUpload_width(visible_torrents)

            self.torrent_title_width = self.width - self.rateUpload_width - 2
            # show downloading column only if any downloading torrents are visible
            if filter(lambda x: x['status']==Transmission.STATUS_DOWNLOAD, visible_torrents):
                self.torrent_title_width -= self.rateDownload_width + 2
        else:
            self.torrent_title_width = 80

    def get_rateDownload_width(self, torrents):
        new_width = max(map(lambda x: len(scale_bytes(x['rateDownload'])), torrents))
        new_width = max(max(map(lambda x: len(scale_time(x['eta'])), torrents)), new_width)
        new_width = max(len(scale_bytes(self.stats['downloadSpeed'])), new_width)
        new_width = max(self.rateDownload_width, new_width) # don't shrink
        return new_width

    def get_rateUpload_width(self, torrents):
        new_width = max(map(lambda x: len(scale_bytes(x['rateUpload'])), torrents))
        new_width = max(max(map(lambda x: len(num2str(x['uploadRatio'])), torrents)), new_width)
        new_width = max(len(scale_bytes(self.stats['uploadSpeed'])), new_width)
        new_width = max(self.rateUpload_width, new_width) # don't shrink
        return new_width


    def run(self):
        self.draw_title_bar()
        self.draw_stats()
        self.draw_torrent_list()

        while True:
            self.server.update(1)

            # display torrentlist
            if self.selected_torrent == -1:
                self.draw_torrent_list()

            # display some torrent's details
            else:
                self.draw_details()

            self.stats = self.server.get_global_stats()
            self.draw_title_bar()  # show shortcuts and stuff
            self.draw_stats()      # show global states

            self.screen.move(0,0)  # in case cursor can't be invisible
            if self.handle_user_input():
                return

    def go_back_or_unfocus(self, c):
        if self.focus_detaillist > -1:   # unfocus and deselect file
            self.focus_detaillist     = -1
            self.scrollpos_detaillist = 0
            self.selected_files       = []
        elif self.selected_torrent > -1: # return from details
            self.details_category_focus = 0
            self.selected_torrent = -1
            self.selected_files   = []
        else:
            if self.focus > -1:
                self.scrollpos = 0    # unfocus main list
                self.focus     = -1
            elif self.filter_list:
                self.filter_list = '' # reset filter

    def leave_details(self, c):
        if self.selected_torrent > -1:
            self.server.set_torrent_details_id(-1)
            self.selected_torrent       = -1
            self.details_category_focus = 0
            self.scrollpos_detaillist   = 0
            self.selected_files         = []

    def go_back_or_quit(self, c):
        if self.selected_torrent == -1:
            config.set('Sorting', 'order',   ','.join(self.sort_orders))
            config.set('Sorting', 'reverse', str(self.sort_reverse))
            config.set('Filtering', 'filter', self.filter_list)
            config.set('Filtering', 'invert', str(self.filter_inverse))
            return quit()
        else: # return to list view
            self.server.set_torrent_details_id(-1)
            self.selected_torrent       = -1
            self.details_category_focus = 0
            self.focus_detaillist       = -1
            self.scrollpos_detaillist   = 0
            self.selected_files         = []

    def a_key(self, c):
        if self.selected_torrent > -1:
            self.select_unselect_file(c)
        else:
            self.add_torrent()

    def o_key(self, c):
        if self.selected_torrent == -1:
            self.draw_options_dialog()
        elif self.selected_torrent > -1:
            self.details_category_focus = 0

    def l_key(self, c):
        if self.focus > -1 and self.selected_torrent == -1:
            self.select_torrent_detail_view(c)
        elif self.selected_torrent > -1:
            self.file_pritority_or_switch_details(c)

    def t_key(self, c):
        if self.selected_torrent == -1:
            self.server.toggle_turtle_mode()
        elif self.selected_torrent > -1:
            self.details_category_focus = 3

    def f_key(self, c):
        if self.selected_torrent == -1:
            self.show_state_filter_menu(c)
        elif self.selected_torrent > -1:
            self.details_category_focus = 1

    def right_key(self, c):
        if self.focus > -1 and self.selected_torrent == -1:
            self.select_torrent_detail_view(c)
        else:
            self.file_pritority_or_switch_details(c)

    def add_torrent(self):
        location = self.dialog_input_text("Add torrent from file or URL", os.getcwd())
        if location:
            error = self.server.add_torrent(location)
            if error:
                msg = wrap("Couldn't add torrent \"%s\":" % location)
                msg.extend(wrap(error, self.width-4))
                self.dialog_ok("\n".join(msg))

    def select_torrent_detail_view(self, c):
        if self.focus > -1 and self.selected_torrent == -1:
            self.screen.clear()
            self.selected_torrent = self.focus
            self.server.set_torrent_details_id(self.torrents[self.focus]['id'])
            self.server.wait_for_details_update()

    def show_sort_order_menu(self, c):
        if self.selected_torrent == -1:
           options = [('name','_Name'), ('addedDate','_Age'), ('percent_done','_Progress'),
                      ('seeders','_Seeds'), ('leechers','Lee_ches'), ('sizeWhenDone', 'Si_ze'),
                      ('status','S_tatus'), ('uploadedEver','Up_loaded'),
                      ('rateUpload','_Upload Speed'), ('rateDownload','_Download Speed'),
                      ('uploadRatio','_Ratio'),
                      ('peersConnected','P_eers'), ('reverse','Re_verse')]
           choice = self.dialog_menu('Sort order', options,
                                     map(lambda x: x[0]==self.sort_orders[-1], options).index(True)+1)
           if choice == 'reverse':
               self.sort_reverse = not self.sort_reverse
           else:
               self.sort_orders.append(choice)
               while len(self.sort_orders) > 2:
                   self.sort_orders.pop(0)

    def show_state_filter_menu(self, c):
        if self.selected_torrent == -1:
            options = [('uploading','_Uploading'), ('downloading','_Downloading'),
                       ('active','Ac_tive'), ('paused','_Paused'), ('seeding','_Seeding'),
                       ('incomplete','In_complete'), ('verifying','Verif_ying'),
                       ('invert','In_vert'), ('','_All')]
            choice = self.dialog_menu(('Show only','Filter all')[self.filter_inverse], options,
                                      map(lambda x: x[0]==self.filter_list, options).index(True)+1)
            if choice == 'invert':
                self.filter_inverse = not self.filter_inverse
            else:
                if choice == '': self.filter_inverse = False
                self.filter_list = choice

    def global_upload(self, c):
       current_limit = (0,self.stats['speed-limit-up'])[self.stats['speed-limit-up-enabled']]
       limit = self.dialog_input_number("Global upload limit in kilobytes per second", current_limit)
       self.server.set_rate_limit('up', limit)

    def global_download(self, c):
       current_limit = (0,self.stats['speed-limit-down'])[self.stats['speed-limit-down-enabled']]
       limit = self.dialog_input_number("Global download limit in kilobytes per second", current_limit)
       self.server.set_rate_limit('down', limit)

    def torrent_upload(self, c):
        if self.focus > -1:
            current_limit = (0,self.torrents[self.focus]['uploadLimit'])[self.torrents[self.focus]['uploadLimited']]
            limit = self.dialog_input_number("Upload limit in kilobytes per second for\n%s" % \
                                                 self.torrents[self.focus]['name'], current_limit)
            self.server.set_rate_limit('up', limit, self.torrents[self.focus]['id'])

    def torrent_download(self, c):
        if self.focus > -1:
            current_limit = (0,self.torrents[self.focus]['downloadLimit'])[self.torrents[self.focus]['downloadLimited']]
            limit = self.dialog_input_number("Download limit in Kilobytes per second for\n%s" % \
                                                 self.torrents[self.focus]['name'], current_limit)
            self.server.set_rate_limit('down', limit, self.torrents[self.focus]['id'])

    def bandwidth_priority(self, c):
        if c == ord('-') and self.focus > -1:
            self.server.decrease_bandwidth_priority(self.torrents[self.focus]['id'])
        elif c == ord('+') and self.focus > -1:
            self.server.increase_bandwidth_priority(self.torrents[self.focus]['id'])

    def pause_unpause_torrent(self, c):
        if self.focus > -1:
            if self.selected_torrent > -1:
                t = self.torrent_details
            else:
                t = self.torrents[self.focus]
            if t['status'] == Transmission.STATUS_STOPPED:
                self.server.start_torrent(t['id'])
            else:
                self.server.stop_torrent(t['id'])

    def pause_unpause_all_torrent(self, c):
        if self.all_paused:
            for t in self.torrents:
                self.server.start_torrent(t['id'])
            self.all_paused = False
        else:
            for t in self.torrents:
                self.server.stop_torrent(t['id'])
            self.all_paused = True

    def verify_torrent(self, c):
        if self.focus > -1:
            if self.torrents[self.focus]['status'] != Transmission.STATUS_CHECK:
                self.server.verify_torrent(self.torrents[self.focus]['id'])

    def reannounce_torrent(self, c):
        if self.focus > -1:
            self.server.reannounce_torrent(self.torrents[self.focus]['id'])

    def remove_torrent(self, c):
        if self.focus > -1:
            name = self.torrents[self.focus]['name'][0:self.width - 15]
            if self.dialog_yesno("Remove %s?" % name) == True:
                if self.selected_torrent > -1:  # leave details
                    self.server.set_torrent_details_id(-1)
                    self.selected_torrent = -1
                    self.details_category_focus = 0
                self.server.remove_torrent(self.torrents[self.focus]['id'])
                self.focus += 1

    def remove_torrent_local_data(self, c):
        if self.focus > -1:
            name = self.torrents[self.focus]['name'][0:self.width - 15]
            if self.dialog_yesno("Remove and delete %s?" % name, important=True) == True:
                if self.selected_torrent > -1:  # leave details
                    self.server.set_torrent_details_id(-1)
                    self.selected_torrent = -1
                    self.details_category_focus = 0
                self.server.remove_torrent_local_data(self.torrents[self.focus]['id'])
                self.focus += 1

    def movement_keys(self, c):
        if self.selected_torrent == -1:
            if   c == curses.KEY_UP or c == ord('k'):
                self.focus, self.scrollpos = self.move_up(self.focus, self.scrollpos, 3)
            elif c == curses.KEY_DOWN or c == ord('j'):
                self.focus, self.scrollpos = self.move_down(self.focus, self.scrollpos, 3,
                                                            self.torrents_per_page, len(self.torrents))
            elif c == curses.KEY_PPAGE:
                self.focus, self.scrollpos = self.move_page_up(self.focus, self.scrollpos, 3,
                                                               self.torrents_per_page)
            elif c == curses.KEY_NPAGE:
                self.focus, self.scrollpos = self.move_page_down(self.focus, self.scrollpos, 3,
                                                                 self.torrents_per_page, len(self.torrents))
            elif c == curses.KEY_HOME:
                self.focus, self.scrollpos = self.move_to_top()
            elif c == curses.KEY_END:
                self.focus, self.scrollpos = self.move_to_end(3, self.torrents_per_page, len(self.torrents))
        elif self.selected_torrent > -1:
            # file list
            if self.details_category_focus == 1:
                # focus/movement
                if c == curses.KEY_UP or c == ord('k'):
                    self.focus_detaillist, self.scrollpos_detaillist = \
                        self.move_up(self.focus_detaillist, self.scrollpos_detaillist, 1)
                elif c == curses.KEY_DOWN or c == ord('j'):
                    self.focus_detaillist, self.scrollpos_detaillist = \
                        self.move_down(self.focus_detaillist, self.scrollpos_detaillist, 1,
                                       self.detaillistitems_per_page, len(self.torrent_details['files']))
                elif c == curses.KEY_PPAGE:
                    self.focus_detaillist, self.scrollpos_detaillist = \
                        self.move_page_up(self.focus_detaillist, self.scrollpos_detaillist, 1,
                                          self.detaillistitems_per_page)
                elif c == curses.KEY_NPAGE:
                    self.focus_detaillist, self.scrollpos_detaillist = \
                        self.move_page_down(self.focus_detaillist, self.scrollpos_detaillist, 1,
                                            self.detaillistitems_per_page, len(self.torrent_details['files']))
                elif c == curses.KEY_HOME:
                    self.focus_detaillist, self.scrollpos_detaillist = self.move_to_top()
                elif c == curses.KEY_END:
                    self.focus_detaillist, self.scrollpos_detaillist = \
                        self.move_to_end(1, self.detaillistitems_per_page, len(self.torrent_details['files']))
            list_len = 0

            # peer list movement
            if self.details_category_focus == 2:
                list_len = len(self.torrent_details['peers'])

            # tracker list movement
            elif self.details_category_focus == 3:
                list_len = len(self.torrent_details['trackerStats']) * self.TRACKER_ITEM_HEIGHT - 1

            # pieces list movement
            elif self.details_category_focus == 4:
                piece_count = self.torrent_details['pieceCount']
                margin = len(str(piece_count)) + 2
                map_width = int(str(self.width-margin-1)[0:-1] + '0')
                list_len = int(piece_count / map_width) + 1

            if list_len:
                if c == curses.KEY_UP or c == ord('k'):
                    if self.scrollpos_detaillist > 0:
                        self.scrollpos_detaillist -= 1
                elif c == curses.KEY_DOWN or c == ord('j'):
                    if self.scrollpos_detaillist < list_len - self.detaillistitems_per_page:
                        self.scrollpos_detaillist += 1
                elif c == curses.KEY_PPAGE:
                    if self.scrollpos_detaillist > self.detaillistitems_per_page - 1:
                        self.scrollpos_detaillist -= self.detaillistitems_per_page - 1
                    else:
                        self.scrollpos_detaillist = 0
                elif c == curses.KEY_NPAGE:
                    if self.scrollpos_detaillist < list_len - self.detaillistitems_per_page * 2 + 1:
                        self.scrollpos_detaillist += self.detaillistitems_per_page - 1
                    elif list_len > self.detaillistitems_per_page:
                        self.scrollpos_detaillist = list_len - self.detaillistitems_per_page
                elif c == curses.KEY_HOME:
                    self.scrollpos_detaillist = 0
                elif c == curses.KEY_END:
                    if list_len > self.detaillistitems_per_page:
                        self.scrollpos_detaillist = list_len - self.detaillistitems_per_page

    def file_pritority_or_switch_details(self, c):
        if self.selected_torrent > -1:
            # file priority OR walk through details
            if c == curses.KEY_RIGHT or c == ord('l'):
                if self.details_category_focus == 1 and \
                        (self.selected_files or self.focus_detaillist > -1):
                    if self.selected_files:
                        files = set(self.selected_files)
                        self.server.increase_file_priority(files)
                    elif self.focus_detaillist > -1:
                        self.server.increase_file_priority([self.focus_detaillist])
                else:
                    self.scrollpos_detaillist = 0
                    self.next_details()
            elif c == curses.KEY_LEFT or c == ord('h'):
                if self.details_category_focus == 1 and \
                        (self.selected_files or self.focus_detaillist > -1):
                    if self.selected_files:
                        files = set(self.selected_files)
                        self.server.decrease_file_priority(files)
                    elif self.focus_detaillist > -1:
                        self.server.decrease_file_priority([self.focus_detaillist])
                else:
                    self.scrollpos_detaillist = 0
                    self.prev_details()

    def select_unselect_file(self, c):
        if self.selected_torrent > -1 and self.details_category_focus == 1:
            # file selection with space
            if c == ord(' '):
                try:
                    self.selected_files.pop(self.selected_files.index(self.focus_detaillist))
                except ValueError:
                    self.selected_files.append(self.focus_detaillist)
                curses.ungetch(curses.KEY_DOWN) # move down
            # (un)select all files
            elif c == ord('a'):
                if self.selected_files:
                    self.selected_files = []
                else:
                    self.selected_files = range(0, len(self.torrent_details['files']))

    def move_in_details(self, c):
        if self.selected_torrent > -1:
            if c == ord("\t"):
                self.next_details()
            elif c == curses.KEY_BTAB:
                self.prev_details()
            elif c == ord('e'):
                self.details_category_focus = 2
            elif c == ord('c'):
                self.details_category_focus = 4

    def call_list_key_bindings(self, c):
        self.list_key_bindings()

    def move_torrent(self, c):
        if self.focus > -1:
            location = homedir2tilde(self.torrents[self.focus]['downloadDir'])
            msg = 'Move "%s" from\n%s to' % (self.torrents[self.focus]['name'], location)
            path = self.dialog_input_text(msg, location)
            if path:
                self.server.move_torrent(self.torrents[self.focus]['id'], tilde2homedir(path))

    def handle_user_input(self):
        c = self.screen.getch()
        if c == -1:
            return 0

        f = self.keybindings.get(c, None)
        if f:
            f(c)

        # update view
        if self.selected_torrent == -1:
            self.draw_torrent_list()
        else:
            self.draw_details()

    def filter_torrent_list(self):
        unfiltered = self.torrents
        if self.filter_list == 'downloading':
            self.torrents = [t for t in self.torrents if t['rateDownload'] > 0]
        elif self.filter_list == 'uploading':
            self.torrents = [t for t in self.torrents if t['rateUpload'] > 0]
        elif self.filter_list == 'paused':
            self.torrents = [t for t in self.torrents if t['status'] == Transmission.STATUS_STOPPED]
        elif self.filter_list == 'seeding':
            self.torrents = [t for t in self.torrents if t['status'] == Transmission.STATUS_SEED]
        elif self.filter_list == 'incomplete':
            self.torrents = [t for t in self.torrents if t['percent_done'] < 100]
        elif self.filter_list == 'active':
            self.torrents = [t for t in self.torrents if t['peersGettingFromUs'] > 0 \
                                 or t['peersSendingToUs'] > 0 or t['status'] == Transmission.STATUS_CHECK]
            #self.torrents = [t for t in self.torrents if t['peersConnected'] > 0]
        elif self.filter_list == 'verifying':
            self.torrents = [t for t in self.torrents if t['status'] == Transmission.STATUS_CHECK \
                                 or t['status'] == Transmission.STATUS_CHECK_WAIT]
        # invert list?
        if self.filter_inverse:
            self.torrents = [t for t in unfiltered if t not in self.torrents]

    def follow_list_focus(self, id):
        if self.focus == -1:
            return
        elif len(self.torrents) == 0:
            self.focus, self.scrollpos = -1, 0
            return

        self.focus = min(self.focus, len(self.torrents)-1)
        if self.torrents[self.focus]['id'] != id:
            for i,t in enumerate(self.torrents):
                if id == t['id']:
                    new_focus = i
                    break
            try:
                self.focus = new_focus
            except UnboundLocalError:
                self.focus, self.scrollpos = -1, 0
                return

        # make sure the focus is not above the visible area
        while self.focus < (self.scrollpos/3):
            self.scrollpos -= 3
        # make sure the focus is not below the visible area
        while self.focus > (self.scrollpos/3) + self.torrents_per_page-1:
            self.scrollpos += 3
        # keep min and max bounds
        self.scrollpos = min(self.scrollpos, (len(self.torrents) - self.torrents_per_page) * 3)
        self.scrollpos = max(0, self.scrollpos)

    def draw_torrent_list(self):
        try:
            focused_id = self.torrents[self.focus]['id']
        except IndexError:
            focused_id = -1
        self.torrents = self.server.get_torrent_list(self.sort_orders, self.sort_reverse)
        self.filter_torrent_list()
        self.follow_list_focus(focused_id)
        self.manage_layout()

        ypos = 0
        for i in range(len(self.torrents)):
            self.draw_torrentlist_item(self.torrents[i], (i == self.focus), ypos)
            ypos += 3

        self.pad.refresh(self.scrollpos,0, 1,0, self.mainview_height,self.width-1)
        self.screen.refresh()


    def draw_torrentlist_item(self, torrent, focused, y):
        # the torrent name is also a progress bar
        self.draw_torrentlist_title(torrent, focused, self.torrent_title_width, y)

        rates = ''
        if torrent['status'] == Transmission.STATUS_DOWNLOAD:
            self.draw_downloadrate(torrent, y)
        if torrent['status'] == Transmission.STATUS_DOWNLOAD or torrent['status'] == Transmission.STATUS_SEED:
            self.draw_uploadrate(torrent, y)
        if torrent['percent_done'] < 100 and torrent['status'] == Transmission.STATUS_DOWNLOAD:
            self.draw_eta(torrent, y)

        self.draw_ratio(torrent, y)

        # the line below the title/progress
        self.draw_torrentlist_status(torrent, focused, y)



    def draw_downloadrate(self, torrent, ypos):
        self.pad.move(ypos, self.width-self.rateDownload_width-self.rateUpload_width-3)
        self.pad.addch(curses.ACS_DARROW, (0,curses.A_BOLD)[torrent['downloadLimited']])
        rate = ('',scale_bytes(torrent['rateDownload']))[torrent['rateDownload']>0]
        self.pad.addstr(rate.rjust(self.rateDownload_width),
                        curses.color_pair(1) + curses.A_BOLD + curses.A_REVERSE)
    def draw_uploadrate(self, torrent, ypos):
        self.pad.move(ypos, self.width-self.rateUpload_width-1)
        self.pad.addch(curses.ACS_UARROW, (0,curses.A_BOLD)[torrent['uploadLimited']])
        rate = ('',scale_bytes(torrent['rateUpload']))[torrent['rateUpload']>0]
        self.pad.addstr(rate.rjust(self.rateUpload_width),
                        curses.color_pair(2) + curses.A_BOLD + curses.A_REVERSE)
    def draw_ratio(self, torrent, ypos):
        self.pad.addch(ypos+1, self.width-self.rateUpload_width-1, curses.ACS_DIAMOND,
                       (0,curses.A_BOLD)[torrent['uploadRatio'] < 1 and torrent['uploadRatio'] >= 0])
        self.pad.addstr(ypos+1, self.width-self.rateUpload_width,
                        num2str(torrent['uploadRatio']).rjust(self.rateUpload_width),
                        curses.color_pair(5) + curses.A_BOLD + curses.A_REVERSE)
    def draw_eta(self, torrent, ypos):
        self.pad.addch(ypos+1, self.width-self.rateDownload_width-self.rateUpload_width-3, curses.ACS_PLMINUS)
        self.pad.addstr(ypos+1, self.width-self.rateDownload_width-self.rateUpload_width-2,
                        scale_time(torrent['eta']).rjust(self.rateDownload_width),
                        curses.color_pair(5) + curses.A_BOLD + curses.A_REVERSE)


    def draw_torrentlist_title(self, torrent, focused, width, ypos):
        if torrent['status'] == Transmission.STATUS_CHECK:
            percent_done = float(torrent['recheckProgress']) * 100
        else:
            percent_done = torrent['percent_done']

        bar_width = int(float(width) * (float(percent_done)/100))
        title = torrent['name'][0:width].ljust(width)

        size = "%5s" % scale_bytes(torrent['sizeWhenDone'])
        if torrent['percent_done'] < 100:
            if torrent['seeders'] <= 0 and torrent['status'] != Transmission.STATUS_CHECK:
                size = "%5s / " % scale_bytes(torrent['available']) + size
            size = "%5s / " % scale_bytes(torrent['haveValid'] + torrent['haveUnchecked']) + size
        size = '| ' + size
        title = title[:-len(size)] + size

        if torrent['status'] == Transmission.STATUS_SEED:
            color = curses.color_pair(4)
        elif torrent['status'] == Transmission.STATUS_STOPPED:
            color = curses.color_pair(5) + curses.A_UNDERLINE
        elif torrent['status'] == Transmission.STATUS_CHECK or \
                torrent['status'] == Transmission.STATUS_CHECK_WAIT:
            color = curses.color_pair(7)
        elif torrent['rateDownload'] == 0:
            color = curses.color_pair(6)
        elif torrent['percent_done'] < 100:
            color = curses.color_pair(3)
        else:
            color = 0

        tag = curses.A_REVERSE
        tag_done = tag + color
        if focused:
            tag += curses.A_BOLD
            tag_done += curses.A_BOLD

        # addstr() dies when you tell it to draw on the last column of the
        # terminal, so we have to catch this exception.
        try:
            self.pad.addstr(ypos, 0, title[0:bar_width].encode('utf-8'), tag_done)
            self.pad.addstr(ypos, bar_width, title[bar_width:].encode('utf-8'), tag)
        except:
            pass


    def draw_torrentlist_status(self, torrent, focused, ypos):
        peers = ''
        parts = [self.server.get_status(torrent)]

        # show tracker error if appropriate
        if torrent['errorString'] and \
                not torrent['seeders'] and not torrent['leechers'] and \
                not torrent['status'] == Transmission.STATUS_STOPPED:
            parts[0] = torrent['errorString']

        else:
            if torrent['status'] == Transmission.STATUS_CHECK:
                parts[0] += " (%d%%)" % int(float(torrent['recheckProgress']) * 100)
            elif torrent['status'] == Transmission.STATUS_DOWNLOAD:
                parts[0] += " (%d%%)" % torrent['percent_done']
            parts[0] = parts[0].ljust(20)

            # seeds and leeches will be appended right justified later
            peers  = "%5s seed%s " % (num2str(torrent['seeders']), ('s', ' ')[torrent['seeders']==1])
            peers += "%5s leech%s" % (num2str(torrent['leechers']), ('es', '  ')[torrent['leechers']==1])

            # show additional information if enough room
            if self.torrent_title_width - sum(map(lambda x: len(x), parts)) - len(peers) > 18:
                uploaded = scale_bytes(torrent['uploadedEver'])
                parts.append("%7s uploaded" % ('nothing',uploaded)[uploaded != '0B'])

            if self.torrent_title_width - sum(map(lambda x: len(x), parts)) - len(peers) > 22:
                parts.append("%4s peer%s connected" % (torrent['peersConnected'],
                                                       ('s',' ')[torrent['peersConnected'] == 1]))


        if focused: tags = curses.A_REVERSE + curses.A_BOLD
        else:       tags = 0

        remaining_space = self.torrent_title_width - sum(map(lambda x: len(x), parts), len(peers)) - 2
        delimiter = ' ' * int(remaining_space / (len(parts)))

        line = self.server.get_bandwidth_priority(torrent) + ' ' + delimiter.join(parts)

        # make sure the peers element is always right justified
        line += ' ' * int(self.torrent_title_width - len(line) - len(peers)) + peers
        self.pad.addstr(ypos+1, 0, line, tags)




    def draw_details(self):
        self.torrent_details = self.server.get_torrent_details()
        self.manage_layout()

        # details could need more space than the torrent list
        self.pad_height = max(50, len(self.torrent_details['files'])+10, (len(self.torrents)+1)*3, self.height)
        self.pad = curses.newpad(self.pad_height, self.width)

        # torrent name + progress bar
        self.draw_torrentlist_item(self.torrent_details, False, 0)

        # divider + menu
        menu_items = ['_Overview', "_Files", 'P_eers', '_Trackers', 'Pie_ces' ]
        xpos = int((self.width - sum(map(lambda x: len(x), menu_items))-len(menu_items)) / 2)
        for item in menu_items:
            self.pad.move(3, xpos)
            tags = curses.A_BOLD
            if menu_items.index(item) == self.details_category_focus:
                tags += curses.A_REVERSE
            title = item.split('_')
            self.pad.addstr(title[0], tags)
            self.pad.addstr(title[1][0], tags + curses.A_UNDERLINE)
            self.pad.addstr(title[1][1:], tags)
            xpos += len(item)+1

        # which details to display
        if self.details_category_focus == 0:
            self.draw_details_overview(5)
        elif self.details_category_focus == 1:
            self.draw_filelist(5)
        elif self.details_category_focus == 2:
            self.draw_peerlist(5)
        elif self.details_category_focus == 3:
            self.draw_trackerlist(5)
        elif self.details_category_focus == 4:
            self.draw_pieces_map(5)

        self.pad.refresh(0,0, 1,0, self.height-2,self.width)
        self.screen.refresh()


    def draw_details_overview(self, ypos):
        t = self.torrent_details
        info = []
        info.append(['Hash: ', "%s" % t['hashString']])
        info.append(['ID: ',   "%s" % t['id']])

        wanted = 0
        for i, file_info in enumerate(t['files']):
            if t['wanted'][i] == True: wanted += t['files'][i]['length']

        sizes = ['Size: ', "%s;  " % scale_bytes(t['totalSize'], 'long'),
                 "%s wanted;  " % (scale_bytes(wanted, 'long'),'everything') [t['totalSize'] == wanted]]
        if t['available'] < t['totalSize']:
            sizes.append("%s available;  " % scale_bytes(t['available'], 'long'))
        sizes.extend(["%s left" % scale_bytes(t['leftUntilDone'], 'long')])
        info.append(sizes)

        info.append(['Files: ', "%d;  " % len(t['files'])])
        complete     = map(lambda x: x['bytesCompleted'] == x['length'], t['files']).count(True)
        not_complete = filter(lambda x: x['bytesCompleted'] != x['length'], t['files'])
        partial      = map(lambda x: x['bytesCompleted'] > 0, not_complete).count(True)
        if complete == len(t['files']):
            info[-1].append("all complete")
        else:
            info[-1].append("%d complete;  " % complete)
            info[-1].append("%d commenced" % partial)

        info.append(['Pieces: ', "%s;  " % t['pieceCount'],
                     "%s each" % scale_bytes(t['pieceSize'], 'long')])

        info.append(['Download: '])
        info[-1].append("%s" % scale_bytes(t['downloadedEver'], 'long') + \
                        " (%d%%) received;  " % int(percent(t['sizeWhenDone'], t['downloadedEver'])))
        info[-1].append("%s" % scale_bytes(t['haveValid'], 'long') + \
                        " (%d%%) verified;  " % int(percent(t['sizeWhenDone'], t['haveValid'])))
        info[-1].append("%s corrupt"  % scale_bytes(t['corruptEver'], 'long'))
        if t['percent_done'] < 100:
            info[-1][-1] += ';  '
            if t['rateDownload']:
                info[-1].append("receiving %s per second" % scale_bytes(t['rateDownload'], 'long'))
                if t['downloadLimited']:
                    info[-1][-1] += " (throttled to %s)" % scale_bytes(t['downloadLimit']*1024, 'long')
            else:
                info[-1].append("no reception in progress")

        try:
            copies_distributed = (float(t['uploadedEver']) / float(t['sizeWhenDone']))
        except ZeroDivisionError:
            copies_distributed = 0
        info.append(['Upload: ', "%s " % scale_bytes(t['uploadedEver'], 'long') + \
                         "(%.2f copies) distributed;  " % copies_distributed])
        if t['rateUpload']:
            info[-1].append("sending %s per second" % scale_bytes(t['rateUpload'], 'long'))
            if t['uploadLimited']:
                info[-1][-1] += " (throttled to %s)" % scale_bytes(t['uploadLimit']*1024, 'long')
        else:
            info[-1].append("no transmission in progress")

        info.append(['Peers: ',
                     "connected to %d;  "     % t['peersConnected'],
                     "downloading from %d;  " % t['peersSendingToUs'],
                     "uploading to %d"        % t['peersGettingFromUs']])

        # average peer speed
        incomplete_peers = [peer for peer in self.torrent_details['peers'] if peer['progress'] < 1]
        if incomplete_peers:
            # use at least 2/3 or 10 of incomplete peers to make an estimation
            active_peers = [peer for peer in incomplete_peers if peer['download_speed']]
            min_active_peers = min(10, max(1, round(len(incomplete_peers)*0.666)))
            if 1 <= len(active_peers) >= min_active_peers:
                swarm_speed  = sum([peer['download_speed'] for peer in active_peers]) / len(active_peers)
                info.append(['Swarm speed: ', "%s on average;  " % scale_bytes(swarm_speed),
                             "distribution of 1 copy takes %s" % \
                                 scale_time(int(t['totalSize'] / swarm_speed), 'long')])
            else:
                info.append(['Swarm speed: ', "<gathering info from %d peers, %d done>" % \
                                 (min_active_peers, len(active_peers))])
        else:
            info.append(['Swarm speed: ', "<no downloading peers connected>"])


        info.append(['Privacy: '])
        if t['isPrivate']:
            info[-1].append('Private to this tracker -- DHT and PEX disabled')
        else:
            info[-1].append('Public torrent')

        info.append(['Location: ',"%s" % homedir2tilde(t['downloadDir'])])

        ypos = self.draw_details_list(ypos, info)

        self.draw_details_eventdates(ypos+1)
        return ypos+1

    def draw_details_eventdates(self, ypos):
        t = self.torrent_details

        self.pad.addstr(ypos,   1, '  Created: ' + timestamp(t['dateCreated']))
        self.pad.addstr(ypos+1, 1, '    Added: ' + timestamp(t['addedDate']))
        self.pad.addstr(ypos+2, 1, '  Started: ' + timestamp(t['startDate']))
        self.pad.addstr(ypos+3, 1, ' Activity: ' + timestamp(t['activityDate']))

        if t['percent_done'] < 100 and t['eta'] > 0:
            self.pad.addstr(ypos+4, 1, 'Finishing: ' + timestamp(time.time() + t['eta']))
        elif t['doneDate'] <= 0:
            self.pad.addstr(ypos+4, 1, 'Finishing: sometime')
        else:
            self.pad.addstr(ypos+4, 1, ' Finished: ' + timestamp(t['doneDate']))

        if t['comment']:
            if self.width >= 90:
                width = self.width - 50
                comment = wrap('Comment: ' + t['comment'], width)
                for i, line in enumerate(comment):
                    if(ypos+i > self.height-1):
                        break
                    self.pad.addstr(ypos+i, 50, line.encode('utf8'))
            else:
                width = self.width - 2
                comment = wrap('Comment: ' + t['comment'], width)
                for i, line in enumerate(comment):
                    self.pad.addstr(ypos+6+i, 2, line.encode('utf8'))

    def draw_filelist(self, ypos):
        column_names = '  #  Progress  Size  Priority  Filename'
        self.pad.addstr(ypos, 0, column_names.ljust(self.width), curses.A_UNDERLINE)
        ypos += 1

        for line in self.create_filelist():
            curses_tags = 0
            # highlight focused/selected line(s)
            while line.startswith('_'):
                if line[1] == 'S':
                    curses_tags  = curses.A_BOLD
                    line = line[2:]
                if line[1] == 'F':
                    curses_tags += curses.A_REVERSE
                    line = line[2:]
                try:
                    self.pad.addstr(ypos, 0, ' '*self.width, curses_tags)
                except: pass

            # colored priority
            xpos = 0
            for part in re.split('(high|normal|low|off)', line, 1):
                if part == 'high':
                    self.pad.addstr(ypos, xpos, part, curses_tags + curses.color_pair(11))
                elif part == 'normal':
                    self.pad.addstr(ypos, xpos, part, curses_tags + curses.color_pair(12))
                elif part == 'low':
                    self.pad.addstr(ypos, xpos, part, curses_tags + curses.color_pair(13))
                elif part == 'off':
                    self.pad.addstr(ypos, xpos, part, curses_tags + curses.color_pair(14))

                else:
                    self.pad.addstr(ypos, xpos, part.encode('utf-8'), curses_tags)
                xpos += len(part)

            ypos += 1
            if ypos > self.height:
                break

    def create_filelist(self):
        filelist = []
        files = self.torrent_details['files']
        current_folder = []
        current_depth = 0
        index = 0
        pos = 0
        pos_before_focus = 0
        for file in files:
            f = file['name'].split('/')
            f_len = len(f) - 1
            if f[:f_len] != current_folder:
                [current_depth, pos] = self.create_filelist_transition(f, current_folder, filelist, current_depth, pos)
                current_folder = f[:f_len]
            filelist.append(self.create_filelist_line(f[-1], index, percent(file['length'], file['bytesCompleted']),
                file['length'], current_depth))
            index += 1
            if self.focus_detaillist == index - 1:
                pos_before_focus = pos
            if index + pos >= self.focus_detaillist + 1 + pos + self.detaillistitems_per_page/2 \
            and index + pos >= self.detaillistitems_per_page:
                if self.focus_detaillist + 1 + pos_before_focus < self.detaillistitems_per_page / 2:
                    return filelist
                return filelist[self.focus_detaillist + 1 + pos_before_focus - self.detaillistitems_per_page / 2
                        : self.focus_detaillist + 1 + pos_before_focus + self.detaillistitems_per_page / 2]
        begin = len(filelist) - self.detaillistitems_per_page
        return filelist[begin > 0 and begin or 0:]

    def create_filelist_transition(self, f, current_folder, filelist, current_depth, pos):
        f_len = len(f) - 1
        current_folder_len = len(current_folder)
        same = 0
        while same < current_folder_len and same  < f_len and f[same] == current_folder[same]:
            same += 1
        for i in range(current_folder_len - same):
            current_depth -= 1
            filelist.append('  '*current_depth + ' '*31 + '/')
            pos += 1
        if f_len < current_folder_len:
            return [current_depth, pos]
        while current_depth < f_len:
            filelist.append('%s\\ %s' % ('  '*current_depth + ' '*31 , f[current_depth]))
            current_depth += 1
            pos += 1
        return [current_depth, pos]

    def create_filelist_line(self, name, index, percent, length, current_depth):
        line = "%s  %6.1f%%" % (str(index+1).rjust(3), percent) + \
            '  '+scale_bytes(length).rjust(5) + \
            '  '+self.server.get_file_priority(self.torrent_details['id'], index).center(8) + \
            " %s| %s" % ('  '*current_depth, name[0:self.width-31-current_depth])
        if index == self.focus_detaillist:
            line = '_F' + line
        if index in self.selected_files:
            line = '_S' + line
        return line

    def draw_peerlist(self, ypos):
        start = self.scrollpos_detaillist
        end   = self.scrollpos_detaillist + self.detaillistitems_per_page
        peers = self.torrent_details['peers'][start:end]

        clientname_width = 0
        for peer in peers:
            if len(peer['clientName']) > clientname_width:
                clientname_width = len(peer['clientName'])

        column_names = "Flags %3d Down %3d Up   Progress      ETA   " % \
            (self.torrent_details['peersSendingToUs'], self.torrent_details['peersGettingFromUs'])
        column_names += 'Client'.ljust(clientname_width) + "          Address"
        if features['geoip']: column_names += "  Country"
        if features['dns']: column_names += "  Host"

        self.pad.addstr(ypos, 0, column_names.ljust(self.width), curses.A_UNDERLINE)
        ypos += 1

        hosts = self.server.get_hosts()
        geo_ips = self.server.get_geo_ips()
        for index, peer in enumerate(peers):
            if features['dns']:
                try:
                    try:
                        host = hosts[peer['address']].check()
                        host_name = host[3][0]
                    except (IndexError, KeyError):
                        host_name = "<not resolvable>"
                except adns.NotReady:
                    host_name = "<resolving>"
                except adns.Error, msg:
                    host_name = msg

# I guess this isn't needed.
#            clientname = peer['clientName']
#            if len(clientname) > clientname_width:
#                clientname = middlecut(peer['clientName'], clientname_width)

            upload_tag = download_tag = line_tag = 0
            if peer['rateToPeer']:   upload_tag   = curses.A_BOLD
            if peer['rateToClient']: download_tag = curses.A_BOLD

            self.pad.move(ypos, 0)
            self.pad.addstr("%-6s   " % peer['flagStr'])
            self.pad.addstr("%5s  " % scale_bytes(peer['rateToClient']), download_tag)
            self.pad.addstr("%5s   " % scale_bytes(peer['rateToPeer']), upload_tag)

            if peer['progress'] < 1:
                self.pad.addstr("%3d%%" % (float(peer['progress'])*100))
            else:
                self.pad.addstr("%3d%%" % (float(peer['progress'])*100), curses.A_BOLD)
            if peer['progress'] < 1 and peer['download_speed'] > 1024:
                self.pad.addstr(" @ ")
                self.pad.addch(curses.ACS_PLMINUS)
                self.pad.addstr("%-5s " % scale_bytes(peer['download_speed']))
                self.pad.addch(curses.ACS_PLMINUS)
                self.pad.addstr("%-4s " % scale_time(peer['time_left']))
            else:
                self.pad.addstr("                ")

#            self.pad.addstr(clientname.ljust(clientname_width).encode('utf-8'))
            self.pad.addstr(peer['clientName'].ljust(clientname_width).encode('utf-8'))
            self.pad.addstr("  %15s  " % peer['address'])
            if features['geoip']:
                self.pad.addstr("  %2s     " % geo_ips[peer['address']])
            if features['dns']:
                self.pad.addstr(host_name.encode('utf-8'), curses.A_DIM)
            ypos += 1

    def draw_trackerlist(self, ypos):
        top = ypos - 1
        def addstr(ypos, xpos, *args):
            if ypos > top and ypos < self.height - 2:
                self.pad.addstr(ypos, xpos, *args)
        tlist = self.torrent_details['trackerStats']
        ypos -= self.scrollpos_detaillist % self.TRACKER_ITEM_HEIGHT
        start = self.scrollpos_detaillist / self.TRACKER_ITEM_HEIGHT
        tlist = tlist[start:]
        current_tier = -1
        for t in tlist:
            announce_msg_size = scrape_msg_size = 0

            if current_tier != t['tier']:
                current_tier = t['tier']
                addstr(ypos, 0, ("Tier %d" % (current_tier+1)).ljust(self.width), curses.A_REVERSE)
                ypos += 1

            addstr(ypos+1, 4,  "Last announce: %s" % timestamp(t['lastAnnounceTime']))
            addstr(ypos+1, 57, "  Last scrape: %s" % timestamp(t['lastScrapeTime']))

            if t['lastAnnounceSucceeded']:
                peers = "%s peer%s" % (num2str(t['lastAnnouncePeerCount']), ('s', '')[t['lastAnnouncePeerCount']==1])
                addstr(ypos,   2, t['announce'], curses.A_BOLD + curses.A_UNDERLINE)
                addstr(ypos+2, 11, "Result: ")
                addstr(ypos+2, 19, "%s received" % peers, curses.A_BOLD)
            else:
                addstr(ypos,   2, t['announce'], curses.A_UNDERLINE)
                addstr(ypos+2, 9, "Response:")
                announce_msg_size = self.wrap_and_draw_result(top, ypos+2, 19, t['lastAnnounceResult'])

            if t['lastScrapeSucceeded']:
                seeds   = "%s seed%s" % (num2str(t['seederCount']), ('s', '')[t['seederCount']==1])
                leeches = "%s leech%s" % (num2str(t['leecherCount']), ('es', '')[t['leecherCount']==1])
                addstr(ypos+2, 57, "Tracker knows: ")
                addstr(ypos+2, 72, "%s and %s" % (seeds, leeches), curses.A_BOLD)
            else:
                addstr(ypos+2, 62, "Response:")
                scrape_msg_size += self.wrap_and_draw_result(top, ypos+2, 72, t['lastScrapeResult'])

            ypos += max(announce_msg_size, scrape_msg_size)

            addstr(ypos+3, 4,  "Next announce: %s" % timestamp(t['nextAnnounceTime']))
            addstr(ypos+3, 57, "  Next scrape: %s" % timestamp(t['nextScrapeTime']))

            ypos += 5

    def wrap_and_draw_result(self, top, ypos, xpos, result):
        result = wrap(result, 30)
        i = 0
        for i, line in enumerate(result):
            if ypos+i > top and ypos+i < self.height - 2:
                self.pad.addstr(ypos+i, xpos, line, curses.A_UNDERLINE)
        return i


    def draw_pieces_map(self, ypos):
        pieces = self.torrent_details['pieces']
        piece_count = self.torrent_details['pieceCount']
        margin = len(str(piece_count)) + 2

        map_width = int(str(self.width-margin-1)[0:-1] + '0')
        for x in range(10, map_width, 10):
            self.pad.addstr(ypos, x+margin-1, str(x), curses.A_BOLD)

        start = self.scrollpos_detaillist * map_width
        end = min(start + (self.height - ypos - 3) * map_width, piece_count)
        if end <= start: return
        block = ord(pieces[start >> 3]) << (start & 7)

        format = "%%%dd" % (margin - 2)
        for counter in xrange(start, end):
            if counter % map_width == 0:
                ypos += 1 ; xpos = margin
                self.pad.addstr(ypos, 1, format % counter, curses.A_BOLD)
            else:
                xpos += 1

            if counter & 7 == 0:
                block = ord(pieces[counter >> 3])
            piece = block & 0x80
            if piece: self.pad.addch(ypos, xpos, ' ', curses.A_REVERSE)
            else:     self.pad.addch(ypos, xpos, '_')
            block <<= 1

        missing_pieces = piece_count - counter - 1
        if missing_pieces:
            line = "%d further piece%s" % (missing_pieces, ('','s')[missing_pieces>1])
            xpos = (self.width - len(line)) / 2
            self.pad.addstr(self.height-3, xpos, line, curses.A_REVERSE)

    def draw_details_list(self, ypos, info):
        key_width = max(map(lambda x: len(x[0]), info))
        for i in info:
            self.pad.addstr(ypos, 1, i[0].rjust(key_width).encode('utf-8')) # key
            # value part may be wrapped if it gets too long
            for v in i[1:]:
                y, x = self.pad.getyx()
                if x + len(v) >= self.width:
                    ypos += 1
                    self.pad.move(ypos, key_width+1)
                self.pad.addstr(v.encode('utf-8'))
            ypos += 1
        return ypos

    def next_details(self):
        if self.details_category_focus >= 4:
            self.details_category_focus = 0
        else:
            self.details_category_focus += 1
        self.focus_detaillist     = -1
        self.scrollpos_detaillist = 0
        self.pad.erase()

    def prev_details(self):
        if self.details_category_focus <= 0:
            self.details_category_focus = 4
        else:
            self.details_category_focus -= 1
        self.pad.erase()




    def move_up(self, focus, scrollpos, step_size):
        if focus < 0: focus = -1
        else:
            focus -= 1
            if scrollpos/step_size - focus > 0:
                scrollpos -= step_size
                scrollpos = max(0, scrollpos)
            while scrollpos % step_size:
                scrollpos -= 1
        return focus, scrollpos

    def move_down(self, focus, scrollpos, step_size, elements_per_page, list_height):
        if focus < list_height - 1:
            focus += 1
            if focus+1 - scrollpos/step_size > elements_per_page:
                scrollpos += step_size
        return focus, scrollpos

    def move_page_up(self, focus, scrollpos, step_size, elements_per_page):
        for x in range(elements_per_page - 1):
            focus, scrollpos = self.move_up(focus, scrollpos, step_size)
        if focus < 0: focus = 0
        return focus, scrollpos

    def move_page_down(self, focus, scrollpos, step_size, elements_per_page, list_height):
        if focus < 0: focus = 0
        for x in range(elements_per_page - 1):
            focus, scrollpos = self.move_down(focus, scrollpos, step_size, elements_per_page, list_height)
        return focus, scrollpos

    def move_to_top(self):
        return 0, 0

    def move_to_end(self, step_size, elements_per_page, list_height):
        focus     = list_height - 1
        scrollpos = max(0, (list_height - elements_per_page) * step_size)
        return focus, scrollpos





    def draw_stats(self):
        self.screen.insstr(self.height-1, 0, ' '.center(self.width), curses.A_REVERSE)
        self.draw_torrents_stats()
        self.draw_global_rates()

    def draw_torrents_stats(self):
        if self.selected_torrent > -1 and self.details_category_focus == 2:
            self.screen.insstr((self.height-1), 0,
                               "%d peer%s connected:" % (self.torrent_details['peersConnected'],
                                                         ('s','')[self.torrent_details['peersConnected'] == 1]) + \
                                   " Trackers: %-3d" % self.torrent_details['peersFrom']['fromTracker'] + \
                                   " DHT: %-3d" % self.torrent_details['peersFrom']['fromDht'] + \
                                   " LTEP: %-3d" % self.torrent_details['peersFrom']['fromLtep'] + \
                                   " PEX: %-3d" % self.torrent_details['peersFrom']['fromPex'] + \
                                   " Incoming: %-3d" % self.torrent_details['peersFrom']['fromIncoming'] + \
                                   " Cache: %-3d" % self.torrent_details['peersFrom']['fromCache'],
                               curses.A_REVERSE)
        else:
            self.screen.addstr((self.height-1), 0, "Torrent%s: " % ('s','')[len(self.torrents) == 1],
                                   curses.A_REVERSE)
            self.screen.addstr("%d (" % len(self.torrents), curses.A_REVERSE)

            downloading = len(filter(lambda x: x['status']==Transmission.STATUS_DOWNLOAD, self.torrents))
            seeding = len(filter(lambda x: x['status']==Transmission.STATUS_SEED, self.torrents))
            paused = self.stats['pausedTorrentCount']

            self.screen.addstr("Downloading: ", curses.A_REVERSE)
            self.screen.addstr("%d " % downloading, curses.A_REVERSE)
            self.screen.addstr("Seeding: ", curses.A_REVERSE)
            self.screen.addstr("%d " % seeding, curses.A_REVERSE)
            self.screen.addstr("Paused: ", curses.A_REVERSE)
            self.screen.addstr("%d) " % paused, curses.A_REVERSE)

            if self.filter_list:
                self.screen.addstr("Showing only: ", curses.A_REVERSE)
                self.screen.addstr("%s%s" % (('','not ')[self.filter_inverse], self.filter_list),
                                   curses.color_pair(10))

    def draw_global_rates(self):
        rates_width = self.rateDownload_width + self.rateUpload_width + 3

        if self.stats['alt-speed-enabled']:
            upload_limit   = "/%dK" % self.stats['alt-speed-up']
            download_limit = "/%dK" % self.stats['alt-speed-down']
        else:
            upload_limit   = ('', "/%dK" % self.stats['speed-limit-up'])[self.stats['speed-limit-up-enabled']]
            download_limit = ('', "/%dK" % self.stats['speed-limit-down'])[self.stats['speed-limit-down-enabled']]

        limits = {'dn_limit' : download_limit, 'up_limit' : upload_limit}
        limits_width = len(limits['dn_limit']) + len(limits['up_limit'])

        if self.stats['alt-speed-enabled']:
            self.screen.move(self.height-1, self.width-rates_width - limits_width - len('Turtle mode '))
            self.screen.addstr('Turtle mode', curses.A_REVERSE + curses.A_BOLD)
            self.screen.addch(' ', curses.A_REVERSE)

        self.screen.move(self.height - 1, self.width - rates_width - limits_width)
        self.screen.addch(curses.ACS_DARROW, curses.A_REVERSE)
        self.screen.addstr(scale_bytes(self.stats['downloadSpeed']).rjust(self.rateDownload_width),
                           curses.A_REVERSE + curses.A_BOLD + curses.color_pair(1))
        self.screen.addstr(limits['dn_limit'], curses.A_REVERSE)
        self.screen.addch(' ', curses.A_REVERSE)
        self.screen.addch(curses.ACS_UARROW, curses.A_REVERSE)
        self.screen.insstr(limits['up_limit'], curses.A_REVERSE)
        self.screen.insstr(scale_bytes(self.stats['uploadSpeed']).rjust(self.rateUpload_width),
                           curses.A_REVERSE + curses.A_BOLD + curses.color_pair(2))


    def draw_title_bar(self):
        self.screen.insstr(0, 0, ' '.center(self.width), curses.A_REVERSE)
        self.draw_connection_status()
        self.draw_quick_help()
    def draw_connection_status(self):
        status = "Transmission @ %s:%s" % (self.server.host, self.server.port)
        if cmd_args.DEBUG:
            status = "%d x %d " % (self.width, self.height) + status 
        self.screen.addstr(0, 0, status.encode('utf-8'), curses.A_REVERSE)

    def draw_quick_help(self):
        help = [('?','Show Keybindings')]

        if self.selected_torrent == -1:
            if self.focus >= 0:
                help = [('enter','View Details'), ('p','Pause/Unpause'), ('r','Remove'), ('v','Verify')]
            else:
                help = [('f','Filter'), ('s','Sort')] + help + [('o','Options'), ('q','Quit')]
        else:
            help = [('Move with','cursor keys'), ('q','Back to List')]
            if self.details_category_focus == 1 and self.focus_detaillist > -1:
                help = [('space','(De)Select File'),
                        ('left/right','De-/Increase Priority'),
                        ('escape','Unfocus/-select')] + help
            elif self.details_category_focus == 2:
                help = [('F1/?','Explain flags')] + help

        line = ' | '.join(map(lambda x: "%s %s" % (x[0], x[1]), help))
        line = line[0:self.width]
        self.screen.insstr(0, self.width-len(line), line, curses.A_REVERSE)


    def list_key_bindings(self):
        message = "           F1/?  Show this help\n" + \
                  "            u/d  Adjust maximum global upload/download rate\n" + \
                  "            U/D  Adjust maximum upload/download rate for focused torrent\n" + \
                  "            +/-  Adjust bandwidth priority for focused torrent\n" + \
                  "              p  Pause/Unpause torrent\n" + \
                  "              P  Pause/Unpause all torrents\n" + \
                  "            v/y  Verify torrent\n" + \
                  "              m  Move torrent\n" + \
                  "              n  Reannounce torrent\n" + \
                  "              a  Add torrent\n" + \
                  "          Del/r  Remove torrent and keep content\n" + \
                  "    Shift+Del/R  Remove torrent and delete content\n"
        if self.selected_torrent == -1:
            message += "              f  Filter torrent list\n" + \
                       "              s  Sort torrent list\n" \
                       "    Enter/Right  View torrent's details\n" + \
                       "              o  Configuration options\n" + \
                       "              t  Toggle turtle mode\n" + \
                       "            Esc  Unfocus\n" + \
                       "              q  Quit"
        else:
            if self.details_category_focus == 2:  # peers
                message = " O  Optimistic unchoke\n" + \
                          " D  Downloading from this peer\n" + \
                          " d  We would download from this peer if they'd let us\n" + \
                          " U  Uploading to peer\n" + \
                          " u  We would upload to this peer if they'd ask\n" + \
                          " K  Peer has unchoked us, but we're not interested\n" + \
                          " ?  We unchoked this peer, but they're not interested\n" + \
                          " E  Encrypted Connection\n" + \
                          " H  Peer was discovered through DHT\n" + \
                          " X  Peer was discovered through Peer Exchange (PEX)\n" + \
                          " I  Peer is an incoming connection"
            else:
                message += "              o  Jump to overview\n" + \
                           "              f  Jump to file list\n" + \
                           "              e  Jump to peer list\n" + \
                           "              t  Jump to tracker information\n" + \
                           "      Tab/Right  Jump to next view\n" + \
                           " Shift+Tab/Left  Jump to previous view\n"
                if self.details_category_focus == 1:  # files
                    if self.focus_detaillist > -1:
                        message += "     Left/Right  Decrease/Increase file priority\n"
                    message += "        Up/Down  Select file\n" + \
                               "          Space  Select/Deselect focused file\n" + \
                               "              a  Select/Deselect all files\n" + \
                               "            Esc  Unfocus+Unselect or Back to torrent list\n" + \
                               "    q/Backspace  Back to torrent list"
                else:
                    message += "q/Backspace/Esc  Back to torrent list"

        width  = max(map(lambda x: len(x), message.split("\n"))) + 4
        width  = min(self.width, width)
        height = min(self.height, message.count("\n")+3)
        win = self.window(height, width, message=message)
        while True:
            if win.getch() >= 0: return



    def window(self, height, width, message=''):
        height = min(self.height, height)
        width  = min(self.width, width)
        ypos = (self.height - height)/2
        xpos = (self.width  - width)/2
        win = curses.newwin(height, width, ypos, xpos)
        win.box()
        win.bkgd(' ', curses.A_REVERSE + curses.A_BOLD)

        if width >= 20:
            win.addch( height-1, width-19, curses.ACS_RTEE)
            win.addstr(height-1, width-18, " Close with Esc ")
            win.addch( height-1, width-2, curses.ACS_LTEE)

        ypos = 1
        for line in message.split("\n"):
            if len(line) > width:
                line = line[0:width-7] + '...'
            win.addstr(ypos, 2, line.encode('utf-8'))
            ypos += 1
        return win


    def dialog_ok(self, message):
        height = 3 + message.count("\n")
        width  = max(max(map(lambda x: len(x), message.split("\n"))), 40) + 4
        win = self.window(height, width, message=message)
        while True:
            if win.getch() >= 0: return


    def dialog_yesno(self, message, important=False):
        height = 5 + message.count("\n")
        width  = max(len(message), 8) + 4
        win = self.window(height, width, message=message)
        win.keypad(True)

        if important:
            win.bkgd(' ', curses.color_pair(11) + curses.A_REVERSE)

        focus_tags   = curses.color_pair(9)
        unfocus_tags = 0

        input = False
        while True:
            win.move(height-2, (width/2)-4)
            if input:
                win.addstr('Y',  focus_tags + curses.A_UNDERLINE)
                win.addstr('es', focus_tags)
                win.addstr('   ')
                win.addstr('N',  curses.A_UNDERLINE)
                win.addstr('o')
            else:
                win.addstr('Y', curses.A_UNDERLINE)
                win.addstr('es')
                win.addstr('   ')
                win.addstr('N',  focus_tags + curses.A_UNDERLINE)
                win.addstr('o', focus_tags)

            c = win.getch()
            if c == ord('y'):
                return True
            elif c == ord('n'):
                return False
            elif c == ord("\t"):
                input = not input
            elif c == curses.KEY_LEFT or c == ord('h'):
                input = True
            elif c == curses.KEY_RIGHT or c == ord('l'):
                input = False
            elif c == ord("\n") or c == ord(' '):
                return input
            elif c == 27 or c == curses.KEY_BREAK:
                return -1

    def dialog_input_text(self, message, input=''):
        width  = self.width - 4
        height = message.count("\n") + 4

        win = self.window(height, width, message=message)
        win.keypad(True)

        index = len(input)
        while True:
            win.addstr(height - 2, 2, input.ljust(width - 4), curses.color_pair(5))
            win.addch(height - 2, index + 2, str(index < len(input) and input[index] or ' '))
            c = win.getch()
            if c == 27 or c == curses.KEY_BREAK:
                return ''
            elif c == curses.KEY_RIGHT and index < len(input):
                index += 1
            elif c == curses.KEY_LEFT and index > 0:
                index -= 1
            elif c == curses.KEY_BACKSPACE and index > 0:
                input = input[:index - 1] + (index < len(input) and input[index:] or '')
                index -= 1
            elif c == curses.KEY_DC and index < len(input):
                input = input[:index] + input[index + 1:]
            elif c == ord('\n'):
                return input
            elif c >= 32 and c < 127 and len(input) + 1 < self.width - 7:
                input = input[:index] + chr(c) + (index < len(input) and input[index:] or '')
                index += 1

    def dialog_input_number(self, message, current_value, cursorkeys=True, floating_point=False):
        width  = max(max(map(lambda x: len(x), message.split("\n"))), 40) + 4
        width  = min(self.width, width)
        height = message.count("\n") + (4,6)[cursorkeys]

        win = self.window(height, width, message=message)
        win.keypad(True)
        input = str(current_value)
        if cursorkeys:
            if floating_point:
                bigstep   = 1
                smallstep = 0.1
            else:
                bigstep   = 100
                smallstep = 10
            win.addstr(height-4, 2, ("   up/down +/- %-3s" % bigstep).rjust(width-4))
            win.addstr(height-3, 2, ("left/right +/- %3s" % smallstep).rjust(width-4))
            win.addstr(height-3, 2, "0 means unlimited")

        while True:
            win.addstr(height-2, 2, input.ljust(width-4), curses.color_pair(5))
            win.addch(height-2, len(input)+2, ' ')
            c = win.getch()
            if c == 27 or c == ord('q') or c == curses.KEY_BREAK:
                return -1
            elif c == ord("\n"):
                try:
                    if floating_point: return float(input)
                    else:              return int(input)
                except ValueError:
                    return -1

            elif c == curses.KEY_BACKSPACE or c == curses.KEY_DC or c == 127 or c == 8:
                input = input[:-1]
            elif len(input) >= width-5:
                curses.beep()
            elif c >= ord('0') and c <= ord('9'):
                input += chr(c)
            elif c == ord('.') and floating_point:
                input += chr(c)

            elif cursorkeys and c != -1:
                try:
                    if floating_point: number = float(input)
                    else:              number = int(input)
                    if number <= 0: number = 0
                    if c == curses.KEY_LEFT or c == ord('h'):    number -= smallstep
                    elif c == curses.KEY_RIGHT or c == ord('l'): number += smallstep
                    elif c == curses.KEY_DOWN or c == ord('j'):  number -= bigstep
                    elif c == curses.KEY_UP or c == ord('k'):    number += bigstep
                    if number <= 0: number = 0
                    input = str(number)
                except ValueError:
                    pass


    def dialog_menu(self, title, options, focus=1):
        height = len(options) + 2
        width  = max(max(map(lambda x: len(x[1])+3, options)), len(title)+3)
        win = self.window(height, width)

        win.addstr(0,1, title)
        win.keypad(True)

        old_focus = focus
        while True:
            keymap = self.dialog_list_menu_options(win, width, options, focus)
            c = win.getch()

            if c > 96 and c < 123 and chr(c) in keymap:
                return options[keymap[chr(c)]][0]
            elif c == 27 or c == ord('q'):
                return options[old_focus-1][0]
            elif c == ord("\n"):
                return options[focus-1][0]
            elif c == curses.KEY_DOWN or c == ord('j'):
                focus += 1
                if focus > len(options): focus = 1
            elif c == curses.KEY_UP or c == ord('k'):
                focus -= 1
                if focus < 1: focus = len(options)
            elif c == curses.KEY_HOME:
                focus = 1
            elif c == curses.KEY_END:
                focus = len(options)

    def dialog_list_menu_options(self, win, width, options, focus):
        keys = dict()
        i = 1
        for option in options:
            title = option[1].split('_')
            if i == focus: tag = curses.color_pair(5)
            else:          tag = 0
            win.addstr(i,2, title[0], tag)
            win.addstr(title[1][0], tag + curses.A_UNDERLINE)
            win.addstr(title[1][1:], tag)
            win.addstr(''.ljust(width - len(option[1]) - 3), tag)

            keys[title[1][0].lower()] = i-1
            i+=1
        return keys


    def draw_options_dialog(self):
        enc_options = [('required','_required'), ('preferred','_preferred'), ('tolerated','_tolerated')]

        while True:
            options = [('Peer _Port', "%d" % self.stats['peer-port']),
                       ('UP_nP/NAT-PMP', ('disabled','enabled ')[self.stats['port-forwarding-enabled']]),
                       ('Peer E_xchange', ('disabled','enabled ')[self.stats['pex-enabled']]),
                       ('_Distributed Hash Table', ('disabled','enabled ')[self.stats['dht-enabled']]),
                       ('_Local Peer Discovery', ('disabled','enabled ')[self.stats['lpd-enabled']]),
                       ('_Global Peer Limit', "%d" % self.stats['peer-limit-global']),
                       ('Peer Limit per _Torrent', "%d" % self.stats['peer-limit-per-torrent']),
                       ('Protocol En_cryption', "%s" % self.stats['encryption']),
                       ('_Seed Ratio Limit', "%s" % ('unlimited',self.stats['seedRatioLimit'])[self.stats['seedRatioLimited']])]
            max_len = max([sum([len(re.sub('_', '', x)) for x in y[0]]) for y in options])
            win = self.window(len(options)+2, max_len+15)
            win.addstr(0, 2, 'Global Options')

            line_num = 1
            for option in options:
                parts = re.split('_', option[0])
                parts_len = sum([len(x) for x in parts])

                win.addstr(line_num, max_len-parts_len+2, parts.pop(0))
                for part in parts:
                    win.addstr(part[0], curses.A_UNDERLINE)
                    win.addstr(part[1:] + ': ' + option[1])
                line_num += 1

            c = win.getch()
            if c == 27 or c == ord('q') or c == ord("\n"):
                return

            elif c == ord('p'):
                port = self.dialog_input_number("Port for incoming connections",
                                                self.stats['peer-port'], cursorkeys=False)
                if port >= 0: self.server.set_option('peer-port', port)
            elif c == ord('n'):
                self.server.set_option('port-forwarding-enabled',
                                       (1,0)[self.stats['port-forwarding-enabled']])
            elif c == ord('x'):
                self.server.set_option('pex-enabled', (1,0)[self.stats['pex-enabled']])
            elif c == ord('d'):
                self.server.set_option('dht-enabled', (1,0)[self.stats['dht-enabled']])
            elif c == ord('l'):
                self.server.set_option('lpd-enabled', (1,0)[self.stats['lpd-enabled']])
            elif c == ord('g'):
                limit = self.dialog_input_number("Maximum number of connected peers",
                                                 self.stats['peer-limit-global'])
                if limit >= 0: self.server.set_option('peer-limit-global', limit)
            elif c == ord('t'):
                limit = self.dialog_input_number("Maximum number of connected peers per torrent",
                                                 self.stats['peer-limit-per-torrent'])
                if limit >= 0: self.server.set_option('peer-limit-per-torrent', limit)
            elif c == ord('s'):
                limit = self.dialog_input_number('Stop seeding with upload/download ratio',
                                                 (0,self.stats['seedRatioLimit'])[self.stats['seedRatioLimited']],
                                                 floating_point=True)
                if limit > 0:
                    self.server.set_option('seedRatioLimit', limit)
                    self.server.set_option('seedRatioLimited', True)
                elif limit == 0:
                    self.server.set_option('seedRatioLimited', False)
            elif c == ord('c'):
                choice = self.dialog_menu('Encryption', enc_options,
                                          map(lambda x: x[0]==self.stats['encryption'], enc_options).index(True)+1)
                self.server.set_option('encryption', choice)

            self.draw_torrent_list()

# End of class Interface



def percent(full, part):
    try: percent = 100/(float(full) / float(part))
    except ZeroDivisionError: percent = 0.0
    return percent


def scale_time(seconds, type='short'):
    minute_in_sec = float(60)
    hour_in_sec   = float(3600)
    day_in_sec    = float(86400)
    month_in_sec  = 27.321661 * day_in_sec # from wikipedia
    year_in_sec   = 365.25    * day_in_sec # from wikipedia

    if seconds < 0:
        return ('?', 'some time')[type=='long']

    elif seconds < minute_in_sec:
        if type == 'long':
            if seconds < 5:
                return 'now'
            else:
                return "%d second%s" % (seconds, ('', 's')[seconds>1])
        else:
            return "%ds" % seconds

    elif seconds < hour_in_sec:
        minutes = round(seconds / minute_in_sec, 0)
        if type == 'long':
            return "%d minute%s" % (minutes, ('', 's')[minutes>1])
        else:
            return "%dm" % minutes

    elif seconds < day_in_sec:
        hours = round(seconds / hour_in_sec, 0)
        if type == 'long':
            return "%d hour%s" % (hours, ('', 's')[hours>1])
        else:
            return "%dh" % hours

    elif seconds < month_in_sec:
        days = round(seconds / day_in_sec, 0)
        if type == 'long':
            return "%d day%s" % (days, ('', 's')[days>1])
        else:
            return "%dd" % days

    elif seconds < year_in_sec:
        months = round(seconds / month_in_sec, 0)
        if type == 'long':
            return "%d month%s" % (months, ('', 's')[months>1])
        else:
            return "%dM" % months

    else:
        years = round(seconds / year_in_sec, 0)
        if type == 'long':
            return "%d year%s" % (years, ('', 's')[years>1])
        else:
            return "%dy" % years


def timestamp(timestamp):
    if timestamp < 1:
        return 'never'

    date_format = "%x %X"
    absolute = time.strftime(date_format, time.localtime(timestamp))
    if timestamp > time.time():
        relative = 'in ' + scale_time(int(timestamp - time.time()), 'long')
    else:
        relative = scale_time(int(time.time() - timestamp), 'long') + ' ago'

    if relative.startswith('now') or relative.endswith('now'):
        relative = 'now'
    return "%s (%s)" % (absolute, relative)


def scale_bytes(bytes, type='short'):
    if bytes >= 1073741824:
        scaled_bytes = round((bytes / 1073741824.0), 2)
        unit = 'G'
    elif bytes >= 1048576:
        scaled_bytes = round((bytes / 1048576.0), 1)
        if scaled_bytes >= 100:
            scaled_bytes = int(scaled_bytes)
        unit = 'M'
    elif bytes >= 1024:
        scaled_bytes = int(bytes / 1024)
        unit = 'K'
    else:
        scaled_bytes = round((bytes / 1024.0), 1)
        unit = 'K'


    # handle 0 bytes special
    if bytes == 0 and type == 'long':
        return 'nothing'

    # convert to integer if .0
    if int(scaled_bytes) == float(scaled_bytes):
        scaled_bytes = str(int(scaled_bytes))
    else:
        scaled_bytes = str(scaled_bytes).rstrip('0')

    if type == 'long':
        return num2str(bytes) + ' [' + scaled_bytes + unit + ']'
    else:
        return scaled_bytes + unit


def homedir2tilde(path):
    return re.sub(r'^'+os.environ['HOME'], '~', path)
def tilde2homedir(path):
    return re.sub(r'^~', os.environ['HOME'], path)

def html2text(str):
    str = re.sub(r'</h\d+>', "\n", str)
    str = re.sub(r'</p>', ' ', str)
    str = re.sub(r'<[^>]*?>', '', str)
    return str

def num2str(num):
    if int(num) == -1:
        return '?'
    elif int(num) == -2:
        return 'oo'
    else:
        string = re.sub(r'(\d{3})', '\g<1>,', str(num)[::-1])[::-1]
        return string.lstrip(',')

# def middlecut(string, width):
#     return string[0:(width/2)-2] + '..' + string[len(string) - (width/2) :]

def debug(data):
    if cmd_args.DEBUG:
        file = open("debug.log", 'a')
        if type(data) == type(str()):
            file.write(data.encode('utf-8'))
        else:
            import pprint
            pp = pprint.PrettyPrinter(indent=4)
            file.write("\n====================\n" + pp.pformat(data) + "\n====================\n\n")
        file.close

def quit(msg='', exitcode=0):
    try:
        curses.endwin()
    except curses.error:
        pass

    # if this is a graceful exit and config file is present
    if not msg and not exitcode and os.path.isfile(cmd_args.configfile):
        try:
            config.write(open(cmd_args.configfile, 'w'))
            os.chmod(cmd_args.configfile, 0600)
        except IOError, msg:
            print >> sys.stderr, "Cannot write config file %s:\n%s" % (cmd_args.configfile, msg)
    else:
        print >> sys.stderr, msg,
    os._exit(exitcode)


def explode_connection_string(connection):
    host, port = config.get('Connection', 'host'), config.getint('Connection', 'port')
    username, password = config.get('Connection', 'username'), config.get('Connection', 'password')
    try:
        if connection.count('@') == 1:
            auth, connection = connection.split('@')
            if auth.count(':') == 1:
                username, password = auth.split(':')
        if connection.count(':') == 1:
            host, port = connection.split(':')
            port = int(port)
        else:
            host = connection
    except ValueError:
        quit("Wrong connection pattern: %s\n" % connection)
    return host, port, username, password


# create initial config file
def create_config(option, opt_str, value, parser):
    configfile = parser.values.configfile
    config.read(configfile)
    if parser.values.connection:
        host, port, username, password = explode_connection_string(parser.values.connection)
        config.set('Connection', 'host', host)
        config.set('Connection', 'port', str(port))
        config.set('Connection', 'username', username)
        config.set('Connection', 'password', password)

    # create directory
    dir = os.path.dirname(configfile)
    if dir != '' and not os.path.isdir(dir):
        try:
            os.makedirs(dir)
        except OSError, msg:
            print msg
            exit(CONFIGFILE_ERROR)

    # create config file
    try:
        config.write(open(configfile, 'w'))
        os.chmod(configfile, 0600)
    except IOError, msg:
        print msg
        exit(CONFIGFILE_ERROR)

    print "Wrote config file %s" % configfile
    exit(0)

# command line parameters
default_config_path = os.environ['HOME'] + '/.config/transmission-remote-cli/settings.cfg'
parser = OptionParser(usage="%prog [options] [-- transmission-remote options]",
                      version="%%prog %s" % VERSION,
                      description="%%prog %s" % VERSION)
parser.add_option("--debug", action="store_true", dest="DEBUG", default=False, help=SUPPRESS_HELP)
parser.add_option("-c", "--connect", action="store", dest="connection", default="",
                  help="Point to the server using pattern [username:password@]host[:port]")
parser.add_option("-f", "--config", action="store", dest="configfile", default=default_config_path,
                  help="Path to configuration file.")
parser.add_option("--create-config", action="callback", callback=create_config,
                  help="Create configuration file CONFIGFILE with default values.")
(cmd_args, transmissionremote_args) = parser.parse_args()


# read config from config file
config.read(cmd_args.configfile)

# command line connection data can override config file
if cmd_args.connection:
    host, port, username, password = explode_connection_string(cmd_args.connection)
    config.set('Connection', 'host', host)
    config.set('Connection', 'port', str(port))
    config.set('Connection', 'username', username)
    config.set('Connection', 'password', password)


# forward arguments after '--' to transmission-remote
if transmissionremote_args:
    cmd = ['transmission-remote', '%s:%s' %
           (config.get('Connection', 'host'), config.get('Connection', 'port'))]

    # one argument and it doesn't start with '-' --> treat it like it's a torrent link/url
    if len(transmissionremote_args) == 1 and not transmissionremote_args[0].startswith('-'):
        cmd.extend(['-a', transmissionremote_args[0]])
    else:
        cmd.extend(transmissionremote_args)

    if config.get('Connection', 'username') and config.get('Connection', 'password'):
        cmd_print = cmd
        cmd_print.extend(['--auth', '%s:PASSWORD' % config.get('Connection', 'username')])
        print "EXECUTING:\n%s\nRESPONSE:" % ' '.join(cmd_print)
        cmd.extend(['--auth', '%s:%s' % (config.get('Connection', 'username'), config.get('Connection', 'password'))])
    else:
        print "EXECUTING:\n%s\nRESPONSE:" % ' '.join(cmd)

    try:
        retcode = call(cmd)
    except OSError, msg:
        quit("Could not execute the above command: %s\n" % msg, 128)
    quit('', retcode)

# run interface
ui = Interface(Transmission(config.get('Connection', 'host'),
                            config.getint('Connection', 'port'),
                            config.get('Connection', 'username'),
                            config.get('Connection', 'password')))
