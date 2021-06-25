import os
import sys
from json import dumps

from httplib2 import Http

import constants


def send_message(payload):
    # Change URL and add security header
    service_base = ''

    url = 'https://' + service_base + '/notification'
    message_headers = {'Content-Type': 'application/json'}

    http_obj = Http()

    resp = http_obj.request(
        uri=url,
        method='POST',
        headers=message_headers,
        body=dumps(payload),
    )[0]

    if resp.status == 200:
        print("Successfully send notification")
    else:
        print("Failed to send notification, status code: " + str(resp.status))
        sys.exit(1)
