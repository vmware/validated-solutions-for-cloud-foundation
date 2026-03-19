# Copyright 2023-2026 Broadcom. All Rights Reserved.
# SPDX-License-Identifier: BSD-2
#
# Description:
# Runs health checks and retrieves data from the SOS Utility running on SDDC Manager.


import os
import subprocess
import shutil


class PSUtility:

    def __init__(self, logger=None):
        self.p = None
        self.logger = logger
        if os.name == 'posix':
            pwsh = '/usr/bin/pwsh'
        else:
            pwsh = 'pwsh.exe' if shutil.which('pwsh.exe') else 'powershell.exe'
        self.log_msg(f'Running PowerShell cmdlets using {pwsh}')
        self.cmd_pre = [pwsh, '-command', 'Import-Module', 'PowerValidatedSolutions', ';']

    def log_msg(self, msg):
        msg_arr = msg.split()
        for word in msg.split():
            if word.startswith('-') and 'pass' in word.lower():
                word_index = msg_arr.index(word)
                msg_arr[word_index + 1] = '********'

        msg = ' '.join(msg_arr)
        if self.logger:
            self.logger.info(msg)
        else:
            print(msg)

    def execute_ps_cmd(self, cmd):
        try:
            self.log_msg(cmd)
            full_cmd = self.cmd_pre + cmd.split(' ')
            self.p = subprocess.Popen(full_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = self.p.communicate()
            output = stdout.decode('utf-8')
            err = stderr.decode('utf-8')
            if err != '':
                self.log_msg(f'Error encountered while running command - {err}')
            self.log_msg(f'output received = {output}')
            return output
        except Exception as e:
            self.log_msg(e)
            return None
        finally:
            if self.p is not None:
                try:
                    self.p.terminate()
                    self.p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.p.kill()
                except Exception as e:
                    self.log_msg(f'Error occurred while terminating process: {e}')


if __name__ == "__main__":
    psu = PSUtility()
    psu.execute_ps_cmd(
        'Publish-BackupStatus -server sfo-vcf01.sfo.rainpole.io -user <user> -pass <pass> '
        '-allDomains -outputJson .')
