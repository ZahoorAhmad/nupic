# ----------------------------------------------------------------------
# Numenta Platform for Intelligent Computing (NuPIC)
# Copyright (C) 2013, Numenta, Inc.  Unless you have an agreement
# with Numenta, Inc., for a separate license for this software code, the
# following terms and conditions apply:
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU Affero Public License for more details.
#
# You should have received a copy of the GNU Affero Public License
# along with this program.  If not, see http://www.gnu.org/licenses.
#
# http://numenta.org/licenses/
# ----------------------------------------------------------------------

import copy
import json
import os
import sys
import tempfile
import logging
import re
import traceback
import StringIO
from collections import namedtuple
import pprint
import shutil
import types
import signal
import uuid
import validictory

from nupic.database.ClientJobsDAO import (
    ClientJobsDAO, InvalidConnectionException)

# TODO: Note the function 'rUpdate' is also duplicated in the
# nupic.data.dictutils module -- we will eventually want to change this
# TODO: 'ValidationError', 'validate', 'loadJSONValueFromFile' duplicated in
# nupic.data.jsonhelpers -- will want to remove later

class JobFailException(Exception):
  """ If a model raises this exception, then the runModelXXX code will
  mark the job as canceled so that all other workers exit immediately, and mark
  the job as failed.
  """
  pass



def getCopyrightHead():
  return """# ----------------------------------------------------------------------
# Numenta Platform for Intelligent Computing (NuPIC)
# Copyright (C) 2013, Numenta, Inc.  Unless you have an agreement
# with Numenta, Inc., for a separate license for this software code, the
# following terms and conditions apply:
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU Affero Public License for more details.
#
# You should have received a copy of the GNU Affero Public License
# along with this program.  If not, see http://www.gnu.org/licenses.
#
# http://numenta.org/licenses/
# ----------------------------------------------------------------------
"""



def _paramsFileHead():
  """
  This is the first portion of every sub-experiment params file we generate. Between
  the head and the tail are the experiment specific options.
  """

  str = getCopyrightHead() + \
"""

## This file defines parameters for a prediction experiment.

###############################################################################
#                                IMPORTANT!!!
# This params file is dynamically generated by the RunExperimentPermutations
# script. Any changes made manually will be over-written the next time
# RunExperimentPermutations is run!!!
###############################################################################


from nupic.frameworks.opf.expdescriptionhelpers import importBaseDescription

# the sub-experiment configuration
config ={
"""

  return str


def _paramsFileTail():
  """
  This is the tail of every params file we generate. Between the head and the tail
  are the experiment specific options.
  """

  str = \
"""
}

mod = importBaseDescription('base.py', config)
locals().update(mod.__dict__)
"""
  return str



def _appendReportKeys(keys, prefix, results):
  """
  Generate a set of possible report keys for an experiment's results.
  A report key is a string of key names separated by colons, each key being one
  level deeper into the experiment results dict. For example, 'key1:key2'.

  This routine is called recursively to build keys that are multiple levels
  deep from the results dict.

  Parameters:
  -----------------------------------------------------------
  keys:         Set of report keys accumulated so far
  prefix:       prefix formed so far, this is the colon separated list of key
                  names that led up to the dict passed in results
  results:      dictionary of results at this level.
  """

  allKeys = results.keys()
  allKeys.sort()
  for key in allKeys:
    if hasattr(results[key], 'keys'):
      _appendReportKeys(keys, "%s%s:" % (prefix, key), results[key])
    else:
      keys.add("%s%s" % (prefix, key))



class _BadKeyError(Exception):
  """ If a model raises this exception, then the runModelXXX code will
  mark the job as canceled so that all other workers exit immediately, and mark
  the job as failed.
  """
  pass



def _matchReportKeys(reportKeyREs=[], allReportKeys=[]):
  """
  Extract all items from the 'allKeys' list whose key matches one of the regular
  expressions passed in 'reportKeys'.

  Parameters:
  ----------------------------------------------------------------------------
  reportKeyREs:     List of regular expressions
  allReportKeys:    List of all keys

  retval:         list of keys from allReportKeys that match the regular expressions
                    in 'reportKeyREs'
                  If an invalid regular expression was included in 'reportKeys',
                    then BadKeyError() is raised
  """

  matchingReportKeys = []

  # Extract the report items of interest
  for keyRE in reportKeyREs:
    # Find all keys that match this regular expression
    matchObj = re.compile(keyRE)
    found = False
    for keyName in allReportKeys:
      match = matchObj.match(keyName)
      if match and match.end() == len(keyName):
        matchingReportKeys.append(keyName)
        found = True
    if not found:
      raise _BadKeyError(keyRE)

  return matchingReportKeys



