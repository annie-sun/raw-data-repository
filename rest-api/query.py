"""A query to run against a DAO (abstracted from the persistent level)."""
from protorpc import messages

class Operator(messages.Enum):
  EQUALS = 0 # Case insensitive comparison for strings, exact comparison otherwise
  LESS_THAN = 1
  GREATER_THAN = 2
  LESS_THAN_OR_EQUALS = 3
  GREATER_THAN_OR_EQUALS = 4
  NOT_EQUALS = 5
  # Note: we don't support contains or exact string comparison at this stage

class PropertyType(messages.Enum):
  STRING = 0
  DATE = 1
  DATETIME = 2
  ENUM = 3
  INTEGER = 4
  CODE = 5

class FieldFilter(object):
  def __init__(self, field_name, operator, value):
    self.field_name = field_name
    self.operator = operator
    self.value = value

class OrderBy(object):
  def __init__(self, field_name, ascending):
    self.field_name = field_name
    self.ascending = ascending

class Query(object):
  def __init__(self, field_filters, order_by, max_results, pagination_token, a_id=None,
               always_return_token=False, include_total=False):
    self.field_filters = field_filters
    self.order_by = order_by
    self.max_results = max_results
    self.pagination_token = pagination_token
    self.ancestor_id = a_id
    self.always_return_token = always_return_token
    self.include_total = include_total

class Results(object):
  def __init__(self, items, pagination_token=None, more_available=False, total=None):
    self.items = items
    self.pagination_token = pagination_token
    self.more_available = more_available
    self.total = total
