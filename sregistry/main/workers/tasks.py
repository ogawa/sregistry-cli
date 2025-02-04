'''

Copyright (C) 2018-2019 Vanessa Sochat.

This Source Code Form is subject to the terms of the
Mozilla Public License, v. 2.0. If a copy of the MPL was not distributed
with this file, You can obtain one at http://mozilla.org/MPL/2.0/.

'''

from sregistry.defaults import DISABLE_SSL_CHECK
from sregistry.logger import bot

from requests.exceptions import HTTPError
import requests

import json
import os
import re
import shutil
import sys
import tempfile


################################################################################
## Shared Tasks for the Worker
################################################################################

def download_task(url, headers, destination, download_type='layer'):
    '''download an image layer (.tar.gz) to a specified download folder.
       This task is done by using local versions of the same download functions
       that are used for the client.
       core stream/download functions of the parent client.

       Parameters
       ==========
       image_id: the shasum id of the layer, already determined to not exist
       repo_name: the image name (library/ubuntu) to retrieve
       download_folder: download to this folder. If not set, uses temp.
 

    '''
    # Update the user what we are doing
    bot.verbose("Downloading %s from %s" % (download_type, url))

    # Step 1: Download the layer atomically
    file_name = "%s.%s" % (destination,
                           next(tempfile._get_candidate_names()))

    tar_download = download(url, file_name, headers=headers)

    try:
        shutil.move(tar_download, destination)
    except:
        msg = "Cannot untar layer %s," % tar_download
        msg += " was there a problem with download?"
        bot.exit(msg)

    return destination


################################################################################
## Base Functions for Tasks
##
##  These basic tasks are intended for the worker to use, without needing
##  to pickle them for multiprocessing. It works because they don't belong
##  to a client (which we cannot pickle) and are imported by the worker
##  functions directly.
##
################################################################################


def post(url, headers=None, data=None, return_json=True):
    '''post will use requests to get a particular url
    '''
    bot.debug("POST %s" %url)
    return call(url,
                headers=headers,
                func=requests.post,
                data=data,
                return_json=return_json)


def get(url, headers=None, token=None, data=None, return_json=True):
    '''get will use requests to get a particular url
    '''
    bot.debug("GET %s" %url)
    return call(url,
                headers=headers,
                func=requests.get,
                data=data,
                return_json=return_json)
        

def download(url, file_name, headers=None, show_progress=True):
    '''stream to a temporary file, rename on successful completion

        Parameters
        ==========
        file_name: the file name to stream to
        url: the url to stream from
        headers: additional headers to add
    '''

    fd, tmp_file = tempfile.mkstemp(prefix=("%s.tmp." % file_name)) 
    os.close(fd)

    if DISABLE_SSL_CHECK is True:
        bot.warning('Verify of certificates disabled! ::TESTING USE ONLY::')

    verify = not DISABLE_SSL_CHECK

    # Does the url being requested exist?
    if requests.head(url, verify=verify).status_code in [200, 401]:
        response = stream(url, headers=headers, stream_to=tmp_file)

        if isinstance(response, HTTPError):
            bot.exit("Error downloading %s, exiting." %url)

        shutil.move(tmp_file, file_name)
    else:
        bot.error("Invalid url or permissions %s" %url)
    return file_name


def stream(url, headers, stream_to=None, retry=True):
    '''stream is a get that will stream to file_name. Since this is a worker
       task, it differs from the client provided version in that it requires
       headers.
    '''

    bot.debug("GET %s" %url)

    if DISABLE_SSL_CHECK is True:
        bot.warning('Verify of certificates disabled! ::TESTING USE ONLY::')

    # Ensure headers are present, update if not
    response = requests.get(url,         
                            headers=headers,
                            verify=not DISABLE_SSL_CHECK,
                            stream=True)

    # Deal with token if necessary
    if response.status_code == 401 and retry is True:
        headers = update_token(response, headers)
        return stream(url, headers, stream_to, retry=False)

    if response.status_code == 200:

        # Keep user updated with Progress Bar
        content_size = None
        if 'Content-Length' in response.headers:
            progress = 0
            content_size = int(response.headers['Content-Length'])
            bot.show_progress(progress,content_size,length=35)

        chunk_size = 1 << 20
        with open(stream_to,'wb') as filey:
            for chunk in response.iter_content(chunk_size=chunk_size):
                filey.write(chunk)
                if content_size is not None:
                    progress+=chunk_size
                    bot.show_progress(iteration=progress,
                                      total=content_size,
                                      length=35,
                                      carriage_return=False)

        # Newline to finish download
        sys.stdout.write('\n')

        return stream_to 

    bot.exit("Problem with stream, response %s" % response.status_code)



def call(url, func, data=None, headers=None, 
                    return_json=True, stream=False, 
                    retry=True):

    '''call will issue the call, and issue a refresh token
    given a 401 response, and if the client has a _update_token function

    Parameters
    ==========
    func: the function (eg, post, get) to call
    url: the url to send file to
    headers: headers for the request
    data: additional data to add to the request
    return_json: return json if successful
    '''
 
    if DISABLE_SSL_CHECK is True:
        bot.warning('Verify of certificates disabled! ::TESTING USE ONLY::')

    if data is not None:
        if not isinstance(data,dict):
            data = json.dumps(data)

    response = func(url=url,
                    headers=headers,
                    data=data,
                    verify=not DISABLE_SSL_CHECK,
                    stream=stream)

    # Errored response, try again with refresh
    if response.status_code in [500, 502]:
        bot.exit("Beep boop! %s: %s" %(response.reason,
                                       response.status_code))

    # Errored response, try again with refresh
    if response.status_code == 404:
        bot.exit("Beep boop! %s: %s" %(response.reason,
                                       response.status_code))

    # Errored response, try again with refresh
    if response.status_code == 401:

        # If client has method to update token, try it once
        if retry is True:

            # A result of None indicates no update to the call
            headers = update_token(response, headers)
            return call(url, func, data=data,
                        headers=headers,
                        return_json=return_json,
                        stream=stream, retry=False)

        bot.exit("Your credentials are expired! %s: %s" %(response.reason,
                                                          response.status_code))

    elif response.status_code == 200:

        if return_json:

            try:
                response = response.json()
            except ValueError:
                bot.exit("The server returned a malformed response.")

    return response


def update_token(response, headers):
    '''update_token uses HTTP basic authentication to attempt to authenticate
    given a 401 response. We take as input previous headers, and update 
    them.

    Parameters
    ==========
    response: the http request response to parse for the challenge.
    
    '''

    not_asking_auth = "Www-Authenticate" not in response.headers
    if response.status_code != 401 or not_asking_auth:
        bot.exit("Authentication error, exiting.")

    challenge = response.headers["Www-Authenticate"]
    regexp = '^Bearer\s+realm="(.+)",service="(.+)",scope="(.+)",?'
    match = re.match(regexp, challenge)

    if not match:
        bot.exit("Unrecognized authentication challenge, exiting.")

    realm = match.group(1)
    service = match.group(2)
    scope = match.group(3).split(',')[0]

    token_url = realm + '?service=' + service + '&expires_in=900&scope=' + scope

    response = get(token_url)

    try:
        token = response["token"]
        token = {"Authorization": "Bearer %s" % token}
        headers.update(token)

    except:
        bot.exit("Error getting token.")

    return headers
