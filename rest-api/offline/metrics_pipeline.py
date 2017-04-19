"""Offline process for calculating metrics.

This pipeline consists of three MapReduces chained together.

The first MR reads in three sets of CSV files sharded by participant ID,
generated by our metrics export code:

participants_<shard>.csv = ['date_of_birth', 'first_order_date', 'first_samples_arrived_date',
                            'first_physical_measurements_date',
                            'first_samples_to_isolate_dna_data', <questionnaire submission times>]
hpo_ids_<shard>.csv = ['participant_id', 'hpo', 'last_modified']
answers_<shard>.csv = ['participant_id', 'start_time', 'end_time', 'question_code', 'answer_code']

All results are mapped to (participant_id, date|metric) tuples
(e.g. (123, "2017-01-01|Participant.race.white)), representing new
values for metrics on the dates in question. The reducer emits to GCS files
hpoId|metric|date|delta strings (e.g. "PITT|Participant.race.white|2017-01-01|1")
representing individual increments or decrements of the metric in
question for the given HPO on the given date. Delta values here are either 1 or -1, based on
whether the metric in question applies or stops applying to a participant on the given date.

The second MR reads the files generated by the first MR, maps them to
(hpoId|participant_type|metric, date|delta)
tuples (e.g. ("PITT|R|Participant.race.white", "2017-01-01|1"), where participant type is 'R'
for registered participants and 'F' for full participants based on the participant's enrollment
status at the date in question. It combines them in the combiner stage
by adding the individual increments and decrements together,
and in the reducer stage emits hpoId|participant_type|metric|date|count
(e.g. "PITT|F|Participant.race.white|2017-01-01|42) to GCS files, representing counts for metrics
for HPOs for each date until today.

The third MR reads the files generated by the second MR, maps them to (hpoId|date,
participant_type|metric|count) tuples (and also a '*' hpoId representing cross-HPO metrics)
(e.g. ("PITT|2017-01-01", "R|Participant.race.white|42")),
and in the reducer phase writes metric buckets to SQL. (This is just grouping the output of the
second MR by HPO + date before writing the buckets.)

The final results are metrics buckets in the database, where HPO ID + date is the primary key,
and the metrics fields is a blob of JSON containing a dict of metrics with counts for participants
and full participants (e.g. {"Participant": 52, "Participant.race.white": 42,
"FullParticipant.race.white": 27... }) This represents timeseries data
for metrics values for each HPO over all dates until the last run of the pipeline.

In order to segregate metrics from one run from another, a MetricsVersion record
is created at the beginning of each run.  At the end of the run its 'completed'
property is set to true.  For every MetricsBucket that is created by this
pipeline, its parent is set to the current MetricsVersion.

The metrics to be collected are specified in the METRICS_CONFIGS dict.  In
addition to the fields specified there, for every entity, a synthetic 'total'
metric is generated.  This is to record the total number of entities over time.

"""

import collections
import copy
import json
import logging
import pipeline

import config
import csv
import offline.metrics_config
import offline.sql_exporter

from cloudstorage import cloudstorage_api
from datetime import datetime, timedelta
from mapreduce import base_handler
from mapreduce import mapreduce_pipeline
from mapreduce import context

