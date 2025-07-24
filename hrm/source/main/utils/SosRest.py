# Copyright 2023-2024 Broadcom. All Rights Reserved.
# SPDX-License-Identifier: BSD-2

# ===================================================================================================================
# Created by: Bhumitra Nagar
# Authors:    Bhumitra Nagar
# ===================================================================================================================
#
# Description:
# Runs health checks on SDDC Manager using SOS Utility and get the data.
# Saves the results as health-results.json in test log directory.

import requests
import time
import os
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from pathlib import Path
import tarfile
import shutil
import glob


class SosRest(object):

    def __init__(self, host, user, password, domain=None, logger=None):
        self.host = host
        self.user = user
        self.password = password
        self.domain = domain
        self.logger = logger
        requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
        self.token = self.get_auth_token()

        if not self.token:
            raise Exception('Unable to get Authorization token from SDDC manager')

    def get_auth_token(self):
        headers = {
            'Content-Type': 'application/json',
        }

        json_data = {
            'username': self.user,
            'password': self.password,
        }

        response = requests.post(f'https://{self.host}/v1/tokens', headers=headers, json=json_data, verify=False)
        token = None
        res = response.json()
        # self.log_msg(res)
        if response.status_code == 200:
            token = res['accessToken']
        return token

    def start_health_checks_op(self, vcf_version=None):

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.token}'
        }

        if self.domain is None:
            domainNameString = "\"\""
            includeAllDomainsString = True
        else:
            domainNameString = self.domain
            includeAllDomainsString = False

        if vcf_version >= '5.2':
            json_data = {
                "healthChecks": {
                    "certificateHealth": True,
                    "computeHealth": True,
                    "connectivityHealth": True,
                    "dnsHealth": True,
                    "generalHealth": True,
                    "hardwareCompatibilityHealth": True,
                    "ntpHealth": True,
                    "passwordHealth": True,
                    "servicesHealth": True,
                    "storageHealth": True,
                    "versionHealth": True
                },
                "options": {
                    "config": {
                        "force": True,
                        "skipKnownHostCheck": True
                    },
                    "include": {
                        "precheckReport": False,
                        "summaryReport": True
                    }
                },
                "scope": {
                    "domains": [{
                        "clusterNames": [""],
                        "domainName": domainNameString
                    }],
                    "includeAllDomains": includeAllDomainsString,
                    "includeFreeHosts": True
                }
            }
        else:
            json_data = {
                "healthChecks": {
                    "certificateHealth": True,
                    "composabilityHealth": True,
                    "computeHealth": True,
                    "connectivityHealth": True,
                    "dnsHealth": True,
                    "generalHealth": True,
                    "hardwareCompatibilityHealth": True,
                    "ntpHealth": True,
                    "passwordHealth": True,
                    "servicesHealth": True,
                    "storageHealth": True,
                    "versionHealth": True
                },
                "options": {
                    "config": {
                        "force": True,
                        "skipKnownHostCheck": True
                    },
                    "include": {
                        "summaryReport": True
                    }
                },
                "scope": {
                    "domains": [{
                        "clusterNames": [""],
                        "domainName": domainNameString
                    }],
                    "includeAllDomains": includeAllDomainsString,
                    "includeFreeHosts": True
                }
            }

        response = requests.post(f'https://{self.host}/v1/system/health-summary', headers=headers, json=json_data,
                                 verify=False)
        id = None
        res = response.json()
        self.log_msg(res)
        self.log_msg(f'status code {response.status_code}')
        if response.status_code == 202:
            id = res['id']
        if response.status_code == 409:
            raise Exception(f'SOS Utility operation in progress - {res}')
        return id

    def log_msg(self, msg):
        if self.logger:
            self.logger.info(msg)
        else:
            print(msg)

    def get_health_checks_status(self, id):
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.token}'
        }

        while True:
            time.sleep(30)
            response = requests.get(f'https://{self.host}/v1/system/health-summary/{id}', headers=headers, verify=False)
            res = response.json()

            self.log_msg(response)
            if res['status'] == 'IN_PROGRESS':
                self.log_msg('Health check bundle creation in progress...')
                self.log_msg('Please wait for it to complete')
                self.log_msg('Polling again in 30 seconds')
                continue
            elif res['status'] == 'COMPLETED_WITH_SUCCESS':
                self.log_msg(f'Bundle created successfully - {res}')
                break
            else:
                self.log_msg(f'Unexpected status code - response is - {res}')
                break
        return res

    def get_health_check_bundle(self, id, path=None):
        headers = {
            'Content-Type': 'application/octet-stream',
            'Authorization': f'Bearer {self.token}'
        }

        response = requests.get(f'https://{self.host}/v1/system/health-summary/{id}/data', headers=headers,
                                verify=False)

        self.logger.info(f'bundle response code - {response.status_code}')

        if path:
            if os.path.exists(path):
                file_path = os.path.join(path, f'health-data-{id}.tar')
            else:
                raise Exception(f'Invalid location - {path} or folder doesnt exist')
        else:
            file_path = f'health-data-{id}.tar'

        with open(file_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=1024):
                if chunk:
                    f.write(chunk)

        if os.path.exists(file_path):
            self.log_msg('Extracting contents of bundle')
            self.get_health_check_json_from_tar(file_path, path)

    def get_health_check_json_from_tar(self, absfname, path=None):
        fname = Path(absfname).name
        foldername = fname.rstrip(".tar")
        dst = path
        if path is None:
            path = os.getcwd()
        path = os.path.join(path, foldername)
        filepath = os.path.join(path, '**')
        filepath = os.path.join(filepath, 'health-results.json')
        tar = tarfile.open(absfname, "r:*")
        tar.extractall(path)
        tar.close()

        for file in glob.glob(filepath, recursive=True):
            if file:
                self.log_msg(f"health-results.json file exists in the path {file}")
                shutil.copy(file, dst)
            else:
                self.log_msg(f"health-results.json file doesn't exist in {fname}")


if __name__ == "__main__":
    host = 'sfo-vcf01.sfo.rainpole.io'
    user = 'svc-hrm-vcf@sfo.rainpole.io'
    password = '********'
    sosrest = SosRest(host=host, user=user, password=password)
    sosrest.get_auth_token()
    time.sleep(5)
    id = sosrest.start_health_checks_op()
    if id:
        sosrest.get_health_checks_status(id)
        sosrest.get_health_check_bundle(id)
