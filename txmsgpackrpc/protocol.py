# http://github.com/donalm/txMsgpack
# Copyright (c) 2013 Donal McMullan

# https://github.com/jakm/txmsgpackrpc
# Copyright (c) 2015 Jakub Matys

from __future__ import print_function

import msgpack
from collections import namedtuple
from twisted.internet import defer, protocol
from twisted.protocols import policies
from twisted.python import failure

from txmsgpackrpc.error import (ConnectionError, ResponseError, InvalidRequest,
                                InvalidResponse, InvalidData, TimeoutError,
                                SerializationError)


MSGTYPE_REQUEST=0
MSGTYPE_RESPONSE=1
MSGTYPE_NOTIFICATION=2


Context = namedtuple('Context', ['peer'])


class MsgpackBaseProtocol(object):
    """
    msgpack rpc client/server protocol - base implementation
    """
    def __init__(self, sendErrors=False, packerEncoding="utf-8", unpackerEncoding="utf-8"):
        """
        @param sendErrors: forward any uncaught Exception details to remote peer.
        @type sendErrors: C{bool}.
        @param packerEncoding: encoding used to encode Python str and unicode. Default is 'utf-8'.
        @type packerEncoding: C{str}
        @param unpackerEncoding: encoding used for decoding msgpack bytes. If None (default), msgpack bytes are deserialized to Python bytes.
        @type unpackerEncoding: C{str}.
        """
        self._sendErrors = sendErrors
        self._incoming_requests = {}
        self._outgoing_requests = {}
        self._next_msgid = 0
        self._packer = msgpack.Packer(encoding=packerEncoding)
        self._unpacker = msgpack.Unpacker(encoding=unpackerEncoding, unicode_errors='strict')

    def isConnected(self):
        raise NotImplementedError('Must be implemented in descendant')

    def writeRawData(self, message, context):
        raise NotImplementedError('Must be implemented in descendant')

    def getRemoteMethod(self, protocol, methodName):
        raise NotImplementedError('Must be implemented in descendant')

    def getClientContext(self):
        raise NotImplementedError('Must be implemented in descendant')

    def createRequest(self, method, params):
        if not self.isConnected():
            raise ConnectionError("Not connected")
        msgid = self.getNextMsgid()
        message = (MSGTYPE_REQUEST, msgid, method, params)
        ctx = self.getClientContext()
        self.writeMessage(message, ctx)

        df = defer.Deferred()
        self._outgoing_requests[msgid] = df
        return df

    def createNotification(self, method, params):
        if not self.isConnected():
            raise ConnectionError("Not connected")
        if not type(params) in (list, tuple):
            params = (params,)
        message = (MSGTYPE_NOTIFICATION, method, params)
        ctx = self.getClientContext()
        self.writeMessage(message, ctx)

    def getNextMsgid(self):
        self._next_msgid += 1
        return self._next_msgid

    def rawDataReceived(self, data, context=None):
        try:
            self._unpacker.feed(data)
            for message in self._unpacker:
                self.messageReceived(message, context)
        except Exception as e:
            print(e)

    def messageReceived(self, message, context):
        if message[0] == MSGTYPE_REQUEST:
            return self.requestReceived(message, context)
        if message[0] == MSGTYPE_RESPONSE:
            return self.responseReceived(message)
        if message[0] == MSGTYPE_NOTIFICATION:
            return self.notificationReceived(message)

        return self.undefinedMessageReceived(message)

    def requestReceived(self, message, context):
        try:
            (msgType, msgid, methodName, params) = message
        except ValueError:
            if self._sendErrors:
                raise
            if not len(message) == 4:
                raise InvalidData("Incorrect message length. Expected 4; received %s" % len(message))
            raise InvalidData("Failed to unpack request.")
        except Exception:
            if self._sendErrors:
                raise
            raise InvalidData("Unexpected error. Failed to unpack request.")

        if msgid in self._incoming_requests:
            raise InvalidRequest("Request with msgid '%s' already exists" % msgid)

        result = defer.maybeDeferred(self.callRemoteMethod, msgid, methodName, params)

        self._incoming_requests[msgid] = (result, context)

        result.addCallback(self.respondCallback, msgid)
        result.addErrback(self.respondErrback, msgid)
        result.addBoth(self.endRequest, msgid)
        return result

    def callRemoteMethod(self, msgid, methodName, params):
        try:
            method = self.getRemoteMethod(self, methodName)
        except Exception:
            if self._sendErrors:
                raise
            raise InvalidRequest("Client attempted to call unimplemented method: remote_%s" % methodName)

        send_msgid = False
        try:
            # If the remote_method has a keyword argment called msgid, then pass
            # it the msgid as a keyword argument. 'params' is always a list.
            method_arguments = method.func_code.co_varnames
            if 'msgid' in method_arguments:
                send_msgid = True
        except Exception:
            pass


        try:
            if send_msgid:
                result = method(*params, msgid=msgid)
            else:
                result = method(*params)
        except TypeError:
            import traceback
            traceback.print_exc()
            if self._sendErrors:
                raise
            raise InvalidRequest("Wrong number of arguments for %s" % methodName)

        return result

    def endRequest(self, result, msgid):
        if msgid in self._incoming_requests:
            del self._incoming_requests[msgid]
        return result

    def responseReceived(self, message):
        try:
            (msgType, msgid, error, result) = message
        except Exception as e:
            if self._sendErrors:
                raise
            raise InvalidResponse("Failed to unpack response: %s" % e)

        try:
            df = self._outgoing_requests.pop(msgid)
        except KeyError:
            # There's nowhere to send this error, except the log
            # if self._sendErrors:
            #     raise
            # raise InvalidResponse("Failed to find dispatched request with msgid %s to match incoming repsonse" % msgid)
            pass

        if error is not None:
            # The remote host returned an error, so we need to create a Failure
            # object to pass into the errback chain. The Failure object in turn
            # requires an Exception
            ex = ResponseError(error)
            df.errback(failure.Failure(exc_value=ex))
        else:
            df.callback(result)

    def respondCallback(self, result, msgid):
        try:
            _, ctx = self._incoming_requests[msgid]
        except KeyError:
            ctx = None

        error = None
        response = (MSGTYPE_RESPONSE, msgid, error, result)
        return self.writeMessage(response, ctx)

    def respondErrback(self, f, msgid):
        """
        """
        result = None
        if self._sendErrors:
            error = f.getBriefTraceback()
        else:
            error = f.getErrorMessage()
        self.respondError(msgid, error, result)

    def respondError(self, msgid, error, result=None):
        try:
            _, ctx = self._incoming_requests[msgid]
        except KeyError:
            ctx = None

        response = (MSGTYPE_RESPONSE, msgid, error, result)
        self.writeMessage(response, ctx)

    def writeMessage(self, message, context):
        try:
            message = self._packer.pack(message)
        except Exception:
            if self._sendErrors:
                raise
            raise SerializationError("ERROR: Failed to write message: %s" % message)

        self.writeRawData(message, context)

    def notificationReceived(self, message):
        # Notifications don't expect a return value, so they don't supply a msgid
        msgid = None

        try:
            (msgType, methodName, params) = message
        except Exception as e:
            # Log the error - there's no way to return it for a notification
            print(e)
            return

        try:
            result = defer.maybeDeferred(self.callRemoteMethod, msgid, methodName, params)
            result.addBoth(self.notificationCallback)
        except Exception as e:
            # Log the error - there's no way to return it for a notification
            print(e)
            return

        return None

    def notificationCallback(self, result):
        # Log the result if required
        pass

    def undefinedMessageReceived(self, message):
        raise NotImplementedError("Msgpack received a message of type '%s', " \
                                  "and no method has been specified to " \
                                  "handle this." % message[0])

    def callbackOutgoingRequests(self, func):
        while self._outgoing_requests:
            msgid, d = self._outgoing_requests.popitem()
            func(d)