from dao.database_utils import parse_datetime
from dateutil.relativedelta import relativedelta
from census_regions import census_regions
from code_constants import UNSET, RACE_QUESTION_CODE, PPI_SYSTEM
from dao.metrics_dao import MetricsBucketDao, MetricsVersionDao
from field_mappings import QUESTION_CODE_TO_FIELD, FieldType, QUESTIONNAIRE_MODULE_FIELD_NAMES
from model.metrics import MetricsBucket
from mapreduce.lib.input_reader._gcs import GCSInputReader
from offline.base_pipeline import BasePipeline
from metrics_config import BIOSPECIMEN_METRIC, BIOSPECIMEN_SAMPLES_METRIC, HPO_ID_METRIC
from metrics_config import PHYSICAL_MEASUREMENTS_METRIC, AGE_RANGE_METRIC, CENSUS_REGION_METRIC
from metrics_config import SPECIMEN_COLLECTED_VALUE, RACE_METRIC, ENROLLMENT_STATUS_METRIC
from metrics_config import SAMPLES_ARRIVED_VALUE, SUBMITTED_VALUE, PARTICIPANT_KIND
from metrics_config import HPO_ID_FIELDS, ANSWER_FIELDS, get_participant_fields, get_fieldnames
from metrics_config import transform_participant_summary_field, SAMPLES_TO_ISOLATE_DNA_METRIC
from metrics_config import FULL_PARTICIPANT_KIND
from participant_enums import get_bucketed_age, get_race, PhysicalMeasurementsStatus, SampleStatus
from participant_enums import EnrollmentStatus
from dao.code_dao import CodeDao

class PipelineNotRunningException(BaseException):
  """Exception thrown when a pipeline is expected to be running but is not."""

DATE_FORMAT = '%Y-%m-%d'
DATE_OF_BIRTH_PREFIX = "DOB"

TOTAL_SENTINEL = '__total_sentinel__'
_NUM_SHARDS = '_NUM_SHARDS'

# Participant type constants
_REGISTERED_PARTICIPANT = 'R'
_FULL_PARTICIPANT = 'F'

def default_params():
  """These can be used in a snapshot to ensure they stay the same across
  all instances of a MapReduce pipeline, even if datastore changes"""
  return {
        _NUM_SHARDS: int(config.getSetting(config.METRICS_SHARDS, 1))
    }

def get_config():
  return offline.metrics_config.get_config()

# This is a indicator of the format of the produced metrics.  If the metrics
# pipeline changes such that the produced metrics are not compatible with the
# serving side of the metrics API, increment this version and increment the
# version in metrics.py.  This will cause no metrics to be served while new
# metrics are calculated, which is better than crashing or serving incorrect
# data.
PIPELINE_METRICS_DATA_VERSION = 1

class BlobKeys(base_handler.PipelineBase):
  """A generator for the mapper params for the second MapReduce pipeline, containing the blob
     keys produced by the first pipeline."""
  def run(self, bucket_name, keys, now, version_id):
    start_index = len(bucket_name) + 2
    return {'input_reader': {GCSInputReader.BUCKET_NAME_PARAM: bucket_name,
                             GCSInputReader.OBJECT_NAMES_PARAM: [k[start_index:] for k in keys]},
            'now': now,
            'version_id': version_id}

class MetricsPipeline(BasePipeline):
  def run(self, *args, **kwargs):  # pylint: disable=unused-argument
    bucket_name = args[0]
    now = args[1]
    input_files = args[2]
    mapper_params = default_params()
    version_id = MetricsVersionDao().set_pipeline_in_progress()
    future = yield SummaryPipeline(bucket_name, now, input_files, version_id, mapper_params)
    # Pass future to FinalizeMetrics to ensure it doesn't start running until SummaryPipeline
    # completes
    yield FinalizeMetrics(future, bucket_name, input_files)

  def handle_pipeline_failure(self):
    logging.info("Pipeline failed; setting current metrics version to incomplete.")
    MetricsVersionDao().set_pipeline_finished(False)

class FinalizeMetrics(pipeline.Pipeline):
  def run(self, future, bucket_name, input_files):  # pylint: disable=unused-argument
    metrics_version_dao = MetricsVersionDao()
    metrics_version_dao.set_pipeline_finished(True)
    # After successfully writing metrics, delete old metrics, and delete the input files used to
    # generate the metrics.
    metrics_version_dao.delete_old_versions()
    for input_file in input_files:
      cloudstorage_api.delete('/' + bucket_name + '/' + input_file)