def _getReportItem(itemName, results):
  """
  Get a specific item by name out of the results dict.

  The format of itemName is a string of dictionary keys separated by colons,
  each key being one level deeper into the results dict. For example,
  'key1:key2' would fetch results['key1']['key2'].

  If itemName is not found in results, then None is returned

  """

  subKeys = itemName.split(':')
  subResults = results
  for subKey in subKeys:
    subResults = subResults[subKey]

  return subResults



def filterResults(allResults, reportKeys, optimizeKey=None):
  """ Given the complete set of results generated by an experiment (passed in
  'results'), filter out and return only the ones the caller wants, as
  specified through 'reportKeys' and 'optimizeKey'.

  A report key is a string of key names separated by colons, each key being one
  level deeper into the experiment results dict. For example, 'key1:key2'.


  Parameters:
  -------------------------------------------------------------------------
  results:             dict of all results generated by an experiment
  reportKeys:          list of items from the results dict to include in
                       the report. These can be regular expressions.
  optimizeKey:         Which report item, if any, we will be optimizing for. This can
                       also be a regular expression, but is an error if it matches
                       more than one key from the experiment's results.
  retval:  (reportDict, optimizeDict)
              reportDict: a dictionary of the metrics named by desiredReportKeys
              optimizeDict: A dictionary containing 1 item: the full name and
                    value of the metric identified by the optimizeKey

  """

  # Init return values
  optimizeDict = dict()

  # Get all available report key names for this experiment
  allReportKeys = set()
  _appendReportKeys(keys=allReportKeys, prefix='', results=allResults)

  #----------------------------------------------------------------------------
  # Extract the report items that match the regular expressions passed in reportKeys
  matchingKeys = _matchReportKeys(reportKeys, allReportKeys)

  # Extract the values of the desired items
  reportDict = dict()
  for keyName in matchingKeys:
    value = _getReportItem(keyName, allResults)
    reportDict[keyName] = value


  # -------------------------------------------------------------------------
  # Extract the report item that matches the regular expression passed in
  #   optimizeKey
  if optimizeKey is not None:
    matchingKeys = _matchReportKeys([optimizeKey], allReportKeys)
    if len(matchingKeys) == 0:
      raise _BadKeyError(optimizeKey)
    elif len(matchingKeys) > 1:
      raise _BadOptimizeKeyError(optimizeKey, matchingKeys)
    optimizeKeyFullName = matchingKeys[0]

    # Get the value of the optimize metric
    value = _getReportItem(optimizeKeyFullName, allResults)
    optimizeDict[optimizeKeyFullName] = value
    reportDict[optimizeKeyFullName] = value

  # Return info
  return(reportDict, optimizeDict)



def _quoteAndEscape(string):
  """
  string:   input string (ascii or unicode)

  Returns:  a quoted string with characters that are represented in python via
            escape sequences converted to those escape sequences
  """
  assert type(string) in types.StringTypes
  return pprint.pformat(string)



def _handleModelRunnerException(jobID, modelID, jobsDAO, experimentDir, logger,
                                e):
  """ Perform standard handling of an exception that occurs while running
  a model.

  Parameters:
  -------------------------------------------------------------------------
  jobID:                ID for this hypersearch job in the jobs table
  modelID:              model ID
  jobsDAO:              ClientJobsDAO instance
  experimentDir:        directory containing the experiment
  logger:               the logger to use
  e:                    the exception that occurred
  retval:               (completionReason, completionMsg)
  """

  msg = StringIO.StringIO()
  print >>msg, "Exception occurred while running model %s: %r (%s)" % (
    modelID, e, type(e))
  traceback.print_exc(None, msg)

  completionReason = jobsDAO.CMPL_REASON_ERROR
  completionMsg = msg.getvalue()
  logger.error(completionMsg)

  # Write results to the model database for the error case. Ignore
  # InvalidConnectionException, as this is usually caused by orphaned models
  #
  # TODO: do we really want to set numRecords to 0? Last updated value might
  #       be useful for debugging
  if type(e) is not InvalidConnectionException:
    jobsDAO.modelUpdateResults(modelID,  results=None, numRecords=0)

  # TODO: Make sure this wasn't the best model in job. If so, set the best
  # appropriately

  # If this was an exception that should mark the job as failed, do that
  # now.
  if type(e) == JobFailException:
    workerCmpReason = jobsDAO.jobGetFields(jobID,
        ['workerCompletionReason'])[0]
    if workerCmpReason == ClientJobsDAO.CMPL_REASON_SUCCESS:
      jobsDAO.jobSetFields(jobID, fields=dict(
          cancel=True,
          workerCompletionReason = ClientJobsDAO.CMPL_REASON_ERROR,
          workerCompletionMsg = ": ".join(str(i) for i in e.args)),
          useConnectionID=False,
          ignoreUnchanged=True)

  return (completionReason, completionMsg)



