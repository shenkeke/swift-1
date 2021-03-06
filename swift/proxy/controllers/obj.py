# Copyright (c) 2010-2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# NOTE: swift_conn
# You'll see swift_conn passed around a few places in this file. This is the
# source bufferedhttp connection of whatever it is attached to.
#   It is used when early termination of reading from the connection should
# happen, such as when a range request is satisfied but there's still more the
# source connection would like to send. To prevent having to read all the data
# that could be left, the source connection can be .close() and then reads
# commence to empty out any buffers.
#   These shenanigans are to ensure all related objects can be garbage
# collected. We've seen objects hang around forever otherwise.

import itertools
import mimetypes
import time
import math
from swift import gettext_ as _
from urllib import unquote, quote

from eventlet import GreenPile
from eventlet.queue import Queue
from eventlet.timeout import Timeout

from swift.common.utils import (
    clean_content_type, config_true_value, ContextPool, csv_append,
    GreenAsyncPile, GreenthreadSafeIterator, json, Timestamp,
    normalize_delete_at_timestamp, public, quorum_size, get_expirer_container)
from swift.common.bufferedhttp import http_connect
from swift.common.constraints import check_metadata, check_object_creation, \
    check_copy_from_header, check_destination_header, \
    check_account_format
from swift.common import constraints
from swift.common.exceptions import ChunkReadTimeout, \
    ChunkWriteTimeout, ConnectionTimeout, ListingIterNotFound, \
    ListingIterNotAuthorized, ListingIterError
from swift.common.http import (
    is_success, is_client_error, is_server_error, HTTP_CONTINUE, HTTP_CREATED,
    HTTP_MULTIPLE_CHOICES, HTTP_NOT_FOUND, HTTP_INTERNAL_SERVER_ERROR,
    HTTP_SERVICE_UNAVAILABLE, HTTP_INSUFFICIENT_STORAGE,
    HTTP_PRECONDITION_FAILED, HTTP_CONFLICT)
from swift.common.storage_policy import POLICIES
from swift.proxy.controllers.base import Controller, delay_denial, \
    cors_validation
from swift.common.swob import HTTPAccepted, HTTPBadRequest, HTTPNotFound, \
    HTTPPreconditionFailed, HTTPRequestEntityTooLarge, HTTPRequestTimeout, \
    HTTPServerError, HTTPServiceUnavailable, Request, \
    HTTPClientDisconnect, HeaderKeyDict, HTTPException
from swift.common.request_helpers import is_sys_or_user_meta, is_sys_meta, \
    remove_items, copy_header_subset


def copy_headers_into(from_r, to_r):
    """
    Will copy desired headers from from_r to to_r
    :params from_r: a swob Request or Response
    :params to_r: a swob Request or Response
    """
    pass_headers = ['x-delete-at']
    for k, v in from_r.headers.items():
        if is_sys_or_user_meta('object', k) or k.lower() in pass_headers:
            to_r.headers[k] = v


def check_content_type(req):
    if not req.environ.get('swift.content_type_overridden') and \
            ';' in req.headers.get('content-type', ''):
        for param in req.headers['content-type'].split(';')[1:]:
            if param.lstrip().startswith('swift_'):
                return HTTPBadRequest("Invalid Content-Type, "
                                      "swift_* is not a valid parameter name.")
    return None