class MsgpackStreamProtocol(protocol.Protocol, policies.TimeoutMixin, MsgpackBaseProtocol):
    """
    msgpack rpc client/server stream protocol

    @ivar factory: The L{MsgpackClientFactory} or L{MsgpackServerFactory}  which created this L{Msgpack}.
    """
    def __init__(self, factory, sendErrors=False, timeout=None, packerEncoding="utf-8", unpackerEncoding="utf-8"):
        """
        @param factory: factory which created this protocol.
        @type factory: C{protocol.Factory}.
        @param sendErrors: forward any uncaught Exception details to remote peer.
        @type sendErrors: C{bool}.
        @param timeout: idle timeout in seconds before connection will be closed.
        @type timeout: C{int}
        @param packerEncoding: encoding used to encode Python str and unicode. Default is 'utf-8'.
        @type packerEncoding: C{str}
        @param unpackerEncoding: encoding used for decoding msgpack bytes. If None (default), msgpack bytes are deserialized to Python bytes.
        @type unpackerEncoding: C{str}.
        """
        super(MsgpackStreamProtocol, self).__init__(sendErrors, packerEncoding, unpackerEncoding)
        self.factory = factory
        self.setTimeout(timeout)
        self.connected = 0

    def isConnected(self):
        return self.connected == 1

    def writeRawData(self, message, context):
        # transport.write returns None
        self.transport.write(message)

    def getRemoteMethod(self, protocol, methodName):
        return self.factory.getRemoteMethod(self, methodName)

    def getClientContext(self):
        return None

    def dataReceived(self, data):
        self.resetTimeout()

        self.rawDataReceived(data)

    def connectionMade(self):
        # print("connectionMade")
        self.connected = 1
        self.factory.addConnection(self)

    def connectionLost(self, reason=protocol.connectionDone):
        # print("connectionLost")
        self.connected = 0
        self.factory.delConnection(self)

        self.callbackOutgoingRequests(lambda d: d.errback(reason))

    def timeoutConnection(self):
        # print("timeoutConnection")
        self.callbackOutgoingRequests(lambda d: d.errback(TimeoutError("Request timed out")))

        policies.TimeoutMixin.timeoutConnection(self)

    def closeConnection(self):
        self.transport.loseConnection()


