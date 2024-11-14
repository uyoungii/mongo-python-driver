# Copyright 2019-present MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Execute Transactions Spec tests."""

import os
import sys
import time
import threading

sys.path[0:0] = [""]

from pymongo.errors import (ConnectionFailure,
                            PyMongoError)
from pymongo.monitoring import (ConnectionPoolListener,
                                ConnectionCheckedInEvent,
                                ConnectionCheckedOutEvent,
                                ConnectionCheckOutFailedEvent,
                                ConnectionCheckOutFailedReason,
                                ConnectionCheckOutStartedEvent,
                                ConnectionClosedEvent,
                                ConnectionClosedReason,
                                ConnectionCreatedEvent,
                                ConnectionReadyEvent,
                                PoolCreatedEvent,
                                PoolClearedEvent,
                                PoolClosedEvent)
from pymongo.read_preferences import ReadPreference
from pymongo.pool import _PoolClosedError

from test import (IntegrationTest,
                  unittest)
from test.utils import (camel_to_snake,
                        client_context,
                        get_pool,
                        get_pools,
                        rs_or_single_client,
                        single_client,
                        TestCreator,
                        wait_until)


OBJECT_TYPES = {
    # Event types.
    'ConnectionCheckedIn': ConnectionCheckedInEvent,
    'ConnectionCheckedOut': ConnectionCheckedOutEvent,
    'ConnectionCheckOutFailed': ConnectionCheckOutFailedEvent,
    'ConnectionClosed': ConnectionClosedEvent,
    'ConnectionCreated': ConnectionCreatedEvent,
    'ConnectionReady': ConnectionReadyEvent,
    'ConnectionCheckOutStarted': ConnectionCheckOutStartedEvent,
    'ConnectionPoolCreated': PoolCreatedEvent,
    'ConnectionPoolCleared': PoolClearedEvent,
    'ConnectionPoolClosed': PoolClosedEvent,
    # Error types.
    'PoolClosedError': _PoolClosedError,
    'WaitQueueTimeoutError': ConnectionFailure,
}


class CMAPListener(ConnectionPoolListener):
    def __init__(self):
        self.events = []

    def add_event(self, event):
        self.events.append(event)

    def event_count(self, event_type):
        return len([event for event in self.events[:]
                    if isinstance(event, event_type)])

    def connection_created(self, event):
        self.add_event(event)

    def connection_ready(self, event):
        self.add_event(event)

    def connection_closed(self, event):
        self.add_event(event)

    def connection_check_out_started(self, event):
        self.add_event(event)

    def connection_check_out_failed(self, event):
        self.add_event(event)

    def connection_checked_out(self, event):
        self.add_event(event)

    def connection_checked_in(self, event):
        self.add_event(event)

    def pool_created(self, event):
        self.add_event(event)

    def pool_cleared(self, event):
        self.add_event(event)

    def pool_closed(self, event):
        self.add_event(event)


class CMAPThread(threading.Thread):
    def __init__(self, name):
        super(CMAPThread, self).__init__()
        self.name = name
        self.exc = None
        self.setDaemon(True)
        self.cond = threading.Condition()
        self.ops = []
        self.stopped = False

    def schedule(self, work):
        self.ops.append(work)
        with self.cond:
            self.cond.notify()

    def stop(self):
        self.stopped = True
        with self.cond:
            self.cond.notify()

    def run(self):
        while not self.stopped or self.ops:
            if not self. ops:
                with self.cond:
                    self.cond.wait(10)
            if self.ops:
                try:
                    work = self.ops.pop(0)
                    work()
                except Exception as exc:
                    self.exc = exc
                    self.stop()


