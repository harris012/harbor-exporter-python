#!/usr/bin/env python3
# Create metrics for exporter metrics based on k8s apiserver or prometheus data
# currently exports:
#    calico_ipip_tunnel_handles_total

import kubernetes as k8s
from prometheus_client import start_http_server, Gauge, Counter
import urllib.request
import urllib.parse
import time
import json
import logging
import os
from socket import gethostbyaddr, herror
from image_metrics import get_image_metrics, IMAGE_MISSING, \
    IMAGE_LAST_MODIFIED_TIMESTAMP, IMAGE_AGE_RETRIEVAL_ERROR
from cachetools.func import ttl_cache
from collections import defaultdict

GITLAB_URL = "WILL NOT TELL YOU" # edit me

EXPORTER_ERROR = "exporter_errors_total"
CALICO_IPIP_TUNNEL_HANDLES = "calico_ipip_tunnel_handles_total"
CALICO_IPAM_BLOCKS = "calico_ipam_blocks_total"
CALICO_NODE_IPAM_BLOCK_ROUTES = "calico_node_ipam_block_routes_total"
MISSING_DEPLOYMENT = "missing_deployment"
DUPLICATE_IP = "services_with_duplicate_ips"
MISSING_DNS = "missing_dns"


def get_head_commit_ids(project, branch):
    project = urllib.parse.quote_plus(project)
    branch = urllib.parse.quote_plus(branch)
    url = "{}/projects/{}/repository/branches/{}".format(
        GITLAB_URL, project, branch
    )
    with urllib.request.urlopen(url) as req:
        data = json.loads(req.read().decode("UTF-8"))
    return data["commit"]["id"]


@ttl_cache(maxsize=4, ttl=600)
def get_namespace_deployment_metrics():
    repo_commit_id = get_head_commit_ids("k8s/kubernetes-namespaces", "master")
    meta_cm = client.read_namespaced_config_map(
        namespace="kube-system",
        name="deploy-info-app-namespaces"
    )
    deployed_commit_id = meta_cm.data["commitId"]
    log.debug("Deployed %s, Gitlab %s", deployed_commit_id, repo_commit_id)

    metrics = dict()
    metric_id = (MISSING_DEPLOYMENT, ("k8s/kubernetes-namespaces",))
    metrics[metric_id] = 0 if deployed_commit_id == repo_commit_id else 1
    return metrics


@ttl_cache(maxsize=4, ttl=1200)
def get_addon_deployment_metrics():
    meta_cm = client.read_namespaced_config_map(
        namespace="kube-system",
        name="deploy-info-addons"
    )
    # only alert in sandbox
    deploy_branch = meta_cm.data["deployBranch"]
    # only handle master branch deployments as staged branches remain
    # undeployed for more than a day
    if deploy_branch != "master":
        return dict()

    repo_commit_id = get_head_commit_ids("k8s/kubernetes-cluster",
                                         deploy_branch)

    deployed_commit_id = meta_cm.data["commitId"]
    log.debug("Deployed %s, Gitlab %s", deployed_commit_id, repo_commit_id)

    metrics = dict()
    metric_id = (MISSING_DEPLOYMENT, ("k8s/kubernetes-cluster",))
    metrics[metric_id] = 0 if deployed_commit_id == repo_commit_id else 1
    return metrics


@ttl_cache(maxsize=4, ttl=60)
def get_calico_metrics():
    metrics = defaultdict(float)
    ipamhandles = custom_client.list_cluster_custom_object(
        group="crd.projectcalico.org",
        version="v1", plural="ipamhandles"
    )["items"]
    n_tunnels = len([
        handle for handle in ipamhandles
        if handle["metadata"]["name"].startswith("ipip-tunnel-addr")
    ])

    metric_id = (CALICO_IPIP_TUNNEL_HANDLES, None)
    metrics[metric_id] = n_tunnels

    ipamblocks = custom_client.list_cluster_custom_object(
        group="crd.projectcalico.org",
        version="v1", plural="ipamblocks"
    )["items"]
    nodes = client.list_node().items

    for block in ipamblocks:
        # host:kmaster-fe-sandbox-iz1-bs003
        ipam_node = block["spec"]["affinity"].split(":")[1]
        metric_id = (CALICO_IPAM_BLOCKS, (ipam_node,))
        metrics[metric_id] += 1

        for node in nodes:
            # blocks on the same node do not need routes
            if node.metadata.name == ipam_node:
                continue
            metric_id = (CALICO_NODE_IPAM_BLOCK_ROUTES, (node.metadata.name,))
            metrics[metric_id] += 1

    return metrics


