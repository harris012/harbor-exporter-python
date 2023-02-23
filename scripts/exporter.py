#!/usr/bin/env python3
# Create metrics based on k8s apiserver

import kubernetes as k8s
from prometheus_client import start_http_server, Gauge, Counter
import urllib.request
import urllib.parse
import time
import json
import logging
import os
from scripts.docker_image_metrics import get_image_metrics, IMAGE_MISSING, \
    IMAGE_LAST_MODIFIED_TIMESTAMP, IMAGE_AGE_RETRIEVAL_ERROR
from cachetools.func import ttl_cache
from collections import defaultdict

EXPORTER_ERROR = "exporter_errors_total"
METRIC_DETAILS = "tenant", "namespace", "registry", "project", "image", "tag", "internal"



def main():
    import argparse
    parser = argparse.ArgumentParser(description='metrics', )
    parser.add_argument('--in-cluster',
                        const=True,
                        action='store_const',
                        default=False,
                        help='use the in-cluster kube config')
    parser.add_argument('--gitlab-url',
                        help='gitlab url',
                        default="http://gitlab")
    parser.add_argument('--port',
                        help='port to export metrics to',
                        default=9100,
                        type=int)
    parser.add_argument('--period',
                        help='metric refresh period',
                        default=30,
                        type=int)
    parser.add_argument('--registry',
                        help='registry url',
                        default="harbor.com")
    args = parser.parse_args()

    if args.in_cluster:
        k8s.config.load_incluster_config()
    else:
        k8s.config.load_kube_config()

    global client, custom_client
    client = k8s.client.CoreV1Api()
    custom_client = k8s.client.CustomObjectsApi()

    global log
    if 'DEBUG' in os.environ:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
    log = logging.getLogger()

    start_http_server(args.port)
    registered = set()
    metric_objects = {
        EXPORTER_ERROR:
        Counter(EXPORTER_ERROR, '', ["collector"]),
        IMAGE_LAST_MODIFIED_TIMESTAMP:
        Gauge(IMAGE_LAST_MODIFIED_TIMESTAMP,
              'Timestamp of last modification of a container image',
              METRIC_DETAILS),
        IMAGE_MISSING:
        Gauge(IMAGE_MISSING, 'Container image is missing from registry',
              METRIC_DETAILS),
        IMAGE_AGE_RETRIEVAL_ERROR:
        Gauge(IMAGE_AGE_RETRIEVAL_ERROR,
              'Container image age could not be discovered', METRIC_DETAILS),
    }

    while True:
        metrics = dict()
        try:
            metrics.update(get_image_metrics(client, registry=args.registry))
        except Exception:
            log.exception("image timestamp metric fetch failed")
            metric_objects[EXPORTER_ERROR].labels("image_timestamp").inc()

        for metric_id, metric in metrics.items():
            name, labels = metric_id
            registered.add(metric_id)
            if labels:
                metric_objects[name].labels(*labels).set(metric)
            else:
                metric_objects[name].set(metric)

        # stop exporting metrices that don't exist anymore
        for removed in registered - set(metrics.keys()):
            name, labels = removed
            registered.discard(removed)
            metric_objects[name].remove(*labels)

        time.sleep(args.period)


if __name__ == "__main__":
    main()
