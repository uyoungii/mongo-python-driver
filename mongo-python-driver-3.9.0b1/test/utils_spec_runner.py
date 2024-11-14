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

"""Utilities for testing driver specs."""

import copy


from bson.binary import Binary
from bson.py3compat import iteritems
from bson.son import SON

from gridfs import GridFSBucket

from pymongo import (client_session,
                     operations)
from pymongo.command_cursor import CommandCursor
from pymongo.cursor import Cursor
from pymongo.errors import (OperationFailure, PyMongoError)
from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference
from pymongo.results import _WriteResult, BulkWriteResult
from pymongo.write_concern import WriteConcern

from test import (client_context,
                  client_knobs,
                  IntegrationTest,
                  unittest)

from test.utils import (camel_to_snake,
                        camel_to_snake_args,
                        camel_to_upper_camel,
                        CompareType,
                        OvertCommandListener,
                        rs_client)
from test.utils_selection_tests import parse_read_preference


class SpecRunner(IntegrationTest):

    @classmethod
    def setUpClass(cls):
        super(SpecRunner, cls).setUpClass()
        cls.mongos_clients = []

        # Speed up the tests by decreasing the heartbeat frequency.
        cls.knobs = client_knobs(min_heartbeat_interval=0.1)
        cls.knobs.enable()

    @classmethod
    def tearDownClass(cls):
        cls.knobs.disable()
        super(SpecRunner, cls).tearDownClass()

    def setUp(self):
        super(SpecRunner, self).setUp()
        self.listener = None

    def _set_fail_point(self, client, command_args):
        cmd = SON([('configureFailPoint', 'failCommand')])
        cmd.update(command_args)
        client.admin.command(cmd)

    def set_fail_point(self, command_args):
        cmd = SON([('configureFailPoint', 'failCommand')])
        cmd.update(command_args)
        clients = self.mongos_clients if self.mongos_clients else [self.client]
        for client in clients:
            self._set_fail_point(client, cmd)

    def targeted_fail_point(self, session, fail_point):
        """Run the targetedFailPoint test operation.

        Enable the fail point on the session's pinned mongos.
        """
        clients = {c.address: c for c in self.mongos_clients}
        client = clients[session._pinned_address]
        self._set_fail_point(client, fail_point)
        self.addCleanup(self.set_fail_point, {'mode': 'off'})

    def assert_session_pinned(self, session):
        """Run the assertSessionPinned test operation.

        Assert that the given session is pinned.
        """
        self.assertIsNotNone(session._transaction.pinned_address)

    def assert_session_unpinned(self, session):
        """Run the assertSessionUnpinned test operation.

        Assert that the given session is not pinned.
        """
        self.assertIsNone(session._pinned_address)
        self.assertIsNone(session._transaction.pinned_address)

    def assertErrorLabelsContain(self, exc, expected_labels):
        labels = [l for l in expected_labels if exc.has_error_label(l)]
        self.assertEqual(labels, expected_labels)

    def assertErrorLabelsOmit(self, exc, omit_labels):
        for label in omit_labels:
            self.assertFalse(
                exc.has_error_label(label),
                msg='error labels should not contain %s' % (label,))

    def kill_all_sessions(self):
        clients = self.mongos_clients if self.mongos_clients else [self.client]
        for client in clients:
            try:
                client.admin.command('killAllSessions', [])
            except OperationFailure:
                # "operation was interrupted" by killing the command's
                # own session.
                pass

    def check_command_result(self, expected_result, result):
        # Only compare the keys in the expected result.
        filtered_result = {}
        for key in expected_result:
            try:
                filtered_result[key] = result[key]
            except KeyError:
                pass
        self.assertEqual(filtered_result, expected_result)

    # TODO: factor the following function with test_crud.py.
    def check_result(self, expected_result, result):
        if isinstance(result, _WriteResult):
            for res in expected_result:
                prop = camel_to_snake(res)
                # SPEC-869: Only BulkWriteResult has upserted_count.
                if (prop == "upserted_count"
                        and not isinstance(result, BulkWriteResult)):
                    if result.upserted_id is not None:
                        upserted_count = 1
                    else:
                        upserted_count = 0
                    self.assertEqual(upserted_count, expected_result[res], prop)
                elif prop == "inserted_ids":
                    # BulkWriteResult does not have inserted_ids.
                    if isinstance(result, BulkWriteResult):
                        self.assertEqual(len(expected_result[res]),
                                         result.inserted_count)
                    else:
                        # InsertManyResult may be compared to [id1] from the
                        # crud spec or {"0": id1} from the retryable write spec.
                        ids = expected_result[res]
                        if isinstance(ids, dict):
                            ids = [ids[str(i)] for i in range(len(ids))]
                        self.assertEqual(ids, result.inserted_ids, prop)
                elif prop == "upserted_ids":
                    # Convert indexes from strings to integers.
                    ids = expected_result[res]
                    expected_ids = {}
                    for str_index in ids:
                        expected_ids[int(str_index)] = ids[str_index]
                    self.assertEqual(expected_ids, result.upserted_ids, prop)
                else:
                    self.assertEqual(
                        getattr(result, prop), expected_result[res], prop)

            return True
        else:
            self.assertEqual(result, expected_result)

    def get_object_name(self, op):
        """Allow CRUD spec to override handling of 'object'

        Transaction spec says 'object' is required.
        """
        return op['object']

    @staticmethod
    def parse_options(opts):
        if 'readPreference' in opts:
            opts['read_preference'] = parse_read_preference(
                opts.pop('readPreference'))

        if 'writeConcern' in opts:
            opts['write_concern'] = WriteConcern(
                **dict(opts.pop('writeConcern')))

        if 'readConcern' in opts:
            opts['read_concern'] = ReadConcern(
                **dict(opts.pop('readConcern')))

        if 'maxTimeMS' in opts:
            opts['max_time_ms'] = opts.pop('maxTimeMS')

        if 'maxCommitTimeMS' in opts:
            opts['max_commit_time_ms'] = opts.pop('maxCommitTimeMS')

        return dict(opts)

    def run_operation(self, sessions, collection, operation):
        original_collection = collection
        name = camel_to_snake(operation['name'])
        if name == 'run_command':
            name = 'command'
        elif name == 'download_by_name':
            name = 'open_download_stream_by_name'
        elif name == 'download':
            name = 'open_download_stream'

        database = collection.database
        collection = database.get_collection(collection.name)
        if 'collectionOptions' in operation:
            collection = collection.with_options(
                **self.parse_options(operation['collectionOptions']))

        object_name = self.get_object_name(operation)
        if object_name == 'gridfsbucket':
            # Only create the GridFSBucket when we need it (for the gridfs
            # retryable reads tests).
            obj = GridFSBucket(
                database, bucket_name=collection.name,
                disable_md5=True)
        else:
            objects = {
                'client': database.client,
                'database': database,
                'collection': collection,
                'testRunner': self
            }
            objects.update(sessions)
            obj = objects[object_name]

        # Combine arguments with options and handle special cases.
        arguments = operation.get('arguments', {})
        arguments.update(arguments.pop("options", {}))
        self.parse_options(arguments)

        cmd = getattr(obj, name)

        for arg_name in list(arguments):
            c2s = camel_to_snake(arg_name)
            # PyMongo accepts sort as list of tuples.
            if arg_name == "sort":
                sort_dict = arguments[arg_name]
                arguments[arg_name] = list(iteritems(sort_dict))
            # Named "key" instead not fieldName.
            if arg_name == "fieldName":
                arguments["key"] = arguments.pop(arg_name)
            # Aggregate uses "batchSize", while find uses batch_size.
            elif ((arg_name == "batchSize" or arg_name == "allowDiskUse") and
                  name == "aggregate"):
                continue
            # Requires boolean returnDocument.
            elif arg_name == "returnDocument":
                arguments[c2s] = arguments.pop(arg_name) == "After"
            elif c2s == "requests":
                # Parse each request into a bulk write model.
                requests = []
                for request in arguments["requests"]:
                    bulk_model = camel_to_upper_camel(request["name"])
                    bulk_class = getattr(operations, bulk_model)
                    bulk_arguments = camel_to_snake_args(request["arguments"])
                    requests.append(bulk_class(**dict(bulk_arguments)))
                arguments["requests"] = requests
            elif arg_name == "session":
                arguments['session'] = sessions[arguments['session']]
            elif name == 'command' and arg_name == 'command':
                # Ensure the first key is the command name.
                ordered_command = SON([(operation['command_name'], 1)])
                ordered_command.update(arguments['command'])
                arguments['command'] = ordered_command
            elif name == 'open_download_stream' and arg_name == 'id':
                arguments['file_id'] = arguments.pop(arg_name)
            elif name != 'find' and c2s == 'max_time_ms':
                # find is the only method that accepts snake_case max_time_ms.
                # All other methods take kwargs which must use the server's
                # camelCase maxTimeMS. See PYTHON-1855.
                arguments['maxTimeMS'] = arguments.pop('max_time_ms')
            elif name == 'with_transaction' and arg_name == 'callback':
                callback_ops = arguments[arg_name]['operations']
                arguments['callback'] = lambda _: self.run_operations(
                    sessions, original_collection, copy.deepcopy(callback_ops),
                    in_with_transaction=True)
            else:
                arguments[c2s] = arguments.pop(arg_name)

        result = cmd(**dict(arguments))

        if name == "aggregate":
            if arguments["pipeline"] and "$out" in arguments["pipeline"][-1]:
                # Read from the primary to ensure causal consistency.
                out = collection.database.get_collection(
                    arguments["pipeline"][-1]["$out"],
                    read_preference=ReadPreference.PRIMARY)
                return out.find()
        if name == "map_reduce":
            if isinstance(result, dict) and 'results' in result:
                return result['results']
        if 'download' in name:
            result = Binary(result.read())

        if isinstance(result, Cursor) or isinstance(result, CommandCursor):
            return list(result)

        return result

    def run_operations(self, sessions, collection, ops,
                       in_with_transaction=False):
        for op in ops:
            expected_result = op.get('result')
            if expect_error(op):
                with self.assertRaises(PyMongoError,
                                       msg=op['name']) as context:
                    self.run_operation(sessions, collection, op.copy())

                if expect_error_message(expected_result):
                    self.assertIn(expected_result['errorContains'].lower(),
                                  str(context.exception).lower())
                if expect_error_code(expected_result):
                    self.assertEqual(expected_result['errorCodeName'],
                                     context.exception.details.get('codeName'))
                if expect_error_labels_contain(expected_result):
                    self.assertErrorLabelsContain(
                        context.exception,
                        expected_result['errorLabelsContain'])
                if expect_error_labels_omit(expected_result):
                    self.assertErrorLabelsOmit(
                        context.exception,
                        expected_result['errorLabelsOmit'])

                # Reraise the exception if we're in the with_transaction
                # callback.
                if in_with_transaction:
                    raise context.exception
            else:
                result = self.run_operation(sessions, collection, op.copy())
                if 'result' in op:
                    if op['name'] == 'runCommand':
                        self.check_command_result(expected_result, result)
                    else:
                        self.check_result(expected_result, result)

    # TODO: factor with test_command_monitoring.py
    def check_events(self, test, listener, session_ids):
        res = listener.results
        if not len(test['expectations']):
            return

        cmd_names = [event.command_name for event in res['started']]
        self.assertEqual(
            len(res['started']), len(test['expectations']), cmd_names)
        for i, expectation in enumerate(test['expectations']):
            event_type = next(iter(expectation))
            event = res['started'][i]

            # The tests substitute 42 for any number other than 0.
            if (event.command_name == 'getMore'
                    and event.command['getMore']):
                event.command['getMore'] = 42
            elif event.command_name == 'killCursors':
                event.command['cursors'] = [42]
            elif event.command_name == 'update':
                # TODO: remove this once PYTHON-1744 is done.
                # Add upsert and multi fields back into expectations.
                updates = expectation[event_type]['command']['updates']
                for update in updates:
                    update.setdefault('upsert', False)
                    update.setdefault('multi', False)

            # Replace afterClusterTime: 42 with actual afterClusterTime.
            expected_cmd = expectation[event_type]['command']
            expected_read_concern = expected_cmd.get('readConcern')
            if expected_read_concern is not None:
                time = expected_read_concern.get('afterClusterTime')
                if time == 42:
                    actual_time = event.command.get(
                        'readConcern', {}).get('afterClusterTime')
                    if actual_time is not None:
                        expected_read_concern['afterClusterTime'] = actual_time

            recovery_token = expected_cmd.get('recoveryToken')
            if recovery_token == 42:
                expected_cmd['recoveryToken'] = CompareType(dict)

            # Replace lsid with a name like "session0" to match test.
            if 'lsid' in event.command:
                for name, lsid in session_ids.items():
                    if event.command['lsid'] == lsid:
                        event.command['lsid'] = name
                        break

            for attr, expected in expectation[event_type].items():
                actual = getattr(event, attr)
                if isinstance(expected, dict):
                    for key, val in expected.items():
                        if val is None:
                            if key in actual:
                                self.fail("Unexpected key [%s] in %r" % (
                                    key, actual))
                        elif key not in actual:
                            self.fail("Expected key [%s] in %r" % (
                                key, actual))
                        else:
                            self.assertEqual(val, actual[key],
                                             "Key [%s] in %s" % (key, actual))
                else:
                    self.assertEqual(actual, expected)

    def maybe_skip_scenario(self, test):
        if test.get('skipReason'):
            raise unittest.SkipTest(test.get('skipReason'))

    def get_scenario_db_name(self, scenario_def):
        """Allow CRUD spec to override a test's database name."""
        return scenario_def['database_name']

    def get_scenario_coll_name(self, scenario_def):
        """Allow CRUD spec to override a test's collection name."""
        return scenario_def['collection_name']

    def get_outcome_coll_name(self, outcome, collection):
        """Allow CRUD spec to override outcome collection."""
        return collection.name

    def run_scenario(self, scenario_def, test):
        self.maybe_skip_scenario(test)
        listener = OvertCommandListener()
        # Create a new client, to avoid interference from pooled sessions.
        # Convert test['clientOptions'] to dict to avoid a Jython bug using
        # "**" with ScenarioDict.
        client_options = dict(test['clientOptions'])
        use_multi_mongos = test['useMultipleMongoses']
        if client_context.is_mongos and use_multi_mongos:
            client = rs_client(client_context.mongos_seeds(),
                               event_listeners=[listener], **client_options)
        else:
            client = rs_client(event_listeners=[listener], **client_options)
        self.listener = listener
        # Close the client explicitly to avoid having too many threads open.
        self.addCleanup(client.close)

        # Kill all sessions before and after each test to prevent an open
        # transaction (from a test failure) from blocking collection/database
        # operations during test set up and tear down.
        self.kill_all_sessions()
        self.addCleanup(self.kill_all_sessions)

        database_name = self.get_scenario_db_name(scenario_def)
        write_concern_db = client_context.client.get_database(
            database_name, write_concern=WriteConcern(w='majority'))
        if 'bucket_name' in scenario_def:
            # Create a bucket for the retryable reads GridFS tests.
            collection_name = scenario_def['bucket_name']
            client_context.client.drop_database(database_name)
            if scenario_def['data']:
                data = scenario_def['data']
                # Load data.
                write_concern_db['fs.chunks'].insert_many(data['fs.chunks'])
                write_concern_db['fs.files'].insert_many(data['fs.files'])
        else:
            collection_name = self.get_scenario_coll_name(scenario_def)
            write_concern_coll = write_concern_db[collection_name]
            write_concern_coll.drop()
            write_concern_db.create_collection(collection_name)
            if scenario_def['data']:
                # Load data.
                write_concern_coll.insert_many(scenario_def['data'])

        # SPEC-1245 workaround StaleDbVersion on distinct
        for c in self.mongos_clients:
            c[database_name][collection_name].distinct("x")

        # Create session0 and session1.
        sessions = {}
        session_ids = {}
        for i in range(2):
            session_name = 'session%d' % i
            opts = camel_to_snake_args(test['sessionOptions'][session_name])
            if 'default_transaction_options' in opts:
                txn_opts = self.parse_options(
                    opts['default_transaction_options'])
                txn_opts = client_session.TransactionOptions(**txn_opts)
                opts['default_transaction_options'] = txn_opts

            s = client.start_session(**dict(opts))

            sessions[session_name] = s
            # Store lsid so we can access it after end_session, in check_events.
            session_ids[session_name] = s.session_id

        self.addCleanup(end_sessions, sessions)

        if 'failPoint' in test:
            self.set_fail_point(test['failPoint'])
            self.addCleanup(self.set_fail_point, {
                'configureFailPoint': 'failCommand', 'mode': 'off'})

        listener.results.clear()

        collection = client[database_name][collection_name]
        self.run_operations(sessions, collection, test['operations'])

        end_sessions(sessions)

        self.check_events(test, listener, session_ids)

        # Disable fail points.
        if 'failPoint' in test:
            self.set_fail_point({
                'configureFailPoint': 'failCommand', 'mode': 'off'})

        # Assert final state is expected.
        outcome = test['outcome']
        expected_c = outcome.get('collection')
        if expected_c is not None:
            outcome_coll_name = self.get_outcome_coll_name(
                outcome, collection)

            # Read from the primary with local read concern to ensure causal
            # consistency.
            outcome_coll = collection.database.get_collection(
                outcome_coll_name,
                read_preference=ReadPreference.PRIMARY,
                read_concern=ReadConcern('local'))
            self.assertEqual(list(outcome_coll.find()), expected_c['data'])


def expect_any_error(op):
    if isinstance(op, dict):
        return op.get('error')

    return False


def expect_error_message(expected_result):
    if isinstance(expected_result, dict):
        return expected_result['errorContains']

    return False


def expect_error_code(expected_result):
    if isinstance(expected_result, dict):
        return expected_result['errorCodeName']

    return False


def expect_error_labels_contain(expected_result):
    if isinstance(expected_result, dict):
        return expected_result['errorLabelsContain']

    return False


def expect_error_labels_omit(expected_result):
    if isinstance(expected_result, dict):
        return expected_result['errorLabelsOmit']

    return False


def expect_error(op):
    expected_result = op.get('result')
    return (expect_any_error(op) or
            expect_error_message(expected_result)
            or expect_error_code(expected_result)
            or expect_error_labels_contain(expected_result)
            or expect_error_labels_omit(expected_result))


def end_sessions(sessions):
    for s in sessions.values():
        # Aborts the transaction if it's open.
        s.end_session()
