#!/usr/bin/env python
#
# Copyright 2009 Phus Lu
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""Blocking and non-blocking Redis client implementations using IOStream."""

import collections
import cStringIO
import logging
import socket

from tornado.ioloop import IOLoop
from tornado.iostream import IOStream
from tornado.util import bytes_type, b

def encode(request):
    '''print repr(encode(('SET', 'mykey', 123)))'''
    assert type(request) is tuple
    data = '*%d\r\n' % len(request) + ''.join(['$%d\r\n%s\r\n' % (len(str(x)), x) for x in request])
    return data

def decode(data):
    '''print decode('*4\r\n$3\r\nfoo\r\n$3\r\nbar\r\n$5\r\nhello\r\n:42\r\n')'''
    assert type(data) is bytes_type
    iodata = cStringIO.StringIO(data)
    c = iodata.read(1)
    if c == '+':
        return True
    elif c == '-':
        error = iodata.readline().rstrip()
        raise RedisError(error)
    elif c == ':':
        return int(iodata.readline())
    elif c == '$':
        number = int(iodata.readline())
        if number == -1:
            return None
        else:
            data = iodata.read(number)
            iodata.read(2)
            return data
    elif c == '*':
        number = int(iodata.readline())
        if number == -1:
            return None
        else:
            result = []
            while number:
                c = iodata.read(1)
                if c == '$':
                    length  = int(iodata.readline())
                    element = iodata.read(length)
                    iodata.read(2)
                    result.append(element)
                else:
                    if c == ':':
                        element = int(iodata.readline())
                    else:
                        element = iodata.readline()[:-2]
                    result.append(element)
                number -= 1
            return result
    else:
        raise RedisError('Redis Reply TypeError: bulk cannot startswith %r', c)

class AsyncRedisClient(object):
    """An non-blocking Redis client.

    Example usage::

        import ioloop

        def handle_request(response):
            print 'Redis reply: %r' % result
            ioloop.IOLoop.instance().stop()

        redis_client = httpclient.AsyncHTTPClient(('127.0.0.1', 6379))
        redis_client.fetch(('set', 'foo', 'bar'), handle_result)
        ioloop.IOLoop.instance().start()

    This class implements a Redis client on top of Tornado's IOStreams.
    It does not currently implement all applicable parts of the Redis
    specification, but it does enough to work with major redis server APIs
    (mostly tested against the LIST/HASH/PUBSUB API so far).

    This class has not been tested extensively in production and
    should be considered somewhat experimental as of the release of
    tornado 1.2.  It is intended to become the default tornado
    AsyncRedisClient implementation.
    """

    def __init__(self, address, io_loop=None):
        """Creates a AsyncRedisClient.

        address is the tuple of redis server address that can be connect by
        IOStream. It defaults to ('127.0.0.1', 6379)
        """
        self.address         = address
        self.io_loop         = io_loop or IOLoop.instance()
        self._callback_queue = collections.deque()
        self._callback       = None
        self._result_queue   = collections.deque()
        self.socket          = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.stream          = IOStream(self.socket)
        self.stream.connect(self.address, self._wait_result)

    def close(self):
        """Destroys this redis client, freeing any file descriptors used.
        Not needed in normal use, but may be helpful in unittests that
        create and destroy redis clients.  No other methods may be called
        on the AsyncRedisClient after close().
        """
        self.stream.close()

    def fetch(self, request, callback):
        """Executes a request, calling callback with an redis `result`.

        The request shuold be a string tuple. like ('set', 'foo', 'bar')

        If an error occurs during the fetch, a `RedisError` exception will
        throw out. You can use try...except to catch the exception (if any)
        in the callback.
        """
        data = encode(request)
        self.stream.write(data)
        self._callback = callback
        self._callback_queue.append(callback)

    def _wait_result(self):
        """Read a completed result data from the redis server."""
        self.stream.read_until(bytes_type('\r\n'), self._on_read_first_line)

    def _maybe_callback(self, data):
        """Try call callback in _callback_queue when we read a redis result."""
        try:
            if self._result_queue:
                self._result_queue.append(data)
                data = self._result_queue.popleft()
            if self._callback_queue:
                callback = self._callback_queue.popleft()
            else:
                callback = self._callback
            result = decode(data)
            callback(result)
        except Exception:
            logging.error('Uncaught callback exception', exc_info=True)
            raise
        finally:
            self._wait_result()

    def _on_read_first_line(self, data):
        self._data = data
        c = data[0]
        if c in '+-:':
            self._maybe_callback(self._data)
        elif c == '$':
            if data[:3] == '$-1':
                self._maybe_callback(self._data)
            else:
                length = int(data[1:])
                self.stream.read_bytes(length+2, self._on_read_bulk_line)
        elif c == '*':
            if data[1] in '-0' :
                self._maybe_callback(self._data)
            else:
                self._multibulk_number = int(data[1:])
                self.stream.read_until(bytes_type('\r\n'), self._on_read_multibulk_linehead)

    def _on_read_bulk_line(self, data):
        self._data += data
        self._maybe_callback(self._data)

    def _on_read_multibulk_linehead(self, data):
        self._data += data
        c = data[0]
        if c == '$':
            length = int(data[1:])
            self.stream.read_bytes(length+2, self._on_read_multibulk_linebody)
        else:
            self._maybe_callback(self._data)

    def _on_read_multibulk_linebody(self, data):
        self._data += data
        self._multibulk_number -= 1
        if self._multibulk_number:
            self.stream.read_until(bytes_type('\r\n'), self._on_read_multibulk_linehead)
        else:
            self._maybe_callback(self._data)

class RedisError(Exception):
    """Exception thrown for an unsuccessful Redis request.

    Attributes:

    data - Redis error data error code, e.g. -(ERR).
    """
    def __init__(self, data, message=None):
        self.data = data
        message = message or 'Unknown Redis Error'
        Exception.__init__(self, 'Redis Error %s:%r' % (message, self.data))


def main():
    import time
    def handle_result(result):
        print 'Redis reply: %r' % result
    redis_client = AsyncRedisClient(('127.0.0.1', 6379))
    redis_client.fetch(('lpush', 'l', 1), handle_result)
    redis_client.fetch(('lpush', 'l', 2), handle_result)
    redis_client.fetch(('lrange', 'l', 0, -1), handle_result)
    IOLoop.instance().add_timeout(time.time()+1, lambda:redis_client.fetch(('llen', 'l'), handle_result))
    IOLoop.instance().add_timeout(time.time()+2, lambda:redis_client.fetch(('psubscribe', '*'), handle_result))
    IOLoop.instance().start()

if __name__ == '__main__':
    main()