class TestCMAP(IntegrationTest):
    # Location of JSON test specifications.
    TEST_PATH = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), 'cmap')

    # Test operations:

    def start(self, op):
        """Run the 'start' thread operation."""
        target = op['target']
        thread = CMAPThread(target)
        thread.start()
        self.targets[target] = thread

    def wait(self, op):
        """Run the 'wait' operation."""
        time.sleep(op['ms'] / 1000.0)

    def wait_for_thread(self, op):
        """Run the 'waitForThread' operation."""
        target = op['target']
        thread = self.targets[target]
        thread.stop()
        thread.join()
        if thread.exc:
            raise thread.exc

    def wait_for_event(self, op):
        """Run the 'waitForEvent' operation."""
        event = OBJECT_TYPES[op['event']]
        count = op['count']
        wait_until(lambda: self.listener.event_count(event) >= count,
                   'find %s %s event(s)' % (count, event))

    def check_out(self, op):
        """Run the 'checkOut' operation."""
        label = op['label']
        with self.pool.get_socket({}, checkout=True) as sock_info:
            if label:
                self.labels[label] = sock_info
            else:
                self.addCleanup(sock_info.close_socket, None)

    def check_in(self, op):
        """Run the 'checkIn' operation."""
        label = op['connection']
        sock_info = self.labels[label]
        self.pool.return_socket(sock_info)

    def clear(self, op):
        """Run the 'clear' operation."""
        self.pool.reset()

    def close(self, op):
        """Run the 'close' operation."""
        self.pool.close()

    def run_operation(self, op):
        """Run a single operation in a test."""
        op_name = camel_to_snake(op['name'])
        thread = op['thread']
        meth = getattr(self, op_name)
        if thread:
            self.targets[thread].schedule(lambda: meth(op))
        else:
            meth(op)

    def run_operations(self, ops):
        """Run a test's operations."""
        for op in ops:
            self.run_operation(op)

    def check_object(self, actual, expected):
        """Assert that the actual object matches the expected object."""
        self.assertEqual(type(actual), OBJECT_TYPES[expected['type']])
        for attr, expected_val in expected.items():
            if attr == 'type':
                continue
            c2s = camel_to_snake(attr)
            actual_val = getattr(actual, c2s)
            if expected_val == 42:
                self.assertIsNotNone(actual_val)
            else:
                self.assertEqual(actual_val, expected_val)

    def check_event(self, actual, expected):
        """Assert that the actual event matches the expected event."""
        self.check_object(actual, expected)

    def actual_events(self, ignore):
        """Return all the non-ignored events."""
        ignore = tuple(OBJECT_TYPES[name] for name in ignore)
        return [event for event in self.listener.events
                if not isinstance(event, ignore)]

    def check_events(self, events, ignore):
        """Check the events of a test."""
        actual_events = self.actual_events(ignore)
        for actual, expected in zip(actual_events, events):
            self.check_event(actual, expected)

        if len(events) > len(actual_events):
            self.fail('missing events: %r' % (events[len(actual_events):],))
        elif len(events) < len(actual_events):
            self.fail('extra events: %r' % (actual_events[len(events):],))

    def check_error(self, actual, expected):
        message = expected.pop('message')
        self.check_object(actual, expected)
        self.assertIn(message, str(actual))

    def run_scenario(self, scenario_def, test):
        """Run a CMAP spec test."""
        self.assertEqual(scenario_def['version'], 1)
        self.assertEqual(scenario_def['style'], 'unit')
        self.listener = CMAPListener()

        opts = test['poolOptions'].copy()
        opts['event_listeners'] = [self.listener]
        client = single_client(**opts)
        self.addCleanup(client.close)
        self.pool = get_pool(client)

        # Map of target names to Thread objects.
        self.targets = dict()
        # Map of label names to Connection objects
        self.labels = dict()

        def cleanup():
            for t in self.targets.values():
                t.stop()
            for t in self.targets.values():
                t.join(5)
            for conn in self.labels.values():
                conn.close_socket(None)

        self.addCleanup(cleanup)

        if test['error']:
            with self.assertRaises(PyMongoError) as ctx:
                self.run_operations(test['operations'])
            self.check_error(ctx.exception, test['error'])
        else:
            self.run_operations(test['operations'])

        self.check_events(test['events'], test['ignore'])

    POOL_OPTIONS = {
        'maxPoolSize': 50,
        'minPoolSize': 1,
        'maxIdleTimeMS': 10000,
        'waitQueueTimeoutMS': 10000
    }

    #
    # Prose tests. Numbers correspond to the prose test number in the spec.
    #
    def test_1_client_connection_pool_options(self):
        client = rs_or_single_client(**self.POOL_OPTIONS)
        pool_opts = get_pool(client).opts
        self.assertEqual(pool_opts.non_default_options, self.POOL_OPTIONS)

    def test_2_all_client_pools_have_same_options(self):
        client = rs_or_single_client(**self.POOL_OPTIONS)
        client.admin.command('isMaster')
        # Discover at least one secondary.
        if client_context.has_secondaries:
            client.admin.command(
                'isMaster', read_preference=ReadPreference.SECONDARY)
        pools = get_pools(client)
        pool_opts = pools[0].opts

        self.assertEqual(pool_opts.non_default_options, self.POOL_OPTIONS)
        for pool in pools[1:]:
            self.assertEqual(pool.opts, pool_opts)

    def test_3_uri_connection_pool_options(self):
        opts = '&'.join(['%s=%s' % (k, v)
                         for k, v in self.POOL_OPTIONS.items()])
        uri = 'mongodb://%s/?%s' % (client_context.pair, opts)
        client = rs_or_single_client(uri, **self.credentials)
        pool_opts = get_pool(client).opts
        self.assertEqual(pool_opts.non_default_options, self.POOL_OPTIONS)

    def test_4_subscribe_to_events(self):
        listener = CMAPListener()
        client = single_client(event_listeners=[listener])
        self.assertEqual(listener.event_count(PoolCreatedEvent), 1)

        # Creates a new connection.
        client.admin.command('isMaster')
        self.assertEqual(
            listener.event_count(ConnectionCheckOutStartedEvent), 1)
        self.assertEqual(listener.event_count(ConnectionCreatedEvent), 1)
        self.assertEqual(listener.event_count(ConnectionReadyEvent), 1)
        self.assertEqual(listener.event_count(ConnectionCheckedOutEvent), 1)
        self.assertEqual(listener.event_count(ConnectionCheckedInEvent), 1)

        # Uses the existing connection.
        client.admin.command('isMaster')
        self.assertEqual(
            listener.event_count(ConnectionCheckOutStartedEvent), 2)
        self.assertEqual(listener.event_count(ConnectionCheckedOutEvent), 2)
        self.assertEqual(listener.event_count(ConnectionCheckedInEvent), 2)

        client.close()
        self.assertEqual(listener.event_count(PoolClearedEvent), 1)
        self.assertEqual(listener.event_count(ConnectionClosedEvent), 1)

    #
    # Extra non-spec tests
    #
    def assertRepr(self, obj):
        new_obj = eval(repr(obj))
        self.assertEqual(type(new_obj), type(obj))
        self.assertEqual(repr(new_obj), repr(obj))

    def test_events_repr(self):
        host = ('localhost', 27017)
        self.assertRepr(ConnectionCheckedInEvent(host, 1))
        self.assertRepr(ConnectionCheckedOutEvent(host, 1))
        self.assertRepr(ConnectionCheckOutFailedEvent(
            host, ConnectionCheckOutFailedReason.POOL_CLOSED))
        self.assertRepr(ConnectionClosedEvent(
            host, 1, ConnectionClosedReason.POOL_CLOSED))
        self.assertRepr(ConnectionCreatedEvent(host, 1))
        self.assertRepr(ConnectionReadyEvent(host, 1))
        self.assertRepr(ConnectionCheckOutStartedEvent(host))
        self.assertRepr(PoolCreatedEvent(host, {}))
        self.assertRepr(PoolClearedEvent(host))
        self.assertRepr(PoolClosedEvent(host))


def create_test(scenario_def, test, name):
    def run_scenario(self):
        self.run_scenario(scenario_def, test)

    return run_scenario


class CMAPTestCreator(TestCreator):

    def tests(self, scenario_def):
        """Extract the tests from a spec file.

        CMAP tests do not have a 'tests' field. The whole file represents
        a single test case.
        """
        return [scenario_def]


test_creator = CMAPTestCreator(create_test, TestCMAP, TestCMAP.TEST_PATH)
test_creator.create_tests()


if __name__ == "__main__":
    unittest.main()
