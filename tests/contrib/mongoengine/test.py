# stdib
import time

# 3p
import mongoengine
from nose.tools import eq_, ok_
import pymongo

# project
from ddtrace import Pin
from ddtrace.constants import ANALYTICS_SAMPLE_RATE_KEY
from ddtrace.contrib.mongoengine.patch import patch, unpatch
from ddtrace.ext import mongo as mongox

# testing
from tests.opentracer.utils import init_tracer
from ..config import MONGO_CONFIG
from ...base import override_config
from ...test_tracer import get_dummy_tracer


class Artist(mongoengine.Document):
    first_name = mongoengine.StringField(max_length=50)
    last_name = mongoengine.StringField(max_length=50)


class MongoEngineCore(object):

    # Define the service at the class level, so that each test suite can use a different service
    # and therefore catch any sneaky badly-unpatched stuff.
    TEST_SERVICE = 'deadbeef'

    def get_tracer_and_connect(self):
        # implement me
        pass

    def test_insert_update_delete_query(self):
        tracer = self.get_tracer_and_connect()

        start = time.time()
        Artist.drop_collection()
        end = time.time()

        # ensure we get a drop collection span
        spans = tracer.writer.pop()
        eq_(len(spans), 1)
        span = spans[0]
        eq_(span.resource, 'drop artist')
        eq_(span.span_type, 'mongodb')
        eq_(span.service, self.TEST_SERVICE)
        _assert_timing(span, start, end)

        start = end
        joni = Artist()
        joni.first_name = 'Joni'
        joni.last_name = 'Mitchell'
        joni.save()
        end = time.time()

        # ensure we get an insert span
        spans = tracer.writer.pop()
        eq_(len(spans), 1)
        span = spans[0]
        eq_(span.resource, 'insert artist')
        eq_(span.span_type, 'mongodb')
        eq_(span.service, self.TEST_SERVICE)
        _assert_timing(span, start, end)

        # ensure full scans work
        start = time.time()
        artists = [a for a in Artist.objects]
        end = time.time()
        eq_(len(artists), 1)
        eq_(artists[0].first_name, 'Joni')
        eq_(artists[0].last_name, 'Mitchell')

        # query names should be used in pymongo>3.1
        name = 'find' if pymongo.version_tuple >= (3, 1, 0) else 'query'

        spans = tracer.writer.pop()
        eq_(len(spans), 1)
        span = spans[0]
        eq_(span.resource, '{} artist'.format(name))
        eq_(span.span_type, 'mongodb')
        eq_(span.service, self.TEST_SERVICE)
        _assert_timing(span, start, end)

        # ensure filtered queries work
        start = time.time()
        artists = [a for a in Artist.objects(first_name="Joni")]
        end = time.time()
        eq_(len(artists), 1)
        joni = artists[0]
        eq_(artists[0].first_name, 'Joni')
        eq_(artists[0].last_name, 'Mitchell')

        spans = tracer.writer.pop()
        eq_(len(spans), 1)
        span = spans[0]
        eq_(span.resource, '{} artist {{"first_name": "?"}}'.format(name))
        eq_(span.span_type, 'mongodb')
        eq_(span.service, self.TEST_SERVICE)
        _assert_timing(span, start, end)

        # ensure updates work
        start = time.time()
        joni.last_name = 'From Saskatoon'
        joni.save()
        end = time.time()

        spans = tracer.writer.pop()
        eq_(len(spans), 1)
        span = spans[0]
        eq_(span.resource, 'update artist {"_id": "?"}')
        eq_(span.span_type, 'mongodb')
        eq_(span.service, self.TEST_SERVICE)
        _assert_timing(span, start, end)

        # ensure deletes
        start = time.time()
        joni.delete()
        end = time.time()

        spans = tracer.writer.pop()
        eq_(len(spans), 1)
        span = spans[0]
        eq_(span.resource, 'delete artist {"_id": "?"}')
        eq_(span.span_type, 'mongodb')
        eq_(span.service, self.TEST_SERVICE)
        _assert_timing(span, start, end)

    def test_opentracing(self):
        """Ensure the opentracer works with mongoengine."""
        tracer = self.get_tracer_and_connect()
        ot_tracer = init_tracer('my_svc', tracer)

        with ot_tracer.start_active_span('ot_span'):
            start = time.time()
            Artist.drop_collection()
            end = time.time()

        # ensure we get a drop collection span
        spans = tracer.writer.pop()
        eq_(len(spans), 2)
        ot_span, dd_span = spans

        # confirm the parenting
        eq_(ot_span.parent_id, None)
        eq_(dd_span.parent_id, ot_span.span_id)

        eq_(ot_span.name, 'ot_span')
        eq_(ot_span.service, 'my_svc')

        eq_(dd_span.resource, 'drop artist')
        eq_(dd_span.span_type, 'mongodb')
        eq_(dd_span.service, self.TEST_SERVICE)
        _assert_timing(dd_span, start, end)

    def test_analytics_default(self):
        tracer = self.get_tracer_and_connect()
        Artist.drop_collection()

        spans = tracer.writer.pop()
        eq_(len(spans), 1)
        ok_(spans[0].get_metric(ANALYTICS_SAMPLE_RATE_KEY) is None)

    def test_analytics_with_rate(self):
        with override_config(
            'pymongo',
            dict(analytics_enabled=True, analytics_sample_rate=0.5)
        ):
            tracer = self.get_tracer_and_connect()
            Artist.drop_collection()

            spans = tracer.writer.pop()
            eq_(len(spans), 1)
            eq_(spans[0].get_metric(ANALYTICS_SAMPLE_RATE_KEY), 0.5)

    def test_analytics_without_rate(self):
        with override_config(
            'pymongo',
            dict(analytics_enabled=True)
        ):
            tracer = self.get_tracer_and_connect()
            Artist.drop_collection()

            spans = tracer.writer.pop()
            eq_(len(spans), 1)
            eq_(spans[0].get_metric(ANALYTICS_SAMPLE_RATE_KEY), 1.0)


