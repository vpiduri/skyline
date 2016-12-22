import logging
import os
import sys
import datetime
import csv
import time
from ast import literal_eval

import traceback
import json
from timeit import default_timer as timer
import numpy as np
import pandas as pd

from tsfresh import extract_features, extract_relevant_features, select_features

import settings
import skyline_version
from skyline_functions import write_data_to_file
from tsfresh_feature_names import TSFRESH_FEATURES, TSFRESH_VERSION

skyline_version = skyline_version.__absolute_version__


def feature_name_id(current_skyline_app, feature_name):
    """
    Determine the Skyline id of a tsfresh feature name

    :param feature_name: the tsfresh feature name
    :type feature_name: str
    :return: id
    :rtype: int

    """

    # f_name = 'value__spkt_welch_density__coeff_8'
    f_name = str(feature_name)
    for feature in TSFRESH_FEATURES:
        if str(feature[1]) == f_name:
            fn_id = str(feature[0])
            del f_name
            del feature
            return fn_id

    del feature
    del f_name
    return 0


def calculate_features_profile(current_skyline_app, timestamp, metric, context):
    """
    Calculates a tsfresh features profile from a training data set

    :param timestamp: the timestamp of metric anomaly with training data
    :type timestamp: str
    :param metric: the base_name of the metric
    :type metric: str
    :return: (features_profile_csv_file_path, successful, fail_msg, traceback_format_exc, calc_time)
    :rtype: int
    :rtype: (str, boolean, str, str, str)
    """

    current_skyline_app_logger = current_skyline_app + 'Log'
    current_logger = logging.getLogger(current_skyline_app_logger)

    base_name = str(metric)
    if context == 'training_data':
        log_context = 'training data'
    if context == 'features_profiles':
        log_context = 'features profile data'
    if context == 'ionosphere':
        log_context = 'ionosphere'
    current_logger.info('%s feature profile creation requested for %s at %s' % (
        context, base_name, timestamp))

    timeseries_dir = base_name.replace('.', '/')
    if context == 'training_data':
        metric_data_dir = '%s/%s/%s' % (
            settings.IONOSPHERE_DATA_FOLDER, timestamp,
            timeseries_dir)
    if context == 'features_profiles':
        metric_data_dir = '%s/%s/%s' % (
            settings.IONOSPHERE_PROFILES_FOLDER, timeseries_dir,
            timestamp)

    features_profile_created_file = '%s/%s.%s.fp.created.txt' % (
        metric_data_dir, str(timestamp), base_name)

    features_profile_details_file = '%s/%s.%s.fp.details.txt' % (
        metric_data_dir, str(timestamp), base_name)

    anomaly_json = '%s/%s.json' % (metric_data_dir, base_name)
    ts_csv = '%s/%s.tsfresh.input.csv' % (metric_data_dir, base_name)
