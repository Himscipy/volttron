# -*- coding: utf-8 -*- {{{
# vim: set fenc=utf-8 ft=python sw=4 ts=4 sts=4 et:
#
# Copyright (c) 2017, Battelle Memorial Institute
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation are
# those of the authors and should not be interpreted as representing official,
# policies either expressed or implied, of the FreeBSD Project.
#

# This material was prepared as an account of work sponsored by an
# agency of the United States Government.  Neither the United States
# Government nor the United States Department of Energy, nor Battelle,
# nor any of their employees, nor any jurisdiction or organization
# that has cooperated in the development of these materials, makes
# any warranty, express or implied, or assumes any legal liability
# or responsibility for the accuracy, completeness, or usefulness or
# any information, apparatus, product, software, or process disclosed,
# or represents that its use would not infringe privately owned rights.
#
# Reference herein to any specific commercial product, process, or
# service by trade name, trademark, manufacturer, or otherwise does
# not necessarily constitute or imply its endorsement, recommendation,
# r favoring by the United States Government or any agency thereof,
# or Battelle Memorial Institute. The views and opinions of authors
# expressed herein do not necessarily state or reflect those of the
# United States Government or any agency thereof.
#
# PACIFIC NORTHWEST NATIONAL LABORATORY
# operated by BATTELLE for the UNITED STATES DEPARTMENT OF ENERGY
# under Contract DE-AC05-76RL01830

# }}}
import logging
import os
import sys
import math
import requests
import sqlite3
import datetime
import threading
from functools import wraps
from abc import abstractmethod
import gevent
from gevent import get_hub
from volttron.platform.agent import utils
from volttron.platform.vip.agent import *
from volttron.platform.messaging.health import (STATUS_BAD,
                                                STATUS_UNKNOWN,
                                                STATUS_GOOD,
                                                STATUS_STARTING,
                                                Status)

_log = logging.getLogger(__name__)