class MsgpackDatagramProtocol(protocol.DatagramProtocol, MsgpackBaseProtocol):
    """
    msgpack rpc client/server datagram protocol
    """
    def __init__(self, address=None, handler=None, sendErrors=False, timeout=None, packerEncoding="utf-8", unpackerEncoding="utf-8"):
        super(MsgpackDatagramProtocol, self).__init__(sendErrors, packerEncoding, unpackerEncoding)

        if address:
            if not isinstance(address, tuple) or len(address) != 2:
                raise ValueError('Address must be tuple(host, port)')
            self.conn_address = address
        else:
            self.conn_address = None

        self.handler = handler
        self.timeout = timeout
        self.connected = 0
        self._pendingTimeouts = {}

    def isConnected(self):
        return self.connected == 1

    def writeRawData(self, message, context):
        # transport.write returns None
        self.transport.write(message, context.peer)

    def getRemoteMethod(self, protocol, methodName):
        return getattr(self.handler, "remote_" + methodName)

    def getClientContext(self):
        return Context(peer=self.conn_address)

    def createRequest(self, method, *params):
        return super(MsgpackDatagramProtocol, self).createRequest(method, params)

    def writeMessage(self, message, context):
        if self.timeout:
            msgid = message[1]
            from twisted.internet import reactor
            dc = reactor.callLater(self.timeout, self.timeoutRequest, msgid)
            self._pendingTimeouts[msgid] = dc

        return super(MsgpackDatagramProtocol, self).writeMessage(message, context)

    def responseReceived(self, message):
        msgid = message[1]
        dc = self._pendingTimeouts.get(msgid)
        if dc is not None:
            dc.cancel()

        return super(MsgpackDatagramProtocol, self).responseReceived(message)

    def startProtocol(self):
        if self.conn_address:
            host, port = self.conn_address
            self.transport.connect(host, port)
        self.connected = 1

    def datagramReceived(self, data, address):
        ctx = Context(peer=address)
        self.rawDataReceived(data, ctx)

    # Possibly invoked if there is no server listening on the
    # address to which we are sending.
    def connectionRefused(self):
        # print("Connection refused")
        self.callbackOutgoingRequests(lambda d: d.errback(ConnectionError("Connection refused")))

    def timeoutRequest(self, msgid):
        # print("timeoutRequest")
        d = self._outgoing_requests.get(msgid)
        if d is not None:
            d.errback(TimeoutError("Request timed out"))

    def closeConnection(self):
        self.connected = 0
        self.transport.stopListening()


__all__ = ['MsgpackStreamProtocol', 'MsgpackDatagramProtocol']