#    anomaly_json = '/opt/skyline/ionosphere/data/1480104000/stats/statsd/graphiteStats/calculationtime/stats.statsd.graphiteStats.calculationtime.json'
#    ts_csv = '/opt/skyline/ionosphere/data/1480104000/stats/statsd/graphiteStats/calculationtime/stats.statsd.graphiteStats.calculationtime.tsfresh.input.csv'
    # This is simply to stay in line with tsfresh naming conventions in their
    # docs and examples
    fname_in = ts_csv
    t_fname_out = fname_in + '.features.transposed.csv'

    fp_created = None
    fp_id = None
    if os.path.isfile(features_profile_created_file):
        current_logger.info('features profile created file exist - %s' % (features_profile_created_file))
        try:
            with open(features_profile_created_file, 'r') as f:
                fp_created_str = f.read()
            fp_created_array = literal_eval(fp_created_str)
            fp_id = fp_created_array[0]
            fp_created = True
        except:
            trace = traceback.format_exc()
            current_logger.error(trace)
            current_logger.error(
                'error: failed to read fp_id from %s' % (features_profile_created_file))
    else:
        current_logger.info('No features profile created file exist - %s' % (features_profile_created_file))

    if os.path.isfile(t_fname_out):
        current_logger.info('transposed features already exist - %s' % (t_fname_out))
        return str(t_fname_out), True, fp_created, fp_id, 'none', 'none', '(previously calculated)'

    start = timer()
    if os.path.isfile(anomaly_json):
        try:
            # Read the timeseries json file
            with open(anomaly_json, 'r') as f:
                raw_timeseries = f.read()
        except:
            trace = traceback.format_exc()
            current_logger.error(trace)
            current_logger.error(
                'error: failed to read timeseries data from %s' % (anomaly_json))
            fail_msg = 'error: failed to read timeseries data from %s' % anomaly_json
            end = timer()
            return 'error', False, fp_created, fp_id, fail_msg, trace
    else:
        trace = 'none'
        fail_msg = 'error: file not found - %s' % (anomaly_json)
        current_logger.error(fail_msg)
        end = timer()
        return 'error', False, fp_created, fp_id, fail_msg, trace, 'unknown'

    # Convert the timeseries to csv
    timeseries_array_str = str(raw_timeseries).replace('(', '[').replace(')', ']')
    timeseries = literal_eval(timeseries_array_str)

    datapoints = timeseries
    converted = []
    for datapoint in datapoints:
        try:
            new_datapoint = [float(datapoint[0]), float(datapoint[1])]
            converted.append(new_datapoint)
        except:
            continue

    if os.path.isfile(ts_csv):
        os.remove(ts_csv)

    for ts, value in converted:
        # print('%s,%s' % (str(int(ts)), str(value)))
        utc_ts_line = '%s,%s,%s\n' % (metric, str(int(ts)), str(value))
        with open(ts_csv, 'a') as fh:
            fh.write(utc_ts_line)

    try:
        df = pd.read_csv(ts_csv, delimiter=',', header=None, names=['metric', 'timestamp', 'value'])
    except:
        trace = traceback.format_exc()
        current_logger.error(trace)
        fail_msg = 'error: failed to create a pandas DataFrame with %s' % ts_csv
        current_logger.error('%s' % fail_msg)
        if os.path.isfile(ts_csv):
            os.remove(ts_csv)
            current_logger.info('removed %s' % ts_csv)
        end = timer()
        return 'error', False, fp_created, fp_id, fail_msg, trace, 'unknown'