def runModelGivenBaseAndParams(modelID, jobID, baseDescription, params,
            predictedField, reportKeys, optimizeKey, jobsDAO,
            modelCheckpointGUID, logLevel=None, predictionCacheMaxRecords=None):
  """ This creates an experiment directory with a base.py description file
  created from 'baseDescription' and a description.py generated from the
  given params dict and then runs the experiment.

  Parameters:
  -------------------------------------------------------------------------
  modelID:              ID for this model in the models table
  jobID:                ID for this hypersearch job in the jobs table
  baseDescription:      Contents of a description.py with the base experiment
                                          description
  params:               Dictionary of specific parameters to override within
                                  the baseDescriptionFile.
  predictedField:       Name of the input field for which this model is being
                                    optimized
  reportKeys:           Which metrics of the experiment to store into the
                                    results dict of the model's database entry
  optimizeKey:          Which metric we are optimizing for
  jobsDAO               Jobs data access object - the interface to the
                                  jobs database which has the model's table.
  modelCheckpointGUID:  A persistent, globally-unique identifier for
                                  constructing the model checkpoint key
  logLevel:             override logging level to this value, if not None

  retval:               (completionReason, completionMsg)
  """
  from nupic.swarming.ModelRunner import OPFModelRunner

  # The logger for this method
  logger = logging.getLogger('com.numenta.nupic.hypersearch.utils')


  # --------------------------------------------------------------------------
  # Create a temp directory for the experiment and the description files
  experimentDir = tempfile.mkdtemp()
  try:
    logger.info("Using experiment directory: %s" % (experimentDir))

    # Create the decription.py from the overrides in params
    paramsFilePath = os.path.join(experimentDir, 'description.py')
    paramsFile = open(paramsFilePath, 'wb')
    paramsFile.write(_paramsFileHead())

    items = params.items()
    items.sort()
    for (key,value) in items:
      quotedKey = _quoteAndEscape(key)
      if isinstance(value, basestring):

        paramsFile.write("  %s : '%s',\n" % (quotedKey , value))
      else:
        paramsFile.write("  %s : %s,\n" % (quotedKey , value))

    paramsFile.write(_paramsFileTail())
    paramsFile.close()


    # Write out the base description
    baseParamsFile = open(os.path.join(experimentDir, 'base.py'), 'wb')
    baseParamsFile.write(baseDescription)
    baseParamsFile.close()


    # Store the experiment's sub-description file into the model table
    #  for reference
    fd = open(paramsFilePath)
    expDescription = fd.read()
    fd.close()
    jobsDAO.modelSetFields(modelID, {'genDescription': expDescription})


    # Run the experiment now
    try:
      runner = OPFModelRunner(
        modelID=modelID,
        jobID=jobID,
        predictedField=predictedField,
        experimentDir=experimentDir,
        reportKeyPatterns=reportKeys,
        optimizeKeyPattern=optimizeKey,
        jobsDAO=jobsDAO,
        modelCheckpointGUID=modelCheckpointGUID,
        logLevel=logLevel,
        predictionCacheMaxRecords=predictionCacheMaxRecords)

      signal.signal(signal.SIGINT, runner.handleWarningSignal)

      (completionReason, completionMsg) = runner.run()

    except InvalidConnectionException:
      raise
    except Exception, e:

      (completionReason, completionMsg) = _handleModelRunnerException(jobID,
                                     modelID, jobsDAO, experimentDir, logger, e)

  finally:
    # delete our temporary directory tree
    shutil.rmtree(experimentDir)
    signal.signal(signal.SIGINT, signal.default_int_handler)

  # Return completion reason and msg
  return (completionReason, completionMsg)



