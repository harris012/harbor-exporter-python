"""
Functions related to obtaining the timestamp of images.
"""

import logging
import urllib.parse
import dateutil.parser
import time
import json
from collections import defaultdict
from cachetools.func import ttl_cache

IMAGE_LAST_MODIFIED_TIMESTAMP = "image_last_modified_timestamp"
IMAGE_AGE_RETRIEVAL_ERROR = "image_age_retrieval_error"
IMAGE_MISSING = "image_missing"


def iter_chunked(function, chunksize=500, **kwargs):
    pods = function(
        limit=chunksize,
        **kwargs,
    )
    yield from pods.items
    while pods.metadata._continue is not None:
        pods = function(
            limit=chunksize,
            _continue=pods.metadata._continue,
            **kwargs,
        )
        yield from pods.items


@ttl_cache(maxsize=1, ttl=3600)
def get_image_metrics(client, registry):
    """Iterate over pods and produce image timestamp metrics.

    Parameters
    ----------
    client : k8s.client
        The client

    Returns
    -------
    collections.defaultdict
        The metrics
    """
    metrics = defaultdict(float)
    start_time = time.time()

    namespace_to_tenant_mapping = get_namespace_to_tenant_mapping(client)

    pod_iterator = iter_chunked(client.list_pod_for_all_namespaces,
                                field_selector="status.phase=Running")

    for pod in pod_iterator:
        namespace = pod.metadata.namespace
        tenant = namespace_to_tenant_mapping.get(namespace, 'system')

        for container in iter_containers(pod):
            registry, project, image, tag, is_image_internal = \
                get_image_details(container.image, registry)

            last_modified_time, vulnerabilities, critical, high, fixable, total = get_last_modified_timestamp(
                project, image, tag, registry)

            logging.getLogger().debug(
                'total details are follows tenant: %s, namespace: %s, registry: %s, project: %s, image_name: %s, tag: %s and time: %s',
                tenant, namespace, registry, project, image, tag,
                last_modified_time)

            if last_modified_time is None:
                metric_id = (IMAGE_MISSING,
                             (tenant, namespace, registry, project, image, tag,
                              vulnerabilities, critical, high, fixable, total,
                              str(is_image_internal).lower()))
                metrics[metric_id] = 1
                continue
            elif last_modified_time == 0:
                metric_id = (IMAGE_AGE_RETRIEVAL_ERROR,
                             (tenant, namespace, registry, project, image, tag,
                              vulnerabilities, critical, high, fixable, total,
                              str(is_image_internal).lower()))
                metrics[metric_id] = 1
                continue

            metric_id = (IMAGE_LAST_MODIFIED_TIMESTAMP,
                         (tenant, namespace, registry, project, image, tag,
                          vulnerabilities, critical, high, fixable, total,
                          str(is_image_internal).lower()))
            metrics[metric_id] = last_modified_time

    logging.getLogger().info("Preparing image metrics took %s",
                             time.time() - start_time)

    return metrics


@ttl_cache(ttl=900)
def get_namespace_to_tenant_mapping(client, default_tenant='system'):
    """Retrieve namespaces and return a mapping of
    namespace to tenant.

    If no tenant or label exists, uses a default tenant name.

    Parameters
    ----------
    client : k8s.client
        The client

    default_tenant : str
        The name to use if no tenant label exists.
        Defaults to 'system'

    Returns
    -------
    dict
        Dictionary with namespace to tenant mapping
    """
    namespaces = client.list_namespace()

    namespace_to_tenant_mapping = {}
    for namespace in namespaces.items:
        name = namespace.metadata.name
        labels = namespace.metadata.labels
        tenant = labels.get('tenant', default_tenant) if labels is not None \
            else default_tenant
        namespace_to_tenant_mapping[name] = tenant
    return namespace_to_tenant_mapping


# GET /projects/{project_name}/repositories/{repository_name}/artifacts
@ttl_cache(ttl=3600)
def get_last_modified_timestamp(project, image, tag, registry):
    """Retrieve image timestamp from harbor registry and return timestamp
    If an HTTP error code (e.g. 404) is returned, return None.

    Parameters
    ----------
    project: str
        The project
    image : str
        The repository in harbor

    tag : str
        The tag

    Returns
    -------
    int
        UNIX timestamp or
        0 if the image exist but date could not be established
    """

    log = logging.getLogger()
    page = 1
    page_size = 100

    # strip project and double-quote, requests library need this
    enc_repo = urllib.parse.quote(urllib.parse.quote('/'.join(
        image.split('/')[1:]),
                                                     safe=''),
                                  safe='')
    path = 'https://%s/api/v2.0/projects/%s/repositories/%s/artifacts/%s?page_size=%s&page=%s&with_scan_overview=true' % (
        registry, project, enc_repo, tag, page_size, page)
    try:
        response = urllib.request.urlopen(path)
    except urllib.error.HTTPError:
        return None
    else:
        data = dict()
        try:
            data = json.loads(response.read().decode("UTF-8"))
        except Exception:
            log.warning("Failed to decode json from registry response: %s",
                        response.read())
        scan_overview = data['scan_overview'][
            'application/vnd.scanner.adapter.vuln.report.harbor+json; version=1.0']
        vulnerabilities = scan_overview['severity']
        if vulnerabilities != "Unknown":
            scan_summary = scan_overview['summary'].get('summary')
            critical = scan_summary.get('Critical')
            high = scan_summary.get('High')
            total = scan_overview['summary'].get('total')
            fixable = scan_overview['summary'].get('fixable')
        else:
            critical = high = fixable = total = None
        return dateutil.parser.parse(data['extra_attrs']['created']).timestamp(
        ), vulnerabilities, critical, high, fixable, total


def iter_containers(pod):
    """
    iterate all containers of pod (include init containers) and return
    a generator
    """
    if pod.spec.init_containers:
        yield from pod.spec.init_containers

    yield from pod.spec.containers


def get_image_details(image_path, registry):
    """Parse image path and split it into its components

    Parameters
    ----------
    image_path : str
        The full image path

    Returns
    -------
    str
        Registry name
    str
        Project name
    str
        Image name
    str
        Image tag
    bool
        Flag indicating if image is internal
    """

    if "/" in image_path and "." in image_path.split("/")[0]:
        # registry probably a hostname, strip registry from image path
        registry, image_path = image_path.split("/", 1)

    # take care of situations like haproxy:latest
    if (len(image_path.split('/')) == 1):
        image_path = 'library/' + image_path

    project = image_path.split('/', 1)[0]

    if "@" in image_path:
        # digest format image@checksum:sum
        image, tag = image_path.split("@")
    elif ":" in image_path:
        image, tag = image_path.split(":")
    else:
        # take care of situations like haproxy
        image = image_path
        tag = "latest"

    is_image_internal = image_path.split('/')[0].lower() == 'internal'

    return registry, project, image, tag, is_image_internal