def iterate_service_ips(services):
    """
    yields service, protocol, ip, port for each port in the service and the
    load_balancer_ip
    """
    for service in services.items:

        if service.spec.load_balancer_ip is not None:
            for port_spec in service.spec.ports:
                yield (service, port_spec.protocol.upper(),
                       service.spec.load_balancer_ip, port_spec.port)

        # Get status ip for service
        if (service.status.load_balancer is not None
                and service.status.load_balancer.ingress is not None):
            for ingress in service.status.load_balancer.ingress:
                if (ingress.ip != service.spec.load_balancer_ip and
                        ingress.ip is not None):
                    for port_spec in service.spec.ports:
                        yield (service, port_spec.protocol.upper(), ingress.ip,
                               port_spec.port)


def parse_f5_configmaps(configmaps):
    """ yields decoded json from f5 configmap """
    for configmap in configmaps.items:
        if configmap.data is None:
            continue

        json_data = configmap.data.get('data')

        if json_data is None:
            continue

        yield configmap, json.loads(json_data)


@ttl_cache(maxsize=1, ttl=60)
def get_service_ip_metrics():
    # get metric on service/f5 ip duplicates
    # valid duplicates:
    # different protocol on same ip
    # different ports on same ip (also requires metallb annotation)

    # invalid duplicates:
    # services same protocol, same port, same ip
    # f5 ip same as a service ip
    # same ip:port in multiple f5 configs

    services = client.list_service_for_all_namespaces(watch=False)
    configmaps = client.list_config_map_for_all_namespaces(
        watch=False,
        label_selector='f5type=virtual-server'
    )

    service_ips = set()
    f5_ips = set()
    ip_usage = defaultdict(set)

    # Collect IPs of every service
    for service, protocol, ip, port in iterate_service_ips(services):
        # Add service name to the IP address
        ip_usage[(protocol, ip, port)].add(
            (service.metadata.namespace, service.metadata.name)
        )
        # ip for F5 <-> Service check
        service_ips.add(ip)

    # Collect IPs of every configmap with f5type == 'virtual-server'
    # and IP:Port for every configmap
    for configmap, data in parse_f5_configmaps(configmaps):
        f5_iapp = data \
            .get('virtualServer', {}) \
            .get('frontend', {}) \
            .get('iappVariables', {})
        f5_ip = f5_iapp.get('Virtual_Definition__VS_IP')
        f5_port = f5_iapp.get('Virtual_Definition__VS_Port')
        f5_profile = f5_iapp.get(
            'Virtual_Definition__VS_TCP_Client_Profile', '/Common/tcp'
        )

        if (f5_ip is not None and f5_port is not None):
            ip_usage[(f5_profile, f5_ip, f5_port)].add(
                (configmap.metadata.namespace, configmap.metadata.name)
            )
            # ip for F5 <-> Service check
            f5_ips.add(f5_ip)

    # Add metrics for duplicate IP usage
    metrics = dict()
    for ip_data, service_data in ip_usage.items():
        count = len(service_data)
        if count > 1:
            log.error("Duplicate protocol:ip:port usage of Service or "
                      "F5 configmap in %r", service_data)
            metric_id = (DUPLICATE_IP, (":".join(map(str, ip_data)),))
            metrics[metric_id] = count

    for ip in f5_ips.intersection(service_ips):
        log.error("IP %s used in F5 config and K8S Service", ip)
        metric_id = (DUPLICATE_IP, (ip,))
        metrics[metric_id] = 1

    return metrics


