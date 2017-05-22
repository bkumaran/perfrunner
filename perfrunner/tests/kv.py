import random
from threading import Thread
from time import sleep

from logger import logger
from mc_bin_client.mc_bin_client import MemcachedClient, MemcachedError

from perfrunner.helpers.cbmonitor import with_stats
from perfrunner.helpers.worker import (
    pillowfight_data_load_task,
    pillowfight_task,
)
from perfrunner.tests import PerfTest
from perfrunner.workloads.pathoGen import PathoGen
from perfrunner.workloads.revAB.__main__ import produce_ab
from perfrunner.workloads.revAB.graph import PersonIterator, generate_graph
from perfrunner.workloads.tcmalloc import WorkloadGen


class KVTest(PerfTest):

    """
    The most basic KV workflow:
        Initial data load ->
            Persistence and intra-cluster replication (for consistency) ->
                Data compaction (for consistency) ->
                    "Hot" load or working set warm up ->
                        "access" phase or active workload
    """

    @with_stats
    def access(self, *args):
        super().sleep()

    def run(self):
        self.load()
        self.wait_for_persistence()
        self.compact_bucket()

        self.hot_load()

        self.workload = self.test_config.access_settings
        self.access_bg()
        self.access()

        self.report_kpi()


class MixedLatencyTest(KVTest):

    """
    Enables reporting of GET and SET latency.
    """

    COLLECTORS = {'latency': True}

    def _report_kpi(self):
        for operation in ('get', 'set'):
            self.reporter.post(
                *self.metrics.calc_kv_latency(operation=operation,
                                              percentile=99)
            )

    def compact_bucket(self):
        pass


class DurabilityTest(KVTest):

    """
    Enables reporting of persistTo=1 and replicateTo=1 latency.
    """

    COLLECTORS = {'durability': True}

    def _report_kpi(self):
        for operation in ('replicate_to', 'persist_to'):
            self.reporter.post(
                *self.metrics.calc_kv_latency(operation=operation,
                                              percentile=99,
                                              dbname='durability')
            )


class ReadLatencyTest(MixedLatencyTest):

    """
    Enables reporting of GET latency.
    """

    COLLECTORS = {'latency': True}

    def _report_kpi(self):
        self.reporter.post(
            *self.metrics.calc_kv_latency(operation='get',
                                          percentile=99)
        )


class SubDocTest(MixedLatencyTest):

    """
    Enables reporting of SubDoc latency.
    """

    COLLECTORS = {'subdoc_latency': True}


class BgFetcherTest(KVTest):

    """
    Enables reporting of average BgFetcher wait time (disk fetches).
    """

    COLLECTORS = {'latency': True}

    ALL_BUCKETS = True

    def _report_kpi(self):
        self.reporter.post(
            self.metrics.calc_avg_bg_wait_time()
        )


class DrainTest(KVTest):

    """
    Enables reporting of average disk write queue size.
    """

    ALL_BUCKETS = True

    def _report_kpi(self):
        self.reporter.post(
            self.metrics.calc_avg_disk_write_queue()
        )


class InitialLoadTest(DrainTest):

    @with_stats
    def load(self, *args, **kwargs):
        super().load(*args, **kwargs)

    def run(self):
        self.load()

        self.report_kpi()


class FlusherTest(KVTest):

    """
    The maximum drain rate benchmark. Data set is loaded while persistence is
    disabled. After that persistence is enabled and ep-engine commits data with
    maximum possible speed (using very large batches).

    Please notice that measured drain rate has nothing to do with typical
    production characteristics. It only used as baseline / reference.
    """

    def mc_iterator(self):
        password = self.test_config.bucket.password
        for host_port in self.cluster_spec.yield_servers():
            host = host_port.split(':')[0]
            memcached_port = self.rest.get_memcached_port(host_port)
            for bucket in self.test_config.buckets:
                mc = MemcachedClient(host=host, port=memcached_port)
                try:
                    mc.sasl_auth_plain(user=bucket, password=password)
                    yield mc
                except MemcachedError:
                    logger.warn('Auth failure')

    def stop_persistence(self):
        for mc in self.mc_iterator():
            mc.stop_persistence()

    def start_persistence(self):
        for mc in self.mc_iterator():
            mc.start_persistence()

    @with_stats
    def drain(self):
        for master in self.cluster_spec.yield_masters():
            for bucket in self.test_config.buckets:
                self.monitor.monitor_disk_queue(master, bucket)

    def _report_kpi(self, time_elapsed):
        self.reporter.post(
            self.metrics.calc_max_drain_rate(time_elapsed)
        )

    def run(self):
        self.stop_persistence()
        self.load()

        self.access_bg()
        self.start_persistence()
        from_ts, to_ts = self.drain()
        time_elapsed = (to_ts - from_ts) / 1000.0

        self.reporter.finish('Drain', time_elapsed)
        self.report_kpi(time_elapsed)


