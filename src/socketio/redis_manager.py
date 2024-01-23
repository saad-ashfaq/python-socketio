import logging
import pickle
import time

try:
    import redis
except ImportError:
    redis = None

from .pubsub_manager import PubSubManager

logger = logging.getLogger('socketio')


class RedisManager(PubSubManager):  # pragma: no cover
    """Redis based client manager.

    This class implements a Redis backend for event sharing across multiple
    processes. Only kept here as one more example of how to build a custom
    backend, since the kombu backend is perfectly adequate to support a Redis
    message queue.

    To use a Redis backend, initialize the :class:`Server` instance as
    follows::

        url = 'redis://hostname:port/0'
        server = socketio.Server(client_manager=socketio.RedisManager(url))

    :param url: The connection URL for the Redis server. For a default Redis
                store running on the same host, use ``redis://``.  To use an
                SSL connection, use ``rediss://``.
    :param channel: The channel name on which the server sends and receives
                    notifications. Must be the same in all the servers.
    :param write_only: If set to ``True``, only initialize to emit events. The
                       default of ``False`` initializes the class for emitting
                       and receiving.
    :param redis_options: additional keyword arguments to be passed to
                          ``Redis.from_url()``.
    """
    name = 'redis'

    def __init__(self, url='redis://localhost:6379/0', channel='socketio',
                 write_only=False, logger=None, redis_options=None):
        if redis is None:
            raise RuntimeError('Redis package is not installed '
                               '(Run "pip install redis" in your '
                               'virtualenv).')
        self.redis_url = url
        self.redis_options = redis_options or {}
        self._redis_connect()
        super(RedisManager, self).__init__(channel=channel,
                                           write_only=write_only,
                                           logger=logger)

    def initialize(self):
        super(RedisManager, self).initialize()

        monkey_patched = True
        if self.server.async_mode == 'eventlet':
            from eventlet.patcher import is_monkey_patched
            monkey_patched = is_monkey_patched('socket')
        elif 'gevent' in self.server.async_mode:
            from gevent.monkey import is_module_patched
            monkey_patched = is_module_patched('socket')
        if not monkey_patched:
            raise RuntimeError(
                'Redis requires a monkey patched socket library to work '
                'with ' + self.server.async_mode)

    def _redis_connect(self):
        self.redis = redis.Redis.from_url(self.redis_url,
                                          **self.redis_options)
        self.pubsub = self.redis.pubsub(ignore_subscribe_messages=True)

    def _publish(self, data):
        retry = True
        while True:
            try:
                if not retry:
                    self._redis_connect()
                return self.redis.publish(self.channel, pickle.dumps(data))
            except redis.exceptions.RedisError:
                if retry:
                    logger.error('Cannot publish to redis... retrying')
                    retry = False
                else:
                    logger.error('Cannot publish to redis... giving up')
                    break

    def _redis_listen_with_retries(self):
        retry_sleep = 1
        connect = False
        while True:
            try:
                if connect:
                    self._redis_connect()
                    self.pubsub.subscribe(self.channel)
                    retry_sleep = 1
                for message in self.pubsub.listen():
                    yield message
            except redis.exceptions.RedisError:
                logger.error('Cannot receive from redis... '
                             'retrying in {} secs'.format(retry_sleep))
                connect = True
                time.sleep(retry_sleep)
                retry_sleep *= 2
                if retry_sleep > 60:
                    retry_sleep = 60

    def _listen(self):
        channel = self.channel.encode('utf-8')
        self.pubsub.subscribe(self.channel)
        for message in self._redis_listen_with_retries():
            if message['channel'] == channel and \
                    message['type'] == 'message' and 'data' in message:
                yield message['data']
        self.pubsub.unsubscribe(self.channel)

import socket
class RedisSentinelManager(RedisManager):
    name = 'sentinel'

    def __init__(self, sentinels=[('localhost', 26379)], channel='socketio',
                 master_name='mymaster', socket_timeout=0.1, db=0,
                 password=None, write_only=False, logger=None):
        if redis is None:
            raise RuntimeError('Redis package is not installed '
                               '(Run "pip install redis" in your '
                               'virtualenv).')
        self.sentinel = redis.Sentinel(sentinels, socket_timeout=socket_timeout)
        self.failover = redis.Redis(host=sentinels[0][0], port=sentinels[0][1]).pubsub()
        self.failover.psubscribe('+switch-master')
        self.redis_options = {
            'master_name': master_name,
            'socket_timeout': socket_timeout,
            'db': db,
            'password': password
        }
        self._redis_connect()
        super(RedisManager, self).__init__(channel=channel,
                                           write_only=write_only,
                                           logger=logger)

    def _redis_connect(self):
        master_ip, master_port = self.sentinel.discover_master(self.redis_options['master_name'])
        self.redis_url = 'redis://:' + self.redis_options['password'] + '@' + master_ip + ':' + str(master_port) + '/' + str(self.redis_options['db'])
        self.redis = redis.Redis.from_url(
            self.redis_url,
            socket_keepalive=True if hasattr(socket, 'TCP_KEEPIDLE') else None,
            socket_keepalive_options={
                socket.TCP_KEEPIDLE: 5,
                socket.TCP_KEEPINTVL: 3,
                socket.TCP_KEEPCNT: 3
            } if hasattr(socket, 'TCP_KEEPIDLE') else None
        )
        self.pubsub = self.redis.pubsub(ignore_subscribe_messages=True)

    def _publish(self, data):
        retry = True
        while True:
            try:
                self._redis_connect()
                return self.redis.publish(self.channel, pickle.dumps(data))
            except redis.exceptions.RedisError:
                if retry:
                    logger.error('Cannot publish to redis... retrying')
                    retry = False
                else:
                    logger.error('Cannot publish to redis... giving up')
                    break

    def _redis_listen_with_retries(self):
        retry_sleep = 1
        connect = False
        while True:
            try:
                if connect:
                    self._redis_connect()
                    self.pubsub.subscribe(self.channel)
                    retry_sleep = 1
                if self.failover.get_message(ignore_subscribe_messages=True):
                    print("FAILOVER")
                    self._redis_connect()
                    self.pubsub.subscribe(self.channel)
                message = self.pubsub.get_message(ignore_subscribe_messages=True)
                if message:
                    yield message
                time.sleep(0.001)
            except redis.exceptions.RedisError as e:
                print(e)
                logger.error('Cannot receive from redis... '
                             'retrying in {} secs'.format(retry_sleep))
                connect = True
                time.sleep(retry_sleep)
                retry_sleep *= 2
                if retry_sleep > 60:
                    retry_sleep = 60
