from __future__ import division
import logging
try:
    from Queue import Empty
except:
    from queue import Empty
from time import time, sleep
from threading import Thread
from collections import defaultdict
# @modified 20190522 - Task #3034: Reduce multiprocessing Manager list usage
# Use Redis sets in place of Manager().list to reduce memory and number of
# processes
# from multiprocessing import Process, Manager, Queue
from multiprocessing import Process, Queue
from msgpack import Unpacker
import os
from os import kill, getpid
import traceback
import re
from sys import version_info
import os.path
from ast import literal_eval

import settings
from skyline_functions import (
    write_data_to_file, send_anomalous_metric_to, mkdir_p,
    filesafe_metricname,
    # @added 20170602 - Feature #2034: analyse_derivatives
    nonNegativeDerivative, strictly_increasing_monotonicity, in_list,
    # @added 20191030 - Bug #3266: py3 Redis binary objects not strings
    #                   Branch #3262: py3
    # Added a single functions to deal with Redis connection and the
    # charset='utf-8', decode_responses=True arguments required in py3
    get_redis_conn, get_redis_conn_decoded,
    # @added 20200506 - Feature #3532: Sort all time series
    sort_timeseries)

# @added 20200425 - Feature #3512: matched_or_regexed_in_list function
#                   Feature #3508: ionosphere_untrainable_metrics
#                   Feature #3486: analyzer_batch
from matched_or_regexed_in_list import matched_or_regexed_in_list

# @modified 20200423 - Feature #3504: Handle airgaps in batch metrics
#                      Feature #3480: batch_processing
#                      Feature #3486: analyzer_batch
# Changed to algoritms_batch so there is no pollution and
# analyzer and analyzer_batch are totally independent
# from algorithms import run_selected_algorithm
from algorithms_batch import run_selected_batch_algorithm

from algorithm_exceptions import TooShort, Stale, Boring

# TODO if settings.ENABLE_CRUCIBLE: and ENABLE_PANORAMA
#    from spectrum import push_to_crucible

skyline_app = 'analyzer_batch'
skyline_app_logger = '%sLog' % skyline_app
logger = logging.getLogger(skyline_app_logger)
skyline_app_logfile = '%s/%s.log' % (settings.LOG_PATH, skyline_app)
skyline_app_loglock = '%s.lock' % skyline_app_logfile
skyline_app_logwait = '%s.wait' % skyline_app_logfile

python_version = int(version_info[0])

this_host = str(os.uname()[1])

try:
    SERVER_METRIC_PATH = '.%s' % settings.SERVER_METRICS_NAME
    if SERVER_METRIC_PATH == '.':
        SERVER_METRIC_PATH = ''
except:
    SERVER_METRIC_PATH = ''

# @added 20190410 - Feature #2916: ANALYZER_ENABLED setting
try:
    ANALYZER_ENABLED = settings.ANALYZER_ENABLED
    logger.info('ANALYZER_ENABLED is set to %s' % str(ANALYZER_ENABLED))
except:
    ANALYZER_ENABLED = True
    logger.info('warning :: ANALYZER_ENABLED is not declared in settings.py, defaults to True')

try:
    from settings import BATCH_PROCESSING
except:
    BATCH_PROCESSING = None

# @added 20200425 - Feature #3508: ionosphere.untrainable_metrics
# Determine if any metrcs have negatives values some they can be
# added to the ionosphere.untrainable_metrics Redis set
try:
    from settings import KNOWN_NEGATIVE_METRICS
except:
    KNOWN_NEGATIVE_METRICS = []


skyline_app_graphite_namespace = 'skyline.%s%s' % (skyline_app, SERVER_METRIC_PATH)

LOCAL_DEBUG = False