class SummaryPipeline(pipeline.Pipeline):
  def run(self, bucket_name, now, input_files, version_id, parent_params=None):
    logging.info('======= Starting Metrics Pipeline')

    mapper_params = {
        'input_reader': {
            GCSInputReader.BUCKET_NAME_PARAM: bucket_name,
            GCSInputReader.OBJECT_NAMES_PARAM: input_files
        }
    }

    if parent_params:
      mapper_params.update(parent_params)

    num_shards = mapper_params[_NUM_SHARDS]
    # Chain together three map reduces; see module comments
    blob_key_1 = (yield mapreduce_pipeline.MapreducePipeline(
        'Process Input CSV',
        mapper_spec='offline.metrics_pipeline.map_csv_to_participant_and_date_metric',
        input_reader_spec='mapreduce.input_readers.GoogleCloudStorageInputReader',
        output_writer_spec='mapreduce.output_writers.GoogleCloudStorageOutputWriter',
        mapper_params=mapper_params,
        reducer_spec='offline.metrics_pipeline.reduce_participant_data_to_hpo_metric_date_deltas',
        reducer_params={
            'now': now,
            'output_writer': {
                'bucket_name': bucket_name,
                'content_type': 'text/plain'
            }
        },
        shards=num_shards))

    blob_key_2 = (yield mapreduce_pipeline.MapreducePipeline(
        'Calculate Counts',
        mapper_spec='offline.metrics_pipeline.map_hpo_metric_date_deltas_to_hpo_metric_key',
        input_reader_spec='mapreduce.input_readers.GoogleCloudStorageInputReader',
        output_writer_spec='mapreduce.output_writers.GoogleCloudStorageOutputWriter',
        mapper_params=(yield BlobKeys(bucket_name, blob_key_1, now, version_id)),
        combiner_spec='offline.metrics_pipeline.combine_hpo_metric_date_deltas',
        reducer_spec='offline.metrics_pipeline.reduce_hpo_metric_date_deltas_to_all_date_counts',
        reducer_params={
            'now': now,
            'output_writer': {
                'bucket_name': bucket_name,
                'content_type': 'text/plain',
            }
        },
        shards=num_shards))
    # TODO(danrodney):
    # We need to find a way to delete data written above (DA-167)
    yield mapreduce_pipeline.MapreducePipeline(
        'Write Metrics',
        mapper_spec='offline.metrics_pipeline.map_hpo_metric_date_counts_to_hpo_date_key',
        input_reader_spec='mapreduce.input_readers.GoogleCloudStorageInputReader',
        mapper_params=(yield BlobKeys(bucket_name, blob_key_2, now, version_id)),
        reducer_spec='offline.metrics_pipeline.reduce_hpo_date_metric_counts_to_database_buckets',
        reducer_params={
            'version_id': version_id
        },
        shards=num_shards)

def map_csv_to_participant_and_date_metric(csv_buffer):
  """Takes a CSV file as input. Emits (participantId, date|metric) tuples.
  """
  reader = csv.reader(csv_buffer, delimiter=offline.sql_exporter.DELIMITER)
  headers = reader.next()

  # It's not clear if we have access to the filename which would indicate what type of data
  # we're dealing with here. Rely on the column headers to detect data type.
  if headers == HPO_ID_FIELDS:
    results = map_hpo_ids(reader)
  elif headers == ANSWER_FIELDS:
    results = map_answers(reader)
  elif headers == get_participant_fields():
    results = map_participants(reader)
  else:
    raise AssertionError("Unrecognized headers: %s", headers)
  for result in results:
    yield result

def map_hpo_ids(reader):
  """Emit (participantId, date|hpoId.<HPO ID>) for each HPO change.

  The first one for each participant represents the HPO when the participant signed up,
  and is the starting point for that participant's history.
  """
  for participant_id, hpo, last_modified in reader:
    yield(participant_id, make_tuple(last_modified, make_metric(HPO_ID_METRIC, hpo)))

