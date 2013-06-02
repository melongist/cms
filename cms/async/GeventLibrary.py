#!/usr/bin/env python2
# -*- coding: utf-8 -*-

# Programming contest management system
# Copyright © 2010-2013 Giovanni Mascellani <mascellani@poisson.phc.unipi.it>
# Copyright © 2010-2012 Stefano Maggiolo <s.maggiolo@gmail.com>
# Copyright © 2010-2012 Matteo Boscariol <boscarim@hotmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""This file defines classes to handle asynchronous RPC communication
using gevent and JSON encoding.

"""

import heapq
import signal
import time
import traceback

import gevent
import gevent.socket
from gevent.server import StreamServer

from cms.async import ServiceCoord, Address, get_service_address
from cms.async.Utils import Logger, encode_json, decode_json
from cms.async.AsyncLibrary import rpc_callback, rpc_method, rpc_threaded, \
    AuthorizationError, RPCRequest
from cms.async.PsycoGevent import make_psycopg_green


logger = None

make_psycopg_green()


class Service:

    def __init__(self, shard=0, custom_logger=None):
        signal.signal(signal.SIGINT, lambda unused_x, unused_y: self.exit())

        global logger
        if custom_logger is None:
            logger = Logger()
        else:
            logger = custom_logger

        self.shard = shard
        # Stores the function to call periodically. It is to be
        # managed with heapq. Format: (next_timeout, period, function,
        # plus)
        self._timeouts = []
        # If we want to exit the main loop
        self._exit = False
        # Dictionaries of (to be) connected RemoteService, and
        # dictionaries of callback functions that are going to be
        # called when the remote service becomes online.
        self.remote_services = {}
        self.on_remote_service_connected = {}

        self._my_coord = ServiceCoord(self.__class__.__name__, self.shard)

        # We setup the listening address for services which want to
        # connect with us.
        try:
            address = get_service_address(self._my_coord)
        except KeyError:
            address = None
        if address is not None:
            self.server = StreamServer(address, self._connection_handler)

    def _connection_handler(self, socket, address):
        try:
            ipaddr, port = address
            ipaddr = gevent.socket.gethostbyname(ipaddr)
            address = Address(ipaddr, port)
        except:
            logger.warning("Error: %s" % (traceback.format_exc()))
            return
        remote_service = RemoteService(self,
                                       address=address)
        remote_service._initialize_channel(socket)

    def connect_to(self, service, on_connect=None):
        """Ask the service to connect to another service. A channel is
        established and connected. The connection will be reopened if
        closed.

        service (ServiceCoord): the service to connect to.
        on_connect (function): to be called when the service connects.
        return (RemoteService): the connected RemoteService istance.

        """
        self.on_remote_service_connected[service] = on_connect
        self.remote_services[service] = RemoteService(self, service)
        return self.remote_services[service]

    def add_timeout(self, func, plus, seconds, immediately=False):
        """Registers a function to be called every x seconds.

        func (function): the function to call.
        plus (object): additional data to pass to the function.
        seconds (float): the function will be called every seconds
                         seconds.
        immediately (bool): if True, func will be called also at the
                            beginning.

        """
        next_timeout = time.time()
        if not immediately:
            next_timeout += seconds
        heapq.heappush(self._timeouts, (next_timeout, seconds, func, plus))

    def exit(self):
        """Terminate the service at the next step.

        """
        self._exit = True
        logger.warning("%s %d dying in 3, 2, 1..." % self._my_coord)

    def run(self):
        """Starts the main loop of the service.

        """
        self.server.start()
        try:
            while not self._exit:
                next_timeout = self._trigger(maximum=0.5)
                gevent.sleep(next_timeout)
        except Exception as error:
            err_msg = "Exception not managed, quitting. " \
                      "Exception `%s' and traceback `%s'" % \
                      (repr(error), traceback.format_exc())
            logger.critical(err_msg)
        self.server.stop()

    def _reconnect(self):
        """Reconnect to all remote services that have been disconnected.

        """
        for service in self.remote_services:
            remote_service = self.remote_services[service]
            if not remote_service.connected:
                try:
                    remote_service.connect_remote_service()
                except:
                    pass
                if remote_service.connected and \
                       self.on_remote_service_connected[service] \
                       is not None:
                    self.on_remote_service_connected[service](service)
        return True

    def _trigger(self, maximum=2.0):
        """Call the timeouts that have expired and find interval to
        next timeout (capped to maximum second).

        maximum (float): seconds to cap to the value.
        return (float): seconds to next timeout.

        """
        current = time.time()

        # Try to connect to disconnected services.
        self._reconnect()

        # Check if some scheduled function needs to be called.
        while self._timeouts != []:
            timeout_data = self._timeouts[0]
            next_timeout, _, _, _ = timeout_data
            if current > next_timeout:
                heapq.heappop(self._timeouts)

                # The helper function checks the return value and, if
                # needed, enqueues the next timeout call
                def helper(timeout_data):
                    next_timeout, seconds, func, plus = timeout_data
                    if plus is None:
                        ret = func()
                    else:
                        ret = func(plus)
                    if ret:
                        heapq.heappush(self._timeouts,
                                       (next_timeout + seconds,
                                        seconds, func, plus))

                gevent.spawn(helper, timeout_data)
            else:
                break

        # Compute time to next timeout call
        next_timeout = maximum
        if self._timeouts != []:
            next_timeout = min(next_timeout, self._timeouts[0][0] - current)
        return max(0.0, next_timeout)

    @rpc_method
    def echo(self, string):
        """Simple RPC method.

        string (string): the string to be echoed.
        return (string): string, again.

        """
        return string

    @rpc_method
    def quit(self, reason=""):
        """Shut down the service

        reason (string): why, oh why, you want me down?

        """
        logger.info("Trying to exit as asked by another service (%s)."
                    % reason)
        self.exit()

    def method_info(self, method_name):
        """Returns some information about the requested method, or
        exceptions if the method does not exists.

        method_name (string): the requested method
        return (dict): infos about the method

        """
        try:
            method = getattr(self, method_name)
        except:
            raise KeyError("Service has no method " + method_name)

        res = {}
        res["callable"] = hasattr(method, "rpc_callable")
        res["threaded"] = hasattr(method, "threaded")

        return res

    def handle_message(self, message):
        """To be called when the channel finishes to collect a message
        that is a RPC request. It calls the requested method.

        message (object): the decoded message.
        return (object, bool): the object is the value returned by the
                               method, the bool is True if the object
                               is to be interpreted as a binary
                               string.
        """
        method_name = message["__method"]
        try:
            method = getattr(self, method_name)
        except:
            raise KeyError("Service has no method " + method_name)

        if not hasattr(method, "rpc_callable"):
            raise AuthorizationError("Method %s not callable from RPC" %
                                     method)

        if "__data" not in message:
            raise ValueError("No data present.")

        result = method(**message["__data"])

        return result


class RemoteService():
    """This class mimick the local presence of a remote service. A
    local service can define many RemoteService object and call
    methods of those services almost as if they were local. Almost
    because being asynchronous, the responses of the requests have to
    be collected using callback functions.

    """

    def __init__(self, service, remote_service_coord=None, address=None):
        """Create a communication channel to a remote service.

        service (Service): the local service.
        remote_service_coord (ServiceCoord): the description of the
                                             remote service to connect
                                             to.
        address (Address): alternatively, the address to connect to
                           (used when accepting a connection).

        """
        if address is None and remote_service_coord is None:
            raise

        # service is the local service connecting to the remote one.
        self.service = service

        if address is None:
            self.remote_service_coord = remote_service_coord
            self.address = get_service_address(remote_service_coord)
        else:
            self.remote_service_coord = ""
            self.address = address
        self.connected = False

    def _initialize_channel(self, sock):
        """When we have a socket, we configure the channel using this
        socket. This spawns a new Greenlet that monitors the incoming
        channel and collects data.

        sock (socket): the socket to use as a communication channel.
        """
        self.socket = sock
        self.connected = True
        gevent.spawn(self._loop)

    def process_data(self, data):
        """Function called when a terminator is detected in the
        stream. It clear the cache and decode the data. Then it ask
        the local service to act and in case the service wants to
        respond, it sends back the response.

        """
        # We decode the arriving data
        try:
            message = decode_json(data)
        except:
            logger.warning("Cannot understand incoming message, discarding.")
            return

        # If __method is present, someone is calling an rpc of the
        # local service
        if "__method" in message:
            # We initialize the data we are going to send back
            response = {"__data": None,
                        "__error": None}
            if "__id" in message:
                response["__id"] = message["__id"]

            # We find the properties of the called rpc method.
            try:
                method_info = self.service.method_info(message["__method"])
                threaded = method_info["threaded"]
            except KeyError as exception:
                response["__error"] = "%s: %s\n%s" % \
                    (exception.__class__.__name__, exception,
                     traceback.format_exc())
                method_response = None
                self.send_reply(response, method_response)
                return

            # Threaded RPC not supported here
            if threaded:
                response["__error"] = "Threaded RPC unsupported"
                method_response = None
                self.send_reply(response, method_response)
                return

            # Otherwise, we compute the method here and send the reply
            # right away.
            try:
                method_response = self.service.handle_message(message)
            except Exception as exception:
                response["__error"] = "%s: %s\n%s" % \
                    (exception.__class__.__name__, exception,
                     traceback.format_exc())
                method_response = None
            self.send_reply(response, method_response)

        # Otherwise, is a response to our rpc call.
        else:
            if "__id" not in message:
                logger.warning("Response without __id field, discarding.")
                return
            ident = message["__id"]
            if ident in RPCRequest.pending_requests:
                rpc = RPCRequest.pending_requests[ident]
                rpc.complete(message)
            else:
                logger.warning("No pending request with id %s found." % ident)

    def send_reply(self, response, method_response):
        """Send back a reply to an rpc call.

        response (dict): the metadata of the reply.
        method_response (object): the actual returned value.

        """
        try:
            response["__data"] = method_response
            json_message = encode_json(response)
        except ValueError as error:
            logger.warning("Cannot send response because of " +
                           "encoding error. %s" % repr(error))
            return
        self._push_right(json_message)

    def _loop(self):
        inbox = b''
        while True:
            buf = self.socket.recv(4096)
            splits = (inbox + buf).split('\r\n')
            inbox = splits[-1]
            # TODO - Add some checks that the data buffer doesn't
            # exceed a maximum size; otherwise, this could be a DOS
            # attack vector
            for data in splits[:-1]:
                self.process_data(data)
            # Connection has been closed
            if buf == b'':
                break

    def connect_remote_service(self):
        """Try to connect to the remote service.

        """
        try:
            sock = gevent.socket.socket(gevent.socket.AF_INET,
                                        gevent.socket.SOCK_STREAM)
            sock.connect(self.address)
        except:
            pass
        else:
            self._initialize_channel(sock)

    def execute_rpc(self, method, data, callback=None, plus=None):
        """Method to send an RPC request to the remote service.

        The message sent to the remote service is of this kind:
        {"__method": <name of the requested method>
         "__data": {"<name of first arg>": <value of first arg,
                    ...
                   }
         "__id": <16 letters random ID>
        }

        The __id field is put by the pre_execute method of
        RPCRequest.

        method (string): the name of the method to call.
        data (object): the object to pass to the remote method.
        callback (function): method to call with the RPC response.
        plus (object): additional object to be passed to the callback.

        return (bool/dict): False if the remote service is not
                            connected; in a non-yielded call True if
                            it is connected; in a yielded call, a
                            dictionary with fields 'completed',
                            'data', and 'error'.

        """
        # Try to connect, or fail.
        if not self.connected:
            self.connect_remote_service()
            if not self.connected:
                return False

        # We start building the request message
        message = {}
        message["__method"] = method
        message["__data"] = data

        # And we remember that we need to wait for a reply
        request = RPCRequest(message, self.service, callback, plus)
        message = request.pre_execute()

        # We encode the request and send it
        try:
            json_message = encode_json(message)
        except ValueError:
            msg = "Cannot send request of method %s because of " \
                "encoding error." % method
            request.complete({"__error": msg})
            return
        ret = self._push_right(json_message)
        if not ret:
            msg = "Transfer interrupted"
            request.complete({"__error": msg})
            return

        return True

    def __getattr__(self, method):
        """Syntactic sugar to call a remote method without using
        execute_rpc. If the local service ask for something that is
        not present, we assume that it is a remote RPC method.

        method (string): the method to call.

        """
        def remote_method(callback=None,
                          plus=None,
                          **data):
            """Call execute_rpc with the given method name.

            """
            return self.execute_rpc(method=method, data=data,
                                    callback=callback, plus=plus)
        return remote_method

    def _push_right(self, data):
        """Send a request or a response with the right terminator in
        the end.

        data (string): the data to send.

        """
        to_push = b''.join(data) + b'\r\n'
        try:
            while to_push != b'':
                num = self.socket.send(to_push)
                to_push = to_push[num:]
                if num == 0:
                    logger.warning("Push not ended correctly: socket close.")
                    self.connected = False
                    return False
        except Exception as error:
            logger.warning("Push not ended correctly because of %r." % error)
            return False
        return True
