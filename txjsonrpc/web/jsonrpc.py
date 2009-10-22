# Copyright (c) 2001-2004 Twisted Matrix Laboratories.
# See LICENSE for details.
"""
A generic resource for publishing objects via JSON-RPC.

Requires simplejson; can be downloaded from
http://cheeseshop.python.org/pypi/simplejson

API Stability: unstable

Maintainer: U{Duncan McGreggor<mailto:oubiwann@adytum.us>}
"""
from __future__ import nested_scopes
import urlparse
import xmlrpclib

from twisted.web import resource, server
from twisted.internet import defer, reactor
from twisted.python import log
from twisted.web import http

from txjsonrpc import jsonrpclib
from txjsonrpc.jsonrpc import BaseProxy, BaseQueryFactory, BaseSubhandler


# Useful so people don't need to import xmlrpclib directly.
Fault = xmlrpclib.Fault
Binary = xmlrpclib.Binary
Boolean = xmlrpclib.Boolean
DateTime = xmlrpclib.DateTime


class NoSuchFunction(Fault):
    """
    There is no function by the given name.
    """


class Handler:
    """
    Handle a JSON-RPC request and store the state for a request in progress.

    Override the run() method and return result using self.result,
    a Deferred.

    We require this class since we're not using threads, so we can't
    encapsulate state in a running function if we're going  to have
    to wait for results.

    For example, lets say we want to authenticate against twisted.cred,
    run a LDAP query and then pass its result to a database query, all
    as a result of a single JSON-RPC command. We'd use a Handler instance
    to store the state of the running command.
    """

    def __init__(self, resource, *args):
        self.resource = resource # the JSON-RPC resource we are connected to
        self.result = defer.Deferred()
        self.run(*args)

    def run(self, *args):
        # event driven equivalent of 'raise UnimplementedError'
        self.result.errback(NotImplementedError("Implement run() in subclasses"))


class JSONRPC(resource.Resource, BaseSubhandler):
    """
    A resource that implements JSON-RPC.

    Methods published can return JSON-RPC serializable results, Faults,
    Binary, Boolean, DateTime, Deferreds, or Handler instances.

    By default methods beginning with 'jsonrpc_' are published.
    """

    # Error codes for Twisted, if they conflict with yours then
    # modify them at runtime.
    NOT_FOUND = 8001
    FAILURE = 8002

    isLeaf = 1
    separator = '.'

    def __init__(self):
        resource.Resource.__init__(self)
        BaseSubhandler.__init__(self)

    def render(self, request):
        request.content.seek(0, 0)
        # Unmarshal the JSON-RPC data
        content = request.content.read()
        parsed = jsonrpclib.loads(content)
        args, functionPath = parsed.get('params'), parsed.get("method")
        try:
            function = self._getFunction(functionPath)
        except jsonrpclib.Fault, f:
            self._cbRender(f, request)
        else:
            request.setHeader("content-type", "text/json")
            defer.maybeDeferred(function, *args).addErrback(
                self._ebRender
            ).addCallback(
                self._cbRender, request
            )
        return server.NOT_DONE_YET

    def _cbRender(self, result, request):
        if isinstance(result, Handler):
            result = result.result
        if not isinstance(result, jsonrpclib.Fault):
            result = (result,)
        # Convert the result (python) to JSON-RPC
        try:
            s = jsonrpclib.dumps(result)
        except:
            f = jsonrpclib.Fault(self.FAILURE, "can't serialize output")
            s = jsonrpclib.dumps(f)
        request.setHeader("content-length", str(len(s)))
        request.write(s)
        request.finish()

    def _ebRender(self, failure):
        if isinstance(failure.value, jsonrpclib.Fault):
            return failure.value
        log.err(failure)
        return jsonrpclib.Fault(self.FAILURE, "error")




class JSONRPCIntrospection(JSONRPC):
    """
    Implement a JSON-RPC Introspection API.

    By default, the methodHelp method returns the 'help' method attribute,
    if it exists, otherwise the __doc__ method attribute, if it exists,
    otherwise the empty string.

    To enable the methodSignature method, add a 'signature' method attribute
    containing a list of lists. See methodSignature's documentation for the
    format. Note the type strings should be JSON-RPC types, not Python types.
    """

    def __init__(self, parent):
        """
        Implement Introspection support for a JSONRPC server.

        @param parent: the JSONRPC server to add Introspection support to.
        """
        JSONRPC.__init__(self)
        self._jsonrpc_parent = parent

    def jsonrpc_listMethods(self):
        """Return a list of the method names implemented by this server."""
        functions = []
        todo = [(self._jsonrpc_parent, '')]
        while todo:
            obj, prefix = todo.pop(0)
            functions.extend([prefix + name for name in obj._listFunctions()])
            todo.extend([(obj.getSubHandler(name),
                          prefix + name + obj.separator)
                         for name in obj.getSubHandlerPrefixes()])
        return functions

    jsonrpc_listMethods.signature = [['array']]

    def jsonrpc_methodHelp(self, method):
        """
        Return a documentation string describing the use of the given method.
        """
        method = self._jsonrpc_parent._getFunction(method)
        return (getattr(method, 'help', None)
                or getattr(method, '__doc__', None) or '').strip()

    jsonrpc_methodHelp.signature = [['string', 'string']]

    def jsonrpc_methodSignature(self, method):
        """
        Return a list of type signatures.

        Each type signature is a list of the form [rtype, type1, type2, ...]
        where rtype is the return type and typeN is the type of the Nth
        argument. If no signature information is available, the empty
        string is returned.
        """
        method = self._jsonrpc_parent._getFunction(method)
        return getattr(method, 'signature', None) or ''

    jsonrpc_methodSignature.signature = [['array', 'string'],
                                        ['string', 'string']]