def map_answers(reader):
  """Emit (participantId, date|<metric>.<answer>) for each answer.

  Metric names are taken from the field name in code_constants.

  Code and string answers are accepted.

  Incoming rows are expected to be sorted by participant ID, start time, and question code,
  such that repeated answers for the same question are next to each other.
  """
  last_participant_id = None
  last_start_time = None
  race_code_values = []
  code_dao = CodeDao()
  for participant_id, start_time, question_code, answer_code, answer_string in reader:

    # Multiple race answer values for the participant at a single time
    # are combined into a single race enum.
    if race_code_values and (last_participant_id != participant_id or
                             last_start_time != start_time or
                             question_code != RACE_QUESTION_CODE):
      race_codes = [code_dao.get_code(PPI_SYSTEM, value) for value in race_code_values]
      race = get_race(race_codes)
      yield(last_participant_id, make_tuple(last_start_time,
                                               make_metric(RACE_METRIC, str(race))))
      race_code_values = []
    last_participant_id = participant_id
    last_start_time = start_time
    if question_code == RACE_QUESTION_CODE:
      race_code_values.append(answer_code)
      continue
    question_field = QUESTION_CODE_TO_FIELD[question_code]
    metric = transform_participant_summary_field(question_field[0])
    if question_field[1] == FieldType.CODE:
      answer_value = answer_code
      if metric == 'state':
        state_val = answer_code[len(answer_code) - 2:]
        census_region = census_regions.get(state_val) or UNSET
        yield(participant_id, make_tuple(start_time, make_metric(CENSUS_REGION_METRIC,
                                                                    census_region)))
    elif question_field[1] == FieldType.STRING:
      answer_value = answer_string
    else:
      raise AssertionError("Invalid field type: %s" % question_field[1])
    yield(participant_id, make_tuple(start_time, make_metric(metric, answer_value)))

  # Emit race for the last participant if we saved some values for it.
  if race_code_values:
    race_codes = [code_dao.get_code(PPI_SYSTEM, value) for value in race_code_values]
    race = get_race(race_codes)
    yield(last_participant_id, make_tuple(last_start_time,
                                             make_metric(RACE_METRIC, str(race))))

def map_participants(reader):
  """Emits any or all of the following:
  (participantId, DOB|<date of birth>)
  (participantId, date|biospecimen.SPECIMEN_COLLECTED)       (for orders)
  (participantId, date|biospecimenSamples.SAMPLES_ARRIVED)   (for samples)
  (participantId, date|physicalMeasurements.COMPLETED)       (for physical measurements)
  (participantId, date|samplesToIsolateDNA.RECEIVED)         (for samples that isolate DNA)
  (participantId, date|<questionnaire or consent>.SUBMITTED) (for questionnaire submissions)
  """
  for row in reader:
    participant_id = row[0]
    date_of_birth = row[1]
    first_order_date = row[2]
    first_samples_arrived_date = row[3]
    first_physical_measurements_date = row[4]
    first_samples_to_isolate_dna = row[5]
    if date_of_birth:
      yield(participant_id, make_tuple(DATE_OF_BIRTH_PREFIX, date_of_birth))
    if first_order_date:
      yield(participant_id, make_tuple(first_order_date,
                                          make_metric(BIOSPECIMEN_METRIC,
                                                      SPECIMEN_COLLECTED_VALUE)))
    if first_samples_arrived_date:
      yield(participant_id, make_tuple(first_samples_arrived_date,
                                          make_metric(BIOSPECIMEN_SAMPLES_METRIC,
                                                      SAMPLES_ARRIVED_VALUE)))
    if first_physical_measurements_date:
      yield(participant_id, make_tuple(first_physical_measurements_date,
                                          make_metric(PHYSICAL_MEASUREMENTS_METRIC,
                                                      str(PhysicalMeasurementsStatus.COMPLETED))))
    if first_samples_to_isolate_dna:
      yield(participant_id, make_tuple(first_samples_to_isolate_dna,
                                          make_metric(SAMPLES_TO_ISOLATE_DNA_METRIC,
                                                      str(SampleStatus.RECEIVED))))
    for i in range(6, len(row)):
      questionnaire_submitted_time = row[i]
      if questionnaire_submitted_time:
        metric = QUESTIONNAIRE_MODULE_FIELD_NAMES[i - 6]
        yield(participant_id, make_tuple(questionnaire_submitted_time,
                                            make_metric(metric, SUBMITTED_VALUE)))

