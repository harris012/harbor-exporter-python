#!/usr/bin/env python3
# Create metrics for generic metrics based on k8s apiserver

import kubernetes as k8s
from prometheus_client import start_http_server, Gauge, Counter
import urllib.request
import urllib.parse
import time
import json
import logging
import os
from image_metrics import get_image_metrics, IMAGE_MISSING, \
    IMAGE_LAST_MODIFIED_TIMESTAMP, IMAGE_AGE_RETRIEVAL_ERROR
from cachetools.func import ttl_cache
from collections import defaultdict

EXPORTER_ERROR = "generic_exporter_errors_total"
CALICO_IPIP_TUNNEL_HANDLES = "calico_ipip_tunnel_handles_total"
CALICO_IPAM_BLOCKS = "calico_ipam_blocks_total"
CALICO_NODE_IPAM_BLOCK_ROUTES = "calico_node_ipam_block_routes_total"
MISSING_DEPLOYMENT = "missing_deployment"
METRIC_DETAILS = "tenant", "namespace", "registry", "project", "image", "tag",
                  "vulnerabilities", "critical", "high", "fixable", "total",
                  "internal"
def get_head_commit_ids(gitlab_url, project, branch):
    project = urllib.parse.quote_plus(project)
    branch = urllib.parse.quote_plus(branch)
    url = "{}/api/v4/projects/{}/repository/branches/{}".format(
        gitlab_url, project, branch)
    with urllib.request.urlopen(url) as req:
        data = json.loads(req.read().decode("UTF-8"))
    return data["commit"]["id"]


@ttl_cache(maxsize=4, ttl=600)
def get_namespace_deployment_metrics(gitlab_url):
    repo_commit_id = get_head_commit_ids(gitlab_url,
                                         "acc/kubernetes-namespaces", "master")
    meta_cm = client.read_namespaced_config_map(namespace="kube-system",
                                                name="gitref-app-namespaces")
    deployed_commit_id = meta_cm.data["commitId"]
    log.debug("Deployed %s, Gitlab %s", deployed_commit_id, repo_commit_id)

    metrics = dict()
    metric_id = (MISSING_DEPLOYMENT, ("acc/kubernetes-namespaces", ))
    metrics[metric_id] = 0 if deployed_commit_id == repo_commit_id else 1
    return metrics


@ttl_cache(maxsize=4, ttl=1200)
def get_addon_deployment_metrics(gitlab_url):
    meta_cm = client.read_namespaced_config_map(namespace="kube-system",
                                                name="gitref-addons")
    # only alert in sandbox
    deploy_branch = meta_cm.data["deployBranch"]
    # only handle master branch deployments as staged branches remain
    # undeployed for more than a day
    if deploy_branch != "master":
        return dict()

    repo_commit_id = get_head_commit_ids(gitlab_url, "acc/kubernetes-cluster",
                                         deploy_branch)

    deployed_commit_id = meta_cm.data["commitId"]
    log.debug("Deployed %s, Gitlab %s", deployed_commit_id, repo_commit_id)

    metrics = dict()
    metric_id = (MISSING_DEPLOYMENT, ("acc/kubernetes-cluster", ))
    metrics[metric_id] = 0 if deployed_commit_id == repo_commit_id else 1
    return metrics


@ttl_cache(maxsize=4, ttl=60)
def get_calico_metrics():
    metrics = defaultdict(float)
    ipamhandles = custom_client.list_cluster_custom_object(
        group="crd.projectcalico.org", version="v1",
        plural="ipamhandles")["items"]
    n_tunnels = len([
        handle for handle in ipamhandles
        if handle["metadata"]["name"].startswith("ipip-tunnel-addr")
    ])

    metric_id = (CALICO_IPIP_TUNNEL_HANDLES, None)
    metrics[metric_id] = n_tunnels

    ipamblocks = custom_client.list_cluster_custom_object(
        group="crd.projectcalico.org", version="v1",
        plural="ipamblocks")["items"]
    nodes = client.list_node().items

    for block in ipamblocks:
        # host:kmaster-fe-sandbox-iz1-bs003
        ipam_node = block["spec"]["affinity"].split(":")[1]
        metric_id = (CALICO_IPAM_BLOCKS, (ipam_node, ))
        metrics[metric_id] += 1

        for node in nodes:
            # blocks on the same node do not need routes
            if node.metadata.name == ipam_node:
                continue
            metric_id = (CALICO_NODE_IPAM_BLOCK_ROUTES, (node.metadata.name, ))
            metrics[metric_id] += 1

    return metrics


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Collect generic metrics', )
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
                        default="registry-lab.1und1.cloud")
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
        CALICO_IPIP_TUNNEL_HANDLES:
        Gauge(CALICO_IPIP_TUNNEL_HANDLES, '', []),
        CALICO_IPAM_BLOCKS:
        Gauge(CALICO_IPAM_BLOCKS, '', ["node"]),
        CALICO_NODE_IPAM_BLOCK_ROUTES:
        Gauge(CALICO_NODE_IPAM_BLOCK_ROUTES,
              'Number of ipam blocks for which routes must exist', ["node"]),
        MISSING_DEPLOYMENT:
        Gauge(MISSING_DEPLOYMENT,
              'Deployed commit id matches repository commit id',
              ["repository"]),
        IMAGE_LAST_MODIFIED_TIMESTAMP:
        Gauge(IMAGE_LAST_MODIFIED_TIMESTAMP,
              'Timestamp of last modification of a container image', METRIC_DETAILS),
        IMAGE_MISSING:
        Gauge(IMAGE_MISSING, 'Container image is missing from registry', METRIC_DETAILS),
        IMAGE_AGE_RETRIEVAL_ERROR:
        Gauge(IMAGE_AGE_RETRIEVAL_ERROR,
              'Container image age could not be discovered', METRIC_DETAILS),
    }

    while True:
        metrics = dict()
        try:
            metrics.update(get_calico_metrics())
        except Exception:
            log.exception("calico metric fetch failed")
            metric_objects[EXPORTER_ERROR].labels("calico_metrics").inc()

        try:
            metrics.update(
                get_namespace_deployment_metrics(gitlab_url=args.gitlab_url))
        except Exception:
            log.exception("namespace deployment metric fetch failed")
            metric_objects[EXPORTER_ERROR].labels("namespace_deployment").inc()

        try:
            metrics.update(
                get_addon_deployment_metrics(gitlab_url=args.gitlab_url))
        except Exception:
            log.exception("addon deployment metric fetch failed")
            metric_objects[EXPORTER_ERROR].labels("addon_deployment").inc()

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