def runDummyModel(modelID, jobID, params, predictedField, reportKeys,
                  optimizeKey, jobsDAO, modelCheckpointGUID, logLevel=None, predictionCacheMaxRecords=None):
  from nupic.swarming.DummyModelRunner import OPFDummyModelRunner

  # The logger for this method
  logger = logging.getLogger('com.numenta.nupic.hypersearch.utils')


  # Run the experiment now
  try:
    if type(params) is bool:
      params = {}

    runner = OPFDummyModelRunner(modelID=modelID,
                                 jobID=jobID,
                                 params=params,
                                 predictedField=predictedField,
                                 reportKeyPatterns=reportKeys,
                                 optimizeKeyPattern=optimizeKey,
                                 jobsDAO=jobsDAO,
                                 modelCheckpointGUID=modelCheckpointGUID,
                                 logLevel=logLevel,
                                 predictionCacheMaxRecords=predictionCacheMaxRecords)

    (completionReason, completionMsg) = runner.run()

  # The dummy model runner will call sys.exit(1) if
  #  NTA_TEST_sysExitFirstNModels is set and the number of models in the
  #  models table is <= NTA_TEST_sysExitFirstNModels
  except SystemExit:
    sys.exit(1)
  except InvalidConnectionException:
    raise
  except Exception, e:
    (completionReason, completionMsg) = _handleModelRunnerException(jobID,
                                   modelID, jobsDAO, "NA",
                                   logger, e)

  # Return completion reason and msg
  return (completionReason, completionMsg)



# Passed as parameter to ActivityMgr
#
# repeating: True if the activity is a repeating activite, False if one-shot
# period: period of activity's execution (number of "ticks")
# cb: a callable to call upon expiration of period; will be called
#     as cb()
PeriodicActivityRequest = namedtuple("PeriodicActivityRequest",
                                     ("repeating", "period", "cb"))



class PeriodicActivityMgr(object):
  """
  TODO: move to shared script so that we can share it with run_opf_experiment
  """

  # iteratorHolder: a list holding one iterator; we use a list so that we can
  #           replace the iterator for repeating activities (a tuple would not
  #           allow it if the field was an imutable value)
  Activity = namedtuple("Activity", ("repeating",
                                     "period",
                                     "cb",
                                     "iteratorHolder"))

  def __init__(self, requestedActivities):
    """
    requestedActivities: a sequence of PeriodicActivityRequest elements
    """

    self.__activities = []
    for req in requestedActivities:
      act =   self.Activity(repeating=req.repeating,
                            period=req.period,
                            cb=req.cb,
                            iteratorHolder=[iter(xrange(req.period))])
      self.__activities.append(act)
    return

  def tick(self):
    """ Activity tick handler; services all activities

    Returns:      True if controlling iterator says it's okay to keep going;
                  False to stop
    """

    # Run activities whose time has come
    for act in self.__activities:
      if not act.iteratorHolder[0]:
        continue

      try:
        next(act.iteratorHolder[0])
      except StopIteration:
        act.cb()
        if act.repeating:
          act.iteratorHolder[0] = iter(xrange(act.period))
        else:
          act.iteratorHolder[0] = None

    return True



def generatePersistentJobGUID():
  """Generates a "persistentJobGUID" value.

  Parameters:
  ----------------------------------------------------------------------
  retval:          A persistentJobGUID value

  """
  return "JOB_UUID1-" + str(uuid.uuid1())



def identityConversion(value, _keys):
  return value



