#!/usr/bin/env python2

"""
Archivematica Client (Gearman Worker)

This executable does the following.

1. Loads tasks from config. Loads a list of performable tasks (client scripts)
   from a config file (typically that in lib/archivematicaClientModules) and
   creates a mapping from names of those tasks (e.g., 'normalize_v1.0') to the
   names of the Python modules that handle them.

2. Registers tasks with Gearman. Creates a Gearman worker and registers the
   loaded tasks with the Gearman server, effectively saying "Hey, I can
   normalize files", etc.

When the MCPServer requests that the MCPClient perform a registered task, the
MCPClient thread calls ``execute_command``, passing it the Gearman job.  This
gets turned into one or more Job objects, each corresponding to a task on the
MCP Server side.  These jobs are sent in batches to the `call()` function of the
Python module configured to handle the registered task type.

The Python modules doing the work receive the list of jobs and set an exit code,
standard out and standard error for each job.  Only one set of jobs executes at
a time, so each Python module is free to assume it has the whole machine at its
disposal, giving it the option of running subprocesses or multiple threads if
desired.

When a set of jobs is complete, the standard output and error of each is written
back to the database.  The exit code of each job is returned to Gearman and
communicated back to the MCP Server (where it is ultimately used to decide which
task to run next).
"""

# This file is part of Archivematica.
#
# Copyright 2010-2017 Artefactual Systems Inc. <http://artefactual.com>
#
# Archivematica is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Archivematica is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Archivematica.  If not, see <http://www.gnu.org/licenses/>.

# @package Archivematica
# @subpackage archivematicaClient
# @author Joseph Perry <joseph@artefactual.com>

import ConfigParser
import cPickle
import logging
import os
from socket import gethostname
import time

import django
django.setup()
from django.conf import settings as django_settings
import gearman

from main.models import Task
from databaseFunctions import getUTCDate, retryOnFailure

from django.db import transaction
import shlex
import importlib

import fork_runner
from job import Job

logger = logging.getLogger('archivematica.mcp.client')

replacement_dict = {
    '%sharedPath%': django_settings.SHARED_DIRECTORY,
    '%clientScriptsDirectory%': django_settings.CLIENT_SCRIPTS_DIRECTORY,
    '%clientAssetsDirectory%': django_settings.CLIENT_ASSETS_DIRECTORY,
}

# Map the name of the client script (that comes in on a Gearman job) to the
# module that will handle it.
supported_modules = {}


def load_supported_modules(file):
    """Populate the global `supported_modules` dict by parsing the MCPClient
    modules config file (typically MCPClient/lib/archivematicaClientModules).
    """
    supported_modules_config = ConfigParser.RawConfigParser()
    supported_modules_config.read(file)
    for client_script, module_name in supported_modules_config.items('supportedBatchCommands'):
        supported_modules[client_script] = module_name


def handle_batch_task(gearman_job):
    module_name = supported_modules.get(gearman_job.task)
    gearman_data = cPickle.loads(gearman_job.data)

    utc_date = getUTCDate()
    jobs = []
    for task_uuid in gearman_data['tasks']:
        task_data = gearman_data['tasks'][task_uuid]
        arguments = task_data['arguments']
        if isinstance(arguments, unicode):
            arguments = arguments.encode('utf-8')

        replacements = (replacement_dict.items() +
                        {'%date%': utc_date.isoformat(),
                         '%taskUUID%': task_uuid,
                         '%jobCreatedDate%': task_data['createdDate']}.items())

        for var, val in replacements:
            arguments = arguments.replace(var, val)

        job = Job(gearman_job.task,
                  task_data['uuid'],
                  _parse_command_line(arguments),
                  caller_wants_output=task_data['wants_output'])
        jobs.append(job)

    # Set their start times.  If we collide with the MCP Server inserting new
    # Tasks (which can happen under heavy concurrent load), retry as needed.
    def set_start_times():
        Task.objects.filter(
            taskuuid__in=[item.UUID for item in jobs]).update(starttime=utc_date)

    retryOnFailure("Set task start times", set_start_times)

    module = importlib.import_module("clientScripts." + module_name)

    # Our module can indicate that it should be run concurrently...
    if hasattr(module, 'concurrent_instances'):
        fork_runner.call("clientScripts." + module_name, jobs, task_count=module.concurrent_instances())
    else:
        module.call(jobs)

    return jobs


def _parse_command_line(s):
    return [_shlex_unescape(x) for x in shlex.split(s)]


