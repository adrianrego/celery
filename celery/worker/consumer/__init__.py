# -*- coding: utf-8 -*-
"""
celery.worker.consumer
~~~~~~~~~~~~~~~~~~~~~~

This module contains the component responsible for consuming messages
from the broker, processing the messages and keeping the broker connections
up and running.

"""
from __future__ import absolute_import

import logging
import socket

from kombu.syn import _detect_environment
from kombu.utils.encoding import safe_repr

from celery.app import app_or_default
from celery.task.trace import build_tracer
from celery.utils.timer2 import default_timer, to_timestamp
from celery.utils.functional import noop
from celery.utils.imports import qualname
from celery.utils.log import get_logger
from celery.utils.text import dump_body
from celery.utils.timeutils import humanize_seconds
from celery.worker import state
from celery.worker.state import maybe_shutdown
from celery.worker.bootsteps import Namespace as _NS, StartStopComponent, CLOSE

from . import loops

logger = get_logger(__name__)
info, warn, error, crit = (logger.info, logger.warn,
                           logger.error, logger.critical)
task_reserved = state.task_reserved

CONNECTION_RETRY = """\
consumer: Connection to broker lost. \
Trying to re-establish the connection...\
"""

CONNECTION_RETRY_STEP = """\
Trying again {when}...\
"""

CONNECTION_ERROR = """\
consumer: Cannot connect to %s: %s.
%s
"""

CONNECTION_FAILOVER = """\
Will retry using next failover.\
"""

UNKNOWN_FORMAT = """\
Received and deleted unknown message. Wrong destination?!?

The full contents of the message body was: %s
"""

#: Error message for when an unregistered task is received.
UNKNOWN_TASK_ERROR = """\
Received unregistered task of type %s.
The message has been ignored and discarded.

Did you remember to import the module containing this task?
Or maybe you are using relative imports?
Please see http://bit.ly/gLye1c for more information.

The full contents of the message body was:
%s
"""

#: Error message for when an invalid task message is received.
INVALID_TASK_ERROR = """\
Received invalid task message: %s
The message has been ignored and discarded.

Please ensure your message conforms to the task
message protocol as described here: http://bit.ly/hYj41y

The full contents of the message body was:
%s
"""

MESSAGE_REPORT = """\
body: {0} {{content_type:{1} content_encoding:{2} delivery_info:{3}}}\
"""


def debug(msg, *args, **kwargs):
    logger.debug('consumer: {0}'.format(msg), *args, **kwargs)


class Component(StartStopComponent):
    name = 'worker.consumer'
    last = True

    def create(self, w):
        prefetch_count = w.concurrency * w.prefetch_multiplier
        c = w.consumer = self.instantiate(w.consumer_cls,
                w.ready_queue,
                hostname=w.hostname,
                send_events=w.send_events,
                init_callback=w.ready_callback,
                initial_prefetch_count=prefetch_count,
                pool=w.pool,
                timer=w.timer,
                app=w.app,
                controller=w,
                hub=w.hub)
        return c


class Namespace(_NS):
    name = 'consumer'
    builtin_boot_steps = ('celery.worker.consumer.components', )

    def shutdown(self, parent):
        delayed = self._shutdown_step(parent, parent.components, force=False)
        self._shutdown_step(parent, delayed, force=True)

    def _shutdown_step(self, parent, components, force=False):
        delayed = []
        for component in components:
            if component:
                logger.debug('Shutdown %s...', qualname(component))
                if not force and getattr(component, 'delay_shutdown', False):
                    delayed.append(component)
                else:
                    component.shutdown(parent)
        return delayed

    def modules(self):
        return (self.builtin_boot_steps +
                self.app.conf.CELERYD_CONSUMER_BOOT_STEPS)