class TestMongoEnginePatchConnectDefault(MongoEngineCore):
    """Test suite with a global Pin for the connect function with the default configuration"""

    TEST_SERVICE = mongox.TYPE

    def setUp(self):
        patch()

    def tearDown(self):
        unpatch()
        # Disconnect and remove the client
        mongoengine.connection.disconnect()

    def get_tracer_and_connect(self):
        tracer = get_dummy_tracer()
        Pin.get_from(mongoengine.connect).clone(
            tracer=tracer).onto(mongoengine.connect)
        mongoengine.connect(port=MONGO_CONFIG['port'])

        return tracer


class TestMongoEnginePatchConnect(TestMongoEnginePatchConnectDefault):
    """Test suite with a global Pin for the connect function with custom service"""

    TEST_SERVICE = 'test-mongo-patch-connect'

    def get_tracer_and_connect(self):
        tracer = TestMongoEnginePatchConnectDefault.get_tracer_and_connect(self)
        Pin(service=self.TEST_SERVICE, tracer=tracer).onto(mongoengine.connect)
        mongoengine.connect(port=MONGO_CONFIG['port'])

        return tracer


class TestMongoEnginePatchClientDefault(MongoEngineCore):
    """Test suite with a Pin local to a specific client with default configuration"""

    TEST_SERVICE = mongox.TYPE

    def setUp(self):
        patch()

    def tearDown(self):
        unpatch()
        # Disconnect and remove the client
        mongoengine.connection.disconnect()

    def get_tracer_and_connect(self):
        tracer = get_dummy_tracer()
        client = mongoengine.connect(port=MONGO_CONFIG['port'])
        Pin.get_from(client).clone(tracer=tracer).onto(client)

        return tracer


class TestMongoEnginePatchClient(TestMongoEnginePatchClientDefault):
    """Test suite with a Pin local to a specific client with custom service"""

    TEST_SERVICE = 'test-mongo-patch-client'

    def get_tracer_and_connect(self):
        tracer = get_dummy_tracer()
        # Set a connect-level service, to check that we properly override it
        Pin(service='not-%s' % self.TEST_SERVICE).onto(mongoengine.connect)
        client = mongoengine.connect(port=MONGO_CONFIG['port'])
        Pin(service=self.TEST_SERVICE, tracer=tracer).onto(client)

        return tracer

    def test_patch_unpatch(self):
        tracer = get_dummy_tracer()

        # Test patch idempotence
        patch()
        patch()

        client = mongoengine.connect(port=MONGO_CONFIG['port'])
        Pin.get_from(client).clone(tracer=tracer).onto(client)

        Artist.drop_collection()
        spans = tracer.writer.pop()
        assert spans, spans
        eq_(len(spans), 1)

        mongoengine.connection.disconnect()
        tracer.writer.pop()

        # Test unpatch
        unpatch()

        mongoengine.connect(port=MONGO_CONFIG['port'])

        Artist.drop_collection()
        spans = tracer.writer.pop()
        assert not spans, spans

        # Test patch again
        patch()

        client = mongoengine.connect(port=MONGO_CONFIG['port'])
        Pin.get_from(client).clone(tracer=tracer).onto(client)

        Artist.drop_collection()
        spans = tracer.writer.pop()
        assert spans, spans
        eq_(len(spans), 1)


def _assert_timing(span, start, end):
    assert start < span.start < end
    assert span.duration < end - start