@ttl_cache(maxsize=4, ttl=120)
def get_dns_metrics():
    services = client.list_service_for_all_namespaces(watch=False)
    configmaps = client.list_config_map_for_all_namespaces(
        watch=False,
        label_selector='f5type=virtual-server'
    )

    ips = dict()
    metrics = dict()

    # Collect IPs of every service (status and spec)
    for service, protocol, ip, port in iterate_service_ips(services):
        ips[ip] = ("Service", service.metadata.namespace,
                   service.metadata.name)

    # Collect IP of every configmap with f5type == 'virtual-server'
    for configmap, data in parse_f5_configmaps(configmaps):
        service_ip_f5 = data \
            .get('virtualServer', {}) \
            .get('frontend', {}) \
            .get('iappVariables', {}) \
            .get('Virtual_Definition__VS_IP')

        if service_ip_f5 is not None:
            ips[service_ip_f5] = ("F5", configmap.metadata.namespace,
                                  configmap.metadata.name)

    # Check if DNS entry for all IPs exist and generate metric if not
    for ip, data in ips.items():
        try:
            gethostbyaddr(ip)
        except herror:
            kind, namespace, name = data
            metric_id = (MISSING_DNS, (ip, kind, namespace, name))
            metrics[metric_id] = 1

    return metrics


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description='Collect exporter metrics',
    )
    parser.add_argument('--in-cluster',
                        const=True, action='store_const', default=False,
                        help='use the in-cluster kube config')
    parser.add_argument('--prometheus-url', help='prometheus url',
                        default="http://prometheus")
    parser.add_argument('--port', help='port to export metrics to',
                        default=9100, type=int)
    parser.add_argument('--period', help='metric refresh period',
                        default=30, type=int)
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
        EXPORTER_ERROR: Counter(EXPORTER_ERROR, '', ["collector"]),
        CALICO_IPIP_TUNNEL_HANDLES: Gauge(CALICO_IPIP_TUNNEL_HANDLES, '', []),
        CALICO_IPAM_BLOCKS: Gauge(CALICO_IPAM_BLOCKS, '', ["node"]),
        CALICO_NODE_IPAM_BLOCK_ROUTES:
            Gauge(
                CALICO_NODE_IPAM_BLOCK_ROUTES,
                'Number of ipam blocks for which routes must exist',
                ["node"]
            ),
        MISSING_DEPLOYMENT:
            Gauge(
                MISSING_DEPLOYMENT,
                'Deployed commit id matches repository commit id',
                ["repository"]
            ),
        DUPLICATE_IP:
            Gauge(
                DUPLICATE_IP,
                'Number of services using the duplicate ip',
                ["ip"]
            ),
        MISSING_DNS:
            Gauge(
                MISSING_DNS,
                'Number of missing DNS PTR entries',
                ["ip", "kind", "namespace", "name"]
            ),
        IMAGE_LAST_MODIFIED_TIMESTAMP:
            Gauge(
                IMAGE_LAST_MODIFIED_TIMESTAMP,
                'Timestamp of last modification of a container image',
                ["tenant", "namespace", "registry", "image", "tag",
                 "internal"]
            ),
        IMAGE_MISSING:
            Gauge(
                IMAGE_MISSING,
                'Container image is missing from registry',
                ["tenant", "namespace", "registry", "image", "tag",
                 "internal"]
            ),
        IMAGE_AGE_RETRIEVAL_ERROR:
            Gauge(
                IMAGE_AGE_RETRIEVAL_ERROR,
                'Container image age could not be discovered',
                ["tenant", "namespace", "registry", "image", "tag",
                 "internal"]
            ),
    }

    while True:
        metrics = dict()
        try:
            metrics.update(get_calico_metrics())
        except Exception:
            log.exception("calico metric fetch failed")
            metric_objects[EXPORTER_ERROR].labels("calico_metrics").inc()

        try:
            metrics.update(get_namespace_deployment_metrics())
        except Exception:
            log.exception("namespace deployment metric fetch failed")
            metric_objects[EXPORTER_ERROR].labels("namespace_deployment").inc()

        try:
            metrics.update(get_addon_deployment_metrics())
        except Exception:
            log.exception("addon deployment metric fetch failed")
            metric_objects[EXPORTER_ERROR].labels("addon_deployment").inc()

        try:
            metrics.update(get_service_ip_metrics())
        except Exception:
            log.exception("service ip metric fetch failed")
            metric_objects[EXPORTER_ERROR].labels("service_ip_metrics").inc()

        try:
            metrics.update(get_dns_metrics())
        except Exception:
            log.exception("dns metric fetch failed")
            metric_objects[EXPORTER_ERROR].labels("dns_metrics").inc()

        try:
            metrics.update(get_image_metrics(client))
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