def make_tuple(*args):
  return '|'.join(args)

def parse_tuple(row):
  return tuple(row.split('|'))

def map_result_key(hpo_id, participant_type, k, v):
  return make_tuple(hpo_id, participant_type, make_metric(k, v))

def sum_deltas(values, delta_map):
  for value in values:
    (date, delta) = parse_tuple(value)
    old_delta = delta_map.get(date)
    if old_delta:
      delta_map[date] = old_delta + int(delta)
    else:
      delta_map[date] = int(delta)

def _add_age_range_metrics(dates_and_metrics, date_of_birth, now):
  creation_date = dates_and_metrics[0][0].date()
  now = now or context.get().mapreduce_spec.mapper.params.get('now')
  # Add entries between the creation date and now for the participant's age range.
  start_age_range = get_bucketed_age(date_of_birth, creation_date)
  difference_in_years = relativedelta(creation_date, date_of_birth).years
  year = relativedelta(years=1)
  date = date_of_birth + relativedelta(years=difference_in_years + 1)
  previous_age_range = start_age_range
  while date and date <= now.date():
    age_range = get_bucketed_age(date_of_birth, date)
    if age_range != previous_age_range:
      dates_and_metrics.append((datetime(year=date.year, month=date.month, day=date.day),
                                make_metric(AGE_RANGE_METRIC, age_range)))
      previous_age_range = age_range
    date = date + year
  return start_age_range

def _update_summary_fields(summary_fields, new_state):
  for summary_field in summary_fields:
    new_state[summary_field.name] = summary_field.compute_func(new_state)

def _process_metric(metrics_fields, summary_fields, metric, new_state):
  metric_name, value = parse_metric(metric)
  something_changed = False
  if metric_name in metrics_fields:
    if new_state[metric_name] != value:
      new_state[metric_name] = value
      something_changed = True

  if something_changed:
    _update_summary_fields(summary_fields, new_state)
  return something_changed