# If we're looking at an escaped backtick, drop the escape
# character.  Shlex doesn't do this but bash unescaping does, and we
# want to remain compatible.
def _shlex_unescape(s):
    return ''.join(c1 for c1, c2 in zip(s, s[1:] + '.') if (c1, c2) != ('\\', '`'))


def fail_all_tasks(gearman_job, reason):
    gearman_data = cPickle.loads(gearman_job.data)

    result = {}

    # Give it a best effort to write out an error for each task.  Obviously if
    # we got to this point because the DB is unavailable this isn't going to
    # work...
    try:
        def fail_all_tasks_callback():
            for task_uuid in gearman_data['tasks']:
                Task.objects.filter(taskuuid=task_uuid).update(stderror=str(reason),
                                                               exitcode=1,
                                                               endtime=getUTCDate())

            retryOnFailure("Fail all tasks", fail_all_tasks_callback)

    except Exception as e:
        logger.exception("Failed to update tasks in DB: %s", e)

    # But we can at least send an exit code back to Gearman
    for task_uuid in gearman_data['tasks']:
        result[task_uuid] = {'exitCode': 1}

    return cPickle.dumps({'task_results': result})


def execute_command(gearman_worker, gearman_job):
    """Execute the command encoded in ``gearman_job`` and return its exit code,
    standard output and standard error as a pickled dict.
    """
    logger.info("\n\n*** RUNNING TASK: %s" % (gearman_job.task))

    try:
        jobs = handle_batch_task(gearman_job)
        results = {}

        def write_task_results_callback():
            with transaction.atomic():
                for job in jobs:
                    logger.info("\n\n*** Completed job: %s" % (job.dump()))

                    kwargs = {
                        'exitcode': job.get_exit_code(),
                        'endtime': getUTCDate(),
                    }
                    if django_settings.CAPTURE_CLIENT_SCRIPT_OUTPUT:
                        kwargs.update({
                            'stdout': job.get_stdout(),
                            'stderror': job.get_stderr(),
                        })
                    Task.objects.filter(taskuuid=job.UUID).update(**kwargs)

                    results[job.UUID] = {'exitCode': job.get_exit_code()}

                    if job.caller_wants_output:
                        # Send back stdout/stderr so it can be written to files.
                        # Most cases don't require this (logging to the database is
                        # enough), but the ones that do are coordinated through the
                        # MCP Server so that multiple MCP Client instances don't try
                        # to write the same file at the same time.
                        results[job.UUID]['stdout'] = job.get_stdout()
                        results[job.UUID]['stderror'] = job.get_stderr()

        retryOnFailure("Write task results", write_task_results_callback)

        return cPickle.dumps({'task_results': results})
    except SystemExit:
        logger.error("IMPORTANT: Task %s attempted to call exit()/quit()/sys.exit(). This module should be fixed!", gearman_job.task)
        return fail_all_tasks(gearman_job, "Module attempted exit")
    except Exception as e:
        logger.exception("Exception while processing task %s: %s", gearman_job.task, e)
        return fail_all_tasks(gearman_job, e)


def start_gearman_worker():
    """Setup a gearman client, for the thread."""
    gm_worker = gearman.GearmanWorker([django_settings.GEARMAN_SERVER])
    host_id = '{}_{}'.format(gethostname(), os.getpid())
    gm_worker.set_client_id(host_id)
    for client_script in supported_modules:
        logger.info('Registering: %s', client_script)
        gm_worker.register_task(client_script, execute_command)
    fail_max_sleep = 30
    fail_sleep = 1
    fail_sleep_incrementor = 2
    while True:
        try:
            gm_worker.work()
        except gearman.errors.ServerUnavailable as inst:
            logger.error('Gearman server is unavailable: %s. Retrying in %d'
                         ' seconds.', inst.args, fail_sleep)
            time.sleep(fail_sleep)
            if fail_sleep < fail_max_sleep:
                fail_sleep += fail_sleep_incrementor
        except Exception as e:
            # Generally execute_command should have caught and dealt with any
            # errors gracefully, but we should never let an exception take down
            # the whole process, so one last catch-all.
            logger.exception('Unexpected error while handling gearman job: %s.'
                             ' Retrying in %d seconds.', e, fail_sleep)
            time.sleep(fail_sleep)
            if fail_sleep < fail_max_sleep:
                fail_sleep += fail_sleep_incrementor


if __name__ == '__main__':
    try:
        load_supported_modules(django_settings.CLIENT_MODULES_FILE)
        start_gearman_worker()
    except (KeyboardInterrupt, SystemExit):
        logger.info('Received keyboard interrupt, quitting.')
