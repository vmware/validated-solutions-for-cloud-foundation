# Copyright 2023-2026 Broadcom. All Rights Reserved.
# SPDX-License-Identifier: BSD-2
#
# Description:
# Implements the Logger class. Used for logging output to a file.

import os
import sys
import logging
import traceback
import inspect

from utils.FolderUtility import FolderUtility

"""Singleton class which wraps around python logger to log to file """

class LogUtility(object):
    __test_logger = None  # test logger which is object of this class
    __logger = None  # python logger
    test_log_folder = None

    @staticmethod
    def __get_call_info():
        stack = inspect.stack()

        # stack[1] gives previous function ('info' in this case)
        # stack[2] gives before previous function and so on

        fname = stack[2][1]  # filename
        line_num = stack[2][2]  # line num

        return os.path.basename(fname), line_num

    def __init__(self, level='INFO'):
        if LogUtility.__test_logger:
            raise Exception('This is singleton class. Cannot instantiate again.')
        else:
            self.__logger = self._get_logger(level)
            self.__logger.info("python logger ready")
        LogUtility.__test_logger = self

    def info(self, msg):
        fname, line_num = self.__get_call_info()
        msg = "[{}:{}] - {}".format(fname, line_num, msg)
        self.__logger.info(msg)

    def warn(self, msg):
        fname, line_num = self.__get_call_info()
        msg = "[{}:{}] - {}".format(fname, line_num, msg)
        self.__logger.warning(msg)
        if sys.exc_info()[0] is not None:
            self.__logger.warning(traceback.format_exc())

    def warning(self, msg):
        """Log at WARNING level (alias for warn; matches stdlib logging.Logger.warning)."""
        self.warn(msg)

    def error(self, msg, trace=True):
        fname, line_num = self.__get_call_info()
        msg = "[{}:{}] - {}".format(fname, line_num, msg)
        self.__logger.error(f"Error - {msg}")
        if trace and (traceback.format_exc() != 'NoneType: None\n'):
            self.__logger.error(traceback.format_exc())

    def debug(self, msg, trace=True):
        fname, line_num = self.__get_call_info()
        msg = "[{}:{}] - {}".format(fname, line_num, msg)
        self.__logger.debug(f"Debug - {msg}")
        if trace and (traceback.format_exc() != 'NoneType: None\n'):
            self.__logger.debug(traceback.format_exc())

    @staticmethod
    def get_logger(log_level='INFO'):
        if not LogUtility.__test_logger:
            return LogUtility(level=log_level)
        else:
            return LogUtility.__test_logger

    def _get_logger(self, level='INFO'):
        """Configure and return the default Python logger."""
        accepted_levels = {'INFO': logging.INFO, 'DEBUG': logging.DEBUG}
        if level.upper() not in accepted_levels:
            raise Exception('INVALID LOG LEVEL SPECIFIED')
        logging_format = '[%(asctime)s] %(levelname)s - %(message)s'
        date_format = '%Y-%m-%d %H:%M:%S'
        test_log_directory = FolderUtility.get_log_directory()
        FolderUtility.make_directory(test_log_directory)
        tmp_log_file_folder = os.path.join(test_log_directory, "send-data_")
        self.test_log_folder = FolderUtility.make_directory_with_timestamp(tmp_log_file_folder)
        log_file_path = os.path.join(self.test_log_folder, "send-data-to-vrops.log")
        logging.basicConfig(level=logging.NOTSET, format=logging_format, datefmt=date_format)
        formatter = logging.Formatter(fmt=logging_format, datefmt=date_format)
        handler = logging.FileHandler(log_file_path)
        handler.setLevel(accepted_levels[level])
        handler.setFormatter(formatter)
        logger = logging.getLogger("HRM")
        logger.addHandler(handler)
        return logger
