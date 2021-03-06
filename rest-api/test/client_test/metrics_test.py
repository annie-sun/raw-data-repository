"""Simple end to end test to exercise the metrics APIs.

The correctness of the metrics is really tested by the unit tests.
As we don't know the state of the database, this is really just testing
that metrics come back and that it doen't crash.
"""

import httplib
import unittest
import pprint

from client import HttpException
from base import BaseClientTest


class MetricsTest(BaseClientTest):
  def test_metrics(self):
    request = {
        'start_date': '2017-01-21',
        'end_date': '2017-01-22'
    }
    try:
      response = self.client.request_json('Metrics', 'POST', request)
      pprint.pprint(response)
    except HttpException as ex:
      if ex.code == httplib.NOT_FOUND:
        print "No metrics loaded"
      else:
        raise

  def test_metrics_limit(self):
    request = {
        'start_date': '2017-01-21',
        'end_date': '2017-01-29'
    }
    with self.assertRaises(HttpException) as cm:
      self.client.request_json('Metrics', 'POST', request)
    self.assertEqual(cm.exception.code, httplib.BAD_REQUEST)

  def test_metrics_empty_dates(self):
    request = {
        'start_date': '',
        'end_date': ''
    }
    with self.assertRaises(HttpException) as cm:
      self.client.request_json('Metrics', 'POST', request)
    self.assertEqual(cm.exception.code, httplib.BAD_REQUEST)

  def test_metrics_fields(self):
    response = self.client.request_json('MetricsFields')
    self.assertEquals('Participant.ageRange', response[0]['name'])


if __name__ == '__main__':
  unittest.main()