class BeamRssTest(KVTest):

    """
    Enables reporting of Erlang (beam.smp process) memory usage.
    """

    def _report_kpi(self):
        self.reporter.post(
            *self.metrics.calc_max_beam_rss()
        )


class WarmupTest(PerfTest):

    """
    After typical workload we restart all nodes and measure the time it takes
    to perform cluster warm up (internal technology, not to be confused with
    "hot" load phase).
    """

    def access(self, *args, **kwargs):
        super().sleep()

    @with_stats
    def warmup(self):
        self.remote.stop_server()
        self.remote.drop_caches()

        self.reporter.start()
        self._warmup()
        self.time_elapsed = self.reporter.finish('Warmup')

    def _warmup(self):
        self.remote.start_server()
        for master in self.cluster_spec.yield_masters():
            for bucket in self.test_config.buckets:
                self.monitor.monitor_warmup(self.memcached, master, bucket)

    def _report_kpi(self):
        self.reporter.post(self.time_elapsed)

    def run(self):
        self.load()
        self.wait_for_persistence()

        self.access_bg()
        self.access()
        self.wait_for_persistence()

        self.warmup()

        self.report_kpi()


class FragmentationTest(PerfTest):

    """
    This test implements the append-only scenario:
    1. Single node.
    2. Load X items, 700-1400 bytes, average 1KB (11-22 fields).
    3. Append data
        3.1. Mark first 80% of items as working set.
        3.2. Randomly update 75% of items in working set by adding 1 field at a time (62 bytes).
        3.3. Mark first 40% of items as working set.
        3.4. Randomly update 75% of items in working set by adding 1 field at a time (62 bytes).
        3.5. Mark first 20% of items as working set.
        3.6. Randomly update 75% of items in working set by adding 1 field at a time (62 bytes).
    4. Repeat step #3 5 times.

    See workloads/tcmalloc.py for details.

    Scenario described above allows to spot issues with memory/allocator
    fragmentation.
    """

    @with_stats
    def load_and_append(self):
        password = self.test_config.bucket.password
        WorkloadGen(self.test_config.load_settings.items,
                    self.master_node, self.test_config.buckets[0],
                    password).run()

    def calc_fragmentation_ratio(self):
        ratios = []
        for target in self.target_iterator:
            host = target.node.split(':')[0]
            port = self.rest.get_memcached_port(target.node)
            stats = self.memcached.get_stats(host, port, target.bucket,
                                             stats='memory')
            ratio = int(stats[b'mem_used']) / int(stats[b'total_heap_bytes'])
            ratios.append(ratio)
        ratio = 100 * (1 - sum(ratios) / len(ratios))
        ratio = round(ratio, 1)
        logger.info('Fragmentation: {}'.format(ratio))
        return ratio

    def _report_kpi(self):
        self.reporter.post(
            self.calc_fragmentation_ratio()
        )

    def run(self):
        self.load_and_append()

        self.report_kpi()


class FragmentationLargeTest(FragmentationTest):

    """
    The same as base test but large documents are used.
    """

    @with_stats
    def load_and_append(self):
        password = self.test_config.bucket.password
        WorkloadGen(self.test_config.load_settings.items,
                    self.master_node, self.test_config.buckets[0], password,
                    small=False).run()


class RevABTest(FragmentationTest):

    def generate_graph(self):
        random.seed(0)
        self.graph = generate_graph(self.test_config.load_settings.items)
        self.graph_keys = self.graph.nodes()
        random.shuffle(self.graph_keys)

    @with_stats
    def load(self, *args):
        for target in self.target_iterator:
            host, port = target.node.split(':')
            conn = {'host': host, 'port': port,
                    'bucket': target.bucket, 'password': target.password}

            threads = list()
            for seqid in range(self.test_config.load_settings.workers):
                iterator = PersonIterator(
                    self.graph,
                    self.graph_keys,
                    seqid,
                    self.test_config.load_settings.workers,
                )
                t = Thread(
                    target=produce_ab,
                    args=(iterator,
                          self.test_config.load_settings.iterations,
                          conn),
                )
                threads.append(t)
                t.start()
            for t in threads:
                t.join()

    def _report_kpi(self):
        fragmentation_ratio = self.calc_fragmentation_ratio()
        self.reporter.post(fragmentation_ratio)
        self.reporter.post(
            *self.metrics.calc_max_memcached_rss()
        )

    def run(self):
        self.generate_graph()
        self.load()

        self.report_kpi()