def reduce_participant_data_to_hpo_metric_date_deltas(reducer_key, reducer_values, now=None):
  """Input:

  reducer_key - participant ID
  reducer_values - strings of the form date|metric, or DOB|date_of_birth.

  Sorts everything by date, and emits hpoId|participant_type|metric|date|delta strings representing
  increments or decrements of metrics based on this participant.
  """
  #pylint: disable=unused-argument
  metrics_conf = get_config()
  metric_fields = get_fieldnames()
  summary_fields = metrics_conf['summary_fields']
  last_state = {}
  last_hpo_id = None
  dates_and_metrics = []

  date_of_birth = None
  for reducer_value in reducer_values:
    t = parse_tuple(reducer_value)
    if t[0] == DATE_OF_BIRTH_PREFIX:
      date_of_birth = datetime.strptime(t[1], DATE_FORMAT).date()
    else:
      dates_and_metrics.append((parse_datetime(t[0]), t[1]))

  if not dates_and_metrics:
    return

  # Sort the dates and metrics, date first then metric.
  dates_and_metrics = sorted(dates_and_metrics)

  initial_state = {f.name: UNSET for f in metrics_conf['fields']}
  initial_state[TOTAL_SENTINEL] = 1
  last_hpo_id = UNSET
  # Look for the starting HPO, update the initial state with it, and remove it from
  # the list of date-and-metrics pairs.
  for i in range(0, len(dates_and_metrics)):
    metric = dates_and_metrics[i][1]
    metric_name, value = parse_metric(metric)
    if metric_name == HPO_ID_METRIC:
      last_hpo_id = value
      initial_state[HPO_ID_METRIC] = last_hpo_id
      break

  # If we know the participant's date of birth, and a starting age range
  # and entries for when it changes over time.
  if date_of_birth:
    initial_state[AGE_RANGE_METRIC] = _add_age_range_metrics(dates_and_metrics, date_of_birth, now)
    # Re-sort with the new entries for age range changes.
    dates_and_metrics = sorted(dates_and_metrics)

  # Run summary functions on the initial state.
  _update_summary_fields(summary_fields, initial_state)

  # Emit 1 values for the initial state before any metrics change.
  initial_date = dates_and_metrics[0][0]
  for k, v in initial_state.iteritems():
    yield reduce_result_value(map_result_key(last_hpo_id, _REGISTERED_PARTICIPANT, k, v),
                              initial_date.date().isoformat(), '1')

  last_state = initial_state
  full_participant = False
  # Loop through all the metric changes for the participant.
  for dt, metric in dates_and_metrics:
    date = dt.date()
    new_state = copy.deepcopy(last_state)

    if not _process_metric(metric_fields, summary_fields, metric, new_state):
      continue  # No changes so there's nothing to do.
    hpo_id = new_state.get(HPO_ID_METRIC)
    hpo_change = last_hpo_id != hpo_id

    last_full_participant = full_participant
    for k, v in new_state.iteritems():
      # Output a delta for this field if it is either the first value we have,
      # or if it has changed. In the case that one of the facets has changed,
      # we need deltas for all fields.
      old_val = last_state and last_state.get(k, None)
      if hpo_change or v != old_val:
        formatted_date = date.isoformat()
        if (k == ENROLLMENT_STATUS_METRIC and v == EnrollmentStatus.FULL_PARTICIPANT and
            not full_participant):
          full_participant = True
          # Emit 1 values for the current state for all fields for the full participant type.
          for k2, v2 in new_state.iteritems():
            yield reduce_result_value(map_result_key(hpo_id, _FULL_PARTICIPANT, k2, v2),
                                      formatted_date, '1')
        yield reduce_result_value(map_result_key(hpo_id, _REGISTERED_PARTICIPANT, k, v),
                                  formatted_date, '1')
        if last_full_participant:
          yield reduce_result_value(map_result_key(hpo_id, _FULL_PARTICIPANT, k, v), formatted_date,
                                    '1')
        if last_state:
          # If the value changed, output -1 delta for the old value.
          yield reduce_result_value(map_result_key(last_hpo_id, _REGISTERED_PARTICIPANT,
                                                   k, old_val),
                                    formatted_date, '-1')
          if last_full_participant:
            yield reduce_result_value(map_result_key(last_hpo_id, _FULL_PARTICIPANT, k, old_val),
                                      formatted_date, '-1')

    last_state = new_state
    last_hpo_id = hpo_id

def map_hpo_metric_date_deltas_to_hpo_metric_key(row_buffer):
  """Emits (hpoId|participant_type|metric, date|delta) pairs for reducing

     row_buffer: buffer containing hpoId|participant_type|metric|date|delta lines
  """
  reader = csv.reader(row_buffer, delimiter='|')
  for line in reader:
    hpo_id = line[0]
    participant_type = line[1]
    metric_key = line[2]
    date_str = line[3]
    delta = line[4]
    # Yield HPO ID|participant_type|metric -> date|delta
    yield (make_tuple(hpo_id, participant_type, metric_key), make_tuple(date_str, delta))

def combine_hpo_metric_date_deltas(key, new_values, old_values):  # pylint: disable=unused-argument
  """ Combines deltas generated for users into a single delta per date
  Args:
     key: hpoId|participant_type|metric (unused)
     new_values: list of date|delta strings (one per participant + type + metric + date + hpoId)
     old_values: list of date|delta strings (one per type + metric + date + hpoId)
  """
  delta_map = {}
  for old_value in old_values:
    (date, delta) = parse_tuple(old_value)
    delta_map[date] = int(delta)
  sum_deltas(new_values, delta_map)
  for date, delta in delta_map.iteritems():
    yield make_tuple(date, str(delta))