class ObjectController(Controller):
    """WSGI controller for object requests."""
    server_type = 'Object'

    def __init__(self, app, account_name, container_name, object_name,
                 **kwargs):
        Controller.__init__(self, app)
        self.account_name = unquote(account_name)
        self.container_name = unquote(container_name)
        self.object_name = unquote(object_name)

    def _listing_iter(self, lcontainer, lprefix, env):
        for page in self._listing_pages_iter(lcontainer, lprefix, env):
            for item in page:
                yield item

    def _listing_pages_iter(self, lcontainer, lprefix, env):
        lpartition = self.app.container_ring.get_part(
            self.account_name, lcontainer)
        marker = ''
        while True:
            lreq = Request.blank('i will be overridden by env', environ=env)
            # Don't quote PATH_INFO, by WSGI spec
            lreq.environ['PATH_INFO'] = \
                '/v1/%s/%s' % (self.account_name, lcontainer)
            lreq.environ['REQUEST_METHOD'] = 'GET'
            lreq.environ['QUERY_STRING'] = \
                'format=json&prefix=%s&marker=%s' % (quote(lprefix),
                                                     quote(marker))
            lresp = self.GETorHEAD_base(
                lreq, _('Container'), self.app.container_ring, lpartition,
                lreq.swift_entity_path)
            if 'swift.authorize' in env:
                lreq.acl = lresp.headers.get('x-container-read')
                aresp = env['swift.authorize'](lreq)
                if aresp:
                    raise ListingIterNotAuthorized(aresp)
            if lresp.status_int == HTTP_NOT_FOUND:
                raise ListingIterNotFound()
            elif not is_success(lresp.status_int):
                raise ListingIterError()
            if not lresp.body:
                break
            sublisting = json.loads(lresp.body)
            if not sublisting:
                break
            marker = sublisting[-1]['name'].encode('utf-8')
            yield sublisting

    def iter_nodes_local_first(self, ring, partition):
        """
        Yields nodes for a ring partition.

        If the 'write_affinity' setting is non-empty, then this will yield N
        local nodes (as defined by the write_affinity setting) first, then the
        rest of the nodes as normal. It is a re-ordering of the nodes such
        that the local ones come first; no node is omitted. The effect is
        that the request will be serviced by local object servers first, but
        nonlocal ones will be employed if not enough local ones are available.

        :param ring: ring to get nodes from
        :param partition: ring partition to yield nodes for
        """

        is_local = self.app.write_affinity_is_local_fn
        if is_local is None:
            return self.app.iter_nodes(ring, partition)

        primary_nodes = ring.get_part_nodes(partition)
        num_locals = self.app.write_affinity_node_count(len(primary_nodes))

        all_nodes = itertools.chain(primary_nodes,
                                    ring.get_more_nodes(partition))
        first_n_local_nodes = list(itertools.islice(
            itertools.ifilter(is_local, all_nodes), num_locals))

        # refresh it; it moved when we computed first_n_local_nodes
        all_nodes = itertools.chain(primary_nodes,
                                    ring.get_more_nodes(partition))
        local_first_node_iter = itertools.chain(
            first_n_local_nodes,
            itertools.ifilter(lambda node: node not in first_n_local_nodes,
                              all_nodes))

        return self.app.iter_nodes(
            ring, partition, node_iter=local_first_node_iter)

    def GETorHEAD(self, req):
        """Handle HTTP GET or HEAD requests."""
        container_info = self.container_info(
            self.account_name, self.container_name, req)
        req.acl = container_info['read_acl']
        # pass the policy index to storage nodes via req header
        policy_index = req.headers.get('X-Backend-Storage-Policy-Index',
                                       container_info['storage_policy'])
        obj_ring = self.app.get_object_ring(policy_index)
        req.headers['X-Backend-Storage-Policy-Index'] = policy_index
        if 'swift.authorize' in req.environ:
            aresp = req.environ['swift.authorize'](req)
            if aresp:
                return aresp
        partition = obj_ring.get_part(
            self.account_name, self.container_name, self.object_name)
        resp = self.GETorHEAD_base(
            req, _('Object'), obj_ring, partition,
            req.swift_entity_path)

        if ';' in resp.headers.get('content-type', ''):
            resp.content_type = clean_content_type(
                resp.headers['content-type'])
        return resp

    @public
    @cors_validation
    @delay_denial
    def GET(self, req):
        """Handler for HTTP GET requests."""
        return self.GETorHEAD(req)

    @public
    @cors_validation
    @delay_denial
    def HEAD(self, req):
        """Handler for HTTP HEAD requests."""
        return self.GETorHEAD(req)

    @public
    @cors_validation
    @delay_denial
    def POST(self, req):
        """HTTP POST request handler."""
        if self.app.object_post_as_copy:
            req.method = 'PUT'
            req.path_info = '/v1/%s/%s/%s' % (
                self.account_name, self.container_name, self.object_name)
            req.headers['Content-Length'] = 0
            req.headers['X-Copy-From'] = quote('/%s/%s' % (self.container_name,
                                               self.object_name))
            req.headers['X-Fresh-Metadata'] = 'true'
            req.environ['swift_versioned_copy'] = True
            if req.environ.get('QUERY_STRING'):
                req.environ['QUERY_STRING'] += '&multipart-manifest=get'
            else:
                req.environ['QUERY_STRING'] = 'multipart-manifest=get'
            resp = self.PUT(req)
            # Older editions returned 202 Accepted on object POSTs, so we'll
            # convert any 201 Created responses to that for compatibility with
            # picky clients.
            if resp.status_int != HTTP_CREATED:
                return resp
            return HTTPAccepted(request=req)
        else:
            error_response = check_metadata(req, 'object')
            if error_response:
                return error_response
            container_info = self.container_info(
                self.account_name, self.container_name, req)
            container_partition = container_info['partition']
            containers = container_info['nodes']
            req.acl = container_info['write_acl']
            if 'swift.authorize' in req.environ:
                aresp = req.environ['swift.authorize'](req)
                if aresp:
                    return aresp
            if not containers:
                return HTTPNotFound(request=req)

            req, delete_at_container, delete_at_part, \
                delete_at_nodes = self._config_obj_expiration(req)

            # pass the policy index to storage nodes via req header
            policy_index = req.headers.get('X-Backend-Storage-Policy-Index',
                                           container_info['storage_policy'])
            obj_ring = self.app.get_object_ring(policy_index)
            req.headers['X-Backend-Storage-Policy-Index'] = policy_index
            partition, nodes = obj_ring.get_nodes(
                self.account_name, self.container_name, self.object_name)

            req.headers['X-Timestamp'] = Timestamp(time.time()).internal

            headers = self._backend_requests(
                req, len(nodes), container_partition, containers,
                delete_at_container, delete_at_part, delete_at_nodes)

            resp = self.make_requests(req, obj_ring, partition,
                                      'POST', req.swift_entity_path, headers)
            return resp

    def _backend_requests(self, req, n_outgoing,
                          container_partition, containers,
                          delete_at_container=None, delete_at_partition=None,
                          delete_at_nodes=None):
        headers = [self.generate_request_headers(req, additional=req.headers)
                   for _junk in range(n_outgoing)]

        for i, container in enumerate(containers):
            i = i % len(headers)

            headers[i]['X-Container-Partition'] = container_partition
            headers[i]['X-Container-Host'] = csv_append(
                headers[i].get('X-Container-Host'),
                '%(ip)s:%(port)s' % container)
            headers[i]['X-Container-Device'] = csv_append(
                headers[i].get('X-Container-Device'),
                container['device'])

        for i, node in enumerate(delete_at_nodes or []):
            i = i % len(headers)

            headers[i]['X-Delete-At-Container'] = delete_at_container
            headers[i]['X-Delete-At-Partition'] = delete_at_partition
            headers[i]['X-Delete-At-Host'] = csv_append(
                headers[i].get('X-Delete-At-Host'),
                '%(ip)s:%(port)s' % node)
            headers[i]['X-Delete-At-Device'] = csv_append(
                headers[i].get('X-Delete-At-Device'),
                node['device'])

        return headers

    def _send_file(self, conn, path):
        """Method for a file PUT coro"""
        while True:
            chunk = conn.queue.get()
            if not conn.failed:
                try:
                    with ChunkWriteTimeout(self.app.node_timeout):
                        conn.send(chunk)
                except (Exception, ChunkWriteTimeout):
                    conn.failed = True
                    self.app.exception_occurred(
                        conn.node, _('Object'),
                        _('Trying to write to %s') % path)
            conn.queue.task_done()

    def _connect_put_node(self, nodes, part, path, headers,
                          logger_thread_locals):
        """
        Make a connection for a replicated object.

        Connects to the first working node that it finds in node_iter
        and sends over the request headers. Returns an HTTPConnection
        object to handle the rest of the streaming.
        """
        self.app.logger.thread_locals = logger_thread_locals
        for node in nodes:
            try:
                start_time = time.time()
                with ConnectionTimeout(self.app.conn_timeout):
                    conn = http_connect(
                        node['ip'], node['port'], node['device'], part, 'PUT',
                        path, headers)
                self.app.set_node_timing(node, time.time() - start_time)
                with Timeout(self.app.node_timeout):
                    resp = conn.getexpect()
                if resp.status == HTTP_CONTINUE:
                    conn.resp = None
                    conn.node = node
                    return conn
                elif is_success(resp.status) or resp.status == HTTP_CONFLICT:
                    conn.resp = resp
                    conn.node = node
                    return conn
                elif headers['If-None-Match'] is not None and \
                        resp.status == HTTP_PRECONDITION_FAILED:
                    conn.resp = resp
                    conn.node = node
                    return conn
                elif resp.status == HTTP_INSUFFICIENT_STORAGE:
                    self.app.error_limit(node, _('ERROR Insufficient Storage'))
                elif is_server_error(resp.status):
                    self.app.error_occurred(
                        node,
                        _('ERROR %(status)d Expect: 100-continue '
                          'From Object Server') % {
                              'status': resp.status})
            except (Exception, Timeout):
                self.app.exception_occurred(
                    node, _('Object'),
                    _('Expect: 100-continue on %s') % path)

    def _await_response(self, conn, **kwargs):
        with Timeout(self.app.node_timeout):
            if conn.resp:
                return conn.resp
            else:
                return conn.getresponse()

    def _get_conn_response(self, conn, req, **kwargs):
        try:
            resp = self._await_response(conn, **kwargs)
            return (conn, resp)
        except (Exception, Timeout):
            self.app.exception_occurred(
                conn.node, _('Object'),
                _('Trying to get final status of PUT to %s') % req.path)
        return (None, None)

    def _get_put_responses(self, req, conns, nodes):
        statuses = []
        reasons = []
        bodies = []
        etags = set()

        pile = GreenAsyncPile(len(conns))
        for conn in conns:
            pile.spawn(self._get_conn_response, conn, req)

        def _handle_response(conn, response):
            statuses.append(response.status)
            reasons.append(response.reason)
            bodies.append(response.read())
            if response.status == HTTP_INSUFFICIENT_STORAGE:
                self.app.error_limit(conn.node,
                                     _('ERROR Insufficient Storage'))
            elif response.status >= HTTP_INTERNAL_SERVER_ERROR:
                self.app.error_occurred(
                    conn.node,
                    _('ERROR %(status)d %(body)s From Object Server '
                      're: %(path)s') %
                    {'status': response.status,
                     'body': bodies[-1][:1024], 'path': req.path})
            elif is_success(response.status):
                etags.add(response.getheader('etag').strip('"'))

        for (conn, response) in pile:
            if response:
                _handle_response(conn, response)
                if self.have_quorum(statuses, len(nodes)):
                    break

        # give any pending requests *some* chance to finish
        finished_quickly = pile.waitall(self.app.post_quorum_timeout)
        for (conn, response) in finished_quickly:
            if response:
                _handle_response(conn, response)

        while len(statuses) < len(nodes):
            statuses.append(HTTP_SERVICE_UNAVAILABLE)
            reasons.append('')
            bodies.append('')
        return statuses, reasons, bodies, etags

    def _config_obj_expiration(self, req):
        delete_at_container = None
        delete_at_part = None
        delete_at_nodes = None

        req = constraints.check_delete_headers(req)

        if 'x-delete-at' in req.headers:
            x_delete_at = int(normalize_delete_at_timestamp(
                int(req.headers['x-delete-at'])))

            req.environ.setdefault('swift.log_info', []).append(
                'x-delete-at:%s' % x_delete_at)

            delete_at_container = get_expirer_container(
                x_delete_at, self.app.expiring_objects_container_divisor,
                self.account_name, self.container_name, self.object_name)

            delete_at_part, delete_at_nodes = \
                self.app.container_ring.get_nodes(
                    self.app.expiring_objects_account, delete_at_container)

        return req, delete_at_container, delete_at_part, delete_at_nodes

    def _handle_copy_request(self, req):
        """
        This method handles copying objects based on values set in the headers
        'X-Copy-From' and 'X-Copy-From-Account'

        This method was added as part of the refactoring of the PUT method and
        the functionality is expected to be moved to middleware
        """
        if req.environ.get('swift.orig_req_method', req.method) != 'POST':
            req.environ.setdefault('swift.log_info', []).append(
                'x-copy-from:%s' % req.headers['X-Copy-From'])
        ver, acct, _rest = req.split_path(2, 3, True)
        src_account_name = req.headers.get('X-Copy-From-Account', None)
        if src_account_name:
            src_account_name = check_account_format(req, src_account_name)
        else:
            src_account_name = acct
        src_container_name, src_obj_name = check_copy_from_header(req)
        source_header = '/%s/%s/%s/%s' % (
            ver, src_account_name, src_container_name, src_obj_name)
        source_req = req.copy_get()

        # make sure the source request uses it's container_info
        source_req.headers.pop('X-Backend-Storage-Policy-Index', None)
        source_req.path_info = source_header
        source_req.headers['X-Newest'] = 'true'

        orig_obj_name = self.object_name
        orig_container_name = self.container_name
        orig_account_name = self.account_name
        sink_req = Request.blank(req.path_info,
                                 environ=req.environ, headers=req.headers)

        self.object_name = src_obj_name
        self.container_name = src_container_name
        self.account_name = src_account_name
        source_resp = self.GET(source_req)

        # This gives middlewares a way to change the source; for example,
        # this lets you COPY a SLO manifest and have the new object be the
        # concatenation of the segments (like what a GET request gives
        # the client), not a copy of the manifest file.
        hook = req.environ.get(
            'swift.copy_hook',
            (lambda source_req, source_resp, sink_req: source_resp))
        source_resp = hook(source_req, source_resp, sink_req)

        # reset names
        self.object_name = orig_obj_name
        self.container_name = orig_container_name
        self.account_name = orig_account_name

        if source_resp.status_int >= HTTP_MULTIPLE_CHOICES:
            # this is a bit of ugly code, but I'm willing to live with it
            # until copy request handling moves to middleware
            return source_resp, None, None, None
        if source_resp.content_length is None:
            # This indicates a transfer-encoding: chunked source object,
            # which currently only happens because there are more than
            # CONTAINER_LISTING_LIMIT segments in a segmented object. In
            # this case, we're going to refuse to do the server-side copy.
            raise HTTPRequestEntityTooLarge(request=req)
        if source_resp.content_length > constraints.MAX_FILE_SIZE:
            raise HTTPRequestEntityTooLarge(request=req)

        data_source = iter(source_resp.app_iter)
        sink_req.content_length = source_resp.content_length
        sink_req.etag = source_resp.etag

        # we no longer need the X-Copy-From header
        del sink_req.headers['X-Copy-From']
        if 'X-Copy-From-Account' in sink_req.headers:
            del sink_req.headers['X-Copy-From-Account']
        if not req.content_type_manually_set:
            sink_req.headers['Content-Type'] = \
                source_resp.headers['Content-Type']
        if config_true_value(
                sink_req.headers.get('x-fresh-metadata', 'false')):
            # post-as-copy: ignore new sysmeta, copy existing sysmeta
            condition = lambda k: is_sys_meta('object', k)
            remove_items(sink_req.headers, condition)
            copy_header_subset(source_resp, sink_req, condition)
        else:
            # copy/update existing sysmeta and user meta
            copy_headers_into(source_resp, sink_req)
            copy_headers_into(req, sink_req)

        # copy over x-static-large-object for POSTs and manifest copies
        if 'X-Static-Large-Object' in source_resp.headers and \
                req.params.get('multipart-manifest') == 'get':
            sink_req.headers['X-Static-Large-Object'] = \
                source_resp.headers['X-Static-Large-Object']

        req = sink_req

        def update_response(req, resp):
            acct, path = source_resp.environ['PATH_INFO'].split('/', 3)[2:4]
            resp.headers['X-Copied-From-Account'] = quote(acct)
            resp.headers['X-Copied-From'] = quote(path)
            if 'last-modified' in source_resp.headers:
                resp.headers['X-Copied-From-Last-Modified'] = \
                    source_resp.headers['last-modified']
            copy_headers_into(req, resp)
            return resp

        # this is a bit of ugly code, but I'm willing to live with it
        # until copy request handling moves to middleware
        return None, req, data_source, update_response

    def _handle_object_versions(self, req):
        """
        This method handles versionining of objects in containers that
        have the feature enabled.

        When a new PUT request is sent, the proxy checks for previous versions
        of that same object name. If found, it is copied to a different
        container and the new version is stored in its place.

        This method was added as part of the PUT method refactoring and the
        functionality is expected to be moved to middleware
        """
        container_info = self.container_info(
            self.account_name, self.container_name, req)
        policy_index = req.headers.get('X-Backend-Storage-Policy-Index',
                                       container_info['storage_policy'])
        obj_ring = self.app.get_object_ring(policy_index)
        partition, nodes = obj_ring.get_nodes(
            self.account_name, self.container_name, self.object_name)
        object_versions = container_info['versions']

        # do a HEAD request for checking object versions
        if object_versions and not req.environ.get('swift_versioned_copy'):
            # make sure proxy-server uses the right policy index
            _headers = {'X-Backend-Storage-Policy-Index': policy_index,
                        'X-Newest': 'True'}
            hreq = Request.blank(req.path_info, headers=_headers,
                                 environ={'REQUEST_METHOD': 'HEAD'})
            hresp = self.GETorHEAD_base(
                hreq, _('Object'), obj_ring, partition,
                hreq.swift_entity_path)

            is_manifest = 'X-Object-Manifest' in req.headers or \
                          'X-Object-Manifest' in hresp.headers
            if hresp.status_int != HTTP_NOT_FOUND and not is_manifest:
                # This is a version manifest and needs to be handled
                # differently. First copy the existing data to a new object,
                # then write the data from this request to the version manifest
                # object.
                lcontainer = object_versions.split('/')[0]
                prefix_len = '%03x' % len(self.object_name)
                lprefix = prefix_len + self.object_name + '/'
                ts_source = hresp.environ.get('swift_x_timestamp')
                if ts_source is None:
                    ts_source = time.mktime(time.strptime(
                                            hresp.headers['last-modified'],
                                            '%a, %d %b %Y %H:%M:%S GMT'))
                new_ts = Timestamp(ts_source).internal
                vers_obj_name = lprefix + new_ts
                copy_headers = {
                    'Destination': '%s/%s' % (lcontainer, vers_obj_name)}
                copy_environ = {'REQUEST_METHOD': 'COPY',
                                'swift_versioned_copy': True
                                }
                copy_req = Request.blank(req.path_info, headers=copy_headers,
                                         environ=copy_environ)
                copy_resp = self.COPY(copy_req)
                if is_client_error(copy_resp.status_int):
                    # missing container or bad permissions
                    raise HTTPPreconditionFailed(request=req)
                elif not is_success(copy_resp.status_int):
                    # could not copy the data, bail
                    raise HTTPServiceUnavailable(request=req)

    def _update_content_type(self, req):
        # Sometimes the 'content-type' header exists, but is set to None.
        req.content_type_manually_set = True
        detect_content_type = \
            config_true_value(req.headers.get('x-detect-content-type'))
        if detect_content_type or not req.headers.get('content-type'):
            guessed_type, _junk = mimetypes.guess_type(req.path_info)
            req.headers['Content-Type'] = guessed_type or \
                'application/octet-stream'
            if detect_content_type:
                req.headers.pop('x-detect-content-type')
            else:
                req.content_type_manually_set = False

    def _update_x_timestamp(self, req):
        # Used by container sync feature
        if 'x-timestamp' in req.headers:
            try:
                req_timestamp = Timestamp(req.headers['X-Timestamp'])
            except ValueError:
                raise HTTPBadRequest(
                    request=req, content_type='text/plain',
                    body='X-Timestamp should be a UNIX timestamp float value; '
                         'was %r' % req.headers['x-timestamp'])
            req.headers['X-Timestamp'] = req_timestamp.internal
        else:
            req.headers['X-Timestamp'] = Timestamp(time.time()).internal
        return None

    def _check_failure_put_connections(self, conns, req, nodes):
        if req.if_none_match is not None and '*' in req.if_none_match:
            statuses = [conn.resp.status for conn in conns if conn.resp]
            if HTTP_PRECONDITION_FAILED in statuses:
                # If we find any copy of the file, it shouldn't be uploaded
                self.app.logger.debug(
                    _('Object PUT returning 412, %(statuses)r'),
                    {'statuses': statuses})
                raise HTTPPreconditionFailed(request=req)

        if any(conn for conn in conns if conn.resp and
               conn.resp.status == HTTP_CONFLICT):
            timestamps = [HeaderKeyDict(conn.resp.getheaders()).get(
                'X-Backend-Timestamp') for conn in conns if conn.resp]
            self.app.logger.debug(
                _('Object PUT returning 202 for 409: '
                  '%(req_timestamp)s <= %(timestamps)r'),
                {'req_timestamp': req.timestamp.internal,
                 'timestamps': ', '.join(timestamps)})
            raise HTTPAccepted(request=req)

        min_conns = quorum_size(len(nodes))
        self._check_min_conn(req, conns, min_conns)

    def _get_put_connections(self, req, nodes, partition, outgoing_headers,
                             policy, expect):
        """
        Establish connections to storage nodes for PUT request
        """
        obj_ring = policy.object_ring
        node_iter = GreenthreadSafeIterator(
            self.iter_nodes_local_first(obj_ring, partition))
        pile = GreenPile(len(nodes))

        for nheaders in outgoing_headers:
            if expect:
                nheaders['Expect'] = '100-continue'
            pile.spawn(self._connect_put_node, node_iter, partition,
                       req.swift_entity_path, nheaders,
                       self.app.logger.thread_locals)

        conns = [conn for conn in pile if conn]

        return conns

    def _check_min_conn(self, req, conns, min_conns, msg=None):
        msg = msg or 'Object PUT returning 503, %(conns)s/%(nodes)s ' \
            'required connections'

        if len(conns) < min_conns:
            self.app.logger.error((msg),
                                  {'conns': len(conns), 'nodes': min_conns})
            raise HTTPServiceUnavailable(request=req)

    def _transfer_data(self, req, data_source, conns, nodes):
        min_conns = quorum_size(len(nodes))

        bytes_transferred = 0
        try:
            with ContextPool(len(nodes)) as pool:
                for conn in conns:
                    conn.failed = False
                    conn.queue = Queue(self.app.put_queue_depth)
                    pool.spawn(self._send_file, conn, req.path)
                while True:
                    with ChunkReadTimeout(self.app.client_timeout):
                        try:
                            chunk = next(data_source)
                        except StopIteration:
                            if req.is_chunked:
                                for conn in conns:
                                    conn.queue.put('0\r\n\r\n')
                            break
                    bytes_transferred += len(chunk)
                    if bytes_transferred > constraints.MAX_FILE_SIZE:
                        raise HTTPRequestEntityTooLarge(request=req)
                    for conn in list(conns):
                        if not conn.failed:
                            conn.queue.put(
                                '%x\r\n%s\r\n' % (len(chunk), chunk)
                                if req.is_chunked else chunk)
                        else:
                            conn.close()
                            conns.remove(conn)
                    self._check_min_conn(
                        req, conns, min_conns,
                        msg='Object PUT exceptions during'
                            ' send, %(conns)s/%(nodes)s required connections')
                for conn in conns:
                    if conn.queue.unfinished_tasks:
                        conn.queue.join()
            conns = [conn for conn in conns if not conn.failed]
            self._check_min_conn(
                req, conns, min_conns,
                msg='Object PUT exceptions after last send, '
                '%(conns)s/%(nodes)s required connections')
        except ChunkReadTimeout as err:
            self.app.logger.warn(
                _('ERROR Client read timeout (%ss)'), err.seconds)
            self.app.logger.increment('client_timeouts')
            raise HTTPRequestTimeout(request=req)
        except HTTPException:
            raise
        except (Exception, Timeout):
            self.app.logger.exception(
                _('ERROR Exception causing client disconnect'))
            raise HTTPClientDisconnect(request=req)
        if req.content_length and bytes_transferred < req.content_length:
            req.client_disconnect = True
            self.app.logger.warn(
                _('Client disconnected without sending enough data'))
            self.app.logger.increment('client_disconnects')
            raise HTTPClientDisconnect(request=req)

    def _store_object(self, req, data_source, nodes, partition,
                      outgoing_headers):
        """
        Store a replicated object.

        This method is responsible for establishing connection
        with storage nodes and sending object to each one of those
        nodes. After sending the data, the "best" reponse will be
        returned based on statuses from all connections
        """
        policy_idx = req.headers.get('X-Backend-Storage-Policy-Index')
        policy = POLICIES.get_by_index(policy_idx)
        if not nodes:
            return HTTPNotFound()

        # RFC2616:8.2.3 disallows 100-continue without a body
        if (req.content_length > 0) or req.is_chunked:
            expect = True
        else:
            expect = False
        conns = self._get_put_connections(req, nodes, partition,
                                          outgoing_headers, policy, expect)

        try:
            # check that a minimum number of connections were established and
            # meet all the correct conditions set in the request
            self._check_failure_put_connections(conns, req, nodes)

            # transfer data
            self._transfer_data(req, data_source, conns, nodes)

            # get responses
            statuses, reasons, bodies, etags = self._get_put_responses(
                req, conns, nodes)
        except HTTPException as resp:
            return resp
        finally:
            for conn in conns:
                conn.close()

        if len(etags) > 1:
            self.app.logger.error(
                _('Object servers returned %s mismatched etags'), len(etags))
            return HTTPServerError(request=req)
        etag = etags.pop() if len(etags) else None
        resp = self.best_response(req, statuses, reasons, bodies,
                                  _('Object PUT'), etag=etag)
        resp.last_modified = math.ceil(
            float(Timestamp(req.headers['X-Timestamp'])))
        return resp

    @public
    @cors_validation
    @delay_denial
    def PUT(self, req):
        """HTTP PUT request handler."""
        if req.if_none_match is not None and '*' not in req.if_none_match:
            # Sending an etag with if-none-match isn't currently supported
            return HTTPBadRequest(request=req, content_type='text/plain',
                                  body='If-None-Match only supports *')
        container_info = self.container_info(
            self.account_name, self.container_name, req)
        policy_index = req.headers.get('X-Backend-Storage-Policy-Index',
                                       container_info['storage_policy'])
        obj_ring = self.app.get_object_ring(policy_index)
        container_nodes = container_info['nodes']
        container_partition = container_info['partition']
        partition, nodes = obj_ring.get_nodes(
            self.account_name, self.container_name, self.object_name)

        # pass the policy index to storage nodes via req header
        req.headers['X-Backend-Storage-Policy-Index'] = policy_index
        req.acl = container_info['write_acl']
        req.environ['swift_sync_key'] = container_info['sync_key']

        # is request authorized
        if 'swift.authorize' in req.environ:
            aresp = req.environ['swift.authorize'](req)
            if aresp:
                return aresp

        if not container_info['nodes']:
            return HTTPNotFound(request=req)

        # update content type in case it is missing
        self._update_content_type(req)

        # check constraints on object name and request headers
        error_response = check_object_creation(req, self.object_name) or \
            check_content_type(req)
        if error_response:
            return error_response

        self._update_x_timestamp(req)

        # check if versioning is enabled and handle copying previous version
        self._handle_object_versions(req)

        # check if request is a COPY of an existing object
        source_header = req.headers.get('X-Copy-From')
        if source_header:
            error_response, req, data_source, update_response = \
                self._handle_copy_request(req)
            if error_response:
                return error_response
        else:
            reader = req.environ['wsgi.input'].read
            data_source = iter(lambda: reader(self.app.client_chunk_size), '')
            update_response = lambda req, resp: resp

        # check if object is set to be automaticaly deleted (i.e. expired)
        req, delete_at_container, delete_at_part, \
            delete_at_nodes = self._config_obj_expiration(req)

        # add special headers to be handled by storage nodes
        outgoing_headers = self._backend_requests(
            req, len(nodes), container_partition, container_nodes,
            delete_at_container, delete_at_part, delete_at_nodes)

        # send object to storage nodes
        resp = self._store_object(
            req, data_source, nodes, partition, outgoing_headers)
        return update_response(req, resp)

    @public
    @cors_validation
    @delay_denial
    def DELETE(self, req):
        """HTTP DELETE request handler."""
        container_info = self.container_info(
            self.account_name, self.container_name, req)
        # pass the policy index to storage nodes via req header
        policy_index = req.headers.get('X-Backend-Storage-Policy-Index',
                                       container_info['storage_policy'])
        obj_ring = self.app.get_object_ring(policy_index)
        # pass the policy index to storage nodes via req header
        req.headers['X-Backend-Storage-Policy-Index'] = policy_index
        container_partition = container_info['partition']
        containers = container_info['nodes']
        req.acl = container_info['write_acl']
        req.environ['swift_sync_key'] = container_info['sync_key']
        object_versions = container_info['versions']
        if object_versions:
            # this is a version manifest and needs to be handled differently
            object_versions = unquote(object_versions)
            lcontainer = object_versions.split('/')[0]
            prefix_len = '%03x' % len(self.object_name)
            lprefix = prefix_len + self.object_name + '/'
            item_list = []
            try:
                for _item in self._listing_iter(lcontainer, lprefix,
                                                req.environ):
                    item_list.append(_item)
            except ListingIterNotFound:
                # no worries, last_item is None
                pass
            except ListingIterNotAuthorized as err:
                return err.aresp
            except ListingIterError:
                return HTTPServerError(request=req)

            while len(item_list) > 0:
                previous_version = item_list.pop()
                # there are older versions so copy the previous version to the
                # current object and delete the previous version
                orig_container = self.container_name
                orig_obj = self.object_name
                self.container_name = lcontainer
                self.object_name = previous_version['name'].encode('utf-8')

                copy_path = '/v1/' + self.account_name + '/' + \
                            self.container_name + '/' + self.object_name

                copy_headers = {'X-Newest': 'True',
                                'Destination': orig_container + '/' + orig_obj
                                }
                copy_environ = {'REQUEST_METHOD': 'COPY',
                                'swift_versioned_copy': True
                                }
                creq = Request.blank(copy_path, headers=copy_headers,
                                     environ=copy_environ)
                copy_resp = self.COPY(creq)
                if copy_resp.status_int == HTTP_NOT_FOUND:
                    # the version isn't there so we'll try with previous
                    self.container_name = orig_container
                    self.object_name = orig_obj
                    continue
                if is_client_error(copy_resp.status_int):
                    # some user error, maybe permissions
                    return HTTPPreconditionFailed(request=req)
                elif not is_success(copy_resp.status_int):
                    # could not copy the data, bail
                    return HTTPServiceUnavailable(request=req)
                # reset these because the COPY changed them
                self.container_name = lcontainer
                self.object_name = previous_version['name'].encode('utf-8')
                new_del_req = Request.blank(copy_path, environ=req.environ)
                container_info = self.container_info(
                    self.account_name, self.container_name, req)
                policy_idx = container_info['storage_policy']
                obj_ring = self.app.get_object_ring(policy_idx)
                # pass the policy index to storage nodes via req header
                new_del_req.headers['X-Backend-Storage-Policy-Index'] = \
                    policy_idx
                container_partition = container_info['partition']
                containers = container_info['nodes']
                new_del_req.acl = container_info['write_acl']
                new_del_req.path_info = copy_path
                req = new_del_req
                # remove 'X-If-Delete-At', since it is not for the older copy
                if 'X-If-Delete-At' in req.headers:
                    del req.headers['X-If-Delete-At']
                break
        if 'swift.authorize' in req.environ:
            aresp = req.environ['swift.authorize'](req)
            if aresp:
                return aresp
        if not containers:
            return HTTPNotFound(request=req)
        partition, nodes = obj_ring.get_nodes(
            self.account_name, self.container_name, self.object_name)
        # Used by container sync feature
        if 'x-timestamp' in req.headers:
            try:
                req_timestamp = Timestamp(req.headers['X-Timestamp'])
            except ValueError:
                return HTTPBadRequest(
                    request=req, content_type='text/plain',
                    body='X-Timestamp should be a UNIX timestamp float value; '
                         'was %r' % req.headers['x-timestamp'])
            req.headers['X-Timestamp'] = req_timestamp.internal
        else:
            req.headers['X-Timestamp'] = Timestamp(time.time()).internal

        headers = self._backend_requests(
            req, len(nodes), container_partition, containers)
        # When deleting objects treat a 404 status as 204.
        status_overrides = {404: 204}
        resp = self.make_requests(req, obj_ring,
                                  partition, 'DELETE', req.swift_entity_path,
                                  headers, overrides=status_overrides)
        return resp

    @public
    @cors_validation
    @delay_denial
    def COPY(self, req):
        """HTTP COPY request handler."""
        if not req.headers.get('Destination'):
            return HTTPPreconditionFailed(request=req,
                                          body='Destination header required')
        dest_account = self.account_name
        if 'Destination-Account' in req.headers:
            dest_account = req.headers.get('Destination-Account')
            dest_account = check_account_format(req, dest_account)
            req.headers['X-Copy-From-Account'] = self.account_name
            self.account_name = dest_account
            del req.headers['Destination-Account']
        dest_container, dest_object = check_destination_header(req)
        source = '/%s/%s' % (self.container_name, self.object_name)
        self.container_name = dest_container
        self.object_name = dest_object
        # re-write the existing request as a PUT instead of creating a new one
        # since this one is already attached to the posthooklogger
        req.method = 'PUT'
        req.path_info = '/v1/%s/%s/%s' % \
                        (dest_account, dest_container, dest_object)
        req.headers['Content-Length'] = 0
        req.headers['X-Copy-From'] = quote(source)
        del req.headers['Destination']
        return self.PUT(req)
