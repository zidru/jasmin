#!/usr/bin/python

import sys
import signal
import syslog
import os
from twisted.python import usage
from jasmin.managers.clients import SMPPClientManagerPB
from jasmin.managers.configs import SMPPClientPBConfig
from jasmin.queues.configs import AmqpConfig
from jasmin.queues.factory import AmqpFactory
from jasmin.protocols.smpp.configs import SMPPServerConfig
from jasmin.protocols.smpp.factory import SMPPServerFactory
from jasmin.protocols.http.configs import HTTPApiConfig
from jasmin.protocols.http.server import HTTPApi
from jasmin.tools.cred.portal import SmppsRealm
from jasmin.tools.cred.checkers import RouterAuthChecker
from jasmin.routing.router import RouterPB
from jasmin.routing.configs import RouterPBConfig, deliverSmThrowerConfig, DLRThrowerConfig
from jasmin.routing.throwers import deliverSmThrower, DLRThrower
from jasmin.redis.configs import RedisForJasminConfig
from jasmin.redis.client import ConnectionWithConfiguration
from jasmin.protocols.cli.factory import JCliFactory
from jasmin.protocols.cli.configs import JCliConfig
from jasmin.interceptor.proxies import InterceptorPBProxy
from jasmin.interceptor.configs import InterceptorPBClientConfig
from twisted.cred import portal
from twisted.cred.checkers import AllowAnonymousAccess, InMemoryUsernamePasswordDatabaseDontUse
from jasmin.tools.cred.portal import JasminPBRealm
from jasmin.tools.spread.pb import JasminPBPortalRoot
from twisted.web import server
from twisted.spread import pb
from twisted.internet import reactor, defer

# Related to travis-ci builds
ROOT_PATH = os.getenv('ROOT_PATH', '/')

class Options(usage.Options):

    optParameters = [
        ['config', 'c', '%s/etc/jasmin/jasmin.cfg' % ROOT_PATH,
         'Jasmin configuration file'],
        ['username', 'u', None,
         'jCli username used to load configuration profile on startup'],
        ['password', 'p', None,
         'jCli password used to load configuration profile on startup'],
        ]

    optFlags = [
        ['disable-smpp-server', None, 'Do not start SMPP Server service'],
        ['disable-dlr-thrower', None, 'Do not DLR Thrower service'],
        ['disable-deliver-thrower', None, 'Do not DeliverSm Thrower service'],
        ['disable-http-api', None, 'Do not HTTP API'],
        ['disable-jcli', None, 'Do not jCli console'],
        ['enable-interceptor-client', None, 'Start Interceptor client'],
    ]