# @added 20161207 - Task #1658: Patterning Skyline Ionosphere
# Coverting the Dataframe types to suit MySQL data types
# For anyone in here if you have done a code review of Skyline there are
# a number of questions that arise from the decision to deviate from json or
# storing msgppack as BLOB etc.  tsfresh used csv and we can csv from Graphite
# etc.  Skyline should be able to handle csv.  As for how data is stored in
# MySQL, this was given considerable review and thought.  Given that Ionosphere
# and Skyline in general should not be limited to the domain of analyzing
# Graphite machine metrics but other timeseries data sources too.
#    df['feature_name'] = df['feature_name'].astype(string)
#    df['value'] = df['value'].astype(float)

    # Test the DataFrame
    try:
        df_created = df.head()
        del df_created
    except:
        trace = traceback.format_exc()
        current_logger.debug(trace)
        fail_msg = 'error: failed to read the pandas DataFrame created with %s' % ts_csv
        current_logger.error('%s' % fail_msg)
        if os.path.isfile(ts_csv):
            os.remove(ts_csv)
            current_logger.info('removed %s' % ts_csv)
        end = timer()
        return 'error', False, fp_created, fp_id, fail_msg, trace, 'unknown'

    df.columns = ['metric', 'timestamp', 'value']

    start_feature_extraction = timer()
    try:
        df_features = extract_features(df, column_id='metric', column_sort='timestamp', column_kind=None, column_value=None)
    except:
        trace = traceback.print_exc()
        current_logger.debug(trace)
        fail_msg = 'error: extracting features with tsfresh from - %s' % ts_csv
        current_logger.error('%s' % fail_msg)
        end_feature_extraction = timer()
        current_logger.debug(
            'debug :: feature extraction failed in %.6f seconds' % (
                end_feature_extraction - start_feature_extraction))
        if os.path.isfile(ts_csv):
            os.remove(ts_csv)
            current_logger.info('removed %s' % ts_csv)
        end = timer()
        return 'error', False, fp_created, fp_id, fail_msg, trace, 'unknown'

    end_feature_extraction = timer()
    feature_extraction_time = end_feature_extraction - start_feature_extraction
    current_logger.info(
        'feature extraction took %.6f seconds' % (feature_extraction_time))

    # write to disk
    fname_out = fname_in + '.features.csv'
    # df_features.to_csv(fname_out)

    # Transpose
    try:
        df_t = df_features.transpose()
        current_logger.info('features transposed')
    except:
        trace = traceback.print_exc()
        current_logger.debug(trace)
        fail_msg = 'error :: transposing tsfresh features from - %s' % ts_csv
        current_logger.error('%s' % fail_msg)
        if os.path.isfile(ts_csv):
            os.remove(ts_csv)
            current_logger.info('removed %s' % ts_csv)
        end = timer()
        return 'error', False, fp_created, fp_id, fail_msg, trace, 'unknown'

    # Create transposed features csv
    t_fname_out = fname_in + '.features.transposed.csv'
    try:
        df_t.to_csv(t_fname_out)
    except:
        trace = traceback.print_exc()
        current_logger.debug(trace)
        fail_msg = 'error: saving transposed tsfresh features from - %s' % ts_csv
        current_logger.error('%s' % fail_msg)
        if os.path.isfile(ts_csv):
            os.remove(ts_csv)
            current_logger.info('removed %s' % ts_csv)
        end = timer()
        return 'error', False, fp_created, fp_id, fail_msg, trace, 'unknown'

    # Calculate the count and sum of the features values
    try:
        df_sum = pd.read_csv(
            t_fname_out, delimiter=',', header=0,
            names=['feature_name', 'value'])
        df_sum.columns = ['feature_name', 'value']
        df_sum['feature_name'] = df_sum['feature_name'].astype(str)
        df_sum['value'] = df_sum['value'].astype(float)
    except:
        trace = traceback.print_exc()
        current_logger.error(trace)
        current_logger.error('error :: failed to create Dataframe to sum')
    try:
        features_count = len(df_sum['value'])
    except:
        trace = traceback.print_exc()
        current_logger.debug(trace)
        current_logger.error('error :: failed to count number of features, set to 0')
        features_count = 0
    try:
        features_sum = df_sum['value'].sum()
    except:
        trace = traceback.print_exc()
        current_logger.debug(trace)
        current_logger.error('error :: failed to sum feature values, set to 0')
        features_sum = 0

    end = timer()

    current_logger.info('features saved to %s' % (fname_out))
    current_logger.info('transposed features saved to %s' % (t_fname_out))
    total_calc_time = '%.6f' % (end - start)
    calc_time = '%.6f' % (feature_extraction_time)
    current_logger.info('total feature profile completed in %s seconds' % str(total_calc_time))

    # Create a features profile details file
    try:
        data = '[%s, \'%s\', %s, %s, %s]' % (str(int(time.time())), str(TSFRESH_VERSION), str(calc_time), str(features_count), str(features_sum))
        write_data_to_file(skyline_app, features_profile_details_file, 'w', data)
    except:
        trace = traceback.format_exc()
        current_logger.error('%s' % trace)
        fail_msg = 'error :: failed to write %s' % features_profile_details_file
        current_logger.error('%s' % fail_msg)

    if os.path.isfile(ts_csv):
        os.remove(ts_csv)
        current_logger.info('removed %s' % ts_csv)

    return str(t_fname_out), True, fp_created, fp_id, 'none', 'none', str(calc_time)