class Consumer(object):
    """Listen for messages received from the broker and
    move them to the ready queue for task processing.

    :param ready_queue: See :attr:`ready_queue`.
    :param timer: See :attr:`timer`.

    """

    #: The queue that holds tasks ready for immediate processing.
    ready_queue = None

    #: Optional callback to be called when the connection is established.
    #: Will only be called once, even if the connection is lost and
    #: re-established.
    init_callback = None

    #: The current hostname.  Defaults to the system hostname.
    hostname = None

    #: The current worker pool instance.
    pool = None

    #: A timer used for high-priority internal tasks, such
    #: as sending heartbeats.
    timer = None

    def __init__(self, ready_queue,
            init_callback=noop, hostname=None,
            pool=None, app=None,
            timer=None, controller=None, hub=None, amqheartbeat=None,
            **kwargs):
        self.app = app_or_default(app)
        self.controller = controller
        self.ready_queue = ready_queue
        self.init_callback = init_callback
        self.hostname = hostname or socket.gethostname()
        self.pool = pool
        self.timer = timer or default_timer
        self.strategies = {}
        conninfo = self.app.connection()
        self.connection_errors = conninfo.connection_errors
        self.channel_errors = conninfo.channel_errors

        self._does_info = logger.isEnabledFor(logging.INFO)
        if not hasattr(self, 'loop'):
            self.loop = loops.asynloop if hub else loops.synloop
        if hub:
            hub.on_init.append(self.on_poll_init)
        self.hub = hub
        self._quick_put = self.ready_queue.put
        self.amqheartbeat = amqheartbeat
        if self.amqheartbeat is None:
            self.amqheartbeat = self.app.conf.BROKER_HEARTBEAT
        if not hub:
            self.amqheartbeat = 0

        if _detect_environment() == 'gevent':
            # there's a gevent bug that causes timeouts to not be reset,
            # so if the connection timeout is exceeded once, it can NEVER
            # connect again.
            self.app.conf.BROKER_CONNECTION_TIMEOUT = None

        self.components = []
        self.namespace = Namespace(app=self.app,
                                   on_start=self.on_start,
                                   on_close=self.on_close)
        self.namespace.apply(self, **kwargs)

    def on_start(self):
        # reload all task's execution strategies.
        self.update_strategies()
        self.init_callback(self)

    def start(self):
        """Start the consumer.

        Automatically survives intermittent connection failure,
        and will retry establishing the connection and restart
        consuming messages.

        """
        ns, loop, loop_args = self.namespace, self.loop, self.loop_args()
        while ns.state != CLOSE:
            maybe_shutdown()
            try:
                ns.start(self)
                loop(*loop_args)
            except self.connection_errors + self.channel_errors:
                error(CONNECTION_RETRY, exc_info=True)
                self.restart()

    def loop_args(self):
        return (self, self.connection, self.task_consumer,
                self.strategies, self.namespace, self.hub, self.qos,
                self.amqheartbeat, self.handle_unknown_message,
                self.handle_unknown_task, self.handle_invalid_task)

    def restart(self):
        return self.namespace.restart(self)

    def on_poll_init(self, hub):
        hub.update_readers(self.connection.eventmap)
        self.connection.transport.on_poll_init(hub.poller)

    def maybe_conn_error(self, fun):
        """Applies function but ignores any connection or channel
        errors raised."""
        try:
            fun()
        except (AttributeError, ) + \
                self.connection_errors + \
                self.channel_errors:
            pass

    def shutdown(self):
        self.namespace.shutdown(self)

    def on_decode_error(self, message, exc):
        """Callback called if an error occurs while decoding
        a message received.

        Simply logs the error and acknowledges the message so it
        doesn't enter a loop.

        :param message: The message with errors.
        :param exc: The original exception instance.

        """
        crit("Can't decode message body: %r (type:%r encoding:%r raw:%r')",
             exc, message.content_type, message.content_encoding,
             dump_body(message, message.body))
        message.ack()

    def on_close(self):
        # Clear internal queues to get rid of old messages.
        # They can't be acked anyway, as a delivery tag is specific
        # to the current channel.
        self.ready_queue.clear()
        self.timer.clear()

    def _open_connection(self):
        """Establish the broker connection.

        Will retry establishing the connection if the
        :setting:`BROKER_CONNECTION_RETRY` setting is enabled

        """
        conn = self.app.connection(heartbeat=self.amqheartbeat)

        # Callback called for each retry while the connection
        # can't be established.
        def _error_handler(exc, interval, next_step=CONNECTION_RETRY_STEP):
            if getattr(conn, 'alt', None) and interval == 0:
                next_step = CONNECTION_FAILOVER
            error(CONNECTION_ERROR, conn.as_uri(), exc,
                  next_step.format(when=humanize_seconds(interval, 'in', ' ')))

        # remember that the connection is lazy, it won't establish
        # until it's needed.
        if not self.app.conf.BROKER_CONNECTION_RETRY:
            # retry disabled, just call connect directly.
            conn.connect()
            return conn

        return conn.ensure_connection(_error_handler,
                    self.app.conf.BROKER_CONNECTION_MAX_RETRIES,
                    callback=maybe_shutdown)

    def stop(self):
        """Stop consuming.

        Does not close the broker connection, so be sure to call
        :meth:`close_connection` when you are finished with it.

        """
        self.namespace.stop(self)

    def add_task_queue(self, queue, exchange=None, exchange_type=None,
            routing_key=None, **options):
        cset = self.task_consumer
        try:
            q = self.app.amqp.queues[queue]
        except KeyError:
            exchange = queue if exchange is None else exchange
            exchange_type = 'direct' if exchange_type is None \
                                     else exchange_type
            q = self.app.amqp.queues.select_add(queue,
                    exchange=exchange,
                    exchange_type=exchange_type,
                    routing_key=routing_key, **options)
        if not cset.consuming_from(queue):
            cset.add_queue(q)
            cset.consume()
            info('Started consuming from %r', queue)

    def cancel_task_queue(self, queue):
        self.app.amqp.queues.select_remove(queue)
        self.task_consumer.cancel_by_queue(queue)

    @property
    def info(self):
        """Returns information about this consumer instance
        as a dict.

        This is also the consumer related info returned by
        ``celeryctl stats``.

        """
        conninfo = {}
        if self.connection:
            conninfo = self.connection.info()
            conninfo.pop('password', None)  # don't send password.
        return {'broker': conninfo, 'prefetch_count': self.qos.value}

    def on_task(self, task, task_reserved=task_reserved):
        """Handle received task.

        If the task has an `eta` we enter it into the ETA schedule,
        otherwise we move it the ready queue for immediate processing.

        """
        if task.revoked():
            return

        if self._does_info:
            info('Got task from broker: %s', task)

        if self.event_dispatcher.enabled:
            self.event_dispatcher.send('task-received', uuid=task.id,
                    name=task.name, args=safe_repr(task.args),
                    kwargs=safe_repr(task.kwargs),
                    retries=task.request_dict.get('retries', 0),
                    eta=task.eta and task.eta.isoformat(),
                    expires=task.expires and task.expires.isoformat())

        if task.eta:
            try:
                eta = to_timestamp(task.eta)
            except OverflowError as exc:
                error("Couldn't convert eta %s to timestamp: %r. Task: %r",
                      task.eta, exc, task.info(safe=True), exc_info=True)
                task.acknowledge()
            else:
                self.qos.increment_eventually()
                self.timer.apply_at(eta, self.apply_eta_task, (task, ),
                                    priority=6)
        else:
            task_reserved(task)
            self._quick_put(task)

    def apply_eta_task(self, task):
        """Method called by the timer to apply a task with an
        ETA/countdown."""
        task_reserved(task)
        self._quick_put(task)
        self.qos.decrement_eventually()

    def _message_report(self, body, message):
        return MESSAGE_REPORT.format(dump_body(message, body),
                                     safe_repr(message.content_type),
                                     safe_repr(message.content_encoding),
                                     safe_repr(message.delivery_info))

    def handle_unknown_message(self, body, message):
        warn(UNKNOWN_FORMAT, self._message_report(body, message))
        message.reject_log_error(logger, self.connection_errors)

    def handle_unknown_task(self, body, message, exc):
        error(UNKNOWN_TASK_ERROR, exc, dump_body(message, body), exc_info=True)
        message.reject_log_error(logger, self.connection_errors)

    def handle_invalid_task(self, body, message, exc):
        error(INVALID_TASK_ERROR, exc, dump_body(message, body), exc_info=True)
        message.reject_log_error(logger, self.connection_errors)

    def update_strategies(self):
        S = self.strategies
        app = self.app
        loader = app.loader
        hostname = self.hostname
        for name, task in self.app.tasks.iteritems():
            S[name] = task.start_strategy(app, self)
            task.__trace__ = build_tracer(name, task, loader, hostname)