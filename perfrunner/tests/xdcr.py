import math

import requests
from logger import logger

from perfrunner.helpers.cbmonitor import with_stats
from perfrunner.settings import TargetSettings
from perfrunner.tests import target_hash, TargetIterator
from perfrunner.tests import PerfTest


class XdcrTest(PerfTest):

    def _start_replication(self, m1, m2):
        name = target_hash(m1, m2)
        self.rest.add_remote_cluster(m1, m2, name)

        for bucket in self.test_config.get_buckets():
            self.rest.start_replication(m1, bucket, bucket, name)

    def init_xdcr(self):
        xdcr_settings = self.test_config.get_xdcr_settings()

        c1, c2 = self.cluster_spec.get_clusters()
        m1, m2 = c1[0], c2[0]

        if xdcr_settings.replication_type == 'unidir':
            self._start_replication(m1, m2)
        if xdcr_settings.replication_type == 'bidir':
            self._start_replication(m1, m2)
            self._start_replication(m2, m1)

        for target in self.target_iterator:
            self.monitor.monitor_xdcr_replication(target)

    @with_stats(xdcr_lag=True)
    def run_access_phase(self):
        super(XdcrTest, self).run_access_phase()

    @staticmethod
    def _calc_percentile(data, percentile):
        data.sort()

        k = (len(data) - 1) * percentile
        f = math.floor(k)
        c = math.ceil(k)

        if f == c:
            return data[int(k)]
        else:
            return data[int(f)] * (c - k) + data[int(c)] * (k - f)

    def _calc_xdcr_lag(self):
        metric = '{0}_95th_xdc_lag_left_{1}'.format(
            self.test_config.name, self.cluster_spec.name)
        descr = '95th percentile XDCR lag, {0}'.format(
            self.test_config.get_test_descr())
        metric_info = {
            'title': descr,
            'cluster': self.cluster_spec.name,
            'larger_is_better': 'false'
        }
        timings = []
        for bucket in self.test_config.get_buckets():
            url = self.cbagent.lag_query_api.format(bucket)
            r = requests.get(url=url).json()
            timings += [value['xdcr_lag'] for value in r.values()]
        self._calc_percentile(timings, 0.95)
        return self._calc_percentile(timings, 0.95), metric, metric_info

    def _get_aggregated_metric(self, params):
        value = 0
        for bucket in self.test_config.get_buckets():
            url = self.cbagent.ns_query_api.format(bucket)
            r = requests.get(url=url, params=params).json()
            value += r.values()[0][0]
        return value

    def _calc_max_replication_changes_left(self):
        metric = '{0}_max_replication_changes_left_{1}'.format(
            self.test_config.name, self.cluster_spec.name)
        descr = 'Peak replication changes left, {0}'.format(
            self.test_config.get_test_descr())
        metric_info = {
            'title': descr,
            'cluster': self.cluster_spec.name,
            'larger_is_better': 'false'
        }
        params = {'group': 86400000,
                  'ptr': '/replication_changes_left', 'reducer': 'max'}
        return self._get_aggregated_metric(params), metric, metric_info

    def _calc_avg_xdc_ops(self):
        metric = '{0}_avg_xdc_ops_{1}'.format(
            self.test_config.name, self.cluster_spec.name)
        descr = 'XDC ops/sec, {0}'.format(self.test_config.get_test_descr())
        metric_info = {
            'title': descr,
            'cluster': self.cluster_spec.name,
            'larger_is_better': 'true'
        }
        params = {'group': 86400000, 'ptr': '/xdc_ops', 'reducer': 'avg'}
        return self._get_aggregated_metric(params), metric, metric_info

    def run(self):
        self.run_load_phase()
        self.compact_bucket()

        self.init_xdcr()

        self.run_access_phase()
        self.reporter.post_to_sf(*self._calc_max_replication_changes_left())
        self.reporter.post_to_sf(*self._calc_avg_xdc_ops())
        self.reporter.post_to_sf(*self._calc_xdcr_lag())


class SrcTargetIterator(TargetIterator):

    def __iter__(self):
        username, password = self.cluster_spec.get_rest_credentials()
        src_cluster = self.cluster_spec.get_clusters()[0]
        src_master = src_cluster[0]
        for bucket in self.test_config.get_buckets():
                prefix = target_hash(src_master, bucket)
                yield TargetSettings(src_master, bucket, username, password,
                                     prefix)


class XdcrInitTest(XdcrTest):

    def run_load_phase(self):
        load_settings = self.test_config.get_load_settings()
        logger.info('Running load phase: {0}'.format(load_settings))
        src_target_iterator = SrcTargetIterator(self.cluster_spec,
                                                self.test_config)
        self._run_workload(load_settings, src_target_iterator)

    def _calc_avg_replication_rate(self, time_elapsed):
        initial_items = self.test_config.get_load_settings().ops
        buckets = self.test_config.get_num_buckets()
        return round(buckets * initial_items / (time_elapsed * 60))

    def run(self):
        self.run_load_phase()
        self.compact_bucket()

        self.reporter.start()
        self.init_xdcr()
        time_elapsed = self.reporter.finish('Initial replication')
        self.reporter.post_to_sf(self._calc_avg_replication_rate(time_elapsed))
