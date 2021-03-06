# -*- encoding: utf-8 -*-
#
# Copyright © 2014-2015 eNovance
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import datetime
import uuid

import mock
import numpy
import six.moves

from gnocchi import archive_policy
from gnocchi import carbonara
from gnocchi import indexer
from gnocchi import storage
from gnocchi.storage import _carbonara
from gnocchi.storage import ceph
from gnocchi.storage import file
from gnocchi.storage import redis
from gnocchi.storage import s3
from gnocchi.storage import swift
from gnocchi.tests import base as tests_base
from gnocchi.tests import utils as tests_utils
from gnocchi import utils


def datetime64(*args):
    return numpy.datetime64(datetime.datetime(*args))


class TestStorageDriver(tests_base.TestCase):
    def setUp(self):
        super(TestStorageDriver, self).setUp()
        # A lot of tests wants a metric, create one
        self.metric, __ = self._create_metric()

    def _create_metric(self, archive_policy_name="low"):
        m = storage.Metric(uuid.uuid4(),
                           self.archive_policies[archive_policy_name])
        m_sql = self.index.create_metric(m.id, str(uuid.uuid4()),
                                         archive_policy_name)
        return m, m_sql

    def test_driver_str(self):
        driver = storage.get_driver(self.conf)

        if isinstance(driver, file.FileStorage):
            s = driver.basepath
        elif isinstance(driver, ceph.CephStorage):
            s = driver.rados.get_fsid()
        elif isinstance(driver, redis.RedisStorage):
            s = driver._client
        elif isinstance(driver, s3.S3Storage):
            s = driver._bucket_name
        elif isinstance(driver, swift.SwiftStorage):
            s = driver._container_prefix

        self.assertEqual(str(driver), "%s: %s" % (
                         driver.__class__.__name__, s))

    def trigger_processing(self, metrics=None):
        if metrics is None:
            metrics = [str(self.metric.id)]
        self.storage.process_background_tasks(
            self.index, self.incoming, metrics, sync=True)

    def test_get_driver(self):
        driver = storage.get_driver(self.conf)
        self.assertIsInstance(driver, storage.StorageDriver)

    def test_corrupted_data(self):
        if not isinstance(self.storage, _carbonara.CarbonaraBasedStorage):
            self.skipTest("This driver is not based on Carbonara")

        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 1), 69),
        ])
        self.trigger_processing()

        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2014, 1, 1, 13, 0, 1), 1),
        ])

        with mock.patch('gnocchi.carbonara.AggregatedTimeSerie.unserialize',
                        side_effect=carbonara.InvalidData()):
            with mock.patch('gnocchi.carbonara.BoundTimeSerie.unserialize',
                            side_effect=carbonara.InvalidData()):
                self.trigger_processing()

        m = self.storage.get_measures(self.metric)
        self.assertIn((datetime64(2014, 1, 1),
                       numpy.timedelta64(1, 'D'), 1), m)
        self.assertIn((datetime64(2014, 1, 1, 13),
                       numpy.timedelta64(1, 'h'), 1), m)
        self.assertIn((datetime64(2014, 1, 1, 13),
                       numpy.timedelta64(5, 'm'), 1), m)

    def test_aborted_initial_processing(self):
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 1), 5),
        ])
        with mock.patch.object(self.storage, '_store_unaggregated_timeserie',
                               side_effect=Exception):
            try:
                self.trigger_processing()
            except Exception:
                pass

        with mock.patch('gnocchi.storage._carbonara.LOG') as LOG:
            self.trigger_processing()
            self.assertFalse(LOG.error.called)

        m = self.storage.get_measures(self.metric)
        self.assertIn((datetime64(2014, 1, 1),
                       numpy.timedelta64(1, 'D'), 5.0), m)
        self.assertIn((datetime64(2014, 1, 1, 12),
                       numpy.timedelta64(1, 'h'), 5.0), m)
        self.assertIn((datetime64(2014, 1, 1, 12),
                       numpy.timedelta64(5, 'm'), 5.0), m)

    def test_list_metric_with_measures_to_process(self):
        metrics = tests_utils.list_all_incoming_metrics(self.incoming)
        self.assertEqual(set(), metrics)
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 1), 69),
        ])
        metrics = tests_utils.list_all_incoming_metrics(self.incoming)
        self.assertEqual(set([str(self.metric.id)]), metrics)
        self.trigger_processing()
        metrics = tests_utils.list_all_incoming_metrics(self.incoming)
        self.assertEqual(set([]), metrics)

    def test_delete_nonempty_metric(self):
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 1), 69),
        ])
        self.trigger_processing()
        self.storage.delete_metric(self.incoming, self.metric, sync=True)
        self.trigger_processing()
        self.assertEqual([], self.storage.get_measures(self.metric))
        self.assertRaises(storage.MetricDoesNotExist,
                          self.storage._get_unaggregated_timeserie,
                          self.metric)

    def test_delete_nonempty_metric_unprocessed(self):
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 1), 69),
        ])
        self.index.delete_metric(self.metric.id)
        self.trigger_processing()
        __, __, details = self.incoming._build_report(True)
        self.assertIn(str(self.metric.id), details)
        self.storage.expunge_metrics(self.incoming, self.index, sync=True)
        __, __, details = self.incoming._build_report(True)
        self.assertNotIn(str(self.metric.id), details)

    def test_delete_expunge_metric(self):
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 1), 69),
        ])
        self.trigger_processing()
        self.index.delete_metric(self.metric.id)
        self.storage.expunge_metrics(self.incoming, self.index, sync=True)
        self.assertRaises(indexer.NoSuchMetric, self.index.delete_metric,
                          self.metric.id)

    def test_measures_reporting_format(self):
        report = self.incoming.measures_report(True)
        self.assertIsInstance(report, dict)
        self.assertIn('summary', report)
        self.assertIn('metrics', report['summary'])
        self.assertIn('measures', report['summary'])
        self.assertIn('details', report)
        self.assertIsInstance(report['details'], dict)
        report = self.incoming.measures_report(False)
        self.assertIsInstance(report, dict)
        self.assertIn('summary', report)
        self.assertIn('metrics', report['summary'])
        self.assertIn('measures', report['summary'])
        self.assertNotIn('details', report)

    def test_measures_reporting(self):
        m2, __ = self._create_metric('medium')
        for i in six.moves.range(60):
            self.incoming.add_measures(self.metric, [
                storage.Measure(datetime64(2014, 1, 1, 12, 0, i), 69),
            ])
            self.incoming.add_measures(m2, [
                storage.Measure(datetime64(2014, 1, 1, 12, 0, i), 69),
            ])
        report = self.incoming.measures_report(True)
        self.assertIsInstance(report, dict)
        self.assertEqual(2, report['summary']['metrics'])
        self.assertEqual(120, report['summary']['measures'])
        self.assertIn('details', report)
        self.assertIsInstance(report['details'], dict)
        report = self.incoming.measures_report(False)
        self.assertIsInstance(report, dict)
        self.assertEqual(2, report['summary']['metrics'])
        self.assertEqual(120, report['summary']['measures'])

    def test_add_measures_big(self):
        m, __ = self._create_metric('high')
        self.incoming.add_measures(m, [
            storage.Measure(datetime64(2014, 1, 1, 12, i, j), 100)
            for i in six.moves.range(0, 60) for j in six.moves.range(0, 60)])
        self.trigger_processing([str(m.id)])

        self.assertEqual(3661, len(self.storage.get_measures(m)))

    @mock.patch('gnocchi.carbonara.SplitKey.POINTS_PER_SPLIT', 48)
    def test_add_measures_update_subset_split(self):
        m, m_sql = self._create_metric('medium')
        measures = [
            storage.Measure(datetime64(2014, 1, 6, i, j, 0), 100)
            for i in six.moves.range(2) for j in six.moves.range(0, 60, 2)]
        self.incoming.add_measures(m, measures)
        self.trigger_processing([str(m.id)])

        # add measure to end, in same aggregate time as last point.
        self.incoming.add_measures(m, [
            storage.Measure(datetime64(2014, 1, 6, 1, 58, 1), 100)])

        with mock.patch.object(self.storage, '_store_metric_measures') as c:
            # should only resample last aggregate
            self.trigger_processing([str(m.id)])
        count = 0
        for call in c.mock_calls:
            # policy is 60 points and split is 48. should only update 2nd half
            args = call[1]
            if (args[0] == m_sql
               and args[2] == 'mean'
               and args[1].sampling == numpy.timedelta64(1, 'm')):
                count += 1
        self.assertEqual(1, count)

    def test_add_measures_update_subset(self):
        m, m_sql = self._create_metric('medium')
        measures = [
            storage.Measure(datetime64(2014, 1, 6, i, j, 0), 100)
            for i in six.moves.range(2) for j in six.moves.range(0, 60, 2)]
        self.incoming.add_measures(m, measures)
        self.trigger_processing([str(m.id)])

        # add measure to end, in same aggregate time as last point.
        new_point = datetime64(2014, 1, 6, 1, 58, 1)
        self.incoming.add_measures(
            m, [storage.Measure(new_point, 100)])

        with mock.patch.object(self.incoming, 'add_measures') as c:
            self.trigger_processing([str(m.id)])
        for __, args, __ in c.mock_calls:
            self.assertEqual(
                list(args[3])[0][0], carbonara.round_timestamp(
                    new_point, args[1].granularity * 10e8))

    def test_delete_old_measures(self):
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 1), 69),
            storage.Measure(datetime64(2014, 1, 1, 12, 7, 31), 42),
            storage.Measure(datetime64(2014, 1, 1, 12, 9, 31), 4),
            storage.Measure(datetime64(2014, 1, 1, 12, 12, 45), 44),
        ])
        self.trigger_processing()

        self.assertEqual([
            (datetime64(2014, 1, 1), numpy.timedelta64(1, 'D'), 39.75),
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(1, 'h'), 39.75),
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(5, 'm'), 69.0),
            (datetime64(2014, 1, 1, 12, 5), numpy.timedelta64(5, 'm'), 23.0),
            (datetime64(2014, 1, 1, 12, 10), numpy.timedelta64(5, 'm'), 44.0),
        ], self.storage.get_measures(self.metric))

        # One year later…
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2015, 1, 1, 12, 0, 1), 69),
        ])
        self.trigger_processing()

        self.assertEqual([
            (datetime64(2014, 1, 1), numpy.timedelta64(1, 'D'), 39.75),
            (datetime64(2015, 1, 1), numpy.timedelta64(1, 'D'), 69),
            (datetime64(2015, 1, 1, 12), numpy.timedelta64(1, 'h'), 69),
            (datetime64(2015, 1, 1, 12), numpy.timedelta64(5, 'm'), 69),
        ], self.storage.get_measures(self.metric))

        self.assertEqual({
            carbonara.SplitKey(numpy.datetime64(1244160000, 's'),
                               numpy.timedelta64(1, 'D')),
        }, self.storage._list_split_keys_for_metric(
            self.metric, "mean", numpy.timedelta64(1, 'D')))
        self.assertEqual({
            carbonara.SplitKey(numpy.datetime64(1412640000, 's'),
                               numpy.timedelta64(1, 'h')),
        }, self.storage._list_split_keys_for_metric(
            self.metric, "mean", numpy.timedelta64(1, 'h')))
        self.assertEqual({
            carbonara.SplitKey(numpy.datetime64(1419120000, 's'),
                               numpy.timedelta64(5, 'm')),
        }, self.storage._list_split_keys_for_metric(
            self.metric, "mean", numpy.timedelta64(5, 'm')))

    def test_rewrite_measures(self):
        # Create an archive policy that spans on several splits. Each split
        # being 3600 points, let's go for 36k points so we have 10 splits.
        apname = str(uuid.uuid4())
        ap = archive_policy.ArchivePolicy(apname, 0, [(36000, 60)])
        self.index.create_archive_policy(ap)
        self.metric = storage.Metric(uuid.uuid4(), ap)
        self.index.create_metric(self.metric.id, str(uuid.uuid4()),
                                 apname)

        # First store some points scattered across different splits
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2016, 1, 1, 12, 0, 1), 69),
            storage.Measure(datetime64(2016, 1, 2, 13, 7, 31), 42),
            storage.Measure(datetime64(2016, 1, 4, 14, 9, 31), 4),
            storage.Measure(datetime64(2016, 1, 6, 15, 12, 45), 44),
        ])
        self.trigger_processing()

        self.assertEqual({
            carbonara.SplitKey(numpy.datetime64(1451520000, 's'),
                               numpy.timedelta64(1, 'm')),
            carbonara.SplitKey(numpy.datetime64(1451736000, 's'),
                               numpy.timedelta64(1, 'm')),
            carbonara.SplitKey(numpy.datetime64(1451952000, 's'),
                               numpy.timedelta64(1, 'm')),
        }, self.storage._list_split_keys_for_metric(
            self.metric, "mean", numpy.timedelta64(1, 'm')))

        if self.storage.WRITE_FULL:
            assertCompressedIfWriteFull = self.assertTrue
        else:
            assertCompressedIfWriteFull = self.assertFalse

        data = self.storage._get_measures(
            self.metric, carbonara.SplitKey(
                numpy.datetime64(1451520000, 's'),
                numpy.timedelta64(1, 'm'),
            ), "mean")
        self.assertTrue(carbonara.AggregatedTimeSerie.is_compressed(data))
        data = self.storage._get_measures(
            self.metric, carbonara.SplitKey(
                numpy.datetime64(1451736000, 's'),
                numpy.timedelta64(60, 's'),
            ), "mean")
        self.assertTrue(carbonara.AggregatedTimeSerie.is_compressed(data))
        data = self.storage._get_measures(
            self.metric, carbonara.SplitKey(
                numpy.datetime64(1451952000, 's'),
                numpy.timedelta64(60, 's'),
            ), "mean")
        assertCompressedIfWriteFull(
            carbonara.AggregatedTimeSerie.is_compressed(data))

        self.assertEqual([
            (datetime64(2016, 1, 1, 12), numpy.timedelta64(1, 'm'), 69),
            (datetime64(2016, 1, 2, 13, 7), numpy.timedelta64(1, 'm'), 42),
            (datetime64(2016, 1, 4, 14, 9), numpy.timedelta64(1, 'm'), 4),
            (datetime64(2016, 1, 6, 15, 12), numpy.timedelta64(1, 'm'), 44),
        ], self.storage.get_measures(self.metric,
                                     granularity=numpy.timedelta64(1, 'm')))

        # Now store brand new points that should force a rewrite of one of the
        # split (keep in mind the back window size in one hour here). We move
        # the BoundTimeSerie processing timeserie far away from its current
        # range.
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2016, 1, 10, 16, 18, 45), 45),
            storage.Measure(datetime64(2016, 1, 10, 17, 12, 45), 46),
        ])
        self.trigger_processing()

        self.assertEqual({
            carbonara.SplitKey(numpy.datetime64(1452384000, 's'),
                               numpy.timedelta64(1, 'm')),
            carbonara.SplitKey(numpy.datetime64(1451736000, 's'),
                               numpy.timedelta64(1, 'm')),
            carbonara.SplitKey(numpy.datetime64(1451520000, 's'),
                               numpy.timedelta64(1, 'm')),
            carbonara.SplitKey(numpy.datetime64(1451952000, 's'),
                               numpy.timedelta64(1, 'm')),
        }, self.storage._list_split_keys_for_metric(
            self.metric, "mean", numpy.timedelta64(1, 'm')))
        data = self.storage._get_measures(
            self.metric, carbonara.SplitKey(
                numpy.datetime64(1451520000, 's'),
                numpy.timedelta64(60, 's'),
            ), "mean")
        self.assertTrue(carbonara.AggregatedTimeSerie.is_compressed(data))
        data = self.storage._get_measures(
            self.metric, carbonara.SplitKey(
                numpy.datetime64(1451736000, 's'),
                numpy.timedelta64(60, 's'),
            ), "mean")
        self.assertTrue(carbonara.AggregatedTimeSerie.is_compressed(data))
        data = self.storage._get_measures(
            self.metric, carbonara.SplitKey(
                numpy.datetime64(1451952000, 's'),
                numpy.timedelta64(1, 'm'),
            ), "mean")
        # Now this one is compressed because it has been rewritten!
        self.assertTrue(carbonara.AggregatedTimeSerie.is_compressed(data))
        data = self.storage._get_measures(
            self.metric, carbonara.SplitKey(
                numpy.datetime64(1452384000, 's'),
                numpy.timedelta64(60, 's'),
            ), "mean")
        assertCompressedIfWriteFull(
            carbonara.AggregatedTimeSerie.is_compressed(data))

        self.assertEqual([
            (datetime64(2016, 1, 1, 12), numpy.timedelta64(1, 'm'), 69),
            (datetime64(2016, 1, 2, 13, 7), numpy.timedelta64(1, 'm'), 42),
            (datetime64(2016, 1, 4, 14, 9), numpy.timedelta64(1, 'm'), 4),
            (datetime64(2016, 1, 6, 15, 12), numpy.timedelta64(1, 'm'), 44),
            (datetime64(2016, 1, 10, 16, 18), numpy.timedelta64(1, 'm'), 45),
            (datetime64(2016, 1, 10, 17, 12), numpy.timedelta64(1, 'm'), 46),
        ], self.storage.get_measures(self.metric,
                                     granularity=numpy.timedelta64(1, 'm')))

    def test_rewrite_measures_oldest_mutable_timestamp_eq_next_key(self):
        """See LP#1655422"""
        # Create an archive policy that spans on several splits. Each split
        # being 3600 points, let's go for 36k points so we have 10 splits.
        apname = str(uuid.uuid4())
        ap = archive_policy.ArchivePolicy(apname, 0, [(36000, 60)])
        self.index.create_archive_policy(ap)
        self.metric = storage.Metric(uuid.uuid4(), ap)
        self.index.create_metric(self.metric.id, str(uuid.uuid4()),
                                 apname)

        # First store some points scattered across different splits
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2016, 1, 1, 12, 0, 1), 69),
            storage.Measure(datetime64(2016, 1, 2, 13, 7, 31), 42),
            storage.Measure(datetime64(2016, 1, 4, 14, 9, 31), 4),
            storage.Measure(datetime64(2016, 1, 6, 15, 12, 45), 44),
        ])
        self.trigger_processing()

        self.assertEqual({
            carbonara.SplitKey(numpy.datetime64(1451520000, 's'),
                               numpy.timedelta64(1, 'm')),
            carbonara.SplitKey(numpy.datetime64(1451736000, 's'),
                               numpy.timedelta64(1, 'm')),
            carbonara.SplitKey(numpy.datetime64(1451952000, 's'),
                               numpy.timedelta64(1, 'm')),
        }, self.storage._list_split_keys_for_metric(
            self.metric, "mean", numpy.timedelta64(1, 'm')))

        if self.storage.WRITE_FULL:
            assertCompressedIfWriteFull = self.assertTrue
        else:
            assertCompressedIfWriteFull = self.assertFalse

        data = self.storage._get_measures(
            self.metric, carbonara.SplitKey(
                numpy.datetime64(1451520000, 's'),
                numpy.timedelta64(1, 'm'),
            ), "mean")
        self.assertTrue(carbonara.AggregatedTimeSerie.is_compressed(data))
        data = self.storage._get_measures(
            self.metric, carbonara.SplitKey(
                numpy.datetime64(1451736000, 's'),
                numpy.timedelta64(1, 'm'),
            ), "mean")
        self.assertTrue(carbonara.AggregatedTimeSerie.is_compressed(data))
        data = self.storage._get_measures(
            self.metric, carbonara.SplitKey(
                numpy.datetime64(1451952000, 's'),
                numpy.timedelta64(1, 'm')
            ), "mean")
        assertCompressedIfWriteFull(
            carbonara.AggregatedTimeSerie.is_compressed(data))

        self.assertEqual([
            (datetime64(2016, 1, 1, 12), numpy.timedelta64(1, 'm'), 69),
            (datetime64(2016, 1, 2, 13, 7), numpy.timedelta64(1, 'm'), 42),
            (datetime64(2016, 1, 4, 14, 9), numpy.timedelta64(1, 'm'), 4),
            (datetime64(2016, 1, 6, 15, 12), numpy.timedelta64(1, 'm'), 44),
        ], self.storage.get_measures(self.metric,
                                     granularity=numpy.timedelta64(60, 's')))

        # Now store brand new points that should force a rewrite of one of the
        # split (keep in mind the back window size in one hour here). We move
        # the BoundTimeSerie processing timeserie far away from its current
        # range.

        # Here we test a special case where the oldest_mutable_timestamp will
        # be 2016-01-10TOO:OO:OO = 1452384000.0, our new split key.
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2016, 1, 10, 0, 12), 45),
        ])
        self.trigger_processing()

        self.assertEqual({
            carbonara.SplitKey(numpy.datetime64('2016-01-10T00:00:00'),
                               numpy.timedelta64(1, 'm')),
            carbonara.SplitKey(numpy.datetime64('2016-01-02T12:00:00'),
                               numpy.timedelta64(1, 'm')),
            carbonara.SplitKey(numpy.datetime64('2015-12-31T00:00:00'),
                               numpy.timedelta64(1, 'm')),
            carbonara.SplitKey(numpy.datetime64('2016-01-05T00:00:00'),
                               numpy.timedelta64(1, 'm')),
        }, self.storage._list_split_keys_for_metric(
            self.metric, "mean", numpy.timedelta64(1, 'm')))
        data = self.storage._get_measures(
            self.metric, carbonara.SplitKey(
                numpy.datetime64(1451520000, 's'),
                numpy.timedelta64(1, 'm'),
            ), "mean")
        self.assertTrue(carbonara.AggregatedTimeSerie.is_compressed(data))
        data = self.storage._get_measures(
            self.metric, carbonara.SplitKey(
                numpy.datetime64(1451736000, 's'),
                numpy.timedelta64(1, 'm'),
            ), "mean")
        self.assertTrue(carbonara.AggregatedTimeSerie.is_compressed(data))
        data = self.storage._get_measures(
            self.metric, carbonara.SplitKey(
                numpy.datetime64(1451952000, 's'),
                numpy.timedelta64(60, 's')
            ), "mean")
        # Now this one is compressed because it has been rewritten!
        self.assertTrue(carbonara.AggregatedTimeSerie.is_compressed(data))
        data = self.storage._get_measures(
            self.metric, carbonara.SplitKey(
                numpy.datetime64(1452384000, 's'),
                numpy.timedelta64(1, 'm'),
            ), "mean")
        assertCompressedIfWriteFull(
            carbonara.AggregatedTimeSerie.is_compressed(data))

        self.assertEqual([
            (datetime64(2016, 1, 1, 12), numpy.timedelta64(1, 'm'), 69),
            (datetime64(2016, 1, 2, 13, 7), numpy.timedelta64(1, 'm'), 42),
            (datetime64(2016, 1, 4, 14, 9), numpy.timedelta64(1, 'm'), 4),
            (datetime64(2016, 1, 6, 15, 12), numpy.timedelta64(1, 'm'), 44),
            (datetime64(2016, 1, 10, 0, 12), numpy.timedelta64(1, 'm'), 45),
        ], self.storage.get_measures(self.metric,
                                     granularity=numpy.timedelta64(60, 's')))

    def test_rewrite_measures_corruption_missing_file(self):
        # Create an archive policy that spans on several splits. Each split
        # being 3600 points, let's go for 36k points so we have 10 splits.
        apname = str(uuid.uuid4())
        ap = archive_policy.ArchivePolicy(apname, 0, [(36000, 60)])
        self.index.create_archive_policy(ap)
        self.metric = storage.Metric(uuid.uuid4(), ap)
        self.index.create_metric(self.metric.id, str(uuid.uuid4()),
                                 apname)

        # First store some points scattered across different splits
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2016, 1, 1, 12, 0, 1), 69),
            storage.Measure(datetime64(2016, 1, 2, 13, 7, 31), 42),
            storage.Measure(datetime64(2016, 1, 4, 14, 9, 31), 4),
            storage.Measure(datetime64(2016, 1, 6, 15, 12, 45), 44),
        ])
        self.trigger_processing()

        self.assertEqual({
            carbonara.SplitKey(numpy.datetime64('2015-12-31T00:00:00'),
                               numpy.timedelta64(1, 'm')),
            carbonara.SplitKey(numpy.datetime64('2016-01-02T12:00:00'),
                               numpy.timedelta64(1, 'm')),
            carbonara.SplitKey(numpy.datetime64('2016-01-05T00:00:00'),
                               numpy.timedelta64(1, 'm')),
        }, self.storage._list_split_keys_for_metric(
            self.metric, "mean", numpy.timedelta64(1, 'm')))

        if self.storage.WRITE_FULL:
            assertCompressedIfWriteFull = self.assertTrue
        else:
            assertCompressedIfWriteFull = self.assertFalse

        data = self.storage._get_measures(
            self.metric,
            carbonara.SplitKey(
                numpy.datetime64(1451520000, 's'),
                numpy.timedelta64(1, 'm'),
            ), "mean")
        self.assertTrue(carbonara.AggregatedTimeSerie.is_compressed(data))
        data = self.storage._get_measures(
            self.metric, carbonara.SplitKey(
                numpy.datetime64(1451736000, 's'),
                numpy.timedelta64(1, 'm')
            ), "mean")
        self.assertTrue(carbonara.AggregatedTimeSerie.is_compressed(data))
        data = self.storage._get_measures(
            self.metric, carbonara.SplitKey(
                numpy.datetime64(1451952000, 's'),
                numpy.timedelta64(1, 'm'),
            ), "mean")
        assertCompressedIfWriteFull(
            carbonara.AggregatedTimeSerie.is_compressed(data))

        self.assertEqual([
            (datetime64(2016, 1, 1, 12),
             numpy.timedelta64(1, 'm'), 69),
            (datetime64(2016, 1, 2, 13, 7),
             numpy.timedelta64(1, 'm'), 42),
            (datetime64(2016, 1, 4, 14, 9),
             numpy.timedelta64(1, 'm'), 4),
            (datetime64(2016, 1, 6, 15, 12),
             numpy.timedelta64(1, 'm'), 44),
        ], self.storage.get_measures(self.metric,
                                     granularity=numpy.timedelta64(60, 's')))

        # Test what happens if we delete the latest split and then need to
        # compress it!
        self.storage._delete_metric_measures(
            self.metric, carbonara.SplitKey(
                numpy.datetime64(1451952000, 's'),
                numpy.timedelta64(1, 'm'),
            ), 'mean')

        # Now store brand new points that should force a rewrite of one of the
        # split (keep in mind the back window size in one hour here). We move
        # the BoundTimeSerie processing timeserie far away from its current
        # range.
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2016, 1, 10, 16, 18, 45), 45),
            storage.Measure(datetime64(2016, 1, 10, 17, 12, 45), 46),
        ])
        self.trigger_processing()

    def test_rewrite_measures_corruption_bad_data(self):
        # Create an archive policy that spans on several splits. Each split
        # being 3600 points, let's go for 36k points so we have 10 splits.
        apname = str(uuid.uuid4())
        ap = archive_policy.ArchivePolicy(apname, 0, [(36000, 60)])
        self.index.create_archive_policy(ap)
        self.metric = storage.Metric(uuid.uuid4(), ap)
        self.index.create_metric(self.metric.id, str(uuid.uuid4()),
                                 apname)

        # First store some points scattered across different splits
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2016, 1, 1, 12, 0, 1), 69),
            storage.Measure(datetime64(2016, 1, 2, 13, 7, 31), 42),
            storage.Measure(datetime64(2016, 1, 4, 14, 9, 31), 4),
            storage.Measure(datetime64(2016, 1, 6, 15, 12, 45), 44),
        ])
        self.trigger_processing()

        self.assertEqual({
            carbonara.SplitKey(numpy.datetime64(1451520000, 's'),
                               numpy.timedelta64(1, 'm')),
            carbonara.SplitKey(numpy.datetime64(1451736000, 's'),
                               numpy.timedelta64(1, 'm')),
            carbonara.SplitKey(numpy.datetime64(1451952000, 's'),
                               numpy.timedelta64(1, 'm')),
        }, self.storage._list_split_keys_for_metric(
            self.metric, "mean", numpy.timedelta64(1, 'm')))

        if self.storage.WRITE_FULL:
            assertCompressedIfWriteFull = self.assertTrue
        else:
            assertCompressedIfWriteFull = self.assertFalse

        data = self.storage._get_measures(
            self.metric, carbonara.SplitKey(
                numpy.datetime64(1451520000, 's'),
                numpy.timedelta64(60, 's'),
            ), "mean")
        self.assertTrue(carbonara.AggregatedTimeSerie.is_compressed(data))
        data = self.storage._get_measures(
            self.metric, carbonara.SplitKey(
                numpy.datetime64(1451736000, 's'),
                numpy.timedelta64(1, 'm'),
            ), "mean")
        self.assertTrue(carbonara.AggregatedTimeSerie.is_compressed(data))
        data = self.storage._get_measures(
            self.metric, carbonara.SplitKey(
                numpy.datetime64(1451952000, 's'),
                numpy.timedelta64(1, 'm'),
            ), "mean")
        assertCompressedIfWriteFull(
            carbonara.AggregatedTimeSerie.is_compressed(data))

        self.assertEqual([
            (datetime64(2016, 1, 1, 12), numpy.timedelta64(1, 'm'), 69),
            (datetime64(2016, 1, 2, 13, 7), numpy.timedelta64(1, 'm'), 42),
            (datetime64(2016, 1, 4, 14, 9), numpy.timedelta64(1, 'm'), 4),
            (datetime64(2016, 1, 6, 15, 12), numpy.timedelta64(1, 'm'), 44),
        ], self.storage.get_measures(self.metric,
                                     granularity=numpy.timedelta64(1, 'm')))

        # Test what happens if we write garbage
        self.storage._store_metric_measures(
            self.metric, carbonara.SplitKey(
                numpy.datetime64(1451952000, 's'),
                numpy.timedelta64(1, 'm'),
            ), "mean",
            b"oh really?")

        # Now store brand new points that should force a rewrite of one of the
        # split (keep in mind the back window size in one hour here). We move
        # the BoundTimeSerie processing timeserie far away from its current
        # range.
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2016, 1, 10, 16, 18, 45), 45),
            storage.Measure(datetime64(2016, 1, 10, 17, 12, 45), 46),
        ])
        self.trigger_processing()

    def test_updated_measures(self):
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 1), 69),
            storage.Measure(datetime64(2014, 1, 1, 12, 7, 31), 42),
        ])
        self.trigger_processing()

        self.assertEqual([
            (datetime64(2014, 1, 1), numpy.timedelta64(1, 'D'), 55.5),
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(1, 'h'), 55.5),
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(5, 'm'), 69),
            (datetime64(2014, 1, 1, 12, 5), numpy.timedelta64(5, 'm'), 42.0),
        ], self.storage.get_measures(self.metric))

        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2014, 1, 1, 12, 9, 31), 4),
            storage.Measure(datetime64(2014, 1, 1, 12, 12, 45), 44),
        ])
        self.trigger_processing()

        self.assertEqual([
            (datetime64(2014, 1, 1), numpy.timedelta64(1, 'D'), 39.75),
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(1, 'h'), 39.75),
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(5, 'm'), 69.0),
            (datetime64(2014, 1, 1, 12, 5), numpy.timedelta64(5, 'm'), 23.0),
            (datetime64(2014, 1, 1, 12, 10), numpy.timedelta64(5, 'm'), 44.0),
        ], self.storage.get_measures(self.metric))

        self.assertEqual([
            (datetime64(2014, 1, 1), numpy.timedelta64(1, 'D'), 69),
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(1, 'h'), 69.0),
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(5, 'm'), 69.0),
            (datetime64(2014, 1, 1, 12, 5), numpy.timedelta64(5, 'm'), 42.0),
            (datetime64(2014, 1, 1, 12, 10), numpy.timedelta64(5, 'm'), 44.0),
        ], self.storage.get_measures(self.metric, aggregation='max'))

        self.assertEqual([
            (datetime64(2014, 1, 1), numpy.timedelta64(1, 'D'), 4),
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(1, 'h'), 4),
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(5, 'm'), 69.0),
            (datetime64(2014, 1, 1, 12, 5), numpy.timedelta64(5, 'm'), 4.0),
            (datetime64(2014, 1, 1, 12, 10), numpy.timedelta64(5, 'm'), 44.0),
        ], self.storage.get_measures(self.metric, aggregation='min'))

    def test_add_and_get_measures(self):
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 1), 69),
            storage.Measure(datetime64(2014, 1, 1, 12, 7, 31), 42),
            storage.Measure(datetime64(2014, 1, 1, 12, 9, 31), 4),
            storage.Measure(datetime64(2014, 1, 1, 12, 12, 45), 44),
        ])
        self.trigger_processing()

        self.assertEqual([
            (datetime64(2014, 1, 1), numpy.timedelta64(1, 'D'), 39.75),
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(1, 'h'), 39.75),
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(5, 'm'), 69.0),
            (datetime64(2014, 1, 1, 12, 5), numpy.timedelta64(5, 'm'), 23.0),
            (datetime64(2014, 1, 1, 12, 10), numpy.timedelta64(5, 'm'), 44.0),
        ], self.storage.get_measures(self.metric))

        self.assertEqual([
            (datetime64(2014, 1, 1), numpy.timedelta64(1, 'D'), 39.75),
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(1, 'h'), 39.75),
            (datetime64(2014, 1, 1, 12, 10), numpy.timedelta64(5, 'm'), 44.0),
        ], self.storage.get_measures(
            self.metric,
            from_timestamp=datetime64(2014, 1, 1, 12, 10, 0)))

        self.assertEqual([
            (datetime64(2014, 1, 1), numpy.timedelta64(1, 'D'), 39.75),
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(1, 'h'), 39.75),
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(5, 'm'), 69.0),
            (datetime64(2014, 1, 1, 12, 5), numpy.timedelta64(5, 'm'), 23.0),
        ], self.storage.get_measures(
            self.metric,
            to_timestamp=datetime64(2014, 1, 1, 12, 6, 0)))

        self.assertEqual([
            (datetime64(2014, 1, 1), numpy.timedelta64(1, 'D'), 39.75),
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(1, 'h'), 39.75),
            (datetime64(2014, 1, 1, 12, 10), numpy.timedelta64(5, 'm'), 44.0),
        ], self.storage.get_measures(
            self.metric,
            to_timestamp=datetime64(2014, 1, 1, 12, 10, 10),
            from_timestamp=datetime64(2014, 1, 1, 12, 10, 10)))

        self.assertEqual([
            (datetime64(2014, 1, 1), numpy.timedelta64(1, 'D'), 39.75),
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(1, 'h'), 39.75),
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(5, 'm'), 69.0),
        ], self.storage.get_measures(
            self.metric,
            from_timestamp=datetime64(2014, 1, 1, 12, 0, 0),
            to_timestamp=datetime64(2014, 1, 1, 12, 0, 2)))

        self.assertEqual([
            (datetime64(2014, 1, 1), numpy.timedelta64(1, 'D'), 39.75),
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(1, 'h'), 39.75),
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(5, 'm'), 69.0),
        ], self.storage.get_measures(
            self.metric,
            from_timestamp=datetime64(2014, 1, 1, 12),
            to_timestamp=datetime64(2014, 1, 1, 12, 0, 2)))

        self.assertEqual([
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(1, 'h'), 39.75),
        ], self.storage.get_measures(
            self.metric,
            from_timestamp=datetime64(2014, 1, 1, 12, 0, 0),
            to_timestamp=datetime64(2014, 1, 1, 12, 0, 2),
            granularity=numpy.timedelta64(1, 'h')))

        self.assertEqual([
            (datetime64(2014, 1, 1, 12), numpy.timedelta64(5, 'm'), 69.0),
        ], self.storage.get_measures(
            self.metric,
            from_timestamp=datetime64(2014, 1, 1, 12, 0, 0),
            to_timestamp=datetime64(2014, 1, 1, 12, 0, 2),
            granularity=numpy.timedelta64(5, 'm')))

        self.assertRaises(storage.GranularityDoesNotExist,
                          self.storage.get_measures,
                          self.metric,
                          granularity=numpy.timedelta64(42, 's'))

    def test_get_cross_metric_measures_unknown_metric(self):
        self.assertEqual([],
                         self.storage.get_cross_metric_measures(
                             [storage.Metric(uuid.uuid4(),
                                             self.archive_policies['low']),
                              storage.Metric(uuid.uuid4(),
                                             self.archive_policies['low'])]))

    def test_get_measure_unknown_aggregation(self):
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 1), 69),
            storage.Measure(datetime64(2014, 1, 1, 12, 7, 31), 42),
            storage.Measure(datetime64(2014, 1, 1, 12, 9, 31), 4),
            storage.Measure(datetime64(2014, 1, 1, 12, 12, 45), 44),
        ])
        self.assertRaises(storage.AggregationDoesNotExist,
                          self.storage.get_measures,
                          self.metric, aggregation='last')

    def test_get_cross_metric_measures_unknown_aggregation(self):
        metric2 = storage.Metric(uuid.uuid4(),
                                 self.archive_policies['low'])
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 1), 69),
            storage.Measure(datetime64(2014, 1, 1, 12, 7, 31), 42),
            storage.Measure(datetime64(2014, 1, 1, 12, 9, 31), 4),
            storage.Measure(datetime64(2014, 1, 1, 12, 12, 45), 44),
        ])
        self.incoming.add_measures(metric2, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 1), 69),
            storage.Measure(datetime64(2014, 1, 1, 12, 7, 31), 42),
            storage.Measure(datetime64(2014, 1, 1, 12, 9, 31), 4),
            storage.Measure(datetime64(2014, 1, 1, 12, 12, 45), 44),
        ])
        self.assertRaises(storage.AggregationDoesNotExist,
                          self.storage.get_cross_metric_measures,
                          [self.metric, metric2],
                          aggregation='last')

    def test_get_cross_metric_measures_unknown_granularity(self):
        metric2 = storage.Metric(uuid.uuid4(),
                                 self.archive_policies['low'])
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 1), 69),
            storage.Measure(datetime64(2014, 1, 1, 12, 7, 31), 42),
            storage.Measure(datetime64(2014, 1, 1, 12, 9, 31), 4),
            storage.Measure(datetime64(2014, 1, 1, 12, 12, 45), 44),
        ])
        self.incoming.add_measures(metric2, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 1), 69),
            storage.Measure(datetime64(2014, 1, 1, 12, 7, 31), 42),
            storage.Measure(datetime64(2014, 1, 1, 12, 9, 31), 4),
            storage.Measure(datetime64(2014, 1, 1, 12, 12, 45), 44),
        ])
        self.assertRaises(storage.GranularityDoesNotExist,
                          self.storage.get_cross_metric_measures,
                          [self.metric, metric2],
                          granularity=numpy.timedelta64(12345456, 'ms'))

    def test_add_and_get_cross_metric_measures_different_archives(self):
        metric2 = storage.Metric(uuid.uuid4(),
                                 self.archive_policies['no_granularity_match'])
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 1), 69),
            storage.Measure(datetime64(2014, 1, 1, 12, 7, 31), 42),
            storage.Measure(datetime64(2014, 1, 1, 12, 9, 31), 4),
            storage.Measure(datetime64(2014, 1, 1, 12, 12, 45), 44),
        ])
        self.incoming.add_measures(metric2, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 1), 69),
            storage.Measure(datetime64(2014, 1, 1, 12, 7, 31), 42),
            storage.Measure(datetime64(2014, 1, 1, 12, 9, 31), 4),
            storage.Measure(datetime64(2014, 1, 1, 12, 12, 45), 44),
        ])

        self.assertRaises(storage.MetricUnaggregatable,
                          self.storage.get_cross_metric_measures,
                          [self.metric, metric2])

    def test_add_and_get_cross_metric_measures(self):
        metric2, __ = self._create_metric()
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 1), 69),
            storage.Measure(datetime64(2014, 1, 1, 12, 7, 31), 42),
            storage.Measure(datetime64(2014, 1, 1, 12, 9, 31), 4),
            storage.Measure(datetime64(2014, 1, 1, 12, 12, 45), 44),
        ])
        self.incoming.add_measures(metric2, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 5), 9),
            storage.Measure(datetime64(2014, 1, 1, 12, 7, 41), 2),
            storage.Measure(datetime64(2014, 1, 1, 12, 10, 31), 4),
            storage.Measure(datetime64(2014, 1, 1, 12, 13, 10), 4),
        ])
        self.trigger_processing([str(self.metric.id), str(metric2.id)])

        values = self.storage.get_cross_metric_measures([self.metric, metric2])
        self.assertEqual([
            (utils.datetime_utc(2014, 1, 1, 0, 0, 0),
             numpy.timedelta64(1, 'D'), 22.25),
            (utils.datetime_utc(2014, 1, 1, 12, 0, 0),
             numpy.timedelta64(1, 'h'), 22.25),
            (utils.datetime_utc(2014, 1, 1, 12, 0, 0),
             numpy.timedelta64(5, 'm'), 39.0),
            (utils.datetime_utc(2014, 1, 1, 12, 5, 0),
             numpy.timedelta64(5, 'm'), 12.5),
            (utils.datetime_utc(2014, 1, 1, 12, 10, 0),
             numpy.timedelta64(5, 'm'), 24.0)
        ], values)

        values = self.storage.get_cross_metric_measures([self.metric, metric2],
                                                        reaggregation='max')
        self.assertEqual([
            (utils.datetime_utc(2014, 1, 1, 0, 0, 0),
             numpy.timedelta64(1, 'D'), 39.75),
            (utils.datetime_utc(2014, 1, 1, 12, 0, 0),
             numpy.timedelta64(1, 'h'), 39.75),
            (utils.datetime_utc(2014, 1, 1, 12, 0, 0),
             numpy.timedelta64(5, 'm'), 69),
            (utils.datetime_utc(2014, 1, 1, 12, 5, 0),
             numpy.timedelta64(5, 'm'), 23),
            (utils.datetime_utc(2014, 1, 1, 12, 10, 0),
             numpy.timedelta64(5, 'm'), 44)
        ], values)

        values = self.storage.get_cross_metric_measures(
            [self.metric, metric2],
            from_timestamp=datetime64(2014, 1, 1, 12, 10, 0))
        self.assertEqual([
            (utils.datetime_utc(2014, 1, 1),
             numpy.timedelta64(1, 'D'), 22.25),
            (utils.datetime_utc(2014, 1, 1, 12),
             numpy.timedelta64(1, 'h'), 22.25),
            (utils.datetime_utc(2014, 1, 1, 12, 10, 0),
             numpy.timedelta64(5, 'm'), 24.0),
        ], values)

        values = self.storage.get_cross_metric_measures(
            [self.metric, metric2],
            to_timestamp=datetime64(2014, 1, 1, 12, 5, 0))

        self.assertEqual([
            (utils.datetime_utc(2014, 1, 1, 0, 0, 0),
             numpy.timedelta64(1, 'D'), 22.25),
            (utils.datetime_utc(2014, 1, 1, 12, 0, 0),
             numpy.timedelta64(1, 'h'), 22.25),
            (utils.datetime_utc(2014, 1, 1, 12, 0, 0),
             numpy.timedelta64(5, 'm'), 39.0),
        ], values)

        values = self.storage.get_cross_metric_measures(
            [self.metric, metric2],
            from_timestamp=datetime64(2014, 1, 1, 12, 10, 10),
            to_timestamp=datetime64(2014, 1, 1, 12, 10, 10))
        self.assertEqual([
            (utils.datetime_utc(2014, 1, 1),
             numpy.timedelta64(1, 'D'), 22.25),
            (utils.datetime_utc(2014, 1, 1, 12),
             numpy.timedelta64(1, 'h'), 22.25),
            (utils.datetime_utc(2014, 1, 1, 12, 10),
             numpy.timedelta64(5, 'm'), 24.0),
        ], values)

        values = self.storage.get_cross_metric_measures(
            [self.metric, metric2],
            from_timestamp=datetime64(2014, 1, 1, 12, 0, 0),
            to_timestamp=datetime64(2014, 1, 1, 12, 0, 1))

        self.assertEqual([
            (utils.datetime_utc(2014, 1, 1),
             numpy.timedelta64(1, 'D'), 22.25),
            (utils.datetime_utc(2014, 1, 1, 12, 0, 0),
             numpy.timedelta64(1, 'h'), 22.25),
            (utils.datetime_utc(2014, 1, 1, 12, 0, 0),
             numpy.timedelta64(5, 'm'), 39.0),
        ], values)

        values = self.storage.get_cross_metric_measures(
            [self.metric, metric2],
            from_timestamp=datetime64(2014, 1, 1, 12, 0, 0),
            to_timestamp=datetime64(2014, 1, 1, 12, 0, 1),
            granularity=numpy.timedelta64(5, 'm'))

        self.assertEqual([
            (utils.datetime_utc(2014, 1, 1, 12, 0, 0),
             numpy.timedelta64(5, 'm'), 39.0),
        ], values)

    def test_add_and_get_cross_metric_measures_with_holes(self):
        metric2, __ = self._create_metric()
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 1), 69),
            storage.Measure(datetime64(2014, 1, 1, 12, 7, 31), 42),
            storage.Measure(datetime64(2014, 1, 1, 12, 5, 31), 8),
            storage.Measure(datetime64(2014, 1, 1, 12, 9, 31), 4),
            storage.Measure(datetime64(2014, 1, 1, 12, 12, 45), 42),
        ])
        self.incoming.add_measures(metric2, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 5), 9),
            storage.Measure(datetime64(2014, 1, 1, 12, 7, 31), 2),
            storage.Measure(datetime64(2014, 1, 1, 12, 9, 31), 6),
            storage.Measure(datetime64(2014, 1, 1, 12, 13, 10), 2),
        ])
        self.trigger_processing([str(self.metric.id), str(metric2.id)])

        values = self.storage.get_cross_metric_measures([self.metric, metric2])
        self.assertEqual([
            (utils.datetime_utc(2014, 1, 1, 0, 0, 0),
             numpy.timedelta64(1, 'D'), 18.875),
            (utils.datetime_utc(2014, 1, 1, 12, 0, 0),
             numpy.timedelta64(1, 'h'), 18.875),
            (utils.datetime_utc(2014, 1, 1, 12, 0, 0),
             numpy.timedelta64(5, 'm'), 39.0),
            (utils.datetime_utc(2014, 1, 1, 12, 5, 0),
             numpy.timedelta64(5, 'm'), 11.0),
            (utils.datetime_utc(2014, 1, 1, 12, 10, 0),
             numpy.timedelta64(5, 'm'), 22.0)
        ], values)

    def test_search_value(self):
        metric2, __ = self._create_metric()
        self.incoming.add_measures(self.metric, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 1,), 69),
            storage.Measure(datetime64(2014, 1, 1, 12, 7, 31), 42),
            storage.Measure(datetime64(2014, 1, 1, 12, 5, 31), 8),
            storage.Measure(datetime64(2014, 1, 1, 12, 9, 31), 4),
            storage.Measure(datetime64(2014, 1, 1, 12, 12, 45), 42),
        ])

        self.incoming.add_measures(metric2, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 5), 9),
            storage.Measure(datetime64(2014, 1, 1, 12, 7, 31), 2),
            storage.Measure(datetime64(2014, 1, 1, 12, 9, 31), 6),
            storage.Measure(datetime64(2014, 1, 1, 12, 13, 10), 2),
        ])
        self.trigger_processing([str(self.metric.id), str(metric2.id)])

        self.assertEqual(
            {metric2: [],
             self.metric: [
                 (datetime64(2014, 1, 1),
                  numpy.timedelta64(1, 'D'), 33),
                 (datetime64(2014, 1, 1, 12),
                  numpy.timedelta64(1, 'h'), 33),
                 (datetime64(2014, 1, 1, 12),
                  numpy.timedelta64(5, 'm'), 69),
                 (datetime64(2014, 1, 1, 12, 10),
                  numpy.timedelta64(5, 'm'), 42)]},
            self.storage.search_value(
                [metric2, self.metric],
                {u"≥": 30}))

        self.assertEqual(
            {metric2: [], self.metric: []},
            self.storage.search_value(
                [metric2, self.metric],
                {u"∧": [
                    {u"eq": 100},
                    {u"≠": 50}]}))

    def test_resize_policy(self):
        name = str(uuid.uuid4())
        ap = archive_policy.ArchivePolicy(name, 0, [(3, 5)])
        self.index.create_archive_policy(ap)
        m = self.index.create_metric(uuid.uuid4(), str(uuid.uuid4()), name)
        m = self.index.list_metrics(ids=[m.id])[0]
        self.incoming.add_measures(m, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 0), 1),
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 5), 1),
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 10), 1),
        ])
        self.trigger_processing([str(m.id)])
        self.assertEqual([
            (datetime64(2014, 1, 1, 12, 0, 0), numpy.timedelta64(5, 's'), 1),
            (datetime64(2014, 1, 1, 12, 0, 5), numpy.timedelta64(5, 's'), 1),
            (datetime64(2014, 1, 1, 12, 0, 10), numpy.timedelta64(5, 's'), 1),
        ], self.storage.get_measures(m))
        # expand to more points
        self.index.update_archive_policy(
            name, [archive_policy.ArchivePolicyItem(granularity=5, points=6)])
        m = self.index.list_metrics(ids=[m.id])[0]
        self.incoming.add_measures(m, [
            storage.Measure(datetime64(2014, 1, 1, 12, 0, 15), 1),
        ])
        self.trigger_processing([str(m.id)])
        self.assertEqual([
            (datetime64(2014, 1, 1, 12, 0, 0), numpy.timedelta64(5, 's'), 1),
            (datetime64(2014, 1, 1, 12, 0, 5), numpy.timedelta64(5, 's'), 1),
            (datetime64(2014, 1, 1, 12, 0, 10), numpy.timedelta64(5, 's'), 1),
            (datetime64(2014, 1, 1, 12, 0, 15), numpy.timedelta64(5, 's'), 1),
        ], self.storage.get_measures(m))
        # shrink timespan
        self.index.update_archive_policy(
            name, [archive_policy.ArchivePolicyItem(granularity=5, points=2)])
        m = self.index.list_metrics(ids=[m.id])[0]
        self.assertEqual([
            (datetime64(2014, 1, 1, 12, 0, 10), numpy.timedelta64(5, 's'), 1),
            (datetime64(2014, 1, 1, 12, 0, 15), numpy.timedelta64(5, 's'), 1),
        ], self.storage.get_measures(m))

    def test_resample_no_metric(self):
        """https://github.com/gnocchixyz/gnocchi/issues/69"""
        self.assertEqual([],
                         self.storage.get_measures(
                             self.metric,
                             datetime64(2014, 1, 1),
                             datetime64(2015, 1, 1),
                             granularity=numpy.timedelta64(300, 's'),
                             resample=numpy.timedelta64(1, 'h')))