class JasminDaemon(object):

    def __init__(self, opt):
        self.options = options
        self.components = {}

    @defer.inlineCallbacks
    def startRedisClient(self):
        "Start AMQP Broker"
        RedisForJasminConfigInstance = RedisForJasminConfig(self.options['config'])
        self.components['rc'] = yield ConnectionWithConfiguration(RedisForJasminConfigInstance)
        # Authenticate and select db
        if RedisForJasminConfigInstance.password is not None:
            yield self.components['rc'].auth(RedisForJasminConfigInstance.password)
            yield self.components['rc'].select(RedisForJasminConfigInstance.dbid)

    def stopRedisClient(self):
        "Stop AMQP Broker"
        return self.components['rc'].disconnect()

    def startAMQPBrokerService(self):
        "Start AMQP Broker"

        AMQPServiceConfigInstance = AmqpConfig(self.options['config'])
        self.components['amqp-broker-factory'] = AmqpFactory(AMQPServiceConfigInstance)
        self.components['amqp-broker-factory'].preConnect()

        # Add service
        self.components['amqp-broker-client'] = reactor.connectTCP(
            AMQPServiceConfigInstance.host,
            AMQPServiceConfigInstance.port,
            self.components['amqp-broker-factory'])

    def stopAMQPBrokerService(self):
        "Stop AMQP Broker"

        return self.components['amqp-broker-client'].disconnect()

    def startRouterPBService(self):
        "Start Router PB server"

        RouterPBConfigInstance = RouterPBConfig(self.options['config'])
        self.components['router-pb-factory'] = RouterPB()
        self.components['router-pb-factory'].setConfig(RouterPBConfigInstance)

        # Set authentication portal
        p = portal.Portal(JasminPBRealm(self.components['router-pb-factory']))
        if RouterPBConfigInstance.authentication:
            c = InMemoryUsernamePasswordDatabaseDontUse()
            c.addUser(RouterPBConfigInstance.admin_username,
                      RouterPBConfigInstance.admin_password)
            p.registerChecker(c)
        else:
            p.registerChecker(AllowAnonymousAccess())
        jPBPortalRoot = JasminPBPortalRoot(p)

        # Add service
        self.components['router-pb-server'] = reactor.listenTCP(
            RouterPBConfigInstance.port,
            pb.PBServerFactory(jPBPortalRoot),
            interface=RouterPBConfigInstance.bind)

        # AMQP Broker is used to listen to deliver_sm/dlr queues
        return self.components['router-pb-factory'].addAmqpBroker(self.components['amqp-broker-factory'])

    def stopRouterPBService(self):
        "Stop Router PB server"
        return self.components['router-pb-server'].stopListening()

    def startSMPPClientManagerPBService(self):
        "Start SMPP Client Manager PB server"

        SMPPClientPBConfigInstance = SMPPClientPBConfig(self.options['config'])
        self.components['smppcm-pb-factory'] = SMPPClientManagerPB()
        self.components['smppcm-pb-factory'].setConfig(SMPPClientPBConfigInstance)

        # Set authentication portal
        p = portal.Portal(JasminPBRealm(self.components['smppcm-pb-factory']))
        if SMPPClientPBConfigInstance.authentication:
            c = InMemoryUsernamePasswordDatabaseDontUse()
            c.addUser(SMPPClientPBConfigInstance.admin_username, SMPPClientPBConfigInstance.admin_password)
            p.registerChecker(c)
        else:
            p.registerChecker(AllowAnonymousAccess())
        jPBPortalRoot = JasminPBPortalRoot(p)

        # Add service
        self.components['smppcm-pb-server'] = reactor.listenTCP(
            SMPPClientPBConfigInstance.port,
            pb.PBServerFactory(jPBPortalRoot),
            interface=SMPPClientPBConfigInstance.bind)

        # AMQP Broker is used to listen to submit_sm queues and publish to deliver_sm/dlr queues
        self.components['smppcm-pb-factory'].addAmqpBroker(self.components['amqp-broker-factory'])
        self.components['smppcm-pb-factory'].addRedisClient(self.components['rc'])
        self.components['smppcm-pb-factory'].addRouterPB(self.components['router-pb-factory'])

        # Add interceptor if enabled:
        if 'interceptor-pb-client' in self.components:
            self.components['smppcm-pb-factory'].addInterceptorPBClient(
                self.components['interceptor-pb-client'])

    def stopSMPPClientManagerPBService(self):
        "Stop SMPP Client Manager PB server"
        return self.components['smppcm-pb-server'].stopListening()

    def startSMPPServerService(self):
        "Start SMPP Server"

        SMPPServerConfigInstance = SMPPServerConfig(self.options['config'])

        # Set authentication portal
        p = portal.Portal(
            SmppsRealm(
                SMPPServerConfigInstance.id,
                self.components['router-pb-factory']))
        p.registerChecker(RouterAuthChecker(self.components['router-pb-factory']))

        # SMPPServerFactory init
        self.components['smpp-server-factory'] = SMPPServerFactory(
            SMPPServerConfigInstance,
            auth_portal=p,
            RouterPB=self.components['router-pb-factory'],
            SMPPClientManagerPB=self.components['smppcm-pb-factory'])

        # Start server
        self.components['smpp-server'] = reactor.listenTCP(
            SMPPServerConfigInstance.port,
            self.components['smpp-server-factory'],
            interface=SMPPServerConfigInstance.bind)

        # Add interceptor if enabled:
        if 'interceptor-pb-client' in self.components:
            self.components['smpp-server-factory'].addInterceptorPBClient(
                self.components['interceptor-pb-client'])

    def stopSMPPServerService(self):
        "Stop SMPP Server"
        return self.components['smpp-server'].stopListening()

    def startdeliverSmThrowerService(self):
        "Start deliverSmThrower"

        deliverThrowerConfigInstance = deliverSmThrowerConfig(self.options['config'])
        self.components['deliversm-thrower'] = deliverSmThrower()
        self.components['deliversm-thrower'].setConfig(deliverThrowerConfigInstance)
        self.components['deliversm-thrower'].addSmpps(self.components['smpp-server-factory'])

        # AMQP Broker is used to listen to deliver_sm queue
        return self.components['deliversm-thrower'].addAmqpBroker(self.components['amqp-broker-factory'])

    def stopdeliverSmThrowerService(self):
        "Stop deliverSmThrower"
        return self.components['deliversm-thrower'].stopService()

    def startDLRThrowerService(self):
        "Start DLRThrower"

        DLRThrowerConfigInstance = DLRThrowerConfig(self.options['config'])
        self.components['dlr-thrower'] = DLRThrower()
        self.components['dlr-thrower'].setConfig(DLRThrowerConfigInstance)
        self.components['dlr-thrower'].addSmpps(self.components['smpp-server-factory'])

        # AMQP Broker is used to listen to DLRThrower queue
        return self.components['dlr-thrower'].addAmqpBroker(self.components['amqp-broker-factory'])

    def stopDLRThrowerService(self):
        "Stop DLRThrower"
        return self.components['dlr-thrower'].stopService()

    def startHTTPApiService(self):
        "Start HTTP Api"

        httpApiConfigInstance = HTTPApiConfig(self.options['config'])

        # Add interceptor if enabled:
        if 'interceptor-pb-client' in self.components:
            interceptorpb_client = self.components['interceptor-pb-client']
        else:
            interceptorpb_client = None

        self.components['http-api-factory'] = HTTPApi(
            self.components['router-pb-factory'],
            self.components['smppcm-pb-factory'],
            httpApiConfigInstance,
            interceptorpb_client)

        self.components['http-api-server'] = reactor.listenTCP(
            httpApiConfigInstance.port,
            server.Site(self.components['http-api-factory'], logPath=httpApiConfigInstance.access_log),
            interface=httpApiConfigInstance.bind)

    def stopHTTPApiService(self):
        "Stop HTTP Api"
        return self.components['http-api-server'].stopListening()

    def startJCliService(self):
        "Start jCli console server"
        loadConfigProfileWithCreds = {
            'username': self.options['username'],
            'password': self.options['password']}
        JCliConfigInstance = JCliConfig(self.options['config'])
        JCli_f = JCliFactory(
            JCliConfigInstance,
            self.components['smppcm-pb-factory'],
            self.components['router-pb-factory'],
            loadConfigProfileWithCreds)

        self.components['jcli-server'] = reactor.listenTCP(
            JCliConfigInstance.port,
            JCli_f,
            interface=JCliConfigInstance.bind)

    def stopJCliService(self):
        "Stop jCli console server"
        return self.components['jcli-server'].stopListening()

    def startInterceptorPBClient(self):
        "Start Interceptor client"

        InterceptorPBClientConfigInstance = InterceptorPBClientConfig(self.options['config'])
        self.components['interceptor-pb-client'] = InterceptorPBProxy()

        return self.components['interceptor-pb-client'].connect(
            InterceptorPBClientConfigInstance.host,
            InterceptorPBClientConfigInstance.port,
            InterceptorPBClientConfigInstance.username,
            InterceptorPBClientConfigInstance.password,
            retry=True)

    def stopInterceptorPBClient(self):
        "Stop Interceptor client"

        if self.components['interceptor-pb-client'].isConnected:
            return self.components['interceptor-pb-client'].disconnect()

    @defer.inlineCallbacks
    def start(self):
        "Start Jasmind daemon"
        syslog.syslog(syslog.LOG_INFO, "Starting Jasmin Daemon ...")

        # Requirements check begin:
        try:
            ########################################################
            # [optional] Start Interceptor client
            if self.options['enable-interceptor-client']:
                yield self.startInterceptorPBClient()
                syslog.syslog(syslog.LOG_INFO, "  Interceptor client Started.")
        except Exception, e:
            syslog.syslog(syslog.LOG_ERR, "  Cannot connect to interceptor: %s" % e)
        # Requirements check end.

        ########################################################
        # Connect to redis server
        yield self.startRedisClient()
        syslog.syslog(syslog.LOG_INFO, "  RedisClient started.")

        ########################################################
        # Start AMQP Broker
        self.startAMQPBrokerService()
        yield self.components['amqp-broker-factory'].getChannelReadyDeferred()
        syslog.syslog(syslog.LOG_INFO, "  AMQP Broker connected.")

        ########################################################
        # Start Router PB server
        yield self.startRouterPBService()
        syslog.syslog(syslog.LOG_INFO, "  RouterPB Started.")

        ########################################################
        # Start SMPP Client connector manager and add rc
        self.startSMPPClientManagerPBService()
        syslog.syslog(syslog.LOG_INFO, "  SMPPClientManagerPB Started.")

        ########################################################
        # [optional] Start SMPP Server
        if not self.options['disable-smpp-server']:
            self.startSMPPServerService()
            syslog.syslog(syslog.LOG_INFO, "  SMPPServer Started.")

        ########################################################
        # [optional] Start deliverSmThrower
        if not self.options['disable-deliver-thrower']:
            yield self.startdeliverSmThrowerService()
            syslog.syslog(syslog.LOG_INFO, "  deliverSmThrower Started.")

        ########################################################
        # [optional] Start DLRThrower
        if not self.options['disable-dlr-thrower']:
            yield self.startDLRThrowerService()
            syslog.syslog(syslog.LOG_INFO, "  DLRThrower Started.")

        ########################################################
        # [optional] Start HTTP Api
        if not self.options['disable-http-api']:
            self.startHTTPApiService()
            syslog.syslog(syslog.LOG_INFO, "  HTTPApi Started.")

        ########################################################
        # [optional] Start JCli server
        if not self.options['disable-jcli']:
            self.startJCliService()
            syslog.syslog(syslog.LOG_INFO, "  jCli Started.")

    @defer.inlineCallbacks
    def stop(self):
        "Stop Jasmind daemon"
        syslog.syslog(syslog.LOG_INFO, "Stopping Jasmin Daemon ...")

        if 'jcli-server' in self.components:
            yield self.stopJCliService()
            syslog.syslog(syslog.LOG_INFO, "  jCli stopped.")

        if 'http-api-server' in self.components:
            yield self.stopHTTPApiService()
            syslog.syslog(syslog.LOG_INFO, "  HTTPApi stopped.")

        if 'dlr-thrower' in self.components:
            yield self.stopDLRThrowerService()
            syslog.syslog(syslog.LOG_INFO, "  DLRThrower stopped.")

        if 'deliversm-thrower' in self.components:
            yield self.stopdeliverSmThrowerService()
            syslog.syslog(syslog.LOG_INFO, "  deliverSmThrower stopped.")

        if 'smpp-server' in self.components:
            yield self.stopSMPPServerService()
            syslog.syslog(syslog.LOG_INFO, "  SMPPServer stopped.")

        if 'smppcm-pb-server' in self.components:
            yield self.stopSMPPClientManagerPBService()
            syslog.syslog(syslog.LOG_INFO, "  SMPPClientManagerPB stopped.")

        if 'router-pb-server' in self.components:
            yield self.stopRouterPBService()
            syslog.syslog(syslog.LOG_INFO, "  RouterPB stopped.")

        if 'amqp-broker-client' in self.components:
            yield self.stopAMQPBrokerService()
            syslog.syslog(syslog.LOG_INFO, "  AMQP Broker disconnected.")

        if 'rc' in self.components:
            yield self.stopRedisClient()
            syslog.syslog(syslog.LOG_INFO, "  RedisClient stopped.")

        # Shutdown requirements:
        if 'interceptor-pb-client' in self.components:
            yield self.stopInterceptorPBClient()
            syslog.syslog(syslog.LOG_INFO, "  Interceptor client stopped.")

        reactor.stop()

    def sighandler_stop(self, signum, frame):
        "Handle stop signal cleanly"
        syslog.syslog(syslog.LOG_INFO, "Received signal to stop Jasmin Daemon")

        return self.stop()

if __name__ == '__main__':
    try:
        options = Options()
        options.parseOptions()
    except usage.UsageError, errortext:
        print '%s: %s' % (sys.argv[0], errortext)
        print '%s: Try --help for usage details.' % (sys.argv[0])
    else:
        ja_d = JasminDaemon(options)
        # Setup signal handlers
        signal.signal(signal.SIGINT, ja_d.sighandler_stop)
        # Start JasminDaemon
        ja_d.start()

        reactor.run()