def reduce_hpo_metric_date_deltas_to_all_date_counts(reducer_key, reducer_values, now=None):
  """Emits hpoId|participant_type|metric|date|count for each date until today.
  Args:
    reducer_key: hpoId|participant_type|metric
    reducer_values: list of date|delta strings
    now: use to set the clock for testing
  """
  delta_map = {}
  sum_deltas(reducer_values, delta_map)
  # Walk over the deltas by date
  last_date = None
  count = 0
  one_day = timedelta(days=1)
  now = now or context.get().mapreduce_spec.mapper.params.get('now')
  for date_str, delta in sorted(delta_map.items()):
    date = datetime.strptime(date_str, DATE_FORMAT).date()
    if date > now.date():
      # Ignore any data after the current run date.
      break
    # Yield results for all the dates in between
    if last_date:
      middle_date = last_date + one_day
      while middle_date < date:
        yield reduce_result_value(reducer_key, middle_date.isoformat(), count)
        middle_date = middle_date + one_day
    count += delta
    if count > 0:
      yield reduce_result_value(reducer_key, date_str, count)
    last_date = date
  # Yield results up until today.
  if count > 0 and last_date:
    last_date = last_date + one_day
    while last_date <= now.date():
      yield reduce_result_value(reducer_key, last_date.isoformat(), count)
      last_date = last_date + one_day

def reduce_result_value(reducer_key, date_str, count):
  return reducer_key + '|' + date_str + '|' + str(count) + '\n'

def map_hpo_metric_date_counts_to_hpo_date_key(row_buffer):
  """Emits (hpoId|date, participant_type|metric|count) pairs for reducing ('*' for cross-HPO counts)
  Args:
     row_buffer: buffer containing hpoId|participant_type|metric|date|count lines
  """
  reader = csv.reader(row_buffer, delimiter='|')
  for line in reader:
    hpo_id = line[0]
    participant_type = line[1]
    metric_key = line[2]
    date_str = line[3]
    count = line[4]
    # Yield HPO ID + date -> metric + count
    yield (make_tuple(hpo_id, date_str), make_tuple(participant_type, metric_key, count))
    # Yield '*' + date -> metric + count (for all HPO counts)
    yield (make_tuple('*', date_str), make_tuple(participant_type, metric_key, count))

def reduce_hpo_date_metric_counts_to_database_buckets(reducer_key, reducer_values, version_id=None):
  """Emits a metrics bucket with counts for metrics for a given hpoId + date to SQL
  Args:
     reducer_key: hpoId|date ('*' for hpoId for cross-HPO counts)
     reducer_values: list of participant_type|metric|count strings
  """
  metrics_dict = collections.defaultdict(lambda: 0)
  (hpo_id, date_str) = parse_tuple(reducer_key)
  if hpo_id == '*':
    hpo_id = ''
  date = datetime.strptime(date_str, DATE_FORMAT)
  for reducer_value in reducer_values:
    (participant_type, metric_key, count) = parse_tuple(reducer_value)
    if metric_key == PARTICIPANT_KIND:
      if participant_type == _REGISTERED_PARTICIPANT:
        metrics_dict[metric_key] += int(count)
    else:
      kind = FULL_PARTICIPANT_KIND if participant_type == _FULL_PARTICIPANT else PARTICIPANT_KIND
      metrics_dict['%s.%s' % (kind, metric_key)] += int(count)

  version_id = version_id or context.get().mapreduce_spec.mapper.params.get('version_id')
  bucket = MetricsBucket(metricsVersionId=version_id,
                         date=date,
                         hpoId=hpo_id,
                         metrics=json.dumps(metrics_dict))
  MetricsBucketDao().upsert(bucket)

def parse_metric(metric):
  return metric.split('.')

def make_metric(key, value):
  if key is TOTAL_SENTINEL:
    return PARTICIPANT_KIND
  return '{}.{}'.format(key, value)