class TestMeasureQuery(tests_base.TestCase):
    def test_equal(self):
        q = storage.MeasureQuery({"=": 4})
        self.assertTrue(q(4))
        self.assertFalse(q(40))

    def test_gt(self):
        q = storage.MeasureQuery({">": 4})
        self.assertTrue(q(40))
        self.assertFalse(q(4))

    def test_and(self):
        q = storage.MeasureQuery({"and": [{">": 4}, {"<": 10}]})
        self.assertTrue(q(5))
        self.assertFalse(q(40))
        self.assertFalse(q(1))

    def test_or(self):
        q = storage.MeasureQuery({"or": [{"=": 4}, {"=": 10}]})
        self.assertTrue(q(4))
        self.assertTrue(q(10))
        self.assertFalse(q(-1))

    def test_modulo(self):
        q = storage.MeasureQuery({"=": [{"%": 5}, 0]})
        self.assertTrue(q(5))
        self.assertTrue(q(10))
        self.assertFalse(q(-1))
        self.assertFalse(q(6))

    def test_math(self):
        q = storage.MeasureQuery(
            {
                u"and": [
                    # v+5 is bigger 0
                    {u"≥": [{u"+": 5}, 0]},
                    # v-6 is not 5
                    {u"≠": [5, {u"-": 6}]},
                ],
            }
        )
        self.assertTrue(q(5))
        self.assertTrue(q(10))
        self.assertFalse(q(11))

    def test_empty(self):
        q = storage.MeasureQuery({})
        self.assertFalse(q(5))
        self.assertFalse(q(10))

    def test_bad_format(self):
        self.assertRaises(storage.InvalidQuery,
                          storage.MeasureQuery,
                          {"foo": [{"=": 4}, {"=": 10}]})

        self.assertRaises(storage.InvalidQuery,
                          storage.MeasureQuery,
                          {"=": [1, 2, 3]})