def addIntrospection(jsonrpc):
    """
    Add Introspection support to an JSONRPC server.

    @param jsonrpc: The jsonrpc server to add Introspection support to.
    """
    jsonrpc.putSubHandler('system', JSONRPCIntrospection(jsonrpc))


class QueryProtocol(http.HTTPClient):

    def connectionMade(self):
        self.sendCommand('POST', self.factory.path)
        self.sendHeader('User-Agent', 'Twisted/JSONRPClib')
        self.sendHeader('Host', self.factory.host)
        self.sendHeader('Content-type', 'text/json')
        self.sendHeader('Content-length', str(len(self.factory.payload)))
        if self.factory.user:
            auth = '%s:%s' % (self.factory.user, self.factory.password)
            auth = auth.encode('base64').strip()
            self.sendHeader('Authorization', 'Basic %s' % (auth,))
        self.endHeaders()
        self.transport.write(self.factory.payload)

    def handleStatus(self, version, status, message):
        if status != '200':
            self.factory.badStatus(status, message)

    def handleResponse(self, contents):
        self.factory.parseResponse(contents)


class QueryFactory(BaseQueryFactory):

    deferred = None
    protocol = QueryProtocol

    def __init__(self, path, host, method, user=None, password=None,
                 version=jsonrpclib.VERSION_PRE1, *args):
        BaseQueryFactory.__init__(self, method, version, *args)
        self.path, self.host = path, host
        self.user, self.password = user, password


class Proxy(BaseProxy):
    """
    A Proxy for making remote JSON-RPC calls.

    Pass the URL of the remote JSON-RPC server to the constructor.

    Use proxy.callRemote('foobar', *args) to call remote method
    'foobar' with *args.
    """

    def __init__(self, url, user=None, password=None,
                 version=jsonrpclib.VERSION_PRE1, factoryClass=QueryFactory):
        """
        @type url: C{str}
        @param url: The URL to which to post method calls.  Calls will be made
        over SSL if the scheme is HTTPS.  If netloc contains username or
        password information, these will be used to authenticate, as long as
        the C{user} and C{password} arguments are not specified.

        @type user: C{str} or None
        @param user: The username with which to authenticate with the server
        when making calls.  If specified, overrides any username information
        embedded in C{url}.  If not specified, a value may be taken from C{url}
        if present.

        @type password: C{str} or None
        @param password: The password with which to authenticate with the
        server when making calls.  If specified, overrides any password
        information embedded in C{url}.  If not specified, a value may be taken
        from C{url} if present.

        @type version: C{int}
        @param version: The version indicates which JSON-RPC spec to support.
        The available choices are jsonrpclib.VERSION*. The default is to use
        the version of the spec that txJSON-RPC was originally released with,
        pre-Version 1.0.
        """
        BaseProxy.__init__(self, version, factoryClass)
        scheme, netloc, path, params, query, fragment = urlparse.urlparse(url)
        netlocParts = netloc.split('@')
        if len(netlocParts) == 2:
            userpass = netlocParts.pop(0).split(':')
            self.user = userpass.pop(0)
            try:
                self.password = userpass.pop(0)
            except:
                self.password = None
        else:
            self.user = self.password = None
        hostport = netlocParts[0].split(':')
        self.host = hostport.pop(0)
        try:
            self.port = int(hostport.pop(0))
        except:
            self.port = None
        self.path = path
        if self.path in ['', None]:
            self.path = '/'
        self.secure = (scheme == 'https')
        if user is not None:
            self.user = user
        if password is not None:
            self.password = password

    def callRemote(self, method, *args, **kwargs):
        version = self._getVersion(kwargs)
        factoryClass = self._getFactoryClass(kwargs)
        factory = factoryClass(self.path, self.host, method, self.user,
            self.password, version, *args)
        if self.secure:
            from twisted.internet import ssl
            reactor.connectSSL(self.host, self.port or 443,
                               factory, ssl.ClientContextFactory())
        else:
            reactor.connectTCP(self.host, self.port or 80, factory)
        return factory.deferred

__all__ = ["JSONRPC", "Handler", "Proxy"]