class BaseWeatherAgent(Agent):
    """Creates weather services based on the json objects from the config,
    uses the services to collect and publish weather data"""

    def __init__(self,
                 service=None,
                 max_size_gb=None,
                 log_sql=False,
                 **kwargs):

        super(BaseWeatherAgent, self).__init__(**kwargs)
        self._service = service
        self._max_size_gb = max_size_gb
        self._log_sql = log_sql
        self._default_config = {
                                "max_size_gb": self._max_size_gb,
                                "log_sql": self._log_sql
                               }

        # TODO create default mapping dictionary with values set to None

        self.vip.config.set_default("config", self._default_config)
        self.vip.config.subscribe(self._configure, actions=["NEW", "UPDATE"], pattern="config")
        self.update_intervals = {}

    def update_default_config(self, config):
        """
        May be called by historians to add to the default configuration for its
        own use.
        """
        self._default_config.update(config)
        self.vip.config.set_default("config", self._default_config)

    # TODO check with Kyle
    def start_process_thread(self):
        if self._process_loop_in_greenlet:
            self._process_thread = self.core.spawn(self._process_loop)
            self._process_thread.start()
            _log.debug("Process greenlet started.")
        else:
            self._process_thread = threading.Thread(target=self._process_loop)
            self._process_thread.daemon = True  # Don't wait on thread to exit.
            self._process_thread.start()
            _log.debug("Process thread started.")

    def manage_cache_size(self):
        """
        Removes data from the weather cache until the cache is a safe size. prioritizes removal from current, then
        forecast, then historical request types
        """
        cursor = self.cache._sqlite_conn.cursor()
        if self._max_size_gb is not None:
            def page_count():
                cursor.execute("PRAGMA page_count")
                return cursor.fetchone()[0]
            row_counts = {}
            for table in self.cache.tables:
                query = "SELECT COUNT(*) FROM {}".format(self.cache.tables[table][0])
                row_counts[table] = (int(cursor.execute(query).fetchone()[0]), self.cache.tables[table][1])
            priority = 1
            while page_count() > self.cache.max_pages:
                for table in row_counts:
                    if priority ==1:
                        # Remove all but the most recent 'current' records
                        if row_counts[table][1] == "current" and row_counts[table][0] > 1:
                            query = "SELECT MAX(DATA_TIME) FROM {}".format(table)
                            most_recent = cursor.execute(query).fetchone()[0]
                            query = "DELETE FROM {} WHERE DATA_TIME < {}".format(table, most_recent)
                            cursor.execute(query)
                            self.cache._sqlite_conn.commit()
                    elif priority == 2:
                        # Remove all but the most recent 'forecast' records
                        if row_counts[table][1] == "forecast" and row_counts[table][0] > 1:
                            query = "SELECT MAX(DATA_TIME) FROM {}".format(table)
                            most_recent = cursor.execute(query).fetchone()[0]
                            query = "DELETE FROM {} WHERE DATA_TIME < {}".format(table, most_recent)
                            cursor.execute(query)
                    elif priority == 3:
                        # Remove historical records in batches of 100 until the table is of appropriate size
                        if row_counts[table][1] == "historical" and row_counts[table][0] >= 1:
                            query = "DELETE FROM {} ORDER BY REQUEST_TIME ASC LIMIT 100".format(table)
                            cursor.execute(query)
                    self.cache._sqlite_conn.commit()
                    priority += 1

    # TODO ask Kyle
    def stop_process_thread(self):
        _log.debug("Stopping the process loop.")
        if self._process_thread is None:
            return

        # Tell the loop it needs to die.
        self._stop_process_loop = True
        # Wake the loop.
        self._event_queue.put(None)

        # 9 seconds as configuration timeout is 10 seconds.
        self._process_thread.join(9.0)
        # Greenlets have slightly different API than threads in this case.
        if self._process_loop_in_greenlet:
            if not self._process_thread.ready():
                _log.error("Failed to stop process greenlet during reconfiguration!")
        elif self._process_thread.is_alive():
            _log.error("Failed to stop process thread during reconfiguration!")

        self._process_thread = None
        _log.debug("Process loop stopped.")

    # TODO take a look at basehistorian
    # TODO query forecast and current to track the last report time
    def _configure(self, contents):
        self.vip.heartbeat.start()
        _log.info("Configuring weather agent.")
        config = self._default_config.copy()
        config.update(contents)
        # TODO fill in inits
        self.cache = WeatherCache()

        try:
            # TODO reset defaults from configuration
        except ValueError as err:
            _log.error("Failed to load base weather agent settings. Settings not applied!")
            return

        self.stop_process_thread()
        try:
            self.configure(config)
        except Exception as err:
            _log.error("Failed to load weather agent settings.")
        self.start_process_thread()

    def configure(self, configuration):
        """Optional, may be implemented by a concrete implementation to add support for the configuration store.
        Values should be stored in this function only.

        The process thread is stopped before this is called if it is running. It is started afterwards."""
        pass

    # RPC METHODS which call abstract methods to be used by concrete implementations of the weather agent

    @RPC.export
    def get_current_weather(self, location):
        return self.query_current_weather(location)

    @abstractmethod
    def query_current_weather(self, location):


    @RPC.export
    def get_hourly_forecast(self, location):
        return self.query_hourly_forecast(location)

    @abstractmethod
    def query_hourly_forecast(self):

    @RPC.export
    def get_daily_historical_weather(self, location, start_period, end_period):
        return self.query_daily_historical_weather(location, start_period, end_period)

    @abstractmethod
    def query_hourly_historical_weather(self, location, start_period, end_period):

    @abstractmethod
    def get_location_specification(self):

    @staticmethod
    def _get_status_from_context(context):
        status = STATUS_GOOD
        if (context.get("backlogged") or
                context.get("cache_full") or
                not context.get("publishing")):
            status = STATUS_BAD
        return status

    # TODO
    def _update_status_callback(self, status, context):
        self.vip.health.set_status(status, context)

    def _update_status(self, updates):
        context_copy, new_status = self._update_and_get_context_status(updates)
        self._async_call.send(None, self._update_status_callback, new_status, context_copy)

    def _send_alert_callback(self, status, context, key):
        self.vip.health.set_status(status, context)
        alert_status = Status()
        alert_status.update_status(status, context)
        self.vip.health.send_alert(key, alert_status)

    def _update_and_get_context_status(self, updates):
        self._current_status_context.update(updates)
        context_copy = self._current_status_context.copy()
        new_status = self._get_status_from_context(context_copy)
        return context_copy, new_status

    def _send_alert(self, updates, key):
        context_copy, new_status = self._update_and_get_context_status(updates)
        self._async_call.send(None, self._send_alert_callback, new_status, context_copy, key)

    # TODO check base historian
    def _process_loop(self):
        _log.debug("Starting process loop.")