def rCopy(d, f=identityConversion, discardNoneKeys=True, deepCopy=True):
  """Recursively copies a dict and returns the result.

  Args:
    d: The dict to copy.
    f: A function to apply to values when copying that takes the value and the
        list of keys from the root of the dict to the value and returns a value
        for the new dict.
    discardNoneKeys: If True, discard key-value pairs when f returns None for
        the value.
    deepCopy: If True, all values in returned dict are true copies (not the
        same object).
  Returns:
    A new dict with keys and values from d replaced with the result of f.
  """
  # Optionally deep copy the dict.
  if deepCopy:
    d = copy.deepcopy(d)

  newDict = {}
  toCopy = [(k, v, newDict, ()) for k, v in d.iteritems()]
  while len(toCopy) > 0:
    k, v, d, prevKeys = toCopy.pop()
    prevKeys = prevKeys + (k,)
    if isinstance(v, dict):
      d[k] = dict()
      toCopy[0:0] = [(innerK, innerV, d[k], prevKeys)
                     for innerK, innerV in v.iteritems()]
    else:
      #print k, v, prevKeys
      newV = f(v, prevKeys)
      if not discardNoneKeys or newV is not None:
        d[k] = newV
  return newDict



def rApply(d, f):
  """Recursively applies f to the values in dict d.

  Args:
    d: The dict to recurse over.
    f: A function to apply to values in d that takes the value and a list of
        keys from the root of the dict to the value.
  """
  remainingDicts = [(d, ())]
  while len(remainingDicts) > 0:
    current, prevKeys = remainingDicts.pop()
    for k, v in current.iteritems():
      keys = prevKeys + (k,)
      if isinstance(v, dict):
        remainingDicts.insert(0, (v, keys))
      else:
        f(v, keys)



def clippedObj(obj, maxElementSize=64):
  """
  Return a clipped version of obj suitable for printing, This
  is useful when generating log messages by printing data structures, but
  don't want the message to be too long.

  If passed in a dict, list, or namedtuple, each element of the structure's
  string representation will be limited to 'maxElementSize' characters. This
  will return a new object where the string representation of each element
  has been truncated to fit within maxElementSize.
  """

  # Is it a named tuple?
  if hasattr(obj, '_asdict'):
    obj = obj._asdict()


  # Printing a dict?
  if isinstance(obj, dict):
    objOut = dict()
    for key,val in obj.iteritems():
      objOut[key] = clippedObj(val)

  # Printing a list?
  elif hasattr(obj, '__iter__'):
    objOut = []
    for val in obj:
      objOut.append(clippedObj(val))

  # Some other object
  else:
    objOut = str(obj)
    if len(objOut) > maxElementSize:
      objOut = objOut[0:maxElementSize] + '...'

  return objOut



class ValidationError(validictory.ValidationError):
  pass



def validate(value, **kwds):
  """ Validate a python value against json schema:
  validate(value, schemaPath)
  validate(value, schemaDict)

  value:          python object to validate against the schema

  The json schema may be specified either as a path of the file containing
  the json schema or as a python dictionary using one of the
  following keywords as arguments:
    schemaPath:     Path of file containing the json schema object.
    schemaDict:     Python dictionary containing the json schema object

  Returns: nothing

  Raises:
          ValidationError when value fails json validation
  """

  assert len(kwds.keys()) >= 1
  assert 'schemaPath' in kwds or 'schemaDict' in kwds

  schemaDict = None
  if 'schemaPath' in kwds:
    schemaPath = kwds.pop('schemaPath')
    schemaDict = loadJsonValueFromFile(schemaPath)
  elif 'schemaDict' in kwds:
    schemaDict = kwds.pop('schemaDict')

  try:
    validictory.validate(value, schemaDict, **kwds)
  except validictory.ValidationError as e:
    raise ValidationError(e)



def loadJsonValueFromFile(inputFilePath):
  """ Loads a json value from a file and converts it to the corresponding python
  object.

  inputFilePath:
                  Path of the json file;

  Returns:
                  python value that represents the loaded json value

  """
  with open(inputFilePath) as fileObj:
    value = json.load(fileObj)

  return value



def sortedJSONDumpS(obj):
  """
  Return a JSON representation of obj with sorted keys on any embedded dicts.
  This insures that the same object will always be represented by the same
  string even if it contains dicts (where the sort order of the keys is
  normally undefined).
  """

  itemStrs = []

  if isinstance(obj, dict):
    items = obj.items()
    items.sort()
    for key, value in items:
      itemStrs.append('%s: %s' % (json.dumps(key), sortedJSONDumpS(value)))
    return '{%s}' % (', '.join(itemStrs))

  elif hasattr(obj, '__iter__'):
    for val in obj:
      itemStrs.append(sortedJSONDumpS(val))
    return '[%s]' % (', '.join(itemStrs))

  else:
    return json.dumps(obj)
