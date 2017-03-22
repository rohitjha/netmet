# Copyright 2017: GoDaddy Inc.

import collections
import logging
import threading

import futurist
import monotonic
import requests


LOG = logging.getLogger(__name__)


class Pusher(object):

    def __init__(self, url, period=10, max_count=1000):
        self.url = url
        self.period = period
        self.max_count = max_count
        self.objects = collections.deque()
        self._worker = None

    def _send(self):
        while not self._death.is_set():
            body = []
            count = 0
            while self.objects and count < self.max_count:
                count += 1
                body.append(self.objects.popleft())
            # Try to push data 3 times. Helps to avoid short network outages
            # and netmet server failures.
            for i in xrange(3):
                r = requests.post(self.url, json=body)
                if r.status_code == 201:
                    break

                LOG.warning("Can't push data to netmet server %s (status %s)"
                            % (self.url, r.status_code))
                self._death.wait(1)
            else:
                # if didn't successed put data back, and go through while
                # condition again. This allow to stop execution on SIGINT
                self.objects.extendleft(body)

            if len(self.objects) < self.max_count:
                break

    def _send_on_size_or_timeout(self):
        while not self._death.is_set():
            try:
                if monotonic.monotonic() - self._started_at > self.period:
                    self._send()
                    self._started_at = monotonic.monotonic()

                elif len(self.objects) > self.max_count:
                    self._send()

                self._death.wait(self.period / 20.0)
            except Exception:
                LOG.exception("Pusher failed")

    def add(self, item):
        self.objects.append(item)

    def start(self):
        if not self._worker:
            self._started_at = monotonic.monotonic()
            self._worker = futurist.ThreadPoolExecutor()
            self._death = threading.Event()
            self._worker.submit(self._send_on_size_or_timeout)

    def stop(self):
        if self._worker:
            self._death.set()
            self._worker.shutdown()