# TODO logging for sql statements
class WeatherCache:
    """Caches data to help reduce the number of requests to the API"""

    # TODO close the database connection onstop for the weather agent
    def __init__(self,
                 service_name,
                 request_info,
                 max_size_gb=1,
                 log_sql=False,
                 check_same_thread=True):
        """

        :param service_name: Name of the weather service (i.e. weather.gov)
        :param request_info: list of tuples containing (request_name, request_type) where request type is  one of
        ['current', 'forecast', 'historical']
        :param max_size_gb: maximum size in gigaBytes of the sqlite database file, useful for deployments with limited
        storage capacity
        :param log_sql: if True, all sql statements executed will be written to log.info
        """
        # TODO check from base historian
        self.service_name = service_name
        self.db_filepath = self.service_name + ".sqlite"
        self.log_sql = log_sql
        self.tables = {}
        for r_ind in request_info:
            request = request_info[r_ind]
            self.tables[request] = (service_name + "_" + request[0], request[1])
        self._max_size_gb = max_size_gb

        self.setup_cache(check_same_thread)

    def setup_cache(self, check_same_thread):
        """
        prepare the cache to begin processing weather data
        :param check_same_thread:
        """
        try:
            # TODO
            self._sqlite_conn = sqlite3.connect(
                self.db_filepath,
                detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
                check_same_thread=check_same_thread)
            self.current_size = os.path.getsize(self.db_filepath)
            _log.info("connected to database {} sqlite version: {}".format(self.db_name, sqlite3.version))
            cursor = self._sqlite_conn.cursor()
            if self._max_size_gb is not None:
                cursor.execute('''PRAGMA page_size''')
                page_size = cursor.fetchone()[0]
                max_storage_bytes = self._max_size_gb * 1024 ** 3
                self.max_pages = max_storage_bytes / page_size

        except sqlite3.Error as err:
            _log.error("Unable to open the sqlite database for caching: {}".format(err))

        for request in self.tables:
            self.create_table(self.tables[request][0], cursor)

    def table_exists(self, request_name, cursor):
        table_query = "SELECT 1 FROM {} WHERE TYPE = 'table' AND NAME='{}'".format(self.service_name, request_name)
        if self.log_sql:
            _log.info(table_query)
        return bool(cursor.execute(table_query))

    def create_table(self, request_name):
        """Populates the database with the given table, and checks that all of the requisite columns exist
        :param request_name: the name of the request for which we want to store data
        """
        cursor = self._sqlite_conn.cursor()
        if not self.table_exists(request_name, cursor):
            create_table ='''CREATE TABLE {}
                            (LOCATION TEXT NOT NULL,
                             REQUEST_TIME TIMESTAMP NOT NULL,
                             DATA_TIME TIMESTAMP NOT NULL, 
                             JSON_RESPONSE TEXT NOT NULL) 
                             PRIMARY KEY (LOCATION, REQUEST_TIME))'''.format(request_name)
            if self.log_sql:
                _log.info(create_table)
            try:
                cursor.execute(create_table)
                self._sqlite_conn.commit()
            except sqlite3.Error as err:
                _log.error("Unable to create database table: {}".format(err))
        else:
            cursor.execute("pragma table_info({});".format(request_name))
            name_index = 0
            for description in cursor.description:
                if description[0] == "name":
                    break
                name_index += 1
            columns = {"LOCATION": False, "REQUEST_TIME":False, "DATA_TIME":False, "JSON_RESPONSE":False}
            for row in cursor:
                if row[name_index] in columns:
                    columns[row[name_index]] = True
            for column in columns:
                if not columns[column]:
                    _log.error("The Database is missing column {}.".format(columns[column]))

    def get_current_data(self, request_name, location):
        """
        Retrieves the most recent current data by location
        :param request_name:
        :param location:
        :return: a single current weather observation record
        """
        try:
            cursor = self._sqlite_conn.cursor()
            query = "SELECT * FROM (SELECT * FROM {} WHERE LOCATION = {} ORDER BY DATA_TIME DESC) LIMIT 1"\
                .format(request_name, location)
            cursor.execute(query)
            return cursor.fetchone()
        except sqlite3.Error as e:
            _log.error("Error fetching current data from cache: {}".format(e))
            return None;

    def get_forecast_data(self, request_name, location):
        """
        Retrieves the most recent forecast record set (forecast should be a time-series) by location
        :param request_name:
        :param location:
        :return: list of forecast records
        """
        try:
            cursor = self._sqlite_conn.cursor()
            query = "SELECT * FROM {} WHERE REQUEST_TIME = (SELECT MAX(REQUEST_TIME) FROM {} WHERE LOCATION = {})"\
                .format(request_name, request_name, location)
            cursor.execute(query)
            return cursor.fetchall()
        except sqlite3.Error as e:
            _log.error("Error fetching forecast data from cache: {}".format(e))

    def get_historical_data(self, request_type, location, start_timestamp, end_timestamp):
        """
        Retrieves historical data over the the given time period by location
        :param request_type:
        :param location:
        :param start_timestamp:
        :param end_timestamp:
        :return: list of historical records
        """
        try:
            cursor = self._sqlite_conn.cursor()
            query = "SELECT * FROM {} WHERE LOCATION = {} AND DATA_TIME >= {} AND DATA_TIME <= {} ORDER BY DATA_TIME ASC".\
                format(request_type, location, start_timestamp, end_timestamp)
            cursor.execute(query)
            return cursor.fetchall()
        except sqlite3.Error as e:
            _log.error("Error fetching historical data from cache: {}".format(e))

    # TODO try catch for this, make sure records are at least sorta formatted right?
    def store_weather_records(self, request_name, records):
        """
        Request agnostic method to store weather records in the cache.
        :param request_name:
        :param records: expects a list of records formatted to match tables
        """
        cursor = self._sqlite_conn.cursor()
        query = "INSERT INTO {} (LOCATION, REQUEST_TIME, DATA_TIME, JSON_RESPONSE) VALUES (?, ?, ?, ?)"\
            .format(request_name)
        cursor.executemany(query, records)
        self._sqlite_conn.commit()


    def close(self):
        """Close the sqlite database connection when the agent stops"""
        self._sqlite_conn.close()
        self._sqlite_conn = None


# Code reimplemented from https://github.com/gilesbrown/gsqlite3
def _using_threadpool(method):
    @wraps(method, ['__name__', '__doc__'])
    def apply(*args, **kwargs):
        return get_hub().threadpool.apply(method, args, kwargs)
    return apply


# TODO checkout base historian
class AsyncWeatherCache(WeatherCache):
    """Asynchronous weather cache wrapper for use with gevent"""
    def __init__(self, **kwargs):
        kwargs["check_same_thread"] = False
        super(AsyncWeatherCache, self).__init__(**kwargs)

# TODO fill with methods, check base historian
for method in []:
    setattr(AsyncWeatherCache, method.__name__, _using_threadpool(method))

# TODO where did this even come from?
if not property_id == "statusFlags":
                    values = []
                    for tag in element.value.tagList:
                        values.append(tag.app_to_object().value)
                    if len(values) == 1:
                        result_dict[property_id] = values[0]
                    else:
                        result_dict[property_id] = values

class BaseWeather(BaseWeatherAgent):
    def __init__(self, **kwargs):
        _log.debug('Constructor of BaseWeather thread: {}'.format(
            threading.currentThread().getName()
        ))
        super(BaseWeather, self).__init__(**kwargs)