class MemUsedTest(KVTest):

    ALL_BUCKETS = True

    def _report_kpi(self):
        for metric in ('max', 'min'):
            self.reporter.post(
                *self.metrics.calc_mem_used(metric)
            )
            self.reporter.post(
                *self.metrics.calc_mem_used(metric)
            )


class PathoGenTest(FragmentationTest):

    """Pathologically bad malloc test. See pathoGen.py for full details."""

    @with_stats
    def access(self, *args):
        for target in self.target_iterator:
            host, port = target.node.split(':')
            pg = PathoGen(num_items=self.test_config.load_settings.items,
                          num_workers=self.test_config.load_settings.workers,
                          num_iterations=self.test_config.load_settings.iterations,
                          frozen_mode=False,
                          host=host, port=port,
                          bucket=target.bucket, password=target.password)
            pg.run()

    def _report_kpi(self):
        self.reporter.post(
            *self.metrics.calc_avg_memcached_rss()
        )
        self.reporter.post(
            *self.metrics.calc_max_memcached_rss()
        )

    def run(self):
        self.access()

        self.report_kpi()


class PathoGenFrozenTest(PathoGenTest):

    """Pathologically bad mmalloc test, Frozen mode. See pathoGen.py for full
    details."""

    @with_stats
    def access(self):
        for target in self.target_iterator:
            host, port = target.node.split(':')
            pg = PathoGen(num_items=self.test_config.load_settings.items,
                          num_workers=self.test_config.load_settings.workers,
                          num_iterations=self.test_config.load_settings.iterations,
                          frozen_mode=True,
                          host=host, port=port,
                          bucket=target.bucket, password=target.password)
            pg.run()


class ThroughputTest(KVTest):

    COLLECTORS = {'latency': True}

    def compact_bucket(self):
        pass

    def _measure_curr_ops(self):
        ops = 0
        for bucket in self.test_config.buckets:
            for server in self.cluster_spec.yield_servers():
                host = server.split(':')[0]
                port = self.rest.get_memcached_port(server)

                stats = self.memcached.get_stats(host, port, bucket, stats='')
                ops += int(stats[b'cmd_total_ops'])
        return ops

    @with_stats
    def access(self):
        curr_ops = self._measure_curr_ops()

        super().sleep()

        self.total_ops = self._measure_curr_ops() - curr_ops

    def _report_kpi(self):
        throughput = self.total_ops // self.test_config.access_settings.time

        self.reporter.post(throughput)


class PillowFightTest(PerfTest):

    """Uses cbc-pillowfight from libcouchbase to drive cluster."""

    ALL_BUCKETS = True

    def load(self, *args):
        PerfTest.load(self, task=pillowfight_data_load_task)

    @with_stats
    def access(self, *args):
        self.download_certificate()

        PerfTest.access(self, task=pillowfight_task)

    def _report_kpi(self):
        self.reporter.post(
            self.metrics.calc_max_ops()
        )

    def run(self):
        self.load()
        self.wait_for_persistence()

        self.access()

        self.report_kpi()


class CompactionTest(KVTest):

    @with_stats
    def compact(self):
        self.compact_bucket()

    def _report_kpi(self, time_elapsed):
        self.reporter.post(time_elapsed)

    def run(self):
        self.load()
        self.wait_for_persistence()

        self.hot_load()

        self.access_bg()

        from_ts, to_ts = self.compact()
        time_elapsed = (to_ts - from_ts) / 1000
        time_elapsed = self.reporter.finish('Full compaction', time_elapsed)

        self.report_kpi(time_elapsed)


class MemoryOverheadTest(PillowFightTest):

    COLLECTORS = {'iostat': False, 'net': False}

    PF_KEY_SIZE = 20

    def _report_kpi(self):
        self.reporter.post(
            self.metrics.calc_memory_overhead(key_size=self.PF_KEY_SIZE)
        )

    @with_stats
    def access(self, *args):
        sleep(self.test_config.access_settings.time)