class AnalyzerBatch(Thread):
    """
    The AnalyzerBatch class which controls the analyzer.batch thread and spawned
    processes.

    Made with love to the analyzer_batch playlist:
    https://soundcloud.com/earthgecko/sets/analyzer_batch
    https://soundcloud.com/thedeltariggs/ode-to-jeremiah (I can't tell what I've seen..)
    https://soundcloud.com/egroove/premiere-francesco-chiocci-feat-black-soda-musumeci-remix-connaisseur-recordings (picking up pieces of my weary mind)
    https://soundcloud.com/when-we-dip/premiere-francesco-chiocci-ft-black-soda-black-sunrise-peter-pardeike-remix
    https://soundcloud.com/timgreen/atelier-francesco-manuel-feat-astrid-dead-end-tim-green-remixcityfox-1
    https://soundcloud.com/imbernonmusic/edu-imbernon-fixing-fires
    https://soundcloud.com/deep-house-amsterdam/oliver-koletzki-deep-house-amsterdam-dgtl-podcast-007
    https://soundcloud.com/crosstownrebels/crm140-damian-lazarus-the-ancient-moons-vermillion-agoria-remix-1
    https://soundcloud.com/wiewouwat/joy-wellboy-before-the-sunrise
    https://soundcloud.com/agoria/damian-lazarus-the-ancent-moons-vermillion-agoria-remix
    https://soundcloud.com/wearesoundspace/premiere-just-her-feat-kieran-fowkes-let-myself-go
    https://soundcloud.com/watergaterecords/matthias-meyer-november-rain
    https://soundcloud.com/musicthatmakesmewannasurf/mixtape-2-w-kosson
    """

    def __init__(self, parent_pid):
        """
        Initialize the AnalyzerBatch

        Create the :obj:`self.batch_exceptions_q` queue
        Create the :obj:`self.batch_anomaly_breakdown_q` queue

        """
        super(AnalyzerBatch, self).__init__()
        self.redis_conn = get_redis_conn(skyline_app)
        self.redis_conn_decoded = get_redis_conn_decoded(skyline_app)
        self.daemon = True
        self.parent_pid = parent_pid
        self.current_pid = getpid()
        self.batch_exceptions_q = Queue()
        self.batch_anomaly_breakdown_q = Queue()

    def check_if_parent_is_alive(self):
        """
        Self explanatory
        """
        try:
            kill(self.current_pid, 0)
            kill(self.parent_pid, 0)
        except:
            exit(0)

    def spin_batch_process(self, i, run_timestamp, metric_name, last_analyzed_timestamp):
        """
        Assign a metric and last_analyzed_timestamp for a process to analyze.

        :param i: python process id
        :param run_timestamp: the epoch timestamp at which this process was called
        :param metric_name: the FULL_NAMESPACE metric name as keyed in Redis
        :param last_analyzed_timestamp: the last analysed timestamp as recorded
            in the Redis key last_timestamp.basename key.

        :return: returns True

        """

        spin_start = time()
        child_batch_process_pid = os.getpid()
        logger.info('child_batch_process_pid - %s, processing %s from %s' % (
            str(child_batch_process_pid), metric_name, str(last_analyzed_timestamp)))

        # Identify last timestamp
        metric_timestamp = None
        # Identify anomalies
        # Handle EXPIRATION_TIME
        # Ship to Analyzer, Mirage or Ionosphere

        raw_series = None
        try:
            raw_series = self.redis_conn.get(metric_name)
        except:
            logger.info(traceback.format_exc())
            logger.error('error :: failed to get %s from Redis' % metric_name)
            raw_series = None

        # Make process-specific dicts
        exceptions = defaultdict(int)
        anomaly_breakdown = defaultdict(int)

        if not raw_series:
            logger.info('No raw_series defined, returning')
            return

        # Determine the unique Mirage and Ionosphere metrics once, which are
        # used later to determine how Analyzer should handle/route anomalies
        try:
            mirage_unique_metrics = list(self.redis_conn_decoded.smembers('mirage.unique_metrics'))
        except:
            mirage_unique_metrics = []
        try:
            ionosphere_unique_metrics = list(self.redis_conn_decoded.smembers('ionosphere.unique_metrics'))
        except:
            ionosphere_unique_metrics = []

        # In order to convert monotonic, incrementing metrics to a deriative
        # metric
        try:
            derivative_metrics = list(self.redis_conn_decoded.smembers('derivative_metrics'))
        except:
            derivative_metrics = []
        try:
            non_derivative_metrics = list(self.redis_conn_decoded.smembers('non_derivative_metrics'))
        except:
            non_derivative_metrics = []
        try:
            non_derivative_monotonic_metrics = settings.NON_DERIVATIVE_MONOTONIC_METRICS
        except:
            non_derivative_monotonic_metrics = []
        non_smtp_alerter_metrics = []
        try:
            non_smtp_alerter_metrics = list(self.redis_conn_decoded.smembers('analyzer.non_smtp_alerter_metrics'))
        except:
            non_smtp_alerter_metrics = []

        try:
            unpacker = Unpacker(use_list=False)
            unpacker.feed(raw_series)
            timeseries = list(unpacker)
        except:
            timeseries = []

        # @added 20200506 - Feature #3532: Sort all time series
        # To ensure that there are no unordered timestamps in the time
        # series which are artefacts of the collector or carbon-relay, sort
        # all time series by timestamp before analysis.
        original_timeseries = timeseries
        if original_timeseries:
            timeseries = sort_timeseries(original_timeseries)
            del original_timeseries

        try:
            del raw_series
        except:
            pass

        timestamps_to_analyse = []
        # Reverse the time series so that only the first (last) items now to be
        # iterated and break after the necessary iterations so the entire
        # time series is not iterated over.
        reversed_timeseries = list(reversed(timeseries))
        for timestamp, value in reversed_timeseries:
            if int(timestamp) > last_analyzed_timestamp:
                timestamps_to_analyse.append(int(timestamp))
            else:
                break
        del reversed_timeseries
        timestamps_to_analyse = list(reversed(timestamps_to_analyse))

        # @added 20200413 - Feature #3486: analyzer_batch
        #                   Feature #3480: batch_processing
        # Handle there being no timestamps_to_analyse and report such as
        # otherwise the only info logged is that the work key just gets removed
        # 2020-04-14 12:57:25 :: 3222 :: there are 1 metrics to process in the analyzer.batch Redis set
        # 2020-04-14 12:57:25 :: 3222 :: processing - ['vista.demo_robustperception_io.prometheus.node_disk_read_time_seconds_total', 1586868000]
        # 2020-04-14 12:57:25 :: 3222 :: starting 1 of 1 spin_batch_process
        # 2020-04-14 12:57:25 :: 7852 :: batch :: child_batch_process_pid - 7852, processing vista.demo_robustperception_io.prometheus.node_disk_read_time_seconds_total from 1586868000
        # 2020-04-14 12:57:25 :: 7852 :: analyzer_batch :: removed work item - ['vista.demo_robustperception_io.prometheus.node_disk_read_time_seconds_total', 1586868000] - from Redis set - analyzer.batch
        # 2020-04-14 12:57:25 :: 7852 :: spin_batch_process took 0.04 seconds
        # 2020-04-14 12:57:25 :: 3222 :: 1 spin_batch_process completed in 0.10 seconds
        # 2020-04-14 12:57:25 :: 3222 :: exceptions - Stale: 9, Boring: 6, TooShort: 0, Other: 0
        # 2020-04-14 12:57:25 :: 3222 :: anomaly_breakdown - histogram_bins: 0, first_hour_average: 0, stddev_from_average: 0, grubbs: 0, ks_test: 0, mean_subtraction_cumulation: 0, median_absolute_deviation: 0, stddev_from_moving_average: 0, least_squares: 0
        number_of_timestamps_to_analyze = len(timestamps_to_analyse)
        if number_of_timestamps_to_analyze == 0:
            logger.info('no timestamps were found to analyze for %s from %s, nothing to do' % (
                metric_name, str(last_analyzed_timestamp)))

            # @added 20200424 - Feature #3486: analyzer_batch
            #                   Feature #3480: batch_processing
            #                   Feature #3504: Handle airgaps in batch metrics
            # If there are no data points to analyze remove from the set
            redis_set = 'analyzer.batch'
            data = [metric_name, int(last_analyzed_timestamp)]
            try:
                self.redis_conn.srem(redis_set, str(data))
                logger.info('analyzer_batch :: removed batch metric item - %s - from Redis set - %s' % (str(data), redis_set))
            except:
                logger.error(traceback.format_exc())
                logger.error('error :: analyzer_batch :: failed to remove batch metric item - %s - from Redis set - %s' % (str(data), redis_set))

            # Clean up and return
            try:
                del timeseries
            except:
                pass
            try:
                del timestamps_to_analyse
            except:
                pass
            try:
                del batch_timeseries
            except:
                pass
            try:
                del mirage_unique_metrics
            except:
                pass
            try:
                del ionosphere_unique_metrics
            except:
                pass
            try:
                del derivative_metrics
            except:
                pass
            try:
                del non_derivative_metrics
            except:
                pass
            try:
                del non_derivative_monotonic_metrics
            except:
                pass
            try:
                del non_smtp_alerter_metrics
            except:
                pass
            return
        else:
            last_redis_data_timestamp = timestamps_to_analyse[-1]
            logger.info('%s timestamps were found to analyze for %s from %s to %s' % (
                str(number_of_timestamps_to_analyze), metric_name,
                str(last_analyzed_timestamp), str(last_redis_data_timestamp)))

        base_name = metric_name.replace(settings.FULL_NAMESPACE, '', 1)

        # @added 20200425 - Feature #3508: ionosphere.untrainable_metrics
        # Determine if any metrcs have negatives values some they can be
        # added to the ionosphere_untrainable_metrics Redis set
        run_negatives_present = False
        if settings.IONOSPHERE_ENABLED:
            run_negatives_present = True
            try:
                known_negative_metric_matched_by = None
                known_negative_metric, known_negative_metric_matched_by = matched_or_regexed_in_list(skyline_app, base_name, KNOWN_NEGATIVE_METRICS)
                if known_negative_metric:
                    run_negatives_present = False
            except:
                run_negatives_present = True

        # @added 20170602 - Feature #2034: analyse_derivatives
        # In order to convert monotonic, incrementing metrics to a deriative
        # metric
        known_derivative_metric = False
        unknown_deriv_status = True
        if metric_name in non_derivative_metrics:
            unknown_deriv_status = False
        if unknown_deriv_status:
            if metric_name in derivative_metrics:
                known_derivative_metric = True
                unknown_deriv_status = False

        # First check if it has its own Redis z.derivative_metric key
        # that has not expired
        derivative_metric_key = 'z.derivative_metric.%s' % str(base_name)

        if unknown_deriv_status:
            # @added 20170617 - Bug #2050: analyse_derivatives - change in monotonicity
            last_derivative_metric_key = False
            try:
                last_derivative_metric_key = self.redis_conn.get(derivative_metric_key)
            except Exception as e:
                logger.error('error :: could not query Redis for last_derivative_metric_key: %s' % e)

            # Determine if it is a strictly increasing monotonically metric
            # or has been in last FULL_DURATION via its z.derivative_metric
            # key
            if not last_derivative_metric_key:
                is_strictly_increasing_monotonically = strictly_increasing_monotonicity(timeseries)
            else:
                # Until the z.derivative_metric key expires, it is classed
                # as such
                is_strictly_increasing_monotonically = True

            skip_derivative = in_list(base_name, non_derivative_monotonic_metrics)
            if skip_derivative:
                is_strictly_increasing_monotonically = False
            if is_strictly_increasing_monotonically:
                known_derivative_metric = True
                try:
                    self.redis_conn.sadd('derivative_metrics', metric_name)
                except:
                    logger.info(traceback.format_exc())
                    logger.error('error :: failed to add metric to Redis derivative_metrics set')
            else:
                try:
                    self.redis_conn.sadd('non_derivative_metrics', metric_name)
                except:
                    logger.info(traceback.format_exc())
                    logger.error('error :: failed to add metric to Redis non_derivative_metrics set')

        # Distill timeseries strings into lists
        for i, batch_timestamp in enumerate(timestamps_to_analyse):
            self.check_if_parent_is_alive()

            batch_timeseries = []
            for timestamp, value in timeseries:
                if int(timestamp) <= batch_timestamp:
                    batch_timeseries.append([timestamp, value])

            if known_derivative_metric:
                try:
                    derivative_timeseries = nonNegativeDerivative(batch_timeseries)
                    batch_timeseries = derivative_timeseries
                except:
                    logger.error('error :: nonNegativeDerivative failed')

            try:
                # Allow for testing.  If you want to test a metric and then stop
                # the metric sending data to carbon-relay (use a vista metric).
                # Determine a timestamp that will fall into the stopped period
                # Add the timestamp to a Redis key called
                # analyzer_batch.test.<metric_name>
                # Start the metric sending data again (re-enable in vista)
                # vista/flux will fill the missing data, when analyzer pushes
                # the metric to analyzer_batch to process, if analyzer_batch
                # is set to test_anomaly True and finds the key, if the
                # timestamp matches the timestamp in the key, analyzer_batch
                # will multiply the timestamp data point by 15, this should
                # trigger an anomaly.  Ensure you use a metric which will
                # trigger, a load related metric is usually adequate.
                test_anomaly = False
                test_anomaly_at = None
                test_anomaly_batch_timeseries = []
                if test_anomaly:
                    test_anomaly_at = None
                    test_anomaly_key = 'analyzer_batch.test.%s' % metric_name
                    try:
                        test_anomaly_at = self.redis_conn.get(test_anomaly_key)
                    except:
                        test_anomaly_at = None
                    if test_anomaly_at:
                        if int(test_anomaly_at) == int(batch_timeseries[-1][0]):
                            for timestamp, value in batch_timeseries:
                                if int(timestamp) == int(test_anomaly_at):
                                    anomaly_value = value * 10
                                    logger.info('test_anomaly - replacing value %s with anomaly_value of %s at %s in %s timeseries' % (
                                        str(value), str(anomaly_value),
                                        str(test_anomaly_at), metric_name))
                                    value = anomaly_value
                                test_anomaly_batch_timeseries.append([timestamp, value])
                            if test_anomaly_batch_timeseries:
                                batch_timeseries = test_anomaly_batch_timeseries
                                logger.info('test_anomaly - replaced %s timeseries with anomaly value in it' % (
                                    metric_name))
                                try:
                                    self.redis_conn.delete(test_anomaly_key)
                                    logger.info('test_anomaly - deleted test_anomaly Redis key - %s' % str(test_anomaly_key))
                                except:
                                    logger.info(traceback.format_exc())
                                    logger.error('error :: failed to delete test_anomaly Redis key - %s' % str(test_anomaly_key))

                # @modified 20200423 - Feature #3504: Handle airgaps in batch metrics
                #                      Feature #3480: batch_processing
                #                      Feature #3486: analyzer_batch
                # Changed to algoritms_batch so there is no pollution and
                # analyzer and analyzer_batch are totally independent
                # metric_airgaps = []
                # anomalous, ensemble, datapoint = run_selected_algorithm(batch_timeseries, metric_name, metric_airgaps)
                # @modified 20200425 - Feature #3508: ionosphere.untrainable_metrics
                # Added run_negatives_present and added negatives_found
                # anomalous, ensemble, datapoint = run_selected_batch_algorithm(batch_timeseries, metric_name)
                anomalous, ensemble, datapoint, negatives_found = run_selected_batch_algorithm(batch_timeseries, metric_name, run_negatives_present)

                if test_anomaly_batch_timeseries:
                    logger.info('test_anomaly - analyzed %s data with anomaly value in it and anomalous = %s' % (
                        metric_name, str(anomalous)))

                # Update the last_timestamp metric Redis key
                last_metric_timestamp_key = 'last_timestamp.%s' % base_name
                redis_key_set = None
                try:
                    int_metric_timestamp = int(batch_timestamp)
                    # @modified 20200503 - Feature #3504: Handle airgaps in batch metrics
                    #                      Feature #3480: batch_processing
                    #                      Feature #3486: analyzer_batch
                    # Set the last_timestamp expiry time to 1 month rather than
                    # settings.FULL_DURATION
                    self.redis_conn.setex(
                        last_metric_timestamp_key, 2592000, int_metric_timestamp)
                    redis_key_set = True
                except:
                    logger.error('error :: failed to set Redis key %s' % last_metric_timestamp_key)
                if anomalous:
                    if redis_key_set:
                        logger.info('anomalous :: anomaly detected on %s at %s with %s, set Redis key %s to %s' % (
                            base_name, str(int_metric_timestamp), str(datapoint),
                            last_metric_timestamp_key, str(int_metric_timestamp)))
                    else:
                        logger.info('anomalous :: anomaly detected on %s at %s with %s' % (
                            base_name, str(int_metric_timestamp),
                            str(datapoint)))

                    # @added 20200425 - Feature #3508: ionosphere.untrainable_metrics
                    # Determine if any metrcs have negatives values some they can be
                    # added to the ionosphere.untrainable_metrics Redis set
                    if run_negatives_present and negatives_found:
                        redis_set = 'ionosphere.untrainable_metrics'
                        try:
                            last_negative_timestamp = int(negatives_found[-1][0])
                            last_negative_value = negatives_found[-1][1]
                            remove_after_timestamp = int(last_negative_timestamp + settings.FULL_DURATION)
                            data = str([metric_name, batch_timestamp, datapoint, last_negative_timestamp, last_negative_value, settings.FULL_DURATION, remove_after_timestamp])
                            self.redis_conn.sadd(redis_set, data)
                        except:
                            logger.info(traceback.format_exc())
                            logger.error('error :: failed to add data to Redis set %s' % (
                                str(redis_set)))

                    # Added a Redis key for Mirage, Panorama and Ionosphere to
                    # query to identify if an anomaly has been added by
                    # analyzer_batch and set a longish TTL as if multiple
                    # anomalies for multiple metrics in a batch are sent to
                    # Ionosphere it could take Ionosphere a while to analyze
                    # them all.  This key circumvents the requirement of each
                    # app to determine if a metric is a batch metric, as this
                    # is only created for batch metric anomalies.
                    analyzer_batch_metric_anomaly_key = '%s.anomaly.%s.%s' % (
                        skyline_app, str(int_metric_timestamp), base_name)
                    try:
                        int_metric_timestamp = int(batch_timestamp)
                        self.redis_conn.setex(
                            analyzer_batch_metric_anomaly_key,
                            3600, int_metric_timestamp)
                        logger.info('set Redis key %s with %s for other apps to identify this as an analyzer_batch anomaly' % (
                            analyzer_batch_metric_anomaly_key,
                            str(int_metric_timestamp)))
                    except:
                        logger.error('error :: failed to set Redis key %s' % analyzer_batch_metric_anomaly_key)
                else:
                    if redis_key_set:
                        logger.info('not anomalous :: %s at %s with %s, set Redis key %s to %s' % (
                            base_name, str(int_metric_timestamp), str(datapoint),
                            last_metric_timestamp_key, str(int_metric_timestamp)))
                    else:
                        logger.info('not anomalous :: %s at %s with %s' % (
                            base_name, str(int_metric_timestamp),
                            str(datapoint)))

                # @added 20190408 - Feature #2882: Mirage - periodic_check
                # Add for Mirage periodic - is really anomalous add to
                # real_anomalous_metrics and if in mirage_periodic_check_metric_list
                # add as anomalous
                if anomalous:
                    metric_timestamp = batch_timeseries[-1][0]
                    metric = [datapoint, base_name, metric_timestamp]

                    # Get the anomaly breakdown - who returned True?
                    triggered_algorithms = []
                    for index, value in enumerate(ensemble):
                        if value:
                            algorithm = settings.ALGORITHMS[index]
                            anomaly_breakdown[algorithm] += 1
                            triggered_algorithms.append(algorithm)

                    # @added 20170206 - Bug #1904: Handle non filesystem friendly metric names in check files
                    sane_metricname = filesafe_metricname(str(base_name))

                    # If Panorama is enabled determine details
                    determine_anomaly_details = False
                    if settings.PANORAMA_ENABLED:
                        determine_anomaly_details = True

                    # If Ionosphere is enabled determine details
                    try:
                        ionosphere_enabled = settings.IONOSPHERE_ENABLED
                        if settings.IONOSPHERE_ENABLED:
                            determine_anomaly_details = True
                    except:
                        ionosphere_enabled = False

                    if determine_anomaly_details:
                        metric_timestamp = str(int(batch_timeseries[-1][0]))
                        from_timestamp = str(int(batch_timeseries[1][0]))
                        timeseries_dir = base_name.replace('.', '/')

                    send_back_to_analyzer = None

                    # @added 20161119 - Branch #922: ionosphere
                    #                   Task #1718: review.tsfresh
                    # Set defaults which can be used later to determine how
                    # Analyzer should handle/route anomalies
                    analyzer_metric = True
                    mirage_metric = False
                    ionosphere_metric = False
                    send_to_ionosphere = False

                    if metric_name in ionosphere_unique_metrics:
                        ionosphere_metric = True
                        send_to_ionosphere = True

                    if metric_name in mirage_unique_metrics:
                        analyzer_metric = False
                        ionosphere_metric = False
                        mirage_metric = True
                        send_to_ionosphere = False

                    # @added 20170108 - Feature #1830: Ionosphere alerts
                    # Only send smtp_alerter_metrics to Ionosphere
                    smtp_alert_enabled_metric = True
                    if base_name in non_smtp_alerter_metrics:
                        smtp_alert_enabled_metric = False

                    if ionosphere_enabled:
                        if analyzer_metric:
                            # We do not want send all anomalous metrics to
                            # Ionosphere if they are not being alerted on as
                            # they will be pointless they will have no alert if
                            # it is within the EXPIRATION_TIME and there will be
                            # no reference graphs from an alert for the user to
                            # action.
                            cache_key = 'last_alert.smtp.%s' % (base_name)
                            last_alert = False
                            try:
                                last_alert = self.redis_conn.get(cache_key)
                            except Exception as e:
                                logger.error('error :: could not query Redis for cache_key: %s' % e)

                            if not last_alert:
                                send_to_ionosphere = True
                            else:
                                send_to_ionosphere = False
                                if ionosphere_metric:
                                    logger.info('not sending to Ionosphere - alert key exists - %s' % (base_name))
                        else:
                            if mirage_metric:
                                logger.info('not sending to Ionosphere - Mirage metric - %s' % (base_name))
                                send_to_ionosphere = False

                                # analyzer_batch sends Analyzer and Mirage
                                # metrics back to analyzer
                                send_back_to_analyzer = True

                                # @added 20170306 - Feature #1960: ionosphere_layers
                                # Ionosphere layers require the timeseries at
                                # FULL_DURATION so if this is a Mirage and
                                # Ionosphere metric, Analyzer needs to provide
                                # the timeseries file for later (within 60
                                # seconds) analysis, however we want the data
                                # that triggered the anomaly, as before this was
                                # only created by Mirage if an alert was
                                # triggered, but Ionosphere layers now require
                                # this file before an alert is triggered
                                timeseries_dir = base_name.replace('.', '/')
                                training_dir = '%s/%s/%s' % (
                                    settings.IONOSPHERE_DATA_FOLDER, str(metric_timestamp),
                                    str(timeseries_dir))
                                if not os.path.exists(training_dir):
                                    mkdir_p(training_dir)
                                full_duration_in_hours = int(settings.FULL_DURATION) / 3600
                                ionosphere_json_file = '%s/%s.mirage.redis.%sh.json' % (
                                    training_dir, base_name,
                                    str(int(full_duration_in_hours)))
                                if not os.path.isfile(ionosphere_json_file):
                                    timeseries_json = str(batch_timeseries).replace('[', '(').replace(']', ')')
                                    try:
                                        write_data_to_file(skyline_app, ionosphere_json_file, 'w', timeseries_json)
                                        logger.info('%s added Ionosphere Mirage %sh Redis data timeseries json file :: %s' % (
                                            skyline_app, str(int(full_duration_in_hours)), ionosphere_json_file))
                                    except:
                                        logger.info(traceback.format_exc())
                                        logger.error('error :: failed to add %s Ionosphere Mirage Redis data timeseries json file - %s' % (skyline_app, ionosphere_json_file))

                    # @modified 20170108 - Feature #1830: Ionosphere alerts
                    # Only send smtp_alerter_metrics to Ionosphere
                    # if send_to_ionosphere:
                    if send_to_ionosphere and smtp_alert_enabled_metric:
                        if metric_name in ionosphere_unique_metrics:
                            logger.info('sending an ionosphere metric to Ionosphere - %s' % (base_name))
                        else:
                            logger.info('sending an analyzer metric to Ionosphere for training - %s' % (base_name))
                        try:
                            # @modified 20161228 Feature #1828: ionosphere - mirage Redis data features
                            # Added full_duration
                            # @modified 20170127 - Feature #1886: Ionosphere learn - child like parent with evolutionary maturity
                            # Added ionosphere_parent_id, always zero from Analyzer and Mirage
                            ionosphere_parent_id = 0
                            send_anomalous_metric_to(
                                skyline_app, 'ionosphere', timeseries_dir,
                                metric_timestamp, base_name, str(datapoint),
                                from_timestamp, triggered_algorithms,
                                batch_timeseries, str(settings.FULL_DURATION),
                                str(ionosphere_parent_id))
                            # @modified 20190522 - Task #3034: Reduce multiprocessing Manager list usage
                            # Moved to Redis key block below
                            # self.sent_to_ionosphere.append(base_name)
                        except:
                            logger.info(traceback.format_exc())
                            logger.error('error :: failed to send_anomalous_metric_to to ionosphere')

                        # @added 20190522 - Task #3034: Reduce multiprocessing Manager list usage
                        redis_set = 'analyzer.batch.sent_to_ionosphere'
                        data = str(base_name)
                        try:
                            self.redis_conn.sadd(redis_set, data)
                        except:
                            logger.info(traceback.format_exc())
                            logger.error('error :: failed to add %s to Redis set %s' % (
                                str(data), str(redis_set)))

                        # @added 20170403 - Feature #1994: Ionosphere training_dir keys
                        #                   Feature #2000: Ionosphere - validated
                        #                   Feature #1996: Ionosphere - matches page
                        # The addition of this key data could be done in
                        # skyline_function.py, however that would introduce
                        # Redis requirements in the send_anomalous_metric_to
                        # function, which is not desirable I think. So this is
                        # a non-KISS pattern that is replicated in mirage.py as
                        # well.
                        # Each training_dir and data set is now Redis keyed to increase efficiency
                        # in terms of disk I/O for ionosphere.py and making keyed data
                        # available for each training_dir data set so that transient matched data
                        # can be surfaced for the webapp along with directory paths, etc
                        ionosphere_training_data_key = 'ionosphere.training_data.%s.%s' % (str(metric_timestamp), base_name)
                        ionosphere_training_data_key_data = [
                            ['metric_timestamp', int(metric_timestamp)],
                            ['base_name', str(base_name)],
                            ['timeseries_dir', str(timeseries_dir)],
                            ['added_by', str(skyline_app)]
                        ]
                        try:
                            self.redis_conn.setex(
                                ionosphere_training_data_key,
                                settings.IONOSPHERE_KEEP_TRAINING_TIMESERIES_FOR,
                                # @modified 20190413 - Task #2824: Test redis-py upgrade
                                #                      Task #2926: Update dependencies
                                # redis-py 3.x only accepts user data as bytes, strings or
                                # numbers (ints, longs and floats).  All 2.X users should
                                # make sure that the keys and values they pass into redis-py
                                # are either bytes, strings or numbers. Use str
                                str(ionosphere_training_data_key_data))
                        except:
                            logger.info(traceback.format_exc())
                            logger.error('error :: failed to set Redis key %s' % ionosphere_training_data_key)
                        try:
                            del ionosphere_training_data_key_data
                        except:
                            pass

                    if ionosphere_metric:
                        analyzer_metric = False

                    # Only send Analyzer metrics
                    if analyzer_metric and settings.PANORAMA_ENABLED:
                        if not os.path.exists(settings.PANORAMA_CHECK_PATH):
                            mkdir_p(settings.PANORAMA_CHECK_PATH)

                        # Note:
                        # The values are enclosed is single quoted intentionally
                        # as the imp.load_source used results in a shift in the
                        # decimal position when double quoted, e.g.
                        # value = "5622.0" gets imported as
                        # 2016-03-02 12:53:26 :: 28569 :: metric variable - value - 562.2
                        # single quoting results in the desired,
                        # 2016-03-02 13:16:17 :: 1515 :: metric variable - value - 5622.0
                        added_at = str(int(time()))
                        source = 'graphite'
                        panorama_anomaly_data = 'metric = \'%s\'\n' \
                                                'value = \'%s\'\n' \
                                                'from_timestamp = \'%s\'\n' \
                                                'metric_timestamp = \'%s\'\n' \
                                                'algorithms = %s\n' \
                                                'triggered_algorithms = %s\n' \
                                                'app = \'%s\'\n' \
                                                'source = \'%s\'\n' \
                                                'added_by = \'%s\'\n' \
                                                'added_at = \'%s\'\n' \
                            % (base_name, str(datapoint), from_timestamp,
                               metric_timestamp, str(settings.ALGORITHMS),
                               triggered_algorithms, skyline_app, source,
                               this_host, added_at)

                        # Create an anomaly file with details about the anomaly
                        panorama_anomaly_file = '%s/%s.%s.txt' % (
                            settings.PANORAMA_CHECK_PATH, added_at,
                            sane_metricname)
                        try:
                            write_data_to_file(
                                skyline_app, panorama_anomaly_file, 'w',
                                panorama_anomaly_data)
                            logger.info('added panorama anomaly file :: %s' % (panorama_anomaly_file))
                            # @modified 20190522 - Task #3034: Reduce multiprocessing Manager list usage
                            # Moved to Redis set block below
                            # self.sent_to_panorama.append(base_name)
                        except:
                            logger.info(traceback.format_exc())
                            logger.error('error :: failed to add panorama anomaly file :: %s' % (panorama_anomaly_file))
                        try:
                            del panorama_anomaly_data
                        except:
                            pass

                        redis_set = 'analyzer_batch.sent_to_panorama'
                        data = str(base_name)
                        try:
                            self.redis_conn.sadd(redis_set, data)
                        except:
                            logger.info(traceback.format_exc())
                            logger.error('error :: failed to add %s to Redis set %s' % (
                                str(data), str(redis_set)))
                    else:
                        # @modified 20160207 - Branch #922: Ionosphere
                        # Handle if all other apps are not enabled
                        other_app = 'none'
                        if mirage_metric:
                            other_app = 'Mirage'
                        if ionosphere_metric:
                            other_app = 'Ionosphere'
                        logger.info('not adding panorama anomaly file for %s - %s' % (other_app, metric))

                    # Send back to Analyzer to alert
                    if analyzer_metric:
                        send_back_to_analyzer = True
                    if send_back_to_analyzer:
                        cache_key = '%s.alert.%s.%s' % (skyline_app, metric_timestamp, base_name)
                        cache_key_value = [float(datapoint), base_name, int(metric_timestamp), triggered_algorithms]
                        try:
                            self.redis_conn.setex(
                                cache_key, 300,
                                str(cache_key_value))
                            logger.info(
                                'add Redis alert key - %s - %s' %
                                (cache_key, str(cache_key_value)))
                        except:
                            logger.error(traceback.format_exc())
                            logger.error(
                                'error :: failed to add Redis key - %s - [%s, \'%s\', %s, %s, %s]' %
                                (cache_key, str(datapoint), base_name,
                                    str(int(metric_timestamp)),
                                    str(triggered_algorithms)))

            # It could have been deleted by the Roomba
            except TypeError:
                # @added 20200430 - Feature #3480: batch_processing
                # Added logging here as the DeletedByRoomba exception is
                # generally not related to that but related to some other fail
                # in the processing of the run algorithms phase
                logger.error(traceback.format_exc())
                logger.error('error :: added as DeletedByRoomba but possibly not see traceback above')

                exceptions['DeletedByRoomba'] += 1
                # @added 20200423 - Feature #3504: Handle airgaps in batch metrics
                #                   Feature #3480: batch_processing
                #                   Feature #3486: analyzer_batch
                # Handle analyzer_batch work being added over and over every
                # minute by also updating the last_timestamp key if stale,
                # boring, etc
                last_metric_timestamp_key = 'last_timestamp.%s' % base_name
                try:
                    int_metric_timestamp = int(time())
                    self.redis_conn.setex(
                        last_metric_timestamp_key, 2592000, int_metric_timestamp)
                    logger.info('set Redis key %s to %s, even though it has been deleted by Roomba' % (
                        last_metric_timestamp_key, str(int_metric_timestamp)))
                except:
                    logger.error('error :: failed to set Redis key %s, even though it is has been deleted by Roomba' % last_metric_timestamp_key)
            except TooShort:
                exceptions['TooShort'] += 1
                # @added 20200423 - Feature #3504: Handle airgaps in batch metrics
                #                   Feature #3480: batch_processing
                #                   Feature #3486: analyzer_batch
                last_metric_timestamp_key = 'last_timestamp.%s' % base_name
                try:
                    int_metric_timestamp = int(batch_timeseries[-1][0])
                    self.redis_conn.setex(
                        last_metric_timestamp_key, 2592000,
                        int_metric_timestamp)
                    logger.info('set Redis key %s to %s, even though it is too short' % (
                        last_metric_timestamp_key, str(int_metric_timestamp)))
                except:
                    logger.error('error :: failed to set Redis key %s, even though it is too short' % last_metric_timestamp_key)
            except Stale:
                exceptions['Stale'] += 1
                # @added 20200423 - Feature #3504: Handle airgaps in batch metrics
                #                   Feature #3480: batch_processing
                #                   Feature #3486: analyzer_batch
                last_metric_timestamp_key = 'last_timestamp.%s' % base_name
                try:
                    int_metric_timestamp = int(batch_timeseries[-1][0])
                    self.redis_conn.setex(
                        last_metric_timestamp_key, 2592000,
                        int_metric_timestamp)
                    logger.info('set Redis key %s to %s, even though it is stale' % (
                        last_metric_timestamp_key, str(int_metric_timestamp)))
                except:
                    logger.error('error :: failed to set Redis key %s, even though it is stale' % last_metric_timestamp_key)
            except Boring:
                exceptions['Boring'] += 1
                # @added 20200423 - Feature #3504: Handle airgaps in batch metrics
                #                   Feature #3480: batch_processing
                #                   Feature #3486: analyzer_batch
                last_metric_timestamp_key = 'last_timestamp.%s' % base_name
                try:
                    int_metric_timestamp = int(batch_timeseries[-1][0])
                    self.redis_conn.setex(
                        last_metric_timestamp_key, 2592000,
                        int_metric_timestamp)
                    logger.info('set Redis key %s to %s, even though it is boring' % (
                        last_metric_timestamp_key, str(int_metric_timestamp)))
                except:
                    logger.error('error :: failed to set Redis key %s, even though it is boring' % last_metric_timestamp_key)
            except:
                logger.error(traceback.format_exc())
                logger.error('error - Other error reported')
                exceptions['Other'] += 1
                # @added 20200423 - Feature #3504: Handle airgaps in batch metrics
                #                   Feature #3480: batch_processing
                #                   Feature #3486: analyzer_batch
                last_metric_timestamp_key = 'last_timestamp.%s' % base_name
                try:
                    int_metric_timestamp = int(time())
                    self.redis_conn.setex(
                        last_metric_timestamp_key, 2592000,
                        int_metric_timestamp)
                    logger.error('error :: set Redis key %s to %s, even though it an other error has been thrown' % (
                        last_metric_timestamp_key, str(int_metric_timestamp)))
                except:
                    logger.error('error :: failed to set Redis key %s, when other exception was thrown' % last_metric_timestamp_key)

        try:
            del timeseries
        except:
            pass
        try:
            del timestamps_to_analyse
        except:
            pass
        try:
            del batch_timeseries
        except:
            pass
        try:
            del mirage_unique_metrics
        except:
            pass
        try:
            del ionosphere_unique_metrics
        except:
            pass
        try:
            del derivative_metrics
        except:
            pass
        try:
            del non_derivative_metrics
        except:
            pass
        try:
            del non_derivative_monotonic_metrics
        except:
            pass
        try:
            del non_smtp_alerter_metrics
        except:
            pass

        # Add values to the queue so the parent process can collate
        for key, value in anomaly_breakdown.items():
            self.batch_anomaly_breakdown_q.put((key, value))

        for key, value in exceptions.items():
            self.batch_exceptions_q.put((key, value))

        # Remove for work list
        redis_set = 'analyzer.batch'
        data = [metric_name, int(last_analyzed_timestamp)]
        try:
            self.redis_conn.srem(redis_set, str(data))
            logger.info('analyzer_batch :: removed batch metric item - %s - from Redis set - %s' % (str(data), redis_set))
        except:
            logger.error(traceback.format_exc())
            logger.error('error :: analyzer_batch :: failed to remove batch metric item - %s - from Redis set - %s' % (str(data), redis_set))

        spin_end = time() - spin_start
        logger.info('spin_batch_process took %.2f seconds' % spin_end)
        return

    def run(self):
        """
        - Called when the process intializes.

        - Determine if Redis is up and discover the number of `unique metrics`.

        - Divide the `unique_metrics` between the number of `ANALYZER_PROCESSES`
          and assign each process a set of metrics to analyse for anomalies.

        - Wait for the processes to finish.

        - Determine whether if any anomalous metrics require:

            - Alerting on (and set `EXPIRATION_TIME` key in Redis for alert).
            - Feed to another module e.g. mirage.
            - Alert to syslog.

        - Populate the webapp json with the anomalous_metrics details.

        - Log the details about the run to the skyline analyzer log.

        - Send skyline.analyzer metrics to `GRAPHITE_HOST`
        """

        # Log management to prevent overwriting
        # Allow the bin/<skyline_app>.d to manage the log
        if os.path.isfile(skyline_app_logwait):
            try:
                os.remove(skyline_app_logwait)
            except OSError:
                logger.error('error - failed to remove %s, continuing' % skyline_app_logwait)
                pass

        now = time()
        log_wait_for = now + 5
        while now < log_wait_for:
            if os.path.isfile(skyline_app_loglock):
                sleep(.1)
                now = time()
            else:
                now = log_wait_for + 1

        logger.info('starting %s run' % skyline_app)
        if os.path.isfile(skyline_app_loglock):
            logger.error('error - bin/%s.d log management seems to have failed, continuing' % skyline_app)
            try:
                os.remove(skyline_app_loglock)
                logger.info('log lock file removed')
            except OSError:
                logger.error('error - failed to remove %s, continuing' % skyline_app_loglock)
                pass
        else:
            logger.info('bin/%s.d log management done' % skyline_app)

        # @added 20190417 - Feature #2950: Report defaulted settings to log
        # Added all the globally declared settings to enable reporting in the
        # log the state of each setting.
        try:
            SERVER_METRIC_PATH = '.%s' % settings.SERVER_METRICS_NAME
            if SERVER_METRIC_PATH == '.':
                SERVER_METRIC_PATH = ''
            logger.info('SERVER_METRIC_PATH is set from settings.py to %s' % str(SERVER_METRIC_PATH))
        except:
            SERVER_METRIC_PATH = ''
            logger.info('warning :: SERVER_METRIC_PATH is not declared in settings.py, defaults to \'\'')
        logger.info('skyline_app_graphite_namespace is set to %s' % str(skyline_app_graphite_namespace))
        try:
            ANALYZER_ENABLED = settings.ANALYZER_ENABLED
            logger.info('ANALYZER_ENABLED is set to %s' % str(ANALYZER_ENABLED))
        except:
            ANALYZER_ENABLED = True
            logger.info('warning :: ANALYZER_ENABLED is not declared in settings.py, defaults to True')

        if not os.path.exists(settings.SKYLINE_TMP_DIR):
            # @modified 20160803 - Adding additional exception handling to Analyzer
            try:
                mkdir_p(settings.SKYLINE_TMP_DIR)
            except:
                logger.error('error :: failed to create %s' % settings.SKYLINE_TMP_DIR)
                logger.info(traceback.format_exc())

        while 1:
            now = time()
            # Make sure Redis is up
            try:
                self.redis_conn.ping()
            except:
                logger.error('error :: Analyzer cannot connect to redis at socket path %s' % settings.REDIS_SOCKET_PATH)
                logger.info(traceback.format_exc())
                sleep(10)
                try:
                    self.redis_conn = get_redis_conn(skyline_app)
                except:
                    logger.info(traceback.format_exc())
                    # logger.error('error :: Analyzer cannot connect to redis at socket path %s' % settings.REDIS_SOCKET_PATH)
                    logger.error('error :: Analyzer cannot connect to get_redis_conn')
                continue
            try:
                self.redis_conn_decoded.ping()
            except:
                logger.error('error :: Analyzer batch cannot connect to redis at socket path %s' % settings.REDIS_SOCKET_PATH)
                logger.info(traceback.format_exc())
                sleep(10)
                try:
                    self.redis_conn_decoded = get_redis_conn_decoded(skyline_app)
                except:
                    logger.info(traceback.format_exc())
                    # logger.error('error :: Analyzer cannot connect to redis at socket path %s' % settings.REDIS_SOCKET_PATH)
                    logger.error('error :: Analyzer batch cannot connect to get_redis_conn')
                continue

            """
            Determine if any metric has been added to process
            """
            while True:

                # Report app up
                try:
                    self.redis_conn.setex(skyline_app, 120, int(now))
                except:
                    logger.error('error :: Analyzer batch could not update the Redis %s key' % skyline_app)
                    logger.info(traceback.format_exc())

                # Discover metrics to analyze
                analyzer_batch_work = None
                redis_set = 'analyzer.batch'
                try:
                    analyzer_batch_work = self.redis_conn_decoded.smembers(redis_set)
                except Exception as e:
                    logger.error('error :: could not query Redis for set %s - %s' % (redis_set, e))
                if analyzer_batch_work:
                    analyzer_batch_work_queue_items = len(analyzer_batch_work)
                    if analyzer_batch_work_queue_items > 0:
                        logger.info('there are %s metrics to process in the %s Redis set' % (
                            str(analyzer_batch_work_queue_items), redis_set))
                        break
                else:
                    logger.info('there are no batch metrics to process')
                    sleep(1)

            metric_name = None
            last_analyzed_timestamp = None
            for index, analyzer_batch in enumerate(analyzer_batch_work):
                try:
                    batch_processing_metric = literal_eval(analyzer_batch)
                    metric_name = str(batch_processing_metric[0])
                    last_analyzed_timestamp = int(batch_processing_metric[1])
                    break
                except:
                    logger.error(traceback.format_exc())
                    logger.error('error :: could not determine details from analyzer_batch entry')
                    metric_name = None
                    last_analyzed_timestamp = None
                    batch_processing_metric = None
                    sleep(1)

            if not metric_name:
                break

            logger.info('processing - %s' % str(batch_processing_metric))

            # Spawn processes
            batch_pids = []
            spawned_batch_pids = []
            batch_pid_count = 0
            run_timestamp = now
            for i in range(1, 2):
                batch_p = Process(target=self.spin_batch_process, args=(i, run_timestamp, metric_name, last_analyzed_timestamp))
                batch_pids.append(batch_p)
                batch_pid_count += 1
                logger.info('starting 1 of %s spin_batch_process' % (str(batch_pid_count)))
                batch_p.start()
                spawned_batch_pids.append(batch_p.pid)

            # Send wait signal to zombie processes
            # for p in pids:
            #     p.join()
            # Self monitor processes and terminate if any spin_batch_process
            # that has run for longer than 300 seconds
            p_starts = time()
            while time() - p_starts <= 300:
                if any(p.is_alive() for p in batch_pids):
                    # Just to avoid hogging the CPU
                    sleep(.1)
                else:
                    # All the processes are done, break now.
                    time_to_run = time() - p_starts
                    logger.info('1 spin_batch_process completed in %.2f seconds' % (time_to_run))
                    break
            else:
                # We only enter this if we didn't 'break' above.
                logger.info('timed out, killing all spin_batch_process processes')
                for p in batch_pids:
                    p.terminate()
                    # p.join()

            for p in batch_pids:
                if p.is_alive():
                    logger.info('stopping spin_process - %s' % (str(p.is_alive())))
                    p.join()

            # Grab data from the queue and populate dictionaries
            exceptions = dict()
            anomaly_breakdown = dict()
            while 1:
                try:
                    key, value = self.batch_anomaly_breakdown_q.get_nowait()
                    if key not in anomaly_breakdown.keys():
                        anomaly_breakdown[key] = value
                    else:
                        anomaly_breakdown[key] += value
                except Empty:
                    break

            while 1:
                try:
                    key, value = self.batch_exceptions_q.get_nowait()
                    if key not in exceptions.keys():
                        exceptions[key] = value
                    else:
                        exceptions[key] += value
                except Empty:
                    break

            # @added 20191021 - Bug #3288: Always send anomaly_breakdown and exception metrics
            #                   Branch #3262: py3
            exceptions_metrics = ['Boring', 'Stale', 'TooShort', 'Other']
            try:
                for i_exception in exceptions_metrics:
                    if i_exception not in exceptions.keys():
                        exceptions[i_exception] = 0
                for i_anomaly_breakdown in settings.ALGORITHMS:
                    if i_anomaly_breakdown not in anomaly_breakdown.keys():
                        anomaly_breakdown[i_anomaly_breakdown] = 0

                exceptions_string = ''
                for i_exception in exceptions.keys():
                    if exceptions_string == '':
                        exceptions_string = '%s: %s' % (str(i_exception), str(exceptions[i_exception]))
                    else:
                        exceptions_string = '%s, %s: %s' % (exceptions_string, str(i_exception), str(exceptions[i_exception]))
                logger.info('exceptions - %s' % str(exceptions_string))

                anomaly_breakdown_string = ''
                if anomaly_breakdown:
                    for i_anomaly_breakdown in anomaly_breakdown.keys():
                        if anomaly_breakdown_string == '':
                            anomaly_breakdown_string = '%s: %s' % (str(i_anomaly_breakdown), str(anomaly_breakdown[i_anomaly_breakdown]))
                        else:
                            anomaly_breakdown_string = '%s, %s: %s' % (anomaly_breakdown_string, str(i_anomaly_breakdown), str(anomaly_breakdown[i_anomaly_breakdown]))
                    logger.info('anomaly_breakdown - %s' % str(anomaly_breakdown_string))
                else:
                    logger.info('anomaly_breakdown - none, no anomalies')
            except:
                logger.error(traceback.format_exc())
                logger.error('error :: could not exceptions and anomaly_breakdown details')
            try:
                del exceptions
            except:
                pass
            try:
                del anomaly_breakdown
            except:
                pass
            try:
                with self.batch_exceptions_q.mutex:
                    self.batch_exceptions_q.queue.clear()
            except:
                pass
            try:
                with self.batch_anomaly_breakdown_q.mutex:
                    self.batch_anomaly_breakdown_q.queue.clear()
            except:
                pass
